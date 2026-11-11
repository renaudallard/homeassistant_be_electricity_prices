"""The snapshot codec against rows other versions wrote or will write."""

from __future__ import annotations

import copy
import json
import re
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from custom_components.be_electricity_prices import snapshot_months
from custom_components.be_electricity_prices.providers._rates import (
    DynamicRates,
    EnergyRates,
    FixedRates,
    ImpactRates,
    InjectionRates,
    SpotMonthlyRates,
    TimeOfUseRates,
    VariableRates,
)
from custom_components.be_electricity_prices.providers.base import (
    DsoOverlay,
    SupplierSnapshot,
    TaxOverlay,
)
from custom_components.be_electricity_prices.snapshot_codec import (
    _MAX_PER_KWH,
    _figure_bound,
    _figure_fields,
    _snapshot_from_dict,
    _snapshot_to_dict,
)
from custom_components.be_electricity_prices.snapshot_months import (
    ArchivedCard,
    _archived_card_from_github,
)
from tests import make_snapshot

NOW = datetime(2026, 9, 12, 6, 0, tzinfo=UTC)


def test_a_register_pair_flag_is_written_only_when_set() -> None:
    """The card archive is read by every installed version, and the ones
    from before the flag cannot decode a row that carries it: a row of any
    other supplier keeps the shape it had, and only a card that sets the
    flag carries it, where it round-trips."""
    plain = make_snapshot(injection=InjectionRates(current=0.05))
    row = _snapshot_to_dict(plain, NOW)
    assert "bi_hourly" not in row["injection"]
    assert _snapshot_from_dict(row).injection == plain.injection
    pair = make_snapshot(
        injection=InjectionRates(current=0.05, peak=0.06, offpeak=0.04, bi_hourly=True)
    )
    row = _snapshot_to_dict(pair, NOW)
    assert row["injection"]["bi_hourly"] is True
    assert _snapshot_from_dict(row).injection == pair.injection


def test_a_happy_sunday_band_is_written_only_when_set() -> None:
    """The same rule for SmartFlex's fourth band, on both energy kinds that
    carry it: every other TOU and monthly row keeps the shape it had."""
    from custom_components.be_electricity_prices.providers._rates import (
        SpotMonthlyRates,
        TimeOfUseRates,
    )

    plain = make_snapshot(
        energy=TimeOfUseRates(peak=0.2, transition=0.18, offpeak=0.09)
    )
    row = _snapshot_to_dict(plain, NOW)
    assert not {"sunday", "formula_factor_sunday"} & set(row["energy"])
    assert _snapshot_from_dict(row).energy == plain.energy
    sunday = make_snapshot(
        energy=TimeOfUseRates(
            peak=0.2,
            transition=0.18,
            offpeak=0.09,
            sunday=0.0,
            formula_factor_sunday=0.0,
            formula_base_sunday=0.0,
        )
    )
    row = _snapshot_to_dict(sunday, NOW)
    assert row["energy"]["sunday"] == 0.0
    assert _snapshot_from_dict(row).energy == sunday.energy
    leg = make_snapshot(
        energy=SpotMonthlyRates(
            factor=1.0, base=0.0, factor_sunday=0.0, base_sunday=0.0
        )
    )
    row = _snapshot_to_dict(leg, NOW)
    assert row["energy"]["factor_sunday"] == 0.0
    assert _snapshot_from_dict(row).energy == leg.energy
    assert (
        "factor_sunday"
        not in _snapshot_to_dict(
            make_snapshot(energy=SpotMonthlyRates(factor=1.0, base=0.0)), NOW
        )["energy"]
    )


def test_an_impact_rlp_index_is_written_only_when_set() -> None:
    """ImpactRates gained the RLP pair for Mega's Off-peak Impact index, and a
    version from before the archive learned to drop unknown fields cannot
    decode a row that carries it. Every other Impact card keeps the shape it
    had; the variable and monthly legs, which carried the pair long before,
    keep writing it."""
    flat = ImpactRates(pic=0.30, medium=0.22, eco=0.15)
    plain = make_snapshot(energy=flat)
    row = _snapshot_to_dict(plain, NOW)
    assert not {"rlp_indexed", "rlp_blend"} & set(row["energy"])
    assert _snapshot_from_dict(row).energy == plain.energy
    for indexed in (
        replace(flat, rlp_indexed=True, rlp_blend="columns"),
        replace(flat, rlp_blend="columns"),
    ):
        row = _snapshot_to_dict(make_snapshot(energy=indexed), NOW)
        assert row["energy"]["rlp_blend"] == "columns"
        assert _snapshot_from_dict(row).energy == indexed
    row = _snapshot_to_dict(make_snapshot(energy=VariableRates(current=0.2)), NOW)
    assert row["energy"]["rlp_indexed"] is False
    assert row["energy"]["rlp_blend"] == "distinct"


def test_a_card_vat_basis_is_written_only_when_set() -> None:
    """The same rule for the tax overlay's two VAT fields: a card stating no
    rate and assuming none keeps the row shape every earlier version reads."""
    plain = make_snapshot()
    row = _snapshot_to_dict(plain, NOW)
    assert "card_vat_rate" not in row["taxes"]
    assert "assumed_vat_rate" not in row["taxes"]
    assert _snapshot_from_dict(row).taxes == plain.taxes
    stated = replace(plain, taxes=replace(plain.taxes, card_vat_rate=0.06))
    row = _snapshot_to_dict(stated, NOW)
    assert row["taxes"]["card_vat_rate"] == 0.06
    assert "assumed_vat_rate" not in row["taxes"]
    assert _snapshot_from_dict(row).taxes == stated.taxes
    assumed = replace(plain, taxes=replace(plain.taxes, assumed_vat_rate=0.06))
    assert _snapshot_from_dict(_snapshot_to_dict(assumed, NOW)).taxes == assumed.taxes


def test_the_unread_excise_flag_is_written_only_when_set() -> None:
    """Only a social tariff card built before the law was read carries it, so
    every other row keeps the shape an earlier version wrote: written at its
    default, the archive's daily replay would rewrite every stored row."""
    plain = make_snapshot()
    row = _snapshot_to_dict(plain, NOW)
    assert "protected_excise_unread" not in row["taxes"]
    unread = replace(plain, taxes=replace(plain.taxes, protected_excise_unread=True))
    row = _snapshot_to_dict(unread, NOW)
    assert row["taxes"]["protected_excise_unread"] is True
    assert _snapshot_from_dict(row).taxes == unread.taxes


def test_the_green_cost_in_energy_flag_is_written_only_when_set() -> None:
    """Only a card whose energy row carries the green energy cost sets it, so
    every other row keeps the shape an earlier version wrote."""
    plain = make_snapshot()
    row = _snapshot_to_dict(plain, NOW)
    assert "renewables_in_energy" not in row["taxes"]
    carried = replace(plain, taxes=replace(plain.taxes, renewables_in_energy=True))
    row = _snapshot_to_dict(carried, NOW)
    assert row["taxes"]["renewables_in_energy"] is True
    assert _snapshot_from_dict(row).taxes == carried.taxes


def test_a_field_this_version_does_not_know_is_dropped_not_refused() -> None:
    row = _snapshot_to_dict(make_snapshot(injection=InjectionRates(current=0.05)), NOW)
    row["injection"]["from_the_future"] = True
    assert _snapshot_from_dict(row).injection == InjectionRates(current=0.05)


@pytest.mark.parametrize(
    "energy",
    [
        FixedRates(single=0.20),
        VariableRates(current=0.21),
        DynamicRates(factor=1.0, base=0.02),
        TimeOfUseRates(peak=0.25, offpeak=0.18, transition=0.21),
        ImpactRates(eco=0.15, medium=0.20, pic=0.28),
        SpotMonthlyRates(factor=1.0, base=0.01),
    ],
    ids=["fixed", "variable", "dynamic", "tou", "impact", "spot_monthly"],
)
def test_every_energy_kind_survives_a_field_from_a_later_version(
    energy: EnergyRates,
) -> None:
    """Each of the six energy kinds is rebuilt by its own `_known_fields` call,
    and the test beside this one exercises exactly one of them, because
    `make_snapshot`'s default leg is `FixedRates`.

    Deleting any of the other five calls left that test green: 4 passed with
    the variable, dynamic, TOU, Impact or spot-monthly guard removed. A field
    added to one of those rate classes later would then take the whole row
    down on every older reader, which is the failure this rule exists to stop.
    """
    row = _snapshot_to_dict(make_snapshot(energy=energy), NOW)
    row["energy"]["from_the_future"] = 1
    assert _snapshot_from_dict(row).energy == energy


def test_every_leg_of_a_row_survives_a_field_from_a_later_version() -> None:
    """Every archived row is read by every installed version, so a field added
    later reaches an older reader as an unexpected keyword: TypeError, the card
    dropped, the entry back on the supplier tier, and a debug line as the only
    trace. The failure lands on the users who did not upgrade.

    The injection leg had that rule and it is one of the eight dataclasses a
    row is rebuilt from. Nothing is exposed today; the next field added to an
    overlay or a rate class is.
    """
    row = _snapshot_to_dict(make_snapshot(injection=InjectionRates(current=0.05)), NOW)
    row["energy"]["from_the_future"] = 1
    row["taxes"]["from_the_future"] = 2
    for overlay in row["dsos"].values():
        overlay["from_the_future"] = 3
    row["injection"]["from_the_future"] = 4

    rebuilt = _snapshot_from_dict(row)
    original = make_snapshot(injection=InjectionRates(current=0.05))
    assert rebuilt.energy == original.energy
    assert rebuilt.taxes == original.taxes
    assert rebuilt.dsos == original.dsos
    assert rebuilt.injection == original.injection


def test_a_settled_index_is_written_only_when_the_month_has_one() -> None:
    """Same rule as the register pair, and for the same reason: the card
    archive holds 1.686 rows and 1.683 of them carry an injection leg, so a
    field written at its default on every one of them is 1.683 rewritten rows
    that are not changed cards.

    The test for "no value here" is the dataclass DEFAULT, not falsiness. A
    month whose index settled at exactly 0 EUR/MWh is a settled month, and
    dropping its field would send the engine back to computing a mean for a
    month the supplier has already published.
    """
    plain = make_snapshot(injection=InjectionRates(current=0.05))
    row = _snapshot_to_dict(plain, NOW)
    assert "index_realised" not in row["injection"]
    assert _snapshot_from_dict(row).injection == plain.injection

    for index in (0.07911, 0.0):
        settled = make_snapshot(
            injection=InjectionRates(current=0.05, index_realised=index)
        )
        row = _snapshot_to_dict(settled, NOW)
        assert row["injection"]["index_realised"] == index
        assert _snapshot_from_dict(row).injection == settled.injection


def test_an_impact_leg_keeps_the_index_its_month_settled_at() -> None:
    """Cociter's trihoraire month settles its three bands on the BELIX the
    next card names, and the leg records it the way the variable card's does.
    A row with none leaves the key out, so the Impact rows every installed
    version reads stay byte-identical until their month is settled."""
    printed = ImpactRates(pic=0.19, medium=0.16, eco=0.13, month_indexed=True)
    row = _snapshot_to_dict(make_snapshot(energy=printed), NOW)
    assert "index_realised" not in row["energy"]
    assert _snapshot_from_dict(row).energy == printed

    settled = make_snapshot(energy=replace(printed, index_realised=0.12932))
    row = _snapshot_to_dict(settled, NOW)
    assert row["energy"]["index_realised"] == 0.12932
    assert _snapshot_from_dict(row).energy == settled.energy


def test_a_stated_settlement_is_written_only_where_a_month_has_one() -> None:
    """Mega's settled months bill the figures its next card states, on both
    legs. Every other row leaves the flag out, so the rows every installed
    version reads stay byte-identical, and a flagged row reads back as one."""
    for energy in (
        VariableRates(current=0.19, month_indexed=True),
        ImpactRates(pic=0.19, medium=0.16, eco=0.13, month_indexed=True),
    ):
        plain = make_snapshot(energy=energy, injection=InjectionRates(current=0.05))
        row = _snapshot_to_dict(plain, NOW)
        assert "settled_as_stated" not in row["energy"]
        assert "settled_as_stated" not in row["injection"]
        assert _snapshot_from_dict(row).energy == plain.energy

        settled = make_snapshot(
            energy=replace(energy, settled_as_stated=True, index_realised=0.135),
            injection=InjectionRates(
                current=0.0433, index_realised=0.0791, settled_as_stated=True
            ),
        )
        row = _snapshot_to_dict(settled, NOW)
        assert row["energy"]["settled_as_stated"] is True
        assert row["injection"]["settled_as_stated"] is True
        rebuilt = _snapshot_from_dict(row)
        assert rebuilt.energy == settled.energy
        assert rebuilt.injection == settled.injection


def test_the_spp_redistributed_flag_is_written_only_where_a_card_sets_it() -> None:
    """Bolt's variable cards credit their variable settlement per register;
    every other row leaves the flag out and stays byte-identical, and a
    flagged one reads back as written. The mark of a quarter settled on
    those figures is left out the same way until a month is settled."""
    plain = make_snapshot(injection=InjectionRates(current=0.05))
    assert "spp_redistributed" not in _snapshot_to_dict(plain, NOW)["injection"]
    assert "spp_settled" not in _snapshot_to_dict(plain, NOW)["injection"]
    flagged = make_snapshot(
        injection=InjectionRates(
            current=0.0621,
            peak=0.0795,
            offpeak=0.0182,
            factor=0.94,
            base=-0.01133,
            slot_indexed=True,
            spp_redistributed=True,
        )
    )
    row = _snapshot_to_dict(flagged, NOW)
    assert row["injection"]["spp_redistributed"] is True
    assert "spp_settled" not in row["injection"]
    assert _snapshot_from_dict(row).injection == flagged.injection
    assert flagged.injection is not None
    settled = make_snapshot(injection=replace(flagged.injection, spp_settled=True))
    row = _snapshot_to_dict(settled, NOW)
    assert row["injection"]["spp_settled"] is True
    assert _snapshot_from_dict(row).injection == settled.injection


def test_the_forward_index_flag_is_written_only_where_a_card_sets_it() -> None:
    """Engie's Easy Variable names its forward index on both legs; every other
    row leaves the flag out and stays byte-identical, and a flagged one reads
    back as written."""
    from custom_components.be_electricity_prices.providers._rates import (
        VariableRates,
    )

    plain = make_snapshot(
        energy=VariableRates(current=0.17), injection=InjectionRates(current=0.05)
    )
    row = _snapshot_to_dict(plain, NOW)
    assert "forward_indexed" not in row["energy"]
    assert "forward_indexed" not in row["injection"]
    flagged = make_snapshot(
        energy=VariableRates(
            current=0.17192,
            formula_factor=1.22112,
            formula_base=0.02768,
            forward_indexed=True,
            index_realised=0.11812,
        ),
        injection=InjectionRates(
            current=0.05624, factor=0.548, base=-0.00849, forward_indexed=True
        ),
    )
    row = _snapshot_to_dict(flagged, NOW)
    assert row["energy"]["forward_indexed"] is True
    assert row["injection"]["forward_indexed"] is True
    back = _snapshot_from_dict(row)
    assert back.energy == flagged.energy
    assert back.injection == flagged.injection


def test_the_impact_pair_statement_is_written_only_where_a_card_sets_it() -> None:
    """Eneco's footnote and Engie's Walloon column head set it; every other
    row keeps the shape an earlier version wrote and reads back unflagged, and
    a flagged leg of each kind that carries it reads back as written."""
    legs: tuple[FixedRates | VariableRates | SpotMonthlyRates, ...] = (
        FixedRates(single=0.221, peak=0.251, offpeak=0.1949),
        VariableRates(current=0.2149, peak=0.2395, offpeak=0.17528),
        SpotMonthlyRates(factor=1.24, base=0.02, factor_peak=1.34, factor_offpeak=1.05),
    )
    for leg in legs:
        row = _snapshot_to_dict(make_snapshot(energy=leg), NOW)
        assert "day_night_under_impact" not in row["energy"]
        assert _snapshot_from_dict(row).energy == leg
        flagged = replace(leg, day_night_under_impact=True)
        row = _snapshot_to_dict(make_snapshot(energy=flagged), NOW)
        assert row["energy"]["day_night_under_impact"] is True
        assert _snapshot_from_dict(row).energy == flagged


def test_the_feed_in_impact_statement_is_written_only_where_a_card_sets_it() -> None:
    """Engie's Walloon social card sets it on its feed-in pair; every other
    feed-in row keeps the shape an earlier version wrote and reads back
    unflagged, and a flagged one reads back as written."""
    pair = InjectionRates(
        current=0.09935, peak=0.11687, offpeak=0.04836, bi_hourly=True
    )
    row = _snapshot_to_dict(make_snapshot(injection=pair), NOW)
    assert "day_night_under_impact" not in row["injection"]
    assert _snapshot_from_dict(row).injection == pair
    flagged = replace(pair, day_night_under_impact=True)
    row = _snapshot_to_dict(make_snapshot(injection=flagged), NOW)
    assert row["injection"]["day_night_under_impact"] is True
    assert _snapshot_from_dict(row).injection == flagged


def test_the_codec_writes_every_snapshot_field() -> None:
    """The encoder names the top-level fields by hand, and nothing held the
    list to the dataclass: a field added to SupplierSnapshot without a line
    here is written by no path, read back at its default from every stored
    snapshot, month row and archive row, and the whole suite stayed green.
    Thirteen of the existing ones could be dropped the same way, eleven of
    them the welcome-credit and direct-debit terms a signing cohort reads.
    ``provisional`` is the one left out on purpose: a provisional row is never
    written."""
    from dataclasses import fields

    from custom_components.be_electricity_prices.providers.base import (
        SupplierSnapshot,
    )

    written = set(_snapshot_to_dict(make_snapshot(), NOW)) - {
        "_cached_at",
        "_probe_key",
        "_schema_version",
        "energy_kind",
    }
    assert written == {f.name for f in fields(SupplierSnapshot)} - {"provisional"}


def test_every_snapshot_field_survives_the_round_trip() -> None:
    """Written is half of it; each field also has to be read back. Every one
    is set off its default here, and the table has to name every field the
    dataclass defaults, so a new one fails until it is added."""
    from dataclasses import MISSING, fields, replace
    from datetime import date

    from custom_components.be_electricity_prices.const import (
        WELCOME_CREDIT_ANNIVERSARY,
    )
    from custom_components.be_electricity_prices.providers.base import (
        SupplierSnapshot,
    )

    values: dict[str, object] = {
        "publication_label": "09/2026",
        "injection": InjectionRates(current=0.05),
        "supplier_prosumer_eur_per_kva_year": 37.1,
        "valid_until": date(2026, 9, 30),
        "welcome_credit_eur": 74.2,
        "welcome_credit_kind": WELCOME_CREDIT_ANNIVERSARY,
        "welcome_credit_lump_capped": True,
        "direct_debit_discount_eur": 12.0,
        "welcome_credit_eur_per_kwh": 0.0424,
        "welcome_credit_cap_eur": 848.0,
        "welcome_credit_direct_debit_eur": 42.4,
        "welcome_credit_requires_direct_debit": True,
        "welcome_credit_pct_of_energy": 0.33,
        "welcome_credit_kwh": 750.0,
        "welcome_credit_kwh_rate": 0.207,
        "welcome_credit_excludes_night_meter": True,
        "welcome_credit_after_months": 14,
        "welcome_credit_injection_eur_per_kwh": 0.0106,
        "welcome_credit_signing_month": date(2026, 10, 1),
        "network_year": 2025,
    }
    defaulted = {
        f.name
        for f in fields(SupplierSnapshot)
        if f.default is not MISSING or f.default_factory is not MISSING
    }
    assert set(values) == defaulted - {"provisional"}
    snap = replace(make_snapshot(), **values)  # type: ignore[arg-type]
    back = _snapshot_from_dict(_snapshot_to_dict(snap, NOW))
    for name in values:
        assert getattr(back, name) == getattr(snap, name), name


# A row of the card archive as the repository serves it, for Trevion Groene
# Stroom Flex in Flanders, September 2026.
_ARCHIVED_ROW = json.loads(
    (
        Path(__file__).parent
        / "fixtures"
        / "archive_row_trevion_groene_stroom_flex_flanders_2026_09.json"
    ).read_text(encoding="utf-8")
)


async def _served(row: dict[str, Any]) -> ArchivedCard | None:
    """``row`` served by the card archive, through the reader that bills it."""
    # json.dumps writes a NaN as the literal json.loads reads back.
    body = json.dumps(row)

    async def _body(*_args: object, **_kw: object) -> str:
        return body

    with patch.object(snapshot_months, "fetch_text", _body):
        return await _archived_card_from_github(
            MagicMock(), "trevion", "groene_stroom_flex", "flanders", date(2026, 9, 1)
        )


async def test_an_archive_row_naming_its_network_year_still_reads() -> None:
    """A year is no figure per kWh: a row whose card prints an earlier year's
    network table reads back with it, where the bound on a rate refused it."""
    row = {**_ARCHIVED_ROW, "network_year": 2025}
    card = await _served(row)
    assert card is not None
    assert card.snapshot.network_year == 2025
    assert _figure_bound("network_year") > 2025


async def test_a_real_archive_row_still_reads() -> None:
    card = await _served(_ARCHIVED_ROW)
    assert card is not None
    assert card.snapshot.publication_label == "2026-09"


@pytest.mark.parametrize(
    "value",
    [987.654, float("nan"), -5.0, "0.0159"],
    ids=["absurd", "NaN", "negative", "text"],
)
async def test_an_archive_row_whose_rates_are_not_figures_reads_as_missing(
    value: object,
) -> None:
    """The archive is billed as it reads: a row whose energy leg holds 987,654
    EUR/kWh, NaN or -5 was billed as it stood. Such a row is refused as one
    that does not decode, so the month is asked elsewhere."""
    row = copy.deepcopy(_ARCHIVED_ROW)
    for key, figure in row["energy"].items():
        if isinstance(figure, float):
            row["energy"][key] = value
    assert await _served(row) is None


@pytest.mark.parametrize(
    ("where", "value"),
    [
        (("dsos", "fluvius_antwerpen", "transport"), float("inf")),
        (("dsos", "fluvius_antwerpen", "brussels_osp_by_tier"), {"t1": -1e6}),
        (("taxes", "vat_rate"), 1.5),
        (("taxes", "federal_excise_bands"), [[20000, float("nan")]]),
        (("injection", "factor"), 5000.0),
        (("welcome_credit_eur",), 1e9),
    ],
    ids=["network", "OSP tier", "VAT", "excise band", "feed-in factor", "credit"],
)
async def test_an_archive_row_holding_any_figure_past_its_unit_reads_as_missing(
    where: tuple[str, ...], value: object
) -> None:
    """Every figure a row holds is held to its unit, the network, the taxes,
    the feed-in and the credits as much as the energy leg."""
    row = copy.deepcopy(_ARCHIVED_ROW)
    part = row
    for key in where[:-1]:
        part = part[key]
    part[where[-1]] = value
    assert await _served(row) is None


def test_every_figure_held_as_a_rate_per_kwh_is_named_as_one() -> None:
    """A figure the bounds do not name is held as a rate per kWh, the tightest
    of them, so a field added in another unit and left out of the table would
    refuse every row that sets it. No field so held is named for another
    unit."""
    other_unit = re.compile(
        r"year|month|_eur$|kva|_kw_|vat|pct|share|osp|power_term|factor"
        r"|(?<!per_)kwh(?!_rate)"
    )
    held = [
        (cls.__name__, name)
        for cls in (
            FixedRates,
            VariableRates,
            DynamicRates,
            SpotMonthlyRates,
            TimeOfUseRates,
            ImpactRates,
            InjectionRates,
            DsoOverlay,
            TaxOverlay,
            SupplierSnapshot,
        )
        for name, _optional in _figure_fields(cls)
        if _figure_bound(name) == _MAX_PER_KWH
    ]
    assert held, "no figure is held as a rate per kWh"
    assert not [f"{cls}.{name}" for cls, name in held if other_unit.search(name)]

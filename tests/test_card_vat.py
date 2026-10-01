"""The VAT basis each card records: the rate it states, or the one its parser
grossed a formula by because it states none (``TaxOverlay.card_vat_rate`` and
``assumed_vat_rate``)."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest

from custom_components.be_electricity_prices.const import (
    REGION_BRUSSELS,
    REGION_FLANDERS,
    REGION_WALLONIA,
    VAT_RATE_REDUCED,
)
from custom_components.be_electricity_prices.providers import (
    _ecopower_overlays,
    _energyvision_cards,
    _energyvision_wallonia,
    _engie_cards,
    _luminus_cards,
    _mega_cards,
    _totalenergies_cards,
    bolt,
    cociter,
    ebem,
    ecofix,
    ecopower,
    eneco,
    energiebe,
    energyknights,
    energyvision,
    engie,
    frank,
    luminus,
    mega,
    octaplus,
    totalenergies,
    trevion,
)
from custom_components.be_electricity_prices.providers._pdf import (
    printed_vat_rate,
    vat_multiplier,
)
from custom_components.be_electricity_prices.providers._rates import (
    DynamicRates,
    FixedRates,
    SpotMonthlyRates,
    vat_basis,
    vat_grossed_fields,
)
from custom_components.be_electricity_prices.providers.base import SupplierSnapshot
from tests import fixture_text


def _layout(name: str) -> str:
    return fixture_text(name, layout=True)


def _aligned(name: str) -> str:
    return fixture_text(name, aligned=True)


@dataclass(frozen=True)
class _Card:
    """One real card: how it is read, parsed, and which patterns state its rate."""

    fixture: str
    render: Callable[[str], str]
    parse: Callable[[str], SupplierSnapshot]
    patterns: tuple[re.Pattern[str], ...]
    # Whether the parser grosses its formula by the rate those patterns read.
    # Frank and Eneco Dynamic gross by the multiplier their formula prints
    # beside it instead, and Mega Dynamic prints its formula VAT-inclusive.
    grossed_by_stated_rate: bool = True
    # Engie binds each formula to the price printed beside it at the stated
    # rate, so a card restated to 7% with its prices still at 6% drops the
    # formula, as it should: only the rate read is checked there.
    cross_checked: bool = False


_PRINTING = {
    "cociter": _Card(
        "cociter_var_2604.pdf",
        fixture_text,
        lambda t: cociter.parse_snapshot(t, "cociter_variable", "t://", "2026-04"),
        (cociter._VAT_RE,),
    ),
    "ebem": _Card(
        "ebem_variable_2026-05.pdf",
        _layout,
        lambda t: ebem.parse_snapshot("ebem_variable", t, "t://", "2026-05"),
        ebem._VAT_PATTERNS,
    ),
    "ecofix": _Card(
        "ecofix_motion_online.pdf",
        _layout,
        lambda t: ecofix.parse_snapshot(
            "ecofix_motion_online", t, REGION_FLANDERS, "t://"
        ),
        (ecofix._VAT_RE,),
    ),
    "eneco": _Card(
        "eneco_flex_aug26.pdf",
        fixture_text,
        lambda t: eneco.parse_snapshot(t, "power_flex", "t://", REGION_FLANDERS),
        (eneco._VAT_RE,),
    ),
    "energyknights": _Card(
        "energyknights_agilior_aug.pdf",
        _layout,
        lambda t: energyknights.parse_snapshot("energyknights_agilior", t, "t://"),
        (energyknights._VAT_RE,),
    ),
    "energyvision": _Card(
        "energyvision_dynamic_jul.pdf",
        _layout,
        lambda t: energyvision.parse_snapshot("energyvision_dynamic", t, "t://"),
        (_energyvision_cards._VAT_RE,),
    ),
    "energyvision_wallonia": _Card(
        "energyvision_tiered_1800_wal_sep.pdf",
        _layout,
        lambda t: energyvision.parse_snapshot(
            "energyvision_tiered_1800", t, "t://", region=REGION_WALLONIA
        ),
        (_energyvision_wallonia._VAT_FR_RE,),
    ),
    "engie": _Card(
        "engie_dynamic_w.pdf",
        fixture_text,
        lambda t: engie.parse_snapshot("engie_dynamic", {REGION_WALLONIA: t}),
        (_engie_cards._VAT_RE,),
    ),
    "frank": _Card(
        "frank_dynamic_apr.pdf",
        _layout,
        lambda t: frank.parse_snapshot(t, "t://", "frank_dynamic", "april 2026"),
        (frank._VAT_RE,),
        grossed_by_stated_rate=False,
    ),
    "luminus": _Card(
        "luminus_dynamic_w.pdf",
        fixture_text,
        lambda t: luminus.parse_snapshot("luminus_dynamic", t, REGION_WALLONIA),
        _luminus_cards._VAT_PATTERNS,
    ),
    "mega": _Card(
        "mega_smart_flex_w.pdf",
        fixture_text,
        lambda t: mega.parse_snapshot("mega_smart_flex", t, REGION_WALLONIA),
        _mega_cards._VAT_PATTERNS,
    ),
    "octaplus": _Card(
        "octaplus_dynamic_v_jan.pdf",
        _aligned,
        lambda t: octaplus.parse_snapshot("octaplus_dynamic", t, REGION_FLANDERS),
        (octaplus._VAT_RE,),
    ),
    "cociter_dynamic": _Card(
        "cociter_dyn_2604.pdf",
        fixture_text,
        lambda t: cociter.parse_snapshot(t, "cociter_dynamic", "t://", "2026-04"),
        (cociter._VAT_RE,),
    ),
    "ebem_dynamic": _Card(
        "ebem_dynamic_2026-05.pdf",
        _layout,
        lambda t: ebem.parse_snapshot("ebem_dynamic", t, "t://", "2026-05"),
        ebem._VAT_PATTERNS,
    ),
    "ecofix_flexy": _Card(
        "ecofix_flexy.pdf",
        _layout,
        lambda t: ecofix.parse_snapshot("ecofix_flexy", t, REGION_FLANDERS, "t://"),
        (ecofix._VAT_RE,),
    ),
    "eneco_dynamic": _Card(
        "eneco_dyn.pdf",
        fixture_text,
        lambda t: eneco.parse_snapshot(t, "power_dynamic", "t://", REGION_FLANDERS),
        (eneco._VAT_RE,),
        grossed_by_stated_rate=False,
    ),
    "energyknights_agilis": _Card(
        "energyknights_agilis_aug.pdf",
        _layout,
        lambda t: energyknights.parse_snapshot("energyknights_agilis", t, "t://"),
        (energyknights._VAT_RE,),
    ),
    "energyknights_essentia": _Card(
        "energyknights_essentia_aug.pdf",
        _layout,
        lambda t: energyknights.parse_snapshot("energyknights_essentia", t, "t://"),
        (energyknights._VAT_RE,),
    ),
    "energyvision_tiered": _Card(
        "energyvision_tiered_1800_sep.pdf",
        _layout,
        lambda t: energyvision.parse_snapshot(
            "energyvision_tiered_1800", t, "t://", region=REGION_FLANDERS
        ),
        (_energyvision_cards._VAT_RE,),
    ),
    "engie_flextime": _Card(
        "engie_empower_flextime_w.pdf",
        fixture_text,
        lambda t: engie.parse_snapshot("engie_empower_flextime", {REGION_WALLONIA: t}),
        (_engie_cards._VAT_RE,),
        cross_checked=True,
    ),
    "engie_variable": _Card(
        "engie_empower_variable_v.pdf",
        fixture_text,
        lambda t: engie.parse_snapshot("engie_empower_variable", {REGION_FLANDERS: t}),
        (_engie_cards._VAT_RE,),
        cross_checked=True,
    ),
    "luminus_comfyflex": _Card(
        "luminus_comfyflex_v.pdf",
        fixture_text,
        lambda t: luminus.parse_snapshot("luminus_comfyflex", t, REGION_FLANDERS),
        _luminus_cards._VAT_PATTERNS,
    ),
    "luminus_smartflex": _Card(
        "luminus_smartflex_w.pdf",
        fixture_text,
        lambda t: luminus.parse_snapshot("luminus_smartflex", t, REGION_WALLONIA),
        _luminus_cards._VAT_PATTERNS,
    ),
    "mega_dynamic": _Card(
        "mega_dynamic_w.pdf",
        fixture_text,
        lambda t: mega.parse_snapshot("mega_dynamic", t, REGION_WALLONIA),
        _mega_cards._VAT_PATTERNS,
        grossed_by_stated_rate=False,
    ),
    "mega_impact": _Card(
        "mega_offpeak_impact_w.pdf",
        fixture_text,
        lambda t: mega.parse_snapshot("mega_offpeak_impact_var", t, REGION_WALLONIA),
        _mega_cards._VAT_PATTERNS,
    ),
    "octaplus_smartvariable": _Card(
        "octaplus_smartvariable_w.pdf",
        _aligned,
        lambda t: octaplus.parse_snapshot("octaplus_smartvariable", t, REGION_WALLONIA),
        (octaplus._VAT_RE,),
    ),
    "totalenergies_impact": _Card(
        "totalenergies_impact_w.pdf",
        _layout,
        lambda t: totalenergies.parse_snapshot(
            "totalenergies_impact", t, REGION_WALLONIA
        ),
        (_totalenergies_cards._VAT_RE,),
    ),
    "totalenergies": _Card(
        "totalenergies_dynamic_w.pdf",
        _layout,
        lambda t: totalenergies.parse_snapshot(
            "totalenergies_mydynamic", t, REGION_WALLONIA
        ),
        (_totalenergies_cards._VAT_RE,),
    ),
}


def _restated(text: str, patterns: tuple[re.Pattern[str], ...], rate: str) -> str:
    """``text`` with every rate the patterns read replaced by ``rate``."""
    for pattern in patterns:
        text = pattern.sub(
            lambda m: (
                m.group(0)[: m.start(1) - m.start(0)]
                + rate
                + m.group(0)[m.end(1) - m.start(0) :]
            ),
            text,
        )
    return text


@pytest.mark.parametrize("name", sorted(_PRINTING))
def test_a_card_records_the_rate_it_states(name: str) -> None:
    card = _PRINTING[name]
    text = card.render(card.fixture)
    snap = card.parse(text)
    assert snap.taxes.card_vat_rate == pytest.approx(0.06)
    assert snap.taxes.assumed_vat_rate is None


@pytest.mark.parametrize("name", sorted(_PRINTING))
def test_a_card_stating_another_rate_is_read_and_grossed_at_it(name: str) -> None:
    """The same card printing 7%: the recorded rate follows, and so does every
    field the parser grosses, by exactly 1,07 / 1,06 and nothing else."""
    card = _PRINTING[name]
    text = card.render(card.fixture)
    at_six = card.parse(text)
    at_seven = card.parse(_restated(text, card.patterns, "7"))
    assert at_seven.taxes.card_vat_rate == pytest.approx(0.07)
    if card.cross_checked:
        return
    grossed = vat_grossed_fields(at_six.energy)
    ratio = 1.07 / 1.06 if card.grossed_by_stated_rate else 1.0
    for field in grossed:
        assert getattr(at_seven.energy, field) == pytest.approx(
            getattr(at_six.energy, field) * ratio, rel=1e-9
        ), field
    for field in set(at_six.energy.__dict__) - set(grossed):
        if isinstance(getattr(at_six.energy, field), str):
            continue  # the formula as the card words it, rate included
        assert getattr(at_seven.energy, field) == getattr(at_six.energy, field), field


@pytest.mark.parametrize(
    ("snap", "expected"),
    [
        pytest.param(
            lambda: bolt.parse_snapshot(
                "bolt_variable", _layout("bolt_variable.pdf"), REGION_WALLONIA
            ),
            (None, VAT_RATE_REDUCED),
            id="bolt variable: no rate stated, formula grossed",
        ),
        pytest.param(
            lambda: bolt.parse_snapshot(
                "bolt_fix", _layout("bolt_fix.pdf"), REGION_WALLONIA
            ),
            (None, None),
            id="bolt fixed: rates printed, nothing grossed",
        ),
        pytest.param(
            lambda: energiebe.parse_snapshot(
                _layout("energiebe_dynamic_jul.pdf"), "t://"
            ),
            (None, VAT_RATE_REDUCED),
            id="energie.be dynamic",
        ),
        pytest.param(
            lambda: energiebe.parse_snapshot(
                _layout("energiebe_variable_aug.pdf"), "t://", "energiebe_variable"
            ),
            (None, VAT_RATE_REDUCED),
            id="energie.be variable",
        ),
        pytest.param(
            lambda: energiebe.parse_snapshot(
                _layout("energiebe_fixed_aug.pdf"), "t://", "energiebe_fixed"
            ),
            (None, None),
            id="energie.be fixed",
        ),
        pytest.param(
            lambda: bolt.parse_snapshot(
                "bolt_pro_variable", _layout("bolt_pro_variable.pdf"), REGION_WALLONIA
            ),
            (None, None),
            id="bolt professional",
        ),
        pytest.param(
            lambda: engie.parse_snapshot(
                "engie_pro_dynamic",
                {REGION_FLANDERS: fixture_text("engie_pro_dynamic_v.pdf")},
            ),
            (None, None),
            id="engie professional",
        ),
        pytest.param(
            lambda: mega.parse_snapshot(
                "mega_pro_dynamic",
                fixture_text("mega_pro_dynamic_w.pdf"),
                REGION_WALLONIA,
            ),
            (None, None),
            id="mega professional",
        ),
    ],
)
def test_a_card_stating_no_rate_records_what_was_assumed(
    snap: Callable[[], SupplierSnapshot], expected: tuple[Any, Any]
) -> None:
    got = snap()
    assert (got.taxes.card_vat_rate, got.taxes.assumed_vat_rate) == expected


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("frank", (None, None)),
        ("eneco_dynamic", (None, None)),
        ("mega_dynamic", (None, None)),
        ("eneco", (None, VAT_RATE_REDUCED)),
        ("luminus", (None, VAT_RATE_REDUCED)),
    ],
)
def test_a_card_whose_rate_is_gone_records_an_assumption_only_if_it_grossed_on_one(
    name: str, expected: tuple[Any, Any]
) -> None:
    """With its stated rate taken out, a card whose formula carries its own
    multiplier, or is printed VAT-inclusive, assumed nothing; one whose parser
    grossed its formula by the missing rate assumed the residential one."""
    card = _PRINTING[name]
    text = _restated(card.render(card.fixture), card.patterns, "")
    assert all(p.search(text) is None for p in card.patterns)
    got = card.parse(text)
    assert (got.taxes.card_vat_rate, got.taxes.assumed_vat_rate) == expected


def test_a_rate_is_read_or_reported_missing() -> None:
    pattern = re.compile(r"TVA\s*(\d+)\s*%")
    assert printed_vat_rate("Tarifs, TVA 6 % incluse", pattern) == pytest.approx(0.06)
    assert printed_vat_rate("TVA % incluse", pattern) is None
    assert vat_multiplier("TVA % incluse", pattern) == pytest.approx(1.06)


def test_only_a_formula_counts_as_grossed() -> None:
    assert vat_grossed_fields(FixedRates(single=0.3, yearly_fixed_fee=50.0)) == ()
    assert vat_grossed_fields(DynamicRates(factor=1.1, base=0.02)) == ("factor", "base")
    monthly = SpotMonthlyRates(
        factor=1.1, base=0.02, ceiling_single=0.4, tier_kwh=1800.0, tier_rate=0.2
    )
    assert vat_grossed_fields(monthly) == ("factor", "base")
    assert vat_basis(None, monthly) == (None, VAT_RATE_REDUCED)
    assert vat_basis(0.06, monthly) == (0.06, None)
    assert vat_basis(None, FixedRates(single=0.3)) == (None, None)
    assert vat_basis(None, monthly, professional=True) == (None, None)


def test_a_brussels_card_whose_text_lost_its_rate_is_assumed() -> None:
    """TotalEnergies' September 2026 Brussels cards render "TVA % incluse": the
    digit is not in the text layer, so their formula is grossed on an
    assumed rate and says so."""
    text = _restated(
        _layout("totalenergies_dynamic_w.pdf"), (_totalenergies_cards._VAT_RE,), ""
    )
    assert _totalenergies_cards._VAT_RE.search(text) is None
    snap = totalenergies.parse_snapshot(
        "totalenergies_mydynamic", text, REGION_WALLONIA
    )
    assert (snap.taxes.card_vat_rate, snap.taxes.assumed_vat_rate) == (
        None,
        VAT_RATE_REDUCED,
    )


_ = REGION_BRUSSELS


def test_trevion_grosses_by_the_multiplier_its_formula_prints() -> None:
    """ "(0,107* Belpex 15 MTU+1,3) *1,06": the multiplier is read, so a card
    printing *1,07 is priced at it instead of refused, and the title line's
    "Incl. 6% BTW" is the rate it states."""
    text = _layout("trevion_dynamic_2026-09.pdf")
    at_six = trevion.parse_snapshot("groene_energie_dynamisch", text)
    assert (at_six.taxes.card_vat_rate, at_six.taxes.assumed_vat_rate) == (0.06, None)
    at_seven = trevion.parse_snapshot(
        "groene_energie_dynamisch", text.replace(") *1,06", ") *1,07")
    )
    assert isinstance(at_six.energy, DynamicRates)
    assert isinstance(at_seven.energy, DynamicRates)
    assert at_seven.energy.factor == pytest.approx(at_six.energy.factor * 1.07 / 1.06)
    assert at_seven.energy.base == pytest.approx(at_six.energy.base * 1.07 / 1.06)
    unstated = trevion.parse_snapshot(
        "groene_energie_dynamisch", _restated(text, (trevion._VAT_RE,), "")
    )
    assert (unstated.taxes.card_vat_rate, unstated.taxes.assumed_vat_rate) == (
        None,
        None,
    )


def test_ecopower_is_priced_at_the_rate_it_states_for_households() -> None:
    """The card is printed excluding VAT and says what a household pays on
    top: that is its vat_rate, read rather than written in."""
    text = _layout("ecopower_burgerstroom_jul.pdf")
    snap = ecopower.parse_snapshot(text, "t://", "2026-07")
    assert snap.taxes.vat_rate == pytest.approx(0.06)
    assert (snap.taxes.card_vat_rate, snap.taxes.assumed_vat_rate) == (0.06, None)
    at_seven = ecopower.parse_snapshot(
        _restated(text, (_ecopower_overlays._VAT_RE,), "7"), "t://", "2026-07"
    )
    assert at_seven.taxes.vat_rate == pytest.approx(0.07)
    assert at_seven.taxes.card_vat_rate == pytest.approx(0.07)
    unstated = ecopower.parse_snapshot(
        _restated(text, (_ecopower_overlays._VAT_RE,), ""), "t://", "2026-07"
    )
    assert unstated.taxes.vat_rate == pytest.approx(VAT_RATE_REDUCED)
    assert (unstated.taxes.card_vat_rate, unstated.taxes.assumed_vat_rate) == (
        None,
        VAT_RATE_REDUCED,
    )

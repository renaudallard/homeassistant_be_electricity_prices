"""The social tariff (``providers/social.py``): the CREG's quarterly card, the
protected customer's excise and the feed-in two suppliers publish."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.be_electricity_prices import excise_law
from custom_components.be_electricity_prices.const import (
    REGION_BRUSSELS,
    REGION_FLANDERS,
    REGION_WALLONIA,
    WALLONIA_DSO_KEYS,
)
from custom_components.be_electricity_prices.injection import (
    _bake_monthly_injection,
    _historical_injection_rate,
    _injection_price_for_slot,
    _tou_injection_rate,
)
from custom_components.be_electricity_prices.pricing import compute_breakdown
from custom_components.be_electricity_prices.providers import social
from custom_components.be_electricity_prices.providers._rates import (
    InjectionRates,
    VariableRates,
)
from custom_components.be_electricity_prices.providers._resolve import (
    omits_brussels_power_term,
    resolve_federal_excise,
    resolve_vreg_network_ceiling,
)
from custom_components.be_electricity_prices.providers.base import (
    ExtractorError,
    SupplierSnapshot,
)
from tests import fixture_text

BRUSSELS = ZoneInfo("Europe/Brussels")
OCT = date(2026, 10, 1)


def _card(name: str) -> social.CregCard:
    return social.parse_creg(fixture_text(name))


def test_the_q4_2026_card_reads_as_the_creg_prints_it() -> None:
    card = _card("creg_social_2026_q4.pdf")
    assert card.quarter == OCT
    assert card.vat == pytest.approx(0.06)
    assert (card.mono.total, card.mono.network) == pytest.approx((26.851, 11.727))
    assert (card.day.total, card.night.total) == pytest.approx((26.851, 25.660))
    assert (card.excl_night.total, card.excl_night.network) == pytest.approx(
        (22.214, 8.594)
    )


def test_a_card_printing_distribution_and_transport_apart_is_read_whole() -> None:
    """Until 2025 the network is two rows, distribution and transport, and
    both are the network: 6,656 + 1,000 on the single register in Q4 2022."""
    card = _card("creg_social_2022_q4.pdf")
    assert card.quarter == date(2022, 10, 1)
    assert card.mono.total == pytest.approx(26.510)
    assert card.mono.network == pytest.approx(7.656)
    assert card.night.network == pytest.approx(6.029)


def test_a_card_whose_rows_do_not_add_up_is_refused() -> None:
    text = fixture_text("creg_social_2026_q4.pdf")
    broken = text.replace(
        "Total (€cent/kWh) 25,331 26,851", "Total (€cent/kWh) 25,331 27,851", 1
    )
    assert broken != text
    with pytest.raises(ExtractorError, match="add up"):
        social.parse_creg(broken)


def test_the_protected_excise_follows_the_law_month_by_month() -> None:
    """Exempt until the law of 19 March 2023, at 0 through its transition to
    30 June 2023, 23,62 EUR/MWh from July, and the 1 EUR/MWh the law of 30
    May 2026 set from August 2026, which is read from Justel."""
    assert excise_law.protected_excise(date(2022, 12, 1)) == 0.0
    assert excise_law.protected_excise(date(2023, 6, 1)) == 0.0
    assert excise_law.protected_excise(date(2023, 7, 1)) == pytest.approx(0.02362)
    assert excise_law.protected_excise(date(2026, 7, 1)) == pytest.approx(0.02362)
    assert excise_law.protected_excise(date(2026, 8, 1)) == pytest.approx(0.001)
    excise_law._HELD.clear()
    # Unread, a month the law's text in force covers is not known, and is
    # never billed at the old rate.
    assert excise_law.protected_excise(date(2026, 8, 1)) is None
    assert excise_law.protected_excise(date(2026, 7, 1)) == pytest.approx(0.02362)


def _wallonia(month: date = OCT) -> SupplierSnapshot:
    return social.build_snapshot(
        social.CONTRACT_OTHER,
        REGION_WALLONIA,
        _card("creg_social_2026_q4.pdf"),
        month,
        injection=None,
        connection_fee=0.00075,
        source_url="t://",
    )


def test_a_walloon_household_pays_the_creg_price_the_excise_and_the_fee() -> None:
    """26,851 c/kWh for the energy and the network, 0,106 of excise and the
    0,075 connection fee: 27,032 c/kWh, and nothing a year."""
    snap = _wallonia()
    assert set(snap.dsos) == WALLONIA_DSO_KEYS
    assert snap.energy.yearly_fixed_fee == 0.0
    overlay = snap.dsos["ores"]
    assert overlay.data_management_per_year == 0.0
    assert overlay.capacity_eur_per_kw_year is None
    assert overlay.prosumer_eur_per_kva_year == 0.0
    when = datetime(2026, 10, 14, 12, tzinfo=BRUSSELS)
    price = compute_breakdown(snap, "ores", REGION_WALLONIA, when)
    assert price.energy + price.network == pytest.approx(0.26851)
    assert price.all_in == pytest.approx(0.26851 + 0.00106 + 0.00075)
    night = compute_breakdown(
        snap, "ores", REGION_WALLONIA, when, meter="exclusive_night"
    )
    assert night.energy + night.network == pytest.approx(0.22214)


def test_impact_bills_the_day_and_night_prices_by_band() -> None:
    """Engie's social card maps its day rate onto PIC-MEDIUM and its night
    rate onto ECO; the network part is the same in every band."""
    snap = _wallonia()
    eco = datetime(2026, 10, 14, 12, tzinfo=BRUSSELS)
    pic = datetime(2026, 10, 14, 18, tzinfo=BRUSSELS)
    for when, total in ((eco, 0.25660), (pic, 0.26851)):
        price = compute_breakdown(
            snap, "ores", REGION_WALLONIA, when, meter="bi", dso_tariff_mode="impact"
        )
        assert price.energy + price.network == pytest.approx(total)


def test_a_card_built_before_the_law_is_read_carries_the_gap(
    excise_law_table: dict[str, tuple[tuple[date, float], ...]],
) -> None:
    """The project's archive builds its rows where Justel cannot be read, so a
    card for a month the law decides carries no excise and says so; the
    resolver fills it once the law is held."""
    excise_law._HELD.clear()
    snap = _wallonia()
    assert snap.taxes.protected_excise_unread
    assert snap.taxes.federal_excise == 0.0
    assert resolve_federal_excise(snap, OCT, professional=False) is snap
    excise_law._HELD.update(excise_law_table)
    resolved = resolve_federal_excise(snap, OCT, professional=False)
    assert not resolved.taxes.protected_excise_unread
    assert resolved.taxes.federal_excise == pytest.approx(0.001 * 1.06)


async def test_nothing_is_published_until_the_law_is_read(
    hass: HomeAssistant,
    excise_law_table: dict[str, tuple[tuple[date, float], ...]],
) -> None:
    """Without the law a social entry would leave the excise out or bill the
    household rate, and neither is the bill: the tick refuses, and a Repairs
    card says why until the law is read."""
    from homeassistant.helpers import issue_registry as ir
    from homeassistant.helpers.update_coordinator import UpdateFailed
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.be_electricity_prices.const import DOMAIN
    from custom_components.be_electricity_prices.coordinator import (
        BePricesCoordinator,
    )

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": "social",
            "contract": social.CONTRACT_OTHER,
            "region": REGION_WALLONIA,
            "dso": "ores",
            "meter": "mono",
        },
    )
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    excise_law._HELD.clear()
    card = _wallonia()
    coord._set_snapshot(card)
    coord._snapshot_fetched_at = dt_util.utcnow()
    issue_id = f"excise_law_unread_{entry.entry_id}"
    with (
        patch.object(coord, "_maybe_refresh_snapshot", AsyncMock()),
        pytest.raises(UpdateFailed, match="excise"),
    ):
        await coord._update_body()
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None
    excise_law._HELD.update(excise_law_table)
    coord._reresolve_snapshot()
    assert coord._snapshot is not None
    assert not coord._snapshot.taxes.protected_excise_unread


def test_the_protected_excise_is_billed_whatever_the_card_month() -> None:
    """The quarter's card is resolved per delivery month: July 2026 owes the
    23,62 EUR/MWh and August the 1 EUR/MWh, on the same Q3 card."""
    card = _card("creg_social_2026_q3.pdf")
    snap = social.build_snapshot(
        social.CONTRACT_OTHER,
        REGION_FLANDERS,
        card,
        date(2026, 9, 1),
        injection=None,
        connection_fee=None,
        source_url="t://",
    )
    july = resolve_federal_excise(snap, date(2026, 7, 1), professional=False)
    assert july.taxes.federal_excise == pytest.approx(0.02362 * 1.06)
    august = resolve_federal_excise(snap, date(2026, 8, 1), professional=False)
    assert august.taxes.federal_excise == pytest.approx(0.001 * 1.06)


def test_the_network_rules_of_the_operators_do_not_reach_it() -> None:
    """The CREG's network component replaces the operator's tariffs: no VREG
    ceiling in Flanders, and no Sibelga power term in Brussels, where the
    social cards print none."""
    card = _card("creg_social_2026_q4.pdf")
    flanders = social.build_snapshot(
        social.CONTRACT_OTHER,
        REGION_FLANDERS,
        card,
        OCT,
        injection=None,
        connection_fee=None,
        source_url="t://",
    )
    assert resolve_vreg_network_ceiling(flanders, OCT) is flanders
    brussels = social.build_snapshot(
        social.CONTRACT_OTHER,
        REGION_BRUSSELS,
        card,
        OCT,
        injection=None,
        connection_fee=None,
        source_url="t://",
    )
    assert not omits_brussels_power_term(brussels, terms=(47.24, 94.48), month=OCT)
    assert brussels.taxes.region_connection_fee == 0.0
    assert not brussels.taxes.region_connection_fee_unavailable


def test_engie_prints_its_feed_in_and_the_formulas_behind_it() -> None:
    card = _card("creg_social_2026_q4.pdf")
    injection, fee = social.parse_engie(
        fixture_text("engie_social_w_2026-10.pdf"), card
    )
    assert (injection.current, injection.peak, injection.offpeak) == pytest.approx(
        (0.09935, 0.11687, 0.04836)
    )
    assert injection.bi_hourly and injection.month_indexed
    assert (injection.factor, injection.base) == pytest.approx((0.632, 0.0005))
    assert fee == pytest.approx(0.00075)


def test_engie_s_day_and_night_feed_in_follow_the_month_too() -> None:
    """The card gives one month formula per register, so a two-register meter
    is credited on the delivery month's EPEXDAM like a single one, on the live
    bake and on the historical walk, instead of at the printed rates (last
    month's index). At a 120 EUR/MWh month: day 0,05 + 0,0744 x 120 = 8,978,
    night 0,05 + 0,0306 x 120 = 3,722 c/kWh."""
    card = _card("creg_social_2026_q4.pdf")
    injection, fee = social.parse_engie(
        fixture_text("engie_social_w_2026-10.pdf"), card
    )
    snap = social.build_snapshot(
        social.CONTRACT_ENGIE,
        REGION_WALLONIA,
        card,
        OCT,
        injection=injection,
        connection_fee=fee,
        source_url="t://",
    )
    mean = 0.120
    baked = _bake_monthly_injection(snap, mean)
    assert baked.injection is not None
    day = datetime(2026, 10, 14, 19, tzinfo=BRUSSELS)
    night = datetime(2026, 10, 14, 23, tzinfo=BRUSSELS)
    for when, expected in ((day, 0.08978), (night, 0.03722)):
        live = _injection_price_for_slot(
            baked.injection,
            baked.energy,
            None,
            when,
            meter="bi",
            region=REGION_WALLONIA,
        )
        walked = _historical_injection_rate(
            injection,
            mean,
            energy=snap.energy,
            when=when,
            meter="bi",
            region=REGION_WALLONIA,
        )
        assert live == pytest.approx(expected)
        assert walked == pytest.approx(expected)
    # A single register keeps its own formula.
    assert baked.injection.current == pytest.approx(0.0005 + 0.632 * mean)


async def test_the_day_and_night_feed_in_sensors_show_what_is_credited(
    hass: HomeAssistant, freezer: Any
) -> None:
    """The two band sensors are the rates the Energy dashboard prices each
    return register at, so they read the leg the tick credits, baked on the
    month, not the printed pair (last month's index)."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.be_electricity_prices.cohort import _CohortLegs
    from custom_components.be_electricity_prices.const import DOMAIN
    from custom_components.be_electricity_prices.coordinator import (
        BePricesCoordinator,
    )

    freezer.move_to("2026-10-14 12:00:00+02:00")
    card = _card("creg_social_2026_q4.pdf")
    injection, fee = social.parse_engie(
        fixture_text("engie_social_w_2026-10.pdf"), card
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": "social",
            "contract": social.CONTRACT_ENGIE,
            "region": REGION_WALLONIA,
            "dso": "ores",
            "meter": "bi",
        },
    )
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    coord._set_snapshot(
        social.build_snapshot(
            social.CONTRACT_ENGIE,
            REGION_WALLONIA,
            card,
            OCT,
            injection=injection,
            connection_fee=fee,
            source_url="t://",
        )
    )
    coord._snapshot_fetched_at = dt_util.utcnow()
    with (
        patch.object(coord, "_maybe_refresh_snapshot", AsyncMock()),
        patch.object(coord, "_track_monthly_peak", AsyncMock()),
        patch.object(coord, "_tick_spot_prices", AsyncMock(return_value={})),
        patch.object(
            coord, "_tick_profiles", AsyncMock(return_value=(False, False, False))
        ),
        patch.object(coord, "_tick_month_means", Mock(return_value=(0.120, 0.120))),
        patch(
            "custom_components.be_electricity_prices.coordinator_tick._cohort_legs",
            AsyncMock(return_value=_CohortLegs(None, None)),
        ),
        patch(
            "custom_components.be_electricity_prices.coordinator_costs."
            "_compute_current_year_cost",
            AsyncMock(return_value=0.0),
        ),
        patch.object(coord, "_save_persistent", AsyncMock()),
    ):
        data = await coord._update_body()
    assert data.static_injection_peak == pytest.approx(0.08978)
    assert data.static_injection_offpeak == pytest.approx(0.03722)


async def test_a_start_date_puts_no_signing_cohort_on_the_social_tariff() -> None:
    """The social tariff is the same card for every protected customer each
    month, so a contract start date freezes nothing: Engie's feed-in pairs on
    a June signer's October bill are October's, not cleared or replaced by the
    signing card's."""
    from types import SimpleNamespace

    from custom_components.be_electricity_prices import cohort

    card = _card("creg_social_2026_q4.pdf")
    injection, fee = social.parse_engie(
        fixture_text("engie_social_w_2026-10.pdf"), card
    )
    october = social.build_snapshot(
        social.CONTRACT_ENGIE,
        REGION_WALLONIA,
        card,
        OCT,
        injection=injection,
        connection_fee=fee,
        source_url="t://",
    )
    july = social.build_snapshot(
        social.CONTRACT_ENGIE,
        REGION_WALLONIA,
        _card("creg_social_2026_q3.pdf"),
        date(2026, 7, 1),
        injection=injection,
        connection_fee=fee,
        source_url="t://",
    )
    entry = SimpleNamespace(
        entry_id="x",
        data={
            "supplier": "social",
            "contract": social.CONTRACT_ENGIE,
            "region": REGION_WALLONIA,
            "contract_start_date": "2026-07-15",
            "meter": "bi",
        },
    )
    with (
        patch.object(cohort, "month_card", AsyncMock(return_value=july)),
        patch.object(cohort, "_month_card_retrievable", lambda *_: True),
    ):
        legs = await cohort._cohort_legs(
            None,  # type: ignore[arg-type]
            None,  # type: ignore[arg-type]
            social.EXTRACTOR,
            social.CONTRACT_ENGIE,
            REGION_WALLONIA,
            entry,  # type: ignore[arg-type]
            october,
        )
    assert legs.splice(october) == october


def test_a_register_pair_without_formulas_is_still_credited_as_printed() -> None:
    """Trevion Vast prints a day and night feed-in pair and no formula: the
    month mean must not move it."""
    pair = InjectionRates(current=0.05, peak=0.06, offpeak=0.04, bi_hourly=True)
    day = datetime(2026, 10, 14, 19, tzinfo=BRUSSELS)
    rate = _tou_injection_rate(
        pair, VariableRates(current=0.2), day, 0.120, meter="bi", region="flanders"
    )
    assert rate == pytest.approx(0.06)


def test_a_supplier_card_of_another_quarter_is_refused() -> None:
    """Its consumption row is the CREG's for the quarter it was written in,
    which is what dates it: October's Engie card against the Q3 prices."""
    with pytest.raises(ExtractorError, match="quarter"):
        social.parse_engie(
            fixture_text("engie_social_w_2026-10.pdf"),
            _card("creg_social_2026_q3.pdf"),
        )


def test_luminus_prints_its_feed_in_and_the_walloon_fee() -> None:
    card = _card("creg_social_2026_q4.pdf")
    injection, fee = social.parse_luminus(
        fixture_text("luminus_social_w_2026-10.pdf"), card
    )
    assert (injection.current, injection.peak, injection.offpeak) == pytest.approx(
        (0.0688, 0.0885, 0.0385)
    )
    assert injection.bi_hourly and not injection.month_indexed
    assert fee == pytest.approx(0.00075)


async def test_another_suppliers_household_reads_the_fee_off_the_luminus_card() -> None:
    """The Walloon connection fee is the same whoever supplies, so a household
    on another supplier reads it on Luminus's social card, with no feed-in;
    if that card cannot be read the fee is disclosed as missing, not billed
    at a guess."""
    creg = fixture_text("creg_social_2026_q4.pdf")
    luminus = fixture_text("luminus_social_w_2026-10.pdf")
    products = '[{"Product": "Tarif social Electricité", "ProductId": "x"}]'

    async def pdf(_session: object, url: str, **_kw: object) -> str:
        return creg if "creg.be" in url else luminus

    with (
        patch.object(social, "fetch_pdf_text", side_effect=pdf),
        patch.object(social, "fetch_text", AsyncMock(return_value=products)),
        patch.object(
            social.dt_util, "now", return_value=datetime(2026, 10, 9, tzinfo=BRUSSELS)
        ),
    ):
        snap = await social.fetch(AsyncMock(), social.CONTRACT_OTHER, REGION_WALLONIA)
    assert snap.injection is None
    assert snap.taxes.region_connection_fee == pytest.approx(0.00075)
    assert snap.publication_label == "Q4 2026"
    assert snap.valid_until == date(2026, 12, 31)

    with (
        patch.object(social, "fetch_pdf_text", side_effect=pdf),
        patch.object(
            social, "fetch_text", AsyncMock(side_effect=ExtractorError("HTTP 404"))
        ),
        patch.object(
            social.dt_util, "now", return_value=datetime(2026, 10, 9, tzinfo=BRUSSELS)
        ),
    ):
        snap = await social.fetch(AsyncMock(), social.CONTRACT_OTHER, REGION_WALLONIA)
    assert snap.taxes.region_connection_fee == 0.0
    assert snap.taxes.region_connection_fee_unavailable


async def test_a_month_before_the_creg_archive_has_no_card() -> None:
    assert (
        await social.fetch_for_month(
            AsyncMock(), social.CONTRACT_OTHER, REGION_FLANDERS, date(2022, 9, 1)
        )
        is None
    )


@pytest.mark.parametrize(
    ("name", "quarter", "mono"),
    [
        ("creg_social_2022_q4.pdf", date(2022, 10, 1), 26.510),
        # Its totals carry two asterisks, for a second footnote.
        ("creg_social_2023_q1.pdf", date(2023, 1, 1), 28.579),
        ("creg_social_2023_q3.pdf", date(2023, 7, 1), 22.238),
        ("creg_social_2025_q2.pdf", date(2025, 4, 1), 22.538),
        ("creg_social_2026_q3.pdf", date(2026, 7, 1), 24.927),
        ("creg_social_2026_q4.pdf", date(2026, 10, 1), 26.851),
    ],
)
def test_every_layout_the_creg_has_used_is_read(
    name: str, quarter: date, mono: float
) -> None:
    card = _card(name)
    assert card.quarter == quarter
    assert card.mono.total == pytest.approx(mono)


def test_engie_s_2023_card_spells_its_formula_without_parentheses() -> None:
    """The 2023 cards print "Normal = 0,0500 + 0,0632 x EPEX DAM", with a
    space and no parentheses, and the footnote marks sit on the line under
    each label. The formula is read all the same: the feed-in follows the
    delivery month's index, not the August figure the card prints at."""
    injection, fee = social.parse_engie(
        fixture_text("engie_social_w_2023-09.pdf"), _card("creg_social_2023_q3.pdf")
    )
    assert (injection.current, injection.peak, injection.offpeak) == pytest.approx(
        (0.05862, 0.06892, 0.02864)
    )
    assert injection.month_indexed
    assert (injection.factor, injection.base) == pytest.approx((0.632, 0.0005))
    assert fee == pytest.approx(0.00075)


def test_luminus_s_card_of_a_change_of_rate_bills_its_own_month() -> None:
    """The June 2025 card prints the feed-in "jusqu'au 30/6/2025" and the
    one "à partir du 1/7/2025"; June is billed on the first."""
    injection, _ = social.parse_luminus(
        fixture_text("luminus_social_w_2025-06.pdf"), _card("creg_social_2025_q2.pdf")
    )
    assert (injection.current, injection.peak, injection.offpeak) == pytest.approx(
        (0.0621, 0.0786, 0.0368)
    )

"""The VAT rate of the month billed, and where it reaches a card
(``vat_rates.py``, ``providers/_resolve.resolve_vat_rate``)."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import date

import pytest

from custom_components.be_electricity_prices import vat_rates
from custom_components.be_electricity_prices.cohort import _CohortLegs
from custom_components.be_electricity_prices.const import (
    REGION_FLANDERS,
    REGION_WALLONIA,
    VAT_RATE_REDUCED,
    VAT_RATE_STANDARD,
    VREG_NETWORK_CEILING_HTVA,
)
from custom_components.be_electricity_prices.providers import (
    _bolt_cards,
    _ecopower_overlays,
    bolt,
    ecopower,
    energiebe,
    octaplus,
)
from custom_components.be_electricity_prices.providers._rates import (
    DynamicRates,
    FixedRates,
)
from custom_components.be_electricity_prices.providers._resolve import (
    card_residential_vat,
    resolve_brussels_power_term,
    resolve_federal_excise,
    resolve_vat_rate,
    resolve_vreg_network_ceiling,
)
from custom_components.be_electricity_prices.providers.base import (
    DsoOverlay,
    SupplierSnapshot,
    TaxOverlay,
)
from tests import fixture_text, make_snapshot
from tests.test_card_vat import _restated

# Lays out real cards, Bolt's among them, which ran past 60 s with no
# text cached on a loaded Raspberry Pi 4.
pytestmark = pytest.mark.timeout(300)

OCT, NOV, DEC = date(2026, 10, 1), date(2026, 11, 1), date(2026, 12, 1)
# The household excise article 419 sets from August 2026, EUR/kWh before VAT.
_LAW_EXCISE = 0.046


@pytest.fixture
def seven_from_november() -> Iterator[None]:
    """The residential rate moving to 7% from November 2026."""
    held = vat_rates.held_table()
    vat_rates.hold(
        {
            "residential": {"2026-10": {"rate": 0.06}, "2026-11": {"rate": 0.07}},
            "standard": {"2026-10": {"rate": 0.21}},
        }
    )
    yield
    vat_rates._RESIDENTIAL.clear()
    vat_rates._STANDARD.clear()
    vat_rates.hold(held)


def test_a_month_takes_its_own_rate_or_the_last_one_before_it(
    seven_from_november: None,
) -> None:
    assert vat_rates.residential_vat(OCT) == 0.06
    assert vat_rates.residential_vat(NOV) == 0.07
    assert vat_rates.residential_vat(DEC) == 0.07
    # Before anything held: the constants, never a later month's rate.
    assert vat_rates.residential_vat(date(2026, 3, 1)) == VAT_RATE_REDUCED
    assert vat_rates.standard_vat(DEC) == 0.21


def test_nothing_held_is_the_constants() -> None:
    assert vat_rates.residential_vat(NOV) == VAT_RATE_REDUCED
    assert vat_rates.standard_vat(NOV) == VAT_RATE_STANDARD


def _bolt(text: str) -> SupplierSnapshot:
    return bolt.parse_snapshot("bolt_variable", text, REGION_WALLONIA)


def _octaplus(text: str) -> SupplierSnapshot:
    return octaplus.parse_snapshot("octaplus_dynamic", text, REGION_WALLONIA)


def _octaplus_text() -> str:
    text = fixture_text("octaplus_dynamic_w.pdf", aligned=True)
    # The header its cards stopped printing in June 2026.
    return _restated(text, (octaplus._VAT_RE,), "")


@pytest.mark.parametrize(
    ("text", "parse", "module", "constant"),
    [
        pytest.param(
            lambda: fixture_text("bolt_variable.pdf", layout=True),
            _bolt,
            _bolt_cards,
            "_RESIDENTIAL_VAT",
            id="bolt variable",
        ),
        pytest.param(
            lambda: fixture_text("energiebe_dynamic_jul.pdf", layout=True),
            lambda t: energiebe.parse_snapshot(t, "t://"),
            energiebe,
            "_VAT_MULT",
            id="energie.be dynamic",
        ),
        pytest.param(
            lambda: fixture_text("energiebe_variable_aug.pdf", layout=True),
            lambda t: energiebe.parse_snapshot(t, "t://", "energiebe_variable"),
            energiebe,
            "_VAT_MULT",
            id="energie.be variable",
        ),
        pytest.param(
            _octaplus_text,
            _octaplus,
            octaplus,
            "_RESIDENTIAL_VAT",
            id="octaplus dynamic",
        ),
    ],
)
def test_an_assumed_rate_becomes_the_months_exactly(
    seven_from_november: None,
    monkeypatch: pytest.MonkeyPatch,
    text: Callable[[], str],
    parse: Callable[[str], SupplierSnapshot],
    module: object,
    constant: str,
) -> None:
    """The card parsed at 6% and resolved for a 7% month is the card parsed at
    7%, in every field: the rescale reaches exactly what the parser grossed
    by its assumption, printed rates and fees untouched."""
    card = text()
    at_six = parse(card)
    assert at_six.taxes.assumed_vat_rate == VAT_RATE_REDUCED
    monkeypatch.setattr(module, constant, 1.07)
    at_seven = parse(card)
    resolved = resolve_vat_rate(at_six, NOV, professional=False)
    assert resolved.taxes.assumed_vat_rate == 0.07
    for name, value in at_seven.energy.__dict__.items():
        got = getattr(resolved.energy, name)
        if isinstance(value, float):
            assert got == pytest.approx(value, rel=1e-12), name
        elif not isinstance(value, str):
            assert got == value, name
    # And a 6% month leaves the card exactly as parsed.
    assert resolve_vat_rate(at_six, OCT, professional=False) is at_six


def test_a_card_priced_excluding_vat_takes_the_months_rate_where_it_assumed(
    seven_from_november: None,
) -> None:
    text = fixture_text("ecopower_burgerstroom_jul.pdf", layout=True)
    stated = ecopower.parse_snapshot(text, "t://", "2026-07")
    assert resolve_vat_rate(stated, NOV, professional=False) is stated
    unstated = ecopower.parse_snapshot(
        _restated(text, (_ecopower_overlays._VAT_RE,), ""), "t://", "2026-07"
    )
    got = resolve_vat_rate(unstated, NOV, professional=False)
    assert got.taxes.vat_rate == 0.07
    assert got.energy == unstated.energy
    # The levy excluding VAT is the law's figure whatever the month's rate,
    # so the bill moves with the rate like on a card priced including it.
    excise = resolve_federal_excise(got, NOV, professional=False).taxes
    assert excise.federal_excise == pytest.approx(_LAW_EXCISE)
    assert excise.federal_excise * (1.0 + excise.vat_rate) == pytest.approx(
        _LAW_EXCISE * 1.07
    )


def test_a_professional_card_takes_the_months_standard_rate(
    seven_from_november: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    card = make_snapshot(
        taxes=TaxOverlay(federal_excise=0.01, energy_contribution=0.0, vat_rate=0.21)
    )
    assert resolve_vat_rate(card, NOV, professional=True) is card
    monkeypatch.setitem(vat_rates._STANDARD, (2026, 11), 0.22)
    assert resolve_vat_rate(card, NOV, professional=True).taxes.vat_rate == 0.22


def test_a_card_stating_its_rate_keeps_it_against_the_month(
    seven_from_november: None,
) -> None:
    card = make_snapshot(
        energy=DynamicRates(factor=1.06, base=0.0106),
        taxes=TaxOverlay(
            federal_excise=0.04876, energy_contribution=0.0, card_vat_rate=0.06
        ),
    )
    assert resolve_vat_rate(card, NOV, professional=False) is card
    assert card_residential_vat(card, NOV) == 0.06
    assert (
        card_residential_vat(
            replace(card, taxes=replace(card.taxes, card_vat_rate=None)), NOV
        )
        == 0.07
    )


def test_the_regulated_figures_follow_the_months_rate(
    seven_from_november: None,
) -> None:
    """The flat excise, the VREG ceiling and Brugel's power term are published
    excluding VAT and put onto a VAT-inclusive card at the month's rate."""
    fluvius = make_snapshot(
        dsos={"fluvius_antwerpen": DsoOverlay(distribution_single=0.1, transport=0.01)},
        taxes=TaxOverlay(federal_excise=0.04876, energy_contribution=0.0),
    )
    excise = resolve_federal_excise(
        fluvius, NOV, professional=False
    ).taxes.federal_excise
    assert excise == pytest.approx(_LAW_EXCISE * 1.07)
    assert resolve_federal_excise(fluvius, OCT, professional=False) is fluvius
    ceiling = (
        resolve_vreg_network_ceiling(fluvius, NOV)
        .dsos["fluvius_antwerpen"]
        .network_ceiling_eur_per_kwh
    )
    assert ceiling == pytest.approx(VREG_NETWORK_CEILING_HTVA * 1.07)
    sibelga = make_snapshot(
        dsos={
            "sibelga": DsoOverlay(
                distribution_single=0.1, transport=0.01, data_management_per_year=10.0
            )
        }
    )
    at_seven = resolve_brussels_power_term(sibelga, terms=(40.0, 80.0), month=NOV)
    at_six = resolve_brussels_power_term(sibelga, terms=(40.0, 80.0), month=OCT)
    grown = at_seven.dsos["sibelga"].data_management_per_year - 10.0
    assert grown == pytest.approx(40.0 * 1.07)
    assert at_six.dsos["sibelga"].data_management_per_year - 10.0 == pytest.approx(
        40.0 * 1.06
    )


def test_a_cohort_leg_is_billed_at_the_vat_of_the_month_delivered(
    seven_from_november: None,
) -> None:
    """Signed in October at 6%: the coefficients locked are the contract's,
    the VAT is November's."""
    signed = DynamicRates(factor=1.06, base=0.0212)
    legs = _CohortLegs(energy=signed, injection=None, vat_rate=0.06)
    november = make_snapshot(energy=FixedRates(single=0.3))
    got = legs.splice(november, NOV).energy
    assert isinstance(got, DynamicRates)
    assert got.factor == pytest.approx(1.07)
    assert got.base == pytest.approx(0.0214)
    assert legs.splice(november, OCT).energy is signed
    # A leg carrying no rate to move is spliced as it is.
    assert (
        _CohortLegs(energy=signed, injection=None).splice(november, NOV).energy
        is signed
    )
    # Nor onto a card priced excluding VAT, which the engine grosses itself.
    htva = replace(november, taxes=replace(november.taxes, vat_rate=0.06))
    assert legs.splice(htva, NOV).energy is signed


_ = REGION_FLANDERS

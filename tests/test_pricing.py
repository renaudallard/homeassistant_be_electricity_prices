# Copyright (c) 2026, Renaud Allard <renaud@allard.it>
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# 1. Redistributions of source code must retain the above copyright notice,
#    this list of conditions and the following disclaimer.
#
# 2. Redistributions in binary form must reproduce the above copyright notice,
#    this list of conditions and the following disclaimer in the documentation
#    and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

"""Tests for the pricing engine working off SupplierSnapshot."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from custom_components.be_electricity_prices.pricing import (
    compute_breakdown,
    dso_impact_band,
    energy_eur_per_kwh,
    is_belgian_holiday,
    is_offpeak,
    network_eur_per_kwh,
    taxes_eur_per_kwh,
    tou_slot,
    yearly_fixed_fee_for_meter,
)
from custom_components.be_electricity_prices.providers.base import (
    DsoOverlay,
    DynamicRates,
    EnergyRates,
    FixedRates,
    ImpactRates,
    SupplierSnapshot,
    TaxOverlay,
    TimeOfUseRates,
    VariableRates,
)
from tests import make_snapshot


def _snapshot(energy: EnergyRates, vat: float = 0.0) -> SupplierSnapshot:
    return make_snapshot(
        energy=energy,
        dsos={
            "fluvius": DsoOverlay(
                distribution_single=0.05,
                distribution_peak=0.06,
                distribution_offpeak=0.04,
                transport=0.015,
            )
        },
        taxes=TaxOverlay(
            federal_excise=0.05,
            energy_contribution=0.002,
            flanders_renewables=0.015,
            wallonia_renewables=0.015,
            region_connection_fee=0.001,
            vat_rate=vat,
        ),
    )


def test_offpeak_weekday_night() -> None:
    assert is_offpeak(datetime(2026, 4, 29, 23, 0))
    assert is_offpeak(datetime(2026, 4, 29, 6, 0))
    assert not is_offpeak(datetime(2026, 4, 29, 12, 0))


def test_offpeak_weekend_always_offpeak() -> None:
    assert is_offpeak(datetime(2026, 5, 2, 12, 0))


def test_belgian_holidays_2026() -> None:
    # Fixed dates.
    assert is_belgian_holiday(date(2026, 1, 1))  # New Year
    assert is_belgian_holiday(date(2026, 5, 1))  # Labour Day
    assert is_belgian_holiday(date(2026, 7, 21))  # National Day
    assert is_belgian_holiday(date(2026, 8, 15))  # Assumption
    assert is_belgian_holiday(date(2026, 11, 1))  # All Saints
    assert is_belgian_holiday(date(2026, 11, 11))  # Armistice
    assert is_belgian_holiday(date(2026, 12, 25))  # Christmas
    # Easter-derived 2026 (Easter Sunday = April 5, 2026).
    assert is_belgian_holiday(date(2026, 4, 6))  # Easter Monday
    assert is_belgian_holiday(date(2026, 5, 14))  # Ascension (+39)
    assert is_belgian_holiday(date(2026, 5, 25))  # Pentecost Monday (+50)
    # Non-holidays.
    assert not is_belgian_holiday(
        date(2026, 4, 5)
    )  # Easter Sunday itself isn't a separate holiday (and it's a weekend anyway)
    assert not is_belgian_holiday(date(2026, 4, 7))  # Tuesday after Easter Monday
    assert not is_belgian_holiday(
        date(2026, 7, 11)
    )  # Flemish regional holiday — federal-only set
    assert not is_belgian_holiday(date(2026, 9, 27))  # French Community holiday — same


def test_offpeak_weekday_holiday_is_region_specific() -> None:
    # May 1, 2026 is a Friday (Labour Day) at noon. Flanders bills weekday
    # holidays at the DAY rate (the meter clock ignores holidays); Brussels
    # folds them into off-peak (the historical Brussels exception).
    holiday_noon = datetime(2026, 5, 1, 12, 0)
    assert not is_offpeak(holiday_noon, "flanders")
    assert is_offpeak(holiday_noon, "brussels")
    # Christmas 2026 (Friday) at 14h - same split.
    assert not is_offpeak(datetime(2026, 12, 25, 14, 0), "flanders")
    assert is_offpeak(datetime(2026, 12, 25, 14, 0), "brussels")


def test_offpeak_wallonia_2026_uniform_daily_schedule() -> None:
    # From 2026-01-01 the Walloon bi-horaire is one schedule every day
    # (weekends and holidays included): off-peak 22-7 AND 11-17, peak
    # otherwise.
    assert is_offpeak(datetime(2026, 4, 29, 3, 0), "wallonia")  # night
    assert is_offpeak(datetime(2026, 4, 29, 12, 0), "wallonia")  # 11-17 window
    assert not is_offpeak(datetime(2026, 4, 29, 9, 0), "wallonia")  # 7-11 peak
    assert not is_offpeak(datetime(2026, 4, 29, 18, 0), "wallonia")  # 17-22 peak
    # Saturday follows the same slots - no all-weekend off-peak.
    assert not is_offpeak(datetime(2026, 5, 2, 9, 0), "wallonia")
    assert is_offpeak(datetime(2026, 5, 2, 12, 0), "wallonia")


# Wednesday 2026-04-29 is a non-holiday weekday for the boundary tests.
def test_tou_slot_weekday_morning_peak() -> None:
    assert tou_slot(datetime(2026, 4, 29, 7, 0)) == "peak"
    assert tou_slot(datetime(2026, 4, 29, 10, 59)) == "peak"


def test_tou_slot_weekday_midday_transition() -> None:
    assert tou_slot(datetime(2026, 4, 29, 11, 0)) == "transition"
    assert tou_slot(datetime(2026, 4, 29, 16, 59)) == "transition"


def test_tou_slot_weekday_evening_peak() -> None:
    assert tou_slot(datetime(2026, 4, 29, 17, 0)) == "peak"
    assert tou_slot(datetime(2026, 4, 29, 21, 59)) == "peak"


def test_tou_slot_weekday_late_night_transition() -> None:
    # 22h-1h is transition (Heures creuses), not offpeak — both
    # SmartFlex and Empower Flextime documents state this.
    assert tou_slot(datetime(2026, 4, 29, 22, 0)) == "transition"
    assert tou_slot(datetime(2026, 4, 29, 23, 59)) == "transition"
    assert tou_slot(datetime(2026, 4, 29, 0, 0)) == "transition"
    assert tou_slot(datetime(2026, 4, 29, 0, 59)) == "transition"


def test_tou_slot_weekday_morning_offpeak() -> None:
    # 1h-7h is offpeak (Heures super-creuses).
    assert tou_slot(datetime(2026, 4, 29, 1, 0)) == "offpeak"
    assert tou_slot(datetime(2026, 4, 29, 6, 59)) == "offpeak"


def test_tou_slot_weekend_offpeak_default() -> None:
    # Default weekend_offpeak: Sat/Sun is entirely off-peak.
    assert tou_slot(datetime(2026, 5, 2, 9, 0)) == "offpeak"
    assert tou_slot(datetime(2026, 5, 2, 19, 0)) == "offpeak"
    assert tou_slot(datetime(2026, 5, 3, 8, 0)) == "offpeak"


def test_tou_slot_holiday_treated_as_weekend_default_rule() -> None:
    # May 1, 2026 is a Friday (weekday 4) but is Labour Day. Under the
    # generic weekend_offpeak rule, holiday afternoon collapses to
    # offpeak just like a weekend.
    assert tou_slot(datetime(2026, 5, 1, 9, 0)) == "offpeak"
    assert tou_slot(datetime(2026, 5, 1, 19, 0)) == "offpeak"


def test_tou_slot_holiday_treated_as_weekend_no_peak_rule() -> None:
    # Same day under Engie's weekend_no_peak rule: 09:00 is transition
    # (would be peak on a non-holiday weekday), 13:00 is offpeak.
    rule = "weekend_no_peak"
    assert tou_slot(datetime(2026, 5, 1, 9, 0), rule) == "transition"
    assert tou_slot(datetime(2026, 5, 1, 13, 0), rule) == "offpeak"


def test_tou_slot_weekend_offpeak_at_hour_boundaries() -> None:
    """The generic weekend_offpeak rule maps every weekend hour to
    offpeak. Probe both Saturday and Sunday at the weekday boundaries
    (07:00, 11:00, 17:00, 22:00, 01:00) so a regression that flipped
    `<` to `<=` (or vice versa) on either edge surfaces here."""
    # Saturday 2026-05-02
    for hour in (0, 1, 6, 7, 10, 11, 16, 17, 21, 22, 23):
        assert tou_slot(datetime(2026, 5, 2, hour, 0)) == "offpeak", (
            f"weekend_offpeak Sat {hour:02d}:00"
        )
    # Sunday 2026-05-03
    for hour in (0, 1, 6, 7, 10, 11, 16, 17, 21, 22, 23):
        assert tou_slot(datetime(2026, 5, 3, hour, 0)) == "offpeak", (
            f"weekend_offpeak Sun {hour:02d}:00"
        )


def test_tou_slot_weekend_no_peak_at_hour_boundaries() -> None:
    """Engie Empower Flextime weekend_no_peak rule:
        transition : 07:00-11:00 + 17:00-01:00 (wraps midnight)
        offpeak    : 01:00-07:00 + 11:00-17:00

    Probe each boundary hour on both Saturday and Sunday."""
    rule = "weekend_no_peak"
    # Saturday boundary table: (hour, expected_slot)
    for day in (date(2026, 5, 2), date(2026, 5, 3)):  # Sat + Sun
        for hour, expected in [
            (0, "transition"),  # 17-01 wraps midnight
            (1, "offpeak"),  # 01:00 flips to offpeak
            (6, "offpeak"),
            (7, "transition"),  # 07:00 flips to transition (morning)
            (10, "transition"),
            (11, "offpeak"),  # 11:00 flips to offpeak
            (16, "offpeak"),
            (17, "transition"),  # 17:00 flips to transition (evening)
            (22, "transition"),
            (23, "transition"),
        ]:
            assert (
                tou_slot(datetime(day.year, day.month, day.day, hour, 0), rule)
                == expected
            ), f"weekend_no_peak {day} {hour:02d}:00"


def test_tou_slot_smartflex_seasonal_midday_is_seasonal() -> None:
    # SmartFlex super-creuses (offpeak) applies 11-17 only in spring/summer;
    # in autumn/winter that midday window is creuses (transition).
    rule = "smartflex_seasonal"
    assert tou_slot(datetime(2026, 7, 1, 13, 0), rule) == "offpeak"  # summer
    assert tou_slot(datetime(2026, 1, 15, 13, 0), rule) == "transition"  # winter


def test_tou_slot_smartflex_seasonal_overnight_always_creuses() -> None:
    # 22-07 is always creuses (transition) both seasons; a regression that
    # billed it offpeak would under-price the whole EV/heat-pump window.
    rule = "smartflex_seasonal"
    for month in (1, 7):  # winter + summer
        for hour in (0, 1, 3, 6, 22, 23):
            assert tou_slot(datetime(2026, month, 20, hour, 0), rule) == "transition", (
                f"smartflex overnight {month:02d} {hour:02d}:00"
            )


def test_tou_slot_smartflex_seasonal_peak_and_weekends() -> None:
    rule = "smartflex_seasonal"
    # Peak 07-11 + 17-22 both seasons.
    for month in (1, 7):
        for hour in (7, 10, 17, 21):
            assert tou_slot(datetime(2026, month, 15, hour, 0), rule) == "peak"
    # No weekend exception: a summer Saturday midday is still super-creuses.
    assert tou_slot(datetime(2026, 7, 4, 13, 0), rule) == "offpeak"  # Sat


def test_tou_slot_smartflex_seasonal_season_boundaries() -> None:
    # Season runs 21/03 (inclusive) to 20/09 (inclusive).
    rule = "smartflex_seasonal"
    assert tou_slot(datetime(2026, 3, 21, 13, 0), rule) == "offpeak"
    assert tou_slot(datetime(2026, 3, 20, 13, 0), rule) == "transition"
    assert tou_slot(datetime(2026, 9, 20, 13, 0), rule) == "offpeak"
    assert tou_slot(datetime(2026, 9, 21, 13, 0), rule) == "transition"


def test_dso_impact_band_does_not_observe_holidays() -> None:
    # Tarif Impact applies 7 days a week per CWaPE and is explicitly
    # NOT sensitive to weekends or holidays. May 1, 2026 17h is still
    # PIC.
    assert dso_impact_band(datetime(2026, 5, 1, 18, 0)) == "pic"
    assert dso_impact_band(datetime(2026, 12, 25, 18, 0)) == "pic"


def test_bihourly_energy_follows_impact_bands_under_impact_mode() -> None:
    # Under Impact comptage a bi-hourly energy rate must follow the CWaPE
    # bands (ECO -> off-peak rate, MEDIUM/PIC -> peak rate), not the plain
    # bi-horaire schedule, so energy stays aligned with the Impact-banded
    # distribution. The 22:00-01:00 window is MEDIUM (day) under Impact but
    # night under bi-horaire.
    e = FixedRates(single=0.2055, peak=0.2055, offpeak=0.1699)
    night_medium = datetime(2026, 4, 15, 23, 30)  # 22-01 MEDIUM / bi-horaire night
    eco_midday = datetime(2026, 4, 15, 13, 30)  # 11-17 ECO
    assert (
        energy_eur_per_kwh(e, night_medium, None, "bi", "wallonia", "impact") == 0.2055
    )
    assert (
        energy_eur_per_kwh(e, night_medium, None, "bi", "wallonia", "bi_horaire")
        == 0.1699
    )
    assert energy_eur_per_kwh(e, eco_midday, None, "bi", "wallonia", "impact") == 0.1699
    # A mono meter can't register bands, so Impact mode leaves it on the
    # single rate.
    assert energy_eur_per_kwh(e, night_medium, None, "mono", "wallonia", "impact") == (
        0.2055
    )


def test_tou_slot_weekend_no_peak_rule() -> None:
    # Engie Empower Flextime weekend rule:
    #   transition: 7-11 + 17-1 (so 17-22, 22-23, 0-1)
    #   offpeak:    1-7 + 11-17
    rule = "weekend_no_peak"
    # Saturday morning at 09:00: transition (would be peak on weekday).
    assert tou_slot(datetime(2026, 5, 2, 9, 0), rule) == "transition"
    # Saturday at 13:00: offpeak (weekend midday is offpeak under this rule).
    assert tou_slot(datetime(2026, 5, 2, 13, 0), rule) == "offpeak"
    # Saturday at 19:00: transition.
    assert tou_slot(datetime(2026, 5, 2, 19, 0), rule) == "transition"
    # Saturday at 23:30: transition (17-1 spans midnight).
    assert tou_slot(datetime(2026, 5, 2, 23, 30), rule) == "transition"
    # Saturday at 00:30: still transition (17-1 wraps).
    assert tou_slot(datetime(2026, 5, 2, 0, 30), rule) == "transition"
    # Saturday at 03:00: offpeak.
    assert tou_slot(datetime(2026, 5, 2, 3, 0), rule) == "offpeak"


def test_energy_tou_dispatches_by_slot() -> None:
    e = TimeOfUseRates(peak=0.30, transition=0.20, offpeak=0.10)
    assert energy_eur_per_kwh(e, datetime(2026, 4, 29, 9), None) == 0.30
    assert energy_eur_per_kwh(e, datetime(2026, 4, 29, 13), None) == 0.20
    assert energy_eur_per_kwh(e, datetime(2026, 4, 29, 5), None) == 0.10
    assert energy_eur_per_kwh(e, datetime(2026, 5, 2, 9), None) == 0.10  # weekend


def test_energy_tou_respects_weekend_no_peak() -> None:
    # Same rates, but the weekend_rule changes the slot picked at 09:00.
    e_off = TimeOfUseRates(
        peak=0.30, transition=0.20, offpeak=0.10, weekend_rule="weekend_offpeak"
    )
    e_no = TimeOfUseRates(
        peak=0.30, transition=0.20, offpeak=0.10, weekend_rule="weekend_no_peak"
    )
    sat_morning = datetime(2026, 5, 2, 9, 0)
    assert energy_eur_per_kwh(e_off, sat_morning, None) == 0.10  # offpeak
    assert energy_eur_per_kwh(e_no, sat_morning, None) == 0.20  # transition


def test_energy_fixed_single() -> None:
    e = FixedRates(single=0.20)
    assert energy_eur_per_kwh(e, datetime(2026, 4, 29, 12), None) == 0.20


def test_energy_fixed_bihourly_picks_offpeak() -> None:
    e = FixedRates(single=0.20, peak=0.22, offpeak=0.18)
    assert energy_eur_per_kwh(e, datetime(2026, 4, 29, 23), None, "bi") == 0.18
    assert energy_eur_per_kwh(e, datetime(2026, 4, 29, 12), None, "bi") == 0.22


def test_energy_fixed_on_smart_meter_bills_bihourly_split() -> None:
    # A smart (dynamic) meter registers peak/offpeak, so a fixed contract
    # on one bills the bi-hourly split when the card publishes it - it does
    # NOT degrade to the single rate (the module docstring used to say it
    # did). Same routing as a bi-hourly meter.
    e = FixedRates(single=0.20, peak=0.22, offpeak=0.18)
    assert energy_eur_per_kwh(e, datetime(2026, 4, 29, 23), None, "dynamic") == 0.18
    assert energy_eur_per_kwh(e, datetime(2026, 4, 29, 12), None, "dynamic") == 0.22
    # With no published split, it falls back to the single rate.
    single = FixedRates(single=0.20)
    assert (
        energy_eur_per_kwh(single, datetime(2026, 4, 29, 12), None, "dynamic") == 0.20
    )


def test_energy_variable_uses_current() -> None:
    e = VariableRates(current=0.139)
    assert energy_eur_per_kwh(e, datetime(2026, 4, 29, 12), None) == 0.139


def test_energy_dynamic_combines_factor_base_and_spot() -> None:
    e = DynamicRates(factor=0.10, base=0.025)
    assert energy_eur_per_kwh(e, datetime(2026, 4, 29, 12), 0.10) == pytest.approx(
        0.035
    )


def test_energy_dynamic_requires_spot() -> None:
    e = DynamicRates(factor=0.10, base=0.025)
    with pytest.raises(ValueError):
        energy_eur_per_kwh(e, datetime(2026, 4, 29, 12), None)


def test_dso_impact_band_pic_evening() -> None:
    assert dso_impact_band(datetime(2026, 4, 29, 17, 0)) == "pic"
    assert dso_impact_band(datetime(2026, 4, 29, 21, 59)) == "pic"


def test_dso_impact_band_medium_morning_and_late_night() -> None:
    assert dso_impact_band(datetime(2026, 4, 29, 7, 0)) == "medium"
    assert dso_impact_band(datetime(2026, 4, 29, 10, 59)) == "medium"
    assert dso_impact_band(datetime(2026, 4, 29, 22, 0)) == "medium"
    assert dso_impact_band(datetime(2026, 4, 29, 23, 59)) == "medium"
    assert dso_impact_band(datetime(2026, 4, 29, 0, 30)) == "medium"


def test_dso_impact_band_eco_night_and_midday() -> None:
    assert dso_impact_band(datetime(2026, 4, 29, 1, 0)) == "eco"
    assert dso_impact_band(datetime(2026, 4, 29, 6, 59)) == "eco"
    assert dso_impact_band(datetime(2026, 4, 29, 11, 0)) == "eco"
    assert dso_impact_band(datetime(2026, 4, 29, 16, 59)) == "eco"


def test_dso_impact_band_no_weekend_exception() -> None:
    # Tarif Impact applies 7 days a week (unlike bi-horaire). A Saturday
    # 17h-22h block is still PIC.
    assert dso_impact_band(datetime(2026, 5, 2, 18, 0)) == "pic"


def test_energy_impact_routes_through_band() -> None:
    rates = ImpactRates(pic=0.18, medium=0.15, eco=0.10)
    # 19:00 sits inside the PIC window (17-22).
    assert energy_eur_per_kwh(rates, datetime(2026, 4, 29, 19, 0), None) == 0.18
    # 09:00 sits inside the MEDIUM window (07-11).
    assert energy_eur_per_kwh(rates, datetime(2026, 4, 29, 9, 0), None) == 0.15
    # 03:00 sits inside the ECO window (01-07).
    assert energy_eur_per_kwh(rates, datetime(2026, 4, 29, 3, 0), None) == 0.10
    # 14:00 ECO via the 11-17 second half.
    assert energy_eur_per_kwh(rates, datetime(2026, 4, 29, 14, 0), None) == 0.10
    # Weekend keeps the same schedule (no weekend exception).
    assert energy_eur_per_kwh(rates, datetime(2026, 5, 2, 18, 0), None) == 0.18


def test_network_impact_dispatches_by_band() -> None:
    overlay = DsoOverlay(
        distribution_single=0.05,
        distribution_peak=0.06,
        distribution_offpeak=0.04,
        transport=0.015,
        distribution_pic=0.10,
        distribution_medium=0.07,
        distribution_eco=0.03,
    )
    pic = network_eur_per_kwh(overlay, datetime(2026, 4, 29, 18), "dynamic", "impact")
    medium = network_eur_per_kwh(overlay, datetime(2026, 4, 29, 8), "dynamic", "impact")
    eco = network_eur_per_kwh(overlay, datetime(2026, 4, 29, 13), "dynamic", "impact")
    assert pic == pytest.approx(0.115)  # 0.10 + 0.015 transport
    assert medium == pytest.approx(0.085)
    assert eco == pytest.approx(0.045)
    # An exclusive-night meter bills its dedicated circuit rate even when
    # the main connection opted into the Impact tariff -- the
    # exclusive-night branch must take precedence over the Impact bands.
    overlay_excl = DsoOverlay(
        distribution_single=0.05,
        distribution_offpeak=0.04,
        distribution_exclusive_night=0.02,
        transport=0.015,
        distribution_pic=0.10,
        distribution_medium=0.07,
        distribution_eco=0.03,
    )
    excl = network_eur_per_kwh(
        overlay_excl, datetime(2026, 4, 29, 18), "exclusive_night", "impact"
    )
    assert excl == pytest.approx(
        0.035
    )  # 0.02 exclusive-night + 0.015, not the pic band


def test_network_impact_falls_back_when_dso_lacks_impact_rates() -> None:
    # Brussels Sibelga / Flanders Fluvius don't publish Impact rates.
    # Asking for "impact" mode there must degrade gracefully — fall back
    # to bi-horaire if peak/offpeak exist, else single. No KeyError.
    overlay = DsoOverlay(
        distribution_single=0.05,
        distribution_peak=0.06,
        distribution_offpeak=0.04,
        transport=0.015,
    )
    # Mid-day on a weekday with bi meter: same as bi_horaire peak path.
    assert network_eur_per_kwh(
        overlay, datetime(2026, 4, 29, 12), "bi", "impact"
    ) == pytest.approx(0.075)


def test_network_simple_mode_ignores_meter() -> None:
    overlay = DsoOverlay(
        distribution_single=0.05,
        distribution_peak=0.06,
        distribution_offpeak=0.04,
        transport=0.015,
    )
    # Even with bi meter at night, "simple" mode forces the single rate.
    assert network_eur_per_kwh(
        overlay, datetime(2026, 4, 29, 23), "bi", "simple"
    ) == pytest.approx(0.065)


def test_network_single_meter() -> None:
    overlay = DsoOverlay(
        distribution_single=0.05,
        distribution_peak=0.06,
        distribution_offpeak=0.04,
        transport=0.015,
    )
    assert network_eur_per_kwh(overlay, datetime(2026, 4, 29, 12)) == pytest.approx(
        0.065
    )


def test_network_bihourly_at_night() -> None:
    overlay = DsoOverlay(
        distribution_single=0.05,
        distribution_peak=0.06,
        distribution_offpeak=0.04,
        transport=0.015,
    )
    assert network_eur_per_kwh(
        overlay, datetime(2026, 4, 29, 23), "bi"
    ) == pytest.approx(0.055)


def test_network_dynamic_meter_with_bi_horaire_dso_uses_band_rate() -> None:
    """Walloon SMR3 customers can pick bi-horaire DSO billing alongside
    a dynamic energy contract; the distribution side must pick up the
    peak / offpeak split rather than collapsing to the single rate."""
    overlay = DsoOverlay(
        distribution_single=0.05,
        distribution_peak=0.06,
        distribution_offpeak=0.04,
        transport=0.015,
    )
    # 23:00 weekday is offpeak under bi-horaire.
    assert network_eur_per_kwh(
        overlay, datetime(2026, 4, 29, 23), "dynamic"
    ) == pytest.approx(0.055)


def test_network_dynamic_meter_with_simple_dso_uses_single_rate() -> None:
    """A dynamic meter on a 'simple' DSO contract collapses to single
    distribution -- that is the supplier's intent."""
    overlay = DsoOverlay(
        distribution_single=0.05,
        distribution_peak=0.06,
        distribution_offpeak=0.04,
        transport=0.015,
    )
    assert network_eur_per_kwh(
        overlay, datetime(2026, 4, 29, 23), "dynamic", "simple"
    ) == pytest.approx(0.065)


def test_compute_breakdown_meter_bi_picks_offpeak_at_night() -> None:
    snap = _snapshot(FixedRates(single=0.20, peak=0.22, offpeak=0.18), vat=0.0)
    night = compute_breakdown(
        snap, "fluvius", "flanders", datetime(2026, 4, 29, 23), meter="bi"
    )
    day = compute_breakdown(
        snap, "fluvius", "flanders", datetime(2026, 4, 29, 12), meter="bi"
    )
    assert night.energy == 0.18
    assert day.energy == 0.22


def test_taxes_brussels_uses_brussels_renewables_only() -> None:
    # A Brussels entry must NOT pick up the Flemish/Walloon rate, even if
    # both are set; only its own brussels_renewables.
    t = TaxOverlay(
        federal_excise=0.05,
        energy_contribution=0.002,
        flanders_renewables=0.015,
        wallonia_renewables=0.0313,
        brussels_renewables=0.0265,
    )
    assert taxes_eur_per_kwh(t, "brussels") == pytest.approx(0.05 + 0.002 + 0.0265)


def test_taxes_wallonia_includes_connection_and_wallonia_renewables() -> None:
    t = TaxOverlay(
        federal_excise=0.05,
        energy_contribution=0.002,
        flanders_renewables=0.015,
        wallonia_renewables=0.0313,
        region_connection_fee=0.00075,
    )
    assert taxes_eur_per_kwh(t, "wallonia") == pytest.approx(
        0.05 + 0.002 + 0.0313 + 0.00075
    )


def test_taxes_flanders_uses_flanders_renewables_only() -> None:
    # Flanders entry must NOT pick up the Wallonia rate, even if both are set.
    t = TaxOverlay(
        federal_excise=0.05,
        energy_contribution=0.002,
        flanders_renewables=0.0152,
        wallonia_renewables=0.0313,
    )
    assert taxes_eur_per_kwh(t, "flanders") == pytest.approx(0.05 + 0.002 + 0.0152)


def test_compute_breakdown_with_vat_inclusive_snapshot() -> None:
    snap = _snapshot(FixedRates(single=0.18), vat=0.0)
    bd = compute_breakdown(snap, "fluvius", "flanders", datetime(2026, 4, 29, 12))
    assert bd.energy == 0.18
    assert bd.network == pytest.approx(0.065)
    assert bd.taxes == pytest.approx(0.067)
    assert bd.all_in == pytest.approx(0.18 + 0.065 + 0.067)


def test_compute_breakdown_with_vat_exclusive_snapshot() -> None:
    snap = _snapshot(FixedRates(single=0.18), vat=0.06)
    bd = compute_breakdown(snap, "fluvius", "flanders", datetime(2026, 4, 29, 12))
    expected = (0.18 + 0.065 + 0.067) * 1.06
    assert bd.all_in == pytest.approx(expected)
    # VAT must spread across every component, not get lumped into taxes.
    assert bd.energy == pytest.approx(0.18 * 1.06)
    assert bd.network == pytest.approx(0.065 * 1.06)
    assert bd.taxes == pytest.approx(0.067 * 1.06)
    # And the components must always sum to all_in to the cent.
    assert bd.energy + bd.network + bd.taxes == pytest.approx(bd.all_in)


def test_compute_breakdown_unknown_dso_raises() -> None:
    snap = _snapshot(FixedRates(single=0.18))
    with pytest.raises(KeyError):
        compute_breakdown(snap, "missing_dso", "flanders", datetime(2026, 4, 29, 12))


def test_exclusive_night_routes_through_supplier_exclusive_night_rate() -> None:
    """An exclusive-night meter circuit (electric water heater /
    night-storage heater) bills its energy at the supplier's published
    exclusive_night rate. compute_breakdown must pick it up regardless
    of hour-of-day -- the meter physically only registers during DSO
    off-peak hours, so we don't gate on is_offpeak() inside the energy
    helper."""
    snap = _snapshot(
        FixedRates(single=0.18, peak=0.20, offpeak=0.16, exclusive_night=0.10)
    )
    # Pick a peak hour to make sure the call doesn't fall through to
    # the bi-hourly peak/offpeak branch.
    when = datetime(2026, 4, 29, 12, 0)
    energy = energy_eur_per_kwh(snap.energy, when, None, meter="exclusive_night")
    assert energy == pytest.approx(0.10)


def test_exclusive_night_distribution_falls_back_to_offpeak_rate() -> None:
    """The DSO overlay doesn't (yet) expose an exclusive_night
    distribution column; route through distribution_offpeak when
    published, distribution_single otherwise. Either way is closer to
    the real bill than always charging at the day rate."""
    overlay = DsoOverlay(
        distribution_single=0.05,
        distribution_peak=0.06,
        distribution_offpeak=0.04,
        transport=0.015,
    )
    # Daytime hour - exclusive-night still routes to offpeak.
    network = network_eur_per_kwh(
        overlay, datetime(2026, 4, 29, 12, 0), "exclusive_night"
    )
    assert network == pytest.approx(0.04 + 0.015)

    # DSO without a peak/offpeak split: falls back to single.
    overlay_mono = DsoOverlay(distribution_single=0.05, transport=0.015)
    network_mono = network_eur_per_kwh(
        overlay_mono, datetime(2026, 4, 29, 12, 0), "exclusive_night"
    )
    assert network_mono == pytest.approx(0.05 + 0.015)


def test_yearly_fixed_fee_selects_exclusive_night_when_published() -> None:
    energy = VariableRates(
        current=0.16, yearly_fixed_fee=85.0, yearly_fixed_fee_exclusive_night=35.04
    )
    # Exclusive-night meter gets the dedicated fee; every other meter gets
    # the standard one.
    assert yearly_fixed_fee_for_meter(energy, "exclusive_night") == pytest.approx(35.04)
    assert yearly_fixed_fee_for_meter(energy, "mono") == pytest.approx(85.0)
    assert yearly_fixed_fee_for_meter(energy, "bi") == pytest.approx(85.0)


def test_yearly_fixed_fee_falls_back_when_no_dedicated_exclusive_night() -> None:
    # No dedicated fee published -> the standard fee applies to all meters.
    energy = FixedRates(single=0.20, yearly_fixed_fee=70.0)
    assert yearly_fixed_fee_for_meter(energy, "exclusive_night") == pytest.approx(70.0)
    assert yearly_fixed_fee_for_meter(energy, "mono") == pytest.approx(70.0)


def test_wallonia_midday_offpeak_only_applies_from_2026() -> None:
    """Wallonia's uniform schedule (which made 11:00-17:00 off-peak every day)
    started on 2026-01-01; before it the region billed the same classic
    bi-hourly schedule as Flanders. No automatic path reaches a pre-2026 hour,
    but backfill_statistics takes a user-supplied start with no lower bound, so
    a manual backfill into 2025 must not re-band six hours of every day onto a
    tariff that was not in force yet."""
    for year, midday_offpeak in ((2024, False), (2025, False), (2026, True)):
        noon = datetime(year, 6, 18, 12, 0)
        assert is_offpeak(noon, "wallonia") is midday_offpeak
    # The night window is unchanged on both sides of the boundary, and the
    # pre-2026 schedule still follows the weekend rule it shared with Flanders.
    for year in (2025, 2026):
        assert is_offpeak(datetime(year, 6, 18, 3, 0), "wallonia")
        assert is_offpeak(datetime(year, 6, 21, 12, 0), "wallonia")

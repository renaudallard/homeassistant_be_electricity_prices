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

"""All-in price formula given a supplier snapshot, a DSO and a region.

Pure functions, no Home Assistant dependencies.

Meter types follow the Belgian convention:

  - ``mono``    - single-rate meter (compteur simple / enkelvoudige meter).
                  Energy and distribution billed at the supplier's single rate.
  - ``bi``      - bi-hourly meter (compteur bi-horaire / tweevoudige meter).
                  Day rate weekdays 07:00-22:00, night rate the rest of the
                  time and full weekends.
  - ``dynamic`` - smart meter (digitale meter) capable of hourly readings.
                  For dynamic contracts, energy is computed as
                  ``factor x spot + base`` per hour. A smart meter also
                  registers peak/offpeak, so a fixed or variable contract
                  on one bills the bi-hourly split when the card publishes
                  it, falling back to the single rate only when it does
                  not (same routing as a bi-hourly meter).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Final, Literal

from .const import (
    REGION_BRUSSELS,
    REGION_FLANDERS,
    REGION_WALLONIA,
    RESOLUTION_QUARTER,
)
from .providers.base import (
    DsoOverlay,
    DynamicRates,
    EnergyRates,
    FixedRates,
    ImpactRates,
    SpotMonthlyRates,
    SupplierSnapshot,
    TaxOverlay,
    TimeOfUseRates,
    VariableRates,
)

MeterType = Literal["mono", "bi", "dynamic", "exclusive_night"]


@dataclass(frozen=True)
class PriceBreakdown:
    """All-in EUR/kWh decomposition for a single hour."""

    energy: float
    network: float
    taxes: float
    all_in: float


def slots_per_hour(resolution: str) -> int:
    """Price slots per clock hour for a grid resolution."""
    return 4 if resolution == RESOLUTION_QUARTER else 1


def slot_delta(resolution: str) -> timedelta:
    """Width of one price slot."""
    if resolution == RESOLUTION_QUARTER:
        return timedelta(minutes=15)
    return timedelta(hours=1)


def slot_start(when: datetime, resolution: str) -> datetime:
    """Truncate ``when`` down to the start of its price slot.

    Hourly grids truncate to :00; quarter-hour grids to the :00 / :15 /
    :30 / :45 boundary at or below ``when``.
    """
    when = when.replace(second=0, microsecond=0)
    if resolution == RESOLUTION_QUARTER:
        return when.replace(minute=(when.minute // 15) * 15)
    return when.replace(minute=0)


# Federal fixed-date holidays (month, day). Lifted to module scope so
# is_belgian_holiday doesn't reallocate the set on every call along the
# 8760-iteration backfill path and the per-hour TOU slot lookup.
_FIXED_HOLIDAYS: Final[frozenset[tuple[int, int]]] = frozenset(
    {(1, 1), (5, 1), (7, 21), (8, 15), (11, 1), (11, 11), (12, 25)}
)


def _easter_sunday(year: int) -> date:
    """Anonymous Gregorian computus for Western (Catholic) Easter Sunday."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    L = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * L) // 451
    month = (h + L - 7 * m + 114) // 31
    day = ((h + L - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def is_belgian_holiday(d: date) -> bool:
    """Federal Belgian public holidays.

    Fixed dates: New Year (1/1), Labour Day (1/5), National Day (21/7),
    Assumption (15/8), All Saints (1/11), Armistice (11/11), Christmas
    (25/12). Easter-derived: Easter Monday (+1), Ascension (+39),
    Pentecost Monday (+50). Regional holidays (Walloon, Flemish,
    Brussels) are deliberately excluded — DSO billing applies federal
    rules uniformly.
    """
    if (d.month, d.day) in _FIXED_HOLIDAYS:
        return True
    easter = _easter_sunday(d.year)
    if d == easter + timedelta(days=1):  # Easter Monday
        return True
    if d == easter + timedelta(days=39):  # Ascension (Thursday)
        return True
    if d == easter + timedelta(days=50):  # Pentecost Monday
        return True
    return False


def is_offpeak(when: datetime, region: str = REGION_FLANDERS) -> bool:
    """Classic bi-hourly (tweevoudig / bi-horaire) off-peak hour, by region.

    The night/weekend schedule and the public-holiday treatment differ by
    region, and Wallonia changed on 2026-01-01:

      Flanders (Fluvius): off-peak Mon-Fri 22:00-07:00 and all weekend.
        Weekday public holidays bill at the DAY rate - the meter's tariff
        clock switches on weekday/weekend only, not on holidays.
      Brussels (Sibelga): off-peak 22:00-07:00, all weekend AND weekday
        public holidays (the historical Brussels exception; unchanged).
      Wallonia (from 2026-01-01): one uniform schedule every day including
        weekends and holidays - off-peak 22:00-07:00 and 11:00-17:00.
    """
    if region == REGION_WALLONIA:
        return when.hour < 7 or when.hour >= 22 or 11 <= when.hour < 17
    if when.weekday() >= 5:
        return True
    if region == REGION_BRUSSELS and is_belgian_holiday(when.date()):
        return True
    return when.hour < 7 or when.hour >= 22


TouSlot = Literal["peak", "transition", "offpeak"]


def _is_smartflex_summer(d: date) -> bool:
    """SmartFlex's spring/summer season: 21 March to 20 September inclusive."""
    return (3, 21) <= (d.month, d.day) <= (9, 20)


def tou_slot(when: datetime, weekend_rule: str = "weekend_offpeak") -> TouSlot:
    """Map a local datetime to its Belgian TOU slot.

    Weekday rule (shared across products):
      peak       : 07:00-11:00 + 17:00-22:00
      transition : 11:00-17:00 + 22:00-01:00
      offpeak    : 01:00-07:00

    Federal Belgian holidays follow the same rule as a weekend day —
    the supplier's published TOU bands explicitly call out weekends
    plus public holidays (TGEPRESC for Engie, equivalent CWaPE
    document for Luminus). Weekend rule depends on the contract:

      weekend_offpeak  Sat/Sun + holidays all off-peak (generic CWaPE
        default).
      weekend_no_peak  Engie Empower Flextime — never peak;
        transition 07:00-11:00 + 17:00-01:00,
        offpeak    01:00-07:00 + 11:00-17:00.
      smartflex_seasonal  Luminus SmartFlex — seasonal bands applied
        every day (the card lists no weekend exception; the "free
        Sundays" promo is a first-year discount, out of scope):
        peak 07:00-11:00 + 17:00-22:00 both seasons; the 11:00-17:00
        midday window is super-creuses (offpeak) only in spring/summer
        (21/03-20/09) and creuses (transition) otherwise; 22:00-07:00
        is always creuses (transition).
    """
    h = when.hour
    if weekend_rule == "smartflex_seasonal":
        if 7 <= h < 11 or 17 <= h < 22:
            return "peak"
        if 11 <= h < 17:
            return "offpeak" if _is_smartflex_summer(when.date()) else "transition"
        return "transition"
    if when.weekday() >= 5 or is_belgian_holiday(when.date()):
        if weekend_rule == "weekend_no_peak":
            if 7 <= h < 11 or h >= 17 or h < 1:
                return "transition"
            return "offpeak"  # 1-7 + 11-17
        return "offpeak"  # weekend_offpeak: whole weekend is off-peak
    # Weekday
    if 1 <= h < 7:
        return "offpeak"
    if 7 <= h < 11 or 17 <= h < 22:
        return "peak"
    return "transition"  # 11-17 + 22-1


def _routed_rate(
    base: float,
    energy: FixedRates | VariableRates,
    when: datetime,
    meter: MeterType,
    region: str,
    *,
    bi_capable: bool,
    dso_tariff_mode: DsoTariffMode = "bi_horaire",
) -> float:
    """Meter-routing shared by the Fixed and Variable energy branches.

    Exclusive-night meters take the dedicated rate when published; a
    bi-hourly-capable meter takes the peak/off-peak split when published;
    otherwise the single/current ``base`` rate applies.

    Under Impact comptage the SMR3 meter registers consumption in the CWaPE
    bands, so a bi-hourly energy rate follows those bands rather than the
    plain bi-horaire schedule: the ECO band bills the off-peak rate and the
    MEDIUM/PIC bands the peak rate (per the supplier cards' Impact footnote).
    This keeps the energy side aligned with the Impact-banded distribution.
    """
    if meter == "exclusive_night" and energy.exclusive_night is not None:
        return energy.exclusive_night
    if bi_capable and energy.peak is not None and energy.offpeak is not None:
        if dso_tariff_mode == "impact":
            return energy.offpeak if dso_impact_band(when) == "eco" else energy.peak
        return energy.offpeak if is_offpeak(when, region) else energy.peak
    return base


def energy_eur_per_kwh(
    energy: EnergyRates,
    when: datetime,
    spot_eur_per_kwh: float | None,
    meter: MeterType = "mono",
    region: str = REGION_FLANDERS,
    dso_tariff_mode: DsoTariffMode = "bi_horaire",
) -> float:
    """Return the energy component in EUR/kWh for the given hour.

    bi-hourly and SMR3 (digital) meters both register peak/offpeak,
    so they share the bi-horaire branch when the supplier publishes
    the split. ``meter == "exclusive_night"`` routes through the
    supplier's ``exclusive_night`` rate when published; the meter
    physically only registers during DSO off-peak hours, so we don't
    need to gate by ``is_offpeak(when)`` here. ``dso_tariff_mode`` lets a
    bi-hourly rate follow the CWaPE Impact bands under Impact comptage.
    """
    bi_capable = meter in ("bi", "dynamic")
    if isinstance(energy, FixedRates):
        return _routed_rate(
            energy.single,
            energy,
            when,
            meter,
            region,
            bi_capable=bi_capable,
            dso_tariff_mode=dso_tariff_mode,
        )
    if isinstance(energy, VariableRates):
        return _routed_rate(
            energy.current,
            energy,
            when,
            meter,
            region,
            bi_capable=bi_capable,
            dso_tariff_mode=dso_tariff_mode,
        )
    if isinstance(energy, DynamicRates):
        if spot_eur_per_kwh is None:
            raise ValueError("dynamic tariff needs a spot price")
        return energy.factor * spot_eur_per_kwh + energy.base
    if isinstance(energy, SpotMonthlyRates):
        # The caller passes the delivery month's mean spot in place of the
        # live slot price, so the flat monthly rate is the same pure formula.
        if spot_eur_per_kwh is None:
            raise ValueError("spot-monthly tariff needs a monthly mean spot")
        return energy.factor * spot_eur_per_kwh + energy.base
    if isinstance(energy, TimeOfUseRates):
        slot = tou_slot(when, energy.weekend_rule)
        if slot == "peak":
            return energy.peak
        if slot == "transition":
            return energy.transition
        return energy.offpeak
    if isinstance(energy, ImpactRates):
        band = dso_impact_band(when)
        if band == "pic":
            return energy.pic
        if band == "medium":
            return energy.medium
        return energy.eco
    raise TypeError(f"unknown energy rates type: {type(energy).__name__}")


StaticBand = Literal["single", "peak", "offpeak"]


def static_energy_eur_per_kwh(energy: EnergyRates, band: StaticBand) -> float | None:
    """Stable (no time-of-day) energy rate for a given band.

    Used by ``static_breakdown`` to compute the all-in rate plugged into
    the current_year_cost sensor. Returns ``None`` for DynamicRates (no
    constant rate exists), TimeOfUseRates (3-band schema doesn't map
    onto the bi-hourly meter convention), and ImpactRates (per-band
    rates vary by hour-of-day; caller must go through the hourly path).
    Falls back to the single rate when the requested peak/offpeak band
    has no published value (mono-only rate sheet).
    """
    if isinstance(energy, FixedRates):
        if band == "single":
            return energy.single
        if band == "peak":
            return energy.peak if energy.peak is not None else energy.single
        return energy.offpeak if energy.offpeak is not None else energy.single
    if isinstance(energy, VariableRates):
        if band == "single":
            return energy.current
        if band == "peak":
            return energy.peak if energy.peak is not None else energy.current
        return energy.offpeak if energy.offpeak is not None else energy.current
    return None


def yearly_fixed_fee_for_meter(energy: EnergyRates, meter: MeterType) -> float:
    """Supplier yearly fixed fee in EUR for the configured meter.

    Exclusive-night circuits are configured as a separate entry; when the
    card prints a dedicated 'exclusief nacht' fixed fee (EBEM Groen
    Variabel) bill that on the exclusive-night meter instead of the
    standard one. Every other meter, and any contract without a dedicated
    fee, uses the standard ``yearly_fixed_fee``.
    """
    standard: float = getattr(energy, "yearly_fixed_fee", 0.0)
    if meter == "exclusive_night":
        excl = getattr(energy, "yearly_fixed_fee_exclusive_night", None)
        if excl is not None:
            return float(excl)
    return standard


def static_breakdown(
    snapshot: SupplierSnapshot,
    dso_key: str,
    region: str,
    band: StaticBand,
    dso_tariff_mode: DsoTariffMode = "bi_horaire",
) -> PriceBreakdown | None:
    """All-in EUR/kWh for the static rate sheet of one band.

    Returns ``None`` when the contract has no stable rate (dynamic,
    TOU) or when the user is on the Wallonia ``impact`` DSO tariff
    (distribution varies by hour-of-day, so the YTD path must read
    hourly statistics rather than per-day totals). Falls back to the
    single-rate distribution when the DSO didn't publish a
    peak/offpeak split or when the user picked ``simple`` DSO billing.
    VAT applies uniformly to each component, mirroring
    :func:`compute_breakdown`.
    """
    energy = static_energy_eur_per_kwh(snapshot.energy, band)
    if energy is None:
        return None
    overlay = snapshot.dsos.get(dso_key)
    if overlay is None:
        raise KeyError(
            f"DSO {dso_key!r} not in snapshot for "
            f"{snapshot.supplier}/{snapshot.contract}; "
            f"available: {sorted(snapshot.dsos)}"
        )
    if dso_tariff_mode == "impact" and overlay.distribution_pic is not None:
        # Impact distribution differs per CWaPE band (PIC / MEDIUM /
        # ECO) and can't collapse to single/peak/offpeak; the caller
        # must route through the per-hour path.
        return None
    if dso_tariff_mode == "simple":
        dist = overlay.distribution_single
    elif band == "peak" and overlay.distribution_peak is not None:
        dist = overlay.distribution_peak
    elif band == "offpeak" and overlay.distribution_offpeak is not None:
        dist = overlay.distribution_offpeak
    else:
        dist = overlay.distribution_single
    network = dist + overlay.transport
    taxes = taxes_eur_per_kwh(snapshot.taxes, region)
    vat_factor = 1.0 + snapshot.taxes.vat_rate
    # Apply VAT per component then sum, so "energy + network + taxes ==
    # all_in" holds bit-for-bit, matching compute_breakdown; (e+n+t)*vat
    # would diverge by sub-femto-euro rounding once vat_rate is non-zero.
    energy_v = energy * vat_factor
    network_v = network * vat_factor
    taxes_v = taxes * vat_factor
    return PriceBreakdown(
        energy=energy_v,
        network=network_v,
        taxes=taxes_v,
        all_in=energy_v + network_v + taxes_v,
    )


DsoTariffMode = Literal["simple", "bi_horaire", "impact"]
ImpactBand = Literal["pic", "medium", "eco"]


def dso_impact_band(when: datetime) -> ImpactBand:
    """Return the Wallonia Tarif Impact band for the given local hour.

    CWaPE-defined bands (every day of the week, no weekend exception):
      pic    17:00-22:00
      medium 07:00-11:00 + 22:00-01:00
      eco    01:00-07:00 + 11:00-17:00

    Source: TotalEnergies Impact tariff card footnote 7 / ORES
    'Comprendre ma facture / Impact'.
    """
    h = when.hour
    if 17 <= h < 22:
        return "pic"
    if 7 <= h < 11 or h >= 22 or h < 1:
        return "medium"
    return "eco"  # 01-07 + 11-17


def network_eur_per_kwh(
    dso: DsoOverlay,
    when: datetime,
    meter: MeterType = "mono",
    dso_tariff_mode: DsoTariffMode = "bi_horaire",
    region: str = REGION_FLANDERS,
) -> float:
    """Distribution + transport (EUR/kWh) for the given hour.

    ``dso_tariff_mode`` selects the distribution-side billing mode set
    on the user's connection, separately from the supplier's energy
    tariff. ``impact`` falls back to bi-horaire if the DSO doesn't
    publish Impact rates (Brussels Sibelga, Flanders Fluvius), and
    ``bi_horaire`` falls back to the single rate when the DSO doesn't
    publish a peak/offpeak split. ``meter`` decides whether the meter
    can register a peak/offpeak split: bi-hourly meters and digital
    (SMR3) meters can; mono meters cannot. ``exclusive_night`` meters
    only run during DSO off-peak hours and bill at the dedicated
    exclusive-night distribution rate when published, falling back to
    the off-peak rate, then to the single rate on DSOs that expose
    neither. That circuit is resolved before the Impact band, so a
    dedicated night meter bills at its own rate even under Impact mode.
    """
    if meter == "exclusive_night":
        # Exclusive-night meters physically only register during DSO
        # off-peak hours and DSOs publish a dedicated distribution
        # rate for the circuit. This dedicated circuit bills at its own
        # rate regardless of the main connection's Impact opt-in, so it
        # is checked BEFORE the Impact band branch. Prefer the
        # exclusive-night rate when the extractor parses it, fall back
        # to the off-peak rate, then to the single rate -- each
        # fall-back step is closer to the real bill than the day rate.
        if dso.distribution_exclusive_night is not None:
            dist = dso.distribution_exclusive_night
        elif dso.distribution_offpeak is not None:
            dist = dso.distribution_offpeak
        else:
            dist = dso.distribution_single
        return dist + dso.transport
    if (
        dso_tariff_mode == "impact"
        and dso.distribution_pic is not None
        and dso.distribution_medium is not None
        and dso.distribution_eco is not None
    ):
        # Every parser binds the Impact triplet (pic / medium / eco)
        # together when it sets pic, so under normal operation a
        # non-None pic implies the other two are populated. Asserting
        # that here used to be enough, but Python -O strips asserts
        # and would let through a TypeError on ``None + transport``.
        # Treat the Impact mode as "available only when all three are
        # set" and fall through to the bi-horaire / single path
        # otherwise -- same end behaviour the per-DSO data drives on
        # Brussels Sibelga / Flanders Fluvius cards that don't publish
        # Impact rates at all.
        band = dso_impact_band(when)
        if band == "pic":
            dist = dso.distribution_pic
        elif band == "medium":
            dist = dso.distribution_medium
        else:
            dist = dso.distribution_eco
        return dist + dso.transport
    if (
        dso_tariff_mode != "simple"
        and meter in ("bi", "dynamic")
        and dso.distribution_peak is not None
        and dso.distribution_offpeak is not None
    ):
        dist = (
            dso.distribution_offpeak
            if is_offpeak(when, region)
            else dso.distribution_peak
        )
    else:
        dist = dso.distribution_single
    return dist + dso.transport


def taxes_eur_per_kwh(taxes: TaxOverlay, region: str) -> float:
    """Per-kWh levies for the configured region."""
    out = taxes.federal_excise + taxes.energy_contribution
    if region == REGION_WALLONIA:
        out += taxes.region_connection_fee + taxes.wallonia_renewables
    elif region == REGION_FLANDERS:
        out += taxes.flanders_renewables
    elif region == REGION_BRUSSELS:
        out += taxes.brussels_renewables
    return out


def compute_breakdown(
    snapshot: SupplierSnapshot,
    dso_key: str,
    region: str,
    when: datetime,
    spot_eur_per_kwh: float | None = None,
    meter: MeterType = "mono",
    dso_tariff_mode: DsoTariffMode = "bi_horaire",
) -> PriceBreakdown:
    """Return the all-in EUR/kWh breakdown for one hour.

    Each component (energy, network, taxes) is reported VAT-inclusive,
    so ``energy + network + taxes == all_in`` exactly. With the current
    convention ``vat_rate = 0.0`` (snapshots already parse VAT-incl
    numbers) the multiplier is 1.0 and components match what the PDF
    prints. If a future extractor parses ex-VAT numbers and sets
    ``vat_rate = 0.06``, VAT applies uniformly to each component
    instead of being rolled into the taxes component.
    """
    overlay = snapshot.dsos.get(dso_key)
    if overlay is None:
        raise KeyError(
            f"DSO {dso_key!r} not in snapshot for "
            f"{snapshot.supplier}/{snapshot.contract}; "
            f"available: {sorted(snapshot.dsos)}"
        )
    energy = energy_eur_per_kwh(
        snapshot.energy, when, spot_eur_per_kwh, meter, region, dso_tariff_mode
    )
    network = network_eur_per_kwh(overlay, when, meter, dso_tariff_mode, region)
    taxes = taxes_eur_per_kwh(snapshot.taxes, region)
    vat_factor = 1.0 + snapshot.taxes.vat_rate
    # Apply VAT to each component first, then sum, so the invariant
    # "energy + network + taxes == all_in" holds bit-for-bit even when
    # vat_rate becomes non-zero in a future extractor that parses
    # ex-VAT prices. Computing all_in as (e+n+t)*vat would diverge from
    # the per-component sum by sub-femto-euro rounding error.
    energy_v = energy * vat_factor
    network_v = network * vat_factor
    taxes_v = taxes * vat_factor
    return PriceBreakdown(
        energy=energy_v,
        network=network_v,
        taxes=taxes_v,
        all_in=energy_v + network_v + taxes_v,
    )

# Pricing model

This document covers the pricing engine: the pure functions in `pricing.py` that
turn a supplier snapshot, a DSO overlay and a region into an all-in EUR/kWh
breakdown for a given hour, plus the injection (feed-in), capacity and prosumer
math that surrounds it in `coordinator.py`. It is the reference for how each
Belgian contract kind resolves to an energy rate, how the network and tax layers
add up, how VAT is (or is not) applied, and how meter type routes the rate on
both the supplier and the DSO side. Read it before changing any rate arithmetic
or adding a contract kind.

Related docs:

- [architecture.md](architecture.md) - where the pricing engine sits in the data flow.
- [provider-framework.md](provider-framework.md) - the dataclasses (`EnergyRates`,
  `DsoOverlay`, `TaxOverlay`, `InjectionRates`, `SupplierSnapshot`) whose fields
  this engine consumes.
- [coordinator.md](coordinator.md) - how the coordinator fetches snapshots and
  spots, tracks the monthly peak, and calls into this engine each hour.
- [glossary.md](glossary.md) - Belgian-energy and HA terms used below (DSO, SMR3,
  bi-horaire, CWaPE, prosumer, injection).
- [data-sources.md](data-sources.md) - the ENTSO-E spot curve the dynamic and
  spot-indexed-injection paths depend on.

## Scope and design invariant

The engine is pure: no Home Assistant imports, no EUR values hardcoded in
source (`pricing.py:26-44`, `providers/base.py:38-39`). Every number comes from a
live-fetched `SupplierSnapshot`. All example numbers below are illustrative and
are taken verbatim from source comments; do not treat them as current rates.

The engine's central invariant is that a `PriceBreakdown`'s components sum to its
total, bit for bit:

```
energy + network + taxes == all_in
```

This holds because VAT is applied to each component separately and then summed,
never as `(e + n + t) * vat`, which would diverge by sub-femto-euro rounding once
`vat_rate` is non-zero (`pricing.py:571-585`, same reasoning at
`pricing.py:411-423` for `static_breakdown`).

## Public surface

`pricing.py` exports one result dataclass and a small set of pure functions.

```python
@dataclass(frozen=True)
class PriceBreakdown:      # pricing.py:73-81
    energy: float          # VAT-incl EUR/kWh, energy component
    network: float         # VAT-incl EUR/kWh, distribution + transport
    taxes: float           # VAT-incl EUR/kWh, per-kWh levies
    all_in: float          # == energy + network + taxes
```

| Function | Location | Returns | Purpose |
| --- | --- | --- | --- |
| `compute_breakdown(snapshot, dso_key, region, when, spot_eur_per_kwh=None, meter="mono", dso_tariff_mode="bi_horaire")` | `pricing.py:540` | `PriceBreakdown` | Top-level all-in EUR/kWh for one hour. |
| `energy_eur_per_kwh(energy, when, spot_eur_per_kwh, meter, region, dso_tariff_mode)` | `pricing.py:262` | `float` | Energy component; dispatches on the `EnergyRates` subtype. |
| `network_eur_per_kwh(dso, when, meter, dso_tariff_mode, region)` | `pricing.py:449` | `float` | Distribution + transport for the hour. |
| `taxes_eur_per_kwh(taxes, region)` | `pricing.py:528` | `float` | Per-kWh federal + regional levies. |
| `_routed_rate(base, energy, when, meter, region, *, bi_capable, dso_tariff_mode)` | `pricing.py:231` | `float` | Shared Fixed/Variable meter routing. |
| `tou_slot(when, weekend_rule="weekend_offpeak")` | `pricing.py:184` | `"peak"|"transition"|"offpeak"` | TOU band for a datetime. |
| `dso_impact_band(when)` | `pricing.py:430` | `"pic"|"medium"|"eco"` | Wallonia Tarif Impact band for a datetime. |
| `is_offpeak(when, region)` | `pricing.py:153` | `bool` | Classic bi-horaire off-peak test, per region. |
| `is_belgian_holiday(d)` | `pricing.py:131` | `bool` | Federal public-holiday test. |
| `static_energy_eur_per_kwh(energy, band)` | `pricing.py:325` | `float | None` | Stable (no time-of-day) rate for a band. |
| `static_breakdown(snapshot, dso_key, region, band, dso_tariff_mode)` | `pricing.py:368` | `PriceBreakdown | None` | All-in for a static band, used by the YTD/current-year path. |
| `yearly_fixed_fee_for_meter(energy, meter)` | `pricing.py:351` | `float` | Supplier yearly fixed fee for the meter type. |
| `slots_per_hour(resolution)` / `slot_delta(resolution)` / `slot_start(when, resolution)` | `pricing.py:83`,`88`,`95` | `int`/`timedelta`/`datetime` | Quarter-hour vs hourly grid helpers. |

The injection, capacity, prosumer and Brussels-OSP arithmetic is not in
`pricing.py`; it lives in `coordinator.py` and is documented in the later sections.

## The all-in formula

For one hour, `compute_breakdown` computes three VAT-incl components and their
sum (`pricing.py:566-585`):

```
all_in = energy(VAT) + network(VAT) + taxes(VAT)

  energy  = energy_eur_per_kwh(snapshot.energy, when, spot, meter, region, dso_tariff_mode)
  network = distribution(when, meter, dso_tariff_mode, region) + dso.transport
  taxes   = federal_excise
          + energy_contribution
          + regional_renewables(region)          # flanders | wallonia | brussels
          + region_connection_fee                # Wallonia only (added in taxes_eur_per_kwh)

  each_component(VAT) = component * (1.0 + snapshot.taxes.vat_rate)
```

Note what is deliberately absent from the per-kWh formula:

- `dso.data_management_per_year`, `capacity_eur_per_kw_year`, the Brussels OSP
  table, and `taxes.energy_fund_eur_per_month` are per-year or per-month EUR
  charges, not EUR/kWh. They are billed by the coordinator's cost sensors, not
  folded into the hourly all-in rate. `taxes_eur_per_kwh` sums only the per-kWh
  levies (`pricing.py:528-537`); `energy_fund_eur_per_month` is defined on the
  `TaxOverlay` (`providers/base.py:314`) but is not touched here.
- The Wallonia `region_connection_fee` is a per-kWh term and IS included in
  `taxes_eur_per_kwh` for Wallonia (`pricing.py:531-533`).

### Regional renewables selection

`taxes_eur_per_kwh` starts from the two always-present federal levies and adds
exactly one region's renewables surcharge (`pricing.py:528-537`):

| Region | Terms added |
| --- | --- |
| Flanders | `flanders_renewables` |
| Wallonia | `region_connection_fee + wallonia_renewables` |
| Brussels | `brussels_renewables` |

The `TaxOverlay` carries all three renewables columns; an extractor that operates
in only one or two regions leaves the others at `0.0`
(`providers/base.py:298-308`). Illustrative magnitudes from the dataclass
docstring: Flanders roughly 1.5 c/kWh, Wallonia roughly 3.1 c/kWh, Brussels
roughly 2.7 c/kWh (`providers/base.py:291-293`, illustrative).

### VAT handling and the vat_rate == 0.0 convention

Belgian residential electricity is billed at 6% VAT, but every current extractor
parses numbers that are already VAT-inclusive, so the snapshot convention is
`vat_rate = 0.0`, and the multiplier `1.0 + vat_rate` is `1.0`
(`providers/base.py:315-308`, `pricing.py:549-558`). Under that convention the
reported components match what the PDF prints exactly.

The multiplier exists only as forward-compatibility: if a future extractor parses
ex-VAT numbers it sets `vat_rate = 0.06`, and VAT then applies uniformly across
energy, network and taxes rather than being smeared into the taxes component
(`pricing.py:571-579`). Applying VAT per component before summing is what keeps
`energy + network + taxes == all_in` exact (see the invariant above).

Injection is the exception: it is VAT-exempt and never VAT-scaled (see
[Injection math](#injection-feed-in-math)).

## Energy rate by contract kind

`energy_eur_per_kwh` dispatches on the runtime type of `snapshot.energy`
(`pricing.py:262-319`). The five `EnergyRates` subtypes are
`FixedRates | VariableRates | DynamicRates | TimeOfUseRates | ImpactRates`
(`providers/base.py:211`). The `TariffKind` string on a `Contract` is
`"fixed" | "variable" | "dynamic" | "tou" | "tou_impact"`
(`providers/base.py:53`).

```
energy_eur_per_kwh(energy, when, spot, meter, region, dso_tariff_mode)
        |
        +-- FixedRates    --> _routed_rate(energy.single,  ...)
        +-- VariableRates --> _routed_rate(energy.current, ...)
        +-- DynamicRates  --> factor * spot + base          (spot required)
        +-- TimeOfUseRates--> tou_slot(when, weekend_rule) -> peak/transition/offpeak
        +-- ImpactRates   --> dso_impact_band(when)        -> pic/medium/eco
```

### Fixed and Variable: `_routed_rate`

Fixed and Variable share the meter-routing helper `_routed_rate`
(`pricing.py:231-259`). Priority order:

1. `meter == "exclusive_night"` and the card published an `exclusive_night` rate:
   use it (`pricing.py:253-254`).
2. `bi_capable` (meter is `bi` or `dynamic`) and both `peak` and `offpeak` are
   published: pick one by schedule (`pricing.py:255-258`):
   - Under `dso_tariff_mode == "impact"`: ECO band bills off-peak, MEDIUM/PIC bill
     peak (`pricing.py:256-257`). This aligns the energy side with the Impact-banded
     distribution when an SMR3 meter registers in CWaPE bands.
   - Otherwise: `is_offpeak(when, region)` picks off-peak vs peak
     (`pricing.py:258`).
3. Fall back to the single/current `base` rate (`pricing.py:259`).

`FixedRates` fields: `single`, optional `peak`/`offpeak`/`exclusive_night`, plus
`yearly_fixed_fee` and `yearly_fixed_fee_exclusive_night`
(`providers/base.py:78-97`). `VariableRates` mirrors it with `current` in place
of `single` and an optional `formula` string (`providers/base.py:100-124`).
Suppliers that publish only a mono rate (e.g. Eneco Power Flex) leave
`peak`/`offpeak` `None`, and routing falls through to the single rate for every
meter type (`providers/base.py:105-108`).

### Dynamic: `factor * spot + base`

`DynamicRates` computes `factor * spot_eur_per_kwh + base` and raises
`ValueError("dynamic tariff needs a spot price")` when `spot` is `None`
(`pricing.py:301-304`). The spot is the ENTSO-E BE day-ahead price for the slot.
`DynamicRates.quarter_hourly` selects whether the contract bills on the native
15-minute grid (Engie, Cociter, EBEM, Ecofix, OCTA+, Ecopower Dynamische
Burgerstroom) or the hourly-aggregated curve (Frank default, Luminus, Mega,
TotalEnergies, Eneco); YTD billing stays hourly regardless
(`providers/base.py:127-147`). See [data-sources.md](data-sources.md) for how the
curve is fetched and the grid helpers `slots_per_hour` / `slot_delta` /
`slot_start` (`pricing.py:83-104`).

### Time-of-use: `tou_slot`

`TimeOfUseRates` has three published rates `peak`, `transition`, `offpeak`, and a
`weekend_rule` (`providers/base.py:163-182`). `tou_slot` maps a local datetime to
its band (`pricing.py:184-228`).

Shared weekday schedule:

| Band | Weekday hours |
| --- | --- |
| peak | 07:00-11:00 and 17:00-22:00 |
| transition | 11:00-17:00 and 22:00-01:00 |
| offpeak | 01:00-07:00 |

Federal Belgian holidays follow the weekend rule, not the weekday rule
(`pricing.py:217`). The weekend rule differs by product (`pricing.py:211-228`):

| `weekend_rule` | Weekend/holiday behaviour |
| --- | --- |
| `weekend_offpeak` (generic CWaPE default) | Whole weekend off-peak. |
| `weekend_no_peak` (Engie Empower Flextime) | Never peak; transition 07:00-11:00 + 17:00-01:00, offpeak 01:00-07:00 + 11:00-17:00. |
| `smartflex_seasonal` (Luminus SmartFlex) | Seasonal bands applied every day, no weekend exception. |

The `smartflex_seasonal` rule ignores weekday/weekend entirely and keys on season
(`pricing.py:211-216`): peak 07:00-11:00 + 17:00-22:00 both seasons; the
11:00-17:00 midday window is off-peak in spring/summer (21 March to 20 September
inclusive, `_is_smartflex_summer`, `pricing.py:179-181`) and transition otherwise;
22:00-07:00 is always transition. The "free Sundays" promo is a first-year
discount and is out of scope (`pricing.py:203-208`).

### Impact: `dso_impact_band`

`ImpactRates` (`tou_impact` kind) is Wallonia's Tarif Impact, distinct from TOU
because its schedule is the CWaPE-defined Impact one with no weekend exception,
matching the DSO Impact distribution tariff that gates eligibility
(`providers/base.py:195-208`). Fields: `pic`, `medium`, `eco`
(`providers/base.py:214-206`). `dso_impact_band` (`pricing.py:430-446`):

| Band | Hours (every day) |
| --- | --- |
| pic (highest) | 17:00-22:00 |
| medium | 07:00-11:00 and 22:00-01:00 |
| eco (lowest) | 01:00-07:00 and 11:00-17:00 |

Source cited in the docstring: TotalEnergies Impact card footnote 7 / ORES
"Comprendre ma facture / Impact" (`pricing.py:438-440`). Requires an SMR3
quarter-hourly meter and an opt-in to the DSO Impact tariff
(`providers/base.py:209-200`).

## Meter routing

`MeterType` is `"mono" | "bi" | "dynamic" | "exclusive_night"`
(`pricing.py:70`, `const.py:136-124`). A digital (SMR3) meter registers
peak/offpeak just like a bi-hourly meter, so `bi_capable = meter in ("bi",
"dynamic")` on both the energy and network sides (`pricing.py:280`,
`pricing.py:514`). The Belgian meter conventions are documented at
`pricing.py:30-43`.

### Supplier (energy) side

Handled by `_routed_rate` for Fixed/Variable (see above). An exclusive-night
meter physically only registers during DSO off-peak hours, so the code does not
gate it by `is_offpeak`; it just takes the `exclusive_night` rate when published,
else falls back to single/current (`pricing.py:253-259`, `pricing.py:270-279`).

### DSO (network) side

`network_eur_per_kwh` returns `distribution + dso.transport`
(`pricing.py:449-525`). Distribution selection, in strict precedence order:

1. **Exclusive night** (`pricing.py:472-487`), resolved BEFORE the Impact band so
   a dedicated night circuit bills its own rate even under Impact mode. Fallback
   chain: `distribution_exclusive_night` -> `distribution_offpeak` ->
   `distribution_single` (`pricing.py:481-486`). Each step is closer to the real
   bill than the day rate.
2. **Impact** (`pricing.py:488-511`), only when `dso_tariff_mode == "impact"` AND
   all three of `distribution_pic`/`medium`/`eco` are non-`None`. The all-three
   guard exists because `python -O` strips `assert`, and a partially populated
   triplet would otherwise raise `TypeError` on `None + transport`; treating
   Impact as available only when complete falls through to bi-horaire/single on
   cards that omit it (Brussels Sibelga, Flanders Fluvius)
   (`pricing.py:494-503`).
3. **Bi-horaire** (`pricing.py:512-522`), when `dso_tariff_mode != "simple"`, the
   meter is `bi`/`dynamic`, and both `distribution_peak`/`offpeak` are published:
   `is_offpeak(when, region)` picks the rate.
4. **Single** (`pricing.py:523-524`), the fallback for everything else, including
   `dso_tariff_mode == "simple"` and mono meters.

`DsoTariffMode` (`"simple" | "bi_horaire" | "impact"`, `pricing.py:426`,
`const.py:154-135`) is orthogonal to the supplier meter: it is the billing mode
set on the user's grid connection, and the coordinator falls back automatically
when the DSO does not publish Impact rates (`const.py:149-135`).

### is_offpeak schedule, per region

`is_offpeak` differs by region and Wallonia changed on 2026-01-01
(`pricing.py:153-173`):

| Region | Off-peak schedule |
| --- | --- |
| Flanders (Fluvius) | Mon-Fri 22:00-07:00 and all weekend. Weekday public holidays bill at the DAY rate (the meter clock switches on weekday/weekend only). |
| Brussels (Sibelga) | 22:00-07:00, all weekend, AND weekday public holidays (historical Brussels exception). |
| Wallonia (from 2026-01-01) | One uniform schedule every day including weekends and holidays: 22:00-07:00 and 11:00-17:00. |

`is_belgian_holiday` covers the seven fixed federal dates plus Easter Monday
(+1), Ascension (+39) and Pentecost Monday (+50) off Gregorian-computus Easter;
regional holidays are deliberately excluded because DSO billing applies federal
rules uniformly (`pricing.py:107-150`). The fixed-holiday set is lifted to module
scope so it is not reallocated on every call along the 8760-iteration backfill
path (`pricing.py:107-112`).

### Exclusive-night yearly fee routing

`yearly_fixed_fee_for_meter` bills the dedicated `yearly_fixed_fee_exclusive_night`
on an exclusive-night config entry when the card prints one (EBEM Groen Variabel),
otherwise the standard `yearly_fixed_fee` for every meter type
(`pricing.py:351-365`, fields at `providers/base.py:92-97` and
`providers/base.py:119-123`). An exclusive-night circuit is configured as a
SECOND config entry pointing at the night kWh sensor; the primary day meter stays
mono/bi/dynamic (`const.py:139-122`).

## The static path

`static_energy_eur_per_kwh` and `static_breakdown` produce a stable, no-time-of-day
rate for the current-year-cost / YTD sensor when the contract has one
(`pricing.py:325-423`).

`static_energy_eur_per_kwh(energy, band)` returns a rate for `band in
("single","peak","offpeak")` for Fixed and Variable, falling back to
single/current when the requested band is unpublished (`pricing.py:325-348`). It
returns `None` for `DynamicRates` (no constant rate), `TimeOfUseRates` (3-band
schema does not map onto the bi-hourly convention) and `ImpactRates` (per-band
rates vary by hour, caller must use the hourly path) (`pricing.py:329-334`).

`static_breakdown` assembles the all-in for one band with the same VAT-per-component
rule as `compute_breakdown` (`pricing.py:411-423`). It returns `None` when the
energy has no stable rate, and also when `dso_tariff_mode == "impact"` and the DSO
publishes Impact distribution: Impact distribution cannot collapse to
single/peak/offpeak, so the YTD path must read hourly statistics instead
(`pricing.py:396-400`). Distribution selection here mirrors the network side:
`simple` -> single, `peak`/`offpeak` band when published, else single
(`pricing.py:401-408`). A missing `dso_key` raises `KeyError` with the available
keys (`pricing.py:390-395`, same guard in `compute_breakdown` at
`pricing.py:559-565`).

## Injection (feed-in) math

Injection is computed in `coordinator.py`, not `pricing.py`, but it consumes the
same snapshot and `tou_slot` rule. `InjectionRates` carries a monthly indicative
`current`, an hourly formula `factor`/`base`, an optional per-slot TOU triplet
`peak`/`transition`/`offpeak`, and a `formula` string (`providers/base.py:214-246`).

**VAT-exempt invariant.** Belgian residential injection is exempt from VAT, so
`InjectionRates` values are NEVER VAT-inclusive regardless of the consumption
snapshot's `vat_rate` (`providers/base.py:216-218`). None of the injection code
paths multiply by `1.0 + vat_rate`.

Injection formulas can go negative at low spot (the producer pays to inject) and
the engine respects that: no clamping in `_compute_injection_price` or
`_historical_injection_rate` (`providers/base.py:224-228`).

### The three injection shapes

| Shape | Populated fields | Needs spot? | Example |
| --- | --- | --- | --- |
| (a) Monthly indicative | `current` set | No | Eneco Fix/Flex, EBEM, DATS 24, monthly-indexed variables |
| (b) Hourly formula | `factor` + `base` set | Yes | Dynamic contracts (Engie, OCTA+, Luminus, Mega, TotalEnergies) |
| (c) Spot-indexed on a static-energy card | `factor` + `base` set, `current is None`, energy NOT dynamic | Yes | Cociter Variable |

Shape (c) is the subtle one: the energy contract is Variable (no spot needed for
energy) but the injection prices off the hourly BELPEX with no printed monthly
indicative, so pricing the credit still needs an ENTSO-E spot. `Contract`
advertises this with `spot_indexed_injection` so the config flow offers the API-key
step on the injection regime (`providers/base.py:69-75`). At runtime,
`_injection_needs_spot` detects it (`coordinator.py:1819-1637`):

```python
def _injection_needs_spot(snapshot, entry) -> bool:   # coordinator.py:1819
    if entry.data.get(CONF_SOLAR_REGIME) != SOLAR_REGIME_INJECTION:
        return False
    inj = snapshot.injection
    return (
        inj is not None
        and inj.current is None
        and inj.factor is not None
        and inj.base is not None
        and not isinstance(snapshot.energy, DynamicRates)   # (c), not (b)
    )
```

The coordinator uses this to fetch spots for a static-energy card too (soft fetch:
falls back to cached curve, then to no injection price) so the credit does not go
unavailable (`coordinator.py:937-771`, `coordinator.py:1005`). This is the
spot-indexed injection invariant: shape (c) must be gated on `_injection_needs_spot`
in the live, backfill and compare paths, or the credit drifts.

### Live injection price: `_compute_injection_price`

`_compute_injection_price(snapshot, entry, spot_prices)` returns the current-hour
EUR/kWh price only on the injection regime and only when the snapshot has injection
data (`coordinator.py:1866-1726`). Priority:

1. **Per-slot TOU** via `_tou_injection_rate` (`coordinator.py:1884-1682`).
2. **Spot formula** `factor * spot + base` when either the energy is
   `DynamicRates` (shape b) OR `inj.current is None` (shape c). If no spot is
   available it returns `None` rather than fabricate a value
   (`coordinator.py:1898-1723`). The spot is looked up on the contract's own grid
   (`RESOLUTION_QUARTER` when `_energy_is_quarter_hourly`, else hourly), snapped
   with `slot_start`, and a nearest substitute is accepted only within one billing
   slot (900 s quarter-hourly, 3600 s hourly) (`coordinator.py:1905-1722`).
3. **Monthly indicative** `inj.current` otherwise, including static-energy cards
   whose injection carries a monthly index but also a printed `current` (Ecofix
   Flexy, EBEM Groen Variabel / B@sic+) (`coordinator.py:1894-1693`,
   `coordinator.py:1724-1726`).

### Per-slot TOU injection: `_tou_injection_rate`

`_tou_injection_rate(inj, energy, when)` returns a per-slot rate only when the
energy is `TimeOfUseRates` and `inj.peak` is set (Engie Empower Flextime publishes
a peak/transition/super-off-peak feed-in triplet, monthly-realized)
(`coordinator.py:1844-1659`, fields at `providers/base.py:245-245`). It reuses the
energy contract's own `weekend_rule` via `tou_slot` so injection and consumption
agree on the slot for a given hour (`coordinator.py:1654`). Returns `None`
otherwise so the caller falls back to the current / factor+base path.

### Historical injection: `_historical_injection_rate`

`_historical_injection_rate(injection, spot, *, energy, when)` mirrors the live
priority for a past hour: TOU slot first, then `factor*spot+base` when both the
formula and a historical spot exist, then `current` (`coordinator.py:1729-1760`).
The ordering (formula before `current`) is a bug fix: several dynamic-injection
contracts (Engie, OCTA+, TotalEnergies, Luminus, Mega) publish BOTH a `current`
indicative and `factor`/`base`, and checking `current` first made the YTD credit
use the flat indicative while the live sensor used the spot formula, so the two
user-facing numbers diverged (`coordinator.py:1946-1748`).

### Historical bug: monthly-indexed injection emitting an hourly factor

A monthly-indexed injection (EBEM Variabel/B@sic+, Eneco Fix/Flex, DATS 24) must
emit only the realized monthly `current`, never an hourly `factor*spot+base`,
because the indicative is the actual credit. The guard that keeps shape (b)/(c)
from swallowing these cards is the `inj.current is None` clause in both
`_injection_needs_spot` (`coordinator.py:1837`) and `_compute_injection_price`
(`coordinator.py:1901`): when a card prints a monthly `current`, the spot branch
is skipped and the realized rate is used, keeping the live sensor consistent with
the YTD credit for the same hour (`coordinator.py:1894-1693`). A latent mis-price
here is masked whenever the indicative prints, which is why it was fixed
explicitly rather than left to fall through.

### YTD injection paths

Past-month YTD billing routes injection per regime (`coordinator.py:2487-2291`,
context):

- `compensation`: per-hour `(cons - inj) * all_in`, netting injection against
  consumption (per band when bi) and clamping at zero.
- `injection`: per-hour `cons * all_in - inj * inj_rate`, where `inj_rate` comes
  from `_historical_injection_rate` (`coordinator.py:2556-2354`).

Shape (c) has a dedicated YTD helper `_ytd_spot_injection_credit`
(`coordinator.py:2570`) that credits a static-energy contract whose injection is a
pure BELPEX formula with no fixed credit; it is a no-op unless the injection is
exactly that shape and an injection sensor is wired, and it skips hours with no
cached spot (`coordinator.py:2583-2397`).

## Capacity tariff

The Flanders capaciteitstarief is billed by the coordinator, not folded into the
per-kWh all-in. Monthly cost (`_compute_capacity`, `coordinator.py:1792-1600`):

```
capacity_cost_eur = peak_kw * overlay.capacity_eur_per_kw_year / 12.0
```

Returns `0.0` when the entry lost its `CONF_DSO` key, the overlay is missing, or
`capacity_eur_per_kw_year is None` (`coordinator.py:1798-1599`). The rate lives on
`DsoOverlay.capacity_eur_per_kw_year` (`providers/base.py:273`); Flanders digital
meters publish it, other regions leave it `None`.

`peak_kw` is resolved in `_track_monthly_peak` (`coordinator.py:1591-1446`) and
applies only in Flanders; outside Flanders the peak is reset to `0.0`
(`coordinator.py:1592-1396`). It rolls over on the local 1st of month
(`coordinator.py:1601-1403`). Two modes (`const.py:219-197`):

- `CAPACITY_MODE_FIXED`: use `CONF_CAPACITY_FIXED_KW` directly (a rolling max would
  ignore a mid-month decrease the user just made) (`coordinator.py:1608-1412`).
- `CAPACITY_MODE_SENSOR`: rolling max of a power sensor, scaled by its unit (W/VA
  scaled by 0.001 to kW; issue #19 was a 1000x inflation when W was stored as kW)
  (`coordinator.py:1615-1441`).

Regardless of mode, the regulated VREG floor is applied last
(`coordinator.py:1645-1446`):

```
peak_kw = max(peak_kw, VREG_CAPACITY_FLOOR_KW)     # VREG_CAPACITY_FLOOR_KW = 2.5
```

`VREG_CAPACITY_FLOOR_KW = 2.5` is the regulated minimum monthly peak Fluvius bills
against, set when the capacity tariff was introduced in January 2023 and unchanged
since (`const.py:222-203`). A household whose peak stays below the floor still pays
the floor.

## Prosumer term

The prosumer (compensation-regime) fee is Walloon-only and monthly
(`_compute_prosumer`, `coordinator.py:1967-1798`):

```
prosumer_cost_eur = kva * (dso_rate + supplier_rate) / 12.0

  dso_rate      = overlay.prosumer_eur_per_kva_year        (DsoOverlay)
  supplier_rate = snapshot.supplier_prosumer_eur_per_kva_year   (SupplierSnapshot)
```

Returns `0.0` unless the regime is `compensation` AND the region is Wallonia AND
`CONF_SOLAR_KVA > 0` (`coordinator.py:1978-1788`). The Wallonia gate is deliberate:
compensation is Walloon-only, and billing a prosumer fee in Flanders on top of the
always-billed capacity tariff would double-count grid recovery
(`coordinator.py:1980-1782`).

The DSO rate lives on `DsoOverlay.prosumer_eur_per_kva_year`
(`providers/base.py:280-273`), published by Wallonia DSOs (valid until 2030 per
CWaPE) and `None` on Flemish SMR3 connections. The supplier-side forfait lives on
`SupplierSnapshot.supplier_prosumer_eur_per_kva_year`
(`providers/base.py:337-332`), billed on top of the DSO tariff; Cociter Variable
publishes one. It is already TVAC (VAT-incl) and summed raw, never VAT-scaled
(`coordinator.py:1999-1797`, `providers/base.py:341`).

Regime semantics (`const.py:206-194`): `compensation` ("compteur qui tourne a
l'envers") applies only to installations certified before 2024-01-01 and stays
valid until 2030-12-31; newer installations use the `injection` tariff (no per-kVA
fee); Flemish digital meters are SMR3 from the start. The YTD counterpart
`_ytd_prosumer` sums the monthly fee across the year using each month's archived
overlay, gated the same Walloon-only way (`coordinator.py:2168-2205`).

## Brussels OSP tier

The Brussels Brugel OSP (Obligations de Service Public) fee is a flat annual
Sibelga charge scaled by contractual connection power
(`_brussels_osp_fee`, `coordinator.py:1603-1612`):

```python
def _brussels_osp_fee(overlay, entry) -> float:      # coordinator.py:1603
    if overlay is None or overlay.brussels_osp_by_tier is None:
        return 0.0
    tier = entry.data.get(CONF_CONNECTION_KVA_TIER, DEFAULT_CONNECTION_KVA_TIER)
    return overlay.brussels_osp_by_tier.get(tier, 0.0)
```

The table lives on `DsoOverlay.brussels_osp_by_tier` and is populated only on the
Sibelga overlay (`providers/base.py:274-268`). The user picks the tier in the
config flow; the four residential tiers are `le1_44`, `le6`, `le9_6`, `le13`
(residential connections are <=13 kVA), default `le6`
(`const.py:164-156`). Returns `0.0` outside Brussels or when the card omits the
OSP table. The fee is added to the Brussels annual cost in the YTD path
(`coordinator.py:2161-2162`), not to the per-kWh all-in.

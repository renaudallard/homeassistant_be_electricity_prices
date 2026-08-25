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
`vat_rate` is non-zero (`pricing.py:453-462`, same reasoning at
`pricing.py:445-488` for `static_breakdown`).

## Public surface

`pricing.py` exports one result dataclass and a small set of pure functions.

```python
@dataclass(frozen=True)
class PriceBreakdown:      # pricing.py:74-81
    energy: float          # VAT-incl EUR/kWh, energy component
    network: float         # VAT-incl EUR/kWh, distribution + transport
    taxes: float           # VAT-incl EUR/kWh, per-kWh levies
    all_in: float          # == energy + network + taxes
```

| Function | Location | Returns | Purpose |
| --- | --- | --- | --- |
| `compute_breakdown(snapshot, dso_key, region, when, spot_eur_per_kwh=None, meter="mono", dso_tariff_mode="bi_horaire")` | `pricing.py:588` | `PriceBreakdown` | Top-level all-in EUR/kWh for one hour. |
| `energy_eur_per_kwh(energy, when, spot_eur_per_kwh, meter, region, dso_tariff_mode)` | `pricing.py:273` | `float` | Energy component; dispatches on the `EnergyRates` subtype. |
| `network_eur_per_kwh(dso, when, meter, dso_tariff_mode, region)` | `pricing.py:497` | `float` | Distribution + transport for the hour. |
| `taxes_eur_per_kwh(taxes, region)` | `pricing.py:593` | `float` | Per-kWh federal + regional levies that VAT applies to. |
| `taxes_vat_exempt_eur_per_kwh(taxes, region)` | `pricing.py:611` | `float` | Per-kWh levies billed at face value whatever the card's VAT basis (the Walloon connection fee). |
| `_routed_rate(base, energy, when, meter, region, *, bi_capable, dso_tariff_mode)` | `pricing.py:242` | `float` | Shared Fixed/Variable meter routing. |
| `tou_slot(when, weekend_rule="weekend_offpeak")` | `pricing.py:195` | `"peak"|"transition"|"offpeak"` | TOU band for a datetime. |
| `dso_impact_band(when)` | `pricing.py:478` | `"pic"|"medium"|"eco"` | Wallonia Tarif Impact band for a datetime. |
| `is_offpeak(when, region)` | `pricing.py:154` | `bool` | Classic bi-horaire off-peak test, per region. |
| `is_belgian_holiday(d)` | `pricing.py:132` | `bool` | Federal public-holiday test. |
| `static_energy_eur_per_kwh(energy, band)` | `pricing.py:342` | `float | None` | Stable (no time-of-day) rate for a band. |
| `static_breakdown(snapshot, dso_key, region, band, dso_tariff_mode)` | `pricing.py:434` | `PriceBreakdown | None` | All-in for a static band, used by the YTD/current-year path. |
| `yearly_fixed_fee_for_meter(energy, meter)` | `pricing.py:368` | `float` | Supplier yearly fixed fee for the meter type. |
| `slots_per_hour(resolution)` / `slot_delta(resolution)` / `slot_start(when, resolution)` | `pricing.py:84`,`89`,`96` | `int`/`timedelta`/`datetime` | Quarter-hour vs hourly grid helpers. |

The injection, capacity, prosumer and Brussels-OSP arithmetic is not in
`pricing.py`; it lives in `coordinator.py` and is documented in the later sections.

## The all-in formula

For one hour, `compute_breakdown` computes three VAT-incl components and their
sum (`pricing.py:588-613`):

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
  levies (`pricing.py:619-634`); `energy_fund_eur_per_month` is defined on the
  `TaxOverlay` (`providers/base.py:526`) but is not touched here.
- `data_management_per_year` carries three different charges depending on the
  region, and one of them is tied to the tariff configuration. The Walloon
  `terme fixe` is not billed under the CWaPE incitative configuration that the
  cards sell as the IMPACT tariff, and `_walloon_fixed_term_applies`
  (`fees.py:101`) is what drops it; nothing offsets it, because CWaPE set the
  capacity term that replaces it to 0 EUR/kW for 2026 through 2029. The Flemish
  `databeheer` and the Brussels `mesure` plus fixed-term pair are billed
  whatever the mode says.
- The Wallonia `region_connection_fee` is a per-kWh term and IS billed, but
  through `taxes_vat_exempt_eur_per_kwh` (`pricing.py:663`), not
  `taxes_eur_per_kwh`. Engie's Walloon card prints `Redevance raccordement(8)`
  and footnote (8) reads *"Vous ne payez pas de TVA sur ces couts"* — the same
  footnote that exempts the Flemish energy fund on its Flanders edition.
  `_finalize_breakdown` adds it to the taxes component **after** the VAT factor,
  so it lands at face value. On a VAT-inclusive card (`vat_rate == 0`) the total
  is identical either way; on a professional Walloon card it was billed at
  0,0009075 EUR/kWh against the 0,00075 printed, about 9,45 EUR/yr at 60 000 kWh.

### Regional renewables selection

`taxes_eur_per_kwh` starts from the two always-present federal levies and adds
exactly one region's renewables surcharge (`pricing.py:576-585`):

| Region | Terms added (VAT-able) | Added VAT-exempt |
| --- | --- | --- |
| Flanders | `flanders_renewables` | — |
| Wallonia | `wallonia_renewables` | `region_connection_fee` |
| Brussels | `brussels_renewables` | — |

The `TaxOverlay` carries all three renewables columns; an extractor that operates
in only one or two regions leaves the others at `0.0`
(`providers/base.py:466-468`). Illustrative magnitudes from the dataclass
docstring: Flanders roughly 1.5 c/kWh, Wallonia roughly 3.1 c/kWh, Brussels
roughly 2.7 c/kWh (`providers/base.py:457-459`, illustrative).

### VAT handling and the vat_rate == 0.0 convention

Belgian residential electricity is billed at 6% VAT, and every scraped
residential card prints numbers that are already VAT-inclusive, so the snapshot
convention for them is `vat_rate = 0.0` and the multiplier `1.0 + vat_rate` is
`1.0` (`providers/base.py:479-482`, `pricing.py:599-605`). Under that convention
the reported components match what the PDF prints exactly.

A non-zero `vat_rate` means the opposite: the snapshot carries the numbers
**excluding** VAT, at the rate given. The expert custom supplier does this today
(the user types ex-VAT coefficients, default rate 0.06), and professional cards,
which print *"Prix tva exclue"* at 21%, do the same.

VAT then has to reach two kinds of value, and only one of them goes through the
pricing engine:

- **Per-kWh rates** are grossed per component in `_finalize_breakdown`
  (`pricing.py:422-425`), uniformly across energy, network and taxes rather than
  smeared into the taxes component. Applying it per component before summing is
  what keeps `energy + network + taxes == all_in` exact (see the invariant
  above).
- **Fixed and annual fees** - the yearly fee, data management, capacity, the DSO
  and supplier prosumer forfaits, the Brussels OSP table - never reach that path:
  the live, YTD, backfill and compare paths each sum them raw.
  `base.apply_vat` (`providers/base.py:594`) bakes them once instead.

`apply_vat` is called per config entry, from `_resolve_snapshot`
(`coordinator.py:568`), never before the shared snapshot cache: that cache is
keyed on `(supplier, contract, region)` and shared between entries that may
answer the VAT question differently. It is identity on a `vat_rate == 0.0`
snapshot, so it costs nothing for a residential entry. `CONF_INCLUDE_VAT`
chooses the factor: a business that deducts VAT sets it False and the card's
own ex-VAT numbers stand.

Two values are exempt outright and are never baked, whatever the card's basis.
The **Flemish energy fund** is levied VAT-free and the cards say so on the fund
row itself: Engie footnotes `Cotisation Fonds Energie Region Flamande(8)` with
*"(8) Vous ne payez pas de TVA sur ces couts"*, and DATS 24 marks `Bijdrage
Energiefonds Vlaams Gewest8` with *"8Niet aan btw onderworpen"*. It used to be
baked with the other annual fees, which charged a professional Flanders entry
12,18 EUR/month against an invoiced 10,07, about 25 EUR/yr on `current_year_cost`,
the compare quote and the config-flow estimate alike.

Injection is the other, and is conditional: residentially it is VAT-exempt and never VAT-scaled,
but professional cards tax it at 21% (*"Le prix d'injection est soumis a la TVA"*,
the reverse of the residential wording). An extractor whose card taxes it sets
`InjectionRates.vat_applies` and `apply_vat` grosses those rates too - they are
not in the per-component path either (see
[Injection math](#injection-feed-in-math)).

### Degressive federal excise

The federal special excise is normally one rate, but a card may print it as a
schedule that decreases by annual consumption band. `TaxOverlay` then carries
`federal_excise_bands` as `((upper_kwh, eur_per_kwh), ...)` ascending
(`providers/base.py:473`), and `resolve_excise_band` (`providers/base.py:768`)
resolves it against the entry's `CONF_ANNUAL_CONSUMPTION_KWH` and writes one
rate to `federal_excise`. The pricing engine never sees a band.

The schedule is billed PER TRANCHE, which the cards state outright: *"un tarif
degressif par tranche de consommation, calcule sur une base annuelle"*. So the
resolved figure is the BLEND over the year's volume (`blended_excise_rate`,
`providers/base.py:719`), not the rate of the band the total lands in. At the
2026 professional schedule a 30.000 kWh site pays the first 20.000 at 1,421 and
the rest at 1,209, which is 405,10 EUR/year and a 1,3503 c/kWh blend; billing
all 30.000 at 1,209 gives 362,70. The engine prices per hour and cannot know
where in the year an hour sits, but it does not need to: the charge is defined
on an annual basis, so the year's total over the year's volume is the honest
per-kWh figure, and the annual bill is exact whenever the volume estimate is.

A volume past the last band is billed at the last band's rate for the
remainder. Residential cards leave `federal_excise_bands` at `None`, where the
resolver is identity.

### Contractual price ceilings

Mega Cap is the one product that caps what the commodity can cost: *"la
composante energie facturee est limitee a un plafond de ... vous payez le
minimum entre les prix variables mensuels et ce plafond"*, per meter, and
guaranteed a year from the start of supply. `VariableRates` carries the four
`ceiling_*` columns and `energy_eur_per_kwh` takes the `min()` of the routed
rate and the routed ceiling.

Per SLOT, not on a mean: a ceiling is a `min()`, so clamping a monthly or
annual average would let an expensive month shelter under a cheap one, which is
the opposite of what the guarantee sells. The ceiling covers the energy
component only; network, taxes and surcharges stay due in full. Cards that cap
nothing leave the columns `None` and price exactly as before.

## Energy rate by contract kind

`energy_eur_per_kwh` dispatches on the runtime type of `snapshot.energy`
(`pricing.py:273-336`). The six `EnergyRates` subtypes are
`FixedRates | VariableRates | DynamicRates | SpotMonthlyRates | TimeOfUseRates | ImpactRates`
(`providers/base.py:257`). The `TariffKind` string on a `Contract` is
`"fixed" | "variable" | "dynamic" | "tou" | "tou_impact" | "spot_monthly"`
(`providers/base.py:53`).

```
energy_eur_per_kwh(energy, when, spot, meter, region, dso_tariff_mode)
        |
        +-- FixedRates       --> _routed_rate(energy.single,  ...)
        +-- VariableRates    --> _routed_rate(energy.current, ...)
        +-- DynamicRates     --> factor * spot + base          (live slot spot required)
        +-- SpotMonthlyRates --> factor * spot + base          (MONTHLY MEAN spot required)
        +-- TimeOfUseRates   --> tou_slot(when, weekend_rule) -> peak/transition/offpeak
        +-- ImpactRates      --> dso_impact_band(when)        -> pic/medium/eco
```

### Fixed and Variable: `_routed_rate`

Fixed and Variable share the meter-routing helper `_routed_rate`
(`pricing.py:242-270`). Priority order:

1. `meter == "exclusive_night"` and the card published an `exclusive_night` rate:
   use it (`pricing.py:264-265`).
2. `bi_capable` (meter is `bi` or `dynamic`) and both `peak` and `offpeak` are
   published: pick one by schedule (`pricing.py:266-269`):
   - Under `dso_tariff_mode == "impact"`: ECO band bills off-peak, MEDIUM/PIC bill
     peak (`pricing.py:267-268`). This aligns the energy side with the Impact-banded
     distribution when an SMR3 meter registers in CWaPE bands.
   - Otherwise: `is_offpeak(when, region)` picks off-peak vs peak
     (`pricing.py:269`).
3. Fall back to the single/current `base` rate (`pricing.py:270`).

`FixedRates` fields: `single`, optional `peak`/`offpeak`/`exclusive_night`, plus
`yearly_fixed_fee` and `yearly_fixed_fee_exclusive_night`
(`providers/base.py:112-145`). `VariableRates` mirrors it with `current` in place
of `single` and an optional `formula` string (`providers/base.py:102-126`).
Suppliers that publish only a mono rate (e.g. Eneco Power Flex) leave
`peak`/`offpeak` `None`, and routing falls through to the single rate for every
meter type (`providers/base.py:108-109`).

### Dynamic: `factor * spot + base`

`DynamicRates` computes `factor * spot_eur_per_kwh + base` and raises
`ValueError("dynamic tariff needs a spot price")` when `spot` is `None`
(`pricing.py:312-315`). The spot is the ENTSO-E BE day-ahead price for the slot.
`DynamicRates.quarter_hourly` selects whether the contract bills on the native
15-minute grid (Engie, Cociter, EBEM, Ecofix, OCTA+, Ecopower Dynamische
Burgerstroom, Bolt Dynamisch, energie.be, EnergyVision) or the hourly-aggregated curve (Frank default, Luminus, Mega,
TotalEnergies, Eneco); YTD billing stays hourly regardless
(`providers/base.py:139-159`). See [data-sources.md](data-sources.md) for how the
curve is fetched and the grid helpers `slots_per_hour` / `slot_delta` /
`slot_start` (`pricing.py:84-105`).

### Spot-monthly: `factor * monthly_mean(spot) + base`

`SpotMonthlyRates` runs the exact same formula as `DynamicRates`, and raises
`ValueError("spot-monthly tariff needs a monthly mean spot")` when `spot` is
`None` (`pricing.py:316-321`). What differs is what the caller threads through
the `spot_eur_per_kwh` parameter: not the live slot price, but the arithmetic
mean of the delivery month's hourly Day-Ahead spot, which the coordinator
computes off its ENTSO-E cache. Reusing the one parameter keeps pricing a pure
formula with no month arithmetic of its own.

The rate is therefore a single flat value for the whole month, so the contract
always bills on the hourly grid and carries no `quarter_hourly` flag. The
current month's mean is a running estimate until the month closes
(`providers/base.py:163-187`).

Used by group-purchase style products that index the commodity to the realized
monthly average (the Mega iChoosr / Samen Overstappen groepsaankoop shape), and
by the expert **custom** supplier. It is also the leg a variable card is
re-priced onto for a signing cohort, which is why it carries
`yearly_fixed_fee_exclusive_night`: the variable cards it inherits from (EBEM
Groen Variabel / B@sic+) print a separate exclusive-night standing charge.

### Time-of-use: `tou_slot`

`TimeOfUseRates` has three published rates `peak`, `transition`, `offpeak`, and a
`weekend_rule` (`providers/base.py:193-228`). `tou_slot` maps a local datetime to
its band (`pricing.py:195-239`).

Shared weekday schedule:

| Band | Weekday hours |
| --- | --- |
| peak | 07:00-11:00 and 17:00-22:00 |
| transition | 11:00-17:00 and 22:00-01:00 |
| offpeak | 01:00-07:00 |

Federal Belgian holidays follow the weekend rule, not the weekday rule
(`pricing.py:228`). The weekend rule differs by product (`pricing.py:222-239`):

| `weekend_rule` | Weekend/holiday behaviour |
| --- | --- |
| `weekend_offpeak` (generic CWaPE default) | Whole weekend off-peak. |
| `weekend_no_peak` (Engie Empower Flextime) | Never peak; transition 07:00-11:00 + 17:00-01:00, offpeak 01:00-07:00 + 11:00-17:00. |
| `smartflex_seasonal` (Luminus SmartFlex) | Seasonal bands applied every day, no weekend exception. |

The `smartflex_seasonal` rule ignores weekday/weekend entirely and keys on season
(`pricing.py:222-227`): peak 07:00-11:00 + 17:00-22:00 both seasons; the
11:00-17:00 midday window is off-peak in spring/summer (21 March to 20 September
inclusive, `_is_smartflex_summer`, `pricing.py:190-192`) and transition otherwise;
22:00-07:00 is always transition. The "free Sundays" promo is a first-year
discount and is out of scope (`pricing.py:214-219`).

### Impact: `dso_impact_band`

`ImpactRates` (`tou_impact` kind) is Wallonia's Tarif Impact, distinct from TOU
because its schedule is the CWaPE-defined Impact one with no weekend exception,
matching the DSO Impact distribution tariff that gates eligibility
(`providers/base.py:253-256`). Fields: `pic`, `medium`, `eco`
(`providers/base.py:268-270`). `dso_impact_band` (`pricing.py:526-542`):

| Band | Hours (every day) |
| --- | --- |
| pic (highest) | 17:00-22:00 |
| medium | 07:00-11:00 and 22:00-01:00 |
| eco (lowest) | 01:00-07:00 and 11:00-17:00 |

Source cited in the docstring: TotalEnergies Impact card footnote 7 / ORES
"Comprendre ma facture / Impact" (`pricing.py:486-488`). Requires an SMR3
quarter-hourly meter and an opt-in to the DSO Impact tariff
(`providers/base.py:244-245`).

`impact_band_hours()` counts the table above off `dso_impact_band` rather than
restating it, returning the hours in each band (5 / 7 / 12 per day, the 35 / 49
/ 84 per week the cards quote). The OptionsFlow annual estimate takes both its
representative hour and its weight from it. Those were literals beside a
comment repeating the schedule, which put the regulated CWaPE table in a second
place: move a boundary in `dso_impact_band` and the estimate kept the old
weighting silently. The weighted mean is exactly the mean over all 24 hours.

## Meter routing

`MeterType` is `"mono" | "bi" | "dynamic" | "exclusive_night"`
(`pricing.py:71`, `const.py:214-223`). A digital (SMR3) meter registers
peak/offpeak just like a bi-hourly meter, so `bi_capable = meter in ("bi",
"dynamic")` on both the energy and network sides (`pricing.py:291`,
`pricing.py:562`). The Belgian meter conventions are documented at
`pricing.py:30-43`.

### Supplier (energy) side

Handled by `_routed_rate` for Fixed/Variable (see above). An exclusive-night
meter physically only registers during DSO off-peak hours, so the code does not
gate it by `is_offpeak`; it just takes the `exclusive_night` rate when published,
else falls back to single/current (`pricing.py:264-270`, `pricing.py:281-290`).

### DSO (network) side

`network_eur_per_kwh` returns `distribution + dso.transport`
(`pricing.py:497-573`). Distribution selection, in strict precedence order:

1. **Exclusive night** (`pricing.py:520-535`), resolved BEFORE the Impact band so
   a dedicated night circuit bills its own rate even under Impact mode. Fallback
   chain: `distribution_exclusive_night` -> `distribution_offpeak` ->
   `distribution_single` (`pricing.py:598-603`). Each step is closer to the real
   bill than the day rate.
2. **Impact** (`pricing.py:536-559`), only when `dso_tariff_mode == "impact"` AND
   all three of `distribution_pic`/`medium`/`eco` are non-`None`. The all-three
   guard exists because `python -O` strips `assert`, and a partially populated
   triplet would otherwise raise `TypeError` on `None + transport`; treating
   Impact as available only when complete falls through to bi-horaire/single on
   cards that omit it (Brussels Sibelga, Flanders Fluvius)
   (`pricing.py:542-551`).
3. **Bi-horaire** (`pricing.py:560-570`), when `dso_tariff_mode != "simple"`, the
   meter is `bi`/`dynamic`, and both `distribution_peak`/`offpeak` are published:
   `is_offpeak(when, region)` picks the rate.
4. **Single** (`pricing.py:571-572`), the fallback for everything else, including
   `dso_tariff_mode == "simple"` and mono meters.

`DsoTariffMode` (`"simple" | "bi_horaire" | "impact"`, `pricing.py:522`,
`const.py:173-177`) is orthogonal to the supplier meter: it is the billing mode
set on the user's grid connection, and the coordinator falls back automatically
when the DSO does not publish Impact rates (`const.py:168-172`).

### is_offpeak schedule, per region

`is_offpeak` differs by region and Wallonia changed on 2026-01-01
(`pricing.py:154-174`):

| Region | Off-peak schedule |
| --- | --- |
| Flanders (Fluvius) | Mon-Fri 22:00-07:00 and all weekend. Weekday public holidays bill at the DAY rate (the meter clock switches on weekday/weekend only). |
| Brussels (Sibelga) | 22:00-07:00, all weekend, AND weekday public holidays (historical Brussels exception). |
| Wallonia (from 2026-01-01) | One uniform schedule every day including weekends and holidays: 22:00-07:00 and 11:00-17:00. |

`is_belgian_holiday` covers the seven fixed federal dates plus Easter Monday
(+1), Ascension (+39) and Pentecost Monday (+50) off Gregorian-computus Easter;
regional holidays are deliberately excluded because DSO billing applies federal
rules uniformly (`pricing.py:108-151`). The fixed-holiday set is lifted to module
scope so it is not reallocated on every call along the 8760-iteration backfill
path (`pricing.py:108-113`).

### Exclusive-night yearly fee routing

`yearly_fixed_fee_for_meter` bills the dedicated `yearly_fixed_fee_exclusive_night`
on an exclusive-night config entry when the card prints one (EBEM Groen Variabel),
otherwise the standard `yearly_fixed_fee` for every meter type
(`pricing.py:394-408`). Three rate shapes carry the dedicated field: `FixedRates`
(`providers/base.py:112-145`), `VariableRates` (`providers/base.py:121-125`) and
`SpotMonthlyRates` (`providers/base.py:191-196`), the last because a variable card
re-priced onto a monthly-mean leg for a signing cohort keeps the separate charge
its card printed. An exclusive-night circuit is configured as a SECOND config
entry pointing at the night kWh sensor; the primary day meter stays
mono/bi/dynamic (`const.py:158-164`).

## The static path

`static_energy_eur_per_kwh` and `static_breakdown` produce a stable, no-time-of-day
rate for the current-year-cost / YTD sensor when the contract has one
(`pricing.py:342-471`).

`static_energy_eur_per_kwh(energy, band)` returns a rate for `band in
("single","peak","offpeak")` for Fixed and Variable, falling back to
single/current when the card publishes no split (`pricing.py:342-365`). A
HALF-published pair counts as no split, the same rule `_routed_rate` applies on
the hourly path: filling the missing half from the single rate reads a rate the
card never printed for that band, and it made the two walks disagree about one
entry, the hourly engine billing the single rate around the clock while the
per-day walk billed the peak rate for peak hours. It
returns `None` for `DynamicRates` (no constant rate), `TimeOfUseRates` (3-band
schema does not map onto the bi-hourly convention) and `ImpactRates` (per-band
rates vary by hour, caller must use the hourly path) (`pricing.py:346-351`).

`static_breakdown` assembles the all-in for one band with the same VAT-per-component
rule as `compute_breakdown` (`pricing.py:644-675`). It returns `None` when the
energy has no stable rate, and also when `dso_tariff_mode == "impact"` and the DSO
publishes Impact distribution: Impact distribution cannot collapse to
single/peak/offpeak, so the YTD path must read hourly statistics instead
(`pricing.py:456-460`). Distribution selection here mirrors the network side:
`simple` -> single, `peak`/`offpeak` band when published, else single
(`pricing.py:492-499`). A missing `dso_key` raises `KeyError` with the available
keys (`pricing.py:644-675`, same guard in `compute_breakdown` at
`pricing.py:401-570`).

## Injection (feed-in) math

Injection is computed in `coordinator.py`, not `pricing.py`, but it consumes the
same snapshot and `tou_slot` rule. `InjectionRates` carries a monthly indicative
`current`, an hourly formula `factor`/`base`, an optional per-slot TOU triplet
`peak`/`transition`/`offpeak`, and a `formula` string (`providers/base.py:325-340`).

**VAT-exempt invariant.** Belgian residential injection is exempt from VAT, so
`InjectionRates` values are NEVER VAT-inclusive regardless of the consumption
snapshot's `vat_rate` (`providers/base.py:562-562`). None of the injection code
paths multiply by `1.0 + vat_rate`.

Injection formulas can go negative at low spot (the producer pays to inject) and
the engine respects that by default. A contract carrying a never-negative
guarantee sets `floor_at_zero` instead, and `_floor_injection`
(`injection.py:193`) then clamps the resolved rate at 0 in
`_compute_injection_price`, in `_historical_injection_rate` and in the compare
estimate. Only the expert custom supplier sets it (`providers/custom.py:237`);
every scraped card leaves it False.

WHERE the clamp lands is a pricing decision, not a detail, because `max()` is
convex and the two orders give different money:

- a PER-SLOT formula floors each slot, because that is what the contract bills.
  The live array and the year-to-date replay (off the hour's own quarters) credit
  each slot at its own rate; the compare estimate (`_compare_injection_credit`,
  `compare_quote.py:171`) has to collapse the window to one number, so it takes
  the mean of the floored rates weighted by the household's own export shape
  (`_export_weighted_credit`, `compare_quote.py:168`), which is the basis the
  year-to-date walk bills on.
- a MONTH-MEAN formula floors once, on the delivery month's tariff, because such
  a card publishes one number a month and the guarantee is written against that
  number. `_bake_monthly_injection` (`injection.py:64`) produces it and the floor
  lands on the flat `current` path.

The per-slot TOU triplet is never clamped. No card ships both a triplet and a
floor, and `tests/test_custom.py` pins that rather than the pricing code
carrying a branch that cannot run.

### The three injection shapes

| Shape | Populated fields | Needs spot? | Example |
| --- | --- | --- | --- |
| (a) Monthly indicative | `current` set | No | Eneco Fix/Flex, EBEM, DATS 24, EnergyVision fixed (both regions), monthly-indexed variables |
| (b) Hourly formula | `factor` + `base` set | Yes | Dynamic contracts (Engie, OCTA+, Luminus, Mega, TotalEnergies) |
| (c) Spot-indexed on a static-energy card | `factor` + `base` set, `current is None`, energy NOT dynamic | Yes | Cociter Variable |

Shape (c) is the subtle one: the energy contract is Variable (no spot needed for
energy) but the injection prices off the hourly BELPEX with no printed monthly
indicative, so pricing the credit still needs an ENTSO-E spot. `Contract`
advertises this with `spot_indexed_injection` so the config flow offers the API-key
step on the injection regime (`providers/base.py:71-77`). At runtime,
`_injection_needs_spot` detects it (`injection.py:94-107`):

```python
def _injection_needs_spot(snapshot, entry) -> bool:   # injection.py:94
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
unavailable (`coordinator.py:592-605`, `coordinator.py:633`). This is the
spot-indexed injection invariant: shape (c) must be gated on `_injection_needs_spot`
in the live, backfill and compare paths, or the credit drifts.

### Live injection price: `_compute_injection_price`

`_compute_injection_price(snapshot, entry, spot_prices)` returns the current-hour
EUR/kWh price only on the injection regime and only when the snapshot has injection
data (`injection.py:223-236`). Priority:

1. **Per-slot TOU** via `_tou_injection_rate` (`injection.py:171-190`).
2. **Spot formula** `factor * spot + base` when either the energy is
   `DynamicRates` (shape b) OR `inj.current is None` (shape c). If no spot is
   available it returns `None` rather than fabricate a value
   (`injection.py:203-205`). The spot is looked up on the contract's own grid
   (`RESOLUTION_QUARTER` when `_energy_is_quarter_hourly`, else hourly), snapped
   with `slot_start`, and a nearest substitute is accepted only within one billing
   slot (900 s quarter-hourly, 3600 s hourly) (`spot_stats.py:214-214`).
3. **Monthly indicative** `inj.current` otherwise, including static-energy cards
   whose injection carries a monthly index but also a printed `current` (Ecofix
   Flexy, EBEM Groen Variabel / B@sic+) (`injection.py:167-170`,
   `injection.py:210`).

This scalar is resolved once per coordinator tick, so it is not what the
`injection_price` sensor publishes when the injection varies intra-day. There the
sensor indexes `injection_hourly` at the current slot (`_current_injection`,
`sensor.py:114`), the same way the price sensors index `hourly`, nearest-slot
guard included: an unpriced slot resolves to an adjacent slot's rate, and the
scalar is reached only for a flat contract, which emits no array at all, or when
nothing lies inside the guard's window. Beware that a dynamic contract with a
hole in its curve therefore shows a neighbouring hour's rate rather than the
tick value the diagnostics dump reports.
The tick is a plain 60-minute interval anchored on setup, so publishing the scalar
directly made the sensor lag every band change by however far the tick had
drifted (issue #44, Engie Empower Flextime).

### Per-slot TOU injection: `_tou_injection_rate`

`_tou_injection_rate(inj, energy, when)` returns a per-slot rate only when the
energy is `TimeOfUseRates` and `inj.peak` is set (Engie Empower Flextime publishes
a peak/transition/super-off-peak feed-in triplet, monthly-realized)
(`injection.py:137-148`, fields at `providers/base.py:304-306`). It reuses the
energy contract's own `weekend_rule` via `tou_slot` so injection and consumption
agree on the slot for a given hour (`injection.py:141`). Returns `None`
otherwise so the caller falls back to the current / factor+base path.

### Historical injection: `_historical_injection_rate`

`_historical_injection_rate(injection, spot, *, energy, when)` mirrors the live
priority for a past hour: TOU slot first, then `factor*spot+base` when both the
formula and a historical spot exist, then `current` (`injection.py:261-282`).
The ordering (formula before `current`) is a bug fix: several dynamic-injection
contracts (Engie, OCTA+, TotalEnergies, Luminus, Mega) publish BOTH a `current`
indicative and `factor`/`base`, and checking `current` first made the YTD credit
use the flat indicative while the live sensor used the spot formula, so the two
user-facing numbers diverged (`injection.py:278-281`).

### Historical bug: monthly-indexed injection emitting an hourly factor

A monthly-indexed injection (EBEM Variabel/B@sic+, Eneco Fix/Flex, DATS 24,
EnergyVision 3 jaar vast / 1 an fixe) must
emit only the realized monthly `current`, never an hourly `factor*spot+base`,
because the indicative is the actual credit. The guard that keeps shape (b)/(c)
from swallowing these cards is the `inj.current is None` clause in both
`_injection_needs_spot` (`injection.py:94`) and `_compute_injection_price`
(`injection.py:169`): when a card prints a monthly `current`, the spot branch
is skipped and the realized rate is used, keeping the live sensor consistent with
the YTD credit for the same hour (`injection.py:167-170`). A latent mis-price
here is masked whenever the indicative prints, which is why it was fixed
explicitly rather than left to fall through.

### YTD injection paths

Past-month YTD billing routes injection per regime (`ytd_cost.py:271-437`,
context):

- `compensation`: per-hour `(cons - inj) * all_in`, netting injection against
  consumption (per band when bi) and clamping at zero.
- `injection`: per-hour `cons * all_in - inj * inj_rate`, where `inj_rate` comes
  from `_historical_injection_rate` (`injection.py:331-385`).

Shape (c) has a dedicated YTD helper `_ytd_spot_injection_credit`
(`ytd_cost.py:444`) that credits a static-energy contract whose injection is a
pure BELPEX formula with no fixed credit; it is a no-op unless the injection is
exactly that shape and an injection sensor is wired, and it skips hours with no
cached spot (`ytd_cost.py:478-480`).

## Capacity tariff

The Flanders capaciteitstarief is billed by the coordinator, not folded into the
per-kWh all-in. It is surfaced on its own `capacity_cost` sensor AND accrued into
`current_year_cost` through `_ytd_capacity`, so the running bill reflects what
Fluvius actually charges rather than the energy side alone. Monthly cost (`_compute_capacity`, `fees.py:75-84`):

```
capacity_cost_eur = peak_kw * overlay.capacity_eur_per_kw_year / 12.0
```

Returns `0.0` when the entry lost its `CONF_DSO` key, the overlay is missing, or
`capacity_eur_per_kw_year is None` (`fees.py:58-72`). The rate lives on
`DsoOverlay.capacity_eur_per_kw_year` (`providers/base.py:324`); Flanders digital
meters publish it, other regions leave it `None`.

`peak_kw` is the *billed* quantity, resolved by `_billed_peak_kw`, and applies
only in Flanders; outside Flanders the peak is reset to `0.0`.

Fluvius bills the "gemiddelde maandpiek": the mean of the last twelve monthly
peaks, where one monthly peak is the highest quarter-hour offtake of that month
(`de maandpiek is het hoogste kwartiervermogen van de maand`). So the tariff is
charged on a twelve-month mean, not on the month being accumulated:

```
billed_peak_kw = mean(max(monthly_peak, VREG_CAPACITY_FLOOR_KW) for the last 12)
```

`_track_monthly_peak` keeps the running month in `_peak_kw` and banks it into
`_peak_history` when the local 1st rolls over, pruning to the eleven most recent
completed months so the running one makes twelve. A month whose peak is still
`0.0` is not banked: that means no reading was ever collected (fresh entry, or
HA down throughout), which is not a measured zero and must not drag the mean
down. The history is persisted alongside the peak and is absent on blobs written
before it shipped, in which case the window simply starts over.

Two modes (`const.py:325-326`):

- `CAPACITY_MODE_FIXED`: use `CONF_CAPACITY_FIXED_KW` directly, bypassing the
  window (the user is stating a peak, not measuring one) and applying only the
  floor. A rolling max would ignore a mid-month decrease the user just made.
- `CAPACITY_MODE_SENSOR`: rolling max of a power sensor, scaled by its unit (W/VA
  scaled by 0.001 to kW; issue #19 was a 1000x inflation when W was stored as kW).
  Prefer the meter's own monthly-peak entity here: a DSMR 5B meter publishes the
  billed quarter-hour peak directly, whereas an instantaneous power sensor is
  sampled hourly and only approximates it (see config-flow.md).

The regulated floor is applied to EACH MONTH before the mean, not to the mean
(`VREG_CAPACITY_FLOOR_KW = 2.5`). Fluvius's estimation methodology gives the
formula outright: `Formule = Rekenkundig gemiddelde van de Max (Maandpiek (m),
2.5) voor elke maand (m) ... Er worden maximaal 12 maanden gebruikt`. The
placement matters: a household at 1.0 kW for eleven months with one 20 kW spike
bills on `(11 x 2.5 + 20) / 12 = 3.96` kW, where flooring the mean instead would
give 2.58 kW. Because every term is then at least the floor, the mean is too, so
no outer clamp is needed; the customer-facing FAQ describes that consequence
(`een minimumbijdrage ... die overeenkomt met een gemiddelde maandpiek 2,5 kW`)
rather than the mechanism.

A month the integration never measured is simply left out of the mean rather
than banked as a zero. That matches the regulator: Fluvius estimates a missing
month as the mean of the validated ones, and inserting a set's own mean into it
leaves the mean unchanged.

`monthly_peak_kw` reports the running month RAW, without the floor: it is a
measurement, and the floor is a billing rule that belongs on the billed quantity.
The `capacity_cost` sensor carries `billed_peak_kw` and `months_counted`
attributes so the two numbers can be told apart, the latter reaching 12 once a
full year of history has accumulated.

## Prosumer term

The prosumer (compensation-regime) fee is Walloon-only and monthly
(`_compute_prosumer`, `fees.py:195-210`):

```
prosumer_cost_eur = kva * (dso_rate + supplier_rate) / 12.0

  dso_rate      = overlay.prosumer_eur_per_kva_year        (DsoOverlay)
  supplier_rate = snapshot.supplier_prosumer_eur_per_kva_year   (SupplierSnapshot)
```

Returns `0.0` unless the regime is `compensation` AND the region is Wallonia AND
`CONF_SOLAR_KVA > 0` (`fees.py:139-159`). The Wallonia gate is deliberate:
compensation is Walloon-only, and billing a prosumer fee in Flanders on top of the
always-billed capacity tariff would double-count grid recovery
(`fees.py:153-154`).

The DSO rate lives on `DsoOverlay.prosumer_eur_per_kva_year`
(`providers/base.py:330-336`), published by Wallonia DSOs (valid until 2030 per
CWaPE) and `None` on Flemish SMR3 connections. The supplier-side forfait lives on
`SupplierSnapshot.supplier_prosumer_eur_per_kva_year`
(`providers/base.py:493-498`), billed on top of the DSO tariff; Cociter Variable
publishes one. It is already TVAC (VAT-incl) and summed raw, never VAT-scaled
(`fees.py:120-136`, `providers/base.py:497`).

Regime semantics (`const.py:308-315`): `compensation` ("compteur qui tourne a
l'envers") applies only to installations certified before 2024-01-01 and stays
valid until 2030-12-31; newer installations use the `injection` tariff (no per-kVA
fee); Flemish digital meters are SMR3 from the start. The YTD counterpart
`_ytd_prosumer` sums the monthly fee across the year using each month's archived
overlay, gated the same Walloon-only way (`ytd_cost.py:203-229`).

## Brussels OSP tier

The Brussels Brugel OSP (Obligations de Service Public) fee is a flat annual
Sibelga charge scaled by contractual connection power
(`_brussels_osp_fee`, `fees.py:87-96`):

```python
def _brussels_osp_fee(overlay, entry) -> float:      # fees.py:87
    if overlay is None or overlay.brussels_osp_by_tier is None:
        return 0.0
    tier = entry.data.get(CONF_CONNECTION_KVA_TIER, DEFAULT_CONNECTION_KVA_TIER)
    return overlay.brussels_osp_by_tier.get(tier, 0.0)
```

The table lives on `DsoOverlay.brussels_osp_by_tier` and is populated only on the
Sibelga overlay (`providers/base.py:325-329`). The user picks the tier in the
config flow; the four residential tiers are `le1_44`, `le6`, `le9_6`, `le13`
(residential connections are <=13 kVA), default `le6`
(`const.py:267-277`). Returns `0.0` outside Brussels or when the card omits the
OSP table. The fee is added to the Brussels annual cost in `_annual_static_fees`
(`fees.py:116`), not to the per-kWh all-in.

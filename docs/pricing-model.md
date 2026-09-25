# Pricing model

This document covers the pricing engine: the pure functions in `pricing.py` that
turn a supplier snapshot, a DSO overlay and a region into an all-in EUR/kWh
breakdown for a given hour, plus the injection (feed-in), capacity and prosumer
math that surrounds it in `injection.py` and `fees.py`. It is the reference for how each
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
source (`pricing.py`, `providers/base.py`). Every number comes from a
live-fetched `SupplierSnapshot`. All example numbers below are illustrative and
are taken verbatim from source comments; do not treat them as current rates.

The engine's central invariant is that a `PriceBreakdown`'s components sum to its
total, bit for bit:

```
energy + network + taxes == all_in
```

This holds because VAT is applied to each component separately and then summed,
never as `(e + n + t) * vat`, which would diverge by sub-femto-euro rounding once
`vat_rate` is non-zero (`pricing.py`, same reasoning at
`pricing.py` for `static_breakdown`).

## Public surface

`pricing.py` exports one result dataclass and a small set of pure functions.

```python
@dataclass(frozen=True)
class PriceBreakdown:      # pricing.py
    energy: float          # VAT-incl EUR/kWh, energy component
    network: float         # VAT-incl EUR/kWh, distribution + transport
    taxes: float           # VAT-incl EUR/kWh, per-kWh levies
    all_in: float          # == energy + network + taxes
```

| Function | Location | Returns | Purpose |
| --- | --- | --- | --- |
| `compute_breakdown(snapshot, dso_key, region, when, spot_eur_per_kwh=None, meter="mono", dso_tariff_mode="bi_horaire")` | `pricing.py` | `PriceBreakdown` | Top-level all-in EUR/kWh for one hour. |
| `energy_eur_per_kwh(energy, when, spot_eur_per_kwh, meter, region, dso_tariff_mode)` | `pricing.py` | `float` | Energy component; dispatches on the `EnergyRates` subtype. |
| `network_eur_per_kwh(dso, when, meter, dso_tariff_mode, region)` | `pricing.py` | `float` | Distribution + transport for the hour. |
| `taxes_eur_per_kwh(taxes, region)` | `pricing.py` | `float` | Per-kWh federal + regional levies that VAT applies to. |
| `taxes_vat_exempt_eur_per_kwh(taxes, region)` | `pricing.py` | `float` | Per-kWh levies billed at face value whatever the card's VAT basis (the Walloon connection fee). |
| `_routed_rate(base, energy, when, meter, region, *, bi_capable, dso_tariff_mode)` | `pricing.py` | `float` | Shared Fixed/Variable meter routing. |
| `tou_slot(when, weekend_rule="weekend_offpeak")` | `pricing.py` | `"peak"|"transition"|"offpeak"` | TOU band for a datetime. |
| `dso_impact_band(when)` | `pricing.py` | `"pic"|"medium"|"eco"` | Wallonia Tarif Impact band for a datetime. |
| `is_offpeak(when, region)` | `pricing.py` | `bool` | Classic bi-horaire off-peak test, per region. |
| `is_belgian_holiday(d)` | `pricing.py` | `bool` | Federal public-holiday test. |
| `static_energy_eur_per_kwh(energy, band)` | `pricing.py` | `float | None` | Stable (no time-of-day) rate for a band. |
| `static_breakdown(snapshot, dso_key, region, band, dso_tariff_mode)` | `pricing.py` | `PriceBreakdown | None` | All-in for a static band, used by the YTD/current-year path. |
| `yearly_fixed_fee_for_meter(energy, meter)` | `pricing.py` | `float` | Supplier yearly fixed fee for the meter type. |
| `slots_per_hour(resolution)` / `slot_delta(resolution)` / `slot_start(when, resolution)` | `pricing.py` | `int`/`timedelta`/`datetime` | Quarter-hour vs hourly grid helpers. |

The injection, capacity, prosumer and Brussels-OSP arithmetic is not in
`pricing.py`; it lives in `injection.py` (the feed-in) and `fees.py` (capacity, prosumer,
Brussels OSP) and is documented in the later sections.

## The all-in formula

For one hour, `compute_breakdown` computes three VAT-incl components and their
sum (`pricing.py`):

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
  levies (`pricing.py`); `energy_fund_eur_per_month` is defined on the
  `TaxOverlay` (`providers/base.py`) but is not touched here.
- `data_management_per_year` carries three different charges depending on the
  region, and one of them is tied to the tariff configuration. The Walloon
  `terme fixe` is not billed under the CWaPE incitative configuration that the
  cards sell as the IMPACT tariff, and `_walloon_fixed_term_applies`
  (`fees.py`) is what drops it; nothing offsets it, because CWaPE set the
  capacity term that replaces it to 0 EUR/kW for 2026 through 2029. The Flemish
  `databeheer` and the Brussels `mesure` plus fixed-term pair are billed
  whatever the mode says.
- The Wallonia `region_connection_fee` is a per-kWh term and IS billed, but
  through `taxes_vat_exempt_eur_per_kwh` (`pricing.py`), not
  `taxes_eur_per_kwh`. Engie's Walloon card prints `Redevance raccordement(8)`
  and footnote (8) reads *"Vous ne payez pas de TVA sur ces couts"* — the same
  footnote that exempts the Flemish energy fund on its Flanders edition.
  `_finalize_breakdown` adds it to the taxes component **after** the VAT factor,
  so it lands at face value. On a VAT-inclusive card (`vat_rate == 0`) the total
  is identical either way; on a professional Walloon card it was billed at
  0,0009075 EUR/kWh against the 0,00075 printed, about 9,45 EUR/yr at 60 000 kWh.

### Regional renewables selection

`taxes_eur_per_kwh` starts from the two always-present federal levies and adds
exactly one region's renewables surcharge (`pricing.py`):

| Region | Terms added (VAT-able) | Added VAT-exempt |
| --- | --- | --- |
| Flanders | `flanders_renewables` | — |
| Wallonia | `wallonia_renewables` | `region_connection_fee` |
| Brussels | `brussels_renewables` | — |

The `TaxOverlay` carries all three renewables columns; an extractor that operates
in only one or two regions leaves the others at `0.0`
(`providers/base.py`). Illustrative magnitudes from the dataclass
docstring: Flanders roughly 1.5 c/kWh, Wallonia roughly 3.1 c/kWh, Brussels
roughly 2.7 c/kWh (`providers/base.py`, illustrative).

### VAT handling and the vat_rate == 0.0 convention

Belgian residential electricity is billed at 6% VAT, and every scraped
residential card prints numbers that are already VAT-inclusive, so the snapshot
convention for them is `vat_rate = 0.0` and the multiplier `1.0 + vat_rate` is
`1.0` (`providers/base.py`, `pricing.py`). Under that convention
the reported components match what the PDF prints exactly.

A non-zero `vat_rate` means the opposite: the snapshot carries the numbers
**excluding** VAT, at the rate given. The expert custom supplier does this today
(the user types ex-VAT coefficients, default rate 0.06), and professional cards,
which print *"Prix tva exclue"* at 21%, do the same.

VAT then has to reach two kinds of value, and only one of them goes through the
pricing engine:

- **Per-kWh rates** are grossed per component in `_finalize_breakdown`
  (`pricing.py`), uniformly across energy, network and taxes rather than
  smeared into the taxes component. Applying it per component before summing is
  what keeps `energy + network + taxes == all_in` exact (see the invariant
  above).
- **Fixed and annual fees** - the yearly fee, data management, capacity, the DSO
  and supplier prosumer forfaits, the Brussels OSP table - never reach that path:
  the live, YTD, backfill and compare paths each sum them raw.
  `_resolve.apply_vat` (`providers/_resolve.py`) bakes them once instead.

`apply_vat` is called per config entry, from `_resolve_snapshot`
(`snapshot_resolve.py`), never before the shared snapshot cache: that cache is
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

### The federal energy contribution stops at a date, not at a card

The Belgian "bijdrage op de energie" was abolished as a line of its own on
2026-08-01 and folded into the special excise, which was flattened in the same
measure. It is a federal levy on consumption rather than a contract term, so
the DELIVERY month decides whether it is owed: a July bill still owes it, a
September one does not, whatever the card prints.

Three residential card families went on printing it into September 2026
(Cociter, Ecofix whose card is an OCR reading of a rasterized July one, and
TotalEnergies), so pricing their cards as printed billed a levy nobody
charges, roughly 7 EUR/year at 3.500 kWh.
`resolve_federal_contribution` (`providers/_resolve.py`) zeroes it for a delivery
month at or after `FEDERAL_CONTRIBUTION_ZEROED_FROM` (`const.py`), and
`_resolve_snapshot` (`snapshot_resolve.py`) applies it once on the way from a
stored card to a priced one, beside `apply_vat` and the two resolvers below.
Running there rather than in the parsers keeps the archive holding what each
card actually printed, needs no schema bump, and reaches every path that
rebuilds a bill. The month rows pass their own delivery month; every other
caller prices the running one.

Professional cards keep whatever they print, which is why the resolver takes
the flag: Bolt, Engie and Mega have all printed 0,0019261 ex-VAT on theirs
through the change and every month since, so the professional scheme keeps the
levy rather than three suppliers being stale in lockstep. The live check still
reports every disagreement against the month's consensus, which is how a stale
card gets noticed at all (`docs/ci-and-testing.md`).

### The special excise is the law's, not a stale card's copy of it

The same measure set the residential special excise at 4,876 c/kWh incl. VAT
from 2026-08-01. It is a federal levy on consumption, so one rate covers the
whole country in any month and two cards disagreeing about it is one of them
being out of date: on the September 2026 cards thirteen suppliers print it,
TotalEnergies prints it rounded to 4,88, and Ecofix prints July's 5,03288
because its card is a picture of July's card, which no parser change can read
differently (about 5,49 EUR/year at 3.500 kWh).

`resolve_federal_excise` (`providers/_resolve.py`) writes the law's figure for a
delivery month inside the window `FEDERAL_EXCISE_KNOWN_FROM` ..
`FEDERAL_EXCISE_KNOWN_UNTIL` (`const.py`), applied beside the contribution in
`_resolve_snapshot` (`snapshot_resolve.py`), and it is identity for a card that
already prints it. On the card's OWN VAT basis: most print the levy including
VAT and Ecopower prints it excluding, the engine grossing it later, so writing
one number into both would be 6% wrong for one of them. A professional card is
left alone, and so is any card carrying `federal_excise_bands`: that scheme
bands the levy by annual volume and is a different rate entirely.

The window is deliberately narrow. The measure steps the rate down again on
1 January 2027, 2028 and 2029, and only a step that is in effect and
cross-checked against the fleet's cards belongs in the constants; past the end
of the window the card is read as before. A schedule left to go stale would be
worse than the bug it fixes, and the live check's consensus row is what says a
step has landed.

Lapsing quietly is the failure mode that costs money, so the check asks for the
window before it closes: `_check_excise_window` (`scripts/live_check.py`) is
silent until eight weeks out, then reports a tax row naming the constants to
move and the announced next rate, and keeps reporting after the date has passed,
which is when the protection is actually gone. Nothing in the code can know the
next rate, so it asks a person, the same way the archive workflow warns before
its upload token expires.

### Degressive federal excise

The federal special excise is normally one rate, but a card may print it as a
schedule that decreases by annual consumption band. `TaxOverlay` then carries
`federal_excise_bands` as `((upper_kwh, eur_per_kwh), ...)` ascending
(`providers/_resolve.py`), and `resolve_excise_band` (`providers/_resolve.py`)
resolves it against the entry's yearly volume (`entry_annual_kwh`) and writes
one rate to `federal_excise`. The pricing engine never sees a band.

The schedule is billed PER TRANCHE, which the cards state outright: *"un tarif
degressif par tranche de consommation, calcule sur une base annuelle"*. So the
resolved figure is the BLEND over the year's volume (`blended_excise_rate`,
`providers/_resolve.py`), not the rate of the band the total lands in. At the
2026 professional schedule a 30.000 kWh site pays the first 20.000 at 1,421 and
the rest at 1,209, which is 405,10 EUR/year and a 1,3503 c/kWh blend; billing
all 30.000 at 1,209 gives 362,70. The engine prices per hour and cannot know
where in the year an hour sits, but it does not need to: the charge is defined
on an annual basis, so the year's total over the year's volume is the honest
per-kWh figure, and the annual bill is exact whenever the volume estimate is.

A volume past the last band is billed at the last band's rate for the
remainder. Residential cards leave `federal_excise_bands` at `None`, where the
resolver is identity.

### The Flemish network ceiling

The ceiling is the VREG's, not the card's. It is one rate for the whole of
Flanders per calendar year, so a card stating another one is wrong rather than
different, which is the reasoning `resolve_federal_excise` already follows for
the excise. `VREG_NETWORK_CEILING_HTVA` (`const.py`) holds it EXCLUDING VAT as
the regulator sets it, with a `KNOWN_FROM` / `KNOWN_UNTIL` window, and
`resolve_vreg_network_ceiling` (`providers/_resolve.py`) puts it on every Fluvius
overlay inside that window.

Five suppliers read it correctly and are unaffected: Luminus, Frank and
energie.be print 0,3472738 including the 6%, and energie.be's professional
card and Ecopower print the 0,3276168 behind it. DATS 24 is what settles that
it is not a per-DSO term, printing it as a COLUMN of its DSO table with eight
identical copies, one per Fluvius area. Mega and Bolt print 0,2035480 instead,
which would cap about 1,7 times too tight and UNDER-bill; Bolt prints that
figure on its Wallonia and Brussels cards too, where no VREG tariff exists,
and on its professional card without the VAT conversion Mega does make, so it
is boilerplate rather than a reading. Ten further suppliers print no ceiling
at all, so the cap simply never bound for their Flemish entries.

It is resolved BEFORE `apply_vat`, which rewrites `vat_rate` to 0 for an entry
that deducts VAT: a resolver running after it would read an ex-VAT card as
VAT-inclusive and put the ceiling 6% above the rate it is compared against.
Only the Fluvius overlays are touched, whatever regions the card covers.

The Flemish cards print a `maximumtarief` per kWh, and Ecopower states what it
does: *"zou u met het capaciteitstarief en het nettarief per kWh meer
nettarieven betalen dan met het maximumtarief? Dan betaalt u het
maximumtarief."* So the capacity charge plus the per-kWh network term may not
exceed the ceiling times the volume, and `_capped_capacity_annual`
(`fees.py`) takes the excess off the capacity term, which is the leg that
produced it.

It cannot be applied without a volume, and the volume it wants is a YEAR's:
the rule caps the two network legs against what the year carries, so a running
total cannot measure itself against it. `_annual_consumption_kwh` supplies the
same figure the excise band and the volume tranche resolve against
(`entry_annual_kwh`: a full year of meter, then the typed figure, then a shorter
measurement scaled up, then the household default), and
`_capped_capacity_monthly_eur` divides the capped year back into the month the
caller is accruing.

All three figures have to be on ONE VAT basis, and on a professional card they
were not. `apply_vat` grosses the overlay's EUR/year fees and leaves every
per-kWh rate as printed, because the pricing engine grosses those per
component; the ceiling is a per-kWh rate and was grossed with the fees anyway,
so the headroom came out as a gross ceiling minus a net distribution term.
The ceiling now stays as printed and the difference is grossed where the
comparison happens, against a capacity charge that already is. On a
residential card the factor is 1 and none of this moves. Every path that bills the charge goes through it -- the
live `capacity_cost` sensor, the year-to-date walk and the backfill's per-hour
accrual, each handing the cap the card's own `vat_rate` (the backfill handed the
default 0 and sat a fifth short on its headroom) -- because it used to sit on the
quote paths alone, and a card printing
a ceiling then had it honoured in the compare page and billed straight through
by the sensor the compare page is meant to match. At the 2026 rates and the
regulated 2,5 kW floor the ceiling binds under about 470 kWh a year, so nothing
moves for a household and the gap only ever showed on a garage box or a second
home.

### Monthly-indexed variable cards

A variable card can print a rate that is not what the contract bills. Cociter
Tarif Variable prints one computed from the PREVIOUS month's BELIX and labels it
so ("le prix indique est calcule avec l'indice BELIX du mois precedent ...
renseigne a titre indicatif"), while note (7) indexes the contract on the
delivery month's own arithmetic mean and settles it retroactively. Billing the
printed rate therefore bills last month's index: the April 2026 card's printed
12,6625 c/kWh is exactly the formula at March's BELIX of 92,61, and April
settled at 78,93 and 11,5749.

`VariableRates.month_indexed` marks such a card, and `_month_indexed_leg`
(`cohort.py`) resolves the coefficients against the delivery month's mean
through the same `SpotMonthlyRates` leg a signing cohort uses, so all five
costing paths follow one decision. BELIX is exactly the arithmetic monthly mean
the coordinator already computes, so nothing is approximated here.

Without an ENTSO-E key it returns `None` and the printed indicative stands: the
key is offered as an optional, skippable step to every contract the registry
flags `month_indexed_energy`, on any solar regime, and an entry that skipped it
is better served by a rate a month stale than by no energy leg at all.

Eneco Zon & Wind Flex and Flex One are the same shape on an RLP-weighted index,
Belpex-RLP-M, and Eneco closes the loop itself: every card footnotes "de
maandprijs berekend op basis van de laatst gekende waarde van Belpex-RLP-M
(08/2026: €133,4366/MWh)", the realised value of the month before, to four
decimals. `eneco.fetch_for_month` therefore looks one card ahead and settles an
archived month on the value the next card prints (`VariableRates.index_realised`,
with `current` re-resolved on it), which is the figure Eneco invoices. Measured
on the 2026 cards the printed estimate ran from 1,50 c/kWh over (April) to
2,50 c/kWh under (June). A month whose next card is not out yet keeps its
estimate and is flagged `SupplierSnapshot.provisional`, so the monthly cache
re-asks after its TTL instead of filing the estimate as a closed month's fact.

The running month has no published value yet, and Eneco's own rule for a
settlement that falls inside it is the index "berekend op basis van de tot dan
toe gekende waarden van Belpex Day Ahead Market". So the Flex cards carry
`month_indexed` and `rlp_indexed`, `_month_indexed_leg` re-prices them like
Cociter, and `_energy_month_spot` resolves the leg in this order: the month's
published `index_realised` when the splice found one -- which is asked FIRST
and needs no day-ahead cache at all, so an empty one still bills a settled
month exactly -- else the RLP-weighted mean of the cached hours (`_rlp_month_mean`, Synergrid's profile as `synergrid.py`
reduces it, reproducing Eneco's seven published 2026 values to the cent), else
the plain mean while the profile is not loaded. The feed-in credit keeps the
plain mean: Belpex-injectie is the arithmetic one, and the live tick computes the
two means separately for that reason. Without an ENTSO-E key the printed figure
stands, as on every month-indexed card.

`TimeOfUseRates.month_indexed` does the same for a time-of-use card that prints
one formula per band: Luminus SmartFlex, and Engie Empower Flextime, whose
"Flextime Heures pleines = 2,3228 + (0,1487 x EPEXDAM)" rows sit on the very card
whose Empower Variable rows were already re-priced. Both become the three-band
`SpotMonthlyRates` leg carrying the card's weekend rule.

The trihoraire card is the same contract on the three CWaPE bands and carries
the same note (7), so `ImpactRates.month_indexed` marks it and the three band
formulas become a `SpotMonthlyRates` leg carrying `factor_pic` / `factor_medium`
/ `factor_eco` with their ceilings, chosen per hour by `dso_impact_band`. Its
September 2026 card printed August's BELIX of 129,32 in every band; the month
to the 7th ran at 136,24, which moves PIC from 19,0079 to 19,7414 c/kWh.

### Monthly-indexed feed-in credits

The same lag applies to a feed-in credit. Eneco Power Fix, Flex and Flex One
print a credit "berekend op basis van de LAATST GEKENDE waarde van Belpex-injectie",
i.e. the previous month's, while the contract indexes it monthly and settles
retroactively. `InjectionRates.month_indexed` marks that, and the credit then
resolves against the delivery month's mean on the live tick, in both
year-to-date walks and in the backfill. Measured on the August 2026 card, the
printed 6,38 c/kWh against 8,0649 that August settles at: a 20,9%
under-credit.

Belpex-injectie is the plain arithmetic monthly mean of the Belgian day-ahead.
Checked against the real 2026 series it reproduces the card's own published
values to four decimals (March 92,6102 against the card's 92,6114, July
109,2488 against 109,2498), so nothing is approximated. `spp_indexed` remains
the solar-weighted sibling for cards that name Belpex_SPP; a card is one or the
other, never both.

A supplier that publishes its solar-weighted index settles the credit outright,
the way Eneco's footnote settles the consumption leg. Two do. Trevion names the
month in as many words on every card since March 2026, and settles both of its
legs that way (`docs/providers/trevion.md`). EBEM prints the month just closed
on every following card ("Voor de injectie is dit op basis van het
SPP0-lastprofiel. De SPP0 vorige maand bedroeg 79,11"), so `ebem.fetch_for_month`
carries it on `InjectionRates.index_realised` and rebuilds `current` from the
card's own coefficients, and `_spp_injection_spot` answers that figure before it
considers any mean. From May 2026 on the two publish identical figures, which is what
makes them the market's numbers rather than one supplier's; over the three
months before that they differ by up to 1,3 EUR/MWh, so each is settled on its
own card and never on the other's.
The mean it replaces is not the same number for these two. It weights each
hour's MEAN price by that hour's solar share, while both of their indices weight
each quarter by its own, and since PV output and the day-ahead price both move
inside the hour the two do not agree. Over January to August 2026 the computed
mean sat about 0,9 EUR/MWh above both published series in every single month,
worth roughly 1,8 EUR a year of over-credit at 2.000 kWh injected.

That is a difference in definition, not an error in the mean, and the hourly one
is not the wrong default: Energy Knights publishes its own settled Belpex-SPP-M
and the integration's hourly computation reproduces that series to 0,007%.
Computing every card at quarter resolution would simply move the 0,9 onto
Essentia. So the resolution is read as a property of the card, the way the RLP
blend is, and only the suppliers that publish a settled value are settled on
it.

Both flags live on the parsed snapshot, and the config flow cannot see a
snapshot before the entry exists. So the registry carries the same fact a
second time, as `Contract.spot_indexed_injection`, and the two have to agree.
When they do not, the parser wins on paper and loses in practice: no key step
means no spots, no monthly mean, and every path silently falls back to the
card's printed figure, which is the defect the flag was set to fix. Nine
contracts across five suppliers shipped in exactly that state.
`test_every_month_indexed_card_can_collect_a_key` holds the registry against
the live check's `_INJECTION_SHAPE`. A contract whose KIND already collects the
key for its energy leg (`dynamic`, `spot_monthly`) is exempt and leaves the
registry flag False, which is why energie.be Variabel does not set it.

A per-slot credit can be month-indexed too. Engie Empower Flextime prints one
EPEXDAM formula per Flextime slot on the injection side as well, so its triplet
carries `factor_peak` / `base_peak` and the two sibling pairs beside the printed
figures, and `_bake_monthly_injection` bakes the three slots on the delivery
month's mean while `_tou_injection_rate` resolves them the same way in the
historical walks. The slot is chosen by the energy leg's weekend rule whichever
shape that leg took this tick: `_tou_weekend_rule` reads it off a
`TimeOfUseRates` card or off the `SpotMonthlyRates` leg the card re-prices
through, so a Flextime entry holding a key does not fall off the slot rate onto
the flat Normal credit the moment its energy re-prices.

`month_indexed` also makes `_injection_is_spot_formula` return False outright.
Month coefficients are never a per-hour formula, and without that guard a card
that stopped printing its indicative would flip to crediting the current slot's
spot, which is the 0.6.7 mis-credit and is silent.

### The direct-debit reduction

Brusol's Groene stroom charges 250 EUR/yr and 230 on domiciliëring, the only card in the
registry that prices how the invoice is settled. `SupplierSnapshot` carries the REDUCTION
(`direct_debit_discount_eur`), the config flow asks the household how it pays
(`CONF_DIRECT_DEBIT`), and `resolve_direct_debit` (`providers/_resolve.py`) takes one off the
other before the snapshot is priced, clearing the field as it goes.

Resolved there rather than at the six places that read a standing charge (the live tick,
the year-to-date, the backfill accrual, the config-flow annual estimate and both comparison
quotes) for the reason the excise band and the volume tranche are: a transform that has to
reach every cost path is baked once into the snapshot the entry reads, and those paths keep
reading one fee and knowing nothing about how it is paid. It runs BEFORE the tranche, which
can turn a spot-monthly leg into a fixed one: the fee travels across that conversion but a
reduction still waiting to be applied would not. The result floors at zero, since a
reduction larger than the charge it comes off would pay the household to be supplied.

`_direct_debit` (`snapshot_resolve.py`) requires the stored answer AND the registry flag of
the card in hand, so on the comparison page the household's payment method reaches a target
whose card prices it and nowhere else.

### Volume-tiered energy

EnergyVision's tiered range prices a first tranche of the YEAR's volume at a
flat rate and only the remainder on a monthly formula: *"de vaste
tariefcomponent van het product Groene stroom is geldig voor 10 jaar en is van
toepassing op de eerste 1.800 kWh verbruik"*, with the rest on
`1,12 x Belpex-RLP-M + 20 EUR/MWh`.

The engine bills per slot and cannot know where in the year's cumulative volume
an hour sits, so the tranche never reaches it. `SpotMonthlyRates` carries the
pair as data (`tier_kwh` / `tier_rate`) and `resolve_volume_tier` folds it into
the coefficients before the snapshot is priced, the same arrangement
`federal_excise_bands` has with `resolve_excise_band`. A fixed tranche blended
with a formula that is linear in the index is still a formula linear in the
index, so no new rate kind is needed:

```
w * rate + (1 - w) * (factor * mean + base)
    == ((1 - w) * factor) * mean + ((1 - w) * base + w * rate)
```

with `w` the tranche's share of the entry's annual volume.

The blend is the annual bill exactly rather than an approximation of it, and
the card is what makes that true. Its *Voordeelzekerheid* clause settles the
year so the full tranche is charged at the fixed rate whenever the volume
allowed it, so the year costs `tier_kwh * rate + rest * formula` however the
pro-rata-per-day allowance fell across the months. That also makes the
bi-hourly split free: the card puts 900 kWh of allowance on each register, and
since both registers bill the same two rates, splitting per register and
blending once over the year reach the same annual total.

The volume comes from `entry_annual_kwh`, which is the one answer three legs
share (this tranche, the degressive excise band and the Flemish network
ceiling): a full year of the coordinator's daily `_annual_volume` measurement
first, then the figure typed on the entry, then a shorter measurement scaled up
to a year, then the 3.500 kWh household default. `_annual_volume` itself, which
is also the kWh the compare page and the projection multiply, ranks them in the
same order, so a card is never priced on one volume and multiplied by another:
before the two agreed, a 30.000 kWh business whose 90 days scaled to 52.000 had
its rates resolved on the stated figure and its rows on the extrapolation. Only a
professional card is ever asked for a typed figure, so before the measurement
was wired in every residential tiered entry split against that default: at
August 2026's index a 6.000 kWh household was billed 88 EUR/year under its own
card and a 2.000 kWh one 53 EUR over it. What is still not exact is the volume
itself, since a household under 90 days of history has none measured and a
wrong estimate moves the split proportionally.

The tranche is dropped rather than folded on an exclusive-night entry. The same
footnote that grants it withholds it there: *"van toepassing op de eerste 1.800
kWh verbruik van je enkelvoudig tarief ... Niet van toepassing op het exclusief
nacht tarief"*, with a day/night pair splitting it 900/900. The card prints no
per-register formula, so a night circuit falls through to the mono pair, and
folding the tranche into that pair credited it a discount it never receives.

And `current_price` shows the blend, because a
tiered contract genuinely has two rates at once and the blend is the annual
average of them, which is also the basis the card's own headline figures use.

A household inside the tranche has no variable leg at all and comes back as
`FixedRates`: leaving it as a zeroed-factor formula would price correctly and
still demand a monthly mean, and a month with no cached spot would then fail
the tick over a coefficient that cannot matter.

### Welcome credits

A card may grant a one-off credit to a new customer's first year. It is an invoice line
rather than a tariff, so it is subtracted from the bill after every component of it, and
only when the entry carries a contract start date: without one there is no first year to
place it in. `SupplierSnapshot` carries the amount and the rule its card states
(`welcome_credit_kind`), and `_welcome_credit_eur` (`fees.py`) applies it:

| kind | who | grant | cap |
| --- | --- | --- | --- |
| `pro_rata` | EnergyVision, four cards | accrued by the day across 365 days from the start date, totalling the printed amount over a full year | the supplier's energy component of the consumption (gross of any feed-in credit, never the network or tax legs) + the supplier's standing charge + the region's green / CHP contribution, prorated onto the credited days |
| `anniversary` | Frank Energie, three tiers · Mega, 17 of 21 contracts | the whole amount, in the window the first anniversary falls in and no other | Frank's card states none; Mega's states its own ceiling (`welcome_credit_cap_eur`, 848 EUR on the residential cards and 800 on the professional ones, which is the same figure ex-VAT) |

The two rules travel together on one field because each card states one complete rule
rather than two independent ones.

How long an anniversary card makes the household wait is its own, and not always a year:
Mega's Zen Fixed and Smart Flex, and their pro twins, grant the ristourne *"apres QUATORZE
mois ininterrompus de consommation ... et octroyee sur la premiere facture de
regularisation apres cette periode"* where every other card says douze.
`welcome_credit_after_months` holds it, `ristourne_wait_months` (`_mega_overlays.py`) reads
it, and `_months_after` (`fees.py`) counts in whole calendar months because the card does.
It moves only WHEN the lump lands, never how much: the amount is measured over the first
year, which is a different sentence on the same card. Paying at a flat 365 days put a
December signing's 320,65 EUR in the calendar year before the one it was paid in.

Mega's ristourne is not a lump. It is *"une reduction de 4.929 c EUR/kWh ... sur le prix de
l'energie ... pour votre premiere annee de consommation nette d'electricite"* plus a flat
cut off the standing charge, so the amount depends on how much the household uses. Most
cards put a ceiling over the total and some do not: of the 371 archived Mega cards granting
a credit, 74 state none at all (every Off-peak Fixed, Off-peak Flex, Off-peak Impact and pro
Off-peak Fixed, and three pro Cosy Fixed), and those are billed uncapped because that is what
their card says. `welcome_credit_eur_per_kwh` carries the per-kWh half and is measured on the volume the
energy price was actually charged on, because the card says *"nette"* and a reduction
*"sur le prix de l'energie"* can only come off what that price billed. Only the
compensation regime bills a netted register: the Walloon reversing meter turns back, so
what is billed already carries the export. The injection regime bills the gross draw and
credits the export on a separate line, and no-solar has nothing to net, so on those two
the export never comes off. Taking it off on all three credited a 3500 kWh site exporting
2500 on 1000 kWh of ristourne, 123,23 EUR under what the card grants it.

That volume is the first contract YEAR's, not the window being shown. `first_year_net_kwh`
(`fees.py`) resolves it from the entry's own yearly figure (`entry_annual_kwh`, the same
cascade the excise band and the volume tier read) scaled by the window's export share, and
the accrual inside `_welcome_credit_eur` is what places the result in the window. The three
windowed callers, the year-to-date sensor, the backfill accrual and the compare page's
year-to-date column, each convert; `_year_ahead_welcome_credit` does not, because the bill
it quotes is already an annual one. Passing the window's own volume billed the per-kWh leg
on whatever share of a year had elapsed: at the March anniversary of a 3500 kWh Mega Smart
Flex entry the engine credited 114,03 EUR against the card's 291,50, and a 20.000 kWh one
took 326,78 against its own 848,00 ceiling.
The flat half may also depend on how the household pays: *"une reduction de base de 37.1 EUR
+ 5.3 EUR supplementaires en cas de paiement par domiciliation bancaire"*, which
`welcome_credit_direct_debit_eur` holds and `resolve_direct_debit` settles once onto the
credit, the way it already settles the standing charge's own direct-debit cut.

The cards state the flat half three ways, and all three are read: the total with the split
beside it, the total alone where there is no split, and a flat-only ristourne with no
per-kWh term at all. Where a card prints both a total and the split, the two agree, which
is how the reading was checked across the range.

A fourth shape makes the payment method decide the whole offer instead of a share of it:
*"Si vous souscrivez a un nouveau contrat Cosy Flex **et optez pour la domiciliation**,
vous beneficiez d'une ristourne composee de ..."*, with no reduced alternative stated, so
a household paying another way is granted nothing. `ristourne_requires_direct_debit`
(`_mega_overlays.py`) reads it and `resolve_direct_debit` drops the base, the per-kWh leg
and the cap together. Cosy Flex, Smart Fixed and both their pro twins state it; pro Cosy
Flex has printed each wording in different months, which is why it is parsed off the card
rather than listed. The two readings are exclusive and the supplement wins: a card
printing it is offering the rest a smaller credit, not none.

Beside the ristourne every Mega card with a feed-in formula prints a bonus on the first
year's EXPORT: *"Si vous injectez de l'energie, en regime de commercialisation contrainte,
vous pouvez egalement beneficier d'un bonus de 1,06 c EUR/kWh (TVA de 6% incluse) ... pour
votre injection sur le reseau de distribution pour votre premiere annee de souscription"*,
1 c/kWh HTVA on the professional cards and up to 5,32 on Smart Flex.
`welcome_credit_injection_eur_per_kwh` carries it (`extract_injection_bonus`,
`_mega_overlays.py`), `apply_vat` grosses it only where the card taxes its feed-in, and
`_welcome_credit_eur` adds it after the ceiling, which the card states for the ristourne
alone. It is paid with the ristourne and on its wait, the two footnotes agreeing on every
archived card. Only a household whose export is SOLD earns it: that is the injection
regime, since under compensation the meter nets the export and there is no feed-in price
to add a bonus to. The volume is a full measured year of that export
(`entry_annual_injection_kwh`, measured daily beside the consumption volume) or nothing,
the rule the projection already holds feed-in to, because PV is too seasonal for a short
window scaled by a day count. The compare page's year-to-date columns quote through a
proxy entry that carries no measurement, so they credit no bonus on either side; the
annual columns and the projection credit it on the year of export they already quote.
The Smart cards add "et disposez d'une puissance de raccordement inferieure ou egale a
10 kVA", which the entry cannot answer: the flow asks no connection power on the
injection regime, so the bonus is credited as printed.

Seventeen products therefore depend on how the household pays, and the flow asks only
where `direct_debit_discount` is set in the registry (`_DIRECT_DEBIT_RISTOURNE`,
`mega.py`). A product whose card states either dependence and which is missing from that
set is never asked, so the resolver is handed a default and the card's wording never
reaches the bill.

Luminus states its credit two further ways, as a new-customer CAMPAIGN tied to the month
of signing: *"En tant que nouveau client, vous beneficiez d'une remise de 33% sur les
couts energetiques, non-valable sur un compteur exclusif nuit, ... pendant 12 mois pour la
conclusion d'un contrat Luminus Comfy Electricite en septembre 2026"*, or as a volume,
*"une remise de 750 kWh"* paid *"12 mois apres la date de debut via un cashback"*.
`welcome_credit_pct_of_energy` and `welcome_credit_kwh` carry them.

Both need a rate to become money, and each takes the rate its own sentence names.

The percentage takes the household's OWN realised rate over the window
(`window_energy_rate`, `fees.py`): the supplier's energy component divided by what the
window drew. Its sentence names both registers (*"en heures pleines et creuses"*), and a
realised rate is what makes that work on every rate shape, blending a bi-hourly card by
the hours actually drawn rather than needing register weights, and a variable or
spot-priced card by what it really billed. It is measured over the contract's own
first-year days inside the window (`in_first_contract_year`, `fees.py`), which the
year-to-date walks and the backfill each sum beside the window's total: the campaign is a
share of what that contract charged, and the window's months before the start carry a
rate it was never a share of. Measured over the whole window, a Luminus Comfy signing on
5 September was credited at January to September's blended rate on the year and at
September's own on the month, and its months summed to 3,53 EUR less than its year. A
window holding none of those days (an anniversary lump paid in a later year) falls back
to the window's rate.

The volume takes the card's own single rate (`static_energy_eur_per_kwh`, band `single`),
because its sentence names one register: *"le prix unitaire en EUR/kWh TTC du cout de
l'energie, applicable aux compteurs mono-horaires tel qu'indique dans les presentes
conditions particulieres, par 750 kWh"*. Valuing it at the realised rate instead cost a
bi-hourly household 0,89 to 4,50 EUR and an exclusive-night one 16,28 to 18,52, and on the
two variable cards it floated with the year where the clause pins the signing card. A card
publishing no single rate falls back to the realised rate rather than crediting nothing.

A percentage then folds into the per-kWh leg and a volume into the flat one, so neither
opens a path of its own and both inherit the cap, the accrual and the VAT basis. The percentage is paid *"au pro rata sur
vos prochains decomptes"*, which is the pro-rata kind, where a volume is a cashback at the
wait the card states.

Two rules the cards make necessary:

- The campaign is read from **its own sentence**, anchored between *"En tant que nouveau
  client"* and the signing gate. The same block prints standing loyalty discounts in
  nearly the same words, *"12 mois apres la date de debut, une reduction de 5 % sur les
  couts energetiques ... pendant 12 mois ... au pro rata de la consommation de votre 2e
  annee"*, which are a year-2 and year-3 benefit this does not model. They also say
  "pendant 12 mois" and carry their own exclusive-night exclusion, so reading the card as
  a whole attributed that exclusion to the campaign.
- `welcome_credit_excludes_night_meter` is settled by `resolve_welcome_credit_meter`
  against the entry's own meter, beside the direct-debit answer and for the same reason.

What a past month can be credited is bounded by what was CAPTURED. Luminus's own archive
endpoint serves a month's tariff without its campaign, so a signing before the project's
archive began capturing live texts cannot be recovered: the promo exists only on the live
card during its own month. The archive has captured since September 2026, `_month_cache`
asks it before the supplier's own archive, and `archive_cards.py` replays every stored
text through the current extractor whenever the parser changes, so months from then on
resolve for every entry whatever date it was installed. A future card that pro-rates without a cap, or caps a
lump, is what would split them.

The cap counts only the three components the card names. Not the energy fund, the
data-management charge or the Brussels OSP fee that sit beside the standing charge in
`_annual_static_fees`, which is why `_ytd_static_fees` reports the supplier's share
separately; and not the all-in figure the walks bill, which carries the network and tax
legs and is net of the feed-in credit, which is why both walks keep the supplier's energy
component beside it (`energy_component_ytd_eur`). Measured against the all-in figure the
cap let a 365 kWh/year connection through at 42,61 EUR over a quarter where the card
grants 26,28, and capped a site exporting more than it used below its own energiekost.
It binds only on a very small connection: around 1.100 kWh a year against a 200 EUR
credit.

The amount is read from the SIGNING month's card (`signing_month_snapshot`), because the
credit belongs to the product version signed and EnergyVision moved its figure four times
between March and September 2026.

The compare page credits both sides, or the household's real bill sits beside alternatives
priced as though nobody had ever been granted one, up to a whole credit in the household's
favour. The year-to-date column walks the same engine for a candidate and credits it on
the entry's own start date, off the candidate's current card: what this year would have
cost on it, signed when the household signed its own. The annual column credits a
candidate what a customer signing it today is granted over the coming year
(`_year_ahead_welcome_credit`: 365 days plus the day a lump paid *"na een jaar"* lands on),
and the household's own row whatever is left of its first year. `_annual_welcome_credit`
re-walks the energy leg on its `energy` component for the cap, and `_annual_bill` takes
the credit off last. A Frank Korting tier therefore ranks on the year it would cost,
cashback included, instead of as JN's loser. `projected_year_cost` takes the same
remaining first-year share off its full year, read off the signing month's card the way
the running bill reads it, and reports it as `welcome_credit_eur`; before that the two
full-year figures on one entry disagreed by the credit.

A negative `yearly_fixed_fee` was the obvious shortcut and is the wrong model: a fee box
has no expiry and no cap, so it would keep taking the discount off every year with nothing
to flag it, and would be right only for households that never hit the cap.

The statistics backfill (`_backfill_cost_sensor`) subtracts the same credit per day, off
the same signing-month card and capped against the same three running components, so the
imported `current_year_cost` series meets the live sensor at the end of every day instead
of at a step of everything credited so far.

### Contractual price ceilings

Mega Cap was the one product that capped what the commodity can cost: *"la
composante energie facturee est limitee a un plafond de ... vous payez le
minimum entre les prix variables mensuels et ce plafond"*, per meter, and
guaranteed a year from the start of supply. Mega retired it in September 2026, and the clamp is
driven by the card rather than by the product, so it carried straight over to
the next card that printed one: Cociter's September 2026 variable and
trihoraire issues cap supply at 26,5 c€/kWh TVAC under their own footnote 8.
`VariableRates` carries the four `ceiling_*` columns, `ImpactRates` the three
per-band ones, and `energy_eur_per_kwh` takes the `min()` of the routed rate
and the routed ceiling in both. `static_energy_eur_per_kwh` takes it too, so
the per-day walk behind `current_year_cost` bills the same capped rate: the cap
used to be dropped there, which meant the running bill would climb past the
plafond in the one situation the plafond is sold for.

The ceiling routes on its own half-pair rule rather than the rate's, which is
what the hourly branch gets for free by handing the four columns to
`_routed_rate` as a rate sheet of their own: a card printing a mono cap beside
a bi-hourly rate pair caps both bands at the mono figure.

Per SLOT, not on a mean: a ceiling is a `min()`, so clamping a monthly or
annual average would let an expensive month shelter under a cheap one, which is
the opposite of what the guarantee sells. The ceiling covers the energy
component only; network, taxes and surcharges stay due in full. Cards that cap
nothing leave the columns `None` and price exactly as before.

## Energy rate by contract kind

`energy_eur_per_kwh` dispatches on the runtime type of `snapshot.energy`
(`pricing.py`). The six `EnergyRates` subtypes are
`FixedRates | VariableRates | DynamicRates | SpotMonthlyRates | TimeOfUseRates | ImpactRates`
(`providers/_rates.py`). The `TariffKind` string on a `Contract` is
`"fixed" | "variable" | "dynamic" | "tou" | "tou_impact" | "spot_monthly"`
(`providers/_rates.py`).

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
(`pricing.py`). Priority order:

1. `meter == "exclusive_night"` and the card published an `exclusive_night` rate:
   use it (`pricing.py`).
2. `bi_capable` (meter is `bi` or `dynamic`) and both `peak` and `offpeak` are
   published: pick one by schedule (`pricing.py`):
   - Under `dso_tariff_mode == "impact"`: ECO band bills off-peak, MEDIUM/PIC bill
     peak (`pricing.py`). This aligns the energy side with the Impact-banded
     distribution when an SMR3 meter registers in CWaPE bands.
   - Otherwise: `is_offpeak(when, region)` picks off-peak vs peak
     (`pricing.py`).
3. Fall back to the single/current `base` rate (`pricing.py`).

`FixedRates` fields: `single`, optional `peak`/`offpeak`/`exclusive_night`, plus
`yearly_fixed_fee` and `yearly_fixed_fee_exclusive_night`
(`providers/_rates.py`). `VariableRates` mirrors it with `current` in place
of `single` and an optional `formula` string (`providers/_rates.py`).
Suppliers that publish only a mono rate (e.g. Eneco Power Flex) leave
`peak`/`offpeak` `None`, and routing falls through to the single rate for every
meter type (`providers/_rates.py`).

### Dynamic: `factor * spot + base`

`DynamicRates` computes `factor * spot_eur_per_kwh + base` and raises
`ValueError("dynamic tariff needs a spot price")` when `spot` is `None`
(`pricing.py`). The spot is the ENTSO-E BE day-ahead price for the slot.
`DynamicRates.quarter_hourly` selects whether the contract bills on the native
15-minute grid (Cociter, EBEM, Ecofix, Ecopower Dynamische Burgerstroom,
energie.be, Energy Knights Agilior Online, EnergyVision, Engie, OCTA+, Trevion)
or the hourly-aggregated curve (Eneco, Energy Knights Agilis Online, Frank by
default, Luminus, Mega, TotalEnergies).
Bolt and Frank sell one card on both settlements, so their entries answer for
themselves and `resolve_settlement_grid` sets the flag; YTD billing stays hourly
regardless
(`providers/_rates.py`). See [data-sources.md](data-sources.md) for how the
curve is fetched and the grid helpers `slots_per_hour` / `slot_delta` /
`slot_start` (`pricing.py`).

### Spot-monthly: `factor * monthly_mean(spot) + base`

`SpotMonthlyRates` runs the exact same formula as `DynamicRates`, and raises
`ValueError("spot-monthly tariff needs a monthly mean spot")` when `spot` is
`None` (`pricing.py`). What differs is what the caller threads through
the `spot_eur_per_kwh` parameter: not the live slot price, but the arithmetic
mean of the delivery month's hourly Day-Ahead spot, which the coordinator
computes off its ENTSO-E cache. Reusing the one parameter keeps pricing a pure
formula with no month arithmetic of its own.

The rate is therefore a single flat value for the whole month, so the contract
always bills on the hourly grid and carries no `quarter_hourly` flag. The
current month's mean is a running estimate until the month closes
(`providers/_rates.py`).

Used by group-purchase style products that index the commodity to the realized
monthly average (the Mega iChoosr / Samen Overstappen groepsaankoop shape), and
by the expert **custom** supplier. It is also the leg a variable card is
re-priced onto for a signing cohort, which is why it carries
`yearly_fixed_fee_exclusive_night`: the variable cards it inherits from (EBEM
Groen Variabel / B@sic+) print a separate exclusive-night standing charge.

It also carries `factor_peak` / `factor_offpeak` for a card that prints one
indexation formula per meter. Mega does, and the bands differ by a fifth: mono
`Epex x 1,1095 + 3,6`, peak `x 1,3275`, off-peak `x 0,94`. Only the mono pair
was parsed, so a bi-hourly signing cohort was re-priced onto it for every hour
of the month, over-charging its peak hours and under-charging its off-peak
ones. The band rule is the one `_routed_rate` uses, Impact comptage included.

The index is exact where the leg says which blend it is. An `rlp_indexed` leg is
resolved against Synergrid's RLP profile in the blend it names (see the data
sources, Part 3): Eneco's Belpex-RLP-M against the equal mean of the three
distinct regional curves, Energy Knights Essentia against the Fluvius curve
alone, each reproducing its own supplier's published 2026 values to the cent.
EBEM Groen Variabel and B@sic+ name the Fluvius curve in as many words, "het
gemiddelde van de RLP's in Vlaanderen van Synergrid", and the workbook says
those are one curve: all eight Flemish columns carry identical values, against
one shared Walloon curve and one for Brussels. So the blend is settled by the
wording, and what was left was the RESOLUTION. Against the eight published 2026
values the Fluvius curve at quarter-hour resolution has a mean absolute error of
0,002 EUR/MWh, which is every month reproduced exactly; the same curve on hourly
prices is 0,19 low, the column-weighted one 0,71 and the equal mean of the three
curves 1,87.

Resolution is a property of the card, not a better way to compute. Eneco's
Belpex-RLP-M goes the other way: the hourly computation reproduces its eight
published values to the cent, and the quarter-hour one misses each by 0,08 to
0,21. Each supplier weights the quotation it contracts on, and the integration
keeps the hourly form for both. The 0,19 EUR/MWh EBEM loses by it is worth about
0,02 c/kWh and reaches only the RUNNING month, because a closed EBEM month is
settled outright on the figure the following card publishes (`ebem.py`); the
estimate that figure replaces is the previous month's index, 15,62 EUR/MWh out
on average, which is the error that actually mattered.
energie.be Variabel against the column-weighted mean, every DSO sub-area counting
once, which is how it publishes the same phrase Eneco reads as the distinct
mean. Each blend was settled against the supplier's own published table rather
than against the wording, which is identical on two of the three cards. A leg
that names no blend keeps the plain arithmetic monthly mean, a few percent
below any of them, since households draw in the expensive hours; the note lives
on `VariableRates.formula_factor` so that is not mistaken for an oversight.

### Time-of-use: `tou_slot`

`TimeOfUseRates` has three published rates `peak`, `transition`, `offpeak`, and a
`weekend_rule` (`providers/_rates.py`). `tou_slot` maps a local datetime to
its band (`pricing.py`).

Shared weekday schedule:

| Band | Weekday hours |
| --- | --- |
| peak | 07:00-11:00 and 17:00-22:00 |
| transition | 11:00-17:00 and 22:00-01:00 |
| offpeak | 01:00-07:00 |

Federal Belgian holidays follow the weekend rule, not the weekday rule
(`pricing.py`). The weekend rule differs by product (`pricing.py`):

| `weekend_rule` | Weekend/holiday behaviour |
| --- | --- |
| `weekend_offpeak` (generic CWaPE default) | Whole weekend off-peak. |
| `weekend_no_peak` (Engie Empower Flextime) | Never peak; transition 07:00-11:00 + 17:00-01:00, offpeak 01:00-07:00 + 11:00-17:00. |
| `smartflex_seasonal` (Luminus SmartFlex) | Seasonal bands applied every day, no weekend exception. |

The `smartflex_seasonal` rule ignores weekday/weekend entirely and keys on season
(`pricing.py`): peak 07:00-11:00 + 17:00-22:00 both seasons; the
11:00-17:00 midday window is off-peak in spring/summer (21 March to 20 September
inclusive, `_is_smartflex_summer`, `pricing.py`) and transition otherwise;
22:00-07:00 is always transition. The "free Sundays" promo is a first-year
discount and is out of scope (`pricing.py`).

### Impact: `dso_impact_band`

`ImpactRates` (`tou_impact` kind) is Wallonia's Tarif Impact, distinct from TOU
because its schedule is the CWaPE-defined Impact one with no weekend exception,
matching the DSO Impact distribution tariff that gates eligibility
(`providers/_rates.py`). Fields: `pic`, `medium`, `eco`
(`providers/_rates.py`). `dso_impact_band` (`pricing.py`):

| Band | Hours (every day) |
| --- | --- |
| pic (highest) | 17:00-22:00 |
| medium | 07:00-11:00 and 22:00-01:00 |
| eco (lowest) | 01:00-07:00 and 11:00-17:00 |

Source cited in the docstring: TotalEnergies Impact card footnote 7 / ORES
"Comprendre ma facture / Impact" (`pricing.py`). Requires an SMR3
quarter-hourly meter and an opt-in to the DSO Impact tariff
(`providers/_rates.py`).

The bands are 5 / 7 / 12 hours a day, the 35 / 49 / 84 per week the cards
quote, and `dso_impact_band` is the only place that says so. The OptionsFlow
annual estimate used to count them into a per-band weighting and price each
band from one representative hour; it averages a representative WEEK instead,
because one sample per band is exact only when both the energy and the network
leg are banded on this schedule, and an Impact card on a standard bi-horaire
connection (or a time-of-use card on an Impact one) is not.

## Meter routing

`MeterType` is `"mono" | "bi" | "dynamic" | "exclusive_night"`
(`pricing.py`, `const.py`). A digital (SMR3) meter registers
peak/offpeak just like a bi-hourly meter, so `bi_capable = meter in ("bi",
"dynamic")` on both the energy and network sides (`pricing.py`,
`pricing.py`). The Belgian meter conventions are documented at
`pricing.py`.

### Supplier (energy) side

Handled by `_routed_rate` for Fixed/Variable (see above). An exclusive-night
meter physically only registers during DSO off-peak hours, so the code does not
gate it by `is_offpeak`; it just takes the `exclusive_night` rate when published,
else falls back to single/current (`pricing.py`).

### DSO (network) side

`network_eur_per_kwh` returns `distribution + dso.transport`
(`pricing.py`). Distribution selection, in strict precedence order:

1. **Exclusive night** (`pricing.py`), resolved BEFORE the Impact band so
   a dedicated night circuit bills its own rate even under Impact mode. Fallback
   chain: `distribution_exclusive_night` -> `distribution_offpeak` ->
   `distribution_single` (`pricing.py`). Each step is closer to the real
   bill than the day rate.
2. **Impact** (`pricing.py`), only when `dso_tariff_mode == "impact"` AND
   all three of `distribution_pic`/`medium`/`eco` are non-`None`. The all-three
   guard exists because `python -O` strips `assert`, and a partially populated
   triplet would otherwise raise `TypeError` on `None + transport`; treating
   Impact as available only when complete falls through to bi-horaire/single on
   cards that omit it (Brussels Sibelga, Flanders Fluvius)
   (`pricing.py`).
3. **Bi-horaire** (`pricing.py`), when `dso_tariff_mode != "simple"`, the
   meter is `bi`/`dynamic`, and both `distribution_peak`/`offpeak` are published:
   `is_offpeak(when, region)` picks the rate.
4. **Single** (`pricing.py`), the fallback for everything else, including
   `dso_tariff_mode == "simple"` and mono meters.

`DsoTariffMode` (`"simple" | "bi_horaire" | "impact"`, `pricing.py`,
`const.py`) is orthogonal to the supplier meter: it is the billing mode
set on the user's grid connection, and the coordinator falls back automatically
when the DSO does not publish Impact rates (`const.py`).

### is_offpeak schedule, per region

`is_offpeak` differs by region and Wallonia changed on 2026-01-01
(`pricing.py`):

| Region | Off-peak schedule |
| --- | --- |
| Flanders (Fluvius) | Mon-Fri 22:00-07:00 and all weekend. Weekday public holidays bill at the DAY rate (the meter clock switches on weekday/weekend only). |
| Brussels (Sibelga) | 22:00-07:00, all weekend, AND weekday public holidays (historical Brussels exception). |
| Wallonia (from 2026-01-01) | One uniform schedule every day including weekends and holidays: 22:00-07:00 and 11:00-17:00. |

`is_belgian_holiday` covers the seven fixed federal dates plus Easter Monday
(+1), Ascension (+39) and Pentecost Monday (+50) off Gregorian-computus Easter;
regional holidays are deliberately excluded because DSO billing applies federal
rules uniformly (`pricing.py`). The fixed-holiday set is lifted to module
scope so it is not reallocated on every call along the 8760-iteration backfill
path (`pricing.py`).

### Exclusive-night yearly fee routing

`yearly_fixed_fee_for_meter` bills the dedicated `yearly_fixed_fee_exclusive_night`
on an exclusive-night config entry when the card prints one (EBEM Groen Variabel),
otherwise the standard `yearly_fixed_fee` for every meter type
(`yearly_fixed_fee_for_meter`, `pricing.py`).
Three rate shapes carry the dedicated field: `FixedRates`
(`providers/_rates.py`), `VariableRates` (`providers/_rates.py`) and
`SpotMonthlyRates` (`providers/_rates.py`), the last because a variable card
re-priced onto a monthly-mean leg for a signing cohort keeps the separate charge
its card printed. An exclusive-night circuit is configured as a SECOND config
entry pointing at the night kWh sensor; the primary day meter stays
mono/bi/dynamic (`const.py`).

## The static path

`static_energy_eur_per_kwh` and `static_breakdown` produce a stable, no-time-of-day
rate for the current-year-cost / YTD sensor when the contract has one
(`pricing.py`).

`static_energy_eur_per_kwh(energy, band)` returns a rate for `band in
("single","peak","offpeak")` for Fixed and Variable, falling back to
single/current when the card publishes no split (`pricing.py`). A
HALF-published pair counts as no split, the same rule `_routed_rate` applies on
the hourly path: filling the missing half from the single rate reads a rate the
card never printed for that band, and it made the two walks disagree about one
entry, the hourly engine billing the single rate around the clock while the
per-day walk billed the peak rate for peak hours. It
returns `None` for `DynamicRates` (no constant rate), `TimeOfUseRates` (3-band
schema does not map onto the bi-hourly convention) and `ImpactRates` (per-band
rates vary by hour, caller must use the hourly path) (`pricing.py`).

`static_breakdown` assembles the all-in for one band with the same VAT-per-component
rule as `compute_breakdown` (`pricing.py`). It returns `None` when the
energy has no stable rate, and also when `dso_tariff_mode == "impact"` and the DSO
publishes Impact distribution: Impact distribution cannot collapse to
single/peak/offpeak, so the YTD path must read hourly statistics instead
(`pricing.py`). Distribution selection here mirrors the network side:
`simple` -> single, the band when the overlay publishes BOTH columns, else
single (inside `static_breakdown`, `pricing.py`). A half-published
pair counts as no split here for the same reason it does on the energy leg:
`network_eur_per_kwh` needs both columns before it bands, so testing one band
at a time billed the published half for its own hours while the hourly engine
billed the single rate around the clock. Only a custom entry can be in that
state, since its two distribution boxes are independently optional and every
parsed card binds the pair together.
A missing `dso_key` raises `KeyError` with the available
keys (`pricing.py`, same guard in `compute_breakdown` at
`pricing.py`).

## Injection (feed-in) math

Injection is computed in `injection.py`, not `pricing.py`, but it consumes the
same snapshot and `tou_slot` rule. `InjectionRates` carries a monthly indicative
`current`, an hourly formula `factor`/`base`, an optional per-slot TOU triplet
`peak`/`transition`/`offpeak`, and a `formula` string (`providers/_rates.py`).

**VAT-exempt invariant.** Belgian residential injection is exempt from VAT, so
`InjectionRates` values are NEVER VAT-inclusive regardless of the consumption
snapshot's `vat_rate` (`providers/base.py`). None of the injection code
paths multiply by `1.0 + vat_rate`.

Injection formulas can go negative at low spot (the producer pays to inject) and
the engine respects that by default. A contract carrying a never-negative
guarantee sets `floor_at_zero` instead, and `_floor_injection`
(`injection.py`) then clamps the resolved rate at 0 in
`_compute_injection_price`, in `_historical_injection_rate` and in the compare
estimate. Only the expert custom supplier sets it (`providers/custom.py`);
every scraped card leaves it False.

WHERE the clamp lands is a pricing decision, not a detail, because `max()` is
convex and the two orders give different money:

- a PER-SLOT formula floors each slot, because that is what the contract bills.
  The live array and the year-to-date replay (off the hour's own quarters) credit
  each slot at its own rate; the compare estimate (`_compare_injection_credit`,
  `compare_weighting.py`) has to collapse the window to one number, so it takes
  the mean of the floored rates weighted by the household's own export shape
  (`_export_weighted_credit`, `compare_weighting.py`), which is the basis the
  year-to-date walk bills on.
- a MONTH-MEAN formula floors once, on the delivery month's tariff, because such
  a card publishes one number a month and the guarantee is written against that
  number. Which of the two a credit is, the live tick and the compare page decide
  with one rule (`_injection_bakes_to_month_mean`, `injection.py`) asked of the
  PRICED leg, so a signing cohort on a quarter-hour card (Trevion LifePowr signed
  from March to May 2026) is credited per slot even though today's card is
  monthly. `_bake_monthly_injection` (`injection.py`) produces it and the floor
  lands on the flat `current` path. Every quote path calls that one helper: the
  compare estimate used to resolve only the Belpex_SPP cards and leave the rest
  to the live helper, which answers the card's printed figure on a snapshot
  nothing has baked, so the page quoted last month's index beside a sensor
  showing this month's. The projection reads the coordinator's already-baked leg
  for the same reason.

The per-slot TOU triplet is never clamped. No card ships both a triplet and a
floor, and `tests/test_custom.py` pins that rather than the pricing code
carrying a branch that cannot run.

The compare page's simple model, the one it falls back to when a side keeps
no archive, a spot-priced side has no day-ahead for the window, or the archive
engine throws, carries the welcome credit on its year-to-date rows too
(`_ytd_welcome_credit`, `compare_quote.py`). Scoped to the window rather than the
year the annual rows quote, with the standing charge in the eligible base
prorated the way the bill beside it prorates it. It carried none at all, so a row
the engine priced included a credit and the same row from the fallback did not,
beside annual figures that always do:
`test_every_compare_annual_bill_carries_a_welcome_credit` reads the source and
holds every call to it.

When the compare page has no measured export shape it falls back to weighting
each slot by how long it is active, and those durations are counted off
`pricing.tou_slot` itself over a year (`_tou_slot_hours`, `compare_weighting.py`)
rather than written out a second time. Three things depend on that. A seasonal
rule has no representative week at all: Luminus SmartFlex moves its 11:00-17:00
block between super-creuses and creuses on 21 March and 20 September, so the
weekly triple this replaced answered the generic rule for it and put the whole
midday block, which is when a solar household exports, in creuses all year.
Federal holidays land in the weekend slot the cards give them, worth 2,7 hours a
week off the weekday slots. And a weekend rule added later is weighted without
anyone remembering to come back, which is how the third one went unweighted.

### The four injection shapes

| Shape | Populated fields | Needs spot? | Example |
| --- | --- | --- | --- |
| (a) Monthly indicative | `current` set | No | Ecofix Flexy, the fixed and variable cards that publish a realized rate |
| (b) Hourly formula | `factor` + `base` set | Yes | Dynamic contracts (Engie, Luminus, Mega, OCTA+, TotalEnergies) |
| (c) Spot-indexed on a static-energy card | `factor` + `base` set, energy NOT dynamic, and either `current is None` or the card flags `slot_indexed` | Yes | Cociter Variable and Variable Trihoraire, every Bolt fixed and variable card. The compare estimate and the projection, which multiply one rate by a whole year's export, price the formula per slot over the day-ahead the coordinator holds, weighted by the household's export shape (`_compare_injection_credit`, `compare_weighting.py`); with no day-ahead on hand the projection leaves the credit out and says so |
| (d) Month-indexed formula | `current` + `factor` + `base`, flagged `spp_indexed` or `month_indexed`; or the per-slot triplet with its three `factor_*` / `base_*` pairs and `month_indexed` | A monthly MEAN, not an hourly spot | DATS 24, EBEM Variabel/B@sic+, Eneco Fix/Flex/Flex One, energie.be, Energy Knights Essentia, EnergyVision fixed (both regions), Trevion Flex/LifePowr; Engie Empower Flextime per slot |
| (e) Register pair | `current` + `peak` + `offpeak`, flagged `bi_hourly` | No | Trevion Groene Energie Vast: one feed-in rate per meter register beside the consumption rates. A bi-hourly or digital meter is credited by register on the region's day/night schedule, a single-register meter at `current`; a pair without the flag is shape (a). Every path asks per register, the per-day year-to-date walk included, which takes one hour inside each block; the compare estimate and the projection, which multiply one rate by a whole year's export, average the two registers by the household's export shape on that schedule (`_register_weights`, `compare_weighting.py`) rather than take the register the clock is in |

Shape (d) resolves through `_spp_injection_spot`, which is the one place that
decides WHICH mean and is deliberately not allowed to answer with an hour's
price. A caller's `spot` is a month mean only when the ENERGY leg is month-priced
too, and shape (d) is defined by the energy leg not being that, so the resolver
computes the mean itself from the spot bucket rather than echoing what it was
handed. Passing the hour through instead resolved a month formula per hour: real
backfilled August rows for Eneco Flex spanned -3,48 to +25,66 c/kWh against a
contractual 8,06, and their unweighted average is exactly right, which is why it
survived review.

Shape (d) is the one that looks like (a) and is not. Those cards print a
figure and a formula side by side, and their footnotes say the figure comes
from the last published value of an index the contract settles on monthly and
retroactively, so crediting the figure bills the previous month every month.
The flag distinguishes which mean resolves the formula and, just as
importantly, stops the month coefficients being read as shape (b).

Shape (c) is the subtle one: the energy contract is Variable (no spot needed for
energy) but the injection prices off the hourly BELPEX, so pricing the credit
still needs an ENTSO-E spot. It arrives two ways. Both Cociter variable cards
print no indicative at all, so the absent `current` is the tell. Bolt prints one and its
own footnotes call it an illustration of a per-quarter-hour settlement, so the
card is flagged `slot_indexed` and the formula wins over the printed figure;
`current` survives only as the fallback for an entry with no ENTSO-E key. Every
predicate that asks "does this credit price off the spot" has to accept both
tells, or the flagged cards silently fall back on a rate they do not bill.
Shape (d) needs
one too, at monthly rather than hourly resolution. `Contract` advertises both
with `spot_indexed_injection` so the config flow offers the API-key
step on the injection regime (`providers/_rates.py`). At runtime,
`_injection_needs_spot` detects it (`injection.py`):

```python
def _injection_needs_spot(snapshot, entry) -> bool:   # injection.py
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
unavailable (`coordinator_tick.py`). This is the
spot-indexed injection invariant: shape (c) must be gated on `_injection_needs_spot`
in the live, backfill and compare paths, or the credit drifts.

### Live injection price: `_compute_injection_price`

`_compute_injection_price(snapshot, entry, spot_prices)` returns the current-hour
EUR/kWh price only on the injection regime and only when the snapshot has injection
data (`injection.py`). Priority:

1. **Per-slot TOU** via `_tou_injection_rate` (`injection.py`).
2. **Spot formula** `factor * spot + base` when either the energy is
   `DynamicRates` (shape b) OR the leg is shape (c) as `_injection_is_spot_formula`
   (`injection.py`) reads it: both coefficients, no month or SPP index, and either
   no printed `current` or one the card flags `slot_indexed`. If no spot is
   available it returns `None` rather than fabricate a value
   (`injection.py`). The spot is looked up on the contract's own grid
   (`RESOLUTION_QUARTER` when `_energy_is_quarter_hourly`, else hourly), snapped
   with `slot_start`, and a nearest substitute is accepted only within one billing
   slot (900 s quarter-hourly, 3600 s hourly) (`spot_stats.py`).
3. **Monthly indicative** `inj.current` otherwise, including static-energy cards
   whose injection carries a monthly index but also a printed `current` (Ecofix
   Flexy, EBEM Groen Variabel / B@sic+) (`injection.py`,
   `injection.py`).

This scalar is resolved once per coordinator tick, so it is not what the
`injection_price` sensor publishes when the injection varies intra-day. There the
sensor indexes `injection_hourly` at the current slot (`_current_injection`,
`sensor.py`), the same way the price sensors index `hourly`, nearest-slot
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
(`injection.py`, fields at `providers/base.py`). It reuses the
energy contract's own `weekend_rule` via `tou_slot` so injection and consumption
agree on the slot for a given hour (`injection.py`). Returns `None`
otherwise so the caller falls back to the current / factor+base path.

### Historical injection: `_historical_injection_rate`

`_historical_injection_rate(injection, spot, *, energy, when)` mirrors the live
priority for a past hour: TOU slot first, then `factor*spot+base` when both the
formula and a historical spot exist, then `current` unless the card calls that
figure an illustration (`injection.py`).
The ordering (formula before `current`) is a bug fix: several dynamic-injection
contracts (Engie, Luminus, Mega, OCTA+, TotalEnergies) publish BOTH a `current`
indicative and `factor`/`base`, and checking `current` first made the YTD credit
use the flat indicative while the live sensor used the spot formula, so the two
user-facing numbers diverged (`injection.py`).

### Historical bug: monthly-indexed injection emitting an hourly factor

A monthly-indexed injection (DATS 24, EBEM Variabel/B@sic+, Eneco Fix/Flex/Flex One,
EnergyVision 3 jaar vast / 1 an fixe) must
emit only the realized monthly `current`, never an hourly `factor*spot+base`,
because the indicative is the actual credit. The guard that keeps shape (b)/(c)
from swallowing these cards is the `month_indexed` / `spp_indexed` refusal that
`_injection_needs_spot` and `_injection_is_spot_formula` (`injection.py`) apply
ahead of their `current is None or slot_indexed` test: a month-indexed leg is
never a per-hour formula, whatever the card prints, so the spot branch
is skipped and the realized rate is used, keeping the live sensor consistent with
the YTD credit for the same hour (`injection.py`). A latent mis-price
here is masked whenever the indicative prints, which is why it was fixed
explicitly rather than left to fall through.

### YTD injection paths

Past-month YTD billing routes injection per regime (`ytd_cost.py`,
context):

- `compensation`: yearly net metering, settled the way the supplier settles a
  yearly-read meter. Each slice's net `(cons - inj)` lands in its meter register
  (`_register_for`: single, day / night, the Impact band under Impact comptage,
  the dedicated night circuit) and `_NetAllocation` prices each register's net
  for the window at the RLP-weighted average of that register's all-in rates,
  clamped per register, since the DSO reports one net figure per register and
  the supplier spreads it over the year by the residential load profile (Eneco's
  rekenvoorbeeld: "Synthetic Load Profiles om de spreiding van het verbruik over
  het jaar heen te simuleren"). Netting the metered hours as they happen priced a
  summer surplus at summer rates against a winter draw at winter rates, a
  different number whenever the tariff moves within the year. Without the
  profile loaded the slices are priced as metered, still clamped per register.
  The coordinator fetches the profile for every compensation entry.

  The compare page and the projection reach the same clamp through
  `_annual_bill` (`compare_quote.py`), and have to divide the year into the
  SAME registers or they forfeit a different quantity than the meter does.
  `_register_weights` therefore reads its registers off the meter and the DSO
  mode exactly as `_register_for` does, and the clamp fires on how many came
  back rather than on a meter list of its own. Asking the meter instead left
  a mono entry under Tarif Impact (TotalEnergies Impact is that pair, and the
  config flow offers it deliberately) with no per-register clamp at all, so a
  midday band running backwards paid off the evening one and a matched
  3500 kWh install billed no energy; a bi-hourly one split its year day and
  night where the engine split it into the three CWaPE bands, 85,79 EUR out
  on the same install.
- `injection`: per-hour `cons * all_in - inj * inj_rate`, where `inj_rate` comes
  from `_historical_injection_rate` (`injection.py`).

Shape (c) has a dedicated YTD helper `_ytd_spot_injection_credit`
(`ytd_cost.py`) that replays the hour's own spot for a static-energy
contract whose injection prices off BELPEX; it is a no-op unless the month's
own card is that shape (the current card decides only when no per-month
resolver is handed in) and an injection sensor is wired, and it skips hours
with no cached spot (`ytd_cost.py`). It belongs to the per-day walk alone: that
is the only branch with no per-hour spot of its own, and the hourly branches
credit the same formula inside the walk.

Both of shape (c)'s tells reach it, and the guard reads them through the one
predicate `_injection_replays_hourly_spot` that also decides whether
`_historical_injection_rate` may fall back on a printed figure. Reading only
the absent-`current` tell excluded every Bolt fixed and variable card, so the
per-day walk credited the illustration while the `injection_price` sensor, the
backfill and the compare page all billed the formula: at a 5 EUR/MWh sunny
midday quotation the two answers do not share a sign. The two guards move
together, because relaxing either one alone would double the feed-in or drop
it.

## Capacity tariff

The Flanders capaciteitstarief is billed by the coordinator, not folded into the
per-kWh all-in. It is surfaced on its own `capacity_cost` sensor AND accrued into
`current_year_cost` through `_ytd_capacity`, so the running bill reflects what
Fluvius actually charges rather than the energy side alone. Monthly cost (`_compute_capacity`, `fees.py`):

```
capacity_cost_eur = capped(peak_kw * overlay.capacity_eur_per_kw_year) / 12.0
```

where `capped` is the VREG network ceiling above, applied to the year and
divided back, so the sensor and the running bill charge what the card allows.

Returns `0.0` when the entry lost its `CONF_DSO` key, the overlay is missing, or
`capacity_eur_per_kw_year is None` (`fees.py`). The rate lives on
`DsoOverlay.capacity_eur_per_kw_year` (`providers/base.py`); Flanders digital
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

Two modes (`const.py`):

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
(`_compute_prosumer`, `fees.py`):

```
prosumer_cost_eur = kva * (dso_rate + supplier_rate) / 12.0

  dso_rate      = overlay.prosumer_eur_per_kva_year        (DsoOverlay)
  supplier_rate = snapshot.supplier_prosumer_eur_per_kva_year   (SupplierSnapshot)
```

Returns `0.0` unless the regime is `compensation` AND the region is Wallonia AND
`CONF_SOLAR_KVA > 0` (`fees.py`). The Wallonia gate is deliberate:
compensation is Walloon-only, and billing a prosumer fee in Flanders on top of the
always-billed capacity tariff would double-count grid recovery
(`fees.py`).

The DSO rate lives on `DsoOverlay.prosumer_eur_per_kva_year`
(`providers/base.py`), published by Wallonia DSOs (valid until 2030 per
CWaPE) and `None` on Flemish SMR3 connections. The supplier-side forfait lives on
`SupplierSnapshot.supplier_prosumer_eur_per_kva_year`
(`providers/base.py`), billed on top of the DSO tariff; Cociter Variable
publishes one. It is already TVAC (VAT-incl) and summed raw, never VAT-scaled
(`fees.py`, `providers/base.py`).

Regime semantics (`const.py`): `compensation` ("compteur qui tourne a
l'envers") applies only to installations certified before 2024-01-01 and stays
valid until 2030-12-31; newer installations use the `injection` tariff (no per-kVA
fee); Flemish digital meters are SMR3 from the start. The YTD counterpart
`_ytd_prosumer` sums the monthly fee across the year using each month's archived
overlay, gated the same Walloon-only way (`ytd_cost.py`).

The flat per-kVA fee is one of the two ways CWaPE bills a compensation prosumer
(*Note explicative tarif prosumer*, 23 June 2025, section 4). It applies to a
household whose meter does not count draw and injection separately. With a
double-flow or communicating meter, distribution and transport are billed
proportionally on the GROSS draws instead, and article 81 of the 2025-2029 tariff
methodology caps that at what the flat fee plus the net draws would cost. That
proportional option is **not modelled**: every compensation entry is billed the
flat fee on its net draws, which for a communicating-meter household is the
ceiling of what it pays rather than the amount. The capacity is required all the
same (`compensation_lacks_kva`, `fees.py`): at 0 neither option is billed, and
the solar step refuses it.

## Brussels OSP tier

The Brussels Brugel OSP (Obligations de Service Public) fee is a flat annual
Sibelga charge scaled by contractual connection power
(`_brussels_osp_fee`, `fees.py`):

```python
def _brussels_osp_fee(overlay, entry) -> float:      # fees.py
    if overlay is None or overlay.brussels_osp_by_tier is None:
        return 0.0
    tier = entry.data.get(CONF_CONNECTION_KVA_TIER, DEFAULT_CONNECTION_KVA_TIER)
    return overlay.brussels_osp_by_tier.get(tier, 0.0)
```

The table lives on `DsoOverlay.brussels_osp_by_tier` and is populated only on the
Sibelga overlay (`providers/base.py`). The user picks the tier in the
config flow; the four residential tiers are `le1_44`, `le6`, `le9_6`, `le13`
(residential connections are <=13 kVA), default `le6`
(`const.py`). Returns `0.0` outside Brussels or when the card omits the
OSP table. The fee is added to the Brussels annual cost in `_annual_static_fees`
(`fees.py`), not to the per-kWh all-in.

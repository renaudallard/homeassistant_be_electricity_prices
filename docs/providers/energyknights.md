# Provider: energyknights

This document is the maintenance reference for the Energy Knights extractor
(`providers/energyknights.py`). It explains how the extractor addresses Energy
Knights' monthly tariff cards, how the energy / injection / tax / DSO fields are
parsed for the three supported products, and the land mines a future maintainer
must know when Energy Knights changes its cards. The test module
`tests/test_energyknights.py` is treated as ground truth throughout: it pins the
expected parse output against six real fixtures.

Related reading:

- [../provider-framework.md](../provider-framework.md) - the extractor protocol, the
  `SupplierExtractor` / `Contract` / `SupplierSnapshot` dataclasses, and the shared PDF
  helpers this module calls.
- [../pricing-model.md](../pricing-model.md) - how `DynamicRates`, `InjectionRates`,
  `TaxOverlay` and `DsoOverlay` are consumed by `compute_breakdown`.
- [energyvision.md](energyvision.md) - the closest sibling for the energy math: both
  cards print their formula in EUR/MWh excluding VAT, so the coefficient takes the VAT
  gross-up and no rescale.
- [energiebe.md](energiebe.md) - the closest sibling for the Flanders DSO + tax table
  shape, and for a supplier selling a dynamic and a monthly-indexed product side by side.

## Overview

Energy Knights BV (Mechelen) sells residential electricity in Flanders only. It
publishes eight products, four base and a "green" twin of each. The integration
tracks three:

| Contract id | Card product | Kind | Settles on |
| --- | --- | --- | --- |
| `energyknights_agilior` | Agilior Online | `dynamic`, quarter-hourly | `Belpex_15`, the 15-minute day-ahead price |
| `energyknights_agilis` | Agilis Online | `dynamic`, hourly | `Belpex_h`, the hourly day-ahead price |
| `energyknights_essentia` | Essentia Online | `spot_monthly` | `Belpex-RLP-M` for offtake, `Belpex-SPP-M` for the credit |

Agilior and Agilis are the same card on different settlement grids; the only thing
separating them in the parsed snapshot is `DynamicRates.quarter_hourly`. Essentia Online
is also the contractual fallback for both: page 3 of every dynamic card says that
consumption Fluvius cannot deliver quarter values for is billed "volgens het variabele
tarief (Essentia Online) dat van toepassing is in dezelfde tariefmaand".

**Optima Online is out of scope.** Its energy block was byte-identical to Agilior's in
August 2026, but its card carries a `Service fee onbalans handelen (€/jaar)` whose
amount depends on which home energy management system the customer runs. It printed
10,00 EUR/jaar for Flexio by Lifepowr on the December 2025 card and printed with no
value at all in August 2026. No field in `SupplierSnapshot` can hold a fee keyed on the
customer's own hardware, and shipping the product would price it at zero and silently
under-bill whenever Energy Knights re-prints it.

**The four green twins are catalogued but not sold here.** The difference is one extra
row, `Groene stroom (c€/kWh) 0,32`, with no formula, stable at 0,32 on every month and
product tested. Adding them is a four-line parser hook plus three contracts; it doubles
the catalogue for a flat adder nobody has asked for. `DISCOVER_IDS`
(`providers/energyknights.py:204`) lists all eight slugs so `discover()` only flags a
genuinely new product.

## Fetching

Every card is served from a stable, product-keyed URL, so there is no listing to resolve
and no versioned blob to chase:

```
https://www.energyknights.be/website/getCurrentTariffchart/<slug>/nl
```

The `/website/` prefix is part of the path. Without it the site answers 404. Slugs are
`agilioronline`, `agilisonline` and `essentiaonline`.

```
config (contract_id) -> fetch(): GET the product's card URL
                                    |
                                    v
                             fetch_pdf_text_layout() -> layout text
                                    |
                                    v
                             parse_snapshot(contract_id, text, url)
```

`probe` HEADs the same URL. Energy Knights sets a `Last-Modified` stamped at the moment
it generated the month's card (09:00 on the last day of the preceding month for the
August 2026 set), so the freshness key flips exactly when the rates do and the
coordinator does not need the 24 h TTL fallback.

`discover` scrapes `https://www.energyknights.be/tariffcharts` for
`getCurrentTariffchart/<slug>/` hrefs.

### The 302 that is not an error

An unknown slug, a malformed month or a month outside a slug's window all answer
**HTTP 302 redirecting to the marketing homepage**, and aiohttp follows redirects by
default. The result is a 200 carrying about 480 KB of HTML, which is the same order of
magnitude as the 334 KB card, so no size heuristic can tell them apart.

Nothing in this module inspects the payload, because it does not have to:
`_fetch_validated_pdf_bytes` (`providers/_pdf.py:178`) checks the `%PDF` magic bytes and
raises `expected a PDF at <url>`, which `is_transient_fetch_error` correctly classes as
**permanent**. Never add a size check here.

## Units

This is the part to read before touching a number.

The card header says `Alle prijzen en tarieven zijn inclusief 6% btw`. That statement is
**not true line by line**:

| Row | Basis as printed |
| --- | --- |
| `Abonnement (€/jaar)` | VAT-inclusive |
| `Verbruik ... (c€/kWh)` | VAT-inclusive |
| the formula column | **excluding VAT**, per `(*)Tariefformule in EUR/MWh excl BTW` |
| `optie "solar" (c€/kWh) (1)` | **excluding VAT**, per footnote `(1) Bedrag niet onderworpen aan BTW` |
| `Bijdrage energiefonds (€/maand) (1)` | **excluding VAT**, same footnote |

The card's own arithmetic settles it. On the August 2026 Agilior card, which quotes a
VREG index of 127,50 EUR/MWh for offtake and 70,54 for injection:

```
offtake:    (127,50 * 1 + 12) / 10 * 1,06 = 14,79   <- card prints 14,79
injection:  ( 70,54 * 1 - 12) / 10        =  5,85   <- card prints 5,85
```

With VAT the injection line would print 6,21. Grossing it would overstate every solar
user's credit by 6%.

So the conversions are:

| Card | Snapshot | Conversion |
| --- | --- | --- |
| formula coefficient | `factor` / `formula_factor` | dimensionless multiplier on a EUR/kWh spot: `* VAT`, **no `* 10`** |
| formula offset (EUR/MWh) | `base` / `formula_base` | `/ 1000 * VAT` |
| injection coefficient | `InjectionRates.factor` | as printed, **no VAT** |
| injection offset (EUR/MWh) | `InjectionRates.base` | `/ 1000`, **no VAT** |
| `Abonnement` | `yearly_fixed_fee` | as printed |
| DSO c€/kWh columns | `distribution_*` | `/ 100` |
| DSO annual columns | `data_management_per_year`, `capacity_eur_per_kw_year` | as printed |
| `Bijdrage energiefonds` | `energy_fund_eur_per_month` | as printed, EUR/**month** |

`taxes.vat_rate` stays `0.0`, which in this codebase means "the values in this snapshot
already include VAT" rather than "no VAT applies". Same as every other residential
Flemish card here.

### The double rounding

Energy Knights rounds the ex-VAT cents to two decimals **before** applying the 6%. On
the May 2026 Agilior card, `(1,07 * 100,71 + 7) / 10` is 11,47597, which rounds to 11,48
and grosses to 12,17 - the figure the card prints. A single multiply gives 12,16.

This does not affect any stored value, since the coefficients are what get stored. It
matters when a test reconstructs the printed price from the parsed coefficients to prove
the VAT bake landed, which `test_the_offtake_indicative_reconciles_with_its_own_formula`
does, so the helper there rounds twice.

## The energy leg

### Agilior and Agilis

`DynamicRates(factor, base, yearly_fixed_fee, quarter_hourly)`. The card prints four
consumption rows (enkelvoudig, dag, nacht, exclusief nacht) and every dynamic card
published so far repeats the same formula in all four, which is what `DynamicRates`
models: one coefficient pair for every meter.

No `current` is stored on the injection leg for these two. The credit settles against
each slot's own index, so the printed figure is an illustration.

### Essentia Online

`SpotMonthlyRates`, from `kind="spot_monthly"`. The card prints four registers with
genuinely different coefficients, and all of them are carried:

```
Verbruik enkelvoudig      14,66   BelpexRLP * 1,03  + 7
Verbruik dag              14,97   BelpexRLP * 1,045 + 8
Verbruik nacht            14,32   BelpexRLP * 0,997 + 8
Verbruik exclusief nacht  14,32   BelpexRLP * 0,997 + 8
```

The dedicated exclusive-night pair is populated even though it happens to equal the
off-peak one on every card so far: `pricing.py` routes it ahead of the bi-hourly band
test, because that circuit is billed per meter rather than per hour of the day, and
OCTA+ proves the two rows can diverge. A test asserting the four are all *different*
would be asserting a coincidence.

All four rows are mandatory on this card, and only on this card. A dynamic card repeats
one formula in all four registers and `DynamicRates` carries a single coefficient pair
for every meter, so Agilior and Agilis read the mono row and a missing band row there is
an unread column. Essentia bills all four, so a row that goes missing is a silent
re-price instead: relabelling the dag row on the August 2026 card moves peak hours
-2,05% and off-peak +2,39%, and every bound in the live check still passes, because what
is left behind is entirely plausible.

**Nothing of the printed c€/kWh column is stored**, and that is the whole reason this
product is `spot_monthly` rather than `variable`. The figure is computed from the VREG
weighted average annual price, not the Belpex-RLP-M the contract settles on, and Energy
Knights publishes both series at `https://www.energyknights.be/priceparameters`. Over the
26 months that table covers, for Fluvius Antwerpen:

| | offtake (RLP) | injection (SPP) |
| --- | --- | --- |
| months at least 10% apart | 19 of 26 | 23 of 26 |
| range | -24,7% to +56,2% | -56,1% to +242,9% |

The VREG series barely moves (78 to 116 EUR/MWh over two years) while the settled index
swings 55 to 131, which is why the gap is large and signed both ways. As an *estimator*
of the month's settled index the printed figure is about 20% out on average and the
previous month's settled value about 15%; the arithmetic mean of the ENTSO-E curve, which
is what the coordinator resolves the coefficients against, is about 5% out and always in
the same direction (below), the known RLP-weighting residual `README.md` already
discloses for the EBEM / Eneco / Mega cohorts.

`spot_monthly` is in `SPOT_PRICED_CONTRACT_KINDS` (`const.py:243`), which routes the
config flow through `async_step_api_key` (`config_flow.py:308`) with a `vol.Required`
field validated live against ENTSO-E. So the coefficients always resolve, at the cost
that a user without a key cannot add this contract at all: they reach a password field
with no skip and the only exit is closing the dialog. That is exactly energie.be
Variabel's behaviour, and it is the trade this measurement buys.

## Injection

| Contract | Card row | Shape |
| --- | --- | --- |
| Agilior / Agilis | `optie "solar" ... Belpex_15\|h * 1 - 12` | `factor` + `base`, no flags, no `current` |
| Essentia | `optie "solar" ... BelpexSPP * 0,98 - 10` | `factor` + `base` + `current`, `spp_indexed=True` |

The two dynamic cards settle the credit on the same per-slot index as their offtake leg,
which page 3 states outright: the quarter values are quoted *"voor zowel afname als
injectie"*. So that leg carries no `current` at all, the same shape EnergyVision
Dynamisch and Bolt Dynamisch use, and the printed 5,85 is an illustration.

Essentia settles it on Belpex-SPP-M, the solar-weighted monthly mean, while its energy
leg indexes on the load-weighted Belpex-RLP-M. `spp_indexed` is what stops the
coordinator resolving the formula against the energy leg's mean. This is the one place
the integration is *exact*: measured against Energy Knights' own published series, the
SPP-weighted mean the coordinator already computes from Synergrid's solar profile
reproduces the settled `BELPEX_SPP_M` to 0,007% mean and 0,015% worst over 2026-01..07.
Its `current` is kept anyway, because that profile has to land before the mean can be
computed and the coordinator leaves the credit on the printed figure until it does; it is
a VREG-derived illustration averaging 56% from the settled index, so it is the cold-start
value and never the answer.

`Contract.spot_indexed_injection` stays `False` on all three. It exists to offer an
*optional* key to a contract whose kind does not collect one, and every kind here is
spot-priced, so the key is already mandatory. `scripts/live_check.py` pins
`energyknights_essentia` to the `"spp"` shape: an unlisted `spot_monthly` derives
`"present"`, which asserts only that a leg exists, so the flag and both coefficients
would go unchecked. The dynamic pair needs no entry, because `"present"` asserts `factor`
and `base` exactly as `"spot"` would.

The offset is a deduction large enough that the credit turns negative whenever the index
falls below it - below 12 EUR/MWh on the August 2026 card, which happens at summer
midday, precisely when a PV installation is exporting.

## Network tariffs

The card prints the eight Fluvius areas **twice**, once for a digital meter and once for
a classic one, and the two differ by far more than rounding:

| Fluvius (Antwerpen), 2026-08 | digital | classic |
| --- | --- | --- |
| afname normaal | 5,35 c€/kWh | 8,09 c€/kWh |
| capaciteitstarief | 52,37 EUR/kW/jaar | 130,92 EUR/kW/jaar |

`_extract_dsos` cuts at the `klassieke meter` header and reads only the digital block.
Both markers are matched case-insensitively and only when they stand alone on their line.
A plain substring search binds the digital one to the solar footnote instead, *"Heb je
zonnepanelen en een digitale meter?"*, which sits 921 characters above the table on every
card where that sentence is not wrapped (the January 2026 card is one). Each DSO row is
also confined to a single line, so a row that loses its figures raises instead of
inheriting its neighbour's: dropping Midden-Vlaanderen used to hand it Fluvius West's
numbers, 27% more distribution, reported silently.
Every Energy Knights product is sold as "100% digitaal" and the dynamic ones bill on
meetregime 3 quarter values, which only a digital meter produces. A parser that read the
classic block would over-bill a typical Flemish entry by a few hundred euro a year.

The digital table's five columns are afname normaal, afname ex nacht, databeheer SMR1,
databeheer SMR3, capaciteitstarief. SMR3 is taken. Energy Knights has printed the two
databeheer columns at the same value on every card since January 2025, so the column choice has never mattered in practice. Should they ever
diverge, that is the decision to revisit.

A missing row raises rather than being skipped: a partial table would leave that area's
users with no distribution charge at all, which prices lower than the truth.

Two fields the card does not supply:

- `network_ceiling_eur_per_kwh` - no Energy Knights card prints the VREG `maximumtarief`,
  so the capacity charge is uncapped here where energie.be, DATS 24, Ecopower and
  EnergyVision cap it. The ceiling binds only below roughly 450 kWh/year at the 2,5 kW
  floor, so the practical impact is small, but it is a regulated rule that applies
  whether or not the card prints it.
- `prosumer_eur_per_kva_year` - the card **does** print it (54,63 EUR/kVA/jaar for
  Antwerpen, in the classic-meter table), but `fees.py:258` gates the prosumer fee to
  Wallonia and this is a Flanders-only supplier, so it is left `None`.

## Taxes

The block is a **two-column interleave**, and `Standaard tarief` heads a row in both
columns:

```
Bijdrage energiefonds (€/maand) (1)   Bijzondere accijns (c€/kWh)
Standaard tarief 0,00                 Verbruik tussen 0 en 3.000 kWh 4,8760
Niet-gedomiciliëerd 10,07             Verbruik tussen 3.000 en 20.000 kWh 4,8760
Beschermd tarief 0,00                 Verbruik tussen 20.000 en 50.000 kWh 4,8760
Energiebijdrage (c€/kWh)              Bijdrage groene stroom en WKK (c€/kWh) (d)
Standaard tarief 0,0000               Bijdrage groene stroom 1,16
Beschermd tarief 0,0000               Bijdrage WKK 0,36
```

A bare `Standaard tarief\s+(NUM)` matches the energy fund (EUR/month) and the
energiebijdrage (c€/kWh) interchangeably, which is a hundredfold unit error in whichever
direction it lands. Both patterns therefore anchor on the header line above them.

Both values happen to be zero on the August 2026 card, so a silent miss would look
identical to a correct parse. `test_tax_block_reads_each_column_of_the_interleave` uses
the May 2026 card, where the energiebijdrage is 0,2042, to prove the anchors hold.

The `Standaard tarief` fund row is taken, not the 10,07 `Niet-gedomiciliëerd` one: this
integration prices a domiciled residential entry, the same choice every sibling extractor
makes. The card's own spelling is `Niet-gedomiciliëerd`, with two e's.

Every value in this block is VAT-inclusive as printed, the excise included: 4,8760 is
4,60 x 1,06, and Ecopower, the one supplier here whose card is ex-VAT, prints 0,04748 for
the same band, which is 5,0329 / 1,06. The energy fund is the single exemption, per
footnote (1).

The excise is read from the first band. Energy Knights prints three bands and they have
**not** always been equal: every month from 2024-06 to 2026-07 read
`5,0329 / 5,0329 / 4,8188`, and only from August 2026 is the table flat at 4,8760, after
the federal contribution was folded into the excise. Band 1 is correct for the volumes a
residential entry bills, and it is the convention every sibling extractor follows.
`federal_excise_bands` stays `None`; `flanders_tax_overlay` cannot emit bands.

## Quirks and land mines

**Nothing on these cards may be pinned as a constant.** Coefficients, offsets and the
standing charge all drift monthly. Agilior ran `x 1,07 + 7` on a 15,00 EUR abonnement
from January to July 2026 and `x 1 + 12` on 25,00 in August; its injection went
`x 0,86 - 5` to `x 0,94 - 11` to `x 1 - 12` inside the same year.

**Two products can print the identical formula.** In August 2026 Agilior and Optima both
read `Belpex_15 * 1 + 12`, the same `- 12` injection and the same 25,00 abonnement. No
check on the formula or the figures can tell them apart, so `_require_product` reads the
card's own intro line, `Met <product> van Energy Knights kies je voor:`, and raises when
it names anything else. That also keeps the "Green" twin, which prints
`Agilior Online Green`, from parsing as the plain product.

**Energy Knights renames its products.** `Elektriciteit Dynamisch15` became
`Agilior Online` at the turn of 2026, `Elektriciteit Dynamisch` became `Agilis Online`
and `Elektriciteit Variabel` became `Essentia Online`. Contract ids do not carry
"online" for that reason. A future rename makes `fetch` raise loudly, which is the
intended failure.

**The coin rebate is not netted.** `Spaar korting met munten (€/jaar) 25,00` is an
average potential rebate for shopping through the supplier's platform, and footnote (2)
settles it on the ex-VAT amount. On the Agilior card it is numerically equal to the
abonnement, so netting it would report a zero standing charge to every user including
those who never use the platform, on a different VAT basis from every stored value.
Same treatment as Luminus' free Sundays and Frank's cashback. `Lid van
energiegemeenschap (€/jaar) 0,00` is likewise not modelled.

**Only the Dutch card parses.** All three languages carry identical numbers, but the
French card wraps the long DSO names onto their own lines, splitting the label away from
its figures:

```
Fluvius (Halle-
5,64 5,12 18,92 18,92 59,41
Vilvoorde)
```

`_LANG` is pinned to `nl` for that reason.

**Footnote (3) is an orphan.** `Op administratieve kosten is 21% BTW van toepassing` is
never referenced from any row on pages 1 or 2, and page 3 says of the reminder fees that
`Op deze bedragen is geen BTW verschuldigd`. No 21% component is modelled from it.

## If an archive is ever added

`getHistoricalTariffchart/<YYYY-MM>/<slug>/nl` is a real `fetch_for_month` on the
`providers/ebem.py:153` shape, but three things have to be right:

1. **The slug changes at 2025-12/2026-01, with no overlap and no gap.** Before that,
   Agilior is `dynamic15`, Agilis is `dynamic`, Essentia is `variable`.
2. **The product-name guard needs the legacy names, including a spelling change.** The
   2025-09 card says `Elektriciteit Dynamisch 15`, with a space; 2025-10 onward say
   `Dynamisch15`.
3. **The horizon must stop at 2025-01.** The 2024 cards print the pre-merger ten Fluvius
   areas (Gaselwest, Iverlek, Pbe, Sibelgas, none of which exist in `const.py`) and omit
   Halle-Vilvoorde and Zenne-Dijle, two live DSO keys. The 2024-06 card prints `0,05` and
   `0,03` under a `(c€/kWh)` header, which are EUR/kWh values in a cents column, and its
   two databeheer columns differ (13,95 SMR1 against 15,14 SMR3) where later cards have
   them equal. Making those months parse would leave the overlays wrong in silence, which
   is worse than the loud failure it replaced.

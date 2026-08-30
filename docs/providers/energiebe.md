# Provider: energiebe

This document is the maintenance reference for the energie.be extractor
(`providers/energiebe.py`). It explains how the extractor fetches energie.be's three
residential tariff cards - a dynamic one from the site's document API, and a variable and
a fixed one whose URLs are named by the site's contracts API - how the energy / injection / tax / DSO
fields are parsed out of each, and the land mines a future maintainer must know when
energie.be changes its cards. The test module `tests/test_energiebe.py` is treated as
ground truth throughout: it pins the expected parse output against real fixtures.

Related reading:

- [../provider-framework.md](../provider-framework.md) - the extractor protocol, the
  `SupplierExtractor` / `Contract` / `SupplierSnapshot` dataclasses, and the shared PDF
  helpers this module calls.
- [../pricing-model.md](../pricing-model.md) - how `DynamicRates`, `InjectionRates`,
  `TaxOverlay` and `DsoOverlay` are consumed by `compute_breakdown`.
- [frank.md](frank.md) - the closest sibling; energie.be reuses Frank's dynamic parsing
  shape but differs on units, fetch and residential scoping (see below).

## Overview

energie.be sells three residential electricity products, all tracked and all
Flanders-only: "Elektriciteit dynamisch tarief particulier online" (spot-indexed per
quarter-hour), "Elektriciteit particulier online" (indexed to a monthly average) and
"Elektriciteit vast particulier online" (a flat rate).
The module docstring (`providers/energiebe.py:26`) and `_ENERGIEBE_REGIONS`
(`providers/energiebe.py:120`) fix the region to `REGION_FLANDERS`; `fetch` rejects any
other region with "energie.be only operates in Flanders" (`providers/energiebe.py:232`).
All eight Fluvius sub-areas are covered (see the DSO section). The dynamic card also
carries a professional block; only the residential rows are parsed (see
[Residential scoping]).

energie.be publishes its cards as PDFs, reached two different ways.

The dynamic card lives at one stable URL, `_CARD_URL`:

```
https://energie-production-api.azurewebsites.net/api/v1/data/document?key=DynamicTariffs
```

A GET on that URL answers `302 Found` and redirects to the current month's versioned Azure
blob (e.g. `energie.blob.core.windows.net/cms/assets/..._Dynamisch_<hash>.pdf`); aiohttp
follows the redirect automatically, so the extractor treats it as a single stable URL
whose body is replaced each month. This is the DATS 24 fetch shape: no HTML scraping, no
discovery, no cheap probe, no archive.

The variable and fixed cards have **no document key of their own**. Their current PDFs
are named by the site's contracts endpoint, `_CONTRACTS_URL`:

```
https://www.energie.be/api/v1/data/contracts
```

which returns one entry per `tariffType` (`Fixed` / `Variable` / `Dynamic`), each carrying
a `contractTypeElRes.tariffDocument` blob URL that is replaced in place each month.
`_resolve_card_url` takes the `tariffType` and reads that entry's residential document and
nothing else - `_TARIFF_TYPE` maps the contract id to it. The host matters: the site's front end calls this API on its own origin, and the
`energie-production-api.azurewebsites.net` host behind it answers `401` on this path.

```
config (contract_id) -> fetch(): dynamic          -> GET _CARD_URL (302 -> blob)
                                 variable / fixed -> GET _CONTRACTS_URL (JSON) -> blob URL
                                    |
                                    v
                             fetch_pdf_text_layout() -> layout text
                                    |
                                    v
                             _residential() -> drop the professional block
                                    |
                                    v
                             parse_snapshot() -> SupplierSnapshot
```

### The `?key=Tariffs` trap

The document API exposes a `Tariffs` key that looks like the variable card's home. **It is
a dead link and must never be used.** It still answers `200` with a PDF titled
"Elektriciteit particulier - april 2024", last modified 2 April 2024, whose DSO table uses
the pre-merger 10-area Fluvius naming (Gaselwest, Iveka, Iverlek, Pbe, Sibelgas, ...) and
two years of superseded network tariffs. Only the site footer and the customer-zone
document list still point at it. That card is why this provider's variable and fixed
products were declined when the dynamic one shipped, and the contracts API is what
corrected the record.

Measured, so the hazard is not overstated: fed to today's parser that card **fails** rather
than mis-bills. Its older layout prints the formulas unparenthesised and in EUR/MWh
(`formule (excl. btw): 1,058 x Belpex_RLP+ €10/MWh`) and names the standing charge
"Abonnementskost", so `_ENERGY_RE`, both injection paths, `_yearly_fee` and the GSC/WKK
levies all raise; only 4 of its 10 DSO rows parse, and `_publication_label` returns "".
That is the layout's doing and not a safeguard - the four rows it *does* read are the 2024
network tariffs, and a re-template would arm exactly the silent mis-billing this warns
about. Hence no fallback, by design.

The `publication_label` is a lowercased "month year" string ("juli 2026") reconstructed
from the residential card header by `_publication_label` (`providers/energiebe.py:335`).

## Contracts

Three contracts are declared in the `EXTRACTOR`:

| contract id | label | TariffKind | regions | needs an ENTSO-E key |
| --- | --- | --- | --- | --- |
| `energiebe_dynamic` | energie.be Dynamisch | dynamic | flanders | yes, per quarter-hour |
| `energiebe_variable` | energie.be Variabel | spot_monthly | flanders | yes, for the month's mean |
| `energiebe_fixed` | energie.be Vast | fixed | flanders | injection only, for the month's SPP mean |

`quarter_hourly=True` on the dynamic contract: the card bills "op kwartierbasis" on
the Day-Ahead EPEX SPOT Belgium 15-minute curve, so the live price table, next-slot sensor
and cheapest-window service keep the native 15-minute slots (`base.py:174`). YTD billing
stays hourly. `spot_indexed_injection` is left at its default `False` on both: a `dynamic`
or `spot_monthly` contract already collects the ENTSO-E key via its energy kind, so the
injection regime does not need to gate it (`base.py:71`).

### Why the variable product is `spot_monthly`, not `variable`

`variable` means the card publishes the month's *resolved* rate and the integration reads
it. energie.be's card does not. It prints the formula
`(1,12 x Belpex_RLP + 0,80) c€/kWh` and, beside it, a price derived from the **VNR
twelve-month forecast** of the index rather than from the month's realised value. The July
2026 card showed 13,13 c€/kWh on a forecast index of 10,34 while the month settled at a
realised Belpex_RLP of 11,42 - a true rate of 14,41 c€/kWh, nearly 10% higher. Reading that number
would ship a knowingly wrong rate that no later tick corrects, the 0.6.7 mispricing class.

`spot_monthly` instead stores the coefficients and lets the coordinator resolve
`factor x mean(this month's spot) + base` from its ENTSO-E cache (`coordinator.py:640`),
which firms up as the month fills in. The arithmetic monthly mean is a close (few-percent)
approximation of the RLP weighting, the same approximation EBEM / Eneco / Mega cohorts use.
The kind is also what makes the config flow collect an ENTSO-E key
(`config_flow.py:488`) - without one this contract cannot be priced at all.

## Fetch strategy

### Download (`fetch`)

`fetch` (`providers/energiebe.py:232`) validates the contract id and region, resolves the
card URL for the contract (`_CARD_URL` for the dynamic one, the contracts API for the
variable one), calls `fetch_pdf_text_layout` to download and layout-extract the PDF (the
layout extractor keeps column alignment, important for the DSO table), then
`parse_snapshot`. Validation is by `%PDF` magic bytes in the shared helper
(`_pdf.py:126`), not Content-Type, so the JSON-style API URL and its blob redirect work.

### Probe and archive

There is neither a probe nor an archive:

- `EXTRACTOR.probe` is `None`. A HEAD on `_CARD_URL` answers `405 Method Not Allowed`, so
  the shared `head_freshness_key` helper cannot produce a key. The coordinator falls back
  to its time-based TTL, which is adequate for a card that only rotates monthly (the live
  price comes from the ENTSO-E spot each tick, not from the card).
- `EXTRACTOR.fetch_for_month` is `None`. Both cards' URLs are overwritten in place, so
  past months bill at the current snapshot as a proxy, the same as Ecofix / EnergyVision.
  energie.be *does* publish a month-scoped archive at
  `www.energie.be/api/v1/data/tariff-cards?isProfessional=<bool>&tariffType=<type>` (33
  months back, one PDF per month per product, uploaded in arrears in the first days of the
  following month). Wiring it up would give this supplier signing-cohort retrieval and
  per-month YTD billing; it is deliberately not done yet, and is the obvious next step.

## Residential scoping

This concerns the dynamic card only. The variable card publishes its professional
sibling as a separate PDF (`contractTypeElPro`), which the extractor never fetches, so the
residential cut below is a no-op there and is left in the shared path rather than branched
around.

The `?key=DynamicTariffs` PDF bundles a residential block (pages 1-2) and a professional
block (pages 3-4). The two blocks share the same energy and injection formula but differ on
GSC/WKK, the tax rows and the DSO net-tariff table (e.g. residential databeheer 18,92
EUR/yr vs professional 17,85). `_residential` (`providers/energiebe.py:329`) slices the
text at the professional section header `_PROF_MARKER = "dynamisch tarief professioneel"`
(`providers/energiebe.py:138`) so no professional row can leak into a residential snapshot.
`test_only_residential_block_is_parsed` (`tests/test_energiebe.py`) pins that the parsed
renewables and every DSO databeheer come from the residential rows.

The marker is the section header only ("Elektriciteit dynamisch tarief professioneel"); the
residential page's "Overzicht actief aanbod" list mentions "Elektriciteit dynamisch
professioneel" without "tarief", so it does not trip the slice.

## Parsing

`parse_snapshot` runs `_residential` first, then assembles the `SupplierSnapshot`. The
regulated half of the card - taxes, DSO rows, publication label - is parsed by the same
code for both products, because both cards carry the same regulated table for the month;
only the energy and injection legs branch on the contract id.

| field | dynamic | variable | fixed |
| --- | --- | --- | --- |
| `energy` | `_extract_energy` -> `DynamicRates` | `_extract_variable_energy` -> `SpotMonthlyRates` | `_extract_fixed_energy` -> `FixedRates` |
| `injection` | `_extract_injection` -> factor/base | `_extract_monthly_injection` -> SPP formula | `_extract_monthly_injection` -> SPP formula |
| `taxes` (`TaxOverlay`) | `_extract_taxes` | same | same |
| `dsos` (`dict[str, DsoOverlay]`) | `_extract_dsos` | same | same |
| `valid_until` | `parse_valid_until` (shared) | same | same |

`test_variable_shares_the_regulated_overlays_with_the_dynamic_card` pins that shared half
against the August 2026 values, since the two PDFs are published independently and a drift
in one would show up there.

`_NUM = r"([\d]+(?:[.,][\d]+)?)"` accepts both decimal separators; a dot-decimal re-render
must not truncate a mandatory value to its integer part.
`test_dot_decimal_render_matches_comma` pins that a comma card and its dot-replaced twin
parse identically.

## Units: Belpex is in c€/kWh, not EUR/MWh

The single most important difference from Frank / Bolt. Those cards print their formula
against Belpex in EUR/MWh, so their extractors scale the factor by 10. energie.be writes
the formula as `(1,04 x Belpex + 0,50) c€/kWh` with **Belpex in c€/kWh**, verified against
the card's own printed price: the shown 11,93 c€/kWh (incl. VAT) equals
`(1,04 x 10,34 + 0,50) x 1,06`. So the spot coefficient is NOT scaled by 10 - getting this
wrong would 10x the energy leg. See the conversion in `_extract_energy`
(`providers/energiebe.py:321`).

## Energy formula

`_extract_energy` (`providers/energiebe.py:340`) parses the formula row with `_ENERGY_RE`
(`providers/energiebe.py:170`), anchored on "formule (excl. BTW):" so it binds the energy
formula and not the injection one that shares the `(factor x Belpex +/- base)` shape:

```
de formule (excl. BTW): (<factor_pdf> x Belpex <sign> <base_cents>) c€/kWh
```

The stored `DynamicRates` feeds `energy_eur_per_kwh = factor * spot + base` where the spot
is ENTSO-E BE day-ahead in EUR/kWh. With Belpex in c€/kWh and the formula quoted ex-VAT
(`providers/energiebe.py:321`):

```
factor = factor_pdf * _VAT_MULT           # no * 10: Belpex already c€/kWh
base   = base_cents / 100.0 * _VAT_MULT
```

`_VAT_MULT = 1.06` (`providers/energiebe.py:133`) is the Belgian residential rate. It is a
constant, not scraped: the formula is stated ex-VAT while every other card value is
VAT-inclusive, and the card's only printed percentage (21% on energiedelen) is unrelated.
Scaling the energy leg to the VAT-inclusive basis keeps `TaxOverlay.vat_rate=0.0`, the same
convention as Frank. `test_energy_formula_factor` pins `1.04 * 1.06` and
`test_energy_formula_base` pins `0.50 / 100 * 1.06`.

The yearly fixed fee ("vaste vergoeding") is parsed by `_FEE_RE` matching
`Vaste vergoeding <num> (.../jaar)` (`providers/energiebe.py:210`). Unlike Frank's
per-month "Abonnementskost", energie.be quotes it already annual (25 EUR/jaar), so it is
carried through unscaled - no x12 (`test_yearly_fixed_fee_is_already_annual`). A missing row
is fatal: "energie.be: vaste vergoeding row not found" (`test_missing_fee_is_fatal`).

`_FEE_RE` allows the rest of the number's line and at most one newline before the unit.
The dynamic card prints the row as three clean lines, but the variable card wraps a
sentence of body text onto the number's line ("35      methodologie): 12,75c €/kWh."), so a
strict same-line pattern reads the dynamic fee and misses the variable one entirely - while
a pattern that *requires* the wrap misses a card that ever collapses the two. The unit
itself stays `\([^)]*jaar`, matching the tax regexes: the same cards render the sibling
energy-fund unit as "(€/maand )" with a stray space, and a missing fee is fatal, so pinning
the € glyph exactly would take both contracts offline over a renderer quirk.
`test_fee_unit_spelling_is_not_load_bearing` and `test_fee_row_on_a_single_line_still_binds`
pin both halves.

### Variable energy (`_extract_variable_energy`)

Same regex, same units, different destination: `_ENERGY_RE` reads both the dynamic card's
bare `Belpex` and the variable card's `Belpex_RLP`, and the conversion is identical
(no `x 10`, energy grossed by `_VAT_MULT`), but the result is a `SpotMonthlyRates` leg:

```
factor = factor_pdf * _VAT_MULT           # 1,12 * 1.06
base   = base_cents / 100.0 * _VAT_MULT   # 0,80 c€/kWh -> EUR/kWh, grossed
```

There is no `current` field on `SpotMonthlyRates`, which is the point: the card's printed
price is a forecast (see [Why the variable product is `spot_monthly`]) and there is nowhere
for it to leak into. The fee is 35 EUR/jaar against the dynamic card's 25.

### The index parameter is a discriminator, not a spelling

`_ENERGY_RE` CAPTURES the `_RLP` suffix rather than merely tolerating it, and each parser
asserts its own: the dynamic leg rejects a `Belpex_RLP` card, the variable leg rejects one
without it, and the variable injection requires `Belpex_SPP`. That name is the only thing
in the card text saying which product the document is for - the two cards print an
otherwise identical `formule (excl. btw): (N x Belpex... +/- N)` row.

Tolerating either spelling for either contract means a card served at the wrong URL parses
SILENTLY into the other product's coefficients: a dynamic entry billing 1,12 x spot + 0,80
per quarter-hour, a monthly formula on a per-slot axis. Not hypothetical for this supplier,
which already serves a two-year-old card at a legacy document key. The failure would be a
wrong price rather than a missing one, which is the worst shape available.

The dynamic card needs one guard more. Both its formulas print the bare `Belpex`, so only
their ORDER separates them; `_extract_injection` therefore refuses a match that starts
before the energy row ends, rather than crediting a solar user the consumption rate.
`test_a_card_served_for_the_wrong_product_is_rejected`,
`test_variable_injection_must_be_indexed_on_spp` and
`test_dynamic_injection_cannot_bind_the_energy_row` pin the three.

## Fixed energy (`_extract_fixed_energy`)

The one card of the three that prints a RATE rather than a formula: 18,26 c€/kWh flat for
the contract's duration, plus the same 35 EUR/jaar vaste vergoeding.

**No VAT multiplier here.** The other two products label their formula "(excl. btw)" and
have to be grossed by `_VAT_MULT`; this column carries no such marker, and the card header
says every price on it is VAT-inclusive unless marked. Running 18,26 through `_VAT_MULT`
anyway bills 19,36 - a 6% overcharge that looks entirely plausible on a bill, which is
exactly why it is worth a test of its own
(`test_fixed_energy_is_a_flat_vat_inclusive_rate`).

The FIXED card prints no peak / offpeak or exclusive-night column, so `FixedRates` carries only
`single` and the pricing engine's fallback routes every meter type through it. That is
correct here rather than an approximation: there is no bi-hourly rate to miss.

All three cards use the identical `Energieprijs` column label, so the number alone cannot
say which card this is. The guard is twofold: the fixed parser refuses a card that carries
an indexation formula at all (`_ENERGY_RE`), and requires the card's own "de energieprijs
is een vaste prijs" wording. Without it, a fixed entry served the variable card would bill
that month's 15,98 c€/kWh as though it were locked for a year.

None of the three prints a dag / nacht column either, so a bi-hourly meter meets the same
energy rate on both registers whichever product is configured. The `FixedRates` note under
*Fixed energy* above says the same thing for one card only; do not read it as the supplier-wide
statement, because a Variabel entry's price table is flat for a different reason - its monthly
index (see "Why the variable product is `spot_monthly`"). On the network side the Nettarieven
table's `excl. nacht` column is the dedicated night circuit, not the night half of a tweevoudig
meter, so there is no day/night split to apply on either leg.

## Injection

Injection is the hourly `factor*spot+base` shape (shape (b) in the taxonomy in
[../pricing-model.md](../pricing-model.md)); on a dynamic card it prices off the live spot
the energy path already fetches, so `current` stays `None`. `_extract_injection`
(`providers/energiebe.py:501`) parses the `terugleveringsvergoeding` row with
`_INJECTION_RE` (`providers/energiebe.py:180`):

```
Terugleveringsvergoeding ... (<factor_pdf> x Belpex <sign> <base_cents>)
```

The regex skips with `.*?` (DOTALL) to the first `(factor x Belpex +/- base)` after its
anchor, because the card interleaves the unit label "(c€/kWh)" between "de formule:" and
the parenthesised formula. The anchor sits below the energy formula, which guarantees the
injection formula is matched and not the energy one. Injection is VAT-exempt
(`base.py:268`) and Belpex is in c€/kWh, so
(`providers/energiebe.py:410`):

```
factor = factor_pdf          # no * 10, no VAT
base   = base_cents / 100.0
```

A missing formula is fatal: "energie.be: injection formula row not found"
(`test_missing_injection_is_fatal`). The July fixture yields factor `1.0` and base
`-0,98 / 100` (`test_injection_factor`, `test_injection_base`).
energie.be publishes no supplier-side prosumer / PV forfait, so
`supplier_prosumer_eur_per_kva_year` stays `None`.

`_INJECTION_RE` anchors on the section header "Terugleveringsvergoeding", which both cards
print, rather than on the body wording, which differs: the dynamic card says
"injectievergoeding" and the variable one "terugleververgoeding". Neither wording is
load-bearing (`test_dynamic_body_wording_alone_is_not_the_anchor`).

### Monthly injection (`_extract_monthly_injection`): the SPP formula, plus a fallback

The variable card prints an injection formula, `(0,60 x Belpex_SPP - 0,80) c€/kWh`, and the
extractor stores all three of: the formula as `factor`/`base`, the printed "Zonnestroom"
column as `current`, and `spp_indexed=True`.

The flag is the load-bearing part. Consumption is priced on Belpex_RLP, injection on the
solar-weighted Belpex_SPP, and the two diverge sharply - July 2026 settled at 6,34 c€/kWh
SPP against 11,42 RLP. A `SpotMonthlyRates` energy leg makes the coordinator bake any
injection factor/base against the *energy* leg's monthly mean
(`_bake_monthly_injection`), which in a month like that would credit 6,05 c€/kWh where the
contract pays 3,00 - roughly double, because PV output peaks exactly when the day-ahead
price troughs. `spp_indexed` makes the coordinator fetch Synergrid's solar production
profile for the entry (no user opt-in: it is a property of the card) and resolve the
formula against the SPP-weighted month mean, which reproduces the contract exactly.

`current` is the fallback, not the answer. It is itself derived from the VNR forecast
rather than the realized month (2,77 printed against 3,00 realized for July 2026), and it
is credited only while no weighted mean is available - a cold start, or a Synergrid fetch
that failed. That fallback is STRICT by design: the plain monthly mean is never substituted,
because it is a different index rather than a coarser one.

It is read with its **sign**: this formula settles negative whenever Belpex_SPP drops below
1,33 c€/kWh (energie.be's own published table bottoms out at 1,65), and both
`InjectionRates` and the live check's `_validate_injection` say a monthly indicative is
allowed to be negative. A sign-blind column pattern would not mis-credit such a card, it
would fail to read it and take the whole contract offline
(`test_negative_injection_indicative_is_read_not_fatal`). It is itself derived from the VNR forecast, but it carries the SPP
shape and lands within a few tenths of a cent of the realised rate (2,77 printed against
3,00 realised for July 2026) - unlike the energy leg, where the forecast error is the whole
problem. A missing row is fatal: "energie.be: injection indicative row not found".
`_INJECTION_SHAPE` in `scripts/live_check.py` pins the `spp` shape - formula, indicative
and flag all present - so dropping any one of them fails CI rather than mis-crediting in
silence.

The FIXED card prints the same formula and resolves it the same way. A fixed ENERGY leg
does not make the feed-in credit fixed: the card says the compensation "wordt geindexeerd
op basis van de Belpex_SPP parameter" and that the invoiced amount follows the index of
the month being billed. The contract therefore declares `spot_indexed_injection`, which
offers the same optional, skippable ENTSO-E key Cociter Variable uses; skip it and the
credit falls back to the printed indicative. Its live-check shape is `spp`, the same as
the variable card.

Storing only the indicative (what this extractor did before) froze the credit at the VNR
forecast for the life of the card. Against energie.be's own published realized index that
is 3,6x the contractual credit in April 2026 and 0,56x in January, and because energie.be
keeps no archive the same frozen number reaches every past month of `current_year_cost`.

## Taxes

`_extract_taxes` (`providers/energiebe.py:521`) parses four levy rows and builds a
`TaxOverlay`. All card values are VAT-inclusive (the federal excise and the energy fund are
VAT-exempt), so `vat_rate=0.0` is set explicitly (`test_taxes_vat_rate_zero`).

| overlay field | card row | regex | required |
| --- | --- | --- | --- |
| `federal_excise` | Bijzondere accijns op Energie (c€/kWh) | `_EXCISE_RE` (`:215`) | yes |
| `energy_contribution` | Bijdrage op de Energie (c€/kWh) | `_CONTRIB_RE` (`:218`) | yes |
| `flanders_renewables` | GSC + WKK (c€/kWh) | `_GSC_RE` (`:213`), `_WKK_RE` (`:214`) | yes (both) |
| `energy_fund_eur_per_month` | Bijdrage Energiefonds Residentieel (EUR/maand) | `_FUND_RE` (`:221`) | no (0.0 default) |

Note the label differences from Frank: energie.be prints the unit as `(c€/kWh)` (Frank uses
`(EURct/kWh)`) and the contribution as "Bijdrage op **de** Energie" (Frank omits "de"). The
tax regexes tolerate the unit with `\([^)]*\)` so a font quirk in the `€` glyph does not
break them. `_FUND_RE` anchors on "Residentieel" immediately after "Energiefonds", so the
sibling "Niet-Residentieel" (VAT-exempt, non-residential) row cannot match; the residential
fund is 0 (`test_taxes_energy_fund_residential_zero`).

`flanders_renewables` is the sum of GSC and WKK. Because energie.be is Flanders-only,
both are mandatory; a miss raises "could not parse energie.be GSC/WKK
levies" (`test_missing_gsc_wkk_is_fatal`). `test_taxes_flanders_renewables_gsc_plus_wkk`
pins GSC 1,17 + WKK 0,39 = 1,56 c€/kWh. All c€/kWh values are divided by 100.

## DSO overlay

`_extract_dsos` (`providers/energiebe.py:539`) covers all eight Fluvius sub-areas via
`_DSO_ROWS` (`providers/energiebe.py:144`), which maps each card label prefix to the
canonical DSO key:

| card label | canonical key |
| --- | --- |
| Antwerpen | `fluvius_antwerpen` |
| Halle-Vilvoorde | `fluvius_halle_vilvoorde` |
| Imewo | `fluvius_imewo` |
| Kempen | `fluvius_iveka` |
| Limburg | `fluvius_limburg` |
| Midden-Vlaanderen | `fluvius_intergem` |
| West | `fluvius_west` |
| Zenne-Dijle | `fluvius_zenne_dijle` |

Note Kempen -> `fluvius_iveka` and Midden-Vlaanderen -> `fluvius_intergem`: the card's
regional trade name is not the canonical key. `test_dsos_cover_all_eight_fluvius_subareas`
asserts all eight are present.

The parser narrows to the "Nettarieven" section, then for each label matches
`Fluvius (<prefix>)` followed by the first four numbers (the digital-meter columns):

```
databeheer  -> data_management_per_year    (EUR/year, no /100)
capacity    -> capacity_eur_per_kw_year    (EUR/kW/year, no /100)
normal      -> distribution_single         (c€/kWh /100)
excl_night  -> distribution_exclusive_night (c€/kWh /100)
```

The row regex uses `Fluvius\s*\(\s*<prefix>[^\d]*` then four `_NUM` columns, so it tolerates
the card wrapping two long labels across the number row -
`Fluvius (Halle-\n<numbers>\nVilvoorde)` and the Midden-Vlaanderen row -
because the numbers still follow the leading fragment. `test_dso_wrapped_label_halle_vilvoorde`
and `test_dso_wrapped_label_midden_vlaanderen` pin both wrapped rows.

`transport` is always 0.0 (bundled into distribution, `test_dso_transport_is_zero`). The
klassieke-meter and prosumer columns that follow on the card are ignored: a dynamic-contract
customer must have a digital SMR3 meter, so the reverse-running-meter prosumer regime does
not apply, and `DsoOverlay.prosumer_eur_per_kva_year` stays `None` (matching Frank). A label
that does not match is skipped (not fatal); the eight-sub-area test is the safety net.

## valid_until

`parse_valid_until` (`_pdf.py:996`) is the shared best-effort validity parser. energie.be's
card carries no month name inside a validity-keyword window (the "juli 2026" sits in the
page header, not after "geldig"), so `valid_until` resolves to `None`. That is the
documented "treat as available" fallback and is correct for a dynamic contract, whose
tomorrow prices come from the ENTSO-E day-ahead publication rather than the card.

## Quirks and historical bugs (land mines)

- **Belpex is c€/kWh, not EUR/MWh.** The energy and injection factors are NOT scaled by 10,
  unlike Frank / Bolt (`providers/energiebe.py:321`, `:410`). Verified against the printed
  11,93 c€/kWh incl. VAT.
- **Two blocks in one PDF.** `_residential` must run before any parsing or the professional
  GSC/WKK, taxes and DSO rows leak in (`providers/energiebe.py:329`).
- **Injection unit label interleaved.** "(c€/kWh)" sits between "de formule:" and the
  injection formula, so `_INJECTION_RE` anchors on the "Terugleveringsvergoeding" section
  header and skips to the first parenthesised formula (`providers/energiebe.py:180`). The
  header, not the body wording: the cards word that row differently.
- **Yearly fee is already annual.** No x12, unlike Frank's per-month Abonnementskost
  (`providers/energiebe.py:381`).
- **Wrapped DSO labels.** Halle-Vilvoorde and Midden-Vlaanderen wrap across the number row;
  the `[^\d]*` gap in the row regex absorbs it (`providers/energiebe.py:443`).
- **Label differences from Frank.** Unit `(c€/kWh)` not `(EURct/kWh)`; "Bijdrage op de
  Energie" not "Bijdrage op Energie"; the tax regexes are energie.be-specific.
- **No probe, no archive wired up.** HEAD is 405 and both card URLs overwrite in place;
  the coordinator uses its time-based TTL and bills past months at the current snapshot.
  A month-scoped archive API exists and is not used yet (see [Probe and archive]).
- **All three cards share the `Energieprijs` column label.** The fixed one prints a rate
  there and the other two a formula, so the number alone cannot identify the card. The
  fixed parser refuses any card carrying an indexation formula and demands the "vaste
  prijs" wording; the indexed parsers demand their own index name (see above).
- **The fixed rate is already VAT-inclusive.** Unlike the two formulas, it carries no
  "(excl. btw)" marker. Grossing it anyway bills 6% over.
- **`?key=Tariffs` is a dead April 2024 card.** It answers 200. Today it fails the parser
  outright (older layout, unparenthesised formulas in EUR/MWh), but 4 of its 10 DSO rows do
  read and they carry 2024 network tariffs, so a re-template would turn a fallback into
  silent mis-billing. The variable URL comes from the contracts API, with no fallback by
  design.
- **The variable card prints a FORECAST, not the month's rate.** Never read its printed
  energieprijs. The contract is `spot_monthly` precisely so that number has nowhere to go.
- **The two legs of the variable card index on different parameters.** Consumption on
  Belpex_RLP, injection on Belpex_SPP. `spp_indexed` is what keeps them apart; drop it and
  the coordinator resolves the injection formula against the energy leg's mean, roughly
  doubling the credit in a sunny month, silently.
- **The injection indicative can print NEGATIVE** (below 1,33 c€/kWh Belpex_SPP). Its
  column pattern captures the sign; dropping that does not mis-credit, it takes the whole
  card offline, because a missing indicative is fatal.
- **`_resolve_variable_card_url` must raise `ExtractorError` and nothing else.** Callers
  catch only that. A well-formed-JSON-but-wrong-shape payload used to escape as a bare
  `TypeError`, and a non-string `tariffDocument` was `str()`-ed into a nonsense URL and
  fetched.
- **Kempen and Midden-Vlaanderen map to non-obvious keys** (`fluvius_iveka`,
  `fluvius_intergem`).

## Test fixtures

The fixtures live under `tests/fixtures/`:

| fixture | represents |
| --- | --- |
| `energiebe_dynamic_jul.pdf` | the full `?key=DynamicTariffs` PDF (residential + professional), July 2026 |
| `energiebe_variable_aug.pdf` | the residential variable card named by the contracts API, August 2026 |
| `energiebe_fixed_aug.pdf` | the residential fixed card named by the contracts API, August 2026 |

Tests load them through `fixture_text(..., layout=True)`, matching the layout-preserving
extraction used in production. The dynamic fixture is from a different month on purpose: the
August card is the one that prints the post-2026-08-01 tax block (excise 4,8760, federal
contribution 0), so the pair covers both sides of that change.

## When the card changes, look here

| symptom | likely culprit | why |
| --- | --- | --- |
| "could not parse energie.be energy formula" | `_ENERGY_RE` | the "formule (excl. BTW):" wording, Belpex wording or sign chars changed |
| "could not parse energie.be variable energy formula" | `_ENERGY_RE` | same row on the variable card; check the `Belpex_RLP` parameter name first |
| "could not parse energie.be fixed energy price" | `_FIXED_ENERGY_RE` | the "Energieprijs" column label on the fixed card changed |
| "fixed contract served an indexed (variable/dynamic) card" / "does not print a 'vaste prijs'" | `_resolve_card_url` or the card itself | the contracts API pointed the Fixed entry at another product's PDF, or the fixed card started printing a formula |
| "energie.be: no residential variable card in contracts API" | `_resolve_variable_card_url` | the contracts API changed its `tariffType` / `contractTypeElRes` shape, or dropped the product |
| "energie.be contracts API parse error" | `_resolve_variable_card_url` | the endpoint stopped returning JSON (an HTML error page, a login wall) |
| "energie.be: injection indicative row not found" | `_INJECTION_CURRENT_RE` | the variable card's "Zonnestroom" column label changed |
| Variable price plausible but consistently off | the `spot_monthly` mean, not the parser | the month is still filling in, or the ENTSO-E cache has gaps; the rate firms up as the month completes |
| Wrong per-kWh price after a card update | the c€/kWh conversion in `_extract_energy` (`:361`) | energie.be switched Belpex units (to EUR/MWh) or the VAT treatment changed |
| "energie.be: vaste vergoeding row not found" | `_FEE_RE` (`:210`) | the "Vaste vergoeding ... (€/jaar)" label reworded |
| Solar credit wrong or "injection formula row not found" | `_INJECTION_RE` | the "Terugleveringsvergoeding" section header reworded, or the sign dropped |
| Solar credit roughly double on the variable contract | `spp_indexed` / `_spp_weighting_enabled` | the flag was dropped, or the Synergrid profile silently stopped being fetched, so the formula resolves against the energy leg's Belpex_RLP mean instead of Belpex_SPP |
| Solar credit slightly off (a few tenths of a cent) on the variable contract | expected while the Synergrid profile is unavailable | the card's printed indicative is a VNR forecast; it is the deliberate fallback, and the credit firms up once the profile loads |
| Tax under/over-billing or "tax block"/"GSC/WKK" errors | `_extract_taxes` regexes (`:213`-221) | a levy row label or unit changed; energy fund is the only optional one |
| Professional rows leaking into the snapshot | `_PROF_MARKER` / `_residential` (`:138`, `:332`) | the professional section header wording changed |
| A DSO sub-area missing, or all DSOs missing | `_DSO_ROWS` and the row regex in `_extract_dsos` (`:144`, `:551`); the "Nettarieven" anchor | a label renamed, a new wrap artifact, or the section header changed |
| Coordinator never refreshes | none - there is no probe; the time-based TTL drives refetch | expected for this supplier |

# Provider: luminus

This document is the maintainer reference for the Luminus extractor
(`providers/luminus.py`). Luminus is a large Belgian residential supplier selling
in Flanders and Wallonia only; it publishes the current month's tariff card as a
fresh PDF served by a public REST endpoint, one card per (product, region). The
extractor fetches exactly the configured region's PDF, never merges regions, and
parses the energy formula, the DSO network / capacity overlay, the tax overlay,
and the injection (solar feed-in) rate out of that single card. Read this
alongside the shared contracts and the pricing math it feeds:

- [../provider-framework.md](../provider-framework.md): the `SupplierExtractor`
  protocol, the `Contract` / `SupplierSnapshot` / `DsoOverlay` dataclasses, and
  the shared `_pdf.py` helpers this module calls.
- [../pricing-model.md](../pricing-model.md): how `compute_breakdown` consumes
  the snapshot's energy, DSO, tax and injection fields.

The test module `tests/test_luminus.py` pins the expected parse output against
real April/May 2026 fixtures and is the ground truth for what the extractor
should produce. Every illustrative number below is quoted from a source comment
or a test assertion and labelled as such.

## Overview

| Property | Value | Source |
| --- | --- | --- |
| Extractor id | `luminus` | `luminus.py` |
| Label | `Luminus` | `luminus.py` |
| Regions served | Flanders, Wallonia | `luminus.py`, `_LUMINUS_REGIONS` |
| Publication | one fresh PDF per (product, region) via REST endpoint | `luminus.py` |
| Probe | none: the `SupplierExtractor` is built without a `probe` argument, so it defaults to `None` | `luminus.py` |
| Archive | `fetch_for_month` reads the site's price-list archive (`api/pricelist/products` then `api/pricelist/pdf`), back to September 2021 (see below) | `luminus.py` |

Brussels is deliberately out of scope. Luminus sells only the regulated Social
tariff there, which is auto-assigned to protected customers, carries an all-in
regulated price with no DSO breakdown, and is not user-selectable
(`luminus.py`). A fetch for `brussels` raises `ExtractorError` with the
message `not available in region` (`luminus.py`, asserted by
`test_brussels_is_unsupported`).

### Source URL pattern

The card is fetched from a single REST endpoint that returns a fresh PDF per
request (`luminus.py`):

```
https://www.luminus.be/api-next/get-pricelist/
    ?documentSlug=<slug>&energyType=electricity&language=fr&tabValue=<Flanders|Wallonia>
```

`slug` is the product's `documentSlug` query parameter (per contract, see the
table below). `tabValue` is the region tab: `Flanders` or `Wallonia`
(`_REGION_TO_TAB`, `luminus.py`). The response filename encodes the
month, e.g. April 2026 -> `202604` (`luminus.py`). `language=fr` is
hardcoded, so every card the extractor parses is the French-language variant;
all label regexes below are French.

## Contracts

Ten user-selectable products, all available in both Flanders and Wallonia (each
`Contract.regions` is `_LUMINUS_REGIONS`, so `EXTRACTOR.regions()` is
`{flanders, wallonia}`). None sets `spot_indexed_injection` (default `False`):
the dynamic contract already collects the ENTSO-E key via its energy formula,
and the non-dynamic contracts print a monthly indicative injection so they never
need a spot for injection.

| Contract id | Label | Kind | Slug | Notes |
| --- | --- | --- | --- | --- |
| `luminus_comfy` | Luminus Comfy | fixed | `comfy` | Fixed price, bi-hourly + exclusive-night columns |
| `luminus_comfy_plus` | Luminus Comfy+ | fixed | `comfy-plus` | Fixed variant |
| `luminus_comfyflex` | Luminus ComfyFlex | variable | `comfyflex` | Monthly-indexed variable |
| `luminus_comfyflex_plus` | Luminus ComfyFlex+ | variable | `comfyflex-plus` | Variable variant (drop-in, same parse path) |
| `luminus_maxxfix` | Luminus MaxxFix | fixed | `maxxfix` | Fixed variant |
| `luminus_maxxflex` | Luminus MaxxFlex | variable | `maxxflex` | Variable variant |
| `luminus_basicfix` | Luminus BasicFix | fixed | `basicfix` | Fixed variant |
| `luminus_basicflex` | Luminus BasicFlex | variable | `basicflex` | Variable variant |
| `luminus_smartflex` | Luminus SmartFlex | tou | `smartflex` | Time-of-use (3 seasonal bands), needs SMR3 |
| `luminus_dynamic` | Luminus Dynamic | dynamic | `dynamic` | `factor*Belpex H + base`, hourly billing |

MaxxFlex and SmartFlex carry `month_indexed_energy`, the registry twin of the parsed
`month_indexed`, which offers the optional ENTSO-E key on every solar regime;
ComfyFlex, ComfyFlex+ and BasicFlex print resolved rates and do not.

Declared in `_CONTRACTS` (`luminus.py`); `_CONTRACTS_BY_ID` indexes them
(`luminus.py`); `EXTRACTOR.contracts` is built from them (`luminus.py`).

Retired / omitted product: **Luminus Sociaal/Social** (the regulated CREG
tariff) is intentionally not declared (`luminus.py`), same reasoning as
the Brussels exclusion above.

### Dynamic billing grid

`luminus_dynamic` bills per clock hour, not per quarter-hour. The `DynamicRates`
returned by `_extract_energy` leaves `quarter_hourly` at its default `False`
(`luminus.py`), which is what routes the coordinator to aggregate
ENTSO-E's 15-minute day-ahead curve to hourly. See the `DynamicRates` docstring
(`base.py`): Luminus is listed among the hourly-billing dynamic
suppliers (Frank default, Mega, TotalEnergies, Eneco).

## Fetch strategy

`fetch(session, contract_id, region)` (`luminus.py`):

1. Reject an unknown `contract_id` (`ExtractorError: unknown Luminus contract`).
2. Reject a region other than Flanders/Wallonia (`not available in region`).
3. Build the URL with `_document_url(contract.slug, region)`.
4. `fetch_pdf_text(session, url)` downloads and pypdf-extracts the PDF text
   (`_pdf.py`); the payload is magic-byte-validated as a real PDF, so a
   CDN 404-disguised-as-HTML fails loud (`_pdf.py`).
5. Hand the text to `parse_snapshot` (the pure parser exposed for unit tests,
   `luminus.py`).

### Probe

There is no probe. `EXTRACTOR` does not set `probe`, so it defaults to `None`
(`base.py`). Per the `SnapshotProbe` contract (`base.py`), the
`api-next/get-pricelist/` endpoint mints a fresh PDF per request with no cheap
freshness key the coordinator can rely on, so the time-based TTL takes over.

### Archive

The live endpoint is overwrite-in-place (each slug always returns the current
month), but the site's "Archives de listes de prix" app is backed by two endpoints
that `fetch_for_month` reads:

- `www.luminus.be/api/pricelist/products?language=FR&customerSegment=Residential&energyType=Electricity&region=<Flanders|Wallonia>&signing=YYYY-MM`
  names the products that month had, each with an opaque product id.
  `_archive_product_name` folds the archive's label ("Luminus Comfy Electricité",
  "Luminus BasicFix Online Electricité") onto the catalogue label, the energy word
  and the online marker being the only differences, and `_resolve_archive_product_id`
  picks the id. A month whose list lacks the product answers `None` without a PDF
  fetch.
- `www.luminus.be/api/pricelist/pdf?language=FR&productId=<id>&date=YYYY-MM&region=<R>&inline=true`
  serves that product's card. The PDF arrives with a UTF-8 byte-order mark before
  the magic bytes, which the shared fetch helper already tolerates.

The app's own picker goes back to September 2021. Measured on 11 September 2026,
the June 2026 Comfy Flanders card parsed whole (`avril`-style label, validity
date 30 June 2026, all eight DSO rows), and the validity date is the authoritative
cross-check in `archive_validity_check`, the month name in the title the fallback.

This is what makes a contract start date work on a Luminus entry: until it was
wired the signing-cohort splice had no card to read, so the entry stayed on the
current card whatever date was set, and every past month of the year-to-date was
billed on today's card as a proxy. The docs used to say there was no accessible
archive; the app had one all along.

### `discover()`

`discover(session)` (`luminus.py`) is a CI / live-check helper (not part
of the coordinator's runtime path). It GETs the sitemap
(`https://www.luminus.be/sitemap.xml`, `luminus.py`) and scrapes product
slugs from the `/fr|nl/particuliers/tarifs-energie|onze-tarieven/<slug>/`
structure (`_PRODUCT_PAGE_RE`, `luminus.py`), excluding the regulated
social-tariff index pages (`_EXCLUDED_SLUGS`, `luminus.py`). A failed sitemap
fetch returns an empty set rather than raising.

## Parsing

`parse_snapshot` (`luminus.py`) assembles the snapshot from a set of
focused helpers. Because energy prices, distribution rows and renewables
surcharges all differ between Flanders and Wallonia on every product, the parser
branches hard on `region` (`luminus.py`) and never merges. That region-
awareness is not cosmetic: `test_dynamic_flanders_has_a_different_base`
(`test_luminus.py`) shows the dynamic formula's base is region-specific
(Flanders 50 cents below Wallonia in the fixtures), so a merged snapshot would
silently give one region the wrong base.

Fields pulled and their helpers:

| Field | Helper | Source |
| --- | --- | --- |
| Energy rates | `_extract_energy` | `luminus.py` |
| Injection | `_extract_injection` | `luminus.py` |
| Publication label | `_extract_publication_month` | `luminus.py` |
| Per-kWh taxes (excise, contribution, connection) | `_extract_per_kwh_taxes` | `luminus.py` |
| Energy fund (Flanders only) | `_extract_energy_fund` | `luminus.py` |
| Flanders renewables | `_extract_flanders_renewables` | `luminus.py` |
| Wallonia renewables | `_extract_wallonia_renewables` | `luminus.py` |
| Flanders DSO overlay | `_extract_flanders_dsos` | `luminus.py` |
| Wallonia DSO overlay | `_extract_wallonia_dsos` | `luminus.py` |
| Yearly fixed fee | `_extract_yearly_fee` | `luminus.py` |
| Exclusive-night fee | `_extract_excl_night_fee` | `luminus.py` |
| VAT multiplier | `_vat_multiplier` | `luminus.py` |
| `valid_until` | `parse_valid_until` (shared) | `_pdf.py` |

### Numeric token

`_NUM = r"\d+(?:[,.]\d+)?"` (`luminus.py`) is anchored on a starting and
ending digit precisely so a trailing sentence period is not captured. The comment
flags the concrete hazard: `0,1019 x Belpex H + 2,4591.\n` from
`luminus_dynamic_w` would grab the final `.` under a lazier `[\d,.]+`
(`luminus.py`). Values are parsed with the shared `to_float` (handles
Belgian comma decimals and every Unicode space variant, `_pdf.py`).

### Units

Printed energy rows are in `c€/kWh`; the extractor divides by 100 to store
EUR/kWh (`luminus.py`, `346-349`). Prices are 6% VAT inclusive as printed
(`luminus.py`), so the snapshot's `TaxOverlay.vat_rate` is set to `0.0`
(`luminus.py`) meaning "already VAT-incl" per the `TaxOverlay` convention
(`base.py`). The one exception is the Dynamic formula, printed `hors TVA`
(ex-VAT), handled below.

### Publication label

`_extract_publication_month` (`luminus.py`) reads the parenthesised
`(<month> <year>)` on the first page, e.g. `(avril 2026)`. The May 2026 cards
started padding the inside of the parens (`(mai 2026 )` with a trailing space),
so the regex tolerates optional whitespace inside the parens
(`test_publication_label_tolerates_padded_parens`, `test_luminus.py`).

## Energy formula per kind

`_extract_energy(text, kind)` (`luminus.py`) always parses the yearly
fixed fee first (`_extract_yearly_fee`), then branches on `kind`.

### fixed / variable

Both parse the same four-column `Énergie fournie (c€/kWh)` row (mono / pleines /
creuses / exclusif-nuit), each divided by 100 (`luminus.py`). `fixed`
returns `FixedRates(single, peak, offpeak, exclusive_night, ...)`
(`luminus.py`); `variable` returns `VariableRates(current=mono, peak,
offpeak, exclusive_night, ...)` (`luminus.py`). Illustrative
(`test_comfy_wallonia_fixed_rates_and_dso`): mono `0.2038`, pleines `0.2374`,
creuses `0.1771`, exclusive-night `0.1771` from `luminus_comfy_w.pdf`.

Both carry two yearly fees: `yearly_fixed_fee` (the standard `Redevance fixe`)
and `yearly_fixed_fee_exclusive_night` from `_extract_excl_night_fee`.

### tou (SmartFlex)

Parses the three-rate `Énergie fournie (c€/kWh)` row (peak / transition /
offpeak) by asking `numeric_row` for a row exactly three figures wide, which is
what tells it from the four-figure row the other products print
(`luminus.py`). The second occurrence later in the PDF is the bi-horaire
fallback for non-SMR3 customers (`luminus.py`); it is also three wide,
and the first match wins. Returns `TimeOfUseRates(peak, transition, offpeak,
yearly_fixed_fee, weekend_rule="smartflex_seasonal")` (`luminus.py`).

SmartFlex uses seasonal windows, not the generic CWaPE schedule: peak (pleines)
07-11 + 17-22 all year, the cheapest super-creuses band 11-17 only in spring/
summer (21/03-20/09), 22-07 always creuses. The `weekend_rule`
`"smartflex_seasonal"` tells `pricing.tou_slot` to bill those windows; the
first-year "free Sundays" promo is not modelled (`luminus.py`).
Illustrative (`test_smartflex_parses_as_time_of_use`): peak `0.1554`, transition
`0.1329`, offpeak `0.0672` from `luminus_smartflex_w.pdf`.

Both the `TimeOfUseRates` docstring in `base.py` and this extractor use the
`smartflex_seasonal` weekend rule for SmartFlex; the extractor and its test
(`test_luminus.py`) pin the seasonal behavior.

### dynamic

Parses the ex-VAT formula
`Prélèvement (...) = <factor> x Belpex H <sign> <base>` via `_DYNAMIC_FORMULA_RE`
(`luminus.py`, matched at `luminus.py`). The sign character is
matched from the shared `SIGN_CHARS` class and resolved with `parse_sign`
(`_pdf.py`), so a card that flips to a Unicode minus does not silently
break polarity.

The PDF formula is `c€/kWh hors TVA = factor_pdf * Belpex_eur_mwh + base_cents`.
The extractor converts to EUR/kWh against a EUR/kWh spot and applies the parsed
6% VAT multiplier (`luminus.py`):

```
factor_eur_kwh = factor_pdf * vat * 10.0        # (*1000 mWh->kWh, /100 c->EUR)
base_eur_kwh   = base_pre_vat_cents * vat / 100.0
```

Illustrative (`test_dynamic_wallonia_extracts_consumption_formula`): the PDF
prints `0,1019 x Belpex H + 2,4591` at 6% VAT, yielding `factor == 1.08014` and
`base == 0.02606646`. The test pins the literal results (not
`0.1019 * 1.06 * 10`) so a `1.06 <-> 10` unit-conversion swap cannot cancel out
and pass (`test_luminus.py`).

The VAT rate is read by `_vat_multiplier` (`luminus.py`), which wraps the
shared `vat_multiplier` helper with two Luminus-specific patterns
(`TVA sur les prix ... N %` and `TVA N %`) and the shared 1.06 default
(`_pdf.py`).

## DSO overlay coverage

The DSO table is parsed per region. Distribution values are stored in EUR/kWh
(divide by 100); capacity, data-management and prosumer fees stay in their EUR/yr
units. Distribution already includes transport on the Flanders side (same
convention as Engie), so `transport` is set to `0.0` there (`luminus.py`,
`luminus.py`).

### Flanders (`_extract_flanders_dsos`, `luminus.py`)

Eight Fluvius sub-areas mapped by printed label to canonical key
(`_FLANDERS_LABELS`, `luminus.py`). Watch the two label-to-key surprises:

| Printed label | Canonical key |
| --- | --- |
| Fluvius Antwerpen | `fluvius_antwerpen` |
| Fluvius Halle-Vilvoorde | `fluvius_halle_vilvoorde` |
| Fluvius Imewo | `fluvius_imewo` |
| Fluvius Kempen | `fluvius_iveka` (note: Kempen -> IVEKA) |
| Fluvius Limburg | `fluvius_limburg` |
| Fluvius Midden-Vlaanderen | `fluvius_intergem` (note: Midden-Vlaanderen -> INTERGEM) |
| Fluvius West | `fluvius_west` |
| Fluvius Zenne-Dijle | `fluvius_zenne_dijle` |

Two column layouts are read by asking for each width in turn
(`luminus.py`):

- **Static (fixed/variable/tou) cards print 8 numbers**: data_mgmt €/an,
  capacity_digital €/kW/yr, dist_normal, dist_excl_night, capacity_classic,
  dist_classic_normal, dist_classic_excl, prosumer €/kW/yr. The parser reads the
  digital-meter columns (`nums[0..3]`) plus `nums[7]` prosumer (`luminus.py`).
- **Dynamic (SMR3) cards print 4 numbers**: data_mgmt, capacity_digital,
  dist_normal, dist_excl_night, no analog or prosumer columns. `prosumer` stays
  `None` (`luminus.py`), because post-2024 SMR3 connections carry no
  compensation regime (see `DsoOverlay.prosumer_eur_per_kva_year`,
  `base.py`).

The SMR3 data-management gotcha: the dynamic product meters quarter-hourly, so
its data-management fee is the reduced value from the
`(**) ... quart d'heure ... gestion des données` footnote, not the table's
monthly-regime column. `_extract_flanders_dsos` reads that footnote when
`kind == "dynamic"` and falls back to the table value if it is absent
(`luminus.py`, applied at `luminus.py`).
`test_flanders_dynamic_dso_table_is_smaller_than_static`
(`test_luminus.py`) pins it: Antwerpen dynamic data-management `18.56`
(footnote) vs static `18.92` (table), and the dynamic prosumer is `None` while
static is `54.63` (illustrative).

### Wallonia (`_extract_wallonia_dsos`, `luminus.py`)

Five DSO sub-areas mapped by printed label (`_WALLONIA_LABELS`,
`luminus.py`):

| Printed label | Canonical key |
| --- | --- |
| AIEG | `aieg` |
| AIESH | `aiesh` |
| ORES (Brabant Wallon) | `ores` |
| TECTEO RESA | `resa` |
| WAVRE | `rew` |

Two column layouts (`luminus.py`):

- **Static rows have 7 numbers**: mono, pleines, creuses, excl_nuit, transport,
  data_mgmt, prosumer. `prosumer` is populated (`nums[6]`), the Impact bands stay
  `None`.
- **Dynamic rows have 9 numbers**: mono, pleines, creuses, ECO, MEDIUM, PIC,
  excl_nuit, transport, data_mgmt. The IMPACT triplet (ECO/MEDIUM/PIC) is unique
  to dynamic and its presence flips prosumer off (SMR3 has no compensation
  regime).

Band-ordering gotcha: Luminus prints the Impact triplet **ECO | MEDIUM | PIC in
ascending order**, unlike OCTA+/Bolt where the columns are PIC-first descending
(`luminus.py`). They are mapped to `distribution_eco` / `_medium` /
`_pic` accordingly (`luminus.py`). Illustrative
(`test_comfy_wallonia_fixed_rates_and_dso`): AIEG mono `0.1087`, pleines
`0.1205`, creuses `0.0666`, transport `0.0274`, prosumer `81.03`.

## Tax overlay

`_extract_per_kwh_taxes` (`luminus.py`) reads the
`3 Taxes et redevances : WAL|FL|BRU` block via `_tax_block_values`
(`luminus.py`). That helper anchors on the colon after the label because
`Taxes et redevances` also appears in the `Composition du prix` legend without a
colon or region (`luminus.py`); the block runs until
`INFORMATION SUR VOTRE TARIF` or `Conditions`. Inside the block, values sit alone
on their own lines, and the parser collects that contiguous run of `-` /
`_NUM` tokens (`luminus.py`). The label order and matching value order are
documented in the `_tax_block_values` docstring (`luminus.py`): BTNR,
BTR, excise, contribution, and (Wallonia only) connection.

| TaxOverlay field | Source | Notes |
| --- | --- | --- |
| `federal_excise` | `values[2]` / 100 | mandatory both regions |
| `energy_contribution` | `values[3]` / 100 | mandatory both regions |
| `region_connection_fee` | `values[4]` / 100 | Wallonia only, iff `Redevance de raccordement` present |
| `energy_fund_eur_per_month` | `_extract_energy_fund` BTR row | Flanders only, `values[1]` |
| `flanders_renewables` | `_extract_flanders_renewables` | Flanders only |
| `wallonia_renewables` | `_extract_wallonia_renewables` | Wallonia only |
| `vat_rate` | `0.0` (prices already VAT-incl) | `luminus.py` |

`_extract_per_kwh_taxes` raises on a short block (`< 4` values) or a missing
Walloon connection row, rather than silently zeroing a regulated tax and
underbilling (`luminus.py`). Illustrative
(`test_taxes_split_correctly_per_region`): excise `0.050329`, contribution
`0.002042` (both regions); Wallonia green `0.0303` and connection `0.00075`;
Flanders green `0.0117` + cogen `0.0039` = `0.0156`.

The energy fund uses the BTR (Basse tension résidentiel) value, not BTNR
(non-residential) which is printed first; a `-` means no fee
(`_extract_energy_fund`, `luminus.py`). In both fixture regions today BTR
is `-`, so `energy_fund_eur_per_month` is `0.0` (`test_luminus.py`).

Flanders renewables splits across green-energy + cogeneration
(`_extract_flanders_renewables`, `luminus.py`): the primary regex sums
both `Coûts énergie verte` and `Coûts cogénération`; a fallback handles cards
that print only the green line. Both regional renewables helpers raise on a miss
(the caller has already gated on region, so a miss is layout drift not a fee-free
card).

## Injection

`_extract_injection(text, kind)` (`luminus.py`) covers two of the three
injection shapes in the project taxonomy, selected by contract kind:

- **fixed / variable / tou -> monthly-indicative-only.** The extractor reads the
  applicable `Tarif de l'énergie injectée` row and stores it as
  `InjectionRates.current` (`luminus.py`). `factor` / `base` stay `None`,
  so the pricing engine credits the indicative and never needs a spot.
- **dynamic -> hourly `factor*spot + base`.** `_INJECTION_FORMULA_RE`
  (`luminus.py`) parses `Injection (...) = <factor> x Belpex H <sign>
  <base>`. Residential injection is VAT-exempt, so **no VAT multiplier is
  applied** (`luminus.py`): `factor = factor_pdf * 10.0`, `base =
  base_pdf_cents / 100.0`. Contrast the consumption dynamic formula, which does
  scale by VAT. The formula text is stored in `InjectionRates.formula`.

Illustrative: dynamic Wallonia injection `0,1019 x Belpex H - 1,2737` yields
`factor == 1.019`, `base == -0.012737` (negative base preserved,
`test_dynamic_extracts_injection_formula_with_negative_base`,
`test_luminus.py`). Non-dynamic indicative
(`test_injection_uses_applicable_rate_not_annual_estimate`): comfy Wallonia
`0.0381`, comfyflex Flanders `0.0396`.

Two anchoring subtleties in the indicative regex (`luminus.py`):

1. **Applicable vs annual estimate.** The card prints both the applicable
   `Tarif de l'énergie injectée` and an `Estimation annuelle du tarif de
   l'énergie injectée` 12-month forecast just below. They share the
   `de l'énergie injectée` tail, but only the applicable row capitalises
   `Tarif`, so the case-sensitive `Tarif` binds to the applicable rate
   (`luminus.py`). `test_injection_uses_applicable_rate_not_annual_estimate`
   (`test_luminus.py`) verifies this in both directions, including the
   May card where the estimate (`3.68`) is below the applicable rate (`3.81`),
   so picking the wrong row would under-credit.
2. **Footnote digit + mid-phrase wrap.** Some cards print a footnote digit right
   after the unit (`(c€/kWh)2 3,81`), so the regex skips an optional
   digit-then-whitespace; and the label can wrap mid-phrase
   (`Tarif de l'énergie \ninjectée`), so `\s+` is used between every word with
   `re.S` (`luminus.py`).

Fail-loud invariant: both Luminus card families always publish injection, so if
neither `current` nor `factor` parses, `_extract_injection` raises rather than
silently crediting nothing (`luminus.py`). `test_missing_injection_row_fails_loud`
(`test_luminus.py`) corrupts the `injectée` label and asserts the raise.

There is no supplier-side prosumer / PV forfait on Luminus cards
(`SupplierSnapshot.supplier_prosumer_eur_per_kva_year` is left `None`). The only
prosumer term is the DSO-side Wallonia `prosumer_eur_per_kva_year`.

## Yearly fees and exclusive-night circuit

`_extract_yearly_fee` (`luminus.py`) captures the
`Redevance fixe (€/an)` line and raises on a miss (a regex miss is layout drift,
not a fee-free contract; the comment notes dropping this would silently lose
~70 EUR/year from the user's annual estimate). Illustrative: ~65 EUR static,
~75 EUR dynamic.

`_extract_excl_night_fee` (`luminus.py`) reads the third column of the
`Redevance fixe` row on static/variable cards (`mono | bi | exclusif nuit`, e.g.
`65,00 65,00 -`). A `-` means the exclusive-night circuit carries no separate
abonnement, so it must bill `0`, not the standard fee (it is billed once on the
main connection). Returns `None` when there is no third column (dynamic cards
print a single value and offer no exclusive-night), so the standard fee applies.
`test_comfy_wallonia_fixed_rates_and_dso` (`test_luminus.py`) confirms
`yearly_fixed_fee_exclusive_night == 0.0` and that
`yearly_fixed_fee_for_meter(..., "exclusive_night")` returns `0.0`.

## Quirks and historical bugs (the land mines)

- **6% VAT-inclusive prices, ex-VAT dynamic formula.** Everything printed is 6%
  VAT-incl, but the Dynamic `Prélèvement` formula is `hors TVA`, so its factor
  and base are scaled by the parsed VAT multiplier (`luminus.py`,
  `330-338`). Injection is always VAT-exempt and is never scaled
  (`luminus.py`).
- **Region-specific dynamic base.** Flanders and Wallonia have different bases
  in the same formula; never merge regions into one snapshot
  (`test_luminus.py`).
- **Applicable-vs-estimate rows** on both the consumption (`Énergie fournie` vs
  `Estimation annuelle de l'énergie fournie`) and injection (`Tarif` vs
  `Estimation annuelle du tarif`) sides. Always take the current-month
  applicable row (`test_comfyflex_flanders_uses_current_monthly_not_annual_estimate`,
  `test_luminus.py`; injection at `test_luminus.py`).
- **SMR3 reduced data-management fee** from the `quart d'heure` footnote on
  dynamic Flanders cards, not the table's monthly column (`luminus.py`).
- **Two DSO column widths** per region (static wide, dynamic narrow); the row
  is asked for at each width in turn, with prosumer present only on static
  (`luminus.py`, `630-683`).
- **Wallonia Impact triplet is ECO/MEDIUM/PIC ascending**, opposite to OCTA+/Bolt
  (`luminus.py`).
- **Label-to-key remaps**: Fluvius Kempen -> IVEKA, Fluvius Midden-Vlaanderen ->
  INTERGEM, in the shared `FLUVIUS_CARD_LABELS` (`const.py`, aliased at
  `luminus.py`).
- **Trailing-period token hazard** in the dynamic formula, guarded by the
  digit-anchored `_NUM` (`luminus.py`).
- **Padded publication parens** on the May 2026 cards (`(mai 2026 )`),
  tolerated by optional whitespace (`luminus.py`).
- **Numeric-token double-occurrence in the TOU row**: the three-figure width
  picks the SMR3 three-band row over the four-figure rows around it, and the
  first match wins over the bi-horaire fallback below (`luminus.py`).
- **Fail-loud policy**: yearly fee, injection, per-kWh taxes, and both regional
  renewables all raise on a miss rather than defaulting to 0 and silently
  mispricing (`luminus.py`, `424`, `485-495`, `540-542`, `554-557`).

## Test fixtures

Under `tests/fixtures/`, exercised by `tests/test_luminus.py`:

| Fixture | Variant | Used by |
| --- | --- | --- |
| `luminus_comfy_w.pdf` | Comfy (fixed), Wallonia, April 2026 | `_comfy_w`, fixed rates + DSO + injection tests |
| `luminus_comfy_w_may.pdf` | Comfy (fixed), Wallonia, May 2026 | padded-parens + estimate-vs-applicable injection tests |
| `luminus_comfyflex_v.pdf` | ComfyFlex (variable), Flanders, April 2026 | current-month energy, Flanders DSO/injection |
| `luminus_comfyflex_plus_w.pdf` | ComfyFlex+ (variable), Wallonia | drop-in variable parse path |
| `luminus_maxxflex_w.pdf` | MaxxFlex (variable), Wallonia | variable parse path |
| `luminus_smartflex_w.pdf` | SmartFlex (tou), Wallonia | TOU three-band + seasonal weekend rule |
| `luminus_dynamic_w.pdf` | Dynamic, Wallonia | consumption + injection formula, taxes |
| `luminus_dynamic_v.pdf` | Dynamic, Flanders | region-specific base, narrow SMR3 DSO table |

Note: fixtures for `luminus_comfy_plus`, `luminus_maxxfix`, `luminus_basicfix`
and `luminus_basicflex` are not present; those contracts share the fixed /
variable parse paths already covered by the fixtures above.

## When the card changes, look here

| Symptom | First place to look | Why |
| --- | --- | --- |
| Every field misses / fetch fails | `fetch` + `fetch_pdf_text` (`luminus.py`, `_pdf.py`) | URL construction, slug/tabValue, PDF magic-byte validation |
| Energy rates wrong / missing | `_extract_energy` (`luminus.py`) | four-column vs three-column row, unit /100, TOU lookahead |
| Dynamic factor/base off by ~1.06 or ~10 | dynamic branch (`luminus.py`) | VAT multiplier + mWh->kWh + c->EUR conversion |
| Injection wrong or raising | `_extract_injection` (`luminus.py`) | applicable-vs-estimate `Tarif` capitalisation, VAT-exempt scaling |
| A DSO row missing | `_FLANDERS_LABELS` / `_WALLONIA_LABELS` + row regexes (`luminus.py`, `567-618`, `621-627`, `630-683`) | printed label renamed, or column count changed |
| Dynamic data-management fee wrong (Flanders) | footnote regex (`luminus.py`) | `quart d'heure ... gestion des données` phrasing drift |
| Tax value zeroed / block too short | `_tax_block_values` + `_extract_per_kwh_taxes` (`luminus.py`) | colon anchor, value-run boundary, BTNR/BTR ordering |
| Yearly / exclusive-night fee wrong | `_extract_yearly_fee` / `_extract_excl_night_fee` (`luminus.py`) | `Redevance fixe` line format, third-column `-` handling |
| Publication label empty | `_extract_publication_month` (`luminus.py`) | parens padding / month spelling |
| A new product appears / a slug 404s | `_CONTRACTS` + `discover` (`luminus.py`, `148-162`) | add a `_ContractDef`; sitemap slug directory |

When the layout drifts, refresh the affected fixture PDF under `tests/fixtures/`
and re-run `pytest tests/test_luminus.py`; the test assertions encode the
expected numeric output and will pinpoint which helper regressed.

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
| Extractor id | `luminus` | `luminus.py:801` |
| Label | `Luminus` | `luminus.py:802` |
| Regions served | Flanders, Wallonia | `luminus.py:798`, `_LUMINUS_REGIONS` |
| Publication | one fresh PDF per (product, region) via REST endpoint | `luminus.py:93`, `luminus.py:140` |
| Probe | none: the `SupplierExtractor` is built without a `probe` argument, so it defaults to `None` | `luminus.py:1025-1042` |
| Archive | `fetch_for_month` reads the site's price-list archive (`api/pricelist/products` then `api/pricelist/pdf`), back to September 2021 (see below) | `luminus.py` |

Brussels is deliberately out of scope. Luminus sells only the regulated Social
tariff there, which is auto-assigned to protected customers, carries an all-in
regulated price with no DSO breakdown, and is not user-selectable
(`luminus.py:36-38`). A fetch for `brussels` raises `ExtractorError` with the
message `not available in region` (`luminus.py:190-193`, asserted by
`test_brussels_is_unsupported`).

### Source URL pattern

The card is fetched from a single REST endpoint that returns a fresh PDF per
request (`luminus.py:93`, `luminus.py:140-145`):

```
https://www.luminus.be/api-next/get-pricelist/
    ?documentSlug=<slug>&energyType=electricity&language=fr&tabValue=<Flanders|Wallonia>
```

`slug` is the product's `documentSlug` query parameter (per contract, see the
table below). `tabValue` is the region tab: `Flanders` or `Wallonia`
(`_REGION_TO_TAB`, `luminus.py:95-107`). The response filename encodes the
month, e.g. April 2026 -> `202604` (`luminus.py:34-36`). `language=fr` is
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

Declared in `_CONTRACTS` (`luminus.py:118-135`); `_CONTRACTS_BY_ID` indexes them
(`luminus.py:137`); `EXTRACTOR.contracts` is built from them (`luminus.py:1021-1030`).

Retired / omitted product: **Luminus Sociaal/Social** (the regulated CREG
tariff) is intentionally not declared (`luminus.py:131-134`), same reasoning as
the Brussels exclusion above.

### Dynamic billing grid

`luminus_dynamic` bills per clock hour, not per quarter-hour. The `DynamicRates`
returned by `_extract_energy` leaves `quarter_hourly` at its default `False`
(`luminus.py:446-450`), which is what routes the coordinator to aggregate
ENTSO-E's 15-minute day-ahead curve to hourly. See the `DynamicRates` docstring
(`base.py:141-154`): Luminus is listed among the hourly-billing dynamic
suppliers (Frank default, Mega, TotalEnergies, Eneco).

## Fetch strategy

`fetch(session, contract_id, region)` (`luminus.py:181-196`):

1. Reject an unknown `contract_id` (`ExtractorError: unknown Luminus contract`).
2. Reject a region other than Flanders/Wallonia (`not available in region`).
3. Build the URL with `_document_url(contract.slug, region)`.
4. `fetch_pdf_text(session, url)` downloads and pypdf-extracts the PDF text
   (`_pdf.py:179-186`); the payload is magic-byte-validated as a real PDF, so a
   CDN 404-disguised-as-HTML fails loud (`_pdf.py:168-176`).
5. Hand the text to `parse_snapshot` (the pure parser exposed for unit tests,
   `luminus.py:298-339`).

### Probe

There is no probe. `EXTRACTOR` does not set `probe`, so it defaults to `None`
(`base.py:570`). Per the `SnapshotProbe` contract (`base.py:1317-1321`), the
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

`discover(session)` (`luminus.py:161-175`) is a CI / live-check helper (not part
of the coordinator's runtime path). It GETs the sitemap
(`https://www.luminus.be/sitemap.xml`, `luminus.py:148`) and scrapes product
slugs from the `/fr|nl/particuliers/tarifs-energie|onze-tarieven/<slug>/`
structure (`_PRODUCT_PAGE_RE`, `luminus.py:152-154`), excluding the regulated
social-tariff index pages (`_EXCLUDED_SLUGS`, `luminus.py:159`). A failed sitemap
fetch returns an empty set rather than raising.

## Parsing

`parse_snapshot` (`luminus.py:297-336`) assembles the snapshot from a set of
focused helpers. Because energy prices, distribution rows and renewables
surcharges all differ between Flanders and Wallonia on every product, the parser
branches hard on `region` (`luminus.py:311-316`) and never merges. That region-
awareness is not cosmetic: `test_dynamic_flanders_has_a_different_base`
(`test_luminus.py:117-129`) shows the dynamic formula's base is region-specific
(Flanders 50 cents below Wallonia in the fixtures), so a merged snapshot would
silently give one region the wrong base.

Fields pulled and their helpers:

| Field | Helper | Source |
| --- | --- | --- |
| Energy rates | `_extract_energy` | `luminus.py:402-477` |
| Injection | `_extract_injection` | `luminus.py:555-688` |
| Publication label | `_extract_publication_month` | `luminus.py:658-667` |
| Per-kWh taxes (excise, contribution, connection) | `_extract_per_kwh_taxes` | `luminus.py:779-809` |
| Energy fund (Flanders only) | `_extract_energy_fund` | `luminus.py:812-821` |
| Flanders renewables | `_extract_flanders_renewables` | `luminus.py:824-855` |
| Wallonia renewables | `_extract_wallonia_renewables` | `luminus.py:858-870` |
| Flanders DSO overlay | `_extract_flanders_dsos` | `luminus.py:830-881` |
| Wallonia DSO overlay | `_extract_wallonia_dsos` | `luminus.py:893-946` |
| Yearly fixed fee | `_extract_yearly_fee` | `luminus.py:437-449` |
| Exclusive-night fee | `_extract_excl_night_fee` | `luminus.py:452-469` |
| VAT multiplier | `_vat_multiplier` | `luminus.py:429-434` |
| `valid_until` | `parse_valid_until` (shared) | `_pdf.py:985-1086` |

### Numeric token

`_NUM = r"\d+(?:[,.]\d+)?"` (`luminus.py:350`) is anchored on a starting and
ending digit precisely so a trailing sentence period is not captured. The comment
flags the concrete hazard: `0,1019 x Belpex H + 2,4591.\n` from
`luminus_dynamic_w` would grab the final `.` under a lazier `[\d,.]+`
(`luminus.py:344-346`). Values are parsed with the shared `to_float` (handles
Belgian comma decimals and every Unicode space variant, `_pdf.py:507-518`).

### Units

Printed energy rows are in `c€/kWh`; the extractor divides by 100 to store
EUR/kWh (`luminus.py:418-420`, `346-349`). Prices are 6% VAT inclusive as printed
(`luminus.py:42-44`), so the snapshot's `TaxOverlay.vat_rate` is set to `0.0`
(`luminus.py:333`) meaning "already VAT-incl" per the `TaxOverlay` convention
(`base.py:479-482`). The one exception is the Dynamic formula, printed `hors TVA`
(ex-VAT), handled below.

### Publication label

`_extract_publication_month` (`luminus.py:658-667`) reads the parenthesised
`(<month> <year>)` on the first page, e.g. `(avril 2026)`. The May 2026 cards
started padding the inside of the parens (`(mai 2026 )` with a trailing space),
so the regex tolerates optional whitespace inside the parens
(`test_publication_label_tolerates_padded_parens`, `test_luminus.py:402-411`).

## Energy formula per kind

`_extract_energy(text, kind)` (`luminus.py:405-480`) always parses the yearly
fixed fee first (`_extract_yearly_fee`), then branches on `kind`.

### fixed / variable

Both parse the same four-column `Énergie fournie (c€/kWh)` row (mono / pleines /
creuses / exclusif-nuit), each divided by 100 (`luminus.py:539-548`). `fixed`
returns `FixedRates(single, peak, offpeak, exclusive_night, ...)`
(`luminus.py:548-556`); `variable` returns `VariableRates(current=mono, peak,
offpeak, exclusive_night, ...)` (`luminus.py:473-480`). Illustrative
(`test_comfy_wallonia_fixed_rates_and_dso`): mono `0.2038`, pleines `0.2374`,
creuses `0.1771`, exclusive-night `0.1771` from `luminus_comfy_w.pdf`.

Both carry two yearly fees: `yearly_fixed_fee` (the standard `Redevance fixe`)
and `yearly_fixed_fee_exclusive_night` from `_extract_excl_night_fee`.

### tou (SmartFlex)

Parses the three-rate `Énergie fournie (c€/kWh)` row (peak / transition /
offpeak) with a negative lookahead `(?!\s+\d)` so it anchors on the first
occurrence and not the second (`luminus.py:412-420`). The second occurrence later
in the PDF is the bi-horaire fallback for non-SMR3 customers
(`luminus.py:408-411`). Returns `TimeOfUseRates(peak, transition, offpeak,
yearly_fixed_fee, weekend_rule="smartflex_seasonal")` (`luminus.py:427-433`).

SmartFlex uses seasonal windows, not the generic CWaPE schedule: peak (pleines)
07-11 + 17-22 all year, the cheapest super-creuses band 11-17 only in spring/
summer (21/03-20/09), 22-07 always creuses. The `weekend_rule`
`"smartflex_seasonal"` tells `pricing.tou_slot` to bill those windows; the
first-year "free Sundays" promo is not modelled (`luminus.py:421-426`).
Illustrative (`test_smartflex_parses_as_time_of_use`): peak `0.1554`, transition
`0.1329`, offpeak `0.0672` from `luminus_smartflex_w.pdf`.

Both the `TimeOfUseRates` docstring in `base.py` and this extractor use the
`smartflex_seasonal` weekend rule for SmartFlex; the extractor and its test
(`test_luminus.py:374`) pin the seasonal behavior.

### dynamic

Parses the ex-VAT formula
`Prélèvement (...) = <factor> x Belpex H <sign> <base>` via `_DYNAMIC_FORMULA_RE`
(`luminus.py:352-355`, matched at `luminus.py:436-440`). The sign character is
matched from the shared `SIGN_CHARS` class and resolved with `parse_sign`
(`_pdf.py:527-539`), so a card that flips to a Unicode minus does not silently
break polarity.

The PDF formula is `c€/kWh hors TVA = factor_pdf * Belpex_eur_mwh + base_cents`.
The extractor converts to EUR/kWh against a EUR/kWh spot and applies the parsed
6% VAT multiplier (`luminus.py:442-450`):

```
factor_eur_kwh = factor_pdf * vat * 10.0        # (*1000 mWh->kWh, /100 c->EUR)
base_eur_kwh   = base_pre_vat_cents * vat / 100.0
```

Illustrative (`test_dynamic_wallonia_extracts_consumption_formula`): the PDF
prints `0,1019 x Belpex H + 2,4591` at 6% VAT, yielding `factor == 1.08014` and
`base == 0.02606646`. The test pins the literal results (not
`0.1019 * 1.06 * 10`) so a `1.06 <-> 10` unit-conversion swap cannot cancel out
and pass (`test_luminus.py:92-106`).

The VAT rate is read by `_vat_multiplier` (`luminus.py:429-434`), which wraps the
shared `vat_multiplier` helper with two Luminus-specific patterns
(`TVA sur les prix ... N %` and `TVA N %`) and the shared 1.06 default
(`_pdf.py:411-438`).

## DSO overlay coverage

The DSO table is parsed per region. Distribution values are stored in EUR/kWh
(divide by 100); capacity, data-management and prosumer fees stay in their EUR/yr
units. Distribution already includes transport on the Flanders side (same
convention as Engie), so `transport` is set to `0.0` there (`luminus.py:891-892`,
`luminus.py:723`).

### Flanders (`_extract_flanders_dsos`, `luminus.py:830-881`)

Eight Fluvius sub-areas mapped by printed label to canonical key
(`_FLANDERS_LABELS`, `luminus.py:830-893`). Watch the two label-to-key surprises:

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

Two column layouts are handled by the same row regex (`luminus.py:709-716`):

- **Static (fixed/variable/tou) cards print 8 numbers**: data_mgmt €/an,
  capacity_digital €/kW/yr, dist_normal, dist_excl_night, capacity_classic,
  dist_classic_normal, dist_classic_excl, prosumer €/kW/yr. The parser reads the
  digital-meter columns (`nums[0..3]`) plus `nums[7]` prosumer (`luminus.py:719`).
- **Dynamic (SMR3) cards print 4 numbers**: data_mgmt, capacity_digital,
  dist_normal, dist_excl_night, no analog or prosumer columns. `prosumer` stays
  `None` (`luminus.py:719`), because post-2024 SMR3 connections carry no
  compensation regime (see `DsoOverlay.prosumer_eur_per_kva_year`,
  `base.py:330-336`).

The SMR3 data-management gotcha: the dynamic product meters quarter-hourly, so
its data-management fee is the reduced value from the
`(**) ... quart d'heure ... gestion des données` footnote, not the table's
monthly-regime column. `_extract_flanders_dsos` reads that footnote when
`kind == "dynamic"` and falls back to the table value if it is absent
(`luminus.py:697-705`, applied at `luminus.py:724-726`).
`test_flanders_dynamic_dso_table_is_smaller_than_static`
(`test_luminus.py:191-309`) pins it: Antwerpen dynamic data-management `18.56`
(footnote) vs static `18.92` (table), and the dynamic prosumer is `None` while
static is `54.63` (illustrative).

### Wallonia (`_extract_wallonia_dsos`, `luminus.py:893-946`)

Five DSO sub-areas mapped by printed label (`_WALLONIA_LABELS`,
`luminus.py:733-739`):

| Printed label | Canonical key |
| --- | --- |
| AIEG | `aieg` |
| AIESH | `aiesh` |
| ORES (Brabant Wallon) | `ores` |
| TECTEO RESA | `resa` |
| WAVRE | `rew` |

Two column layouts (`luminus.py:755-782`):

- **Static rows have 7 numbers**: mono, pleines, creuses, excl_nuit, transport,
  data_mgmt, prosumer. `prosumer` is populated (`nums[6]`), the Impact bands stay
  `None`.
- **Dynamic rows have 9 numbers**: mono, pleines, creuses, ECO, MEDIUM, PIC,
  excl_nuit, transport, data_mgmt. The IMPACT triplet (ECO/MEDIUM/PIC) is unique
  to dynamic and its presence flips prosumer off (SMR3 has no compensation
  regime).

Band-ordering gotcha: Luminus prints the Impact triplet **ECO | MEDIUM | PIC in
ascending order**, unlike OCTA+/Bolt where the columns are PIC-first descending
(`luminus.py:942-995`). They are mapped to `distribution_eco` / `_medium` /
`_pic` accordingly (`luminus.py:986-997`). Illustrative
(`test_comfy_wallonia_fixed_rates_and_dso`): AIEG mono `0.1087`, pleines
`0.1205`, creuses `0.0666`, transport `0.0274`, prosumer `81.03`.

## Tax overlay

`_extract_per_kwh_taxes` (`luminus.py:779-809`) reads the
`3 Taxes et redevances : WAL|FL|BRU` block via `_tax_block_values`
(`luminus.py:540-576`). That helper anchors on the colon after the label because
`Taxes et redevances` also appears in the `Composition du prix` legend without a
colon or region (`luminus.py:564-567`); the block runs until
`INFORMATION SUR VOTRE TARIF` or `Conditions`. Inside the block, values sit alone
on their own lines, and the parser collects that contiguous run of `-` /
`_NUM` tokens (`luminus.py:347`). The label order and matching value order are
documented in the `_tax_block_values` docstring (`luminus.py:743-766`): BTNR,
BTR, excise, contribution, and (Wallonia only) connection.

| TaxOverlay field | Source | Notes |
| --- | --- | --- |
| `federal_excise` | `values[2]` / 100 | mandatory both regions |
| `energy_contribution` | `values[3]` / 100 | mandatory both regions |
| `region_connection_fee` | `values[4]` / 100 | Wallonia only, iff `Redevance de raccordement` present |
| `energy_fund_eur_per_month` | `_extract_energy_fund` BTR row | Flanders only, `values[1]` |
| `flanders_renewables` | `_extract_flanders_renewables` | Flanders only |
| `wallonia_renewables` | `_extract_wallonia_renewables` | Wallonia only |
| `vat_rate` | `0.0` (prices already VAT-incl) | `luminus.py:330` |

`_extract_per_kwh_taxes` raises on a short block (`< 4` values) or a missing
Walloon connection row, rather than silently zeroing a regulated tax and
underbilling (`luminus.py:596-607`). Illustrative
(`test_taxes_split_correctly_per_region`): excise `0.050329`, contribution
`0.002042` (both regions); Wallonia green `0.0303` and connection `0.00075`;
Flanders green `0.0117` + cogen `0.0039` = `0.0156`.

The energy fund uses the BTR (Basse tension résidentiel) value, not BTNR
(non-residential) which is printed first; a `-` means no fee
(`_extract_energy_fund`, `luminus.py:812-821`). In both fixture regions today BTR
is `-`, so `energy_fund_eur_per_month` is `0.0` (`test_luminus.py:220-221`).

Flanders renewables splits across green-energy + cogeneration
(`_extract_flanders_renewables`, `luminus.py:824-855`): the primary regex sums
both `Coûts énergie verte` and `Coûts cogénération`; a fallback handles cards
that print only the green line. Both regional renewables helpers raise on a miss
(the caller has already gated on region, so a miss is layout drift not a fee-free
card).

## Injection

`_extract_injection(text, kind)` (`luminus.py:558-691`) covers two of the three
injection shapes in the project taxonomy, selected by contract kind:

- **fixed / variable / tou -> monthly-indicative-only.** The extractor reads the
  applicable `Tarif de l'énergie injectée` row and stores it as
  `InjectionRates.current` (`luminus.py:625-631`). `factor` / `base` stay `None`,
  so the pricing engine credits the indicative and never needs a spot.
- **dynamic -> hourly `factor*spot + base`.** `_INJECTION_FORMULA_RE`
  (`luminus.py:356-359`) parses `Injection (...) = <factor> x Belpex H <sign>
  <base>`. Residential injection is VAT-exempt, so **no VAT multiplier is
  applied** (`luminus.py:525-527`): `factor = factor_pdf * 10.0`, `base =
  base_pdf_cents / 100.0`. Contrast the consumption dynamic formula, which does
  scale by VAT. The formula text is stored in `InjectionRates.formula`.

Illustrative: dynamic Wallonia injection `0,1019 x Belpex H - 1,2737` yields
`factor == 1.019`, `base == -0.012737` (negative base preserved,
`test_dynamic_extracts_injection_formula_with_negative_base`,
`test_luminus.py:132-138`). Non-dynamic indicative
(`test_injection_uses_applicable_rate_not_annual_estimate`): comfy Wallonia
`0.0381`, comfyflex Flanders `0.0396`.

Two anchoring subtleties in the indicative regex (`luminus.py:509-514`):

1. **Applicable vs annual estimate.** The card prints both the applicable
   `Tarif de l'énergie injectée` and an `Estimation annuelle du tarif de
   l'énergie injectée` 12-month forecast just below. They share the
   `de l'énergie injectée` tail, but only the applicable row capitalises
   `Tarif`, so the case-sensitive `Tarif` binds to the applicable rate
   (`luminus.py:673-681`). `test_injection_uses_applicable_rate_not_annual_estimate`
   (`test_luminus.py:381-399`) verifies this in both directions, including the
   May card where the estimate (`3.68`) is below the applicable rate (`3.81`),
   so picking the wrong row would under-credit.
2. **Footnote digit + mid-phrase wrap.** Some cards print a footnote digit right
   after the unit (`(c€/kWh)2 3,81`), so the regex skips an optional
   digit-then-whitespace; and the label can wrap mid-phrase
   (`Tarif de l'énergie \ninjectée`), so `\s+` is used between every word with
   `re.S` (`luminus.py:683-692`).

Fail-loud invariant: both Luminus card families always publish injection, so if
neither `current` nor `factor` parses, `_extract_injection` raises rather than
silently crediting nothing (`luminus.py:724-730`). `test_missing_injection_row_fails_loud`
(`test_luminus.py:141-147`) corrupts the `injectée` label and asserts the raise.

There is no supplier-side prosumer / PV forfait on Luminus cards
(`SupplierSnapshot.supplier_prosumer_eur_per_kva_year` is left `None`). The only
prosumer term is the DSO-side Wallonia `prosumer_eur_per_kva_year`.

## Yearly fees and exclusive-night circuit

`_extract_yearly_fee` (`luminus.py:437-449`) captures the
`Redevance fixe (€/an)` line and raises on a miss (a regex miss is layout drift,
not a fee-free contract; the comment notes dropping this would silently lose
~70 EUR/year from the user's annual estimate). Illustrative: ~65 EUR static,
~75 EUR dynamic.

`_extract_excl_night_fee` (`luminus.py:452-469`) reads the third column of the
`Redevance fixe` row on static/variable cards (`mono | bi | exclusif nuit`, e.g.
`65,00 65,00 -`). A `-` means the exclusive-night circuit carries no separate
abonnement, so it must bill `0`, not the standard fee (it is billed once on the
main connection). Returns `None` when there is no third column (dynamic cards
print a single value and offer no exclusive-night), so the standard fee applies.
`test_comfy_wallonia_fixed_rates_and_dso` (`test_luminus.py:137-164`) confirms
`yearly_fixed_fee_exclusive_night == 0.0` and that
`yearly_fixed_fee_for_meter(..., "exclusive_night")` returns `0.0`.

## Quirks and historical bugs (the land mines)

- **6% VAT-inclusive prices, ex-VAT dynamic formula.** Everything printed is 6%
  VAT-incl, but the Dynamic `Prélèvement` formula is `hors TVA`, so its factor
  and base are scaled by the parsed VAT multiplier (`luminus.py:42-44`,
  `330-338`). Injection is always VAT-exempt and is never scaled
  (`luminus.py:525-527`).
- **Region-specific dynamic base.** Flanders and Wallonia have different bases
  in the same formula; never merge regions into one snapshot
  (`test_luminus.py:117-129`).
- **Applicable-vs-estimate rows** on both the consumption (`Énergie fournie` vs
  `Estimation annuelle de l'énergie fournie`) and injection (`Tarif` vs
  `Estimation annuelle du tarif`) sides. Always take the current-month
  applicable row (`test_comfyflex_flanders_uses_current_monthly_not_annual_estimate`,
  `test_luminus.py:180-188`; injection at `test_luminus.py:381-399`).
- **SMR3 reduced data-management fee** from the `quart d'heure` footnote on
  dynamic Flanders cards, not the table's monthly column (`luminus.py:697-705`).
- **Two DSO column widths** per region (static wide, dynamic narrow); the same
  row regex must match both, with prosumer present only on static
  (`luminus.py:679-730`, `630-683`).
- **Wallonia Impact triplet is ECO/MEDIUM/PIC ascending**, opposite to OCTA+/Bolt
  (`luminus.py:767-770`).
- **Label-to-key remaps**: Fluvius Kempen -> IVEKA, Fluvius Midden-Vlaanderen ->
  INTERGEM, in the shared `FLUVIUS_CARD_LABELS` (`const.py:109`, aliased at
  `luminus.py:676`).
- **Trailing-period token hazard** in the dynamic formula, guarded by the
  digit-anchored `_NUM` (`luminus.py:346-350`).
- **Padded publication parens** on the May 2026 cards (`(mai 2026 )`),
  tolerated by optional whitespace (`luminus.py:483-492`).
- **Numeric-token double-occurrence in the TOU row**: the negative lookahead
  `(?!\s+\d)` anchors on the SMR3 three-band row, not the bi-horaire fallback
  below (`luminus.py:412-420`).
- **Fail-loud policy**: yearly fee, injection, per-kWh taxes, and both regional
  renewables all raise on a miss rather than defaulting to 0 and silently
  mispricing (`luminus.py:381`, `424`, `485-495`, `540-542`, `554-557`).

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
| Every field misses / fetch fails | `fetch` + `fetch_pdf_text` (`luminus.py:181-196`, `_pdf.py:179-186`) | URL construction, slug/tabValue, PDF magic-byte validation |
| Energy rates wrong / missing | `_extract_energy` (`luminus.py:402-477`) | four-column vs three-column row, unit /100, TOU lookahead |
| Dynamic factor/base off by ~1.06 or ~10 | dynamic branch (`luminus.py:435-450`) | VAT multiplier + mWh->kWh + c->EUR conversion |
| Injection wrong or raising | `_extract_injection` (`luminus.py:558-691`) | applicable-vs-estimate `Tarif` capitalisation, VAT-exempt scaling |
| A DSO row missing | `_FLANDERS_LABELS` / `_WALLONIA_LABELS` + row regexes (`luminus.py:830-893`, `567-618`, `621-627`, `630-683`) | printed label renamed, or column count changed |
| Dynamic data-management fee wrong (Flanders) | footnote regex (`luminus.py:697-705`) | `quart d'heure ... gestion des données` phrasing drift |
| Tax value zeroed / block too short | `_tax_block_values` + `_extract_per_kwh_taxes` (`luminus.py:694-763`) | colon anchor, value-run boundary, BTNR/BTR ordering |
| Yearly / exclusive-night fee wrong | `_extract_yearly_fee` / `_extract_excl_night_fee` (`luminus.py:370-448`) | `Redevance fixe` line format, third-column `-` handling |
| Publication label empty | `_extract_publication_month` (`luminus.py:658-667`) | parens padding / month spelling |
| A new product appears / a slug 404s | `_CONTRACTS` + `discover` (`luminus.py:118-135`, `148-162`) | add a `_ContractDef`; sitemap slug directory |

When the layout drifts, refresh the affected fixture PDF under `tests/fixtures/`
and re-run `pytest tests/test_luminus.py`; the test assertions encode the
expected numeric output and will pinpoint which helper regressed.

# Provider: engie

This document is the maintenance reference for the Engie Belgium tariff-card
extractor (`providers/engie.py`). Engie is the largest single supplier in the
registry: it publishes ten residential electricity products across all three
regions through a query-string PDF API, and one PDF card (Empower Variable)
carries three different billing modes at once. Read this before touching the
extractor, and always cross-check the test module (`tests/test_engie.py`): every
number in that file is pinned against a real April 2026 fixture and is the ground
truth for what a correct parse produces.

Related reading:

- [../provider-framework.md](../provider-framework.md): the `SupplierExtractor`
  protocol, the `Contract` / `SupplierSnapshot` / rate dataclasses, and the
  shared `_pdf` helpers this module calls.
- [../pricing-model.md](../pricing-model.md): how `compute_breakdown` consumes
  the snapshot (energy formula, DSO overlay, tax overlay, injection shapes,
  `tou_slot()` selection).

## Overview

| Property | Value |
| --- | --- |
| Extractor id | `engie` (`engie.py:875`) |
| Label | `Engie` (`engie.py:876`) |
| Regions served | Flanders, Wallonia, Brussels (union across contracts, `engie.py:870`) |
| Publication form | Per (contract, region) PDF behind a REST query endpoint |
| `source_url` | the bare API base, `engie.py:396` |
| `probe` | none declared (TTL-only, see below) |
| `fetch_for_month` | none declared (API is overwrite-in-place) |

Engie has no single card and no listing page of PDFs. Each (contract, region)
tuple maps to a document slug, and the current month's PDF for that slug is
served by a REST endpoint (`engie.py:98`):

```
https://www.engie.be/api/engie/be/ms/pricing/v1/public/pricesAndConditionsPDF
    ?document=<DOC_CODE>&monthOffset=0&segment=R&language=F
```

The `DOC_CODE` (called the "slug" in the code) is assembled from the contract
family, green/grey colour, fixed/indexed rate letter, the duration in months for
that region, and the region letter (`engie.py:235`):

```
E_<FAMILY>_R_<COLOR>_C_<RATE>_<MONTHS>_<REGION>_F
```

Example (illustrative, from the module docstring `engie.py:31`): the Dynamic
card is fetched as `E_DYNAMIC_R_GREY_C_I_12_V_F` for the Flanders variant.
`segment=R` is residential and `language=F` requests the French document (all
regex anchors in the parser are French: `Consommation`, `Injection`,
`Formule de prix hors TVA`, and so on).

Engie ships up to three regional documents per contract (V/W/B for Vlaanderen /
Wallonie / Bruxelles). The energy formula is region-uniform, but the DSO overlay
and regional levies are not, so `fetch()` downloads only the configured region's
PDF (`engie.py:339`). `parse_snapshot()` nonetheless accepts a multi-region map
so tests can exercise the merge path that stitches DSOs from several regions into
one snapshot (`engie.py:39`, `engie.py:343`).

## Contracts

Ten products are declared in `_CONTRACTS` (`engie.py:129`). Every `Contract`
exposed to the registry (`engie.py:877`) sets `regions` from the contract's
`months_per_region` keys (`engie.py:870`); none set `spot_indexed_injection`
(dynamic contracts already collect the ENTSO-E key via their energy formula, so
the flag stays False, matching the framework note at `base.py:77`).

| contract_id | label | kind | regions | Notes |
| --- | --- | --- | --- | --- |
| `engie_easy_fixed` | Engie Easy Fixed | fixed | V, W, B | GREEN, F. Standard 12-month (V/W), 36-month (B). |
| `engie_easy_variable` | Engie Easy Variable | variable | V, W, B | GREEN, I. Monthly-indexed. |
| `engie_direct_online` | Engie Direct Online | variable | V, W, B | GREEN, I. Online-only variable. |
| `engie_basic_online` | Engie Basic Online | variable | V, W | GREY, I. No Brussels document (`months_per_region` has no `_B`). |
| `engie_dynamic` | Engie Dynamic | dynamic | V, W, B | GREY, I. `quarter_hourly=True` (see below). |
| `engie_empower_fixed` | Engie Empower Fixed | fixed | V, W, B | GREEN, F. Duration slug `00`. |
| `engie_empower_variable` | Engie Empower Variable | variable | V, W, B | GREEN, I. 7-price Consommation row (bi-horaire + Flextime triplet + excl. night). |
| `engie_empower_flextime` | Engie Empower Flextime | tou | V, W, B | GREEN, I. SMR3-only TOU billing of the Empower Variable card (same PDF). |
| `engie_flow` | Engie Flow | variable | V, W, B | GREEN, I. 24-month (V/W), 48-month (B). |
| `engie_empty_house` | Engie Empty House | variable | V, W, B | GREY, I. Mono-only card for vacant homes; bills the `sans domicile` energy fund. |

Region availability is expressed only through the presence of a region letter in
`months_per_region`. Basic Online omits `_B` (`engie.py:164`), so
`fetch()` raises `ExtractorError("... not available in region ...")` if Brussels
is requested for it (`engie.py:336`).

Retired / omitted: Engie's Tarif Social (`E_SOCIAL_R_GREY_C_F`) is deliberately
not in the catalogue (`engie.py:225`): the social tariff is set quarterly by the
CREG, auto-assigned to protected customers rather than picked from a list, and
its PDF carries an all-in regulated price with no DSO breakdown, so it does not
fit the energy-plus-network-plus-tax model.

### Dynamic billing grid

`engie_dynamic` sets `quarter_hourly=True` on its `DynamicRates`
(`engie.py:474`). Engie bills the dynamic consumer formula against `eSpot_15`,
the Belgian day-ahead EPEX price for that specific quarter-hour, so the
integration keeps the native 15-minute slots rather than aggregating to hourly
(`engie.py:466`, framework note `base.py:140`). Billing of long-term YTD
statistics still collapses to hourly because Home Assistant only retains hourly
long-term statistics.

## Fetch strategy

### `fetch(session, contract_id, region)` (`engie.py:323`)

1. Look up the contract def by id, raise `unknown Engie contract` on miss
   (`engie.py:329`).
2. Map `region` to its V/W/B code (`_REGION_TO_CODE`, `engie.py:106`); raise
   `unknown region` on miss (`engie.py:334`).
3. Reject regions the contract has no document for (`engie.py:336`).
4. Build the slug and query URL (`_document_url`, `engie.py:240`), download and
   extract the PDF text with `fetch_pdf_text` (`_pdf.py:179`), and delegate to
   `parse_snapshot` with a single-region text map (`engie.py:340`).

### `probe`: none

`EXTRACTOR` declares no `probe` (`engie.py:874` has no `probe=` argument, so it
defaults to `None`, `base.py:541`). Engie's tariff API has no cheap freshness
key: the endpoint always serves "the current month" for a slug with no ETag or
listing to diff. Per the framework contract (`base.py:513`), a `None` probe means
the coordinator's time-based TTL governs refresh instead.

### `fetch_for_month`: none

`EXTRACTOR` declares no `fetch_for_month` (`engie.py:874`), so it defaults to
`None` (`base.py:547`). The API is overwrite-in-place / API-only: `monthOffset=0`
is hardcoded in the URL (`engie.py:243`) and there is no accessible archive of
past months. The framework lists Engie explicitly as an API-only supplier with no
month archive (`base.py:523`). For historical billing the coordinator falls back
to the current snapshot as a proxy.

### `discover(session)` (`engie.py:298`)

A best-effort, informational family-level catalog check, not part of the fetch
path. Engie has no list endpoint, so this scrapes `sitemap.xml`
(`_SITEMAP_URL`, `engie.py:247`) for `/(fr|nl)/<token>-(tarief|faq|contract|...)`
product-page URLs (`_PRODUCT_PAGE_RE`, `engie.py:278`), maps each token to a
registry family via `_URL_TOKEN_TO_FAMILY` (`engie.py:253`), and surfaces
unmapped tokens as possible new families. It filters `_NOISE_TOKENS`
(`engie.py:286`): NL/FR common words like `uw` ("your") and `vragen`
("questions") that match the product-page pattern in non-product marketing pages.
A sitemap fetch failure returns an empty set (`engie.py:310`); false positives are
tolerated because the output is a catalog hint only.

## Parsing

`parse_snapshot(contract_id, region_texts)` (`engie.py:343`) is the pure parser
used by both `fetch()` and the tests. Because the energy formula, injection,
federal excise, and energy contribution are supplier-set or federal and identical
across regions, it reads them from any one region's PDF (`engie.py:352`). It then
loops over the region texts to gather each region's DSO rows and regional levies
(`engie.py:365`).

Fields pulled from the card:

| Field | Helper | Notes |
| --- | --- | --- |
| Energy rates | `_extract_energy` (`engie.py:431`) | Branches on `TariffKind`; returns Fixed/Variable/Dynamic/TOU rates. |
| Injection | `_extract_injection` (`engie.py:546`) | See taxonomy below. |
| Publication month label | `_extract_publication_month` (`engie.py:538`) | Anchored on `contrats conclus en <Month> <Year>`. |
| Federal excise | `_extract_federal_excise` (`engie.py:623`) | Flat "Toutes consommations" row when present, else the 0-3000 kWh tier row. The federal scheme folded the energy contribution into the special excise and flattened it on 2026-08-01, so the August card dropped the four-tier table. The energy contribution row went with it and now defaults to 0 rather than raising. |
| Energy contribution | `_extract_energy_contribution` (`engie.py:649`) | Comma-stripped digits reconstructed. |
| Regional renewables | `_extract_consumption_renewables` (`engie.py:605`) | Trailing column of the Consommation row. |
| Flemish energy fund | `_extract_energy_fund` (`engie.py:671`) | `avec`/`sans domicile` cases. |
| Walloon connection fee | `_extract_connection_fee` (`engie.py:688`) | Wallonia only. |
| Flanders DSOs | `_extract_flanders_dsos` (`engie.py:715`) | Digital-meter Fluvius table. |
| Wallonia DSOs | `_extract_wallonia_dsos` (`engie.py:764`) | 9/10-number rows, ORES divergence guard. |
| Brussels DSO | `_extract_brussels_dsos` (`engie.py:834`) | Sibelga row + Brugel OSP table. |
| `valid_until` | `parse_valid_until` (`_pdf.py:794`) | Best-effort validity date. |

### The yearly-fee two-layout problem

Engie prints the yearly subscription fee in two different places
(`engie.py:432`):

- Standard cards (Easy / Dynamic / Empty House): the fee sits on the same logical
  row as `Type d'usage`, e.g. `65,00 €/an Type d'usage` (`engie.py:445`).
- Empower variants (Variable / Flextime): the fee is the first number on the
  `Prix mensuels` row, just before `Consommation(2)`, and there is no
  `Type d'usage` anchor at all (`engie.py:447`).

The parser tries the standard anchor, falls back to the Empower layout, and
raises `yearly fee row not found` if neither matches (`engie.py:450`): every
residential Engie card carries a fee, so a miss is layout drift, not a fee-free
product. The apostrophe in `d'usage` is matched as `[©']` because pypdf sometimes
renders the typographic apostrophe as a copyright glyph.

### Consumption row and price-column counts

`_extract_energy` captures the whole `Consommation(2)` row and reads its numbers
(`engie.py:480`). The last column is always the regional renewables levy and is
dropped (`engie.py:485`); the remaining numbers are the price columns, and their
count selects the layout (`engie.py:490`):

| Column count | Layout | Meaning |
| --- | --- | --- |
| 4 | `mono | bi-pleines | bi-creuses | excl-nuit` | Standard bi-hourly card. |
| 7 | `mono | bi-pleines | bi-creuses | Flextime pleines | Flextime creuses | Flextime super-creuses | excl-nuit` | Empower Variable card (carries Flextime). |
| 1 | `mono` | Mono-only card (Empty House). |

Prices are divided by 100 (the card prints c€/kWh). Any other count raises
`unexpected price column count` (`engie.py:514`).

On the 7-column Empower card the pricing model only carries mono + bi-horaire +
exclusive-night, so the three Flextime middle columns are skipped for the
non-Flextime variants, and exclusive-night is taken from index 6, not from the
visually-cheapest Flextime super-creuses column (`engie.py:509`, test
`test_empower_variable_skips_flextime_tiers` `tests/test_engie.py:261`).

### Dynamic formula parsing and unit conversion

Dynamic cards print `Formule de prix hors TVA <base> + (<factor> x eSpot_15)`.
`_FORMULA_RE` (`engie.py:425`) accepts the full sign class `[SIGN_CHARS]` on both
the base and the factor and routes each through `parse_sign` (`_pdf.py:532`) so a
re-render that swaps a hyphen-minus for a Unicode minus or en-dash does not
silently miss (`engie.py:419`). The formula is printed pre-VAT, so factor and
base are scaled by the parsed VAT multiplier (`engie.py:461`). The conversion
(`engie.py:463`), converting c€/kWh-hors-TVA over EUR/MWh spot into EUR/kWh over
EUR/kWh spot:

```
factor_eur_kwh = factor_pdf * vat * 1000 / 100 = factor_pdf * vat * 10
base_eur_kwh   = base_cents  * vat / 100
```

`test_dynamic_extracts_consumption_formula` (`tests/test_engie.py:130`) pins the
result (illustrative, April 2026 card printing `0,8702 + (0,1039 x eSpot_15)` at
6% VAT): `factor == 1.10134`, `base == 0.00922412`, `yearly_fixed_fee == 100.7`.
The pinned literal deliberately guards a `1.06` vs `10` unit-swap bug that would
otherwise cancel out (`0.1039 * 10.6 == 0.1039 * 1.06 * 10`).

### Tax-block parse hurdles

- `_extract_energy_contribution` (`engie.py:649`): Engie's PDF strips the comma,
  so `0,20417` renders as `020417`. The regex matches an optional separator and
  reconstructs the value as `0.<digits>` with a `\d{4,6}` quantifier
  (illustrative parsed value `0.0020417`, test `tests/test_engie.py:230`).
- `_extract_federal_excise` (`engie.py:623`): anchored on
  `Consommation entre 0 et 3.000 kWh`; mandatory across regions, raises on miss.
- `_extract_consumption_renewables` (`engie.py:605`): takes the last number on
  the Consommation row as the regional renewable surcharge (Flanders cogen +
  green, Wallonia green contribution, or Brussels green levy). Mandatory in every
  region (source comment `~1.5-3 c€/kWh`); raises on miss so a levy is never
  silently dropped.

## Energy formula by TariffKind

| kind | Returned dataclass | How the rates map |
| --- | --- | --- |
| fixed | `FixedRates` via `fixed_or_variable_rates` (`engie.py:528`) | `single/peak/offpeak/exclusive_night` from the 4- or 7-column row + `yearly_fixed_fee`. |
| variable | `VariableRates` via `fixed_or_variable_rates` (`engie.py:528`) | `current/peak/offpeak/exclusive_night`; monthly-indexed. Reads the `Prix mensuels` row, not the `Prix annuels estimés` row (see quirks). |
| dynamic | `DynamicRates` (`engie.py:470`) | `factor * eSpot_15 + base`, VAT-scaled, `quarter_hourly=True`. |
| tou | `TimeOfUseRates` (`engie.py:499`) | Flextime triplet from columns 4/5/6, `weekend_rule="weekend_no_peak"`. |

Empower Flextime (`kind="tou"`) is the SMR3-only TOU billing mode of the Empower
Variable product, sharing the same PDF (`engie.py:194`). It requires the 7-price
Empower row; the parser raises if it is asked for Flextime on a card that does
not carry the triplet (a 4-price row, `engie.py:519`). Its weekend rule is
`weekend_no_peak` (peak never applies at weekends; transition/offpeak split is
kept), distinct from Luminus SmartFlex's `weekend_offpeak`, per CWaPE Engie
publication (`engie.py:197`, framework schedule `base.py:202`).

## DSO overlay coverage

### Flanders (`_extract_flanders_dsos`, `engie.py:715`)

Reads the `Compteur digital` Fluvius table only (the analog table is ignored,
`engie.py:723`). Fluvius distribution rates already include Elia transport
(`incluant déjà les coûts de transport`), so the parser sets `transport=0` and
rolls the full c€/kWh into `distribution_single` (`engie.py:742`, test
`test_dynamic_flanders_dso_includes_transport_in_distribution`
`tests/test_engie.py:187`). The eight Fluvius sub-areas are mapped through
`_FLANDERS_LABELS` (`engie.py:703`); note the card labels do not match the
canonical keys one-to-one:

| Card label | Canonical key |
| --- | --- |
| FLUVIUS ANTWERPEN | `fluvius_antwerpen` |
| FLUVIUS HALLE-VILVOORDE | `fluvius_halle_vilvoorde` |
| FLUVIUS IMEWO | `fluvius_imewo` |
| FLUVIUS KEMPEN | `fluvius_iveka` |
| FLUVIUS LIMBURG | `fluvius_limburg` |
| FLUVIUS MIDDEN-VLAANDEREN | `fluvius_intergem` |
| FLUVIUS WEST | `fluvius_west` |
| FLUVIUS ZENNE-DIJLE | `fluvius_zenne_dijle` |

Each row yields capacity (`capacity_eur_per_kw_year`), distribution single,
distribution exclusive-night (a lower dedicated night-meter rate), and the
quarter-hourly data-management fee (`engie.py:738`).

### Wallonia (`_extract_wallonia_dsos`, `engie.py:764`)

Five DSOs mapped via `_WALLONIA_LABELS` (`engie.py:755`): AIEG, AIESH,
`ORES (Brab. Wal.)` -> `ores`, `REGIE DE WAVRE` -> `rew`, `TECTEO - RESA` ->
`resa`. Rows carry 10 numbers on static contracts (with a prosumer column) and 9
on dynamic contracts (the prosumer column is replaced by nothing, since dynamic
SMR3 contracts have no compensation regime; `engie.py:793`, test
`test_dynamic_wallonia_dso_has_separate_transport_no_prosumer`
`tests/test_engie.py:202`). The last column is always the c€/kWh transport rate,
so it is billed separately (unlike Flanders).

Two gotchas guard this parser:

- Horizontal-whitespace-only matching (`[^\S\n]`, `engie.py:782`): a fix for a
  bug where a greedy match spanned a blank line and pulled the next row's or a
  footnote's leading number into this row, shifting every column right and
  billing transport at a stray value.
- ORES sub-area divergence guard (`engie.py:819`): the card lists ~7 ORES
  sub-areas that are numerically identical today, and only the `Brab. Wal.` row
  is mapped into the single `ores` key. The parser asserts every other ORES
  sub-area row equals the first and raises `ORES sub-area tariffs diverged`
  otherwise, so a future tariff split is caught rather than silently billing
  every ORES customer the Brab. Wal. rate. Test
  `test_wallonia_ores_subarea_divergence_is_fatal` (`tests/test_engie.py:142`).

### Brussels (`_extract_brussels_dsos`, `engie.py:834`)

Reads the single Sibelga row (`engie.py:841`). Brussels has no separate capacity
charge (capacity is Flanders-only), so the parser folds two flat annual euros,
the metering fee (`Activité de mesure`, column 5) and the Sibelga <=13kVA power
term (column 6), into `data_management_per_year` (`engie.py:861`, test
`test_dynamic_brussels_extracts_sibelga` `tests/test_engie.py:214`, illustrative
`14.73 + 50.07`). It also parses the Brugel OSP annual-fee table via the shared
`parse_brussels_osp` (`_pdf.py:553`) into `brussels_osp_by_tier`.

## Tax overlay

`parse_snapshot` builds one `TaxOverlay` (`engie.py:386`):

| Field | Source helper | Region gating |
| --- | --- | --- |
| `federal_excise` | `_extract_federal_excise` | Any PDF (federal). |
| `energy_contribution` | `_extract_energy_contribution` | Any PDF (federal). |
| `flanders_renewables` | `_extract_consumption_renewables` | Flanders text only (`engie.py:367`). |
| `wallonia_renewables` | `_extract_consumption_renewables` | Wallonia text only (`engie.py:373`). |
| `brussels_renewables` | `_extract_consumption_renewables` | Brussels text only (`engie.py:377`). |
| `region_connection_fee` | `_extract_connection_fee` | Wallonia only (`engie.py:376`). |
| `energy_fund_eur_per_month` | `_extract_energy_fund` | Flanders only (`engie.py:370`). |
| `vat_rate` | hardcoded `0.0` (`engie.py:394`) | Card is 6% VAT inclusive. |

`vat_rate=0.0` is the "prices are already VAT-incl" convention (`base.py:471`).
Engie's cards print 6% VAT inclusive (`engie.py:42`), so the extracted energy /
network / tax numbers are post-VAT and must not be re-scaled; the one exception is
the dynamic formula, which is printed pre-VAT and is scaled locally in
`_extract_energy` (see above). Test `test_dynamic_extracts_taxes_for_every_region`
(`tests/test_engie.py:226`) asserts `vat_rate == 0.0`.

The Flemish energy fund has two sub-cases (`_extract_energy_fund`,
`engie.py:671`): `Résidentiel (avec domicile)` (0 for most products) and
`Résidentiel (sans domicile)` (a positive fee). The Empty House product is for
vacant homes with no registered domicile, so `parse_snapshot` passes
`sans_domicile=True` for it (`engie.py:371`) and it bills the `sans domicile`
rate (illustrative `10,07/mo`, tests `test_empty_house_is_mono_only`
`tests/test_engie.py:280` and `test_energy_fund_selects_domicile_case`
`tests/test_engie.py:300`). A miss legitimately means "no fund on this card"
outside Flanders, so this helper keeps a silent `0.0` default (`engie.py:685`),
unlike the mandatory levies which raise.

## Injection

`_extract_injection` (`engie.py:546`) produces all three shapes of the injection
taxonomy (see [../pricing-model.md](../pricing-model.md)) depending on the
contract:

- Monthly-indicative-only (`current`): the first `Injection(3)` row's first
  column (`engie.py:551`, divided by 100). The second `Injection(3)` row is the
  annual estimate and is ignored. Fixed and non-Flextime variable contracts carry
  only this (test `test_easy_fixed_extracts_bihourly_rates`
  `tests/test_engie.py:242`: `current` set, `factor`/`base` None;
  `test_empower_variable_injection_is_single_rate` `tests/test_engie.py:109`).
- Per-slot TOU triplet (`peak`/`transition`/`offpeak`): only for `kind == "tou"`
  when the row has >=6 numbers (`engie.py:562`), reading columns 4/5/6. Engie
  Empower Flextime's feed-in tariff varies by slot, so the pricing engine selects
  the slot with the same `tou_slot()` rule as consumption (`base.py:296`). Issue
  #34; test `test_empower_flextime_injection_varies_by_slot`
  (`tests/test_engie.py:93`).
- Hourly `factor * spot + base`: only for `kind == "dynamic"` when the card
  carries a second BELPEX formula (`engie.py:579`). The second `_FORMULA_RE`
  match is the injection formula. Residential injection is VAT-exempt
  (`base.py:271`), so it is not VAT-scaled: `factor = factor_pdf * 10` and
  `base = base_pdf_cents / 100` (`engie.py:589`, no `vat` multiplier, contrast the
  consumption path). Test `test_dynamic_extracts_injection_formula`
  (`tests/test_engie.py:166`): illustrative `-1,3135 + (0,1000 x eSpot_15)` gives
  `factor == 1.0`, `base == -0.013135`, and the indicative `current == 0.09136`
  from the row.

The dynamic injection formula path is deliberately gated on `kind == "dynamic"`
(`engie.py:579`): a future indexed or variable card that happens to print a price
formula must not flip the injection taxonomy into a spot factor/base shape. If
none of `current`, `factor`, or `peak` is set, injection is `None`
(`engie.py:592`).

No supplier-side PV / prosumer forfait: Engie does not populate
`supplier_prosumer_eur_per_kva_year` (the field stays at its `None` default,
`base.py:498`). The Wallonia DSO overlay carries the DSO-side
`prosumer_eur_per_kva_year` on static contracts only.

## Quirks and historical bugs (land mines)

- 6% VAT-inclusive convention with a pre-VAT dynamic formula. Everything on the
  card is VAT-incl (`vat_rate=0.0`), except the dynamic formula which is pre-VAT
  and scaled by the parsed multiplier (`engie.py:42`).
- Missing-VAT-phrase fail-loud. `_vat_multiplier` (`engie.py:409`) requires the
  `<N>% de tva comprise` phrase and raises `could not parse Engie dynamic VAT
  multiplier` if absent, rather than falling back to the shared helper's 6%
  default and masking a rate/wording change. Test
  `test_dynamic_missing_vat_phrase_is_fatal` (`tests/test_engie.py:154`).
- Yearly-fee two-layout fallback (standard `Type d'usage` vs Empower
  `Prix mensuels`), `engie.py:432`.
- One PDF, three billing modes. Empower Variable and Empower Flextime share the
  same 7-column card; the parser returns bi-horaire rates for `variable` and the
  Flextime triplet for `tou` from the same row (`engie.py:493`).
- `Prix mensuels` vs `Prix annuels estimés`. The variable card prints two
  Consommation rows; the extractor must take the monthly one (the first match),
  because the annual estimate over-bills by ~7% in a falling-price month (test
  `test_easy_variable_uses_monthly_not_annual_estimate` `tests/test_engie.py:312`).
- Comma-stripped energy contribution (`020417` for `0,20417`), `engie.py:649`.
- Apostrophe glyph drift: `d'usage` matched as `d[©']usage`, `Cotisation sur
  l['©]énergie` (`engie.py:445`, `engie.py:663`).
- Flanders distribution includes transport, so `transport=0` there
  (`engie.py:742`); Wallonia and Brussels bill transport as a separate column.
- Wallonia whitespace-only row matching to avoid a column-shift bug
  (`engie.py:777`).
- ORES sub-area divergence guard raises on a future tariff split (`engie.py:819`).
- Brussels folds metering + <=13kVA power term into the DSO fee (`engie.py:861`).
- Dynamic Wallonia rows have no prosumer column (9 numbers, not 10),
  `engie.py:793`.
- Tarif Social is intentionally excluded (`engie.py:225`).
- Partial-region resilience: `parse_snapshot` accepts a single-region map so a
  snapshot still builds if Engie's API is down for one region (test
  `test_parse_snapshot_with_partial_regions_still_works` `tests/test_engie.py:336`).

## Test fixtures

All fixtures live under `tests/fixtures/` and are April 2026 cards (test module
docstring `tests/test_engie.py:26`). The pinned numeric literals in the tests
re-index monthly, so they are frozen snapshots, not forever-facts.

| Fixture | Card variant exercised |
| --- | --- |
| `engie_dynamic_v.pdf` | Dynamic, Flanders (Fluvius DSO table, formula, taxes). |
| `engie_dynamic_w.pdf` | Dynamic, Wallonia (5 DSOs, ORES divergence guard, connection fee). |
| `engie_dynamic_b.pdf` | Dynamic, Brussels (Sibelga row, Brugel OSP, brussels renewables). |
| `engie_easy_fixed_v.pdf` | Easy Fixed, Flanders (bi-hourly + excl-night, indicative injection). |
| `engie_easy_indexed_v.pdf` | Easy Variable, Flanders (`Prix mensuels` vs annual estimate). |
| `engie_empower_flextime_w.pdf` | Empower Flextime, Wallonia (7-price TOU triplet, per-slot injection). |
| `engie_empower_variable_v.pdf` | Empower Variable, Flanders (7-price row, Flextime columns skipped). |
| `engie_empty_house_v.pdf` | Empty House, Flanders (mono-only, `sans domicile` energy fund). |

## When the card changes, look here

If Engie re-renders its cards and the extractor breaks, inspect these functions in
likely-to-break order:

1. `_extract_energy` (`engie.py:431`): the yearly-fee anchors, the
   `Consommation(2)` column-count branches (4 / 7 / 1), and the c€/kWh division.
   Most layout drift surfaces here first (`yearly fee row not found`,
   `unexpected price column count`, `could not parse ... consumption block`).
2. `_FORMULA_RE` and `_vat_multiplier` (`engie.py:425`, `engie.py:409`): dynamic
   formula punctuation (sign chars, `eSpot_15` token) and the mandatory VAT
   phrase.
3. `_extract_injection` (`engie.py:546`): the `Injection(3)` row column order and
   the second-formula gate for dynamic.
4. DSO row parsers (`_extract_flanders_dsos` `engie.py:715`,
   `_extract_wallonia_dsos` `engie.py:764`, `_extract_brussels_dsos`
   `engie.py:834`): the DSO-label-to-key maps, the digital-meter block boundary,
   the Wallonia 9/10-number split and ORES guard, and the Sibelga column order.
5. The tax helpers (`_extract_federal_excise`, `_extract_energy_contribution`,
   `_extract_consumption_renewables`, `_extract_energy_fund`,
   `_extract_connection_fee`): if a levy anchor phrase is reworded, the mandatory
   ones raise and the optional energy fund silently defaults.
6. The slug builder / URL (`_slug` `engie.py:235`, `_document_url`
   `engie.py:240`): if Engie changes its `DOC_CODE` scheme or API path, `fetch()`
   404s before parsing ever runs; `discover()` / `_URL_TOKEN_TO_FAMILY`
   (`engie.py:253`) will flag a new product family.

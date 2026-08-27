# Provider: octaplus

This document describes the OCTA+ tariff-card extractor
(`providers/octaplus.py`), the source of truth for turning an OCTA+ residential
electricity card into a `SupplierSnapshot`. It is written for a maintainer who
must repair the extractor after OCTA+ changes its PDF layout. It is grounded in
`providers/octaplus.py` and its fixture-driven test `tests/test_octaplus.py`
(the test pins the expected parse output against real April 2026 cards, so it is
the ground truth for what a correct parse produces).

Related reading:

- [../provider-framework.md](../provider-framework.md): the `SupplierExtractor`
  protocol, the dataclasses (`FixedRates`, `VariableRates`, `DynamicRates`,
  `ImpactRates`, `InjectionRates`, `DsoOverlay`, `TaxOverlay`,
  `SupplierSnapshot`), the probe / fetch / fetch_for_month contracts, and the
  shared PDF helpers in `_pdf.py`.
- [../pricing-model.md](../pricing-model.md): how the snapshot feeds
  `compute_breakdown`, how the injection shape is priced, and how the
  supplier-side PV forfait is billed on top of the DSO prosumer column.

## Overview

OCTA+ (extractor `id="octaplus"`, label `"OCTA+"`, `octaplus.py:672-686`) sells
residential electricity only in Wallonia and Flanders. Brussels is rejected: the
Brussels offers on OCTA+'s site are professional-only, so `_OCTAPLUS_REGIONS`
(`octaplus.py:670`) is `frozenset({REGION_FLANDERS, REGION_WALLONIA})` and
`EXTRACTOR.regions()` (the union over contracts, `base.py:560-565`) is those two
regions. `fetch` raises `ExtractorError("... not available in region ...")` for
any other region (`octaplus.py:182-183`, exercised by
`test_brussels_region_rejected`).

OCTA+ publishes one PDF per (product, region) at a stable, predictable URL
(`octaplus.py:28-31, 87, 136-137`):

```
https://files.octaplus.be/tariffs/E_OCTA_<SLUG>_RE_<VL|WL>_FR.pdf
```

`<SLUG>` is the product slug (see the contracts table), and `<VL|WL>` is the
region code (`_REGION_TO_CODE`, `octaplus.py:89-92`): `VL` for Flanders, `WL`
for Wallonia. There is no per-month archive: OCTA+ overwrites each card in place
under the same filename (`octaplus.py:145-149`). A human-facing listing page at
`_LISTING_URL` (`octaplus.py:135`) links every card and is used only by
`discover` for CI drift detection, not by `fetch`.

## Contracts

Eight products are declared in `_CONTRACTS` (`octaplus.py:106-128`). The
`_ContractDef` `slug` field is the URL token; `regions=None` means "every region
OCTA+ serves" (both), overridden only for the Impact variant.

| contract id | label | TariffKind | slug | regions | notes |
| --- | --- | --- | --- | --- | --- |
| `octaplus_fixed` | OCTA+ Fixed | `fixed` | `FIXED` | FL + WL | Standard fixed card. Bi-hourly + exclusive-night rates. |
| `octaplus_fixed_impact` | OCTA+ Fixed Impact | `tou_impact` | `FIXED` | WL only | Impact comptage (SMR3). Reuses the same `FIXED` PDF but prices the three CWaPE bands. Wallonia-only. |
| `octaplus_ecofixed` | OCTA+ Eco Fixed | `fixed` | `ECOFIXED` | FL + WL | Green fixed variant. |
| `octaplus_smartvariable` | OCTA+ Smart Variable | `variable` | `SMARTVARIABLE` | FL + WL | Monthly-indexed variable. |
| `octaplus_flux` | OCTA+ Flux | `variable` | `FLUX` | FL + WL | Variable variant. |
| `octaplus_ecoflux` | OCTA+ Eco Flux | `variable` | `ECOFLUX` | FL + WL | Green variable variant. |
| `octaplus_dynamic` | OCTA+ Dynamic | `dynamic` | `DYNAMIC` | FL + WL | `Epex 15'` spot formula, quarter-hourly. |
| `octaplus_ecodynamic` | OCTA+ Eco Dynamic | `dynamic` | `ECODYNAMIC` | FL + WL | Green dynamic variant. |

Notes:

- `octaplus_fixed_impact` is the only region-limited product. It sets
  `regions=frozenset({REGION_WALLONIA})` (`octaplus.py:110-116`) because
  Impact comptage is a Walloon CWaPE concept and the Flanders `FIXED` card
  carries no Impact block. `test_octaplus_is_registered` pins this: eight
  contract ids, and `impact.regions == frozenset({"wallonia"})`.
- Both dynamic products set `quarter_hourly=True` (`octaplus.py:348-349`),
  because OCTA+ indexes on the 15-minute EPEX spot (`Epex 15'`). Billing thus
  uses the native 15-minute grid, like Engie / Cociter / EBEM / Ecofix; without
  it the live price table would aggregate to hourly and the current / next-slot
  sensors and the cheapest-window service would lose the quarter-hour
  resolution. YTD billing stays hourly regardless (HA keeps only hourly
  long-term statistics). See `DynamicRates` docs in `base.py:183-210`.
- No product carries `spot_indexed_injection=True`. That flag means a PER-HOUR
  spot index, and no OCTA+ card has one on a static-energy product: the
  non-dynamic cards index their credit on the monthly Epex SPP, which the
  coordinator reaches through `_injection_needs_month_spot` instead (contrast
  Cociter Variable, whose card says the injection price varies every hour).

## Fetch strategy

### `fetch` (`octaplus.py:173-189`)

1. Validate `contract_id` against `_CONTRACTS_BY_ID`, raise
   `ExtractorError("unknown OCTA+ contract ...")` on miss
   (`test_unknown_contract_raises`).
2. Validate `region` is `VL`/`WL`, raise `not available in region` otherwise.
3. Build the URL via `_document_url` (`octaplus.py:137-138`).
4. Fetch aligned PDF text with `fetch_pdf_text_aligned(session, url,
   x_join_threshold=1.0)` and hand off to `parse_snapshot`.

The `x_join_threshold=1.0` is load-bearing. OCTA+'s tax block renders each glyph
as its own pdfplumber word with sub-point gaps (`"5 ,0 3 2 9 0 ,2 0 4 2"`); a
1.0pt merge threshold reassembles them into `"5,0329 0,2042"` while keeping real
inter-word spacing intact (`octaplus.py:185-188`,
`extract_pdf_text_aligned` at `_pdf.py:335-384`, exercised by
`test_federal_taxes_use_first_tier`). The aligned extractor also exists because
pdfplumber's default text extractor returns OCTA+'s DSO block in column-major
order (one number per line); bucketing words by y coordinate reassembles each
visual row into a single line like `AIEG 10,87 12,05 ...`
(`octaplus.py:35-39`).

### `probe` (`octaplus.py:140-153`)

The freshness key is `head_freshness_key(session, url)` (`_pdf.py:347-384`),
which HEADs the per-(contract, region) PDF and returns its `Last-Modified` (or
`ETag`) header. This works because OCTA+ overwrites cards in place under stable
filenames, so the file's modification time is the correct freshness signal. The
probe returns `None` (falling the coordinator back to its time-based TTL) when
the contract or region is unknown, or when the HEAD fails or carries none of the
preferred headers.

### `fetch_for_month`

OCTA+ declares no `fetch_for_month` (the `SupplierExtractor` is built with only
`fetch` and `probe`, `octaplus.py:794-809`). There is no accessible archive:
cards are overwrite-in-place, so past months fall back to the current snapshot
as a proxy. This is the documented behaviour for overwrite-in-place suppliers in
`base.py:519-524`.

### `discover` (`octaplus.py:156-167`)

CI-only. It GETs `_LISTING_URL` and regex-scrapes every
`E_OCTA_<SLUG>_RE_(VL|WL)_FR.pdf` link, returning the set of slugs.
`live_check` diffs this against `{c.slug for c in _CONTRACTS}` to catch OCTA+
adding, renaming, or retiring a product. It swallows `ExtractorError` and
returns an empty set on fetch failure.

## Parsing

`parse_snapshot` (`octaplus.py:191-233`) is a pure function (no I/O) exposed for
unit tests. It dispatches by `contract.kind` and by region. Fields pulled:

| snapshot field | source function | notes |
| --- | --- | --- |
| `energy` | `_extract_energy` (`:321`) | shape depends on kind (see below) |
| `injection` | `_extract_injection` (`:439`) | flat current on non-dynamic, factor/base on dynamic |
| `publication_label` | `_extract_publication_month` (`:407`) | MM/YYYY |
| `taxes.federal_excise`, `taxes.energy_contribution` | `_extract_taxes` (`:464`) | first federal tier (0-3.000 kWh) |
| `taxes.region_connection_fee` | `_extract_taxes` | Wallonia only |
| `taxes.flanders_renewables` | `_extract_flanders_renewables` (`:516`) | Flanders only, green + cogen |
| `taxes.wallonia_renewables` | `_extract_wallonia_renewables` (`:501`) | Wallonia only |
| `dsos` | `_extract_flanders_dsos` (`:609`) or `_extract_wallonia_dsos` (`:557`) | region-branched |
| `valid_until` | `parse_valid_until` (`_pdf.py:947`) | shared helper |
| `supplier_prosumer_eur_per_kva_year` | `_extract_supplier_prosumer` (`:239`) | PV forfait, annualised |

### Energy block (`_extract_energy`, `octaplus.py:318-386`)

`_extract_yearly_fee` (`:267-279`) always runs first and matches `Redevance
fixe (€/an) <value>` (illustrative ~65 EUR/year per the comment and
`test_fixed_wallonia_extracts_meter_rates`). A miss raises rather than defaulting
to 0, so a layout drift surfaces instead of silently dropping the annual fee.

By kind:

- **`dynamic`**: parses the prose formula `Epex 15' * <factor> <sign> <base>`
  (`_EPEX_FORMULA`, `:287-289`). The consumption formula is picked by
  `_dynamic_consumption_formula` (`:304-324`), which first locates the injection
  formula (the one after the `Le prix de votre injection` lead-in) and skips it,
  so reordering the two paragraphs cannot bind the injection formula as the
  consumption rate (`test_dynamic_consumption_formula_skips_injection_on_reorder`).
  The card formula is HTVA and in EUR/MWh, so it is converted to the model's
  TVAC EUR/kWh: `factor = factor_pdf * vat`, `base = base_eur_mwh / 1000 * vat`
  (`:336-342`). VAT comes from `_vat_multiplier` reading `Tarifs N% TVAC`
  (`:282-284`). `test_dynamic_parses_smr3_formula` pins illustrative
  `factor == 1.14798` and `base == 0.0044202` for `Epex 15' * 1,083 + 4,17` at
  6% VAT.
- **`tou_impact`**: reads three CWaPE-band supplier rates `Impact Pic`,
  `Impact Medium`, `Impact Eco` (c€/kWh) off the Fixed card and returns
  `ImpactRates` (`:352-361`). Missing any band raises. Illustrative expected
  values in `test_fixed_impact_extracts_three_cwape_bands`: eco 0.1284, medium
  0.1683, pic 0.1972.
- **`fixed` / `variable`**: reads the aligned meter table via `_meter_value`
  (`:392-396`), which matches `<label> <value>` and divides by 100 (c€ to EUR):
  `Compteur monohoraire` (single/current), `Heures pleines` (peak), `Heures
  creuses` (offpeak), `Compteur exclusif nuit` (exclusive_night). `mono` is
  mandatory; `peak` and `offpeak` are mandatory too (OCTA+ always prints the
  bi-hourly table, so a miss is a drift, not a mono-only card, and raises
  `bi-hourly rates`; `test_missing_bihourly_rates_raises`). `exclusive_night`
  stays nullable (separate optional circuit). `fixed` returns `FixedRates`,
  `variable` returns `VariableRates`.

### Publication month (`_extract_publication_month`, `octaplus.py:469-490`)

Two layouts are handled. Pre-2026 cards print `Clients résidentiels en <region>
- MM/YYYY - Tarifs N% TVAC`; the regex anchors on that prose so a footer
`-MM/YYYY-` cannot shadow the title date. The 2026 redesign dropped that line
and moved the date to a `FICHE TARIFAIRE <MOIS> <YYYY>` banner with the French
month spelled out and accented; the fallback maps the folded month name through
`_FRENCH_MONTHS` (`:402-404`). `test_publication_month_reads_fiche_tarifaire_banner`
exercises both, including accented `FÉVRIER` and `AOÛT`.

### Taxes (`_extract_taxes`, `octaplus.py:570-604`)

OCTA+ prints four federal-tier rows on page 2; the residential tier is the first
(`0 & 3.000 kWh`). The regex `0\s*&\s*3\.000\s*kWh\s+<a>\s+<b>` anchors on the
kWh range rather than the leading `Consommation` word, because that word can be
mangled on Flanders cards where the federal column shares its row bucket with the
Fonds Energie sidebar (e.g. `CCCConsommaaaation`). Group 1 is `federal_excise`,
group 2 is `energy_contribution`, both divided by 100. A miss raises
(`test_missing_federal_tax_tier_raises`; note that test's fixture text uses a
non-matching separator to force the raise). Wallonia adds
`region_connection_fee` from `Redevance raccordement Wallonie <value>`
(`:490-497`). Illustrative pinned values: `federal_excise 0.050329`,
`energy_contribution 0.002042` (`test_federal_taxes_use_first_tier`),
`region_connection_fee 0.00075` (`test_taxes_split_correctly_per_region`).

The `TaxOverlay` sets `vat_rate=0.0` (`octaplus.py:228`): OCTA+ snapshots ship
VAT-incl (TVAC) numbers, so the pricing engine must not re-scale them. See the
`vat_rate` convention in `base.py:702-702`.

### Regional renewables

- **Wallonia** (`_extract_wallonia_renewables`, `:501-513`): anchors on `Région
  wallonne` and takes its first numeric neighbour, bounding the non-digit run to
  80 chars so a far-away digit cannot be grabbed. Mandatory; raises on a miss.
  Illustrative ~3.1 c€/kWh; pinned `0.03095`.
- **Flanders** (`_extract_flanders_renewables`, `:516-532`): sums two rows,
  `Coûts énergie verte` (green energy) and `Coûts cogénération` (WKK). Raises
  only if both are absent. Pinned illustrative `(1.166 + 0.430) / 100`
  (`test_taxes_split_correctly_per_region`).

Both raises are covered by `test_missing_regional_renewables_raises`.

### Injection (`_extract_injection`, `octaplus.py:501-555`)

Injection taxonomy: **month-indexed formula** on fixed/variable/Impact,
**hourly factor*spot+base** on dynamic. `current` is the second number on the
`Compteur monohoraire` line (the injection column next to the consumption rate),
divided by 100, pinned illustrative `0.0472`
(`test_fixed_wallonia_extracts_meter_rates`). On a non-dynamic card that number
sits under the card's own **Prix estimés** heading and is not the rate: *"Le prix
de votre injection est indexé mensuellement sur base du paramètre d'indexation de
la Epex SPP ... La valeur de la Epex du mois en cours ne sera connue qu'en fin de
mois"*. `_SPP_FORMULA_RE` parses the stated formula (EUR/MWh,
so `base = b_eur_mwh / 1000`) and the leg is marked `spp_indexed`, which resolves
it against the delivery month's own solar-weighted mean and keeps the month
coefficients off the hourly spot. The estimate stays as `current`, the fallback
while that mean is unpublished
(`test_fixed_injection_carries_the_monthly_spp_formula`,
`test_fixed_injection_is_never_priced_per_hour`,
`test_every_non_dynamic_kind_gets_the_spp_formula`).

The card states the formula three times, once per meter configuration
(`monohoraire`, `bihoraire heures pleines`, `bihoraire heures creuses`). They are
identical today and `InjectionRates` carries a single coefficient pair, so the
coefficients are surfaced only while all three agree; a card that splits them
falls back to the printed estimate rather than billing two meter types on a
third one's formula (`test_disagreeing_meter_formulas_keep_the_estimate`).

For `dynamic`, the injection
formula is found after the `_INJECTION_LEAD` prose and yields `factor` and `base`
that are NOT VAT-adjusted (injection is VAT-exempt, `base.py:269-289`);
`base = b_eur_mwh / 1000`. Pinned illustrative `factor 1.0`, `base -0.01389` for
`Epex 15' * 1 - 13,89 €/MWh` (`test_dynamic_extracts_injection_formula`). Returns
`None` only when both `current` and `factor` are absent.

`_INJECTION_LEAD` (`:298-301`) accepts either the pre-2026 lead-in `Le prix de
votre injection` or the 2026 rewording `les prix de l'électricité injectée sont
indexés`, with the curly apostrophe the card uses.
`test_dynamic_injection_survives_reworded_lead_in` guards this.

### Supplier PV forfait (`_extract_supplier_prosumer`, `octaplus.py:236-258`)

Fixed and variable cards print `+ <value> €/kVA par mois` ("Forfait panneaux
solaires", applicable only under the compensation regime). It is TVAC and must
NOT be VAT-scaled. The regex `([\d.,]+)\s*€/kVA\s+par\s+mois` deliberately omits
the `HTVA` the unrelated AMR fallback rate carries, so it picks the forfait and
never the AMR `1,50 €/kVA HTVA par mois`. The card value is per month, so it is
annualised (`* 12`) to match `supplier_prosumer_eur_per_kva_year` (the
coordinator divides by 12). The SMR3 dynamic product drops the compensation
regime and omits the line, so `kind == "dynamic"` returns `None`; on any other
kind a miss raises (layout drift). Pinned illustrative `4,77 * 12 = 57.24`
TVAC, and `None` on dynamic (`test_supplier_pv_forfait_extracted_and_absent_on_dynamic`).
This forfait is billed on top of the DSO prosumer column by `_compute_prosumer`,
exactly like the Cociter Variable and Mega forfaits (see
[../pricing-model.md](../pricing-model.md)).

## DSO overlay coverage

Region-branched in `parse_snapshot` (`octaplus.py:208-213`).

### Wallonia (`_extract_wallonia_dsos`, `octaplus.py:622-668`)

Five DSO keys via `_WALLONIA_LABELS` (`:541-554`): `AIEG` -> `DSO_AIEG`,
`AIESH` -> `DSO_AIESH`, `ORES\(` -> `DSO_ORES` (eight ORES sub-areas share one
tariff line, match the first), `RESA` -> `DSO_RESA` (bare token anchors both
`RESA` and the older `TECTEO - RESA`), `Régie de Wavre` -> `DSO_REW` (accent
class + optional spacing anchors both the old `REGIEDEWAVRE` and the spaced
`REGIE DE WAVRE`). Labels are matched case-insensitively because the 2026
template recased ALLCAPS to title case (`:538-540`).

Each Wallonia row carries 10 numbers: `mono | jour | nuit | PIC | MEDIUM | ECO |
excl_nuit | terme_fixe (€/an) | col_a | col_b`. The last two columns are the
prosumer forfait (€/kVA/an) and transport rate (c€/kWh), but the 2026 template
swapped their order. The parser disambiguates by magnitude:
`prosumer = max(col_a, col_b)`, `transport = min(col_a, col_b)`, because the
forfait (~80-100) always dwarfs the transport rate (~2-3 c€/kWh)
(`:583-590`). The three PIC/MEDIUM/ECO bands populate
`distribution_pic/medium/eco`, feeding the Impact product's DSO side. Pinned
illustrative for `aieg`: single 0.1087, peak 0.1205, offpeak 0.0667, transport
0.0275, data_management 19.49, prosumer 81.04
(`test_wallonia_dsos_extract_full_set`, and the 2026-template variant
`test_wallonia_dsos_new_2026_template`).

### Flanders (`_extract_flanders_dsos`, `octaplus.py:674-732`)

Eight Fluvius sub-areas via `_FLANDERS_LABELS` (`:627-636`). Note the label-to-
key mapping is not one-to-one by name: `Fluvius Kempen` -> `DSO_FLUVIUS_IVEKA`
and `Fluvius Midden-Vlaanderen` -> `DSO_FLUVIUS_INTERGEM`.

Flanders cards carry two rows per DSO. The digital-meter row has
`dist_normal | dist_excl_night | data_mgmt_qh (€/an) | data_mgmt_year (€/an) |
capacity (€/kW/yr) | - | -`; a second analog-meter row carries the prosumer rate
as its last column. The parser first collects prosumer rates from the analog row
(`:622-632`), then reads the digital row per DSO (`:634-667`). The digital regex
is anchored on the sub-area suffix (label with `Fluvius ` stripped) rather than
the full label, and tolerates multi-glyph cell separators (`-------- --------`),
because some cards (Dynamic Flanders) prepend header glyphs to one digital row
and strip the leading `F` from the rest (e.g. `luvius Halle-Vilvoorde`).

Gotcha: group 3 is the card's `quart-horaire` data-management column (~61 EUR),
deliberately NOT used. It contradicts the authoritative Fluvius SMR3
data-management fee (~18,56 EUR per the Luminus card footnote); billing the
dynamic at it would over-charge ~42 EUR/yr. The mensuel/annuel value (group 4,
~18,92 EUR) matches the standard databeheer the rest of the integration uses, so
it is used for all meter regimes pending an authoritative Fluvius quart-horaire
rate (`:649-657`). Flanders `transport` is set to 0.0 (`:662`). Pinned
illustrative for `fluvius_antwerpen`: transport 0.0, single 0.0535, capacity
52.37, prosumer 54.63 (`test_flanders_dsos_extract_full_set`).

## Quirks and historical bugs (land mines)

- **VAT convention**: snapshot prices are TVAC, so `vat_rate=0.0`
  (`octaplus.py:228`). The dynamic formula is the exception: it is HTVA on the
  card and is scaled by the parsed VAT multiplier before storage
  (`:335-342`).
- **Injection is VAT-exempt**: dynamic injection factor/base are stored
  un-scaled (`:458-459`); a regression that VAT-scaled them would mis-credit
  feed-in.
- **Fixed-card injection column index**: `current` is the *second* number on the
  `Compteur monohoraire` line; a column-index slip that grabbed the consumption
  rate would over-credit feed-in ~3.4x (`test_fixed_wallonia_extracts_meter_rates`).
- **The variable ENERGY column is an estimate too, and a worse one.** The card
  says so: *"Les prix de l'électricité consommée mentionnés en page 1 sont
  purement indicatifs et sont basés sur la valeur actuelle du paramètre
  « V-test » ... Cette méthode a pour but de refléter les prix moyens attendus
  pour les 12 mois à venir."* That is a forward forecast of a whole year, not
  last month's index, so it does not track the delivery month at all: measured
  against the contract over Jan–Aug 2026 the April card ran +9,4% on average
  and +37,2% in April alone, the August card +19,8% and +50,2%. `_extract_energy`
  parses the four per-meter `Epex RLP M` formulas and sets `month_indexed`, with
  the printed figure kept as the keyless fallback.

  **The residual, stated plainly:** `Epex RLP M` weights the day-ahead by the
  residual load profile, and we resolve it against the plain arithmetic month
  mean, which sits about 3–4% below because consumption leans into the
  expensive hours. Synergrid publishes the RLP profile only as `.xlsb`
  (`SLP-RLP-SPP/2026/RLP0N 2026 Electricity.xlsb`), a binary workbook no
  current dependency can read. A bounded 3–4% one-way error replaces a
  +9,4%-to-+19,8% one that swings 50 points month to month, so the trade is
  worth making — but it is a trade, not an exact answer. Note this is the
  OPPOSITE conclusion to Eneco's energy leg, where the printed figure is last
  month's REALISED index: there the error is a lag that reverts across a year,
  and swapping it for a systematic under-bill would make the annual figure
  worse.
- **That column is an ESTIMATE, not the rate**: it sits under `Prix estimés` and
  the card says the delivery month's Epex is only known at month-end. Billing it
  flat is a month-lag mis-credit, which is what `_SPP_FORMULA_RE` and
  `spp_indexed` exist to stop.
- **The trailing period after the last formula**: the third meter variant ends
  the sentence, so a `[\d.,]+` value class swallows the period and turns
  `13,39.` into a different number. The value is anchored as
  `\d+(?:[.,]\d+)?`.
- **Two card generations, two spellings of the same formula.** OCTA+ reissued
  every card in August 2026: `Epex SPP x 0,852 - 13,39` became
  `Epex SPP M * 0,8560 - 16,20`, and the three per-meter rows collapsed to one.
  `_SPP_FORMULA_RE` accepts both. The optional `M` must be followed by the
  operator, because the same prose names the parameter on its own first
  (*"sur base du paramètre « Epex SPP M » dont les dernières valeurs connues"*)
  and that sentence would otherwise bind as a formula. This is also why the
  fixtures now carry an August card beside the April ones: the first version
  of this parser was written against April cards only and matched nothing on
  anything live, while the suite stayed green
  (`test_august_redesign_formula_is_parsed`).
- **Consumption vs injection formula collision**: both dynamic formulas share the
  `Epex 15'` shape; `_dynamic_consumption_formula` must skip the injection one by
  offset, robust to paragraph reordering (`:304-324`).
- **Reworded 2026 injection lead-in and curly apostrophe**: `_INJECTION_LEAD`
  accepts both phrasings (`:298-301`).
- **Wallonia column swap (2026)**: prosumer and transport columns swapped order;
  disambiguated by magnitude, not position (`:583-590`).
- **Wallonia label recasing/renaming (2026)**: case-insensitive match,
  `TECTEO - RESA` -> `RESA`, `REGIEDEWAVRE`/`REGIE DE WAVRE` -> Régie de Wavre
  (`:538-554`, `test_dynamic_pdf_uses_spaced_dso_label`).
- **Flanders glyph corruption**: digital rows can lose the leading `F` and gain
  header glyphs; the suffix-anchored regex and multi-glyph separator tolerance
  work around it (`:618-621, 634-644`).
- **Flanders quart-horaire data-management trap**: use group 4 (~18,92), not the
  ~61 EUR group 3, or the dynamic over-charges ~42 EUR/yr (`:649-657`).
- **Tax glyph explosion**: page 2 renders each character as its own word;
  `x_join_threshold=1.0` reassembles values (`:185-188`,
  `test_federal_taxes_use_first_tier`).
- **Federal-tier anchor**: match on `0 & 3.000 kWh`, not the mangleable
  `Consommation` word (`:475-481`).
- **PV forfait `HTVA` trap**: the regex must exclude the AMR `HTVA` rate to avoid
  binding `1,50` instead of `4,77` (`:257-261`).
- **Fail-loud policy**: yearly fee, federal tier, regional renewables, and the
  bi-hourly rows all raise on a miss instead of defaulting to 0, so a layout
  drift surfaces (the coordinator then serves the cached snapshot).

## Test fixtures

Under `tests/fixtures/`, exercised by `tests/test_octaplus.py` (April 2026
cards):

| fixture | card variant |
| --- | --- |
| `octaplus_fixed_w.pdf` | OCTA+ Fixed, Wallonia (`WL`). Also parsed as the Impact comptage variant. |
| `octaplus_fixed_v.pdf` | OCTA+ Fixed, Flanders (`VL`). Flanders tax + DSO branch. |
| `octaplus_smartvariable_w.pdf` | OCTA+ Smart Variable, Wallonia. Variable-kind path. |
| `octaplus_fixed_w_aug.pdf` | OCTA+ Fixed, Wallonia, **August 2026 redesign**. `Epex SPP M * 0,8560 - 16,20` in place of April's three `Epex SPP x` rows. Kept as served, not re-rendered: ghostscript reorders the DSO and tax column headers. |
| `octaplus_dynamic_w.pdf` | OCTA+ Dynamic, Wallonia. `Epex 15'` consumption + injection formulas, spaced DSO labels. |

Fixture text is read through `extract_pdf_text_aligned(..., x_join_threshold=1.0)`
in the test helper `_text` (`test_octaplus.py:54-57`), matching the production
fetch path.

## When the card changes, look here

- URL / slug / region change: `_document_url` (`:136-137`), `_CONTRACTS`
  (`:106-128`), `_REGION_TO_CODE` (`:89-92`), and `discover` (`:156-167`) if the
  listing markup changes.
- Meter-rate rows renamed or reordered: `_meter_value` label patterns in
  `_extract_energy` (`:369-372`).
- Dynamic formula wording / unit change: `_EPEX_FORMULA` (`:287-289`),
  `_dynamic_consumption_formula` (`:304-324`), the VAT/unit conversion
  (`:335-342`), and `_INJECTION_LEAD` (`:298-301`).
- Impact bands relabelled: `_extract_energy` Impact branch (`:352-361`).
- Tax tier / connection fee moved: `_extract_taxes` (`:464-498`); if the glyph
  spacing changes, revisit `x_join_threshold` in `fetch` (`:188`).
- Regional renewables rows renamed: `_extract_wallonia_renewables` (`:501-513`)
  and `_extract_flanders_renewables` (`:516-532`).
- Wallonia DSO labels/columns change: `_WALLONIA_LABELS` (`:541-554`) and the
  10-number row regex + magnitude disambiguation in `_extract_wallonia_dsos`
  (`:564-590`).
- Flanders DSO rows change: `_FLANDERS_LABELS` (`:627-636`) and the two-row
  logic in `_extract_flanders_dsos` (`:622-667`), especially the group-index
  choice for data-management.
- Publication banner reworded: `_extract_publication_month` (`:407-428`) and
  `_FRENCH_MONTHS` (`:402-404`).
- PV forfait wording change: `_extract_supplier_prosumer` (`:239-261`).

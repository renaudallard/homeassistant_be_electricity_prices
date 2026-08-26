# Provider: totalenergies

This document describes the `totalenergies` supplier extractor
(`providers/totalenergies.py`), the code that turns TotalEnergies Belgium's
published residential tariff cards into a `SupplierSnapshot`. It is written for
a contributor who has to repair the extractor after TotalEnergies changes a
card layout. It is grounded in the module source and in `tests/test_totalenergies.py`,
which pins the expected parse output against real April 2026 fixtures and is the
ground truth for what the extractor must produce.

Related reading:

- [../provider-framework.md](../provider-framework.md) : the `SupplierExtractor`
  protocol, the `Contract` / `SupplierSnapshot` / `DsoOverlay` / `TaxOverlay` /
  `InjectionRates` dataclasses, the registry and the shared `_pdf` helpers.
- [../pricing-model.md](../pricing-model.md) : how `compute_breakdown` consumes
  the snapshot (energy formula, DSO overlay, taxes, injection credit).

## Overview

TotalEnergies is a full-service supplier that sells residential electricity in
all three Belgian regions: Flanders, Wallonia and Brussels. `EXTRACTOR.regions()`
(the union over every contract's `regions`, `providers/base.py:73`) therefore
resolves to all three. Only one product, Impact, is region limited (Wallonia
only, see the contracts table).

TotalEnergies publishes one PDF card per (product, region) at a stable,
predictable URL. The `/latest/` path segment auto rolls each month, so the
current card is always reachable without scraping a listing page
(`totalenergies.py:185-187`). The URL pattern (`_document_url`, `totalenergies.py:185`):

```
https://totalenergies.be/static/marketing-documents/b2c/tariff-card/latest/
    <SLUG>_ELECTRICITY_<REGION>_FR.pdf
```

where `<SLUG>` is the per contract file prefix (see table) and `<REGION>` is one
of `VL` / `WAL` / `BXL` (`_REGION_TO_CODE`, `totalenergies.py:100`). All cards
are fetched in the French (`_FR`) edition.

These PDFs contain rotated DSO and tax columns that pypdf cannot read (it emits
"Rotated text discovered. Output will be incomplete."). The extractor therefore
downloads with `fetch_pdf_text_layout` (pdfplumber, layout aware), unlike the
horizontal text only cards that most other providers parse with pypdf
(`totalenergies.py:38-42`, `totalenergies.py:256`).

## Contracts

Nine residential electricity products are registered (`_CONTRACTS`,
`totalenergies.py:122`). The `test_totalenergies_is_registered` test asserts the
count is exactly 9 (`tests/test_totalenergies.py:55`).

| Contract id | Label | Kind | Slug | Regions | Notes |
|---|---|---|---|---|---|
| `totalenergies_electricite_fixe` | TotalEnergies Electricité Fixe | fixed | `ELECTRICITE-FIXE` | V/W/B | Constant EUR/kWh, optionally bi-hourly |
| `totalenergies_electricite_variable` | TotalEnergies Electricité Variable | variable | `ELECTRICITE-VARIABLE` | V/W/B | Monthly-indexed (BELPEX_M_RLP) |
| `totalenergies_impact` | TotalEnergies Impact | variable | `IMPACT` | Wallonia only | CWaPE 3-band; flat supplier energy, band split is DSO-side |
| `totalenergies_mycomfort` | TotalEnergies myComfort | variable | `MYCOMFORT` | V/W/B | Monthly-indexed |
| `totalenergies_mycomfort_fixed` | TotalEnergies myComfort Fixe | fixed | `MYCOMFORT-FIXED` | V/W/B | Fixed variant of myComfort |
| `totalenergies_mydrive` | TotalEnergies myDrive | variable | `MYDRIVE` | V/W/B | Monthly-indexed (EV-oriented) |
| `totalenergies_mydynamic` | TotalEnergies myDynamic | dynamic | `MYDYNAMIC` | V/W/B | `factor * BELPEXH + base`, hourly billing |
| `totalenergies_myessential` | TotalEnergies myEssential | variable | `MYESSENTIAL` | V/W/B | Monthly-indexed |
| `totalenergies_myessential_fixed` | TotalEnergies myEssential Fixe | fixed | `MYESSENTIAL-FIXED` | V/W/B | Fixed variant of myEssential |

Notes on the kind mapping:

- Only three `TariffKind` values are used here: `fixed`, `variable`, `dynamic`.
  TotalEnergies does not register a `tou` or `tou_impact` product.
- **Impact is declared `variable`, not `tou_impact`, on purpose.** The supplier
  energy on an Impact card is flat (the PIC/MEDIUM/ECO columns are all equal);
  the three band split lives entirely on the DSO side (`DsoOverlay.distribution_pic`
  / `_medium` / `_eco`, applied under `dso_tariff_mode=impact`). See
  `test_impact_parses_as_flat_supplier_energy_with_impact_dso_bands`
  (`tests/test_totalenergies.py:194`).
- **myDynamic bills per clock hour, not per quarter hour.** `DynamicRates.quarter_hourly`
  defaults `False` (`providers/base.py:159`) and TotalEnergies never overrides it,
  so the integration aggregates the ENTSO-E 15 minute curve to hourly for this
  contract (same grid choice as Frank/Luminus/Mega/Eneco, `providers/base.py:140-154`).
- No contract sets `spot_indexed_injection` (it defaults `False`,
  `providers/base.py:77`); non-dynamic injection is monthly indicative, so no
  ENTSO-E key is needed for the injection side.
- No product is retired in the current registry.

The per contract `regions` override exists because TotalEnergies's listing page
advertises every product in V/W/B, but some only have a Wallonia PDF; the rest
return a "200 OK" HTML 404 page (`totalenergies.py:113-116`). `fetch` and `probe`
both reject a (contract, region) pair that is not in the contract's `regions`
(`totalenergies.py:251`, `totalenergies.py:208`).

## Fetch strategy

### Current card

`fetch` (`totalenergies.py:237`) validates the contract id and region, then
constructs the URL with `_document_url` and downloads via `fetch_pdf_text_layout`,
handing the extracted text to `parse_snapshot`. There is no listing scrape on the
hot path: the `/latest/` segment guarantees the URL always points at the current
month (`totalenergies.py:26-36`). `fetch_pdf_text_layout` treats an HTTP 200 that
returns `text/html` (a disguised 404) as a fetch failure, so a product that is
not actually published in a region raises rather than parsing an HTML error page
(`_pdf.py:337-344`).

### Probe (freshness key)

`probe` (`totalenergies.py:190`) issues a HEAD against the same per (contract,
region) URL and returns `head_freshness_key`'s first present header
(`Last-Modified`, then `ETag`; `_pdf.py:347`). Because TotalEnergies overwrites
each card in place under `/latest/`, `Last-Modified` is the correct freshness
signal, and the coordinator only re-runs `fetch` when it changes. `probe` returns
`None` for an unknown contract, an unknown region, or a region the contract does
not serve; the coordinator then falls back to its time based TTL.

### Historical fetch (archive)

There is **no** `fetch_for_month` on the extractor (`EXTRACTOR`,
`totalenergies.py:820`, only sets `fetch` and `probe`). TotalEnergies is an
overwrite-in-place supplier: the `/latest/` URL exposes only the current month
and no dated archive is reachable (`providers/base.py:519-524`). The yearly cost
backfill therefore bills every past month with the current snapshot as a proxy.

### Discovery (CI only)

`discover` (`totalenergies.py:211`) fetches the human `cartes-tarifaires` listing
page (`_LISTING_URL`, `totalenergies.py:180`) and regex-extracts every
`tariff-card/latest/<SLUG>_ELECTRICITY_(VL|WAL|BXL)_FR` slug, dropping the
regulated `TARIFF_SOCIAL` entry (not a residential-market product). This is used
by `live_check` to diff the live catalogue against `{c.slug for c in _CONTRACTS}`;
it is not on the runtime fetch path. On a listing fetch error it returns an empty
set rather than raising.

## Parsing

`parse_snapshot` (`totalenergies.py:255`) is the pure, test-exposed entry point.
It dispatches per region and per `TariffKind` and assembles the `SupplierSnapshot`.
All monetary values in the cards are printed in c€/kWh and divided by 100 to reach
EUR/kWh; annual fees (yearly fee, data-management, capacity, prosumer, power term)
stay in EUR.

Fields pulled and their helpers:

| Field | Helper | Source anchor |
|---|---|---|
| Energy rates | `_extract_energy` | `totalenergies.py:364` |
| Injection | `_extract_injection` | `totalenergies.py:533` |
| Publication label | `_extract_publication_month` | `totalenergies.py:463` |
| Federal excise (0-3000 kWh tier) | `_extract_federal_excise` | `totalenergies.py:583` |
| Federal energy contribution | `_extract_energy_contribution` + `_energy_contribution_from_table` | `totalenergies.py:613`, `:620` |
| Yearly fee + regional renewables | `_extract_fee_and_renewables` | `totalenergies.py:430` |
| Wallonia connection fee | `_extract_connection_fee` | `totalenergies.py:646` |
| Flanders energy fund | `_extract_energy_fund` | `totalenergies.py:655` |
| DSO overlay (Flanders) | `_extract_flanders_dsos` | `totalenergies.py:680` |
| DSO overlay (Wallonia) | `_extract_wallonia_dsos` | `totalenergies.py:724` |
| DSO overlay (Brussels) | `_extract_brussels_dsos` | `totalenergies.py:768` |
| Validity date | `parse_valid_until` (shared) | `_pdf.py:947` |

Notable parsing hurdles:

- **Two competing energy prices per card.** A variable card prints both the
  Vlaamse-Nutsregulator annual ESTIMATE (in the standard 4 column table) and the
  realized monthly indicative ("prix mensuels calcules sur base de la derniere
  valeur connue du BELPEX_M_RLP"). The billed price is the realized block, so the
  extractor prefers it and only falls back to the table estimate when the block is
  absent (`totalenergies.py:478-509`, `_realized_monthly_consumption`,
  `totalenergies.py:485`). The test pins realized (13,53 / 14,65 / 12,55 / 12,39)
  over estimate (15,62 / ...) values as illustrative
  (`tests/test_totalenergies.py:153-168`).
- **Per-contract table drift.** The `Consommation` row has 0 to 5 trailing
  asterisks and may or may not carry an intervening `Tarif annuel` / `Tarif mensuel`
  label; the four meter values (mono / jour / nuit / excl_nuit) are separated by
  `[ \t]+` and the row must end at the line break (`totalenergies.py:416-420`).
- **Split-line dynamic formula (Brussels).** Wallonia and Flanders print
  `0.1034 * BELPEXH + 1.75` on one line; Brussels splits it, printing the factor
  line then the bases after a `Formule tarifaire` header. `_resolve_consumption_formula`
  handles both (`totalenergies.py:333`).
- **Sign character variance.** Formula signs are parsed with `parse_sign` over the
  shared `SIGN_CHARS` class, which covers ASCII `+`/`-` plus several Unicode dashes
  that TotalEnergies flips between on re-renders (`_pdf.py:521-539`).
- **DSO name to canonical key mapping.** Card labels are mapped to `DSO_*`
  constants via `_FLANDERS_LABELS` (`totalenergies.py:677`) and `_WALLONIA_LABELS`
  (`totalenergies.py:722`). Note the non-obvious ones: `Fluvius Kempen` maps to
  `DSO_FLUVIUS_IVEKA`, `Fluvius Midden-Vlaanderen` to `DSO_FLUVIUS_INTERGEM`, and
  Wallonia uses the exact card strings `ORES (Namur - Namen)`, `REGIE DE WAVRE`
  (-> `DSO_REW`), `RESA SA`.
- **Wrapped tax headers.** The Brussels and Flanders cards wrap the
  "Cotisation sur l'énergie" header across two lines, so the only machine readable
  copy is a column in the DSO table; see the historical bug note below.

## Energy formula per kind

```
fixed    -> FixedRates(single, peak, offpeak, exclusive_night, yearly_fixed_fee)
variable -> VariableRates(current, peak, offpeak, exclusive_night, yearly_fixed_fee)
            current is the realized monthly indicative when present, else the
            V-test table estimate
dynamic  -> DynamicRates(factor, base, yearly_fixed_fee)   # quarter_hourly=False
            factor = factor_pdf * vat * 10.0
            base   = sign * base_cents * vat / 100.0
```

The dynamic scaling converts the card's HTVA c€/kWh formula (against BELPEX in
EUR/MWh) into a VAT-incl EUR/kWh formula against a EUR/kWh spot. The derivation
is in the source (`totalenergies.py:379-387`): factor gains `vat * 10`, base gains
`vat / 100`. The VAT multiplier is read from the card header pattern `TVA\s*(\d+)\s*%`
via `_vat_multiplier` (`totalenergies.py:360`), defaulting to 1.06 when absent
(`_pdf.py:411-438`). The illustrative test pins Wallonia myDynamic
`0.1034 * BELPEXH + 1.75` (HTVA, 6% VAT) to `factor == 1.09604`, `base == 0.01855`
(`tests/test_totalenergies.py:58-69`); Brussels resolves the same factor with
`base == 0.04081` from the split layout (`tests/test_totalenergies.py:72-86`).

The `yearly_fixed_fee` (~90 EUR/yr, illustrative) comes from
`_extract_fee_and_renewables` and is shared across all kinds
(`totalenergies.py:465`).

### DSO overlay coverage

| Region | Sub-areas mapped | Row width | Fields surfaced |
|---|---|---|---|
| Flanders | 8 Fluvius sub-areas (`_FLANDERS_LABELS`) | 9 numbers | `distribution_single` (digital, includes transport), `capacity_eur_per_kw_year`, `data_management_per_year` (digital meter col), `prosumer_eur_per_kva_year` |
| Wallonia | AIEG, AIESH, ORES (Namur), REW, RESA (`_WALLONIA_LABELS`) | 12 numbers | `distribution_single/peak/offpeak/exclusive_night`, Impact `pic/medium/eco`, `transport`, `data_management_per_year` (terme fixe), `prosumer_eur_per_kva_year` |
| Brussels | Sibelga | 7 numbers + power term | `distribution_single/peak/offpeak/exclusive_night`, `transport`, `data_management_per_year` (metering + power term), `brussels_osp_by_tier` |

Region specifics:

- **Flanders** distribution already includes transport, so `transport=0.0` and the
  c€/kWh lands in `distribution_single` (same convention as Engie/Luminus/Mega
  Flanders, `totalenergies.py:687-719`, `tests/test_totalenergies.py:279-292`).
  The Flanders row's 9th column is surfaced into `prosumer_eur_per_kva_year`
  (`totalenergies.py:711`); capacity is a Flanders only field.
- **Wallonia** rows carry 12 numbers; the extractor surfaces mono/jour/nuit/excl,
  the Impact PIC/MEDIUM/ECO triplet, terme fixe (as `data_management_per_year`),
  transport and prosumer. The two capacity columns (cols 10-11) are not surfaced
  (`totalenergies.py:731-772`, `tests/test_totalenergies.py:260-276`).
- **Brussels** Sibelga has no separate capacity charge, so the metering fee and the
  `<=13kVA` "Terme de puissance mise a disposition" power term are folded together
  into `data_management_per_year` (`totalenergies.py:796-814`). The OSP annual fee
  table is parsed by the shared `parse_brussels_osp` into `brussels_osp_by_tier`
  (`_pdf.py:553`). The test pins `data_management_per_year == 14.73 + 50.07`
  (illustrative, `tests/test_totalenergies.py:218-235`).

### Tax overlay

`TaxOverlay` is built in `parse_snapshot` (`totalenergies.py:255-312`):

- `federal_excise`: first excise tier (0-3000 kWh), mandatory, raises on a miss
  (`totalenergies.py:590`). Illustrative pinned value 0.0503 EUR/kWh across all
  three regions (`tests/test_totalenergies.py:312-314`).
- `energy_contribution`: federal levy. Read from the labelled "Cotisation sur
  l'énergie" line, or, when the header is wrapped, from the DSO table fallback
  (see historical bug). Both readers return `None` on a miss rather than 0.0, so
  a card that PRINTS a zero is taken at face value while a card that omits the
  row entirely still raises (`totalenergies.py:272-281`). That distinction
  matters since 2026-08-01: the levy fell to zero, and the old
  `if energy_contribution == 0.0: raise` would have taken every TotalEnergies
  contract offline the way it took Frank offline (issue #49).
  `test_zero_energy_contribution_is_accepted` (`tests/test_totalenergies.py:238`)
  and `test_missing_energy_contribution_is_fatal` (`:249`) pin both halves.
- Regional renewables land in exactly one of `flanders_renewables`,
  `wallonia_renewables`, `brussels_renewables` per region (all others 0), taken
  from the second number on the fee+renewables line (`_extract_renewables`,
  `totalenergies.py:671`). Illustrative: Flanders 0.0157 (green + cogen merged),
  Wallonia 0.032, Brussels 0.0285 (`tests/test_totalenergies.py:316-323`).
- `region_connection_fee`: Wallonia only ("Redevance de raccordement"), mandatory
  there, raises on a miss (`totalenergies.py:653`). Illustrative 0.0007 EUR/kWh.
- `energy_fund_eur_per_month`: Flanders only ("Résidence principale sans tarif
  social" line, `_extract_energy_fund`, `totalenergies.py:655`).
- `vat_rate` is set to `0.0`, meaning the snapshot's consumption prices are already
  VAT-incl and must not be rescaled by the pricing engine (`providers/base.py:471-474`).
  The dynamic path applies VAT during parsing (see above); the fixed/variable table
  and realized values are stored as printed.

### Injection

Two shapes, selected on `kind` in `_extract_injection` (`totalenergies.py:533`):

- **Dynamic contracts: hourly `factor * spot + base`.** The injection block always
  prints the formula on one clean line ("0.1 * BELPEXH -1.3 ..."); the regex anchors
  after the `Injection` header so the consumption formula above is never captured
  (`totalenergies.py:554-573`). `factor = f_pdf * 10.0`, `base = b_cents / 100.0`,
  with **no VAT scaling** because residential injection is VAT-exempt
  (`providers/base.py:271-273`). Illustrative Wallonia: `0.1 * BELPEXH - 1.3` ->
  `factor == 1.0`, `base == -0.013` (`tests/test_totalenergies.py:89-103`). A
  dynamic card whose injection block is missing the BELPEXH formula raises rather
  than silently pricing feed-in at the flat monthly rate every hour
  (`totalenergies.py:559-567`, `tests/test_totalenergies.py:116-122`).
- **Non-dynamic contracts: monthly-indicative-only.** The table injection value is
  the V-test annual ESTIMATE; the billed value is the realized monthly indicative
  ("prix mensuels de l'injection"), so `_realized_monthly_injection`
  (`totalenergies.py:519`) overrides `current` and `factor`/`base` stay `None`
  (`totalenergies.py:574-580`). Illustrative 0.0112 EUR/kWh for both a variable and
  a fixed card (`tests/test_totalenergies.py:174-191`).

This places TotalEnergies in two of the three injection taxonomy shapes: shape (b)
hourly factor*spot+base for myDynamic, shape (a) monthly-indicative-only for every
other product. Shape (c) spot-indexed-variable is not used; no contract sets
`spot_indexed_injection`.

There is **no supplier-side prosumer/PV forfait**: `supplier_prosumer_eur_per_kva_year`
is left `None` (`SupplierSnapshot` default, `providers/base.py:666`). The only
prosumer charge is the DSO tariff (`DsoOverlay.prosumer_eur_per_kva_year`), surfaced
for both the Flanders and Wallonia rows where the card publishes it.

## Quirks and historical bugs

The land mines a future maintainer must know, each traceable to a source comment:

- **Rotated columns need pdfplumber.** pypdf cannot read the rotated DSO/tax cells;
  the extractor uses `fetch_pdf_text_layout` for these cards while other providers
  stay on pypdf (`totalenergies.py:38-42`).
- **Realized monthly indicative vs annual estimate.** For variable/fixed cards the
  table row is the regulator's annual estimate; billing uses the realized monthly
  indicative block. Prefer the realized block on both the consumption and injection
  sides (`totalenergies.py:393-405`, `:574-580`).
- **Wrapped "Cotisation sur l'énergie" header (Brussels + Flanders).** These cards
  wrap the label across two lines, so the labelled `_extract_energy_contribution`
  regex misses. The fallback `_energy_contribution_from_table` reads the levy from a
  DSO table column: the 7th SIBELGA number in Brussels, the 8th of nine on any
  Flanders Fluvius row (it is federal and identical across rows). Without this the
  all-in price silently dropped the contribution (~0.20 c€/kWh);
  `parse_snapshot` raises when both attempts return `None`
  (`totalenergies.py:272-281`, `:620-650`; tests `:171`, `:257`).
- **3-column card must fail loud.** The old 4-value regex used `\s+` between groups,
  spanning the line break and grabbing the 90,00 yearly fee as the exclusive-night
  rate (0.90 EUR/kWh) with no error. The row now ends at the line break, so a card
  with too few columns misses and raises (`totalenergies.py:407-422`,
  `tests/test_totalenergies.py:140-150`).
- **Impact is flat supplier energy with DSO-side bands.** It used to fail to parse
  as a standard variable card. The PIC value under `Heures PIC/MEDIUM/ECO` is the
  single supplier rate; the band variation comes from the DSO Impact distribution
  (`totalenergies.py:512-515`, `tests/test_totalenergies.py:194-215`).
- **Distinct anchors for the two BELPEX formulas.** Both consumption and injection
  print `factor * BELPEXH`. Consumption always appears first (so the first match is
  consumption, `totalenergies.py:336-338`); injection is anchored after the
  `Injection` header so the consumption formula cannot be mistaken for it
  (`totalenergies.py:552-558`).
- **Same-line base regex guards against back-off.** The tail regex uses `(?=\s|$)`
  to stop `[\d.,]+` from backing off `0.1034` to `0.103`, and `(?!\s*\*\s*BELPEXH)`
  to avoid grabbing the next column's formula (`totalenergies.py:349-356`).
- **Sibelga power term is a separate line.** The `<=13kVA` "Terme de puissance mise
  a disposition" is not in the DSO row; it is folded into `data_management_per_year`
  and is mandatory (raises on a miss, `totalenergies.py:796-806`).
- **Mandatory levies fail loud.** Federal excise, Wallonia connection fee and the
  Sibelga power term all raise rather than defaulting to 0, so a layout drift
  surfaces as an extractor failure instead of an undercounted bill
  (`totalenergies.py:602`, `:658`, `:805`). The energy contribution raises only on
  a genuinely absent row (`:281`) — a printed zero is a valid rate since
  2026-08-01, not drift.
- **200-OK HTML 404s.** Some products only publish a Wallonia PDF; the others return
  a "200 OK" HTML 404, which `fetch_pdf_text_layout` rejects. The per-contract
  `regions` override (e.g. Impact = Wallonia only) keeps the extractor from ever
  requesting a non-existent card (`totalenergies.py:116-119`, `:256`).

## Test fixtures

The tests exercise six real April 2026 fixture PDFs under `tests/fixtures/`
(all read with `layout=True`, i.e. pdfplumber):

| Fixture | Card variant |
|---|---|
| `totalenergies_dynamic_w.pdf` | myDynamic, Wallonia (same-line formula, 12-col DSO rows, connection fee) |
| `totalenergies_dynamic_v.pdf` | myDynamic, Flanders (9-col Fluvius rows, capacity, transport folded into distribution) |
| `totalenergies_dynamic_b.pdf` | myDynamic, Brussels (split-line formula, Sibelga row + power term, OSP) |
| `totalenergies_impact_w.pdf` | Impact, Wallonia (flat supplier energy, CWaPE DSO bands) |
| `totalenergies_mycomfort_fixed_w.pdf` | myComfort Fixe, Wallonia (bi-hourly fixed rates) |
| `totalenergies_mycomfort_v.pdf` | myComfort, Flanders (realized monthly indicative vs annual estimate) |

## When the card changes, look here

Ordered by how likely a card change is to break them:

1. **URL pattern**: `_BASE_URL` / `_document_url` (`totalenergies.py:185`, `:188`)
   and `_REGION_TO_CODE`. If TotalEnergies renames the `/latest/` path, a product
   slug, or a `_FR` suffix, every fetch and probe 404s.
2. **Consumption table regex**: `_extract_energy` (`totalenergies.py:416-420`). New
   asterisk counts, a new intervening label, or a changed column count breaks fixed
   and variable parsing.
3. **Realized monthly block**: `_MONTHLY_BLOCK_RE` and `_realized_monthly_consumption`
   / `_realized_monthly_injection` (`totalenergies.py:512`, `:485`, `:519`). A
   reworded "prix mensuels ... BELPEX_M_RLP" heading or changed meter labels
   (`Compteur Simple`, `Heures Pleines/Creuses`, `Compteur Excl. Nuit`, `Heures PIC`)
   silently reverts the extractor to the annual estimate.
4. **Dynamic formula**: `_resolve_consumption_formula` and the injection regex
   (`totalenergies.py:333`, `:554`). A layout change to `factor * BELPEXH + base`,
   or a swap of `BELPEXH` for another spot token, breaks myDynamic. Re-check the
   split-line Brussels path too.
5. **Fee + renewables line**: `_extract_fee_and_renewables` (`totalenergies.py:430`).
   Both numbers are mandatory; a moved or reshaped `Tarif (mensuel|annuel)` anchor
   raises.
6. **Tax anchors**: `_extract_federal_excise` ("Consommation entre 0 et 3.000 kWh"),
   `_extract_energy_contribution` + `_energy_contribution_from_table`,
   `_extract_connection_fee`, `_extract_energy_fund` (`totalenergies.py:655`-`:668`).
   Watch especially for the wrapped-header fallback column indices if the DSO table
   width changes.
7. **DSO row parsers**: `_FLANDERS_LABELS` / `_extract_flanders_dsos` (9 cols),
   `_WALLONIA_LABELS` / `_extract_wallonia_dsos` (12 cols),
   `_extract_brussels_dsos` (7 cols + power term) (`totalenergies.py:768`-`:817`). A
   new DSO name, a renamed sub-area, or a changed column order needs the label map
   and the fixed group indices updated together.
8. **Publication label + validity**: `_extract_publication_month`
   (`totalenergies.py:470`) and the shared `parse_valid_until` (`_pdf.py:947`) drive
   the `publication_label` and `valid_until` diagnostics.
9. **Discovery (CI)**: `discover` (`totalenergies.py:211`). If the listing markup or
   the `tariff-card/latest/<SLUG>_ELECTRICITY_<REGION>_FR` link format changes,
   `live_check` will report a slug diff before the runtime fetch path breaks.

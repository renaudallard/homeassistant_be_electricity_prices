# Provider: ecofix

This document describes the Ecofix Gas & Power extractor
(`providers/ecofix.py`), the module that turns Ecofix's published residential
electricity tariff cards into a `SupplierSnapshot`. It is a reference for the
maintainer who has to repair the parser after Ecofix re-renders a card, adds or
retires a product, or Fluvius/CWaPE change a column. Everything below is grounded
in `providers/ecofix.py` and its test suite `tests/test_ecofix.py`, which pins
the expected parse output against real May 2026 fixtures and is the ground truth
for what the extractor must produce.

Related reading:

- [../provider-framework.md](../provider-framework.md): the `SupplierExtractor`
  protocol, the `Contract` / `SupplierSnapshot` / `DsoOverlay` / `InjectionRates`
  dataclasses, and the shared `_pdf` helpers used here.
- [../pricing-model.md](../pricing-model.md): how the snapshot's energy formula,
  DSO overlay, tax overlay and injection shape are combined into the all-in price.

## Overview

> [!WARNING]
> **Broken since the August 2026 card, and the September card repeats it: the
> PDFs are page images.** Every page of every product is now a single full-page
> image covering 99.9% of the sheet, in both the NL and FR editions. Measured on
> the August cards:
>
> | card | page 1 | pages 2-5 |
> | --- | --- | --- |
> | `EL_Ecofix_Motion_NL` | 116 chars, 1 image @ 99.9% | 13 chars each (the month) |
> | `EL_Ecofix_Motion_FR` | 108 chars, 1 image @ 99.9% | 9 chars each |
> | `EL_Ecofix_Flexy_NL` | 121 chars, 1 image @ 99.9% | 13 chars (178 on p4) |
>
> What survives as live text is the supplier's own side: the formulas, and a
> handful of unlabelled fee numbers. Motion keeps them on page 1, Flexy on
> page 4:
>
> ```
> Motion   (0,1000 x Belpex 15M) + 1,1020    inj (0,0884 x Belpex 15M) - 0,5000
> Flexy    (BELPEX-RLP-M * 0,1020) + 1,2000  inj (BELPEX-SPP-M * 0,0884) - 0,5000
> ```
>
> **September 2026 changed nothing.** All three NL cards were republished on 31
> August 2026 at 11:19 GMT and came back as page images again, so the shape has
> now survived a month boundary. Against the copies committed here when the
> supplier was added, `tests/fixtures/ecofix_*.pdf` (2 May 2026, `Producer:
> Canva`), the whole-document totals are:
>
> | card | 2 May 2026 | 31 August 2026 |
> | --- | ---: | ---: |
> | `EL_Ecofix_Flexy_NL` | 5 pages, 11 851 chars | 5 pages, 344 chars |
> | `EL_Ecofix_Motion_NL` | 5 pages, 11 406 chars | 5 pages, 174 chars |
> | `EL_Ecofix_Motion_Online_NL` | 4 pages, 8 400 chars | 4 pages, 158 chars |
>
> Same page counts, about 97% of the text gone, and `Producer` changed from
> Canva to pypdf with the creation dates dropped. That is what a
> rasterise-and-reassemble step added to a publishing pipeline looks like, which
> is why this reads as a pipeline change rather than a one-month accident.
>
> Ecofix has been contacted with these figures, asking them to export the cards
> with their text layer again. No change here is needed if they do: the
> unreadable signal is derived per fetch, not from a stored flag, so support
> resumes on the next refresh.
>
> Until then an existing entry keeps serving the last card it managed to parse,
> across restarts and across an integration upgrade. The schema gate would
> normally throw that card away so a newer parser could re-read it, which is how
> a parser fix reaches an existing user; here there is no next fetch to heal
> with, so `_replay_stale_snapshot` puts the rejected card back rather than
> leaving the entry with nothing. A brand-new entry has no card to replay, so it
> sets up with every sensor unavailable and the `extractor_unreadable_no_prices`
> Repairs card pointing at the Custom (expert) supplier.
>
> What is gone is the whole regulated side — the DSO network tables and the tax
> block — which is most of a Belgian all-in price, so `parse_snapshot` fails
> loud rather than assembling a partial card. There is no fallback: `current/`
> is overwrite-in-place and Ecofix publishes no dated archive (every archive URL
> pattern probed returns 404).
>
> **Do not add OCR.** Dense numeric tables printed with Belgian comma decimals
> are OCR's weakest case, and a misread digit mis-prices silently, which is the
> failure mode every extractor here is built to avoid; it would also mean an OCR
> engine as a runtime dependency for one supplier. **Do not cross-fill from a
> sibling card either**: DSO distribution and transport tariffs genuinely are
> regulated and identical per DSO, but the green-certificate quota cost is
> supplier-specific (EnergyVision prints 3,00 c€/kWh where DATS 24 prints 2,860
> for the same month), so the tax block still cannot be reconstructed — and it
> would break the invariant stated at the top of `providers/base.py`, that no
> EUR values live in Python source.
>
> Affected users are pointed at the **Expert: custom formula** supplier
> (`providers/custom.py`), which collects exactly the missing DSO and tax blocks
> and supports `quarter_hourly`, so Motion is reproduced faithfully. July's card
> parsed normally, so this is a regression in Ecofix's document generator and
> the real fix is upstream.

Ecofix is a Belgian residential supplier selling in **Flanders and Wallonia**
only. There are no Brussels rows on any current card, and the registry advertises
Flanders + Wallonia so config-flow never offers Ecofix to a Brussels household
where every fetch would fail (`ecofix.py:747`, asserted by
`test_ecofix_is_registered` at `tests/test_ecofix.py:65`). `EXTRACTOR.regions()`
therefore returns `{flanders, wallonia}`.

Prices are published as **stable-URL PDF cards, one PDF per product**, at:

```
https://portal.ecofixgp.be/docs/prices/current/EL_Ecofix_<SLUG>_NL.pdf
```

built by `_document_url` from `_BASE_URL` (`ecofix.py:99`, `ecofix.py:99`). A
single PDF carries both the Flanders and the Wallonia overlays; `fetch()` narrows
the parsed snapshot to the requested `region` (`ecofix.py:136`). The three
products share the same monthly DSO and tax overlay; only the energy formula and
the yearly fixed fee differ between them (`ecofix.py:38`, pinned by
`test_motion_publication_and_renewables_match_motion_online` at
`tests/test_ecofix.py:236`).

There is no listing endpoint. `/docs/prices/` is a 404 and the public `/tarieven`
page links only a subset of products, so the module hardcodes the three known
filenames and HEAD-probes them in `discover()` (`ecofix.py:171`).

## Contracts

Declared in `_CONTRACTS` (`ecofix.py:110`) and mapped into `Contract` objects in
the registry (`ecofix.py:755`). All three carry `regions = {flanders, wallonia}`
The dynamic pair leave `spot_indexed_injection` at its default `False`, collecting
the ENTSO-E key via their energy formula; **Flexy sets it**, because its injection
indexes on the monthly `BELPEX-SPP-M` and its variable energy leg fetches no spots.

| Contract id | Label | TariffKind | Slug (filename stem) | quarter_hourly | Product |
| --- | --- | --- | --- | --- | --- |
| `ecofix_motion` | Ecofix Motion | `dynamic` | `Motion` | `True` | 15-min Belpex-indexed, phone customer service, full yearly fee (illustrative 60,00 EUR in the fixture) |
| `ecofix_motion_online` | Ecofix Motion Online | `dynamic` | `Motion_Online` | `True` | 15-min Belpex-indexed, online-only, low yearly fee (illustrative 10,00 EUR in the fixture) |
| `ecofix_flexy` | Ecofix Flexy | `variable` | `Flexy` | n/a | Monthly RLP-weighted Belpex average, indexation `BELPEX-RLP-M` |

Notes:

- Both dynamic products set `quarter_hourly=True` (`ecofix.py:374`): the cards bill
  on the 15-minute Belpex spot ("Belpex 15M"), so the integration keeps the native
  quarter-hour slots rather than the hourly mean, like Engie and OCTA+ (see the
  `DynamicRates.quarter_hourly` docstring at `providers/base.py:140`). YTD billing
  is still hourly because HA only retains hourly long-term statistics.
- Motion vs Motion Online differ only in the energy formula and the yearly fixed
  fee; the yearly fee is the sole reason two dynamic products exist. Motion Online
  is the online-only cheaper-fee variant.
- No product is region-limited within Belgium's two selling regions and none is
  currently retired. A future retirement shows up as a 404 dropped by `discover()`
  (`ecofix.py:177`); a new product needs a code change to add to `_CONTRACTS`.

## Fetch strategy

### fetch()

`fetch(session, contract_id, region)` (`ecofix.py:136`):

1. Validates `contract_id` against `_CONTRACTS_BY_ID`, raising `ExtractorError` on
   an unknown id.
2. Builds the URL with `_document_url` and downloads the PDF as
   **layout-preserving text** via `fetch_pdf_text_layout` (`_pdf.py:411`). Layout
   mode is mandatory here: pdfplumber's row reconstruction keeps each DSO row on
   one line, whereas pypdf returns the Wallonia DSO block in column-major order
   that the row-anchored regexes cannot match (`ecofix.py:42`). `fetch_pdf_text_layout`
   also rejects CDN 404-pages disguised as `text/html` 200s.
3. Delegates to the pure parser `parse_snapshot(contract_id, text, region, url)`.

### probe()

`probe()` (`ecofix.py:154`) returns a freshness key via `head_freshness_key`
(`_pdf.py:347`), which HEADs the per-contract PDF and returns its `Last-Modified`
(preferred) or `ETag`. Ecofix overwrites the card in place under a stable filename,
so that header flips when a new month is published, and the coordinator re-runs
`fetch()` only when the key changes. `head_freshness_key` returns `None` on any
4xx/5xx, timeout, or when neither header is present; the coordinator then falls
back to its time-based TTL. The `region` argument is unused (the URL is
region-agnostic).

### fetch_for_month() / archive

There is **no** `fetch_for_month`: the registry leaves it unset (`ecofix.py:747`
constructs `SupplierExtractor` with only `fetch` and `probe`). Filenames are
overwrite-in-place and Ecofix publishes no public archive of past months
(`ecofix.py:45`), so the coordinator's proxy-forward fallback bills past
consumption windows at the current snapshot's rates. If Ecofix ever exposes a
dated archive, add an `ArchivedSnapshotFetcher` (see `providers/base.py:965`).

### discover()

`discover(session)` (`ecofix.py:171`) HEAD-probes the three known URLs and returns
the contract ids whose URL currently returns `< 400`. Because there is no listing
endpoint, this is how the live-check script detects a retired product (its URL
starts 404ing) versus the registry's declared ids. A brand-new product is invisible
to `discover()` until its filename is added to `_CONTRACTS`. Behaviour is pinned by
`test_discover_returns_all_three_contracts_when_each_url_200s` and
`test_discover_drops_retired_product_when_url_404s` (`tests/test_ecofix.py:407`).

## Parsing

`parse_snapshot` (`ecofix.py:183`) is the pure entry point; it fans out to a set
of narrowly-anchored helpers. The fields it pulls out:

| Field | Helper | Source |
| --- | --- | --- |
| Yearly fixed fee + Flanders renewables | `_extract_fee_and_flanders_renewables` | `ecofix.py:254` |
| Energy formula / rates | `_extract_energy` | `ecofix.py:336` |
| Injection | `_extract_injection` | `ecofix.py:409` |
| Publication label + `valid_until` | `_extract_publication` | `ecofix.py:498` |
| Federal excise + energy contribution | `_extract_federal_taxes` | `ecofix.py:519` |
| Wallonia connection fee | `_extract_wallonia_connection_fee` | `ecofix.py:535` |
| Wallonia renewables | `_extract_wallonia_renewables` | `ecofix.py:544` |
| Flanders DSO overlays | `_extract_flanders_dsos` | `ecofix.py:597` |
| Wallonia DSO overlays | `_extract_wallonia_dsos` | `ecofix.py:692` |

The overlay is region-selected: `parse_snapshot` computes the Wallonia connection
fee and Wallonia renewables only for `wallonia`, the Flanders renewables only for
`flanders`, and picks `_extract_flanders_dsos` vs `_extract_wallonia_dsos`
accordingly (`ecofix.py:206`). A `brussels` region yields an empty `dsos` dict and
zeroed regional levies, kept well-formed even though the registry filter should
never let a Brussels config reach here (`ecofix.py:220`).

Notable parsing hurdles:

- **Label order flips across cards.** The Vlaanderen block prints the yearly fee
  and the Flanders renewable in different relative orders on Motion vs Motion
  Online, and a third layout on Flexy. `_extract_fee_and_flanders_renewables`
  disambiguates by magnitude, not position: renewables on Belgian residential
  cards are `< 5` c/kWh and yearly fees are `>= 10` EUR/jaar, so the smaller of the
  two tokens is always the renewable and the larger is the fee (`ecofix.py:263`,
  `ecofix.py:322`).
- **Vlaanderen block slice.** `_flanders_energy_block` (`ecofix.py:240`) carves the
  text from the `Vlaanderen` heading to `Wallonië`; both the fee and the FL
  renewable live in that slice. Scoping the renewable regex to this block stops the
  later federal "Verbruik tussen 0 & 3.000 kWh" row from shadowing it.
- **Belpex 15M formula anchoring.** `_dynamic_formula_match` (`ecofix.py:318`)
  anchors each formula on its own label (`Afname` for consumption, `Injectie` for
  injection) instead of indexing into a document-order `findall`. The fill between
  the label and its `(factor x Belpex 15M) <sign> base` formula is tempered with a
  negative lookahead so an `Afname` match can never cross the `Injectie` label; a
  reworded or absent consumption formula produces a clean miss instead of binding
  the injection formula to consumption. This is pinned by
  `test_afname_anchor_does_not_reach_injection_formula` (`tests/test_ecofix.py:81`).
- **Unit conversion.** PDF formulas are in c/kWh ex-VAT against Belpex in EUR/MWh.
  The dynamic energy branch converts `factor` (unitless, x1000/100 = x10) and
  `base` (cents to EUR, /100), then applies the VAT multiplier read from the card
  banner (`ecofix.py:363`). Belgian residential VAT is currently 6% and the card
  prints "Prijzen inclusief X% BTW"; `vat_multiplier` reads X so a future VAT change
  needs no code edit (`ecofix.py:364`, helper at `_pdf.py:411`).
- **Sign handling.** The `<sign>` between factor and base is matched against
  `SIGN_CHARS` and resolved with `parse_sign`, which treats every hyphen/dash/minus
  variant as negative (`_pdf.py:528`). Supplier PDFs flip between these silently on
  re-renders.
- **DSO-name to canonical-key mapping.** Flanders labels map through
  `_FLANDERS_LABELS` (`ecofix.py:594`); note "Fluvius Kempen" maps to the
  integration's `fluvius_iveka` key and "Fluvius Midden-Vlaanderen" to
  `fluvius_intergem`. Wallonia labels map through `_WALLONIA_LABELS`
  (`ecofix.py:660`), where `WAVRE` maps to `rew` and the regex `TECTEO\s*-\s*RESA`
  maps to `resa`.

## Energy formula per TariffKind

### Dynamic (Motion, Motion Online)

`_extract_energy` with `kind == "dynamic"` (`ecofix.py:336`) reads the `Afname`
Belpex 15M formula and returns a `DynamicRates`:

```
factor = factor_pdf * vat * 10.0
base   = base_pdf_cents * vat / 100.0
yearly_fixed_fee = <parsed yearly fee>
quarter_hourly = True
```

Illustrative (from `test_motion_online_energy_formula`, `tests/test_ecofix.py:96`):
a card printing `(0.1010 x Belpex 15M) + 0,9` c/kWh ex-VAT at 6% VAT yields
`factor = 0.1010 * 1.06 * 10 = 1.0706` and `base = 0.9 * 1.06 / 100 = 0.00954`.
A missing `Afname` formula is fatal (`ExtractorError`).

### Variable (Flexy)

`_extract_energy` with `kind == "variable"` (`ecofix.py:336`) reads the indicative
`Maandprijs:` row, which carries four columns `(mono, peak, off-peak,
exclusive_night)` that hold the same rate for every meter type today; all four are
surfaced into a `VariableRates`. The `BELPEX-RLP-M` indexation expression is
surfaced as the `formula` diagnostic string only (no cross-check against the rates;
a miss just leaves `formula` None). Illustrative: `Maandprijs: 11,81 11,81 11,81
11,81` gives `current = peak = offpeak = exclusive_night = 0.1181`
(`test_flexy_is_variable_with_indicative_monthly_rate`, `tests/test_ecofix.py:256`).
A missing `Maandprijs` row is fatal.

## DSO overlay coverage

### Flanders

`_extract_flanders_dsos` (`ecofix.py:597`) parses the eight Fluvius sub-areas from
the `Vlaams gewest Digitale meter` table. Each digital-meter row holds five numbers:
capacity (EUR/kW/jaar), kWh-tarief total (c/kWh), kWh-tarief excl. nacht (c/kWh),
data-management per-kwartier (EUR/jaar), data-management monthly/yearly (EUR/jaar).

The two data-management columns matter: `kind` selects which column is billed.
Dynamic contracts meter quarter-hourly and read the per-kwartier column (group 4),
Flexy meters monthly and reads the monthly/yearly column (group 5). They are equal
on today's cards, so a single column had been masking the mismatch until Fluvius
diverges the two regimes (`ecofix.py:642`,
`test_flanders_data_management_column_follows_metering_regime`,
`tests/test_ecofix.py:184`).

A second `Vlaams gewest Analoge meter` table below carries the analog-meter prosumer
rate in its 5th column, attached as `prosumer_eur_per_kva_year` for analog-meter
holdouts only. It is attached even for digital-meter users; the integration filters
by meter type downstream (`ecofix.py:620`, `tests/test_ecofix.py:170`).

Sub-areas mapped: `fluvius_antwerpen`, `fluvius_halle_vilvoorde`, `fluvius_imewo`,
`fluvius_iveka`, `fluvius_limburg`, `fluvius_intergem`, `fluvius_west`,
`fluvius_zenne_dijle` (all eight, `tests/test_ecofix.py:157`).

### Wallonia

`_extract_wallonia_dsos` (`ecofix.py:692`) parses each Walloon DSO row, which carries
10 numbers in order: Enkelvoudig, Piek, Dal, PIC, MEDIUM, ECO, Excl. nacht,
Jaarlijkse meteropname (EUR/jaar), Prosumenten tarief (EUR/kWe/jaar), Transport
(c/kWh). `_build_wallonia_overlay` (`ecofix.py:737`) unpacks these into a
`DsoOverlay`, including the three CWaPE Tarif Impact bands (`distribution_pic`,
`distribution_medium`, `distribution_eco`) that Wallonia cards publish on every row.

Non-ORES DSOs mapped from `_WALLONIA_LABELS`: `aieg`, `aiesh`, `rew` (label `WAVRE`),
`resa` (label `TECTEO - RESA`). ORES is handled separately by `_extract_ores`
(`ecofix.py:718`): the card lists nine numerically-identical ORES sub-area rows,
which are collapsed to a single `ores` key. If a future card splits sub-areas
(different numbers per row), `_extract_ores` raises `ExtractorError` rather than
silently billing at the first sub-area's rate
(`test_ores_subarea_drift_is_rejected`, `tests/test_ecofix.py:347`).

## Tax overlay

`_extract_federal_taxes` (`ecofix.py:519`) reads the residential federal excise
from the 0-3.000 kWh band (`Verbruik tussen 0 & 3.000 kWh`) and the single-rate
`Energiebijdrage`; both missing rows are fatal. Regional levies are region-gated in
`parse_snapshot`:

| TaxOverlay field | Flanders | Wallonia |
| --- | --- | --- |
| `federal_excise` | parsed | parsed |
| `energy_contribution` | parsed | parsed |
| `flanders_renewables` | parsed (from Vlaanderen block) | 0.0 |
| `wallonia_renewables` | 0.0 | `_extract_wallonia_renewables` |
| `region_connection_fee` | 0.0 | `_extract_wallonia_connection_fee` |
| `vat_rate` | 0.0 | 0.0 |

`vat_rate = 0.0` does **not** mean tax-free; per the `TaxOverlay` convention
(`providers/base.py:471`) it means the snapshot's prices are already handled for
VAT. The dynamic energy formula applies the VAT multiplier inline, so the emitted
rates are VAT-incl and `vat_rate` is left at 0 so the pricing engine does not
re-scale. Illustrative Flanders values (`test_motion_online_taxes_flanders`,
`tests/test_ecofix.py:135`): federal_excise 0.0503288, energy_contribution
0.0020417, flanders_renewables 0.016.

`_extract_wallonia_connection_fee` (`ecofix.py:535`) and `_extract_wallonia_renewables`
(`ecofix.py:531`) are fatal on a miss because both are mandatory in Wallonia. The
renewables parser is defensive: pdfplumber can co-locate the bare
`Bijdrage groene energie` value with an unrelated left-column label, so it iterates
lines after the anchor, skips consumption/injection/formula rows via `skip_prefixes`,
stops at `stop_markers`, and returns the first remaining numeric token.

## Injection shape

Ecofix spans two of the three injection taxonomy shapes depending on TariffKind
(see [../pricing-model.md](../pricing-model.md) for the taxonomy):

- **Dynamic (Motion, Motion Online): hourly `factor*spot+base` (spot-indexed).**
  `_extract_injection` with `kind == "dynamic"` (`ecofix.py:409`) anchors on the
  `Injectie` label to read the injection Belpex 15M formula, emits `factor =
  factor_pdf * 10` and `base = base_pdf_cents / 100` (no VAT, since Belgian
  residential injection is VAT-exempt, `ecofix.py:418`), and surfaces the printed
  indicative rate (`Injectie <n>`) as `current` for consumers without a live spot.
  A missing formula is **fatal**: raising rather than returning None avoids
  silently zeroing the feed-in credit, and refusing to fall back to the indicative
  alone avoids freezing a spot-indexed injection at a flat rate
  (`test_dynamic_injection_missing_formula_is_fatal`, `tests/test_ecofix.py:175`).
  Illustrative (`test_motion_online_injection`, `tests/test_ecofix.py:112`):
  `(0.0884 x Belpex 15M) - 0.5000` c/kWh gives `factor = 0.884`, `base = -0.005`,
  `current = 0.0483`.
- **Variable (Flexy): month-indexed formula.** Flexy injection settles on
  `BELPEX-SPP-M`, the solar-weighted monthly index, and the card says which month:
  *"worden berekend op basis van de index die van toepassing is tijdens de periode
  waarvoor je wordt gefactureerd bij de afrekening van je reële verbruik en
  desgevallend injectie."* The printed `Maandprijs` is not that month's. Invert the
  Mei 2026 card's 4,32 c/kWh through its own coefficients and the index comes out
  at 54,52 EUR/MWh, which is **March's** — two months back. April's own index is
  worth 2,08 c/kWh, less than half what the card printed.

  `_extract_injection` surfaces `factor = 0,884`, `base = -0,005` (the card states
  c/kWh per EUR/MWh of index, so a x10 onto a EUR/kWh spot and a /100 base;
  VAT-exempt, so neither is grossed) and marks the leg `spp_indexed`. That flag is
  what makes emitting them safe: it routes the coefficients to the delivery month's
  own weighted mean and makes `_injection_is_spot_formula` return False, so they
  can never reach the hourly spot. The old comment refused to emit them at all for
  fear of exactly that, and was right to — the flag is the part that was missing,
  not the caution. The printed figure stays as `current`, the fallback for an entry
  with no ENTSO-E key, and a missing indicative is still fatal
  (`test_flexy_injection_carries_the_spp_formula`,
  `test_flexy_printed_figure_is_two_months_stale`,
  `test_flexy_injection_is_never_priced_per_hour`).

No supplier-side PV/prosumer forfait is emitted (`supplier_prosumer_eur_per_kva_year`
is left None). The only prosumer figure Ecofix carries is the DSO-side
`prosumer_eur_per_kva_year` on the `DsoOverlay` (Flanders analog-meter column and
the Wallonia row's Prosumenten tarief).

## Quirks and historical bugs

These are the land mines a future maintainer must know; each is a real comment or
test in the source.

- **Layout mode is mandatory.** pypdf returns the Wallonia DSO block column-major
  and breaks the row-anchored regexes; only `fetch_pdf_text_layout` (pdfplumber row
  reconstruction) parses correctly (`ecofix.py:42`).
- **Overwrite-in-place, no archive.** Filenames are stable and reused each month,
  so there is no `fetch_for_month` and past months fall back to the current snapshot
  as a proxy (`ecofix.py:45`).
- **Magnitude disambiguation of fee vs renewable.** The two Vlaanderen numbers flip
  order across cards; the parser keys off magnitude (`< 5` = renewable, `>= 10` =
  fee) not position (`ecofix.py:263`).
- **July 2026 Flexy reflow (regression fix).** The July 2026 Flexy card re-rendered
  the Vlaanderen renewable onto its own line above the `Verbruik` label
  (`1,60\nVerbruik`) instead of after it (`Verbruik 1,60`), which stopped the old
  same-line anchor and took the whole card offline. `_extract_fee_and_flanders_renewables`
  now accepts either order, scoped to the Vlaanderen block so the later federal
  `Verbruik tussen 0 & 3.000 kWh` row cannot shadow it (`ecofix.py:288`,
  `test_flexy_renewables_survives_number_before_verbruik_layout`,
  `tests/test_ecofix.py:299`).
- **Afname anchor must not reach the Injectie formula.** The tempered lookahead in
  `_dynamic_formula_match` is the guard; without it a reworded/absent consumption
  formula would bind the injection formula to consumption (`ecofix.py:327`,
  `tests/test_ecofix.py:81`).
- **Injection role-swap protection.** Both dynamic formulas are anchored on their
  own label rather than by document order, so a reordered card cannot swap
  consumption and injection (`ecofix.py:429`).
- **VAT read from banner, not hardcoded.** `vat_multiplier` reads the `inclusief X%
  BTW` banner so a VAT change needs no code edit; the default is 1.06 if the banner
  is absent (`ecofix.py:364`, `_pdf.py:411`).
- **Publication label scan skips a colliding version token.** The product name
  prints on the line above the month; `_extract_publication` scans the first 1000
  chars for a word+year token that is actually a Dutch month, so a future
  `... Versie 2026` header cannot shadow the real month line and drop validity
  (`ecofix.py:519`, `test_publication_scan_skips_colliding_version_token`,
  `tests/test_ecofix.py:269`). The card has no `geldig`/`valable` keyword, so the
  shared `parse_valid_until` helper would return None; the month name is parsed
  directly and `valid_until` is set to the last day of that month for the monthly
  rotation binary sensor.
- **Two data-management columns per Fluvius row.** Bill the column matching the
  metering regime (dynamic = per-kwartier group 4, Flexy = monthly group 5); they
  are equal today (`ecofix.py:642`).
- **ORES sub-area drift is fatal.** Nine identical ORES rows collapse to one key;
  any numeric divergence raises so a silent sub-area split cannot mis-bill
  (`ecofix.py:718`).
- **Mandatory Walloon fees are fatal on a miss.** The connection fee and green-energy
  contribution raise rather than zero out (`ecofix.py:544`, `ecofix.py:531`,
  `test_missing_wallonia_connection_fee_is_fatal`, `tests/test_ecofix.py:277`).
- **Injection is never VAT-scaled.** Residential injection is VAT-exempt; the
  dynamic injection factor/base omit the VAT multiplier that the consumption side
  applies (`ecofix.py:418`).

## Test fixtures

The tests read three PDF fixtures under `tests/fixtures/` via `fixture_text(name,
layout=True)` (mirroring `fetch_pdf_text_layout`):

| Fixture | Constant | Card variant |
| --- | --- | --- |
| `ecofix_motion.pdf` | `_MOTION` | Motion dynamic card (full yearly fee, Ecofix Digi) |
| `ecofix_motion_online.pdf` | `_MOTION_ONLINE` | Motion Online dynamic card (low yearly fee) |
| `ecofix_flexy.pdf` | `_FLEXY` | Flexy variable card (RLP-M monthly indicative) |

All three are the May 2026 edition (`tests/test_ecofix.py:26`). Several tests
synthesize edge-case text inline rather than from a fixture (the July 2026 reflow,
the diverging data-management columns, the ORES drift, the version-token collision),
so those layouts are exercised without needing a separate fixture file.

## When the card changes, look here

Ranked by how likely a card re-render is to break them:

1. `_extract_fee_and_flanders_renewables` (`ecofix.py:254`) and `_flanders_energy_block`
   (`ecofix.py:249`): the Vlaanderen block is the most re-flowed part of the card
   (the July 2026 regression lived here). Watch the fee/renewable order and the
   `Verbruik` / `meter Piekuren` anchors.
2. `_dynamic_formula_match` (`ecofix.py:318`) and the `_extract_energy` /
   `_extract_injection` dynamic branches: any change to the `Afname` / `Injectie`
   labels, the `Belpex 15M` wording, or the sign glyph.
3. `_extract_flanders_dsos` (`ecofix.py:597`) and `_FLANDERS_LABELS` (`ecofix.py:594`):
   a Fluvius rename (labels are matched literally) or a change in the number of
   columns per row, especially if Fluvius diverges the two data-management regimes.
4. `_extract_wallonia_dsos` / `_extract_ores` / `_ORES_PATTERN` (`ecofix.py:669`):
   a Walloon column reorder or an ORES sub-area split (the latter raises by design).
5. `_extract_publication` (`ecofix.py:498`): a new header token near the month, or a
   language switch away from Dutch month names in `_DUTCH_MONTHS` (`ecofix.py:125`).
6. `discover()` / `_document_url` (`ecofix.py:121`, `ecofix.py:121`): a change to the
   `_BASE_URL` path, the `EL_Ecofix_<slug>_NL.pdf` filename scheme, or the product
   lineup.

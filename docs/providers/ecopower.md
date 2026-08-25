# Provider: ecopower

This document is a maintenance reference for the Ecopower supplier extractor
(`providers/ecopower.py`). Ecopower is a Flemish citizen cooperative that sells two residential
electricity products, both published as monthly PDF tariff cards behind a rotating CDN URL and
linked from two public price pages. This doc explains what the extractor fetches, how it parses
each card, the injection and VAT conventions that make Ecopower an outlier among the suppliers,
and the historical-bug land mines a future maintainer must respect when a card layout changes.

Read alongside:

- [../provider-framework.md](../provider-framework.md): the `SupplierExtractor` protocol, the
  snapshot dataclasses (`VariableRates`, `DynamicRates`, `InjectionRates`, `DsoOverlay`,
  `TaxOverlay`), and the shared `_pdf.py` helpers.
- [../pricing-model.md](../pricing-model.md): how `compute_breakdown` consumes the snapshot,
  including the VAT scaling that Ecopower's HTVA cards depend on.

## Overview

Ecopower sells residential electricity in **Flanders only** (`_ECOPOWER_REGIONS =
frozenset({REGION_FLANDERS})`, `ecopower.py:848`). `fetch()` rejects any other region up front
(`ecopower.py:154`), and `EXTRACTOR.regions()` returns just Flanders because both contracts
declare `regions=_ECOPOWER_REGIONS` (`ecopower.py:858`, `ecopower.py:864`).

The two products (module docstring, `ecopower.py:26-64`):

1. **Groene burgerstroom** (green citizen power): a half-fixed, half-indexed tariff against the
   monthly RLP-weighted Belpex Day-Ahead average. Modelled as a `variable` contract. A new card
   is published every month.
2. **Dynamische burgerstroom**: a quarter-hourly EPEX Day-Ahead dynamic tariff (quarter-hourly
   since the SDAC 15-minute market switch of 2025-10-01). Modelled as a `dynamic` contract. The
   card is republished only when the formula, DSO or tax rates change.

Both cards print all amounts **HTVA** (ex-VAT). Ecopower is the cooperative outlier here: every
other supplier publishes TVAC and sets `vat_rate=0.0`. Ecopower sets `vat_rate=0.06` in the tax
overlay so `compute_breakdown` scales the per-kWh energy and levies up to TVAC (module docstring
`ecopower.py:613-651`; `_extract_taxes` `ecopower.py:606-644`). Residential injection is VAT-exempt,
so injection formulas are stored unscaled.

That `vat_rate=0.06` also makes Ecopower the one residential card where `base.apply_vat` is **not**
a no-op: it grosses every flat annual fee (the yearly fee, data management, capacity, the prosumer
forfaits) once per entry, because those never reach `compute_breakdown`'s per-kWh factor. So the
extractor must store flat fees exactly as the card prints them. It used to bake the 6% itself as
well, which billed it twice once the B2B work routed every snapshot through `apply_vat`: Fluvius
Antwerpen databeheer was served at `17,85 × 1,06² = 20,06` instead of `18,92`, overstating a 4 kW
entry by about 13,70 EUR/yr.

### Publication URLs

| Product | Price page (scraped) | Card filename pattern |
| --- | --- | --- |
| Groene burgerstroom (gbs) | `ecopower.be/groene-stroom/prijs-nieuw` (`_PRICE_PAGE`, `ecopower.py:110`) | `<YYYYMM>_gbs_tariefkaart.pdf` |
| Dynamische burgerstroom (dbs) | `ecopower.be/groene-stroom/dynamische-burgerstroom` (`_DBS_PAGE`, `ecopower.py:146`) | `<YYYYMM|YYYYMMDD>[a-z]?_dbs_tariefkaart*.pdf` |

Card PDFs live at a rotating `cdn.nimbu.io/.../<YYYYMM>_..._tariefkaart.pdf` URL that changes each
month, so the extractor never hardcodes a card URL. It scrapes the price page HTML to discover the
current card link, then downloads and parses that PDF. The `source_url` stored on the snapshot is
the resolved CDN URL, not the price page.

## Contracts

| id | label | kind | regions | quarter_hourly | spot_indexed_injection |
| --- | --- | --- | --- | --- | --- |
| `ecopower_burgerstroom` | Ecopower Groene Burgerstroom | `variable` | Flanders | n/a | False (default) |
| `ecopower_dynamische_burgerstroom` | Ecopower Dynamische Burgerstroom | `dynamic` | Flanders | True | False (default) |

Constants: `_CONTRACT_ID` / `_CONTRACT_LABEL` (`ecopower.py:141-142`), `_DBS_CONTRACT_ID` /
`_DBS_CONTRACT_LABEL` (`ecopower.py:144-145`). The `EXTRACTOR` registration is at
`ecopower.py:887-907`.

Notes:

- **Groene burgerstroom** is a `variable` contract even though it is 50% fixed. The card resolves
  the blended rate against the current month's Belpex average and prints the resolved number; the
  extractor takes that resolved figure into `VariableRates.current` rather than re-deriving it,
  because there is no Belpex feed at parse time (`_extract_energy` docstring,
  `ecopower.py:386-402`).
- **Dynamische burgerstroom** sets `quarter_hourly=True` (`ecopower.py:417`). Ecopower's card
  multiplies the 15-minute EPEX DA spot, so the live price table, current / next-slot sensors and
  the cheapest-window service keep the native 15-minute slots. YTD billing stays hourly regardless
  (Home Assistant only retains hourly long-term statistics). See `DynamicRates` docstring,
  `base.py:141-154`.
- Neither contract sets `spot_indexed_injection`. The dynamic contract already collects the
  ENTSO-E key via its energy formula, and the variable contract publishes a monthly indicative
  injection credit, so no separate spot key step is needed.

No product has been retired. Both are Flanders-only by construction (there is no other region to
be limited to).

## Fetch strategy

```
fetch(session, contract_id, region)              ecopower.py:149
  region != Flanders                  -> ExtractorError
  ecopower_burgerstroom               -> _resolve_latest_pdf   -> parse_snapshot
  ecopower_dynamische_burgerstroom    -> _resolve_latest_dbs_pdf -> parse_dbs_snapshot
```

### Current card discovery

- **gbs** (`_resolve_latest_pdf`, `ecopower.py:797`): GET the price page HTML, run `_CARD_RE`
  (`ecopower.py:113`) over it to collect every `(sort_key, YYYYMM, url)` triple, **drop any URL
  containing `inschatting`** (the next-month estimation preview), sort ascending and take the
  highest. That is the card billing today. Label is `YYYY-MM`.
- **dbs** (`_resolve_latest_dbs_pdf`, `ecopower.py:826`): GET the dynamic product page, run
  `_DBS_CARD_RE` (`ecopower.py:155`), sort and take the highest. The dynamic formula is
  stable across months, so the newest card is the one in effect.

Both patterns accept a **six- or eight-digit** stamp. Ecopower named every card `YYYYMM_...` until
the August 2026 dynamic card arrived as `20260801_dbs_tariefkaart.pdf`; the six-digit pattern could
not match eight digits, so the resolver silently fell back to the January card and served its
pre-August tax block for eleven days. Nothing failed, because the January card is a real card that
downloads and parses. Ordering across the two widths goes through `_card_stamp_keys`
(`ecopower.py:119`), which pads the month-only form to `YYYYMM00` so a dated reissue outranks a
bare month card for the same month, and keys the month off the first six digits either way.

`_CARD_RE` matches only the definitive `_gbs_tariefkaart.pdf` form. `_DBS_CARD_RE` additionally
allows an optional single-letter suffix after the stamp (e.g. `202501b_dbs_tariefkaart.pdf`) and
a trailing brand token (`..._dbs_tariefkaart_ecopower.pdf`); the letter is consumed but not
captured so ordering stays numeric (comment `ecopower.py:145`).

### Probe (freshness key)

`probe()` (`ecopower.py:239-254`) HEADs the relevant price page and returns its `Last-Modified`
header via `head_freshness_key` (`_pdf.py:414`). The gbs contract probes `_PRICE_PAGE`, the dbs
contract probes `_DBS_PAGE`. The page returns a stable `Last-Modified` (server-side cache key), so
a HEAD round-trip detects a publication. On transport error or a missing header the helper returns
`None` and the coordinator's time-based TTL takes over (probe is not `None` for Ecopower; only its
failure path falls back to TTL).

### Historical fetch (`fetch_for_month`)

`fetch_for_month` (`ecopower.py:189-238`) supports the time-correct yearly-cost flow. Ecopower's
price pages retain roughly the last four months of cards, so an accessible (if shallow) archive
exists, and past months are billed at their own card rather than the current-snapshot proxy where
possible.

- **gbs** (`ecopower.py:202`): scrape `_PRICE_PAGE`, find the `_CARD_RE` matches whose month
  equals the requested one and whose URL is not an `inschatting` preview, take the highest stamp
  among them (a month can carry both a bare and a dated card), download and
  `parse_snapshot`. Then `archive_validity_check` (`_pdf.py:908`) cross-checks that the parsed card
  actually covers the requested month, using Dutch month names (`_NL_MONTHS`, `ecopower.py:107-107`)
  for the textual fallback when `valid_until` is absent. This guards against the CDN serving the
  current card under a historical URL and mis-billing past consumption at current rates. Returns
  `None` when the listing lacks the month, the URL 404s, or the PDF does not parse.
- **dbs** (`_fetch_dbs_for_month`, `ecopower.py:846`): dynamic cards do not rotate monthly, so
  pick the most recent card whose **month** is not after the requested one (`yyyymm <= target`,
  taken from `_card_stamp_keys` rather than the raw stamp -- comparing the raw stamp excluded a
  `YYYYMMDD` card from its own month, since `"20260801" > "202608"`). That is the card that was
  billing then across a year-boundary rate change. Returns
  `None` before the earliest published card, and there is no `archive_validity_check` step (the
  card-in-effect logic already picks the right one).

### Catalog discovery (`discover`)

`discover()` (`ecopower.py:257-301`) is a CI / live-check helper, not part of runtime pricing. It
scrapes both pages, emits `_CONTRACT_ID` when a gbs card is seen and `_DBS_DISCOVER_ID`
(`ecopower_dbs`, `ecopower.py:141`) when a dbs card is seen, and surfaces any **new**
`..._tariefkaart.pdf` family on either page as `ecopower_<family>` for the drift detector. The
baseline it diffs against is `DISCOVER_IDS = {_CONTRACT_ID, _DBS_DISCOVER_ID}`
(`ecopower.py:146`). A page that fails to fetch is logged, not swallowed, so a partial failure
cannot slip a dropped family past the empty-result warning.

## Parsing

Two pure parsers, both exposed for unit tests:

- `parse_snapshot(text, source_url, publication_label)` (`ecopower.py:302-316`): gbs card.
- `parse_dbs_snapshot(text, source_url, publication_label)` (`ecopower.py:319-338`): dbs card. The
  tax block layout is identical, so it reuses `_extract_taxes`; only energy, DSO and injection
  parsers differ.

Both operate on layout-preserving pdfplumber text (`extract_pdf_text_layout`, `_pdf.py:311`;
fetched via `fetch_pdf_text_layout`, `_pdf.py:401`). pdfplumber is required because the cards use
rotated multi-column DSO and tax tables that a plain text extractor drops. Fixture tests read the
same layout text through `fixture_text(name, layout=True)` (`test_ecopower.py:55-56`).

### Fields extracted per card

| Snapshot field | gbs source | dbs source |
| --- | --- | --- |
| `energy` | `_extract_energy` (`ecopower.py:386`) -> `VariableRates.current` | `_extract_dbs_energy` (`ecopower.py:424`) -> `DynamicRates` |
| `dsos` | `_extract_dsos` (`ecopower.py:470`) | `_extract_dbs_dsos` (`ecopower.py:547`) |
| `taxes` | `_extract_taxes` (`ecopower.py:613`) | same helper reused |
| `injection` | `_extract_injection` (`ecopower.py:730`) | `_extract_dbs_injection` (`ecopower.py:776`) |
| `valid_until` | `parse_valid_until` (`_pdf.py:947`) | same |
| `publication_label` | passed in (`YYYY-MM`) | passed in |

### Energy parsing

**gbs** (`_extract_energy`, `ecopower.py:386-402`): the card prints a formula breakdown
`(50% vast aan 0,17 euro + 50% variabel aan 0,08472117 euro)` followed by the resolved figure
(illustrative `0,1274 euro/kWh` in the April fixture, `test_ecopower.py:92-97`). Three regexes,
tried in this order:

- `_ENERGY_RE` (`ecopower.py:354`): same-line `Groene burgerstroom ... <rate> euro/kWh`.
- `_ENERGY_VARIABEL_RE` (`ecopower.py:378-383`): the July 2026 layout, which broke the 50/50 split
  onto its own `VAST` and `VARIABEL` lines with the resolved rate trailing the VARIABEL half.
  It anchors on those two literal rows rather than merely skipping a line: a looser
  "label, then one or two lines" pattern matches `Kost WKK 0,00392 euro/kWh` two lines under the
  label on the same-line cards, which would bill the cogeneration levy as the commodity rate the
  moment the same-line regex missed (`test_variabel_layout_does_not_capture_the_wkk_levy`).
- `_ENERGY_SPLIT_RE` (`ecopower.py:363-366`): fallback for mid-2026 cards that moved the resolved
  rate onto the line **below** the `Afname Groene burgerstroom (...)` label.

The resolved number is used (not the formula components) because there is no live Belpex feed at
parse time, and carrying a variable cost without a live spot is what `VariableRates` is for.

**dbs** (`_extract_dbs_energy`, `ecopower.py:424-450`): the card prints
`Dynamische burgerstroom elk kwartier 0,00102 × EPEX DA +0,004 euro/kWh` (illustrative,
`test_ecopower.py:359-368`). `_DBS_ENERGY_RE` (`ecopower.py:414-419`) captures factor, sign, base.

- **Factor is scaled by 1000** because the card multiplies EPEX DA in EUR/MWh while the pricing
  engine feeds the spot in EUR/kWh (`0,00102 × MWh = 1.02 × kWh`).
- The multiplication glyph is `×` (U+00D7); the regex accepts `[×xX*]` in case a re-render swaps
  it.
- The additive base sign is parsed through `SIGN_CHARS` / `parse_sign` (`_pdf.py:660`,
  `_pdf.py:532`) so a punctuation drift (hyphen vs en-dash vs U+2212) never flips the sign
  silently.
- Values stay HTVA; `vat_rate=0.06` scales them later. They are NOT pre-scaled.

The monthly subscription `Abonnementskost <n> euro/maand` (`_ABONNEMENT_RE`, `ecopower.py:421`)
maps to `yearly_fixed_fee` via `_extract_dbs_abonnement` (`ecopower.py:453-463`). Because
`yearly_fixed_fee` is summed as actual euros, the parser multiplies the monthly figure by 12 and
leaves it HTVA (`5 × 12 = 60,00`); `apply_vat` turns that into the `63,60` an entry is billed.

### DSO parsing

Ecopower maps all eight Fluvius sub-areas via `_DSO_LABELS` (`ecopower.py:138-138`). Note the two
label-to-key mappings that are not literal transliterations:

| Card label | Canonical key |
| --- | --- |
| Fluvius Kempen | `DSO_FLUVIUS_IVEKA` |
| Fluvius Midden-Vlaanderen | `DSO_FLUVIUS_INTERGEM` |

The other six map by their obvious name. Tests assert all eight are present for both cards
(`test_ecopower.py:100-103`, `314-318`).

**gbs** (`_extract_dsos`, `ecopower.py:470-530`): the card lists two networks per sub-area, a
DIGITAL METER block and an ANALOG METER block. The integration only models the **digital** path
(`_slice_between(text, "DIGITALE METER", "ANALOGE METER")`, `ecopower.py:450`), which is where the
post-2024-mandatory-rollout majority of Flemish residential sits. Analog-meter users still get
realistic prices because Ecopower bills them the same energy rate; only network costs differ.

Digital row layout (comment `ecopower.py:487-493`):

```
<label> | databeheer EUR/yr | capacity EUR/kW/yr | - | enkelvoudig EUR/kWh | uitsluitend_nacht EUR/kWh | [maximumtarief] | -
```

Row regex `ecopower.py:494-499`. The optional 7th `Maximumtarief` column slides in between the
exclusive-night rate and the trailing dash on rows where Fluvius publishes a maximum (the Imewo
April 2026 card has one, `test_ecopower.py:124-134`); the `(?:\s+[\d,]+)?` group skips it without
mis-aligning the distribution rate.

Captured columns map to `DsoOverlay` (`ecopower.py:516-522`):

- `data_management_per_year` = databeheer (as printed, HTVA; `apply_vat` grosses it)
- `capacity_eur_per_kw_year` = capacity (as printed, HTVA; `apply_vat` grosses it)
- `distribution_single` = enkelvoudig (unscaled)
- `distribution_exclusive_night` = uitsluitend-nacht (unscaled)
- `transport = 0.0` (Elia transport is rolled into distribution on Ecopower's card; there is no
  separate transport line, so it stays 0 rather than being double-counted by a guess,
  `test_ecopower.py:118-121`)

**dbs** (`_extract_dbs_dsos`, `ecopower.py:547-588`): the dynamic card has only a digital block (a
dynamic contract requires a smart meter), sliced `_slice_between(text, "Nettarieven",
"Heffingen")` (`ecopower.py:530`). The row layout differs (no separating dashes):

```
databeheer | capacity | afname enkelvoudig | afname uitsluitend-nacht | [maximumtarief] | injectietarief
```

Row regex reads the first four numeric columns (`ecopower.py:570-574`) and ignores the optional
maximumtarief and the trailing injection network tariff (`DsoOverlay` does not model them). Column
mapping (`ecopower.py:577-586`): `distribution_single` = group 3, `distribution_exclusive_night` =
group 4, `capacity_eur_per_kw_year` = group 2, `data_management_per_year` = group 1 (both as
printed, HTVA; `apply_vat` grosses them), `transport = 0.0`.

The dbs DSO block has a wrapped-label hurdle: on the narrower dynamic card pdfplumber wraps the
longest label `Fluvius Midden-Vlaanderen` across three lines (`Fluvius Midden-` /
`<numbers>` / `Vlaanderen`). `_DBS_WRAPPED_LABEL_RE` (`ecopower.py:544`) plus the `.sub`
(`ecopower.py:565-567`) stitches the two label fragments back around the rate row so the per-DSO
row regex sees one line. Tests assert the stitched row keeps its real rates
(`test_ecopower.py:336-343`).

Both DSO parsers **fail loud** with `ExtractorError("Ecopower: no DSO rows parsed ...")` if the
section header matches but no DSO row does (`ecopower.py:525-528`, `534-537`). Returning `{}`
would let the backfill path silently skip whole months (it swallows the resulting KeyError). The
`test_empty_dso_overlay_is_fatal` test verifies this by renaming `Fluvius` to `XXX`
(`test_ecopower.py:67-72`).

### Tax parsing

`_extract_taxes` (`ecopower.py:606-644`), shared by both cards. Regexes at `ecopower.py:613-651`:

| TaxOverlay field | Card row | Regex | Required? |
| --- | --- | --- | --- |
| `federal_excise` | Bijzondere accijns (0 - 3.000) | `_FEDERAL_EXCISE_RE` | yes |
| `energy_contribution` | Bijdrage op de energie | `_ENERGY_CONTRIB_RE` | yes |
| `flanders_renewables` | Kost GSC + Kost WKK | `_GSC_RE` + `_WKK_RE` | yes (both) |
| `energy_fund_eur_per_month` | Bijdrage Energiefonds euro/maand | `_FUND_RE` | optional (0 if absent) |
| `vat_rate` | (constant) | n/a | always `0.06` |

Wallonia and Brussels renewables stay 0 (Ecopower is Flanders-only, `test_ecopower.py:148-150`).

GSC (Groenestroomcertificaten) and WKK (warmte-krachtkoppeling / cogen) certificate costs are the
Flanders renewable surcharge in disguise. They are printed in the energy block but passed straight
through per-kWh, so they belong in `flanders_renewables` rather than being baked into
`energy.current` (which would move their value silently when Fluvius changes the certificate
quota, docstring `ecopower.py:614-619`). **Both are mandatory**: a missing GSC or WKK raises,
because treating them as optional would let a relabel silently drop a per-kWh charge
(`ecopower.py:639-640`; `test_missing_gsc_or_wkk_surcharge_is_fatal`, `test_ecopower.py:75-81`).

### Injection parsing

**gbs** injection is a **monthly-indicative-only** shape (taxonomy: `current` set, no
`factor`/`base`). `_extract_injection` (`ecopower.py:723-756`). The terugleververgoeding is a
feed-in credit the customer *receives*; Ecopower states it is never negative, but the card prints
it as a negative EUR/kWh figure because it sits in the energy/cost column where a credit shows as a
negative cost. The parser takes the magnitude (`abs`) so `current` holds a positive credit,
matching every other supplier's sign (`test_ecopower.py:153-160`).

Three matching strategies, in priority order:

1. `_INJECTION_FIXED_RE` (`ecopower.py:698-701`): an authoritative `OPGELET t.e.m. <date> is de
   terugleververgoeding <value> euro/kWh en 100% vast` note. When present **and still in effect**
   (`_fixed_note_in_effect`, `ecopower.py:702-720`), this fixed value wins.
2. `_INJECTION_RE` (`ecopower.py:650-660`): the label line, matching both the pre-May-2026 label
   `Terugleververgoeding (digitale meter)` and the post-May-2026 label
   `Injectie Groene Burgerstroom (terugleververgoeding)`.
3. `_INJECTION_VARIABEL_RE` (`ecopower.py:677-682`): the July 2026 `VAST` / `VARIABEL` layout,
   mirroring `_ENERGY_VARIABEL_RE` and anchored the same way. Before it existed the whole block
   missed and `_extract_injection` returned `None`, which costs a solar user their entire feed-in
   credit with no error raised anywhere.
4. `_INJECTION_SPLIT_RE` (`ecopower.py:672-676`): split-layout fallback where the resolved value
   is on the line below the label.

All four use `SIGN_CHARS` for the leading sign, and non-ASCII minus glyphs are normalised to `-`
before `to_float` (`ecopower.py:755-763`). Returns `None` when nothing matches (injection is
nullable).

**dbs** injection is an **hourly `factor*spot+base`** shape. `_extract_dbs_injection`
(`ecopower.py:761-776`) parses `Terugleververgoeding elk kwartier 0,00098 × EPEX DA - 0,015
euro/kWh` via `_DBS_INJECTION_RE` (`ecopower.py:768-773`). Same MWh->kWh factor scaling (`× 1000`)
and signed base as the consumption formula. The base can be negative (the credit drops below zero
at low spot, which the pricing engine respects). Stored unscaled (residential injection is
VAT-exempt). Sets `current=None`, `factor`, `base`, and a diagnostic `formula` string
(`test_ecopower.py:302-311`).

## Energy formula, overlays and injection summary

| Aspect | Groene burgerstroom (variable) | Dynamische burgerstroom (dynamic) |
| --- | --- | --- |
| Energy | resolved monthly blended rate -> `VariableRates.current` | `factor × spot + base` (EUR/kWh), `quarter_hourly=True`, plus `yearly_fixed_fee` |
| Fixed fee | none | Abonnementskost, VAT-incl 12-month total |
| DSO coverage | all 8 Fluvius sub-areas, digital-meter block | all 8 Fluvius sub-areas, digital-only block |
| Capacity / databeheer | flat euro fees, 6% VAT baked in | same |
| Distribution | per-kWh, unscaled (pricing applies VAT) | same |
| Transport | 0.0 (rolled into distribution) | 0.0 |
| Tax overlay | federal excise + energy contribution + GSC+WKK renewables + energy fund, `vat_rate=0.06` | identical block, reused |
| Injection shape | monthly-indicative-only (`current`) | hourly `factor × spot + base` |
| Prosumer / PV forfait | none (Flanders digital SMR3 has no prosumer tariff) | none |

There is no supplier-side PV/prosumer forfait: `supplier_prosumer_eur_per_kva_year` stays `None`.
Flanders digital meters (post-2024 SMR3) do not carry the compensation-regime prosumer tariff
(`DsoOverlay.prosumer_eur_per_kva_year` docstring, `base.py:458-458`), so the extractor never sets
it. No Wallonia Tarif Impact or Brussels OSP applies (Flanders-only).

### VAT scaling: the recurring Ecopower gotcha

Because Ecopower publishes HTVA, **every** value is stored exactly as the card prints it and the
6% is applied once, downstream, by whichever layer bills that value:

| value | grossed by | where |
| --- | --- | --- |
| per-kWh energy, distribution, levies | `compute_breakdown` via `vat_rate=0.06` | `pricing._finalize_breakdown` |
| flat annual euro fees (yearly fee, data management, capacity, prosumer forfaits) | `base.apply_vat`, once per entry | `base.py:631` |

The worked example: the same Fluvius databeheer prints `17,85` HTVA on Ecopower's card versus
`18,92` TVAC on other suppliers' cards, and `apply_vat` is what turns one into the other
(`ecopower.py:475`; `test_ecopower.py`, `test_flat_fees_are_grossed_exactly_once_end_to_end`).

The extractor used to bake the 6% into the flat fees itself, on the reasoning that they bypass the
pricing engine's VAT factor. That was correct until the B2B work routed every snapshot through
`apply_vat`, which grosses exactly those fees — after which they were billed twice: databeheer
served at `17,85 × 1,06² = 20,06` instead of `18,92`, capacity at `55,51` instead of `52,36`,
overstating a 4 kW entry by about 13,70 EUR/yr (8,99 at the 2,5 kW Flemish floor, 19,99 at 6 kW).
**Do not reintroduce a `* 1.06` anywhere in this module.** If a flat fee looks 6% light, the bug is
that `apply_vat` is not reaching it, not that the extractor should pre-scale it.

## Quirks and historical bugs

Every non-obvious hazard the source comments flag:

- **HTVA cards, `vat_rate=0.06`.** Ecopower is the cooperative outlier. Do not blindly copy a
  TVAC-publishing supplier's `vat_rate=0.0` convention here (`ecopower.py:59-63`, `581-591`).
- **`inschatting` next-month preview.** Around month-end Ecopower publishes an estimation card
  (`..._gbs_inschatting_tariefkaart_ecopower.pdf`) alongside the definitive one. The fetcher and
  `fetch_for_month` both drop any URL containing `inschatting`; `_CARD_RE` matches only the
  definitive form (`ecopower.py:109-119`, `191-197`, `733-739`). Test:
  `test_fetch_for_month_skips_inschatting_preview` (`test_ecopower.py:472-484`).
- **Issue #31, May 2026 injection relabel.** The injection row was renamed from
  `Terugleververgoeding (digitale meter)` to `Injectie Groene Burgerstroom (terugleververgoeding)`,
  which the old regex missed, so the injection price went unavailable. `_INJECTION_RE` now matches
  both labels (`ecopower.py:657-667`; `test_may_card_injection_label_is_matched`,
  `test_ecopower.py:252-260`).
- **Split-layout cards (mid-2026).** The resolved energy and injection values moved onto the line
  **below** their label. `_ENERGY_SPLIT_RE` and `_INJECTION_SPLIT_RE` are the fallbacks
  (`ecopower.py:363-366`, `633-637`; `test_split_layout_card_parses_energy_and_injection`,
  `test_ecopower.py:171-185`).
- **`100% vast` injection note vs the 50/50 variable formula.** On split-layout cards the label
  line carries only the 50/50 formula and the line below resolves the **variable** half, which
  only applies once Ecopower flips injection to 50% variable (from 1 July 2026). While the card
  prints `OPGELET t.e.m. <date> ... en 100% vast`, that fixed credit is authoritative and must
  win, or users get credited the variable value (illustrative `0,0329`) instead of the fixed one
  (illustrative `0,020`) they actually receive (`ecopower.py:691-694`, `668-676`;
  `test_split_layout_card_parses_energy_and_injection`).
- **Stale carried-over note.** A later month's card can still carry the old note while already
  printing the variable formula. `_fixed_note_in_effect` (`ecopower.py:702-720`) compares the
  card's own month (`Tariefkaart <month> <year>`) against the note's declared expiry (`t.e.m. <n>
  <month>`) and ignores a stale note, falling back to the variable value. It returns `True` when
  staleness cannot be established (a card with no parseable month still trusts its note). Test:
  `test_stale_fixed_injection_note_is_ignored_on_a_later_card` (`test_ecopower.py:314-327`).
- **Injection sign / never-negative.** The card prints the credit in the cost column as negative;
  the parser takes `abs()` so a card that ever prints it positive is not flipped into a debit
  (`ecopower.py:750-756`).
- **`Maximumtarief` optional 7th column.** Slides into a DSO row where Fluvius publishes a maximum
  (Imewo April 2026). The regex skips it; misreading it would mis-align the distribution rate
  (`ecopower.py:487-499`; `test_april_card_extracts_imewo_with_optional_max_column`,
  `test_ecopower.py:124-134`).
- **Wrapped `Fluvius Midden-Vlaanderen` label on the dbs card.** pdfplumber splits the long label
  across three lines on the narrower dynamic card; `_DBS_WRAPPED_LABEL_RE` stitches it
  (`ecopower.py:514`, `512-514`).
- **Transport rolled into distribution.** Ecopower's card has no separate Elia transport line, so
  `transport=0.0`; do not invent a transport value or it double-counts (`ecopower.py:489`, `527`;
  `test_ecopower.py:118-121`).
- **Digital-meter-only model.** Only the DIGITALE METER block is parsed; the ANALOGE METER block
  is ignored (same energy rate, different network cost, `ecopower.py:470-481`).
- **MWh vs kWh factor scaling.** Both dbs formulas print the factor against EPEX DA in EUR/MWh, so
  factor is `× 1000` (`ecopower.py:411`, `712`). Forgetting this understates the spot component
  by 1000x.
- **dbs `yearly_fixed_fee` is absolute euros, stored HTVA.** It is summed without rescaling in the
  YTD path, so the parser multiplies out the 12 months (`ecopower.py:421`) and stops there:
  `apply_vat` adds the 6% once per entry. Multiplying it here as well billed it twice.
- **Fail-loud on empty DSO / missing GSC/WKK.** Both are deliberate guards against a silent
  backfill skip / silently-dropped mandatory charge (`ecopower.py:525-528`, `579-580`).
- **`discover` logs unreachable pages.** A partial page failure is logged, not swallowed, so a
  dropped family is not masked by a still-non-empty result (`ecopower.py:239-256`).

## Test fixtures

Fixtures under `tests/fixtures/` exercised by `tests/test_ecopower.py`:

| Fixture | Card variant | Exercises |
| --- | --- | --- |
| `ecopower_burgerstroom_apr.pdf` | April 2026 gbs, same-line layout | energy resolved rate, all 8 DSOs, Antwerpen + Imewo (optional-max) columns, HTVA taxes, positive injection credit, fail-loud guards |
| `ecopower_burgerstroom_may.pdf` | May 2026 gbs, relabelled injection row (#31) | injection label match |
| `ecopower_burgerstroom_jun_split.pdf` | June 2026 gbs, split layout + `100% vast` note | split-layout energy/injection, fixed-note precedence, stale-note handling (relabelled to July in one test) |
| `ecopower_burgerstroom_jul.pdf` | July 2026 gbs, `VAST` / `VARIABEL` layout, injection gone 50% variable | VARIABEL-layout energy + injection, and the WKK-capture regression guard (`test_ecopower.py:244`, `:261`) |
| `ecopower_burgerstroom_feb.pdf` | Feb 2026 gbs | `fetch_for_month` happy path |
| `ecopower_dynamische_burgerstroom_jan.pdf` | Jan 2026 dbs, dynamic + wrapped Midden-Vlaanderen label | dynamic formula, subscription fee, dynamic injection, wrapped-label stitch, dbs taxes, dbs `fetch_for_month` |

`fetch_for_month` tests also use inline HTML listings `_LISTING_HTML` (`test_ecopower.py:442-448`)
and `_DBS_LISTING_HTML` (`test_ecopower.py:514-519`) with a stub `_Session` / `_Resp`, patching
`fetch_pdf_text_layout` to return fixture text.

## When the card changes, look here

Ranked by likelihood of breaking when Ecopower re-renders a card:

1. **Energy rate moved or relabelled** -> `_ENERGY_RE` / `_ENERGY_VARIABEL_RE` /
   `_ENERGY_SPLIT_RE` (`ecopower.py:354-383`)
   for gbs, `_DBS_ENERGY_RE` (`ecopower.py:414-419`) for dbs. A `raise ExtractorError("could not
   parse ... rate/formula")` is the symptom.
2. **Injection label / layout / note changed** -> `_INJECTION_RE`, `_INJECTION_SPLIT_RE`,
   `_INJECTION_FIXED_RE`, `_fixed_note_in_effect` (`ecopower.py:650-720`). Injection is nullable,
   so a miss shows as an unavailable injection sensor, not a hard error (watch for silent loss).
3. **DSO table column shuffle or new sub-area label** -> `_DSO_LABELS` (`ecopower.py:138-138`),
   the gbs row regex (`ecopower.py:544-544`), the dbs row regex + `_DBS_WRAPPED_LABEL_RE`
   (`ecopower.py:514`, `517-521`). Symptom: `Ecopower: no DSO rows parsed` or a missing sub-area.
4. **Tax row relabelled** -> `_extract_taxes` regexes (`ecopower.py:613-651`). Symptom: `could not
   parse Ecopower federal tax block` or `GSC/WKK renewable surcharge`.
5. **Card filename family or price-page structure changed** -> `_CARD_RE`, `_DBS_CARD_RE`
   (`ecopower.py:112-158`), `_resolve_latest_pdf` / `_resolve_latest_dbs_pdf`
   (`ecopower.py:257-299`), and `discover` (`ecopower.py:257-301`). Symptom: `no Ecopower
   tariefkaart link found`.
6. **VAT rate change (no longer 6%)** -> the single `0.06` in `_extract_taxes`
   (`ecopower.py:611`). Nothing else: it is the only place the rate is written, and both
   `compute_breakdown` and `apply_vat` read it from there.

All EUR figures above (0,1274 energy, 0,1378 split energy, 0,02 / 0,0329 injection, 17,85 / 18,92
databeheer, 49,40 / 54,20 / 50,12 capacity, 60,00 HTVA / 63,60 TVAC subscription) come from source
comments or test assertions and are **illustrative** only; no price lives in the extractor source.

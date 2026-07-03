# Provider: dats24

This document is the maintainer reference for the DATS 24 supplier extractor
(`providers/dats24.py`). DATS 24 is the fuel-and-energy retail brand of Colruyt
Group; it sells one residential electricity product across Flanders and Wallonia
and publishes its rates as a monthly PDF served from a stable public API URL. The
notes below are grounded in the module and its fixture-driven test
(`tests/test_dats24.py`), which encodes the exact parse output against two real
cards and is treated here as ground truth for what the extractor must produce.

Related reading:

- [../provider-framework.md](../provider-framework.md): the `SupplierExtractor`
  protocol, the `Contract` / `SupplierSnapshot` / `EnergyRates` / `DsoOverlay` /
  `TaxOverlay` / `InjectionRates` dataclasses, the shared PDF helpers.
- [../pricing-model.md](../pricing-model.md): how `compute_breakdown` consumes the
  snapshot, the VAT convention, and how the injection shape is priced.

## Overview

| Property | Value |
|---|---|
| Extractor id | `dats24` (`dats24.py:451`) |
| Label | `DATS 24` (`dats24.py:452`) |
| Regions served | Flanders, Wallonia (`dats24.py:448`, `_DATS24_REGIONS`) |
| Products | one: `dats24_groen_variabel` (`dats24.py:98`) |
| Card format | single monthly PDF served from a fixed API URL |
| Probe | none (`EXTRACTOR` sets no `probe`, `dats24.py:450`) |
| Archive | none (no `fetch_for_month`); past months fall back to current snapshot |

DATS 24 sells one residential electricity product, "Elektriciteit Groen
Variabel" (`dats24.py:26-47`). It is a variable (monthly-indexed) contract. The
module docstring records the contract formula verbatim (illustrative, from the
source comment `dats24.py:33-34`):

```
afname        = (BE_spotRLP * 0.1124 + 0.511) * 1.06   c€/kWh   (single rate)
teruglevering = (BE_spotSPP * 0.0766 - 1.11)           c€/kWh   (VAT-exempt)
```

`BE_spotRLP` is a monthly Belgian spot index (quarter-hourly spot, RLP-weighted);
`BE_spotSPP` is a monthly synthetic-profile index. Both are monthly, not the
hourly ENTSO-E day-ahead spot, which is why the extractor surfaces printed
indicative values rather than a spot formula (see Injection below).

### Source URL

The card is fetched from one stable endpoint (`dats24.py:93-96`,
`_RATECARD_URL`):

```
https://profile.dats24.be/api/v1/ratecard
    ?energyType=electricity&contractType=variable&language=nl
```

The URL looks like a JSON API but actually returns a PDF (`dats24.py:41-42`).
Nothing in the URL varies by region, meter type, or month: one document carries
every region's DSO tables, tax block, and the current-month indicative rates, and
`parse_snapshot` slices out the requested region's overlays.

## Contracts

| id | label | kind | regions | spot_indexed_injection |
|---|---|---|---|---|
| `dats24_groen_variabel` | `DATS 24 Elektriciteit Groen Variabel` | `variable` | Flanders, Wallonia | False (default) |

Only one `Contract` is declared (`dats24.py:453-460`). There is no fixed, TOU, or
dynamic product, so `quarter_hourly` does not apply (that flag lives on
`DynamicRates`, not on a variable contract). `spot_indexed_injection` is left at
its default `False`: even though the energy is variable, the injection is a
monthly indicative (not a per-hour spot formula), so no ENTSO-E key is needed for
this contract. See [../provider-framework.md](../provider-framework.md) for what
`spot_indexed_injection` gates in the config flow.

`fetch` rejects any other contract id with `ExtractorError` (`dats24.py:131-132`)
and rejects Brussels or any non-FL/WA region (`dats24.py:133-136`): DATS 24 does
not sell residential electricity outside Flanders and Wallonia.

## Fetch strategy

```
fetch(session, contract_id, region)          dats24.py:126
  ├─ reject unknown contract id                dats24.py:131
  ├─ reject region not in {flanders, wallonia} dats24.py:133
  ├─ fetch_pdf_text_layout(session, URL)       dats24.py:137   (PDF -> pdfplumber layout text)
  └─ parse_snapshot(text, URL, region)         dats24.py:138
```

`fetch` downloads the PDF and runs it through `fetch_pdf_text_layout`
(`_pdf.py:316`), the layout-preserving pdfplumber path. Layout mode is required
because the card lays out DSO and tax data as multi-column tables; the plain text
extractor would collapse the columns. `fetch_pdf_text_layout` also guards against
CDNs that return HTTP 200 `text/html` for a missing PDF and rejects a
pages-present-but-no-text document as a hard error (`_pdf.py:319-326`,
`extract_pdf_text_layout` at `_pdf.py:196-232`).

### Probe

There is no `probe`. `EXTRACTOR` (`dats24.py:450-462`) sets only `id`, `label`,
`contracts`, and `fetch`; `probe` and `fetch_for_month` default to `None`. Because
the card is served from one overwrite-in-place API URL with no per-month key,
there is nothing cheap to diff, so the coordinator's time-based TTL governs
refresh. This is exactly the "DATS 24 single-PDF" case called out in the
`SnapshotProbe` contract comment in `base.py:347-351`.

Note the module does define a `discover` coroutine (`dats24.py:141-162`), but it
is a catalog-drift check for the live-check harness, not a coordinator probe. It
HEADs `_RATECARD_URL` and returns `{_CONTRACT_ID}` on any status below 400,
`set()` otherwise. Its docstring explains the deliberate design: a single 200 is
enough, and if DATS 24 ever splits the endpoint into `vast` / `tou` variants the
check stays green so we notice via a real extractor failure rather than a
false-positive new-product alert (`dats24.py:142-150`).

### Archive / historical months

There is no accessible archive and no `fetch_for_month`. The API URL is
overwrite-in-place (API-only, as listed for DATS 24 in the
`ArchivedSnapshotFetcher` comment `base.py:353-361`). Past months therefore fall
back to the current snapshot as a proxy in the yearly-cost backfill flow.

## Parsing

`parse_snapshot` (`dats24.py:165-177`) is a pure function exposed for unit tests
(the test harness feeds it fixture text directly, never the network). It builds a
`SupplierSnapshot` from five sub-parsers plus two shared helpers:

| Snapshot field | Parser | Source |
|---|---|---|
| `energy` | `_extract_energy` | `dats24.py:183-224` |
| `dsos` | `_extract_dsos` (dispatches per region) | `dats24.py:230-308` |
| `taxes` | `_extract_taxes` | `dats24.py:314-385` |
| `injection` | `_extract_injection` | `dats24.py:391-434` |
| `publication_label` | `_extract_publication` | `dats24.py:440-442` |
| `valid_until` | `parse_valid_until` (shared) | `_pdf.py:677` |
| `supplier` / `contract` | literals | `dats24.py:168-169` |

Every numeric value is parsed with `to_float` (`_pdf.py:452-463`), which strips
Unicode thousands separators and accepts both the Belgian comma decimal and a dot
decimal. This dot tolerance is not cosmetic: the May 2026 card switched its
separator from `,` to `.` (see Quirks).

### Energy (`_extract_energy`, `dats24.py:183-224`)

The card prints four indicative TVAC c€/kWh values on one row under `Afname1`
(single/mono, bi-hourly day, bi-hourly night, exclusive-night), plus a yearly
standing charge. The extractor uses these printed indicatives directly rather than
re-solving the formula, because spot data is not available at parse time and the
printed values are exactly what the monthly invoice settles at (`dats24.py:184-192`).

Layout the regex targets (illustrative comment values, `dats24.py:196-198`):

```
Afname1 (c€/kWh) 12,18 13,48 10,97 10,97
                 single  Day   Night Excl-night
```

- Afname row regex: `Afname1?\s*\(c€/kWh\)\s+(...)` four capture groups
  (`dats24.py:201-204`). A miss raises `could not parse DATS 24 indicative afname
  row` (`dats24.py:205-206`).
- Yearly fee regex: `VASTE VERGOEDING\s*\(€/jaar\)\s+(...)` (`dats24.py:211`). A
  miss raises `could not parse DATS 24 yearly fixed fee` (`dats24.py:212-216`);
  the standing charge is mandatory on every card, so a miss is layout drift, not a
  fee-free contract.

Output is `VariableRates(current, peak, offpeak, exclusive_night,
yearly_fixed_fee)` with the four c€/kWh values divided by 100 (`dats24.py:218-224`).
All four include 6% VAT. Illustrative parse from the April fixture
(`test_dats24.py:93-98`): `current 0.1218`, `peak 0.1348`, `offpeak 0.1097`,
`exclusive_night 0.1097`, `yearly_fixed_fee 38.50` EUR/yr.

### DSO overlay

`_extract_dsos` dispatches on region (`dats24.py:230-235`). It returns `{}` for
any region other than Flanders or Wallonia (defensive; `fetch` already rejects
those).

#### Flanders (`_extract_flanders_dsos`, `dats24.py:238-269`)

Iterates the eight Fluvius sub-areas in `_FLANDERS_DSOS` (`dats24.py:102-111`),
which maps the card's Dutch labels to the integration's canonical keys:

| Card label | Canonical key |
|---|---|
| ANTWERPEN | `fluvius_antwerpen` |
| HALLE-VILVOORDE | `fluvius_halle_vilvoorde` |
| IMEWO | `fluvius_imewo` |
| KEMPEN | `fluvius_iveka` |
| LIMBURG | `fluvius_limburg` |
| MIDDEN-VLAANDEREN | `fluvius_intergem` |
| WEST | `fluvius_west` |
| ZENNE-DIJLE | `fluvius_zenne_dijle` |

Note the two label-to-key renames a maintainer must preserve: DATS 24 prints
`KEMPEN` for Fluvius IVEKA and `MIDDEN-VLAANDEREN` for Fluvius INTERGEM.

Each row carries ten numeric columns; the comment documents the layout
(`dats24.py:240-249`):

```
cap_digital | afname_dig | afname_dig_excl_nacht | max_tarief
cap_classical | afname_class | afname_class_excl_nacht | prosumer
meteropname_kwartier | meteropname_jaarlijks
```

The extractor models only the digital-meter path (post-2024 Fluvius rollout
target); the four classical/analog numbers are ignored. It fills `DsoOverlay` with
(`dats24.py:262-268`): `distribution_single` = col 2 /100, `distribution_exclusive_night`
= col 3 /100, `transport` = 0.0 (rolled into Fluvius distribution on this card),
`capacity_eur_per_kw_year` = col 1 (the digital capacity term, EUR/kW/yr, NOT
divided by 100), `data_management_per_year` = col 10 (jaarlijks meteropname). A
row that does not match is skipped (`continue`, `dats24.py:260-261`), not fatal, so
a partial card still yields the DSOs it could parse. Illustrative Antwerpen values
(`test_dats24.py:207-212`): capacity 52.37 EUR/kW/yr, distribution 5.35 c€/kWh,
data-management 18.92 EUR/yr.

#### Wallonia (`_extract_wallonia_dsos`, `dats24.py:272-308`)

Iterates `_WALLONIA_DSOS` (`dats24.py:117-123`), an ordered tuple:

| Card label | Canonical key |
|---|---|
| AIEG | `aieg` |
| AIESH | `aiesh` |
| ORES (Brabant Wallon) | `ores` |
| RÉGIE DE WAVRE | `rew` |
| RESA | `resa` |

The ORES collapse is the key gotcha: DATS 24 lists seven ORES sub-areas (Brabant
Wallon, Est, Hainaut, Luxembourg, Mouscron, Namur, Verviers) with identical rates,
but the integration has one `ores` key, so the extractor matches only the Brabant
Wallon row (`dats24.py:113-116`, `280-283`). The regex is anchored at start of
line with `re.escape(label)`, so `ORES (Brabant Wallon)` only matches that one row.

Ten columns per row (`dats24.py:274-280`):

```
single | day | night | PIC | MEDIUM | ECO | excl_nacht
transport | data-beheer (€/yr) | prosumer (€/kVA/yr)
```

Mapped to `DsoOverlay` (`dats24.py:296-307`): `distribution_single` (col1/100),
`distribution_peak` (col2/100), `distribution_offpeak` (col3/100),
`distribution_pic` (col4/100), `distribution_medium` (col5/100),
`distribution_eco` (col6/100), `distribution_exclusive_night` (col7/100),
`transport` (col8/100, c€/kWh), `data_management_per_year` (col9, EUR/yr),
`prosumer_eur_per_kva_year` (col10, EUR/kVA/yr). The PIC/MEDIUM/ECO columns are the
Wallonia Tarif Impact CWaPE bands; they are carried on the DSO overlay even though
DATS 24's own product is not an Impact contract, because the DSO overlay is shared
across the pricing engine. There is no `capacity_eur_per_kw_year` on the Walloon
rows (Wallonia has no capacity term).

Illustrative ORES values (`test_dats24.py:225-234`): single 11.98, day 13.27,
night 7.39, PIC 16.57, medium 10.83, eco 5.09 c€/kWh, transport 2.74 c€/kWh, data
14.10 EUR/yr, prosumer 85.84 EUR/kVA/yr. RESA is deliberately distinct (single
11.06, prosumer 84.22, `test_dats24.py:237-244`), which guards against a regex that
would silently align all Walloon DSOs to one row.

### Taxes (`_extract_taxes`, `dats24.py:314-385`)

Federal levies are region-agnostic; regional renewables and fees are gated by
`region` so a Flanders user never accrues the Walloon connection fee and a
Wallonia user never accrues the Flemish Energiefonds (`dats24.py:325-330`).

Federal (both regions, mandatory, raise on miss `dats24.py:335-336`):

- `energy_contribution`: `Energiebijdrage\s+(...)\s*c€/kWh` (`dats24.py:331`) /100.
- `federal_excise`: `Verbruik tussen 0 kWh en 3\.000 kWh\s+(...)\s*c€/kWh`
  (`dats24.py:332-334`) /100. This is the lowest excise band (0-3000 kWh).

Flanders-only (`dats24.py:343-360`):

- `flanders_renewables` = GSC + WKC: `Vlaams Gewest:\s*GSC\s*\(c€/kWh\)\s+(...)`
  and `WKC\s*\(c€/kWh\)\s+(...)` summed, each /100. Both are mandatory and always
  printed together; either miss raises `DATS 24: Flanders GSC/WKC renewables not
  found` (`dats24.py:346-353`). GSC is the dominant half (the comment cites 1,183
  vs 0,378 c€/kWh, illustrative), so silently zeroing a missed GSC would under-bill
  by ~1.2 c€/kWh.
- `energy_fund_eur_per_month`: `Hoofdverblijf\s*\(domicilie\)\s+(...)\s*€/maand`
  (`dats24.py:357-360`). This one is NULLABLE: if the row is absent it defaults to
  0.0 rather than raising. The residential default is 0 (`test_dats24.py:116-118`);
  second-home users override in the OptionsFlow.

Wallonia-only (`dats24.py:361-375`):

- `wallonia_renewables` = CV: `Waals Gewest:\s*CV\s*\(c€/kWh\)\s+(...)` /100.
- `region_connection_fee`: `Aansluitingsvergoeding\s+Walloni[eë]\d*\s+(...)\s*c€/kWh`
  /100. The `\d*` tolerates a footnote digit that the layout-aware text glues onto
  the word `Wallonië` (`dats24.py:363-369`). Both are mandatory; either miss raises
  `DATS 24: Wallonia CV / connection fee not found` (`dats24.py:370-373`).

`TaxOverlay` sets `vat_rate=0.0` (`dats24.py:384`): all card values are already
TVAC (6% VAT), so `compute_breakdown` must not re-scale them. The card footer reads
`Alle prijzen ... inclusief 6% btw, tenzij anders vermeld`; the two exceptions
tagged `Niet aan btw onderworpen` (the Walloon connection fee and the Flemish
Energiefonds) happen to use the same per-kWh / per-month conventions, so they slot
in without conversion (`dats24.py:317-322`). Illustrative April values: Flanders
renewables 0.01561, Wallonia renewables 0.03032, connection fee 0.00075 EUR/kWh
(`test_dats24.py:113`, `127-128`).

### Injection (`_extract_injection`, `dats24.py:391-434`)

Injection shape: **monthly-indicative-only** (shape (a) of the three-shape
taxonomy). DATS 24 settles teruglevering on `BE_spotSPP`, a monthly synthetic
index, not the hourly day-ahead spot. The card prints the realized monthly
indicative right after the formula (`dats24.py:395-403`):

```
formula:    (BE_spotSPP x 0,0766 - 1,11)   c€/kWh, VAT-exempt
indicative: Teruglevering2 (c€/kWh) 3,26
```

The extractor surfaces ONLY the indicative as `InjectionRates.current`, leaving
`factor` and `base` as `None` (`dats24.py:431-434`). Emitting factor/base would make
the pricing engine apply the monthly coefficient to the hourly spot, mispricing the
credit; this mirrors EBEM Groen Variabel / B@sic+ and Ecofix Flexy's BELPEX-SPP-M
handling (`dats24.py:399-408`). The `formula` string is retained verbatim for
diagnostics only (`dats24.py:427-434`, and `test_injection_formula_text_retained_for_any_operator`
`test_dats24.py:146-159` proves any operator, including `+`, is captured as text
without affecting the price).

Two hard invariants encoded in tests:

- **Flanders-only.** Returns `None` in Wallonia (`dats24.py:414-415`): the card
  footnote reserves the teruglevering tariff to Flemish digital-meter customers, so
  a Walloon prosumer accrues no feed-in credit and the shared card's indicative must
  not be surfaced for them (`test_injection_is_flanders_only`,
  `test_dats24.py:162-171`).
- **Negative-safe sign parsing.** The indicative regex captures an optional leading
  sign, `Teruglevering2?\s*\(c€/kWh\)\s+([SIGN_CHARS]?)\s*(...)` (`dats24.py:419-421`),
  and applies `parse_sign` (`_pdf.py:477`). When `BE_spotSPP` is low the monthly
  indicative goes negative (the producer pays to inject); an earlier version without
  the sign group silently dropped the credit (`dats24.py:416-418`,
  `test_injection_indicative_handles_negative_value` `test_dats24.py:174-192`, which
  also checks a Unicode-minus glyph). A miss (no indicative at all) raises `DATS 24
  injection: monthly indicative missing` (`dats24.py:422-426`).

One shared teruglevering value covers all three meter types (single, bi-hourly day,
bi-hourly night), so a single `InjectionRates` entry serves everyone
(`dats24.py:410-412`).

There is no supplier-side prosumer/PV forfait on DATS 24 (`supplier_prosumer_eur_per_kva_year`
is left unset). The Walloon DSO prosumer term (`prosumer_eur_per_kva_year`) is the
only prosumer charge, and it lives on the DSO overlay, not the supplier snapshot.

### Publication label (`_extract_publication`, `dats24.py:440-442`)

`TARIEFKAART\s+(\w+\s+20\d{2})` case-insensitive, lowercased. Illustrative:
`april 2026` (`test_dats24.py:80`), `mei 2026` (`test_dats24.py:255`). Empty string
on miss (non-fatal). `valid_until` is parsed separately by the shared
`parse_valid_until` (`_pdf.py:677`), which catches the explicit `GELDIG VAN 1 APRIL
2026 T.E.M 30 APRIL 2026` header (`test_dats24.py:81-83`, expects `date(2026, 4, 30)`).

## Quirks and historical bugs

These are the land mines a future maintainer must know, each traceable to a source
comment or test:

1. **JSON-looking URL, PDF payload.** `_RATECARD_URL` ends in `ratecard?...` and
   reads like a JSON API but returns a PDF (`dats24.py:41-42`). Use the PDF helper,
   not a JSON decoder.
2. **All values are TVAC; `vat_rate=0.0`.** The card is 6% VAT-inclusive except two
   `Niet aan btw onderworpen` lines that still use per-kWh/per-month conventions
   (`dats24.py:317-322`, `384`). Do not add VAT scaling in the pricing engine.
3. **Decimal separator flipped between months.** The May 2026 card switched from
   comma to dot (`Afname1 10.64 11.77 ...` instead of `12,18 13,48 ...`). All
   regexes use the `[\d,.]+` class and delegate to `to_float`, which handles both;
   a comma-only regex would raise `could not parse DATS 24 indicative afname row`
   (`test_may_card_uses_dot_decimal_separator`, `test_dats24.py:247-272`). June
   reverted to commas.
4. **Seven ORES sub-areas collapse to one key.** Only the `ORES (Brabant Wallon)`
   row is kept (`dats24.py:113-116`, `280-283`,
   `test_april_card_wallonia_dsos_collapse_seven_ores_subareas_to_one`).
5. **Label renames KEMPEN->iveka, MIDDEN-VLAANDEREN->intergem** in the Flanders map
   (`dats24.py:106`, `108`).
6. **Digital-meter-only modeling.** Both DSO parsers read only the digital-meter
   columns; the analog/classical columns are intentionally ignored
   (`dats24.py:245-249`).
7. **Flanders capacity is not /100.** `capacity_eur_per_kw_year` and both
   `data_management_per_year` and `prosumer_eur_per_kva_year` are raw EUR values;
   only the c€/kWh distribution and transport columns are divided by 100. Mixing
   these up mis-scales by 100.
8. **Injection is Flanders-only and monthly.** Never emit factor/base; never surface
   a credit in Wallonia (`dats24.py:399-415`). See Injection above.
9. **Negative injection indicative.** Keep the optional sign group and `parse_sign`
   (`dats24.py:416-421`).
10. **Fatal-vs-nullable asymmetry.** Afname row, yearly fee, federal excise/contribution,
    Flanders GSC+WKC, Wallonia CV+connection fee, and the injection indicative all
    raise on miss. The Flemish Energiefonds `Hoofdverblijf (domicilie)` row and the
    publication label are nullable (default 0 / empty). This split is deliberate:
    mandatory charges must fail loud rather than silently under-bill.
11. **`discover` is a catalog check, not a probe.** It stays green if DATS 24 adds a
    second contract type; that is by design (`dats24.py:142-150`).
12. **May fixture excise artifact.** The hand-built dot-decimal May fixture left the
    excise row as a stray `000,005 c€/kWh` that cannot be patched (non-contiguous in
    the compressed stream, the real May card is gone). The May test intentionally
    does not value-assert taxes; tax extraction is covered by the April fixture and
    dot-decimal numeric parsing by the May energy asserts. Do not "fix" the fixture
    by asserting the stray value (`test_dats24.py:262-272`).

## Test fixtures

Under `tests/fixtures/`, exercised by `tests/test_dats24.py`:

| Fixture | Card variant | Exercises |
|---|---|---|
| `dats24_groen_variabel_apr.pdf` | April 2026 card, comma decimals | the default fixture (`test_dats24.py:44`); all value asserts (energy, taxes, both region DSO blocks, injection, publication metadata) and every fatal-miss test |
| `dats24_groen_variabel_may.pdf` | May 2026 card, dot decimals | dot-decimal tolerance across energy + DSO parsing (`test_dats24.py:247-272`); taxes deliberately not asserted (fixture artifact) |

The tests read fixtures via `fixture_text(name, layout=True)`, matching the
production `fetch_pdf_text_layout` path, and call `parse_snapshot` directly (no
network). `parse_snapshot` and `_extract_injection` are imported directly, so the
pure parsers are the unit under test.

## When the card changes, look here

| Symptom | Likely function | Why |
|---|---|---|
| `could not parse DATS 24 indicative afname row` | `_extract_energy` (`dats24.py:201-206`) | the `Afname1 (c€/kWh)` label, column count, or separator changed |
| `could not parse DATS 24 yearly fixed fee` | `_extract_energy` (`dats24.py:211-216`) | `VASTE VERGOEDING (€/jaar)` label moved |
| A Flanders DSO silently missing from `snapshot.dsos` | `_extract_flanders_dsos` / `_FLANDERS_DSOS` (`dats24.py:238-269`, `102-111`) | a Fluvius label was renamed (row skipped on no-match) or the 10-column layout changed |
| A Walloon DSO missing, or all sharing one row | `_extract_wallonia_dsos` / `_WALLONIA_DSOS` (`dats24.py:272-308`, `117-123`) | `ORES (Brabant Wallon)` / `RÉGIE DE WAVRE` label drift, or column reorder |
| `DATS 24: Flanders GSC/WKC renewables not found` | `_extract_taxes` (`dats24.py:344-353`) | the fragile `Vlaams Gewest: GSC` / `WKC` prefixes changed |
| `DATS 24: Wallonia CV / connection fee not found` | `_extract_taxes` (`dats24.py:362-373`) | `Waals Gewest: CV` or the `Aansluitingsvergoeding Wallonië` footnote changed |
| `could not parse DATS 24 federal tax block` | `_extract_taxes` (`dats24.py:331-336`) | `Energiebijdrage` or `Verbruik tussen 0 kWh en 3.000 kWh` moved |
| `DATS 24 injection: monthly indicative missing` | `_extract_injection` (`dats24.py:419-426`) | the `Teruglevering2 (c€/kWh)` label changed, or the card went spot-formula |
| Wrong publication label / `valid_until` | `_extract_publication` (`dats24.py:440-442`), `parse_valid_until` (`_pdf.py:677`) | `TARIEFKAART <month> <year>` or the `GELDIG VAN` header changed |
| Values off by 100x | the per-column `/100.0` divisions in the DSO/energy/tax parsers | a c€/kWh column became EUR/kWh (or a EUR/yr column got divided) |
| `PDF layout parse error` / html-not-pdf | `_pdf.py:196-232`, `316-326` | the API returned HTML (endpoint moved) or an undecodable PDF |

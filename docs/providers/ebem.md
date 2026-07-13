# Provider: ebem

This document is the maintainer reference for the EBEM tariff-card extractor
(`providers/ebem.py`). EBEM (Ebem bvba, Merksplas) is a small Flemish supplier
serving the Mol / Geel area. It publishes monthly PDF tariff cards behind
opaque Umbraco CMS media URLs, keeps a public archive of past months on the
same listing page, and sells three residential electricity products across two
distinct PDF cards. Read this alongside the framework and pricing references:

- [../provider-framework.md](../provider-framework.md): the `SupplierExtractor`
  protocol, the `SupplierSnapshot` / `*Rates` dataclasses, the shared `_pdf`
  helpers, and the fetch / probe / `fetch_for_month` contracts.
- [../pricing-model.md](../pricing-model.md): how `compute_breakdown` consumes
  the energy, DSO, tax, and injection overlays this extractor produces.

## Overview

| Property | Value | Source |
| --- | --- | --- |
| Extractor id | `ebem` | `ebem.py:710` |
| Label | `EBEM` | `ebem.py:711` |
| Region(s) served | Flanders only | `_EBEM_REGIONS`, `ebem.py:707` |
| Publication form | Monthly PDF cards linked from one HTML listing page | `ebem.py:36` |
| Listing URL | `https://www.ebem.be/tarieven/` | `_LISTING_URL`, `ebem.py:91` |
| PDF base | `https://www.ebem.be` | `_PDF_BASE`, `ebem.py:92` |
| Archive | Yes, on the listing page (>= 6 months at last check) | `ebem.py:40` |
| Probe | HEAD the listing page (`Last-Modified` / `ETag`) | `probe`, `ebem.py:195` |

Every contract's `regions` is `frozenset({REGION_FLANDERS})`, so
`EXTRACTOR.regions()` is `{"flanders"}`. The `region` argument to `fetch`,
`fetch_for_month`, and `probe` is accepted but ignored (marked `# noqa: ARG001`)
because EBEM never sells outside Flanders. The registry must not advertise the
other regions, or the config flow would offer EBEM to households where every
fetch would 404 (`tests/test_ebem.py:80`).

Prices are published as PDF cards whose URLs are opaque Umbraco media-folder
hashes that change per file, so the URL cannot be constructed directly. Every
fetch scrapes the listing page and resolves the hash from the `<a href>`
(`ebem.py:36`). The link filename pattern is:

```
/media/<hash>/ebem_tariefkaart-<kind>-MM-YYYY.pdf
```

captured by `_PDF_RE` (`ebem.py:101`):

```python
r'href="(/media/[^"]+/ebem_tariefkaart-([a-z]+)[-_](\d{2})-(\d{4})\.pdf)"'
```

Groups are `(path, kind, MM, YYYY)`. Note `[-_]` between kind and MM: the
2026-01 dynamic file is named `ebem_tariefkaart-dynamic_01-2026.pdf` with an
underscore instead of a dash, so the regex accepts either separator or that
month would silently fall through to the coordinator proxy (`ebem.py:96`,
`tests/test_ebem.py:354`). The `kind` group is open-ended (`[a-z]+`) so a
future PDF kind surfaces in `discover()` even though only `elek` and `dynamic`
map to contract ids.

## Contracts

| Contract id | Label | Kind | PDF kind | Injection shape | quarter_hourly |
| --- | --- | --- | --- | --- | --- |
| `ebem_variable` | EBEM Groen Variabel | `variable` | `elek` | monthly indicative only | n/a |
| `ebem_basic_plus` | EBEM Groen B@sic+ | `variable` | `elek` | monthly indicative only | n/a |
| `ebem_dynamic` | EBEM Groen Dyn@mic | `dynamic` | `dynamic` | hourly factor*spot+base | True (15-min) |

Declared in `_CONTRACTS` (`ebem.py:120`) and turned into `Contract` objects in
the registry (`ebem.py:712`). None of the three sets `spot_indexed_injection`,
so all default to `False`.

- **Groen Variabel**: monthly RLP-weighted Belpex variable with a full
  per-meter-type energy split (mono, bi-hourly peak / off-peak, exclusive
  night). Injection settles at a monthly indicative.
- **Groen B@sic+**: an online-only, single-rate variant of the variable product
  living in the *same* `elek` PDF as Groen Variabel. One energy rate for all
  hours (no peak / off-peak / excl-night energy split), but an exclusive-night
  meter still bills the shared card's dedicated night fixed fee
  (`ebem.py:378`).
- **Groen Dyn@mic**: 15-minute Belpex spot dynamic, SMR3 (smart meter) only,
  with its own `dynamic` card. `quarter_hourly=True` (`ebem.py:366`) keeps the
  native 15-minute slots like Engie / Cociter rather than aggregating to hourly;
  see the `DynamicRates.quarter_hourly` docstring (`providers/base.py:140`).

Retired products: the variable card explicitly notes EBEM stopped selling fixed
contracts for now (`ebem.py:98`, `ebem.py:220`). No fixed contract is declared.
If EBEM revives one, its PDF kind (e.g. `fix`) surfaces verbatim from
`discover()` so live_check files a tracking issue (`ebem.py:237`).

## Fetch strategy

### `fetch` (`ebem.py:132`)

1. Look up the `_ContractDef` by id, raising `ExtractorError` on an unknown id.
2. `_find_latest(session, contract.pdf_kind)` (`ebem.py:242`) GETs the listing,
   filters `_PDF_RE` matches to the requested `pdf_kind`, sorts ascending by
   `(YYYY, MM)`, and returns the newest `(url, "YYYY-MM")`. It raises
   `ExtractorError` when no card of that kind is linked.
3. `fetch_pdf_text_layout` (`_pdf.py:315`) downloads the PDF and extracts
   layout-preserving text. This variant rejects CDNs that return HTTP 200 with
   `text/html` for a missing PDF (`_pdf.py:318`).
4. `parse_snapshot` (`ebem.py:259`) does the parsing.

Two of the three contracts (`ebem_variable`, `ebem_basic_plus`) share the same
`elek` PDF; the parser branches on `contract_id` when extracting the energy
block (`ebem.py:336`).

### `probe` (`ebem.py:195`)

HEADs the listing page via `head_freshness_key` (`_pdf.py:328`) and returns its
`Last-Modified` (preferred) or `ETag`. EBEM publishes a new monthly card by
editing the listing page (the opaque media-hash URL changes for every month),
so the listing's freshness header is the right key for *every* contract at once;
`probe` ignores `contract_id`. It returns `None` on any 4xx/5xx, network error,
or missing header, in which case the coordinator's time-based TTL takes over.

### `fetch_for_month` (`ebem.py:152`)

EBEM keeps an accessible public archive on the listing page, so past months can
be billed at their own rates instead of the current-snapshot proxy.

1. Look up the contract; return `None` on an unknown id.
2. GET the listing (`fetch_text`); return `None` on `ExtractorError`.
3. Build `target = (pdf_kind, "MM", "YYYY")` and find the first `_PDF_RE` match
   whose `(kind.lower(), MM, YYYY)` equals it; return `None` when the month is
   not published.
4. Resolve the URL, set `label = "YYYY-MM"`, download + parse; return `None` on
   `ExtractorError`.
5. Pass the result through `archive_validity_check` (`_pdf.py:734`).

`archive_validity_check` is called with `month_names=None`. When
`snap.valid_until` parsed, it rejects the snapshot if that date does not fall in
the requested month (a defence against a CDN-substituted current card
mis-billing past consumption). When `valid_until` is `None` and `month_names` is
`None` (the EBEM case), the textual fallback is skipped and the snapshot is
accepted on the strength of the URL resolver alone (`_pdf.py:766`). This is why
`_extract_validity` failing silently degrades the safety check, not the fetch.

`test_fetch_for_month_handles_underscore_separator` (`tests/test_ebem.py:354`)
documents the interaction: the 2026-01 dynamic URL resolves via the underscore
branch, but the mocked PDF text says `mei 2026`, so the cross-check returns
`None`. That is correct safety behaviour, not a bug.

### `discover` (`ebem.py:209`)

Returns the contract ids visible on the listing:

- any `elek` PDF surfaces both `ebem_variable` and `ebem_basic_plus` (same
  card),
- any `dynamic` PDF surfaces `ebem_dynamic`,
- `gas` / `aardgas` kinds (`_NON_ELEC_KINDS`, `ebem.py:109`) are dropped
  silently (electricity-only integration),
- any other kind is returned verbatim so live_check's catalog-drift alert
  files a tracking issue.

## Parsing

`parse_snapshot` (`ebem.py:259`) assembles a `SupplierSnapshot` from six
sub-parsers over the layout text. All rates are read from the card, never
hardcoded; the VAT multiplier is read from the card header too so a
regulator-driven rate change propagates without a code change (`ebem.py:338`).

| Field | Parser | Notes |
| --- | --- | --- |
| `energy` | `_extract_energy` (`ebem.py:336`) | branches on `contract_id` |
| `injection` | `_extract_injection` (`ebem.py:518`) | branches dynamic vs variable |
| `dsos` | `_extract_dsos` (`ebem.py:648`) | digital-meter table, optional analog prosumer |
| `taxes.federal_excise` + `energy_contribution` | `_extract_federal_taxes` (`ebem.py:587`) | |
| `taxes.flanders_renewables` | `_extract_flanders_renewables` (`ebem.py:608`) | |
| `valid_until` | `_extract_validity` (`ebem.py:298`) | printed Dutch month + year |

### Validity: `_extract_validity` (`ebem.py:298`)

EBEM cards have no `geldig` / `valable` validity keyword that the shared
`_pdf.parse_valid_until` would anchor on, so that helper returns `None`. The
card title ends with `<maand> <jaar>` (Dutch month name plus year), parsed
directly via `_DUTCH_MONTHS` (`ebem.py:295`) and returned as the last day of
that month (`calendar.monthrange`). Two hurdles:

- It matches case-insensitively (`mei 2026` and `Mei 2026`), because a future
  drift to title case would otherwise silently miss.
- It scans the first 600 chars for the first `word + year` token that is
  *actually a month*, rather than aborting on the first `word + 20\d\d`
  token: the dynamic card header carries a colliding `VERSIE 2026` token, and
  the month line's position after it is what keeps the parse working
  (`ebem.py:322`, `tests/test_ebem.py:60`).

### VAT convention

The registry convention is that stored snapshot values are VAT-inclusive
(`TaxOverlay.vat_rate=0.0` means "already VAT-incl"; `providers/base.py:465`).
EBEM cards print both ex-VAT and incl-VAT columns:

- `_vat_multiplier` (`ebem.py:322`) reads the percentage from the card header
  (`INCL. BTW N%` or `BTW N%`) via the shared `vat_multiplier` helper
  (`_pdf.py:392`), defaulting to 1.06.
- Dynamic energy factor / base are converted from the ex-VAT formula:
  `_formula_to_dynamic` (`ebem.py:330`) does `factor * vat * 10` and
  `base_cents * vat / 100`, identical to `cociter.py`'s dynamic conversion.
- Variable indicative rates, yearly fees, and the Flanders renewables total are
  read from the *incl-VAT* column directly so VAT is not double-applied.
- Injection is VAT-exempt (Belgian residential injection is never VAT-incl,
  `providers/base.py:216`), so injection factor / base skip the VAT multiplier.

## Energy formula per TariffKind

### Groen Dyn@mic (`DynamicRates`, `ebem.py:337`)

The dynamic card prints, for consumption:

```
alle uren 0,108 Belpex15' + 1,625
```

(offset illustrative, from the card / test at `ebem.py:339`,
`tests/test_ebem.py:254`). The regex anchors on a line starting with
`alle uren` that is NOT preceded by `injectie` (`(?<!injectie\s)`) so it does
not pick the injection row, and accepts any `SIGN_CHARS` between factor and base
so a re-render to a Unicode minus or a negative offset does not dead-end the
parser. Factor / base go through `_formula_to_dynamic` with the card VAT.
`quarter_hourly=True`. Illustrative from the test: `0.108 * 1.06 * 10 = 1.1448`
and `1.625 * 1.06 / 100 = 0.017225` (`tests/test_ebem.py:256`).

Yearly fee: `_extract_yearly_fee_abonnement` (`ebem.py:507`), the
`Abonnement ... €/jaar ... €/jaar` row (incl-VAT second column). Illustrative
`70` (`tests/test_ebem.py:260`).

### Groen B@sic+ (`VariableRates`, `ebem.py:369`)

Single rate for all hours. The formula row is
`Verbruik alle uren <factor> Belpex <sign> <base>` (`ebem.py:370`). The stored
`current` is the printed indicative from `_indicative_from_row`, not a
recomputed value. `yearly_fixed_fee` is the `Abonnement` row (illustrative `70`,
`tests/test_ebem.py:222`); `yearly_fixed_fee_exclusive_night` still comes from
the shared card's dedicated night fee (illustrative `35.04`) because an
exclusive-night meter bills that even though the energy is single-rate
(`ebem.py:378`). `peak` / `offpeak` / `exclusive_night` energy are left `None`.

### Groen Variabel (`VariableRates`, `ebem.py:399`)

Parses four meter-type formula rows into `parsed` (only used to build the
diagnostic `formula` string):

| Meter type | Row label |
| --- | --- |
| mono | `Enkelvoudige teller` |
| bi-hourly peak | `Dubbele teller piek` |
| bi-hourly off-peak | `Dubbele teller dal` |
| exclusive night | `Exclusief nacht` |

Each row is `<label> <factor> Belpex <sign> <base>`. A missing row raises
`ExtractorError` per label (`ebem.py:413`). The stored rates (`current`, `peak`,
`offpeak`, `exclusive_night`) are each the printed incl-VAT indicative from
`_indicative_from_row`, not recomputed. `yearly_fixed_fee` is the
`Vaste vergoeding (jaarlijkse ...)` row; `yearly_fixed_fee_exclusive_night` is
the `Vaste vergoeding exclusief nacht` row.

### `_indicative_from_row` (`ebem.py:447`)

EBEM variable cards print four numeric columns per row after the formula:
`EXCL.BTW`, `INCL.BTW 6%`, `GESCHATTE JAARPRIJS EXCL.BTW`, and
`GESCHATTE JAARPRIJS INCL.BTW 6%`. Columns 1 + 2 are the per-kWh indicative at
last month's Belpex; columns 3 + 4 use the VNR yearly-forecast Belpex. The
extractor surfaces column 2 (incl-VAT per-kWh at last month's Belpex) as
`current`, because it matches the value EBEM customers see on their bill more
faithfully than recomputing against a placeholder spot. The regex accepts any
`SIGN_CHARS` between `Belpex` and the offset: some months EBEM prints a
U+2212 minus / negative offset, which a literal `+` missed and failed the whole
snapshot on an otherwise valid card (`ebem.py:459`).

Illustrative indicatives from the test (`tests/test_ebem.py:93`): mono
`0.123363`, peak `0.132458`, off-peak / excl-night `0.113359` EUR/kWh; B@sic+
`0.121243`.

## DSO overlay: `_extract_dsos` (`ebem.py:648`)

Maps the eight Fluvius sub-areas via `_FLANDERS_LABELS` (`ebem.py:636`). Note
the card label to canonical DSO key mapping is not one-to-one on names:

| Card label | Canonical key |
| --- | --- |
| Fluvius Antwerpen | `DSO_FLUVIUS_ANTWERPEN` |
| Fluvius Halle Vilvoorde | `DSO_FLUVIUS_HALLE_VILVOORDE` |
| Fluvius Imewo | `DSO_FLUVIUS_IMEWO` |
| Fluvius Kempen | `DSO_FLUVIUS_IVEKA` |
| Fluvius Limburg | `DSO_FLUVIUS_LIMBURG` |
| Fluvius Midden-Vlaanderen | `DSO_FLUVIUS_INTERGEM` |
| Fluvius West | `DSO_FLUVIUS_WEST` |
| Fluvius Zenne-Dijle | `DSO_FLUVIUS_ZENNE_DIJLE` |

`Fluvius Kempen` maps to `fluvius_iveka` and `Fluvius Midden-Vlaanderen` maps
to `fluvius_intergem`; these are the two renames a future card drift is most
likely to break.

The parser anchors on the `DIGITALE METER` heading (`ebem.py:661`) and reads
each DSO row's four numbers into a `DsoOverlay`:

```
capacity (EUR/kW/jaar) | netkosten (c€/kWh) |
netkosten excl. nacht (c€/kWh) | tarief databeheer (EUR/jaar)
```

mapped to `capacity_eur_per_kw_year`, `distribution_single`,
`distribution_exclusive_night`, and `data_management_per_year` respectively
(the c€/kWh columns are divided by 100). `transport` is `0.0`.

The variable (`elek`) card additionally carries an `ANALOGE METER` table; when
present, the parser walks it to attach the fifth column as
`prosumer_eur_per_kva_year` (`ebem.py:668`). The dynamic card is SMR3-only, so
it has no analog table and the prosumer column stays `None`
(`tests/test_ebem.py:273`). A DSO row that fails to match is skipped, not
fatal (`ebem.py:687`). Illustrative for `fluvius_iveka`: capacity `59.58`,
`distribution_single 0.0634`, `distribution_exclusive_night 0.0566`,
`data_management_per_year 18.92`, prosumer `67.79` (`tests/test_ebem.py:156`).

## Tax overlay

`_extract_federal_taxes` (`ebem.py:587`) returns
`(federal_excise, energy_contribution)` in EUR/kWh:

- Federal excise: the residential `0-3 MWH` band (note the capital `MWH` on this
  row only, the other bands print lowercase `MWh`). Illustrative `0.050329`.
- Energy contribution: the value next to the `Beschermende klanten ... €0`
  residential energy-fund row. Illustrative `0.0020417`.

Both raise `ExtractorError` when the anchor row is missing.

`_extract_flanders_renewables` (`ebem.py:608`) combines
`Bijdrage groene stroom` + `Bijdrage WKK` into `flanders_renewables`. The card
prints both an ex-VAT total and an incl-VAT total; the parser anchors on
`Totale bijdrage` and reads the `<value> c€/kWh incl. BTW N%` figure (any 1-2
digit VAT percentage) so VAT is not double-applied and a future VAT change does
not fail the fetch. Anchoring on `Totale bijdrage` prevents a future footnote
printing another `incl. BTW N%` line from shadowing the value. Illustrative
`0.016112`. A layout drift that wipes the `incl. BTW N%` row is fatal
(`tests/test_ebem.py:292`), guarding against silently zeroing ~1.6 c€/kWh of
every bill.

The remaining `TaxOverlay` fields are zeroed because EBEM is Flanders-only:
`wallonia_renewables`, `brussels_renewables`, `region_connection_fee`,
`energy_fund_eur_per_month`, and `vat_rate` are all `0.0` (`ebem.py:281`,
`tests/test_ebem.py:136`). The residential energy-fund tariff is EUR 0; the
non-residential EUR 10.07/month tier is not modelled (`tests/test_ebem.py:140`).

## Injection: `_extract_injection` (`ebem.py:518`)

EBEM spans two of the three injection shapes in the taxonomy (see
[../pricing-model.md](../pricing-model.md)):

### Dynamic: hourly factor*spot+base (`ebem.py:520`)

`Belpex 15'` is a true hourly-spot formula. The row is
`injectie alle uren <factor> Belpex15' <sign> <base>`. It emits an
`InjectionRates` with `current=None`, `factor = factor_pdf * 10`, and
`base = base_cents / 100` (VAT-exempt, so no VAT multiplier). Illustrative from
the test: `0,0925 Belpex15' - 1,10` gives `factor 0.925`, `base -0.011`
(`tests/test_ebem.py:263`). Negative base is expected: a low spot can make a
producer pay to inject, and the pricing engine respects that.

### Variable / B@sic+: monthly indicative only (`ebem.py:548`)

Both `Variabel` and `B@sic+` settle injection at a MONTHLY rate: the factor /
base weight the monthly Belpex-SPP0 average, not the hourly spot. The card
prints the realized monthly indicative right after the formula (illustrative
`... Belpex - 1,25 1,3354 4,3252`, where `1,3354 = factor*last_month_SPP0 +
base` and `4,3252` is the VNR yearly forecast). The extractor surfaces *only*
that indicative as `current` and leaves `factor` / `base` `None`
(`tests/test_ebem.py:173`). Emitting factor / base would make the pricing engine
apply monthly coefficients to the hourly spot; this mirrors Ecofix Flexy's
BELPEX-SPP-M handling (`ebem.py:554`). The regex captures an optional fourth
group (the indicative), and a missing indicative is fatal
(`ebem.py:569`, `tests/test_ebem.py:180`).

There is no supplier-side PV forfait: `SupplierSnapshot.supplier_prosumer_eur_per_kva_year`
is left at its `None` default. The only prosumer figure is the DSO-side analog
rate on the variable card.

Every card carries an injection row, so a fully-absent row (e.g. a
capitalization re-render) raises `ExtractorError` for both the dynamic and the
variable branch rather than returning `None` and silently zeroing a prosumer's
feed-in credit (`ebem.py:529`, `ebem.py:562`, `tests/test_ebem.py:194`).

## Quirks and historical bugs

- **Two products, one PDF**: `ebem_variable` and `ebem_basic_plus` share the
  `elek` card; `parse_snapshot` branches by id and gives each its own energy
  rate and yearly fee, but their DSO and tax overlays are byte-identical
  (`tests/test_ebem.py:228`).
- **Underscore separator**: the 2026-01 dynamic file uses `dynamic_01-2026`
  (underscore), so `_PDF_RE` accepts `[-_]` between kind and MM (`ebem.py:96`).
- **Colliding `VERSIE 2026` token**: `_extract_validity` scans for the first
  real Dutch month rather than aborting on the first `word + year`
  (`ebem.py:298`).
- **VAT columns**: cards print both ex-VAT and incl-VAT; the extractor stores
  incl-VAT for consumption, ex-VAT-based factors are VAT-scaled, injection is
  never VAT-scaled (`providers/base.py:216`).
- **`MWH` vs `MWh` casing**: the residential federal-excise band is the only row
  with capital `MWH` (`ebem.py:592`).
- **Sign flexibility everywhere**: every formula and indicative regex accepts
  `SIGN_CHARS` (plus, hyphen, figure/en/em dash, U+2212) because supplier PDFs
  flip silently between them on re-render (`_pdf.py:500`). A literal `+` in
  `_indicative_from_row` previously failed valid cards that printed a negative
  offset (`ebem.py:459`).
- **Exclusive-night yearly fee**: both variable products surface a dedicated
  `yearly_fixed_fee_exclusive_night` billed instead of the standard fee on an
  exclusive-night meter, even B@sic+ which has no energy split (`ebem.py:378`).
- **Fixed contracts retired**: EBEM stopped selling fixed contracts; a revived
  `fix` PDF kind would surface from `discover()` for a tracking issue
  (`ebem.py:220`).
- **`fetch_for_month` validity cross-check**: a resolved past-month URL whose
  served PDF is actually the current card is rejected by `archive_validity_check`
  and falls back to the coordinator proxy (`ebem.py:192`).
- **Loud-fail policy**: missing renewables total, missing injection row, and
  missing monthly injection indicative all raise rather than silently zeroing a
  material part of the bill.

## Test fixtures

| Fixture | Variant | Used by |
| --- | --- | --- |
| `tests/fixtures/ebem_variable_2026-05.pdf` | `elek` card (Groen Variabel + B@sic+), May 2026 | most parse tests |
| `tests/fixtures/ebem_dynamic_2026-05.pdf` | `dynamic` card (Groen Dyn@mic), May 2026 | dynamic energy / injection / no-prosumer tests |
| `tests/fixtures/discover/ebem.html` | listing page snapshot | `fetch_for_month` + `discover` tests |

Test constants `_VARIABLE` / `_DYNAMIC` (`tests/test_ebem.py:52`); the listing
HTML is read at `tests/test_ebem.py:306`. Fixture text is loaded with
`layout=True` (`tests/test_ebem.py:56`) so it matches the production
`fetch_pdf_text_layout` extraction.

## When the card changes, look here

| Symptom | Function | Why it breaks |
| --- | --- | --- |
| Wrong / missing month, or archive proxy silently used | `_extract_validity` (`ebem.py:298`) | title format or a new colliding `word + year` token |
| Every fetch 404s or picks wrong month | `_PDF_RE` (`ebem.py:101`) / `_find_latest` (`ebem.py:242`) | filename pattern, separator, or kind token changed |
| Dynamic energy / injection missing | `_extract_energy` dynamic branch (`ebem.py:337`) / `_extract_injection` dynamic (`ebem.py:520`) | `alle uren` / `Belpex15'` label or spacing drift |
| Variable rate / indicative wrong | `_indicative_from_row` (`ebem.py:447`) | column order / count or sign changed |
| Variable formula row not found | row regexes (`ebem.py:403`) | meter-type label wording changed |
| Yearly fees wrong | `_extract_yearly_fee_variable` / `_excl_night` / `_abonnement` (`ebem.py:476`) | `Vaste vergoeding` / `Abonnement` label changed |
| A DSO drops out or a rate shifts | `_extract_dsos` (`ebem.py:648`) / `_FLANDERS_LABELS` (`ebem.py:636`) | Fluvius label rename, table heading, or column order |
| Taxes zeroed or wrong | `_extract_federal_taxes` (`ebem.py:587`) / `_extract_flanders_renewables` (`ebem.py:608`) | `0-3 MWH` casing, `Beschermende klanten`, or `Totale bijdrage` row drift |
| Monthly injection indicative missing (fatal) | `_extract_injection` variable branch (`ebem.py:556`) | card stopped printing the realized indicative column |

Regenerate the fixtures from the current card before adjusting a regex, and keep
the test-encoded illustrative values in sync with the new card.

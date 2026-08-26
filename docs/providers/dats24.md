# Provider: dats24

This document is the maintainer reference for the DATS 24 supplier extractor
(`providers/dats24.py`). DATS 24 is the fuel-and-energy retail brand of Colruyt
Group; it sells one residential electricity product across Flanders and Wallonia
and publishes its rates as one PDF per month on the Colruyt Group static CDN. The
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
| Extractor id | `dats24` (`dats24.py:523`) |
| Label | `DATS 24` (`dats24.py:524`) |
| Regions served | Flanders, Wallonia (`dats24.py:520`, `_DATS24_REGIONS`) |
| Products | one: `dats24_groen_variabel` (`dats24.py:111`) |
| Card format | one PDF per month on a CDN, month spelled in the filename |
| Probe | none (`EXTRACTOR` sets no `probe`, `dats24.py:507-524`) |
| Archive | none (no `fetch_for_month`); past months fall back to current snapshot |
| Lifecycle | withdrawn: contracts transfer to EnergyVision on 2026-08-31 |

DATS 24 sells one residential electricity product, "Elektriciteit Groen
Variabel" (`dats24.py:26-58`). It is a variable (monthly-indexed) contract whose
formula shape is (the per-meter-type coefficients are re-published with every
card, so they are deliberately not pinned here or in the module docstring):

```
afname        = (BE_spotRLP * <factor> + 0.511) * 1.06   c€/kWh
teruglevering = (BE_spotSPP * <factor> - 1.11)           c€/kWh (VAT-exempt)
```

`BE_spotRLP` is a monthly Belgian spot index (quarter-hourly spot, RLP-weighted);
`BE_spotSPP` is a monthly synthetic-profile index. Both are monthly, not the
hourly ENTSO-E day-ahead spot, which is why the extractor surfaces printed
indicative values rather than a spot formula (see Injection below).

> **Withdrawal.** DATS 24 is leaving residential energy supply: its own site
> states contracts transfer automatically to EnergyVision on 31 August 2026, so
> the August 2026 card is expected to be the last one published
> (`dats24.py:41-44`). The successor products live in
> [energyvision.md](./energyvision.md). After the transfer the CDN stops
> publishing and `discover` goes empty rather than raising.
>
> This is declared on the registry entry as `deprecated_until=date(2026, 8, 31)`
> and `deprecated_successor="energyvision"` (`dats24.py:522-523`), which drops
> DATS 24 from the config flow's new-setup and compare pickers and raises the
> `supplier_deprecated` Repairs card on every entry still using it. Existing
> entries keep pricing normally -- see
> [../provider-framework.md](../provider-framework.md) for what the two fields
> do and do not affect.

### Source URL

Each month's card has its own CDN URL, built by `_card_url`
(`dats24.py:103-103`) from `_CDN_BASE` (`dats24.py:103`):

```
https://api.colruytgroup.com/api/static/dats24/parameters/site
    /<YYYY>/ELEK/NL/Elektriciteit%20Groen%20Variabel%20-%20Versie%20<MM>%20<YYYY>.pdf
```

This replaced `profile.dats24.be/api/v1/ratecard?...`, which began answering
every request (every contract type, both languages, gas as well as electricity)
with HTTP 500 on 2026-07-29 and is not coming back. The CDN file is the same
document the API served: the CDN's April 2026 PDF is byte-identical to
`tests/fixtures/dats24_groen_variabel_apr.pdf`.

Nothing in the URL varies by region or meter type: one document per month carries
every region's DSO tables, tax block, and that month's indicative rates, and
`parse_snapshot` slices out the requested region's overlays. Only the month
varies, which is what the resolver below handles.

### Month resolution

`_fetch_card` (`dats24.py:179-199`) asks for the month being billed and falls
back exactly one month:

```
_card_months()                dats24.py:156   (this month, previous month) in Brussels local time
  ├─ GET _card_url(this month)
  │    └─ 200 -> (url, text)
  └─ HTTP 404 / 410 -> GET _card_url(previous month)
```

Two deliberate choices:

- **Brussels local anchor** (`dats24.py:163-165`), matching `bolt.py:157-166`: a
  UTC anchor still names last month during the first two hours of every Belgian
  month and would fetch a card that has just been superseded.
- **Only an absent card triggers the fallback** (`_card_absent`,
  `dats24.py:168-176`). A timeout, a 5xx or an unreadable payload propagates, so
  the coordinator classifies it transient and keeps serving its cached
  current-month snapshot. Falling back on any error would silently re-price
  every user at last month's rates, which is worse than a deferred refresh. This
  is stricter than `bolt.py:389-433`, which falls back on any `ExtractorError`
  because Bolt's cards expose no parseable `valid_until` to signal the swap.

## Contracts

| id | label | kind | regions | spot_indexed_injection |
|---|---|---|---|---|
| `dats24_groen_variabel` | `DATS 24 Elektriciteit Groen Variabel` | `variable` | Flanders, Wallonia | False (default) |

Only one `Contract` is declared (`dats24.py:527-534`). There is no fixed, TOU, or
dynamic product, so `quarter_hourly` does not apply (that flag lives on
`DynamicRates`, not on a variable contract). `spot_indexed_injection` is left at
its default `False`: that flag means a PER-HOUR spot formula, and this injection
is month-indexed. An ENTSO-E key still improves it, since the delivery month's
SPP-weighted mean is resolved from the spot cache, but the contract prices
without one by falling back to the card's printed figure. See [../provider-framework.md](../provider-framework.md) for what
`spot_indexed_injection` gates in the config flow.

`fetch` rejects any other contract id with `ExtractorError` (`dats24.py:187-199`)
and rejects Brussels or any non-FL/WA region (`dats24.py:209-212`): DATS 24 does
not sell residential electricity outside Flanders and Wallonia.

## Fetch strategy

```
fetch(session, contract_id, region)          dats24.py:202
  ├─ reject unknown contract id                dats24.py:207
  ├─ reject region not in {flanders, wallonia} dats24.py:209
  ├─ _fetch_card(session)                      dats24.py:213   (month resolution -> (url, text))
  │    └─ fetch_pdf_text_layout(session, url)  dats24.py:189   (PDF -> pdfplumber layout text)
  └─ parse_snapshot(text, url, region)         dats24.py:214
```

`fetch` downloads the PDF and runs it through `fetch_pdf_text_layout`
(`_pdf.py:334`), the layout-preserving pdfplumber path. Layout mode is required
because the card lays out DSO and tax data as multi-column tables; the plain text
extractor would collapse the columns. `fetch_pdf_text_layout` also guards against
CDNs that return HTTP 200 `text/html` for a missing PDF and rejects a
pages-present-but-no-text document as a hard error (`_pdf.py:337-344`,
`extract_pdf_text_layout` at `_pdf.py:311-332`).

### Probe

There is no `probe`. `EXTRACTOR` (`dats24.py:507-524`) sets only `id`, `label`,
`contracts`, and `fetch`; `probe` and `fetch_for_month` default to `None`. The
month-keyed URL is not a freshness signal either: within a month the file is
replaced in place, so a HEAD tells us nothing a cheap diff could use, and the
coordinator's time-based TTL governs refresh. This is the "DATS 24 single-PDF"
case called out in the `SnapshotProbe` contract comment in `base.py:909-909`.

Note the module does define a `discover` coroutine (`dats24.py:202-215`), but it
is a catalog-drift check for the live-check harness, not a coordinator probe. It
HEADs both candidate months and returns `{_CONTRACT_ID}` on the first status
below 400, `set()` otherwise. Accepting either month matters on the 1st of a
month, before the new card is up. The drift it is meant to catch is publication
stopping altogether, which is now the expected end state after the 2026-08-31
transfer; if DATS 24 ever split the product into `vast` / `tou` variants the
check would stay green so we notice via a real extractor failure rather than a
false-positive new-product alert (`dats24.py:218-226`).

### Archive / historical months

There is no `fetch_for_month`, so past months fall back to the current snapshot
as a proxy in the yearly-cost backfill flow. Note that the CDN *does* retain
per-month cards back to 2023, so unlike the old overwrite-in-place API URL an
archive fetcher is now technically possible (DATS 24 is listed as API-only in the
`ArchivedSnapshotFetcher` comment `base.py:917-919`, which that change would need
to correct). It is deliberately not implemented: the product ends on 2026-08-31,
so the payoff would be one month of more accurate YTD backfill.

## Parsing

`parse_snapshot` (`dats24.py:218-230`) is a pure function exposed for unit tests
(the test harness feeds it fixture text directly, never the network). It builds a
`SupplierSnapshot` from five sub-parsers plus two shared helpers:

| Snapshot field | Parser | Source |
|---|---|---|
| `energy` | `_extract_energy` | `dats24.py:236-277` |
| `dsos` | `_extract_dsos` (dispatches per region) | `dats24.py:283-288` |
| `taxes` | `_extract_taxes` | `dats24.py:371-442` |
| `injection` | `_extract_injection` | `dats24.py:463-506` |
| `publication_label` | `_extract_publication` | `dats24.py:519-521` |
| `valid_until` | `parse_valid_until` (shared) | `_pdf.py:947` |
| `supplier` / `contract` | literals | `dats24.py:221-222` |

Every numeric value is parsed with `to_float` (`_pdf.py:615-626`), which strips
Unicode thousands separators and accepts both the Belgian comma decimal and a dot
decimal. This dot tolerance is not cosmetic: the May 2026 card switched its
separator from `,` to `.` (see Quirks).

### Energy (`_extract_energy`, `dats24.py:236-277`)

The card prints four indicative TVAC c€/kWh values on one row under `Afname1`
(single/mono, bi-hourly day, bi-hourly night, exclusive-night), plus a yearly
standing charge. The extractor uses these printed indicatives directly rather than
re-solving the formula, because spot data is not available at parse time and the
printed values are exactly what the monthly invoice settles at (`dats24.py:252-260`).

Layout the regex targets (illustrative comment values, `dats24.py:262-265`):

```
Afname1 (c€/kWh) 12,18 13,48 10,97 10,97
                 single  Day   Night Excl-night
```

- Afname row regex: `Afname1?\s*\(c€/kWh\)\s+(...)` four capture groups
  (`dats24.py:269-272`). A miss raises `could not parse DATS 24 indicative afname
  row` (`dats24.py:273-274`).
- Yearly fee regex: `VASTE VERGOEDING\s*\(€/jaar\)\s+(...)` (`dats24.py:279`). A
  miss raises `could not parse DATS 24 yearly fixed fee` (`dats24.py:280-284`);
  the standing charge is mandatory on every card, so a miss is layout drift, not a
  fee-free contract.

Output is `VariableRates(current, peak, offpeak, exclusive_night,
yearly_fixed_fee)` with the four c€/kWh values divided by 100 (`dats24.py:286-292`).
All four include 6% VAT. Illustrative parse from the April fixture
(`test_dats24.py:104-109`): `current 0.1218`, `peak 0.1348`, `offpeak 0.1097`,
`exclusive_night 0.1097`, `yearly_fixed_fee 38.50` EUR/yr.

### DSO overlay

`_extract_dsos` dispatches on region (`dats24.py:283-288`). It returns `{}` for
any region other than Flanders or Wallonia (defensive; `fetch` already rejects
those).

#### Flanders (`_extract_flanders_dsos`, `dats24.py:291-326`)

Iterates the eight Fluvius sub-areas in `_FLANDERS_DSOS` (`dats24.py:109-109`),
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
(`dats24.py:309-313`):

```
cap_digital | afname_dig | afname_dig_excl_nacht | max_tarief
cap_classical | afname_class | afname_class_excl_nacht | prosumer
meteropname_kwartier | meteropname_jaarlijks
```

The extractor models only the digital-meter path (post-2024 Fluvius rollout
target); the four classical/analog numbers are ignored. It fills `DsoOverlay` with
(`dats24.py:315-325`): `distribution_single` = col 2 /100, `distribution_exclusive_night`
= col 3 /100, `transport` = 0.0 (rolled into Fluvius distribution on this card),
`capacity_eur_per_kw_year` = col 1 (the digital capacity term, EUR/kW/yr, NOT
divided by 100), `data_management_per_year` = col 10 (jaarlijks meteropname). A
row that does not match is skipped (`continue`, `dats24.py:313-314`), not fatal, so
a partial card still yields the DSOs it could parse. Illustrative Antwerpen values
(`test_dats24.py:209-214`): capacity 52.37 EUR/kW/yr, distribution 5.35 c€/kWh,
data-management 18.92 EUR/yr.

#### Wallonia (`_extract_wallonia_dsos`, `dats24.py:329-365`)

Iterates `_WALLONIA_DSOS` (`dats24.py:115-121`), an ordered tuple:

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
Wallon row (`dats24.py:126-129`, `359-365`). The regex is anchored at start of
line with `re.escape(label)`, so `ORES (Brabant Wallon)` only matches that one row.

Ten columns per row (`dats24.py:347-350`):

```
single | day | night | PIC | MEDIUM | ECO | excl_nacht
transport | data-beheer (€/yr) | prosumer (€/kVA/yr)
```

Mapped to `DsoOverlay` (`dats24.py:310-320`): `distribution_single` (col1/100),
`distribution_peak` (col2/100), `distribution_offpeak` (col3/100),
`distribution_pic` (col4/100), `distribution_medium` (col5/100),
`distribution_eco` (col6/100), `distribution_exclusive_night` (col7/100),
`transport` (col8/100, c€/kWh), `data_management_per_year` (col9, EUR/yr),
`prosumer_eur_per_kva_year` (col10, EUR/kVA/yr). The PIC/MEDIUM/ECO columns are the
Wallonia Tarif Impact CWaPE bands; they are carried on the DSO overlay even though
DATS 24's own product is not an Impact contract, because the DSO overlay is shared
across the pricing engine. There is no `capacity_eur_per_kw_year` on the Walloon
rows (Wallonia has no capacity term).

Illustrative ORES values (`test_dats24.py:227-236`): single 11.98, day 13.27,
night 7.39, PIC 16.57, medium 10.83, eco 5.09 c€/kWh, transport 2.74 c€/kWh, data
14.10 EUR/yr, prosumer 85.84 EUR/kVA/yr. RESA is deliberately distinct (single
11.06, prosumer 84.22, `test_dats24.py:239-246`), which guards against a regex that
would silently align all Walloon DSOs to one row.

### Taxes (`_extract_taxes`, `dats24.py:371-442`)

Federal levies are region-agnostic; regional renewables and fees are gated by
`region` so a Flanders user never accrues the Walloon connection fee and a
Wallonia user never accrues the Flemish Energiefonds (`dats24.py:396-401`).

Federal (both regions, mandatory, raise on miss `dats24.py:407-408`):

- `energy_contribution`: `Energiebijdrage\s+(...)\s*c€/kWh` (`dats24.py:403`) /100.
- `federal_excise`: `Verbruik tussen 0 kWh en 3\.000 kWh\s+(...)\s*c€/kWh`
  (`dats24.py:404-406`) /100. This is the lowest excise band (0-3000 kWh).

Flanders-only (`dats24.py:415-432`):

- `flanders_renewables` = GSC + WKC: `Vlaams Gewest:\s*GSC\s*\(c€/kWh\)\s+(...)`
  and `WKC\s*\(c€/kWh\)\s+(...)` summed, each /100. Both are mandatory and always
  printed together; either miss raises `DATS 24: Flanders GSC/WKC renewables not
  found` (`dats24.py:418-425`). GSC is the dominant half (the comment cites 1,183
  vs 0,378 c€/kWh, illustrative), so silently zeroing a missed GSC would under-bill
  by ~1.2 c€/kWh.
- `energy_fund_eur_per_month`: `Hoofdverblijf\s*\(domicilie\)\s+(...)\s*€/maand`
  (`dats24.py:429-432`). This one is NULLABLE: if the row is absent it defaults to
  0.0 rather than raising. The residential default is 0 (`test_dats24.py:127-129`);
  second-home users override in the OptionsFlow.

Wallonia-only (`dats24.py:433-447`):

- `wallonia_renewables` = CV: `Waals Gewest:\s*CV\s*\(c€/kWh\)\s+(...)` /100.
- `region_connection_fee`: `Aansluitingsvergoeding\s+Walloni[eë]\d*\s+(...)\s*c€/kWh`
  /100. The `\d*` tolerates a footnote digit that the layout-aware text glues onto
  the word `Wallonië` (`dats24.py:435-441`). Both are mandatory; either miss raises
  `DATS 24: Wallonia CV / connection fee not found` (`dats24.py:442-445`).

`TaxOverlay` sets `vat_rate=0.0` (`dats24.py:441`): all card values are already
TVAC (6% VAT), so `compute_breakdown` must not re-scale them. The card footer reads
`Alle prijzen ... inclusief 6% btw, tenzij anders vermeld`; the two exceptions
tagged `Niet aan btw onderworpen` (the Walloon connection fee and the Flemish
Energiefonds) happen to use the same per-kWh / per-month conventions, so they slot
in without conversion (`dats24.py:389-394`). Illustrative April values: Flanders
renewables 0.01561, Wallonia renewables 0.03032, connection fee 0.00075 EUR/kWh
(`test_dats24.py:124`, `138-139`).

### Injection (`_extract_injection`, `dats24.py:448-491`)

Injection shape: **month-indexed on Belpex-SPP**. DATS 24 settles teruglevering
on `BE_spotSPP`, a monthly synthetic index, not the hourly day-ahead spot. The
card prints a figure right after the formula and says which month it came from:
*"de terugleveringsvergoeding wordt verkregen door de MEEST RECENTE waarde van
BE_spotSPP (maart 2026: 57,11 EUR/MWh) in te vullen in de tariefformule"* on the
April card (`dats24.py:466-470`):

```
formula:    (BE_spotSPP x 0,0766 - 1,11)   c€/kWh, VAT-exempt
indicative: Teruglevering2 (c€/kWh) 3,26
```

So the printed `3,26` is MARCH's index, and crediting it credits last month's.
April's own `BE_spotSPP` was 27,95, worth 1,0310 c/kWh, so the printed figure
paid more than three times what April owed.

The extractor surfaces the coefficients with `spp_indexed=True`
(`dats24.py:488-491`), which routes them to the delivery month's own
solar-weighted mean, the same one the coordinator computes from the Synergrid
profile, and keeps them off the per-hour path. The card's SPP is Synergrid's, so
the two are the same index. `current` remains as the fallback for an entry with
no profile, and the `formula` string is still retained verbatim for diagnostics
(`test_injection_formula_text_retained_for_any_operator`, `test_dats24.py:158`,
proves a `+` operator parses into a positive base and is kept as text too).

Two hard invariants encoded in tests:

- **Flanders-only.** Returns `None` in Wallonia (`dats24.py:486-487`): the card
  footnote reserves the teruglevering tariff to Flemish digital-meter customers, so
  a Walloon prosumer accrues no feed-in credit and the shared card's indicative must
  not be surfaced for them (`test_injection_is_flanders_only`,
  `test_dats24.py:173-182`).
- **Negative-safe sign parsing.** The indicative regex captures an optional leading
  sign, `Teruglevering2?\s*\(c€/kWh\)\s+([SIGN_CHARS]?)\s*(...)` (`dats24.py:491-493`),
  and applies `parse_sign` (`_pdf.py:660`). When `BE_spotSPP` is low the monthly
  indicative goes negative (the producer pays to inject); an earlier version without
  the sign group silently dropped the credit (`dats24.py:488-490`,
  `test_injection_indicative_handles_negative_value` `test_dats24.py:185-203`, which
  also checks a Unicode-minus glyph). A miss (no indicative at all) raises `DATS 24
  injection: monthly indicative missing` (`dats24.py:494-498`).

One shared teruglevering value covers all three meter types (single, bi-hourly day,
bi-hourly night), so a single `InjectionRates` entry serves everyone
(`dats24.py:482-484`).

There is no supplier-side prosumer/PV forfait on DATS 24 (`supplier_prosumer_eur_per_kva_year`
is left unset). The Walloon DSO prosumer term (`prosumer_eur_per_kva_year`) is the
only prosumer charge, and it lives on the DSO overlay, not the supplier snapshot.

### Publication label (`_extract_publication`, `dats24.py:519-521`)

`TARIEFKAART\s+(\w+\s+20\d{2})` case-insensitive, lowercased. Illustrative:
`april 2026` (`test_dats24.py:91`), `mei 2026` (`test_dats24.py:257`). Empty string
on miss (non-fatal). `valid_until` is parsed separately by the shared
`parse_valid_until` (`_pdf.py:947`), which catches the explicit `GELDIG VAN 1 APRIL
2026 T.E.M 30 APRIL 2026` header (`test_dats24.py:92-94`, expects `date(2026, 4, 30)`).

## Quirks and historical bugs

These are the land mines a future maintainer must know, each traceable to a source
comment or test:

1. **The API source is dead; the CDN one is month-keyed.** The original source was
   `profile.dats24.be/api/v1/ratecard?...` -- a JSON-looking URL that actually
   returned a PDF, and stable enough that the extractor hardcoded it. On
   2026-07-29 it began answering HTTP 500 to everything and did not recover, which
   is what forced the move to the per-month CDN URL (`dats24.py:46-51`). Two
   consequences: the URL is now computed, not constant (so `snapshot.source_url`
   varies by month), and a fetch failure must be classified before falling back --
   see `_card_absent` (`dats24.py:168-176`).
2. **All values are TVAC; `vat_rate=0.0`.** The card is 6% VAT-inclusive except two
   `Niet aan btw onderworpen` lines that still use per-kWh/per-month conventions
   (`dats24.py:389-394`, `456`). Do not add VAT scaling in the pricing engine.
3. **Decimal separator flipped between months.** The May 2026 card switched from
   comma to dot (`Afname1 10.64 11.77 ...` instead of `12,18 13,48 ...`). All
   regexes use the `[\d,.]+` class and delegate to `to_float`, which handles both;
   a comma-only regex would raise `could not parse DATS 24 indicative afname row`
   (`test_may_card_uses_dot_decimal_separator`, `test_dats24.py:249-274`). June
   reverted to commas.
4. **Seven ORES sub-areas collapse to one key.** Only the `ORES (Brabant Wallon)`
   row is kept (`dats24.py:126-129`, `359-365`,
   `test_april_card_wallonia_dsos_collapse_seven_ores_subareas_to_one`).
5. **Label renames KEMPEN->iveka, MIDDEN-VLAANDEREN->intergem** in the Flanders map
   (`dats24.py:119`, `121`).
6. **Digital-meter-only modeling.** Both DSO parsers read only the digital-meter
   columns; the analog/classical columns are intentionally ignored
   (`dats24.py:315-317`).
7. **Flanders capacity is not /100.** `capacity_eur_per_kw_year` and both
   `data_management_per_year` and `prosumer_eur_per_kva_year` are raw EUR values;
   only the c€/kWh distribution and transport columns are divided by 100. Mixing
   these up mis-scales by 100.
8. **Injection is Flanders-only and monthly.** Never emit factor/base; never surface
   a credit in Wallonia (`dats24.py:472-487`). See Injection above.
9. **Negative injection indicative.** Keep the optional sign group and `parse_sign`
   (`dats24.py:488-493`).
10. **Fatal-vs-nullable asymmetry.** Afname row, yearly fee, federal excise/contribution,
    Flanders GSC+WKC, Wallonia CV+connection fee, and the injection indicative all
    raise on miss. The Flemish Energiefonds `Hoofdverblijf (domicilie)` row and the
    publication label are nullable (default 0 / empty). This split is deliberate:
    mandatory charges must fail loud rather than silently under-bill.
11. **`discover` is a catalog check, not a probe.** It stays green if DATS 24 adds a
    second contract type; that is by design (`dats24.py:218-226`).
12. **May fixture excise artifact.** The hand-built dot-decimal May fixture left the
    excise row as a stray `000,005 c€/kWh` that cannot be patched (non-contiguous in
    the compressed stream, the real May card is gone). The May test intentionally
    does not value-assert taxes; tax extraction is covered by the April fixture and
    dot-decimal numeric parsing by the May energy asserts. Do not "fix" the fixture
    by asserting the stray value (`test_dats24.py:264-274`).

## Test fixtures

Under `tests/fixtures/`, exercised by `tests/test_dats24.py`:

| Fixture | Card variant | Exercises |
|---|---|---|
| `dats24_groen_variabel_apr.pdf` | April 2026 card, comma decimals | the default fixture (`test_dats24.py:55`); all value asserts (energy, taxes, both region DSO blocks, injection, publication metadata) and every fatal-miss test |
| `dats24_groen_variabel_may.pdf` | May 2026 card, dot decimals | dot-decimal tolerance across energy + DSO parsing (`test_dats24.py:249-274`); taxes deliberately not asserted (fixture artifact) |

The tests read fixtures via `fixture_text(name, layout=True)`, matching the
production `fetch_pdf_text_layout` path, and call `parse_snapshot` directly (no
network). `parse_snapshot` and `_extract_injection` are imported directly, so the
pure parsers are the unit under test.

## When the card changes, look here

| Symptom | Likely function | Why |
|---|---|---|
| `could not parse DATS 24 indicative afname row` | `_extract_energy` (`dats24.py:269-274`) | the `Afname1 (c€/kWh)` label, column count, or separator changed |
| `could not parse DATS 24 yearly fixed fee` | `_extract_energy` (`dats24.py:236-277`) | `VASTE VERGOEDING (€/jaar)` label moved |
| A Flanders DSO silently missing from `snapshot.dsos` | `_extract_flanders_dsos` / `_FLANDERS_DSOS` (`dats24.py:291-326`, `115-124`) | a Fluvius label was renamed (row skipped on no-match) or the 10-column layout changed |
| A Walloon DSO missing, or all sharing one row | `_extract_wallonia_dsos` / `_WALLONIA_DSOS` (`dats24.py:329-365`, `130-136`) | `ORES (Brabant Wallon)` / `RÉGIE DE WAVRE` label drift, or column reorder |
| `DATS 24: Flanders GSC/WKC renewables not found` | `_extract_taxes` (`dats24.py:416-425`) | the fragile `Vlaams Gewest: GSC` / `WKC` prefixes changed |
| `DATS 24: Wallonia CV / connection fee not found` | `_extract_taxes` (`dats24.py:371-442`) | `Waals Gewest: CV` or the `Aansluitingsvergoeding Wallonië` footnote changed |
| `could not parse DATS 24 federal tax block` | `_extract_taxes` (`dats24.py:403-408`) | `Energiebijdrage` or `Verbruik tussen 0 kWh en 3.000 kWh` moved |
| `DATS 24 injection: monthly indicative missing` | `_extract_injection` (`dats24.py:448-491`) | the `Teruglevering2 (c€/kWh)` label changed, or the card went spot-formula |
| Wrong publication label / `valid_until` | `_extract_publication` (`dats24.py:519-521`), `parse_valid_until` (`_pdf.py:947`) | `TARIEFKAART <month> <year>` or the `GELDIG VAN` header changed |
| Values off by 100x | the per-column `/100.0` divisions in the DSO/energy/tax parsers | a c€/kWh column became EUR/kWh (or a EUR/yr column got divided) |
| `PDF layout parse error` / html-not-pdf | `_pdf.py:244-251`, `334-344` | the CDN returned HTML (file moved) or an undecodable PDF |

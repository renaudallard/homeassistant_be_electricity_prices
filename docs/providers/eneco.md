# Provider: eneco

This document is the maintenance reference for the Eneco extractor
(`providers/eneco.py`). Eneco publishes one VAT-inclusive PDF tariff card per
electricity contract on its own CDN, with the current URL advertised on a public
listing page whose issue number rotates monthly. The extractor scrapes that
listing to resolve the live PDF, downloads it, and parses the energy formula, the
Flanders (Fluvius) and Wallonia DSO overlays, the federal and regional tax block,
and the injection block into a `SupplierSnapshot`. This page maps every parse
helper to the card region it reads, and flags the historical bugs whose fixes the
code still guards against. Read the source alongside it: the test module
(`tests/test_eneco.py`) pins the expected parse output against real fixtures and is
the ground truth for what the extractor must produce.

Related reading:

- [../provider-framework.md](../provider-framework.md): the `SupplierExtractor`
  protocol, the shared dataclasses (`FixedRates`, `VariableRates`, `DynamicRates`,
  `DsoOverlay`, `TaxOverlay`, `InjectionRates`), and the PDF helpers in `_pdf.py`.
- [../pricing-model.md](../pricing-model.md): how `compute_breakdown` consumes the
  snapshot (energy formula, network overlay, taxes, injection credit).

## Overview

| Field | Value | Source |
| --- | --- | --- |
| Extractor id | `eneco` | `eneco.py:645` |
| Label | `Eneco` | `eneco.py:646` |
| Regions served | Flanders and Wallonia (no Brussels) | `eneco.py:642`, module docstring `eneco.py:38` |
| Publication | one PDF per contract on the Eneco CDN | `eneco.py:28-36` |
| VAT convention | prices are VAT-inclusive (6 %), so `TaxOverlay.vat_rate = 0.0` | `eneco.py:36`, `eneco.py:560` |
| Probe | listing scrape returning the resolved PDF URL | `eneco.py:210-228` |
| Archive | per-month issues kept on the CDN, resolved by volume walk | `eneco.py:164-207` |

`EXTRACTOR.regions()` (the union over its contracts, `base.py:590`) is
`{flanders, wallonia}`. Power Fix and Power Flex cover both regions; Power Dynamic
is Flanders-only (see the contracts table). Brussels (Sibelga) is never served, so
`TaxOverlay.brussels_renewables` stays 0 and no Sibelga overlay is emitted.

### Source URL pattern

Cards live under a fixed CDN base with a rotating 6-digit issue number
(`eneco.py:30-34`, `eneco.py:91`):

```
https://cdn.eneco.be/downloads/nl/general/tk/BC_032_<ISSUE>_NL_ENECO_POWER_<NAME>.pdf
```

`<NAME>` is the product slug (`FIX`, `FLEX`, `DYNAMIC`, `eneco.py:101-105`).
`<ISSUE>` is `<VOL><YY><MM>`: a 2-digit volume (usually `01`, higher on re-issues)
followed by the 2-digit year and month. Stale issues stay served, so the live URL
is never hardcoded; it is resolved from the listing page (`eneco.py:33-35`):

```
https://eneco.be/nl/elektriciteit-gas/tariefkaarten
```

## Contracts

| id | label | kind | regions | source line |
| --- | --- | --- | --- | --- |
| `power_fix` | Eneco Zon & Wind Vast | `fixed` | flanders, wallonia | `eneco.py:670-678` |
| `power_flex` | Eneco Zon & Wind Flex | `variable` | flanders, wallonia | `eneco.py:679-685` |
| `power_dynamic` | Eneco Zon & Wind Dynamisch | `dynamic` | flanders only | `eneco.py:686-697` |

Notes:

- **Power Fix** is a fixed contract with a single rate plus a bi-hourly (day /
  night) split and a dedicated exclusive-night circuit rate (`_extract_fixed`,
  `eneco.py:309-327`).
- **Power Flex** is a variable (monthly-indexed) contract: the card prints the
  current month's effective rate and a monthly Belpex indexation formula
  (`_extract_variable`, `eneco.py:330-376`).
- **Power Dynamic** is an hourly dynamic contract indexed on the hourly spot
  (Belpex-H). It is Flanders-only: the card reads "voor Vlaanderen" / "in
  Vlaanderen" and requires a Flemish SMR3 digital meter, whereas Fix and Flex
  cover both regions. The Walloon DSO rows on the Dynamic card are vestigial
  reference and must not be offered in Wallonia (`eneco.py:661-670`, enforced by
  `test_power_dynamic_offered_in_flanders_only`, `tests/test_eneco.py:55-62`).
- `DynamicRates.quarter_hourly` is left at its default `False` (`_extract_dynamic`
  returns a `DynamicRates` without setting it, `eneco.py:415-419`). Eneco Dynamic
  bills per clock hour, so the integration aggregates the ENTSO-E 15-minute curve
  to hourly (`base.py:140-154`).
- No contract sets `spot_indexed_injection`; it stays `False` on all three
  (`Contract` default, `base.py:64`). See the injection section for why Fix / Flex
  never surface spot coefficients.

## Fetch strategy

### `fetch` (current card)

`fetch` (`eneco.py:146-161`) validates the contract id, scrapes the listing,
resolves the live PDF URL, downloads and extracts its text, then delegates to
`parse_snapshot`. The `region` argument is ignored: one PDF per contract covers
every region the contract is sold in (`eneco.py:149`).

```
fetch(session, contract_id, region)
  |-> _fetch_listing            GET the tariefkaarten HTML  (eneco.py:248-249)
  |-> _resolve_url              regex the live PDF URL      (eneco.py:252-274)
  |-> fetch_pdf_text            download + pypdf text       (_pdf.py:179-186)
  '-> parse_snapshot            build the SupplierSnapshot  (eneco.py:277-291)
```

`_resolve_url` (`eneco.py:252-274`) accepts either a full href
(`https://.../BC_..._NL_ENECO_POWER_FLEX.pdf`) or a bare filename in the listing
HTML, reconstructing the absolute URL from `_BASE_URL` when only the filename is
present. It keeps the first match: the listing only advertises one issue at a
time, but the single-shot search defends against a future duplicate silently
selecting a stale revision.

### `probe` (freshness)

`probe` (`eneco.py:210-228`) returns the resolved current PDF URL as the freshness
key. A header-only (HEAD) probe is unusable here because the listing returns a
per-request ETag under `Cache-Control: no-store` (`eneco.py:217-221`). Because the
issue number is embedded in the filename and rotates monthly, the URL itself is
the freshness signal: when it changes, the coordinator refetches. Any
`ExtractorError` while scraping the listing is swallowed and returns `None`, in
which case the coordinator's time-based TTL takes over.

### `fetch_for_month` (historical billing)

`fetch_for_month` (`eneco.py:164-207`) supports the time-correct yearly-cost flow
by fetching the card that was in force for a past `(year, month)`. The CDN keeps
every monthly issue indefinitely, so the archive is real (not overwrite-in-place).
The URL is reconstructed directly from the pattern rather than scraped:

```
BC_032_<VOL><YY><MM>_NL_ENECO_POWER_<slug>.pdf
```

For the requested month it walks volumes `01`..`05` (`eneco.py:185`). For each
candidate:

1. `head_freshness_key` HEAD-probes the URL first (`eneco.py:194`). A missing
   volume returns inside the 10 s HEAD budget instead of stalling on the 30 s GET
   timeout, dropping worst-case missing-month latency from `5 x 30s` to about
   `5 x 10s` under sustained CDN issues. A `None` (404 or no header) skips the
   volume.
2. `fetch_pdf_text` downloads and extracts; an `ExtractorError` skips the volume
   (`eneco.py:196-199`).
3. `parse_snapshot` parses; an `ExtractorError` skips the volume
   (`eneco.py:200-203`).
4. `archive_validity_check` (`_pdf.py:958-995`) confirms the snapshot actually
   covers `year_month`, passing `month_names=_NL_MONTHS` (`eneco.py:204`). This
   guards against the CDN silently substituting the current card at a historical
   URL: when `valid_until` parses, it must fall in the requested month; when it is
   missing, a textual mention of the month is required.

Returns `None` when no volume in the range yields a covering snapshot
(`eneco.py:207`) or when the contract id is unknown (`eneco.py:181-183`); the
coordinator then falls back to the current snapshot as a proxy. This behaviour is
pinned by `test_fetch_for_month_*` (`tests/test_eneco.py:309-367`): a match
returns the snapshot, a validity mismatch returns `None`, a 404 for every volume
returns `None`, and an unknown contract returns `None` rather than raising.

### `discover`

`discover` (`eneco.py:231-245`) returns the set of `power_<name>` slugs advertised
on the listing, extracted from every `BC_..._NL_ENECO_POWER_<NAME>.pdf` link and
lower-cased to match the registry contract ids.

## Parsing

`parse_snapshot` (`eneco.py:277-291`) assembles the snapshot from six extractors.
It is exposed at module level so the tests can drive it with fixture text without
touching the network.

```python
SupplierSnapshot(
    supplier="eneco",
    contract=contract_id,
    energy=_extract_energy(text, contract_id),      # eneco.py:299-306
    dsos=_extract_dsos(text),                        # eneco.py:415-427
    taxes=_extract_taxes(text),                      # eneco.py:503-560
    source_url=source_url,
    publication_label=_extract_publication_month(text),  # eneco.py:294-296
    valid_until=parse_valid_until(text),             # _pdf.py:794
    injection=_extract_injection(text, contract_id), # eneco.py:563-638
)
```

### The `_NUM` numeric token

The shared numeric token (`eneco.py:142`) is the parsing linchpin. It matches
either a thousands-grouped Belgian number using a non-breaking / thin /
narrow-no-break space (`\xa0`, ` `, ` `), or an ungrouped run of digits
with an optional comma-decimal:

```
_NUM = r"(\d{1,3}(?:[\xa0  ]\d{3})+(?:,\d{1,4})?|\d+(?:[\.,]\d{1,4})?)"
```

Column separators in the PDF are ordinary ASCII spaces, so grouping on the special
spaces is unambiguous. The comment records a historical bug: the previous
`\d{1,3}...` form capped the integer part at three digits, so any value of 1000 or
more (for example a four-digit yearly fee) was truncated to its first `1.xxx` and
mis-parsed. `test_num_parses_thousands_grouped_and_four_digit_values`
(`tests/test_eneco.py:194-207`) locks both the NBSP-grouped and ungrouped
four-digit round-trips. `_WS` (`eneco.py:143`) matches ASCII whitespace or NBSP
and is used to span line wraps in the tax block. All numeric values are parsed via
`to_float` (`_pdf.py:665-677`), which strips every Unicode space variant before
swapping comma for dot.

### Publication label and validity

`_extract_publication_month` (`eneco.py:294-296`) captures `Tariefkaart <month>
<year>` (for example `mei 2026`). `valid_until` comes from the shared
`parse_valid_until` (`_pdf.py:1004`), which reads the "Geldig van ... t.e.m. ..."
line. `test_extracts_valid_until_from_geldig_line` (`tests/test_eneco.py:281-295`)
pins April 30 2026 on all three fixtures so the `tomorrow_prices_available` binary
sensor flips off at month end.

### Energy formula per kind

`_extract_energy` (`eneco.py:299-306`) dispatches on the contract id.

| kind | helper | fields returned | key anchors |
| --- | --- | --- | --- |
| fixed | `_extract_fixed` (`eneco.py:309-327`) | `single`, `peak` (day), `offpeak` (night), `exclusive_night`, `yearly_fixed_fee` | `DAG NACHT` header then five `_NUM` |
| variable | `_extract_variable` (`eneco.py:330-376`) | `current`, `yearly_fixed_fee`, `formula` | `(€/jaar)` + `Geschatte jaarprijs`; `Maandprijs`; Belpex formula |
| dynamic | `_extract_dynamic` (`eneco.py:379-412`) | `factor`, `base`, `yearly_fixed_fee` | `Enkelvoudige meter`; `(f X BELPEX-H +- base) X vat` |

Fixed (`_extract_fixed`) reads a five-number row (yearly fee, single, day, night,
exclusive-night) after the `DAG NACHT` header, converting the four rates from
cents to EUR/kWh (`/ 100.0`) and keeping the yearly fee in EUR. Illustrative pinned
values from `test_fix_extracts_energy_block` (`tests/test_eneco.py:65-72`):
`single = 0.1865`, `peak = 0.2055`, `offpeak = 0.1699`, `exclusive_night = 0.1699`,
`yearly_fixed_fee = 65.0`.

Variable (`_extract_variable`) anchors the yearly fee on the `(€/jaar)` header and
the `Geschatte jaarprijs` row rather than counting newlines. The comment records
the reason (`eneco.py:331-336`): a single extra header line on a future card broke
the earlier rigid four-newline skip and took Power Flex offline.
`test_flex_yearly_fee_survives_extra_header_line` (`tests/test_eneco.py:228-237`)
injects an extra header line to guard the anchor. The current rate is the first of
four numbers before `Maandprijs`; the formula string accepts any sign character
between the Belpex factor and the base (`SIGN_CHARS`, `_pdf.py:713`) so a polarity
flip does not drop the display string. Illustrative:
`current = 0.1390`, `yearly_fixed_fee = 65.0`
(`test_flex_extracts_current_monthly_rate`, `tests/test_eneco.py:218-225`).

Dynamic (`_extract_dynamic`) captures the full PDF formula including the VAT
multiplier the card actually prints (`eneco.py:389-392`): `(f X BELPEX-H +- base)
X vat`. It reads the multiplier from the card (for example `1,06` for 6 % VAT,
`1,21` if Belgium reverts to 21 %) rather than assuming a constant. The unit
conversion (`eneco.py:400-407`) turns the PDF's cents-per-kWh-from-EUR-per-MWh form
into the integration's `energy_eur_per_kwh = factor * spot_eur_per_kwh + base`:

```
factor = factor_pdf * vat_mult * 1000 / 100 = factor_pdf * vat_mult * 10
base   = base_cents * vat_mult / 100
```

Illustrative, from `test_dynamic_extracts_factor_and_base`
(`tests/test_eneco.py:210-223`): PDF `(0.102 X BELPEX-H + 1) X 1.06` yields
`factor = 1.0812` (`0.102 * 10.6`), `base = 0.0106`, `yearly_fixed_fee = 100.0`.
The literal `1.0812` is pinned deliberately to catch a unit-conversion bug that
would otherwise cancel.

### DSO overlay

`_extract_dsos` (`eneco.py:415-427`) walks two label maps and emits a `DsoOverlay`
per matched row.

Wallonia (`_WALLONIA_LABELS`, `eneco.py:110-116`), via `_find_wallonia_row`
(`eneco.py:430-466`):

| PDF label | canonical key |
| --- | --- |
| AIEG | `aieg` |
| AIESH | `aiesh` |
| ORES (Brabant Wallon) | `ores` |
| REGIE DE WAVRE | `rew` |
| TECTEO RESA | `resa` |

ORES sub-zones share a uniform rate, so only the first ORES row encountered is
kept as the canonical `ores` (`eneco.py:107-109`, dedup guard at `eneco.py:418`).
A Wallonia row carries 7 numbers on Power Dynamic or 10 on Power Fix. The optional
middle triplet is the Tarif Impact (CWaPE 3-band) set, and Eneco's column order is
`MEDIUM | PIC | ECO`, which differs from OCTA+ / Bolt (`PIC | MEDIUM | ECO`); the
code maps groups 4/5/6 to `medium`/`pic`/`eco` accordingly (`eneco.py:454-461`).
Layout: Enkelvoudig, Dag, Nacht, Uitsl. nacht, `[MEDIUM PIC ECO]`, Transport,
Databeheer (EUR/year), Prosument (EUR/kVA/year). The trailing three columns are
read positionally from the end (`groups[-3:]`), so the optional Impact triplet does
not shift them. Illustrative AIEG values from `test_fix_extracts_dso_overlay`
(`tests/test_eneco.py:75-90`): `distribution_single = 0.1087`,
`distribution_peak = 0.1205`, `distribution_offpeak = 0.0666`,
`distribution_exclusive_night = 0.0666`, `transport = 0.0274`,
`data_management_per_year = 19.49`, `prosumer_eur_per_kva_year = 81.04`.

Flanders (`_FLUVIUS_LABELS`, `eneco.py:124-133`), via `_find_fluvius_row`
(`eneco.py:469-500`):

| PDF label | canonical key |
| --- | --- |
| FLUVIUS HALLE VILVOORDE | `fluvius_halle_vilvoorde` |
| FLUVIUS ANTWERPEN | `fluvius_antwerpen` |
| FLUVIUS IMEWO | `fluvius_imewo` |
| FLUVIUS LIMBURG | `fluvius_limburg` |
| FLUVIUS WEST | `fluvius_west` |
| FLUVIUS MIDDEN VLAANDEREN (INTERGEM) | `fluvius_intergem` |
| FLUVIUS KEMPEN (IVEKA) | `fluvius_iveka` |
| FLUVIUS ZENNE DIJLE | `fluvius_zenne_dijle` |

Fluvius rows carry 5 numbers plus two `-` placeholders: Normaal, Uitsl. nacht,
SMR1 databeheer (EUR/year), SMR3 databeheer (EUR/year), Capaciteitstarief
(EUR/kW/year). The match anchors on the `DIGITALE METER` section
(`eneco.py:482-486`) so it does not pick up the analogue-meter row further down.
Key modelling decisions (`eneco.py:469-500`):

- `transport = 0.0`: the Flemish Afnametarief already bundles Elia transmission, so
  no separate transport applies here (same convention as Engie and Luminus
  Flanders rows). The Walloon Transport-kosten column does not apply.
- `distribution_peak` / `distribution_offpeak` are `None`: post-capacity-tariff
  Flemish digital meters bill at a single rate, so no day / night split.
- `distribution_exclusive_night` is column 2 (Uitsl. nacht), the dedicated
  night-circuit rate, distinct from the single day rate.
- `data_management_per_year` is the SMR3 column (group 4), `capacity_eur_per_kw_year`
  is group 5.
- `prosumer_eur_per_kva_year` stays `None`: SMR3 connections do not sit under the
  compensation regime (`test_fix_fluvius_has_no_prosumer_rate`,
  `tests/test_eneco.py:93-97`).

`test_fix_extracts_all_fluvius_sub_areas` and
`test_fix_fluvius_sub_areas_have_distinct_rates` (`tests/test_eneco.py:136-147`)
assert all eight sub-areas are present with materially different rates.
Illustrative Antwerpen values: `distribution_single = 0.0535`,
`distribution_exclusive_night = 0.0481`, `data_management_per_year = 18.92`,
`capacity_eur_per_kw_year = 52.37`.

### Tax overlay

`_extract_taxes` (`eneco.py:503-560`) builds a `TaxOverlay` with
`vat_rate = 0.0` (prices are already VAT-incl, `eneco.py:559`). Fields:

| field | source token | line |
| --- | --- | --- |
| `federal_excise` | first number in the "Verbruik tussen 0 en 3.000 kWh" or "Alle verbruik" tier (Tiers are abolished 2026-08-01) | `eneco.py:511-520` |
| `energy_contribution` | second number in that tier (0.0 default, abolished 2026-08-01) | `eneco.py:516-522` |
| `flanders_renewables` | "Bijdrage groene stroom en WKK ... (€cent/kWh)" | `eneco.py:528-532`, `552` |
| `wallonia_renewables` | "Bijdrage groene stroom Wallonie ... (€cent/kWh)" | `eneco.py:533-537`, `551-557` |
| `region_connection_fee` | "Aansluitingsvergoeding elektriciteit ... (€cent/kWh)" | `eneco.py:538-551`, `558` |
| `energy_fund_eur_per_month` | "Standaard tarief (domicilieadres)" | `eneco.py:552-565`, `576` |

The renewables and connection-fee matches anchor on the `(€cent/kWh)` unit token
rather than the first number after the label, because sibling rows carry `(2)(4)`
footnote markers that a lazy `_NUM` would otherwise capture (`eneco.py:524-527`).
Both regional renewables are populated from every card; the pricing engine selects
the right one per region (`tests/test_eneco.py:150-154`). Illustrative values from
`test_fix_extracts_taxes` (`tests/test_eneco.py:177-204`):
`federal_excise = 0.050329`, `energy_contribution = 0.002042`,
`flanders_renewables = 0.0152`, `wallonia_renewables = 0.0313`,
`region_connection_fee = 0.00075`.

The excise tier and the Walloon connection fee are the two **fail-loud** anchors: a
regex miss raises `ExtractorError` rather than defaulting to `0.0`
(`eneco.py:512-513`, `eneco.py:537-544`). The connection fee is a mandatory all-in
component for Walloon customers with no `live_check` gate, so a silent zero would
under-bill; the renewables, by contrast, are gated in `live_check` and default to
`0.0` when absent. `test_missing_connection_fee_is_fatal`
(`tests/test_eneco.py:136-143`) removes the label and asserts the raise.

### Injection

`_extract_injection` (`eneco.py:564-639`) is the most delicate block. Layout on
every contract:

```
AFNAME EN INJECTIE [/ VALORISATIE]
 ... [optional 'Zie afname' recap block on Power Dynamic] ...
 INJECTIE
  <c/kWh value(s)> Geschatte jaarprijs
  [<c/kWh value(s)> Maandprijs]           (Fix / Flex only)
  <factor> X BELPEX[-H] [+-] <base> Tariefformule
```

Steps (`eneco.py:584-639`):

1. Anchor on the stable `AFNAME EN INJECTIE` prefix (`eneco.py:584`), then cut the
   section at the next ALL-CAPS heading (`ENERGIEDELEN`, `BELASTINGEN`, ...) so
   unrelated blocks do not pollute the matches (`eneco.py:590-594`).
2. Read the monthly indicative (`Maandprijs`), falling back to the yearly estimate
   (`Geschatte jaarprijs`) when no `Maandprijs` prints (`eneco.py:615-630`). Both
   patterns use a numeric-prefix-only capture to dodge the `Zie afname Geschatte
   jaarprijs` recap line on Power Dynamic.
3. Only for `power_dynamic`, parse the Belpex-H formula into hourly-spot
   coefficients (`eneco.py:622-630`): `factor = factor_pdf * 10`,
   `base = base_cents / 100` (VAT-exempt for residential, so no VAT scaling).

Injection taxonomy (the three-shape rule, `base.py:268-306`):

- **Power Fix and Power Flex are MONTH-indexed**: the extractor surfaces the
  `Maandprijs` as `current` AND the card's Belpex-injectie coefficients, with
  `month_indexed=True` (`eneco.py:614-631`). The flag is what makes surfacing
  them safe: the engine resolves them against the delivery month's mean and
  `_injection_is_spot_formula` refuses them to the per-hour path outright, so
  monthly coefficients can never reach the hourly spot even if the `Maandprijs`
  stops printing. Without them the credit was the printed indicative, which the
  card computes from the LAST KNOWN (previous) month's index: on the August 2026
  card, 6,38 c/kWh against the 8,0649 August settles at, a 20,9% under-credit.
  Illustrative: `current = 0.0476`, `factor = 0.8`, `base = -0.0265`
  (`tests/test_eneco.py:246-270`).
- **Power Dynamic is hourly `factor * spot + base`**: it indexes on Belpex-H, so it
  is the only contract that surfaces spot coefficients. Illustrative, from
  `test_dynamic_extracts_injection_rates` (`tests/test_eneco.py:331-341`): formula
  `0,1 X BELPEX-H -1,188` yields `factor = 1.0`, `base = -0.01188`, and (no
  `Maandprijs`) `current = 0.0592` from the yearly estimate. The negative base is a
  real Belgian outcome (the producer can pay to inject at low spot), preserved via
  `parse_sign` (`_pdf.py:710-721`).

No Eneco contract is the spot-indexed-variable shape (Cociter Variable), so
`spot_indexed_injection` is `False` everywhere.

There is no supplier-side prosumer / PV forfait on Eneco cards: the prosumer term
lives only on the Wallonia DSO overlay (`prosumer_eur_per_kva_year`), and
`SupplierSnapshot.supplier_prosumer_eur_per_kva_year` is left `None`.

## Quirks and historical bugs

- **VAT-inclusive cards**: all prices are 6 % VAT-incl, so `vat_rate = 0.0` and the
  pricing engine does not rescale (`eneco.py:36`, `eneco.py:559`, `base.py:471-474`).
  Dynamic is the exception where a VAT multiplier is read from the card and folded
  into `factor` / `base` (`eneco.py:385-407`).
- **`AFNAME EN INJECTIE / VALORISATIE` rename (issue #35)**: the July 2026 cards
  dropped the `/ VALORISATIE` suffix from the injection heading. The old anchor
  keyed off that suffix, which zeroed every Eneco injection credit. The anchor now
  keys off the stable `AFNAME EN INJECTIE` prefix (`eneco.py:579-583`), guarded by
  `test_injection_survives_valorisatie_suffix_drop` (`tests/test_eneco.py:344-361`).
- **`_NUM` four-digit truncation**: the previous integer-part cap of three digits
  silently truncated any yearly fee of 1000 or more; the token now handles grouped
  and ungrouped four-digit values (`eneco.py:136-142`, guarded at
  `tests/test_eneco.py:194-207`).
- **Rigid newline skip broke Power Flex**: the variable yearly-fee parser used to
  count a fixed number of header lines; a single extra header line on a future card
  took the product offline. It now anchors on the `(€/jaar)` and
  `Geschatte jaarprijs` tokens (`eneco.py:331-341`, guarded at
  `tests/test_eneco.py:228-237`).
- **Fail-loud excise and connection fee**: unlike the gated renewables, these two
  raise on a regex miss to avoid silent under-billing (`eneco.py:511-512`,
  `536-543`).
- **Eneco Impact column order is `MEDIUM | PIC | ECO`**, not `PIC | MEDIUM | ECO`
  like OCTA+ / Bolt; mapping the wrong order swaps the CWaPE bands
  (`eneco.py:454-461`).
- **Fluvius transport is 0**: the Flemish Afnametarief bundles Elia transmission;
  do not add a transport term (`eneco.py:121-123`, `475-477`).
- **Flemish digital meters have no peak / offpeak split and no prosumer rate**
  (`eneco.py:492-500`, `tests/test_eneco.py:93-97`).
- **`fetch_for_month` HEAD-before-GET budget**: HEAD-probing each volume keeps the
  missing-month path inside the 10 s probe budget instead of stalling on 30 s GET
  timeouts (`eneco.py:188-194`).
- **Archive substitution guard**: `archive_validity_check` rejects a snapshot whose
  `valid_until` does not fall in the requested month, so a CDN that overwrites a
  historical URL with the current card cannot mis-bill past consumption
  (`_pdf.py:756-793`, `tests/test_eneco.py:329-347`).
- **Sign flexibility**: every Belpex formula match (consumption and injection)
  accepts the full `SIGN_CHARS` class so a card that flips to a Unicode minus does
  not silently drop the formula or the base (`eneco.py:347-349`, `389-392`,
  `600-603`).
- **Power Dynamic is Flanders-only**: its Walloon DSO rows are vestigial reference;
  do not offer the product in Wallonia (`eneco.py:661-670`,
  `tests/test_eneco.py:55-62`).

## Test fixtures

Fixtures live under `tests/fixtures/` and are loaded via `fixture_text(...)`.

| fixture | card variant it represents |
| --- | --- |
| `eneco_fix.pdf` | Power Fix, April 2026 (fixed energy, full Wallonia 10-column + Fluvius overlay, taxes, monthly injection) |
| `eneco_flex.pdf` | Power Flex, April 2026 (variable energy, monthly indexation, monthly injection) |
| `eneco_dyn.pdf` | Power Dynamic, April 2026 (hourly Belpex-H energy and injection formulas) |
| `eneco_flex_dec25.pdf` | Power Flex, December 2025 (archive fixture for `fetch_for_month` validity checks) |
| `eneco_flex_aug26.pdf` | Power Flex, August 2026 (archive fixture for federal tax excise update validity checks) |

## When the card changes, look here

| symptom | first suspect | line |
| --- | --- | --- |
| every snapshot fails to fetch | `_resolve_url` / `_fetch_listing` (listing HTML or URL pattern changed) | `eneco.py:248-274` |
| fixed energy parse error | `_extract_fixed` (`DAG NACHT` header or column count changed) | `eneco.py:309-327` |
| variable energy or yearly fee wrong | `_extract_variable` anchors (`(€/jaar)`, `Geschatte jaarprijs`, `Maandprijs`) | `eneco.py:330-376` |
| dynamic factor / base wrong | `_extract_dynamic` formula regex or VAT multiplier | `eneco.py:379-412` |
| a DSO row missing | its label string in `_WALLONIA_LABELS` / `_FLUVIUS_LABELS`, or the row column count | `eneco.py:110-133`, `430-500` |
| Impact bands swapped | column-order mapping in `_find_wallonia_row` | `eneco.py:454-461` |
| tax value wrong or fatal error | `_extract_taxes` anchors (tier label, `(€cent/kWh)`, `Aansluitingsvergoeding`) | `eneco.py:503-560` |
| injection credit zeroed | `_extract_injection` heading anchor / section cutoff | `eneco.py:584-603` |
| four-digit fee truncated | `_NUM` token | `eneco.py:142` |
| historical month mis-billed | `fetch_for_month` volume walk or `archive_validity_check` | `eneco.py:164-207` |

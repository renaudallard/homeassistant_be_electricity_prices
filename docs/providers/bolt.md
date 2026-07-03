# Provider: bolt

This document is the maintainer's reference for the Bolt Belgium tariff extractor
(`providers/bolt.py`). Read it alongside the source when Bolt changes its tariff card and the
weekly live-check starts failing. Bolt is a nationwide (all three regions) supplier that publishes
one visually rich PDF per contract, at a predictable CDN URL, with the DSO, tax and injection
overlays for every region packed into the same document. The extractor is a pure-regex parser over
`pdfplumber` layout text; almost every hurdle here is a PDF-layout quirk, not a pricing-model one.

Related reading:

- [../provider-framework.md](../provider-framework.md) : the extractor protocol, the dataclasses
  (`SupplierSnapshot`, `EnergyRates`, `DsoOverlay`, `TaxOverlay`, `InjectionRates`) and the
  `_pdf.py` helper library this module leans on.
- [../pricing-model.md](../pricing-model.md) : how `compute_breakdown` consumes the snapshot this
  extractor produces (meter routing, exclusive-night fallback, injection, capacity, taxes).

## Overview

| Property | Value | Source |
| --- | --- | --- |
| Extractor id / label | `bolt` / `Bolt` | `bolt.py:776` |
| Regions served | Flanders, Wallonia, Brussels (all three) | every `Contract` uses the default `regions`; `EXTRACTOR.regions()` unions them, `base.py:383` |
| Publication shape | Monthly PDF card per contract, at a predictable CDN URL; a public HTML listing page links every current PDF | `bolt.py:28`, `bolt.py:108` |
| Fetch transport | `fetch_pdf_text_layout` (pdfplumber, layout-aware) | `bolt.py:233` |
| Probe | HEAD the listing page, prefer `ETag` then `Last-Modified` | `bolt.py:172` |
| Archive | Only `bolt_fix` (slug `fix`) is monthly-archived back to 2024-01; everything else falls back to the current snapshot | `bolt.py:273` |
| VAT convention | Prices are VAT-incl; `vat_rate=0.0` | `bolt.py:361`, `base.py:307` |

Bolt's PDFs are the reason this extractor exists in its current form. They are around 5 MB each,
with rotated columns and a column-major text layout that `pypdf` cannot read, so the module fetches
through `pdfplumber` (`bolt.py:37`). Each PDF covers all three regions in one document (same
convention as Eneco), so `fetch` downloads once and `parse_snapshot` slices out the region the
caller asked for.

### Source URL pattern

Two folder families, one filename convention (`bolt.py:160`):

```
fix cards:  https://files.boltenergie.be/pricelists/fix/<slug>_res_el_fr_<YYYYMM>.pdf
var cards:  https://files.boltenergie.be/pricelists/var/<slug>_res_el_fr_11.pdf
```

Fixed cards roll monthly via a `YYYYMM` suffix; variable cards use a stable version-number suffix
(`_VARIABLE_SUFFIX = "11"` today, `bolt.py:110`). The `_fr_` token means the French-language card;
all three regions still live inside that one French document. The listing page
`https://www.boltenergie.be/fr/listes-des-prix` (`bolt.py:109`) links every current PDF directly.

## Contracts

Bolt declares six residential-electricity contracts (`bolt.py:140`). All are region-unrestricted
(default `regions` = all three) and none is dynamic, so there is no `quarter_hourly` value in play
and no ENTSO-E spot dependency; injection is a flat monthly indicative, never a spot formula (see
Injection below).

| id | label | kind | folder / slug | spot-indexed injection | Notes |
| --- | --- | --- | --- | --- | --- |
| `bolt_fix` | Bolt Fixe (1 year) | fixed | `fix` / `fix` | no | The only card with a real monthly archive |
| `bolt_plenty_fix` | Bolt Plenty Fixe (1 year) | fixed | `fix` / `plenty_fix` | no | Fixed, but no per-month archive (see fetch_for_month) |
| `bolt_variable` | Bolt Variable | variable | `var` / `bolt` | no | Monthly-indexed variable |
| `bolt_plenty` | Bolt Plenty Variable | variable | `var` / `plenty` | no | |
| `bolt_online` | Bolt Online | variable | `var` / `online` | no | |
| `bolt_plenty_online` | Bolt Plenty Online | variable | `var` / `plenty_online` | no | |

`test_bolt_is_registered` (`tests/test_bolt.py:47`) pins the count at exactly six and asserts
`bolt_fix` and `bolt_variable` are present, so adding or removing a product must update that test.

Bolt has no `dynamic`, `tou`, or `tou_impact` product today. `_extract_energy` keeps an explicit
`raise` on the dynamic path so a mislabeled contract fails loud rather than silently mispricing
(`bolt.py:453`).

## Fetch strategy

### `fetch` (current snapshot)

`fetch(session, contract_id, region)` (`bolt.py:260`) validates the contract id, calls the shared
`_fetch_pdf_text` to get `(url, text)`, then hands off to `parse_snapshot`. The download is factored
into `_fetch_pdf_text` (`bolt.py:214`) precisely so `live_check.py` can fetch a card once and parse
three region-specific snapshots from the same 5 MB text instead of paying for the round-trip three
times.

Two timing decisions live in this path:

- **60 s PDF timeout** (`bolt.py:230`). The shared default is 30 s, but Bolt's CDN occasionally
  needs well over that to deliver one 5 MB card. Issue #13 records all six fetches timing out for
  around 25 minutes on 2026-05-09 while the URLs themselves were healthy. The 60 s budget lets a
  2-3x CDN slowdown still yield a snapshot instead of an `UpdateFailed`.
- **Fixed-card previous-month fallback** (`bolt.py:234`). Fixed cards may not be published yet on
  the 1st of the month. On any `ExtractorError` for a `fix`-folder contract, the extractor retries
  the previous month's URL so the user keeps seeing plausible prices. The month boundary is computed
  in Brussels local time (`dt_util.now()`), matching `_document_url`, so it never rolls back two
  months on the new-month UTC seam. Bolt cards expose no parseable `valid_until`, so this fallback
  cannot signal staleness through it; the `_LOGGER.warning` at `bolt.py:248` is the only trace that
  last month's card is being served. Variable-folder contracts do not get this fallback (they
  re-raise, `bolt.py:242`).

The month suffix in `_document_url` is deliberately `dt_util.now()` (Brussels local) and not UTC
(`bolt.py:164`): UTC would mis-key by last month for the first 1-2 Brussels hours of every month.

### `probe` (freshness)

`probe` (`bolt.py:172`) HEADs the listing page and returns the first present header, preferring
`ETag` then `Last-Modified` via `head_freshness_key` (`_pdf.py:329`). Bolt is the reason
`head_freshness_key` accepts a `prefer` order: its listing returns a stable `ETag` while
`Last-Modified` flips on every CDN edge cache, so every other supplier prefers `Last-Modified` and
Bolt inverts it (`_pdf.py:343`). The probe returns a single key for the whole listing (it ignores
`region`, and returns `None` for an unknown contract id). When the HEAD fails or carries neither
header, `head_freshness_key` returns `None` and the coordinator's time-based TTL takes over.

### `discover` (live-check coverage)

`discover` (`bolt.py:191`) GETs the listing HTML and returns the set of `<folder>/<slug>` prefixes
it links for residential electricity (`pricelists/(fix|var)/([a-z_]+)_res_el_fr_`). `live_check.py`
diffs that against the registry's `{c.folder + '/' + c.slug for c in _CONTRACTS}` set, so a new Bolt
product or a renamed slug surfaces as a coverage gap. On fetch failure it returns an empty set
rather than raising.

### `fetch_for_month` (archive / YTD backfill)

`fetch_for_month(session, contract_id, region, year_month)` (`bolt.py:273`) supports the
time-correct yearly-cost flow. It gates on folder `fix` AND slug `fix` (`bolt.py:291`) and returns
`None` for everything else. Only `bolt_fix` clears that gate, and it is archived monthly under the
`YYYYMM` suffix going back to 2024-01. Variable cards use a stable version-number suffix, so past
months cannot be addressed at all; `plenty_fix` sits in the `fix` folder and so its URL rolls
monthly by `YYYYMM` like `bolt_fix`, but the slug gate still declines it. All of these return
`None`, and the YTD path falls back to the current snapshot as a proxy (`bolt.py:283`).

Because a `fix` card carries no parseable `valid_until`, `fetch_for_month` cannot trust the URL
alone: the CDN could serve a current card under a historical URL and silently bill a past month at
today's rates. So after parsing it runs `archive_validity_check` (`bolt.py:309`,
`_pdf.py:638`) with `month_names=_FR_MONTH_NAMES`. Since `valid_until` is `None` for Bolt, that
check falls through to `text_mentions_month`, which requires the printed `<Month> <Year>` header (or
`MM/YYYY` / `YYYY-MM`) to reference the requested month inside an anchored window; a mismatch
returns `None` and the caller uses the proxy. `test_fetch_for_month_rejects_mismatched_month`
(`tests/test_bolt.py:222`) pins this: the April fixture is accepted for April 2026 and rejected for
January 2026.

## Parsing

`parse_snapshot(contract_id, text, region, source_url)` (`bolt.py:312`) is the pure parser exposed
for unit tests. First it normalizes U+2028 LINE SEPARATOR characters that Bolt sprinkles where a
newline is expected, replacing them with `\n` so one set of regexes covers every block
(`bolt.py:319`). Then it fans out to the field extractors and assembles a `SupplierSnapshot`.

### Field map

| Snapshot field | Extractor | Notes |
| --- | --- | --- |
| `energy` | `_extract_energy` (`bolt.py:390`) | `FixedRates` or `VariableRates` |
| `injection` | `_extract_injection` (`bolt.py:462`) | flat monthly indicative, `factor`/`base` = `None` |
| `publication_label` | `_extract_publication_month` (`bolt.py:457`) | `<Month> <Year>` header |
| `taxes.federal_excise`, `energy_contribution`, `region_connection_fee` | `_extract_taxes` (`bolt.py:483`) | 3-column FL/WAL/BX rows, sliced by region |
| `taxes.energy_fund_eur_per_month` | `_extract_energy_fund` (`bolt.py:538`) | Flanders only, usually `-` (0) |
| `taxes.{flanders,wallonia,brussels}_renewables` | `_extract_renewables` (`bolt.py:549`) | certificats verts + Flanders WKK; zeroed outside the active region |
| `dsos` | `_extract_flanders_dsos` / `_extract_wallonia_dsos` / `_extract_brussels_dsos` | picked by region |
| `valid_until` | `parse_valid_until` (`_pdf.py:677`) | always `None` in practice; Bolt prints no parseable validity date |

The region argument slices the multi-region document: `parse_snapshot` zeroes the two non-active
regional renewables columns (`bolt.py:334`) and calls exactly one of the three DSO parsers. The
Flanders energy fund is only read when `region == flanders`.

### Energy block (`_extract_energy`)

Bolt's price model has two convention quirks the parser normalizes:

1. **Monthly platform fee, billed annually.** `_extract_yearly_fee` (`bolt.py:373`) matches
   `€ N[,NN] / mois` and multiplies by 12 to fit the integration's annual-fee convention. The
   platform fee is the entire Bolt monetisation, so a missing match raises rather than returning 0
   (a silent miss would undercount the bill by roughly 130 EUR/year, illustrative from the
   docstring). The decimal portion is optional so a future round fee like `€ 11 / mois` still
   parses. `test_fix_yearly_fee_is_monthly_x_12` (`tests/test_bolt.py:56`) asserts `10.99 * 12`
   (illustrative).

2. **`Prix mensuel` is the current month's price for every kind.** The line prints two adjacent
   numbers: mono, then the **exclusive-night** rate (group 2 is the dedicated night-circuit rate,
   NOT a day/peak rate) (`bolt.py:397`). Values are in c/kWh, so the parser divides by 100.

Bi-hourly (Jour / Nuit) rates come from a separate `Prix de l'électricité verte` block that prints
two `Jour Nuit` subheads: the first pair is for consumption, the second is for injection
(`bolt.py:410`). The bi-horaire consumption row is the LAST same-line adjacent-number pair between
the two subheads, so the parser scopes a `re.S` span between them and takes `pairs[-1]`. This is the
stable invariant because `pdfplumber` sometimes renders the annual-estimate column vertically above
the row (variable cards) and sometimes drops it entirely (fixed cards), so a fixed positional offset
would break. `test_variable_uses_current_monthly_not_annual_estimate` (`tests/test_bolt.py:116`)
verifies the parser skips the annual estimate (15,20 / 15,20 illustrative) and picks the current
monthly (14,56 / 12,09 illustrative).

The fallback logic when no bi-horaire pair is found is kind-dependent (`bolt.py:427`):

- `fixed`: mono == peak == offpeak, and the card sometimes omits the bi-horaire row entirely, so the
  single rate is the right value for all three. `test_fix_extracts_consumption_rates`
  (`tests/test_bolt.py:67`) confirms all four rates equal the single value (16,71 c/kWh
  illustrative).
- `variable`: a miss is a layout drift, not a mono contract; variable cards always publish distinct
  Jour / Nuit rates, so the parser raises rather than silently billing a bi-hourly user at the mono
  rate. `test_variable_missing_bihourly_rates_fails_loud` (`tests/test_bolt.py:108`) enforces this.

`exclusive_night` is populated for every card from `Prix mensuel` group 2; the pricing engine routes
an exclusive-night meter through it.

### Injection block (`_extract_injection`)

Bolt's feed-in is a flat monthly indicative (`Prix mensuel 5,31 4,03`) inside the block following
the `Injection` header, on both fix and variable cards. This is INJECTION SHAPE (a) in the
three-shape taxonomy: monthly-indicative-only. `factor`, `base` and `formula` stay `None`
(`bolt.py:477`); there is no spot dependency.

Two land mines are baked into the anchor (`bolt.py:462`):

- The consumption side also has a `Prix mensuel` line ABOVE the injection block, so the parser
  anchors on the `Injection` header (`re.S` across to the injection `Prix mensuel`) rather than
  counting occurrences. A third consumption-side row therefore cannot shift the match.
- The July 2026 fix cards print a NEGATIVE second column (`Prix mensuel 3,40 -0,43`, the "Exclusif
  nuit" injection column). Only the first column is billed, but the second is a required anchor
  token, so the regex allows an optional minus on it (`-?[\d.,]+`).
  `test_injection_accepts_negative_second_column` (`tests/test_bolt.py:95`) locks this in.

`test_injection_is_flat_monthly_indicative` (`tests/test_bolt.py:79`) checks both fix and variable
cards yield `current` = 5,31 c/kWh (illustrative) with `factor`/`base` `None`.

### Tax block (`_extract_taxes`)

Taxes print as 3-column rows (Flandres / Wallonie / Bruxelles) after the U+2028 normalization
flattened them to newlines (`bolt.py:483`). Federal excise (`Droit d'accise spécial`) and energy
contribution (`Contribution sur l'énergie`) are mandatory federal levies on every Belgian card, so a
regex miss raises (`bolt.py:505`); the apostrophe class `['’]` handles both straight and curly
quotes. `_per_region` (`bolt.py:523`) indexes group 1/2/3 by region and treats `-` or empty as 0.

The connection-fee row (`Redevance de raccordement`) is Wallonia-only on real cards, so a miss is
permitted (returns 0). Its regex eats up to three integer footnote markers ahead of the FL/WAL/BX
values (`bolt.py:517`); the `{0,3}` cap deliberately stops a future integer-only Flanders value from
being mistaken for a footnote and silently shifting the columns.

`test_taxes_split_correctly_per_region` (`tests/test_bolt.py:130`) checks nationwide excise
(0.050329) and contribution (0.002042), Wallonia connection fee (0.00075), and per-region renewables
(all illustrative).

### Renewables block (`_extract_renewables`)

Three columns under `Certificats verts (c€/kWh)`, plus a Flanders-only `WKK` (cogeneration) row that
is ADDED to the Flanders certificats-verts value (`bolt.py:549`). This split across two lines is the
second Bolt-specific convention (`bolt.py:44`). The WKK regex skips an optional multi-digit footnote
ref before the value and requires a real whitespace separator so a greedy `\d*` cannot swallow the
leading digits of a multi-digit value (`bolt.py:561`). Certificats verts is charged in every region,
so a miss raises; WKK is optional. `test_taxes_split_correctly_per_region` asserts Flanders
renewables = `(1.17 + 0.39)/100` (cert + WKK, illustrative), proving the footnote skip and the sum.

### DSO overlays

Bolt maps every DSO sub-area the integration knows, region by region. A structural quirk that spans
all three parsers: `pdfplumber` sometimes renders a row vertically (one number per line), so the
regexes use `\s+` (which matches newlines) between values to handle both layouts.
`test_wallonia_dso_handles_vertical_layout` (`tests/test_bolt.py:156`) exercises this.

**Flanders (`_extract_flanders_dsos`, `bolt.py:589`).** Eight Fluvius sub-areas via `_FLANDERS_LABELS`
(`bolt.py:577`). Note the label-to-key mapping is not one-to-one by name: `Fluvius Kempen` maps to
`DSO_FLUVIUS_IVEKA` and `Fluvius Midden-Vl` to `DSO_FLUVIUS_INTERGEM`. Each row has 8 numbers; the
extractor bills the digital (SMR3) block (columns 1-4 plus the prosumer column 8) and ignores the
trailing classic columns. Group 4 is the dedicated exclusive-night meter rate, lower than normal
digital distribution, so a night circuit is billed at it. `transport` is `0.0` (Flanders folds
transport into distribution). Flanders digital meters carry no prosumer tariff on the DSO side in
the general case, but Bolt still exposes a prosumer column, which is read into
`prosumer_eur_per_kva_year`. `test_flanders_dso_includes_transport_in_distribution`
(`tests/test_bolt.py:186`) checks Antwerpen: transport 0.0, distribution 0.0535, exclusive-night
0.0481 (< distribution), capacity 52.37 (all illustrative).

**Wallonia (`_extract_wallonia_dsos`, `bolt.py:655`).** Five DSOs via `_WALLONIA_LABELS`
(`bolt.py:646`). Ten numbers per row: mono, jour, nuit, excl_nuit, PIC, MEDIUM, ECO, transport,
terme_fixe (EUR/an), prosumer (EUR/kVA/an). PIC/MEDIUM/ECO populate the CWaPE Tarif Impact band
columns (`distribution_pic` / `_medium` / `_eco`); `terme_fixe` becomes `data_management_per_year`.

The Wallonia parser carries the module's single largest land mine, the **RESA/REW label swap**
(`bolt.py:632`). In Bolt's `pdfplumber` text extraction the rows labeled `TECTEO RESA` and `WAVRE`
carry each other's values, so `_WALLONIA_LABELS` deliberately maps `TECTEO RESA -> DSO_REW` and
`WAVRE -> DSO_RESA` to un-swap them. This was verified against the regulator's rates and every other
supplier's PDF. After parsing, a runtime sanity check enforces the invariant that RESA's
`distribution_single` stays strictly cheaper than REW's (a Walloon-tariff pattern that holds for
every card parsed). The check uses a process-wide `_RESA_REW_LOGGED` latch (`bolt.py:106`) so it
rings HA's notification bell at most once per boot. Three outcomes (`bolt.py:701`):

- Both rows missing: stay quiet (the parser already raised on the wider drift).
- Only one row parsed: log at ERROR once (the surviving row may now carry the other DSO's values
  with nothing to compare against).
- Both parsed but the inequality flipped: log at ERROR once, meaning Bolt likely fixed the upstream
  layout and the compensating swap now inverts correct values, so it should be removed.

The swap needs manual re-validation at least every 6 months (last done 2026-05, next due 2026-11,
`bolt.py:642`). `test_resa_is_cheaper_than_rew_after_label_swap` (`tests/test_bolt.py:172`) guards
the invariant in CI.

**Brussels (`_extract_brussels_dsos`, `bolt.py:741`).** One row, `Sibelga`, with six captured
numbers: mono, jour, nuit, excl_nuit, transport, terme_fixe (the prosumer trailing token is `-`).
The exclusive-night column (group 4) is now wired into `distribution_exclusive_night`; the comment
records that it was previously dropped, causing a Brussels night meter to fall back to off-peak,
correct only while the two columns happened to be equal (`bolt.py:764`). The Sibelga overlay also
carries the Brussels Brugel OSP annual-fee table via `parse_brussels_osp` (`bolt.py:770`,
`_pdf.py:498`); Bolt prints `Obligations de service publique` with a lowercase `s`, which the
case-insensitive helper handles. A missing Sibelga row returns an empty dict (permitted).
`test_brussels_extracts_sibelga` (`tests/test_bolt.py:201`) checks distribution 0.0996, off-peak
0.0753, exclusive-night 0.0753, transport 0.0227 (all illustrative).

## Quirks and historical bugs (the land mines)

- **Monthly fee, x12.** `€ N / mois` is multiplied by 12 for the annual convention; a miss raises,
  not returns 0 (`bolt.py:373`).
- **Split renewables.** Flanders renewables = `Certificats verts` + `WKK`, two separate lines
  (`bolt.py:44`, `bolt.py:549`).
- **VAT-incl.** Prices are already VAT-incl, so `vat_rate=0.0` (`bolt.py:361`). An extractor that
  ever ships ex-VAT numbers must set the parsed rate explicitly.
- **No parseable `valid_until`.** Bolt cards print `Carte Tarifaire Bolt Fixe <Month> <Year>` but no
  machine-readable validity date, so `parse_valid_until` returns `None` and the archive cross-check
  falls back to a textual month match on `_FR_MONTH_NAMES` (`bolt.py:112`, `bolt.py:309`).
- **U+2028 line separators.** Normalized to `\n` at the top of `parse_snapshot` (`bolt.py:319`);
  every downstream regex depends on that.
- **5 MB PDFs, slow CDN.** 60 s timeout to survive a 2-3x slowdown (issue #13, `bolt.py:228`).
  The CDN-slowness signature (empty error after `:` plus a missing per-supplier metrics row) is a
  transient aiohttp `TimeoutError`, not a regression.
- **First-of-month fixed fallback.** Missing current-month fix card falls back to the previous month
  with a warning; Brussels-local month math avoids the UTC seam (`bolt.py:234`).
- **RESA/REW swap.** Compensating label inversion plus a self-disarming ERROR invariant; re-validate
  every 6 months (`bolt.py:632`).
- **Negative injection second column.** July 2026 fix cards; the anchor token tolerates a minus
  (`bolt.py:471`).
- **Vertical `pdfplumber` rows.** Every DSO regex uses `\s+` to span one-number-per-line renders
  (`bolt.py:600`).
- **Exclusive-night everywhere.** `Prix mensuel` group 2 and Fluvius group 4 and the Sibelga column
  are all dedicated night-circuit rates, not day/peak rates (`bolt.py:396`, `bolt.py:621`,
  `bolt.py:764`).

## Test fixtures

Under `tests/fixtures/` (both around 5 MB, April 2026 cards, French-language, all three regions):

| Fixture | Card variant | Exercised by |
| --- | --- | --- |
| `bolt_fix.pdf` | Bolt Fixe (fixed) April 2026 | most tests: yearly fee, consumption rates, injection, per-region taxes, Wallonia/Flanders/Brussels DSOs, RESA/REW swap, `fetch_for_month` accept/reject |
| `bolt_variable.pdf` | Bolt Variable April 2026 | injection parity with fix, current-vs-annual bi-horaire selection, loud failure on missing Jour/Nuit |

Fixtures are loaded via `fixture_text("bolt_fix.pdf", layout=True)` (`tests/test_bolt.py:61`), which
routes through the `pdfplumber` layout extractor so tests see the same text the live path parses.

## When the card changes, look here

Ordered by likelihood of breaking when Bolt re-renders or restructures a card:

1. **DSO row regexes** (`_extract_flanders_dsos` `bolt.py:589`, `_extract_wallonia_dsos`
   `bolt.py:655`, `_extract_brussels_dsos` `bolt.py:741`). Column-count changes, a renamed sub-area
   label, or a new footnote marker breaks these first. A row that stops matching is silently dropped
   (Flanders/Brussels) or raises via the Wallonia invariant path.
2. **RESA/REW swap** (`_WALLONIA_LABELS` `bolt.py:646`). If the ERROR invariant fires, Bolt probably
   fixed the upstream layout; remove the swap and re-point the labels straight.
3. **`_extract_energy` bi-horaire span** (`bolt.py:410`). The two-`Jour Nuit`-subhead anchor is
   fragile; if Bolt reorders the injection/consumption blocks or drops a subhead, the variable path
   raises loud.
4. **`_extract_yearly_fee`** (`bolt.py:373`). A phrasing change away from `€ N / mois` raises.
5. **`_extract_injection`** (`bolt.py:462`). A relabeled `Injection` header or a third
   consumption-side `Prix mensuel` row shifts the anchor; a new second-column sign convention needs
   the `-?` tolerance revisited.
6. **`_extract_taxes` / `_extract_renewables`** (`bolt.py:483`, `bolt.py:549`). Federal levy and
   certificats-verts misses raise; the connection-fee footnote `{0,3}` cap may need widening if Bolt
   adds markers.
7. **URL construction** (`_document_url` `bolt.py:160`, `_VARIABLE_SUFFIX` `bolt.py:110`). If Bolt
   bumps the variable version past `11`, update the constant; `discover` (`bolt.py:191`) plus the
   live-check coverage diff will flag a slug or folder rename.

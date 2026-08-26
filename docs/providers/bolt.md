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
| Extractor id / label | `bolt` / `Bolt` | `bolt.py:836` |
| Regions served | Flanders, Wallonia, Brussels (all three) | every `Contract` uses the default `regions`; `EXTRACTOR.regions()` unions them, `base.py:73` |
| Publication shape | Monthly PDF card per contract, at a predictable CDN URL; a public HTML listing page links every current PDF | `bolt.py:28`, `bolt.py:114` |
| Fetch transport | `fetch_pdf_text_layout` (pdfplumber, layout-aware) | `bolt.py:353` |
| Probe | HEAD the listing page, prefer `ETag` then `Last-Modified` | `bolt.py:169` |
| Archive | Only `bolt_fix` (slug `fix`) is monthly-archived back to 2024-01; everything else falls back to the current snapshot | `bolt.py:270` |
| VAT convention | Prices are VAT-incl; `vat_rate=0.0` | `bolt.py:358`, `base.py:474` |

Bolt's PDFs are the reason this extractor exists in its current form. They are around 5 MB each,
with rotated columns and a column-major text layout that `pypdf` cannot read, so the module fetches
through `pdfplumber` (`bolt.py:37`). Each PDF covers all three regions in one document (same
convention as Eneco), so `fetch` downloads once and `parse_snapshot` slices out the region the
caller asked for.

### Source URL pattern

Two folder families, one filename convention (`bolt.py:157`):

```
fix cards:  https://files.boltenergie.be/pricelists/fix/<slug>_res_el_fr_<YYYYMM>.pdf
var cards:  https://files.boltenergie.be/pricelists/var/<slug>_res_el_fr_<version>.pdf
```

Fixed cards roll monthly via a `YYYYMM` suffix. Variable cards carry a version number that Bolt
bumps in place on no fixed schedule, leaving every superseded file served, so a pinned version
keeps returning 200 and parsing cleanly while billing an old formula. `_resolve_variable_suffix`
(`bolt.py:255`) therefore reads the version off the listing on every fetch rather than trusting a
constant; `_VARIABLE_SUFFIX_FALLBACK` (`bolt.py:147`) is used only when the listing is unreadable
or does not advertise that card. A pinned `_11` billed June's formula for ten weeks after `_13`
shipped on 2026-08-01.

The version is resolved **per (slug, segment)**, not once for the whole variable family. The four
slugs and both segments happen to sit on the same version today, but nothing enforces that, and a
global maximum would build a URL that does not exist for any slug whose counter lagged - trading a
stale card for a 404 on that product. The `_fr_` token means the French-language card;
all three regions still live inside that one French document. The listing page
`https://www.boltenergie.be/fr/listes-des-prix` (`bolt.py:115`) links every current PDF directly.

## Contracts

Bolt declares seven residential-electricity contracts and a professional edition of each, fourteen
in all (`bolt.py:144`). All are region-unrestricted (default `regions` = all three). Six of the
seven are fixed / variable, all billing injection per quarter-hour off Belpex; the seventh, `bolt_dynamic`,
is a `quarter_hourly` dynamic contract that depends on the ENTSO-E spot and has a spot-indexed
injection (see Contracts / Injection below).

### The professional editions

Bolt publishes each product twice at the same path, with `_res_` or `_pro_` in the filename, so the
pro lane is just `_ContractDef.segment` feeding `_document_url`. Three things differ on the card:

- **It prices excluding VAT at 21%**, so the snapshot carries `vat_rate=0.21`. The distribution
  block is still headed `TTC`, but its numbers match the other suppliers' ex-VAT tables to the
  cent, so the label is stale rather than the values - do not trust that heading.
- **Injection is taxed** (`vat_applies=True`), against the residential exemption.
- **The `N% TVA` phrase is gone.** `_extract_dynamic_energy` reads it to scale the Belpex formula,
  and `vat_multiplier` falls back to 6% when it is missing, which would have scaled an already
  ex-VAT formula and then let `vat_rate` scale it again. The professional branch asserts `HTVA`
  and scales by 1.0 instead.

Bolt prints only the first excise tranche (`1,4210` c€/kWh, the 0-20.000 kWh band) rather than the
whole schedule Engie and Mega publish, so a professional Bolt snapshot carries no
`federal_excise_bands` and a site above 20 MWh/year is billed the first band's rate. Nothing can be
done about that from the card alone, and inventing the other bands would put EUR values in source.

The same limitation applies to **Brussels**, and it is worth stating so nobody "fixes" it by
hardcoding. Sibelga bills two separate regulated annual terms for a residential connection:
*Activités de mesure et de comptage* and *Puissance mise à disposition ≤13 kVA*. Engie sums both
(`engie.py:1027`) and so does Mega (`_mega_overlays.py:258`, `mesure + fixed_term_le13`), which is why their
Brussels `data_management_per_year` is around 64,80 EUR/yr. **Bolt's card prints only six numbers
for the Sibelga row**, ending at the metering term, with no ≤13 kVA column anywhere in the
document, so its Brussels `data_management_per_year` is that term alone and a Bolt Brussels entry
under-states the annual network fee against an Engie or Mega quote for the same connection.

There is nothing to read, and `providers/base.py` is explicit that no EUR value lives in Python
source — every number in a `SupplierSnapshot` comes from a live fetch. Sourcing the term from the
Brugel/Sibelga publication the way the Brussels OSP table is handled would be a real fix; putting
the figure in the extractor would not. Left as a known gap.

| id | label | kind | folder / slug | spot-indexed injection | Notes |
| --- | --- | --- | --- | --- | --- |
| `bolt_fix` | Bolt Fixe (1 year) | fixed | `fix` / `fix` | no | The only card with a real monthly archive |
| `bolt_plenty_fix` | Bolt Plenty Fixe (1 year) | fixed | `fix` / `plenty_fix` | no | Fixed, month archive like `bolt_fix` |
| `bolt_variable` | Bolt Variable | variable | `var` / `bolt` | no | Monthly-indexed variable |
| `bolt_dynamic` | Bolt Dynamisch | dynamic | `var` / `bolt` | via energy | Same variable card, formula on the 15-min Belpex spot |
| `bolt_plenty` | Bolt Plenty Variable | variable | `var` / `plenty` | no | |
| `bolt_online` | Bolt Online | variable | `var` / `online` | no | |
| `bolt_plenty_online` | Bolt Plenty Online | variable | `var` / `plenty_online` | no | |

`test_bolt_is_registered` (`tests/test_bolt.py:52`) pins the count at exactly seven and asserts
`bolt_fix`, `bolt_variable` and `bolt_dynamic` are present, so adding or removing a product must
update that test.

`bolt_dynamic` reuses the `var` / `bolt` card (Bolt's dynamic option on the variable contract): the
card prints its tariff formula as `Belpex * <factor> <sign> <base>` in EUR/MWh HTVA, and
`_extract_dynamic_energy` builds a `DynamicRates(quarter_hourly=True)` by applying the consumption
formula to the live 15-minute spot (factor stays a ratio, base is EUR/MWh -> EUR/kWh, both VAT-baked
since `vat_rate=0`). Injection is the first formula that differs from consumption (factor < 1),
returned as a spot-indexed `InjectionRates` (VAT-exempt). Bolt has no `tou` / `tou_impact` product;
`_extract_energy` still raises on any other kind.

## Fetch strategy

### `fetch` (current snapshot)

`fetch(session, contract_id, region)` (`bolt.py:257`) validates the contract id, calls the shared
`_fetch_pdf_text` to get `(url, text)`, then hands off to `parse_snapshot`. The download is factored
into `_fetch_pdf_text` (`bolt.py:360`) precisely so `live_check.py` can fetch a card once and parse
three region-specific snapshots from the same 5 MB text instead of paying for the round-trip three
times.

Two timing decisions live in this path:

- **60 s PDF timeout** (`bolt.py:227`). The shared default is 30 s, but Bolt's CDN occasionally
  needs well over that to deliver one 5 MB card. Issue #13 records all six fetches timing out for
  around 25 minutes on 2026-05-09 while the URLs themselves were healthy. The 60 s budget lets a
  2-3x CDN slowdown still yield a snapshot instead of an `UpdateFailed`.
- **Fixed-card previous-month fallback** (`bolt.py:231`). Fixed cards may not be published yet on
  the 1st of the month, so for a `fix`-folder contract the extractor retries the previous month's
  URL and the user keeps seeing plausible prices. The month boundary is computed in Brussels local
  time (`dt_util.now()`), matching `_document_url`, so it never rolls back two months on the
  new-month UTC seam. Bolt cards expose no parseable `valid_until`, so this fallback cannot signal
  staleness through it; the `_LOGGER.warning` at `bolt.py:245` is the only trace that last month's
  card is being served. Variable-folder contracts do not get it at all (they re-raise,
  `bolt.py:239`).

  Because that trace is so thin, the path is narrow on purpose: **only a card that is genuinely
  absent may take it.** Two things that are not absent cards re-raise instead — a card that
  downloaded fine and carries no text layer (`CardNotReadableError`), and a fetch that failed
  transiently (`is_transient_fetch_error`: a timeout, a 5xx, a 403). Either would serve last
  month's prices with no Repairs card and no staleness signal, since the successful fallback fetch
  resets the snapshot age. A transient error means this month's card is probably fine and simply
  did not arrive, so failing lets the coordinator keep the snapshot it already holds — which is
  this month's. An unpublished card answers 404, classified permanent, so the case the fallback
  exists for still works.

  Live-check run 32223861276 is what the transient hole cost: a runner-wide network slowdown timed
  out three fixed contracts, each quietly fell back a month, and the card-period gate then reported
  nine stale-card failures against a supplier that was publishing normally.

The month suffix in `_document_url` is deliberately `dt_util.now()` (Brussels local) and not UTC
(`bolt.py:163`): UTC would mis-key by last month for the first 1-2 Brussels hours of every month.

### `probe` (freshness)

`probe` (`bolt.py:319`) HEADs the listing page and returns the first present header, preferring
`ETag` then `Last-Modified` via `head_freshness_key` (`_pdf.py:414`). Bolt is the reason
`head_freshness_key` accepts a `prefer` order: its listing returns a stable `ETag` while
`Last-Modified` flips on every CDN edge cache, so every other supplier prefers `Last-Modified` and
Bolt inverts it (`_pdf.py:361`). The probe returns a single key for the whole listing (it ignores
`region`, and returns `None` for an unknown contract id). When the HEAD fails or carries neither
header, `head_freshness_key` returns `None` and the coordinator's time-based TTL takes over.

### `discover` (live-check coverage)

`discover` (`bolt.py:338`) GETs the listing HTML and returns the set of `<folder>/<slug>` prefixes
it links for residential electricity, matching `_CARD_URL_RE` (`bolt.py:140`) and filtering to the
`res` segment, so it still diffs on folder/slug alone. That pattern is shared with the version
resolver, and keeping it usable by both is why the version group is `\w+` and the match is
case-insensitive: pinning it to `\d+` for the resolver's benefit silently narrowed discovery, and a
slug this pattern cannot see is a new product the catalog diff reports as silence. `live_check.py`
diffs that against the registry's `{c.folder + '/' + c.slug for c in _CONTRACTS}` set, so a new Bolt
product or a renamed slug surfaces as a coverage gap. On fetch failure it returns an empty set
rather than raising.

### `fetch_for_month` (archive / YTD backfill)

`fetch_for_month(session, contract_id, region, year_month)` (`bolt.py:270`) supports the
time-correct yearly-cost flow. It gates on the `fix` FOLDER (`bolt.py:288`) and returns `None` for
everything else. That folder is archived monthly under the `YYYYMM` suffix going back to 2024-01,
and every card in it addresses its current card the same way, so all four clear the gate:
`bolt_fix`, `bolt_plenty_fix` and their professional twins. Variable cards are keyed by version
(`bolt_res_el_fr_13.pdf`) rather than by month, so past months cannot be addressed there at all;
those return `None` and the YTD path falls back to the current snapshot as a proxy (`bolt.py:290`).

The gate used to require the slug to be `fix` as well, which locked the two `plenty_fix` contracts
out of an archive that does exist (verified against the CDN: `plenty_fix_res_el_fr_202601.pdf` and
`plenty_fix_pro_el_fr_202601.pdf` both serve). A one-year fixed contract signed in January was
therefore priced all year at the current card.

Because a `fix` card carries no parseable `valid_until`, `fetch_for_month` cannot trust the URL
alone: the CDN could serve a current card under a historical URL and silently bill a past month at
today's rates. So after parsing it runs `archive_validity_check` (`bolt.py:432`,
`_pdf.py:755`) with `month_names=_FR_MONTH_NAMES`. Since `valid_until` is `None` for Bolt, that
check falls through to `text_mentions_month`, which requires the printed `<Month> <Year>` header (or
`MM/YYYY` / `YYYY-MM`) to reference the requested month inside an anchored window; a mismatch
returns `None` and the caller uses the proxy. `test_fetch_for_month_rejects_mismatched_month`
(`tests/test_bolt.py:225`) pins this: the April fixture is accepted for April 2026 and rejected for
January 2026.

> [!IMPORTANT]
> **The archive spans two card layouts.** Bolt redesigned its cards between March
> and April 2026, and the pre-redesign PDFs are still served, so any year-to-date
> walk crossing Q1 (and every Q1 signing cohort) reaches for them. They differ in
> two places:
>
> | | pre-April 2026 | April 2026 onward |
> | --- | --- | --- |
> | energy rates | `Coût de l'énergie Simple` + one labelled line per meter type | `Prix mensuel` row |
> | tax columns | three values **inline** on the label line | on the lines below it |
> | connection fee | markers as `(*)(***)` | bare digits (`6 7`) |
> | Brussels DSO | `SIBELGA` | `Sibelga` |
> | feed-in | `Injection (c€/kWh)` row under `Tarif d'injection (HTVA)` | `Prix mensuel` under the `Injection` header |
>
> `_extract_legacy_energy` (`bolt.py:623`) reads the older shape, keyed on which
> anchor the card actually carries rather than on a date, so it neither guesses at
> the boundary nor needs revisiting the next time Bolt redesigns. The tax reader
> takes either column shape. Before this, `parse_snapshot` raised on those months,
> `fetch_for_month` swallowed the error, and January through March silently billed
> at the CURRENT card's rate: 16,71 c€/kWh against January's actual 13,27.
>
> One trap in the tax row: the current layout prints a bare footnote digit between
> the label and the values (`... (c€/kWh) 5` then `5,0329`). Matching the first
> number after the label captures that `5` and bills the excise at 5 c€/kWh.
> Requiring a decimal separator tells a value from a marker but is the wrong
> discriminator twice over — a levy printed as a whole number is rejected, and
> `_extract_taxes` raises on a miss, so every Bolt contract in all three regions
> stops refreshing (Belgium zeroed the federal levy in August 2026, so that is not
> hypothetical); and an unbounded skip runs past a row whose own values are missing
> and captures the next row's silently. `_three_col_row` bounds the row instead —
> read forward from the label until a line opens a new one, take the **last** three
> numbers — so a leading marker falls off the front, whole numbers are fine, and a
> genuinely empty row yields fewer than three and raises. `tests/fixtures/bolt_fix_jan_legacy.pdf` is the real January 2026 card
> and pins the old shape.
>
> **Teaching the energy block to parse is not enough on its own.** Every overlay
> reader has to take both layouts too, and each miss was silent rather than loud.
> The Brussels one was the worst: matching only `Sibelga` returned an EMPTY dso
> map, `static_breakdown` raises `KeyError` on a missing DSO, and the year-to-date
> walk reads that as "no rate to apply" -- so a Brussels entry billed Q1 at zero,
> which is worse than the pre-fix behaviour of falling back to the current card.
> Wallonia's connection fee sat behind parenthesised footnote markers and came out
> at zero, and the feed-in indicative vanished entirely.
>
> The feed-in row carries its own trap. `Injection (c€/kWh) 5,87 6,69 3,78` sits on
> a page whose tax rows are headed `VL WAL BRU`, so it reads as a regional split --
> but its header is `(*) TVA non applicable. Simple Jour Nuit` a few lines up, and
> the `Belpex Q4 2025` row above it uses the same three columns. They are METER
> REGISTERS. Billing them as regions credited Wallonia the Jour rate and Brussels
> the Nuit one. Every region bills the **Simple** column, which is what the current
> card's `Prix mensuel` branch already does with its own `Compteur simple` /
> `Exclusif nuit` pair.

## Parsing

`parse_snapshot(contract_id, text, region, source_url)` (`bolt.py:309`) is the pure parser exposed
for unit tests. First it normalizes U+2028 LINE SEPARATOR characters that Bolt sprinkles where a
newline is expected, replacing them with `\n` so one set of regexes covers every block
(`bolt.py:319`). Then it fans out to the field extractors and assembles a `SupplierSnapshot`.

### Field map

| Snapshot field | Extractor | Notes |
| --- | --- | --- |
| `energy` | `_extract_energy` (`bolt.py:689`) | `FixedRates` or `VariableRates` |
| `injection` | `_extract_injection` | printed figure PLUS the quarter-hourly `factor`/`base`, flagged `slot_indexed`; `current=None` with `factor`/`base` for `bolt_dynamic` |
| `publication_label` | `_extract_publication_month` (`bolt.py:779`) | `<Month> <Year>` header. The accent classes span the whole Latin-1 range rather than the accents French month names actually use: Bolt's August 2026 fixed card prints "Aôut 2026" (circumflex on the wrong vowel) and an exact class blanked the label on that typo. The value is display-only and never feeds pricing, so a misspelling is tolerated verbatim rather than corrected or dropped. |
| `taxes.federal_excise`, `energy_contribution`, `region_connection_fee` | `_extract_taxes` (`bolt.py:937`) | 3-column FL/WAL/BX rows, sliced by region |
| `taxes.energy_fund_eur_per_month` | `_extract_energy_fund` (`bolt.py:1000`) | Flanders only. The card prints both categories: a domiciled residential connection pays the `résidentiel` row, which is `-` (0); a **professional** contract pays the `non-résidentiel` row (10,07 EUR/month on the August 2026 card). The two rows need separate patterns, since the residential value sits after a U+2028 and the non-residential values are inline on the label line |
| `taxes.{flanders,wallonia,brussels}_renewables` | `_extract_renewables` (`bolt.py:1041`) | certificats verts + Flanders WKK; zeroed outside the active region |
| `dsos` | `_extract_flanders_dsos` / `_extract_wallonia_dsos` / `_extract_brussels_dsos` | picked by region |
| `valid_until` | `parse_valid_until` (`_pdf.py:947`) | always `None` in practice; Bolt prints no parseable validity date |

The region argument slices the multi-region document: `parse_snapshot` zeroes the two non-active
regional renewables columns (`bolt.py:331`) and calls exactly one of the three DSO parsers. The
Flanders energy fund is only read when `region == flanders`.

### Energy block (`_extract_energy`)

Bolt's price model has two convention quirks the parser normalizes:

1. **Monthly platform fee, billed annually.** `_extract_yearly_fee` (`bolt.py:558`) matches
   `€ N[,NN] / mois` and multiplies by 12 to fit the integration's annual-fee convention. The
   platform fee is the entire Bolt monetisation, so a missing match raises rather than returning 0
   (a silent miss would undercount the bill by roughly 130 EUR/year, illustrative from the
   docstring). The decimal portion is optional so a future round fee like `€ 11 / mois` still
   parses. `test_fix_yearly_fee_is_monthly_x_12` (`tests/test_bolt.py:63`) asserts `10.99 * 12`
   (illustrative).

2. **`Prix mensuel` is the current month's price for every kind.** The line prints two adjacent
   numbers: mono, then the **exclusive-night** rate (group 2 is the dedicated night-circuit rate,
   NOT a day/peak rate) (`bolt.py:429`). Values are in c/kWh, so the parser divides by 100.

Bi-hourly (Jour / Nuit) rates come from a separate `Prix de l'électricité verte` block that prints
two `Jour Nuit` subheads: the first pair is for consumption, the second is for injection
(`bolt.py:442`). The bi-horaire consumption row is the LAST same-line adjacent-number pair between
the two subheads, so the parser scopes a `re.S` span between them and takes `pairs[-1]`. This is the
stable invariant because `pdfplumber` sometimes renders the annual-estimate column vertically above
the row (variable cards) and sometimes drops it entirely (fixed cards), so a fixed positional offset
would break. `test_variable_uses_current_monthly_not_annual_estimate` (`tests/test_bolt.py:154`)
verifies the parser skips the annual estimate (15,20 / 15,20 illustrative) and picks the current
monthly (14,56 / 12,09 illustrative).

The fallback logic when no bi-horaire pair is found is kind-dependent (`bolt.py:459`):

- `fixed`: mono == peak == offpeak, and the card sometimes omits the bi-horaire row entirely, so the
  single rate is the right value for all three. `test_fix_extracts_consumption_rates`
  (`tests/test_bolt.py:70`) confirms all four rates equal the single value (16,71 c/kWh
  illustrative).
- `variable`: a miss is a layout drift, not a mono contract; variable cards always publish distinct
  Jour / Nuit rates, so the parser raises rather than silently billing a bi-hourly user at the mono
  rate. `test_variable_missing_bihourly_rates_fails_loud` (`tests/test_bolt.py:146`) enforces this.

`exclusive_night` is populated for every card from `Prix mensuel` group 2; the pricing engine routes
an exclusive-night meter through it.

### Injection block (`_extract_injection`)

Every Bolt card, dynamic or not, bills injection PER QUARTER-HOUR off the Belpex index, and the
non-dynamic cards say so in the same paragraph as the figure: *"Le tableau ci-dessus indique le prix
de vente basé sur la valeur Belpex la plus récente. Dans la facturation, l'injection par quart
d'heure est multipliée par la valeur Belpex pour ce quart d'heure."* The fixed card is blunter
still: *"Contrairement au prix fixe de consommation pour l'électricité, le prix pour l'injection est
quant à lui variable selon l'indice Belpex."*

So the `Prix mensuel 5,31 4,03` figure under the `Injection` header is an illustration at the latest
QUARTERLY index, not a rate. The archive shows it standing still while the market does not: 202604,
202605 and 202606 all print 5,31; 202607 and 202608 both print 3,40. Crediting it flat also cannot
express a negative credit at all, and 15% of Apr–Aug 2026 quarters are negative under the card's own
formula.

`_with_slot_formula` therefore parses the figure AND the `Belpex * 0,94 - 11,33` row beside it,
marking the leg `slot_indexed`. That flag is what stops the pricing engine preferring a printed
`current` on a card whose ENERGY is static — correct for a card publishing a realized monthly rate,
wrong for this one. The figure is kept as the fallback for an entry with no ENTSO-E key. This is
INJECTION SHAPE (c): per-slot formula on a static-energy card, the same shape `bolt_dynamic` has
always had, and VAT-exempt on both. A card generation that prints no formula table keeps the figure
alone rather than losing the credit.

Two land mines are baked into the anchor (`bolt.py:537`):

- The consumption side also has a `Prix mensuel` line ABOVE the injection block, so the parser
  anchors on the `Injection` header (`re.S` across to the injection `Prix mensuel`) rather than
  counting occurrences. A third consumption-side row therefore cannot shift the match.
- The July 2026 fix cards print a NEGATIVE second column (`Prix mensuel 3,40 -0,43`, the "Exclusif
  nuit" injection column). Only the first column is billed, but the second is a required anchor
  token, so the regex allows an optional minus on it (`-?[\d.,]+`).
  `test_injection_accepts_negative_second_column` (`tests/test_bolt.py:130`) locks this in.

`test_injection_carries_the_quarter_hourly_formula` checks both fix and variable
cards yield `current` = 5,31 c/kWh (illustrative) with `factor`/`base` `None`.

### Tax block (`_extract_taxes`)

Taxes print as 3-column rows (Flandres / Wallonie / Bruxelles) after the U+2028 normalization
flattened them to newlines (`bolt.py:547`). Federal excise (`Droit d'accise spécial`) and energy
contribution (`Contribution sur l'énergie`) are mandatory federal levies on every Belgian card, so a
regex miss raises (`bolt.py:569`); the apostrophe class `['’]` handles both straight and curly
quotes. `_per_region` (`bolt.py:587`) indexes group 1/2/3 by region and treats `-` or empty as 0.

The connection-fee row (`Redevance de raccordement`) is Wallonia-only on real cards, so a miss is
permitted (returns 0). Its regex eats up to three integer footnote markers ahead of the FL/WAL/BX
values (`bolt.py:582`); the `{0,3}` cap deliberately stops a future integer-only Flanders value from
being mistaken for a footnote and silently shifting the columns.

`test_taxes_split_correctly_per_region` (`tests/test_bolt.py:168`) checks nationwide excise
(0.050329) and contribution (0.002042), Wallonia connection fee (0.00075), and per-region renewables
(all illustrative).

### Renewables block (`_extract_renewables`)

Three columns under `Certificats verts (c€/kWh)`, plus a Flanders-only `WKK` (cogeneration) row that
is ADDED to the Flanders certificats-verts value (`bolt.py:634`). This split across two lines is the
second Bolt-specific convention (`bolt.py:44`). The WKK regex skips an optional multi-digit footnote
ref before the value and requires a real whitespace separator so a greedy `\d*` cannot swallow the
leading digits of a multi-digit value (`bolt.py:625`). Certificats verts is charged in every region,
so a miss raises; WKK is optional. `test_taxes_split_correctly_per_region` asserts Flanders
renewables = `(1.17 + 0.39)/100` (cert + WKK, illustrative), proving the footnote skip and the sum.

### DSO overlays

Bolt maps every DSO sub-area the integration knows, region by region. A structural quirk that spans
all three parsers: `pdfplumber` sometimes renders a row vertically (one number per line), so the
regexes use `\s+` (which matches newlines) between values to handle both layouts.
`test_wallonia_dso_handles_vertical_layout` (`tests/test_bolt.py:194`) exercises this.

**Flanders (`_extract_flanders_dsos`, `bolt.py:1088`).** Eight Fluvius sub-areas via `_FLANDERS_LABELS`
(`bolt.py:641`). Note the label-to-key mapping is not one-to-one by name: `Fluvius Kempen` maps to
`DSO_FLUVIUS_IVEKA` and `Fluvius Midden-Vl` to `DSO_FLUVIUS_INTERGEM`. Each row has 8 numbers; the
extractor bills the digital (SMR3) block (columns 1-4 plus the prosumer column 8) and ignores the
trailing classic columns. Group 4 is the dedicated exclusive-night meter rate, lower than normal
digital distribution, so a night circuit is billed at it. `transport` is `0.0` (Flanders folds
transport into distribution). Flanders digital meters carry no prosumer tariff on the DSO side in
the general case, but Bolt still exposes a prosumer column, which is read into
`prosumer_eur_per_kva_year`. `test_flanders_dso_includes_transport_in_distribution`
(`tests/test_bolt.py:189`) checks Antwerpen: transport 0.0, distribution 0.0535, exclusive-night
0.0481 (< distribution), capacity 52.37 (all illustrative).

**Wallonia (`_extract_wallonia_dsos`, `bolt.py:1154`).** Five DSOs via `_WALLONIA_LABELS`
(`bolt.py:710`). Ten numbers per row: mono, jour, nuit, excl_nuit, PIC, MEDIUM, ECO, transport,
terme_fixe (EUR/an), prosumer (EUR/kVA/an). PIC/MEDIUM/ECO populate the CWaPE Tarif Impact band
columns (`distribution_pic` / `_medium` / `_eco`); `terme_fixe` becomes `data_management_per_year`.

The Wallonia parser carries the module's single largest land mine, the **RESA/REW label swap**
(`bolt.py:714`). In Bolt's `pdfplumber` text extraction the rows labeled `TECTEO RESA` and `WAVRE`
carry each other's values, so `_WALLONIA_LABELS` deliberately maps `TECTEO RESA -> DSO_REW` and
`WAVRE -> DSO_RESA` to un-swap them. This was verified against the regulator's rates and every other
supplier's PDF. After parsing, a runtime sanity check enforces the invariant that RESA's
`distribution_single` stays strictly cheaper than REW's (a Walloon-tariff pattern that holds for
every card parsed). The check uses a process-wide `_RESA_REW_LOGGED` latch (`bolt.py:124`) so it
rings HA's notification bell at most once per boot. Three outcomes (`bolt.py:768`):

- Both rows missing: stay quiet (the parser already raised on the wider drift).
- Only one row parsed: log at ERROR once (the surviving row may now carry the other DSO's values
  with nothing to compare against).
- Both parsed but the inequality flipped: log at ERROR once, meaning Bolt likely fixed the upstream
  layout and the compensating swap now inverts correct values, so it should be removed.

The swap needs manual re-validation at least every 6 months (last done 2026-05, next due 2026-11,
`bolt.py:706`). `test_resa_is_cheaper_than_rew_after_label_swap` (`tests/test_bolt.py:210`) guards
the invariant in CI.

**Brussels (`_extract_brussels_dsos`, `bolt.py:1240`).** One row, `Sibelga`, with six captured
numbers: mono, jour, nuit, excl_nuit, transport, terme_fixe (the prosumer trailing token is `-`).
The exclusive-night column (group 4) is wired into `distribution_exclusive_night` via the shared
`brussels_sibelga_overlay` builder (`bolt.py:1179`); earlier it was dropped, which made a Brussels
night meter fall back to off-peak, correct only while the two columns happened to be equal. The
Sibelga overlay also carries the Brussels Brugel OSP annual-fee table via `parse_brussels_osp`
(`bolt.py:831`,
`_pdf.py:553`); Bolt prints `Obligations de service publique` with a lowercase `s`, which the
case-insensitive helper handles. A missing Sibelga row returns an empty dict (permitted).
`test_brussels_extracts_sibelga` (`tests/test_bolt.py:239`) checks distribution 0.0996, off-peak
0.0753, exclusive-night 0.0753, transport 0.0227 (all illustrative).

## Quirks and historical bugs (the land mines)

- **Monthly fee, x12.** `€ N / mois` is multiplied by 12 for the annual convention; a miss raises,
  not returns 0 (`bolt.py:370`).
- **Split renewables.** Flanders renewables = `Certificats verts` + `WKK`, two separate lines
  (`bolt.py:44`, `bolt.py:634`).
- **VAT-incl.** Prices are already VAT-incl, so `vat_rate=0.0` (`bolt.py:358`). An extractor that
  ever ships ex-VAT numbers must set the parsed rate explicitly.
- **No parseable `valid_until`.** Bolt cards print `Carte Tarifaire Bolt Fixe <Month> <Year>` but no
  machine-readable validity date, so `parse_valid_until` returns `None` and the archive cross-check
  falls back to a textual month match on `_FR_MONTH_NAMES` (`bolt.py:152`, `bolt.py:152`).
- **U+2028 line separators.** Normalized to `\n` at the top of `parse_snapshot` (`bolt.py:488`);
  every downstream regex depends on that.
- **5 MB PDFs, slow CDN.** 60 s timeout to survive a 2-3x slowdown (issue #13, `bolt.py:227`).
  The CDN-slowness signature is a detail ending in `: TimeoutError` plus a missing per-supplier
  metrics row: a transient aiohttp timeout, not a regression. (Before `error_text`
  (`_pdf.py:86`) the same failure printed nothing after the colon, so an empty tail in an old
  run log means the same thing.)
- **First-of-month fixed fallback.** Missing current-month fix card falls back to the previous month
  with a warning; Brussels-local month math avoids the UTC seam (`bolt.py:231`). Only a genuinely
  absent card qualifies: a textless card and a transient fetch failure both re-raise, or they would
  serve last month's prices with no staleness signal at all.
- **RESA/REW swap.** Compensating label inversion plus a self-disarming ERROR invariant; re-validate
  every 6 months (`bolt.py:714`).
- **Negative injection second column.** July 2026 fix cards; the anchor token tolerates a minus
  (`bolt.py:537`).
- **Vertical `pdfplumber` rows.** Every DSO regex uses `\s+` to span one-number-per-line renders
  (`bolt.py:670`).
- **Exclusive-night everywhere.** `Prix mensuel` group 2 and Fluvius group 4 and the Sibelga column
  are all dedicated night-circuit rates, not day/peak rates (`bolt.py:433`, `bolt.py:680`,
  `bolt.py:820`).

## Test fixtures

Under `tests/fixtures/` (both around 5 MB, April 2026 cards, French-language, all three regions):

| Fixture | Card variant | Exercised by |
| --- | --- | --- |
| `bolt_fix.pdf` | Bolt Fixe (fixed) April 2026 | most tests: yearly fee, consumption rates, injection, per-region taxes, Wallonia/Flanders/Brussels DSOs, RESA/REW swap, `fetch_for_month` accept/reject |
| `bolt_variable.pdf` | Bolt Variable April 2026 | injection parity with fix, current-vs-annual bi-horaire selection, loud failure on missing Jour/Nuit |

Fixtures are loaded via `fixture_text("bolt_fix.pdf", layout=True)` (`tests/test_bolt.py:64`), which
routes through the `pdfplumber` layout extractor so tests see the same text the live path parses.

## When the card changes, look here

Ordered by likelihood of breaking when Bolt re-renders or restructures a card:

1. **DSO row regexes** (`_extract_flanders_dsos` `bolt.py:1088`, `_extract_wallonia_dsos`
   `bolt.py:719`, `_extract_brussels_dsos` `bolt.py:1240`). Column-count changes, a renamed sub-area
   label, or a new footnote marker breaks these first. A row that stops matching is silently dropped
   (Flanders/Brussels) or raises via the Wallonia invariant path.
2. **RESA/REW swap** (`_WALLONIA_LABELS` `bolt.py:1145`). If the ERROR invariant fires, Bolt probably
   fixed the upstream layout; remove the swap and re-point the labels straight.
3. **`_extract_energy` bi-horaire span** (`bolt.py:689`). The two-`Jour Nuit`-subhead anchor is
   fragile; if Bolt reorders the injection/consumption blocks or drops a subhead, the variable path
   raises loud.
4. **`_extract_yearly_fee`** (`bolt.py:558`). A phrasing change away from `€ N / mois` raises.
5. **`_extract_injection`** (`bolt.py:833`). A relabeled `Injection` header or a third
   consumption-side `Prix mensuel` row shifts the anchor; a new second-column sign convention needs
   the `-?` tolerance revisited.
6. **`_extract_taxes` / `_extract_renewables`** (`bolt.py:1041`, `bolt.py:1041`). Federal levy and
   certificats-verts misses raise; the connection-fee footnote `{0,3}` cap may need widening if Bolt
   adds markers.
7. **URL construction** (`_document_url` `bolt.py:248`, `_resolve_variable_suffix` `bolt.py:263`).
   A variable-version bump now resolves itself off the listing, and the live-check freshness gate
   fails the run if it ever stops doing so. What still needs a code change is a change in the
   filename *shape* -- a version that grows a letter, or a folder or slug rename -- which
   `_CARD_URL_RE` (`bolt.py:140`) would stop matching; `discover` (`bolt.py:338`) plus the
   live-check coverage diff flag that case.

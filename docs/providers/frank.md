# Provider: frank

This document is the maintenance reference for the Frank Energie Belgium extractor
(`providers/frank.py`). It explains how the extractor discovers Frank's monthly PDF
tariff cards through a Sanity CMS API, how it maps each of the five dynamic tiers to a
contract id, how the energy / injection / tax / DSO fields are parsed out of one shared
PDF layout, and the land mines a future maintainer must know when Frank changes its card.
The test module `tests/test_frank.py` is treated as ground truth throughout: it pins the
expected parse output against real fixtures, so the illustrative numbers below all come
from that test file or from source comments.

Related reading:

- [../provider-framework.md](../provider-framework.md) - the extractor protocol, the
  `SupplierExtractor` / `Contract` / `SupplierSnapshot` dataclasses, and the shared PDF
  helpers this module calls.
- [../pricing-model.md](../pricing-model.md) - how `DynamicRates`, `InjectionRates`,
  `TaxOverlay` and `DsoOverlay` are consumed by `compute_breakdown`.

## Overview

Frank Energie Belgium sells only dynamic (spot-indexed) electricity, and only in
Flanders. The module docstring (`providers/frank.py:26`) and `_FRANK_REGIONS`
(`providers/frank.py:125`) fix the region to `REGION_FLANDERS`; `fetch` rejects any other
region with "Frank Energie only operates in Flanders" (`providers/frank.py:217`). All
eight Fluvius sub-areas are covered (see the DSO section).

Frank publishes one PDF per tier per month, hosted as a Sanity CMS file asset. The
extractor never scrapes an HTML page and never downloads a fixed URL: it queries the
Sanity file-asset API with a GROQ expression, filters the returned filenames to the
requested tier, and downloads the winning asset's `url`. The API base is
`_SANITY_API = "https://8navd656.api.sanity.io/v2023-01-01/data/query/production-be"`
(`providers/frank.py:85`); it is public and needs no auth.

```
config (contract_id) ->  probe(): GROQ newest _createdAt  -> freshness key
                     \-> fetch():  GROQ "*Elektriciteit Dynamisch*"
                                     |
                                     v
                              _matches_suffix() filters rows to this tier
                                     |
                                     v
                              newest _createdAt row -> asset .url
                                     |
                                     v
                              fetch_pdf_text_layout() -> layout text
                                     |
                                     v
                              parse_snapshot() -> SupplierSnapshot
```

The `source_url` stored in the snapshot is the resolved Sanity asset URL (whatever
`_resolve_pdf_url` returned), not a stable human-facing page. The `publication_label` is
a lowercased "month year" string ("april 2026") reconstructed from the filename by
`_resolve_pdf_url` (`providers/frank.py:199`).

## Contracts

Five tiers are declared in `_TIERS` (`providers/frank.py:94`) and turned into `Contract`
objects by the `EXTRACTOR` comprehension (`providers/frank.py:492`). Every one is
`kind="dynamic"`, `regions=_FRANK_REGIONS` (Flanders only), and leaves
`spot_indexed_injection` at its default `False` (a dynamic contract already collects the
ENTSO-E key via its energy formula, so the injection regime does not need to gate it; see
`base.py:71`). None of them sets `quarter_hourly`, so all bill per clock hour: Frank is
listed in the `DynamicRates` docstring as an hourly-billing supplier
(`base.py:144`). The integration aggregates the ENTSO-E 15-minute curve to hourly for
these contracts.

| contract id | label | TariffKind | regions | filename suffix | quarter_hourly | note |
| --- | --- | --- | --- | --- | --- | --- |
| `frank_dynamic` | Frank Energie Dynamisch | dynamic | flanders | none (bare month) | False | standard tier |
| `frank_dynamic_hv` | Frank Energie Dynamisch HV | dynamic | flanders | `HV` | False | higher subscription, lower per-kWh margin |
| `frank_dynamic_korting` | Frank Energie Dynamisch Korting | dynamic | flanders | `VT` | False | 120 EUR cashback after 1 year |
| `frank_dynamic_jn` | Frank Energie Dynamisch JN | dynamic | flanders | `JN` | False | lower subscription, different formula and injection base |
| `frank_dynamic_slim` | Frank Energie Dynamisch Slim | dynamic | flanders | `SL` | False | requires smart devices (solar, EV, battery, heat pump) |

The tier descriptions come from the module docstring (`providers/frank.py:30`). Note the
suffix mapping is not identity: the Korting tier's PDF filename token is `VT`, not
`Korting`, and the Slim tier's token alternates between `SL` and the full word `Slim` from
month to month (both live in the CMS at once), so `_SUFFIX_ALIASES` treats them as aliases
(`providers/frank.py:112`). `test_matches_suffix_slim_accepts_both_sl_and_full_word`
(`tests/test_frank.py:354`) pins this behaviour.

No tier is retired. `discover()` (`providers/frank.py:264`) surfaces any unrecognised
suffix as `frank_dynamic_<suffix>` so the catalog-drift detector flags a genuinely new
sixth tier instead of silently ignoring it.

## Fetch strategy

### Discovery and download (`fetch`)

`fetch` (`providers/frank.py:210`) validates the contract id and region, calls
`_resolve_pdf_url(session, contract_id)` with no target month to get the latest card, then
`fetch_pdf_text_layout` to download and layout-extract the PDF, then `parse_snapshot`.

`_resolve_pdf_url` (`providers/frank.py:156`) builds a GROQ query. With no target month it
asks for every "Elektriciteit Dynamisch" file asset ordered newest first
(`[0..29]`):

```groq
*[_type=="sanity.fileAsset" && originalFilename match "*Elektriciteit Dynamisch*"]
  {originalFilename,url,_createdAt} | order(_createdAt desc)[0..29]
```

The returned rows are filtered through `_matches_suffix(filename, suffix)`
(`providers/frank.py:131`), which extracts the word after "Dynamisch" and checks: for the
standard tier (`suffix is None`) the word must be a Dutch month name (so a tier-suffixed
card is rejected); otherwise the word must be the tier suffix or one of its aliases. The
surviving rows are sorted by `_createdAt` descending and the newest is chosen
(`providers/frank.py:195`). A no-match raises `ExtractorError`
(`providers/frank.py:190`).

### Probe (freshness key)

`probe` (`providers/frank.py:243`) returns the `_createdAt` of the single newest
"Elektriciteit Dynamisch" asset across all tiers:

```groq
*[_type=="sanity.fileAsset" && originalFilename match "*Elektriciteit Dynamisch*"]
  | order(_createdAt desc)[0]{_createdAt}
```

It is intentionally tier-agnostic: any tier publishing a fresh card flips the key and the
coordinator re-fetches. Network or parse failure returns `None`, in which case the
coordinator's TTL takes over. Note this is a coarse probe: it can trigger a re-fetch for a
tier the user is not on, but that is cheap and safe.

The GROQ `[0]` selector is a maintenance land mine. When a GROQ query ends in `[0]`,
Sanity returns a single object, not a one-element array. `_sanity_query`
(`providers/frank.py:142`) normalises this: after `json.loads(...).get("result", [])` it
checks `isinstance(result, dict)` and wraps a lone dict in a list
(`providers/frank.py:149`). Do not remove that branch or `probe` and any future `[0]`
query will crash on `list(result)` returning the dict's keys.

### Archive support (`fetch_for_month`)

Frank has a real, queryable archive: past months' PDFs remain in the Sanity CMS.
`fetch_for_month` (`providers/frank.py:224`) passes `target_month=year_month` into
`_resolve_pdf_url`, which switches to the month-scoped GROQ query that adds
`originalFilename match "*<MonthName>*"` and `"*<year>*"` filters
(`providers/frank.py:171`). The Dutch month title comes from `_NL_MONTHS_TITLE`
(`providers/frank.py:90`) indexed by `target_month.month - 1`.

After parsing, the result is passed through `archive_validity_check(snap, text,
year_month, month_names=_NL_MONTHS)` (`providers/frank.py:240`). Because Frank cards carry
a parseable `valid_until` (see below), that check authoritatively rejects any snapshot
whose validity does not fall in the requested month, guarding against a CDN substituting
the wrong card (`_pdf.py:781`). Because `month_names` is supplied, a snapshot with no
`valid_until` still gets the textual month cross-check. Any `ExtractorError` during
resolution or parsing is swallowed and `fetch_for_month` returns `None`
(`providers/frank.py:238`), letting the coordinator fall back to the current snapshot as a
proxy.

## Parsing

`parse_snapshot` (`providers/frank.py:302`) assembles the `SupplierSnapshot` from five
sub-parsers. All five run against the layout-preserving text from
`fetch_pdf_text_layout` (which keeps column alignment, important for the DSO table).

| field | parser | source |
| --- | --- | --- |
| `energy` (`DynamicRates`) | `_extract_dynamic` | `providers/frank.py:341` |
| `injection` (`InjectionRates`) | `_extract_injection` | `providers/frank.py:382` |
| `taxes` (`TaxOverlay`) | `_extract_taxes` | `providers/frank.py:419` |
| `dsos` (`dict[str, DsoOverlay]`) | `_extract_dsos` | `providers/frank.py:454` |
| `valid_until` | `parse_valid_until` (shared) | `_pdf.py:794` |

### Number format

`_NUM = r"([\d]+(?:[.,][\d]+)?)"` (`providers/frank.py:328`) accepts both decimal
separators and `to_float` normalises either. The comment (`providers/frank.py:323`)
records why: a dot-decimal re-render of the card would otherwise truncate a mandatory tax
row to 0 or collapse the VAT multiplier 1,06 to 1 rather than failing loud.
`test_dot_decimal_render_matches_comma` (`tests/test_frank.py:132`) pins that a comma card
and its dot-replaced twin parse identically.

## Energy formula

`_extract_dynamic` (`providers/frank.py:341`) parses the PDF formula row with `_FORMULA_RE`
(`providers/frank.py:330`), which matches:

```
(<factor_pdf> x BELPEX per uur* <sign> <base_cents>) x <vat_mult>
```

The stored `DynamicRates` feeds `energy_eur_per_kwh = factor * spot + base` in the
integration's canonical EUR/kWh, where the spot is ENTSO-E BE day-ahead in EUR/kWh
(EUR/MWh / 1000). The card is in EURct/kWh with BELPEX in EUR/MWh, so
(`providers/frank.py:354`):

```
factor = factor_pdf * vat_mult * 10.0          # EURct/kWh -> EUR/kWh with MWh->kWh
base   = base_pre_vat_cents * vat_mult / 100.0
```

The VAT multiplier (1,06 for the 6% residential rate) is applied here because the energy
formula on the card is stated ex-VAT and multiplied by `x 1,06`. `test_energy_formula_factor`
(`tests/test_frank.py:64`) pins the standard April card at factor `0.1068 * 1.06 * 10`
(illustrative) and `test_energy_formula_base` at `1.500 * 1.06 / 100` (illustrative).

The monthly standing charge is parsed by `_MONTHLY_FEE_RE` matching
`Abonnementskost (EUR/maand) <num>` (`providers/frank.py:336`) and multiplied by 12 to
`yearly_fixed_fee` (`providers/frank.py:363`). A missing row is fatal: the comment notes
the ~35 EUR/yr charge is mandatory, so a miss raises "monthly fixed fee row not found"
rather than silently billing zero (`providers/frank.py:362`).
`test_missing_monthly_fee_is_fatal` (`tests/test_frank.py:177`) locks this. The April
fixture's 2,92 EUR/month resolves to 35.04 EUR/year (illustrative,
`tests/test_frank.py:78`).

## Injection

Frank's injection is the hourly `factor*spot+base` shape (shape (b) in the taxonomy in
[../pricing-model.md](../pricing-model.md)), not a monthly indicative and not the
spot-indexed-variable shape. `_extract_injection` (`providers/frank.py:382`) parses a
`terugleveringsvergoeding` row with `_INJECTION_RE` (`providers/frank.py:374`):

```
terugleveringsvergoeding: (<factor_pdf> x BELPEX per uur* <sign> <base_cents>)
```

Injection is VAT-exempt (Belgian residential feed-in is never VAT-incl,
`base.py:271`), so no `vat_mult` is applied (`providers/frank.py:396`):

```
factor = factor_pdf * 10.0
base   = base_cents / 100.0
```

A missing formula is fatal: every Frank dynamic card prints one, so a miss is layout
drift, not a fee-free contract, and raising avoids silently crediting a solar user 0
EUR/kWh (`providers/frank.py:385`). `test_missing_injection_is_fatal`
(`tests/test_frank.py:192`) pins this. The sign between BELPEX and the base is mandatory
in the regex; a sign-less or reworded formula misses and raises rather than defaulting to
minus (`providers/frank.py:391`).

The April fixture yields factor `0.1 * 10 = 1.0` and base `-1.150 / 100` (illustrative,
`tests/test_frank.py:217`). Watch the JN tier: it carries a different injection base
(-0,02 vs -0,0115 on the other four tiers), pinned by the parametrized tier test
(`tests/test_frank.py:250` and the comment at `tests/test_frank.py:272`).

Frank publishes no supplier-side prosumer / PV forfait; `parse_snapshot` leaves
`supplier_prosumer_eur_per_kva_year` at its default `None`. Flanders digital meters
(post-2024 SMR3) also carry no DSO prosumer tariff, so `DsoOverlay.prosumer_eur_per_kva_year`
stays `None` too.

## Taxes

`_extract_taxes` (`providers/frank.py:419`) parses five levy rows and builds a `TaxOverlay`.
All card values are VAT-inclusive (6% BTW), so `vat_rate=0.0` is set explicitly
(`providers/frank.py:447`, comment at :439) and pinned by `test_taxes_vat_rate_zero`
(`tests/test_frank.py:211`).

| overlay field | card row | regex | required |
| --- | --- | --- | --- |
| `federal_excise` | Bijzondere accijns op Energie (EURct/kWh) | `_EXCISE_RE` (`:408`) | yes |
| `energy_contribution` | Bijdrage op Energie (EURct/kWh) | `_ENERGY_CONTRIB_RE` (`:411`) | no (0.0 default, abolished 2026-08-01) |
| `flanders_renewables` | GSC + WKK (EURct/kWh) | `_GSC_RE` (`:412`), `_WKK_RE` (`:413`) | yes (both) |
| `energy_fund_eur_per_month` | Bijdrage Energiefonds Residentieel (EUR/maand) | `_FUND_RE` (`:414`) | no (0.0 default) |

`flanders_renewables` is the sum of the GSC (green-certificate) and WKK
(cogeneration) surcharges (`providers/frank.py:443`). Because Frank is Flanders-only, both
are mandatory renewables levies on every card; a miss raises "could not parse Frank
Energie GSC/WKK levies" rather than under-billing (`providers/frank.py:434`).
`test_missing_gsc_wkk_is_fatal` (`tests/test_frank.py:184`) locks this, and
`test_taxes_flanders_renewables_gsc_plus_wkk` pins GSC 1,166 + WKK 0,371 = 1,537 EURct/kWh
(illustrative, `tests/test_frank.py:200`).

The federal excise is mandatory; a miss raises "could not parse Frank Energie tax block"
(`providers/frank.py:422`), pinned by `test_missing_federal_excise_is_fatal`
(`tests/test_frank.py:169`).

The federal energy contribution used to be mandatory too. It dropped to zero on
2026-08-01 and Frank deleted the row from the card outright rather than printing a zero,
which took every Frank contract offline (issue #49). An absent row is now read as the
levy being abolished and defaults to 0.0 (`providers/frank.py:430`, comment at :423);
`test_august_card_drops_the_energy_contribution_row` (`tests/test_frank.py:150`) pins it
against the August 2026 fixture, and the April fixture still pins the pre-reform 0,2042.
The energy fund is
optional and defaults to 0.0; the April fixture has no residential fund row, pinned by
`test_taxes_energy_fund_residential_zero` (`tests/test_frank.py:206`). All EURct/kWh values
are divided by 100 to reach EUR/kWh.

## DSO overlay

`_extract_dsos` (`providers/frank.py:454`) covers all eight Fluvius sub-areas via
`_FLUVIUS_LABELS` (`providers/frank.py:114`), which maps the card's human label to the
canonical DSO key:

| card label | canonical key |
| --- | --- |
| Antwerpen | `fluvius_antwerpen` |
| Halle-Vilvoorde | `fluvius_halle_vilvoorde` |
| Imewo | `fluvius_imewo` |
| Kempen | `fluvius_iveka` |
| Limburg | `fluvius_limburg` |
| Midden-Vlaanderen | `fluvius_intergem` |
| West | `fluvius_west` |
| Zenne-Dijle | `fluvius_zenne_dijle` |

Note Kempen -> `fluvius_iveka` and Midden-Vlaanderen -> `fluvius_intergem`: the card's
regional trade name is not the canonical key. `test_dsos_cover_all_eight_fluvius_subareas`
(`tests/test_frank.py:85`) asserts all eight are present.

The parser first narrows to the digital-meter section between the "Digitale meter" and
"Klassieke meter" markers (`providers/frank.py:455`); a missing "Digitale meter" marker
raises "could not locate Frank Energie DSO table". For each label it matches a row of the
form `Fluvius [<label>]` followed by four numbers on their own lines
(`providers/frank.py:464`):

```
databeheer  -> data_management_per_year   (EUR/year, no /100)
capacity    -> capacity_eur_per_kw_year   (EUR/kW/year, no /100)
normal      -> distribution_single        (EURct/kWh /100)
excl_night  -> distribution_exclusive_night (EURct/kWh /100)
```

`transport` is always 0.0: transport is bundled into distribution on Frank's card, pinned
by `test_dso_transport_is_zero` (`tests/test_frank.py:106`). A label that does not match is
skipped (not fatal), so a single relabelled sub-area drops out silently rather than failing
the whole snapshot; the eight-sub-area test is the safety net.

Two layout hurdles are handled in the row regex:

- The bracket around the label is character-class-tolerant: `[\[\(]...[\]\)]`
  (`providers/frank.py:465`) accepts a mismatched open/close bracket. Frank's PDF renders
  Kempen as `Fluvius [Kempen)` with mismatched brackets;
  `test_dso_kempen_despite_bracket_artifact` (`tests/test_frank.py:113`) pins that it still
  parses.
- The hyphen in labels like "Halle-Vilvoorde" is loosened to `[\s\-]*` so a space or
  missing hyphen in the rendered text still matches (`providers/frank.py:463`).

`test_dso_antwerpen_distribution` and `test_dso_antwerpen_capacity_and_databeheer`
(`tests/test_frank.py:91`) pin the Antwerpen row (illustrative: normaal 5,35 ct/kWh, excl
nacht 4,81 ct/kWh, capacity 52,37 EUR/kW/yr, databeheer 18,92 EUR/yr).

## valid_until

`parse_valid_until` (`_pdf.py:794`) is the shared best-effort validity parser; Frank cards
resolve to the last day of the pricing month. `test_valid_until_is_end_of_april`
(`tests/test_frank.py:292`) pins the April fixture to a date in month 4, year 2026. This
parsed date is what makes `archive_validity_check` authoritative in `fetch_for_month`.

## Quirks and historical bugs (land mines)

- GROQ `[0]` returns a dict, not a list. `_sanity_query` wraps a lone dict
  (`providers/frank.py:149`); removing that branch breaks `probe`.
- Suffix is not the tier name. Korting's filename token is `VT`, Slim's is `SL` or the
  full word `Slim` (aliased in `_SUFFIX_ALIASES`, `providers/frank.py:112`). Both Slim
  spellings are live simultaneously.
- Both decimal separators must be accepted (`_NUM`, `providers/frank.py:328`); a
  comma-only regex truncated dot-rendered values (`test_dot_decimal_render_matches_comma`).
- VAT applied only to energy. The `x 1,06` multiplier scales the energy factor and base
  (`providers/frank.py:354`) but not injection (VAT-exempt, `providers/frank.py:396`).
  Card levy values are already VAT-incl, so `TaxOverlay.vat_rate=0.0`
  (`providers/frank.py:447`).
- Fail-loud on missing mandatory rows: energy formula, monthly fee, injection formula,
  federal excise, and GSC/WKK all raise `ExtractorError` rather than silently zeroing.
  The energy contribution is the one federal row that is NOT mandatory any more — see
  Taxes below. Five fatal-miss tests guard the rest (`tests/test_frank.py:177`-197,
  :184, :192, and `test_missing_federal_excise_is_fatal` at :169).
- Mismatched bracket artifact in the DSO table (`Fluvius [Kempen)`) is tolerated by the
  character-class brackets (`providers/frank.py:465`).
- Kempen and Midden-Vlaanderen map to non-obvious canonical keys (`fluvius_iveka`,
  `fluvius_intergem`).
- JN tier's injection base differs (-0,02 vs -0,0115 elsewhere,
  `tests/test_frank.py:272`).
- Hourly billing: no tier sets `quarter_hourly`, so the integration aggregates ENTSO-E's
  15-minute curve to hourly (`base.py:144`).

## Test fixtures

Fixtures live under `tests/fixtures/`. Each is a real Frank PDF for one tier and month:

| fixture | tier | represents |
| --- | --- | --- |
| `frank_dynamic_apr.pdf` | `frank_dynamic` | standard tier, April 2026; the primary fixture, drives most assertions |
| `frank_dynamic_hv_jun.pdf` | `frank_dynamic_hv` | HV tier, June; higher subscription |
| `frank_dynamic_korting_jun.pdf` | `frank_dynamic_korting` | Korting tier (filename token `VT`), June |
| `frank_dynamic_jn_jun.pdf` | `frank_dynamic_jn` | JN tier, June; different formula and injection base |
| `frank_dynamic_slim_may.pdf` | `frank_dynamic_slim` | Slim tier (`SL`), May |
| `frank_dynamic_aug.pdf` | `frank_dynamic` | standard tier, August 2026; the first card with the energy-contribution row deleted |

The five tiers share one PDF layout, but only the default tier had a fixture originally;
`test_non_default_tiers_extract_energy_and_injection` (`tests/test_frank.py:231`) was added
with the other four fixtures to catch a tier-specific card regression. Tests load fixtures
through `fixture_text(name, layout=True)` (`tests/test_frank.py:47`), matching the
layout-preserving extraction used in production.

## When the card changes, look here

| symptom | likely culprit | why |
| --- | --- | --- |
| A tier stops fetching, or a new sixth tier is ignored | `_TIERS`, `_TIER_SUFFIX`, `_SUFFIX_ALIASES`, `_matches_suffix` (`providers/frank.py:94`-139) | filename token renamed or a new suffix appears; `discover` will surface `frank_dynamic_<suffix>` |
| "could not parse Frank Energie energy formula" | `_FORMULA_RE` (`:330`) | BELPEX wording, sign chars, or the `x 1,06` multiplier changed on the card |
| Wrong per-kWh price after a card update | the EURct->EUR conversion in `_extract_dynamic` (`:354`) | Frank switched units or dropped the VAT multiplier |
| "monthly fixed fee row not found" | `_MONTHLY_FEE_RE` (`:336`) | "Abonnementskost (EUR/maand)" label reworded |
| Solar credit wrong or "injection formula row not found" | `_INJECTION_RE` (`:374`) | "terugleveringsvergoeding" reworded or the sign dropped |
| Tax under/over-billing or "tax block"/"GSC/WKK" errors | `_extract_taxes` regexes (`:408`-416) | a levy row label changed; energy fund is the only optional one |
| A DSO sub-area missing, or all DSOs missing | `_FLUVIUS_LABELS` and the row regex in `_extract_dsos` (`:114`, `:457`); the "Digitale meter"/"Klassieke meter" section markers (`:448`) | a label renamed, a new bracket artifact, or the section headers changed |
| Historical months mis-billed | `_resolve_pdf_url` month query (`:169`) and `archive_validity_check` call (`:240`) | the filename month/year tokens or validity text changed |
| Coordinator never refreshes, or refreshes constantly | `probe` GROQ and the `[0]` dict handling in `_sanity_query` (`:243`, `:149`) | Sanity changed the response shape |

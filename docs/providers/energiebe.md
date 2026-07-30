# Provider: energiebe

This document is the maintenance reference for the energie.be extractor
(`providers/energiebe.py`). It explains how the extractor fetches energie.be's single
dynamic residential tariff card from the site's document API, how the energy / injection /
tax / DSO fields are parsed out of the residential half of a card that also carries a
professional block, and the land mines a future maintainer must know when energie.be
changes its card. The test module `tests/test_energiebe.py` is treated as ground truth
throughout: it pins the expected parse output against a real fixture.

Related reading:

- [../provider-framework.md](../provider-framework.md) - the extractor protocol, the
  `SupplierExtractor` / `Contract` / `SupplierSnapshot` dataclasses, and the shared PDF
  helpers this module calls.
- [../pricing-model.md](../pricing-model.md) - how `DynamicRates`, `InjectionRates`,
  `TaxOverlay` and `DsoOverlay` are consumed by `compute_breakdown`.
- [frank.md](frank.md) - the closest sibling; energie.be reuses Frank's dynamic parsing
  shape but differs on units, fetch and residential scoping (see below).

## Overview

energie.be sells one dynamic (spot-indexed) residential electricity product the
integration tracks, "Elektriciteit dynamisch tarief particulier", and only in Flanders.
The module docstring (`providers/energiebe.py:26`) and `_ENERGIEBE_REGIONS`
(`providers/energiebe.py:89`) fix the region to `REGION_FLANDERS`; `fetch` rejects any
other region with "energie.be only operates in Flanders" (`providers/energiebe.py:164`).
All eight Fluvius sub-areas are covered (see the DSO section). The card also carries a
professional block; only the residential rows are parsed (see [Residential scoping]).

energie.be publishes its cards as PDFs behind a small JSON document API. The dynamic card
lives at one stable URL, `_CARD_URL` (`providers/energiebe.py:82`):

```
https://energie-production-api.azurewebsites.net/api/v1/data/document?key=DynamicTariffs
```

A GET on that URL answers `302 Found` and redirects to the current month's versioned Azure
blob (e.g. `energie.blob.core.windows.net/cms/assets/..._Dynamisch_<hash>.pdf`); aiohttp
follows the redirect automatically, so the extractor treats it as a single stable URL
whose body is replaced each month. This is the DATS 24 fetch shape: no HTML scraping, no
discovery, no cheap probe, no archive.

```
config (contract_id) -> fetch(): GET _CARD_URL (302 -> Azure blob, aiohttp follows)
                                    |
                                    v
                             fetch_pdf_text_layout() -> layout text
                                    |
                                    v
                             _residential() -> drop the professional block
                                    |
                                    v
                             parse_snapshot() -> SupplierSnapshot
```

The `publication_label` is a lowercased "month year" string ("juli 2026") reconstructed
from the residential card header by `_publication_label` (`providers/energiebe.py:199`).

## Contracts

One contract is declared in the `EXTRACTOR` (`providers/energiebe.py:306`):

| contract id | label | TariffKind | regions | quarter_hourly |
| --- | --- | --- | --- | --- |
| `energiebe_dynamic` | energie.be Dynamisch | dynamic | flanders | True |

`quarter_hourly=True` (`providers/energiebe.py:227`): the card bills "op kwartierbasis" on
the Day-Ahead EPEX SPOT Belgium 15-minute curve, so the live price table, next-slot sensor
and cheapest-window service keep the native 15-minute slots (`base.py:144`). YTD billing
stays hourly. `spot_indexed_injection` is left at its default `False`: a dynamic contract
already collects the ENTSO-E key via its energy formula, so the injection regime does not
need to gate it (`base.py:71`).

## Fetch strategy

### Download (`fetch`)

`fetch` (`providers/energiebe.py:157`) validates the contract id and region, calls
`fetch_pdf_text_layout(session, _CARD_URL)` to download and layout-extract the PDF (the
layout extractor keeps column alignment, important for the DSO table), then
`parse_snapshot`. Validation is by `%PDF` magic bytes in the shared helper
(`_pdf.py:126`), not Content-Type, so the JSON-style API URL and its blob redirect work.

### Probe and archive

There is neither a probe nor an archive:

- `EXTRACTOR.probe` is `None`. A HEAD on `_CARD_URL` answers `405 Method Not Allowed`, so
  the shared `head_freshness_key` helper cannot produce a key. The coordinator falls back
  to its time-based TTL, which is adequate for a card that only rotates monthly (the live
  price comes from the ENTSO-E spot each tick, not from the card).
- `EXTRACTOR.fetch_for_month` is `None`. The document API overwrites the single key in
  place with no month-scoped archive, so past months bill at the current snapshot as a
  proxy, the same as DATS 24 / Ecofix.

## Residential scoping

The `?key=DynamicTariffs` PDF bundles a residential block (pages 1-2) and a professional
block (pages 3-4). The two blocks share the same energy and injection formula but differ on
GSC/WKK, the tax rows and the DSO net-tariff table (e.g. residential databeheer 18,92
EUR/yr vs professional 17,85). `_residential` (`providers/energiebe.py:193`) slices the
text at the professional section header `_PROF_MARKER = "dynamisch tarief professioneel"`
(`providers/energiebe.py:103`) so no professional row can leak into a residential snapshot.
`test_only_residential_block_is_parsed` (`tests/test_energiebe.py`) pins that the parsed
renewables and every DSO databeheer come from the residential rows.

The marker is the section header only ("Elektriciteit dynamisch tarief professioneel"); the
residential page's "Overzicht actief aanbod" list mentions "Elektriciteit dynamisch
professioneel" without "tarief", so it does not trip the slice.

## Parsing

`parse_snapshot` (`providers/energiebe.py:173`) runs `_residential` first, then assembles
the `SupplierSnapshot` from four sub-parsers plus the shared `parse_valid_until`.

| field | parser | source |
| --- | --- | --- |
| `energy` (`DynamicRates`) | `_extract_energy` | `providers/energiebe.py:204` |
| `injection` (`InjectionRates`) | `_extract_injection` | `providers/energiebe.py:231` |
| `taxes` (`TaxOverlay`) | `_extract_taxes` | `providers/energiebe.py:249` |
| `dsos` (`dict[str, DsoOverlay]`) | `_extract_dsos` | `providers/energiebe.py:274` |
| `valid_until` | `parse_valid_until` (shared) | `_pdf.py:794` |

`_NUM = r"([\d]+(?:[.,][\d]+)?)"` accepts both decimal separators; a dot-decimal re-render
must not truncate a mandatory value to its integer part.
`test_dot_decimal_render_matches_comma` pins that a comma card and its dot-replaced twin
parse identically.

## Units: Belpex is in c€/kWh, not EUR/MWh

The single most important difference from Frank / Bolt. Those cards print their formula
against Belpex in EUR/MWh, so their extractors scale the factor by 10. energie.be writes
the formula as `(1,04 x Belpex + 0,50) c€/kWh` with **Belpex in c€/kWh**, verified against
the card's own printed price: the shown 11,93 c€/kWh (incl. VAT) equals
`(1,04 x 10,34 + 0,50) x 1,06`. So the spot coefficient is NOT scaled by 10 - getting this
wrong would 10x the energy leg. See the conversion in `_extract_energy`
(`providers/energiebe.py:214`).

## Energy formula

`_extract_energy` (`providers/energiebe.py:204`) parses the formula row with `_ENERGY_RE`
(`providers/energiebe.py:124`), anchored on "formule (excl. BTW):" so it binds the energy
formula and not the injection one that shares the `(factor x Belpex +/- base)` shape:

```
de formule (excl. BTW): (<factor_pdf> x Belpex <sign> <base_cents>) c€/kWh
```

The stored `DynamicRates` feeds `energy_eur_per_kwh = factor * spot + base` where the spot
is ENTSO-E BE day-ahead in EUR/kWh. With Belpex in c€/kWh and the formula quoted ex-VAT
(`providers/energiebe.py:214`):

```
factor = factor_pdf * _VAT_MULT           # no * 10: Belpex already c€/kWh
base   = base_cents / 100.0 * _VAT_MULT
```

`_VAT_MULT = 1.06` (`providers/energiebe.py:98`) is the Belgian residential rate. It is a
constant, not scraped: the formula is stated ex-VAT while every other card value is
VAT-inclusive, and the card's only printed percentage (21% on energiedelen) is unrelated.
Scaling the energy leg to the VAT-inclusive basis keeps `TaxOverlay.vat_rate=0.0`, the same
convention as Frank. `test_energy_formula_factor` pins `1.04 * 1.06` and
`test_energy_formula_base` pins `0.50 / 100 * 1.06`.

The yearly fixed fee ("vaste vergoeding") is parsed by `_FEE_RE` matching
`Vaste vergoeding <num> (.../jaar)` (`providers/energiebe.py:137`). Unlike Frank's
per-month "Abonnementskost", energie.be quotes it already annual (25 EUR/jaar), so it is
carried through unscaled - no x12 (`test_yearly_fixed_fee_is_already_annual`). A missing row
is fatal: "energie.be: vaste vergoeding row not found" (`providers/energiebe.py:225`,
`test_missing_fee_is_fatal`).

## Injection

Injection is the hourly `factor*spot+base` shape (shape (b) in the taxonomy in
[../pricing-model.md](../pricing-model.md)); on a dynamic card it prices off the live spot
the energy path already fetches, so `current` stays `None`. `_extract_injection`
(`providers/energiebe.py:231`) parses the `terugleveringsvergoeding` row with
`_INJECTION_RE` (`providers/energiebe.py:132`):

```
injectievergoeding ... (<factor_pdf> x Belpex <sign> <base_cents>)
```

The regex anchors on "injectievergoeding" and skips with `.*?` (DOTALL) to the first
`(factor x Belpex +/- base)`, because the card interleaves the unit label "(c€/kWh)"
between "de formule:" and the parenthesised formula. Anchoring on "injectievergoeding"
(which follows the energy formula) also guarantees the injection formula is matched, not
the energy one. Injection is VAT-exempt (`base.py:268`) and Belpex is in c€/kWh, so
(`providers/energiebe.py:242`):

```
factor = factor_pdf          # no * 10, no VAT
base   = base_cents / 100.0
```

A missing formula is fatal: "energie.be: injection formula row not found"
(`providers/energiebe.py:236`, `test_missing_injection_is_fatal`). The July fixture yields
factor `1.0` and base `-0,98 / 100` (`test_injection_factor`, `test_injection_base`).
energie.be publishes no supplier-side prosumer / PV forfait, so
`supplier_prosumer_eur_per_kva_year` stays `None`.

## Taxes

`_extract_taxes` (`providers/energiebe.py:249`) parses four levy rows and builds a
`TaxOverlay`. All card values are VAT-inclusive (the federal excise and the energy fund are
VAT-exempt), so `vat_rate=0.0` is set explicitly (`test_taxes_vat_rate_zero`).

| overlay field | card row | regex | required |
| --- | --- | --- | --- |
| `federal_excise` | Bijzondere accijns op Energie (c€/kWh) | `_EXCISE_RE` (`:140`) | yes |
| `energy_contribution` | Bijdrage op de Energie (c€/kWh) | `_CONTRIB_RE` (`:143`) | yes |
| `flanders_renewables` | GSC + WKK (c€/kWh) | `_GSC_RE` (`:138`), `_WKK_RE` (`:139`) | yes (both) |
| `energy_fund_eur_per_month` | Bijdrage Energiefonds Residentieel (EUR/maand) | `_FUND_RE` (`:146`) | no (0.0 default) |

Note the label differences from Frank: energie.be prints the unit as `(c€/kWh)` (Frank uses
`(EURct/kWh)`) and the contribution as "Bijdrage op **de** Energie" (Frank omits "de"). The
tax regexes tolerate the unit with `\([^)]*\)` so a font quirk in the `€` glyph does not
break them. `_FUND_RE` anchors on "Residentieel" immediately after "Energiefonds", so the
sibling "Niet-Residentieel" (VAT-exempt, non-residential) row cannot match; the residential
fund is 0 (`test_taxes_energy_fund_residential_zero`).

`flanders_renewables` is the sum of GSC and WKK. Because energie.be dynamic is
Flanders-only, both are mandatory; a miss raises "could not parse energie.be GSC/WKK
levies" (`test_missing_gsc_wkk_is_fatal`). `test_taxes_flanders_renewables_gsc_plus_wkk`
pins GSC 1,17 + WKK 0,39 = 1,56 c€/kWh. All c€/kWh values are divided by 100.

## DSO overlay

`_extract_dsos` (`providers/energiebe.py:274`) covers all eight Fluvius sub-areas via
`_DSO_ROWS` (`providers/energiebe.py:109`), which maps each card label prefix to the
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
asserts all eight are present.

The parser narrows to the "Nettarieven" section, then for each label matches
`Fluvius (<prefix>)` followed by the first four numbers (the digital-meter columns):

```
databeheer  -> data_management_per_year    (EUR/year, no /100)
capacity    -> capacity_eur_per_kw_year    (EUR/kW/year, no /100)
normal      -> distribution_single         (c€/kWh /100)
excl_night  -> distribution_exclusive_night (c€/kWh /100)
```

The row regex uses `Fluvius\s*\(\s*<prefix>[^\d]*` then four `_NUM` columns, so it tolerates
the card wrapping two long labels across the number row -
`Fluvius (Halle-\n<numbers>\nVilvoorde)` and the Midden-Vlaanderen row -
because the numbers still follow the leading fragment. `test_dso_wrapped_label_halle_vilvoorde`
and `test_dso_wrapped_label_midden_vlaanderen` pin both wrapped rows.

`transport` is always 0.0 (bundled into distribution, `test_dso_transport_is_zero`). The
klassieke-meter and prosumer columns that follow on the card are ignored: a dynamic-contract
customer must have a digital SMR3 meter, so the reverse-running-meter prosumer regime does
not apply, and `DsoOverlay.prosumer_eur_per_kva_year` stays `None` (matching Frank). A label
that does not match is skipped (not fatal); the eight-sub-area test is the safety net.

## valid_until

`parse_valid_until` (`_pdf.py:794`) is the shared best-effort validity parser. energie.be's
card carries no month name inside a validity-keyword window (the "juli 2026" sits in the
page header, not after "geldig"), so `valid_until` resolves to `None`. That is the
documented "treat as available" fallback and is correct for a dynamic contract, whose
tomorrow prices come from the ENTSO-E day-ahead publication rather than the card.

## Quirks and historical bugs (land mines)

- **Belpex is c€/kWh, not EUR/MWh.** The energy and injection factors are NOT scaled by 10,
  unlike Frank / Bolt (`providers/energiebe.py:214`, `:242`). Verified against the printed
  11,93 c€/kWh incl. VAT.
- **Two blocks in one PDF.** `_residential` must run before any parsing or the professional
  GSC/WKK, taxes and DSO rows leak in (`providers/energiebe.py:193`).
- **Injection unit label interleaved.** "(c€/kWh)" sits between "de formule:" and the
  injection formula, so `_INJECTION_RE` anchors on "injectievergoeding" and skips to the
  first parenthesised formula (`providers/energiebe.py:132`).
- **Yearly fee is already annual.** No x12, unlike Frank's per-month Abonnementskost
  (`providers/energiebe.py:224`).
- **Wrapped DSO labels.** Halle-Vilvoorde and Midden-Vlaanderen wrap across the number row;
  the `[^\d]*` gap in the row regex absorbs it (`providers/energiebe.py:279`).
- **Label differences from Frank.** Unit `(c€/kWh)` not `(EURct/kWh)`; "Bijdrage op de
  Energie" not "Bijdrage op Energie"; the tax regexes are energie.be-specific.
- **No probe, no archive.** HEAD is 405 and the API overwrites in place; the coordinator
  uses its time-based TTL and bills past months at the current snapshot.
- **Kempen and Midden-Vlaanderen map to non-obvious keys** (`fluvius_iveka`,
  `fluvius_intergem`).

## Test fixtures

The fixture lives under `tests/fixtures/`:

| fixture | represents |
| --- | --- |
| `energiebe_dynamic_jul.pdf` | the full `?key=DynamicTariffs` PDF (residential + professional), July 2026; drives every assertion |

Tests load it through `fixture_text("energiebe_dynamic_jul.pdf", layout=True)`, matching the
layout-preserving extraction used in production.

## When the card changes, look here

| symptom | likely culprit | why |
| --- | --- | --- |
| "could not parse energie.be energy formula" | `_ENERGY_RE` (`:124`) | the "formule (excl. BTW):" wording, Belpex wording or sign chars changed |
| Wrong per-kWh price after a card update | the c€/kWh conversion in `_extract_energy` (`:214`) | energie.be switched Belpex units (to EUR/MWh) or the VAT treatment changed |
| "energie.be: vaste vergoeding row not found" | `_FEE_RE` (`:137`) | the "Vaste vergoeding ... (€/jaar)" label reworded |
| Solar credit wrong or "injection formula row not found" | `_INJECTION_RE` (`:132`) | "injectievergoeding" reworded or the sign dropped |
| Tax under/over-billing or "tax block"/"GSC/WKK" errors | `_extract_taxes` regexes (`:140`-146) | a levy row label or unit changed; energy fund is the only optional one |
| Professional rows leaking into the snapshot | `_PROF_MARKER` / `_residential` (`:103`, `:193`) | the professional section header wording changed |
| A DSO sub-area missing, or all DSOs missing | `_DSO_ROWS` and the row regex in `_extract_dsos` (`:109`, `:279`); the "Nettarieven" anchor | a label renamed, a new wrap artifact, or the section header changed |
| Coordinator never refreshes | none - there is no probe; the time-based TTL drives refetch | expected for this supplier |

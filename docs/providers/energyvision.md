# Provider: energyvision

This document is the maintenance reference for the EnergyVision extractor
(`providers/energyvision.py`). It explains how the extractor resolves EnergyVision's
monthly "Goedkope stroom" tariff cards off the site listing, how the energy / injection /
tax / DSO fields are parsed for the two supported products, and the land mines a future
maintainer must know when EnergyVision changes its cards. The test module
`tests/test_energyvision.py` is treated as ground truth throughout: it pins the expected
parse output against two real fixtures.

Related reading:

- [../provider-framework.md](../provider-framework.md) - the extractor protocol, the
  `SupplierExtractor` / `Contract` / `SupplierSnapshot` dataclasses, and the shared PDF
  helpers this module calls.
- [../pricing-model.md](../pricing-model.md) - how `DynamicRates`, `FixedRates`,
  `InjectionRates`, `TaxOverlay` and `DsoOverlay` are consumed by `compute_breakdown`.
- [bolt.md](bolt.md) - the closest sibling for the dynamic energy math: EnergyVision's
  dynamic card prints Belpex in EUR/MWh HTVA, the same axis as Bolt Dynamisch (see below).
- [energiebe.md](energiebe.md) - the closest sibling for the Flanders DSO + tax table
  shape (both cards carry the eight-Fluvius net-tariff table and the same tax block).

## Overview

EnergyVision sells a "Goedkope stroom" family of residential electricity cards; the
integration tracks two of them, both in Flanders only:

- **GSDYN** ("Goedkope Stroom Dynamisch") - a quarter-hourly spot-indexed dynamic product.
- **GS3JV** ("Goedkope stroom 3 jaar vast") - a flat 3-year fixed rate.

`_REGIONS` (`providers/energyvision.py:95`) fixes the region to `REGION_FLANDERS`; `fetch`
rejects any other region with "EnergyVision cards are Flanders-only"
(`providers/energyvision.py:210`). Wallonia (`-WAL-fr` cards) and gas are separate cards
that are out of scope here. The other NL electricity SKUs (GSVI3 / GS1800V / GSLP / GSEZ /
GSEZLP) are per-volume tiered products the pricing model cannot represent (a first-N-kWh
fixed block plus a variable remainder), so they are catalogued-but-declined; GRSO is a
transient group-buy SKU. `DISCOVER_IDS` (`providers/energyvision.py:117`) lists all of
them so `discover()` only flags a genuinely new code.

EnergyVision publishes its cards as PDFs named `EV-<MMYY>-<CODE>-<lang>.pdf` under
`/sites/default/files/inline-files/`. The filename carries the pricing month
(`EV-0726-GSDYN-nl.pdf` = July 2026) and the Drupal CMS adds dedup suffixes (the fixed
card ships as `EV-0726-GS3JV-nl_0.pdf`), so a constructed URL would miss it. The fetch
therefore scrapes the current card href off the tariefkaart listing page
(`_LISTING_URL = https://www.energyvision.be/nl-be/tariefkaart`,
`providers/energyvision.py:94`) - the Mega / Frank listing-resolution shape.

```
config (contract_id) -> fetch(): GET listing -> regex the EV-<MMYY>-<CODE>-nl href
                                    |
                                    v
                             fetch_pdf_text_layout() -> layout text
                                    |
                                    v
                             parse_snapshot(contract_id, ...) -> SupplierSnapshot
```

The `publication_label` is a lowercased "month year" string ("juli 2026") from the card
header via `_publication_label` (`providers/energyvision.py:283`).

## Contracts

Two contracts are declared in the `EXTRACTOR` (`providers/energyvision.py:399`), built from
`_CONTRACTS` (`providers/energyvision.py:106`):

| contract id | label | TariffKind | code | regions | quarter_hourly |
| --- | --- | --- | --- | --- | --- |
| `energyvision_dynamic` | EnergyVision Dynamisch | dynamic | GSDYN | flanders | True |
| `energyvision_fixed_3y` | EnergyVision 3 jaar vast | fixed | GS3JV | flanders | n/a |

`quarter_hourly=True` on the dynamic card (`providers/energyvision.py:315`): it bills "op
kwartierbasis" on the Day-Ahead EPEX SPOT Belgium 15-minute curve, so the live price table,
next-slot sensor and cheapest-window service keep the native 15-minute slots. YTD billing
stays hourly. `spot_indexed_injection` is left at its default `False` for both: the dynamic
card already collects the ENTSO-E key via its energy formula, and the fixed card's
injection is a monthly indicative that needs no live spot.

## Fetch strategy

### Resolve + download (`fetch`, `_resolve_card_url`)

`fetch` (`providers/energyvision.py:201`) validates the contract id and region, then
`_resolve_card_url` (`providers/energyvision.py:241`) GETs the listing HTML and regexes the
first `href="/sites/default/files/inline-files/EV-<4 digits>-<CODE>-nl...pdf"` for the
contract's code. The `[^"]*` before `.pdf` tolerates the Drupal `_0` dedup suffix. The
resolved absolute URL is layout-extracted with `fetch_pdf_text_layout` (the layout
extractor keeps column alignment, important for the DSO table), then parsed.

### Probe

`EXTRACTOR.probe` = `probe` (`providers/energyvision.py:216`): a cheap
`head_freshness_key` HEAD on the listing page (ETag / Last-Modified). That key flips when
EnergyVision rotates the monthly cards, which is exactly when the resolved PDF URL changes,
so the coordinator re-fetches on a month roll rather than on the time-based TTL.

### Discover + archive

`discover` (`providers/energyvision.py:231`) returns the residential NL electricity product
codes on the listing (`EV-<MMYY>-<CODE>-nl`), which live_check diffs against `DISCOVER_IDS`
to flag a new SKU. `EXTRACTOR.fetch_for_month` is `None`: the listing only exposes the
current month and old versioned URLs are not reliably reachable, so past months bill at the
current snapshot as a proxy, the same as DATS 24 / Ecofix / energie.be.

## Parsing

`parse_snapshot` (`providers/energyvision.py:256`) dispatches on the contract kind
(dynamic -> `_extract_dynamic`, fixed -> `_extract_fixed`) for the energy + injection legs,
and shares `_extract_dsos` + `_extract_taxes` across both cards (their DSO and tax tables
are identical). `_NUM = r"([\d]+(?:[.,][\d]+)?)"` accepts both decimal separators; a
dot-decimal re-render must not truncate a mandatory value.
`test_dot_decimal_render_matches_comma` pins that a comma card and its dot-replaced twin
parse identically.

## Units: the dynamic card is EUR/MWh HTVA (Bolt axis, no c€/kWh trap)

The most important thing to get right. EnergyVision's dynamic card writes the formula as
`1,05 x Belpex per kwartier + 15 EUR/MWh` **(exclusief btw)**, with Belpex in **EUR/MWh** -
the same axis as Bolt / Frank, *not* energie.be's c€/kWh axis. The `+15` and `-15`
constants are stated in EUR/MWh, and the printed headline (12,63 c€/kWh incl. VAT) confirms
the scaling: `1,05 x 1,06 x Belpex + 15/1000 x 1,06` reconciles to the printed value at a
plausible annual-average Belpex. The coefficient `1,05` is a **dimensionless Belpex
multiplier**, so it is NOT scaled by 10 the way Frank's cents-output coefficient is -
applying the x10 would 10x the energy leg.

## Energy formula (GSDYN)

`_extract_dynamic` (`providers/energyvision.py:297`) does one `findall` with
`_DYN_FORMULA_RE` (`providers/energyvision.py:142`), which matches both the `afnametarief`
and `injectietarief` rows in the running formula sentence and keys them by group 1:

```
<afname|injectie>tarief ... : <factor> x Belpex per kwartier <sign> <base> EUR/MWh
```

The card quotes the formula ex-VAT while every printed price is VAT-inclusive, so the
energy leg is scaled to the VAT-inclusive basis (`vat_rate` then stays 0.0, matching Bolt /
Frank). `vat_multiplier(text, _VAT_RE)` reads the "6% BTW" header
(`providers/energyvision.py:137`). Converting EUR/MWh HTVA to the EUR/kWh basis applied
against the EUR/kWh spot:

```
factor = factor_pdf * vat          # dimensionless, NO * 10
base   = base_eur_mwh / 1000 * vat # EUR/MWh -> EUR/kWh, VAT baked
```

`test_dynamic_energy_factor` pins `1,05 * 1,06`, `test_dynamic_energy_base` pins
`15/1000 * 1,06`, and `test_dynamic_yearly_fixed_fee` pins the 50 EUR/jaar vaste
vergoeding.

## Fixed energy (GS3JV)

`_extract_fixed` (`providers/energyvision.py:334`) parses the "Groene stroom - vast tarief
13,57 €cent/kWh" row with `_FIXED_ENERGY_RE` (`providers/energyvision.py:150`). The fixed
rate is printed VAT-inclusive, so it is used as-is (`single = 13,57 / 100`), with the 75
EUR/jaar vaste vergoeding as `yearly_fixed_fee`. `test_fixed_energy_is_fixed_rates` and
`test_fixed_yearly_fixed_fee` pin both.

## Injection

Two shapes, one per contract:

- **GSDYN (spot).** `_extract_dynamic` reads the `injectietarief` row (`1 x Belpex per
  kwartier - 15 EUR/MWh`) into `InjectionRates(factor, base)` - the hourly `factor*spot+base`
  shape. Injection is VAT-exempt, so no VAT bake: `factor = 1,0` (as printed) and
  `base = -15/1000`. The coefficient is exactly `1,0`, which is why the extractor parses the
  injection row explicitly rather than copying Bolt's `factor < 1.0` row discriminator (that
  heuristic would miss it). `test_dynamic_injection_factor_is_one` /
  `test_dynamic_injection_base` / `test_dynamic_injection_current_is_none` pin the shape.
- **GS3JV (monthly).** The card's injection is indexed monthly (`0,6 x Belpex-SPP-M - 15
  EUR/MWh`, known only at month-end), and it prints the resolved monthly indicative "Injectie
  – variabel 2,07 €cent/kWh". `_extract_fixed` bills that indicative as
  `InjectionRates(current=2,07/100)` and never a live hourly factor/base against the spot
  (the 0.6.7-class latent-mispricing trap). It is VAT-exempt, and the card's 1 c€/kWh floor
  is already applied to the printed value. `test_fixed_injection_is_monthly_indicative`
  pins `current` set and `factor`/`base` None.

A missing injection row is fatal on the dynamic card ("could not parse dynamic injectie
formula", `test_missing_dynamic_injection_is_fatal`) rather than silently crediting 0.

## Taxes

`_extract_taxes` (`providers/energyvision.py:352`) parses the shared Flanders levy block
into a `TaxOverlay`. All card values are VAT-inclusive (the federal excise and energy fund
are VAT-exempt), so `vat_rate=0.0` is set explicitly (`test_taxes_vat_rate_zero`).

| overlay field | card row | regex | required |
| --- | --- | --- | --- |
| `federal_excise` | Federale accijns, Verbruik tussen 0 & 3.000 kWh | `_EXCISE_RE` (`:166`) | yes |
| `energy_contribution` | Energiebijdrage | `_CONTRIB_RE` (`:165`) | yes |
| `flanders_renewables` | Kosten GSC en WKC geldig voor | `_GSC_WKC_RE` (`:164`) | yes |
| `energy_fund_eur_per_month` | Standaard tarief gedomicilieerd (EUR/maand) | `_FUND_RE` (`:169`) | no (0.0 default) |

Two EnergyVision-specific notes versus energie.be: GSC and WKC print as a **single combined
value** ("1,554 €cent/kWh"), not two rows to sum; and the energiefonds prints a domiciled
row (standard residential = 0 EUR/month) and a non-domiciled row (10,07) - the extractor
bills the domiciled one, and its regex anchors on "Standaard tarief gedomicilieerd" so the
"niet-gedomicilieerd" sibling cannot match. The federal excise uses the residential
0-20.000 kWh band (5,03288 c€/kWh). All c€/kWh values are divided by 100.
`test_taxes_flanders_renewables_combined_gsc_wkc`, `test_taxes_federal_excise`,
`test_taxes_energy_contribution` and `test_taxes_energy_fund_domiciled_zero` pin the block.

## DSO overlay

`_extract_dsos` (`providers/energyvision.py:370`) covers all eight Fluvius sub-areas via
`_DSO_ROWS` (`providers/energyvision.py:186`). EnergyVision prints the area names in **upper
case** ("FLUVIUS ANTWERPEN", "FLUVIUS KEMPEN", ...), so the shared Title-case
`FLUVIUS_CARD_LABELS` map does not apply and this module carries its own:

| card label | canonical key |
| --- | --- |
| FLUVIUS ANTWERPEN | `fluvius_antwerpen` |
| FLUVIUS HALLE-VILVOORDE | `fluvius_halle_vilvoorde` |
| FLUVIUS IMEWO | `fluvius_imewo` |
| FLUVIUS KEMPEN | `fluvius_iveka` |
| FLUVIUS LIMBURG | `fluvius_limburg` |
| FLUVIUS MIDDEN-VLAANDEREN | `fluvius_intergem` |
| FLUVIUS WEST | `fluvius_west` |
| FLUVIUS ZENNE-DIJLE | `fluvius_zenne_dijle` |

Note Kempen -> `fluvius_iveka` and Midden-Vlaanderen -> `fluvius_intergem`: the card's
regional trade name is not the canonical key. `test_dsos_cover_all_eight_fluvius_subareas`
asserts all eight are present on both cards.

The card prints two meter tables (`Vlaams Gewest Digitale Meter` then
`Vlaams Gewest Analoge Meter`); the parser slices to the **digital-meter** block between
`_DIGITAL_MARKER` and `_ANALOG_MARKER` (`providers/energyvision.py:180`) - a modern SMR3
customer is on a digital meter - and reads the five columns:

```
capaciteitstarief -> capacity_eur_per_kw_year     (EUR/kW/year, no /100)  [col 1]
kWh-tarief         -> distribution_single          (c€/kWh /100)          [col 2]
excl. nacht        -> distribution_exclusive_night (c€/kWh /100)          [col 3]
databeheer         -> data_management_per_year     (EUR/year, no /100)    [col 4]
maximumtarief      -> ignored (ceiling)                                   [col 5]
```

`transport` is always 0.0 (bundled into distribution, `test_dso_transport_is_zero`). The
analog-meter block (with its prosumer column) is skipped, so
`DsoOverlay.prosumer_eur_per_kva_year` stays `None`.
`test_dso_antwerpen_digital_meter_columns`, `test_dso_kempen_maps_to_iveka` and
`test_dso_midden_vlaanderen_maps_to_intergem` pin the columns and the two non-obvious keys.

## valid_until

`parse_valid_until` (`_pdf.py`) is the shared best-effort validity parser. EnergyVision's
card says "is geldig voor het product ... van juli 2026", so the month name sits inside a
"geldig" validity-keyword window and resolves to the last day of the month
(`date(2026, 7, 31)`, `test_publication_label_and_valid_until`).

## Quirks and historical bugs (land mines)

- **Dynamic card is EUR/MWh HTVA (Bolt axis).** The `1,05` coefficient is a dimensionless
  Belpex multiplier scaled only by VAT - NOT by 10 like Frank. The base goes EUR/MWh ->
  EUR/kWh (`/1000`). Getting the axis wrong 10x's the energy leg
  (`providers/energyvision.py:310`).
- **Injection coefficient is exactly 1,0.** Bolt's `factor < 1.0` injection-row heuristic
  would miss it, so the dynamic row is parsed explicitly by label
  (`providers/energyvision.py:297`).
- **GS3JV injection is monthly, not spot.** It is `Belpex-SPP-M` (month-end); bill the
  printed monthly indicative as `current`, never a live factor/base against the hourly spot
  (`providers/energyvision.py:346`).
- **Month-versioned URLs + Drupal dedup suffix.** The card URL carries the pricing month and
  the CMS may append `_0`; resolve it off the listing, do not construct it
  (`providers/energyvision.py:241`).
- **Upper-case Fluvius labels.** EnergyVision prints them in caps, so it needs its own
  `_DSO_ROWS` map, not `FLUVIUS_CARD_LABELS` (`providers/energyvision.py:186`).
- **Two meter tables.** Slice to the digital-meter block before parsing DSO rows, or the
  analog rows leak in (`providers/energyvision.py:370`).
- **GSC + WKC are combined; energiefonds is domiciled.** A single combined renewables value,
  and the domiciled (0 EUR/month) fund row is billed, not the non-domiciled one
  (`providers/energyvision.py:352`).
- **Kempen and Midden-Vlaanderen map to non-obvious keys** (`fluvius_iveka`,
  `fluvius_intergem`).

## Test fixtures

The fixtures live under `tests/fixtures/`:

| fixture | represents |
| --- | --- |
| `energyvision_dynamic_jul.pdf` | the GSDYN "Goedkope Stroom Dynamisch" card, July 2026 |
| `energyvision_fixed_3y_jul.pdf` | the GS3JV "Goedkope stroom 3 jaar vast" card, July 2026 |

Tests load them through `fixture_text("energyvision_<...>_jul.pdf", layout=True)`, matching
the layout-preserving extraction used in production.

## When the card changes, look here

| symptom | likely culprit | why |
| --- | --- | --- |
| "could not parse dynamic afname formula" | `_DYN_FORMULA_RE` (`:142`) | the "formule (exclusief btw): ... x Belpex per kwartier ... EUR/MWh" wording or sign chars changed |
| Wrong dynamic per-kWh price | the EUR/MWh conversion in `_extract_dynamic` (`:310`) | EnergyVision switched Belpex units or the VAT treatment changed |
| "could not parse fixed energy price" | `_FIXED_ENERGY_RE` (`:150`) | the "Groene stroom - vast tarief ... €cent/kWh" label reworded |
| Solar credit wrong (dynamic) | `_DYN_FORMULA_RE` injection row (`:142`) | the "injectietarief" wording or sign dropped |
| Solar credit wrong (fixed) | `_FIXED_INJECTION_RE` (`:154`) | the "Injectie – variabel ... €cent/kWh" indicative label reworded |
| "EnergyVision: vaste vergoeding row not found" | `_FEE_RE` (`:159`) | the "Vaste vergoeding ... €/jaar" label reworded |
| Tax under/over-billing or "could not parse tax block" | `_extract_taxes` regexes (`:164`-169) | a levy row label or unit changed; energy fund is the only optional one |
| A DSO sub-area missing, or all DSOs missing | `_DSO_ROWS` and the row regex in `_extract_dsos` (`:186`, `:370`); the "Digitale Meter" anchor | a label renamed or the section header changed |
| "EnergyVision: no listing entry for card ..." | `_resolve_card_url` (`:241`) | the listing markup or the `EV-<MMYY>-<CODE>-nl` filename scheme changed |

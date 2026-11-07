# Provider: dats24

This document is the maintainer reference for the DATS 24 supplier extractor
(`providers/dats24.py`). DATS 24 is the fuel-and-energy retail brand of Colruyt
Group; it sold one residential electricity product across Flanders and Wallonia
until July 2026 and published its rates as one PDF per month on the Colruyt Group
static CDN. Its households moved to EnergyVision on 1 September 2026 on the same
contract and tariff, so the product is modelled as withdrawn and set up from the
card the household signed (issue #169). The notes below are grounded in the
module and its fixture-driven tests (`tests/test_dats24.py`,
`tests/test_dats24_transferred.py`), which encode the exact parse output against
real cards and are treated here as ground truth for what the extractor must
produce.

Related reading:

- [../provider-framework.md](../provider-framework.md): the `SupplierExtractor`
  protocol, the `Contract` / `SupplierSnapshot` / `EnergyRates` / `DsoOverlay` /
  `TaxOverlay` / `InjectionRates` dataclasses, the shared PDF helpers.
- [../pricing-model.md](../pricing-model.md): how `compute_breakdown` consumes the
  snapshot, the VAT convention, and how the injection shape is priced.

## Overview

| Property | Value |
|---|---|
| Extractor id | `dats24` (`dats24.py`) |
| Label | `DATS 24` (`dats24.py`) |
| Regions served | Flanders, Wallonia (`dats24.py`, `_DATS24_REGIONS`) |
| Products | one: `dats24_groen_variabel` (`dats24.py`) |
| Card format | one PDF per month on a CDN, month spelled in the filename |
| Probe | a constant key, `withdrawn 2026-08-01` (`probe`, `dats24.py`) |
| Archive | `fetch_for_month` over the month-keyed CDN URL; cards from 2023 to July 2026 |
| Lifecycle | product withdrawn from 2026-08-01; households moved to EnergyVision on 2026-09-01 |

DATS 24 sold one residential electricity product, "Elektriciteit Groen
Variabel" (`dats24.py`). It is a variable (monthly-indexed) contract whose
formula shape is (the per-meter-type coefficients are re-published with every
card, so they are deliberately not pinned here or in the module docstring):

```
afname        = (BE_spotRLP * <factor> + 0.511) * 1.06   c€/kWh
teruglevering = (BE_spotSPP * <factor> - 1.11)           c€/kWh (VAT-exempt)
```

`BE_spotRLP` is a monthly Belgian spot index (quarter-hourly spot, RLP-weighted);
`BE_spotSPP` is a monthly synthetic-profile index. Both are monthly, not the
hourly ENTSO-E day-ahead spot, so both formulas are resolved against the
delivery month's weighted mean (`month_indexed` on the energy, `spp_indexed` on
the feed-in), with the card's printed figures, which use the previous month's
index, as the fallback without a key.

> **Withdrawal and transfer.** DATS 24 left residential energy supply. Its
> last card is of July 2026: the CDN holds none for August or later, and the
> live check found the August address answering 404 on 1 September (issue #78).
> Its contracts moved automatically to EnergyVision on 1 September 2026, whose
> welcome mail says contract, tariff and term stay the same and whose invoices
> name the product "Elektriciteit Groen Variabel vanaf 01/09/2026". EnergyVision
> publishes no card for these households (issue #100), so the integration prices
> them on DATS 24's own cards.
>
> Until issue #169 the supplier carried `deprecated_until=date(2026, 8, 31)` and
> `deprecated_successor="energyvision"`, which hid DATS 24 from new setups,
> stopped every fetch past the date and kept the `supplier_deprecated_ended`
> Repairs card on every entry, telling households whose contract carried on
> unchanged that their prices were no longer what they were billed. Now the
> supplier carries neither and its one product is withdrawn instead
> (`Contract.withdrawn=date(2026, 8, 1)`, the month after its last card): the
> contract picker lists it marked †, labelled "(transferred to EnergyVision)",
> the compare pages leave it out, and an entry on it raises `contract_withdrawn`.
> The successor products EnergyVision sells to new customers live in
> [energyvision.md](./energyvision.md).
>
> **Which card prices it.** The card of the month the household signed, the
> tariff card month or else the contract start date's ("Versie MM/YYYY" on the
> DATS 24 order confirmation), through the signing cohort: its energy and feed-in
> formulas, re-indexed on each delivery month's BE_spotRLP and BE_spotSPP. With
> no signing month, the last card's, July 2026's. The network and the levies are
> the delivery month's card's, July 2026's for every month after it, and from
> 2027 on the regulator's sheet of the year (`past_last_card`,
> `snapshot_resolve.py`).
>
> **The start date is DATS 24's.** A contract start date is the day DATS 24
> began supplying, not 1 September 2026, the day EnergyVision took over and the
> date its invoices print. The cohort starts at the later of the card month and
> the start date (`_cohort_first_month`, `cohort.py`), so a September start
> bills every month before September 2026 on its own card: in 2026 their feed-in
> factors run from 0,0665 to 0,0766 c€/kWh per EUR/MWh against the 0,068 of a
> December 2025 card.
>
> **The risk.** It is a variable contract and nobody publishes its card any
> more. If EnergyVision changed its formulas, nothing here would see it: the
> entry would go on pricing on the card the household signed. The transfer mail
> says the tariff stays the same, which is what this rests on. The cards were
> estimates in their own right (EnergyVision's answer on issue #100): each
> prints the formula at the previous month's index, which the integration
> replaces by the delivery month's own when it has a day-ahead source.

### Source URL

Each month's card has its own CDN URL, built by `_card_url`
(`dats24.py`) from `_CDN_BASE` (`dats24.py`):

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
varies.

### The last card

`fetch` (`dats24.py`) reads `_card_url(2026, 7)`, the last card published,
whatever the month: no current card exists to ask for. Once the CDN drops it the
404 propagates as a missing card, and the coordinator keeps the card it holds or
reads the card archive's copy of the month before `withdrawn`
(`_keep_withdrawn_card`, `coordinator_snapshot.py`). Until issue #169 it asked
for the running month and fell back one month, which raised on both 404s once
publication stopped.

## Contracts

| id | label | kind | regions | spot_indexed_injection | withdrawn |
|---|---|---|---|---|---|
| `dats24_groen_variabel` | `DATS 24 Elektriciteit Groen Variabel (transferred to EnergyVision)` | `variable` | Flanders, Wallonia | True | 2026-08-01 |

Only one `Contract` is declared (`dats24.py`). There is no fixed, TOU, or
dynamic product, so `quarter_hourly` does not apply (that flag lives on
`DynamicRates`, not on a variable contract). `spot_indexed_injection` is set:
the credit is indexed on BE_spotSPP, the delivery month's solar-weighted mean,
which is resolved from the spot cache, so the flow offers the ENTSO-E key on the
injection regime. The contract still prices without one by falling back to the
card's printed figure. See [../provider-framework.md](../provider-framework.md) for what
`spot_indexed_injection` gates in the config flow.

`fetch` rejects any other contract id with `ExtractorError` (`dats24.py`)
and rejects Brussels or any non-FL/WA region (`dats24.py`): DATS 24 does
not sell residential electricity outside Flanders and Wallonia.

## Fetch strategy

```
fetch(session, contract_id, region)          dats24.py
  ├─ reject unknown contract id                dats24.py
  ├─ reject region not in {flanders, wallonia} dats24.py
  ├─ fetch_pdf_text_layout(session, _card_url(2026, 7))  dats24.py   (PDF -> pdfplumber layout text)
  └─ parse_snapshot(text, url, region)         dats24.py
```

`fetch` downloads the PDF and runs it through `fetch_pdf_text_layout`
(`_pdf.py`), the layout-preserving pdfplumber path. Layout mode is required
because the card lays out DSO and tax data as multi-column tables; the plain text
extractor would collapse the columns. `fetch_pdf_text_layout` also guards against
CDNs that return HTTP 200 `text/html` for a missing PDF and rejects a
pages-present-but-no-text document as a hard error (`_pdf.py`,
`extract_pdf_text_layout` at `_pdf.py`).

### Probe

`probe` (`dats24.py`) returns `withdrawn 2026-08-01` and asks nothing, as
Energy Knights' does for its withdrawn products: the card read never changes, so
it is fetched once and held. Without a probe a card past its validity is asked
for again every hour (`_row_is_fresh`, `snapshot_store.py`), which for July's
card would be every hour for good.

There is no `discover` any more: nothing is sold, so there is no listing for the
live check's catalog diff, and `_CATALOG_BASELINES` (`scripts/live_check.py`)
has no entry for DATS 24.

### Archive / historical months

`fetch_for_month` walks the same month-keyed CDN URL `_card_url` already builds,
so the archive costs one HTTP GET per past month and no new resolution logic.
The CDN retains per-month cards back to 2023.

This was previously left unimplemented, on the stated grounds that "the product
ends on 2026-08-31, so the payoff would be one month of more accurate YTD
backfill". That reasoning was wrong by measurement rather than by judgement: a
year-to-date walk spans every month since 1 January, not the month in progress.
Proxying the current card into all of them ran Flanders **10,5% high** over
Jan–Jul at 3500 kWh (EUR 543,12 against 491,54) and Wallonia 5,7%, worst in May
at 18,0%, with January's ORES distribution under-billed by 2,459 c€/kWh. The
backfill shares the same cache, so those prices were written into the recorder
too, where they do not self-heal.

Returns `None` for a month the CDN does not hold, and without a request for any
month after the last card. The card archive walks DATS 24 again, so its last
twelve months are mirrored like any supplier's.

## Parsing

`parse_snapshot` (`dats24.py`) is a pure function exposed for unit tests
(the test harness feeds it fixture text directly, never the network). It builds a
`SupplierSnapshot` from five sub-parsers plus two shared helpers:

| Snapshot field | Parser | Source |
|---|---|---|
| `energy` | `_extract_energy` | `dats24.py` |
| `dsos` | `_extract_dsos` (dispatches per region) | `dats24.py` |
| `taxes` | `_extract_taxes` | `dats24.py` |
| `injection` | `_extract_injection` | `dats24.py` |
| `publication_label` | `_extract_publication` | `dats24.py` |
| `valid_until` | `parse_valid_until` (shared) | `_validity.py` |
| `supplier` / `contract` | literals | `dats24.py` |

Every numeric value is parsed with `to_float` (`_parse.py`), which strips
Unicode thousands separators and accepts both the Belgian comma decimal and a dot
decimal. This dot tolerance is not cosmetic: the May 2026 card switched its
separator from `,` to `.` (see Quirks).

### Energy (`_extract_energy`, `dats24.py`)

The card prints four indicative TVAC c€/kWh values on one row under `Afname1`
(single/mono, bi-hourly day, bi-hourly night, exclusive-night), plus a yearly
standing charge. They are the formulas at the PREVIOUS month's BE_spotRLP ("de
meest recente waarde van BE_spotRLP (maart 2026: 97,64 €/MWh)" on the April card),
while the contract is indexed monthly on it, so the delivery month settles on its
own index. The extractor keeps the printed values as the fallback and also reads
each register's formula, `(BE_spotRLP x 0,1124 + 0,511) + 6% btw` (the
coefficients move with every card), as `month_indexed` coefficients on the
`columns` RLP blend (`_register_formulas`, `dats24.py`).

The formulas are bound by arithmetic: each one has to reproduce the rate the card
prints for its register at the index the card states, to the cent. A card that
fails that keeps its printed rates and no formula. The hand-built May fixture is
the case: it prints `(BE_spotRLP x 00,000 + 0,001)` in every row.

The blend is measured, not read: the card's wording ("rekenkundig gemiddelde van
de profielen van de verschillende distributienetbeheerders") is the one Eneco and
energie.be use for two different blends. Against the values the February to July
2026 cards state for January to June, the mean over every DSO column of the
Synergrid sheet weighting quarter-hour prices reproduces them to 0,04 EUR/MWh on
average (January to March to the cent), and the engine's hourly weighting to 0,15;
the distinct-curve mean is 1,1 off and the Flemish curve 0,4.

Layout the regex targets (illustrative comment values, `dats24.py`):

```
Afname1 (c€/kWh) 12,18 13,48 10,97 10,97
                 single  Day   Night Excl-night
```

- Afname row regex: `Afname1?\s*\(c€/kWh\)\s+(...)` four capture groups
  (`dats24.py`). A miss raises `could not parse DATS 24 indicative afname
  row` (`dats24.py`).
- Yearly fee regex: `VASTE VERGOEDING\s*\(€/jaar\)\s+(...)` (`dats24.py`). A
  miss raises `could not parse DATS 24 yearly fixed fee` (`dats24.py`);
  the standing charge is mandatory on every card, so a miss is layout drift, not a
  fee-free contract.

Output is `VariableRates(current, peak, offpeak, exclusive_night,
yearly_fixed_fee)` with the four c€/kWh values divided by 100 (`dats24.py`),
plus the four formula pairs, `month_indexed`, `rlp_indexed` and `rlp_blend`.
All four include 6% VAT. Illustrative parse from the April fixture
(`test_dats24.py`): `current 0.1218`, `peak 0.1348`, `offpeak 0.1097`,
`exclusive_night 0.1097`, `yearly_fixed_fee 38.50` EUR/yr.

### DSO overlay

`_extract_dsos` dispatches on region (`dats24.py`). It returns `{}` for
any region other than Flanders or Wallonia (defensive; `fetch` already rejects
those).

#### Flanders (`_extract_flanders_dsos`, `dats24.py`)

Iterates the eight Fluvius sub-areas in `_FLANDERS_DSOS` (`dats24.py`),
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
(`dats24.py`):

```
cap_digital | afname_dig | afname_dig_excl_nacht | max_tarief
cap_classical | afname_class | afname_class_excl_nacht | prosumer
meteropname_kwartier | meteropname_jaarlijks
```

The extractor models only the digital-meter path (post-2024 Fluvius rollout
target); the four classical/analog numbers are ignored. It fills `DsoOverlay` with
(`dats24.py`): `distribution_single` = col 2 /100, `distribution_exclusive_night`
= col 3 /100, `transport` = 0.0 (rolled into Fluvius distribution on this card),
`capacity_eur_per_kw_year` = col 1 (the digital capacity term, EUR/kW/yr, NOT
divided by 100), `data_management_per_year` = col 10 (jaarlijks meteropname). A
row that does not match is skipped (`continue`, `dats24.py`), not fatal, so
a partial card still yields the DSOs it could parse. Illustrative Antwerpen values
(`test_dats24.py`): capacity 52.37 EUR/kW/yr, distribution 5.35 c€/kWh,
data-management 18.92 EUR/yr.

#### Wallonia (`_extract_wallonia_dsos`, `dats24.py`)

Iterates `_WALLONIA_DSOS` (`dats24.py`), an ordered tuple:

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
Wallon row (`dats24.py`). The label is matched by similarity
against the text a row starts with, and the other six sub-areas score well below
the threshold, so `ORES (Brabant Wallon)` only picks up that one row.

Ten columns per row (`dats24.py`):

```
single | day | night | PIC | MEDIUM | ECO | excl_nacht
transport | data-beheer (€/yr) | prosumer (€/kVA/yr)
```

Mapped to `DsoOverlay` (`dats24.py`): `distribution_single` (col1/100),
`distribution_peak` (col2/100), `distribution_offpeak` (col3/100),
`distribution_pic` (col4/100), `distribution_medium` (col5/100),
`distribution_eco` (col6/100), `distribution_exclusive_night` (col7/100),
`transport` (col8/100, c€/kWh), `data_management_per_year` (col9, EUR/yr),
`prosumer_eur_per_kva_year` (col10, EUR/kVA/yr). The PIC/MEDIUM/ECO columns are the
Wallonia Tarif Impact CWaPE bands; they are carried on the DSO overlay even though
DATS 24's own product is not an Impact contract, because the DSO overlay is shared
across the pricing engine. There is no `capacity_eur_per_kw_year` on the Walloon
rows (Wallonia has no capacity term).

Illustrative ORES values (`test_dats24.py`): single 11.98, day 13.27,
night 7.39, PIC 16.57, medium 10.83, eco 5.09 c€/kWh, transport 2.74 c€/kWh, data
14.10 EUR/yr, prosumer 85.84 EUR/kVA/yr. RESA is deliberately distinct (single
11.06, prosumer 84.22, `test_dats24.py`), which guards against a regex that
would silently align all Walloon DSOs to one row.

### Taxes (`_extract_taxes`, `dats24.py`)

Federal levies are region-agnostic; regional renewables and fees are gated by
`region` so a Flanders user never accrues the Walloon connection fee and a
Wallonia user never accrues the Flemish Energiefonds (`dats24.py`).

Federal (both regions, mandatory, raise on miss `dats24.py`):

- `energy_contribution`: `Energiebijdrage\s+(...)\s*c€/kWh` (`dats24.py`) /100.
- `federal_excise`: `Verbruik tussen 0 kWh en 3\.000 kWh\s+(...)\s*c€/kWh`
  (`dats24.py`) /100. This is the lowest excise band (0-3000 kWh).
- `federal_excise_bands`: the four "Verbruik tussen" rows from that one
  (`excise_tier_bands`, `_parse.py`), which fall by volume on every card.

Flanders-only (`dats24.py`):

- `flanders_renewables` = GSC + WKC: `Vlaams Gewest:\s*GSC\s*\(c€/kWh\)\s+(...)`
  and `WKC\s*\(c€/kWh\)\s+(...)` summed, each /100. Both are mandatory and always
  printed together; either miss raises `DATS 24: Flanders GSC/WKC renewables not
  found` (`dats24.py`). GSC is the dominant half (the comment cites 1,183
  vs 0,378 c€/kWh, illustrative), so silently zeroing a missed GSC would under-bill
  by ~1.2 c€/kWh.
- `energy_fund_eur_per_month`: `Hoofdverblijf\s*\(domicilie\)\s+(...)\s*€/maand`
  (`dats24.py`). This one is NULLABLE: if the row is absent it defaults to
  0.0 rather than raising. The residential default is 0 (`test_dats24.py`);
  second-home users override in the OptionsFlow.

Wallonia-only (`dats24.py`):

- `wallonia_renewables` = CV: `Waals Gewest:\s*CV\s*\(c€/kWh\)\s+(...)` /100.
- `region_connection_fee`: `Aansluitingsvergoeding\s+Walloni[eë]\d*\s+(...)\s*c€/kWh`
  /100. The `\d*` tolerates a footnote digit that the layout-aware text glues onto
  the word `Wallonië` (`dats24.py`). Both are mandatory; either miss raises
  `DATS 24: Wallonia CV / connection fee not found` (`dats24.py`).

`TaxOverlay` sets `vat_rate=0.0` (`dats24.py`): all card values are already
TVAC (6% VAT), so `compute_breakdown` must not re-scale them. The card footer reads
`Alle prijzen ... inclusief 6% btw, tenzij anders vermeld`; the two exceptions
tagged `Niet aan btw onderworpen` (the Walloon connection fee and the Flemish
Energiefonds) happen to use the same per-kWh / per-month conventions, so they slot
in without conversion (`dats24.py`). Illustrative April values: Flanders
renewables 0.01561, Wallonia renewables 0.03032, connection fee 0.00075 EUR/kWh
(`test_dats24.py`).

### Injection (`_extract_injection`, `dats24.py`)

Injection shape: **month-indexed on Belpex-SPP**. DATS 24 settles teruglevering
on `BE_spotSPP`, a monthly synthetic index, not the hourly day-ahead spot. The
card prints a figure right after the formula and says which month it came from:
*"de terugleveringsvergoeding wordt verkregen door de MEEST RECENTE waarde van
BE_spotSPP (maart 2026: 57,11 EUR/MWh) in te vullen in de tariefformule"* on the
April card (`dats24.py`):

```
formula:    (BE_spotSPP x 0,0766 - 1,11)   c€/kWh, VAT-exempt
indicative: Teruglevering2 (c€/kWh) 3,26
```

So the printed `3,26` is MARCH's index, and crediting it credits last month's.
April's own `BE_spotSPP` was 27,95, worth 1,0310 c/kWh, so the printed figure
paid more than three times what April owed.

The extractor surfaces the coefficients with `spp_indexed=True`
(`dats24.py`), which routes them to the delivery month's own
solar-weighted mean, the same one the coordinator computes from the Synergrid
profile, and keeps them off the per-hour path. The card's SPP is Synergrid's, so
the two are the same index. `current` remains as the fallback for an entry with
no profile, and the `formula` string is still retained verbatim for diagnostics
(`test_injection_formula_text_retained_for_any_operator`, `test_dats24.py`,
proves a `+` operator parses into a positive base and is kept as text too).

Two hard invariants encoded in tests:

- **Flanders-only.** Returns `None` in Wallonia (`dats24.py`): the card
  footnote reserves the teruglevering tariff to Flemish digital-meter customers, so
  a Walloon prosumer accrues no feed-in credit and the shared card's indicative must
  not be surfaced for them (`test_injection_is_flanders_only`,
  `test_dats24.py`).
- **Negative-safe sign parsing.** The indicative regex captures an optional leading
  sign, `Teruglevering2?\s*\(c€/kWh\)\s++([SIGN_CHARS]?)\s*(...)` (`dats24.py`),
  and applies `parse_sign` (`_parse.py`). The blanks before the sign are taken whole
  (`\s++`): shared with the blanks after it when no sign is printed, a long run of
  blanks was split between the two in every way before the figure failed
  (`test_a_crafted_indicative_is_read_in_time_linear_in_its_length`). When `BE_spotSPP` is low the monthly
  indicative goes negative (the producer pays to inject); an earlier version without
  the sign group silently dropped the credit (`dats24.py`,
  `test_injection_indicative_handles_negative_value` `test_dats24.py`, which
  also checks a Unicode-minus glyph). A miss (no indicative at all) raises `DATS 24
  injection: monthly indicative missing` (`dats24.py`).

One shared teruglevering value covers all three meter types (single, bi-hourly day,
bi-hourly night), so a single `InjectionRates` entry serves everyone
(`dats24.py`).

There is no supplier-side prosumer/PV forfait on DATS 24 (`supplier_prosumer_eur_per_kva_year`
is left unset). The Walloon DSO prosumer term (`prosumer_eur_per_kva_year`) is the
only prosumer charge, and it lives on the DSO overlay, not the supplier snapshot.

### Publication label (`_extract_publication`, `dats24.py`)

`TARIEFKAART\s+(\w+\s+20\d{2})` case-insensitive, lowercased. Illustrative:
`april 2026` (`test_dats24.py`), `mei 2026` (`test_dats24.py`). Empty string
on miss (non-fatal). `valid_until` is parsed separately by the shared
`parse_valid_until` (`_validity.py`), which catches the explicit `GELDIG VAN 1 APRIL
2026 T.E.M 30 APRIL 2026` header (`test_dats24.py`, expects `date(2026, 4, 30)`).

## Quirks and historical bugs

These are the land mines a future maintainer must know, each traceable to a source
comment or test:

1. **The API source is dead; the CDN one is month-keyed.** The original source was
   `profile.dats24.be/api/v1/ratecard?...` -- a JSON-looking URL that actually
   returned a PDF, and stable enough that the extractor hardcoded it. On
   2026-07-29 it began answering HTTP 500 to everything and did not recover, which
   is what forced the move to the per-month CDN URL (`dats24.py`). Two
   consequences: the URL is now computed, not constant (so `snapshot.source_url`
   varies by month), and a fetch failure must be classified before falling back --
   see `is_missing_card_error` (`_pdf.py`).
2. **All values are TVAC; `vat_rate=0.0`.** The card is 6% VAT-inclusive except two
   `Niet aan btw onderworpen` lines that still use per-kWh/per-month conventions
   (`dats24.py`). Do not add VAT scaling in the pricing engine.
3. **Decimal separator flipped between months.** The May 2026 card switched from
   comma to dot (`Afname1 10.64 11.77 ...` instead of `12,18 13,48 ...`). All
   regexes use the `[\d,.]+` class and delegate to `to_float`, which handles both;
   a comma-only regex would raise `could not parse DATS 24 indicative afname row`
   (`test_may_card_uses_dot_decimal_separator`, `test_dats24.py`). June
   reverted to commas.
4. **Seven ORES sub-areas collapse to one key.** Only the `ORES (Brabant Wallon)`
   row is kept (`dats24.py`,
   `test_april_card_wallonia_dsos_collapse_seven_ores_subareas_to_one`).
5. **Label renames KEMPEN->iveka, MIDDEN-VLAANDEREN->intergem** in the Flanders map
   (`dats24.py`).
6. **Digital-meter-only modeling.** Both DSO parsers read only the digital-meter
   columns; the analog/classical columns are intentionally ignored
   (`dats24.py`).
7. **Flanders capacity is not /100.** `capacity_eur_per_kw_year` and both
   `data_management_per_year` and `prosumer_eur_per_kva_year` are raw EUR values;
   only the c€/kWh distribution and transport columns are divided by 100. Mixing
   these up mis-scales by 100.
8. **Injection is Flanders-only and month-indexed.** Emit the BE_spotSPP coefficients
   with `spp_indexed` beside the printed indicative, never a per-hour formula; never
   surface a credit in Wallonia (`dats24.py`). See Injection above.
9. **Negative injection indicative.** Keep the optional sign group and `parse_sign`
   (`dats24.py`).
10. **Fatal-vs-nullable asymmetry.** Afname row, yearly fee, federal excise/contribution,
    Flanders GSC+WKC, Wallonia CV+connection fee, and the injection indicative all
    raise on miss. The Flemish Energiefonds `Hoofdverblijf (domicilie)` row and the
    publication label are nullable (default 0 / empty). This split is deliberate:
    mandatory charges must fail loud rather than silently under-bill.
11. **No current card.** Never ask for the running month: `fetch` reads the July
    2026 card and `fetch_for_month` answers `None` after it without a request.
12. **May fixture excise artifact.** The hand-built dot-decimal May fixture left the
    excise row as a stray `000,005 c€/kWh` that cannot be patched (non-contiguous in
    the compressed stream, the real May card is gone). The May test intentionally
    does not value-assert taxes; tax extraction is covered by the April fixture and
    dot-decimal numeric parsing by the May energy asserts. Do not "fix" the fixture
    by asserting the stray value (`test_dats24.py`).

## Test fixtures

Under `tests/fixtures/`, exercised by `tests/test_dats24.py`:

| Fixture | Card variant | Exercises |
|---|---|---|
| `dats24_groen_variabel_apr.pdf` | April 2026 card, comma decimals | the default fixture (`test_dats24.py`); all value asserts (energy, taxes, both region DSO blocks, injection, publication metadata) and every fatal-miss test |
| `dats24_groen_variabel_may.pdf` | May 2026 card, dot decimals | dot-decimal tolerance across energy + DSO parsing (`test_dats24.py`); taxes deliberately not asserted (fixture artifact) |
| `dats24_groen_variabel_2025-12.pdf` | December 2025 card | the issue #169 example, signed on (BE_spotRLP x 0,1141 + 0,511) + 6% and BE_spotSPP x 0,068 - 1,11 (`test_dats24_transferred.py`) |
| `dats24_groen_variabel_2026-07.pdf` | July 2026 card, the last | what `fetch` reads and an entry with no signing month is priced on (`test_dats24.py`, `test_dats24_transferred.py`) |

The tests read fixtures via `fixture_text(name, layout=True)`, matching the
production `fetch_pdf_text_layout` path, and call `parse_snapshot` directly (no
network). `parse_snapshot` and `_extract_injection` are imported directly, so the
pure parsers are the unit under test.

## When the card changes, look here

| Symptom | Likely function | Why |
|---|---|---|
| `could not parse DATS 24 indicative afname row` | `_extract_energy` (`dats24.py`) | the `Afname1 (c€/kWh)` label, column count, or separator changed |
| `could not parse DATS 24 yearly fixed fee` | `_extract_energy` (`dats24.py`) | `VASTE VERGOEDING (€/jaar)` label moved |
| A Flanders DSO silently missing from `snapshot.dsos` | `_extract_flanders_dsos` / `_FLANDERS_DSOS` (`dats24.py`) | a Fluvius label was renamed (row skipped on no-match) or the 10-column layout changed |
| A Walloon DSO missing, or all sharing one row | `_extract_wallonia_dsos` / `_WALLONIA_DSOS` (`dats24.py`) | `ORES (Brabant Wallon)` / `RÉGIE DE WAVRE` label drift, or column reorder |
| `DATS 24: Flanders GSC/WKC renewables not found` | `_extract_taxes` (`dats24.py`) | the fragile `Vlaams Gewest: GSC` / `WKC` prefixes changed |
| `DATS 24: Wallonia CV / connection fee not found` | `_extract_taxes` (`dats24.py`) | `Waals Gewest: CV` or the `Aansluitingsvergoeding Wallonië` footnote changed |
| `could not parse DATS 24 federal tax block` | `_extract_taxes` (`dats24.py`) | `Energiebijdrage` or `Verbruik tussen 0 kWh en 3.000 kWh` moved |
| `DATS 24 injection: monthly indicative missing` | `_extract_injection` (`dats24.py`) | the `Teruglevering2 (c€/kWh)` label changed, or the card went spot-formula |
| Wrong publication label / `valid_until` | `_extract_publication` (`dats24.py`), `parse_valid_until` (`_validity.py`) | `TARIEFKAART <month> <year>` or the `GELDIG VAN` header changed |
| Values off by 100x | the per-column `/100.0` divisions in the DSO/energy/tax parsers | a c€/kWh column became EUR/kWh (or a EUR/yr column got divided) |
| `PDF layout parse error` / html-not-pdf | `_pdf.py` | the CDN returned HTML (file moved) or an undecodable PDF |

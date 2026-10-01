# Provider: totalenergies

This document describes the `totalenergies` supplier extractor
(`providers/totalenergies.py`), the code that turns TotalEnergies Belgium's
published residential tariff cards into a `SupplierSnapshot`. It is written for
a contributor who has to repair the extractor after TotalEnergies changes a
card layout. It is grounded in the module source and in `tests/test_totalenergies.py`,
which pins the expected parse output against real April 2026 fixtures and is the
ground truth for what the extractor must produce.

Related reading:

- [../provider-framework.md](../provider-framework.md) : the `SupplierExtractor`
  protocol, the `Contract` / `SupplierSnapshot` / `DsoOverlay` / `TaxOverlay` /
  `InjectionRates` dataclasses, the registry and the shared `_pdf` helpers.
- [../pricing-model.md](../pricing-model.md) : how `compute_breakdown` consumes
  the snapshot (energy formula, DSO overlay, taxes, injection credit).

## Overview

TotalEnergies is a full-service supplier that sells residential electricity in
all three Belgian regions: Flanders, Wallonia and Brussels. `EXTRACTOR.regions()`
(the union over every contract's `regions`, `providers/base.py`) therefore
resolves to all three. Only one product, Impact, is region limited (Wallonia
only, see the contracts table).

TotalEnergies publishes one PDF card per (product, region) at a stable,
predictable URL. The `/latest/` path segment auto rolls each month, so the
current card is always reachable without scraping a listing page
(`totalenergies.py`). The URL pattern (`_document_url`, `totalenergies.py`):

```
https://totalenergies.be/static/marketing-documents/b2c/tariff-card/latest/
    <SLUG>_ELECTRICITY_<REGION>_FR.pdf
```

where `<SLUG>` is the per contract file prefix (see table) and `<REGION>` is one
of `VL` / `WAL` / `BXL` (`_REGION_TO_CODE`, `totalenergies.py`). All cards
are fetched in the French (`_FR`) edition.

These PDFs contain rotated DSO and tax columns that pypdf cannot read (it emits
"Rotated text discovered. Output will be incomplete."). The extractor therefore
downloads with `fetch_pdf_text_layout` (pdfplumber, layout aware), unlike the
horizontal text only cards that most other providers parse with pypdf
(`totalenergies.py`).

## Contracts

Nine residential electricity products are registered (`_CONTRACTS`,
`totalenergies.py`). The `test_totalenergies_is_registered` test asserts the
count is exactly 9 (`tests/test_totalenergies.py`).

| Contract id | Label | Kind | Slug | Regions | Notes |
|---|---|---|---|---|---|
| `totalenergies_electricite_fixe` | TotalEnergies Electricité Fixe | fixed | `ELECTRICITE-FIXE` | V/W/B | Constant EUR/kWh, optionally bi-hourly |
| `totalenergies_electricite_variable` | TotalEnergies Electricité Variable | variable | `ELECTRICITE-VARIABLE` | V/W/B | Monthly-indexed (BELPEX_M_RLP) |
| `totalenergies_impact` | TotalEnergies Impact | variable | `IMPACT` | Wallonia only | CWaPE 3-band; flat supplier energy, band split is DSO-side |
| `totalenergies_mycomfort` | TotalEnergies myComfort | variable | `MYCOMFORT` | V/W/B | Monthly-indexed |
| `totalenergies_mycomfort_fixed` | TotalEnergies myComfort Fixe | fixed | `MYCOMFORT-FIXED` | V/W/B | Fixed variant of myComfort |
| `totalenergies_mydrive` | TotalEnergies myDrive | variable | `MYDRIVE` | V/W/B | Monthly-indexed (EV-oriented) |
| `totalenergies_mydynamic` | TotalEnergies myDynamic | dynamic | `MYDYNAMIC` | V/W/B | `factor * BELPEXH + base`, hourly billing |
| `totalenergies_myessential` | TotalEnergies myEssential | variable | `MYESSENTIAL` | V/W/B | Monthly-indexed |
| `totalenergies_myessential_fixed` | TotalEnergies myEssential Fixe | fixed | `MYESSENTIAL-FIXED` | V/W/B | Fixed variant of myEssential |

Notes on the kind mapping:

- Only three `TariffKind` values are used here: `fixed`, `variable`, `dynamic`.
  TotalEnergies does not register a `tou` or `tou_impact` product.
- **Impact is declared `variable`, not `tou_impact`, on purpose.** The supplier
  energy on an Impact card is flat (the PIC/MEDIUM/ECO columns are all equal);
  the three band split lives entirely on the DSO side (`DsoOverlay.distribution_pic`
  / `_medium` / `_eco`, applied under `dso_tariff_mode=impact`). See
  `test_impact_parses_as_flat_supplier_energy_with_impact_dso_bands`
  (`tests/test_totalenergies.py`).
- **myDynamic bills per clock hour, not per quarter hour.** `DynamicRates.quarter_hourly`
  defaults `False` (`providers/_rates.py`) and TotalEnergies never overrides it,
  so the integration aggregates the ENTSO-E 15 minute curve to hourly for this
  contract (same grid choice as Frank/Luminus/Mega/Eneco, `providers/_rates.py`).
- 8 of the 9 contracts set `spot_indexed_injection`, derived in
  `totalenergies.py` as "every kind except dynamic": their credit indexes on a
  monthly mean the energy leg never fetches, so the flow has to offer the key.
  Only MyDynamic leaves it False, its energy formula collecting one already.
  This said "no contract sets it" long after that derivation landed.
- No product is retired in the current registry.

The per contract `regions` override exists because TotalEnergies's listing page
advertises every product in V/W/B, but some only have a Wallonia PDF; the rest
return a "200 OK" HTML 404 page (`totalenergies.py`). `fetch` and `probe`
both reject a (contract, region) pair that is not in the contract's `regions`
(`totalenergies.py`).

## Fetch strategy

### Current card

`fetch` (`totalenergies.py`) validates the contract id and region, then
constructs the URL with `_document_url` and downloads via `fetch_pdf_text_layout`,
handing the extracted text to `parse_snapshot`. There is no listing scrape on the
hot path: the `/latest/` segment guarantees the URL always points at the current
month (`totalenergies.py`). `fetch_pdf_text_layout` treats an HTTP 200 that
returns `text/html` (a disguised 404) as a fetch failure, so a product that is
not actually published in a region raises rather than parsing an HTML error page
(`_pdf.py`).

### Probe (freshness key)

`probe` (`totalenergies.py`) issues a HEAD against the same per (contract,
region) URL and returns `head_freshness_key`'s first present header
(`Last-Modified`, then `ETag`; `_pdf.py`). Because TotalEnergies overwrites
each card in place under `/latest/`, `Last-Modified` is the correct freshness
signal, and the coordinator only re-runs `fetch` when it changes. `probe` returns
`None` for an unknown contract, an unknown region, or a region the contract does
not serve; the coordinator then falls back to its time based TTL.

### Historical fetch (archive)

There is **no** `fetch_for_month` on the extractor (`EXTRACTOR`,
`totalenergies.py`, only sets `fetch` and `probe`). TotalEnergies is an
overwrite-in-place supplier: the `/latest/` URL exposes only the current month
and no dated archive is reachable (`providers/base.py`). Since September
2026 the repository's own card archive (`.github/workflows/archive_cards.yml`)
stores each month's card as it was live, and the month cache reads it before
proxying the current snapshot, so past months from then on bill at their own
rate here too; earlier months still take the current snapshot as a proxy.

That proxy is permanent for January to August 2026, and the archive can only
grow forward. Checked on 15 September 2026, against
`ELECTRICITE-FIXE_ELECTRICITY_WAL_FR.pdf`: `latest/` is the only path that
exists. Every dated shape tried in its place (`2026-08/`, `202608/`,
`08-2026/`, `2026/`, plus `archive/` and `previous/`) came back with the
site's 205 KB HTML 404 page, which it serves under **HTTP 200** rather than a
404 status. Read the content type, never the status, when probing this host
by hand; the extractor is safe either way, since both PDF readers reject that
page outright (`invalid pdf header`, `No /Root object!`). The one avenue not
yet ruled out is a web.archive.org capture of the `latest/` URL, which could
not be checked because the Internet Archive was offline that day.

### Discovery (CI only)

`discover` (`totalenergies.py`) fetches the human `cartes-tarifaires` listing
page (`_LISTING_URL`, `totalenergies.py`) and regex-extracts every
`tariff-card/latest/<SLUG>_ELECTRICITY_(VL|WAL|BXL)_FR` slug, dropping the
regulated `TARIFF_SOCIAL` entry (not a residential-market product). This is used
by `live_check` to diff the live catalogue against `{c.slug for c in _CONTRACTS}`;
it is not on the runtime fetch path. On a listing fetch error it returns an empty
set rather than raising.

## Parsing

`parse_snapshot` (`totalenergies.py`) is the pure, test-exposed entry point.
It dispatches per region and per `TariffKind` and assembles the `SupplierSnapshot`.
All monetary values in the cards are printed in c€/kWh and divided by 100 to reach
EUR/kWh; annual fees (yearly fee, data-management, capacity, prosumer, power term)
stay in EUR.

Fields pulled and their helpers:

| Field | Helper | Source anchor |
|---|---|---|
| Energy rates | `_extract_energy` | `totalenergies.py` |
| Injection | `_extract_injection` | `totalenergies.py` |
| Publication label | `_extract_publication_month` | `totalenergies.py` |
| Federal excise (0-3000 kWh tier) | `_extract_federal_excise` | `_totalenergies_overlays.py` |
| Federal energy contribution | `_extract_energy_contribution` + `_energy_contribution_from_table` | `_totalenergies_overlays.py` |
| Yearly fee + regional renewables | `_extract_fee_and_renewables` | `_totalenergies_overlays.py` |
| Wallonia connection fee | `_extract_connection_fee` | `totalenergies.py` |
| Flanders energy fund | `_extract_energy_fund` | `_totalenergies_overlays.py` |
| DSO overlay (Flanders) | `_extract_flanders_dsos` | `_totalenergies_overlays.py` |
| DSO overlay (Wallonia) | `_extract_wallonia_dsos` | `_totalenergies_overlays.py` |
| DSO overlay (Brussels) | `_extract_brussels_dsos` | `_totalenergies_overlays.py` |
| Validity date | `parse_valid_until` (shared) | `_validity.py` |

Notable parsing hurdles:

- **Two competing energy prices per card.** A variable card prints both the
  Vlaamse-Nutsregulator annual ESTIMATE (in the standard 4 column table) and the
  realized monthly indicative ("prix mensuels calcules sur base de la derniere
  valeur connue du BELPEX_M_RLP"). The billed price is the realized block, so the
  extractor prefers it and only falls back to the table estimate when the block is
  absent, or holds figures the card's formula cannot price as on the morning
  October 2026 cards (`totalenergies.py`, `_realized_monthly_consumption`,
  `_priced_on_formula`). The test pins realized (13,53 / 14,65 / 12,55 / 12,39)
  over estimate (15,62 / ...) values as illustrative
  (`tests/test_totalenergies.py`).
- **Per-contract table drift.** The `Consommation` row has 0 to 5 trailing
  asterisks and may or may not carry an intervening `Tarif annuel` / `Tarif mensuel`
  label; the four meter values (mono / jour / nuit / excl_nuit) are separated by
  `[ \t]+` and the row must end at the line break (`totalenergies.py`).
- **Split-line dynamic formula (Brussels).** Wallonia and Flanders print
  `0.1034 * BELPEXH + 1.75` on one line; Brussels splits it, printing the factor
  line then the bases after a `Formule tarifaire` header. `_resolve_consumption_formula`
  handles both (`totalenergies.py`).
- **Sign character variance.** Formula signs are parsed with `parse_sign` over the
  shared `SIGN_CHARS` class, which covers ASCII `+`/`-` plus several Unicode dashes
  that TotalEnergies flips between on re-renders (`_parse.py`).
- **DSO name to canonical key mapping.** Card labels are mapped to `DSO_*`
  constants via `_FLANDERS_LABELS` (`_totalenergies_overlays.py`) and `_WALLONIA_LABELS`
  (`_totalenergies_overlays.py`). Note the non-obvious ones: `Fluvius Kempen` maps to
  `DSO_FLUVIUS_IVEKA`, `Fluvius Midden-Vlaanderen` to `DSO_FLUVIUS_INTERGEM`, and
  Wallonia uses the exact card strings `ORES (Namur - Namen)`, `REGIE DE WAVRE`
  (-> `DSO_REW`), `RESA SA`.
- **Wrapped tax headers.** The Brussels and Flanders cards wrap the
  "Cotisation sur l'énergie" header across two lines, so the only machine readable
  copy is a column in the DSO table; see the historical bug note below.

## Energy formula per kind

```
fixed    -> FixedRates(single, peak, offpeak, exclusive_night, yearly_fixed_fee)
variable -> VariableRates(current, peak, offpeak, exclusive_night, yearly_fixed_fee)
            current is the realized monthly indicative when present, else the
            V-test table estimate
dynamic  -> DynamicRates(factor, base, yearly_fixed_fee)   # quarter_hourly=False
            factor = factor_pdf * vat * 10.0
            base   = sign * base_cents * vat / 100.0
```

The dynamic scaling converts the card's HTVA c€/kWh formula (against BELPEX in
EUR/MWh) into a VAT-incl EUR/kWh formula against a EUR/kWh spot. The derivation
is in the source (`totalenergies.py`): factor gains `vat * 10`, base gains
`vat / 100`. The VAT multiplier is read from the card header pattern `TVA\s*(\d+)\s*%`
via `_vat_multiplier` (`totalenergies.py`), defaulting to 1.06 when absent
(`_pdf.py`). The illustrative test pins Wallonia myDynamic
`0.1034 * BELPEXH + 1.75` (HTVA, 6% VAT) to `factor == 1.09604`, `base == 0.01855`
(`tests/test_totalenergies.py`); Brussels resolves the same factor with
`base == 0.04081` from the split layout (`tests/test_totalenergies.py`).

The `yearly_fixed_fee` (~90 EUR/yr, illustrative) comes from
`_extract_fee_and_renewables` (`_totalenergies_overlays.py`) and is shared across all
kinds (`totalenergies.py`).

### The October 2026 fixed cards

From October 2026 the three fixed cards print the yearly fee as the first figure of
the consumption row, with the header after the four rates:

```
Consommation
100,00 22,74 24,52 21,19 21,70 Tarif annuel
```

and no longer print the green energy contribution (CEV) beside it. Footnote 0 says
instead that "les prix de l'énergie et les formules tarifaires ... comprennent la
Contribution Énergie Verte (CEV), dont le montant est fixé à : 1,57 € cent/kWh"
(2,85 in Brussels, 3,36 in Wallonia). `cev_included` reads that figure,
`consumption_row` reads the row (`_totalenergies_overlays.py`), and
`_without_renewables` (`totalenergies.py`) takes the contribution back out of every
printed rate and formula base, so it stays in `TaxOverlay` like every other card's
and the card's 22,74 is billed once, as 21,17 of energy plus 1,57 of contribution.
Adding the footnote's figure on top of the printed rate would bill it twice. Keeping
it in the tax leg is also what the card's footnote 4 asks for, since a change in the
law reaches a fixed contract. The figure is the one the earlier cards printed in a
column of their own (1,57 and 2,85 on both), on the same VAT-inclusive basis. A card
without the footnote takes the old path unchanged.

The same cards dropped the injection block and the page of feed-in conditions, so
their snapshot carries no injection leg and an export is measured but not credited.
The registry keeps `spot_indexed_injection` on them, because a contract start date
names a card from before October, whose feed-in is a `BELPEXM` formula. The
October myComfort Fixe card in Flanders spells the brand "Total Energies" in its
title, which `_extract_publication_month` allows.

### The October 2026 variable cards

The variable cards republished in October 2026 take the same layout, with a
`Tarif mensuel` header and the month formula on the next two lines:

```
Consommation
94,34 22,87 24,87 21,12 21,74 Tarif mensuel
0.1098 * BELPEXM_RLP 0.1223 * BELPEXM_RLP 0.0989 * BELPEXM_RLP 0.1034 * BELPEXM_RLP Formule tarifaire
+ 3.87 + 3.87 + 3.77 + 3.87
```

Brussels and Impact print the yearly fee alone on the line after the rates
(`25,34 27,34 23,60 24,21 Tarif mensuel` then `94,34`), so `consumption_row`
takes the fee from either place. The caller says how many rates it expects,
three Impact bands or four meter columns, because a row of four figures is
otherwise either four rates with the fee left blank or Impact's fee and its
three bands. The myDrive card in Wallonia first served on 1 October 2026 was the
first case and was refused rather than read as the second; its republication the
same day prints the fee and parses, billing 24,44 c/kWh as printed. Impact prints its one energy rate in
each band, and a card whose bands differed would be refused too. A row that
prints its yearly fee among the rates is an unfilled card and is refused: the
Impact card served on the morning of 1 October 2026 was a template with 94,34
in every column, and it read as 90,98 c/kWh of energy.

The first October cards printed their "A titre indicatif" block broken: every
figure in it was a formula base (`Compteur Simple : 3.87`), the formula with
its index term left out. The myComfort card in Brussels printed 7.01 under every
meter where its exclusive-night base is 6.91, so no comparison of figures with
bases catches every variant. `_priced_on_formula` (`totalenergies.py`) solves
each billed figure for the index instead, VAT off and against its own column's
`factor * BELPEXM_RLP + base`, and a figure counts as a price only when every
column solves to at least 1 EUR/MWh: the broken blocks solve to between -4 and
-2. A card that fails it has its row billed instead, the Vlaamse Nutsregulator
estimate the table has always carried, and the row has to pass the same test or
the card is refused. Read as the price, the broken block billed 2,30 c/kWh in
Flanders where the card prints 22,87, and 4,16 c/kWh on myComfort in Brussels
where it prints 24,83. TotalEnergies filled the blocks in on its afternoon
republications (26,43 under "Compteur Simple" on the Brussels Electricité
Variable card, the formula at 158,6 EUR/MWh), and those are billed again. A
card stating its contribution in a footnote always prints the formula, so one
that does not is refused rather than billed unchecked. The block also no longer has an injection column, and
`_realized_monthly_injection` only reads a block headed `Injection`: the last
`Compteur Simple` of a consumption-only block credited 3,87 c/kWh of feed-in on
a card that offers none. Like the fixed cards, these print no feed-in offer, so
their snapshot carries no injection leg. The live check expects none of a
non-dynamic card from October 2026 on, by the card's month, since
TotalEnergies republishes product by product.

The footnote says the formulas include the contribution as well, and on every
card of the range but one they do. The myEssential card in Brussels of 1 October
2026 prints `0.11 * BELPEXM_RLP + 3.65` where the other Brussels cards print
bases of 7 and more: its four columns solve to one index, 165 EUR/MWh, only with
the contribution left out of the bases, and to 186,7 to 191,7 with it in, where
the rest of the range agrees to within 1,3 EUR/MWh with it in.
`_formulas_hold_contribution` (`totalenergies.py`) solves the billed rates both
ways and leaves the bases as printed only when the figures settle it that way;
the rates themselves still lose the contribution, which the tax leg bills. A
card with a single column, Impact, cannot say, and the footnote stands.

The test cannot fire on a Flemish card either. The two readings drift apart by
the contribution times the spread of `1 / factor` across the columns, and
Flanders' 1,57 c/kWh is small enough that the wrong reading misses by only 2,65
to 2,69 EUR/MWh on the October 2026 Electricité Variable, myComfort and myDrive
cards, under the 3 EUR/MWh the rule needs (Wallonia's 3,36 misses by 5,74 and
Brussels' 2,85 by 3,95, both caught). A Flemish card printing its bases without
the contribution would be read as its footnote says, and an entry with a key
billed 1,57 c/kWh short. No Flemish card does: myEssential in Flanders prints
`0.11 * BELPEXM_RLP + 3.09`, which fits with the contribution inside.

The October myDynamic cards drop the exclusive-night column: the header ends on
`Heures creuses` and the row prints three figures, `19,37 19,37 19,37 Tarif
mensuel`, with the yearly fee alone on the next line. `_meter_columns`
(`totalenergies.py`) reads how many columns a dynamic card prints off that header.
The formula carries the contribution like the rest of the range, and the card does
not mention injection anywhere, so a dynamic card without the word has no
injection leg rather than failing for want of a `BELPEXH` feed-in formula.

The myComfort cards first served in Flanders and Brussels on 1 October 2026
were empty templates, with the rates left blank, and were refused. Both were
republished the same day and bill as printed, 23,18 and 26,43 c/kWh. Late on 1
October 2026 three October cards still fail, each with the "layout changed"
Repairs card while the entry keeps its September card, and each because of what
the URL serves rather than the parser:

- myComfort in Wallonia: the URL serves the Dutch card ("Tariefkaart ...
  myComfort Variabel"), which the French parser does not read.
- myComfort Fixe in Brussels: the URL serves the injection card ("Injection pour
  l'électricité"), with no consumption row at all.
- myEssential in Flanders: the footnote says the prices include the
  contribution and leaves its amount blank ("dont le montant est fixé à : €
  cent/kWh"), so it cannot be taken back out of the rates, and the formula row
  prints its first base inline and the other three on the next line.

### Month-indexed energy on the variable cards

Electricité Variable, myComfort, myDrive and myEssential print a
`factor * BELPEXM_RLP + base` formula beside their four meter columns and state
that the rates above it are computed on the *previous* month's index. The pairs
are read by `_consumption_month_formula` and attached by `_with_month_formula`
(`totalenergies.py`), which sets `month_indexed` and `rlp_indexed`, so the
delivery month's own mean re-prices the leg instead of the card's stale row.
The scaling is the dynamic branch's, because it is the same card printing the
same kind of formula: `factor * vat * 10`, `base * vat / 100`. Dividing both by
100 instead put the mono column at 0.02275 EUR/kWh against the 0.18140 it
prints, about 555 EUR a year at 3500 kWh.

The four carry the registry twin `month_indexed_energy` (`_MONTH_INDEXED_ENERGY`,
`totalenergies.py`), without which the config flow never offers the optional
ENTSO-E key on the no-solar and compensation regimes and the re-price has no
spots to resolve against: the parser half alone reached only the injection
regime, through `spot_indexed_injection`.

Impact prints the same formula the same way, but once per CWaPE band rather
than once per meter column, because its ENERGY leg does not band at all: the
three bands are the network side, and the card shows one rate for all of them.
`_consumption_month_formula` returns the pairs it finds and
`_with_month_formula` puts a single repeated pair on the one rate the leg has,
leaving the peak, off-peak and night coefficients unset, because that leg has
no such columns to re-price. Its September 2026 Wallonia card inverts to
135,07 EUR/MWh, the same index the four sibling cards solve to that month, and
requiring four pairs left it alone on the previous month's row.

### DSO overlay coverage

| Region | Sub-areas mapped | Row width | Fields surfaced |
|---|---|---|---|
| Flanders | 8 Fluvius sub-areas (`_FLANDERS_LABELS`) | 9 numbers | `distribution_single` (digital, includes transport), `capacity_eur_per_kw_year`, `data_management_per_year` (digital meter col), `prosumer_eur_per_kva_year` |
| Wallonia | AIEG, AIESH, ORES (Namur), REW, RESA (`_WALLONIA_LABELS`) | 12 numbers | `distribution_single/peak/offpeak/exclusive_night`, Impact `pic/medium/eco`, `transport`, `data_management_per_year` (terme fixe), `prosumer_eur_per_kva_year` |
| Brussels | Sibelga | 7 numbers + power term | `distribution_single/peak/offpeak/exclusive_night`, `transport`, `data_management_per_year` (metering + power term), `brussels_osp_by_tier` |

Region specifics:

- **Flanders** distribution already includes transport, so `transport=0.0` and the
  c€/kWh lands in `distribution_single` (same convention as Engie/Luminus/Mega
  Flanders, `_totalenergies_overlays.py`, `tests/test_totalenergies.py`).
  The Flanders row's 9th column is surfaced into `prosumer_eur_per_kva_year`
  (`_totalenergies_overlays.py`); capacity is a Flanders only field.
- **Wallonia** rows carry 12 numbers; the extractor surfaces mono/jour/nuit/excl,
  the Impact PIC/MEDIUM/ECO triplet, terme fixe (as `data_management_per_year`),
  transport and prosumer. The two capacity columns (cols 10-11) are not surfaced
  (`_totalenergies_overlays.py`, `tests/test_totalenergies.py`).
- **Brussels** Sibelga has no separate capacity charge, so the metering fee and the
  `<=13kVA` "Terme de puissance mise a disposition" power term are folded together
  into `data_management_per_year` (`_totalenergies_overlays.py`). The OSP annual fee
  table is parsed by the shared `parse_brussels_osp` into `brussels_osp_by_tier`
  (`_parse.py`). The test pins `data_management_per_year == 14.73 + 50.07`
  (illustrative, `tests/test_totalenergies.py`).

### Tax overlay

`TaxOverlay` is built in `parse_snapshot` (`totalenergies.py`):

- `federal_excise`: first excise tier (0-3000 kWh), mandatory, raises on a miss
  (`_totalenergies_overlays.py`). Illustrative pinned value 0.0503 EUR/kWh across all
  three regions (`tests/test_totalenergies.py`).
- `energy_contribution`: federal levy. Read from the labelled "Cotisation sur
  l'énergie" line, or, when the header is wrapped, from the DSO table fallback
  (see historical bug). Both readers return `None` on a miss rather than 0.0, so
  a card that PRINTS a zero is taken at face value while a card that omits the
  row entirely still raises (`totalenergies.py`). That distinction
  matters since 2026-08-01: the levy fell to zero, and the old
  `if energy_contribution == 0.0: raise` would have taken every TotalEnergies
  contract offline the way it took Frank offline (issue #49).
  `test_zero_energy_contribution_is_accepted` (`tests/test_totalenergies.py`)
  and `test_missing_energy_contribution_is_fatal` pin both halves.
- Regional renewables land in exactly one of `flanders_renewables`,
  `wallonia_renewables`, `brussels_renewables` per region (all others 0), taken
  from the second number on the fee+renewables line (`_extract_renewables`,
  `_totalenergies_overlays.py`). Illustrative: Flanders 0.0157 (green + cogen merged),
  Wallonia 0.032, Brussels 0.0285 (`tests/test_totalenergies.py`).
- `region_connection_fee`: Wallonia only ("Redevance de raccordement"), mandatory
  there, raises on a miss (`totalenergies.py`). Illustrative 0.0007 EUR/kWh.
- `energy_fund_eur_per_month`: Flanders only ("Résidence principale sans tarif
  social" line, `_extract_energy_fund`, `_totalenergies_overlays.py`).
- `vat_rate` is set to `0.0`, meaning the snapshot's consumption prices are already
  VAT-incl and must not be rescaled by the pricing engine (`providers/base.py`).
  The dynamic path applies VAT during parsing (see above); the fixed/variable table
  and realized values are stored as printed.

### Injection

Two shapes, selected on `kind` in `_extract_injection` (`totalenergies.py`):

- **Dynamic contracts: hourly `factor * spot + base`.** The injection block always
  prints the formula on one clean line ("0.1 * BELPEXH -1.3 ..."); the regex anchors
  after the `Injection` header so the consumption formula above is never captured
  (`totalenergies.py`). `factor = f_pdf * 10.0`, `base = b_cents / 100.0`,
  with **no VAT scaling** because residential injection is VAT-exempt
  (`providers/_rates.py`). Illustrative Wallonia: `0.1 * BELPEXH - 1.3` ->
  `factor == 1.0`, `base == -0.013` (`tests/test_totalenergies.py`). A
  dynamic card whose injection block is missing the BELPEXH formula raises rather
  than silently pricing feed-in at the flat monthly rate every hour
  (`totalenergies.py`, `tests/test_totalenergies.py`).
- **Non-dynamic contracts: monthly-indicative-only.** The table injection value is
  the V-test annual ESTIMATE; the billed value is the realized monthly indicative
  ("prix mensuels de l'injection"), so `_realized_monthly_injection`
  (`totalenergies.py`) overrides `current`, and the month formula printed under
  the injection heading is surfaced as `factor`/`base` with `month_indexed`, so
  the delivery month's mean re-prices it (`totalenergies.py`). Illustrative 0.0112 EUR/kWh for both a variable and
  a fixed card (`tests/test_totalenergies.py`).

This placed TotalEnergies in two of the three injection taxonomy shapes until
September 2026: shape (b) hourly factor*spot+base for myDynamic, shape (a)
monthly-indicative-only for every other product. The cards republished from October
2026 on offer no feed-in price at all, myDynamic's included. Shape (c) spot-indexed-variable is not used, which is a statement
about the INDEX and not about the flag: 8 of the 9 contracts set
`spot_indexed_injection`, because a monthly-mean credit needs spots the energy leg
never fetches.

There is **no supplier-side prosumer/PV forfait**: `supplier_prosumer_eur_per_kva_year`
is left `None` (`SupplierSnapshot` default, `providers/base.py`). The only
prosumer charge is the DSO tariff (`DsoOverlay.prosumer_eur_per_kva_year`), surfaced
for both the Flanders and Wallonia rows where the card publishes it.

## Quirks and historical bugs

The land mines a future maintainer must know, each traceable to a source comment:

- **Rotated columns need pdfplumber.** pypdf cannot read the rotated DSO/tax cells;
  the extractor uses `fetch_pdf_text_layout` for these cards while other providers
  stay on pypdf (`totalenergies.py`).
- **Realized monthly indicative vs annual estimate.** For variable/fixed cards the
  table row is the regulator's annual estimate; billing uses the realized monthly
  indicative block. Prefer the realized block on both the consumption and injection
  sides (`totalenergies.py`).
- **Wrapped "Cotisation sur l'énergie" header (Brussels + Flanders).** These cards
  wrap the label across two lines, so the labelled `_extract_energy_contribution`
  regex misses. The fallback `_energy_contribution_from_table` reads the levy from a
  DSO table column: the 7th SIBELGA number in Brussels, the 8th of nine on any
  Flanders Fluvius row (it is federal and identical across rows). Without this the
  all-in price silently dropped the contribution (~0.20 c€/kWh);
  `parse_snapshot` raises when both attempts return `None`
  (`totalenergies.py`; tests ).
- **3-column card must fail loud.** The old 4-value regex used `\s+` between groups,
  spanning the line break and grabbing the 90,00 yearly fee as the exclusive-night
  rate (0.90 EUR/kWh) with no error. The row now ends at the line break, so a card
  with too few columns misses and raises (`totalenergies.py`,
  `tests/test_totalenergies.py`).
- **Impact is flat supplier energy with DSO-side bands.** It used to fail to parse
  as a standard variable card. The PIC value under `Heures PIC/MEDIUM/ECO` is the
  single supplier rate; the band variation comes from the DSO Impact distribution
  (`totalenergies.py`, `tests/test_totalenergies.py`).
- **Distinct anchors for the two BELPEX formulas.** Both consumption and injection
  print `factor * BELPEXH`. Consumption always appears first (so the first match is
  consumption, `totalenergies.py`); injection is anchored after the
  `Injection` header so the consumption formula cannot be mistaken for it
  (`totalenergies.py`).
- **Same-line base regex guards against back-off.** The tail regex uses `(?=\s|$)`
  to stop `[\d.,]+` from backing off `0.1034` to `0.103`, and `(?!\s*\*\s*BELPEXH)`
  to avoid grabbing the next column's formula (`totalenergies.py`).
- **Sibelga power term is a separate line.** The `<=13kVA` "Terme de puissance mise
  a disposition" is not in the DSO row; it is folded into `data_management_per_year`
  and is mandatory (raises on a miss, `_totalenergies_overlays.py`).
- **Mandatory levies fail loud.** Federal excise, Wallonia connection fee and the
  Sibelga power term all raise rather than defaulting to 0, so a layout drift
  surfaces as an extractor failure instead of an undercounted bill
  (`totalenergies.py`, `_totalenergies_overlays.py`). The energy contribution raises only on
  a genuinely absent row — a printed zero is a valid rate since
  2026-08-01, not drift.
- **200-OK HTML 404s.** Some products only publish a Wallonia PDF; the others return
  a "200 OK" HTML 404, which `fetch_pdf_text_layout` rejects. The per-contract
  `regions` override (e.g. Impact = Wallonia only) keeps the extractor from ever
  requesting a non-existent card (`totalenergies.py`).

## Test fixtures

The tests exercise six real April 2026 fixture PDFs and the October 2026 cards listed below under `tests/fixtures/`
(all read with `layout=True`, i.e. pdfplumber):

| Fixture | Card variant |
|---|---|
| `totalenergies_dynamic_w.pdf` | myDynamic, Wallonia (same-line formula, 12-col DSO rows, connection fee) |
| `totalenergies_dynamic_v.pdf` | myDynamic, Flanders (9-col Fluvius rows, capacity, transport folded into distribution) |
| `totalenergies_dynamic_b.pdf` | myDynamic, Brussels (split-line formula, Sibelga row + power term, OSP) |
| `totalenergies_impact_w.pdf` | Impact, Wallonia (flat supplier energy, CWaPE DSO bands) |
| `totalenergies_mycomfort_fixed_w.pdf` | myComfort Fixe, Wallonia (bi-hourly fixed rates) |
| `totalenergies_mycomfort_v.pdf` | myComfort, Flanders (realized monthly indicative vs annual estimate) |
| `totalenergies_electricite_fixe_v_2026-10.pdf` | Electricité Fixe, Flanders, October 2026 (fee on the consumption row, CEV in the price) |
| `totalenergies_myessential_fixed_w_2026-10.pdf` | myEssential Fixe, Wallonia, October 2026 (the same layout, no feed-in offer) |
| `totalenergies_electricite_variable_v_2026-10.pdf` | Electricité Variable, Flanders, October 2026 (fee on the row, indicative block at a zero index) |
| `totalenergies_electricite_variable_b_2026-10.pdf` | Electricité Variable, Brussels, October 2026 (fee on the line below the rates) |
| `totalenergies_impact_w_2026-10.pdf` | Impact, Wallonia, October 2026 (three bands, fee below) |
| `totalenergies_mydrive_w_2026-10.pdf` | myDrive, Wallonia, October 2026, the first card served on 1 October (no yearly fee printed: refused; the republication parses) |
| `totalenergies_mycomfort_b_2026-10.pdf` | myComfort, Brussels, October 2026 (block at no index, 7.01 against a 6.91 base) |
| `totalenergies_electricite_variable_b_2026-10_filled.pdf` | Electricité Variable, Brussels, October 2026 afternoon (block filled in, billed) |
| `totalenergies_impact_w_2026-10_template.pdf` | Impact, Wallonia, October 2026 morning (unfilled, the fee in every rate column: refused) |
| `totalenergies_mydynamic_v_2026-10.pdf` | myDynamic, Flanders, October 2026 (three meter columns, fee below, no feed-in) |
| `totalenergies_mydynamic_w_2026-10.pdf` | myDynamic, Wallonia, October 2026 (the same, base printed `5,40`) |
| `totalenergies_myessential_b_2026-10.pdf` | myEssential, Brussels, October 2026 (formula bases printed without the contribution its footnote names) |

## When the card changes, look here

Ordered by how likely a card change is to break them:

1. **URL pattern**: `_BASE_URL` / `_document_url` (`totalenergies.py`)
   and `_REGION_TO_CODE`. If TotalEnergies renames the `/latest/` path, a product
   slug, or a `_FR` suffix, every fetch and probe 404s.
2. **Consumption table regex**: `_extract_energy` (`totalenergies.py`). New
   asterisk counts, a new intervening label, or a changed column count breaks fixed
   and variable parsing.
3. **Realized monthly block**: `_MONTHLY_BLOCK_RE` and `_realized_monthly_consumption`
   / `_realized_monthly_injection` (`totalenergies.py`). A
   reworded "prix mensuels ... BELPEX_M_RLP" heading or changed meter labels
   (`Compteur Simple`, `Heures Pleines/Creuses`, `Compteur Excl. Nuit`, `Heures PIC`)
   silently reverts the extractor to the annual estimate.
4. **Dynamic formula**: `_resolve_consumption_formula` and the injection regex
   (`totalenergies.py`). A layout change to `factor * BELPEXH + base`,
   or a swap of `BELPEXH` for another spot token, breaks myDynamic. Re-check the
   split-line Brussels path too.
5. **Fee + renewables line**: `_extract_fee_and_renewables` (`_totalenergies_overlays.py`).
   Both numbers are mandatory; a moved or reshaped `Tarif (mensuel|annuel)` anchor
   raises. On a card whose footnote 0 says its prices include the CEV, the fee comes
   from the consumption row and the contribution from the footnote, so a reworded
   footnote sends the card back to the old path, which then raises.
6. **Tax anchors**: `_extract_federal_excise` ("Consommation entre 0 et 3.000 kWh"),
   `_extract_energy_contribution` + `_energy_contribution_from_table`,
   `_extract_connection_fee` (`totalenergies.py`), `_extract_energy_fund`
   (`_totalenergies_overlays.py`).
   Watch especially for the wrapped-header fallback column indices if the DSO table
   width changes.
7. **DSO row parsers**: `_FLANDERS_LABELS` / `_extract_flanders_dsos` (9 cols),
   `_WALLONIA_LABELS` / `_extract_wallonia_dsos` (12 cols),
   `_extract_brussels_dsos` (7 cols + power term) (`_totalenergies_overlays.py`-). A
   new DSO name, a renamed sub-area, or a changed column order needs the label map
   and the fixed group indices updated together.
8. **Publication label + validity**: `_extract_publication_month`
   (`totalenergies.py`) and the shared `parse_valid_until` (`_validity.py`) drive
   the `publication_label` and `valid_until` diagnostics.
9. **Discovery (CI)**: `discover` (`totalenergies.py`). If the listing markup or
   the `tariff-card/latest/<SLUG>_ELECTRICITY_<REGION>_FR` link format changes,
   `live_check` will report a slug diff before the runtime fetch path breaks.

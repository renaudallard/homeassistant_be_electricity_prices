# Provider: cociter

This document is the maintenance reference for the Cociter tariff extractor
(`providers/cociter.py`). It describes what the extractor fetches, how it parses
Cociter's monthly PDF cards, the exact shape it produces, and every non-obvious
quirk a future maintainer needs when Cociter changes its card layout. Read it
alongside the provider framework and pricing docs it plugs into:

- [../provider-framework.md](../provider-framework.md): the `SupplierExtractor`
  protocol, the shared dataclasses (`SupplierSnapshot`, `EnergyRates`,
  `DsoOverlay`, `TaxOverlay`, `InjectionRates`), and the PDF helpers.
- [../pricing-model.md](../pricing-model.md): how `compute_breakdown` consumes
  the snapshot (energy, DSO overlay, taxes, injection, prosumer forfait).
- [../data-sources.md](../data-sources.md): the ENTSO-E spot client that the
  dynamic contract and the spot-indexed variable injection depend on.

All numbers quoted below that appear in the source module or its tests are
labelled illustrative. The codebase stores no prices; every EUR value shown
here is copied from a source comment or a test assertion for orientation only.

## Overview

Cociter is a Wallonian citizen energy cooperative. It sells only in Wallonia,
so both contracts set `regions=_COCITER_REGIONS`, defined as
`frozenset({REGION_WALLONIA})` (`cociter.py:535`, `cociter.py:545`,
`cociter.py:554`) and
`EXTRACTOR.regions()` (the union over contracts, see
`base.py:560`) resolves to Wallonia alone.

Cociter publishes one PDF card per (product, month) under predictable
filenames on a single listing page (`cociter.py:28-38`, `cociter.py:80`):

```
https://www.cociter.be/electricite/cartes-tarifaires/
    RCVar_YMR_Coop-YYMM-fr.pdf   variable contract (BELIX-indexed)
    RCVaI_YMR_Coop-YYMM-fr.pdf   variable trihoraire (BELIX on the CWaPE bands)
    RCDyn_SM3_Coop-YYMM-fr.pdf   dynamic contract (quarter-hourly BELPEX)

The filename patterns accept an optional `-<n>` before `.pdf`: Cociter's site is WordPress, which appends `-1`, `-2`, ... when a file is re-uploaded under an existing name, and July 2026's dynamic card is published as `RCDyn_SM3_Coop-2607-fr-1.pdf`. Requiring `-fr.pdf` to follow the month immediately dropped that month from the archive silently, so the year-to-date walk billed July at the current card. When both the original and a re-upload are listed the **suffixed** URL is the newer file, so every path that resolves a card ranks on that counter via `_dedup_rank`: `fetch_for_month` takes the highest suffix for the requested month, and `_find_latest` (which backs both the live `fetch` and the `probe` freshness key) sorts on `(month, counter)`. Sorting on the month alone left ties in listing order, so the live card and the archived card for the SAME month could be different files -- and when the index lists the newest first, the live price came off the superseded one and the probe pinned the cache to it. A re-upload is not always an improvement, though: July 2026's dynamic card was republished with the index renamed from `QUARTER HOURLY BELPEX` to `15 MIN BELPEX`, the meter labels dropped and the injection prose moved below its own formula, so it does not parse. `fetch_for_month` therefore walks its candidates newest-first and falls through on a parse failure, ending with the unsuffixed URL derived from the same `-N` convention — the original is still served, Cociter just stopped linking it. A healthy month still costs exactly one fetch. The injection pattern keeps its mandatory `Compteur` label through all of this: made optional it matches the **consumption** formula on that card and bills a 1,03 × +3 feed-in.
```

`YYMM` is a 2-digit year followed by a 2-digit month, e.g. `2604` for April
2026. Each card carries the energy formula plus the full DSO and tax overlay
for every Wallonian DSO Cociter serves (AIEG, AIESH, ORES, RESA, REW). All
printed values are VAT-inclusive (TVAC), which is why the produced
`TaxOverlay.vat_rate` is `0.0` (see [Tax overlay](#tax-overlay)).

Both the current-card fetch and the historical archive fetch scrape the same
listing page; there is no JSON or CMS API. The listing is HTML with plain
`<a href>` links, so discovery is a regex over the page body.

## Contracts

| id | label | TariffKind | regions | spot_indexed_injection | quarter_hourly | notes |
|----|-------|------------|---------|------------------------|----------------|-------|
| `cociter_variable` | Cociter Tarif Variable | `variable` | Wallonia | `True` | n/a | BELIX-indexed monthly variable energy. Publishes per-meter indicative rates (mono, bi-hourly, exclusive-night). Injection is an hourly `factor*spot+base` BELPEX formula with no printed monthly indicative, so it needs an ENTSO-E spot: hence `spot_indexed_injection=True` (`cociter.py:722-730`). |
| `cociter_variable_impact` | Cociter Tarif Variable Trihoraire | `tou_impact` | Wallonia | `True` | n/a | BELIX-indexed on the three CWaPE Impact bands (PIC / MEDIUM / ECO), one formula each. First card published for September 2026 (`RCVaI_YMR_Coop-2609-fr.pdf`). Same injection block as the variable card, hence the same flag. Carries the variable card's note (7) too, so `ImpactRates.month_indexed` is set and the three formulas re-price on the delivery month's BELIX through `_month_indexed_leg` when the entry holds an ENTSO-E key; the printed bands are last month's index and stand only without one. |
| `cociter_dynamic` | Cociter Tarif Dynamique | `dynamic` | Wallonia | `False` (default) | `True` | SMR3 quarter-hourly BELPEX dynamic contract. Bills on the native 15-minute Belpex grid, so `DynamicRates.quarter_hourly=True` (`cociter.py:400`). Dynamic contracts already collect the ENTSO-E key via the energy formula, so `spot_indexed_injection` stays `False`. |

No product is retired. All three are Wallonia-only by design, not by
regression. Contract ids are canonical throughout the integration; the
`_CONTRACT_PATTERNS` map (`cociter.py:130-134`) binds each id to the filename
regex used for discovery, fetch, and probe.

The trihoraire family name is one letter from the variable one, `RCVaI`
against `RCVar`, and the patterns run `IGNORECASE`: `i` is still not `r`, so
they cannot collide, and `test_cociter_discover_returns_known_family_ids`
pins that they resolve to different contract ids.

The `spot_indexed_injection=True` flag on both variable contracts is load-bearing
for the config flow: it makes the wizard offer the ENTSO-E API-key step on the
injection regime even though the energy side is "just" variable
(`base.py:71-77`). Do not drop it; without a spot curve the variable card's
injection cannot be priced and would silently zero the solar credit (see the
[spot-indexed injection invariant](#injection)).

## Fetch strategy

### Current card: `fetch`

`fetch` (`cociter.py:137-149`) looks up the filename pattern for the requested
`contract_id`, calls `_find_latest` to pick the newest matching PDF, downloads
it with `fetch_pdf_text`, and hands the extracted text to `parse_snapshot`. An
unknown `contract_id` raises `ExtractorError` (`cociter.py:144-145`), covered
by `test_unknown_contract_raises`.

`_find_latest` (`cociter.py:829-845`) fetches the listing HTML once, runs the
contract's regex (`_VAR_RE` / `_DYN_RE`, `cociter.py:96-101`) to collect every
`(url, yymm)` pair, sorts by the `YYMM` string, and returns the last (newest).
Because `YYMM` is fixed-width and zero-padded, lexical sort equals
chronological sort within a century. It raises `ExtractorError` when the
listing links no matching card (`cociter.py:561-562`).

### Freshness probe: `probe`

`probe` (`cociter.py:203-221`) is the cheap freshness key the coordinator
calls hourly. Cociter's listing returns no `Last-Modified` or `ETag`, so the
probe GETs the listing and returns the latest matching PDF URL. The URL embeds
`YYMM`, so any monthly rotation flips the probe key and triggers a refetch. On
any fetch error it returns `None` (`cociter.py:188-189`), letting the
time-based TTL take over rather than wedging the coordinator. This is a real
probe (unlike API-only suppliers whose `probe` is `None`); see
[../coordinator.md](../coordinator.md) for how the key gates `fetch`.

### Historical archive: `fetch_for_month`

Cociter keeps every monthly card linked on the same listing page, so a real
archive exists (unlike overwrite-in-place suppliers). `fetch_for_month`
(`cociter.py:135-169`) builds the target `YYMM` from the requested
`year_month`, fetches the listing, finds the URL whose `YYMM` suffix matches,
downloads and parses it, then runs `archive_validity_check` to confirm the PDF
really covers the requested month.

It returns `None` (so the coordinator falls back to the current snapshot as a
proxy) in every soft-failure case:

- unknown contract id (`cociter.py:149-151`),
- listing fetch fails (`cociter.py:154-156`),
- the listing does not link the requested month (`cociter.py:162-163`),
- the PDF 404s or does not parse (`cociter.py:164-168`),
- `archive_validity_check` rejects the card as not covering the month
  (`cociter.py:169`).

`archive_validity_check` (`_pdf.py:958-995`) is two-tier: if the parsed
`valid_until` is present it must fall in the requested month; if it is missing
it falls back to a textual month-name mention via `text_mentions_month`, using
the French month names `_FR_MONTHS` (`cociter.py:85-88`). This guards against a
CDN serving a substituted current card under an archived URL, which would
otherwise mis-bill past consumption.

The three `fetch_for_month` tests (`test_cociter.py:404-450`) exercise: a
matching listing URL returning a parsed snapshot with the right
`publication_label`, a missing month returning `None`, and an unknown contract
returning `None`. The listing fixture `_LISTING_HTML` (`test_cociter.py:373-377`)
is inline HTML with three `RCVar_YMR_Coop-YYMM-fr.pdf` links.

### Product discovery: `discover`

`discover` (`cociter.py:231-247`) is the new-product tripwire. It fetches the
listing and regexes every `RC<family>_<suffix>_Coop-<digits>-(fr|nl).pdf`
filename, mapping known families (`RCVar_YMR`, `RCVaI_YMR`, `RCDyn_SM3`) back to contract ids
via `_DISCOVER_FAMILIES` (`cociter.py:235-239`) and surfacing any unknown
family verbatim. An unrecognised family in the output means Cociter has added a
product the integration does not yet model.

## Parsing

`parse_snapshot` (`cociter.py:250-268`) is the pure, test-exposed parser. It
assembles a `SupplierSnapshot` from six sub-parsers. The mapping of snapshot
field to helper:

| Snapshot field | Helper | Source |
|----------------|--------|--------|
| `energy` | `_extract_energy` | `cociter.py:521-642` |
| `dsos` | `_extract_dsos` (+ `_extract_transport`) | `cociter.py:645-756` |
| `taxes` | `_extract_taxes` | `cociter.py:755-801` |
| `injection` | `_extract_injection` | `cociter.py:263-313` |
| `supplier_prosumer_eur_per_kva_year` | `_extract_supplier_prosumer` | `cociter.py:271-291` |
| `valid_until` | `parse_valid_until` (shared) | `_pdf.py:1004` |

Shared numeric helpers: `to_float` (`_pdf.py:665-677`) parses Belgian decimals
(`15,93`) and strips every Unicode space variant used as a thousands separator;
`parse_sign` (`_pdf.py:710-721`) turns any hyphen/dash/Unicode-minus into
`-1.0`; `SIGN_CHARS` (`_pdf.py:713`) is the character class of accepted sign
glyphs. `fetch_pdf_text` (`_pdf.py:179-186`) downloads the PDF and extracts
text with pypdf off the event loop.

### Energy: `_extract_energy`

The yearly abonnement is common to both products, matched by
`(\d+,\d+) €/an ... TVAC` (`cociter.py:317`). A miss is fatal
(`ExtractorError`, `cociter.py:362`) rather than a silent zero standing charge.
The comment notes the abonnement is 53,00 EUR/an TVAC (illustrative), and the
variable test pins `yearly_fixed_fee == 53.0` (`test_cociter.py:70`).

For `cociter_variable` (`cociter.py:533-608`), four indicative per-meter rates
are matched against their French row labels, all in c€/kWh and divided by 100
to reach EUR/kWh:

| Meter | Label regex anchor | Result field |
|-------|--------------------|--------------|
| mono | `Compteur monohoraire` | `current` (required) |
| bi-hourly peak | `Heures pleines` | `peak` (optional) |
| bi-hourly off-peak | `Heures creuses` | `offpeak` (optional) |
| exclusive night | `Compteur exclusif nuit` | `exclusive_night` (optional) |

Only the mono rate is mandatory (`cociter.py:330-333`); the others are `None`
when absent so pricing falls back to `current`. The BELIX indexation formula is
parsed for the diagnostic `formula` string only (`cociter.py:337-341`); it
accepts any `SIGN_CHARS` sign between BELIX and the base and captures the VAT
percentage the card prints. The variable test pins the four rates as
illustrative TVAC values (`test_cociter.py:71-76`).

For `cociter_variable_impact` the energy leg is `ImpactRates`, built by
`_impact_energy`. The card prints one BELIX row per CWaPE band -- `Heures PIC
(0,1 x BELIX + 5) + 6% TVA 129,32 19,0079 c€/kWh` -- so each band is the
variable card's mono row over again, and the conversion is identical: the
coefficients print c€/kWh against a BELIX in EUR/MWh, so the factor takes a x10
and the base a /100, with the VAT printed outside the parens landing on both
(PIC: `0,1 * 1,06 * 10 = 1,06` and `5 * 1,06 / 100 = 0,053`, illustrative).
All three bands are mandatory: defaulting one to another's rate would bill a
third of the day wrong, so a miss raises like the injection and tax parsers.

The card also prints a `Compteur exclusif nuit` row carrying the ECO formula.
It is deliberately not parsed: `ImpactRates` has no exclusive-night slot, and a
`tou_impact` contract offers only the dynamic (SMR3) meter in the config flow
(`flow_schemas.py:869`), so a dedicated night circuit cannot be configured on
one. Adding the field would be unreachable code.

The `Prix maximum` column IS parsed, on both cards. The card caps supply at
26,5 c€/kWh TVAC (footnote 8, "si le Belix venait a augmenter au point que
l'application du prix variable donne une valeur superieure au prix maximum,
c'est le prix maximum qui s'applique"). `_price_ceilings` reads it for the four
variable meter rows and the three Impact bands, and `ImpactRates` gained
`ceiling_pic` / `ceiling_medium` / `ceiling_eco` alongside the `ceiling_*`
columns `VariableRates` already had.

The column is the SECOND `c€/kWh` figure on the row, after the indicative, and
it arrived with the September 2026 cards: the December 2025 and April 2026
issues end the row at the indicative. A card that does not print it yields
`None`, never `0.0`, so an older issue stays uncapped instead of clamping every
kWh to zero, and the dynamic card carries no cap at all. At the September 2026
BELIX the cap sits 7 to 11 c€/kWh above the billed rate, so it binds only in a
price spike: above a BELIX monthly mean of about 267 EUR/MWh on the mono row
and about 200 on the Impact PIC band.

For `cociter_dynamic` (`cociter.py:617-622`) the SMR3 formula
`(factor x QUARTER HOURLY BELPEX sign base) + N% TVA` is parsed. Note the
regex tolerates the pypdf-split spelling `QUARTER HOURL Y` (the space inside
"HOURLY") via `QUARTER\s*HOURL\s*Y`. The unit conversion (`cociter.py:388-398`)
is the subtle part:

- BELPEX is quoted in EUR/MWh in the card; the spot the pricing engine uses is
  already EUR/kWh, so the factor is multiplied by 10.
- The card prints c€/kWh and applies VAT after the parentheses, so both factor
  and base are multiplied by the VAT multiplier `1 + N/100`.
- Net: `factor = factor_pdf * vat_mult * 10`, `base = base_c * vat_mult / 100`.

`test_dynamic_extracts_factor_and_base` (`test_cociter.py:302-317`) pins the
result literally: from `(0.103 x BELPEX + 3) x 1.06`, `factor == 1.0918` and
`base == 0.0318` (illustrative), and it checks `factor*0.10 + base == 0.14098`
at a spot of 100 EUR/MWh so a unit-conversion swap cannot cancel out. The
`quarter_hourly=True` flag (`cociter.py:400`) keeps the native 15-minute slots
(see `DynamicRates`, `base.py:166-206`).

### DSO overlay: `_extract_dsos`

`_extract_dsos` (`cociter.py:596-659`) parses one row per Wallonian DSO. The
DSO label to canonical registry key map is `_DSO_KEY` (`cociter.py:118-124`):

| PDF row label | Registry key |
|---------------|--------------|
| `AIEG` | `aieg` |
| `AIESH` | `aiesh` |
| `ORES` | `ores` |
| `RESA` | `resa` |
| `REW` | `rew` |

An assertion (`cociter.py:110-112`) enforces that this key set equals
`const.WALLONIA_DSO_KEYS`; if Cociter starts or stops serving a Wallonian DSO,
update `_DSO_KEY` and `const.WALLONIA_DSO_KEYS` in lockstep or import fails.

The three card layouts (`cociter.py:407-455`):

- Variable card: 6 numbers per row.
  `yearly | mono | dag | nacht | uitsl_nacht | tarif_prosumer`.
- Dynamic (SMR3) card: 8 numbers per row. The prosumer column is replaced by
  three Tarif Impact columns `PIC | MEDIUM | ECO`, because SMR3 dispenses with
  the compensation regime.
- Trihoraire card: 5 numbers per row, `yearly | pic | medium | eco |
  uitsl_nacht`. It is the one layout that is not an extension of the others:
  the mono and bi-hourly columns are gone, because an Impact customer is billed
  on the Impact network tariff and on nothing else. It also drops the
  `tarif_prosumer` column, which the variable card does carry, while still
  printing the supplier's own 37,10 EUR/kVA forfait "en regime de
  compensation". That combination is real (the card is headed `Compteur a
  releve annuel`), so a compensation entry on this contract gets the supplier
  half of its prosumer fee and none of the DSO half. The tariff is regulated
  and identical whichever supplier reprints it, which TotalEnergies settles by
  publishing it on the same Walloon row as its PIC / MEDIUM / ECO bands, so the
  omission is the card's and not the tariff's. Rather than borrow another
  supplier's figure, the coordinator raises `prosumer_tariff_missing` and says
  what the cost excludes: 81 to 99 EUR per kVA per year across the five GRDs,
  405 to 496 EUR a year on a 5 kVA inverter.

The trihoraire layout is picked out FIRST, by the card's own title (`prix
variable trihoraire`, `_TRIHORAIRE_CARD_RE`), before anything counts columns.
It has to be, because counting cannot tell it apart: the six-number pattern
below matches a five-number row by running past the end of the line -- the last
DSO row is followed by the `3.` of the taxes heading -- and when it does, it
lands the PIC rate on `distribution_single` and reports one DSO instead of
five. `test_trihoraire_layout_is_chosen_by_the_card_title` pins exactly that
mis-parse against a card with its title taken away. `_extract_impact_dsos`
anchors its row at both ends for the same reason, and raises when the table
yields nothing, since an empty overlay bills the whole network leg at zero.

`distribution_single` has no source on the trihoraire card and is filled with
the PIC rate. Nothing reads it -- the contract is `tou_impact`, so the config
flow forces the Impact network mode in Wallonia (`config_flow.py:543`) and the
compare page does the same (`compare_flow.py:1227`) -- and it is the highest of
the three bands so a path that somehow reached it would over-bill visibly
rather than under-bill quietly.

The parser discriminates on the literal header string `"Tarif prosumer"` in
the document (`cociter.py:421`), not on column count. This is deliberate: an
end-of-line anchor would silently lose the prosumer value if a 7th column were
ever added to the variable card. The row regex (`cociter.py:463-468`) captures
six mandatory numbers plus an optional trailing pair (columns 7 and 8). When
the header is present, column 6 is the prosumer rate; otherwise columns 6-8 are
the Impact `pic/medium/eco` distribution rates (divided by 100 to EUR/kWh).

`test_dso_extraction_keys_off_header_not_column_count` (`test_cociter.py:248-263`)
proves the discrimination: strip `"Tarif prosumer"` out of the variable card
and the parser reports no prosumer rate even though column 6 still has a
number, while distribution rates still parse. The variable DSO test
(`test_cociter.py:84-99`) pins illustrative AIEG values: `distribution_single
0.1087`, `distribution_peak 0.1205`, `distribution_offpeak 0.0666`, `transport
0.0274252`, `data_management_per_year 19.49`, `prosumer_eur_per_kva_year 81.03`.

Each row produces a `DsoOverlay` (`base.py:467-527`) with the four distribution
rates, the shared transport rate, `data_management_per_year` (column 1, not
divided), the optional prosumer forfait, and the optional Impact triplet.

### Transport: `_extract_transport`

`_extract_transport` (`cociter.py:761-769`) parses the single ELIA transport
rate from the `Tarifs de transport TVAC` row, shared across all DSO rows and
divided by 100 to EUR/kWh. The comment flags it as ~2.7-3.2 c€/kWh, roughly
20% of the all-in (illustrative). A miss is fatal (`cociter.py:462`);
under-billing every kWh silently is worse than a loud failure. The
`test_missing_transport_or_abonnement_is_fatal` test (`test_cociter.py:266-283`)
confirms both this and the abonnement raise.

### Taxes: `_extract_taxes`

`_extract_taxes` (`cociter.py:755-801`) pulls three things:

1. The Walloon renewables contribution, anchored on the quoted heading
   `"énergies renouvelables"` (accepting straight or curly quote glyphs) with
   the number within ~200 chars (`cociter.py:510-518`). A miss is fatal
   (`cociter.py:534-540`); the ~3 c€/kWh contribution is mandatory.
2. The `Taxes et redevances` block, a single line of three values anchored on
   the literal label trio `Cotisation énergie | Droit d'accises spécial |
   Redevance de raccordement` (`cociter.py:524-531`). A miss is fatal
   (`cociter.py:532-533`).

Mapping into `TaxOverlay` (`cociter.py:808-814`): `energy_contribution`
(cotisation énergie), `federal_excise` (droit d'accises spécial),
`region_connection_fee` (redevance de raccordement), and `wallonia_renewables`
(the quoted renewables value). Because Cociter is Wallonia-only,
`flanders_renewables` and `brussels_renewables` stay at their `0.0` defaults.

Critically `vat_rate=0.0` (`cociter.py:511`): the whole card is TVAC, so the
snapshot's prices are already VAT-inclusive and the pricing engine must not
re-apply VAT (see `TaxOverlay` comment, `base.py:644-691`). The tax test
(`test_cociter.py:166-179`) pins illustrative Wallonian values and asserts
`vat_rate == 0.0` and `flanders_renewables == 0.0`.

### Injection: `_extract_injection`

See the dedicated [Injection](#injection) section below.

### Supplier prosumer forfait: `_extract_supplier_prosumer`

`_extract_supplier_prosumer` (`cociter.py:271-291`) parses the variable card's
supplier-side PV forfait, billed on top of the DSO prosumer column. It is
returned for `cociter_variable` and `cociter_variable_impact`
(`cociter.py:286-287`); the dynamic SMR3 card dispenses with the compensation
regime and returns `None`.

The value comes from footnote (6), matched on the unique wording
`([\d,]+) €/kVA/an TVAC` (`cociter.py:257`). The card prints 37,10 EUR/kVA/an
TVAC (illustrative, pinned by `test_variable_extracts_supplier_prosumer_forfait`,
`test_cociter.py:102-111`). The anchor is deliberately the "EUR/kVA/an TVAC"
footnote wording, not the bare "(EUR/kVA/an)" DSO prosumer column header, so the
two do not collide. The value is already TVAC and must NOT be VAT-scaled
(`SupplierSnapshot` comment, `base.py:712-742`). A miss on the variable card is
fatal (`cociter.py:258-259`): every variable card prints it, so absence is a
layout drift, not a fee-free contract.

On the **trihoraire** card a miss returns `None` instead. That card prints the
forfait today, but it is a September 2026 product with one card published so
far, and its DSO table has already dropped the prosumer column the forfait sits
beside, so a later edition dropping it too is a plausible editorial change
rather than a layout drift -- and raising would take a working contract offline
over it. The live check asserts the forfait is present on both cards instead
(`cociter/<id>: supplier PV forfait present`), which notices the change without
an entry losing its price: about 185 EUR/yr on a 5 kVA install.

## Energy formula per TariffKind

```
cociter_variable  -> VariableRates(current, peak, offpeak, exclusive_night,
                                    yearly_fixed_fee, formula)
                     energy is the printed monthly indicative rate per meter;
                     BELIX formula is diagnostic text only.

cociter_variable_impact
                  -> ImpactRates(pic, medium, eco, yearly_fixed_fee, formula,
                                  <band>_factor, <band>_base)
                     one BELIX formula per CWaPE band; the printed rates are
                     the indicatives, the coefficients are diagnostic (see
                     ImpactRates, base.py).

cociter_dynamic   -> DynamicRates(factor, base, yearly_fixed_fee,
                                   quarter_hourly=True)
                     billed as factor * spot_eur_per_kwh + base per 15-min slot.
```

## DSO overlay coverage

All five Wallonian DSO sub-areas are mapped: AIEG, AIESH, ORES, RESA, REW
(`cociter.py:102-108`). Every card carries all five rows. The overlay per DSO
carries the shared ELIA transport rate, `data_management_per_year`, and then
what its card prints: the four distribution rates (single, peak, offpeak,
exclusive-night) plus the compensation-regime prosumer forfait on the variable
card, those four plus the three Tarif Impact bands PIC/MEDIUM/ECO on the
dynamic SMR3 card, and the three Impact bands plus exclusive-night alone on the
trihoraire card. The Impact bands feed the
CWaPE 3-band pricing when a customer opts into the DSO Impact tariff (see
`DsoOverlay` and `ImpactRates`, `base.py:467-527`, `base.py:332-364`).

## Tax overlay

Wallonia-only: `federal_excise`, `energy_contribution`, `wallonia_renewables`,
`region_connection_fee`, and `vat_rate=0.0` (prices are TVAC). Flanders and
Brussels renewables stay `0.0`.

## Injection

Cociter's injection is the third taxonomy shape: **spot-indexed variable**, an
hourly `factor*spot+base` BELPEX formula with no printed monthly indicative.
`_extract_injection` (`cociter.py:263-313`) sets `current=None` and populates
`factor` and `base` (`cociter.py:329-334`). Because there is no indicative
fallback, pricing the injection requires an ENTSO-E spot in the live, backfill,
and compare paths, all gated on the contract's `spot_indexed_injection` flag
(`cociter.py:588-590`); dropping the gate or the spot silently drifts the solar
credit.

Injection is VAT-exempt for residential, so `factor`/`base` are never VAT-scaled
(`InjectionRates` comment, `base.py:427-454`). Unit handling mirrors the dynamic
consumption side: the PDF factor (against BELPEX in EUR/MWh) is multiplied by 10
to work against a EUR/kWh spot, and the base (c€/kWh) is divided by 100
(`cociter.py:306-311`). From the printed `(0,097 x BELPEX - 2,1)` the tests pin
`factor == 0.97` and `base == -0.021` (illustrative,
`test_cociter.py:200-227`). The variable and dynamic cards carry the same
injection coefficients.

Two parsing subtleties:

- The dynamic SMR3 card carries two formulas (consumption first, injection
  later). The primary regex anchors on the `Le prix de l'injection` lead-in so
  the second formula is the one matched even when both use the same sign
  glyph (`cociter.py:287-294`). The apostrophe class `['‘’ʼ]` tolerates the
  several apostrophe glyphs pypdf may emit.
- The variable card has no anchor prose around the injection block, so a
  fallback regex matches the first `Tout compteur` formula
  (`cociter.py:295-303`).

Both regexes accept any `SIGN_CHARS` sign between the BELPEX factor and base,
and tolerate the split `QUARTER HOURL Y` spelling. A miss is fatal
(`cociter.py:304-305`): both products always publish an injection formula, so
absence is layout drift, not a fee-free contract. Failing loud keeps last-good
data and surfaces the breakage in logs and live-check.
`test_injection_missing_formula_raises` (`test_cociter.py:350-359`) confirms the
raise.

Prosumer forfaits: the DSO `prosumer_eur_per_kva_year` (variable card only) and
the supplier-side `supplier_prosumer_eur_per_kva_year` (variable card only, 37,10
EUR/kVA/an TVAC illustrative) are the compensation-regime PV charges billed to
prosumers; the dynamic SMR3 contract carries neither.

## Quirks and historical bugs

The land mines a future maintainer must know, each traceable to a source
comment:

- **Everything is TVAC.** Prices are VAT-inclusive, so `vat_rate=0.0`
  (`cociter.py:511`, `base.py:479-482`). The supplier PV forfait is likewise
  already TVAC and must never be VAT-scaled (`cociter.py:249-250`).
- **Fail-loud parsers.** The abonnement (`cociter.py:322`), ELIA transport
  (`cociter.py:462`), taxes block (`cociter.py:492`), Walloon renewables
  (`cociter.py:538-540`), injection formula (`cociter.py:305`), and the
  variable PV forfait (`cociter.py:259`) all raise on a miss rather than
  defaulting to zero, so a layout drift is visible in logs and live-check
  instead of silently under-billing.
- **Header-based DSO discrimination.** Column 6 means "prosumer" on the
  variable card but is the first of three Impact columns on the dynamic card;
  the parser keys off the literal `"Tarif prosumer"` header, not column count,
  to survive future column additions (`cociter.py:453-458`).
- **BELPEX unit + VAT conversion.** Dynamic factor is `factor_pdf * vat_mult *
  10` and base is `base_c * vat_mult / 100` (`cociter.py:388-398`). The VAT
  percentage is captured from the card's trailing `+ N% TVA`, not hardcoded, so
  a VAT change tracks automatically.
- **quarter_hourly=True.** Cociter Dynamique bills on the 15-minute Belpex grid;
  keep native quarter-hour slots, not the hourly mean (`cociter.py:394-400`,
  `base.py:166-206`). YTD statistics still aggregate to hourly.
- **Split-glyph spellings.** pypdf can split "HOURLY" into `HOURL Y` and emit
  several apostrophe/quote/dash glyphs; the regexes tolerate all of these
  (`cociter.py:340-352`, `cociter.py:382`, `SIGN_CHARS`, `_pdf.py:713`).
- **Injection has no indicative fallback.** `current=None` always; the credit
  is spot-only, gated on `spot_indexed_injection` (`cociter.py:865-873`).
  Losing the gate zeros or drifts the solar credit.
- **Archive validity cross-check.** `fetch_for_month` runs
  `archive_validity_check` with the French month names so a CDN-substituted
  current card served under an archived URL is rejected rather than mis-billed
  (`cociter.py:169`, `_pdf.py:755-791`).
- **DSO map / const lockstep assertion.** `_DSO_KEY` must equal
  `WALLONIA_DSO_KEYS` or import fails (`cociter.py:126-128`).

## Test fixtures

Under `tests/fixtures/`:

| Fixture | Card variant |
|---------|--------------|
| `cociter_var_2604.pdf` | Variable card, April 2026. Ground truth for indicative rates, DSO overlay, taxes, injection, and the supplier PV forfait. |
| `cociter_dyn_2604.pdf` | Dynamic SMR3 card, April 2026. Ground truth for the factor/base conversion, `quarter_hourly`, absence of the prosumer forfait, and the SMR3 injection formula. |
| `cociter_vai_2609.pdf` | Trihoraire card, September 2026 -- the first one published. Ground truth for the three Impact bands and their coefficients, the five-column DSO row, and the title-driven layout choice. |
| `cociter_var_2512.pdf` | Variable card, December 2025. Used by `test_fetch_for_month_returns_snapshot_when_listing_has_url` to prove archive fetch parses a non-current month and sets `publication_label == "2025-12"`. |

The `_LISTING_HTML` fixture is inline in the test module
(`test_cociter.py:253-257`), not a file, and models the listing page's
`<a href>` links for the three `fetch_for_month` tests.

## When the card changes, look here

Ordered by likelihood of breaking when Cociter re-renders its cards:

1. **Filename or listing URL change** -> `_VAR_RE` / `_DYN_RE`
   (`cociter.py:99-104`), `_INDEX_URL` (`cociter.py:83`), and the `discover`
   family regex (`cociter.py:212-214`). Everything (fetch, probe,
   fetch_for_month, discover) keys off these.
2. **Energy row labels or formula wording** -> `_extract_energy`
   (`cociter.py:316-401`): the `Compteur monohoraire` / `Heures pleines` /
   `Heures creuses` / `Compteur exclusif nuit` anchors and the
   `QUARTER HOURLY BELPEX ... + N% TVA` dynamic regex.
3. **DSO table layout / column order / new column** -> `_extract_dsos`
   (`cociter.py:487-550`) and the `"Tarif prosumer"` header discriminator
   (`cociter.py:421`). A new DSO also needs `_DSO_KEY` and
   `const.WALLONIA_DSO_KEYS` updated together (`cociter.py:126-128`).
4. **Injection formula relocation or wording** -> `_extract_injection`
   (`cociter.py:263-313`): the `Le prix de l'injection` anchor and the
   `Tout compteur` fallback.
5. **Tax block relabeling** -> `_extract_taxes` (`cociter.py:755-801`): the
   `énergies renouvelables` and `Cotisation énergie / Droit d'accises spécial /
   Redevance de raccordement` anchors.
6. **Transport row rename** -> `_extract_transport` (`cociter.py:761-769`).
7. **PV forfait footnote rewording** -> `_extract_supplier_prosumer`
   (`cociter.py:240-260`): the `EUR/kVA/an TVAC` anchor.
8. **Validity-header format change** -> shared `parse_valid_until`
   (`_pdf.py:794`) and the `_FR_MONTHS` fallback used by
   `archive_validity_check`.

When a card changes, capture the new PDF into `tests/fixtures/` and update the
pinned assertions in `tests/test_cociter.py`; the tests are the ground truth for
what each parser must produce.

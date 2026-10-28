# Provider: luminus

This document is the maintainer reference for the Luminus extractor
(`providers/luminus.py`). Luminus is a large Belgian residential supplier selling
in Flanders and Wallonia only; it publishes the current month's tariff card as a
fresh PDF served by a public REST endpoint, one card per (product, region). The
extractor fetches exactly the configured region's PDF, never merges regions, and
parses the energy formula, the DSO network / capacity overlay, the tax overlay,
and the injection (solar feed-in) rate out of that single card. Read this
alongside the shared contracts and the pricing math it feeds:

- [../provider-framework.md](../provider-framework.md): the `SupplierExtractor`
  protocol, the `Contract` / `SupplierSnapshot` / `DsoOverlay` dataclasses, and
  the shared `_pdf.py` helpers this module calls.
- [../pricing-model.md](../pricing-model.md): how `compute_breakdown` consumes
  the snapshot's energy, DSO, tax and injection fields.

The test module `tests/test_luminus.py` pins the expected parse output against
real April/May 2026 fixtures and is the ground truth for what the extractor
should produce. Every illustrative number below is quoted from a source comment
or a test assertion and labelled as such.

## Overview

| Property | Value | Source |
| --- | --- | --- |
| Extractor id | `luminus` | `luminus.py` |
| Label | `Luminus` | `luminus.py` |
| Regions served | Flanders, Wallonia | `luminus.py`, `_LUMINUS_REGIONS` |
| Publication | one fresh PDF per (product, region) via REST endpoint | `luminus.py` |
| Probe | none: the `SupplierExtractor` is built without a `probe` argument, so it defaults to `None` | `luminus.py` |
| Archive | `fetch_for_month` reads the site's price-list archive (`api/pricelist/products` then `api/pricelist/pdf`), back to September 2021 (see below) | `luminus.py` |

Brussels is deliberately out of scope. Luminus sells only the regulated Social
tariff there, which the social tariff supplier prices from the CREG's card
([social.md](social.md)). A fetch for `brussels` raises `ExtractorError` with the
message `not available in region` (`luminus.py`, asserted by
`test_brussels_is_unsupported`).

### Source URL pattern

The card is fetched from a single REST endpoint that returns a fresh PDF per
request (`luminus.py`):

```
https://www.luminus.be/api-next/get-pricelist/
    ?documentSlug=<slug>&energyType=electricity&language=fr&tabValue=<Flanders|Wallonia>
```

`slug` is the product's `documentSlug` query parameter (per contract, see the
table below). `tabValue` is the region tab: `Flanders` or `Wallonia`
(`_REGION_TO_TAB`, `luminus.py`). The response filename encodes the
month, e.g. April 2026 -> `202604` (`luminus.py`). `language=fr` is
hardcoded, so every card the extractor parses is the French-language variant;
all label regexes below are French.

## Contracts

Ten user-selectable products, all available in both Flanders and Wallonia (each
`Contract.regions` is `_LUMINUS_REGIONS`, so `EXTRACTOR.regions()` is
`{flanders, wallonia}`). Nine set `spot_indexed_injection` (Comfy, Comfy Plus,
ComfyFlex, ComfyFlex Plus, MaxxFix, MaxxFlex, BasicFix, BasicFlex, SmartFlex), so
the flow offers the ENTSO-E key on the injection regime; Dynamic collects the key
through its energy formula already. ComfyFlex, ComfyFlex Plus, MaxxFlex, SmartFlex
and BasicFlex also carry `month_indexed_energy`, which offers the key on every
solar regime.

| Contract id | Label | Kind | Slug | Notes |
| --- | --- | --- | --- | --- |
| `luminus_comfy` | Luminus Comfy | fixed | `comfy` | Fixed price, bi-hourly + exclusive-night columns |
| `luminus_comfy_plus` | Luminus Comfy+ | fixed | `comfy-plus` | Fixed variant |
| `luminus_comfyflex` | Luminus ComfyFlex | variable | `comfyflex` | Indexed on the delivery quarter's Belpex |
| `luminus_comfyflex_plus` | Luminus ComfyFlex+ | variable | `comfyflex-plus` | Variable variant (drop-in, same parse path) |
| `luminus_maxxfix` | Luminus MaxxFix | fixed | `maxxfix` | Fixed variant |
| `luminus_maxxflex` | Luminus MaxxFlex | variable | `maxxflex` | Variable variant |
| `luminus_basicfix` | Luminus BasicFix | fixed | `basicfix` | Fixed variant |
| `luminus_basicflex` | Luminus BasicFlex | variable | `basicflex` | Monthly-indexed on the delivery month's Belpex RLP M |
| `luminus_smartflex` | Luminus SmartFlex | tou | `smartflex` | Time-of-use (3 seasonal bands), needs SMR3 |
| `luminus_dynamic` | Luminus Dynamic | dynamic | `dynamic` | `factor*Belpex H + base`, hourly billing |

MaxxFlex, SmartFlex and BasicFlex carry `month_indexed_energy`, the registry twin of
the parsed `month_indexed`, which offers the optional ENTSO-E key on every solar
regime, and ComfyFlex and ComfyFlex+ carry it for the parsed `quarter_indexed`.
MaxxFlex and SmartFlex index on the plain monthly Belpex. BasicFlex prints "x Belpex RLP M" and
weights the month "avec une ponderation RLP ... basee sur la moyenne arithmetique des
profils de consommation RLP des differents gestionnaires", so its leg is
`rlp_indexed` on the `columns` blend, which reproduces the Belpex RLP M Luminus
published for January to September 2026 to 0,01 EUR/MWh. Its rows have a pattern
of their own (`_rlp_band_formula_re`), so the bare-Belpex rows stay as narrow as
they were.

ComfyFlex and ComfyFlex+ print the same per-meter rows with a second term,
"Compteur mono-horaire = 0,1099 x Belpex + 0,0000 x Endex 1-0-3 + 4,5041", and index
them on the delivery QUARTER: "Votre tarif sera indexe tous les trimestres ... Les
prix affiches sont calcules sur la base de la derniere valeur Belpex connue
(trimestre precedent) ... Sur le decompte, Luminus appliquera les valeurs moyennes
trimestrielles". `_quarterly_energy_coefficients` (`_luminus_cards.py`) reads them,
gated on that sentence and on the energy block naming a quarter
("valeur de l'indice du 3ieme trimestre 2026"), and only at a zero Endex factor,
which every card since January 2026 prints: a card giving the forward index weight
keeps its printed rates. The leg is `quarter_indexed` and not `month_indexed`, as
Bolt's variable cards are, so an entry with day-ahead prices bills the running
quarter on its mean to date, each register on its own pair
(`cohort_legs._quarter_leg`), and a closed quarter settles on the next quarter's
card (below). The index is the quarter's plain mean: the monthly values Luminus
prints for MaxxFlex average back to the second and third quarters of 2026
(94,25 and 131,39). Read as printed figures before, every closed quarter of 2026
billed on the one before it: 4,33 c/kWh under on ComfyFlex in Flanders in the
third, the feed-in 1,79 under. The April to June 2026 cards wrap the mono label
after its hyphen ("Compteur mono-\nhoraire"), which `_ENERGY_BANDS` allows for.

Declared in `_CONTRACTS` (`luminus.py`); `_CONTRACTS_BY_ID` indexes them
(`luminus.py`); `EXTRACTOR.contracts` is built from them (`luminus.py`).

Not declared: **Luminus Sociaal/Social**, the regulated CREG tariff, which the
social tariff supplier prices; it reads Luminus's social card for its feed-in
and the Walloon connection fee ([social.md](social.md)).

### Dynamic billing grid

`luminus_dynamic` bills per clock hour, not per quarter-hour. The `DynamicRates`
returned by `_extract_energy` leaves `quarter_hourly` at its default `False`
(`_luminus_cards.py`), which is what routes the coordinator to aggregate
ENTSO-E's 15-minute day-ahead curve to hourly. See the `DynamicRates` docstring
(`_rates.py`): Luminus is listed among the hourly-billing dynamic
suppliers (Frank default, Mega, TotalEnergies, Eneco).

## Fetch strategy

`fetch(session, contract_id, region)` (`luminus.py`):

1. Reject an unknown `contract_id` (`ExtractorError: unknown Luminus contract`).
2. Reject a region other than Flanders/Wallonia (`not available in region`).
3. Build the URL with `_document_url(contract.slug, region)`.
4. `fetch_pdf_text(session, url)` downloads and pypdf-extracts the PDF text
   (`_pdf.py`); the payload is magic-byte-validated as a real PDF, so a
   CDN 404-disguised-as-HTML fails loud (`_pdf.py`).
5. Hand the text to `parse_snapshot` (the pure parser exposed for unit tests,
   `luminus.py`).

### Probe

There is no probe. `EXTRACTOR` does not set `probe`, so it defaults to `None`
(`base.py`). Per the `SnapshotProbe` contract (`base.py`), the
`api-next/get-pricelist/` endpoint mints a fresh PDF per request with no cheap
freshness key the coordinator can rely on, so the time-based TTL takes over.

### Archive

The live endpoint is overwrite-in-place (each slug always returns the current
month), but the site's "Archives de listes de prix" app is backed by two endpoints
that `fetch_for_month` reads:

- `www.luminus.be/api/pricelist/products?language=FR&customerSegment=Residential&energyType=Electricity&region=<Flanders|Wallonia>&signing=YYYY-MM`
  names the products that month had, each with an opaque product id.
  `_archive_product_name` folds the archive's label ("Luminus Comfy Electricité",
  "Luminus BasicFix Online Electricité") onto the catalogue label, the energy word
  and the online marker being the only differences, and `_resolve_archive_product_id`
  picks the id. A month whose list lacks the product answers `None` without a PDF
  fetch.
- `www.luminus.be/api/pricelist/pdf?language=FR&productId=<id>&date=YYYY-MM&region=<R>&inline=true`
  serves that product's card. The PDF arrives with a UTF-8 byte-order mark before
  the magic bytes, which the shared fetch helper already tolerates.

The app's own picker goes back to September 2021. Measured on 11 September 2026,
the June 2026 Comfy Flanders card parsed whole (`avril`-style label, validity
date 30 June 2026, all eight DSO rows), and the validity date is the authoritative
cross-check in `archive_validity_check`, the month name in the title the fallback.
No card of 2025 parsed until the feed-in row those cards print was read (see
Injection), so a contract signed in 2025 was billed today's card whatever its start
date. Measured on 28 October 2026, every Flemish card of the eight products sold in
2025 (Comfy, Comfy+, ComfyFlex, ComfyFlex+, MaxxFix, MaxxFlex, BasicFix and
BasicFlex, January to December, 96 cards) reads whole, each field equal to the
card's text. Those cards served both regions, and their Walloon side reads whole
too, as does every card of those products the archive serves for 2024 on the
Wallonia tab: 184 cards. The Dynamic card reads there from May 2025, when it was
first sold in Wallonia, its network rows taken from a sibling card (see Wallonia
below).

The running month is not asked for, and `fetch_for_month` answers `None` for it.
The archive keeps serving a month's first edition after the live card is
reissued: on 5 October 2026 it still served the October cards of the 1st, priced
on August's Belpex, where the live cards had been corrected on the 2nd to
September's, 4,3 c/kWh apart on ComfyFlex. The month cache then takes the live
card for the running month, as it does for Mega.

### Settling a closed month in place

The month-indexed rates (MaxxFlex, SmartFlex and BasicFlex energy, and the monthly
feed-in of every card but ComfyFlex, ComfyFlex+ and Dynamic) are printed at the
previous month's index, and the next card names the month's own: "Belpex = 156,41 EUR/MWh (valeur de l'indice
de septembre 2026)", and "Belpex RLP M = 164,42" on BasicFlex (`published_indices`,
`_luminus_cards.py`). `settle_month` (`luminus.py`) re-prices the card held for a
closed month on that index, through `settled_energy` and `settled_injection`
(`_settle.py`), and records it as `index_realised`. The next card is the live one
while its month runs and the archive's after it. Not out yet, or naming another
month while it is the live one, the month stays `provisional`; a closed next card
naming another month settles nothing.

ComfyFlex and ComfyFlex+ settle the same way a quarter at a time: every month of a
quarter, energy and feed-in, settles on the index the first card of the quarter
after names for it ("Belpex = 131,39 EUR/MWh (valeur de l'indice du 3ieme trimestre
2026)" on October's), read by `published_indices(text, quarterly=True)`. The
energy leg is settled register by register on its own pairs, and stays
`quarter_indexed`. Until then an entry without day-ahead prices bills the printed
rates, the previous quarter's.

It is IN PLACE (`EXTRACTOR.settle_month`), never a replacement by
`fetch_for_month`'s answer: the archive serves a month without its new-customer
campaign, and served the uncorrected first October edition for days. The month
cache and the daily archive run both keep the captured card whole and move only
those rates. Replayed over every archived 2026 month, the settled rates match the
keyed engine's month mean to 0,001 c/kWh, where the printed ones ran a month
behind: BasicFlex 3,26 c/kWh under in September, MaxxFlex 3,15, SmartFlex's peak
band 3,74, the feed-in 1,30.

This is what makes a contract start date work on a Luminus entry: until it was
wired the signing-cohort splice had no card to read, so the entry stayed on the
current card whatever date was set, and every past month of the year-to-date was
billed on today's card as a proxy. The docs used to say there was no accessible
archive; the app had one all along.

### `discover()`

`discover(session)` (`luminus.py`) is a CI / live-check helper (not part
of the coordinator's runtime path). It GETs the sitemap
(`https://www.luminus.be/sitemap.xml`, `luminus.py`) and scrapes product
slugs from the `/fr|nl/particuliers/tarifs-energie|onze-tarieven/<slug>/`
structure (`_PRODUCT_PAGE_RE`, `luminus.py`), excluding the regulated
social-tariff index pages (`_EXCLUDED_SLUGS`, `luminus.py`). A failed sitemap
fetch returns an empty set rather than raising.

## Parsing

`parse_snapshot` (`luminus.py`) assembles the snapshot from a set of
focused helpers. Because energy prices, distribution rows and renewables
surcharges all differ between Flanders and Wallonia on every product, the parser
branches hard on `region` (`luminus.py`) and never merges. That region-
awareness is not cosmetic: `test_dynamic_flanders_has_a_different_base`
(`test_luminus.py`) shows the dynamic formula's base is region-specific
(Flanders 50 cents below Wallonia in the fixtures), so a merged snapshot would
silently give one region the wrong base.

Fields pulled and their helpers:

| Field | Helper | Source |
| --- | --- | --- |
| Energy rates | `_extract_energy` | `_luminus_cards.py` |
| Injection | `_extract_injection` | `_luminus_cards.py` |
| Publication label | `_extract_publication_month` | `luminus.py` |
| Per-kWh taxes (excise, contribution, connection) | `_extract_per_kwh_taxes` | `_luminus_overlays.py` |
| Energy fund (Flanders only) | `_extract_energy_fund` | `_luminus_overlays.py` |
| Flanders renewables | `_extract_flanders_renewables` | `_luminus_overlays.py` |
| Wallonia renewables | `_extract_wallonia_renewables` | `_luminus_overlays.py` |
| Flanders DSO overlay | `_extract_flanders_dsos` | `_luminus_overlays.py` |
| Wallonia DSO overlay | `_extract_wallonia_dsos` | `_luminus_overlays.py` |
| Yearly fixed fee | `_extract_yearly_fee` | `_luminus_cards.py` |
| Exclusive-night fee | `_extract_excl_night_fee` | `_luminus_cards.py` |
| VAT multiplier | `_vat_multiplier` | `_luminus_cards.py` |
| `valid_until` | `parse_valid_until` (shared) | `_validity.py` |

### Numeric token

`_NUM = r"\d+(?:[,.]\d+)?"` (`_luminus_overlays.py`) is anchored on a starting and
ending digit precisely so a trailing sentence period is not captured. The comment
flags the concrete hazard: `0,1019 x Belpex H + 2,4591.\n` from
`luminus_dynamic_w` would grab the final `.` under a lazier `[\d,.]+`
(`_luminus_overlays.py`). Values are parsed with the shared `to_float` (handles
Belgian comma decimals and every Unicode space variant, `_parse.py`).

### Units

Printed energy rows are in `c€/kWh`; the extractor divides by 100 to store
EUR/kWh (`_luminus_cards.py`). Prices are 6% VAT inclusive as printed
(`luminus.py`), so the snapshot's `TaxOverlay.vat_rate` is set to `0.0`
(`luminus.py`) meaning "already VAT-incl" per the `TaxOverlay` convention
(`base.py`). The one exception is the Dynamic formula, printed `hors TVA`
(ex-VAT), handled below.

### Publication label

`_extract_publication_month` (`luminus.py`) reads the parenthesised
`(<month> <year>)` on the first page, e.g. `(avril 2026)`. The May 2026 cards
started padding the inside of the parens (`(mai 2026 )` with a trailing space),
so the regex tolerates optional whitespace inside the parens
(`test_publication_label_tolerates_padded_parens`, `test_luminus.py`).

## Energy formula per kind

`_extract_energy(text, kind)` (`_luminus_cards.py`) always parses the yearly
fixed fee first (`_extract_yearly_fee`), then branches on `kind`.

### fixed / variable

Both parse the same four-column `Énergie fournie (c€/kWh)` row (mono / pleines /
creuses / exclusif-nuit), each divided by 100 (`_luminus_cards.py`). `fixed`
returns `FixedRates(single, peak, offpeak, exclusive_night, ...)`
(`_luminus_cards.py`); `variable` returns `VariableRates(current=mono, peak,
offpeak, exclusive_night, ...)` (`_luminus_cards.py`). Illustrative
(`test_comfy_wallonia_fixed_rates_and_dso`): mono `0.2038`, pleines `0.2374`,
creuses `0.1771`, exclusive-night `0.1771` from `luminus_comfy_w.pdf`.

Both carry two yearly fees: `yearly_fixed_fee` (the standard `Redevance fixe`)
and `yearly_fixed_fee_exclusive_night` from `_extract_excl_night_fee`.

The formula rows of the indexed variable cards are read by `_ENERGY_BANDS`
(`_luminus_cards.py`) in both spellings: `Compteur mono-horaire`, `Heures pleines
(Compteur bi-horaire)`, `Heures creuses` and `Exclusif nuit` since August 2025, and
until July 2025 rows named after the meter, `Jour (compteur de jour)`, `Jour
(compteur de jour et de nuit)`, `et nuit` (`Nuit` on ComfyFlex) and `Exclusif nuit`
(`test_a_2025_variable_card_reads_its_formulas_by_meter`). The energy block ends at
the feed-in block's heading, `Formule tarifaire de` since January 2026 and `Formule
tarifaire pour la compensation de` before, so a feed-in row is never read as an
energy one.

### tou (SmartFlex)

Parses the `Énergie fournie (c€/kWh)` row (peak / transition / offpeak) by
asking `numeric_row` for a row four figures wide, then three. The October 2026
cards print a fourth column, Happy Sunday; earlier ones print three. The
lookup stops at "Informations sur le prix transitoire": below it the same
label carries the transitional MaxxFlex rates (mono / jour / nuit) billed to a
customer whose SMR3 conditions are not met, three wide, which the three-figure
search reached as soon as the real row grew a fourth column
(`_luminus_cards.py`). Returns `TimeOfUseRates(peak, transition, offpeak,
yearly_fixed_fee, weekend_rule="smartflex_seasonal")` (`_luminus_cards.py`).

SmartFlex uses seasonal windows, not the generic CWaPE schedule: peak (pleines)
07-11 + 17-22 all year, the cheapest super-creuses band 11-17 only in spring/
summer (21/03-20/09), 22-07 always creuses. The `weekend_rule`
`"smartflex_seasonal"` tells `pricing.tou_slot` to bill those windows. The
October 2026 card adds a fourth band, Happy Sunday: Sundays 11-17 of the
spring/summer season, which super-creuses now leaves out ("Du lundi au
samedi"), priced *"Prelevement Happy Sunday = 0 x Belpex + 0"*. Earlier cards
ran free Sundays as a first-year promotion, which was never modelled; this
one is a rate. Its printed figure lands in `TimeOfUseRates.sunday` and its
formula in `formula_factor_sunday` / `formula_base_sunday` (`_luminus_cards.py`),
and both the printed rate and the monthly leg bill it ahead of the slot rule
(`is_happy_sunday`, `pricing.py`).
Illustrative (`test_smartflex_parses_as_time_of_use`): peak `0.1554`, transition
`0.1329`, offpeak `0.0672` from `luminus_smartflex_w.pdf`.

Both the `TimeOfUseRates` docstring in `_rates.py` and this extractor use the
`smartflex_seasonal` weekend rule for SmartFlex; the extractor and its test
(`test_luminus.py`) pin the seasonal behavior.

### dynamic

Parses the ex-VAT formula `<label> = <factor> x Belpex H <sign> <base>` out of
the block the card heads "Formule(s) tarifaire(s) pour le coût de l'énergie"
(`_DYNAMIC_ENERGY_BLOCK_RE`, rows `_BELPEX_H_ROW_RE`, read by `_belpex_h_pair`,
`_luminus_cards.py`). The rows are one per meter register in three spellings:
a single `Prélèvement (Compteur mono-horaire et bi-horaire)` row since
September 2025; `Compteur mono-horaire`, `Heures pleines (Compteur
bi-horaire)` and `Heures creuses` in August 2025; `Jour (compteur de jour)`,
`Jour (compteur de jour et de nuit)` and `et nuit` from January 2024 to July
2025. Every row is read and a card whose rows disagree is refused, since a
`DynamicRates` leg bills one formula on every meter; every card on file prints
the same one on each register. Reading the later spelling alone left every card
before September 2025 unparsed, so a contract signed on one billed each month's
own card and those months stood in on the current one; their printed annual
estimates reproduce at the Belpex H each expects for the year ahead
(`test_dynamic_cards_before_september_2025_read_their_formulas`). That made the
Flemish edition read, January 2024 to August 2025. The Walloon one did not: the
product was sold in Flanders alone until April 2025 (*"le point de fourniture se
trouve en Région flamande"*), and the archive serves that Flemish card on the
Wallonia tab, which rightly stays unread there; from May to December 2025 the
card served both regions, and its Walloon side reads since the renewables and
tax readers find the Walloon column (see Tax overlay), its network rows taken
from a sibling card of the month (see Wallonia below). The sign
character is matched from the shared `SIGN_CHARS` class and resolved with
`parse_sign` (`_parse.py`), so a card that flips to a Unicode minus does not
silently break polarity.

The PDF formula is `c€/kWh hors TVA = factor_pdf * Belpex_eur_mwh + base_cents`.
The extractor converts to EUR/kWh against a EUR/kWh spot and applies the parsed
6% VAT multiplier (`_luminus_cards.py`):

```
factor_eur_kwh = factor_pdf * vat * 10.0        # (*1000 mWh->kWh, /100 c->EUR)
base_eur_kwh   = base_pre_vat_cents * vat / 100.0
```

Illustrative (`test_dynamic_wallonia_extracts_consumption_formula`): the PDF
prints `0,1019 x Belpex H + 2,4591` at 6% VAT, yielding `factor == 1.08014` and
`base == 0.02606646`. The test pins the literal results (not
`0.1019 * 1.06 * 10`) so a `1.06 <-> 10` unit-conversion swap cannot cancel out
and pass (`test_luminus.py`).

The VAT rate is read by `_vat_multiplier` (`_luminus_cards.py`), which wraps the
shared `vat_multiplier` helper with two Luminus-specific patterns
(`TVA sur les prix ... N %` and `TVA N %`) and the shared 1.06 default
(`_pdf.py`).

## DSO overlay coverage

The DSO table is parsed per region. Distribution values are stored in EUR/kWh
(divide by 100); capacity, data-management and prosumer fees stay in their EUR/yr
units. Distribution already includes transport on the Flanders side (same
convention as Engie), so `transport` is set to `0.0` there (`_luminus_overlays.py`).

### Flanders (`_extract_flanders_dsos`, `_luminus_overlays.py`)

Eight Fluvius sub-areas mapped by printed label to canonical key
(`_FLANDERS_LABELS`, `_luminus_overlays.py`). Watch the two label-to-key surprises:

| Printed label | Canonical key |
| --- | --- |
| Fluvius Antwerpen | `fluvius_antwerpen` |
| Fluvius Halle-Vilvoorde | `fluvius_halle_vilvoorde` |
| Fluvius Imewo | `fluvius_imewo` |
| Fluvius Kempen | `fluvius_iveka` (note: Kempen -> IVEKA) |
| Fluvius Limburg | `fluvius_limburg` |
| Fluvius Midden-Vlaanderen | `fluvius_intergem` (note: Midden-Vlaanderen -> INTERGEM) |
| Fluvius West | `fluvius_west` |
| Fluvius Zenne-Dijle | `fluvius_zenne_dijle` |

Two column layouts are read by asking for each width in turn
(`_luminus_overlays.py`):

- **Static (fixed/variable/tou) cards print 8 numbers**: data_mgmt €/an,
  capacity_digital €/kW/yr, dist_normal, dist_excl_night, capacity_classic,
  dist_classic_normal, dist_classic_excl, prosumer €/kW/yr. The parser reads the
  digital-meter columns (`nums[0..3]`) plus `nums[7]` prosumer (`_luminus_overlays.py`).
- **Dynamic (SMR3) cards print 4 numbers**: data_mgmt, capacity_digital,
  dist_normal, dist_excl_night, no analog or prosumer columns. `prosumer` stays
  `None` (`_luminus_overlays.py`), because post-2024 SMR3 connections carry no
  compensation regime (see `DsoOverlay.prosumer_eur_per_kva_year`,
  `base.py`).

The SMR3 data-management gotcha: the dynamic card prints a
`(**) Pour des régimes de mesure par quart d'heure, le tarif de gestion des
données s'élève à 18,56 € par an` footnote beside the table's 18,92. Fluvius has
billed every low-voltage metering regime one data-management fee since 2025,
17,85 EUR a year excluding VAT in 2026 (18,92 with VAT), so the footnote kept
2025's figure through 2026, and `_extract_flanders_dsos` (`_luminus_overlays.py`)
bills the table on every card from 2025. Billing the footnote put a
quarter-hourly household 0,36 EUR a year under. Before 2025 the quarter-hourly
regime the dynamic contract needs had a fee of its own and the footnote carried
it (15,14 against the table's monthly-regime 13,95 in June 2024), so a dynamic
card whose label names an earlier year bills the footnote
(`test_the_quarter_hour_data_fee_is_billed_while_fluvius_kept_it_apart`).
`test_flanders_dynamic_dso_table_is_smaller_than_static`
(`test_luminus.py`) pins the 2026 case: Antwerpen data-management `18.92` on
both the dynamic and the static card, and the dynamic prosumer is `None` while
static is `54.63` (illustrative).

### Wallonia (`_extract_wallonia_dsos`, `_luminus_overlays.py`)

Five DSO sub-areas mapped by printed label (`_WALLONIA_LABELS`,
`_luminus_overlays.py`):

| Printed label | Canonical key |
| --- | --- |
| AIEG | `aieg` |
| AIESH | `aiesh` |
| ORES (Brabant Wallon) | `ores` |
| TECTEO RESA | `resa` |
| WAVRE | `rew` |

Two column layouts (`_luminus_overlays.py`):

- **Static rows have 7 numbers**: mono, pleines, creuses, excl_nuit, transport,
  data_mgmt, prosumer. `prosumer` is populated (`nums[6]`), the Impact bands stay
  `None`.
- **SMR3 rows have 9 numbers**: mono, pleines, creuses, the Impact triplet,
  excl_nuit, transport, data_mgmt. The Impact triplet takes the prosumer
  column's place. Dynamic's card names no prosumer tariff and keeps none.
  SmartFlex's card printed the prosumer column until September 2026 and the
  triplet from October, while its footnote still bills the tariff to a
  bidirectional meter under compensation (*"si ce calcul est plus avantageux
  que la facturation du tarif prosumer"*). The rate is the DSO's, so
  `_with_sibling_prosumer` (`luminus.py`) takes it from a sibling Wallonia card
  of the same month (ComfyFlex, then MaxxFlex, then BasicFlex; the archive's
  card of that month for a past one) whenever a card names the tariff and
  prints no rate, and the card fails to parse when no sibling carries it.
- **The Dynamic card of May to December 2025 prints 4 numbers**: mono,
  pleines, creuses, excl_nuit, the distribution alone, with no transport and no
  fixed term. `_extract_wallonia_dsos` reads no row off it, since those would be
  figures the card never printed. The figures are the DSO's, so
  `_with_sibling_network` (`luminus.py`) takes the whole rows from a sibling card
  of the same month and region (ComfyFlex, then MaxxFlex, then BasicFlex), and
  only from one printing the very distribution figures the Dynamic card prints,
  area by area (`_wallonia_distribution`), leaving out the prosumer rate, which
  no Dynamic card names. Measured on 28 October 2026, all eight months match
  ComfyFlex's card of the month field for field. Where no sibling matches, the
  card keeps no network table and a month billed on it is dropped as on any card
  short of the entry's area; a cohort signed on it bills its formulas either
  way. The short-table borrowing could not have served it: a card with no row
  read searches no other month (`snapshot_areas._short_of`), and no Dynamic card
  of 2025 prints the Walloon table whole
  (`test_the_dynamic_card_takes_its_walloon_network_from_a_sibling`).

Band-ordering gotcha: the triplet's order is read from the column headings
(`_impact_order`, `_luminus_overlays.py`), never assumed. Cards up to September
2026 print **ECO | MEDIUM | PIC**, the October 2026 cards turned it round to
**PIC | MEDIUM | ECO** with the same figures, and a positional read billed every
ECO hour at the PIC rate. Headings that do not name all three bands raise.
pypdf 6.20 runs the headings and their hours together with no space
("Tarif ImpactHeures PIC17h-22h Heures MEDIUM07h-11h22h-01h ..."), so neither
"Impact" nor a band name is read with a word boundary after it.
Illustrative
(`test_comfy_wallonia_fixed_rates_and_dso`): AIEG mono `0.1087`, pleines
`0.1205`, creuses `0.0666`, transport `0.0274`, prosumer `81.03`.

## Tax overlay

`_extract_per_kwh_taxes` (`_luminus_overlays.py`) reads the
`3 Taxes et redevances : WAL|FL|BRU` block via `_tax_block_values`
(`_luminus_overlays.py`). That helper anchors on the colon after the label because
`Taxes et redevances` also appears in the `Composition du prix` legend without a
colon or region (`_luminus_overlays.py`); the block runs until
`INFORMATION SUR VOTRE TARIF` or `Conditions`. Inside the block, values sit alone
on their own lines, and the parser collects that contiguous run of `-` /
`_NUM` tokens (`_luminus_overlays.py`). The label order and matching value order are
documented in the `_tax_block_values` docstring (`_luminus_overlays.py`): BTNR,
BTR, excise, contribution, and (Wallonia only) connection.

Until December 2025 one card served both regions. Its block is headed
`3 Taxes et redevances : FL WAL` and prints the Flemish column's five values and
then the Walloon column's, so `_tax_block_values(text, region)` takes the
region's own share of the run, and a block naming one region is that region's
whole. Read as one run, a Walloon card took the Flemish column's `-` for its
connection fee (`test_a_card_printed_for_both_regions_reads_on_the_walloon_side`).
The `values[...]` below index the region's column.

| TaxOverlay field | Source | Notes |
| --- | --- | --- |
| `federal_excise` | `values[2]` / 100 | mandatory both regions |
| `federal_excise_bands` | `_extract_excise_bands`, the `(**)` footnote's `0-3.000 kWh : 5,0329 c€/kWh, ...` tiers, read by the shared `excise_tier_bands` (`_parse.py`) | until July 2026 only; a footnote whose tiers all carry one rate (August 2026 on) is one rate and leaves it None, so the law's excise still applies; the first tier has to equal `values[2]` or the card raises |
| `energy_contribution` | `values[3]` / 100 | mandatory both regions |
| `region_connection_fee` | `values[4]` / 100 | where `Redevance de raccordement` is printed: a Walloon card, and a card for both regions, whose Flemish column prints `-` |
| `energy_fund_eur_per_month` | `_extract_energy_fund` BTR row | Flanders only, `values[1]` |
| `flanders_renewables` | `_extract_flanders_renewables` | Flanders only |
| `wallonia_renewables` | `_extract_wallonia_renewables` | Wallonia only |
| `vat_rate` | `0.0` (prices already VAT-incl) | `luminus.py` |

`_extract_per_kwh_taxes` raises on a short block (`< 4` values) or a missing
Walloon connection row, rather than silently zeroing a regulated tax and
underbilling (`_luminus_overlays.py`). Illustrative
(`test_taxes_split_correctly_per_region`): excise `0.050329`, contribution
`0.002042` (both regions); Wallonia green `0.0303` and connection `0.00075`;
Flanders green `0.0117` + cogen `0.0039` = `0.0156`.

The energy fund uses the BTR (Basse tension résidentiel) value, not BTNR
(non-residential) which is printed first; a `-` means no fee
(`_extract_energy_fund`, `_luminus_overlays.py`). In both fixture regions today BTR
is `-`, so `energy_fund_eur_per_month` is `0.0` (`test_luminus.py`).

Flanders renewables splits across green-energy + cogeneration
(`_extract_flanders_renewables`, `_luminus_overlays.py`): the primary regex sums
both `Coûts énergie verte` and `Coûts cogénération`; a fallback handles cards
that print only the green line. Both regional renewables helpers raise on a miss
(the caller has already gated on region, so a miss is layout drift not a fee-free
card). A card printed for both regions, every one until December 2025, puts the
Walloon figure after the Flemish pair (`Coûts énergie verte (c€/kWh)3`,
`Coûts cogénération (c€/kWh)3`, `FL 1,18 0,42`, `WAL 3,03 -`), which
`_extract_wallonia_renewables` reads past; reaching for `WAL` straight after the
green line left every Walloon card of 2024 and 2025 unread.

## Injection

`_extract_injection(text, kind)` (`_luminus_cards.py`) covers three of the
injection shapes in the project taxonomy (`docs/pricing-model.md`), selected by
contract kind and by the card's indexation sentence:

- **fixed / variable / tou -> month-indexed formula.** The extractor reads the
  applicable `Tarif de l'énergie injectée` row, `Compensation pour l'énergie
  injectée` on the cards until December 2025 (`_INJECTION_ROW_RE`), and stores its
  first figure as `InjectionRates.current` (`_luminus_cards.py`). Where the card
  also prints `Formule tarifaire de l'énergie injectée = <factor> x Belpex <sign>
  <base>` (`Formule tarifaire pour la compensation de l'énergie injectée` until
  December 2025) followed by `Votre tarif sera indexé tous les mois` (or `chaque mois`),
  `_INJECTION_INDEXED_RE` reads the formula: `factor = factor_pdf * 10.0`,
  `base = base_pdf_cents / 100.0`, no VAT (the block is HTVA at 0%), and
  `month_indexed = True`. The credit then settles on the delivery month's Belpex
  mean, and the printed `current`, computed on the previous month's value, is
  only the fallback while that mean is unpublished. Comfy, Comfy Plus, MaxxFix,
  MaxxFlex, BasicFix, BasicFlex and SmartFlex print it, which is why they set
  `spot_indexed_injection` (`_MONTHLY_INJECTION_CONTRACTS`, `luminus.py`).
- **ComfyFlex and ComfyFlex+ -> the same formula, indexed on the quarter.** They
  print it under `Votre tarif sera indexé tous les trimestres`, which the same
  regex (`_INJECTION_INDEXED_RE`) accepts. A feed-in leg has no quarter flag, so
  the leg is `month_indexed`: every month of a quarter settles on the quarter's
  index once the next quarter's card names it, and until then an entry with
  day-ahead prices is credited on the delivery month's mean, the value Luminus
  states it bills a period on whose quarter is not known at settlement, and one
  without on the printed figure. Read as printed before, every closed quarter was
  credited on the one before it, for good.
- **Per meter register.** The fixed and variable cards print one feed-in figure
  per register, single, then a bi-hourly meter's day and night, and the formula
  block one row each. Every card since July 2025 prints the three alike, and the
  leg is the one rate. The cards of January to June 2025 price them apart:
  `Jour (compteur de jour) = 0,0644 x Belpex - 1,5900; Jour (compteur de jour et
  de nuit)) = 0,0794 x Belpex - 1,5900; Nuit = 0,0414 x Belpex - 1,5900`, 6,71,
  8,64 and 3,75 c/kWh in March 2025. `_with_registers` then carries the day and
  night figures as the two registers' rates (`bi_hourly`, `peak`, `offpeak`) with
  their own formulas (`factor_peak` / `base_peak`, `factor_offpeak` /
  `base_offpeak`), the shape the social tariff's cards are read in, and a card
  pricing them apart whose block lacks a register's formula raises
  (`test_a_2025_card_credits_each_register_on_its_own_formula`). A contract signed
  on such a card keeps the three formulas (`_cohort_injection_from_archived`,
  `cohort_legs.py`), so a bi-hourly meter is credited each register on its own at
  the month's index, and without one at the figure the month's card prints, as
  the single register is; the live price, the year to date and the backfill agree
  (`test_a_bi_hourly_meter_signed_in_2025_is_credited_each_register`).
- **dynamic -> hourly `factor*spot + base`.** `_DYNAMIC_INJECTION_BLOCK_RE`
  (`_luminus_cards.py`) scopes the feed-in block, headed "pour la compensation
  de", "pour" or "de l'énergie injectée" as the cards went, and
  `_belpex_h_pair` reads its `<label> = <factor> x Belpex H <sign> <base>` rows
  as for the energy (`Injection (...)` since September 2025, one per register
  before). Residential injection is VAT-exempt, so **no VAT multiplier is
  applied** (`_luminus_cards.py`): `factor = factor_pdf * 10.0`, `base =
  base_pdf_cents / 100.0`. Contrast the consumption dynamic formula, which does
  scale by VAT. The first row's text is stored in `InjectionRates.formula`.

Illustrative: dynamic Wallonia injection `0,1019 x Belpex H - 1,2737` yields
`factor == 1.019`, `base == -0.012737` (negative base preserved,
`test_dynamic_extracts_injection_formula_with_negative_base`,
`test_luminus.py`). Non-dynamic indicative
(`test_injection_uses_applicable_rate_not_annual_estimate`): comfy Wallonia
`0.0381`, comfyflex Flanders `0.0396`. Month-indexed
(`test_monthly_cards_carry_the_injection_formula`): Comfy, MaxxFlex and
SmartFlex Wallonia print `0,0481 x Belpex - 0,6392`, so `factor == 0.481` and
`base == -0.006392`, which reproduces the printed `0.0381` at the card's index;
`test_quarterly_cards_carry_the_injection_formula` pins the two quarterly cards
to the same formula, month indexed.

Two anchoring subtleties in the indicative regex (`_luminus_cards.py`):

1. **Applicable vs annual estimate.** The card prints both the applicable
   `Tarif de l'énergie injectée` and an `Estimation annuelle du tarif de
   l'énergie injectée` 12-month forecast just below. They share the
   `de l'énergie injectée` tail, but only the applicable row capitalises
   `Tarif`, so the case-sensitive `Tarif` binds to the applicable rate
   (`_luminus_cards.py`). `test_injection_uses_applicable_rate_not_annual_estimate`
   (`test_luminus.py`) verifies this in both directions, including the
   May card where the estimate (`3.68`) is below the applicable rate (`3.81`),
   so picking the wrong row would under-credit.
2. **Footnote digit + mid-phrase wrap.** Some cards print a footnote digit right
   after the unit (`(c€/kWh)2 3,81`), so the regex skips an optional
   digit-then-whitespace; and the label can wrap mid-phrase
   (`Tarif de l'énergie \ninjectée`), so `\s+` is used between every word with
   `re.S` (`_luminus_cards.py`).

Fail-loud invariant: both Luminus card families always publish injection, so if
neither `current` nor `factor` parses, `_extract_injection` raises rather than
silently crediting nothing (`_luminus_cards.py`). `test_missing_injection_row_fails_loud`
(`test_luminus.py`) corrupts the `injectée` label and asserts the raise.

There is no supplier-side prosumer / PV forfait on Luminus cards
(`SupplierSnapshot.supplier_prosumer_eur_per_kva_year` is left `None`). The only
prosumer term is the DSO-side Wallonia `prosumer_eur_per_kva_year`.

## Yearly fees and exclusive-night circuit

`_extract_yearly_fee` (`_luminus_cards.py`) captures the
`Redevance fixe (€/an)` line and raises on a miss (a regex miss is layout drift,
not a fee-free contract; the comment notes dropping this would silently lose
~70 EUR/year from the user's annual estimate). Illustrative: ~65 EUR static,
~75 EUR dynamic.

`_extract_excl_night_fee` (`_luminus_cards.py`) reads the third column of the
`Redevance fixe` row on static/variable cards (`mono | bi | exclusif nuit`, e.g.
`65,00 65,00 -`). A `-` means the exclusive-night circuit carries no separate
abonnement, so it must bill `0`, not the standard fee (it is billed once on the
main connection). Returns `None` when there is no third column (dynamic cards
print a single value and offer no exclusive-night), so the standard fee applies.
`test_comfy_wallonia_fixed_rates_and_dso` (`test_luminus.py`) confirms
`yearly_fixed_fee_exclusive_night == 0.0` and that
`yearly_fixed_fee_for_meter(..., "exclusive_night")` returns `0.0`.

## Quirks and historical bugs (the land mines)

- **6% VAT-inclusive prices, ex-VAT dynamic formula.** Everything printed is 6%
  VAT-incl, but the Dynamic `Prélèvement` formula is `hors TVA`, so its factor
  and base are scaled by the parsed VAT multiplier (`_luminus_cards.py`).
  Injection is always VAT-exempt and is never scaled (`_luminus_cards.py`).
- **Region-specific dynamic base.** Flanders and Wallonia have different bases
  in the same formula; never merge regions into one snapshot
  (`test_luminus.py`).
- **Applicable-vs-estimate rows** on both the consumption (`Énergie fournie` vs
  `Estimation annuelle de l'énergie fournie`) and injection (`Tarif` vs
  `Estimation annuelle du tarif`) sides. Always take the current-month
  applicable row (`test_comfyflex_flanders_uses_current_monthly_not_annual_estimate`,
  `test_luminus.py`; injection at `test_luminus.py`).
- **SMR3 data-management footnote is stale**: the dynamic Flanders card's
  `quart d'heure` footnote still prints 2025's 18,56, while Fluvius bills every
  regime the table's figure since 2025, so the table is read
  (`_luminus_overlays.py`).
- **Two DSO column widths** per region (static wide, dynamic narrow); the row
  is asked for at each width in turn, with prosumer present only on static
  (`_luminus_overlays.py`).
- **Wallonia Impact triplet order follows the headings**: ECO/MEDIUM/PIC up to
  September 2026, PIC/MEDIUM/ECO from October (`_luminus_overlays.py`).
- **Label-to-key remaps**: Fluvius Kempen -> IVEKA, Fluvius Midden-Vlaanderen ->
  INTERGEM, in the shared `FLUVIUS_CARD_LABELS` (`const.py`, aliased at
  `_luminus_overlays.py`).
- **Trailing-period token hazard** in the dynamic formula, guarded by the
  digit-anchored `_NUM` (`_luminus_overlays.py`).
- **Padded publication parens** on the May 2026 cards (`(mai 2026 )`),
  tolerated by optional whitespace (`luminus.py`).
- **Numeric-token double-occurrence in the TOU row**: the SmartFlex row is
  read at four figures (three bands plus Happy Sunday, October 2026 on) or
  three (earlier cards), and only above "Informations sur le prix
  transitoire": the same label returns there with the MaxxFlex mono / jour /
  nuit rates, which a four-figure October row once let the three-figure
  search reach (`_luminus_cards.py`).
- **Fail-loud policy**: yearly fee, injection, per-kWh taxes, and both regional
  renewables all raise on a miss rather than defaulting to 0 and silently
  mispricing (`_luminus_cards.py`, `_luminus_overlays.py`).

## Test fixtures

Under `tests/fixtures/`, exercised by `tests/test_luminus.py`:

| Fixture | Variant | Used by |
| --- | --- | --- |
| `luminus_comfy_w.pdf` | Comfy (fixed), Wallonia, April 2026 | `_comfy_w`, fixed rates + DSO + injection tests |
| `luminus_comfy_w_may.pdf` | Comfy (fixed), Wallonia, May 2026 | padded-parens + estimate-vs-applicable injection tests |
| `luminus_comfyflex_v.pdf` | ComfyFlex (variable), Flanders, April 2026 | current-month energy, Flanders DSO/injection |
| `luminus_comfyflex_plus_w.pdf` | ComfyFlex+ (variable), Wallonia | drop-in variable parse path |
| `luminus_maxxflex_w.pdf` | MaxxFlex (variable), Wallonia | variable parse path |
| `luminus_smartflex_w.pdf` | SmartFlex (tou), Wallonia | TOU three-band + seasonal weekend rule |
| `luminus_dynamic_w.pdf` | Dynamic, Wallonia | consumption + injection formula, taxes |
| `luminus_dynamic_v.pdf` | Dynamic, Flanders | region-specific base, narrow SMR3 DSO table |
| `luminus_dynamic_w_oct.pdf` | Dynamic, Wallonia, October 2026 | Impact columns printed PIC first |
| `luminus_dynamic_v_2024-06.pdf` | Dynamic, Flanders, June 2024 | "Jour (compteur de jour)" rows, quarter-hour data fee footnote |
| `luminus_dynamic_v_2025-06.pdf` | Dynamic, Flanders, June 2025 | formulas one row per register, "Jour (compteur de jour)" |
| `luminus_dynamic_v_2025-08.pdf` | Dynamic, Flanders, August 2025 | formulas one row per register, "Compteur mono-horaire" |
| `luminus_smartflex_w_oct.pdf` | SmartFlex (tou), Wallonia, October 2026 | four-figure row with Happy Sunday |
| `luminus_smartflex_v_oct.pdf` | SmartFlex (tou), Flanders, October 2026 | four-figure row with Happy Sunday |
| `luminus_comfyflex_w_oct.pdf` | ComfyFlex (variable), Wallonia, October 2026 | campaign gated by the product name |
| `luminus_maxxflex_w_oct.pdf` | MaxxFlex (variable), Wallonia, October 2026 | cashback valued at the annual estimate |
| `luminus_comfyflex_plus_w_oct.pdf` | ComfyFlex+ (variable), Wallonia, October 2026 | cashback valued at the annual estimate |
| `luminus_maxxfix_w_oct.pdf` | MaxxFix (fixed), Wallonia, October 2026 | cashback valued at the printed rate |
| `luminus_basicflex_w_oct.pdf` | BasicFlex (variable), Wallonia, October 2026 | formulas on the delivery month's Belpex RLP M |
| `luminus_maxxfix_v_oct.pdf` | MaxxFix (fixed), Flanders, October 2026 | today's card beside a 2025 signing card; the feed-in registers alike |
| `luminus_maxxfix_v_2025-03.pdf` | MaxxFix (fixed), March 2025, one card for both regions | "Compensation pour l'énergie injectée" row, registers priced apart, a signing month of 2025 |
| `luminus_maxxflex_v_2025-03.pdf` | MaxxFlex (variable), March 2025, one card for both regions | energy formulas named after the meter, "Jour (compteur de jour)" |
| `luminus_comfyflex_w_2025-06.pdf` | ComfyFlex (variable), June 2025, one card for both regions | the Walloon network rows the June 2025 Dynamic card borrows |

Note: fixtures for `luminus_comfy_plus` and `luminus_basicfix` are not present;
those contracts share the fixed / variable parse paths already covered by the
fixtures above.

## When the card changes, look here

| Symptom | First place to look | Why |
| --- | --- | --- |
| Every field misses / fetch fails | `fetch` + `fetch_pdf_text` (`luminus.py`, `_pdf.py`) | URL construction, slug/tabValue, PDF magic-byte validation |
| Energy rates wrong / missing | `_extract_energy` (`_luminus_cards.py`) | four-column vs three-column row, unit /100, TOU lookahead |
| Dynamic factor/base off by ~1.06 or ~10 | dynamic branch (`_luminus_cards.py`) | VAT multiplier + mWh->kWh + c->EUR conversion |
| Injection wrong or raising | `_extract_injection` (`_luminus_cards.py`) | applicable-vs-estimate `Tarif` capitalisation, VAT-exempt scaling |
| A DSO row missing | `_FLANDERS_LABELS` / `_WALLONIA_LABELS` + row regexes (`_luminus_overlays.py`) | printed label renamed, or column count changed |
| Dynamic data-management fee wrong (Flanders) | table column (`_extract_flanders_dsos`, `_luminus_overlays.py`) | four-column row width; Fluvius splitting the regimes again |
| Tax value zeroed / block too short | `_tax_block_values` + `_extract_per_kwh_taxes` (`_luminus_overlays.py`) | colon anchor, value-run boundary, BTNR/BTR ordering |
| Yearly / exclusive-night fee wrong | `_extract_yearly_fee` / `_extract_excl_night_fee` (`_luminus_cards.py`) | `Redevance fixe` line format, third-column `-` handling |
| Publication label empty | `_extract_publication_month` (`luminus.py`) | parens padding / month spelling |
| A new product appears / a slug 404s | `_CONTRACTS` + `discover` (`luminus.py`) | add a `_ContractDef`; sitemap slug directory |

When the layout drifts, refresh the affected fixture PDF under `tests/fixtures/`
and re-run `pytest tests/test_luminus.py`; the test assertions encode the
expected numeric output and will pinpoint which helper regressed.

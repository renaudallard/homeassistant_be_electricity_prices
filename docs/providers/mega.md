# Provider: mega

This document covers the Mega Belgium extractor (`providers/mega.py`). Mega is a
multi-region residential supplier with the widest product line in the registry:
fixed, variable ("Flex"), dynamic, and a Wallonia-only Tarif Impact contract, all
published as monthly per-region PDF cards discovered through a public listing page.
Read this before you touch the code the day Mega rotates its card layout, renames a
product, or ships a new tariff. The test module `tests/test_mega.py` encodes the
expected parse output against real April 2026 fixtures and is the ground truth for
what each extractor function must produce.

Related reading:

- [../provider-framework.md](../provider-framework.md): the `SupplierExtractor`
  protocol, the dataclasses (`SupplierSnapshot`, `EnergyRates`, `DsoOverlay`,
  `TaxOverlay`, `InjectionRates`), the registry, and the shared `_pdf` helpers.
- [../pricing-model.md](../pricing-model.md): how `compute_breakdown` turns a
  snapshot plus a DSO sub-area into the all-in price and the injection credit,
  including the prosumer forfait summation and the CWaPE Impact band routing.

## Overview

| Property | Value |
| --- | --- |
| Registry id | `mega` (`_mega_cards.py:255`) |
| Label | `Mega` (`_mega_cards.py:256`) |
| Regions served | Flanders, Wallonia, Brussels (union across contracts, `base.py:560`) |
| Publication | Monthly per-region PDF cards, one file per (product, region) |
| Discovery | Scrape the public listing page, regex the `data-product-element` anchor to its PDF URL |
| Probe | Listing GET + filename match (the URL month rotates monthly) |
| Archive | Month-addressable via URL rewriting of both month placeholders |

Mega publishes each monthly card under a predictable CDN filename
(`mega.py:28`):

```
https://my.mega.be/resources/tarif/Mega-FR-EL-B2C-<REGION>-<MMYYYY>-<SUFFIX>.pdf
```

`<REGION>` is one of `VL` / `WL` / `BX` (`_REGION_TO_CODE`, `mega.py:125`). The
`<MMYYYY>` segment rolls every month, and the product `<SUFFIX>` (for example
`Smart0104`, `Smart2204-Fixed`, `Cap0104`) carries an internal launch-date code that
drifts whenever Mega launches a product variant. Because the suffix is unstable, the
extractor never constructs the URL from a hardcoded suffix. Instead it scrapes the
public listing page `https://www.mega.be/fr/energie/cartes-tarifaires` (`_LISTING_URL`,
`mega.py:116`), where every product card exposes an
`<a data-product-element="<Product Name>" ... href="<PDF URL>">` anchor, and matches
the anchor to its PDF with a regex (`_find_pdf_url`, `mega.py:316`).

The `source_url` recorded on the snapshot is the resolved PDF URL for the live
`fetch` (`mega.py:448`); the pure `parse_snapshot` defaults it to `_LISTING_URL`
when called without one (`mega.py:440`).

## Contracts

Eleven residential electricity products are registered, plus nine professional
editions (`_CONTRACTS`, `mega.py:172`; the test pins
`len(contract_ids) == 20` at `test_mega.py:70`). Zen Fixed's residential edition
was retired in August 2026 (its professional one survives). Off-peak Fixed was
retired in July 2026 and **came back for the August 2026 card**, in all three
regions and with a B2B edition it had not had before -- the catalog check
flagged it the day it reappeared (issue #57).

| id | label | TariffKind | Regions | Notes |
| --- | --- | --- | --- | --- |
| `mega_smart_fixed` | Mega Smart Fixed (2 years) | `fixed` | all three | flagship fixed product |
| `mega_smart_flex` | Mega Smart Flex (2 years) | `variable` | all three | monthly-indexed "Flex" |
| `mega_online_fixed` | Mega Online Fixed | `fixed` | all three | |
| `mega_online_flex` | Mega Online Flex | `variable` | all three | |
| `mega_cosy_fixed` | Mega Cosy Fixed | `fixed` | all three | publishes on a non-1st day |
| `mega_cosy_flex` | Mega Cosy Flex | `variable` | all three | |
| `mega_offpeak_flex` | Mega Off-peak Flex | `variable` | all three | |
| `mega_offpeak_impact_var` | Mega Off-peak Impact | `tou_impact` | Wallonia only | CWaPE Tarif réseau IMPACT + SMR3 meter |
| `mega_dynamic` | Mega Dynamic | `dynamic` | all three | hourly-billed (see below) |
| `mega_cap` | Mega Cap | `variable` | all three | product name `Mega Cap`, suffix `Cap0104` |

Notes on the enumeration:

- Off-peak Impact is Wallonia-only (`regions=frozenset({REGION_WALLONIA})`,
  `mega.py:187`) because it needs the CWaPE Tarif réseau IMPACT plus an SMR3 smart
  meter, both Wallonia-specific. The test `test_offpeak_impact_contract_is_wallonia_only`
  (`test_mega.py:455`) enforces this. Every other product declares the default
  `_MEGA_ALL_REGIONS` (`mega.py:142`).
- Off-peak Fixed was discontinued in July 2026 and revived for the August 2026
  card. This is exactly what `discover()` exists for: the daily catalog diff
  flagged the returned `data-product-element` and the product went back into the
  registry. It parses on the existing fixed path with no parser change -- it is
  an ordinary bi-hourly fixed card (Wallonia August 2026: single 0,1932, peak
  0,2350, off-peak 0,1610, fee 74,20 EUR/yr).
- The Tarif Social variant is deliberately omitted, same reasoning as Engie and
  Luminus: it is a regulated CREG tariff, auto-assigned, with no DSO breakdown
  (`mega.py:52`).
- `mega_dynamic` is hourly-billed. `DynamicRates.quarter_hourly` defaults to `False`
  (`base.py:159`) and Mega leaves it unset, so the coordinator aggregates the
  ENTSO-E 15-minute curve to clock hours for this contract, unlike Engie / Cociter /
  EBEM / Ecofix / OCTA+ / Ecopower which bill per quarter-hour.
- No contract sets `spot_indexed_injection`; Mega's injection is either a printed
  monthly indicative or (dynamic only) the HTVA formula, never a spot-only-variable
  shape.

The catalog also carries `Prepaid Fixed` / `Prepaid Flex`, which are topup-card
products with a different billing model (no monthly invoice, no recorder-backed
consumption sensors), out of scope for the Energy-dashboard integration
(`_KNOWN_UNSUPPORTED_PRODUCTS`, `mega.py:305`).

### The professional editions

Mega publishes a B2B card for eight of its products, to the same CDN, but never
links them from the public listing: the `data-product-element` anchors carry
only `Mega-FR-EL-B2C-` hrefs. There is nothing to scrape, so the pro lane builds
the URL instead (`_pro_pdf_url`, `mega.py:400`):

```
https://my.mega.be/resources/tarif/Mega-FR-EL-B2B-<REGION>-<MMYYYY>-<Family>01<MM>[-<Variant>].pdf
```

`01<MM>` is the card's validity start, always the first of its month, so a
contract only needs its family token (`Smart`, `Cosy`, `Dynamic`, ...) and the
`-Fixed` variant suffix. A month Mega has not published resolves to the CDN's
HTML stub, which `fetch_pdf_text` rejects, so a wrong guess fails loud; `fetch`
then falls back to the previous month, which covers the day or two of lag around
a month boundary.

Consequences of having no listing:

- **No probe.** The built URL only changes at a month boundary, so returning it
  as a freshness key would pin the snapshot for a whole month and swallow a
  mid-month re-publish. `probe` returns `None` for a pro contract and the
  24-hour TTL takes over.
- **`discover()` cannot see them**, so a new professional product will not
  surface in the daily catalog diff. The residential listing remains the only
  discovery signal.

Online Flex, Off-peak Flex and Off-peak Impact have no B2B card. Off-peak Fixed
gained one when it returned in August 2026 (`Offpeak-Bi01<MM>-Fix`), and Zen
Fixed has one even though Mega retired the residential edition that month.

Card differences, all handled in `parse_snapshot` on the contract's
`professional` flag:

| | residential | professional |
| --- | --- | --- |
| Header | `Client résidentiel - <Region>` | `Client professionnel - <Region>` |
| VAT basis | TVAC, `vat_rate=0.0` | HTVA, `vat_rate=0.21` |
| Federal excise | one `Accise spéciale` rate since August 2026 | three tranches (0-20.000 / 20.000-50.000 / 50.000-1.000.000 kWh) into `federal_excise_bands` |
| Energy contribution | folded into the excise, row gone | still printed, one column per tranche |
| Injection | not taxed | fixed and smart cards say *"les prix d'injection sont à majorer de la TVA"*; the DYNAMIC card says *"exemptés de TVA"* like its residential twin |

The region header check accepts either wording but still pins the region, since
a wrong-region card mis-prices silently.

`vat_applies` is read off the card's own sentence rather than off the edition
(`_injection_vat_applies`, `providers/mega.py:739`), because the two sentences
do not split the way the editions do: the professional dynamic card is exempt.
Keying on the edition grossed that one card's feed-in credit by 21%. A card
printing neither sentence falls back to the edition, which is what every card
did before.

## Fetch strategy

### Live fetch

`fetch(session, contract_id, region)` (`mega.py:434`):

1. Validate the contract id and resolve the region to its `VL` / `WL` / `BX` code.
2. GET the listing HTML (`_fetch_listing_html`, `mega.py:372`).
3. Resolve the current PDF URL with `_resolve_pdf_url` (`mega.py:334`). It first
   regexes the listing with `_find_pdf_url` (`mega.py:316`), which pins the pattern
   to `data-product-element="<Product Name>"` followed by an `href` matching
   `Mega-FR-EL-B2C-<REGION>-\d{6}-...\.pdf`. Pinning to `Mega-FR-EL-B2C-<REGION>-` is
   what stops the gas links (`Mega-FR-NG-...`) and the other-region links from
   matching (`mega.py:221`). When that regional block is missing but the card is
   still published (Dynamic Wallonia, July 2026, #42), the resolver rewrites a
   surviving sibling region's URL by swapping the `-B2C-<REGION>-` code
   (`_REGION_SEGMENT_RE`, `mega.py:146`). The test
   `test_listing_url_finder_picks_electricity_for_region` (`test_mega.py:75`) confirms
   a Smart Fixed / WL match ends `Smart2204-Fixed.pdf` and never contains `NG`;
   `test_resolver_falls_back_to_sibling_region_when_block_missing` (`test_mega.py:101`)
   covers the fallback.
4. Download the PDF text (`fetch_pdf_text`, `_pdf.py:224`) and hand it to
   `parse_snapshot`, which asserts the card's own `Client résidentiel - <Region>`
   header matches the requested region (`_assert_card_region`, `mega.py:712`) so a
   wrong sibling guess fails loud rather than mis-pricing a region's overlays.

`fetch` raises `ExtractorError` on an unknown contract, an unknown region, or a
listing that has no entry for the (product, region) pair in any region
(`mega.py:336`).

### Probe

`probe(session, contract_id, region)` returns the resolved PDF URL for the pair
(`mega.py:297`). Mega's listing has neither `Last-Modified` nor `ETag`, so the
cheapest reliable freshness key is the listing GET plus filename match: the URL
contains the publication month (`MMYYYY`), so the probe value changes whenever Mega
rotates the card. The coordinator only re-runs `fetch` when this key changes. The
probe returns `None` (falling back to the TTL) for an unknown contract or region, or
when the listing fetch fails.

### Historical fetch (archive)

`fetch_for_month(session, contract_id, region, year_month)` (`mega.py:470`) uses
the fact that Mega's CDN keeps every monthly issue under a stable URL. The month
appears twice in the filename: the `<MMYYYY>` segment and the `<MM>` half of the
product's effective-date `<DD><MM>` suffix. Both must rotate to the requested month
while the effective day `<DD>` is preserved (most products publish on the 1st; Cosy,
for example, uses another day). The rewrite:

1. Resolve the current URL from the listing (never guess a suffix).
2. Match the `-MMYYYY-` segment (`mmyyyy_re`, `mega.py:515`) and capture the current
   month, then substitute the requested `MMYYYY` (year untouched).
3. In the filename tail after the `-MMYYYY-` segment, rewrite the two-digit
   effective-date month while preserving the day (`Cap0106 -> Cap0105`,
   `Online0106-Fixed -> Online0105-Fixed`, `Cosy1306 -> Cosy1305`) (`mega.py:396`).
   The rewrite cannot anchor on `.pdf` because the suffix can sit mid-token before a
   `-Fixed` / `-Green` / `-Fix` variant.
4. Download, parse, then cross-check the parsed card against the requested month
   with `archive_validity_check` (`_pdf.py:908`), passing `_FR_MONTH_NAMES`
   (`mega.py:419`). If validity or the month text does not match, return `None` so
   the YTD walk falls back to the proxy snapshot rather than mis-billing.

`fetch_for_month` returns `None` when the URL 404s, when the CDN serves its HTML
stub for a non-archived effective day (the PDF magic-byte check in
`_is_pdf_payload`, `_pdf.py:137`, rejects it), when the parse fails, or when the
requested month falls outside the archive. A product whose publication day varies
month to month resolves to the HTML stub and correctly falls back to the proxy
(`mega.py:389`).

The test `test_fetch_for_month_rewrites_effective_date_month_preserving_day`
(`test_mega.py:117`) is the canonical regression: Smart Fixed publishes on day 22 with
the suffix mid-token (`Smart2204-Fixed`), exactly the case the old `01<MM>.pdf$`
rewrite missed. Requesting March 2026 must yield a URL ending
`-032026-Smart2203-Fixed.pdf` (both months rotate, day 22 stays, year untouched).

A **professional** contract skips all of that: the B2B cards are absent from the
listing, so `fetch_for_month` builds the filename with `_pro_pdf_url` for the
requested month exactly as `fetch` does (`mega.py:448`). Routing them through the
listing matched the residential card of the same `product_name` (`Smart Fixed` is
shared by `mega_smart_fixed` and `mega_pro_smart_fixed`) and billed a B2B contract
at residential rates on every archived month. Unlike `fetch`, the archive branch has
**no previous-month retry**: a month Mega never published must return `None` so the
caller falls back to the current-card proxy rather than billing the neighbouring
month's card. `test_fetch_for_month_builds_the_b2b_url_for_a_professional_contract`
(`test_mega.py:154`) pins the `-B2B-` segment, the variant suffix and the single
fetch attempt.

### Catalog discovery

`discover(session)` (`mega.py:276`) returns every `data-product-element` value on
the listing minus `_KNOWN_UNSUPPORTED_PRODUCTS`. It is best-effort catalog signal
for the daily live-check: diffing against `{c.product_name for c in _CONTRACTS}`
flags any new Mega product to add to the registry. Filtering out the prepaid
products keeps them from re-opening the same catalog issue every day (regression
2026-05-05, `test_discover_filters_known_unsupported_products`, `test_mega.py:671`).
On a listing fetch failure it returns an empty set rather than raising.

## Parsing

`parse_snapshot(contract_id, text, region, source_url)` (`mega.py:439`) is the pure,
unit-tested parser. It first asserts the card is the requested region's edition
(`_assert_card_region`, `mega.py:712`), anchoring on the `Client résidentiel -
<Region>` header rather than a bare region name (every card names all three regions
in its cross-region Cotisation Verte table); this backstops the sibling-region URL
fallback so a wrong guess raises instead of applying the wrong region's overlays.
It then dispatches on the contract's `TariffKind` and the configured region, then
assembles a `SupplierSnapshot`. All rates are printed in c€/kWh on the card and
divided by 100 to reach EUR/kWh. The general pypdf hazard on these cards is
that labels and their values land on separate lines, so nearly every parser anchors
on a label token and takes the first number on the following line.

Fields pulled and their helpers:

| Field | Helper | Location |
| --- | --- | --- |
| Energy rates (per kind) | `_extract_energy` | `_mega_cards.py:236` |
| Injection | `_extract_injection` | `_mega_cards.py:528` |
| Publication label | `_extract_publication_month` | `_mega_cards.py:489` |
| Valid until | `parse_valid_until` then `_extract_valid_until` | `_pdf.py:947`, `_mega_cards.py:508` |
| Federal excise | `_extract_federal_excise` | `_mega_overlays.py:149` |
| Energy contribution | `_extract_energy_contribution` | `_mega_overlays.py:180` |
| Wallonia connection fee | `_extract_connection_fee` (Wallonia only) | `_mega_overlays.py:198` |
| Regional renewables | `_extract_flanders_renewables` / `_extract_renewables` | `_mega_overlays.py:229`, `_mega_overlays.py:229` |
| DSO overlay | `_extract_flanders_dsos` / `_extract_wallonia_dsos` / `_extract_brussels_dsos` | `_mega_overlays.py:381`, `_mega_overlays.py:381`, `_mega_overlays.py:381` |
| Supplier PV forfait | `_extract_supplier_prosumer` | `_mega_overlays.py:99` |

### Energy block

`_extract_energy` (`_mega_cards.py:236`) always reads the yearly standing charge first
(`_extract_yearly_fee`, `_mega_cards.py:474`), which accepts both the split dynamic layout
(`Redevance fixe\n(€/an)\n42.4`) and the joined fixed layout
(`Redevance fixe (€/an)\n111.3`). A missing standing charge raises (it is on every
card); `test_missing_yearly_fee_is_fatal` (`test_mega.py:411`) enforces it.

- `dynamic`: parse the consumption formula (see next section) into `DynamicRates`.
- `tou_impact`: parse three CWaPE bands (`_extract_impact_tier` for `PIC`,
  `MEDIUM`, `ECO`, `mega.py:655`) plus the footnote formula text into `ImpactRates`.
  The regex is permissive on the `Tarif` prefix because circulating cards print a
  bare `PIC` on the last row and lowercase `tarif` in the footnote.
  `_impact_band_coefficients` (`_mega_cards.py:201`) also parses the footnote's three
  per-band formulas (`Epex * 0,8528 + 0,95 c€/kWh` and friends) into numeric
  coefficients, converted the same way the variable card's are: x 1,06 and /100 on a
  residential card, left ex-VAT on a professional one. Inverting the three bands on
  that basis lands on one index (8,466 c€/kWh), which is what proves the conversion;
  the ex-VAT reading does not agree across bands. PIC's printed rate is one unit in
  the last decimal above what its own formula gives, because Mega rounds each band
  separately -- `test_offpeak_impact_coefficients_agree_with_the_printed_rates` pins
  that rather than hiding it.
- `fixed` / `variable`: read `Compteur mono-horaire` (mono), plus `Tarif jour`
  (peak), `Tarif nuit` (offpeak) and `Exclusif nuit` (exclusive night) via
  `_extract_meter_value` (`_mega_cards.py:449`). The bi-hourly labels are only read inside
  the `Compteur bi-horaire` scope so a later mention in a dynamic-formula footnote
  cannot shadow the energy-block value; the anchor regex tolerates the newline pypdf
  inserts inside `Compteur bi-horaire`. A missing mono rate raises. `fixed` builds
  `FixedRates`, everything else builds `VariableRates`.

> [!IMPORTANT]
> **On a `variable` or `tou_impact` card, that table is not the billed rate.** The
> card states it outright: *"Les prix affiches dans le tableau ci-dessus et utilises
> pour realiser une simulation tarifaire sont calcules sur base d'une prevision des
> prix de l'energie pour une livraison les 12 prochains mois."* The rates Mega
> settles on are in the sentence below it, *"Les derniers prix constates et utilises
> pour le calcul de votre facture de regularisation pour le mois de &lt;month&gt;"*,
> in c€/kWh. `_realized_rates` (`_mega_cards.py:354`) parses that sentence and both
> `_extract_energy` and `_extract_injection` prefer it, falling back to the table
> when it is absent.
>
> Two label sets share one parser: `Compteur mono-horaire / Jour / Nuit / Exclusif
> nuit` on a variable card, `tarif ECO / MEDIUM / PIC` on an Impact one, each
> followed by `Injection`. The text layer wraps mid-word (`Compteur mono-\nhoraire`)
> so the soft hyphen is stripped first, `Nuit` must not match inside `Exclusif
> nuit`, and the number pattern must not swallow the sentence's closing period
> (`Injection : 2.32.`). It must also accept a leading minus: every May 2026 card
> prints `Injection : -0.32`, a month the customer pays to inject. Matching digits
> only dropped the key, so `_extract_injection` fell back to the simulation table
> and credited +2,42 c€/kWh against a billed -0,32 — the wrong sign, about
> **82 EUR** over 3000 kWh injected.
>
> Reading the table billed the April 2026 Walloon Smart Flex card at 17,42 c€/kWh
> where Mega settles 15,30 — about **74 EUR/yr** at 3500 kWh — and credited
> injection at 3,84 where it pays 2,32. `fixed` and `dynamic` cards carry no such
> disclaimer and are left alone.
>
> **The sentence names the month BEFORE the card's own**, so on the ARCHIVE path
> it has to be read off the *next* month's card. The June card's sentence says
> "pour le mois de mai"; the figures that bill June are on the July card. On the
> live path that lag is unavoidable — the current month's index does not exist
> yet — but `fetch_for_month` was taking it at face value and billing every past
> month of the year-to-date at the month before it. Measured on four consecutive
> Walloon Cap cards: May was billed 12,67 c€/kWh where Mega settled 14,24, June
> 14,24 where it settled 16,95.
>
> `_apply_realized_for_month` fetches the M+1 card and splices in only its energy
> and injection legs; the DSO and tax overlays, the yearly fee and the cohort
> coefficients stay the delivery month's, because those really are properties of
> its own card. The M+1 card goes through the same `archive_validity_check` as
> the main path, so a CDN stub cannot shift the rates by a further month. When
> that card is not out yet the mapping comes back empty and the month keeps its
> own figures — the newest month therefore behaves exactly as before.
>
> Two consequences worth knowing. It costs **one extra archive fetch per month**
> for a variable or Impact contract, on a walk that already caches one snapshot
> per month. And the most recently completed month's source card is the CURRENT
> one, so `_archive_pdf_url` takes `allow_current=True` there; `fetch_for_month`
> itself still refuses to serve the current card as a historical month.

The Wallonia Smart Fixed fixture pins mono / peak / offpeak / exclusive-night to
0.1712 / 0.1938 / 0.1549 / 0.1549 EUR/kWh with a 111.3 EUR/yr fee (illustrative,
`test_mega.py:196`).

### Dynamic formula

Mega prints two distinct formulas in every Dynamic PDF (`mega.py:531`):

- Consumption: `formule tarifaire suivante : Day Ahead Epex Spot * 1.05 + 1.35 c€/kWh`
  (TVAC, spot already in c€/kWh, result in c€/kWh).
- Injection: `formule suivante (HTVA) : Day Ahead EPEX SPOT Belgium * 1 - 4 c€/kWh`
  (HTVA, but residential injection is VAT-exempt so the HTVA value is already what
  the user receives).

Each is matched by its own label-anchored regex (`_CONSUMPTION_FORMULA_RE`
`mega.py:544`, `_INJECTION_FORMULA_RE` `_mega_cards.py:79`) sharing `_FORMULA_TAIL`
(`mega.py:536`). This is critical because Mega prints the injection formula BEFORE
the consumption formula, so a naive "first / second formula" policy swaps them; the
test `test_dynamic_consumption_and_injection_are_not_swapped` (`test_mega.py:226`)
guards against exactly that. `_parse_formula` (`_mega_cards.py:84`) converts factor and
signed base-cents to EUR via `to_float` and `parse_sign`. The sign parser accepts any
Unicode dash, which matters because the injection base uses an en-dash, not an ASCII
hyphen (`test_dynamic_injection_uses_separate_htva_formula_with_endash`,
`test_mega.py:164`). The tail regex accepts dot or comma decimals so a re-render of
`* 1,05 + 1,35` as `* 1.05 + 1.35` does not dead-end the snapshot.

Because the consumption formula is already TVAC with the spot in c€/kWh, the parsed
`factor` maps EUR/kWh-spot to EUR/kWh-energy directly (no VAT multiplier); only the
base cents are converted to EUR (`mega.py:596`). Illustrative: factor 1.05, base
0.0135 EUR, fee 42.4 EUR/yr (`test_mega.py:151`); injection factor 1.0, base -0.04
EUR (`test_mega.py:164`).

### DSO overlays

The region dispatch in `parse_snapshot` (`mega.py:761`) selects exactly one DSO
parser and one renewables levy per snapshot.

Flanders (`_extract_flanders_dsos`, `_mega_overlays.py:250`, `_FLANDERS_LABELS` `_mega_overlays.py:247`)
maps the eight Fluvius sub-areas. Note the label-to-key gotchas: `Fluvius Kempen`
maps to `DSO_FLUVIUS_IVEKA` and `Fluvius Midden-Vlaanderen` to
`DSO_FLUVIUS_INTERGEM`. Static cards print 6 numbers per row (digital + classic
bundles); dynamic cards print only the 2 digital-meter numbers and surface the
`Tarif de gestion des données` fee in a separate `18.92 €/an` line outside the table.
The third digital column (exclusive-night distribution, lower than normal) is
captured optionally and only set when present, so dynamic cards leave
`distribution_exclusive_night` as `None`. Distribution rates already include
transport (`incluant déjà les coûts de transport`), so `transport` is set to 0.0,
same convention as Engie / Luminus Flanders. Compensation-regime Flanders cards also
carry a Fluvius `Tarif Prosumer` (EUR/kW/an) table, scoped to its own block to avoid
picking up a distribution rate; dynamic cards omit it.

Wallonia (`_extract_wallonia_dsos`, `_mega_overlays.py:323`, `_WALLONIA_LABELS` `_mega_overlays.py:314`)
maps AIEG, AIESH, ORES (Brabant wallon), RESA, and Régie de Wavre (`DSO_REW`). Each
row is a 9-number vertical block: mono, jour, nuit, excl_nuit, terme_fixe (€/an),
PIC, MEDIUM, ECO, transport. The Impact triplet (`distribution_pic` / `_medium` /
`_eco`) is always populated here because every Wallonia card prints the three CWaPE
bands. Prosumer rates come from a separate small `Tarif Prosumer (€/kW/an)` table
further down and are cross-referenced onto each overlay
(`test_wallonia_dso_carries_prosumer_rate_from_separate_table`, `test_mega.py:286`).

Brussels (`_extract_brussels_dsos`, `_mega_overlays.py:381`) maps the single Sibelga row, an
8-number block: mono, jour, nuit, excl_nuit, transport, mesure_comptage (€/an),
terme_fixe <=13kVA (€/an), terme_fixe >13kVA (€/an). Brussels has no capacity charge
(capacity is Flanders-only), so both flat annual euros (the metering fee and the
Sibelga <=13kVA fixed term) are folded into `data_management_per_year`; the >13kVA
term (group 8) is not billed here. The Brugel OSP annual fee table is parsed by the
shared `parse_brussels_osp` (`_pdf.py:690`) and keyed by connection-power tier. The
test `test_smart_fixed_brussels_extracts_sibelga_row` (`test_mega.py:258`) pins the
folded fee to 14.73 + 50.0744 and the OSP tiers to
`{le1_44: 0.0, le6: 13.36, le9_6: 21.37, le13: 26.71}` (illustrative).

### Publication label and validity

`_extract_publication_month` (`_mega_cards.py:489`) first tries the versioned Smart Fixed
prefix `V<n> <month> <year>` (the token class includes `é` and `û` so `août` keeps
its version, `test_publication_month_keeps_version_for_august`, `test_mega.py:404`),
then falls back to `Prix du mois MM/YYYY` rendered as `<month-name> YYYY` from
`_FR_MONTH_NAMES` (`_mega_cards.py:486`). `valid_until` prefers the shared
`parse_valid_until` keyword scan and falls back to `_extract_valid_until`
(`mega.py:727`), which reads `mois MM/YYYY` and returns the last calendar day of that
month (Mega cards are valid for the printed month).

## Taxes

`TaxOverlay` (`mega.py:781`) is assembled from:

- Federal excise: the flat `Accise speciale (c€/kWh)` value when the card prints
  one, else the first tier `Consommation entre 0 et 3000 kWh`
  (`_extract_federal_excise`, `_mega_overlays.py:149`), uniform across regions. On
  2026-08-01 the federal scheme folded the energy contribution into the special
  excise and flattened it, so the August card dropped the tier table; Mega renders
  the flat value with a DOT decimal (`4.876`) where the tiered rows used commas.
  Still mandatory: a miss on BOTH shapes raises, because dropping it would silently
  undercount the bill by roughly 5 c€/kWh (about 50 EUR/year at 1000 kWh,
  `mega.py:783`, illustrative).
- Energy contribution: the next number on the same `0 et 3000 kWh` row
  (`_extract_energy_contribution`, `_mega_overlays.py:180`). No longer mandatory: the levy
  was abolished on 2026-08-01 and the row went with the tier table, so an absent
  row returns 0 rather than raising.
- Regional renewables: exactly one of `flanders_renewables` (a single
  `Cotisation Verte` line that already folds in cogeneration, so no separate
  cogénération row appears, `_mega_overlays.py:111`), `wallonia_renewables`, or
  `brussels_renewables`, per region. Each raises on a miss in its own region.
- `region_connection_fee`: the Wallonia raccordement
  (`Redevance de raccordement`, `_mega_overlays.py:99`), 0.0 outside Wallonia. Mandatory in
  Wallonia, raises on a miss.
- `energy_fund_eur_per_month`: the Flemish `Fonds Energie` block
  (`_extract_energy_fund`, `mega.py:797`), 0.0 outside Flanders, where the cards
  carry no such row. Inside it a **professional** contract bills `Montant de base`
  (10.07 EUR/month on the August 2026 card) and a residential one bills `Montant
  réduit (résidentiel avec domicile)` (0.00); the professional card omits the
  reduced row entirely. A residential contract never falls back to the base
  amount on a missing reduced row, so a household is never billed the business
  rate. This was hardcoded 0.0 until it was found to drop 120,84 EUR/yr on every
  professional Flanders contract.
- `vat_rate`: **0.0**, meaning the snapshot's prices are already VAT-incl (`mega.py:794`).
  This is the same convention as Eneco and Cociter; do not add a VAT multiplier when
  parsing.

The test `test_taxes_split_correctly_per_region` (`test_mega.py:379`) pins the
cross-region excise (0.0503288) and the per-region renewables split.

## Injection

`_extract_injection` (`_mega_cards.py:528`) produces an `InjectionRates` from the same energy
block, second column. There are three shapes depending on the kind:

- `tou_impact`: injection is the second number under any of the three tier labels
  (all three rows print the same value, so the first found wins). This is a
  monthly-indicative `current` only (`test_offpeak_impact_injection_uses_per_tier_column`,
  `test_mega.py:424`, illustrative 0.0292 EUR).
- `fixed` / `variable`: injection is the second number under
  `Compteur mono-horaire`, a monthly-indicative `current` only.
- `dynamic`: injection is the HTVA `factor * spot + base` formula
  (`_INJECTION_FORMULA_RE`), stored as `factor`, `base`, and the raw `formula` text.
  Residential injection is VAT-exempt, so the HTVA numbers are used as-is.

In the taxonomy from [../pricing-model.md](../pricing-model.md), Mega uses shape (a)
monthly-indicative-only for its non-dynamic products and shape (b) hourly
`factor*spot+base` for Dynamic. It never uses the spot-indexed-variable shape (c), so
no contract sets `spot_indexed_injection`. `_extract_injection` returns `None` only
when both `current` and `factor` are absent (`_mega_cards.py:87`).

### Supplier-side PV forfait

`_extract_supplier_prosumer` (`_mega_overlays.py:99`) parses the compensation-regime
`Forfait panneaux solaires (EUR/kVA par mois)` line and annualises it (times 12) into
`supplier_prosumer_eur_per_kva_year`. This forfait is TVA 6% incl, so it must NOT be
VAT-scaled: `pricing._compute_prosumer` sums it raw on top of the DSO
`Tarif prosumer` column, exactly like the Cociter Variable forfait (`mega.py:502`).
Because pypdf splits the label and value three ways across the card family (value
after, before, or with the label line-wrapped), the parser anchors on the
`Forfait panneaux` lead-in and takes the first decimal in the following 200-char
window rather than a fixed layout. Illustrative: 7.63 EUR/kVA/month annualises to
91.56 EUR/kVA/yr (`test_mega.py:242`).

Absence is legitimate only on Brussels cards and the Flanders Dynamic card (neither
carries a compensation regime); everywhere else a miss is a layout drift and raises
(`mega.py:523`). `test_supplier_pv_forfait_absent_on_brussels_and_flanders_dynamic`
(`test_mega.py:258`) pins the two legitimate-`None` cases.

## Quirks and historical bugs

The land mines a future maintainer must know, drawn from the module comments:

- **Consumption / injection formulas are printed in reverse order.** Injection comes
  first in the Dynamic PDF; anchor on the distinct labels, never on position
  (`mega.py:531`, `test_mega.py:177`).
- **The injection base can be an en-dash, not an ASCII hyphen.** `SIGN_CHARS` /
  `parse_sign` handle every Unicode dash variant (`_pdf.py:660`); do not narrow the
  sign class (`test_mega.py:164`).
- **VAT convention is TVAC (`vat_rate=0.0`).** Both energy and taxes are already
  VAT-incl; the PV forfait is TVA 6% incl and must not be VAT-scaled
  (`mega.py:484`, `mega.py:502`).
- **`Compteur bi-horaire` is split across a newline by pypdf**, so a literal `find`
  never matched; the anchor uses `Compteur\s+bi-horaire` (`mega.py:683`). The same
  split affects `Fluvius` label matching (matched via `re.IGNORECASE` and `\s`).
- **`Redevance fixe` heading splits differently on dynamic vs fixed cards**; accept
  both the joined and the split layout (`mega.py:693`).
- **Off-peak Impact cards lack the `Compteur mono-horaire` anchor** and use a bare
  `PIC` (not `Tarif PIC`) on the last row; the tier regex is permissive on the
  prefix (`mega.py:655`).
- **DSO label-to-key is not one-to-one:** `Fluvius Kempen` maps to IVEKA,
  `Fluvius Midden-Vlaanderen` to INTERGEM (`_mega_cards.py:80`).
- **Prosumer rates live in a separate table** from the main DSO row in both Flanders
  and Wallonia; scope the match to the `Tarif Prosumer` block to avoid grabbing a
  distribution rate (`_mega_cards.py:105`, `_mega_cards.py:164`).
- **Dynamic cards carry no compensation regime:** no supplier forfait, no
  DSO prosumer table, only 2 Fluvius columns (digital), and the data-management fee
  is broken out into a separate paragraph (`mega.py:523`, `_mega_cards.py:97`).
- **Brussels folds two flat annual euros** (metering fee + Sibelga <=13kVA term) into
  `data_management_per_year`; the >13kVA term is intentionally dropped
  (`_mega_cards.py:234`).
- **Off-peak Fixed retired (July 2026), revived (August 2026)** with a B2B
  edition it previously lacked. `discover()` surfaced the return the same day;
  a product Mega pulls is dropped from the registry rather than left to 404, and
  re-added when the catalog check says it is back.
- **Mandatory-line policy:** the yearly fee, federal excise, Wallonia raccordement,
  regional renewables, and the (non-Brussels, non-dynamic) PV forfait all raise on a
  miss rather than silently defaulting to 0, so a layout drift fails loudly instead
  of mis-billing (`mega.py:525`, `mega.py:701`, `mega.py:805`, `_mega_overlays.py:107`,
  `_mega_overlays.py:126`). The energy contribution left that set on 2026-08-01: the levy is
  abolished, so an absent row is a real zero rather than drift.
- **`fetch_for_month` must rotate two month placeholders and preserve the effective
  day**, and reject the CDN HTML stub via the PDF magic-byte check plus
  `archive_validity_check` (`mega.py:652`).
- **Every segment-aware path needs the `contract.professional` branch.** `fetch`,
  `probe` and `fetch_for_month` all have to know that the B2B cards live off the
  listing; `fetch_for_month` lacked it and served the residential card, because the
  B2C and B2B definitions of a product share `product_name`.

## Test fixtures

The fixtures under `tests/fixtures/` that this provider's tests exercise:

| Fixture | Card variant |
| --- | --- |
| `mega_listing.html` | The public `cartes-tarifaires` listing (URL finder, probe, discover, fetch_for_month tests) |
| `mega_smart_fixed_w.pdf` | Smart Fixed, Wallonia (bi-hourly rates, prosumer rate, PV forfait) |
| `mega_smart_fixed_v.pdf` | Smart Fixed, Flanders (static DSO exclusive-night column, Fluvius prosumer, taxes) |
| `mega_smart_fixed_b.pdf` | Smart Fixed, Brussels (Sibelga row, OSP tiers, no PV forfait) |
| `mega_smart_flex_w.pdf` | Smart Flex, Wallonia (variable contract, PV forfait) |
| `mega_dynamic_w.pdf` | Dynamic, Wallonia (consumption + injection formulas, connection fee) |
| `mega_dynamic_v.pdf` | Dynamic, Flanders (2-column Fluvius rows, external data fee, no prosumer, no PV forfait) |
| `mega_offpeak_impact_w.pdf` | Off-peak Impact, Wallonia (three-tier rates, per-tier injection, Impact DSO triplet) |

## When the card changes, look here

Ranked by how likely each is to break when Mega restyles or rotates its card, and why:

1. `_find_pdf_url` (`mega.py:316`) and the listing HTML shape. If Mega changes the
   `data-product-element` attribute, the CDN host, or the `Mega-FR-EL-B2C-<REGION>-`
   filename convention, every `fetch` / `probe` / `fetch_for_month` / `discover`
   fails at once. Start here on a total outage.
2. `_extract_meter_value` / `_extract_impact_tier` / `_extract_yearly_fee`
   (`mega.py:668`, `mega.py:655`, `mega.py:693`). Label wording or the label / value
   newline split is the most common drift; these anchor on French labels
   (`Compteur mono-horaire`, `Tarif jour`, `Redevance fixe`).
3. `_CONSUMPTION_FORMULA_RE` / `_INJECTION_FORMULA_RE` (`_mega_cards.py:79`). A reworded
   `Day Ahead Epex Spot` formula, a swapped decimal separator, or a new dash glyph
   breaks the Dynamic snapshot; check the label prefixes and `_FORMULA_TAIL` first.
4. The DSO row parsers (`_mega_cards.py:83`, `_mega_cards.py:156`, `_mega_cards.py:214`). A changed
   column count, a renamed Fluvius sub-area, or a moved prosumer table breaks the
   overlay; the fixed vs dynamic column-count difference in Flanders is the subtle
   one.
5. `_extract_supplier_prosumer` (`_mega_overlays.py:99`). A reworded `Forfait panneaux` line,
   or the appearance of the forfait on a card the code currently treats as
   legitimately absent, raises where it should not (or vice versa).
6. `fetch_for_month` (`mega.py:601`). If Mega changes the effective-date suffix
   scheme (day placement, variant tokens), the two-placeholder rewrite mis-targets;
   the YTD walk then silently falls back to the proxy, which is safe but hides the
   archive.

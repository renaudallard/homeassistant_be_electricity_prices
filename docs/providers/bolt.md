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
| Extractor id / label | `bolt` / `Bolt` | `bolt.py` |
| Regions served | Flanders, Wallonia, Brussels (all three) | every `Contract` uses the default `regions`; `EXTRACTOR.regions()` unions them, `_rates.py` |
| Publication shape | Monthly PDF card per contract, at a predictable CDN URL; a public HTML listing page links every current PDF | `bolt.py` |
| Fetch transport | `fetch_pdf_text_layout` (pdfplumber, layout-aware) | `bolt.py` |
| Probe | HEAD the listing page, prefer `ETag` then `Last-Modified` | `bolt.py` |
| Archive | The `fix` folder (`bolt_fix`, `bolt_plenty_fix` and their professional twins) is monthly-archived back to 2024-01; the variable folder has no month-addressable card and falls back to the current snapshot | `bolt.py` |
| VAT convention | Prices are VAT-incl; `vat_rate=0.0` | `bolt.py`, `base.py` |

Bolt's PDFs are the reason this extractor exists in its current form. They are around 5 MB each,
with rotated columns and a column-major text layout that `pypdf` cannot read, so the module fetches
through `pdfplumber` (`bolt.py`). Each PDF covers all three regions in one document (same
convention as Eneco), so `fetch` downloads once and `parse_snapshot` slices out the region the
caller asked for.

### Source URL pattern

Two folder families, one filename convention (`bolt.py`):

```
fix cards:  https://files.boltenergie.be/pricelists/fix/<slug>_res_el_fr_<YYYYMM>.pdf
var cards:  https://files.boltenergie.be/pricelists/var/<slug>_res_el_fr_<version>.pdf
```

Fixed cards roll monthly via a `YYYYMM` suffix. Variable cards carry a version number that Bolt
bumps in place on no fixed schedule, leaving every superseded file served, so a pinned version
keeps returning 200 and parsing cleanly while billing an old formula. `_resolve_variable_suffix`
(`bolt.py`) therefore reads the version off the listing on every fetch rather than trusting a
constant. A pinned `_11` billed June's formula for ten weeks after `_13` shipped on 2026-08-01.
When the listing cannot be read the fetch fails in the fetch helper's words, so a timeout or a
5xx stays transient and the coordinator keeps the card it holds; a listing that advertises no
such card fails as the catalog change it is. Up to 0.34.8 both fell back to a fixed version,
`_13`, which on 1 October 2026 was September's card: still served, still parsing, and billing
last quarter's 14,18 c/kWh where October's card prints 19,05.

The version is resolved **per (slug, segment)**, not once for the whole variable family. The four
slugs and both segments happen to sit on the same version today, but nothing enforces that, and a
global maximum would build a URL that does not exist for any slug whose counter lagged - trading a
stale card for a 404 on that product. The `_fr_` token means the French-language card;
all three regions still live inside that one French document. The listing page
`https://www.boltenergie.be/fr/listes-des-prix` (`bolt.py`) links every current PDF directly.

## Contracts

Bolt declares six residential-electricity contracts and a professional edition of each, twelve
in all (`bolt.py`). All are region-unrestricted (default `regions` = all three). Two are
fixed and four are variable, and every one of them bills injection per quarter-hour off Belpex.

### One card, two settlements

Each variable card is sold on either settlement, and says so in the same paragraph on all four:

> Dans le cadre d'une facturation dynamique, la consommation ou l'injection enregistree est
> multipliee, pour chaque quart d'heure, par la valeur Belpex correspondante pour ce meme quart
> d'heure. En optant pour une facturation variable, nous redistribuerons la consommation ponderee
> RLP (publication par Synergrid). Pour l'injection, nous redistribuerons l'injection ponderee SPP.

One printed formula, two ways of settling it, and nothing on the card says which one a given
account is on. So the variable contracts carry `quarter_hourly_option` and the config flow asks;
`resolve_settlement_grid` then builds the dynamic leg out of the coefficients the parser put on
the variable one.

This used to be modelled as a second contract id per card (`bolt_dynamic` and friends). It is not
any more, and the difference matters for more than tidiness: the settlement changes the contract
KIND, and everything the flow decides before it has ever fetched a card reads the kind. Unticked
the product is `variable`, so the meter step offers the full list and no ENTSO-E key is demanded;
ticked it is `dynamic`, so the meter narrows to SMR3 and the key becomes mandatory. That is why
`effective_kind` is a function of the entry rather than a field on the contract, and why the
settlement step runs directly after the contract step and before the signing-rate one.

Only the `bolt` slug ever had a dynamic sibling, which left a Plenty, Online or Plenty Online
household settling dynamically with nothing in the picker that matched their contract, and no
usable stand-in: the four cards carry different coefficients and different standing charges.
Plenty Online is billed on the base card's `1,168 + 16,90` at 0,99 EUR/month against 8,99 (its
French card prints another formula, see below), about 96 EUR/yr apart, all of it the standing
charge.

`_migrate_bolt_dynamic_contract` (`__init__.py`) moves an entry stored under one of the eight
retired ids onto its variable card with the box ticked, and moves the unique id and the title with
it. The bill does not change: same coefficients, same standing charge, same feed-in formula, same
card. The unique id stays put in the one case where the target is already taken, which is a
household that deliberately ran both readings as two entries.

### Plenty Online's formula

The French residential Plenty Online card prints the Online card's monthly prices, `19,05
20,09 18,15 18,15` in October 2026, beside `Belpex * 1,145 + 16,45` and the Impact bands
`13,37 / 14,45 / 17,77`: the professional cards' formula and bands, digit for digit. The
prices back-solve to another index than that formula's (139,40 EUR/MWh on the base cards,
142,59 on Plenty Online), so the card contradicts itself. Its Dutch edition settles which half
is right: it prints the same prices beside the Online card's `Belpex * 1,168 + 16,90` and
Impact bands `14,47 / 20,77 / 24,19`, as the Online card does in both languages. The French
card's formula row is the professional one carried over.

So `bolt_plenty_online` carries `index_slug = "online"`: `fetch` also reads the Online card,
and while Plenty Online prints the Online card's monthly prices, `_with_index_card_formula`
(`_bolt_cards.py`) bills it on that card's formula and Impact bands. The prices, the 0,99
EUR/month standing charge, the feed-in and the levies stay Plenty Online's own. A card that
stops printing the Online card's prices keeps everything it prints. Up to 0.34.6 the prices
were instead re-derived at Plenty Online's French formula, 18,66 c/kWh mono where the card
prints 19,05, and a quarter-hourly entry was billed that formula: both about 0,39 c/kWh, 13,58
EUR a year at 3500 kWh, too low. `parse_snapshot` refuses to price the contract without the
Online card's text rather than fall back to the professional formula, and the live check hands
it the card it already fetched. It also refuses a pair whose `<Month> <Year>` headers differ:
each card's version comes off its own listing read, and Bolt can list a new version between the
two, which would pair October's card with September's, whose prices differ, and leave the professional formula in place, and Bolt's probe key, the
listing's ETag, would keep that snapshot until the listing next changes. The refusal's message
opens with `OUT_OF_STEP` (`_pdf.py`), which `is_transient_fetch_error` counts as transient: the
next fetch pairs the cards again, so it is held to the softer "could not reach the supplier"
card rather than the layout-change one.

### Plenty offers

The Plenty cards print a new-signing offer under a `Réduction` heading, read by
`_extract_promotion` (`_bolt_cards.py`) into the snapshot's welcome-credit fields. It
has taken two shapes:

- **Until September 2026, a lump and a feed-in bonus for the first year:** *"Lorsque vous
  concluez un nouveau contrat Plenty Fixe en Flandre au cours du mois de septembre 2026 (...),
  vous bénéficiez d'une réduction de €300 (TVA incluse), ainsi que d'une indemnité d'injection
  supplémentaire de 1,0 c€/kWh (hors TVA), valable durant votre première année de contrat."*
  The lump goes to `welcome_credit_eur`, the bonus to `welcome_credit_injection_eur_per_kwh`.
  The bonus is capped at 12 MWh of export a year, which binds only an installation far
  above a household's; the cap is not modelled.
- **From October 2026, a cut in the energy price:** *"Si vous souscrivez à un nouveau contrat
  Plenty Fixe en Flandre au cours du mois d'octobre 2026, vous bénéficiez d'une réduction de
  9,0 c€/kWh (TVA comprise), ainsi que d'une compensation d'injection supplémentaire de 1,0
  c€/kWh (hors TVA) pendant toute la durée du contrat."* The cut goes to
  `welcome_credit_eur_per_kwh`, measured on the first year's volume like Mega's ristourne.
  At 3500 kWh it is worth 315 EUR, more than the card's whole margin over Belpex.

Both are paid at the yearly settlement (*"octroyée via la facture de régularisation après une
année de consommation ininterrompue"*, later *"lors de la facture de décompte annuelle"*), so
`welcome_credit_kind` is `anniversary`. The October wording runs *"pendant toute la durée du
contrat"*, and the same card fixes the contract's conditions at one year in Flanders and
Wallonia, so it is carried as the first year's, the way the lump before it was. A renewal is a
new year on whatever card is current then.

**Region.** The Plenty Fixe offer has said *"en Flandre"* since June 2026 (the Dutch cards say
*"in Vlaanderen"*), and a snapshot for another region carries no offer. Bolt republished the
October cards during 1 October to add those words: the first render of the day had none.

**Basis.** Each figure is read on the basis its card states and brought onto the card's own:
TVAC on a residential card, excluding VAT on a professional one, which `apply_vat` then
grosses for a business that pays VAT. On a professional card the basis comes from the Dutch
edition of the same card (`_dutch_edition` in `bolt.py`, the French URL with `_el_nl_`), because
the French sentence is the residential one carried over: the October 2026 French professional
Plenty Fixe card states its 10,0 c€/kWh *"TVA comprise"* on a card priced HTVA throughout, where
the Dutch edition says *"10,0 c€/kWh (excl. btw) korting"*, so the cut is 10,0 c€/kWh before VAT.
The two editions agree in August (*"470 € (TVA incluse)"*, *"€ 470 (incl. btw) korting"*), and that
lump stays 388,43 EUR before VAT. The Dutch label counts only for the figure the French card
prints; a Dutch card that is not there, or names no such figure, leaves the French words as
printed, and a timeout fails the fetch so the tick retries. The feed-in bonus is *"hors TVA"*
everywhere and is carried as printed. The archive replays a row captured before the Dutch card
was read on its French reading (`_respelled` in `scripts/archive_cards.py`).

**Signing month.** The offer is for contracts signed in the month the sentence names, held in
`welcome_credit_signing_month`. The variable cards are addressed by version, so a Plenty Online
contract signed in a month no archive holds is billed on today's card as a stand-in, and
without that month `signing_month_snapshot` (`cohort.py`) would hand it today's campaign.
It withholds a credit whose month is not the contract's card month, and a compare candidate is
held to the household's own signing month the same way in the year-to-date column, and to the
month it is quoted in for the coming year (`_candidate_welcome_credit`, `compare_quote.py`).

The column beside the block is interleaved into the sentence by `pdfplumber`, even between a
figure and its unit (*"réduction de du lundi au vendredi, de 9 h à 17 h. €300"*). Each part is
therefore searched for on its own past the anchor, and a figure only counts with its currency at
most two line breaks on.

### The professional editions

Bolt publishes each product twice at the same path, with `_res_` or `_pro_` in the filename, so the
pro lane is just `_ContractDef.segment` feeding `_document_url`. Three things differ on the card:

- **It prices excluding VAT at 21%**, so the snapshot carries `vat_rate=0.21`. The distribution
  block is still headed `TTC`, but its numbers match the other suppliers' ex-VAT tables to the
  cent, so the label is stale rather than the values - do not trust that heading.
- **Injection is taxed** (`vat_applies=True`), against the residential exemption.
- **No residential card states its VAT rate either.** The variable cards' settlement formula and
  Impact bands are grossed by the residential rate (`_RESIDENTIAL_VAT`), recorded on the snapshot
  as `TaxOverlay.assumed_vat_rate`; a fixed card grosses nothing and records none.
- **The `N% TVA` phrase is gone.** `_consumption_formula` (`_bolt_cards.py`) reads it to scale the Belpex formula,
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
(`engie.py`) and so does Mega (`_mega_overlays.py`, `mesure + fixed_term_le13`), which is why their
Brussels `data_management_per_year` is around 64,80 EUR/yr. **Bolt's card prints only six numbers
for the Sibelga row**, ending at the metering term, with no ≤13 kVA column anywhere in the
document, so its Brussels `data_management_per_year` is that term alone and a Bolt Brussels entry
under-states the annual network fee against an Engie or Mega quote for the same connection.

There is nothing to read on the card, and `providers/base.py` is explicit that no supplier EUR value lives
in Python source: every number in a `SupplierSnapshot` comes from a live fetch. So the term is read
from the regulator that sets it. **Closed**: `brugel.py` fetches Brugel's published "Grille
tarifaire - Electricite" for the year, one small PDF stating "prix hors TVA", and reads the two
`Puissance mise a disposition` rows out of its "Sans mesure de pointe" block (47,24 and 94,48
EUR/year for 2026). When the year's final sheet is not served, the year's page of the
2025 to 2027 grid Brugel published with its tariff methodology is read instead
(`_indicative_term`, `_parse_grid`: 53,22 and 106,43 for 2027), which Brugel calls indicative;
the figure is flagged (`power_term_is_indicative`), the `brussels_power_term_missing` card
says so under its `brussels_power_term_indicative` wording, and the final sheet, asked for
again on the six-hour failure schedule, replaces it once served. `resolve_brussels_power_term` adds them to a Sibelga overlay that is missing
them, on the card's own VAT basis, and `_resolve_snapshot` applies it, so every path prices the
same completed overlay.

Two signals have to agree before a card is touched: it prints no band above 13 kVA, which every
card carrying the full charge does print, and its fixed term is smaller than the power part alone,
so it cannot already contain it. Bolt completing its row therefore retires the workaround with no
edit here, and the peers are untouched today. The fetch never raises: until the sheet has been read
the card bills exactly what it billed before, which is the gap this closes rather than a new
failure mode.

Sibelga's own site answers 403 to this integration, which is why the regulator's publication is the
source and not the operator's.

| id | label | registered kind | folder / slug | `spot_indexed_injection` | `quarter_hourly_option` | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| `bolt_fix` | Bolt Fixe (1 year) | fixed | `fix` / `fix` | yes | no | The only card with a real monthly archive |
| `bolt_plenty_fix` | Bolt Plenty Fixe (1 year) | fixed | `fix` / `plenty_fix` | yes | no | Fixed, month archive like `bolt_fix` |
| `bolt_variable` | Bolt Variable | variable | `var` / `bolt` | yes | yes | `Belpex * 1,168 + 16,90`, 8,99 EUR/month |
| `bolt_plenty` | Bolt Plenty Variable | variable | `var` / `plenty` | yes | yes | Same formula, 3,99 EUR/month |
| `bolt_online` | Bolt Online | variable | `var` / `online` | yes | yes | Same formula, 5,99 EUR/month |
| `bolt_plenty_online` | Bolt Plenty Online | variable | `var` / `plenty_online` | yes | yes | Same formula, 0,99 EUR/month; its French card prints the professional formula, so the formula and Impact bands are the Online card's (below) |

The `spot_indexed_injection` column is the registry flag verbatim, and it reads the way it does
because the flag answers "does this product's feed-in need spots its ENERGY leg never fetches".
Every fixed and variable Bolt card sets it: they print the quarter-hourly Belpex injection formula
beside the illustrative figure and settle on it, while a fixed card's energy leg is a printed rate
that asks for no spot at all. On the variable cards it is redundant beside `month_indexed_energy`,
which they also set (see [Quarterly index](#quarterly-index)). The column used to read `no` down
the whole non-dynamic half, which is the exact inverse of `bolt.py`.

`test_bolt_is_registered` (`tests/test_bolt.py`) pins the count at exactly twelve, so adding or
removing a product must update that test. `test_every_variable_card_offers_the_settlement_choice`
pins which cards carry the box, `test_the_settlement_answer_moves_the_contract_kind` pins that the
answer reaches `effective_kind`, and `test_each_card_keeps_its_own_coefficients_and_standing_charge`
pins that the four slugs resolve to four different documents, which is how the mis-price above
would come back.

The variable branch of `_extract_energy` reads both halves of the card. The printed `Prix mensuel`
becomes `VariableRates.current`: the formula at the last closed quarter's index, which is what an
entry without an ENTSO-E key keeps (see [Quarterly index](#quarterly-index)); `_consumption_formula` reads the `Belpex * <factor> <sign> <base>` row beside it into
`formula_factor` / `formula_base`, converted to the EUR/kWh basis applied against the EUR/kWh spot
(factor stays a ratio, base is EUR/MWh -> EUR/kWh, both VAT-baked since `vat_rate=0`).
`resolve_settlement_grid` turns that pair into a `DynamicRates(quarter_hourly=True)` for an entry
that ticked the box. The professional branch asserts `HTVA` and scales by 1.0 instead, because the
pro card drops the `N% TVA` phrase the multiplier reads.

`_extract_injection` does not branch on the settlement at all: the card applies the injection
formula per quarter-hour whichever way consumption is settled, so both readings share one leg, with
the printed indicative kept as the fallback for an entry with no ENTSO-E key. Bolt has no `tou` /
`tou_impact` product; `_extract_energy` still raises on any other kind.

## Fetch strategy

### `fetch` (current snapshot)

`fetch(session, contract_id, region)` (`bolt.py`) validates the contract id, calls the shared
`_fetch_pdf_text` to get `(url, text)`, then hands off to `parse_snapshot`. The download is factored
into `_fetch_pdf_text` (`bolt.py`) precisely so `live_check.py` can fetch a card once and parse
three region-specific snapshots from the same 5 MB text instead of paying for the round-trip three
times.

Two timing decisions live in this path:

- **60 s PDF timeout** (`bolt.py`). The shared default is 30 s, but Bolt's CDN occasionally
  needs well over that to deliver one 5 MB card. Issue #13 records all six fetches timing out for
  around 25 minutes on 2026-05-09 while the URLs themselves were healthy. The 60 s budget lets a
  2-3x CDN slowdown still yield a snapshot instead of an `UpdateFailed`.
- **Fixed-card previous-month fallback** (`bolt.py`). Fixed cards may not be published yet on
  the 1st of the month, so for a `fix`-folder contract the extractor retries the previous month's
  URL and the user keeps seeing plausible prices. The month boundary is computed in Brussels local
  time (`dt_util.now()`), matching `_document_url`, so it never rolls back two months on the
  new-month UTC seam. Bolt cards expose no parseable `valid_until`, so this fallback cannot signal
  staleness through it; the `_LOGGER.warning` at `bolt.py` is the only trace that last month's
  card is being served. Variable-folder contracts do not get it at all (they re-raise,
  `bolt.py`).

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
(`bolt.py`): UTC would mis-key by last month for the first 1-2 Brussels hours of every month.

### `probe` (freshness)

`probe` (`bolt.py`) HEADs the listing page and returns the first present header, preferring
`ETag` then `Last-Modified` via `head_freshness_key` (`_pdf.py`). Bolt is the reason
`head_freshness_key` accepts a `prefer` order: its listing returns a stable `ETag` while
`Last-Modified` flips on every CDN edge cache, so every other supplier prefers `Last-Modified` and
Bolt inverts it (`_pdf.py`). The probe returns a single key for the whole listing (it ignores
`region`, and returns `None` for an unknown contract id). When the HEAD fails or carries neither
header, `head_freshness_key` returns `None` and the coordinator's time-based TTL takes over.

### `discover` (live-check coverage)

`discover` (`bolt.py`) GETs the listing HTML and returns the set of `<folder>/<slug>` prefixes
it links for residential electricity, matching `_CARD_URL_RE` (`bolt.py`) and filtering to the
`res` segment, so it still diffs on folder/slug alone. That pattern is shared with the version
resolver, and keeping it usable by both is why the version group is `\w+` and the match is
case-insensitive: pinning it to `\d+` for the resolver's benefit silently narrowed discovery, and a
slug this pattern cannot see is a new product the catalog diff reports as silence. `live_check.py`
diffs that against the registry's `{c.folder + '/' + c.slug for c in _CONTRACTS}` set, so a new Bolt
product or a renamed slug surfaces as a coverage gap. On fetch failure it returns an empty set
rather than raising.

### `fetch_for_month` (archive / YTD backfill)

`fetch_for_month(session, contract_id, region, year_month)` (`bolt.py`) supports the
time-correct yearly-cost flow. It gates on the `fix` FOLDER (`bolt.py`) and returns `None` for
everything else. That folder is archived monthly under the `YYYYMM` suffix going back to 2024-01,
and every card in it addresses its current card the same way, so all four clear the gate:
`bolt_fix`, `bolt_plenty_fix` and their professional twins. Variable cards are keyed by version
(`bolt_res_el_fr_13.pdf`) rather than by month, so past months cannot be addressed there at all;
those return `None` and the YTD path falls back to the current snapshot as a proxy (`bolt.py`).

The gate used to require the slug to be `fix` as well, which locked the two `plenty_fix` contracts
out of an archive that does exist (verified against the CDN: `plenty_fix_res_el_fr_202601.pdf` and
`plenty_fix_pro_el_fr_202601.pdf` both serve). A one-year fixed contract signed in January was
therefore priced all year at the current card.

Because a `fix` card carries no parseable `valid_until`, `fetch_for_month` cannot trust the URL
alone: the CDN could serve a current card under a historical URL and silently bill a past month at
today's rates. So after parsing it runs `archive_validity_check` (`bolt.py`,
`_validity.py`) with `month_names=_FR_MONTH_NAMES`. Since `valid_until` is `None` for Bolt, that
check falls through to `text_mentions_month`, which requires the printed `<Month> <Year>` header (or
`MM/YYYY` / `YYYY-MM`) to reference the requested month inside an anchored window; a mismatch
returns `None` and the caller uses the proxy. `test_fetch_for_month_rejects_mismatched_month`
(`tests/test_bolt.py`) pins this: the April fixture is accepted for April 2026 and rejected for
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
> `_extract_legacy_energy` (`_bolt_cards.py`) reads the older shape, keyed on which
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

`parse_snapshot(contract_id, text, region, source_url)` (`bolt.py`) is the pure parser exposed
for unit tests. First it normalizes U+2028 LINE SEPARATOR characters that Bolt sprinkles where a
newline is expected, replacing them with `\n` so one set of regexes covers every block
(`bolt.py`). Then it fans out to the field extractors and assembles a `SupplierSnapshot`.

### Field map

| Snapshot field | Extractor | Notes |
| --- | --- | --- |
| `energy` | `_extract_energy` (`_bolt_cards.py`) | `FixedRates` or `VariableRates` |
| `injection` | `_extract_injection` | printed figure PLUS the quarter-hourly `factor`/`base`, flagged `slot_indexed`, on every card and either settlement |
| `publication_label` | `_extract_publication_month` (`bolt.py`) | `<Month> <Year>` header. The accent classes span the whole Latin-1 range rather than the accents French month names actually use: Bolt's August 2026 fixed card prints "Aôut 2026" (circumflex on the wrong vowel) and an exact class blanked the label on that typo. The value is display-only and never feeds pricing, so a misspelling is tolerated verbatim rather than corrected or dropped. |
| `taxes.federal_excise`, `energy_contribution`, `region_connection_fee` | `_extract_taxes` (`_bolt_overlays.py`) | 3-column FL/WAL/BX rows, sliced by region |
| `taxes.energy_fund_eur_per_month` | `_extract_energy_fund` (`_bolt_overlays.py`) | Flanders only. The card prints both categories: a domiciled residential connection pays the `résidentiel` row, which is `-` (0); a **professional** contract pays the `non-résidentiel` row (10,07 EUR/month on the August 2026 card). The two rows need separate patterns, since the residential value sits after a U+2028 and the non-residential values are inline on the label line |
| `taxes.{flanders,wallonia,brussels}_renewables` | `_extract_renewables` (`_bolt_overlays.py`) | certificats verts + Flanders WKK; zeroed outside the active region |
| `dsos` | `_extract_flanders_dsos` / `_extract_wallonia_dsos` / `_extract_brussels_dsos` | picked by region |
| `valid_until` | `parse_valid_until` (`_validity.py`) | always `None` in practice; Bolt prints no parseable validity date |

The region argument slices the multi-region document: `parse_snapshot` zeroes the two non-active
regional renewables columns (`bolt.py`) and calls exactly one of the three DSO parsers. The
Flanders energy fund is only read when `region == flanders`.

### Energy block (`_extract_energy`)

Bolt's price model has two convention quirks the parser normalizes:

1. **Monthly platform fee, billed annually.** `_extract_yearly_fee` (`_bolt_cards.py`) matches
   `€ N[,NN] / mois` and multiplies by 12 to fit the integration's annual-fee convention. The
   platform fee is the entire Bolt monetisation, so a missing match raises rather than returning 0
   (a silent miss would undercount the bill by roughly 130 EUR/year, illustrative from the
   docstring). The decimal portion is optional so a future round fee like `€ 11 / mois` still
   parses. `test_fix_yearly_fee_is_monthly_x_12` (`tests/test_bolt.py`) asserts `10.99 * 12`
   (illustrative).

2. **`Prix mensuel` is the price a fixed card bills, and a variable card's last closed quarter.**
   On a variable card it is the formula at the index of the quarter before (see
   [Quarterly index](#quarterly-index)). The line prints two adjacent
   numbers: mono, then the **exclusive-night** rate (group 2 is the dedicated night-circuit rate,
   NOT a day/peak rate) (`_bolt_cards.py`). Values are in c/kWh, so the parser divides by 100.

Bi-hourly (Jour / Nuit) rates come from a separate `Prix de l'électricité verte` block that prints
two `Jour Nuit` subheads: the first pair is for consumption, the second is for injection
(`_bolt_cards.py`). The bi-horaire consumption row is the LAST same-line adjacent-number pair between
the two subheads, so the parser scopes a `re.S` span between them and takes `pairs[-1]`. This is the
stable invariant because `pdfplumber` sometimes renders the annual-estimate column vertically above
the row (variable cards) and sometimes drops it entirely (fixed cards), so a fixed positional offset
would break. `test_variable_uses_current_monthly_not_annual_estimate` (`tests/test_bolt.py`)
verifies the parser skips the annual estimate (15,20 / 15,20 illustrative) and picks the current
monthly (14,56 / 12,09 illustrative).

The fallback logic when no bi-horaire pair is found is kind-dependent (`_bolt_cards.py`):

- `fixed`: mono == peak == offpeak, and the card sometimes omits the bi-horaire row entirely, so the
  single rate is the right value for all three. `test_fix_extracts_consumption_rates`
  (`tests/test_bolt.py`) confirms all four rates equal the single value (16,71 c/kWh
  illustrative).
- `variable`: a miss is a layout drift, not a mono contract; variable cards always publish distinct
  Jour / Nuit rates, so the parser raises rather than silently billing a bi-hourly user at the mono
  rate. `test_variable_missing_bihourly_rates_fails_loud` (`tests/test_bolt.py`) enforces this.

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
INJECTION SHAPE (c): per-slot formula on a static-energy card, the same shape a ticked settlement has
always had, and VAT-exempt on both. A card generation that prints no formula table keeps the figure
alone rather than losing the credit.

Two land mines are baked into the anchor (`_bolt_cards.py`):

- The consumption side also has a `Prix mensuel` line ABOVE the injection block, so the parser
  anchors on the `Injection` header (`re.S` across to the injection `Prix mensuel`) rather than
  counting occurrences. A third consumption-side row therefore cannot shift the match.
- The July 2026 fix cards print a NEGATIVE second column (`Prix mensuel 3,40 -0,43`, the "Exclusif
  nuit" injection column). Only the first column is billed, but the second is a required anchor
  token, so the regex allows an optional minus on it (`-?[\d.,]+`).
  `test_injection_accepts_negative_second_column` (`tests/test_bolt.py`) locks this in.

`test_injection_carries_the_quarter_hourly_formula` checks both fix and variable
cards yield `current` = 5,31 c/kWh (illustrative) beside the printed Belpex
coefficients (`factor` 0,94, `base` -0,01133 EUR/kWh) and `slot_indexed` set.

### Tax block (`_extract_taxes`)

Taxes print as 3-column rows (Flandres / Wallonie / Bruxelles) after the U+2028 normalization
flattened them to newlines (`bolt.py`). Federal excise (`Droit d'accise spécial`) and energy
contribution (`Contribution sur l'énergie`) are mandatory federal levies on every Belgian card, so a
regex miss raises (`_bolt_overlays.py`); the apostrophe class `['’]` handles both straight and curly
quotes.

One exception, scoped to the contribution row alone: Belgium abolished that levy in August 2026 and
Bolt kept the row while replacing its rates with `-`, one per region, the excise beside it having
absorbed it. `_row_is_explicit_zero` (`_bolt_overlays.py`) recognises that shape and prices it as 0. A dash
is the card **saying** zero, which is a different fact from a row whose values could not be read, and
only the first may pass silently -- reading a missing row as zero is how a card that changed shape
bills several c€/kWh short behind a passing extractor. So a row that vanishes entirely still raises,
and a dashed **excise** still raises too, since that levy is never zero (issue #78). `_pick` (`_bolt_overlays.py`) indexes group 1/2/3 by region and treats `-` or empty as 0.

The connection-fee row (`Redevance de raccordement`) is Wallonia-only on real cards, so a miss is
permitted (returns 0). Its regex eats up to three integer footnote markers ahead of the FL/WAL/BX
values (`_bolt_overlays.py`); the `{0,4}` cap deliberately stops a future integer-only Flanders value from
being mistaken for a footnote and silently shifting the columns.

`test_taxes_split_correctly_per_region` (`tests/test_bolt.py`) checks nationwide excise
(0.050329) and contribution (0.002042), Wallonia connection fee (0.00075), and per-region renewables
(all illustrative).

### Renewables block (`_extract_renewables`)

Three columns under `Certificats verts (c€/kWh)`, plus a Flanders-only `WKK` (cogeneration) row that
is ADDED to the Flanders certificats-verts value (`_bolt_overlays.py`). This split across two lines is the
second Bolt-specific convention (`_bolt_overlays.py`). The WKK regex skips an optional multi-digit footnote
ref before the value and requires a real whitespace separator so a greedy `\d*` cannot swallow the
leading digits of a multi-digit value (`_bolt_overlays.py`). Certificats verts is charged in every region,
so a miss raises; WKK is optional. `test_taxes_split_correctly_per_region` asserts Flanders
renewables = `(1.17 + 0.39)/100` (cert + WKK, illustrative), proving the footnote skip and the sum.

### DSO overlays

Bolt maps every DSO sub-area the integration knows, region by region. A structural quirk that spans
all three parsers: `pdfplumber` sometimes renders a row vertically (one number per line), so the
regexes use `\s+` (which matches newlines) between values to handle both layouts.
`test_wallonia_dso_handles_vertical_layout` (`tests/test_bolt.py`) exercises this.

**Flanders (`_extract_flanders_dsos`, `_bolt_overlays.py`).** Eight Fluvius sub-areas via `_FLANDERS_LABELS`
(`_bolt_overlays.py`). Note the label-to-key mapping is not one-to-one by name: `Fluvius Kempen` maps to
`DSO_FLUVIUS_IVEKA` and `Fluvius Midden-Vl` to `DSO_FLUVIUS_INTERGEM`. Each row has 8 numbers; the
extractor bills the digital (SMR3) block (columns 1-4 plus the prosumer column 8) and ignores the
trailing classic columns. Group 4 is the dedicated exclusive-night meter rate, lower than normal
digital distribution, so a night circuit is billed at it. `transport` is `0.0` (Flanders folds
transport into distribution). Flanders digital meters carry no prosumer tariff on the DSO side in
the general case, but Bolt still exposes a prosumer column, which is read into
`prosumer_eur_per_kva_year`. `test_flanders_dso_includes_transport_in_distribution`
(`tests/test_bolt.py`) checks Antwerpen: transport 0.0, distribution 0.0535, exclusive-night
0.0481 (< distribution), capacity 52.37 (all illustrative).

**Wallonia (`_extract_wallonia_dsos`, `_bolt_overlays.py`).** Five DSOs via `_WALLONIA_LABELS`
(`_bolt_overlays.py`). Ten numbers per row: mono, jour, nuit, excl_nuit, PIC, MEDIUM, ECO, transport,
terme_fixe (EUR/an), prosumer (EUR/kVA/an). PIC/MEDIUM/ECO populate the CWaPE Tarif Impact band
columns (`distribution_pic`, `distribution_medium`, `distribution_eco`); `terme_fixe` becomes `data_management_per_year`.

The Wallonia parser carries the module's single largest land mine, the **RESA/REW label swap**
(`_bolt_overlays.py`). In Bolt's `pdfplumber` text extraction the rows labeled `TECTEO RESA` and `WAVRE`
carry each other's values, so `_WALLONIA_LABELS` deliberately maps `TECTEO RESA -> DSO_REW` and
`WAVRE -> DSO_RESA` to un-swap them. This was verified against the regulator's rates and every other
supplier's PDF. After parsing, a runtime sanity check enforces the invariant that RESA's
`distribution_single` stays strictly cheaper than REW's (a Walloon-tariff pattern that holds for
every card parsed). The check uses a process-wide `_RESA_REW_LOGGED` latch (`_bolt_overlays.py`) so it
rings HA's notification bell at most once per boot. Three outcomes (`_bolt_overlays.py`):

- Both rows missing: stay quiet (the parser already raised on the wider drift).
- Only one row parsed: log at ERROR once (the surviving row may now carry the other DSO's values
  with nothing to compare against).
- Both parsed but the inequality flipped: log at ERROR once, meaning Bolt likely fixed the upstream
  layout and the compensating swap now inverts correct values, so it should be removed.

The swap needs manual re-validation at least every 6 months (last done 2026-05, next due 2026-11,
`_bolt_overlays.py`). `test_resa_is_cheaper_than_rew_after_label_swap` (`tests/test_bolt.py`) guards
the invariant in CI.

**Walloon figures the card prints differently from every other card.** The network rates are set
per DSO by the CWaPE, so every card for one month carries the same row, and Bolt's does not in
four places. Each is billed as printed: `providers/base.py` forbids EUR values in Python
source, and filling a Bolt row from another supplier's card would import a figure Bolt's own card
contradicts. `_check_network_consensus` (`scripts/live_check.py`) reports each one, and each is
allowed on its exact figure in `_KNOWN_NETWORK_FIGURES` until 2027-01-01, so a Bolt card that
changes the figure is reported again.

- **Professional cards, Medium = Pic.** All six professional contracts print the Pic rate in the
  Medium column for ORES (15,18), RESA, REW and AIESH, every month held; AIEG is right. An entry on
  the Impact network mode is therefore billed Pic for every Medium hour: on ORES about 51 EUR/yr ex
  VAT at 3500 kWh on a flat load, 289 at 20 MWh. The residential card prints distinct columns.
  This is the network overlay, not the energy bands, which Bolt derives from each row's formula
  (see [Wallonia Tarif Impact](#wallonia-tarif-impact)).
- **Professional cards, ORES and AIEG rows.** The whole row is the older set (ORES 10,85 c/kWh
  ex VAT) where every other card has printed 11,30 since February: about 16 EUR/yr low at 3500 kWh.
  In January the archive splits four against four: Bolt, EnergyVision, Mega and OCTA+ printed the
  older set, while Cociter, Eneco, Engie and Luminus already printed the newer one. By February
  Bolt was the only one left on it.
- **Residential fixed cards, January to August.** `bolt_fix` and `bolt_plenty_fix` printed the
  older ORES and AIEG rows (ORES 11,50 including VAT against 11,98), the same January split, until
  the September card corrected them, so a past month re-priced off the archive bills that figure.
- **Residential cards, ORES Medium.** 10,38 including VAT where every other card prints 10,83.

**Brussels (`_extract_brussels_dsos`, `_bolt_overlays.py`).** One row, `Sibelga`, with six captured
numbers: mono, jour, nuit, excl_nuit, transport, terme_fixe (the prosumer trailing token is `-`).
The exclusive-night column (group 4) is wired into `distribution_exclusive_night` via the shared
`brussels_sibelga_overlay` builder (`_bolt_overlays.py`); earlier it was dropped, which made a Brussels
night meter fall back to off-peak, correct only while the two columns happened to be equal. The
Sibelga overlay also carries the Brussels Brugel OSP annual-fee table via `parse_brussels_osp`
(`_parse.py`); Bolt prints `Obligations de service publique` with a lowercase `s`, which the
case-insensitive helper handles. A missing Sibelga row returns an empty dict (permitted).
`test_brussels_extracts_sibelga` (`tests/test_bolt.py`) checks distribution 0.0996, off-peak
0.0753, exclusive-night 0.0753, transport 0.0227 (all illustrative).

## Quirks and historical bugs (the land mines)

- **Monthly fee, x12.** `€ N / mois` is multiplied by 12 for the annual convention; a miss raises,
  not returns 0 (`_bolt_cards.py`).
- **Split renewables.** Flanders renewables = `Certificats verts` + `WKK`, two separate lines
  (`_bolt_overlays.py`).
- **VAT-incl.** Prices are already VAT-incl, so `vat_rate=0.0` (`bolt.py`). An extractor that
  ever ships ex-VAT numbers must set the parsed rate explicitly.
- **No parseable `valid_until`.** Bolt cards print `Carte Tarifaire Bolt Fixe <Month> <Year>` but no
  machine-readable validity date, so `parse_valid_until` returns `None` and the archive cross-check
  falls back to a textual month match on `_FR_MONTH_NAMES` (`bolt.py`).
- **U+2028 line separators.** Normalized to `\n` at the top of `parse_snapshot` (`bolt.py`);
  every downstream regex depends on that.
- **5 MB PDFs, slow CDN.** 60 s timeout to survive a 2-3x slowdown (issue #13, `bolt.py`).
  The CDN-slowness signature is a detail ending in `: TimeoutError` plus a missing per-supplier
  metrics row: a transient aiohttp timeout, not a regression. (Before `error_text`
  (`_pdf.py`) the same failure printed nothing after the colon, so an empty tail in an old
  run log means the same thing.)
- **First-of-month fixed fallback.** Missing current-month fix card falls back to the previous month
  with a warning; Brussels-local month math avoids the UTC seam (`bolt.py`). Only a genuinely
  absent card qualifies: a textless card and a transient fetch failure both re-raise, or they would
  serve last month's prices with no staleness signal at all.
- **RESA/REW swap.** Compensating label inversion plus a self-disarming ERROR invariant; re-validate
  every 6 months (`_bolt_overlays.py`).
- **Negative injection second column.** July 2026 fix cards; the anchor token tolerates a minus
  (`_bolt_cards.py`).
- **Vertical `pdfplumber` rows.** Every DSO regex uses `\s+` to span one-number-per-line renders
  (`_bolt_overlays.py`).
- **Walloon network rows that disagree with the fleet.** Billed as printed and watched by the live
  check; see the DSO overlays section.
- **Exclusive-night everywhere.** `Prix mensuel` group 2 and Fluvius group 4 and the Sibelga column
  are all dedicated night-circuit rates, not day/peak rates (`_bolt_cards.py`,
  `_bolt_overlays.py`).

## Test fixtures

Under `tests/fixtures/` (2,5 to 5 MB each, French-language, all three regions):

| Fixture | Card variant | Exercised by |
| --- | --- | --- |
| `bolt_fix.pdf` | Bolt Fixe (fixed) April 2026 | most tests: yearly fee, consumption rates, injection, per-region taxes, Wallonia/Flanders/Brussels DSOs, RESA/REW swap, `fetch_for_month` accept/reject |
| `bolt_variable.pdf` | Bolt Variable April 2026 | injection parity with fix, current-vs-annual bi-horaire selection, loud failure on missing Jour/Nuit |
| `bolt_plenty_fix_sep.pdf` | Bolt Plenty Fixe September 2026 | the lump-and-bonus offer, its figure split from its currency by the other column |
| `bolt_plenty_fix_oct.pdf` | Bolt Plenty Fixe October 2026, as republished on 1 October | the per-kWh offer, its Flanders gate and its basis |
| `bolt_plenty_online_oct_reissue.pdf` | Bolt Plenty Online October 2026, as replaced on 2 October | the Online card's prices beside the professional formula |
| `bolt_online_oct_reissue.pdf` | Bolt Online October 2026, as replaced on 2 October | the card Plenty Online's formula and Impact bands are read off, the quarterly re-price |
| `bolt_online_oct.pdf` | Bolt Online October 2026, as first published | a card whose index table is the quarter before its prices', refused |

Fixtures are loaded via `fixture_text("bolt_fix.pdf", layout=True)` (`tests/test_bolt.py`), which
routes through the `pdfplumber` layout extractor so tests see the same text the live path parses.

## When the card changes, look here

Ordered by likelihood of breaking when Bolt re-renders or restructures a card:

1. **DSO row regexes** (`_extract_flanders_dsos` `_bolt_overlays.py`, `_extract_wallonia_dsos`
   `_bolt_overlays.py`, `_extract_brussels_dsos` `_bolt_overlays.py`). Column-count changes, a renamed sub-area
   label, or a new footnote marker breaks these first. A row that stops matching is silently dropped
   (Flanders/Brussels) or raises via the Wallonia invariant path.
2. **RESA/REW swap** (`_WALLONIA_LABELS` `_bolt_overlays.py`). If the ERROR invariant fires, Bolt probably
   fixed the upstream layout; remove the swap and re-point the labels straight.
3. **`_extract_energy` bi-horaire span** (`_bolt_cards.py`). The two-`Jour Nuit`-subhead anchor is
   fragile; if Bolt reorders the injection/consumption blocks or drops a subhead, the variable path
   raises loud.
4. **`_extract_yearly_fee`** (`_bolt_cards.py`). A phrasing change away from `€ N / mois` raises.
5. **`_extract_injection`** (`_bolt_cards.py`). A relabeled `Injection` header or a third
   consumption-side `Prix mensuel` row shifts the anchor; a new second-column sign convention needs
   the `-?` tolerance revisited.
6. **`_extract_taxes` / `_extract_renewables`** (`_bolt_overlays.py`). Federal levy and
   certificats-verts misses raise; the connection-fee footnote `{0,4}` cap may need widening if Bolt
   adds markers.
7. **URL construction** (`_document_url` `bolt.py`, `_resolve_variable_suffix` `bolt.py`).
   A variable-version bump now resolves itself off the listing, and the live-check freshness gate
   fails the run if it ever stops doing so. What still needs a code change is a change in the
   filename *shape* -- a version that grows a letter, or a folder or slug rename -- which
   `_CARD_URL_RE` (`bolt.py`) would stop matching; `discover` (`bolt.py`) plus the
   live-check coverage diff flag that case.


### Wallonia Tarif Impact

The Walloon variable cards print a `Tarif Impact (Wallonie)` block beside the
standard rates: one row per CWaPE band, each with its printed price, that band's
own quarterly index and the shared formula
(`Eco consommation 9,91 65,59 Belpex * 1,168 + 16,90`).

**The fixed cards print the block too, and it is not read.** `bolt_fix` and
`bolt_plenty_fix` carry the variable card's three rows digit for digit, with the formula
column replaced by `Fixe` (`Eco consommation 9,91 65,59 Fixe`, `Medium consommation 14,64
111,82 Fixe`, `Pic consommation 19,23 140,87 Fixe`), on the September and October 2026
cards alike. A banded fixed price would not quote a quarterly Belpex index beside each
band, so this is a template left over from the variable card rather than an offer, and a
Walloon Bolt Fixe entry on the Impact network mode is billed the one fixed price in every
band (`_extract_energy`, `_bolt_cards.py`, which says so where the fixed branch returns).

This is one product in two network configurations, not a second product, so the
bands live on `VariableRates.impact_*` and are selected by `dso_tariff_mode`
exactly as the DSO side already is. Before this the network leg moved with the
band while the supplier energy stayed on the mono or bi-hourly rate, so the two
halves billed on different schedules: at 3500 kWh that is about EUR 36/yr on a
flat load and EUR 75/yr on the shifted load a customer opts into Impact for.

**The bands are derived from each row's formula and index, never from the printed
column.** At the Q2 2026 indices Eco resolves to 9,912 against a printed 9,91 and
Pic to 19,232 against 19,23, but Medium resolves to 15,636 against a printed
14,64 — a one-digit supplier typo that reading the column would bake into every
Medium hour.

**The bands belong to the monthly settlement, so ticking the quarter-hour box drops
them.** `resolve_settlement_grid` produces `DynamicRates`, which carries no
`impact_*` fields, and it is right that it does: the CWaPE band schedule prices a
month-indexed rate by hour of day, while a quarter-hourly settlement already prices
each quarter at its own Belpex. Measured on the Plenty card in Wallonia at a
0,10 EUR/kWh spot, the energy leg goes from 0,09912 / 0,15636 / 0,19232 by band to a
flat 0,14172. That is not a change of behaviour — the retired `bolt_dynamic`
contract produced exactly the same leg, and a differential across 135 priced
combinations confirms the two are identical — but it is now reachable by ticking a
box rather than by picking a different product, so a Walloon household on the
incitative network tariff should know that its energy stops being banded when it
switches settlement. The network leg and the Walloon terme fixe still follow
`dso_tariff_mode`, which is the connection and does not move with the contract.

**A re-priced card keeps the bands.** The bands come from one formula on three per-band indices,
and the Jour / Nuit rates from per-register ones, so the monthly leg a variable card is re-priced
through (`_cohort_energy_from_archived`, `cohort_legs.py`) carries each as the formula with the
spread the card prints between that index and the mono one (see [Quarterly index](#quarterly-index)).
Before that leg existed, a Walloon Impact entry with a contract start date was billed the mono
formula in every band (on the September 2026 card Eco hours 4,3 c/kWh high, Pic hours 5,1 low)
and a bi-hourly meter the same rate on both registers. A variable card whose Impact bands carry
no formula and that prints no quarterly index table is still left un-re-priced, and the entry
keeps the current card, as one without a start date does.

## Quarterly index

The variable cards are not indexed monthly. They print one formula, `Belpex * 1,168 + 16,90` in
October 2026, beside a table headed `Belpex Q3 2026`: 139,36 EUR/MWh for a single meter, 147,78
day, 132,12 night and exclusive night, and 102,42 / 153,31 / 180,89 for the Eco / Medium / Pic
bands. The card defines the index as "la moyenne pondérée par le RLP des prix par quart d'heure
belges", calls the printed rates "le prix de vente basé sur la valeur Belpex la plus récente", and
bills on "l'indice applicable pendant la période pour laquelle vous êtes facturé". So the printed
`Prix mensuel` is last quarter's index, and the bill is the delivery quarter's.

Of the RLP blends, the Walloon DSOs' curve alone (`rlp_blend = "wallonia"`, the ORES columns,
`synergrid.py`) reproduces every one of the third quarter's six printed indices to 0,01 EUR/MWh
at quarter-hour resolution, in every region; Fluvius misses by up to 1,0 and the distinct-curve
mean by 2,6. The day register is Monday to Friday 07:00 to 22:00 with no public-holiday exception.
The engine's spot cache is hourly, which leaves 139,14 against the printed 139,36 for the third
quarter and 100,00 against 100,09 for the second, 0,03 and 0,01 c/kWh.

`_extract_energy` (`_bolt_cards.py`) marks a variable card that prints the table
`quarter_indexed` and `rlp_indexed` on that blend, and the registry flags the variable contracts
`month_indexed_energy`, so the flow offers the ENTSO-E key. The card leaves `month_indexed`
False: a version that predates `quarter_indexed` reads that flag alone and would re-price the
card on one month's mean with no per-register spread and the Impact bands in every
configuration, so it keeps billing the printed rates instead. With a key,
`_quarter_leg` (`cohort_legs.py`) builds the monthly leg: the formula on the mono index, and each
register and band on the same formula with the spread the card prints between its index and the
mono one, read as its printed rate less the printed mono rate. `_energy_month_spot`
(`spot_stats.py`) resolves it on the quarter's RLP-weighted mean, quarter to date while it runs,
for the year-to-date walk, the backfill and, through `_quarter_index` (`coordinator_spots.py`), the
live price, the projections and the comparison page's own row. A closed month of the quarter too
thinly cached falls back to the month's own mean, so the day-ahead history is fetched from the
quarter's first day even for an entry billing from a later start date (`index_window_start`,
`spot_stats.py`). The Impact bands bill only on the Impact
configuration and a night circuit keeps its own rate (`energy_eur_per_kwh`, `pricing.py`).

The spreads are the last closed quarter's, the only ones the card prints. They moved by 0,6 to
2,4 EUR/MWh between the second and third quarter of 2026: priced on the October card, the second
quarter comes out 0,06 c/kWh high by day, 0,13 low at night, 0,32 low on Eco and 0,26 high on
Medium, against about 4,9 c/kWh on every register in the third quarter before, when the
second quarter's printed rates were billed. Without a
key the printed rates stand. The quarter-hour settlement box is unaffected: it bills the same
formula per quarter-hour.

The table is checked against the price it stands beside. `check_quarter_table` (`_bolt_cards.py`)
puts the table's mono index through the card's formula and refuses the card (an `ExtractorError`,
so the card held before keeps billing) when the result is more than 0,01 c/kWh, one step of the
printed price's last digit, from `Prix mensuel`. The Online card first published for October 2026
printed 19,05 c/kWh, the formula at the third quarter's index, beside the second quarter's table
(100,09 EUR/MWh mono), which prices it at 14,18; its Impact bands were that table's, about 5 c/kWh
below the rest of the card. Bolt replaced the file in place two days later with the third
quarter's table. Plenty Online is checked on the Online card's table, the one behind the formula
it is billed on, so it is refused with it. Every other variable card on file reproduces its price
to 0,005 c/kWh.

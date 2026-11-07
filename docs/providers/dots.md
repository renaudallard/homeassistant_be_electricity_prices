# Provider: dots

This document is the maintainer reference for the Dots Energy tariff-card
extractor (`providers/dots.py`). Dots energy BV, of Zonhoven, sells two
residential electricity products in Flanders, both dynamic and both for a
digital meter read every quarter hour (meetregime 3, an SMR3 meter). Read this
alongside the framework and pricing references:

- [../provider-framework.md](../provider-framework.md): the `SupplierExtractor`
  protocol, the `SupplierSnapshot` / `*Rates` dataclasses, the shared `_pdf`
  helpers, and the fetch / probe / `fetch_for_month` contracts.
- [../pricing-model.md](../pricing-model.md): how `compute_breakdown` consumes
  the energy, DSO, tax, and injection overlays this extractor produces.

## Overview

| Property | Value | Source |
| --- | --- | --- |
| Extractor id | `dots` | `dots.py` |
| Label | `Dots Energy` | `dots.py` |
| Region(s) served | Flanders only | `EXTRACTOR`, `dots.py` |
| Publication form | One PDF card per product and month, read word-aligned | `fetch` |
| Current card | `https://www.dotsenergy.be/api/tariefkaarten/<slug>`, which redirects to the month's PDF | `_card_url` |
| Archive | None: the months before the running one answer 404 | `EXTRACTOR` |
| Probe | None, the coordinator's time-based TTL | `EXTRACTOR` |
| Discovery | The cards the home page `https://www.dotsenergy.be/nl` links | `discover` |

Each product's address answers with a 302 to the month's card,
`/documents/tariefkaarten/tariefkaart-<slug>-MM-YYYY.pdf`, on the same site.
The fetch holds every hop to `www.dotsenergy.be` (`hosts`, through
`checked_request` in `_pdf.py`), where the cards have always been served, so
a redirect anywhere else is refused before it is asked. Both products' cards
of October and September 2026 answered 404 on 7 November 2026, so there is no
supplier archive to read a past month from, and `fetch_for_month` is `None`:
past months come from the repository's card archive, which files the card
each day it is served (`scripts/archive_cards.py`), or else the current card
stands in. The gas integration reads Dots's gas card the same way.

`discover` reads the home page's links, each held to the page's own site
(`on_page_site`, `_links.py`), and keeps the ones under
`/api/tariefkaarten/`. A slug no product carries surfaces as `dots_<slug>`,
and the gas card the page links too (`dots-connect-gas-digital`) is left out.
The page also shows SEG - Dynamic Flex, sold only to Smart-E-Grid customers
with steered assets, which links no card and is not a product a household can
choose here.

## Contracts

| Contract id | Label | Slug | Kind | Injection shape |
| --- | --- | --- | --- | --- |
| `dots_dynamic` | Dots Dynamic | `dots-dynamic` | `dynamic`, quarter-hourly | quarter-hourly formula |
| `dots_dynamic_insights` | Dots Dynamic Insights | `dots-dynamic-insights-elektriciteit` | `dynamic`, quarter-hourly | quarter-hourly formula |

Both cards say their prices apply only to a meter on meetregime 3 and are
worked out "op basis van kwartierprijzen op de EPEX SPOT BE groothandel", so
`DynamicRates.quarter_hourly` is set and the live table, the slot sensors and
the cheapest-window service keep ENTSO-E's 15-minute slots. A household whose
meter does not send its data is billed advances on its RLP, settled later on
the real data, which is not modelled.

## Energy and feed-in

The first page prints one price row, the offtake then the feed-in, each
formula beside the price the card works out for it:

```
Elektriciteit 1 * BELPEX + 1,25 19,253 Elektriciteit 1 * BELPEX - 1,25 8,127
```

on Dynamic, and on Insights:

```
Elektriciteit 1 * BELPEX + 1,5 19,503 Elektriciteit 1 * BELPEX - 1,5 7,877
```

The footnotes state each formula's unit: "Tariefformule afname: in c€/kWh
excl. BTW" and "Tariefformule injectie: in c€/kWh (BTW n.v.t.)". BELPEX is in
c€/kWh too, so the factor applies as printed to a spot in EUR/kWh and the base
is divided by 100. The offtake is grossed by the 6% VAT the card states; the
feed-in carries none. The rate is read off the header alone ("Alle prijzen
zijn inclusief 6% BTW"): the Insights dongle is "inclusief 21% BTW", and a
reworded header leaves the rate assumed, which the live check reports, rather
than grossing by 21%. A formula in any other unit or basis is refused.

The same footnotes name the VNR year price each printed price is worked out
at ("11/2026: c€18,003/kWh" for the offtake and c€9,377 for the feed-in), and
the reader holds each formula to it: at that price it has to give back the
printed figure to the rounding (18,003 + 1,25 is 19,253, 9,377 - 1,25 is
8,127). That pins the units: the OCTA+ cards of November 2026 changed theirs
under formulas that kept parsing (see [octaplus.md](octaplus.md)), and a Dots card doing
the same is refused rather than billed a factor of ten out.

### The balancing contribution (Insights)

The Insights card adds an evenwichtsbijdrage to each leg:

```
Evenwichtsbijdrage afname (4) 0,005 * BELPEX + 0
Evenwichtsbijdrage injectie (4) 0,005 * BELPEX + 0
```

Its footnote says the formula is the upper bound of what is billed
("de bovengrens van de te factureren bijdrage"), that a lower real cost comes
back as a discount, and that it is printed before VAT. It is billed at that
bound, folded into each formula: the offtake becomes 1,005 x BELPEX + 1,5
before VAT, grossed like the rest, and the feed-in 0,995 x BELPEX - 1,5. Its
printed prices leave it out (19,503 is 18,003 + 1,5), so at November's VNR
prices the offtake is billed 0,090 c€/kWh more than that figure before VAT,
and the feed-in credited 0,047 c€/kWh less.

The fold follows BELPEX through zero, as the formula reads: on a negative
price the contribution is negative too, so it lowers the offtake price and
raises the feed-in credit by half a percent of the price. The feed-in leg
carries no VAT in the model, so the 6% on its contribution is not billed:
0,003 c€/kWh at a BELPEX of 10 c€/kWh.

A card naming a contribution and printing no formula for it, or not stating
that it is before VAT, is refused. The Dynamic card prints none.

### The standing charge

"Vaste vergoeding €5,3/maand/EAN" on Dynamic and €7,95 on Insights, billed
per day of the month. Read as printed, VAT included, times twelve: 63,60 and
95,40 EUR a year. Both are a round 5,00 and 7,50 before VAT, and the gas card
prints the same €5,3 under the same statement. On the Dynamic card the figure
sits on the line above its label, so it is read by its unit (`/maand/EAN`).
Ending the contract in its first six months owes the fee to the sixth month,
which is not modelled.

## Network and taxes

The second page prints the digital-meter network table of the eight Fluvius
areas: data management (18,92 €/year for a quarter-hour reading), the capacity
rate (€/kW/year), and the normal and exclusive-night rates (c€/kWh),
transmission included. They are the figures every other Flemish card prints,
VAT included. The page's rotated "Vlaanderen" label is read backwards in
front of the row it sits level with (Imewo's), and is taken out before the
rows are read (`numeric_row`, `_parse.py`). An area whose row does not read is
left out and `fetch` refuses the card (`require_every_area`): with no archive
of its own the supplier never keeps a short table. The VREG ceiling is printed
in a footnote the shared reader does not match ("maximumtarief van
€0,3472738/kWh"), so it is filled from Fluvius's sheet
(`resolve_vreg_network_ceiling`).

The levies are read through `regional_tax_overlay`, VAT included:

| Row | November 2026 | Billed |
| --- | --- | --- |
| Bijzondere accijns op Energie, Standaard tarief | 4,8760 c€/kWh | the law's rate (`resolve_federal_excise`): the card prints October's |
| Bijdrage op Energie, Standaard tarief | 0,2042 c€/kWh | nothing, abolished from August 2026 (`resolve_federal_contribution`) |
| Bijdrage Energiefonds Residentieel, Standaard tarief | 0,00 €/month | as printed, a domiciled household's |
| Kosten groene stroom | c€1,166/kWh (11% x C x €106/st.) | as printed |
| Kosten WKK | c€0,4304/kWh (14% x C x €30,74/st.) | as printed |

The certificate costs are taken as printed, including VAT, like every other
figure on the card: the certificate prices they are worked out at, €106 and
€30,74, are 100 and 29 before VAT. The stale excise and the contribution are
listed among the live check's known tax blocks.

The plain pypdf reading of the second page detaches figures from their rows
(Antwerpen's and Imewo's among them) and every levy's figure from its label,
so the card is read word-aligned (`fetch_pdf_text_aligned` with OCTA+'s word
joining), which keeps every row whole. The plain reading is refused: its price row is split over lines.

## Not modelled

- The Dynamic Green option, c€0,419/kWh on top of the energy price for the
  guarantees of origin. It is an extra a household opts into, and the
  integration has no setting for one; an entry on it is billed that much
  short.
- Insights' P1 dongle, €49,99 once including 21% VAT.
- Energiedelen, 12,1 € a month, billed only to a household sharing energy.
- The Connecting the Dots referral credit, €1,50 a month per contract a
  customer brings in.
- SEG - Dynamic Flex, which publishes no card.

## Tests

`tests/test_dots.py` reads the November 2026 cards
(`tests/fixtures/dots_dynamic_2026-11.pdf`,
`tests/fixtures/dots_dynamic_insights_2026-11.pdf`) and holds both formulas
to the prices they print, the balancing contribution's fold and sign, the
network table and levies, the refusals (another product's card, another
unit or basis, a price check that does not hold, a contribution with no
formula or basis, the plain reading, a short network table) and the time of
every pattern on crafted text. `tests/test_redirect_hops.py` drives the
fetch over a local TLS server: a redirect on the site is followed and one
off it is never asked. `discover` is read against the saved product section
of the home page (`tests/fixtures/discover/dots.html`).

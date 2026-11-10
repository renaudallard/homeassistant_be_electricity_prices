# Provider: belvus

This document is the maintainer reference for the Belvus tariff-card
extractor (`providers/belvus.py`). Belvus BV, of Antwerp, sells electricity
and gas in Flanders; this extractor reads its three residential electricity
products. The gas integration reads its gas cards. Read this alongside the
framework and pricing references:

- [../provider-framework.md](../provider-framework.md): the `SupplierExtractor`
  protocol, the `SupplierSnapshot` / `*Rates` dataclasses, the shared `_pdf`
  helpers, and the fetch / probe / `fetch_for_month` contracts.
- [../pricing-model.md](../pricing-model.md): how `compute_breakdown` consumes
  the energy, DSO, tax, and injection overlays this extractor produces.

## Overview

| Property | Value | Source |
| --- | --- | --- |
| Extractor id | `belvus` | `belvus.py` |
| Label | `Belvus` | `belvus.py` |
| Region(s) served | Flanders only | `EXTRACTOR`, `belvus.py` |
| Publication form | One PDF card per product and month, read with pypdf | `fetch` |
| Card path | `https://www.belvus.be/public/tariefkaarten/<YYYY-MM>/Tariefkaart_<Product>_EL.pdf` | `_card_url` |
| Current card | The newest month `/diensten` or `/historische-tariefkaarten` links, at the link it carries | `fetch`, `_listed` |
| Archive | The same path for any month; read from September 2026 | `fetch_for_month` |
| Probe | The newest card's link, whose `?v=` hash changes when the card is uploaded again | `probe` |
| Discovery | The residential cards `/diensten` links | `discover` |

`/diensten` links every product's current card and `/historische-tariefkaarten`
the months before; on 3 November 2026 the first already linked the November
cards while the second still ended with October, so both are read and the
current cards page wins a month both list. Each link carries `?v=` and the
first twelve hex digits of the file's SHA-256. The cards are served
`Cache-Control: public, max-age=31536000, immutable`, and the November 2026
Dynamisch card was uploaded again on 10 November under the same name, so a
card is fetched at the link the page carries, and only at its bare path
where no page links the month. Only the path and the hash are taken from a
link (`_LINK_RE`), so a card is always fetched from www.belvus.be.

## Contracts

| Contract id | Label | File | Kind | Injection shape |
| --- | --- | --- | --- | --- |
| `belvus_dynamisch` | Belvus Dynamisch | `Dynamisch` | `dynamic`, quarter-hourly | quarter-hourly formula |
| `belvus_flex_online` | Belvus Flex Online | `FlexOnline` | `variable`, month-indexed | SPP-indexed monthly formula |
| `belvus_smart_plus` | Belvus Smart Plus | `SmartPlus` | `variable`, month-indexed | SPP-indexed monthly formula |

Dynamisch is sold since September 2026, for a communicating digital meter on
SMR3. Flex Online and Smart Plus are for an analog meter or a digital one on
SMR1. The `<Product>Pro` cards beside them are professional and are not read:
their header says "Professioneel", which the reader refuses. FlowPlus, listed
from January to March 2026, is no longer sold.

The header of every page after the first names the product, "Particulier
Elektriciteit Belvus Dynamisch", then an em dash and "November 2026", and
the reader holds it to the contract asked for (`_card_month`). The product is
spelt five ways on the September to November 2026 cards: "Dynamisch",
"Belvus Dynamisch", "FlexOnline", "FlexOnline EL" and "Flex Online EL";
"Belvus " in front, " EL" behind and the blanks are taken out before the
comparison.

## Energy and feed-in

| Contract | Energy, before VAT | Feed-in | Standing charge |
| --- | --- | --- | --- |
| Dynamisch | `(Belpex_15M × 1,01) + € 7,50/MWh` | `(Belpex_15M × 1) + € -13/MWh` | € 8/jaar |
| Flex Online | `(Belpex_RLP × 1,21) + € 4/MWh` | `(Belpex_SPP × 1) + € -20/MWh` | € 50/jaar |
| Smart Plus | `(Belpex_RLP × 1,1) + € 19/MWh` | `(Belpex_SPP × 1) + € -20/MWh` | € 130/jaar |

Every formula is in EUR/MWh on an index in EUR/MWh, so against a spot in
EUR/kWh the factor keeps its value and the base is divided by a thousand.
The energy block is headed "Tarieven excl. btw" and the cards state no VAT
rate, so the energy formulas and the printed energy price are grossed by the
residential 6%, recorded as assumed (`assumed_vat_rate`) so the delivery
month's rate replaces it. The feed-in is VAT-exempt and the standing charge
is printed "(incl. btw)".

Each block prints, beside its formula, the price it gives at the month
before's Belpex RLP-M (energy) or SPP-M (feed-in), with a "**", and names
that value in its footnote: "Belpex RLP-M oktober 2026 van 181,836 EUR/MWh"
on Dynamisch's card, "laatst gekende waarde van Belpex RLP-M 10/2026:
€181,836/MWh" on the others. Every formula is held to that price within half
a cent (`_leg`), which pins the unit and the scale; a formula on another
index than the product's, or another formula for the energy anywhere outside
the feed-in block (the third page restates it), is refused.

Dynamisch bills each quarter hour on its own day-ahead price
(`quarter_hourly`), the feed-in too, so its printed prices are no rate.
Flex Online and Smart Plus bill the delivery month's mean: `month_indexed`,
with the printed price as the fallback for an entry without day-ahead prices.
The energy is weighted by Synergrid's RLP, "RLP_BE ... het rekenkundige
gemiddelde van de profielen van de distributienetbeheerders in Vlaanderen".
Against the integration's four blends on the January to October 2026 prices,
Belvus's printed RLP-M ran 0,09 to 0,27 EUR/MWh above the column-weighted
mean in every month, closer and steadier than the others (Fluvius alone ran
up to 0,87 off), so the leg is `rlp_blend="columns"`, the blend Frank's
RLP_BE follows too. The feed-in's SPP-M is the series EBEM, Elegant and
Trevion publish (79,108 for August 2026), `spp_indexed`.

A closed month of Flex Online and Smart Plus is settled on the next card
(`_settle_on_next_card`): its RLP-M and SPP-M, checked to name the month,
rebuild both legs (`settled_energy`, `settled_injection`). While the next
card is not out, or names another month, the month is `provisional`.
Dynamisch asks for no second card.

## Network and taxes

The second page prints the digital-meter network table of the eight Fluvius
areas, capacity (€/kW/year), normal and exclusive-night rates (c€/kWh), and
the third the classic meter's, whose first column, the prosumer rate
(€/kVA/year), is read with it. The data management fee, € 18,92/jaar, is read
from the classic table's heading, the one figure for every meter. Every
figure is VAT-inclusive. The classic table wraps "Fluvius Midden-" onto the
line of its figures, which is joined before the rows are read. An area whose
digital row does not read is left out and `fetch` refuses the live card
(`require_every_area`).

Four figures on every card from September to November 2026 disagree with
Fluvius's 2026 sheet and with every other supplier's card. They are billed as
printed, and the two the live check's network consensus reports are listed
among its known network figures:

| Area | Field | Belvus | Fluvius, incl. 6% | Reported |
| --- | --- | --- | --- | --- |
| Fluvius West | exclusive night | 5,6 | 5,9928 | yes, allowed |
| Fluvius Imewo | normal | 5,42 | 5,5424 | yes, allowed |
| Fluvius Midden-Vlaanderen | exclusive night | 4,79 | 4,7783 | within tolerance |
| Fluvius West | capacity | 60,52 | 60,5255 | within tolerance |

The levies are read through `regional_tax_overlay`, VAT included:

| Row | November 2026 | Billed |
| --- | --- | --- |
| Bijzondere accijns | 5,0329 c€/kWh | the law's rate (`resolve_federal_excise`): the card prints the first band of before August 2026 |
| Bijdrage op de energie | 0,2042 c€/kWh | nothing, abolished from August 2026 (`resolve_federal_contribution`) |
| Bijzondere Energiefonds, laagspanning residentieel | € 0 a month | as printed; the non-residential row above it is not read |
| GSC, groenestroomcertificaten | 1,18 c€/kWh | as printed |
| WKK, warmte-krachtkoppeling | 0,42 c€/kWh | as printed |

The volume bands in the excise footnote print one rate for the first two and
are not read as bands. The stale excise and the contribution are listed among
the live check's known tax blocks. The VREG ceiling is not printed, so it is
filled from Fluvius's sheet (`resolve_vreg_network_ceiling`).

## Not read

The cards up to August 2026 copied the Energy Together template, which the
gas integration reads for its gas cards, in another layout with no page
header; the reader refuses them, so a month before September 2026 falls back
to the card archive and then to the current card.

## Not modelled

- Energiedelen, € 100 a year, billed only to a connection sharing energy.
- Ending a contract in its first six months owes six months of the standing
  charge.
- A digital meter that does not communicate is billed the classic meter's
  network tariffs, and Dynamisch without SMR3 may be moved to a variable
  product: both are the entry's own setup to describe.

## Tests

`tests/test_belvus.py` reads the November 2026 cards of the three products,
Flex Online's October card and Dynamisch's September one
(`tests/fixtures/belvus_*.pdf`), and holds every formula to the price its
card prints at the index it names, the network table as printed, the levies,
the refusals (another product's card, a professional card, another index,
unit or basis, a price check that does not hold, a second energy formula, the
older template), the settlement of a closed month on the next card and the
provisional month, the links the pages carry and the time of every pattern
on crafted text. `discover` is read against the saved card links of
`/diensten` (`tests/fixtures/discover/belvus.html`).

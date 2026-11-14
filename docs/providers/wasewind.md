# Provider: wasewind

This document is the maintainer reference for the Wase Wind tariff-card
extractor (`providers/wasewind.py`). Wase Wind cv, of Sint-Gillis-Waas, is a
cooperative selling the power of its wind parks to households in eighteen
municipalities of East Flanders and Antwerp's left bank (listed on
`/klant-worden/gezin`), with three residential products in 2026 and a fourth,
Vast, from 2027. Read this
alongside the framework and pricing references:

- [../provider-framework.md](../provider-framework.md): the `SupplierExtractor`
  protocol, the `SupplierSnapshot` / `*Rates` dataclasses, the shared `_pdf`
  helpers, and the fetch / probe / `fetch_for_month` contracts.
- [../pricing-model.md](../pricing-model.md): how `compute_breakdown` consumes
  the energy, DSO, tax, and injection overlays this extractor produces.

## Overview

| Property | Value | Source |
| --- | --- | --- |
| Extractor id | `wasewind` | `wasewind.py` |
| Label | `Wase Wind` | `wasewind.py` |
| Region(s) served | Flanders only | `EXTRACTOR`, `wasewind.py` |
| Publication form | One PDF card per product and month, read layout-preserving | `fetch` |
| Listing | `https://www.wasewind.be/tariefkaarten-<year>-incl.-btw-<product>`, one page per product and year | `_listing_url` |
| Archive | Every card of the year the listing links | `fetch_for_month` |
| Probe | The address of the card `fetch` reads | `probe` |
| Discovery | The product pages `/klant-worden/gezin/tarieven/tarieven-<year>-gezin` links, for the running year and the next one once its page is up | `discover` |

The listing links each month's card as
`/frontend/files/userfiles/files/[<folder>/...]Tariefkaart WW-<code>-HH-<YYMM>.pdf`,
the folder changing over the year (none, `Tariefkaarten/` and
`Tariefkaarten/Tarieven 2026/2026-09/` in 2026), and once with a `(1)` after the
number. Only a link of that shape, on the site, of the product's code and of
the page's year is read (`listed_cards`). `fetch` takes the newest card up to
the running month, never one published ahead of it. A card numbered `00` is the
year's card published before the year starts (`2700` in November 2026): it
prices January until January's own is listed. A product not sold yet (Vast,
`since=2027`) is fetched on its first card until its year starts
(`_fetched_month`), so an entry on it in November 2026 prices on the January
2027 card, and `fetch_for_month` has no card for a month before it.

## Contracts

| Contract id | Label | Code | Kind | Injection shape |
| --- | --- | --- | --- | --- |
| `wasewind_vol_dynamisch` | Vol dynamisch | `DY` | `dynamic`, quarter-hourly | quarter-hourly formula |
| `wasewind_vast_dynamisch` | Vast-dynamisch | `VD` | `dynamic`, quarter-hourly | fixed (a quarter-hourly formula from 2027) |
| `wasewind_vast_variabel` | Vast-variabel | `VV` | `variable`, month-indexed | fixed (a month formula from 2027) |
| `wasewind_vast` | Vast | `V` | `fixed`, from 2027 | fixed |

Vol dynamisch and Vast-dynamisch are for a digital meter on meetregime 3 and
price each quarter hour on its Belpex Day Ahead price; Vast-variabel takes any
meter and the delivery month's mean of the day-ahead prices
(`month_indexed`, the plain mean). Vast takes an analog meter or a digital one
on SMR1 or SMR3. A connection that shares energy is always on Vol dynamisch,
which the other cards say.

## Energy and feed-in

Every figure is printed in c€/kWh including the 6% VAT the header states
("Particulier incl. 6% btw tenzij anders vermeld"), the feed-in without VAT
("btw niet van toepassing", "btw n.v.t."), and the index in EUR/MWh, which the
footnote defining it says. The 2026 formulas:

```
Vol dynamisch   Alle meetmomenten 0,106 x Belpex kwartier + 1,59
                Alle meetmomenten 0,10 x Belpex kwartier - 1,50   (feed-in)
Vast-dynamisch  Alle meetmomenten 0,106 x 50% x Belpex kwartier + 7,72
                Vaste injectievergoeding: 2,0 c€/kWh
Vast-variabel   Enkelvoudig, piek en dal 0,122 x 50% x Belpex maand + 7,72
                Uitsluitend nacht 0,106 x 50% x Belpex maand + 6,10
                Vaste injectievergoeding: 2,0 c€/kWh
```

January 2026's Vast-variabel card printed 0,138 for the first formula; every
other term is the same on every card of the year. A factor in c€/kWh on a
Belpex in EUR/MWh is ten times the factor on a spot in EUR/kWh, so Vol
dynamisch's offtake is 1,06 x spot + 0,0159. Each card works every formula out
at the Belpex the VNR estimated for its month and prints the price, and the
reader holds each formula to it to four decimals (`_formulas`): 0,106 x
180,03122 + 1,59 is the 20,6733 of November 2026. The prices are printed with
a comma or a point, and the estimate on its own line on some cards. Vast-variabel's
indicative rates are those prices, re-priced on the delivery month's mean
once its spots are held.

The 2027 cards, published in November 2026, are another layout: "Enkelvoudig,
piek en dal 0,106 x Belpex kwartier + 0,69", no "50%" printed (Vast-dynamisch's
0,053 is the 2026 share folded in, Vast-variabel's 0,0583 a new factor),
and the feed-in of Vast-dynamisch and Vast-variabel on a formula
(`0,05 x Belpex kwartier + 2` and `0,02 x Belpex maand + 2`). They are read
the same way, and a month formula for the feed-in is `month_indexed`.

Vast's 2027 card prints its prices and no formula: "Enkelvoudig, piek en dal
20,00" and "Uitsluitend nacht 16,00" under "Energiekost in c€/kWh", and the
feed-in "Alle meetmomenten 4,00" under "Injectievergoeding in c€/kWh". Each is
read once, under that heading, and a formula on the card refuses it
(`_fixed_prices`). It names no month, only the year it applies over ("PERIODE
VAN 01/01/27 TOT 01/01/28"), so it prices the month the listing gives it,
which has to fall in that year (`listed_month`).

The month a card prices is the one its offtake estimate names ("in november
2026", or "in november," with no year on Vast-variabel's), in the year of the
period it applies to ("in de periode 01/01/2026 tot 01/01/2027", "01/01/27 TOT
01/01/28"). An estimate of the year before is the card published ahead of its
year, which prices January.

The subscription is 65 € a year on all four, and the "Tariefelement analoge
meter + pv" of 30 € per kVA a year Vast-variabel and Vast print is read into
`supplier_prosumer_eur_per_kva_year`, which is billed only under the Walloon
compensation regime, as Elegant's is.

## Network and taxes

The cards print no network table and no levy but the certificates: "Tarieven
exclusief netkosten, heffingen en toeslagen". So:

- **Network.** Every area's rates come off Fluvius's sheet for the card's year
  (`fluvius.ensure_area_terms`, `held_area_terms`), the peak-metered column a
  custom entry can take, grossed at the card's 6%. They are the figures every
  other Flemish card prints: Dots's November 2026 table less VAT is each
  area's sheet to the card's rounding. The card is refused while an area has
  none. Fluvius had not listed its 2027 sheets on 2026-11-14, so a 2027 card
  stands on the 2026 sheets until it does and records it (`network_year`);
  the contracts carry `earlier_network_cards`, so a month of 2027 is billed
  its own sheet once read (`past_last_card`, `snapshot_resolve.py`). The VREG
  ceiling and an analog meter's surcharge come off the sheets as on every card.
- **Federal levies.** The excise and the energy contribution are left at 0
  with `TaxOverlay.federal_levies_unread`, and the resolver bills the delivery
  month's from the law: the archived wording's schedule and contribution up to
  July 2026, the flat rate of the text in force from August
  (`_stand_in_levies`, `resolve_federal_excise`). Until the law is read the
  entry publishes nothing, with the `excise_law_unread` Repairs card in its own
  wording.
- **Certificates.** "Groene stroom certificaten 1,13" and "Warmtekracht
  certificaten 0,33" c€/kWh, VAT included, into `flanders_renewables`.
- **Energy fund.** Not printed, and 0 €/month: the Flemish fund charges a
  domiciled household nothing in 2026, as every Flemish card that prints the
  row says.

The live check reads all four cards daily, and expects Vast-dynamisch's and
Vast-variabel's feed-in in the shape of the card's year
(`_INJECTION_SHAPE_FROM_YEAR`). The rows of these cards cast no
vote in the network consensus (`_SHEET_NETWORK_SUPPLIERS`), since they carry
the sheet itself, and none in the federal tax consensus, whose block they do
not print.

## Not modelled

- Energiedelen, 136,98 € a year including 21% VAT, billed pro rata only while
  the connection shares energy.

## Tests

`tests/test_wasewind.py` reads ten cards (`tests/fixtures/wasewind_*.pdf`):
Vol dynamisch of January and November 2026, Vast-dynamisch of November 2026,
Vast-variabel of January, June and November 2026, and the four 2027 cards. It
holds every formula to the price printed, the network to the eight 2026 area
sheets (`tests/fixtures/fluvius_*_2026.xlsx`), the levies to the law on a
resolved card, the refusals (another product's card, a formula off its price,
no VAT rate, no feed-in basis, an index not in EUR/MWh, no certificates or
fee, a short network, and on Vast a formula, a missing price, a unit other
than c€/kWh or a period of more than a year), the listing of
Vast-dynamisch's 2026 and 2027 pages and Vast's 2027 one, the month `fetch`
and `fetch_for_month` read, Vast's first card read before its year, the
year-before network, `discover` on the 2026 and 2027 household tariffs pages,
and the time of every pattern on crafted text.

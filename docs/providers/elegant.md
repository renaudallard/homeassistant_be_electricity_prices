# Provider: elegant

This document is the maintainer reference for the Elegant tariff-card extractor
(`providers/elegant.py`). Elegant BV sells electricity and gas in Flanders
only; this extractor reads its three residential electricity products. Read
this alongside the framework and pricing references:

- [../provider-framework.md](../provider-framework.md): the `SupplierExtractor`
  protocol, the `SupplierSnapshot` / `*Rates` dataclasses, the shared `_pdf`
  helpers, and the fetch / probe / `fetch_for_month` contracts.
- [../pricing-model.md](../pricing-model.md): how `compute_breakdown` consumes
  the energy, DSO, tax, and injection overlays this extractor produces.

## Overview

| Property | Value | Source |
| --- | --- | --- |
| Extractor id | `elegant` | `elegant.py` |
| Label | `Elegant` | `elegant.py` |
| Region(s) served | Flanders only | `EXTRACTOR`, `elegant.py` |
| Publication form | One text PDF of two pages per product, read with pdfplumber (`fetch_pdf_text_layout`) | `fetch` |
| Current card | `https://cdn.elegant.be/Pricing/TariffCharts/Current/<key>_Residential.pdf`, overwritten in place | `_current_url` |
| Archive | The card in force on the first day of each month, from November 2025 (the launch day, 8 November, for that month), through the site's tariff archive | `fetch_for_month` |
| Probe | The card's `Last-Modified` | `probe` |
| Catalog | The residential electricity links on `https://www.elegant.be/tariefkaarten` | `discover` |

The listing at `elegant.be/tariefkaarten` links the same stable addresses for
the gas cards and for the non-residential edition of each product, which this
extractor does not read. `discover` keeps the `<Product>Electricity_Residential`
links alone and names a product it does not know by Elegant's own key.

## Contracts

| Contract id | Label | Kind | Card key | Injection shape |
| --- | --- | --- | --- | --- |
| `elegant_flex` | Flex | `variable`, `month_indexed` | `FlexElectricity` | EPEXDAMSPP formula plus the printed figure (`spp_indexed`) |
| `elegant_comfortflex` | ComfortFlex | `variable`, `month_indexed` | `ComfortFlexElectricity` | same |
| `elegant_zeker_vast` | Zeker & Vast | `fixed` | `ZekerVastElectricity` | same |

Flex runs for an indefinite term and ComfortFlex for a year; their cards print
the same figures and differ in the heading and in the fixed fee's notice
period alone. Zeker & Vast fixes its energy for a year. All three carry
`spot_indexed_injection`, and the two variable ones `month_indexed_energy`: the
ENTSO-E key is optional, and without it an entry bills the printed rates, which
are the previous month's.

Because Flex and ComfortFlex print identical figures, the card's heading is
checked: its second line names the product ("Flex", "ComfortFlex", "Zeker &
Vast", followed on the archive's copies by the card's month as "09.26", or on
the cards of 8 November 2025 by that day as "08/11/2025") and the third the
segment ("Particulier"). A card for another product or for the
professional edition is refused.

## The indices

Footnote (5) defines both indices on the Belgian day-ahead quotations:
EPEXDAMREK, "het rekenkundig gemiddelde van de quoteringen voor Day Ahead EPEX
Spot Belgium", for consumption, and EPEXDAMSPP, "de SPP gewogen quoteringen",
for feed-in. Neither is known before the month closes, so the card prints its
rates at the last value known and names it: "namelijk de waarde van september
2026. Deze bedroeg 15,641 c€/kWh voor afname en 10,053 c€/kWh voor injectie".
The Zeker & Vast card names the feed-in value alone.

Elegant publishes both series behind `elegant.be/indexatie`, the page the
footnote refers to (the site's `energyExchanges` tRPC calls, in EUR/MWh). They
were checked against the other suppliers' published indices on 2026-10-16:

- EPEXDAMREK for August 2026 is 129,3175 EUR/MWh, the plain monthly mean
  Aspiravi prints for the same month (129,32), which the integration's own
  monthly mean reproduces. So `rlp_indexed` stays off and the coefficients
  resolve on the plain mean.
- EPEXDAMSPP is the series EBEM and Trevion publish from May 2026 on (42,37,
  70,89, 62,65, 79,11 and 100,53 EUR/MWh for May to September 2026, and EBEM's
  27,95 for April). It weights each quarter-hour by its own solar share, and
  sits about 0,9 EUR/MWh below the hourly SPP mean the integration computes.

A closed month is settled on the values the next card names for it (see
[Archive](#archive)), so only the running month bills on the integration's
own means, with an ENTSO-E key.

## Energy

The consumption formulas are printed once per register, in c€/kWh against the
index in c€/kWh and with the VAT inside:

```
24h          (1,0800 x EPEXDAMREK + 1,850) x 1,06
Piek         (1,0920 x EPEXDAMREK + 1,850) x 1,06
Dal          (1,0680 x EPEXDAMREK + 1,850) x 1,06
Excl. nacht  (1,0680 x EPEXDAMREK + 1,850) x 1,06
```

Against a mean in EUR/kWh a factor keeps its value and a base goes from cents to
euros; both are grossed by the multiplier the formula prints rather than by the
header's rate. Every register is read on its own row and a missing one is
refused, since each bills its own pair. The coefficients moved in August 2026
(1,1419 / 1,1632 / 1,1206 and a base of 1,987 before), so none is pinned.

The printed rates are the "Maandelijkse prijzen" row, read from the block above
the "Geschatte jaarprijs" one: the latter is a yearly estimate on the
regulator's method and no price anybody is billed. At September's 15,641 the
formulas give back every printed rate to the cent it is printed to (19,87,
20,07, 19,67 and 19,67 c€/kWh on the October 2026 card).

Zeker & Vast prints the same row with no formula behind it, which is read as a
`FixedRates` leg. The fixed fee is 75 EUR/yr on the variable cards and 120 on
Zeker & Vast. The "Energiedelen" fee (75 EUR/yr) applies only to energy sharing
and is not read.

## Feed-in

`0,8500 x EPEXDAMSPP - 1,500` in c€/kWh on all three cards, VAT-exempt (footnote
(2)), printed for the 24h, Piek and Dal registers alike beside the figure at
last month's index. The leg carries the formula, the printed figure as
`current` and `spp_indexed`. A field holds one credit for every register, so a
card that gives a register a formula or a figure of its own is refused. The
base was -2,000 until March 2026, -1,000 from April and -1,500 from August.

## Network and taxes

The network table prints the eight Fluvius areas as `FLUVIUS_CARD_LABELS` spells
them, Midden-Vlaanderen cut short to "Fluvius Midden-Vl.". Each row is read with
`numeric_row` as seven figures: the digital meter's capacity, distribution and
exclusive-night rates, then the classic meter's three and its prosumer rate, of
which only the prosumer rate is kept. The data management fee is one row for
every area. On the October 2026 card every figure matches the Fluvius rows
Aspiravi, EBEM, Energy Knights and Trevion print that month. A row that does
not read leaves its area out of the table. `fetch` then refuses the live card
rather than adopt it with a hole in it, since an entry on the missing area
would fail every tick until the next good card (`require_every_area`,
`_parse.py`), and `fetch_for_month` refuses the running month's the same way
(`running_month_short_of_an_area`), so the last good card stands in. A past
month's card keeps its short table: its energy is what a contract signed that
month is billed on, and the month walk drops the month on the missing area
alone. Neither check applies to the next card a closed month is settled on,
which is read for its indices only
(`test_a_past_month_keeps_a_short_network_table`, `tests/test_elegant.py`).

The tax block goes through `regional_tax_overlay`: the excise row opens a table
by annual volume (read into bands while its rates differ, as before August
2026), green power and WKK are summed into `flanders_renewables` (1,17 and 0,42
c€/kWh, VAT-inclusive like the rest of the card), the energy contribution and
the residential energy fund row are optional. The excise is billed from the law
whatever the card prints (`resolve_federal_excise`).

"Forfait zonnepanelen" (4,00 EUR/kVA a month) is owed only on a compensation
meter and is kept as `supplier_prosumer_eur_per_kva_year`; the integration
bills the compensation regime in Wallonia alone. The card prints no VREG
maximum tariff, which `resolve_vreg_network_ceiling` supplies.

## Archive

The site's `tarief-archief` page queries the tRPC call
`api/trpc/tariefArchief.search` with a date, the energy type and the customer
type, and gets the product offers in force on that date, each keyed
`<key>_<YYYYMMDD>` and carrying a `tariffChartUrl`; `api/tarief-archief/tariff-chart`
turns that path into the PDF. `fetch_for_month` asks for the first day of the
month, so it reads the card in force on the 1st, and passes it through
`archive_validity_check`, which refuses a card of another month: asked for a
date ahead, the archive offers the card in force today. The archive's copies
carry the month after the product's name ("Flex 09.26"), or on the launch
cards the day the offer started ("Flex 08/11/2025"), which the heading reader
allows. The same approach reads Elegant's gas cards in the sibling gas
integration.

The current product keys start on 8 November 2025. On 1 November the archive
still offers the products Elegant sold before (`BEGreenFlexStroom` and the
like, a combined electricity and gas card of another layout), which no
contract here matches, and on the 30th the three current ones alone. So where
the 1st has no offer for the product, `_first_offer_in` asks about the month's
last day and, when that has one, halves the days between to find the first day
that offers it: at most seven searches, and November 2025 is read on the cards
of the 8th (`FlexElectricity_20251108` and its two siblings). A contract
signed later that month is billed on them rather than on each month's card.
The months before November 2025 offer none of the current products on either
day, are `None` and fall back to the proxy. A product's offer can change
within a month: Zeker & Vast was reissued on 6 and 21 March 2026, and the
month is billed on the card of the 1st.

A month is then settled on the next card. `published_indices` reads the
sentence naming the month the printed rates were computed on and its indices,
EPEXDAMREK and EPEXDAMSPP, or on Zeker & Vast the feed-in one alone. When the
next month's card names this month, Flex and ComfortFlex rebuild every register
and the credit on them (`settled_energy`, `settled_injection`) and Zeker & Vast
its credit; while that card is not out, or names another month, or a variable
card names a single figure that could be either index, the month comes back
`provisional`. Checked on 2026-10-16, every month from November 2025 to
September 2026 of all three products settles on the values Elegant publishes
at `elegant.be/indexatie`, to the rounding of the three decimals the card
prints.

## Tests

`tests/test_elegant.py` runs against four cards:

- `elegant_flex_2026-10.pdf`, `elegant_comfortflex_2026-10.pdf` and
  `elegant_zeker_vast_2026-10.pdf`: the October 2026 cards, every field read,
  and the formulas reproducing the rates the Flex card prints at September's
  indices.
- `elegant_flex_2025-11.pdf` and `elegant_zeker_vast_2025-11.pdf`: the cards
  of 8 November 2025, when the current products were launched, with the day
  after the product's name, and the launch month read on them and settled on
  the indices the December card names.
- `elegant_flex_2025-12.pdf`: the December 2025 card as the archive serves it,
  with the month after the product's name, the volume table of the excise and
  the energy contribution still printed.
- `elegant_flex_2026-09.pdf` and `elegant_zeker_vast_2026-09.pdf`: the
  September 2026 cards as the archive serves them, settled on the indices the
  October cards name, the running month left provisional, and a next card
  naming another month, or no index, settling nothing.

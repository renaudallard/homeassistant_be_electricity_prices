# Provider: aspiravi

This document is the maintainer reference for the Aspiravi Energy tariff-card
extractor (`providers/aspiravi.py`). Aspiravi Energy NV sells one residential
product, Eco Plus Flex, to members of its partner cooperatives (Limburg wind,
Aspiravi Samen, ECO2050), in Flanders only. Read this alongside the framework and pricing references:

- [../provider-framework.md](../provider-framework.md): the `SupplierExtractor`
  protocol, the `SupplierSnapshot` / `*Rates` dataclasses, the shared `_pdf`
  helpers, and the fetch / probe / `fetch_for_month` contracts.
- [../pricing-model.md](../pricing-model.md): how `compute_breakdown` consumes
  the energy, DSO, tax, and injection overlays this extractor produces.

## Overview

| Property | Value | Source |
| --- | --- | --- |
| Extractor id | `aspiravi` | `aspiravi.py` |
| Label | `Aspiravi Energy` | `aspiravi.py` |
| Region(s) served | Flanders only | `EXTRACTOR`, `aspiravi.py` |
| Publication form | One monthly PDF card, a Word export read with pypdf | `aspiravi.py` |
| Current card | The link labelled "Huidige tariefkaart" on `https://aspiravi-energy.be/downloads/` | `_current_card_url` |
| Archive | Every past card on `https://aspiravi-energy.be/tariefkaarten/`, labelled by month | `fetch_for_month` |
| Probe | The current card's `Last-Modified` | `probe` |

The card URLs are WordPress uploads whose names follow no pattern
(`...-formule-2026-sep.pdf`, `...-formule-202603.pdf`, `...-juni-25.pdf`), so
both pages are scraped rather than a URL built. The listing pages send no
`Last-Modified` or `ETag`, which is why the probe reads the downloads page to
find the card and asks the card itself. The WordPress REST API is closed
(401), so it cannot list the uploads instead.

## Contract

| Contract id | Label | Kind | Injection shape |
| --- | --- | --- | --- |
| `aspiravi_eco_plus_flex` | Eco Plus Flex | `variable`, `month_indexed` | month-indexed formula plus the printed indicative (`month_indexed`) |

`month_indexed_energy` and `spot_indexed_injection` are both set: the energy
and the credit are indexed on the delivery month's mean, and the card prints
last month's rates. The ENTSO-E key is optional; without it an entry bills the
printed rates, which are the previous month's.

## The index

The card indexes energy and feed-in on "het rekenkundig gemiddelde van de
Belpex 'Day Ahead' kwartierprijs van de maand van levering", the plain
arithmetic mean of the month's day-ahead prices. Checked against the table of
the last twelve months the September 2026 card prints, the mean of the Belgian
day-ahead prices reproduces every one of the thirteen published months from
August 2025 to August 2026 to the cent, across the switch to quarter-hour
prices in October 2025 (129,317 computed for August 2026 against 129,32
printed). So `rlp_indexed` stays off and no archived month is settled on a
published figure: the mean the coordinator computes is the one Aspiravi bills.

## Energy

The second page prints one formula per meter register, before VAT and before
the charity contribution:

```
Enkelvoudige dagmeter           0,116 * Belpex + 2 (c€/kWh)
Tweevoudige meter       Dag     0,1335 * Belpex + 2 (c€/kWh)
                        Nacht   0,09854 * Belpex + 2 (c€/kWh)
Exclusief nachtmeter            0,09588 * Belpex + 2 (c€/kWh)
Terugleververgoeding            0,07 * Belpex - 2 (c€/kWh)
```

Belpex is in EUR/MWh and the result in c€/kWh, so a factor is multiplied by
10 to meet a spot in EUR/kWh and a base divided by 100, and both are grossed by
the card's VAT ("incl. 6% BTW") onto the VAT-inclusive basis every other figure
is in. The formulas have not changed since April 2024.

The charity contribution ("bijdrage aan een goed doel", 1 EUR/MWh before VAT,
printed as 0,106 c€/kWh with VAT) is part of the energy price by the card's own
words, while both the formulas and the printed rates leave it out: at
August's 129,32, (0,116 x 129,32 + 2) x 1,06 is the 18,021 printed. It is
added to every register's base and printed rate.

The printed rates are read from the front page, one row per register
("Energiekosten dag", "nacht" and "excl. nacht"). The last row of the
"Energiekost van de afgelopen 12 maanden" table carries the month they were
computed on and dates the card: the card is for the month after it. The
sentence naming the month ("afgesloten in september 2026") and the start of the
period the formulas hold for were both left on the previous month on the
September 2025 and March 2026 cards, while the table moved on every time.

The table repeats the four rates too, and those are not read: the copy has been
typed wrong. The March 2026 row prints 12,558 for the single meter where the
front page prints 12,588, which is what the formula gives at the row's own
85,13 ((0,116 x 85,13 + 2) x 1,06 is 12,5876), and every later card repeats the
typo in its table. Read from the table, a keyless entry billed March 0,03
c€/kWh low.

The day formula prints its coefficient rounded: the card's own day rates work
back to 0,13348 against the printed 0,1335, 3e-5 EUR/kWh at August's mean. The
printed formula is what the contract states, so it is what is billed.

The yearly fee row prints one column per meter type. A dual meter's own fee has
no field to go in, so a card printing one is refused; an exclusive-night fee
that differs is kept as `yearly_fixed_fee_exclusive_night`. The "Energiedelen"
administrative cost (125 EUR/yr) applies only to energy sharing and is not
read.

## Network and taxes

The network table prints eight Fluvius rows as "Fluvius (Imewo)" and so on,
read through the shared `FLUVIUS_CARD_LABELS` with the area put in brackets.
Each row carries the data management fee, the digital meter's distribution,
exclusive-night and capacity rates, then the classic meter's four columns, of
which only the prosumer rate is used. The figures include VAT: for Imewo they
match Frank Energie's VAT-inclusive card to the cent.

Green power and WKK are Aspiravi's own certificate costs, printed before VAT
(1,078 and 0,406 c€/kWh on the September 2026 card), so they are grossed by the
card's VAT after `regional_tax_overlay` has read them.

The card still prints the federal excise and energy contribution of before
August 2026 (5,03288 and 0,2042 c€/kWh). Both levies are billed from the law,
and the live check lists the pair among its known tax blocks.

## Archive

Every card from April 2024 on is in the current layout and parses. The cards
before it, back to August 2022, print another layout and are left to the proxy.
The 2024 cards' network tables still name the ten Fluvius areas of that year,
five of which carry the names used today; those cards only ever price months of
2024.

The archive page labels its links "900027 eco plus flex (huishoudelijk) aug 26",
with the month spelled out or cut short and a two-digit year. Two labels are
wrong: "juni 24" and "mei 24" each appear a second time on the 2023 and 2022
cards. `fetch_for_month` tries every link carrying the month and keeps the first
card that `archive_validity_check` accepts. November 2024 is not published.

`discover` reads the product code every card file name starts with, 900027 for
Eco Plus Flex, off both pages. A code no contract carries surfaces as
`aspiravi_<code>`.

## Tests

`tests/test_aspiravi.py` runs against two cards:

- `aspiravi_eco_plus_flex_2026-09.pdf`: the September 2026 card, every field
  read, and the formulas reproducing the rates it prints at August's mean.
- `aspiravi_eco_plus_flex_2026-03.pdf`: the March 2026 card, whose own sentences
  name February, dated by its price table.

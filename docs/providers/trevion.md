# Provider: Trevion

This document is the maintainer reference for the Trevion tariff-card extractor
(`providers/trevion.py`). Trevion publishes one residential PDF per product and
month on a public listing page. All seven products are Flemish and every card
contains the commodity formula, feed-in rate, eight Fluvius rows, and regulated
taxes needed for a complete snapshot.

Read this with [the provider framework](../provider-framework.md) and
[the pricing model](../pricing-model.md).

## Overview

| Property | Value |
| --- | --- |
| Extractor id | `trevion` |
| Label | `Trevion` |
| Region | Flanders only |
| Listing URL | `https://trevion.be/tariefkaarten/` |
| Publication | One PDF per product and month |
| Archive | The listing retains month-stamped cards |
| Probe | The card URL `_find_card` resolves (the listing sends no `Last-Modified` or `ETag`) |
| PDF reader | `fetch_pdf_text_layout` (`pdfplumber`) |

The filenames end in `YYYYMM.pdf` or `YYYYMM-N.pdf`. `_find_card` scrapes the
listing and hands it to `_resolve_card`, which chooses the newest month, or the
one asked for. `_archive_links` reads each link against the listing and keeps
it only on Trevion's own site (`on_page_site`, `_links.py`): every card the
listing has linked, in the card archive and live on 2026-11-06, sits on
`trevion.be`, so a link to another host is not followed. The
split exists so a month and the card that settles it come out of one fetch of
the page both are listed on. `fetch_for_month` uses the same catalog with an exact month filter
and passes the parsed result through `archive_validity_check`.

It then looks one card further and settles the month on the indices that card
names. Both legs price on a Belpex mean of the delivery month, which is not
known while the month runs, so a card prints the last value published instead;
the following one names this month's, with the month spelled out: "De laatst
gekende waarde is deze van augustus 2026 (79,11 EUR/MWh)". The sentence appears
twice in the same words, once for `Belpex_RLP_VL` and once for `Belpex_SPP_BE`,
so each reader is anchored on its own parameter. Only indexed legs ask, so the
fixed and dynamic contracts pay for no second card, and a month whose following
card is not out yet, or names another month or no index for a leg, comes back
`provisional`. A withdrawn product's last month
has no following card of its own, so it is settled on the card of the product
`_ContractDef.successor` names, which prints the same indices: LifePowr's
September 2026 on the October FlexiO Max card.

The card also defines both indices on "het gewogen gemiddelde van de Belgische
kwartierprijzen", the quarter-hour prices, while the integration computes its
means on hourly ones. Over the 2026 months that put the computed credit index
about 0,9 EUR/MWh above the published one and the energy index 0,19 below, which
is what settling on the published figures removes. From May 2026 on, Trevion and
EBEM publish identical values for both indices.

The hourly computation is not simply wrong, though: Energy Knights defines its
own Belpex-SPP-M on the hourly quotation and the same code reproduces its
published series to 0,007%. Resolution is a property of the card, so it is not
changed integration-wide, and only the suppliers that publish a settled value
are settled on it.

## Contracts

| Contract id | Product | Kind | Consumption index | Injection |
| --- | --- | --- | --- | --- |
| `groene_energie_vast` | Groene Energie Vast | `fixed` | Published mono/day/night rates | Published mono/day/night rates |
| `groene_stroom_flex` | Groene Stroom Flex | `spot_monthly` | Belpex_RLP_VL | Belpex_SPP_BE |
| `groene_energie_dynamisch` | Groene Energie Dynamisch | `dynamic` | Belpex 15 MTU | Belpex 15 MTU |
| `groene_energie_dynamisch_plus` | Groene Energie Dynamisch Plus | `dynamic` | Belpex 15 MTU | Belpex 15 MTU |
| `lifepowr` | LifePowr by Trevion | `spot_monthly` | Belpex_RLP_VL (Belpex 15 MTU until May 2026) | Belpex_SPP_BE (Belpex 15 MTU until May 2026) |
| `energreen` | Energreen by Trevion | `dynamic` | Belpex 15 MTU | Belpex 15 MTU |
| `flexio_max` | FlexiO Max by Trevion | `spot_monthly` | Belpex_RLP_VL | Belpex_SPP_BE |

FlexiO Max arrived in October 2026, for households that steer their
installations through a FlexiO EMS+. Its card is Groene Stroom Flex's with its
own coefficients and only the SMR3 meter row. The flexibility fee the EMS+ earns
is "los van de energieprijzen en terugleveringsvergoedingen", depends on the
imbalance market and on what the household makes available, and is settled
monthly, so it is not modelled.

LifePowr carries `withdrawn=date(2026, 10, 1)`. FlexiO Max replaced it: the
listing links no LifePowr card after September 2026, the LifePowr product pages
redirect to FlexiO Max, and the October FlexiO Max card prints LifePowr's
formula, fee, network rows and taxes unchanged. The contract stays registered so
an entry on it keeps pricing on the September card and is told so by the
`contract_withdrawn` Repairs card; new setups still list it, marked †, for a
household under contract on it, and both compare pages no longer offer it. Its `successor` is `flexio_max`, which settles its last month.

The three RLP products use `SpotMonthlyRates` with `rlp_indexed=True` and
`rlp_blend="flanders"`; their contract kind already makes the setup flow require
an ENTSO-E key. Their feed-in formulas set `spp_indexed=True`, because the
consumption and injection indices use different Synergrid profiles. The three
dynamic products set `quarter_hourly=True`. Every contract leaves
`spot_indexed_injection=False`: the fixed card's injection is static, while the
other energy legs already collect a key and fetch the spots their injection
formula needs.

## Catalog matching

`_archive_links` matches a product fragment before the common `Particulier`
suffix and the stamp the link ends on, reading each link once: as one pattern of
three open runs, a long link was read again for every place each could end
(`test_a_crafted_listing_or_card_is_read_in_time_linear_in_its_length`). The
base Dynamic expression has a `(?!-Plus)` guard; without it the
base product can bind the Dynamic Plus PDF when both have the same month.
Tests construct a listing containing all seven real filename shapes and assert
that every contract resolves its own card.

The six digits the expression captures are read as `YYYYMM`, and nothing on the
listing guarantees they are one: a file named for an id rather than a month
matches just as well. Such an entry is skipped rather than allowed to raise, and
a listing where none of the matches names a month fails as a plain
`ExtractorError` like any other miss, so the Repairs card the user sees is the
one that fits.

`discover` reads the same listing for every residential card it links, by the
product the file is named after, and answers the contract id where a
`card_re` claims the name and the name itself where none does, so live_check
reports a new product as Trevion names it. The gas card and the professional
editions are left out. FlexiO Max sat on the listing for a day before anything
noticed it, because nothing read the listing for more than the cards already
registered.

## Parsing

`parse_snapshot` selects one of three commodity parsers and then applies the
same DSO, tax, and validity parsers to every product.

### Fixed

`_extract_fixed` reads the `Enkelvoudig`, `Piekuren`, `Daluren`, and
`Exclusief Nacht` rows. Consumption and injection are published in cEUR/kWh
and converted to EUR/kWh. The feed-in pair is kept and flagged `bi_hourly`:
the card prints one feed-in rate per meter register, so `injection.py`
credits a bi-hourly or digital meter by register on Flanders' day/night
schedule and a single-register meter at the `Enkelvoudig` rate. The flag is
what makes the pair readable; the engine ignores an unflagged pair on a
fixed or variable card, so no other supplier is affected.

The table holding both columns is headed `1 jaar vast`, so the feed-in pair is
fixed with the consumption price and the leg sets `fixed_for_term`. A signing
cohort keeps its own card's pair (`_cohort_injection_from_archived`,
`cohort_legs.py`): a March 2026 signer is paid 2,81 c/kWh on a single-register meter
for the year, not the 5,76 September's card prints.

Every path that credits the feed-in asks per register. The live sensor and the
today/tomorrow array resolve the hour's own, and the year-to-date walk asks once
for each: it is a per-day walk holding the day and night kWh apart already, so it
takes one hour inside each block (`_DAY_REGISTER_HOUR` / `_NIGHT_REGISTER_HOUR`,
`ytd_cost.py`) and credits each register at its own rate. It used to ask without
the energy leg, the hour, the meter or the region, which are the four arguments
that reach the register branch, so the whole year was credited the flat printed
rate while the sensor beside it credited 6,3329 c/kWh by day against the printed
5,7615: about 6 EUR a year on 3.500 kWh exported, and a contradiction visible
hour by hour. A card with no flagged pair answers the same rate to both
questions, so nothing else moved.

### Monthly indexed

`_extract_monthly` reads the VAT-exclusive formula printed as
`(factor * Belpex_RLP_VL +/- base) * 1,06`. Both coefficients are grossed to
the VAT-inclusive snapshot convention, by the multiplier the formula prints
(`_extract_formula` reads it rather than requiring `1,06`, so a card at
another rate is priced at it instead of refused). The title line's
`Incl. 6% BTW` is recorded as `TaxOverlay.card_vat_rate`. The feed-in formula is VAT-exempt and
is converted from the card's EUR/MWh notation without the multiplier. Until the
delivery month's SPP-weighted mean is available, `current` is derived from the
latest known SPP index printed beside the formula: the first value after the
first "laatst gekende waarde" past the first "Belpex_SPP_BE parameter" (`_after`).

### Dynamic

`_extract_dynamic` reads the same VAT conversion around `Belpex 15 MTU`, which the
October 2026 cards rename `EPEX Spot` with the same formulas and coefficients
(`_QUARTER_HOUR_INDEX` accepts both), and marks the energy rate quarter-hourly.
Trevion PDFs use an en dash or Unicode minus in feed-in formulas, so every sign
regex uses `_parse.SIGN_CHARS` rather than a literal ASCII hyphen. On both kinds of
card the feed-in formula is the first after the first "formule" past the first
"teruglevering" (`_injection_formula`), read from where its number starts, and the
gap before a word stops at the next one that starts it, so a card of those words
with no formula or index after them costs its length
(`test_a_crafted_card_is_read_in_time_linear_in_its_length`,
`test_a_crafted_monthly_card_is_read_in_time_linear_in_its_length`).

### Units and formula check

The meter table heads its price columns `Afname (c€/kWh) Injectie (c€/kWh)` on
every card on file, February to November 2026, in both text orders, ending 109 to
166 characters ahead of the first figure under them. Each heading must state
`c€/kWh` within 300 characters of the `Enkelvoudig` row on Vast and of the meter
row on the others (`_require_column_units`); a card heading either column in
another unit, or no longer stating one, raises.

The formulas state no unit of their own, so one restated on another scale would
read clean. Each must give back, within 2 % (`_require_printed`), the price the
meter row prints for it at the index quoted in the sentence before it: "De laatst
gekende waarde is deze van oktober 2026 (182,447 €/MWh). De tariefformule
elektriciteit is (0,107* Belpex_RLP_VL+1,5) *1,06." gives the 22,28 of
`Tweevoudig 22,28 10,94`, VAT included on both sides, and the feed-in formula at
the SPP value its own sentence quotes gives the 10,94, VAT-exempt. The dynamic
cards quote the VREG's estimate for the year ahead the same way and print the
result in the `SMR3` row. On every archived card the energy formulas do within
0,04 % and the feed-in ones within 0,43 %, the farthest a feed-in printed at 0,27
c€/kWh; a factor read ten times off misses by 77 % or more. A formula that does
not, or a card quoting no index or printing no price for it, raises as a formula
that does not read does. The Flex feed-in crosses zero at an SPP of 26,3 EUR/MWh
(the May 2026 card printed 0,27 at April's 29,17), and a figure printed that
close to it is out by more than any share, so a gap of 0,01 c€/kWh is allowed
outright (`_FORMULA_FLOOR`): about half of it is the printed rounding, and the
other half keeps an exact tie from failing on floating point. The meter row is
read with any sign printed ahead of each figure (`_meter_row`), so a figure under
zero is compared as negative. It is only compared: the monthly credit is the
formula at the quoted SPP and the dynamic one the formula at each quarter hour.

A leg restated whole, formula and printed price together, still gives its price
back, and only the column heading tells it apart.

### Shared meter columns

The PDF readers serialize the commodity table in two observed orders:

```text
Tweevoudig <green> <WKK> <fee>
Tweevoudig|SMR3 <energy> <injection> <green> <WKK> <fee>
```

The plain pypdf extraction can instead place the three shared values after the
`Enkelvoudig` row and before `Tweevoudig`. `_meter_row` reads the row, and
`_meter_shared_values` handles all three shapes and is the single source for
the green certificate cost, WKK cost, and yearly subscription; the energy and
feed-in figures ahead of them are what the formula check compares. This
prevents one parser path from interpreting the commodity and feed-in columns
as regulated levies.

## Network and taxes

`_extract_dsos` reads the digital-meter section only and maps Trevion's eight
current Fluvius labels to the canonical keys. Each row provides capacity tariff,
single consumption distribution, exclusive-night distribution, and annual data
management fee. The transport field is zero because the card's distribution
column already represents the network rate surfaced by Trevion. An area whose
row does not read is left out of the table. `fetch` then refuses the live card
rather than adopt it with a hole in it, since an entry on the missing area would
fail every tick until the next good one (`require_every_area`, `_parse.py`), and
`fetch_for_month` refuses the running month's the same way
(`running_month_short_of_an_area`), so the last good card stands in. A past
month's card keeps its short table: its energy is what the month is billed on,
and the month cache bills the missing area on the nearest card of the same
calendar year that prints it (`_with_nearest_area`, `snapshot_areas.py`). The
next card a closed month is settled on is read for its indices only. A table
where no row reads at all raises
(`test_a_live_card_missing_an_area_is_refused`,
`test_a_past_month_keeps_a_short_network_table`, `tests/test_trevion.py`).

`_extract_taxes` reads:

- `Bijdrage op de energie` as EUR/kWh after conversion from cents, 0 when the
  row is gone (the levy was abolished on 2026-08-01 and every other Flemish
  card may drop the row; requiring it would take every contract offline);
- the flat `Bijzondere accijns` on current cards;
- the `0-3 MWh` row of the degressive block on older tiered cards, under either
  reader's layout of that block, and with the layout reader, which keeps each
  tier's rate on its row, the whole block into `federal_excise_bands`
  (`excise_tier_bands`, `_parse.py`);
- green certificate and WKK costs from `_meter_shared_values`;
- the domiciled Energiefonds row in EUR/month, 0 when the row is gone, for the
  same reason; the gap to its figure stops at the next such heading, so a card of
  headings with no figure costs its length.

Residential card values are already VAT-inclusive, so `vat_rate=0.0`, as on
every other residential card; `published_vat_rate` is left at its default for
the professional path to fill. Feed-in remains VAT-exempt.

## Validity

The card body states that prices apply to contracts concluded during a Dutch
month and year. `_extract_validity` finds that token and returns the month's
last calendar day. The archive cross-check rejects a card whose body names a
different month from the requested filename.

## Fixtures

`tests/test_trevion.py` uses these public card samples:

| Fixture | Coverage |
| --- | --- |
| `trevion_vast_2026-04.pdf` | Older tiered excise and both PDF text orders |
| `trevion_vast_2026-09.pdf` | Fixed consumption and day/night feed-in |
| `trevion_flex_2026-05.pdf` | The older Flex layout, feed-in formula written with an `x` |
| `trevion_flex_2026-09.pdf` | RLP consumption and SPP injection |
| `trevion_dynamic_2026-09.pdf` | Quarter-hourly dynamic product |
| `trevion_dynamic_plus_2026-09.pdf` | Dynamic Plus catalog disambiguation |
| `trevion_lifepowr_2026-05.pdf` | LifePowr while it was a quarter-hourly Belpex 15 MTU product |
| `trevion_lifepowr_2026-09.pdf` | LifePowr monthly product |
| `trevion_flexio_max_2026-10.pdf` | FlexiO Max monthly product |
| `trevion_energreen_2026-09.pdf` | Energreen dynamic coefficients |

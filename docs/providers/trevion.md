# Provider: Trevion

This document is the maintainer reference for the Trevion tariff-card extractor
(`providers/trevion.py`). Trevion publishes one residential PDF per product and
month on a public listing page. All six products are Flemish and every card
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
| Probe | HEAD the listing page for `Last-Modified` / `ETag` |
| PDF reader | `fetch_pdf_text_layout` (`pdfplumber`) |

The filenames end in `YYYYMM.pdf` or `YYYYMM-N.pdf`. `_find_card` scrapes the
listing and hands it to `_resolve_card`, which resolves relative links against
`https://trevion.be` and chooses the newest month, or the one asked for. The
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
card is not out yet comes back `provisional`.

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

The two RLP products use `SpotMonthlyRates` with `rlp_indexed=True` and
`rlp_blend="flanders"`; their contract kind already makes the setup flow require
an ENTSO-E key. Their feed-in formulas set `spp_indexed=True`, because the
consumption and injection indices use different Synergrid profiles. The three
dynamic products set `quarter_hourly=True`. Every contract leaves
`spot_indexed_injection=False`: the fixed card's injection is static, while the
other energy legs already collect a key and fetch the spots their injection
formula needs.

## Catalog matching

`_archive_re` matches a product fragment before the common `Particulier`
suffix. The base Dynamic expression has a `(?!-Plus)` guard; without it the
base product can bind the Dynamic Plus PDF when both have the same month.
Tests construct a listing containing all six real filename shapes and assert
that every contract resolves its own card.

The six digits the expression captures are read as `YYYYMM`, and nothing on the
listing guarantees they are one: a file named for an id rather than a month
matches just as well. Such an entry is skipped rather than allowed to raise, and
a listing where none of the matches names a month fails as a plain
`ExtractorError` like any other miss, so the Repairs card the user sees is the
one that fits.

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
`cohort.py`): a March 2026 signer is paid 2,81 c/kWh on a single-register meter
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
the VAT-inclusive snapshot convention. The feed-in formula is VAT-exempt and
is converted from the card's EUR/MWh notation without the multiplier. Until the
delivery month's SPP-weighted mean is available, `current` is derived from the
latest known SPP index printed beside the formula.

### Dynamic

`_extract_dynamic` reads the same VAT conversion around `Belpex 15 MTU` and
marks the energy rate quarter-hourly. Trevion PDFs use an en dash or Unicode
minus in feed-in formulas, so every sign regex uses `_parse.SIGN_CHARS` rather
than a literal ASCII hyphen.

### Shared meter columns

The PDF readers serialize the commodity table in two observed orders:

```text
Tweevoudig <green> <WKK> <fee>
Tweevoudig|SMR3 <energy> <injection> <green> <WKK> <fee>
```

The plain pypdf extraction can instead place the three shared values after the
`Enkelvoudig` row and before `Tweevoudig`. `_meter_shared_values` handles all
three shapes and is the single source for the green certificate cost, WKK
cost, and yearly subscription. This prevents one parser path from interpreting
the commodity and feed-in columns as regulated levies.

## Network and taxes

`_extract_dsos` reads the digital-meter section only and maps Trevion's eight
current Fluvius labels to the canonical keys. Each row provides capacity tariff,
single consumption distribution, exclusive-night distribution, and annual data
management fee. The transport field is zero because the card's distribution
column already represents the network rate surfaced by Trevion.

`_extract_taxes` reads:

- `Bijdrage op de energie` as EUR/kWh after conversion from cents, 0 when the
  row is gone (the levy was abolished on 2026-08-01 and every other Flemish
  card may drop the row; requiring it would take all six contracts offline);
- the flat `Bijzondere accijns` on current cards;
- the `0-3 MWh` row of the degressive block on older tiered cards, the tier a
  household pays and the one every sibling extractor reads, under either reader's
  layout of that block;
- green certificate and WKK costs from `_meter_shared_values`;
- the domiciled Energiefonds row in EUR/month, 0 when the row is gone, for the
  same reason.

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
| `trevion_energreen_2026-09.pdf` | Energreen dynamic coefficients |

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
listing, resolves relative links against `https://trevion.be`, and chooses the
newest month. `fetch_for_month` uses the same catalog with an exact month filter
and passes the parsed result through `archive_validity_check`.

## Contracts

| Contract id | Product | Kind | Consumption index | Injection |
| --- | --- | --- | --- | --- |
| `groene_energie_vast` | Groene Energie Vast | `fixed` | Published mono/day/night rates | Published mono/day/night rates |
| `groene_stroom_flex` | Groene Stroom Flex | `spot_monthly` | Belpex_RLP_VL | Belpex_SPP_BE |
| `groene_energie_dynamisch` | Groene Energie Dynamisch | `dynamic` | Belpex 15 MTU | Belpex 15 MTU |
| `groene_energie_dynamisch_plus` | Groene Energie Dynamisch Plus | `dynamic` | Belpex 15 MTU | Belpex 15 MTU |
| `lifepowr` | LifePowr by Trevion | `spot_monthly` | Belpex_RLP_VL | Belpex_SPP_BE |
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
minus in feed-in formulas, so every sign regex uses `_pdf.SIGN_CHARS` rather
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

- `Bijdrage op de energie` as EUR/kWh after conversion from cents;
- the flat `Bijzondere accijns` on current cards;
- the final `50-1000 MWh` value on older tiered cards;
- green certificate and WKK costs from `_meter_shared_values`;
- the domiciled Energiefonds row in EUR/month.

Residential card values are already VAT-inclusive, so `vat_rate=0.0` and
`published_vat_rate=0.06`. Feed-in remains VAT-exempt.

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
| `trevion_flex_2026-09.pdf` | RLP consumption and SPP injection |
| `trevion_dynamic_2026-09.pdf` | Quarter-hourly dynamic product |
| `trevion_dynamic_plus_2026-09.pdf` | Dynamic Plus catalog disambiguation |
| `trevion_lifepowr_2026-09.pdf` | LifePowr monthly product |
| `trevion_energreen_2026-09.pdf` | Energreen dynamic coefficients |

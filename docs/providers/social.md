# Provider: social

This document is the maintainer reference for the social tariff extractor
(`providers/social.py`). A household granted protected status ("client
protégé", "beschermde afnemer") pays the social tariff whoever supplies it:
one price per meter register for the whole country, set each quarter by the
CREG. Read this alongside the framework and pricing references:

- [../provider-framework.md](../provider-framework.md): the `SupplierExtractor`
  protocol, the `SupplierSnapshot` / `*Rates` dataclasses, the shared `_pdf`
  helpers, and the fetch / probe / `fetch_for_month` contracts.
- [../pricing-model.md](../pricing-model.md): how `compute_breakdown` consumes
  the energy, DSO, tax, and injection overlays this extractor produces.
- [../data-sources.md](../data-sources.md), Part 5: the excise read from the
  law, which includes the protected customer's rate.

## Overview

| Property | Value | Source |
| --- | --- | --- |
| Extractor id | `social` (`SUPPLIER_SOCIAL`) | `const.py` |
| Label | `Social tariff (CREG, protected customers only)` | `social.py` |
| Region(s) served | All three | `_DSO_KEYS` |
| Publication form | One CREG PDF per quarter, `E-TSS-FR-<year>-Q<n>.pdf` | `_CREG_URL` |
| Archive | Every quarter from Q4 2022, the oldest the CREG still serves | `fetch_for_month`, `FIRST_QUARTER` |
| Probe | None: the 24 h TTL refetches it | `EXTRACTOR` |

## Contracts

The price is the same whoever supplies the household, but the feed-in credit
is not: no law or regulator sets it, the CREG card says so ("Le tarif
d'injection n'est pas calculé par la CREG" on Engie's), and each supplier pays
its own or none. A survey of every supplier and every distribution operator
acting as social supplier in October 2026 found two that publish it:

| Contract | Label | Feed-in |
| --- | --- | --- |
| `social_engie` | Supplied by Engie | Engie's social card, `E_SOCIAL_R_GREY_C_F_00_<V\|W\|B>_F` on its document service |
| `social_luminus` | Supplied by Luminus | Luminus's social card, "Tarif social Electricité" on its price-list archive |
| `social_other` | Supplied by another supplier or the network operator | None |

Fluvius, the social supplier in Flanders, states that it pays none
("ontvang je bij de sociale leverancier geen vergoeding"). TotalEnergies'
social card prints no feed-in rate and is offered on meters read once a year.
No other supplier or operator publishes one, so `social_other` credits
nothing rather than a guess.

All three are `kind="variable"`: the price moves each quarter whatever the
signing date, so no signing cohort applies.

## Price

`parse_creg` reads the four registers in the order the card prints them:
single, day, night and exclusive night. Each table opens on its energy row and
closes on its total, and the network is one "Composante réseau" row since 2026
and a distribution plus a transport row before. The rows have to add up to the
total before and after VAT, and every VAT-inclusive figure has to match its
ex-VAT one at the rate the card states ("TVA 6% comprise"), or the card is
refused.

The card says its components are "uniquement communiquées à titre
d'information" and recommends billing the total, so the snapshot keeps the
total exact: the network is the VAT-inclusive total minus the VAT-inclusive
energy component, and the energy leg is the rest.

| Field | Value |
| --- | --- |
| `energy` | `VariableRates`: the four registers' energy part, no standing charge |
| `dsos` | One overlay for every operator of the region: the network part, flat, on the single register, the day and night ones only where they differ, and the exclusive night register's own |
| Impact bands | The network part in all three: it is the same in every band |
| `prosumer_eur_per_kva_year` | 0 in Wallonia: the CWaPE exempts a protected customer |
| `taxes.federal_excise` | The protected customer's excise, `excise_law.protected_excise` |
| `taxes.region_connection_fee` | Wallonia only, read off the supplier's social card |

The quarter's own card is fetched for every month, and `valid_until` is the
quarter's last day.

Under Wallonia's Impact tariff the day price bills the PIC and MEDIUM bands and
the night price the ECO band, which is the mapping Engie's social card prints
("Bihoraire / IMPACT: Heures pleines / PIC-MEDIUM, Heures creuses / ECO") and
the one `_routed_rate` already applies.

## Excise

A protected customer pays a special excise of its own, which the CREG card
leaves out ("Ces tarifs ne comprennent pas ... accise spéciale").
`excise_law.protected_excise` answers it per delivery month:

| From | EUR/MWh before VAT | Source |
| --- | --- | --- |
| before 2023-04-01 | 0 | exempt: the Q4 2022 CREG card says so ("Les clients protégés sont exonérés de la cotisation énergie, de l'accise spéciale ...") |
| 2023-04-01 | 0 | the law of 19 March 2023, article 14, for its transition to 30 June 2023 |
| 2023-07-01 | 23,62 | the law of 19 March 2023, article 9 |
| 2026-08-01 | 1 | the law of 30 May 2026, article 38, read from Justel |

The first three are closed history and are typed in
(`_PROTECTED_CLOSED`, `excise_law.py`); the last is read from the law, and so
is any later step. A month from August 2026 is not known while the law has not
been read, and is never billed at the rate it replaced: billing it at the old
rate would over-bill, and without the excise it would under-bill every kWh.
`build_snapshot` then leaves the excise at 0 and sets
`TaxOverlay.protected_excise_unread`, `resolve_federal_excise` fills the rate
and clears the flag once the law is held, and the tick refuses to publish a
card still carrying it (`UpdateFailed`), raising the `excise_law_unread`
Repairs card until it is read.

Two suppliers print this rate wrong on their social card: Engie and
TotalEnergies print 1,06 c€/kWh from August 2026, ten times the law's 0,106.
Neither is read for it.

`resolve_federal_excise` takes the protected rate for a social snapshot instead
of the household one, per delivery month, so a Q3 2026 card bills July at
23,62 and August at 1.

## Rules that do not apply

The network component replaces the distribution operator's tariffs, so two
regional rules are skipped for a social snapshot:

- the VREG network ceiling (`resolve_vreg_network_ceiling`), which caps the
  Flemish capacity and network terms the social tariff does not have;
- Sibelga's power term (`omits_brussels_power_term`), which the social cards
  of both Engie and Luminus leave out in Brussels, where they print the excise
  alone.

## Comparison and archive

Nobody can switch to the social tariff: it follows a protected status, not a
choice. So it is never a candidate on the comparison pages, neither the 1:1
quote (`_compare_supplier_options`, `compare_flow.py`) nor the ranking
(`_sweep_candidates`, `flow_contracts.py`), while a social tariff entry can still compare itself
with the commercial offers.

The project's card archive keeps it like any card, so a past month is one
small JSON row in `be_price_cards` rather than a CREG PDF and a supplier card.
GitHub's runners are not served the law, so a row for a month from August 2026
carries `protected_excise_unread` and no excise, and the integration fills the
protected rate from the law it holds when it reads the row
(`resolve_federal_excise`). The live check leaves these rows out of its federal
levy, VREG ceiling and network consensus (`_OFF_MARKET_SUPPLIERS`,
`scripts/live_check.py`): they are right and agree with no market card.

## Feed-in

`parse_engie` reads the "Injection" row (single, day, night) and, since 2024,
the three formulas, `0,0500 + (0,0632 x EPEXDAM)` for the single register in
October 2026; the 2023 cards print the month's rates and no formula, which
are credited as printed. The card prints its rates at last month's EPEXDAM but leaves the index
itself out, so the formulas are bound by arithmetic: the three printed rates
have to imply one index. The single register's formula is `month_indexed`, so
it resolves on the delivery month's mean where the entry has an ENTSO-E key;
the day and night rates are the printed ones. The card limits its feed-in to a
digital or bidirectional meter.

`parse_luminus` reads the feed-in row, "Tarif de l'énergie injectée" since
2026 and "Compensation pour l'énergie injectée" before; the card a change of
rate falls in (June 2025) prints two dated rows, and the first, "jusqu'au" the
end of its own month, is the month's. The card indexes
it on the delivery quarter's Belpex and prints it on the previous quarter's,
the one figure known while the quarter runs, which is what is credited.

Both cards print the quarter's CREG prices on their consumption row, and that
is what dates them: a card whose row is not the quarter's CREG price is
refused.

## Live check

`_check_social` (`scripts/live_check.py`) walks every contract in every region
on its own bounds rather than the shared snapshot validation, which expects a
capacity term in Flanders and a household excise: the card has to be the
running quarter's, every operator of the region has to carry the overlay, the
single register's price has to sit between 8 and 80 c/kWh (a unit slip is a
factor of ten), the Engie and Luminus contracts have to carry a feed-in rate,
and a Walloon card the connection fee. The excise is not checked there: the
runners are not served the law, so the card carries `protected_excise_unread`.

## Tests

`tests/test_social.py`, on six CREG cards from Q4 2022 to Q4 2026 (one per
layout), the Walloon social cards of Engie for September 2023 (no formula) and
October 2026, and those of Luminus for June 2025 (two dated feed-in rows) and
October 2026. Parsed beyond the fixtures when this was written: all seventeen
CREG quarters, Engie's Walloon social card monthly back to November 2022 and
Luminus's quarterly back to October 2022.

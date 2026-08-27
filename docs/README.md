# Contributor documentation

This directory documents the internals of the **Belgian Electricity Prices** Home
Assistant integration (domain `be_electricity_prices`). It is written for people who
maintain or extend the code, not for end users. End-user setup lives in the
[project README](../README.md).

## What the integration does

It computes the true all-in residential electricity price, and the solar injection
credit, for a Belgian household. It fuses three live inputs per refresh:

1. A **supplier tariff snapshot** (the energy formula), fetched from each supplier's
   own published tariff card. No EUR values are hardcoded anywhere in the source.
2. The user's **DSO** (distribution grid operator) sub-area network and capacity
   overlay, parsed from the same card.
3. Federal and regional **taxes and levies**, plus (for solar) the **injection**
   tariff.

Belgium has three regions (Flanders, Wallonia, Brussels), many DSO sub-areas, and
several contract kinds (fixed, variable, dynamic, time-of-use, Wallonia Tarif
Impact). A `DataUpdateCoordinator` refreshes hourly, and per-supplier extractor
modules under `providers/` do the fetching and parsing. See
[architecture.md](architecture.md) for the full picture.

## Map of this documentation

### Core

| Document | Covers |
| --- | --- |
| [architecture.md](architecture.md) | Big-picture design, module map, end-to-end data flow, how to add a supplier |
| [glossary.md](glossary.md) | Belgian-energy and Home Assistant domain terms |
| [coordinator.md](coordinator.md) | The `DataUpdateCoordinator`: refresh lifecycle, probe/cache/TTL, the data dict entities read, YTD cost |
| [pricing-model.md](pricing-model.md) | `pricing.compute_breakdown`: energy/network/tax/capacity/injection math |
| [config-flow.md](config-flow.md) | The setup wizard and options flow |
| [data-sources.md](data-sources.md) | The ENTSO-E spot client (`api.py`) and recorder backfill (`backfill.py`) |
| [provider-framework.md](provider-framework.md) | The extractor protocol, dataclasses, registry, and shared PDF helpers |
| [entities.md](entities.md) | Sensors, binary sensor, button, diagnostics, services, translations |
| [ci-and-testing.md](ci-and-testing.md) | `scripts/live_check.py`, the test suite, and the GitHub workflows |

### Provider reference

One document per registered supplier extractor. Each is a "when the tariff card
changes, look here" reference tied to the provider's tests and fixtures.

| Supplier | Document |
| --- | --- |
| Bolt | [providers/bolt.md](providers/bolt.md) |
| Cociter | [providers/cociter.md](providers/cociter.md) |
| DATS 24 | [providers/dats24.md](providers/dats24.md) |
| EBEM | [providers/ebem.md](providers/ebem.md) |
| Ecofix | [providers/ecofix.md](providers/ecofix.md) |
| Ecopower | [providers/ecopower.md](providers/ecopower.md) |
| Eneco | [providers/eneco.md](providers/eneco.md) |
| energie.be | [providers/energiebe.md](providers/energiebe.md) |
| Energy Knights | [providers/energyknights.md](providers/energyknights.md) |
| EnergyVision | [providers/energyvision.md](providers/energyvision.md) |
| Engie | [providers/engie.md](providers/engie.md) |
| Frank Energie | [providers/frank.md](providers/frank.md) |
| Luminus | [providers/luminus.md](providers/luminus.md) |
| Mega | [providers/mega.md](providers/mega.md) |
| OCTA+ | [providers/octaplus.md](providers/octaplus.md) |
| TotalEnergies | [providers/totalenergies.md](providers/totalenergies.md) |

## Reading order

New contributors should read [architecture.md](architecture.md) first, keep
[glossary.md](glossary.md) open alongside it, then dive into whichever area they are
changing. Anyone touching a supplier should read
[provider-framework.md](provider-framework.md) before the specific
[provider doc](providers/).

## A note on prices

The codebase deliberately stores no EUR values in Python source: every rate comes
from a live fetch. These docs follow the same rule. Any number shown is either a
constant that is genuinely in the code (for example the VREG capacity floor) or an
illustrative value taken from a source comment or test, and is labelled as such.

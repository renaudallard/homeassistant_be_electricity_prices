<p align="center">
  <img src="logo.svg" alt="BE electricity - real-time prices" width="640"/>
</p>

<p align="center">
  <a href="https://github.com/renaudallard/homeassistant_be_electricity_prices/releases/latest">
    <img src="https://img.shields.io/github/v/release/renaudallard/homeassistant_be_electricity_prices?label=version&style=flat-square&sort=semver" alt="Latest release"/>
  </a>
  <a href="https://github.com/renaudallard/homeassistant_be_electricity_prices/actions/workflows/validate.yml">
    <img src="https://img.shields.io/github/actions/workflow/status/renaudallard/homeassistant_be_electricity_prices/validate.yml?style=flat-square&label=hacs%20%2F%20hassfest" alt="Validate"/>
  </a>
  <a href="https://github.com/renaudallard/homeassistant_be_electricity_prices/actions/workflows/test.yml">
    <img src="https://img.shields.io/github/actions/workflow/status/renaudallard/homeassistant_be_electricity_prices/test.yml?style=flat-square&label=tests" alt="Tests"/>
  </a>
  <a href="https://www.home-assistant.io/">
    <img src="https://img.shields.io/badge/Home%20Assistant-2026.4%2B-41BDF5?logo=home-assistant&logoColor=white&style=flat-square" alt="Home Assistant"/>
  </a>
  <a href="https://hacs.xyz">
    <img src="https://img.shields.io/badge/HACS-Custom-41BDF5.svg?style=flat-square" alt="HACS"/>
  </a>
  <a href="./LICENSE">
    <img src="https://img.shields.io/github/license/renaudallard/homeassistant_be_electricity_prices?style=flat-square" alt="License"/>
  </a>
  <a href="https://www.paypal.me/RenaudAllard">
    <img src="https://img.shields.io/badge/PayPal-Donate-blue.svg?logo=paypal&style=flat-square" alt="PayPal"/>
  </a>
</p>

---

Home Assistant integration that exposes the **all-in real EUR/kWh paid** for
Belgian electricity, taking into account every component of a Belgian bill
(energy + transport + distribution + levies + VAT) plus the Flanders
capacity tariff billed on the monthly peak.

Energy prices are fetched **live** from each supplier's own published
tariff card. **No EUR values are hardcoded in the source.** Add a supplier
by writing one Python module that knows where to find that supplier's
publication and how to parse it.

> Targets Home Assistant **2026.4 or newer**.

---

## Supported providers

| Supplier | Status | Source |
| --- | --- | --- |
| **Eneco** | Working - Power Fix / Flex / Dynamic | [Eneco PDF tariff cards](https://eneco.be/nl/elektriciteit-gas/tariefkaarten/) |
| **Cociter** | Working - Tarif Variable (BELIX-indexed) + Tarif Dynamique (quarter-hourly BELPEX). Monthly cards `RCVar_YMR_Coop-YYMM-fr.pdf` / `RCDyn_SM3_Coop-YYMM-fr.pdf`. | [`providers/cociter.py`](./custom_components/be_electricity_prices/providers/cociter.py) |

Adding another supplier is a self-contained PR: drop a new module under
[`custom_components/be_electricity_prices/providers/`](./custom_components/be_electricity_prices/providers/),
register it in [`providers/__init__.py`](./custom_components/be_electricity_prices/providers/__init__.py),
and ship a fixture-based unit test. The Eneco module is the reference.

## What the integration computes

For every hour, an all-in EUR/kWh built up as

```
energy + distribution + transport + levies + VAT
```

where each component comes from the supplier's tariff card and the
configured DSO. For dynamic contracts the energy term is `factor x spot + base`,
where `spot` is the Belgian day-ahead price from the ENTSO-E Transparency
Platform.

## Sensors

Per config entry, grouped on a single device:

| Sensor | Description |
| --- | --- |
| `current_price` | All-in EUR/kWh now. Attributes: hourly breakdown, snapshot age, last fetch error. |
| `next_hour_price` | All-in EUR/kWh for the next hour. |
| `today_average` | Daily average all-in EUR/kWh. |
| `today_min` / `today_max` | Daily extremes. |
| `energy_component` | Energy-only EUR/kWh now. |
| `network_component` | Distribution + transport EUR/kWh now. |
| `taxes_component` | Levies + VAT EUR/kWh now. |
| `capacity_cost` | Flanders only - current monthly capacity cost in EUR. |
| `monthly_peak_kw` | Flanders only - running monthly peak power in kW. |

## Installation

### HACS (recommended)

1. Open HACS, go to *Integrations*, click the three-dot menu and pick *Custom repositories*.
2. Add `https://github.com/renaudallard/homeassistant_be_electricity_prices` as type *Integration*.
3. Install **Belgian Electricity Prices** and restart Home Assistant.
4. Settings -> Devices & services -> Add integration -> *Belgian Electricity Prices*.

### Manual

Download the latest [release zip](https://github.com/renaudallard/homeassistant_be_electricity_prices/releases),
extract it under `<config>/custom_components/be_electricity_prices/`, and restart Home Assistant.

`pypdf` is the only extra runtime dependency; Home Assistant installs it
automatically from the manifest.

## Configuration

The UI flow asks six things at most:

1. **Supplier** + **Region** (Flanders / Wallonia / Brussels).
2. **Contract** filtered by supplier (populated from the registry).
3. **DSO** filtered by region.
4. **Meter type** - mono (single rate), bi (peak / off-peak), or dynamic (smart
   meter). Drives whether energy and distribution are billed at single or
   time-of-use rates.
5. **ENTSO-E API key** - only when the chosen contract is dynamic.
6. **Capacity tariff peak source** - only when region is Flanders. Either a power
   sensor reporting your live kW draw, or a fixed kW value (default 2.5 kW).

No EUR values are asked. Energy + DSO + tax rates all come from the
supplier's tariff card.

### Getting an ENTSO-E API key

Required only for dynamic contracts. Register on the
[ENTSO-E Transparency Platform](https://transparency.entsoe.eu/) and email
`transparency@entsoe.eu` from your registration address with subject
`Restful API access`.

## Refresh and fail mode

- Supplier snapshot: refreshed every 24 h.
- Spot prices (dynamic only): hourly via ENTSO-E; tomorrow's curve picked up after 13:00 CET.
- Monthly capacity peak (Flanders): tracked continuously, resets on the 1st.

If a refresh fails, the coordinator keeps serving the last known snapshot
and exposes `snapshot_age_hours`, `snapshot_stale` and `last_error` as
attributes on `sensor.<...>_current_price`. Snapshots older than 7 days
are flagged stale.

## Development

```bash
ruff check .
ruff format --check .
mypy --strict custom_components/be_electricity_prices
pytest tests/
```

Tests run against fixture PDFs in `tests/fixtures/` (real Eneco + Cociter
tariff cards captured 2026-04-29). Refresh fixtures with the supplier's
current PDF to re-run the tests against new data.

## License

BSD 2-Clause. See [LICENSE](./LICENSE).

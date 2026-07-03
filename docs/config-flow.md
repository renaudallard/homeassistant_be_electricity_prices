# Config and options flow

This document covers `config_flow.py`, the multi-step wizard that turns a user's
supplier, region, DSO, meter, solar, and sensor choices into a config entry. It
walks the config-flow steps in order, the branching between them, the validation
rules that reject impossible combinations, and the parallel options flow (edit
plus the one-off "compare another supplier" quote). No EUR values are asked
anywhere in this flow: energy, network, and tax rates are fetched live by the
coordinator from each supplier's own publication. The flow only collects
*structural* choices (who, where, which meter, which sensors).

Related docs:

- [architecture.md](architecture.md) - where the config flow sits in the module map
- [coordinator.md](coordinator.md) - what the coordinator does with the collected `entry.data`
- [pricing-model.md](pricing-model.md) - how `compute_breakdown` consumes region/DSO/meter/mode
- [provider-framework.md](provider-framework.md) - `Contract`, the extractor registry, `all_extractors`
- [data-sources.md](data-sources.md) - the ENTSO-E client the api-key steps validate against
- [entities.md](entities.md) - `strings.json`/translation keys shared with entities and services

## Two flows, one shared step chain

`config_flow.py` defines three classes:

| Class | Base | Role |
| --- | --- | --- |
| `_WizardStepsMixin` | - | The shared step chain (`async_step_contract` through `async_step_meters`) plus the branch helpers (`config_flow.py:761`) |
| `BePricesConfigFlow` | `_WizardStepsMixin, ConfigFlow` | Install-time flow; entry step `async_step_user`, finalizes with `async_create_entry` (`config_flow.py:979`) |
| `BePricesOptionsFlow` | `_WizardStepsMixin, OptionsFlow` | Post-install; menu -> `edit` (re-runs the chain pre-filled) or `compare` (throwaway quote) (`config_flow.py:1020`) |

Both flows walk the *same* chain: `supplier/region -> contract -> dso -> meter ->
(dso_tariff_mode) -> (api_key) -> (capacity) -> (connection_power) -> solar ->
(injection_api_key) -> meters`. Only the entry step and `_finalize` differ. The
mixin's docstring at `config_flow.py:761` states the invariant: `_after_meter` is
overridden in `BePricesConfigFlow` to add the install-time unique-id reject, and
`_finalize` is abstract (`config_flow.py:972` raises `NotImplementedError`).

The OptionsFlow pre-fills every field with the current value, so a user can change
anything post-install (including supplier, contract, and region). On finalize it
writes back to `entry.data` (not `entry.options`) and updates the entry title
(`config_flow.py:26` module docstring, `config_flow.py:1050`).

## Config-flow step reference

Steps in call order. "Written keys" are the `CONF_*` values `self._data.update`
persists at that step. Steps in parentheses in the chain above are conditional;
the "Shown when" column gives the gate.

| Step id | Method | Asks | Writes | Shown when / branch |
| --- | --- | --- | --- | --- |
| `user` | `async_step_user` (`config_flow.py:984`) | Supplier, region | `CONF_SUPPLIER`, `CONF_REGION` | Always (install entry step) |
| `contract` | `async_step_contract` (`config_flow.py:777`) | Contract (region-filtered) | `CONF_CONTRACT` | Always; aborts `supplier_region_unavailable` if none |
| `dso` | `async_step_dso` (`config_flow.py:792`) | Distribution operator | `CONF_DSO` | Always |
| `meter` | `async_step_meter` (`config_flow.py:803`) | Meter type | `CONF_METER` | Always; option list narrows by contract kind |
| `dso_tariff_mode` | `async_step_dso_tariff_mode` (`config_flow.py:919`) | DSO billing mode (simple/bi/impact) | `CONF_DSO_TARIFF_MODE` | Region == Wallonia (`config_flow.py:930`) |
| `api_key` | `async_step_api_key` (`config_flow.py:816`) | ENTSO-E token (required) | `CONF_API_KEY` | Contract kind == `dynamic` (`config_flow.py:957`) |
| `capacity` | `async_step_capacity` (`config_flow.py:834`) | Peak source (sensor/fixed) + value | `CONF_CAPACITY_MODE`, `CONF_CAPACITY_PEAK_SENSOR`, `CONF_CAPACITY_FIXED_KW` | Region == Flanders (`config_flow.py:963`, `:968`) |
| `connection_power` | `async_step_connection_power` (`config_flow.py:938`) | Brussels connection-power tier | `CONF_CONNECTION_KVA_TIER` | Region == Brussels (`config_flow.py:949`) |
| `solar` | `async_step_solar` (`config_flow.py:846`) | Inverter kVA + regime | `CONF_SOLAR_KVA`, `CONF_SOLAR_REGIME` | Always |
| `injection_api_key` | `async_step_injection_api_key` (`config_flow.py:874`) | ENTSO-E token (optional) | `CONF_API_KEY` | `_needs_injection_api_key` true (`config_flow.py:856`) |
| `meters` | `async_step_meters` (`config_flow.py:907`) | kWh sensors (registers or totals) | 6 `CONF_*_KWH` keys | Always (final step, then `_finalize`) |

### Flow diagram

```
                  ┌──────────────────────────────────────────────┐
                  │ user (install)  /  edit (options)            │
                  │   CONF_SUPPLIER, CONF_REGION                 │
                  └───────────────────────┬──────────────────────┘
                                          │
                          async_step_contract  (region-filtered)
                                          │  abort if no contract in region
                          async_step_dso
                                          │
                          async_step_meter  (kind-narrowed list)
                                          │
                          _after_meter  ── install adds unique-id reject
                                          │
                     region == wallonia? ─┼── yes → async_step_dso_tariff_mode
                                          │                    │
                          _after_dso_tariff_mode  ◄────────────┘
                                          │
              contract kind == dynamic? ──┼── yes → async_step_api_key
                                          │            (validate ENTSO-E)
                          _after_api_key  ◄───────────┘
                                          │
                     region == flanders? ─┼── yes → async_step_capacity
                                          │              │
                          _before_solar  ◄──────────────┘
                                          │
                     region == brussels? ─┼── yes → async_step_connection_power
                                          │              │
                          async_step_solar  ◄────────────┘
                                          │
        _after_solar → _needs_injection_api_key? ─ yes → async_step_injection_api_key
                                          │                     │ (optional, skippable)
                          async_step_meters  ◄──────────────────┘
                                          │
                          _finalize  (create entry / update entry)
```

The branch helpers that join the conditional steps back into the main line are all
in the mixin: `_after_meter` (`config_flow.py:930`), `_after_dso_tariff_mode`
(`config_flow.py:957`), `_after_api_key` (`config_flow.py:967`), `_before_solar`
(`config_flow.py:949`), and `_after_solar` (`config_flow.py:869`).

## Step details and the billing constraint behind each branch

### `user` / `edit`: supplier + region

Schema `_user_schema` (`config_flow.py:219`). Two dropdowns:

- Supplier: `_supplier_options()` (`config_flow.py:126`) lists every registered
  extractor by `id`/`label`. The install step lists all suppliers unfiltered;
  region filtering happens at the *contract* step instead, so a supplier with no
  product in the chosen region aborts there with a clear message rather than being
  hidden.
- Region: the `REGIONS` tuple (`const.py:43`), rendered with `translation_key="region"`
  so `selector.region.options` in `strings.json:230` supplies the localized labels.

`async_step_user` seeds `self._data = {}` on first entry (`config_flow.py:987`).
The OptionsFlow's `edit` step seeds instead from `{**entry.data, **entry.options}`
(`config_flow.py:1041`), which is why every later step can pre-fill.

### `contract`: region-filtered product list

Schema `_contract_schema` (`config_flow.py:240`). Contracts come from
`_contracts_for(supplier_id, region)` (`config_flow.py:133`), which reads
`get_extractor(supplier_id).contracts` and keeps only those whose
`Contract.regions` frozenset contains the region. `Contract` is defined at
`providers/base.py:59`; its `kind` is one of the `TariffKind` literals
`fixed | variable | dynamic | tou | tou_impact` (`providers/base.py:53`).

Guard: `async_step_contract` aborts with `supplier_region_unavailable` when the
filtered list is empty (`config_flow.py:782`), for example a Flanders-only supplier
selected with region Wallonia. The default is pre-selected only when the stored
`CONF_CONTRACT` still exists in the filtered set (`config_flow.py:250`); a stale id
leaves the field unset so the user must repick.

### `dso`: distribution operator

Schema `_dso_schema` (`config_flow.py:255`). Options come from `DSO_CHOICES[region]`
(`const.py:85`) via `_region_dso_options` (`config_flow.py:140`): 8 Fluvius
sub-areas in Flanders, 5 operators in Wallonia, Sibelga only in Brussels. The DSO
keys are canonical and stored verbatim in `CONF_DSO`; `const.py:45` warns they are
"stable forever" because they key into `SupplierSnapshot.dsos`. As with the contract
step, a stored value is only defaulted when it is still a valid slug for the region
(`config_flow.py:262`).

### `meter`: type, narrowed by contract kind

Schema `_meter_schema` (`config_flow.py:299`). The key rule (`config_flow.py:310`):

- If contract kind is `dynamic`, `tou`, or `tou_impact`, the only option is
  `METER_DYNAMIC` and the default is `METER_DYNAMIC`.
- Otherwise the full `METER_TYPES` list applies (`mono`, `bi`, `dynamic`,
  `exclusive_night`; `const.py:124`) with `METER_MONO` as the fallback.

Why: dynamic/TOU/Impact contracts bill energy by quarter-hour or hour-of-day and
require a smart (SMR3) meter. Picking `bi` on a TOU contract would route
distribution through the bi-horaire DSO peak/offpeak split while the supplier still
billed energy by TOU slot, two billing modes that do not mix (`config_flow.py:304`
comment). `_contract_kind` (`config_flow.py:151`) resolves the kind from the
registry and returns `""` when the stored contract is no longer in the catalogue,
so a stale OptionsFlow entry still renders the meter step with a sensible default
rather than raising.

The `exclusive_night` meter is not a first-class branch of the wizard: per
`const.py:116`, a dedicated night circuit (electric water heater, night-storage
heater) is configured as a *second* config entry pointing at the exclusive-night
kWh sensor; the primary (day) meter stays mono/bi/dynamic. The two entries get
distinct unique ids because the meter is part of neither, see the unique-id note
below (the tuple is supplier:contract:region:dso, so two entries with different
meters but the same tuple would still collide, which is why the pattern is one
day-meter entry plus one night-circuit entry on a *different* contract or the same
tuple is not created twice).

### `dso_tariff_mode`: Wallonia-only DSO billing mode

Schema `_dso_tariff_mode_schema` (`config_flow.py:267`), default `DSO_MODE_BI_HORAIRE`.
Options are `DSO_TARIFF_MODES` = `simple | bi_horaire | impact` (`const.py:135`),
`translation_key="dso_tariff_mode"`.

Reached only when region is Wallonia (`_after_meter`, `config_flow.py:930`). Tarif
Impact is the CWaPE 3-band hour-of-day distribution tariff (PIC 17-22, MEDIUM 7-11
+ 22-1, ECO 1-7 + 11-17, per `strings.json:34`) and needs a smart meter. Outside
Wallonia only `simple`/`bi_horaire` are meaningful and the coordinator falls back
automatically when the DSO does not publish Impact rates (`const.py:126`), so the
step is skipped entirely (Brussels has only Sibelga, Flanders bills via the
capacity tariff; `config_flow.py:930` comment).

### `api_key`: ENTSO-E token for dynamic energy (required)

Schema `_api_key_schema` (`config_flow.py:332`), a `PASSWORD` text field. Reached
from `_after_dso_tariff_mode` when the contract kind is `dynamic`
(`config_flow.py:957`). The typed key is stripped and validated live against the
ENTSO-E day-ahead endpoint by `_validate_entsoe_key` (`config_flow.py:343`) before
the flow proceeds:

- returns `None` on success,
- `"invalid_api_key"` when ENTSO-E returns 401 (`EntsoeAuthError`),
- `"cannot_connect"` on transport/parse error *and* on an HTTP 200 that comes back
  as an empty `Acknowledgement_MarketDocument` with no `TimeSeries`.

The validator queries a 24h window anchored on yesterday (`config_flow.py:360`): a
quota-exhausted token returns 200 plus an empty acknowledgement, and the BE bidding
zone effectively never goes a full local day with no publication, so an empty 24h
response reliably means "key not usable" (quota or maintenance). This blocks the
user from finalizing an entry that would fail on its first refresh. The two error
strings map to `config.error.invalid_api_key` / `config.error.cannot_connect`
(`strings.json:96`).

### `capacity`: Flanders capacity-tariff peak source

Schema `_capacity_schema` (`config_flow.py:374`). Reached from `_after_api_key` or
`_after_dso_tariff_mode` when region is Flanders (`config_flow.py:963`, `:968`).
Fields:

- `CONF_CAPACITY_MODE`: `sensor` (default) or `fixed`, `translation_key="capacity_mode"`.
- `CONF_CAPACITY_PEAK_SENSOR`: an `EntitySelector` restricted to
  `device_class=["power","apparent_power"]` (`config_flow.py:392`). The restriction
  is deliberate (issue #19): a kWh/unitless/temperature sensor would inflate the
  capacity bill. The coordinator already scales W/kW/VA/kVA, but cutting the long
  tail at the picker is the only guarantee the bug class cannot recur
  (`config_flow.py:388` comment).
- `CONF_CAPACITY_FIXED_KW`: a `NumberSelector` box, 0-50 kW step 0.1, default
  `VREG_CAPACITY_FLOOR_KW` (2.5 kW, the regulated minimum monthly peak Fluvius bills
  against; `const.py:199`).

Energy-dashboard default: before rendering, `async_step_capacity` copies
`self._data` and calls `_apply_energy_manager_capacity_default`
(`config_flow.py:631`). That helper walks dashboard kWh grid source -> Riemann
`integration` helper config entry -> the helper's `source` (the kW sensor) and
pre-fills `CONF_CAPACITY_PEAK_SENSOR` only when that source is a real power sensor
(device_class power/apparent_power, or unit W/kW/VA/kVA). It is skipped when the
user already picked a sensor, the energy component is not loaded, there is no grid
source, or the consumption sensor is not a Riemann child (`config_flow.py:643`
comment). A non-power source is left blank so the device_class-filtered picker
forces a deliberate choice (issue #19 again, `config_flow.py:691`).

### `connection_power`: Brussels connection-power tier

Schema `_connection_power_schema` (`config_flow.py:283`), default
`DEFAULT_CONNECTION_KVA_TIER` = `le6` (`const.py:156`). Options are the four
residential tiers `CONNECTION_KVA_TIERS` (`const.py:150`): `le1_44`, `le6`,
`le9_6`, `le13`, `translation_key="connection_kva_tier"`. Reached from
`_before_solar` when region is Brussels (`config_flow.py:949`). Brussels bills a
Brugel OSP (Obligations de Service Public) annual fee scaled by contractual
connection power, so the tier is asked before solar. Residential connections are
<=13 kVA, so only the four residential tiers are offered; the key is matched
against the parsed OSP table (`const.py:141`). Other regions have no such fee and go
straight to solar (`config_flow.py:949` comment).

### `solar`: inverter kVA + regime

Schema `_solar_schema` (`config_flow.py:712`). Fields:

- `CONF_SOLAR_KVA`: `NumberSelector` box 0-50 step 0.1, default 0.0 (0 means no
  panels, no prosumer cost; `const.py:179`).
- `CONF_SOLAR_REGIME`: `translation_key="solar_regime"`, options built from
  `SOLAR_REGIMES` (`const.py:190`) with a region filter.

The region filter (`config_flow.py:717`): `SOLAR_REGIME_COMPENSATION` is offered
only when `CONF_REGION == REGION_WALLONIA`. Compensation ("terugdraaiende teller" /
net-metering, "compteur qui tourne a l'envers") is Walloon-only: that meter pays
the prosumer tariff and no capacity tariff, so offering it in Flanders would
double-count the Flemish capaciteitstarief. Outside Wallonia only `none` and
`injection` apply. If the stored regime is not in the filtered list (for example a
compensation entry re-edited after switching region away from Wallonia), the default
falls back to `SOLAR_REGIME_NONE` (`config_flow.py:723`).

### `injection_api_key`: optional ENTSO-E token for spot-indexed injection

Schema is inline (`config_flow.py:896`), an *optional* `PASSWORD` field. The gate is
`_needs_injection_api_key` (`config_flow.py:856`), which is true when all of:

1. `CONF_SOLAR_REGIME == SOLAR_REGIME_INJECTION`,
2. no `CONF_API_KEY` was already collected (dynamic energy would have collected it),
3. `_contract_has_spot_injection(supplier, contract)` is true.

`_contract_has_spot_injection` (`config_flow.py:165`) reads the registry's
`Contract.spot_indexed_injection` flag (`providers/base.py:75`). That flag marks a
non-dynamic product whose *injection* is a per-hour spot formula with no printed
monthly indicative, currently Cociter Variable: the energy is priced without a spot
but the feed-in credit needs the day-ahead curve. Unlike the required `api_key`
step, this one is skippable (`config_flow.py:874` docstring): submitting blank pops
`CONF_API_KEY` and continues to `meters`, leaving the injection price unavailable
until a key is added via Reconfigure. A typed key is validated by
`_validate_entsoe_key` the same way as the dynamic step (`config_flow.py:890`).

### `meters`: cumulative kWh sensors (current-year cost)

Schema `_meters_schema` (`config_flow.py:413`). All six fields are optional
`EntitySelector`s restricted to `device_class="energy"` (`config_flow.py:430`) so a
power/temperature/unitless sensor cannot be read as raw kWh. There are two wirings
per side, both feeding the `current_year_cost` computation:

| Wiring | Keys | Behaviour |
| --- | --- | --- |
| Day/night registers | `CONF_DAY_CONSUMPTION_KWH`, `CONF_NIGHT_CONSUMPTION_KWH`, `CONF_DAY_INJECTION_KWH`, `CONF_NIGHT_INJECTION_KWH` | Used as-is; exact from the start, no warm-up |
| Single cumulative totals | `CONF_CONSUMPTION_KWH`, `CONF_INJECTION_KWH` | Coordinator splits deltas into day/night via `is_offpeak(now)` and persists them (`config_flow.py:413` docstring; `const.py:158`) |

When both are filled for the same side, the day/night registers win (more accurate;
`config_flow.py:422`). Each side (consumption, injection) is resolved independently,
so the user can mix one side as registers and the other as a total
(`strings.json:79`).

Energy-dashboard defaults: `async_step_meters` copies `self._data` and calls
`_apply_energy_manager_defaults` (`config_flow.py:560`) before rendering, but only
when *none* of the six keys is already set (`config_flow.py:574`). It reads the
dashboard's grid source `flow_from[0].stat_energy_from` (consumption) and
`flow_to[0].stat_energy_to` (injection), accepting them only when the statistic id
starts with `sensor.` (a recorder-only statistic id would render as a broken
`EntitySelector` default; `config_flow.py:607`). For each side it then tries
`_utility_meter_day_night_children` (`config_flow.py:481`) to also pre-fill the
day/night registers from a `utility_meter` helper rooted at the same source. That
helper:

- checks UI-configured `utility_meter` config entries (source + tariffs in options),
  and YAML-configured helpers (source/tariff from live state attributes),
- classifies each tariff name via `_classify_tariff` (`config_flow.py:456`), which
  tokenizes on `_-`/whitespace and matches English/French/Dutch day/night tokens
  (`peak/day/jour/dag/piek` vs `night/nuit/nacht/dal`, plus a contiguous `offpeak`
  special-case),
- bails to `{}` on any ambiguity (a name carrying both a day and a night token, or
  two children mapping to the same slot), because a wrong day/night pick mis-bills
  the year cost (`config_flow.py:499` comment).

Anything pre-filled stays editable (`strings.json:79`).

## Validation and rejection rules

| Rule | Where | Reason |
| --- | --- | --- |
| Supplier has no contract in region -> abort `supplier_region_unavailable` | `config_flow.py:782` | Region filtering deferred from the supplier step to here |
| Dynamic/TOU/Impact contract forces `METER_DYNAMIC` | `config_flow.py:310` | Smart meter required; mixing bi-horaire network with TOU energy mis-bills |
| `dso_tariff_mode` (incl. Impact) only in Wallonia | `config_flow.py:930` | Impact is CWaPE-only; other regions bill differently |
| `capacity` step only in Flanders | `config_flow.py:963`, `:968` | Only Flanders has the capaciteitstarief |
| `connection_power` step only in Brussels | `config_flow.py:949` | Only Brussels charges the Brugel OSP fee |
| Compensation regime only in Wallonia | `config_flow.py:717` | Avoids double-counting the Flemish capacity tariff |
| Peak sensor restricted to power/apparent_power | `config_flow.py:392` | Issue #19: a kWh sensor would inflate the capacity bill |
| kWh sensors restricted to device_class energy | `config_flow.py:430` | A non-energy sensor would be read as raw kWh |
| ENTSO-E key validated live before finalize | `config_flow.py:343` | Prevents finalizing an entry that fails on first refresh |
| Duplicate (supplier, contract, region, dso) tuple rejected | `config_flow.py:996`, `:1050` | Two coordinators on the same tuple double-poll the supplier |

Note on partial register-pair wiring: the *config flow* accepts any subset of the
six kWh fields (all are `vol.Optional`). The "partial register-pair wiring on either
side is rejected" rule described in `const.py:158` is enforced downstream in the
coordinator's `current_year_cost` engine (each side needs *both* day and night, or
falls back to the single total), not in the flow. The flow's job is only to collect
entity ids; it does not couple the day and night fields.

### Unique id and duplicate rejection

The unique id is the string `supplier:contract:region:dso`. On install,
`BePricesConfigFlow._after_meter` (`config_flow.py:996`) sets it after the meter
step and calls `_abort_if_unique_id_configured`; the same tuple already running its
own coordinator would double-poll the supplier and break shared-snapshot dedup. The
OptionsFlow enforces the same at finalize (`config_flow.py:1050`): if the edited
tuple differs from the entry's current unique id, it scans other `DOMAIN` entries and
aborts `already_configured` on a collision. The abort strings are
`config.abort.supplier_region_unavailable` / `already_configured` (`strings.json:90`).

### Defaults selection pattern

Every schema builder follows the same "default only if still valid" pattern so a
stale stored value never renders as an invalid pre-selection:

- `_contract_schema` defaults `CONF_CONTRACT` only if it is in the region-filtered
  id set (`config_flow.py:250`).
- `_dso_schema` defaults `CONF_DSO` only if it is a valid slug for the region
  (`config_flow.py:262`).
- `_meter_schema` clears the default when the stored meter is not in the
  kind-narrowed option list (`config_flow.py:317`).
- `_solar_schema` falls back to `none` when the stored regime is filtered out
  (`config_flow.py:723`).

## Options flow

`BePricesOptionsFlow` (`config_flow.py:1020`) opens on `async_step_init`
(`config_flow.py:1030`) with a two-item menu (`async_show_menu`):

| Menu option | Step | Effect |
| --- | --- | --- |
| `edit` | `async_step_edit` (`config_flow.py:1038`) | Re-run the whole step chain pre-filled, save back to `entry.data` |
| `compare` | `async_step_compare` (`config_flow.py:1100`) | One-off quote against another supplier; nothing saved |

Menu labels live in `options.step.init.menu_options` (`strings.json:103`).

### Edit path

`async_step_edit` seeds `self._data = {**config_entry.data, **config_entry.options}`
(`config_flow.py:1041`) so every downstream schema pre-fills from the live entry,
then hands off to `async_step_contract`, joining the exact same shared chain as the
install flow. Because supplier/region/contract/DSO are all editable, the live
choices are re-read on each render: `_supplier_options`, `_contracts_for`,
`_region_dso_options` all query the registry and `DSO_CHOICES` fresh, so a supplier
that added or dropped a product since install shows the current catalogue. The
kind-dependent meter narrowing and every region branch re-evaluate against the
edited values, so changing region from Flanders to Wallonia mid-edit drops the
capacity step and adds the `dso_tariff_mode` step on the next pass.

`_finalize` (`config_flow.py:1050`):

1. Recomputes the unique id from the edited tuple and aborts `already_configured`
   on collision with another entry (`config_flow.py:1058`).
2. Computes the new title via `_entry_title` (`config_flow.py:749`),
   `"<supplier label> - <contract label> (<Region>)"`.
3. Skips the write entirely when nothing changed. The no-op check compares against
   the *merged* `{**data, **options}` (`config_flow.py:1077`), not `entry.data`
   alone; `self._data` was seeded from that merge, so comparing against
   `entry.data` would never match for an entry that already carried options and
   would force a needless reload on every re-edit. When unchanged in data, title,
   and unique id, the write is skipped so HA's update listener does not tear down
   entities and the warmed snapshot for no benefit (`config_flow.py:1065` comment).
4. Otherwise calls `async_update_entry(data=self._data, options={}, title=...,
   unique_id=...)`: values persist to `entry.data`, stale options are discarded, the
   title and unique id refresh. It returns `async_create_entry(title="", data={})`,
   the OptionsFlow idiom for "I already wrote the entry myself".

Reconfigure vs re-add: there is no separate `async_step_reconfigure`; editing an
existing entry through the options `edit` path *is* the reconfigure surface, and it
mutates `entry.data` in place (same entry id, entities preserved unless supplier/
contract/region/DSO changed enough to force a reload via the update listener).
Adding a brand new entry from scratch goes through `BePricesConfigFlow` and is
rejected as a duplicate if it collides with an existing tuple; the message tells the
user to edit the existing entry instead (`strings.json:92`).

### Compare path (one-off quote, nothing saved)

The compare branch (`config_flow.py:1093` onward) walks `compare -> compare_contract
-> compare_meter -> (compare_api_key) -> compare_result` and exits via
`async_abort`, so it creates no entry and writes no options. Region, DSO, solar,
peak, and DSO mode all stay fixed to the current entry so the quote is
apples-to-apples; only supplier, contract, and (for static targets) meter vary.

| Step | Method | Notes |
| --- | --- | --- |
| `compare` | `config_flow.py:1100` | Supplier picker via `_compare_supplier_options` (`config_flow.py:182`): suppliers with at least one contract in the user's region. Aborts `compare_no_alternative` if none |
| `compare_contract` | `config_flow.py:1128` | Contract picker via `_compare_contract_schema` (`config_flow.py:199`), spans static and dynamic kinds; excludes the user's current contract only when the same supplier is picked. Aborts `compare_no_alternative` when nothing remains |
| `compare_meter` | `config_flow.py:1166` | Only for static targets; dynamic/TOU/TOU-Impact targets are forced to `METER_DYNAMIC` and skip the step (`config_flow.py:1189`) |
| `compare_api_key` | `config_flow.py:1236` | Shown when `_after_compare_meter` (`config_flow.py:1208`) finds the quote needs spot data the entry lacks: a dynamic target, or (injection regime) a spot-indexed-injection contract on *either* side. Key used only for the quote, not saved (`strings.json:141`) |
| `compare_result` | `config_flow.py:1264` | Renders a side-by-side annual + YTD estimate via `_build_compare_placeholders` (`config_flow.py:1277`); submit aborts `compare_done` |

The compare-meter narrowing mirrors the install `_meter_schema` exactly (dynamic/
tou/tou_impact all require a smart meter; `config_flow.py:1184` comment). The
compare result never mutates coordinator state: when it must borrow the historical
spot cache for a spot-indexed injection it saves and restores
`coord._historical_spots` around the fetch (`config_flow.py:1594`). Placeholder
tokens map to `options.step.compare_result.description` (`strings.json:146`), which
references `{meter_used}`, `{current_annual}`, `{delta_ytd}`, the ASCII bar charts
`{annual_chart}`/`{ytd_chart}`, and so on; `_build_compare_placeholders` always
populates every token (even the reloading-entry fallback at `config_flow.py:1300`)
so HA never renders a raw `{token}`.

## strings.json and translations

Every step id, field key, selector option, abort reason, and error code the flow
emits has a matching entry under `config.step.*` / `config.abort.*` / `config.error.*`
(install) and `options.step.*` (options) in `strings.json`. The correspondence:

| Flow surface | strings.json path |
| --- | --- |
| `step_id="user"` + fields | `config.step.user` (`strings.json:4`) |
| Each `async_step_<x>` | `config.step.<x>` (title, description, `data.<CONF>`) |
| `errors[CONF_API_KEY]="invalid_api_key"` | `config.error.invalid_api_key` (`strings.json:97`) |
| `async_abort(reason="...")` | `config.abort.<reason>` (`strings.json:90`) |
| `translation_key=` on a selector | `selector.<key>.options.*` (`strings.json:229`) |
| Options menu + every options step | `options.step.*` (`strings.json:101`) |

The `translation_key` selectors (`region`, `capacity_mode`, `meter`,
`dso_tariff_mode`, `connection_kva_tier`, `solar_regime`, and `supplier` in the
compare step) resolve their option labels from `selector.<key>.options`
(`strings.json:229`), not from the raw enum values. The options-flow steps reuse the
config-flow strings through `[%key:component::be_electricity_prices::config::step::...%]`
references (for example `options.step.edit.title` -> `config.step.user.title`,
`strings.json:111`), so the same text is not duplicated.

`translations/en.json` is the compiled/expanded form of `strings.json`: identical
except that the `[%key:...%]` cross-references in the options section are resolved to
their literal English text (verified by diffing the two files; the only differences
are the expanded key references). The `de.json`, `fr.json`, and `nl.json` files
mirror the same key structure with translated values. When you add or rename a step,
field, selector option, abort reason, or error code in `config_flow.py`, add the
matching key to `strings.json` and to all four translation files (`en/de/fr/nl`),
keeping the option enums (meter types, regimes, tariff modes, kVA tiers) in lockstep
with `const.py`.

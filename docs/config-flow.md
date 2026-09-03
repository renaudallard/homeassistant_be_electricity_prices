# Config and options flow

This document covers the config-flow package -- `config_flow.py` plus the four
modules split out of it (`flow_schemas.py`, `flow_prefill.py`, `compare_flow.py`,
`compare_quote.py`) -- the multi-step wizard that turns a user's
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

`config_flow.py` defines three classes (a fourth, `_CompareStepsMixin`, lives in
`compare_flow.py`):

| Class | Base | Role |
| --- | --- | --- |
| `_WizardStepsMixin` | - | The shared step chain (`async_step_contract` through `async_step_meters`) plus the branch helpers (`config_flow.py:503`) |
| `BePricesConfigFlow` | `_WizardStepsMixin, ConfigFlow` | Install-time flow; entry step `async_step_user`, finalizes with `async_create_entry` (`config_flow.py:711`) |
| `BePricesOptionsFlow` | `_WizardStepsMixin, OptionsFlow` | Post-install; menu -> `edit` (re-runs the chain pre-filled) or `compare` (throwaway quote) (`config_flow.py:736`) |

Both flows walk the *same* chain: `supplier/region -> contract -> (signed_rate) ->
dso -> meter ->
(dso_tariff_mode) -> (api_key) -> (custom_energy) -> (capacity) ->
(connection_power) -> solar -> (injection_api_key) -> (custom_injection) ->
(custom_dso) -> (custom_tax) -> meters`. The four `custom_*` steps run only for
the expert custom supplier. Only the entry step and `_finalize` differ. The
mixin's docstring at `flow_prefill.py:214` states the invariant: `_after_meter` is
overridden in `BePricesConfigFlow` to add the install-time unique-id reject, and
`_finalize` is abstract (`config_flow.py:666` raises `NotImplementedError`).

The OptionsFlow pre-fills every field with the current value, so a user can change
anything post-install (including supplier, contract, and region). On finalize it
writes back to `entry.data` (not `entry.options`) and updates the entry title
(`config_flow.py:26` module docstring, `config_flow.py:321`).

## Config-flow step reference

Steps in call order. "Written keys" are the `CONF_*` values `self._data.update`
persists at that step. Steps in parentheses in the chain above are conditional;
the "Shown when" column gives the gate.

| Step id | Method | Asks | Writes | Shown when / branch |
| --- | --- | --- | --- | --- |
| `user` | `async_step_user` (`config_flow.py:711`) | Supplier, region | `CONF_SUPPLIER`, `CONF_REGION` | Always (install entry step) |
| `contract` | `async_step_contract` (`config_flow.py:198`) | Contract (region-filtered), optional start / end date | `CONF_CONTRACT`, `CONF_CONTRACT_START_DATE`, `CONF_CONTRACT_END_DATE` | Always. A supplier/region mismatch is now caught on the step where BOTH are chosen (`_region_mismatch_error`) and re-shows that form with `supplier_region_unavailable` on the supplier field, instead of aborting a step later and discarding every other edit made in the same options run; rejects a future start or an end not after the start |
| `signed_rate` | `async_step_signed_rate` (`config_flow.py:258`) | The rate actually signed at: single / peak / offpeak / exclusive night, or spot factor / base, plus the yearly fee | The 6 `CONF_MANUAL_*` keys (`_MANUAL_RATE_KEYS`) | `_needs_manual_rate` true (`config_flow.py:230`): a start date is set on a fixed, dynamic or spot-monthly contract of a non-custom supplier (the two spot-priced kinds sign a coefficient pair, so they get the factor / base boxes; fixed gets the rate boxes). Offered whether or not the supplier archives past cards, because what is typed wins over the archived card |
| `dso` | `async_step_dso` (`config_flow.py:275`) | Distribution operator | `CONF_DSO` | Always |
| `meter` | `async_step_meter` (`config_flow.py:286`) | Meter type | `CONF_METER` | Always; option list narrows by contract kind |
| `dso_tariff_mode` | `async_step_dso_tariff_mode` (`config_flow.py:533`) | DSO billing mode (simple/bi/impact) | `CONF_DSO_TARIFF_MODE` | Region == Wallonia AND the contract is not `tou_impact` (`config_flow.py:533`) |
| `api_key` | `async_step_api_key` (`config_flow.py:383`) | ENTSO-E token (required) | `CONF_API_KEY` | Contract kind == `dynamic` or `spot_monthly` (both are spot-indexed) |
| `custom_energy` | `async_step_custom_energy` | Commodity formula (mode-dependent fields) | `CONF_CUSTOM_ENERGY_*`, `CONF_CUSTOM_YEARLY_FIXED_FEE` | Custom supplier only, after the energy/api-key step. The peak / off-peak energy boxes carry **no default** (`_add_custom_num(..., fallback=True)`): the pricing engine falls back to the single rate when they are absent, and a `vol.Optional` default is submitted verbatim when the user leaves the box alone, which wrote 0,00 into the entry and billed zero. They are shown for **both** `bi` and `dynamic` meters, matching `bi_capable` in `pricing.py:291`; gating on `bi` alone billed a fixed contract on a smart meter at the single rate for all 24 hours |
| `capacity` | `async_step_capacity` (`config_flow.py:415`) | Peak source (sensor/fixed) + value | `CONF_CAPACITY_MODE`, `CONF_CAPACITY_PEAK_SENSOR`, `CONF_CAPACITY_FIXED_KW` | Region == Flanders (`config_flow.py:415`) |
| `connection_power` | `async_step_connection_power` (`config_flow.py:573`) | Brussels connection-power tier | `CONF_CONNECTION_KVA_TIER` | Region == Brussels (`config_flow.py:573`) |
| `solar` | `async_step_solar` (`config_flow.py:431`) | Inverter kVA + regime | `CONF_SOLAR_KVA`, `CONF_SOLAR_REGIME` | Always |
| `injection_api_key` | `async_step_injection_api_key` (`config_flow.py:468`) | ENTSO-E token (optional) | `CONF_API_KEY` | `_needs_injection_api_key` true (`config_flow.py:441`) |
| `custom_injection` | `async_step_custom_injection` | Injection formula (flat / spot / monthly-mean, floor; plus an SPP-weighted toggle on the monthly-average mode) | `CONF_CUSTOM_INJECTION_*` | Custom supplier on the injection regime |
| `custom_dso` | `async_step_custom_dso` | Hand-entered DSO overlay (region/meter-relevant fields) | `CONF_CUSTOM_DSO_*` | Custom supplier only. The `distribution_peak` / `distribution_offpeak` / `distribution_exclusive_night` boxes carry **no default**, for the same reason as the energy ones: they all fall back to `distribution_single`, so a submitted 0,00 zeroes the network leg. The bi-hourly pair is shown for **both** `bi` and `dynamic` meters, matching `pricing.network_eur_per_kwh` (`pricing.py:562`), which routes both through that split when the DSO mode is not `simple`. A dynamic / TOU contract forces `METER_DYNAMIC`, so gating on `bi` alone left those entries unable to supply the rates their own network leg bills on. The Walloon CWaPE **Impact triplet** (`pic` / `medium` / `eco`) carries no default for a sharper version of the same reason: `network_eur_per_kwh` takes the Impact branch as soon as all three are non-None, so a defaulted 0,00 does not fall back to the single rate, it bills **no distribution at all** in every band and every hour. A Walloon Impact entry that filled in only `distribution_single` lost 0,1198 EUR/kWh, about EUR 419/yr at 3500 kWh, across the live tick, the year-to-date walk, the backfill and the compare quote, and raised no Repairs card because `_sync_impact_gap_issue` tests for `None` and a stored zero is not `None`. Entries that already hold the zeros are cleared by `_migrate_zeroed_custom_impact_bands` at setup, which drops an **all-zero** triplet only: a genuine tariff has no zero bands, and a partly filled one is the user's own data |
| `custom_tax` | `async_step_custom_tax` | Hand-entered taxes/levies + VAT rate | `CONF_CUSTOM_TAX_*`, `CONF_CUSTOM_VAT_RATE` | Custom supplier only |
| `meters` | `async_step_meters` (`config_flow.py:503`) | kWh sensors (registers or totals) | 6 `CONF_*_KWH` keys | Always (final step, then `_finalize`). Rejects a **half-wired day/night pair** with `register_pair_incomplete` on the night field: the coordinator needs both halves or neither (`_resolve_daily_kwh`, `_hourly_consumption_sensors`), and one half alone silently collapsed `current_year_cost` to the fees-only floor with no error, repair or visible log line |

### Flow diagram

```
                  ┌──────────────────────────────────────────────┐
                  │ user (install)  /  edit (options)            │
                  │   CONF_SUPPLIER, CONF_REGION                 │
                  └───────────────────────┬──────────────────────┘
                                          │
                          async_step_contract  (region-filtered)
                                          │  abort if no contract in region
      _after_contract → _needs_manual_rate? ┼── yes → async_step_signed_rate
                                          │              │ (all fields optional)
                          async_step_dso  ◄─────────────┘
                                          │
                          async_step_meter  (kind-narrowed list)
                                          │
                          _after_meter  ── install adds unique-id reject
                                          │
                     region == wallonia? ─┼── yes → async_step_dso_tariff_mode
                                          │        (skipped for tou_impact,
                                          │         which forces mode=impact)
                          _after_dso_tariff_mode  ◄────────────┘
                                          │
       kind == dynamic | spot_monthly? ──┼── yes → async_step_api_key
                                          │            (validate ENTSO-E)
                          _after_api_key  ◄───────────┘
                                          │
                       _after_energy_key  ── custom? → async_step_custom_energy
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
                          _custom_tail  ── custom? → (custom_injection) →
                                          │            custom_dso → custom_tax
                          async_step_meters  ◄──────────────────┘
                                          │
                          _finalize  (create entry / update entry)
```

The branch helpers that join the conditional steps back into the main line are all
in the mixin: `_after_meter` (`config_flow.py:544`), `_after_dso_tariff_mode`
(`config_flow.py:501`), `_after_api_key` (`config_flow.py:602`), `_before_solar`
(`config_flow.py:493`), and `_after_solar` (`config_flow.py:454`).

## Step details and the billing constraint behind each branch

### `user` / `edit`: supplier + region

Schema `_user_schema` (`flow_schemas.py:392`). Two dropdowns:

- Supplier: `_supplier_options()` (`config_flow.py:180`) lists every registered
  extractor by `id`/`label`, minus any carrying `deprecated_until` (a supplier that
  has announced it is leaving the residential market -- you cannot sign up for a
  contract being transferred away). Region filtering happens at the *contract* step
  instead, so a supplier with no product in the chosen region aborts there with a
  clear message rather than being hidden.

  `_user_schema` serves BOTH the install step and the options-flow `edit` step, so
  it passes `keep=defaults.get(CONF_SUPPLIER)` and the filter re-admits the entry's
  own stored supplier. This is load-bearing, not defensive: HA's `SelectSelector`
  validates with `vol.In(options)`, so a default outside the option list makes every
  submit of the edit form fail and an existing entry on a withdrawn supplier becomes
  impossible to edit at all (`tests/test_options_flow.py`,
  `test_edit_branch_offers_a_withdrawn_supplier_it_already_has`).
- Region: the `REGIONS` tuple (`const.py:43`), rendered with `translation_key="region"`
  so `selector.region.options` in `strings.json:393` supplies the localized labels.

`async_step_user` seeds `self._data = {}` on first entry (`config_flow.py:711`).
The OptionsFlow's `edit` step seeds instead from `{**entry.data, **entry.options}`
(`config_flow.py:355`), which is why every later step can pre-fill.

### `contract`: region-filtered product list

Schema `_contract_schema` (`flow_schemas.py:417`). Contracts come from
`_contracts_for(supplier_id, region)` (`config_flow.py:200`), which reads
`get_extractor(supplier_id).contracts` and keeps only those whose
`Contract.regions` frozenset contains the region. `Contract` is defined at
`providers/base.py:61`; its `kind` is one of the `TariffKind` literals
`fixed | variable | dynamic | tou | tou_impact | spot_monthly` (`providers/base.py:53`).

Guard: `async_step_contract` aborts with `supplier_region_unavailable` when the
filtered list is empty (`flow_prefill.py:235`), for example a Flanders-only supplier
selected with region Wallonia. The default is pre-selected only when the stored
`CONF_CONTRACT` still exists in the filtered set (`flow_schemas.py:340`); a stale id
leaves the field unset so the user must repick.

### `dso`: distribution operator

Schema `_dso_schema` (`flow_schemas.py:587`). Options come from `DSO_CHOICES[region]`
(`const.py:101`) via `_region_dso_options` (`flow_schemas.py:228`): 8 Fluvius
sub-areas in Flanders, 5 operators in Wallonia, Sibelga only in Brussels. The DSO
keys are canonical and stored verbatim in `CONF_DSO`; `const.py:145` warns they are
"stable forever" because they key into `SupplierSnapshot.dsos`. As with the contract
step, a stored value is only defaulted when it is still a valid slug for the region
(`config_flow.py:440`).

### `meter`: type, narrowed by contract kind

Schema `_meter_schema` (`flow_schemas.py:878`). The key rule (`flow_schemas.py:878`):

- If contract kind is `dynamic`, `tou`, or `tou_impact`, the only option is
  `METER_DYNAMIC` and the default is `METER_DYNAMIC`.
- Otherwise the full `METER_TYPES` list applies (`mono`, `bi`, `dynamic`,
  `exclusive_night`; `const.py:230`) with `METER_MONO` as the fallback.

Why: dynamic/TOU/Impact contracts bill energy by quarter-hour or hour-of-day and
require a smart (SMR3) meter. Picking `bi` on a TOU contract would route
distribution through the bi-horaire DSO peak/offpeak split while the supplier still
billed energy by TOU slot, two billing modes that do not mix (`config_flow.py:647`
comment). `_contract_kind` (`flow_schemas.py:239`) resolves the kind from the
registry and returns `""` when the stored contract is no longer in the catalogue,
so a stale OptionsFlow entry still renders the meter step with a sensible default
rather than raising.

The `exclusive_night` meter is not a first-class branch of the wizard: per
`const.py:158`, a dedicated night circuit (electric water heater, night-storage
heater) is configured as a *second* config entry pointing at the exclusive-night
kWh sensor; the primary (day) meter stays mono/bi/dynamic. The night-circuit entry
gets its own unique id because that one meter type is appended to the key, so it no
longer collides with the household's main entry on the same
supplier:contract:region:dso tuple; see the unique-id note below.

### `dso_tariff_mode`: Wallonia-only DSO billing mode

Schema `_dso_tariff_mode_schema` (`flow_schemas.py:617`), default `DSO_MODE_BI_HORAIRE`.
Options are `DSO_TARIFF_MODES` = `simple | bi_horaire | impact` (`const.py:320`),
`translation_key="dso_tariff_mode"`.

Reached only when region is Wallonia (`_after_meter`, `config_flow.py:544`). Tarif
Impact is the CWaPE 3-band hour-of-day distribution tariff (PIC 17-22, MEDIUM 7-11
+ 22-1, ECO 1-7 + 11-17, per `strings.json:52`) and needs a smart meter. Outside
Wallonia only `simple`/`bi_horaire` are meaningful and the coordinator falls back
automatically when the DSO does not publish Impact rates (`const.py:168`), so the
step is skipped entirely (Brussels has only Sibelga, Flanders bills via the
capacity tariff; `config_flow.py:178` comment).

### `api_key`: ENTSO-E token for spot-indexed energy (required)

Schema `_api_key_schema` (`flow_schemas.py:911`), a `PASSWORD` text field. Reached
from `_after_dso_tariff_mode` when the contract kind is `dynamic` or
`spot_monthly` (both price off ENTSO-E spots — live per-slot for dynamic, monthly
mean for spot-monthly). The typed key is stripped, rejected outright when what is
left is empty, and otherwise validated live against the ENTSO-E day-ahead endpoint
by `_validate_entsoe_key` (`flow_schemas.py:922`) before the flow proceeds:

- returns `None` on success,
- `"invalid_api_key"` when ENTSO-E returns 401, *and* on an HTTP 200 that comes
  back as an empty `Acknowledgement_MarketDocument` with no `TimeSeries`, which
  `parse_day_ahead_xml` also raises `EntsoeAuthError` for,
- `"cannot_connect"` on transport/parse error, and on a document that parses but
  covers none of the requested window.

The two outcomes are handled differently, because only one of them is the user's to
fix. `"invalid_api_key"` keeps the user on the form: ENTSO-E answered and refused the
token. `"cannot_connect"` diverts to the `api_key_unreachable` **menu**
(`config_flow.py:325`), which offers *Check the key again* and *Continue without
verifying*, in that order. ENTSO-E was unreachable for over a day at the end of August
2026 and nobody could add a contract meanwhile (discussion #77): while the platform is
down there is no way to tell a good key from a bad one, so blocking setup only punishes
the user for someone else's outage.

It is a menu rather than a silent pass because the user is choosing to finish setup on
a key nothing has verified, and that choice should be theirs and visible. Continuing
stores the key as typed; the coordinator validates it for real on the first refresh and
raises the existing `entsoe_auth_failed` Repairs card if it was wrong. A re-check that
comes back `"invalid_api_key"` returns to the form with that error rather than leaving
a *continue anyway* the user should not take. Both key steps share the path, so the
optional injection key behaves the same way.

The blank check comes first for that reason: while ENTSO-E is down *every* key
validates as `"cannot_connect"`, an empty one included, so without it the menu
would offer to store `""`. That is the one value nothing downstream recovers from.
`_fetch_spot_prices` (`coordinator_spots.py:541`) raises `missing ENTSO-E API key`
before `fetch_day_ahead_or_fallback` is ever called, so an entry holding an empty
key never reaches the keyless energy-charts source that a merely *wrong* key would
have been priced from until ENTSO-E came back to reject it. The step answers
`"empty_api_key"` itself and sends no request.

The validator queries a 24h window anchored on yesterday (`flow_schemas.py:639`): a
quota-exhausted token returns 200 plus an empty acknowledgement, and the BE bidding
zone effectively never goes a full local day with no publication, so an empty 24h
response reliably means "key not usable" (quota or maintenance). That is why the
empty acknowledgement is grouped with the refusal rather than with the outage: it
keeps the user on the form instead of offering them the continue-anyway menu, and
so blocks them from finalizing an entry that would fail on its first refresh. The
two error strings map to `config.error.invalid_api_key` /
`config.error.cannot_connect`, and the blank one to `config.error.empty_api_key`
(`strings.json:170`).

### `capacity`: Flanders capacity-tariff peak source

Schema `_capacity_schema` (`flow_schemas.py:963`). Reached from `_after_api_key` or
`_after_dso_tariff_mode` when region is Flanders (`config_flow.py:592`, `:507`).
Fields:

- `CONF_CAPACITY_MODE`: `sensor` (default) or `fixed`, `translation_key="capacity_mode"`.
- `CONF_CAPACITY_PEAK_SENSOR`: an `EntitySelector` restricted to
  `device_class=["power","apparent_power"]` (`flow_schemas.py:673`). The restriction
  is deliberate (issue #19): a kWh/unitless/temperature sensor would inflate the
  capacity bill. The coordinator already scales W/kW/VA/kVA, but cutting the long
  tail at the picker is the only guarantee the bug class cannot recur
  (`flow_schemas.py:666` comment).
- `CONF_CAPACITY_FIXED_KW`: a `NumberSelector` box, 0-50 kW step 0.1, default
  `VREG_CAPACITY_FLOOR_KW` (2.5 kW, the regulated minimum monthly peak Fluvius bills
  against; `const.py:254`).

Pre-fill: before rendering, `async_step_capacity` copies `self._data` and calls
`_apply_energy_manager_capacity_default` (`flow_prefill.py:287`), which tries two
sources in order.

First `_dsmr_monthly_peak_sensor` looks for the meter's own monthly peak: a
registry entity on the `dsmr` platform whose `translation_key` is
`maximum_demand_current_month`, matched on the translation key rather than the
entity id because the user may rename the latter. That entity is what a Belgian
DSMR 5B meter publishes on the P1 port, and it is the highest quarter-hour
offtake of the month, i.e. exactly the quantity Fluvius bills. Preferring it
means the coordinator's hourly sampling cannot lose anything: the value is a
monthly maximum that only rises within a month, so reading it once an hour and
keeping the running max is lossless. Disabled entities are skipped, since they
never report a state.

Only when there is no such entity does the helper fall back to the Energy
dashboard walk: dashboard kWh grid source -> Riemann `integration` helper config
entry -> the helper's `source` (the kW sensor). That source is *instantaneous*
power, so the resulting peak is an hourly-sampled estimate of a quarter-hour
average rather than the billed figure; the config-flow description says so. The
fallback pre-fills `CONF_CAPACITY_PEAK_SENSOR` only when that source is a real power sensor
(device_class power/apparent_power, or unit W/kW/VA/kVA). It is skipped when the
user already picked a sensor, the energy component is not loaded, there is no grid
source, or the consumption sensor is not a Riemann child (`flow_prefill.py:92`
comment). A non-power source is left blank so the device_class-filtered picker
forces a deliberate choice (issue #19 again, `flow_prefill.py:143`).

### `connection_power`: Brussels connection-power tier

Schema `_connection_power_schema` (`flow_schemas.py:636`), default
`DEFAULT_CONNECTION_KVA_TIER` = `le6` (`const.py:358`). Options are the four
residential tiers `CONNECTION_KVA_TIERS` (`const.py:339`): `le1_44`, `le6`,
`le9_6`, `le13`, `translation_key="connection_kva_tier"`. Reached from
`_before_solar` when region is Brussels (`config_flow.py:584`). Brussels bills a
Brugel OSP (Obligations de Service Public) annual fee scaled by contractual
connection power, so the tier is asked before solar. Every band the card prints
is offered, not just the four at or below 13 kVA: a 3x400 V / 25 A residential
connection is 17,3 kVA, and the same answer also picks Sibelga's power term,
which bands at the same line (`fees.py:102`). The key is matched against the
parsed OSP table (`const.py:183`). Other regions have no such fee and go
straight to solar (`config_flow.py:206` comment).

### `solar`: inverter kVA + regime

Schema `_solar_schema` (`flow_schemas.py:1115`). Fields:

- `CONF_SOLAR_KVA`: `NumberSelector` box 0-50 step 0.1, default 0.0 (0 means no
  panels, no prosumer cost; `const.py:230`).
- `CONF_SOLAR_REGIME`: `translation_key="solar_regime"`, options built from
  `SOLAR_REGIMES` (`const.py:392`) with a region filter.

The region filter (`flow_prefill.py:169`): `SOLAR_REGIME_COMPENSATION` is offered
only when `CONF_REGION == REGION_WALLONIA`. Compensation ("terugdraaiende teller" /
net-metering, "compteur qui tourne a l'envers") is Walloon-only: that meter pays
the prosumer tariff and no capacity tariff, so offering it in Flanders would
double-count the Flemish capaciteitstarief. Outside Wallonia only `none` and
`injection` apply. If the stored regime is not in the filtered list (for example a
compensation entry re-edited after switching region away from Wallonia), the default
falls back to `SOLAR_REGIME_NONE` (`const.py:389`).

### `injection_api_key`: optional ENTSO-E token for spot-indexed injection

Schema is inline (`config_flow.py:139`), an *optional* `PASSWORD` field. The gate is
`_needs_injection_api_key` (`config_flow.py:441`), which is true when all of:

1. `CONF_SOLAR_REGIME == SOLAR_REGIME_INJECTION`,
2. no `CONF_API_KEY` was already collected (dynamic energy would have collected it),
3. `_contract_has_spot_injection(supplier, contract)` is true.

`_contract_has_spot_injection` (`flow_schemas.py:294`) reads the registry's
`Contract.spot_indexed_injection` flag (`providers/base.py:77`). That flag marks a
non-dynamic product whose *injection* is a per-hour spot formula with no printed
monthly indicative, currently the two Cociter variable cards: the energy is priced without a spot
but the feed-in credit needs the day-ahead curve. Unlike the required `api_key`
step, this one is skippable (`flow_schemas.py:965` docstring): submitting blank pops
`CONF_API_KEY` and continues to `meters`, leaving the injection price unavailable
until a key is added via Reconfigure. A typed key is validated by
`_validate_entsoe_key` the same way as the dynamic step (`flow_schemas.py:922`).

### `meters`: cumulative kWh sensors (current-year cost)

Schema `_meters_schema` (`flow_schemas.py:1048`). All six fields are optional
`EntitySelector`s restricted to `device_class="energy"` (`flow_schemas.py:727`) so a
power/temperature/unitless sensor cannot be read as raw kWh. A stored entity id is
rendered as a `description={"suggested_value": ...}`, never a `default`: ha-form
omits a blanked selector from `user_input` entirely and voluptuous re-injects a
default, so a wired sensor came straight back and could not be unwired. The step
handler pops any of `_METER_SENSOR_KEYS` missing from `user_input`, which is what
actually clears one. The capacity-peak picker uses the same shape. There are two
wirings per side, both feeding the `current_year_cost` computation:

| Wiring | Keys | Behaviour |
| --- | --- | --- |
| Day/night registers | `CONF_DAY_CONSUMPTION_KWH`, `CONF_NIGHT_CONSUMPTION_KWH`, `CONF_DAY_INJECTION_KWH`, `CONF_NIGHT_INJECTION_KWH` | Used as-is; exact from the start, no warm-up |
| Single cumulative totals | `CONF_CONSUMPTION_KWH`, `CONF_INJECTION_KWH` | Coordinator splits deltas into day/night via `is_offpeak(now)` and persists them (`const.py:379` docstring; `const.py:379`) |

When both are filled for the same side, the day/night registers win (more accurate;
`flow_schemas.py:719`). Each side (consumption, injection) is resolved independently,
so the user can mix one side as registers and the other as a total
(`strings.json:323`). All three resolvers enforce that precedence:
`_kwh_sensor_ids` (daily path plus diagnostics), `_hourly_consumption_sensors`
and `_hourly_injection_sensors` (hourly path plus backfill). The hourly pair
used to check the totals sensor first, so a user with both wirings was billed
off a different meter depending on their contract kind and the two figures
drifted apart.

Energy-dashboard defaults: `async_step_meters` copies `self._data` and calls
`_apply_energy_manager_defaults` (`flow_prefill.py:201`) before rendering, but only
when *none* of the six keys is already set (`flow_schemas.py:869`). It reads the
dashboard's grid source `flow_from[0].stat_energy_from` (consumption) and
`flow_to[0].stat_energy_to` (injection), accepting them only when the statistic id
starts with `sensor.` (a recorder-only statistic id would render as a broken
`EntitySelector` default; `flow_schemas.py:907`). For each side it then tries
`_utility_meter_day_night_children` (`flow_prefill.py:85`) to also pre-fill the
day/night registers from a `utility_meter` helper rooted at the same source. That
helper:

- checks UI-configured `utility_meter` config entries (source + tariffs in options),
  and YAML-configured helpers (source/tariff from live state attributes),
- classifies each tariff name via `_classify_tariff` (`flow_prefill.py:60`), which
  tokenizes on `_-`/whitespace and matches English/French/Dutch day/night tokens
  (`peak/day/jour/dag/piek` vs `night/nuit/nacht/dal`, plus a contiguous `offpeak`
  special-case),
- bails to `{}` on any ambiguity (a name carrying both a day and a night token, or
  two children mapping to the same slot), because a wrong day/night pick mis-bills
  the year cost (`flow_schemas.py:845` comment),
- resolves each classified tariff to its child by matching the registry unique id
  **exactly** against `f"{entry_id}_{tariff}"`, the shape HA's `utility_meter`
  builds. It used to test `endswith(f"_{tariff}")`, which binds the wrong child
  whenever one tariff name ends with another across an underscore: with the common
  `["off_peak", "peak"]` pair, `<entry>_off_peak` also ends with `_peak`, so the day
  slot took the off-peak register and night kWh got billed at the day rate. As a
  backstop it also refuses a result where both slots resolved to the same entity.

Anything pre-filled stays editable (`strings.json:199`).

## Validation and rejection rules

| Rule | Where | Reason |
| --- | --- | --- |
| Supplier has no contract in region -> abort `supplier_region_unavailable` | `flow_prefill.py:235` | Region filtering deferred from the supplier step to here |
| Dynamic/TOU/Impact contract forces `METER_DYNAMIC` | `flow_schemas.py:749` | Smart meter required; mixing bi-horaire network with TOU energy mis-bills |
| `dso_tariff_mode` (incl. Impact) only in Wallonia | `config_flow.py:451` | Impact is CWaPE-only; other regions bill differently |
| `capacity` step only in Flanders | `config_flow.py:326` | Only Flanders has the capaciteitstarief |
| `connection_power` step only in Brussels | `config_flow.py:209` | Only Brussels charges the Brugel OSP fee |
| Compensation regime only in Wallonia | `flow_prefill.py:169` | Avoids double-counting the Flemish capacity tariff |
| Peak sensor restricted to power/apparent_power | `flow_schemas.py:673` | Issue #19: a kWh sensor would inflate the capacity bill |
| kWh sensors restricted to device_class energy | `flow_schemas.py:727` | A non-energy sensor would be read as raw kWh |
| ENTSO-E key validated live before finalize | `config_flow.py:688` | Prevents finalizing an entry that fails on first refresh |
| Duplicate (supplier, contract, region, dso) tuple rejected | `config_flow.py:611-612`, `:667` | Two coordinators on the same tuple double-poll the supplier |

Note on partial register-pair wiring: the *config flow* accepts any subset of the
six kWh fields (all are `vol.Optional`). The "partial register-pair wiring on either
side is rejected" rule described in `const.py:200` is enforced downstream in the
coordinator's `current_year_cost` engine (each side needs *both* day and night, or
falls back to the single total), not in the flow. The flow's job is only to collect
entity ids; it does not couple the day and night fields.

All three billing paths share one predicate for that rule,
`_partial_register_pair` (`coordinator.py`). Only the static per-day path used to
enforce it: the hourly path (TOU / Impact / dynamic / exclusive-night) and the
backfill resolved each side independently and bailed only when BOTH were empty, so
a half-wired consumption pair collapsed to "no consumption sensors" while a wired
injection sensor kept crediting. That billed the feed-in credit against zero
consumption and drove the YTD negative instead of resting on the fees-only floor.

### Unique id and duplicate rejection

The unique id is built by `_unique_id_for` (`config_flow.py:670`): the string
`supplier:contract:region:dso`, **plus the meter for an exclusive-night circuit**.
On install, `BePricesConfigFlow._after_meter` (`config_flow.py:430`) sets it after
the meter step and calls `_abort_if_unique_id_configured`; the same tuple already
running its own coordinator would double-poll the supplier and break shared-snapshot
dedup. The OptionsFlow enforces the same at finalize (`config_flow.py:487`): if the
edited key differs from the entry's current unique id, it scans other `DOMAIN`
entries and aborts `already_configured` on a collision. The abort strings are
`config.abort.supplier_region_unavailable` / `already_configured`.

Only the exclusive-night meter extends the key, for a reason worth keeping straight.
That circuit is a whole-entry meter type, so the docs tell the user to add it as a
second entry — but a household has one contract on one DSO, so that second entry
carried the identical tuple and **always** aborted: the documented setup could not be
performed at all. Appending just that one meter unblocks it while the standard meters
keep claiming the exact string existing entries were created with, so an existing
entry still matches and a real duplicate is still caught, and two night circuits on
one tuple still collide with each other. It does not reintroduce the double poll
either: the snapshot, archive and spot caches are shared per
(supplier, contract, region) across entries. Install and edit **must** build the key
through the same helper, or editing a night-circuit entry computes the plain tuple,
finds the main entry holding exactly that, and aborts.

### Defaults selection pattern

Every schema builder follows the same "default only if still valid" pattern so a
stale stored value never renders as an invalid pre-selection:

- `_contract_schema` defaults `CONF_CONTRACT` only if it is in the region-filtered
  id set (`config_flow.py:331`).
- `_dso_schema` defaults `CONF_DSO` only if it is a valid slug for the region
  (`config_flow.py:440`).
- `_meter_schema` clears the default when the stored meter is not in the
  kind-narrowed option list (`config_flow.py:662`).
- `_solar_schema` falls back to `none` when the stored regime is filtered out
  (`flow_prefill.py:176`).

## Options flow

`BePricesOptionsFlow` (`config_flow.py:736`) opens on `async_step_init`
(`config_flow.py:343`) with a two-item menu (`async_show_menu`):

| Menu option | Step | Effect |
| --- | --- | --- |
| `edit` | `async_step_edit` (`config_flow.py:757`) | Re-run the whole step chain pre-filled, save back to `entry.data` |
| `compare` | `async_step_compare` (`compare_flow.py:1542`) | One-off quote against another supplier; nothing saved |

Menu labels live in `options.step.init.menu_options` (`strings.json:193`).

### Edit path

`async_step_edit` seeds `self._data = {**config_entry.data, **config_entry.options}`
(`config_flow.py:355`) so every downstream schema pre-fills from the live entry,
then hands off to `async_step_contract`, joining the exact same shared chain as the
install flow. Because supplier/region/contract/DSO are all editable, the live
choices are re-read on each render: `_supplier_options`, `_contracts_for`,
`_region_dso_options` all query the registry and `DSO_CHOICES` fresh, so a supplier
that added or dropped a product since install shows the current catalogue. The
kind-dependent meter narrowing and every region branch re-evaluate against the
edited values, so changing region from Flanders to Wallonia mid-edit drops the
capacity step and adds the `dso_tariff_mode` step on the next pass.

`_finalize` (`config_flow.py:666`):

1. Recomputes the unique id from the edited tuple and aborts `already_configured`
   on collision with another entry (`config_flow.py:377`).
2. Computes the new title via `_entry_title` (`config_flow.py:130`),
   `"<supplier label> - <contract label> (<Region>)"`.
3. Skips the write entirely when nothing changed. The no-op check compares against
   the *merged* `{**data, **options}` (`config_flow.py:390`), not `entry.data`
   alone; `self._data` was seeded from that merge, so comparing against
   `entry.data` would never match for an entry that already carried options and
   would force a needless reload on every re-edit. When unchanged in data, title,
   and unique id, the write is skipped so HA's update listener does not tear down
   entities and the warmed snapshot for no benefit (`config_flow.py:379` comment).
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
user to edit the existing entry instead (`strings.json:166`).

### Compare path (one-off quote, nothing saved)

The compare branch (`compare_flow.py:286` onward) walks `compare -> compare_contract
-> compare_meter -> compare_solar -> (compare_api_key) -> compare_result` and exits
via `async_abort`, so it creates no entry and writes no options. Region, DSO and
peak stay fixed to the current entry so the quote is apples-to-apples;
supplier, contract, (for static targets) meter, the DSO tariff mode and the
solar regime vary.

The meter override applies to the TARGET side only: it is a billing mode the
quoted contract can differ on. So does the DSO tariff mode, and for one product
family it is not optional: a `tou_impact` card carries three CWaPE band rates
and no mono/bi structure at all, so the band schedule prices its energy whatever
the household is on, while the network leg and the Walloon terme fixe follow the
mode. Quoting such a target on the household's own bi-horaire settings therefore
banded the energy, billed the network off the standard jour/nuit columns and
charged a fixed term the incitative tariff does not have. The target side is
forced to `DSO_MODE_IMPACT` for that kind, mirroring what `_after_meter` already
does at install, and it rides the `_QuoteEntry` proxy rather than a parameter
because the fee leg and the year-to-date engine both read the mode straight off
`entry.data`. The gate is the registered kind, which leaves `totalenergies_impact`
out on purpose: it is registered `variable` and its impact bands are read only in
impact mode, so a household on the standard configuration quoting it still bills
the target's network leg off the jour/nuit columns, worth about EUR 29/yr on a bi
meter and EUR 113 on a mono one. It is left un-forced for the reason
`_IMPACT_DEFAULT_CONTRACTS` gives (`flow_schemas.py:614`): that card states only
that a communicating digital meter is required, so a holder on the standard
configuration genuinely exists and forcing would under-bill them by the same
amount in the other direction. The install flow pre-selects the mode for it and
lets the user say otherwise; the compare flow has no step to ask, so it does not
decide. The solar regime override applies to BOTH sides,
because the regime belongs to the grid connection rather than to the supplier, so
two suppliers at one address are necessarily on the same one. That symmetry is
also why it barely moves `delta_annual`, and why the result page prints the user's
own contract priced both ways inside `{solar_note}`: with both sides moving
together the printed supplier delta barely shifts, so without that clause the
question "what does leaving compensation cost me" would have no direct answer on
the page. The clause predates the picker offering the user's own contract
(`e2a52af`); picking yourself is now a second route to the same number, and the
table below is the authority on what the picker excludes.

| Step | Method | Notes |
| --- | --- | --- |

### The ranking branch

`_SweepStepsMixin` (`compare_flow.py:2388`) is a separate branch reached from a
third menu entry. It subclasses `_CompareStepsMixin` because it reuses
`_resolve_household` and the live-validated key prompt; only the menu entry and
the steps are separate. `_sweep_candidates` (`flow_schemas.py:321`) narrows to
the entry's own `KIND_GROUP`, region and professional segment, and drops the
entry's own contract - the opposite of the one-to-one picker, which keeps it on
purpose. An empty cell aborts `compare_all_no_alternatives`, which is an answer
rather than a failure: a Brussels `tou` household has exactly one slot contract
in the region and it is theirs.

The pricing itself is not in the flow. `_SweepEngine` (`compare_flow.py:508`)
holds only an entry, a hass and the dialog's what-if overrides, which is
everything `_resolve_household` (`compare_flow.py:830`), `_sweep_own_row`
(`compare_flow.py:1105`) and `_sweep_one` (`compare_flow.py:1407`) ever read
off the flow they used to live on. That is what lets a sweep run with nobody
watching. Faking a flow object would work today, since `OptionsFlow.config_entry`
resolves through the handler, but it would tie a scheduled job to flow-manager
internals that move between Home Assistant releases.

`build_sweep` (`compare_flow.py:778`) resolves the cell for both callers, so
the dialog and the schedule never drift on which contracts count. It returns
the abort reason as a string rather than raising, because the dialog turns
that into an abort and the scheduled run into a log line.

#### The scheduled ranking

Opt-in per entry (`CONF_DAILY_COMPARE`, default off), a box on the `meters`
step. `async_run_daily_compare` (`compare_flow.py:473`) drives
`run_full_sweep` (`compare_flow.py:715`) and parks the result on
`coordinator.daily_compare`, which is all the delivery the sensor needs: it is
a `CoordinatorEntity`, so setting the attribute and calling
`async_update_listeners` is the whole path, with no dispatcher.

Three things differ from the dialog's sweep, all for the same reason - nobody
is watching:

- **No budget and no skipping.** `COMPARE_SWEEP_BUDGET_S` exists because a
  progress bar is on screen. Stopping early on a schedule would publish a
  ranking whose cheapest row is only the cheapest that *fitted*.
- **Sequential, not gathered.** Sixteen suppliers at once is a burst on
  sixteen servers to save minutes nobody is waiting through, and the listing
  memo only pays off when candidates sharing a listing page run adjacently.
- **Failures are swallowed and logged.** A supplier that changed its site
  overnight must not take the entry down; the sensor keeps the previous
  answer and timestamps it.

The time of day is `crc32` of the entry id over `DAILY_COMPARE_WINDOW_MINUTES`,
the same spreading the midnight rebuild uses, salted so an entry does not land
on a time correlated with its rollover second. Derived rather than randomised
each day: an install that runs at a different time every day cannot be
reasoned about when it fails.

Cheap in the steady state. Twelve of the seventeen suppliers publish a
freshness probe, including the two slowest cards, so a day on which nothing
was republished costs a handful of conditional requests rather than the ~164 s
a cold sweep takes; cards move about monthly.

| Step | Method | Notes |
| --- | --- | --- |
| `compare_all` | `compare_flow.py:2139` | Resolves the cell through `build_sweep`. When the entry ranks on a schedule and a result is stored, jumps straight to the result step: the wait disappears rather than moving |
| `compare_all_progress` | `compare_flow.py:2184` | One `asyncio.Task` per candidate. HA re-renders a progress step only when the step returns a new result, and a step only returns when its task finishes, so one task for the whole sweep could never move the counter. The live task is re-shown before a new one is created, because the flow manager re-enters the step on every frontend poll |
| `compare_all_result` | `compare_flow.py:2330` | One `{ranking}` token carrying the whole table, plus the opt-in for the year-to-date pass. A stored ranking dates itself and offers `refresh`, which clears the rows and sweeps live; nothing is reported pending, since the scheduled run skipped nothing |
| `compare_all_ytd` | `compare_flow.py:2381` | Second pass, now a thin wrapper: the pass itself is `_SweepEngine.fill_ytd_column`, so the nightly sweep runs the same one. A row prints a figure only where it replayed the same real archived months the baseline did (`archived_months_present`) **and** where its feed-in can be credited: the pass takes the coordinator's historical spot cache, and `_needs_missing_spots` drops a row whose injection is spot-indexed when that cache is empty, since the credit is lost whole rather than approximated |

| `compare` | `compare_flow.py:409` | Supplier picker via `_compare_supplier_options` (`compare_flow.py:181`): suppliers with at least one contract in the user's region **and the entry's own segment**, excluding the expert `custom` supplier and any withdrawn one. Aborts `compare_no_alternative` if none |
| `compare_contract` | `compare_flow.py:319` | Contract picker via `_compare_contract_schema` (`compare_flow.py:214`), spans static and dynamic kinds but never crosses the residential/professional line: a pro card is published ex-VAT and bands the excise by annual volume, so `_resolve_snapshot` grosses it at the entry's own rate and the row is neither what the household would pay nor a contract it could sign. Excludes the user's current contract only when the same supplier is picked. Aborts `compare_no_alternative` when nothing remains |
| `compare_meter` | `compare_flow.py:355` | Only for static targets; dynamic/TOU/TOU-Impact targets are forced to `METER_DYNAMIC` and skip the step (`const.py:230`) |
| `compare_solar` | `compare_flow.py:397` | What-if solar regime via `_compare_solar_schema` (`flow_schemas.py:1143`), narrowed to the region by the shared `_regime_options` (`flow_schemas.py:1095`). Skipped for an entry with no solar. Reached from both exits of `compare_meter`, so a dynamic target gets it too |
| `compare_api_key` | `compare_flow.py:486` | Shown when `_after_compare_meter` (`compare_flow.py:1721`) finds the quote needs spot data the entry lacks: a spot-priced target (`SPOT_PRICED_CONTRACT_KINDS` - dynamic per slot, spot-monthly on the delivery month's mean), or (injection regime) a spot-indexed-injection contract on *either* side. Key used only for the quote, not saved. Skippable like `injection_api_key`: a blank submission asks ENTSO-E nothing and goes straight on, since a quote is a one-off and every reader of the key falls back to the entry's own with `or` |
| `compare_result` | `compare_flow.py:514` | Renders a side-by-side annual + YTD estimate via `_build_compare_placeholders` (`compare_flow.py:1816`); submit aborts `compare_done`. Each side is priced on the spot its own energy shape bills: a dynamic leg on the mean of the fetched day-ahead window (linear in spot, so the yearly average is that mean), a spot-monthly leg on the DELIVERY MONTH's mean, which is the flat rate it actually bills and does not move with the day the dialog opened |

The quoted supplier's freshly fetched card is resolved through
`snapshot_store._resolve_snapshot`, the same helper the live path uses, so it gets
**both** per-entry transforms: `apply_vat` and `resolve_excise_band`. It previously
called `apply_vat` alone, which priced a banded professional card at its first
excise tier however much the household uses (1,421 c€/kWh instead of 1,139 at
60 000 kWh/yr, about 169 EUR/yr) while the user's own side, coming off the
coordinator, was fully resolved — a comparison biased against the alternative.
Anything that turns a parsed card into a priced one belongs in that helper, not
inlined at a call site.

The compare-meter narrowing mirrors the install `_meter_schema` exactly (dynamic/
tou/tou_impact all require a smart meter; `config_flow.py:500` comment). The
compare result never mutates coordinator state: both places that borrow the
historical spot cache go through `_borrowed_spot_cache` (`compare_flow.py:139`),
which saves and restores `_historical_spots`, `_historical_spot_quarters` and
`_complete_spot_days` around the fetch — the completeness set travels with the
two dicts because a day listed there counts as fully present without consulting
them, so isolating the dicts alone would make the fetch skip every day the
coordinator had already walked. The month-mean borrow merges
(`compare_flow.py:753`); the YTD borrow isolates (`compare_flow.py:1307`).
The builder is in two halves: `_resolve_household` (`compare_flow.py:830`) resolves everything that does not depend on which contract is being quoted -- the meter reads, the recorder walk, the measured hour shapes, the day-ahead window -- and returns a `_HouseholdQuote` (`compare_flow.py:421`); the rest of `_build_compare_placeholders` is the target side, recomputed per contract. The household half is O(1) in the number of contracts compared, which is what makes quoting more than one affordable. Placeholder
tokens map to `options.step.compare_result.description` (`strings.json:236`), which
references `{meter_used}`, `{current_annual}`, `{delta_ytd}`, the ASCII bar charts
`{annual_chart}`/`{ytd_chart}`, `{card_note}` (per-side caveats about what a card
does not print, built by `_card_caveats`), and so on; `_build_compare_placeholders` always
populates every token (even the reloading-entry fallback at `compare_flow.py:491`)
so HA never renders a raw `{token}`.

## strings.json and translations

Every step id, field key, selector option, abort reason, and error code the flow
emits has a matching entry under `config.step.*` / `config.abort.*` / `config.error.*`
(install) and `options.step.*` (options) in `strings.json`. The correspondence:

| Flow surface | strings.json path |
| --- | --- |
| `step_id="user"` + fields | `config.step.user` (`strings.json:4`) |
| Each `async_step_<x>` | `config.step.<x>` (title, description, `data.<CONF>`) |
| `errors[CONF_API_KEY]="invalid_api_key"` | `config.error.invalid_api_key` (`strings.json:171`) |
| `errors[CONF_API_KEY]="empty_api_key"` | `config.error.empty_api_key` (`strings.json:172`) |
| `async_abort(reason="...")` | `config.abort.<reason>` (`strings.json:164`) |
| `translation_key=` on a selector | `selector.<key>.options.*` (`strings.json:391`) |
| Options menu + every options step | `options.step.*` (`strings.json:190`) |

The `translation_key` selectors (`region`, `capacity_mode`, `meter`,
`dso_tariff_mode`, `connection_kva_tier`, `solar_regime`, and `supplier` in the
compare step) resolve their option labels from `selector.<key>.options`
(`strings.json:391`), not from the raw enum values. The options-flow steps reuse the
config-flow strings through `[%key:component::be_electricity_prices::config::step::...%]`
references (for example `options.step.edit.title` -> `config.step.user.title`,
`strings.json:199`), so the same text is not duplicated.

`translations/en.json` is the compiled/expanded form of `strings.json`: identical
except that the `[%key:...%]` cross-references in the options section are resolved to
their literal English text (verified by diffing the two files; the only differences
are the expanded key references). The `de.json`, `fr.json`, and `nl.json` files
mirror the same key structure with translated values. When you add or rename a step,
field, selector option, abort reason, or error code in `config_flow.py`, add the
matching key to `strings.json` and to all four translation files (`en/de/fr/nl`),
keeping the option enums (meter types, regimes, tariff modes, kVA tiers) in lockstep
with `const.py`.

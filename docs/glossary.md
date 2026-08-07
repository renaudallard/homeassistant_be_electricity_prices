# Glossary

This document covers the Belgian-energy and Home Assistant domain vocabulary a
contributor meets while working on the `be_electricity_prices` integration. Each
entry is defined by how *this codebase* uses the term, with a reference into the
source where it is declared or consumed. References are relative to the
`custom_components/be_electricity_prices/` package unless prefixed with `scripts/`
or `tests/`; provider paths are under `providers/`. No EUR values are invented
here: any number shown is copied from a source comment or test and labelled
illustrative.

Related reading:

- [architecture.md](architecture.md) - module map and end-to-end data flow.
- [pricing-model.md](pricing-model.md) - how these terms combine into the all-in price.
- [provider-framework.md](provider-framework.md) - the extractor protocol and dataclasses many of these terms live in.
- [coordinator.md](coordinator.md) - snapshot, probe, and refresh lifecycle.
- [config-flow.md](config-flow.md) - the config keys (`CONF_*`) users set.

## How the pieces fit

The integration fuses three inputs into one all-in EUR/kWh. The glossary terms
below map onto these three layers plus the taxes:

```
                 SupplierSnapshot (one live tariff card)
                 =========================================
  energy formula      DSO overlay           tax overlay
  (EnergyRates)       (DsoOverlay, per       (TaxOverlay:
   fixed / variable    sub-area: distribution federal excise,
   / dynamic / tou /    + transport +          energy contribution,
   tou_impact)          capacity + prosumer)   regional renewables, VAT)
        |                    |                        |
        +---------- pricing.compute_breakdown --------+
                             |
                    all-in EUR/kWh (+ injection credit)
```

`all_in = (energy + distribution + transport + levies) * (1 + VAT)`
(README.md:145). The coordinator selects one `contract` and one `dso` sub-area
per config entry (`providers/base.py:478`).

## Terms

| Term | Definition (as used here) | Reference |
| --- | --- | --- |
| AIEG | Small Walloon DSO, one of the five Wallonia distribution operators offered in the config flow. The source carries only the key and display label, not a spelled-out name. | `const.py:58`, `const.py:113` |
| AIESH | Small Walloon DSO (Association Intercommunale d'Electricite du Sud-Hainaut). | `const.py:59`, `const.py:114` |
| ArchivedSnapshotFetcher | Optional per-supplier callable that fetches the published card for a specific `(year, month)` so past consumption bills at that month's real rate; returns `None` when the supplier has no accessible archive (overwrite-in-place or API-only suppliers). | `providers/base.py:525`, `providers/base.py:547` |
| all-in price | The single EUR/kWh the integration exposes: energy + distribution + transport + levies, times (1 + VAT). VAT spreads uniformly so the three component sensors sum to `current_price`. | README.md:155, README.md:172 |
| Belpex / eSpot_15 / EPEX / EPEX DA | Names Belgian suppliers print for the wholesale day-ahead spot their dynamic formula multiplies. The integration always sources this curve from ENTSO-E, not the branded feed; the labels are only documentation of what a card references. | `providers/base.py:148`, README.md:150 |
| bi-hourly meter (`METER_BI`) | Meter with two registers, peak (day) and off-peak (night). Drives the `peak` / `offpeak` energy and distribution columns. | `const.py:194`, `providers/base.py:101` |
| billing grid (quarter-hourly vs hourly) | The time resolution a contract is billed on. `DynamicRates.quarter_hourly` selects it: hourly by default (Frank, Luminus, Mega, TotalEnergies, Eneco), native 15-minute for suppliers whose cards multiply the 15-minute spot (Engie, Cociter, EBEM, Ecofix, OCTA+, Ecopower Dynamische Burgerstroom, Bolt Dynamisch, energie.be, EnergyVision). YTD billing stays hourly because HA only retains hourly long-term statistics. | `providers/base.py:143`, `providers/base.py:147` |
| Brugel | Brussels energy regulator. Sets the OSP annual fee tiers carried on the Sibelga overlay. | `const.py:180`, `providers/base.py:325` |
| Brussels (`REGION_BRUSSELS`) | One of the three Belgian regions. Single DSO (Sibelga), Brugel regulator, `brussels_renewables` levy. | `const.py:41`, `providers/base.py:524` |
| capacity tariff | Flanders-only network charge: `billed_peak_kw * capacity_eur_per_kw_year / 12`, where `billed_peak_kw` is `mean(max(monthly_peak, VREG_CAPACITY_FLOOR_KW))` over the last 12 months and one monthly peak is the highest quarter-hour offtake of that month (the floor applies per month, before the mean)`. | `const.py:238`, `providers/base.py:324`, README.md:53 |
| compensation regime (`SOLAR_REGIME_COMPENSATION`) | Walloon "compteur qui tourne a l'envers": injection nets against consumption. Only for installs certified before 2024-01-01, valid until 2030-12-31. Adds the DSO prosumer fee (per kVA) and, for some suppliers, a supplier PV forfait. | `const.py:282`, `const.py:282` |
| Contract | A dataclass for one product a supplier sells: `id`, `label`, `kind` (a `TariffKind`), the `regions` it is published in, and `spot_indexed_injection`. | `providers/base.py:86` |
| current_year_cost | Sensor: running bill since Jan 1, computed from HA's recorder per day (fixed/variable) or per hour (TOU/dynamic), with per-month archived cards and pro-rated annual fees. | README.md:55, README.md:177 |
| CWaPE | Walloon energy regulator. Defines the Tarif Impact 3-band hour-of-day schedule and the compensation-regime transition dates. | `const.py:166`, `providers/base.py:338` |
| CWaPE bands (pic / medium / eco) | The three hour-of-day bands of Tarif Impact, every day of the week: pic 17:00-22:00 (highest), medium 07:00-11:00 + 22:00-01:00, eco 01:00-07:00 + 11:00-17:00 (lowest). Both the energy side (`ImpactRates`) and the DSO side (`DsoOverlay.distribution_pic/medium/eco`) carry them. | `providers/base.py:241`, `providers/base.py:241` |
| data management fee | DSO fixed yearly fee for meter data management, `EUR/year`, added to annual fees in the YTD bill. | `providers/base.py:323` |
| DsoOverlay | Per-sub-area network cost dataclass: distribution (single / peak / offpeak / exclusive_night / impact bands), transport, data-management fee, capacity rate, prosumer rate, Brussels OSP table. | `providers/base.py:310` |
| DSO (distribution grid operator) | The regulated operator of the local low-voltage grid; its distribution + capacity charges are one of the three fused inputs. Selected per entry as `CONF_DSO`, keyed by a canonical sub-area string. | `const.py:145`, `const.py:145` |
| DSO overlay | The `DsoOverlay` for the one sub-area a snapshot's `dsos` dict is keyed by the user's `CONF_DSO`; the coordinator picks it and feeds `compute_breakdown`. | `providers/base.py:563`, `providers/base.py:563` |
| DSO tariff mode (`CONF_DSO_TARIFF_MODE`) | DSO-side billing mode orthogonal to the meter type: `simple`, `bi_horaire`, or `impact` (Wallonia only). Falls back automatically when the DSO publishes no Impact rates. | `const.py:225`, `const.py:225` |
| dynamic contract (`DynamicRates`, kind `dynamic`) | Energy = `factor * spot + base` per price slot against the ENTSO-E BE day-ahead spot. Requires an ENTSO-E API key. | `providers/base.py:149`, README.md:50 |
| dynamic meter (`METER_DYNAMIC`) | Smart-meter mode where consumption is priced against the live spot / hour-of-day; dynamic and TOU contracts lock the meter picker to this. | `const.py:195`, README.md:220 |
| energy contribution | Federal levy (cotisation energie), EUR/kWh, part of the tax overlay. | `providers/base.py:465` |
| energy fund | Flemish Energiefonds, billed as EUR/month (not per kWh); 0 outside Flanders and 0 for domiciled Flemish customers. | `providers/base.py:470`, README.md:176 |
| ENTSO-E day-ahead spot | The Belgian day-ahead wholesale price from the ENTSO-E Transparency Platform, the `spot` term in every dynamic and spot-indexed-injection formula. Fetched from `ENTSOE_BASE_URL` for BE domain `ENTSOE_BE_DOMAIN`. | `const.py:300`, `const.py:300` |
| EnergyRates | Union of the six energy-formula dataclasses a snapshot can carry: `FixedRates | VariableRates | DynamicRates | TimeOfUseRates | ImpactRates | SpotMonthlyRates`. | `providers/base.py:257` |
| exclusive-night circuit (`METER_EXCLUSIVE_NIGHT`) | A separate meter that only registers during DSO off-peak hours (electric water heater, night-storage heater), billed at the supplier's `exclusive_night` rate. Configured as a second config entry. | `const.py:202`, `const.py:202` |
| ExtractorError | Raised when a supplier source cannot be fetched or parsed; surfaces the `extractor_failed` repair issue. | `providers/base.py:574` |
| federal excise | Federal excise duty (accijns / droit d'accise), EUR/kWh, in the tax overlay. | `providers/base.py:464` |
| fetch_for_month | The `ArchivedSnapshotFetcher` slot on a `SupplierExtractor`; enables time-correct historical billing. `None` means the coordinator uses the current snapshot as a proxy. | `providers/base.py:757` |
| fixed contract (`FixedRates`, kind `fixed`) | Constant EUR/kWh, optionally bi-hourly (`peak`/`offpeak`) and with an `exclusive_night` rate and a `yearly_fixed_fee`. | `providers/base.py:103` |
| Flanders (`REGION_FLANDERS`) | One of the three regions. Eight Fluvius sub-areas, VREG regulator, the capacity tariff, `flanders_renewables` levy, Energiefonds. | `const.py:39`, `providers/base.py:522` |
| Fluvius | The single Flemish DSO, but with eight sub-areas that have materially different distribution rates; each is a distinct canonical DSO key. | `const.py:66`, `const.py:98` |
| Fluvius sub-areas (8) | Antwerpen, Halle-Vilvoorde, Imewo, Intergem (Midden-Vlaanderen), Iveka (Kempen), Limburg, West, Zenne-Dijle. Stored verbatim in `CONF_DSO`, so the keys are stable forever. | `const.py:145`, `const.py:145` |
| injection (feed-in) | Solar energy fed back to the grid; compensated via `InjectionRates`. Residential injection is exempt from VAT, so its values are NEVER VAT-inclusive regardless of the consumption snapshot's `vat_rate`; professional cards tax it at 21% and set `vat_applies`, which makes `apply_vat` gross the injection rates too. | `providers/base.py:638`, `providers/base.py:638` |
| InjectionRates | Injection compensation dataclass: a monthly `current` indicative and/or the hourly `factor * spot + base` formula, plus optional per-TOU-slot triplet. Values may go negative at low spot. | `providers/base.py:126`, `providers/base.py:126` |
| injection regime (`SOLAR_REGIME_INJECTION`) | Post-2024 Walloon installs and Flemish smart meters: each injected kWh is credited at the supplier's own injection price. Creates the `injection_price` sensor. | `const.py:283`, README.md:187 |
| ImpactRates | Wallonia Tarif Impact energy dataclass, three rates on the CWaPE `pic`/`medium`/`eco` bands (no weekend exception). Requires an SMR3 meter and DSO Impact opt-in. | `providers/base.py:261` |
| meter types | The four `CONF_METER` values: `mono`, `bi`, `dynamic`, `exclusive_night`. | `const.py:174`, `const.py:174` |
| mono meter (`METER_MONO`) | Single-register meter, one rate around the clock (`single`). | `const.py:193`, `providers/base.py:99` |
| MTU (Market Time Unit) | The ENTSO-E settlement interval. Belgium moved to a 15-minute MTU at the SDAC 15-min go-live (2025-10-01), which is why the spot curve is now published at quarter-hour granularity. | `const.py:247`, `const.py:254` |
| ORES | The largest Walloon DSO, covering most of the region. | `const.py:60`, `const.py:115` |
| OSP tiers (Brussels) | Brugel Obligations de Service Public annual fee, keyed by residential connection-power tier (`le1_44`, `le6`, `le9_6`, `le13`, all <=13 kVA). Only the Sibelga overlay carries the table; `CONF_CONNECTION_KVA_TIER` selects the billed value. | `const.py:239`, `const.py:239`, `providers/base.py:329` |
| probe (`SnapshotProbe`) | A cheap freshness check (HEAD, ETag, Last-Modified, or resolved PDF URL) run hourly; an unchanged return means the snapshot is still valid, so the full fetch is skipped. `None` means no probe and the time-based TTL takes over. | `providers/base.py:743`, README.md:132 |
| prosumer / prosumer fee | Compensation-regime solar cost in EUR per kVA of inverter capacity per year. The DSO publishes `prosumer_eur_per_kva_year` (Wallonia only, until 2030); some suppliers add `supplier_prosumer_eur_per_kva_year` on top (already TVAC). | `providers/base.py:575`, `providers/base.py:575` |
| quarter-hourly (`quarter_hourly=True`) | See billing grid. Keeps ENTSO-E's native 15-minute slots for the live table, next-slot sensor, and cheapest-window service. | `providers/base.py:159` |
| region | One of Flanders / Wallonia / Brussels (`REGIONS`). Determines DSO choices, regulator, and which regional renewables levy applies. | `const.py:43` |
| region connection fee | Regional per-kWh connection levy in the tax overlay (`region_connection_fee`). | `providers/base.py:525` |
| regional renewables | Region-specific green-energy / cogen surcharges: `flanders_renewables`, `wallonia_renewables`, `brussels_renewables` (EUR/kWh). The pricing engine picks the one matching the entry's region; extractors leave the others at 0. | `providers/base.py:524`, `providers/base.py:524` |
| RESA | Walloon DSO for the Liege area. | `const.py:61`, `const.py:116` |
| REW (Regie de Wavre) | Small Walloon municipal DSO. | `const.py:62`, `const.py:117` |
| SDAC 15-minute go-live | The Single Day-Ahead Coupling switch to a 15-minute MTU on 2025-10-01, after which ENTSO-E publishes the BE curve at 15-minute resolution. | `const.py:247` |
| Sibelga | The single Brussels DSO; its overlay uniquely carries the Brugel OSP tier table. | `const.py:64`, `const.py:119` |
| SMR3 | Smart Meter Rollout phase 3, the Belgian digital smart meter. Required for TOU, dynamic, and Tarif Impact billing; Flemish digital meters are SMR3 from install (no compensation-regime prosumer fee there). | `providers/base.py:218`, `const.py:225` |
| snapshot (`SupplierSnapshot`) | Everything extracted from one supplier tariff card for one `(supplier, contract)`: energy formula, per-DSO overlays, tax overlay, optional injection, source URL, and `valid_until`. | `providers/base.py:583` |
| SnapshotFetcher | The required per-supplier callable that fetches and returns a `SupplierSnapshot` from a live source. | `providers/base.py:553` |
| solar_kva / kVA | Solar inverter apparent-power capacity in kVA (`CONF_SOLAR_KVA`); 0 means no panels. Multiplies the prosumer fee. Also the unit of the Brussels connection-power tier. | `const.py:274`, `const.py:274` |
| SOLAR_REGIME_* | The three solar regimes: `none`, `compensation`, `injection` (`CONF_SOLAR_REGIME`). | `const.py:275`, `const.py:275` |
| source_url / publication_label | Snapshot provenance: the card URL and its human publication month, surfaced in diagnostics and the `snapshot_publication` attribute. | `providers/base.py:490`, `providers/base.py:491` |
| spot_indexed_injection | Contract flag: True when a non-dynamic product's injection is itself a per-hour `factor * spot + base` formula (Cociter Variable), so pricing the injection needs an ENTSO-E key even though the energy is variable. | `providers/base.py:77` |
| SpotMonthlyRates | Energy shape billing a flat rate for the whole month: `factor * monthly_mean(spot) + base`. Used by the expert custom monthly-average mode; the coordinator threads the delivery month's mean spot through the same `spot_eur_per_kwh` parameter `DynamicRates` uses. | `providers/base.py:149` |
| SPP (Synergrid solar production profile) | The national 15-minute solar production profile Synergrid publishes for PV settlement. SPP-indexed injection contracts weight the day-ahead price by it. The optional SPP-weighted custom injection fetches the ex-ante profile (a free `.xlsx`) and weights the monthly mean by it; `synergrid.py` streams and parses it, `coordinator._spp_weighted_month_mean` applies it. | `synergrid.py`, `coordinator.py` |
| supplier: custom (`SUPPLIER_CUSTOM`) | The expert escape-hatch supplier for products with no public card: the user types the commodity formula and all regulated DSO + tax values, and the coordinator builds the snapshot from the config entry (no fetch). Three modes: dynamic, monthly-average, fixed. | `const.py`, `providers/custom.py` |
| SupplierExtractor | The registry entry a provider module exposes as top-level `EXTRACTOR`: id, label, contracts, `fetch`, optional `probe` and `fetch_for_month`. | `providers/base.py:773`, `providers/base.py:773` |
| Tarif Impact (kind `tou_impact`) | Wallonia CWaPE 3-band time-of-use tariff (pic/medium/eco), opt-in for SMR3 customers. Distinct from plain TOU because the schedule is the CWaPE one with no weekend exception, and the DSO Impact tariff gates eligibility. | `providers/base.py:232`, `const.py:173` |
| TariffKind | The `Literal` of the six contract kinds: `fixed`, `variable`, `dynamic`, `tou`, `tou_impact`, `spot_monthly`. | `providers/base.py:53` |
| TaxOverlay | Federal + regional levy dataclass: excise (a single rate, or `federal_excise_bands` on a card that prints a degressive schedule), energy contribution, three regional renewables, connection fee, energy fund (per month), `vat_rate`, and `published_vat_rate`. | `providers/base.py:549` |
| apply_vat / CONF_INCLUDE_VAT | Resolves an ex-VAT snapshot for one config entry: bakes the fixed and annual fees by the chosen factor and sets the per-kWh `vat_rate` with it. The Flemish energy fund is excluded: it is levied VAT-free and is billed exactly as the card prints it. Identity on a VAT-inclusive card. Called per entry (never before the cross-entry shared cache) so a VAT-deductible business and a private customer can share a tuple. It zeroes `vat_rate` on an ex-VAT resolve, so it copies the card's own rate into `published_vat_rate` first: that is the only surviving record of the basis a typed yearly fee was entered at, and the cohort leg reads it as `published_vat_rate or vat_rate` on all three of its call paths. | `providers/base.py:549`, `cohort.py:313` |
| federal_excise_bands / CONF_ANNUAL_CONSUMPTION_KWH | Degressive excise schedule `((upper_kwh, eur_per_kwh), ...)` printed by cards that band the special excise by annual consumption. `resolve_excise_band` picks the entry's band into `federal_excise`, clamping past the last one; identity when the card prints one rate. | `providers/base.py:512`, `providers/base.py:512` |
| TimeOfUseRates (kind `tou`) | Three-slot hour-of-day energy contract (peak / transition / offpeak) with a product-dependent `weekend_rule`. Requires an SMR3 meter. | `providers/base.py:237` |
| TOU slots (peak / transition / offpeak) | The weekday TOU schedule shared across products: peak 07:00-11:00 + 17:00-22:00, transition 11:00-17:00 + 22:00-01:00, offpeak 01:00-07:00. | `providers/base.py:198` |
| transport | High-voltage transmission (TSO) charge in EUR/kWh on the DSO overlay, part of the network component. | `providers/base.py:322` |
| TSO (transmission system operator) | Elia, the operator of the Belgian high-voltage grid whose transmission charge is the `transport` term. Distinct from the DSO (local distribution). | `providers/base.py:361`, README.md:36 |
| TTL | Time-based cache expiry (24 h) used for suppliers with no usable probe (Engie, Luminus, DATS 24). | README.md:364, `providers/base.py:516` |
| TVAC / VAT convention (`vat_rate`) | TVAC = "TVA comprise" (VAT included). `vat_rate = 0.0` means the snapshot's prices are ALREADY VAT-inclusive (the convention for cards that print TVAC, e.g. Eneco, Cociter); a snapshot shipping ex-VAT numbers sets the rate explicitly (e.g. Ecopower cards are HTVA so `0.06`). Per-kWh values are then grossed in `_finalize_breakdown`, and the fixed/annual fees, which no pricing path grosses, are baked by `apply_vat`. | `providers/base.py:638`, `providers/base.py:638`, README.md:155 |
| variable contract (`VariableRates`, kind `variable`) | Monthly-reindexed EUR/kWh: `current` effective rate, optional `peak`/`offpeak`/`exclusive_night`, a `formula` string, and `yearly_fixed_fee`. | `providers/base.py:103` |
| VREG | Flemish energy regulator. Set the capacity tariff and its regulated monthly-peak floor. | `const.py:238`, `const.py:242` |
| VREG_CAPACITY_FLOOR_KW | Regulated minimum (2.5 kW) the capacity tariff bills against. Fluvius's methodology applies it to each monthly peak before the twelve-month mean (`Rekenkundig gemiddelde van de Max (Maandpiek (m), 2.5)`), which is why the mean itself needs no clamp. Set by VREG at the January 2023 capacity-tariff introduction. | `const.py:242` |
| Wallonia (`REGION_WALLONIA`) | One of the three regions. Five DSOs, CWaPE regulator, Tarif Impact, the compensation-regime prosumer fee, `wallonia_renewables` levy. | `const.py:40`, `const.py:40` |
| weekend rule (`WeekendRule`) | Per-TOU-product weekend override: `weekend_offpeak` (Luminus SmartFlex, whole weekend off-peak), `weekend_no_peak` (Engie Empower Flextime, no peak slot on weekends), `smartflex_seasonal`. | `providers/base.py:199`, `providers/base.py:199` |
| yearly_fixed_fee | Supplier flat annual subscription (EUR/year) on every energy dataclass; a separate `yearly_fixed_fee_exclusive_night` applies on an exclusive-night entry when the card prints one. | `providers/base.py:108`, `providers/base.py:108` |

## Notes and gotchas

- Canonical DSO keys are frozen: they are stored verbatim in every user's
  `CONF_DSO` and are keys of `SupplierSnapshot.dsos`, so renaming one silently
  breaks existing entries (`const.py:45`). Per-provider extractors map their PDF
  labels onto these keys, never the reverse.
- `vat_rate = 0.0` does not mean "no VAT": it means the prices are already
  VAT-inclusive. Injection is a separate case, always VAT-exempt regardless of
  `vat_rate` (`providers/base.py:538`).
- The `exclusive_night` distribution rate falls back to `distribution_offpeak`
  when an extractor has not mapped the column yet (`providers/base.py:321`); the
  supplier `exclusive_night` energy rate falls back to `single` / `current`
  (`providers/base.py:84`, `providers/base.py:111`).
- `quarter_hourly` affects the live/next-slot/cheapest-window paths only; YTD
  billing is always hourly because HA retains only hourly long-term statistics
  (`providers/base.py:151`).
- Compensation-regime prosumer costs come from two independent sources that add
  together: the DSO rate (`DsoOverlay.prosumer_eur_per_kva_year`) and the
  optional supplier forfait (`SupplierSnapshot.supplier_prosumer_eur_per_kva_year`,
  already TVAC).

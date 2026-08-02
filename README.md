<p align="center">
  <img src="logo.svg" alt="BE electricity - real-time prices" width="640"/>
</p>

<p align="center">
  <a href="https://github.com/renaudallard/homeassistant_be_electricity_prices/releases/latest">
    <img src="https://img.shields.io/github/v/release/renaudallard/homeassistant_be_electricity_prices?label=version&style=flat-square&sort=semver" alt="Latest release"/>
  </a>
  <a href="https://github.com/renaudallard/homeassistant_be_electricity_prices/releases">
    <img src="https://img.shields.io/github/downloads/renaudallard/homeassistant_be_electricity_prices/total?style=flat-square&label=downloads" alt="GitHub release downloads"/>
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

> Targets Home Assistant **2026.4 or newer** (the minimum declared in `hacs.json`).

## Highlights

- **Live tariff cards** — prices come straight from the supplier's published PDF; no EUR values live in this repo.
- **Whole-bill view** — energy, transport, distribution, regional levies and VAT all add up to a single EUR/kWh sensor.
- **Dynamic contracts** — `factor × spot + base`, where `spot` is the Belgian day-ahead price from ENTSO-E. Priced per hour by default; suppliers that bill per quarter-hour (Engie, Cociter, EBEM, Ecofix, OCTA+, Ecopower Dynamische Burgerstroom, Bolt Dynamisch, energie.be and EnergyVision, after the SDAC 15-minute market switch of Oct 2025) keep the native 15-minute slots for the live price, next slot and cheapest-window service. Year-to-date billing stays hourly, since Home Assistant only retains hourly long-term statistics.
- **Time-of-Use contracts** — Luminus SmartFlex and Engie Empower Flextime: 3 hour-of-day bands (peak / transition / offpeak) with the supplier's published rates per slot. Luminus SmartFlex is billed on its *seasonal* schedule: peak 07:00-11:00 + 17:00-22:00 all year, the cheapest super-creuses band 11:00-17:00 only in spring/summer (21/03-20/09), and 22:00-07:00 always at the middle creuses rate (the "free electricity on Sundays" first-year promo is not modelled).
- **Tarif Impact (Wallonia)** — opt-in CWaPE 3-band distribution pricing (PIC 17–22, MEDIUM 7–11 + 22–1, ECO 1–7 + 11–17), selectable independently of the supplier tariff. Under Impact comptage the SMR3 meter registers in these bands, so a bi-hourly supplier energy rate follows them too (ECO → off-peak rate, MEDIUM/PIC → peak rate) rather than the plain bi-horaire clock.
- **Flanders capacity tariff** — billed the way Fluvius bills it, on the mean of your last twelve monthly peaks (not on the month in progress), each month floored at the 2.5 kW regulated minimum before averaging. The monthly peak comes from your meter's own monthly-peak sensor when you have one (a DSMR 5B meter publishes it as *Maximum demand current month*, which is the quarter-hour peak Fluvius bills), else from any power sensor (W, kW, VA, or kVA — the unit is honoured) or a fixed value; billed against the configured Fluvius sub-area.
- **Solar** — prosumer fee for the Walloon compensation regime (until 2030-12-31), and a per-kWh injection price entity that plugs straight into HA Energy.
- **Year-to-date cost** — `current_year_cost` sensor reports your running bill in EUR since Jan 1, computed day by day (or hour by hour for TOU and dynamic contracts) from HA's recorder (consumption × the tariff of the month that day/hour belongs to). Each day is billed at its own month's published rate when the supplier archives historical cards (Eneco / Cociter / Ecopower / Bolt fix / Mega / EBEM / Frank); other suppliers fall back to the current rate as a proxy. The annual fees, the Walloon prosumer charge and the Flemish capacity tariff accrue into it too, each pro-rated over the elapsed year, so the figure is the whole bill rather than the energy side alone. **TOU contracts** (Engie Empower Flextime, Luminus SmartFlex) use the per-hour path so each kWh hits its actual peak / transition / offpeak rate. **Dynamic contracts** replay historical hourly ENTSO-E day-ahead spots from a persistent cache so each past kWh is billed at its actual `factor × spot + base` rate; missing hours (cold-start gaps) are skipped rather than zeroed. Compensation regime nets injection against consumption across the whole year (clamped at zero, since most Walloon suppliers forfeit surplus injection past consumption). Annual fees are pro-rated to the elapsed fraction of the year so the figure grows day by day instead of jumping to the full annual on Jan 1.
- **Cheapest / most-expensive window services** — find the optimal contiguous N-hour window in the upcoming price table for EV charging, heat-pump cycles, or peak avoidance.
- **Statistics backfill** — on first install (or after a database reset) the integration populates the recorder's long-term statistics for the price sensors and `current_year_cost` from Jan 1 of the current year up to "now", so the Energy dashboard shows price history immediately. A `backfill_statistics` service is exposed for re-runs after a tariff change.
- **Tomorrow-available trigger** — `tomorrow_prices_available` binary sensor flips ON once ENTSO-E publishes the next-day curve, so dynamic automations don't fire too early.
- **Signing-cohort pricing** — set an optional **contract start date** and a fixed or dynamic contract is priced at the rate you locked in that month instead of today's new-customer card, for suppliers that archive past cards (Bolt fix, Eneco, Mega). Only the commodity price is frozen to the signing month; the regulated network tariffs and taxes still track the current month, and the year-to-date cost bills every past month at the same locked rate. For suppliers without an archive, or a start date older than the archive reaches, the setup flow offers an optional **signing-rate** step where you type the rate you locked in (leave it blank to keep the current card). **Variable** contracts whose card exposes a numeric index formula (Cociter Variable, EBEM Groen Variabel / B@sic+, Eneco Flex, Mega Flex) re-price differently: the signing month's *formula coefficients* are frozen and re-applied to the current month's mean spot, since a variable rate re-indexes monthly. This is exact for Cociter (its BELIX index is the arithmetic monthly mean); for the RLP-weighted cards (EBEM / Eneco / Mega) the arithmetic mean is a close approximation of the residential-load-profile weighting, so their re-price runs a few percent off, and a bi-hourly meter is billed the mono formula for the month. Resolving that mean needs spot data, so the variable re-price only runs when an **ENTSO-E API key** is configured; without one the entry keeps the current card and prices off its published rate. Time-of-use contracts still keep the current card.
- **Renewal reminder** — set an optional **contract end date** when you add or edit the entry and it is exposed as a `contract_end_date` timestamp sensor, so an automation can remind you to shop around before your contract rolls over. Purely informational; it does not affect pricing.
- **ENTSO-E key validated at setup** — the config flow hits the real endpoint with the entered token and rejects bad keys before the entry is saved.
- **Translated UI** — English, French, Dutch and German.
- **One-off supplier comparison** — the OptionsFlow has a *Compare another supplier* path that quotes a different supplier and contract against your current region / DSO / peak / solar settings. **Static and dynamic contracts can be quoted against each other** ("should I switch from fixed to dynamic?"); the flow prompts for an ENTSO-E key when a side needs spot data (a dynamic contract, or a spot-indexed-injection target like Cociter Variable on the injection regime) and you don't already have one saved. The annual estimate uses your **measured rolling-year consumption** (and, for solar users, injection) read from the same kWh sensors that feed `current_year_cost`, with a sensible 3500 kWh fallback when no sensor is wired. The result page also shows a **year-to-date what-if**: the actual kWh you've used since 1 January re-priced at each supplier's current rate, with two-row unicode bar charts so the difference reads at a glance. The meter type is overridable for static contracts (compare *what if I were on bi-hourly billing under supplier X*). Solar regimes are honoured: compensation nets consumption against injection, injection regime credits each supplier's own injection price. No second entry, no extra polling, nothing saved.
- **Self-healing** — last-known prices keep serving on outage. Five repair issues surface under **Settings → System → Repairs**: snapshot older than 7 days, a supplier extractor parse failure (layout drift), the supplier being unreachable after repeated fetch failures, ENTSO-E rejecting the API key, and a supplier that has announced it is leaving the residential market. A single transient fetch timeout no longer raises an issue; each auto-clears on the next successful refresh.
- **Catalog drift detection** — the daily live-check diffs each supplier's public catalog against the registry and opens a GitHub issue when a new product appears, plus per-supplier wallclock + bytes-received telemetry to flag silent slowdowns and PDF size jumps.
- **Expert custom formula** — an escape hatch for suppliers that publish no public tariff card (group-purchase deals, B2B-flavoured products). You type the commodity formula (`factor × spot + base`, a monthly-averaged spot rate, or a flat rate) and all regulated DSO + tax values; there is no live card, so it's a static snapshot with none of the auto-update or drift-check safety net. Listed last in the supplier dropdown and clearly labelled as expert.

## Supported providers

| Supplier | Contracts | Source |
| --- | --- | --- |
| **Bolt** | Bolt Fixe · Bolt Plenty Fixe · Bolt Variable · Bolt Dynamisch *(quarter-hourly Belpex)* · Bolt Plenty Variable · Bolt Online · Bolt Plenty Online | [`providers/bolt.py`](./custom_components/be_electricity_prices/providers/bolt.py) — stable URLs at `files.boltenergie.be/pricelists/<fix\|var>/`, parsed via `pdfplumber` (rotated columns + Unicode line-separators). Bolt Dynamisch reads the same variable card and applies its printed `Belpex × factor + base` formula to the 15-minute spot. |
| **Cociter** | Tarif Variable (BELIX) · Tarif Dynamique (quarter-hourly BELPEX) | [`providers/cociter.py`](./custom_components/be_electricity_prices/providers/cociter.py) — monthly cards `RCVar_YMR_Coop-YYMM-fr.pdf` / `RCDyn_SM3_Coop-YYMM-fr.pdf` |
| **DATS 24** *(withdrawn 2026-08-31)* | Elektriciteit Groen Variabel (BE_spotRLP-indexed monthly) | [`providers/dats24.py`](./custom_components/be_electricity_prices/providers/dats24.py) — one PDF per month on the Colruyt Group CDN, month spelled in the filename (`api.colruytgroup.com/api/static/dats24/parameters/site/<YYYY>/ELEK/NL/... Versie <MM> <YYYY>.pdf`), falling back one month while the new card is unpublished. Colruyt subsidiary; Flanders + Wallonia. Single product covers mono / bi / exclusive-night meter rates and includes the BE_spotSPP injection formula. **DATS 24 is leaving residential energy supply: contracts transfer automatically to EnergyVision on 31 August 2026**, so switch the entry to **EnergyVision**, which covers both regions — see [docs/providers/dats24.md](./docs/providers/dats24.md). |
| **EBEM** | Groen Variabel (BelpexRLP0 monthly, mono / bi / excl. night) · Groen B@sic+ (BelpexRLP0 monthly, single rate, online-only) · Groen Dyn@mic (Belpex 15-min, SMR3) | [`providers/ebem.py`](./custom_components/be_electricity_prices/providers/ebem.py) — Mol/Geel-area Flemish supplier (Ebem bvba). Monthly cards linked from `ebem.be/tarieven/` under opaque Umbraco media-hash URLs; the provider scrapes the listing each fetch and supports `fetch_for_month` against the public archive (≥ 6 months back), so past consumption bills at each month's actual rates. Variabel + B@sic+ share the `elek` PDF; Dyn@mic has its own. Flanders only. |
| **Ecofix** | Motion (quarter-hourly Belpex 15M) · Motion Online (same formula, online-only) · Flexy (BELPEX-RLP-M monthly variable) | [`providers/ecofix.py`](./custom_components/be_electricity_prices/providers/ecofix.py) — stable URLs at `portal.ecofixgp.be/docs/prices/current/EL_Ecofix_<PRODUCT>_NL.pdf`, overwrite-in-place each month. One PDF carries Flanders + Wallonia overlays (no Brussels). Parsed via `pdfplumber` for the column-major Wallonia DSO table. |
| **Ecopower** | Groene Burgerstroom (50% fixed + 50% Belpex DA, indexed monthly) · Dynamische Burgerstroom *(quarter-hourly EPEX DA)* | [`providers/ecopower.py`](./custom_components/be_electricity_prices/providers/ecopower.py) — Groene Burgerstroom from the monthly cards at `ecopower.be/groene-stroom/prijs-nieuw`; Dynamische Burgerstroom from the `dbs` card at `ecopower.be/groene-stroom/dynamische-burgerstroom` (`afname = 1,02 × EPEX DA + 4 €/MWh`, `injectie = 0,98 × EPEX DA − 15 €/MWh`). Flanders cooperative, Flanders only. Cards are HTVA so `vat_rate=0.06`. |
| **Eneco** | Zon & Wind Vast · Zon & Wind Flex · Zon & Wind Dynamisch | [`providers/eneco.py`](./custom_components/be_electricity_prices/providers/eneco.py) — monthly cards `cdn.eneco.be/downloads/nl/general/tk/BC_032_<NNNNNN>_NL_ENECO_POWER_<FIX\|FLEX\|DYNAMIC>.pdf` resolved from the public listing page each fetch (issue number rotates monthly), V/W only (no Brussels) |
| **Engie** | Easy Fixed · Easy Variable · Direct Online · Basic Online · Dynamic · Empower Fixed · Empower Variable · Empower Flextime *(TOU)* · Flow · Empty House | [`providers/engie.py`](./custom_components/be_electricity_prices/providers/engie.py) — Engie's public REST endpoint at `engie.be/api/engie/be/ms/pricing/v1/public/pricesAndConditionsPDF`, one PDF per (contract, region) |
| **Luminus** | Comfy · Comfy+ · ComfyFlex · ComfyFlex+ · MaxxFix · MaxxFlex · BasicFix · BasicFlex · SmartFlex *(TOU)* · Dynamic | [`providers/luminus.py`](./custom_components/be_electricity_prices/providers/luminus.py) — Luminus's public REST endpoint at `luminus.be/api-next/get-pricelist/`, V/W only (no Brussels for market products) |
| **Mega** | Smart Fixed/Flex · Online Fixed/Flex · Cosy Fixed/Flex · Off-peak Flex · Off-peak Impact *(Wallonia, CWaPE 3-band)* · Dynamic · Cap | [`providers/mega.py`](./custom_components/be_electricity_prices/providers/mega.py) — scrapes the public listing at `mega.be/fr/energie/cartes-tarifaires` to resolve each `(product, region)` to its current PDF on `my.mega.be` |
| **OCTA+** | Fixed · Fixed Impact *(Wallonia, CWaPE 3-band)* · Eco Fixed · Smart Variable · Flux · Eco Flux · Dynamic · Eco Dynamic | [`providers/octaplus.py`](./custom_components/be_electricity_prices/providers/octaplus.py) — stable URLs at `files.octaplus.be/tariffs/E_OCTA_<PRODUCT>_RE_<VL\|WL>_FR.pdf`, parsed via word-coordinate alignment (heavy character spacing in the tax block) — Flanders + Wallonia only |
| **TotalEnergies** | Electricité Fixe/Variable · Impact · myComfort · myComfort Fixe · myDrive · myDynamic · myEssential · myEssential Fixe | [`providers/totalenergies.py`](./custom_components/be_electricity_prices/providers/totalenergies.py) — stable URLs at `totalenergies.be/static/marketing-documents/b2c/tariff-card/latest/`, parsed via `pdfplumber` (rotated columns) |
| **Frank Energie** | Dynamisch · Dynamisch HV · Dynamisch Korting · Dynamisch JN · Dynamisch Slim | [`providers/frank.py`](./custom_components/be_electricity_prices/providers/frank.py) — monthly tariff card PDFs discovered via the public Sanity CMS file-asset API (`8navd656.api.sanity.io`), parsed via `pdfplumber`. Flanders only, five dynamic contract tiers with different factor/base/fee combinations. |
| **energie.be** | Dynamisch *(quarter-hourly EPEX)* | [`providers/energiebe.py`](./custom_components/be_electricity_prices/providers/energiebe.py) — dynamic residential card served at the document API `energie-production-api.azurewebsites.net/api/v1/data/document?key=DynamicTariffs` (302-redirects to the current month's Azure blob), parsed via `pdfplumber`. Flanders only; only the residential block is read, and the card prints Belpex in c€/kWh so the spot factor is not scaled by 10. |
| **EnergyVision** | Dynamisch *(quarter-hourly Belpex)* · 3 jaar vast · 1 an fixe *(Wallonia)* | [`providers/energyvision.py`](./custom_components/be_electricity_prices/providers/energyvision.py) — monthly `Goedkope stroom` cards resolved from the `energyvision.be/nl-be/tariefkaart` listing (filenames carry the pricing month, e.g. `EV-0726-GSDYN-nl.pdf`), parsed via `pdfplumber`. Each product is published for one region in one language: the two Flemish cards in Dutch, the Walloon `1 an fixe` in French (`-WAL-fr`). The dynamic card prints Belpex in EUR/MWh (Bolt axis, factor not scaled by 10); both fixed cards bill injection from the monthly Belpex-SPP-M indicative. Gas and the per-volume tiered products are out of scope. |
| **Expert: custom formula** *(no public card)* | Dynamic (`factor × spot + base`) · Monthly average (`factor × monthly-mean spot + base`) · Fixed / manual rate | [`providers/custom.py`](./custom_components/be_electricity_prices/providers/custom.py) — an escape hatch for suppliers with **no public, machine-resolvable tariff card** (see below). Not scraped: you type the commodity formula and all regulated DSO + tax values, and the coordinator builds the snapshot from your config entry. |

Adding another supplier is a self-contained PR: drop a new module under
[`custom_components/be_electricity_prices/providers/`](./custom_components/be_electricity_prices/providers/),
register it in [`providers/__init__.py`](./custom_components/be_electricity_prices/providers/__init__.py),
and ship a fixture-based unit test. The Eneco module is the reference.

**Why isn't a business-only supplier like Yuso listed?** Not because it is
business-only — professional tariffs *are* supported, see below — but because
Yuso's cards price the energy commodity alone (platform fee plus green/CHP
certificates) and state that network tariffs and taxes are passed through
one-for-one, billed separately by the grid operator. There is no all-in price to
assemble from such a card, for a household or a business alike. The same applies
to any supplier that publishes commodity-only pricing. If you know your own
formula and grid/tax rates, the **Expert: custom formula** supplier lets you
enter them by hand (see below).

**Professional (B2B) tariffs.** Engie, Mega and Bolt publish full professional
tariff cards carrying the same DSO and tax tables as their residential ones,
printed **excluding VAT**, and the integration reads them. Pick a *Pro* contract
in the supplier dropdown and two extra settings appear:

- **Prices include VAT** — professional electricity is taxed at 21%. Leave this
  on if your business cannot deduct VAT; turn it off if it can, and every price,
  fee and cost sensor reports the ex-VAT amount you actually bear. Injection
  follows the same choice (unlike residential injection, which is VAT-exempt
  outright). Residential contracts print VAT-inclusive already, so the setting is
  hidden for them.
- **Estimated yearly consumption** — the federal special excise is degressive on
  professional cards (bands at 20 000 and 50 000 kWh/year), so the band your
  site falls in has to be known to price a kWh.

Scope limits, taken from the cards themselves: they price **low-voltage**
(*basse tension* / *laagspanning*) connections only, with injection up to 10 kVA
and consumption up to 1 000 000 kWh/year. Medium- and high-voltage connections,
contracted-power demand charges and reactive-power billing are out of scope —
they depend on per-site contract terms no public card lists.

**Expert: custom formula (no public card).** Some products can't be scraped
because the supplier publishes no public, machine-resolvable tariff card — the
Yuso day-ahead offer, or a one-off group-purchase deal like the Mega iChoosr /
Samen Overstappen *groepsaankoop*. For those, the last entry in the supplier
dropdown lets a knowledgeable user type the pricing themselves: a dynamic
`factor × spot + base` formula, a monthly-average variant that bills a flat rate
equal to `factor × the delivery month's mean spot + base` (with an optional
never-negative injection floor), or a plain fixed rate — plus the regulated DSO
and tax values, which are identical for every supplier on your grid. Coefficients
are entered excluding VAT (as printed on a tariff sheet) and the VAT rate grosses
them up. This trades away the whole point of the live-extractor model: there is no
card to refresh and no drift check, so the numbers are a static snapshot you must
keep current yourself, and a monthly-average rate is a running estimate until the
month closes. For injection, the monthly-average mode offers an optional
**SPP-weighted** setting: it fetches Synergrid's national solar production profile
and weights the monthly day-ahead mean by it (as SPP-indexed contracts do) instead
of a plain average — much closer for a solar prosumer, since the plain mean
over-credits injection by weighting the cheap midday hours the same as the rest.
It uses the published *ex-ante* (forecast) profile, so it is close to but not
exactly the settled SPP value, and it falls back to the plain mean if the profile
can't be fetched.

### How often the integration polls

The coordinator ticks once an hour. On each tick it runs the supplier's
**`probe()`** — a cheap freshness check that returns a key (`Last-Modified`,
`ETag`, or the resolved PDF URL) — and only re-runs the full PDF fetch when
that key changes from what we last fetched. This catches a supplier
publication within an hour at near-zero ongoing bandwidth instead of a
fixed 24-hour schedule. Suppliers that have no usable probe (Engie,
Luminus and DATS 24, where the only cheap response is the PDF itself)
keep the time-based 24-hour TTL.

## What the integration computes

For every hour, an all-in EUR/kWh built up as

```
all_in = (energy + distribution + transport + levies) × (1 + VAT)
```

Each component comes from the supplier's tariff card and the configured DSO.
For dynamic contracts the energy term is `factor × spot + base`, where `spot`
is the Belgian day-ahead price from the ENTSO-E Transparency Platform —
published at 15-minute resolution since the SDAC switch of Oct 2025. The
integration aggregates it to hourly except for suppliers that bill per
quarter-hour (Engie, Cociter, EBEM, Ecofix, OCTA+, Ecopower Dynamische Burgerstroom, Bolt Dynamisch, energie.be and EnergyVision), which keep the native 15-minute slots.

VAT spreads uniformly across components, so `energy_component +
network_component + taxes_component` always equals `current_price` to the cent.

## Sensors

All sensors share one device per config entry.

### Always created

| Sensor | Description |
| --- | --- |
| `current_price` | All-in EUR/kWh **now**. Attributes: `today` and `tomorrow` (chronological lists of `{start, energy, network, taxes, all_in}`), `snapshot_publication` (the card's publication month), `snapshot_age_hours`, `snapshot_stale`, `last_error`, `cheapest_4h_today` and `most_expensive_4h_today` (chronologically sorted, disjoint lists of `{start, price}`). On flat-tariff days where every hour rounds to the same all-in price (typical for fixed contracts), the cheapest list always comes back as the first 4 hours of the day and the most-expensive as the last 4 — automations keying on these for "cheapest window" should treat the output as undefined when the day's prices don't actually vary. When a **contract start date** is set on a fixed / dynamic contract, the energy component is priced at the signing month's card (see Highlights). |
| `next_hour_price` | All-in EUR/kWh for the next hour. |
| `today_average` | Daily average all-in EUR/kWh. |
| `today_min` / `today_max` | Daily extremes. |
| `tomorrow_average` | Average all-in EUR/kWh for tomorrow. Empty until ENTSO-E publishes the next-day curve (~13:00 CET) for dynamic contracts; available all day for fixed/variable contracts, except on the last day covered by a monthly card, where next month's rates are not published yet. Tracks `tomorrow_prices_available` exactly: the sensor has a value when that binary sensor is on. |
| `tomorrow_min` / `tomorrow_max` | Tomorrow's extremes. Same availability as `tomorrow_average`. |
| `energy_component` | Energy-only EUR/kWh now (VAT-inclusive). |
| `network_component` | Distribution + transport EUR/kWh now (VAT-inclusive). |
| `taxes_component` | Levies EUR/kWh now (VAT-inclusive). |
| `fixed_fee_eur_per_year` | Supplier's flat annual subscription fee (EUR/year), parsed from the tariff card. |
| `energy_fund_eur_per_month` | Flemish Energiefonds in EUR/month (€0 outside Flanders, and €0 in Flanders for domiciled customers). |
| `current_year_cost` | Running bill **since Jan 1 of the current year**, computed against HA's recorder (per day for fixed/variable, per hour for TOU and dynamic). Configure once in the **Energy meters** step, two ways: (a) point at the four day/night register sensors directly (preferred when available); or (b) point at single cumulative consumption / injection sensors (for bi-hourly meters the integration recovers the day/night split per past day from the recorder's hourly statistics binned by the bi-hourly schedule). Each kWh is multiplied by the tariff in effect for the month/hour it belongs to: when the supplier archives historical cards (Eneco / Cociter / Ecopower / Bolt fix / Mega / EBEM / Frank) past months use their own published rates; suppliers without an archive (OCTA+ / TotalEnergies / Engie / Luminus / DATS 24 / Ecofix / energie.be / EnergyVision) fall back to the current rate as a proxy. Dynamic contracts replay historical hourly ENTSO-E spots from a persistent cache so each past kWh hits its actual `factor × spot + base` rate; missing hours (cold-start gaps) are skipped rather than zeroed. Annual fees (`yearly_fixed_fee + 12 × energy_fund_eur_per_month + 12 × prosumer_cost`, plus the DSO data-management fee and, in Brussels, the Brugel OSP fee for the configured connection-power tier) are summed per archived month using each month's snapshot, then pro-rated by `days_in_month_in_ytd / days_in_year` so the YTD running total still grows uniformly across the calendar year — on Jan 1 the sensor sits at ~0 and grows day by day, and on Dec 31 it carries the full annual amount. A supplier that re-indexes its fixed fee or energy fund mid-year is honoured for the months it applies to (same per-month snapshot path the prosumer fee already uses). Under Walloon compensation regime, injection is netted against consumption across the whole YTD and the energy term is clamped at zero (most suppliers forfeit surplus injection past consumption, so the bill never settles negative) — so once your year-to-date injection exceeds your consumption the energy term stays at zero and the sensor rests flat on the fees floor (`= fees_ytd_eur`); a value that stops moving while you keep injecting is that floor, working as designed, not a stalled sensor (the `energy_ytd_raw_eur` attribute shows the hidden negative term). If your meter is bidirectional and your contract actually pays for injected surplus, pick the **injection tariff** regime instead of compensation so that surplus is credited rather than forfeited. Today's partial day is read from the live meter (its current cumulative reading minus the reading at local midnight) rather than the day's long-term statistic, so the running total tracks today's usage in real time and keeps moving even when HA's statistics compilation lags or stalls; past days still come from the daily statistics. Always numeric: a fresh install in May still produces a meaningful figure for the year so far, as long as the recorder has history for the configured kWh sensors. When a **contract start date** is set on a fixed / dynamic contract, every past month's energy is billed at the signing-month rate (archive suppliers only) rather than each month's own card. For static (fixed / variable) contracts the sensor also carries diagnostic attributes — `consumption_ytd_kwh`, `injection_ytd_kwh`, `consumption_today_kwh`, `injection_today_kwh`, `energy_ytd_raw_eur` (the energy term **before** the compensation zero-floor) and `fees_ytd_eur` — so a flat value can be read: a negative `energy_ytd_raw_eur` means banked injection has zeroed the energy term and the bill correctly rests on the fees floor (`= fees_ytd_eur`), while a `consumption_today_kwh` that never moves points at a stalled meter input rather than the integration. |
| `tomorrow_prices_available` | Binary sensor. ON when the price table covers at least one hour with tomorrow's local date **and** the supplier's published validity still covers tomorrow. Useful as a trigger for dynamic-tariff automations that should only fire after ENTSO-E publishes the next-day curve (~13:00 CET). For fixed/variable contracts it is ON throughout the month, but flips OFF on the last day of a month whose card stops at month-end, since next month's rates are not published yet. |

### Conditional

| Sensor | Created when | Description |
| --- | --- | --- |
| `capacity_cost` | Region = Flanders | Current monthly capacity cost in EUR (`billed_peak_kw × DSO_capacity_rate / 12`). `billed_peak_kw` is the mean of your last twelve monthly peaks, each floored at 2.5 kW first, which is what Fluvius charges on, so this stays steady through the year rather than tracking whichever month you are in. This charge also accrues into `current_year_cost`, so the two are consistent rather than the capacity term being invisible in the running bill. Carries `billed_peak_kw` and `months_counted` attributes; `months_counted` reaches 12 after a full year of history, and until then the mean covers only the months measured so far. |
| `monthly_peak_kw` | Region = Flanders | Running monthly peak power in kW (resets the 1st), reported as measured: the 2.5 kW regulated minimum is a billing rule and is applied to `capacity_cost` instead, so a quiet household now reads its true peak here rather than 2.5. State class is `MEASUREMENT` (mandated by HA for the POWER device class), so the long-term-statistics graph defaults to the **mean** aggregation. To see the true monthly peaks, switch the statistic-graph card to **Max** under Developer Tools → Statistics. A diagnostic **Reset monthly peak** button on the device page drops the rolling max so the next tick rebuilds it (use after a misconfigured sensor inflated the peak). |
| `prosumer_cost` | Compensation regime + `solar_kva > 0` | Monthly compensation fee in EUR (`solar_kva × (DSO_prosumer_rate + supplier_forfait) / 12`). Most suppliers bill only the regulated DSO rate; Cociter Variable, Mega and OCTA+ add a supplier-side PV forfait (already TVAC) on top. Only valid for Walloon installations certified before 2024-01-01; ends 2030-12-31. |
| `injection_price` | Injection regime | EUR/kWh paid for energy fed back to the grid. Dynamic contracts get `factor × spot + base` from the supplier's PDF using the live ENTSO-E spot. One variable contract whose injection is itself spot-indexed (Cociter Variable) also uses `factor × spot + base` and needs an ENTSO-E key to show a value. Other static contracts (including EBEM Groen Variabel / B@sic+, whose injection is a monthly SPP0 index) get the supplier's printed monthly indicative. Plug into HA Energy's *Solar production* → *I receive variable compensation based on a tariff* slot. Can go negative at low spot (you pay to inject). When the injection price varies across the day, `today` and `tomorrow` attributes carry chronological lists of `{start, injection}` so a battery force-export automation can rank the day's injection hours ahead of time (same hourly resolution as `current_price`; `tomorrow` fills in once the day-ahead publishes, ~13:00 CET). Only contracts whose injection actually varies expose these arrays: every dynamic contract, Cociter Variable (both spot-indexed), and Engie Empower Flextime (a fixed time-of-use schedule); flat and monthly-indexed contracts omit them since the value would just repeat. On those contracts the sensor's own value changes at each slot boundary together with `current_price`: on an hourly contract it is normally the `today` row for the hour you are in, and on a 15-minute contract it is the live quarter, while the `today` rows stay hourly means. An hour the day-ahead curve never published has no row and reads as its nearest neighbour instead, so treat the row as the authority when an automation needs the two to agree. |
| `contract_end_date` | A contract end date is set | Timestamp of your contract's end date (`device_class: timestamp`), so an automation can remind you to renew before it rolls over. Purely informational; it has no effect on pricing, and stays available even when a supplier fetch fails. |

## Installation

### HACS (recommended)

1. Open HACS, three-dot menu → **Custom repositories**.
2. Add `https://github.com/renaudallard/homeassistant_be_electricity_prices` as type **Integration**.
3. Install **Belgian Electricity Prices** and restart Home Assistant.
4. **Settings → Devices & services → Add integration → Belgian Electricity Prices**.

### Manual

Download the latest [release zip](https://github.com/renaudallard/homeassistant_be_electricity_prices/releases),
extract it under `<config>/custom_components/be_electricity_prices/`, and
restart Home Assistant.

`pypdf`, `pdfplumber` and `defusedxml` are the only extra runtime
dependencies; Home Assistant installs them automatically from the
manifest.

## Configuration

The UI walks **up to nine steps**, depending on contract type and region.
No EUR values are asked — energy, DSO and tax rates all come from the
supplier's tariff card.

1. **Supplier + Region** — Flanders / Wallonia / Brussels. Suppliers that
   don't sell in your region are filtered out.
2. **Contract** — filtered by supplier *and* region (e.g., TotalEnergies
   Impact only appears in Wallonia).
3. **DSO** — filtered by region.
4. **Meter type** — *mono* (single rate), *bi* (peak / off-peak), or
   *dynamic* (smart meter). Dynamic and TOU contracts (Luminus SmartFlex,
   Engie Empower Flextime) lock the picker to *dynamic* — the SMR3
   meter is required to bill by hour-of-day.
5. **DSO billing mode** *(Wallonia only)* — *Simple* / *Bi-horaire* / *Tarif
   Impact*. Tarif Impact uses the CWaPE 3-band hour-of-day rates and
   requires a smart meter; Simple and Bi-horaire follow the existing
   meter convention.
6. **ENTSO-E API key** *(dynamic contracts; also offered on the injection
   regime for a contract whose injection is itself spot-indexed —
   Cociter Variable)* — validated against the real ENTSO-E endpoint at
   submission; bad keys are rejected before the entry is saved. For the
   injection case it is optional and skippable: leave it blank to finish
   setup, and the injection price simply stays unavailable until you add
   a key via Reconfigure.
7. **Capacity tariff peak source** *(Flanders only)* — a sensor, or a fixed
   kW value (default 2.5 kW, the VREG regulated minimum). Fluvius bills the
   highest **quarter-hour** average offtake of the month, and a DSMR 5B meter
   computes exactly that and publishes it on the P1 port; Home Assistant's
   `dsmr` integration exposes it as *Maximum demand current month*. Point the
   field at that entity when you have it and the figure matches the meter.
   Any other power sensor (W, kW, VA, or kVA; the unit is honoured so a
   Riemann-source sensor in W is not misread as kW) reports your *live* draw,
   which the integration samples once an hour and keeps the maximum of: that
   is an estimate, not the billed quantity, and it can miss a peak between
   samples or read a momentary spike as a quarter-hour one. The picker is
   restricted to power / apparent-power sensors so a kWh / temperature /
   unitless sensor cannot be selected. The field is auto-filled with the
   meter's monthly-peak entity when one exists, otherwise with the power
   input of any Riemann `integration` helper that feeds the Energy
   dashboard's grid source, so users with the typical P1-power →
   kWh-Riemann → dashboard chain don't have to pick the same sensor
   twice; the auto-pick refuses non-power sources.
8. **Connection power** *(Brussels only)* — the contractual connection power
   tier (≤ 1.44 / 1.44-6 / 6-9.6 / 9.6-13 kVA). Brussels bills a Brugel OSP
   (Obligations de Service Public) annual fee scaled by this tier; existing
   entries default to the 1.44-6 kVA tier.
9. **Solar panels** — inverter capacity in kVA + the regime that applies:
   - **No solar panels** *(default)* — no extra sensors.
   - **Compensation regime** — Wallonia only, installations **certified before
     2024-01-01**, valid until 2030-12-31. Creates `prosumer_cost`.
   - **Injection tariff** — post-2024 Walloon installations and Flemish smart
     meters. Creates `injection_price`, ready for HA Energy.
10. **Energy meters** *(optional, all four / two fields are skippable)* —
   feeds the `current_year_cost` sensor. Two ways to wire it:
   - **Day/night register sensors** (4 fields): point at the cumulative
     kWh registers from your meter. The integration reads each day's
     delta from HA's long-term statistics, so the sensor reflects
     metered totals exactly and resets cleanly on Jan 1.
   - **Cumulative total sensors** (2 fields): point at a single
     running consumption sensor and a single running injection sensor.
     The integration reads daily kWh from the recorder and recovers
     the day/night split per past day from the recorder's hourly
     statistics binned via the bi-hourly schedule (no in-process
     buckets). Useful when your P1 / digital-meter integration only
     exposes totals (the standard HA case).
   - **Mix and match**: each side (consumption, injection) is
     resolved independently. You can wire registers for consumption
     and a single total for injection, or vice-versa. Partial
     register-pair wiring on either side is rejected so a missing
     band can't silently undercount.
   - When both wirings are filled for the same side the day/night
     registers win. Missing inputs collapse to the fees-only floor —
     the sensor never goes unknown.
   - **Auto-fill from the Energy dashboard**: if you've already
     configured a grid source in HA's Energy dashboard, the cumulative
     consumption / injection fields are pre-selected from the
     dashboard's first grid source so you don't pick the same sensor
     twice. When a `utility_meter` helper rooted at that grid source
     splits it into peak / offpeak (or jour / nuit, dag / nacht, piek /
     dal — case-insensitive, separator-tolerant) child tariffs, the
     four day/night registers are pre-selected too. Tariffs whose
     names don't map unambiguously to a day/night slot are left blank
     so a misnamed helper can't silently mis-bill. Whatever is
     pre-filled stays editable; an existing manual pick is never
     overwritten.

### Getting an ENTSO-E API key

Required for dynamic contracts, which is where the setup flow asks for it.
It is optional everywhere else, but two features use it when present: an
injection tariff that is itself spot-indexed (Cociter Variable), and the
signing-cohort re-price of a variable contract, which resolves the current
month's mean spot. Both stay off without a key rather than failing the
entry — the injection price goes unavailable, and the cohort re-price keeps
the current card. The token is free but ENTSO-E does not auto-grant it —
you have to request access explicitly:

1. **Register** an account on the
   [ENTSO-E Transparency Platform](https://transparency.entsoe.eu/) and
   confirm the verification email.
2. **Email** `transparency@entsoe.eu` from that address with the
   subject `Restful API access` and a one-line body asking to enable
   API access for the account. Allow 1–3 business days for the
   confirmation reply.
3. Once granted, on the Transparency Platform open
   **My Account Settings → Web API Security Token** and generate (or
   copy) the token. Paste it into the integration's *ENTSO-E API key*
   field — the config flow validates it against the real endpoint
   before saving the entry.

The token does not expire unless you regenerate it. If
`transparency.entsoe.eu` later rejects it with 401, the
`entsoe_auth_failed_<entry>` repair issue fires; paste a fresh token in
the entry's options to clear it.

### Reconfiguring later

**Settings → Devices & services → Belgian Electricity Prices → Configure**
opens a two-option menu:

- **Edit settings** — walks the same chain of steps, pre-filled with the
  current values. Change supplier, contract, region, DSO, meter, DSO
  billing mode, ENTSO-E API key, capacity peak source, or solar
  parameters — anything. The integration reloads automatically when you
  finish, picking the new tariff card on the next refresh.
- **Compare another supplier** — one-off price quote against a different
  supplier and contract, with your region / DSO / peak / solar
  settings held fixed for an apples-to-apples comparison. **Static
  ↔ dynamic crossings are allowed**: the flow prompts for an ENTSO-E
  API key when a side needs spot data (a dynamic contract, or a
  spot-indexed-injection target like Cociter Variable on the injection
  regime) and your current entry doesn't already carry one. Static
  contracts also let you
  override the meter type (mono / bi) so you can quote *what if I
  were on bi-hourly billing under supplier X*. The result page lists
  per-kWh price now, a projected yearly bill computed from your
  **measured rolling-year kWh** (recorder data from the consumption
  sensor configured in the meters step, or a 3500 kWh fallback), and
  a **year-to-date what-if** that re-prices your actual YTD kWh at
  each supplier's current rate with pro-rated annual fees, plus
  unicode bar charts so the difference reads at a glance. Solar
  regimes are honoured: compensation nets consumption against
  injection, injection regime credits each supplier's own injection
  price against the bill.
  Submit closes the dialog without changing anything; nothing is saved.

## Daily operation

### Refresh cadence

- **Supplier snapshot** — the coordinator runs a cheap `probe()` every
  hour and only re-fetches the full PDF when the probe key changes
  (see *How often the integration polls* above). Suppliers without a
  probe (Engie, Luminus, DATS 24, energie.be) fall back to a 24 h time-based TTL.
  Multiple entries pointing at the same
  `(supplier, contract, region)` tuple share their fetched snapshot
  through an in-memory cache, so the same PDF is never polled twice.
- **Spot prices** *(dynamic only)* — fetched from ENTSO-E at hourly resolution, or at the native 15-minute resolution for suppliers that bill per quarter-hour (Engie, Cociter, EBEM, Ecofix, OCTA+, Ecopower Dynamische Burgerstroom, Bolt Dynamisch, energie.be and EnergyVision); tomorrow's curve picked up after publication around 12:55 CET. Historical spots are backfilled lazily at hourly resolution into a per-entry persistent cache so dynamic `current_year_cost` replays each past hour at its actual rate without re-fetching the same window every tick.
- **Monthly capacity peak** *(Flanders)* — tracked continuously, resets on the 1st of each local month.
- **`current_year_cost`** — recomputed every coordinator tick from HA's
  recorder. The recorder's daily statistics are the source of truth for
  per-band kWh (no in-process counters that could drift across restarts);
  per-month tariff cards live in an in-memory cache keyed by
  `(supplier, contract, region, YYYY-MM)`, looked up once per month
  touched by the YTD window. Annual fees are pro-rated to the elapsed
  fraction of the year, so on Jan 1 the sensor sits at ~0 and grows day
  by day instead of jumping to the full annual upfront.

### Failure mode

If a refresh fails, the coordinator keeps serving the last known snapshot
and exposes `snapshot_age_hours`, `snapshot_stale` and `last_error` as
attributes on `sensor.<...>_current_price`. `last_error` always names the
failing exception, so a CDN timeout reads `network error fetching <url>:
TimeoutError` rather than trailing off after the colon. Five repair issues surface
under **Settings → System → Repairs** so problems are visible without
inspecting attributes; each auto-clears on the next successful refresh:

- **`snapshot_stale_<entry>`** — the cached snapshot is older than **7
  days**.
- **`extractor_failed_<entry>`** — the supplier extractor could not parse
  the tariff card (typically a layout drift on the supplier's PDF/HTML).
  Raised on the first failure, since a parse error will not self-heal;
  cached prices keep serving.
- **`extractor_unreachable_<entry>`** — the tariff card could not be
  downloaded (network timeout, reset or a transient server error). Raised
  only after several consecutive failed refreshes, since a single CDN
  hiccup usually clears on the next tick; cached prices keep serving.
- **`entsoe_auth_failed_<entry>`** *(dynamic contracts only)* — ENTSO-E
  returned 401 for the configured API key. Edit the entry's options
  and replace the key with a fresh token from
  transparency.entsoe.eu.
- **`supplier_deprecated_<entry>`** — the supplier has announced it is
  leaving the residential market, and names the successor and the transfer
  date (currently **DATS 24 → EnergyVision on 2026-08-31**). Prices stay
  correct until the supplier stops publishing its card; edit the entry and
  select the successor once your transfer is confirmed. Unlike the four
  above, this one is not a failure and does not clear on a refresh — it
  clears when the entry points at a supplier that is still selling. The
  successor is only named when this integration can actually price it in
  your region; otherwise the card says the entry will stop updating and
  asks you to check the letter your supplier sends.

### `be_electricity_prices.refresh` service

Drops the cached supplier snapshot **and** the ENTSO-E spot cache for every
loaded entry, then re-fetches both immediately. Handy after a tariff card
update or to clear a transient fetch error without waiting for the next
hourly tick. No fields.

### `be_electricity_prices.cheapest_window` / `most_expensive_window` services

Return the cheapest (or most expensive) contiguous N-hour window in the
upcoming price table. Both services share the same fields:

| Field | Default | Description |
| --- | --- | --- |
| `duration_hours` | _required_ | Window length in whole hours (1-48). On a 15-minute contract (Engie / Cociter / EBEM / Ecofix / OCTA+ / Ecopower Dynamische Burgerstroom / Bolt Dynamisch / energie.be / EnergyVision) the window aligns to quarter-hour boundaries. |
| `entry_id` | first loaded | Optional config entry to target. |
| `earliest_start` | now | Don't consider windows starting before this time. |
| `latest_end` | end of the cached table | Don't consider windows ending after this time. |

Response shape:

```yaml
start: "2026-04-30T03:00:00+02:00"
end:   "2026-04-30T06:00:00+02:00"
duration_hours: 3
average_eur_per_kwh: 0.184372
hours:
  - hour: "2026-04-30T03:00:00+02:00"
    all_in: 0.18012
  - hour: "2026-04-30T04:00:00+02:00"
    all_in: 0.18391
  - hour: "2026-04-30T05:00:00+02:00"
    all_in: 0.18908
```

Example automation that starts EV charging at the cheapest 4 h block of the
night:

```yaml
trigger:
  - platform: time
    at: "13:30:00"  # ENTSO-E next-day curve is published around 13:00 CET
condition:
  - condition: state
    entity_id: binary_sensor.<your_entry>_tomorrow_prices_available
    state: "on"
action:
  - service: be_electricity_prices.cheapest_window
    data:
      duration_hours: 4
      earliest_start: "{{ today_at('22:00') }}"
      latest_end: "{{ (today_at('06:00') + timedelta(days=1)) }}"
    response_variable: window
  - service: switch.turn_on
    target:
      entity_id: switch.ev_charger
    # Schedule the rest of the automation at window.start.
```

### `be_electricity_prices.backfill_statistics` service

Populates the recorder's long-term statistics for this entry's price
sensors (`current_price`, `energy_component`, `network_component`,
`taxes_component`, plus `injection_price` for injection-regime users)
and the `current_year_cost` running bill. The Energy dashboard and
the Statistics graph card then show price + cost history that
predates the entry's first live update tick.

The integration auto-triggers a one-shot backfill on first install
(or after a database reset) covering Jan 1 of the current local year
through "now"; the service is for re-runs after fixing a tariff card
or to redo a narrower window:

| Field | Default | Description |
| --- | --- | --- |
| `entry_id` | first loaded | Optional config entry to target. |
| `start` | Jan 1 00:00 local | First hour to backfill. The price sensors are written from this hour. `current_year_cost` resets each Jan 1, so it is backfilled only for the **end year**, accumulated from that Jan 1 — a mid-year `start` still carries the correct year-to-date total, and a multi-year range backfills only the current year's running cost (avoiding a spurious negative jump at the year boundary). |
| `end` | current hour | First hour NOT to backfill (exclusive); the in-progress hour is left to the live coordinator. |
| `clear` | `false` | Delete the target series first. Use after a tariff change so old rows don't mislead. |

Re-runs without `clear` are idempotent (rows are upserted by
`(statistic_id, hour)`). For dynamic suppliers the service reuses
the coordinator's ENTSO-E historical-spot cache, so a year-wide
backfill on a fresh install can take tens of seconds while the spots
land. Response is a `{rows_written, sensors, range}` object you can
inspect from Developer Tools → Services.

States history (the per-entity timeline shown in the **History**
view) is append-only by design and is not affected; only the
long-term statistics tables are written.

### Diagnostics

**Settings → Devices & services → Belgian Electricity Prices →** three-dot
menu **→ Download diagnostics** dumps the active config (with the ENTSO-E
API key redacted), the snapshot metadata, and the full hourly breakdown
for today + tomorrow. Attach it when reporting an issue.

## Exclusive-night meter circuit

Belgian households with an electric water heater or night-storage
heater often have a separate exclusive-night meter circuit billed at
the supplier's published `exclusive_night` rate. Configure it as a
**second config entry**:

1. Add a new Belgian Electricity Prices entry alongside your primary
   one.
2. On the meter step, pick **Exclusive-night circuit (separate
   meter)**.
3. On the energy meters step, point the cumulative-consumption sensor
   at the kWh sensor wired to the exclusive-night circuit.

Energy is billed at the supplier's `exclusive_night` rate; distribution
uses the DSO's published exclusive-night rate when the extractor
parses it (Bolt, Cociter, DATS 24, EBEM, Ecofix, Ecopower, Eneco,
energie.be, EnergyVision, Engie, Frank, Luminus, Mega, OCTA+, TotalEnergies), falling back to the
DSO's
off-peak rate for the few rows where the column position isn't yet
mapped — both better approximations than the day rate. The primary
entry keeps your day-circuit consumption on mono / bi / dynamic; YTD
and capacity tracking work normally on both entries.

## Development

Architecture and internals are documented for contributors under
[`docs/`](./docs/): a module map and end-to-end data flow, the coordinator
refresh lifecycle, the pricing model, the config and options flow, the ENTSO-E
and backfill data sources, the provider framework, and one reference page per
supplier extractor. Start with [`docs/README.md`](./docs/README.md).

```bash
ruff check .
ruff format --check .
mypy --strict custom_components/be_electricity_prices
pytest tests/
python scripts/live_check.py    # hits real supplier endpoints
```

Tests run against fixture PDFs and HTML snippets in
[`tests/fixtures/`](./tests/fixtures/) (real April 2026 cards from every
registered supplier, plus tiny HTML snippets under
`tests/fixtures/discover/` for catalog-discovery tests). Refresh a
fixture with the supplier's current PDF to re-run against new data.

A daily GitHub Actions workflow
([`.github/workflows/live_check.yml`](./.github/workflows/live_check.yml))
runs two phases against the live supplier endpoints:

- **Extractor phase** — every (contract, region) tuple is fetched and
  parsed; each fetch retries transient network errors up to three times,
  and the CI workflow re-runs the whole check up to seven times with
  escalating backoff. Persistent failures open or update a GitHub issue
  titled
  `[live-check] supplier extractor broken …`.
- **Catalog phase** — each supplier's `discover()` is run against its
  public listing page; any product visible at the supplier but missing
  from the registry opens a separate issue
  `[live-check] new supplier products detected …` so a parser regression
  and a catalogue addition stay in distinct threads.

## License

BSD 2-Clause. See [LICENSE](./LICENSE).

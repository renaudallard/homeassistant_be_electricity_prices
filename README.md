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
    <img src="https://img.shields.io/badge/HACS-Default-41BDF5.svg?style=flat-square" alt="HACS"/>
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
- **Dynamic contracts** — `factor × spot + base`, where `spot` is the Belgian day-ahead price from ENTSO-E. Priced per hour by default; suppliers that bill per quarter-hour (Bolt Dynamisch, Cociter, EBEM, Ecofix, Ecopower Dynamische Burgerstroom, energie.be, Energy Knights Agilior Online, EnergyVision, Engie and OCTA+, after the SDAC 15-minute market switch of Oct 2025) keep the native 15-minute slots for the live price, next slot and cheapest-window service. The `today` / `tomorrow` **list attributes** carry those same 15-minute slots, so an automation can plan a day against the grid it is billed on; only the cheapest / most-expensive lists stay hourly, since they are counted in hours (see `current_price` below). Year-to-date billing stays hourly too, since Home Assistant only retains hourly long-term statistics.
- **Monthly-indexed contracts** — `factor × this month's mean spot + base`, for cards that index the commodity to a monthly average rather than the live hourly price (energie.be Variabel and Energy Knights Essentia Online, both on Belpex_RLP). The rate is flat for the whole delivery month and firms up as the month fills in, so an ENTSO-E API key is required just as for a dynamic contract. When such a card indexes its injection on a *different* parameter, the credit is resolved on that one: energie.be pays on the solar-weighted Belpex_SPP, which runs far below the consumption index in sunny months, so the integration downloads Synergrid's solar production profile and weights the month's spot prices by it. Until that profile is available the card's own printed indicative is credited instead — never the consumption index, which would roughly double the credit.
- **Time-of-Use contracts** — Engie Empower Flextime and Luminus SmartFlex: 3 hour-of-day bands (peak / transition / offpeak) with the supplier's published rates per slot. Luminus SmartFlex is billed on its *seasonal* schedule: peak 07:00-11:00 + 17:00-22:00 all year, the cheapest super-creuses band 11:00-17:00 only in spring/summer (21/03-20/09), and 22:00-07:00 always at the middle creuses rate (the "free electricity on Sundays" first-year promo is not modelled).
- **Tarif Impact (Wallonia)** — opt-in CWaPE 3-band distribution pricing (PIC 17–22, MEDIUM 7–11 + 22–1, ECO 1–7 + 11–17), selectable independently of the supplier tariff. Under Impact comptage the SMR3 meter registers in these bands, so a bi-hourly supplier energy rate follows them too (ECO → off-peak rate, MEDIUM/PIC → peak rate) rather than the plain bi-horaire clock.
- **Flanders capacity tariff** — billed the way Fluvius bills it, on the mean of your last twelve monthly peaks (not on the month in progress), each month floored at the 2.5 kW regulated minimum before averaging. The monthly peak comes from your meter's own monthly-peak sensor when you have one (a DSMR 5B meter publishes it as *Maximum demand current month*, which is the quarter-hour peak Fluvius bills), else from any power sensor (W, kW, VA, or kVA — the unit is honoured) or a fixed value; billed against the configured Fluvius sub-area. **It is worth more than most people expect, and it is invisible on the price graph.** Fluvius charges between 52 and 60 EUR per kW of peak per *year* depending on the sub-area, so an 11 kW car charger pulling your monthly peak from 4 kW to 12 kW adds roughly 450 EUR a year on its own. None of that appears in `current_price` or the per-kWh sensors, because those are EUR/kWh and this is a yearly charge on kW: it is folded into `current_year_cost` only. If two entries on the same meter disagree by hundreds of euro, compare their `capacity_ytd_eur` and `billed_peak_kw` attributes first, since an entry left on the 2.5 kW default while the other reads your real peak will differ by exactly that gap and by nothing you can see anywhere else.
- **Solar** — prosumer fee for the Walloon compensation regime (until 2030-12-31), and a per-kWh injection price entity that plugs straight into HA Energy.
- **Year-to-date cost** — `current_year_cost` sensor reports your running bill in EUR since Jan 1 (or since your contract start date, if you tick that option), computed day by day (or hour by hour for TOU, dynamic and monthly-indexed contracts) from HA's recorder (consumption × the tariff of the month that day/hour belongs to). Each day is billed at its own month's published rate when the supplier archives historical cards (Bolt fix / Cociter / DATS 24 / EBEM / Ecopower / Eneco / Energy Knights / Frank / Mega); other suppliers fall back to the current rate as a proxy. The annual fees, the Walloon prosumer charge and the Flemish capacity tariff accrue into it too, each pro-rated over the elapsed year, so the figure is the whole bill rather than the energy side alone. **TOU contracts** (Engie Empower Flextime, Luminus SmartFlex) use the per-hour path so each kWh hits its actual peak / transition / offpeak rate. **Dynamic contracts** replay historical hourly ENTSO-E day-ahead spots from a persistent cache so each past kWh is billed at its actual `factor × spot + base` rate; an hour the cache cannot price still bills its network and tax legs and forfeits only the energy term, since those two are known from the month's card whatever the day-ahead price did. Compensation regime nets injection against consumption across the whole year (clamped at zero, since most Walloon suppliers forfeit surplus injection past consumption). Annual fees are pro-rated to the elapsed fraction of the year so the figure grows day by day instead of jumping to the full annual on Jan 1.
- **Projected year cost** — `projected_year_cost` sensor estimates what the whole calendar year will cost, so the figure can sit beside your supplier's monthly advance without a translation step. It prices a full year at today's tariffs against your own metered volume, **an indication rather than a forecast**, and it reports no value wherever a year cannot be honestly priced: contracts settling on a Belpex index for months that have not happened, a card that a contract start date has re-priced onto its signing cohort's formula, and Walloon compensation entries without about a year of metered feed-in to net against. Every leg's basis is published as an attribute, and `energy_basis` says which of these applies.
- **Cheapest / most-expensive window services** — find the optimal contiguous N-hour window in the upcoming price table for EV charging, heat-pump cycles, or peak avoidance.
- **Statistics backfill** — on first install (or after a database reset) the integration populates the recorder's long-term statistics for the price sensors and `current_year_cost` from Jan 1 of the current year up to "now", so the Energy dashboard shows price history immediately. This runs in the background after setup, as does the year's spot-price fetch on a dynamic or monthly-indexed contract: **finishing the wizard no longer waits on either**, so the last step returns in seconds rather than minutes. The year-to-date fills itself in over the following minute or two, and until it does the past hours it has not priced yet bill their network and tax legs and forfeit only the energy term. A `backfill_statistics` service is exposed for re-runs after a tariff change.
- **Tomorrow-available trigger** — `tomorrow_prices_available` binary sensor flips ON once ENTSO-E publishes the next-day curve, so dynamic automations don't fire too early.
- **Signing-cohort pricing** — set an optional **contract start date** and a fixed or dynamic contract is priced at the rate you locked in that month instead of today's new-customer card, for suppliers that archive past cards (Bolt fix / Cociter / EBEM / Ecopower / Eneco / Energy Knights / Frank / Mega), on their fixed and dynamic products. Only the contract's own terms are frozen to the signing month, both the commodity price and the feed-in coefficients, since a contract that locks one locks the other; the regulated network tariffs and taxes still track the current month, and the year-to-date cost bills every past month at the same locked rate. The setup flow also offers an optional **signing-rate** step where you type the rate you actually locked in — energy (single, day, night, exclusive-night circuit) or spot factor / base (for a dynamic contract and for a monthly-indexed one, both of which sign a coefficient pair), plus the yearly fee. What you type wins, field by field: the published card only ever knew the new-customer rate, so a promotional, brokered or negotiated rate has to come from you. Every box left blank falls back to the archived signing-month card, or to the current card when the supplier keeps no archive (or the start date is older than the archive reaches). Leave the whole step blank to price purely off the card. **Variable** contracts whose card exposes a numeric index formula (Cociter Variable, EBEM Groen Variabel / B@sic+, Eneco Flex and Flex One, Mega Flex) re-price differently: the signing month's *formula coefficients* are frozen and re-applied to the current month's mean spot, since a variable rate re-indexes monthly. This is exact for Cociter (its BELIX index is the arithmetic monthly mean); for the RLP-weighted cards (EBEM / Eneco / Mega) the arithmetic mean is a close approximation of the residential-load-profile weighting, so their re-price runs a few percent off. Where a card prints one formula per meter, each meter is billed its own — including a dedicated exclusive-night circuit, which is a separate contractual formula rather than the off-peak one. Resolving that mean needs spot data, so the variable re-price only runs when an **ENTSO-E API key** is configured; without one the entry keeps the current card and prices off its published rate. Time-of-use contracts still keep the current card.
- **Renewal reminder** — set an optional **contract end date** when you add or edit the entry and it is exposed as a `contract_end_date` timestamp sensor, so an automation can remind you to shop around before your contract rolls over. It also bounds what `projected_year_cost` claims: when the date falls inside the projected year, that sensor's `contract_basis` attribute says how many of the remaining days are actually under this contract. It changes no billed rate.
- **ENTSO-E key validated at setup** — the config flow hits the real endpoint with the entered token and rejects bad keys before the entry is saved.
- **Translated UI** — English, French, Dutch and German.
- **Ranked comparison of every alternative** — a *Compare every supplier* path in the OptionsFlow that prices every contract of your own kind sold in your region against your own settings and sorts them cheapest first. Your own contract sits in the table under a `YOUR CONTRACT` badge, and every other row states its gap against it, signed, so a minus is money you would save. Kept separate from the one-off quote below because the two answer different questions: a ranked table sorts on one number, and that number only means the same thing down a column of contracts shaped alike. Bounded by a wall-clock budget rather than a timeout, since a tariff card cannot be parsed halfway and abandoned: the quickest cards to fetch are downloaded first (Bolt and TotalEnergies take the longest, so they are the ones left pending), the table is on screen while it fills, a card that would not fit in the time left is skipped rather than started, whatever did not fit is named as still pending, and reopening finishes it from what was already downloaded. Rows that failed are shown with the reason rather than dropped. Optionally the whole ranking runs **once a day in the background** instead, at a time derived from the entry so installs do not all fetch at once, publishing the yearly saving of the best alternative as a sensor and making the page open instantly.
- **One-off contract comparison** — the OptionsFlow has a *Compare another supplier (one-off quote)* path that quotes a supplier and contract against your current region / DSO / peak settings. **Your own contract is in the list**, so the same path answers the two questions that need no change of supplier at all: *what would this contract cost me on a bi-hourly meter*, and *what would it cost off the compensation regime*. Picking your own contract makes the supplier delta zero by construction, and the baseline line described below is then the answer. **Static and dynamic contracts can be quoted against each other** ("should I switch from fixed to dynamic?"), but the list never crosses the residential/professional line, because a professional card is published excluding VAT and bands the excise by annual volume, so quoting one against a residential entry produces neither a price that household would pay nor a contract it could sign; the flow prompts for an ENTSO-E key when a side needs spot data (a contract priced off the spot — dynamic per slot or monthly-indexed on the delivery month's mean — or a spot-indexed-injection target like Cociter Variable on the injection regime) and you don't already have one saved. A monthly-indexed side is quoted at that month's mean rather than at a single day's, so the comparison matches what the contract actually bills and doesn't move with the day you opened the dialog. The annual estimate uses your **measured rolling-year consumption** (and, for solar users, injection) read from the same kWh sensors that feed `current_year_cost`, and it says which of four it used: a full year of history is taken as it stands, a shorter window down to 90 days is scaled up to a year and labelled as scaled, and anything thinner falls back to the yearly volume set on the entry or to the 3500 kWh household default. A six-week window is no longer presented as a year, which used to understate both sides of the quote. On a time-of-use or Impact card the slot rates are weighted by **your own hour-of-day consumption shape** rather than by how many hours each slot lasts, and a per-slot injection credit is weighted by when your panels actually export, so the estimate rests on the same basis as the `current_year_cost` sensor printed beside it; without enough history either falls back to the published slot durations. A dynamic contract's energy is priced at the mean weighted by when you actually draw, and a spot-indexed feed-in credit at the mean weighted by when your panels actually export, rather than at the window's flat clock average. Export is nothing all night and peaks at midday, which is where the day-ahead price troughs, and for a never-negative formula the rate of the average is not even the average of the rates. **The annual figure is an indication, not a prediction, and it will not match your final settlement.** It prices a full year at today's tariffs against a volume estimated from your own history. Tariffs move during the year, your consumption will not repeat exactly, and nothing here forecasts either of those. Read it as roughly what a year costs and as a way of ranking two suppliers against each other, which is what it is for, rather than as the bill you will receive. The result page also shows a **year-to-date what-if**: the actual kWh you've used since 1 January re-priced at each supplier's current rate (or since your contract start date, on an entry that bills its year-to-date from there, so the page and the sensor beside it cover the same period), with two-row unicode bar charts so the difference reads at a glance. The meter type is overridable for static contracts (compare *what if I were on bi-hourly billing*, under your own supplier or another one). The solar-weighted Belpex_SPP index is applied to a compared contract only when that contract's own card names it; an expert custom entry that opted into SPP weighting for its own formula no longer has that choice applied to other suppliers' cards, which had inverted their feed-in credit. A side whose rate is indexed on the delivery month is labelled as such, because the figure such a card prints is computed on the PREVIOUS month's index and says so: only your own contract is re-resolved against the month you are actually in, and only when your entry carries an ENTSO-E key, so an alternative is quoted a month behind. When the two sides are priced off cards published in different months, the result page says which one is older: every supplier transcribes the same regulated tariffs onto its own card, so a levy change landing between two publications moves one side by around 13 EUR a year for a reason that has nothing to do with its offer. A Walloon card that prints no connection-fee row is called out by name on the result page, because Wallonia still levies that fee and the supplier still passes it through, so such a card bills short and would otherwise rank cheaper for a reason that is not its offer. A Walloon *Tarif Impact* contract is always quoted on the CWaPE incitative network tariff it is sold on, whatever your own entry is set to: its card carries three band rates and no day/night structure at all, so pricing it any other way banded the energy while billing the network off the standard columns and charging a fixed term that tariff does not have. Solar regimes are honoured: compensation nets consumption against injection, with each side priced on its own hour-of-day shape so the netting matches what the reversing meter does rather than pricing exported kWh at the hours you draw them; injection regime credits each supplier's own injection price. The regime itself is overridable too (compare *what if I moved off the compensation regime*): unlike the meter type it applies to **both** sides, because it belongs to your grid connection and not to the supplier, and the result page prints your own contract priced both ways so the answer does not depend on the supplier you happened to pick. Entries with no injection meter are asked for their gross yearly volumes first, since a meter that runs backwards reports consumption already netted against injection. No second entry, no extra polling, nothing saved.
- **Keyless day-ahead fallback** — when ENTSO-E itself is unreachable, the Belgian day-ahead curve is fetched from [energy-charts.info](https://energy-charts.info) instead, so a platform outage no longer stops a dynamic contract pricing. ENTSO-E stays the source of record and the fallback is only consulted when it fails; a rejected API key is never masked by it, so the "rotate your token" repair still appears. The `current_price` sensor carries a `spot_source` attribute (`entsoe` or `energy-charts`) so a fallback price is always distinguishable from a source-of-record one. The fallback endpoint rate-limits to **two requests per minute per client IP**, so the year-to-date backfill asks it once for everything ENTSO-E could not answer rather than once per week-sized chunk: a whole year is a single request either way, and an ENTSO-E outage no longer spends its retries collecting HTTP 429s. If one does arrive, the `Retry-After` it carries is honoured: the source is left alone until the server says it may be asked again, across every entry on the host, since the limit counts the host and not the entry. The fallback data is CC BY 4.0 from Bundesnetzagentur | SMARD.de, credited on the sensor while it is in use, and matches the cleared prices published by Nord Pool, the exchange that runs the Belgian auction.
- **Self-healing** — last-known prices keep serving on outage, and the day-ahead curve is persisted, so an ENTSO-E outage spanning a Home Assistant restart no longer blanks a dynamic entry. A stored curve is only ever used for the day it actually prices: once it is outlived it is discarded rather than served as the current one. Nine repair issues surface under **Settings → System → Repairs**: snapshot older than 7 days, a supplier extractor parse failure (layout drift), a card that downloads fine but carries no readable text layer, the supplier being unreachable after repeated fetch failures, ENTSO-E rejecting the API key, a supplier that has announced it is leaving the residential market, and three cards for a value the supplier stopped printing (the exclusive-night distribution rate, the Walloon Tarif Impact bands, the Walloon connection fee). A single transient fetch timeout no longer raises an issue. The fetch and staleness ones auto-clear on the next successful refresh; the deprecation and missing-row ones clear when the underlying situation changes.
- **Catalog drift detection** — the daily live-check diffs each supplier's public catalog against the registry and opens a GitHub issue when a new product appears, verifies the card resolved is the newest one the supplier advertises, plus per-supplier wallclock + bytes-received telemetry to flag silent slowdowns and PDF size jumps.
- **Expert custom formula** — an escape hatch for suppliers that publish no public tariff card (group-purchase deals, B2B-flavoured products). You type the commodity formula (`factor × spot + base`, a monthly-averaged spot rate, or a flat rate) and all regulated DSO + tax values; there is no live card, so it's a static snapshot with none of the auto-update or drift-check safety net. Listed last in the supplier dropdown and clearly labelled as expert.

## Supported providers

| Supplier | Contracts | Source |
| --- | --- | --- |
| **Bolt** | Bolt Fixe · Bolt Plenty Fixe · Bolt Variable · Bolt Dynamisch *(quarter-hourly Belpex)* · Bolt Plenty Variable · Bolt Online · Bolt Plenty Online · all seven as **pro** contracts | [`providers/bolt.py`](./custom_components/be_electricity_prices/providers/bolt.py) — card URLs under `files.boltenergie.be/pricelists/<fix\|var>/`: fixed cards roll monthly by `<YYYYMM>`, variable and dynamic cards carry a version suffix Bolt bumps in place, read off the `boltenergie.be/fr/listes-des-prix` listing on every fetch (superseded versions stay served and still parse). Parsed via `pdfplumber` (rotated columns + Unicode line-separators). Bolt Dynamisch reads the same variable card and applies its printed `Belpex × factor + base` formula to the 15-minute spot. |
| **Cociter** | Tarif Variable (BELIX) · Tarif Variable Trihoraire *(BELIX on the CWaPE 3-band schedule)* · Tarif Dynamique (quarter-hourly BELPEX) | [`providers/cociter.py`](./custom_components/be_electricity_prices/providers/cociter.py) — monthly cards `RCVar_YMR_Coop-YYMM-fr.pdf` / `RCVaI_YMR_Coop-YYMM-fr.pdf` / `RCDyn_SM3_Coop-YYMM-fr.pdf`. Walloon citizen cooperative, **Wallonia only** |
| **DATS 24** *(withdrawn 2026-08-31)* | Elektriciteit Groen Variabel (BE_spotRLP-indexed monthly) | [`providers/dats24.py`](./custom_components/be_electricity_prices/providers/dats24.py) — one PDF per month on the Colruyt Group CDN, month spelled in the filename (`api.colruytgroup.com/api/static/dats24/parameters/site/<YYYY>/ELEK/NL/... Versie <MM> <YYYY>.pdf`), falling back one month while the new card is unpublished. Colruyt subsidiary; Flanders + Wallonia. Single product covers mono / bi / exclusive-night meter rates and includes the BE_spotSPP injection formula. **DATS 24 is leaving residential energy supply: contracts transfer automatically to EnergyVision on 31 August 2026**, so switch the entry to **EnergyVision**, which covers both regions. Existing entries keep pricing normally until that date and raise a Repairs card telling you where the contract is going. After it, the card changes to the past tense and the entry stops updating; the usual "could not reach the supplier" alert is suppressed, because by then the card simply is not published any more and that is expected rather than a fault — see [docs/providers/dats24.md](./docs/providers/dats24.md). |
| **EBEM** | Groen Variabel (BelpexRLP0 monthly, mono / bi / excl. night) · Groen B@sic+ (BelpexRLP0 monthly, single rate, online-only) · Groen Dyn@mic (Belpex 15-min, SMR3) | [`providers/ebem.py`](./custom_components/be_electricity_prices/providers/ebem.py) — Mol/Geel-area Flemish supplier (Ebem bvba). Monthly cards linked from `ebem.be/tarieven/` under opaque Umbraco media-hash URLs; the provider scrapes the listing each fetch and supports `fetch_for_month` against the public archive (≥ 6 months back), so past consumption bills at each month's actual rates. Variabel + B@sic+ share the `elek` PDF; Dyn@mic has its own. Flanders only. |
| **Ecofix** ⚠️ *(August and September 2026 cards unreadable)* | Motion (quarter-hourly Belpex 15M) · Motion Online (same formula, online-only) · Flexy (BELPEX-RLP-M monthly variable) · Flexy Online (same formula, online-only) | [`providers/ecofix.py`](./custom_components/be_electricity_prices/providers/ecofix.py) — stable URLs at `portal.ecofixgp.be/docs/prices/current/EL_Ecofix_<PRODUCT>_NL.pdf`, overwrite-in-place each month; the catalogue is read from the public tariefkaarten listing, which is the only surface that names a product the registry does not already carry. One PDF carries Flanders + Wallonia overlays (no Brussels). Parsed via `pdfplumber` for the column-major Wallonia DSO table. **Broken since the August 2026 card**, and the September card repeats it — see the note below. |
| **Ecopower** | Groene Burgerstroom (50% fixed + 50% Belpex DA, indexed monthly) · Dynamische Burgerstroom *(quarter-hourly EPEX DA)* | [`providers/ecopower.py`](./custom_components/be_electricity_prices/providers/ecopower.py) — Groene Burgerstroom from the monthly cards at `ecopower.be/groene-stroom/prijs-nieuw`; Dynamische Burgerstroom from the `dbs` card at `ecopower.be/groene-stroom/dynamische-burgerstroom` (`afname = 1,02 × EPEX DA + 4 €/MWh`, `injectie = 0,98 × EPEX DA − 15 €/MWh`). Flanders cooperative, Flanders only. Cards are HTVA so `vat_rate=0.06`. |
| **Eneco** | Zon & Wind Vast · Zon & Wind Flex · Zon & Wind Flex One · Zon & Wind Dynamisch | [`providers/eneco.py`](./custom_components/be_electricity_prices/providers/eneco.py) — monthly cards `cdn.eneco.be/downloads/nl/general/tk/BC_032_<NNNNNN>_NL_ENECO_POWER_<FIX\|FLEX\|FLEX_ONE\|DYNAMIC>.pdf` resolved from the public listing page each fetch (issue number rotates monthly), no Brussels; Vast, Flex and Flex One cover Flanders + Wallonia, Dynamisch is Flanders only |
| **energie.be** | Dynamisch *(quarter-hourly EPEX)* · Variabel *(monthly Belpex_RLP)* · Vast | [`providers/energiebe.py`](./custom_components/be_electricity_prices/providers/energiebe.py) — the dynamic residential card is served at the document API `energie-production-api.azurewebsites.net/api/v1/data/document?key=DynamicTariffs` (302-redirects to the current month's Azure blob); the variable card has no document key and its current PDF is named by `www.energie.be/api/v1/data/contracts`. Both parsed via `pdfplumber`. Flanders only; on the dynamic card only the residential block is read, and both print Belpex in c€/kWh so the spot factor is not scaled by 10. Variabel is billed as a **monthly-indexed** contract (see the highlight above) because the card prints only the VNR forecast of its index, not the realised month rate. Vast is a flat rate, so its energy leg needs no ENTSO-E key, but its card carries the same Belpex_SPP injection formula and settles on it, so a key is offered (and skippable) to credit the feed-in on the realized month rather than the card's printed forecast. |
| **Energy Knights** | Agilior Online *(quarter-hourly Belpex_15)* · Agilis Online *(hourly Belpex_h)* · Essentia Online *(monthly Belpex_RLP)* · all three as **Green** | [`providers/energyknights.py`](./custom_components/be_electricity_prices/providers/energyknights.py) — one card per product per month at a stable, product-keyed URL (`www.energyknights.be/website/getCurrentTariffchart/<slug>/nl`, the `/website/` prefix is required), parsed via `pdfplumber`; past months come off `getHistoricalTariffchart/<YYYY-MM>/<slug>/nl`, which switches to the pre-2026 slug and product name (`dynamic15` / `dynamic` / `variable`) and stops at 2025-01, before which the cards print the pre-merger ten Fluvius areas. Flanders only, and the eight Fluvius rows and the tax block are printed on the card itself, so one fetch carries the whole snapshot. Agilior and Agilis are the same card on different settlement grids. Essentia is billed as a **monthly-indexed** contract (see the highlight above) because the figure it prints is computed from the VREG weighted average annual price rather than the Belpex_RLP it settles on, and the two sit at least 10% apart in 19 of the 26 months Energy Knights publishes at `/priceparameters`; its four registers each carry their own coefficients, and its injection settles on the solar-weighted Belpex_SPP. The **Green** variants are the same cards plus a flat `Groene stroom` adder on every register, which moves (0,42 c€/kWh in Sept 2025 against 0,32 since). The card quotes its formulas in EUR/MWh excluding VAT while printing every rate VAT-inclusive, **except the injection row, which is VAT-exempt** — applying the 6% uniformly would overstate every feed-in credit. Optima Online and its Green twin are out of scope: their imbalance-trading service fee depends on the customer's own energy management system and has no field here. |
| **EnergyVision** | Dynamisch *(quarter-hourly Belpex)* · 3 jaar vast · 1 an fixe *(Wallonia)* | [`providers/energyvision.py`](./custom_components/be_electricity_prices/providers/energyvision.py) — monthly `Goedkope stroom` cards resolved from the `energyvision.be/nl-be/tariefkaart` listing (filenames carry the pricing month, e.g. `EV-0726-GSDYN-nl.pdf`), parsed via `pdfplumber`. Each product is published for one region in one language: the two Flemish cards in Dutch, the Walloon `1 an fixe` in French (`-WAL-fr`). The dynamic card prints Belpex in EUR/MWh (Bolt axis, factor not scaled by 10); both fixed cards index injection on the monthly Belpex-SPP-M and are credited at the delivery month's own solar-weighted mean, with the card's printed figure kept only as the fallback while that mean is unpublished; the card's 1 c€/kWh guarantee is applied on top. Gas and the per-volume tiered products are out of scope. Since the August 2026 card the Walloon `1 an fixe` no longer prints the `Redevance de raccordement`, a charge Wallonia still levies; prices exclude it (about €2.60/yr at 3500 kWh) and the entry raises a Repairs card saying so, which clears when EnergyVision prints the row again. |
| **Engie** | Easy Fixed · Easy Variable · Direct Online · Basic Online · Dynamic · Empower Fixed · Empower Variable · Empower Flextime *(TOU)* · Flow · Empty House · the same eight as **pro** contracts, minus Direct Online and Basic Online | [`providers/engie.py`](./custom_components/be_electricity_prices/providers/engie.py) — Engie's public REST endpoint at `engie.be/api/engie/be/ms/pricing/v1/public/pricesAndConditionsPDF`, one PDF per (contract, region). The professional editions are the same endpoint with `segment=P` and `_P_` in the document slug: identical layout, priced excluding 21% VAT, with the degressive excise schedule and the professional energy-fund row. |
| **Frank Energie** | Dynamisch · Dynamisch HV · Dynamisch Korting · Dynamisch JN · Dynamisch Slim | [`providers/frank.py`](./custom_components/be_electricity_prices/providers/frank.py) — monthly tariff card PDFs discovered via the public Sanity CMS file-asset API (`8navd656.api.sanity.io`), parsed via `pdfplumber`. Flanders only, five dynamic contract tiers with different factor/base/fee combinations. |
| **Luminus** | Comfy · Comfy+ · ComfyFlex · ComfyFlex+ · MaxxFix · MaxxFlex · BasicFix · BasicFlex · SmartFlex *(TOU)* · Dynamic | [`providers/luminus.py`](./custom_components/be_electricity_prices/providers/luminus.py) — Luminus's public REST endpoint at `luminus.be/api-next/get-pricelist/`, V/W only (no Brussels for market products) |
| **Mega** | Smart Fixed/Flex · Zen Fixed · Online Fixed/Flex · Cosy Fixed/Flex · Off-peak Fixed · Off-peak Flex · Off-peak Impact *(Wallonia, CWaPE 3-band)* · Dynamic · **pro**: SME Fixed/Flex · Smart Fixed/Flex · Online Fixed · Cosy Fixed/Flex · Off-peak Fixed · Dynamic · Zen Fixed | [`providers/mega.py`](./custom_components/be_electricity_prices/providers/mega.py) — scrapes the public listing at `mega.be/fr/energie/cartes-tarifaires` to resolve each `(product, region)` to its current PDF on `my.mega.be` |
| **OCTA+** | Fixed · Fixed Impact *(Wallonia, CWaPE 3-band)* · Eco Fixed · Smart Variable · Flux · Eco Flux · Dynamic · Eco Dynamic | [`providers/octaplus.py`](./custom_components/be_electricity_prices/providers/octaplus.py) — stable URLs at `files.octaplus.be/tariffs/E_OCTA_<PRODUCT>_RE_<VL\|WL>_FR.pdf`, parsed via word-coordinate alignment (heavy character spacing in the tax block) — Flanders + Wallonia only. Every non-dynamic card indexes its feed-in credit on the monthly Epex SPP and prints only an estimate of it, so the credit is resolved against the delivery month's own solar-weighted mean. |
| **TotalEnergies** | Electricité Fixe/Variable · Impact · myComfort · myComfort Fixe · myDrive · myDynamic · myEssential · myEssential Fixe | [`providers/totalenergies.py`](./custom_components/be_electricity_prices/providers/totalenergies.py) — stable URLs at `totalenergies.be/static/marketing-documents/b2c/tariff-card/latest/`, parsed via `pdfplumber` (rotated columns) |
| **Expert: custom formula** *(no public card)* | Dynamic (`factor × spot + base`) · Monthly average (`factor × monthly-mean spot + base`) · Fixed / manual rate | [`providers/custom.py`](./custom_components/be_electricity_prices/providers/custom.py) — an escape hatch for suppliers with **no public, machine-resolvable tariff card** (see below). Not scraped: you type the commodity formula and all regulated DSO + tax values, and the coordinator builds the snapshot from your config entry. |

> [!WARNING]
> **Ecofix: the August and September 2026 cards cannot be read automatically
> — but you can still price the contract, see the workaround below.** Ecofix
> regenerated its tariff PDFs as page images: every page of every product is one full-page
> image covering 99.9% of the sheet, in both the NL and FR editions. The pages
> carrying the **DSO network tables and the tax block hold no text at all** —
> only the month name. Those are most of a Belgian all-in price, so no snapshot
> can be assembled and the extractor fails loud rather than billing an
> incomplete figure. `current/` is overwrite-in-place and Ecofix publishes no
> dated archive, so there is no text-era card to fall back to.
>
> **The September 2026 cards repeat it.** All were republished on 31
> August 2026 at 11:19 GMT and are still page images, so this has now survived a
> month boundary and looks like a change to their publishing pipeline rather
> than a one-off accident. Measured on the same three files, against the copies
> committed here when Ecofix was added in May:
>
> | Card | 2 May 2026 | 31 August 2026 |
> | --- | ---: | ---: |
> | `EL_Ecofix_Flexy_NL.pdf` | 5 pages, 11 851 chars | 5 pages, 344 chars |
> | `EL_Ecofix_Motion_NL.pdf` | 5 pages, 11 406 chars | 5 pages, 174 chars |
> | `EL_Ecofix_Motion_Online_NL.pdf` | 4 pages, 8 400 chars | 4 pages, 158 chars |
| `EL_Ecofix_Flexy_Online_NL.pdf` | not committed in May | 4 pages, 325 chars |
>
> The page counts are unchanged and only the text layer is gone. The PDF
> metadata shows the producing tool changed from Canva to pypdf, which is what a
> rasterise-and-reassemble step in a publishing pipeline looks like.
>
> **Ecofix has been contacted** about this, with the figures above, asking them
> to export the cards with their text layer again. Nothing here needs to change
> if they do: the integration decides from the card it just downloaded, so
> support resumes on the next refresh with no update on your side.
>
> The extractor will not OCR them. Reading dense numeric tables printed with
> Belgian comma decimals is where OCR is least reliable, and a single misread
> digit would mis-price a bill *silently* — the opposite of how every other
> extractor here behaves. It would also mean shipping an OCR engine as a
> runtime dependency for one supplier.
>
> An Ecofix entry raises a dedicated Repairs card naming this workaround, rather
> than the usual "the supplier changed its layout, please open a GitHub issue"
> one: there is no text layer left to re-anchor a parser against, so that
> report would be unactionable. The integration decides this from the card it
> just downloaded rather than from a hardcoded per-supplier flag, so the moment
> Ecofix publishes a card with a text layer again everything resumes on the next
> refresh, with no update needed on your side.
>
> **Workaround: use the Expert: custom formula supplier**
> ([`providers/custom.py`](./custom_components/be_electricity_prices/providers/custom.py),
> listed in the table above and offered in the supplier picker). It
> collects exactly what went missing — the whole DSO block and the whole tax
> block — and it supports quarter-hourly billing, so Motion is reproduced
> faithfully rather than approximated. The part that is easy to get wrong is
> still machine-readable: page 1 (page 4 for Flexy) keeps the formulas as live
> text, so you can copy them straight out of the PDF. The August 2026 cards
> printed:
>
> | product | energy | injection |
> | --- | --- | --- |
> | Motion / Motion Online | `(0,1000 x Belpex 15M) + 1,1020` | `(0,0884 x Belpex 15M) - 0,5000` |
> | Flexy | `(BELPEX-RLP-M * 0,1020) + 1,2000` | `(BELPEX-SPP-M * 0,0884) - 0,5000` |
>
> Read the DSO and tax numbers off the card with your eyes — the image renders
> fine for a human — and enter them once. Check the formulas against your own
> current card rather than trusting the table above, which is a snapshot of one
> month.
>
> Existing Ecofix entries keep serving their last good snapshot and raise a
> Repairs card; they simply cannot pick up new months. That state now survives
> a restart and an upgrade: a cached card is normally discarded when a newer
> release parses more out of it, which is how a parser fix reaches an existing
> user, but for a supplier whose card can never be read again there is no next
> fetch to heal with, so the rejected card is replayed rather than dropped. An
> entry with no cached card at all, a brand-new one, still sets up, with every
> sensor unavailable and a Repairs card pointing at the Custom (expert)
> workaround. July's card parsed normally, so this is an unintended regression
> in Ecofix's document generator rather than a deliberate format change, and
> the real fix is upstream: nothing restores the automatic path until they
> publish a text PDF again.

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

**Professional (B2B) tariffs.** Bolt, Engie and Mega publish full professional
tariff cards carrying the same DSO and tax tables as their residential ones,
printed **excluding VAT**, and the integration reads them. Pick the supplier, then a *Pro* contract on the contract step, and two
extra settings appear:

- **Prices include VAT** — professional electricity is taxed at 21%. Leave this
  on if your business cannot deduct VAT; turn it off if it can, and every price,
  fee and cost sensor reports the ex-VAT amount you actually bear. Injection
  follows the same choice (unlike residential injection, which is VAT-exempt
  outright). Residential contracts print VAT-inclusive already, so the setting is
  hidden for them.
- **Estimated yearly consumption** — the Engie and Mega professional cards
  print the federal special excise as a degressive schedule (bands at
  20 000, 50 000 and 1 000 000 kWh/year) billed *per tranche*, so the rate
  applied is the blend of every band your year's volume spans rather than
  the band it lands in: at 30 000 kWh that is 0.013503 EUR/kWh, not
  0.012090. Bolt's professional cards print a single flat excise, so the
  setting does not change what they bill per kWh.

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
fixed 24-hour schedule. Suppliers that have no usable probe (DATS 24, energie.be, Engie
and Luminus, where no cheap freshness key is exposed) keep the time-based
24-hour TTL.

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
quarter-hour (Bolt Dynamisch, Cociter, EBEM, Ecofix, Ecopower Dynamische Burgerstroom, energie.be, Energy Knights Agilior Online, EnergyVision, Engie and OCTA+), which keep the
native 15-minute slots.

VAT spreads uniformly across components, so `energy_component +
network_component + taxes_component` always equals `current_price` to the cent.

## Sensors

All sensors share one device per config entry.

### Always created

| Sensor | Description |
| --- | --- |
| `current_price` | All-in EUR/kWh **now**. Attributes: `today` and `tomorrow` (chronological lists of `{start, energy, network, taxes, all_in}`), `snapshot_publication` (the card's publication month), `snapshot_age_hours`, `snapshot_stale`, `last_error`, `cheapest_4h_today` and `most_expensive_4h_today` (chronologically sorted, disjoint lists of `{start, price}`). **`today` and `tomorrow` carry the grid your contract settles on**: 24 rows a day on an hourly contract, 96 on a quarter-hourly one, so a battery or EV schedule can plan against the slots it is actually billed at. `cheapest_4h_today` and `most_expensive_4h_today` stay hourly on every contract, because they are counted in hours: ranking the native slots and taking four of them would turn *the cheapest four hours* into the cheapest one. The scalar today/tomorrow min/max/average sensors and the `cheapest_window` service have always kept the native resolution. On flat-tariff days where every hour rounds to the same all-in price (typical for fixed contracts), the cheapest list always comes back as the first 4 hours of the day and the most-expensive as the last 4 — automations keying on these for "cheapest window" should treat the output as undefined when the day's prices don't actually vary. When a **contract start date** is set on a fixed / dynamic contract, the energy component is priced at the signing month's card (see Highlights). |
| `next_hour_price` | All-in EUR/kWh for the next hour. |
| `today_average` | Daily average all-in EUR/kWh. |
| `today_min` / `today_max` | Daily extremes. |
| `tomorrow_average` | Average all-in EUR/kWh for tomorrow. Empty until ENTSO-E publishes the next-day curve (~13:00 CET) for dynamic contracts; available all day for fixed, variable and monthly-indexed contracts, except on the last day covered by a monthly card, where next month's rates are not published yet. Tracks `tomorrow_prices_available` exactly: the sensor has a value when that binary sensor is on. |
| `tomorrow_min` / `tomorrow_max` | Tomorrow's extremes. Same availability as `tomorrow_average`. |
| `energy_component` | Energy-only EUR/kWh now (VAT-inclusive). |
| `network_component` | Distribution + transport EUR/kWh now (VAT-inclusive). |
| `taxes_component` | Levies EUR/kWh now (VAT-inclusive). |
| `fixed_fee_eur_per_year` | Supplier's flat annual subscription fee (EUR/year), parsed from the tariff card. |
| `energy_fund_eur_per_month` | Flemish Energiefonds in EUR/month (€0 outside Flanders, and €0 in Flanders for domiciled customers). |
| `current_year_cost` | Running bill **since Jan 1 of the current year** (or since your contract start date, if you tick that option — see below), computed against HA's recorder (per day for fixed/variable, per hour for TOU, dynamic and monthly-indexed). Configure once in the **Energy meters** step, two ways: (a) point at the four day/night register sensors directly (preferred when available); or (b) point at single cumulative consumption / injection sensors (for bi-hourly meters the integration recovers the day/night split per past day from the recorder's hourly statistics binned by the bi-hourly schedule). Each kWh is multiplied by the tariff in effect for the month/hour it belongs to: when the supplier archives historical cards (Bolt fix / Cociter / DATS 24 / EBEM / Ecopower / Eneco / Energy Knights / Frank / Mega) past months use their own published rates; suppliers without an archive (Ecofix / energie.be / EnergyVision / Engie / Luminus / OCTA+ / TotalEnergies) fall back to the current rate as a proxy. Dynamic contracts replay historical hourly ENTSO-E spots from a persistent cache so each past kWh hits its actual `factor × spot + base` rate; an hour with no cached spot (cold start, or an ENTSO-E gap) bills its network and tax legs and forfeits only the energy term, so a partially filled cache understates the bill by the commodity alone rather than by the whole hour. Monthly-indexed contracts resolve one mean per delivery month, so a month that has already closed must hold at least 24 cached hours before its mean is billed: that average is applied to every hour of the month, and a handful of hours would price the whole month off an unrepresentative sample. The threshold is a count rather than a share of the month because refusing forfeits the entire commodity leg, so the mean only has to beat a 100% error to be worth billing, and against real Belgian prices it does so down to about a day's worth of hours. The month in progress keeps its running mean, being partial by nature. Annual fees (`yearly_fixed_fee + 12 × energy_fund_eur_per_month + 12 × prosumer_cost`, plus the DSO data-management fee and, in Brussels, the Brugel OSP fee for the configured connection-power tier) are summed per archived month using each month's snapshot, then pro-rated by `days_in_month_in_ytd / days_in_year` so the YTD running total still grows uniformly across the calendar year — on Jan 1 the sensor sits at ~0 and grows day by day, and on Dec 31 it carries the full annual amount. A supplier that re-indexes its fixed fee or energy fund mid-year is honoured for the months it applies to (same per-month snapshot path the prosumer fee already uses). Under Walloon compensation regime, injection is netted against consumption across the whole YTD and the energy term is clamped at zero (most suppliers forfeit surplus injection past consumption, so the bill never settles negative) — so once your year-to-date injection exceeds your consumption the energy term stays at zero and the sensor rests flat on the fees floor (`= fees_ytd_eur`); a value that stops moving while you keep injecting is that floor, working as designed, not a stalled sensor (the `energy_ytd_raw_eur` attribute shows the hidden negative term). If your meter is bidirectional and your contract actually pays for injected surplus, pick the **injection tariff** regime instead of compensation so that surplus is credited rather than forfeited. Today's partial day is read from the live meter (its current cumulative reading minus the reading at local midnight) rather than the day's long-term statistic, so the running total tracks today's usage in real time and keeps moving even when HA's statistics compilation lags or stalls; past days still come from the daily statistics. Always numeric: a fresh install in May still produces a meaningful figure for the year so far, as long as the recorder has history for the configured kWh sensors. When a **contract start date** is set on a fixed / dynamic contract, every past month's energy is billed at the signing-month rate (archive suppliers only) rather than each month's own card. Beside that date the contract step offers **"Bill the year-to-date from the contract start date"**: off by default, and with it on the window starts at that date instead of 1 January, so the figure covers only this contract rather than months another supplier billed. Fees pro-rate over the same window, so a contract signed on 30 June carries six months of standing charges rather than twelve, and the historical spot fetch stops at the start date too. The value drops the first time it applies, since it is now measuring a shorter period. It only affects the contract's **first calendar year**: from the next 1 January the two windows are the same, because the sensor is a yearly total the recorder buckets per calendar year and a window reaching back into a previous year cannot survive that. Every one of the coverage and volume attributes below spans the same window the cost does, so with the option on they count from your contract start date rather than 1 January and the rate they imply stays the rate you are billed. On hourly-billed contracts (TOU, dynamic, monthly-indexed) the sensor carries `hours_seen`, `hours_priced`, `hours_elapsed`, `consumption_ytd_kwh`, `injection_ytd_kwh` and `fees_ytd_eur`. Read them in that order: `hours_priced` below `hours_seen` means the spot cache could not price part of the window, so the bill is missing those hours' energy term and will rise as the cache fills; `hours_seen` below `hours_elapsed` means the recorder returned no statistics at all for the difference, which is the larger problem and does not heal on its own. Compare against `hours_elapsed` rather than reading `hours_priced` against `hours_seen` alone: a gap shrinks both of the first two together, so that pair reads a confident 100% while hundreds of hours are missing entirely. For static (fixed / variable) contracts the sensor instead carries — `consumption_ytd_kwh`, `injection_ytd_kwh`, `consumption_today_kwh`, `injection_today_kwh`, `days_seen` against `days_elapsed` (the same coverage check per day), `energy_ytd_raw_eur` (the energy term **before** the compensation zero-floor) and `fees_ytd_eur` — so a flat value can be read: a negative `energy_ytd_raw_eur` means banked injection has zeroed the energy term and the bill correctly rests on the fees floor (`= fees_ytd_eur`), while a `consumption_today_kwh` that never moves points at a stalled meter input rather than the integration. Both paths also split that fees figure into `capacity_ytd_eur`, `prosumer_ytd_eur` and `standing_charges_ytd_eur`, with `billed_peak_kw` beside them. The Flanders capacity tariff is charged per kW of monthly peak per year (52 to 60 EUR/kW across the Fluvius areas), so two entries reading the same meter and the same card can still differ by hundreds of euro when they resolve different peaks, and none of that shows on the per-kWh price graph. Comparing `capacity_ytd_eur` between two entries answers that in one glance. |
| `tomorrow_prices_available` | Binary sensor. ON when the price table covers at least one hour with tomorrow's local date **and** the supplier's published validity still covers tomorrow. Useful as a trigger for dynamic-tariff automations that should only fire after ENTSO-E publishes the next-day curve (~13:00 CET). For fixed/variable contracts it is ON throughout the month, but flips OFF on the last day of a month whose card stops at month-end, since next month's rates are not published yet. |
| `projected_year_cost` | Roughly what a year on this contract costs in EUR: a full year priced at **today's** tariffs against a yearly volume read from your own meter. **An indication, not a forecast**, and it will not match your settlement: tariffs move during the year and your consumption will not repeat exactly. It is a standalone estimate rather than the running bill plus a remainder, which is what keeps it steady through the year instead of sliding toward the fees floor, and what lets the Walloon compensation net be clamped once over the whole year rather than twice. The volume is your measured trailing year where you have one, otherwise it is annualised from a shorter window or falls back to a typed or default figure, and the attributes always say which. **No value at all for dynamic and monthly-indexed contracts**, for a variable card that a contract start date has re-priced to its signing cohort's monthly index, nor for a **compensation-regime** entry whose feed-in history falls short of about a year (350 of the trailing 365 days), which includes every such entry with no injection meter wired. The spot cases settle on a Belpex index for months that have not happened, ENTSO-E publishes day-ahead only, and there is no free forward curve, so the sensor reports nothing rather than inventing a number. Attributes carry the basis of every leg (`energy_basis`, `fee_basis`, `volume_basis`, `injection_basis`), `contract_basis` saying how much of the year today's contract still covers, and the volumes used (`annual_kwh`, `annual_injection_kwh`). Solar feed-in is folded in only when roughly a full year of injection history exists, since PV is far more seasonal than consumption and a partial window cannot be scaled by a day count, and the feed-in rate is time-weighted rather than read off the current slot. Under the compensation regime a short feed-in history is not a missing credit but the wrong bill, since the meter is netted against its own injection, so there the sensor reports nothing at all rather than billing the year gross. Not backfilled: a projection has no meaning as history. |

### Conditional

| Sensor | Created when | Description |
| --- | --- | --- |
| `capacity_cost` | Region = Flanders | Current monthly capacity cost in EUR (`billed_peak_kw × DSO_capacity_rate / 12`). `billed_peak_kw` is the mean of your last twelve monthly peaks, each floored at 2.5 kW first, which is what Fluvius charges on, so this stays steady through the year rather than tracking whichever month you are in. This charge also accrues into `current_year_cost`, so the two are consistent rather than the capacity term being invisible in the running bill. Carries `billed_peak_kw` and `months_counted` attributes; `months_counted` reaches 12 after a full year of history, and until then the mean covers only the months measured so far. |
| `monthly_peak_kw` | Region = Flanders | Running monthly peak power in kW (resets the 1st), reported as measured: the 2.5 kW regulated minimum is a billing rule and is applied to `capacity_cost` instead, so a quiet household now reads its true peak here rather than 2.5. State class is `MEASUREMENT` (mandated by HA for the POWER device class), so the long-term-statistics graph defaults to the **mean** aggregation. To see the true monthly peaks, switch the statistic-graph card to **Max** under Developer Tools → Statistics. A diagnostic **Reset monthly peak** button on the device page drops the rolling max so the next tick rebuilds it (use after a misconfigured sensor inflated the peak). |
| `prosumer_cost` | Compensation regime + `solar_kva > 0` | Monthly compensation fee in EUR (`solar_kva × (DSO_prosumer_rate + supplier_forfait) / 12`). Most suppliers bill only the regulated DSO rate; Cociter Variable, Mega and OCTA+ add a supplier-side PV forfait (already TVAC) on top. Only valid for Walloon installations certified before 2024-01-01; ends 2030-12-31. |
| `injection_price` | Injection regime | EUR/kWh paid for energy fed back to the grid. Dynamic contracts get `factor × spot + base` from the supplier's PDF using the live ENTSO-E spot. Several static contracts index their feed-in per slot and use the same `factor × spot + base`: Cociter Variable, and every Bolt fixed and variable card (whose printed injection figure the card itself calls an illustration). All of them need an ENTSO-E key — without one the sensor reads unavailable. energie.be Variabel and Vast index their injection on the monthly solar-weighted Belpex_SPP instead (a flat energy rate does not make the feed-in credit flat), so their credit is `factor × the month's SPP-weighted mean spot + base`, falling back to the card's printed indicative until Synergrid's solar profile is available. Other static contracts whose card indexes the feed-in on a *monthly* mean — including EBEM Groen Variabel / B@sic+, whose injection is a monthly SPP0 index — are credited at the delivery month's own resolved mean rather than at the printed figure: the card's indicative is computed on last month's index and serves only as the fallback until the month's own mean (and, for an SPP index, Synergrid's solar profile) is available. Only a card that prints a flat indicative with no formula is credited at that figure outright. Plug into HA Energy's *Solar production* → *I receive variable compensation based on a tariff* slot. Can go negative at low spot (you pay to inject). When the injection price varies across the day, `today` and `tomorrow` attributes carry chronological lists of `{start, injection}` so a battery force-export automation can rank the day's injection hours ahead of time (same resolution as `current_price`, so per quarter-hour on a 15-minute contract; `tomorrow` fills in once the day-ahead publishes, ~13:00 CET). Only contracts whose injection actually varies expose these arrays: every dynamic contract, Cociter Variable and every Bolt fixed / variable card (all spot-indexed), and Engie Empower Flextime (a fixed time-of-use schedule); flat and monthly-indexed contracts omit them since the value would just repeat. On those contracts the sensor's own value changes at each slot boundary together with `current_price`: it is normally the `today` row for the slot you are in, on either grid. An hour the day-ahead curve never published has no row and reads as its nearest neighbour instead, so treat the row as the authority when an automation needs the two to agree. |
| `contract_end_date` | A contract end date is set | Timestamp of your contract's end date (`device_class: timestamp`), so an automation can remind you to renew before it rolls over. Changes no billed rate. It does bound the projection: `projected_year_cost` reads it to report how much of the year today's contract still covers. Stays available even when a supplier fetch fails. |
| `potential_saving` | *Compare every supplier daily* is ticked | EUR a year the cheapest alternative would save you against your own contract, from the nightly ranking. Negative means nothing on the market beats what you have; unknown until the first sweep runs, or when your own contract could not be priced. The ranking is stored on disk, so it survives a Home Assistant restart instead of going unknown until the next nightly sweep; changing supplier discards it, since it was priced against the contract you left. Attributes carry the whole ranking (`ranking`, `cheapest`, `cheapest_annual_eur`, `own_annual_eur`, `priced`, `total`, `last_run`). Each `ranking` row has `label`, `annual_eur`, `is_own`, an optional `status` when it could not be priced, and an optional **`ytd_eur`**: what that contract would have cost you since 1 January, replayed over the same real archived months your own contract was replayed over. A row omits `ytd_eur` rather than showing a figure built on a different set of months, so a supplier with no month-addressable archive simply has no year-to-date cell. Your own row always carries one where any row does, since it is the figure the rest are read against. The `ranking` list is kept out of the recorder: it is replaced wholesale every night, so storing a snapshot of every row daily forever answers nothing. No `state_class`: a standing comparison is not metered. |

## Installation

### HACS (recommended)

1. Open HACS and search for **Belgian Electricity Prices** — it ships in the
   HACS default store, so no custom repository is needed.
2. Install it and restart Home Assistant.
3. **Settings → Devices & services → Add integration → Belgian Electricity Prices**.

### Manual

Download the latest [release zip](https://github.com/renaudallard/homeassistant_be_electricity_prices/releases),
extract it under `<config>/custom_components/be_electricity_prices/`, and
restart Home Assistant.

`pypdf`, `pdfplumber` and `defusedxml` are the only extra runtime
dependencies; Home Assistant installs them automatically from the
manifest.

## Configuration

The UI walks **up to ten steps**, twelve with the *Expert: custom formula*
supplier, depending on contract type and region. Apart from two paths no
EUR values are asked, since energy, DSO and tax rates all come from the
supplier's tariff card. The exceptions are the optional **signing-rate**
step, which appears when you set a contract start date and lets you type
the rate and yearly fee you actually signed, and the **Expert: custom
formula** supplier, which has no card and asks for the whole set.

1. **Supplier + Region** — Flanders / Wallonia / Brussels. Suppliers that
   have announced their exit from the residential market are dropped from
   the list, though an entry already on one keeps showing it so it stays
   editable. The region does *not* filter the list: pick a supplier that
   sells nothing in the chosen region and the step is re-shown with an
   error rather than the supplier being hidden.
2. **Contract** — filtered by supplier *and* region (e.g., TotalEnergies
   Impact only appears in Wallonia).
3. **DSO** — filtered by region.
4. **Meter type** — *mono* (single rate), *bi* (peak / off-peak),
   *dynamic* (smart meter), or *exclusive-night circuit* (a separate
   meter; see the section below). Dynamic, TOU (Engie Empower Flextime,
   Luminus SmartFlex) and Impact contracts (Cociter Tarif Variable
   Trihoraire, Mega Off-peak Impact, OCTA+ Fixed Impact) lock the picker to
   *dynamic* — the SMR3 meter is required to bill by hour-of-day.
5. **DSO billing mode** *(Wallonia only, and skipped for the three contracts sold on the CWaPE bands — Cociter Tarif Variable Trihoraire, Mega Off-peak Impact and OCTA+ Fixed Impact, which are locked to Tarif Impact)* — *Simple* / *Bi-horaire* / *Tarif Impact*. Tarif Impact uses the CWaPE 3-band hour-of-day rates and
   requires a smart meter; Simple and Bi-horaire follow the existing
   meter convention.
6. **ENTSO-E API key** *(dynamic and monthly-indexed contracts, both of
   which price the commodity off spot; also offered on the injection
   regime for a contract whose injection is itself index-linked, which is
   most static cards and not the handful it once was — every Bolt card and
   both Cociter variable cards index it per hour, while energie.be Vast and most of the
   rest index it on a monthly mean)* — validated against the real ENTSO-E endpoint at
   submission; bad keys are rejected before the entry is saved. If ENTSO-E is
   *unreachable* rather than rejecting the key, setup no longer dead-ends: the
   wizard says so and offers to check again or to continue without verifying.
   ENTSO-E has outages lasting a day or more, and while one is running there is
   no way to tell a good key from a bad one, so blocking setup would only punish
   you for their downtime. A key accepted that way is checked for real on the
   first price refresh, and the usual repair notice appears if it turns out to
   be rejected. Blank is not one of the answers here: the field is rejected
   on the spot, without asking ENTSO-E, because a dynamic contract has no
   price at all without a key. It is also the one value the keyless fallback
   cannot rescue, since the entry never gets as far as trying it. For the
   injection case it is optional and skippable: leave it blank to finish
   setup, and the injection price simply stays unavailable until you add
   a key via Reconfigure.
7. **Capacity tariff peak source** *(Flanders only)* — a sensor, or a fixed
   kW value (default 2.5 kW, the VREG regulated minimum). Leaving it at that
   default when your real peak is higher understates the bill by the
   difference times the per-kW rate, which is the largest single thing that
   can be wrong about `current_year_cost` without anything looking wrong on
   the price graph. Fluvius bills the
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
   tier (≤ 1.44 / 1.44-6 / 6-9.6 / 9.6-13 / 13-18 / 18-36 / 36-56 / > 56 kVA).
   Brussels bills a Brugel OSP (Obligations de Service Public) annual fee
   scaled by this tier, and a connection above 13 kVA is billed Sibelga's
   own higher power term in place of the data-management charge; existing
   entries default to the 1.44-6 kVA tier.
9. **Solar panels** — inverter capacity in kVA + the regime that applies:
   - **No solar panels** *(default)* — no extra sensors.
   - **Compensation regime** — Wallonia only, installations **certified before
     2024-01-01**, valid until 2030-12-31. Creates `prosumer_cost`.
   - **Injection tariff** — post-2024 Walloon installations and Flemish smart
     meters. Creates `injection_price`, ready for HA Energy.
10. **Energy meters** *(optional, all four / two fields are skippable)* —
   feeds the `current_year_cost` sensor. Whichever way you wire it, every
   field wants a **cumulative** kWh reading, one that only ever climbs.
   A sensor that resets, such as a "this year" or "this month" total,
   will not work: the integration bills the day-to-day *change* in the
   reading, and a reset reads as a large negative day. If you fill both
   wirings for the same side, the day/night registers win and the totals
   field on that side is ignored. Two ways to wire it:
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

Required for dynamic and monthly-indexed contracts (energie.be Variabel,
Energy Knights Essentia Online and Essentia Online Green, and the custom
supplier's monthly-average formula), which is where the setup flow asks
for it as a mandatory, validated field.
It is optional everywhere else, but two features use it when present: an
injection tariff that is itself index-linked — the hourly-spot shape
(Cociter Variable and Variable Trihoraire, every Bolt fixed and variable
card) and the monthly-mean shape (energie.be Vast on Belpex_SPP, and most
other static cards), 63 contracts across 14 suppliers between them, and the
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
opens a three-option menu:

- **Edit settings** — walks the same chain of steps, pre-filled with the
  current values. Change supplier, contract, region, DSO, meter, DSO
  billing mode, ENTSO-E API key, capacity peak source, or solar
  parameters — anything. The integration reloads automatically when you
  finish, picking the new tariff card on the next refresh.
- **Compare every supplier (ranked)** — a separate menu entry from the one-off
  quote below, and a different question. It prices **every contract of your own
  kind sold in your region** against your own settings and sorts them, cheapest
  first. **Your own contract is in the table** under a `YOUR CONTRACT` badge,
  priced from the card your entry already holds rather than re-fetched — so it keeps the signing
  rate and cohort splice you are actually billed on — and **every other row
  states its gap against yours**, signed, so a minus is money you would save.
  Same-kind on purpose: a fixed rate is a contracted price while a dynamic row
  is a projection of one year of spot prices onto the next, and sorting the two
  together puts the least certain number on top and calls it the cheapest. The
  one-off quote below is the place to cross that line, because it explains one
  pair at a time and has room to say why.
  The sweep is **bounded by a clock, not a timeout** — a tariff card cannot be
  parsed halfway and abandoned — so it fetches the cheapest cards first, and
  **the table is on screen while it fills**, rows appearing and reordering as
  each card lands. A card that would not fit in the time left is skipped rather
  than started, so the page stops cleanly instead of freezing for most of a
  minute on one slow supplier; reopening finishes the rest from what it already
  downloaded. On a Raspberry Pi the 50-candidate Flanders static cell prices
  about 15 rows in the first ten seconds, 34 in the first minute and 39 within
  the two-minute budget, the tail being Bolt and TotalEnergies at 13 to 45
  seconds a card.
  Rows that could not be priced are **shown, not dropped**, saying whether the
  card was unreadable or the supplier unreachable, because a missing row reads
  as *not competitive* and that is the one thing it does not mean. Suppliers
  that publish nothing for your region and segment are not offered, and if your
  own contract is the only one of its kind where you live, the page says so
  instead of showing an empty table. A **year-to-date column** is offered as a
  second, slower pass: it fetches each past month's card, and a row prints a
  figure only when it replayed the *same* real archived months your own side
  did — a supplier that keeps no month-addressable card would otherwise reuse
  today's card for January and print a confident number that is up to 23% out.
  A contract whose feed-in tariff is indexed on the spot price likewise prints
  a figure only where the day-ahead history to credit it is on hand, since that
  credit is dropped whole rather than estimated. Nothing is saved.
  A tick box on the last setup step, **off by default**, runs the whole ranking
  **once a day in the background** instead of on demand. It publishes a
  **Potential yearly saving** sensor — the euro a year the cheapest alternative
  would save you, with the full ranking in its attributes — and the comparison
  page then opens on the stored answer instead of making you watch the sweep,
  with the table saying when it ran and a box to price it again on the spot.
  Off by default because it fetches tariff cards from suppliers you have no
  relationship with, which is a decision to take rather than one an update
  makes for you. It is cheap once running: twelve of the seventeen suppliers
  publish a freshness check, including the two slowest cards, so a day on which
  nothing was republished costs a handful of conditional requests rather than
  the ~164 seconds a cold sweep takes, and tariff cards move about monthly. A
  negative reading on the sensor is a real answer: nothing on the market beats
  what you already have. Unlike the dialog the scheduled run has **no time
  budget and skips nothing**, because the budget only exists to keep a progress
  bar honest, and a ranking stopped early would call the cheapest row that
  *fitted* the cheapest there is.
- **Compare another supplier** — one-off price quote against a different
  supplier and contract, with your region / DSO / peak
  settings held fixed for an apples-to-apples comparison. **Static
  ↔ dynamic crossings are allowed**: the flow prompts for an ENTSO-E
  API key when a side needs spot data (a dynamic or monthly-indexed
  contract, or an index-linked-injection target like the Cociter variable
  cards or energie.be Vast on the injection regime) and your current entry doesn't already carry
  one. That prompt is skippable: a quote is a one-off, so leaving it blank
  still shows you every other line of the comparison rather than stopping
  you on a page you may have no token for. Static
  contracts also let you
  override the meter type (mono / bi) so you can quote *what if I
  were on bi-hourly billing under supplier X*. The result page lists
  per-kWh price now, a projected yearly bill computed from your
  **measured rolling-year kWh** (recorder data from the consumption
  sensor configured in the meters step, scaled up when the window is
  short, or a fallback volume when there is too little history), and
  a **year-to-date what-if** that re-prices your actual YTD kWh at
  each supplier's current rate with pro-rated annual fees, plus
  unicode bar charts so the difference reads at a glance. The yearly
  bill is an indication of the order of magnitude and will not match
  your settlement: it holds today's tariffs for twelve months and
  assumes your past consumption repeats, neither of which is a
  forecast. It is meant for ranking one supplier against another, and
  both sides are quoted on the same volume so the comparison stays
  fair even where the absolute figure is off. Solar
  regimes are honoured: compensation nets consumption against
  injection, injection regime credits each supplier's own injection
  price against the bill. A solar step lets you quote the whole thing
  under a **different regime** ("what would I pay off the compensation
  regime?"): it moves both sides, drops or adds the Walloon prosumer
  fee accordingly, and prints your own contract priced both ways.
  Without an injection meter it asks for your gross yearly consumption
  and injection first, because a meter that runs backwards reports a
  netted figure that the injection tariff does not bill; the
  year-to-date rows are then left blank, since they replay meter
  history recorded under your configured regime.
  Submit closes the dialog without changing anything; nothing is saved.

## Daily operation

### Refresh cadence

- **Supplier snapshot** — the coordinator runs a cheap `probe()` every
  hour and only re-fetches the full PDF when the probe key changes
  (see *How often the integration polls* above). Suppliers without a
  probe (DATS 24, energie.be, Engie, Luminus) fall back to a 24 h time-based TTL.
  Multiple entries pointing at the same
  `(supplier, contract, region)` tuple share their fetched snapshot
  through an in-memory cache, so the same PDF is never polled twice.
- **Spot prices** *(dynamic and monthly-indexed contracts)* — fetched from ENTSO-E at hourly resolution, or at the native 15-minute resolution for suppliers that bill per quarter-hour (Bolt Dynamisch, Cociter, EBEM, Ecofix, Ecopower Dynamische Burgerstroom, energie.be, Energy Knights Agilior Online, EnergyVision, Engie and OCTA+); tomorrow's curve picked up on the first hourly tick after publication (~13:00 CET), so up to an hour after it, since the coordinator's tick is not clock-aligned. Historical spots are backfilled lazily into a per-entry persistent cache, requested on the same grid the contract settles on and stored as one price per clock hour (the mean of that hour's slots), so `current_year_cost` replays each past hour at its actual rate (the live spot for a dynamic contract, the delivery month's mean for a monthly-indexed one) without re-fetching the same window every tick. A window neither source could answer is retried three hours later rather than on the next tick, so a platform outage no longer has every hourly tick re-requesting the whole year behind it; the missing hours forfeit only their energy term in the meantime, and fill in once a source answers again. An hour is the finest a replay can price because the recorder only keeps hourly consumption, and the mean is exact there for every formula that is linear in the spot. A never-negative feed-in formula is the one that is not (clamping at zero makes it convex, so an hour whose spot crossed the floor inside it is worth more than flooring its mean says), so an entry configured that way keeps that hour's own 15-minute slots beside the mean and replays the credit off them, matching what its `injection_price` sensor shows.
- **Monthly capacity peak** *(Flanders)* — tracked continuously, resets on the 1st of each local month.
- **`current_year_cost`** — recomputed every coordinator tick from HA's
  recorder; no in-process counters that could drift across restarts. Past
  days come from the recorder's long-term statistics: daily statistics on
  the static per-day path (where a bi-hourly totals meter recovers its
  day/night split from that period's hourly statistics), hourly statistics
  for the contracts billed hour by hour (TOU, dynamic, monthly-indexed,
  Impact, exclusive-night). Today is read live off the meter instead, its
  cumulative reading now minus the reading at local midnight, so the
  figure keeps moving even when statistics compilation lags.
  Per-month tariff cards live in an in-memory cache keyed by
  `(supplier, contract, region, YYYY-MM)`, looked up once per month
  touched by the YTD window. Annual fees are pro-rated to the elapsed
  fraction of the year, so on Jan 1 the sensor sits at ~0 and grows day
  by day instead of jumping to the full annual upfront.

### Failure mode

If a refresh fails, the coordinator keeps serving the last known snapshot
and exposes `snapshot_age_hours`, `snapshot_stale` and `last_error` as
attributes on `sensor.<...>_current_price`. `last_error` always names the
failing exception, so a CDN timeout reads `network error fetching <url>:
TimeoutError` rather than trailing off after the colon. Nine repair issues surface
under **Settings → System → Repairs** so problems are visible without
inspecting attributes; the fetch-related ones auto-clear on the next
successful refresh:

- **`snapshot_stale_<entry>`** — the cached snapshot is older than **7
  days**.
- **`extractor_failed_<entry>`** — the supplier extractor could not parse
  the tariff card (typically a layout drift on the supplier's PDF/HTML).
  Raised on the first failure, since a parse error will not self-heal;
  cached prices keep serving.
- **`extractor_unreachable_<entry>`** — the tariff card could not be
  downloaded (network timeout, reset, a transient server error, or the
  supplier's own file store refusing the download). Raised only after
  two consecutive failed refreshes, since a single CDN hiccup usually
  clears on the next tick; cached prices keep serving.
- **`entsoe_auth_failed_<entry>`** *(dynamic and monthly-indexed contracts)* — ENTSO-E
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
- **`extractor_unreadable_<entry>`** — the card downloaded fine but its
  pages carry no text layer, so no parser change here can read it (Ecofix
  since the August 2026 card). Cached prices keep serving, and it clears
  by itself the moment the supplier publishes a readable card.
- **`extractor_unreadable_no_prices_<entry>`** — the same unreadable card
  on an entry with no cached one to stand in: a brand-new entry, or one
  whose cache predates the card-as-parsed change. Every sensor on it reads
  unavailable until the supplier publishes a readable card, so the card
  points at the Custom (expert) supplier rather than warning about drift.
- **`exclusive_night_rate_missing_<entry>`** — the entry is on an
  exclusive-night meter but the supplier's DSO table prints neither an
  exclusive-night nor an off-peak distribution rate, so the night circuit
  is billed at the day distribution rate (TotalEnergies' Flemish cards).
- **`impact_rates_missing_<entry>`** — the entry is on the Walloon Tarif
  Impact distribution mode but the card omits the CWaPE PIC / MEDIUM /
  ECO bands, so it falls back to the bi-hourly split.
- **`connection_fee_missing_<entry>`** — a Walloon card stopped printing
  the connection fee, so that term is left out of the bill rather than
  guessed.

  The last four are not failures either: each clears when the supplier
  prints the missing row again.

### `be_electricity_prices.refresh` service

Drops the cached supplier snapshot **and today's** ENTSO-E prices for every
loaded entry, then re-fetches both immediately. Handy after a tariff card
update or to clear a transient fetch error without waiting for the next
hourly tick.

| Field | Default | Meaning |
| --- | --- | --- |
| `clear_history` | `false` | Also discard the cached **past** hourly prices that `current_year_cost` replays, and re-fetch them. Off by default because it re-fetches every day since 1 January against a rate-limited endpoint. |

Without `clear_history` the past-price cache is left alone, which is worth
knowing when a year-to-date figure looks wrong: an ordinary refresh will not
change it. Nothing else repairs that cache either, since a cached day already holding
at least 20 of its 24 hours is never re-fetched however wrong the values are, so this flag is
the only way to correct one short of deleting and re-adding the entry.

### `be_electricity_prices.cheapest_window` / `most_expensive_window` services

Return the cheapest (or most expensive) contiguous N-hour window in the
upcoming price table. Both services share the same fields:

| Field | Default | Description |
| --- | --- | --- |
| `duration_hours` | _required_ | Window length in whole hours (1-48). On a 15-minute contract (Bolt Dynamisch / Cociter / EBEM / Ecofix / Ecopower Dynamische Burgerstroom / energie.be / Energy Knights Agilior Online / EnergyVision / Engie / OCTA+) the window aligns to quarter-hour boundaries. |
| `entry_id` | first loaded | Optional config entry to target. |
| `earliest_start` | now | Don't consider windows starting before this time. |
| `latest_end` | end of the cached table | Don't consider windows ending after this time. |

Response shape:

```yaml
start: "2026-04-30T03:00:00+02:00"
end:   "2026-04-30T06:00:00+02:00"
duration_hours: 3
resolution: "PT60M"
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
| `start` | Start of the year-to-date window (Jan 1 00:00 local, or your contract start date if the entry bills from there) | First hour to backfill. The price sensors are written from this hour. `current_year_cost` resets at that window start, so it is backfilled only for the **end year**, accumulated from that Jan 1 — a mid-year `start` still carries the correct year-to-date total, and a multi-year range backfills only the current year's running cost (avoiding a spurious negative jump at the year boundary). |
| `end` | current hour | First hour NOT to backfill (exclusive); the in-progress hour is left to the live coordinator. Set it on or before 1 January of the current year and only the price sensors are rebuilt: a past year's cost series would sit immediately before the current year's, and the recorder ignores `last_reset` on imported statistics, so the join would show roughly minus one annual bill on the Energy dashboard. The response then carries a `skipped` note saying so. |
| `clear` | `false` | **Destructive.** Wipes each target statistic series in full, not just the requested range, while the re-import only repopulates `[start, end)` — for the price series, anything outside the window is gone. Use it for a full-year re-run (the default Jan 1 → now window). To redo a narrower window after fixing a tariff card, leave it off: the re-import upserts on `(statistic_id, hour)` and already overwrites exactly those hours. |

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
for today + tomorrow. It also summarises the replayed day-ahead cache per delivery month (hour
count, mean, min and max), the archived card labels used for past months,
and the shared-fetch failure marker when the integration has been backing
off. The year-to-date cost split into its capacity, prosumer and
standing-charge legs is not in the dump: it lives on the
`current_year_cost` sensor's own attributes.
Attach it when reporting an issue.

### When the year-to-date looks too low

A `current_year_cost` well under your real bill is almost always the kWh
side rather than the tariff side, and the integration says so in the log
rather than quietly billing what it was given. Three messages are worth
searching for:

- **"negative change"** — Home Assistant restarts the running total behind
  a sensor after an outage longer than `purge_keep_days`, and the restart
  lands as one large negative hour. Adding that up cancels real energy
  elsewhere in the year: a meter that moved 4687 kWh was billed for 942.
  Those hours are now ignored and the sensor is named. The figure corrects
  itself on the next refresh, since the year is recomputed from scratch
  each time.
- **"returned no statistics"** — one half of a day/night register pair is
  wired but produces nothing, so the pair cannot be billed. A sensor with
  `device_class: energy` but no `state_class` compiles no long-term
  statistics at all, and neither does `state_class: measurement`; both look
  perfectly normal in the UI.
- **"accumulated before the window"** — the first hour of the year carried
  energy from before 1 January, which happens when the run-up to New Year
  is missing from the recorder. That one over-bills rather than under-bills.

If none of those appear, check the coverage pair on the sensor's
attributes: `hours_seen` against `hours_elapsed` on an hourly-billed
contract (TOU, dynamic, monthly-indexed, Impact DSO mode or an
exclusive-night meter), or `days_seen` against `days_elapsed` on a fixed
or variable one.

## Dashboard cards

The `current_price` sensor already carries the whole price curve in its
`today` and `tomorrow` attributes, so a price graph needs no second
integration (EPEX Spot, Nordpool) alongside this one, and it plots your
own all-in rate rather than the raw wholesale spot.

The built-in history and statistics cards cannot draw it: they only
render the past, while half of this curve is in the future, and the
`today` / `tomorrow` arrays are deliberately kept out of the recorder so
they never bloat the database. Use the
[ApexCharts Card](https://github.com/RomRider/apexcharts-card) (HACS,
frontend) and build the series from the live attributes with
`data_generator`, which bypasses history entirely:

```yaml
type: custom:apexcharts-card
header:
  show: true
  title: Electricity Prices
graph_span: 2d
span:
  start: day
now:
  show: true
  label: Now
update_interval: 1min
yaxis:
  - decimals: 1
series:
  - entity: sensor.YOUR_ENTRY_current_price
    name: All-in price
    type: column
    unit: c€/kWh
    float_precision: 2
    data_generator: |
      const rows = [...(entity.attributes.today || []),
                    ...(entity.attributes.tomorrow || [])];
      return rows.map(r => [new Date(r.start).getTime(), r.all_in * 100]);
```

Replace `sensor.YOUR_ENTRY_current_price` with your own entity id
(Developer Tools → States, search `current_price`). The `* 100`
converts EUR/kWh to c€/kWh; drop it to plot EUR/kWh.

`update_interval` is what keeps the **Now** marker honest. ApexCharts
only redraws when the sensor writes a new state, which here happens at each
slot boundary — once an hour on an hourly contract, every 15 minutes on a
quarter-hourly one — so without it the marker drifts up to a full slot
behind the clock.
It costs nothing because `data_generator` reads the attributes directly
and never queries the database.

Two notes on the `tomorrow` half of the chart:

- On a **dynamic** contract it stays empty until the day-ahead curve
  publishes, around 13:00 CET. The `tomorrow_prices_available` binary
  sensor on the same device says when it has arrived.
- On a **fixed or variable** contract the curve is flat by design, so
  the chart is a row of equal bars, except on a bi-hourly meter or a
  time-of-use contract, where the day/night step shows, and under the
  Walloon Tarif Impact distribution mode, where the PIC / MEDIUM / ECO
  bands show as a three-level step on any meter.

Each row also carries the `energy`, `network` and `taxes` components, so
stacking them shows where the money actually goes — something a spot
price alone cannot tell you. Set `stacked: true` and give each component
its own series:

```yaml
type: custom:apexcharts-card
header:
  show: true
  title: Electricity Prices
graph_span: 2d
span:
  start: day
stacked: true
now:
  show: true
  label: Now
update_interval: 1min
series:
  - entity: sensor.YOUR_ENTRY_current_price
    name: Energy
    type: column
    unit: c€/kWh
    data_generator: |
      return [...(entity.attributes.today || []),
              ...(entity.attributes.tomorrow || [])]
        .map(r => [new Date(r.start).getTime(), r.energy * 100]);
  - entity: sensor.YOUR_ENTRY_current_price
    name: Network
    type: column
    unit: c€/kWh
    data_generator: |
      return [...(entity.attributes.today || []),
              ...(entity.attributes.tomorrow || [])]
        .map(r => [new Date(r.start).getTime(), r.network * 100]);
  - entity: sensor.YOUR_ENTRY_current_price
    name: Taxes
    type: column
    unit: c€/kWh
    data_generator: |
      return [...(entity.attributes.today || []),
              ...(entity.attributes.tomorrow || [])]
        .map(r => [new Date(r.start).getTime(), r.taxes * 100]);
```

On the injection regime the `injection_price` sensor exposes the same
`today` / `tomorrow` shape, with an `injection` key instead of `all_in`,
so the same card draws the injection curve after swapping the entity and
the field:

```yaml
type: custom:apexcharts-card
header:
  show: true
  title: Injection Price
graph_span: 2d
span:
  start: day
now:
  show: true
  label: Now
update_interval: 1min
yaxis:
  - decimals: 1
series:
  - entity: sensor.YOUR_ENTRY_injection_price
    name: Injection price
    type: column
    unit: c€/kWh
    float_precision: 2
    data_generator: |
      const rows = [...(entity.attributes.today || []),
                    ...(entity.attributes.tomorrow || [])];
      return rows.map(r => [new Date(r.start).getTime(), r.injection * 100]);
```

The bars can dip below zero at low spot, where you pay to inject. The
sensor only publishes those arrays on contracts whose injection actually
varies during the day (every dynamic contract, both Cociter variable cards,
every Bolt fixed and variable card, and Engie Empower Flextime); a flat or
monthly-indexed injection has no curve to draw, so the chart comes up
empty.

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
uses the DSO's published exclusive-night rate when the supplier's card
prints it (Bolt, Cociter, DATS 24, EBEM, Ecofix, Ecopower, Eneco,
energie.be, Energy Knights, EnergyVision, Engie, Frank, Luminus, Mega,
OCTA+, and TotalEnergies in Wallonia and Brussels), falling back to the
DSO's off-peak rate where it does not, and finally to the single day rate
on a card that publishes neither. TotalEnergies' Flemish cards are that
last case: such an entry raises an `exclusive_night_rate_missing` repair
saying the night circuit is being billed at the day distribution rate,
since no figure can be substituted for a column the supplier does not
print. The supplier's own exclusive-night *energy* rate still applies; it
is only the network leg that cannot be resolved. The primary entry keeps
your day-circuit consumption on mono / bi / dynamic; YTD and capacity
tracking work normally on both entries.

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
[`tests/fixtures/`](./tests/fixtures/) (real supplier cards spanning June 2025 to September 2026, one or more
per card-publishing supplier — the expert custom supplier has no card —
plus tiny HTML snippets under `tests/fixtures/discover/` for
catalog-discovery tests). Refresh a current-month fixture with the
supplier's current PDF to re-run against new data; the dated archive
fixtures are pinned to their month on purpose to guard the archive
parsers, and must not be refreshed.

A daily GitHub Actions workflow
([`.github/workflows/live_check.yml`](./.github/workflows/live_check.yml))
runs three phases against the live supplier endpoints:

- **Extractor phase** — every (contract, region) tuple is fetched and
  parsed; each fetch retries transient network errors up to three times,
  and the CI workflow re-runs the whole check up to seven times with
  escalating backoff. Only a check that fails in *every* one of those
  runs opens or updates a GitHub issue titled
  `[live-check] supplier extractor broken …`, so a slow runner timing
  out on a different supplier each time stays quiet.
- **Catalog phase** — the `discover()` of every supplier that implements one
  (all but energie.be and the expert custom supplier) is run against its
  public listing page; any product visible at the supplier but missing
  from the registry opens a separate issue
  `[live-check] new supplier products detected …` so a parser regression
  and a catalogue addition stay in distinct threads.
- **Freshness phase** — for the eight supplier-families that pick a card
  from several advertised ones, the card actually resolved is compared
  against the newest one the supplier advertises. A superseded card still
  downloads and still parses, so without this a stale URL looks identical
  to a healthy run. Suppliers that construct a single URL per contract are
  excluded: there is no candidate set to choose wrongly from, so a bad
  resolution fails loudly on its own.

## License

BSD 2-Clause. See [LICENSE](./LICENSE).

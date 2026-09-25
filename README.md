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
tariff card. **No EUR values are hardcoded in the source.** A supplier is one
Python module that knows where to find that supplier's publication and how to
read it. If yours is missing, **open an issue asking for it** rather than
writing the module: a tariff card carries a dozen things that are easy to read
almost right -- which column a meter type bills on, whether a figure is per
year or per month, whether a levy carries VAT -- and getting those wrong
mis-prices a bill quietly. Reviewing that costs more than writing it. There is a
[supplier request](https://github.com/renaudallard/homeassistant_be_electricity_prices/issues/new?template=supplier_request.yml)
form that asks for the four things it takes: the supplier, the product, a link
to the card, and the regions it is sold in.

> Targets Home Assistant **2026.4 or newer** (the minimum declared in `hacs.json`).

## Highlights

Each of these has a section of its own further down; this is the scan.

**Prices**

- **Live tariff cards** — every figure comes from the supplier's own published card. No EUR value is hardcoded.
- **Whole-bill view** — energy, distribution, transport, levies and VAT in one EUR/kWh, not just the commodity.
- **Dynamic contracts** — `factor x spot + base` against the Belgian day-ahead price, per hour or per quarter-hour depending on how the supplier bills. See [What the integration computes](#what-the-integration-computes).
- **Monthly-indexed contracts** — `factor x this month's mean spot + base`, for cards that settle on a month rather than an hour.
- **Time-of-Use and Tarif Impact** — peak / off-peak / super-off-peak cards, and the Walloon three-band CWaPE tariff, each priced on its own schedule.
- **Flanders capacity tariff** — billed the way Fluvius bills it, on the mean of your last twelve monthly peaks, floored per month at 2,5 kW. Worth 52 to 60 EUR per kW per year and invisible on the price graph, so it lands in `current_year_cost` alone.
- **Solar** — compensation regime, injection tariff, or neither.

**Costs over time**

- **Year-to-date cost** — your running bill since 1 January, each kWh priced at the tariff that applied when you used it. See [Sensors](#sensors) and [When the year-to-date looks too low](#when-the-year-to-date-looks-too-low).
- **Month-to-date cost** — the same for the month in progress.
- **Projected year cost** — roughly what a year on this contract costs, priced once at today's tariffs against your own measured volume. An indication, not a forecast.
- **Statistics backfill** — on first install the integration fills Home Assistant's long-term statistics from your recorder history, so the energy dashboard is not blank. See [the backfill service](#be_electricity_pricesbackfill_statistics-service).

**Choosing a contract**

- **Ranked comparison of every alternative** — prices every contract sold in your region against your own settings and sorts them cheapest first, with your own row badged and every gap signed. Optionally once a day in the background, publishing the best saving as a sensor.
- **One-off contract comparison** — quotes one supplier and contract against your settings, including your own contract, which answers *what would this cost me on a bi-hourly meter* and *what would it cost off the compensation regime*. Both live under [Reconfiguring later](#reconfiguring-later).
- **Signing-cohort pricing** — set a contract start date and past months bill at the rate you actually signed, not at today's card, for the suppliers listed under `current_year_cost` in [Sensors](#sensors). The feed-in credit follows the formula you signed, and where the card fixes its feed-in price for the term too (Mega's fixed range, Trevion Groene Energie Vast, EnergyVision's fixed-injection card) the price you signed at. See [Configuration](#configuration).
- **Renewal reminder** — an optional notice before a fixed contract's end date.

**Running it**

- **Cheapest / most-expensive window services** — ask for the best N-hour block of the day from an automation.
- **Tomorrow-available trigger** — a binary sensor that flips when tomorrow's prices land.
- **Keyless day-ahead fallback** — ENTSO-E is down: prices keep coming from a keyless source until it recovers. It only steps in for an entry that holds a key; a dynamic contract needs its own, and an optional key you skipped bills a monthly-indexed card's printed figure while a feed-in formula settled per slot stays uncredited and its price unavailable.
- **Self-healing** — last-known prices keep serving through an outage, and Repairs cards explain anything that needs you. See [Failure mode](#failure-mode).
- **Catalog drift detection** — a daily check that tells the maintainer when a supplier changes its lineup.
- **Expert custom formula** — type a card in by hand when a supplier is not covered.
- **ENTSO-E key validated at setup**, and a **translated UI** (EN, NL, FR, DE).

## Supported providers

| Supplier | Contracts | Source |
| --- | --- | --- |
| **Bolt** | Bolt Fixe · Bolt Plenty Fixe · Bolt Variable · Bolt Plenty Variable · Bolt Online · Bolt Plenty Online · all six as **pro** contracts | [`bolt.py`](./custom_components/be_electricity_prices/providers/bolt.py) · [notes](./docs/providers/bolt.md)
| **Cociter** | Tarif Variable (BELIX) · Tarif Variable Trihoraire *(BELIX on the CWaPE 3-band schedule)* · Tarif Dynamique (quarter-hourly BELPEX) | Wallonia only · [`cociter.py`](./custom_components/be_electricity_prices/providers/cociter.py) · [notes](./docs/providers/cociter.md)
| **DATS 24** *(withdrawn 2026-08-31)* | Elektriciteit Groen Variabel (BE_spotRLP-indexed monthly) | Flanders + Wallonia · [`dats24.py`](./custom_components/be_electricity_prices/providers/dats24.py) · [notes](./docs/providers/dats24.md)
| **EBEM** | Groen Variabel (BelpexRLP0 monthly, mono / bi / excl. night) · Groen B@sic+ (BelpexRLP0 monthly, single rate, online-only) · Groen Dyn@mic (Belpex 15-min, SMR3) | Flanders only · [`ebem.py`](./custom_components/be_electricity_prices/providers/ebem.py) · [notes](./docs/providers/ebem.md)
| **Ecofix** ⚠️ *(cards are page images since August 2026, read in CI)* | Motion (quarter-hourly Belpex 15M) · Motion Online (same index, own coefficients and a 10 €/yr standing charge, online-only) · Flexy (BELPEX-RLP-M monthly variable) · Flexy Online (same index, own coefficients and the same 10 €/yr charge, online-only) | Flanders + Wallonia · [`ecofix.py`](./custom_components/be_electricity_prices/providers/ecofix.py) · [notes](./docs/providers/ecofix.md) · **see the note below the table**
| **Ecopower** | Groene Burgerstroom (50% fixed + 50% Belpex DA, indexed monthly) · Dynamische Burgerstroom *(quarter-hourly EPEX DA)* | Flanders cooperative, Flanders only · [`ecopower.py`](./custom_components/be_electricity_prices/providers/ecopower.py) · [notes](./docs/providers/ecopower.md)
| **Eneco** | Zon & Wind Vast · Zon & Wind Flex · Zon & Wind Flex One · Zon & Wind Dynamisch | Flanders + Wallonia, Dynamisch is Flanders only · [`eneco.py`](./custom_components/be_electricity_prices/providers/eneco.py) · [notes](./docs/providers/eneco.md)
| **energie.be** | Dynamisch *(quarter-hourly EPEX)* · Variabel *(monthly Belpex_RLP)* · Vast | Flanders only; on the dynamic card only · [`energiebe.py`](./custom_components/be_electricity_prices/providers/energiebe.py) · [notes](./docs/providers/energiebe.md)
| **Energy Knights** | Agilior Online *(quarter-hourly Belpex_15)* · Agilis Online *(hourly Belpex_h)* · Essentia Online *(monthly Belpex_RLP)* · all three as **Green** | Flanders only · [`energyknights.py`](./custom_components/be_electricity_prices/providers/energyknights.py) · [notes](./docs/providers/energyknights.md)
| **EnergyVision** | Dynamisch *(quarter-hourly Belpex)* · 3 jaar vast · 1 an fixe *(Wallonia)* · 1.800 kWh vast *(all three regions)* · vaste injectieprijs 3 jaar · Laadpunt · Groene stroom *(Brussels, monthly Belpex-RLP-M)* | The 1.800 kWh card is sold in all three regions and prices the same energy leg in each, but Wallonia pays no standing charge where the other two pay 50 EUR/yr. In Brussels EnergyVision trades as **Brusol** and publishes on its own site, and there the card is sold only to households with EnergyVision/Brusol panels on the roof; Groene stroom is Brusol's own product, any Brussels household can take it, and it is the one card in the registry that prices a direct-debit payer differently (250 EUR/yr, 230 on domiciliëring), which the setup flow asks about · [`energyvision.py`](./custom_components/be_electricity_prices/providers/energyvision.py) · [notes](./docs/providers/energyvision.md)
| **Engie** | Easy Fixed · Easy Variable · Direct Online · Basic Online · Dynamic · Empower Fixed · Empower Variable · Empower Flextime *(TOU)* · Flow · Empty House · the same eight as **pro** contracts, minus Direct Online and Basic Online | All three regions, Basic Online is Flanders + Wallonia only · [`engie.py`](./custom_components/be_electricity_prices/providers/engie.py) · [notes](./docs/providers/engie.md)
| **Frank Energie** | Dynamisch · Dynamisch HV · Dynamisch Korting · Dynamisch JN · Dynamisch Slim | Flanders only · [`frank.py`](./custom_components/be_electricity_prices/providers/frank.py) · [notes](./docs/providers/frank.md)
| **Luminus** | Comfy · Comfy+ · ComfyFlex · ComfyFlex+ · MaxxFix · MaxxFlex · BasicFix · BasicFlex · SmartFlex *(TOU)* · Dynamic · most months run a new-customer campaign tied to the month you sign, either a share of the energy cost (33% on the September 2026 Comfy card) or a volume of free energy (750 kWh as a cashback after 12 months, valued at the card's own mono-hourly rate because that is the rate its terms name), and it is billed from the card of your contract start month, which the archive can only supply from September 2026 on: Luminus's own tariff archive serves a past month without its campaign, so a contract signed before then is priced with no campaign at all | Flanders + Wallonia only · [`luminus.py`](./custom_components/be_electricity_prices/providers/luminus.py) · [notes](./docs/providers/luminus.md)
| **Mega** | Smart Fixed/Flex · Zen Fixed · Online Fixed/Flex · Cosy Fixed/Flex · Off-peak Fixed · Off-peak Flex · Off-peak Impact *(Wallonia, CWaPE 3-band)* · Dynamic · the Flex and Impact cards index monthly on the RLP-weighted Belpex, the SME cards on the plain mean; most of the range grants a first-year ristourne, credited at the twelve-month anniversary (fourteen on Zen Fixed and its pro twin), and seventeen cards price it on whether the household pays by direct debit · **pro**: SME Fixed/Flex · Smart Fixed/Flex · Online Fixed · Cosy Fixed/Flex · Off-peak Fixed · Dynamic · Zen Fixed | [`mega.py`](./custom_components/be_electricity_prices/providers/mega.py) · [notes](./docs/providers/mega.md)
| **OCTA+** | Fixed · Fixed Impact *(Wallonia, CWaPE 3-band)* · Eco Fixed · Smart Variable · Flux · Eco Flux · Dynamic · Eco Dynamic | Flanders + Wallonia only · [`octaplus.py`](./custom_components/be_electricity_prices/providers/octaplus.py) · [notes](./docs/providers/octaplus.md)
| **TotalEnergies** | Electricité Fixe/Variable · Impact *(Wallonia)* · myComfort · myComfort Fixe · myDrive · myDynamic · myEssential · myEssential Fixe | [`totalenergies.py`](./custom_components/be_electricity_prices/providers/totalenergies.py) · [notes](./docs/providers/totalenergies.md)
| **Trevion** | Groene Energie Vast · Groene Stroom Flex *(monthly Belpex_RLP_VL)* · Groene Energie Dynamisch · Groene Energie Dynamisch Plus · LifePowr *(monthly Belpex_RLP_VL since June 2026, quarter-hourly Belpex 15 MTU before)* · Energreen | Flanders only · [`trevion.py`](./custom_components/be_electricity_prices/providers/trevion.py) · [notes](./docs/providers/trevion.md)
| **Expert: custom formula** *(no public card)* | Dynamic (`factor × spot + base`) · Monthly average (`factor × monthly-mean spot + base`) · Fixed / manual rate | Flanders and the green-energy contribution in Wallonia and Brussels, and the connection-fee box (the Walloon redevance de raccordement, VAT-exempt) appears on Walloon entries only · [`custom.py`](./custom_components/be_electricity_prices/providers/custom.py)

> [!WARNING]
> **Ecofix publishes its cards as page images, and has since August 2026.**
> Nothing in the integration can parse a document with no text in it, so the
> prices come from this project's own reading of the card's pixels: the card
> archive runs a reader built for these cards once a day, files what it read,
> and your entry uses that. It stops by itself when Ecofix publishes a readable
> card again, because what makes a card unreadable is measured on every fetch
> rather than held as a flag against the supplier.
>
> **The comparison pages use that reading too**, and say so. A supplier whose
> card cannot be read used to show up as an error on the ranking, which is the
> one screen that exists to tell you whether to switch to it. Its row is now
> priced from the same reading your own entry uses and tagged `OCR`, and the
> one-off quote adds a line naming the supplier and saying where the figure
> came from. A price you might act on should never hide that it was read off a
> picture.
>
> **Check the taxes against your own card.** The energy formula and the
> standing charge survive as live text and are read exactly, and the DSO tables
> read off the image are right: those tariffs are set for the calendar year, so
> a picture taken in July still prints September's. The tax block is the one
> that has moved. Ecofix's September 2026 card still carries the federal scheme
> that ended on 1 August, an excise of 0,0503288 with a 0,0020417 contribution
> beside it, where every other supplier's card for the same month carries a flat
> 0,04876 and no contribution. Neither is billed. Both are federal levies set
> by law rather than by your supplier, so the integration bills what the law
> sets for the month being billed: the contribution was abolished on 1 August
> and is dropped, and the excise is the 0,04876 every other card in the country
> prints. That is the 0,0036105 €/kWh, about 12,64 € a year on 3.500 kWh, the
> stale block used to cost you. The daily live check still compares every
> supplier's federal block against the rest and reports a card that drifts. A
> figure the reader could not read whole is left out rather than guessed. If you would rather type the numbers in yourself, the
> **Expert: custom formula** supplier takes them.
>
> The full story — what changed in their generator, what Ecofix said about it,
> the measurements, and the current formulas to copy — is in
> [docs/providers/ecofix.md](./docs/providers/ecofix.md).

Missing a supplier? Ask for it with the
[supplier request](https://github.com/renaudallard/homeassistant_be_electricity_prices/issues/new?template=supplier_request.yml)
form rather than sending a module — see the note at the top of this file for
why. One module, its registration and a fixture-based test is all it takes to
add one; what takes the time is reading the card correctly.

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
It uses the published *ex-ante* profile, which is the one the suppliers' own
settled indices are computed on: measured over January to August 2026 this
reproduces the Belpex-SPP-M Energy Knights publishes to 0,007%. Two suppliers
weight the quarter-hour prices instead of the hourly ones and so publish a value
about 0,9 EUR/MWh lower; their contracts are settled on the figure their own card
prints rather than on this mean. It falls back to the plain mean if the profile
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
quarter-hour (Cociter, EBEM, Ecofix, Ecopower Dynamische Burgerstroom, energie.be, Energy Knights Agilior Online, EnergyVision, Engie, OCTA+ and Trevion, plus Bolt and Frank Energie with the quarter-hour box ticked), which keep the
native 15-minute slots. An OCTA+ contract signed before 2026 is the exception: its card names the
hourly Belpex, so with a contract start date before 2026 it is priced by the hour.

VAT spreads uniformly across components, so `energy_component +
network_component + taxes_component` always equals `current_price` to the cent.

### The federal levies come from the law, not from your card

Two items in that `levies` term are set by federal law rather than by your
supplier: the **special excise** and the **energy contribution**. One rate
applies to every residential customer in the country in a given month, so the
month being billed decides them, not the month your card was printed in.

Since **1 August 2026** the law sets a flat excise of **4,876 c€/kWh** and
abolished the energy contribution, folding it into that rate. The integration
bills exactly that for every month from then on, and a card that still prints
the old pair is not billed as printed. Three suppliers were still printing it
in September 2026: Ecofix, whose card is an image of its July card, Cociter,
and TotalEnergies with a rounded figure. Together the two corrections are worth
about 12,64 € a year at 3.500 kWh on an Ecofix contract.

Two limits worth knowing. **Professional contracts are untouched**, because
that scheme bands the excise by annual volume and is a genuinely different
rate. And **the correction covers the months we can verify**: the same measure
steps the excise down again each January from 2027, so from 1 January 2027 your
card's own figure is used again until the new rate is confirmed against the
fleet. Before August 2026 the card's figures were always used, so a bill for an
earlier month is unaffected.

The daily live check compares every supplier's federal block against the rest
and reports a card that drifts, which is how a supplier that corrects itself
gets noticed: Bolt and Trevion both did between August and September.

The network rows are compared the same way, per DSO: distribution, transport,
metering, capacity and prosumer figures are set by the regulators, so a card
printing another figure than the rest is reported. Unlike the federal levies,
network figures are billed as each card prints them, so the report names the
cards whose households pay a figure the others do not.

## Sensors

All sensors share one device per config entry.

### Always created

| Sensor | Description |
| --- | --- |
| `current_price` | All-in EUR/kWh **now**. Attributes carry today's and tomorrow's slot-by-slot prices, the cheapest and most expensive 4-hour windows, the snapshot's publication month and age, and `card_read_by_ocr` while a card is being read off its pixels. See [docs/entities.md](./docs/entities.md). |
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
| `current_year_cost` | Running bill **since 1 January**, or since your contract start date if you tick that option. Every kWh is priced at the tariff that applied when you used it: past months bill on their own card where the supplier archives historical cards (Bolt fix / Cociter / DATS 24 / EBEM / Ecopower / Eneco / energie.be / Energy Knights / EnergyVision / Engie / Frank / Luminus / Mega / OCTA+ / Trevion), on the current one as a stand-in where it does not, dynamic contracts replay each hour's actual spot, and annual fees pro-rate across the year. Under the Walloon compensation regime injection nets against consumption and the energy term is floored at zero, so a value that stops moving while you keep injecting is that floor rather than a stalled sensor. Changed supplier during the year? Record the switch and each contract is billed on its own supplier's cards for its own days, listed in the `previous_contracts` attribute: see [Switching supplier during the year](#switching-supplier-during-the-year). Configured in the **Energy meters** step. Coverage and cost attributes (`hours_seen` / `hours_elapsed`, `days_seen` / `days_elapsed`, `capacity_ytd_eur`, `fees_ytd_eur` and the rest) say how complete the figure is — read them with [When the year-to-date looks too low](#when-the-year-to-date-looks-too-low), and see [docs/entities.md](./docs/entities.md) for the full list. |
| `current_month_cost` | The same bill as `current_year_cost` over the running month, which is the period a household budgets in and the one an invoice covers. Priced as its own window rather than sliced off the year, so under the Walloon compensation regime it nets **that month's** registers and twelve of these do not add up to the yearly figure; on every other regime they do. Resets on the 1st. See [docs/entities.md](./docs/entities.md). |
| `tomorrow_prices_available` | Binary sensor. ON when the price table covers at least one hour with tomorrow's local date **and** the supplier's published validity still covers tomorrow. Useful as a trigger for dynamic-tariff automations that should only fire after ENTSO-E publishes the next-day curve (~13:00 CET). For fixed/variable contracts it is ON throughout the month, but flips OFF on the last day of a month whose card stops at month-end, since next month's rates are not published yet. |
| `projected_year_cost` | Roughly what a year on this contract costs in EUR, priced once at today's tariffs against your own measured yearly volume. An indication for ranking contracts, not a forecast of your settlement: tariffs move and your usage will not repeat exactly. Unknown when the rate is a formula over an index that does not exist yet. See [docs/entities.md](./docs/entities.md). |

### Conditional

| Sensor | Created when | Description |
| --- | --- | --- |
| `capacity_cost` | Region = Flanders | Current monthly capacity cost in EUR (`billed_peak_kw × DSO_capacity_rate / 12`). `billed_peak_kw` is the mean of your last twelve monthly peaks, each floored at 2.5 kW first, which is what Fluvius charges on, so this stays steady through the year rather than tracking whichever month you are in. This charge also accrues into `current_year_cost`, so the two are consistent rather than the capacity term being invisible in the running bill. Carries `billed_peak_kw` and `months_counted` attributes; `months_counted` reaches 12 after a full year of history, and until then the mean covers only the months measured so far. |
| `monthly_peak_kw` | Region = Flanders | Running monthly peak power in kW (resets the 1st), reported as measured: the 2.5 kW regulated minimum is a billing rule and is applied to `capacity_cost` instead, so a quiet household now reads its true peak here rather than 2.5. State class is `MEASUREMENT` (mandated by HA for the POWER device class), so the long-term-statistics graph defaults to the **mean** aggregation. To see the true monthly peaks, switch the statistic-graph card to **Max** under Developer Tools → Statistics. A diagnostic **Reset monthly peak** button on the device page drops the rolling max so the next tick rebuilds it (use after a misconfigured sensor inflated the peak). |
| `prosumer_cost` | Compensation regime + `solar_kva > 0` | Monthly compensation fee in EUR (`solar_kva × (DSO_prosumer_rate + supplier_forfait) / 12`). Most suppliers bill only the regulated DSO rate; Cociter Variable, Mega and OCTA+ add a supplier-side PV forfait (already TVAC) on top. Only valid for Walloon installations certified before 2024-01-01; ends 2030-12-31. |
| `price_peak` / `price_offpeak` | Bi-hourly meter | The contract's **constant** day and night all-in rates, both readable at once. `current_price` follows the clock and holds whichever band applies now, which the Home Assistant energy dashboard cannot use: configuring a grid source with two tariffs asks for one price entity per tariff, permanently. These are those two. Unavailable on a contract with no constant band rate, which is every dynamic and time-of-use one, and on the Walloon *Tarif Impact*. |
| `injection_price_peak` / `injection_price_offpeak` | Bi-hourly or digital meter, injection regime | The same for the feed-in credit, for a card that prints a day and a night injection rate (Trevion Groene Energie Vast); unavailable on every other card, which prints none. Both or neither: a card carrying only one of the pair publishes no band sensors, since a day rate beside an unavailable night one reads as a broken sensor rather than as a card without the split. |
| `injection_price` | Injection regime | EUR/kWh paid for energy fed back to the grid. A card that indexes the credit per settlement slot follows the spot hour by hour; one that indexes it on the delivery month is resolved against that month's own mean; a flat card shows its printed figure. Without an ENTSO-E key a spot-indexed card falls back to the indicative its card prints, where it prints one. See [docs/entities.md](./docs/entities.md). |
| `contract_end_date` | A contract end date is set | Timestamp of your contract's end date (`device_class: timestamp`), so an automation can remind you to renew before it rolls over. Changes no billed rate. It does bound the projection: `projected_year_cost` reads it to report how much of the year today's contract still covers. Stays available even when a supplier fetch fails. |
| `potential_saving` | *Compare every supplier daily* is ticked | EUR a year the cheapest alternative would save you against your own contract, from the nightly ranking. Negative means nothing on the market beats what you have. Attributes carry the whole ranking, each row with its annual cost and, where the same months could be replayed for it, a year-to-date figure. Survives a restart; discarded when you change supplier. See [docs/entities.md](./docs/entities.md). |
| `ev_home_charging_rate` | *Publish the CREG home charging rate for a company car* is ticked | For a company car charged at home: the most an employer may reimburse per metered kWh, free of tax and social contributions, when it pays a flat rate (circular 2024/C/77), in EUR/kWh for your region and the running quarter. Computed from the CREG's monthly prices the way the SPF Finances does in the circular's quarterly addenda, and read once a quarter. It is a ceiling, not what every employer pays: an employer may pay less, must pay the lowest of the three regions if it ignores where its staff live, or may reimburse your actual cost instead. Attributes carry the quarter and every quarter the CREG's file covers, so last quarter's kWh can be settled at last quarter's rate. Not part of `current_price`. |

## Installation

### HACS (recommended)

[![Open this repository in HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=renaudallard&repository=homeassistant_be_electricity_prices&category=integration)

That button opens HACS on your own Home Assistant, already on this
integration: download it there, then restart Home Assistant and add it with

[![Add the integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=be_electricity_prices)

Both links go through [my.home-assistant.io](https://my.home-assistant.io),
which redirects to whatever address you use for Home Assistant; the first
time, it asks you for it once and remembers.

By hand, if you would rather: open HACS, search for **Belgian Electricity
Prices** — it ships in the HACS default store, so no custom repository is
needed — download it, restart, then **Settings → Devices & services → Add
integration → Belgian Electricity Prices**.

### Manual

Download the latest [release zip](https://github.com/renaudallard/homeassistant_be_electricity_prices/releases),
extract it under `<config>/custom_components/be_electricity_prices/`, and
restart Home Assistant.

`pypdf`, `pdfplumber`, `defusedxml` and `pyxlsb` are the only extra
runtime dependencies; Home Assistant installs them automatically from the
manifest.

## Configuration

The UI walks **up to twelve steps**, thirteen with the *Expert: custom formula*
supplier, depending on contract type and region. Apart from two paths no
EUR values are asked, since energy, DSO and tax rates all come from the
supplier's tariff card. The exceptions are the optional **signing-rate**
step, which appears when you set a contract start date or a tariff card
month and lets you type the rate and yearly fee you actually signed, and the **Expert: custom
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
5. **Direct debit** *(only where the card prices it)* — whether you pay your
   supplier by direct debit. Some cards charge a lower yearly standing
   charge when the invoice is settled that way: Brusol Groene stroom is
   250 € a year and 230 € on domiciliëring. On seventeen Mega cards it is
   the first-year ristourne that depends on it instead, either as a larger
   credit (42,40 € more on Cosy Fixed) or, on Cosy Flex, Smart Fixed and
   both their pro twins, as the whole credit, which those cards grant to a
   direct-debit payer and to nobody else. The amounts are read off the card,
   not typed, and nothing else on the bill changes. The box is hidden on
   every contract whose card grants none, and an answer given on one
   contract is dropped when you switch to another, so it cannot come back
   into force later.
6. **DSO billing mode** *(Wallonia only, and skipped for the three contracts sold on the CWaPE bands — Cociter Tarif Variable Trihoraire, Mega Off-peak Impact and OCTA+ Fixed Impact, which are locked to Tarif Impact)* — *Simple* / *Bi-horaire* / *Tarif Impact*. Tarif Impact uses the CWaPE 3-band hour-of-day rates and
   requires a smart meter; Simple and Bi-horaire follow the existing
   meter convention.
7. **ENTSO-E API key** *(dynamic and monthly-indexed contracts, both of
   which price the commodity off spot; also offered, skippable, to every
   contract whose energy is indexed on the delivery month's mean and whose
   card prints last month's figure, on any solar regime — Cociter Variable
   and Trihoraire, Engie's EPEXDAM cards, Luminus MaxxFlex and SmartFlex,
   OCTA+ Smart Variable, Flux and Eco Flux, Eneco Flex and Flex One, EBEM
   Groen Variabel and B@sic+, TotalEnergies Electricité Variable, Impact,
   myComfort, myDrive and myEssential, every Mega Flex and Off-peak Impact
   card — and on
   the injection regime for a contract whose injection is itself
   index-linked, which is most static cards and not the handful it once was
   — every Bolt card and both Cociter variable cards index it per hour, while
   energie.be Vast and most of the rest index it on a monthly mean)* —
   validated against the real ENTSO-E endpoint at
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
8. **Capacity tariff peak source** *(Flanders only)* — a sensor, or a fixed
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
9. **Connection power** *(Brussels only)* — the contractual connection power
   tier (≤ 1.44 / 1.44-6 / 6-9.6 / 9.6-13 / 13-18 / 18-36 / 36-56 / > 56 kVA).
   Brussels bills a Brugel OSP (Obligations de Service Public) annual fee
   scaled by this tier, and a connection above 13 kVA is billed Sibelga's
   own higher power term in place of the data-management charge; existing
   entries default to the 1.44-6 kVA tier.
10. **Solar panels** — inverter capacity in kVA + the regime that applies:
   - **No solar panels** *(default)* — no extra sensors.
   - **Compensation regime** — Wallonia only, installations **certified before
     2024-01-01**, valid until 2030-12-31. Creates `prosumer_cost`.
   - **Injection tariff** — post-2024 Walloon installations and Flemish smart
     meters. Creates `injection_price`, ready for HA Energy.
11. **Energy meters** *(optional, all four / two fields are skippable)* —
   feeds the `current_year_cost` sensor. Whichever way you wire it, every
   field wants a **cumulative** kWh reading, one that only ever climbs.
   A sensor that resets, such as a "this year" or "this month" total,
   will not work: the integration bills the day-to-day *change* in the
   reading, and a reset reads as a large negative day. A net register that
   counts down while you export (a `utility_meter` with `net_consumption`,
   a bidirectional meter) is not supported either: a fall reads as zero,
   so wire consumption and injection as two separate climbing sensors.
   If you fill both wirings for the same side, the day/night registers
   win and the totals field on that side is ignored. Two ways to wire it:
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
Energy Knights Essentia Online and Essentia Online Green, EnergyVision's
1.800 kWh vast, vaste injectieprijs 3 jaar, Laadpunt and Groene stroom,
Trevion Groene Stroom
Flex and LifePowr, and the custom supplier's monthly-average formula), which is where the setup flow asks
for it as a mandatory, validated field.
It is optional everywhere else, but two features use it when present: an
injection tariff that is itself index-linked — the hourly-spot shape
(Cociter Variable and Variable Trihoraire, every Bolt fixed and variable
card) and the monthly-mean shape (energie.be Vast on Belpex_SPP, and most
other static cards), 65 contracts across 14 suppliers between them, and the
re-price of a month-indexed contract on the delivery month's own mean, cohort
or not, for which the flow offers the key on every solar regime (Cociter
Variable and Trihoraire, Engie's EPEXDAM cards, Luminus MaxxFlex and
SmartFlex, OCTA+ Smart Variable, Flux and Eco Flux, Eneco Flex and Flex
One, EBEM Groen Variabel and B@sic+, every Mega Flex and Off-peak Impact
card). Both stay off without a key rather than failing the entry — the
injection price goes unavailable, and the re-price keeps the card's printed
figure, which is the previous month's. The token is free but ENTSO-E does not auto-grant it —
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
the entry's options to clear it. The key step shows on every edit of a
contract that reads a key, with the stored token filled in.

### Reconfiguring later

**Settings → Devices & services → Belgian Electricity Prices → Configure**
opens a four-option menu:

- **Edit settings** — walks the same chain of steps, pre-filled with the
  current values. Change supplier, contract, region, DSO, meter, DSO
  billing mode, ENTSO-E API key, capacity peak source, or solar
  parameters — anything. The integration reloads automatically when you
  finish, picking the new tariff card on the next refresh. A sensor your new
  settings no longer create (the EV rate after unticking its box, the band
  sensors after leaving a bi-hourly meter, the capacity sensors after leaving
  Flanders) is removed rather than left unavailable; its history stays.
- **Record a supplier switch** — for a household that changed supplier
  during the year. Asks for the first day of the new contract, keeps your
  current settings as the contract you held until the day before, then walks
  the edit steps for the new one. See
  [Switching supplier during the year](#switching-supplier-during-the-year).
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
  **Each row is priced on the index its own card names.** Several Flemish cards
  settle on a monthly Belpex weighted by the residential load profile, and they
  do not all weight it the same way: Eneco averages the three regional curves,
  energie.be weights every DSO column, Energy Knights and Trevion read the
  Flemish curve alone. Those are three different indices, about 2 €/MWh apart,
  and all three can sit in one table, so the ranking resolves each card against
  its own rather than against yours.
  The sweep is **bounded by a clock, not a timeout** — a tariff card cannot be
  parsed halfway and abandoned — so it fetches the cheapest cards first, and
  **the table is on screen while it fills**, rows appearing and reordering as
  each card lands. A card that would not fit in the time left is skipped rather
  than started, so the page stops cleanly instead of freezing for most of a
  minute on one slow supplier; reopening finishes the rest from what it already
  downloaded. On a Raspberry Pi the 50-candidate Flanders static cell prices
  about 15 rows in the first ten seconds, 34 in the first minute and 39 within
  the two-minute budget, the tail being Bolt and TotalEnergies at 13 to 45
  seconds a card. Where two products share one tariff card the sweep reads it
  once and reuses the text, which matters most on Bolt: each of its four
  variable cards is sold on both settlements, and the parse, not the download,
  is what the budget is spent on.
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
  makes for you. It is cheap once running: thirteen of the seventeen suppliers
  publish a freshness check, including the two slowest cards, so a day on which
  nothing was republished costs a handful of conditional requests rather than
  the minutes of fetching a cold sweep costs, and tariff cards move about
  monthly. A
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

### Switching supplier during the year

An entry prices one contract at a time, so a household that changes supplier
during the year records the switch: **Configure → Record a supplier switch**,
then the first day the new supplier supplied, as on its welcome letter or on
the old supplier's final bill. Your current settings are kept as the contract
you held until the day before, and the edit steps that follow set up the new
one, starting from what you have now. From then on:

- `current_year_cost` is the bill across both contracts: the old one priced on
  its own supplier's cards for its own days (its own archive, or the project's
  card archive, month by month), the new one from its first day. The
  `previous_contracts` attribute lists each earlier contract with its dates
  and cost, and `previous_contracts_eur` their total.
- Under the Walloon compensation regime each contract nets its own injection,
  as the regulator rules for a switch during the year (CWaPE communication
  CD-14d03, section 5.1.2): a surplus banked before the switch does not offset
  consumption after it.
- The earlier contracts are priced once a day in the background, because that
  means fetching the old supplier's cards. After you record a switch,
  `current_year_cost` reads unknown until that pricing lands, usually within
  minutes, rather than a year missing a whole contract. It stays unknown while
  an earlier contract cannot be priced, and is tried again every hour: a
  contract the integration can never price (a supplier it no longer knows, or
  a card that does not cover your network operator) keeps it unknown, and the
  log names the contract.
- `current_month_cost` includes the old contract's days in the month of the
  switch, the comparison pages price *your contract* the same way, and the
  statistics backfill prices each hour on the contract that supplied it.
- A supplier that can no longer be reached, and whose cards no archive kept,
  has its days priced on your current card, and the contract's row in
  `previous_contracts` says so (`priced_on_current_card`).

Record a switch as soon as you can. Until then the new contract's days are
priced as the old contract's, and recording it later corrects the figure,
which the long-term statistics show as one step on the day you record it. A
second switch in the same year is recorded the same way. Recording a switch
unticks **Bill the year-to-date from the contract start date**; ticking it
again leaves the earlier contracts out of the year.

## Daily operation

### Refresh cadence

- **Supplier snapshot** — the coordinator runs a cheap `probe()` every
  hour and only re-fetches the full PDF when the probe key changes
  (see *How often the integration polls* above). Suppliers without a
  probe (DATS 24, energie.be, Engie, Luminus) fall back to a 24 h time-based TTL.
  Multiple entries pointing at the same
  `(supplier, contract, region)` tuple share their fetched snapshot
  through an in-memory cache, so the same PDF is never polled twice.
- **Spot prices** *(dynamic and monthly-indexed contracts)* — fetched from
  ENTSO-E at hourly resolution, or at the native 15-minute resolution for
  suppliers that bill per quarter-hour. Tomorrow's curve is picked up on the
  first tick after publication, around 13:00 CET. Past hours are backfilled
  lazily into a per-entry cache on the grid the contract settles on, so
  `current_year_cost` replays each one at its actual rate without refetching
  the same window every tick; a window neither source could answer is retried
  three hours later rather than on the next tick, and the missing hours
  forfeit only their energy term until it fills. See
  [docs/data-sources.md](./docs/data-sources.md).
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
TimeoutError` rather than trailing off after the colon. Sixteen repair issues surface
under **Settings → System → Repairs** so problems are visible without
inspecting attributes; the fetch-related ones auto-clear on the next
successful refresh:

- **`snapshot_stale_<entry>`** — the cached snapshot is older than **7
  days**. Not raised once the supplier has left the market (its
  `deprecated_until` has passed): the final card stays stale for good, the
  deprecation notice below already says so, and the entry stops asking the
  supplier for a card that is gone.
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
  asks you to check the letter your supplier sends. Naming the successor is
  not a promise that your product is in its list: a withdrawal announces a
  supplier, not the product each customer is moved to, and DATS 24's Flemish
  customers were moved to one EnergyVision publishes no card for. So the card
  tells you to pick the product named on your letter, and what to do when it
  is not there: the **Expert: custom formula** supplier prices it from your
  own card in the meantime.
- **`extractor_unreadable_<entry>`** — the card downloaded fine but its
  pages carry no text layer, so no parser change here can read it (Ecofix
  since the August 2026 card). Cached prices keep serving, and it clears
  by itself the moment the supplier publishes a readable card.
- **`extractor_unreadable_no_prices_<entry>`** — the same unreadable card
  on an entry with no cached one to stand in: a brand-new entry, or one
  whose cache predates the card-as-parsed change. Every sensor on it reads
  unavailable until the supplier publishes a readable card, so the card
  points at the Custom (expert) supplier rather than warning about drift.
- **`card_read_by_ocr_<entry>`** — the same unreadable card, being priced
  anyway off the reading this project's daily card archive makes of its
  pixels. Not a failure and not a drift warning: the figures are the ones
  the engine read whole or did not read at all. It says so because a price
  read off a picture of a card is worth checking against your own, and it
  clears by itself the moment the supplier publishes a readable card.
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
- **`prosumer_tariff_missing_<entry>`** — the entry is a Walloon
  compensation install but its card omits the DSO prosumer tariff, so only
  the supplier's own PV forfait is billed and the network half is left out
  rather than borrowed from another supplier's card.
- **`compensation_kva_missing_<entry>`** — the entry is on the Walloon
  compensation regime with an inverter capacity of 0, so no prosumer fee
  is billed at all. The solar step refuses that now; an entry saved before
  it gets this notice until the capacity is filled in.
- **`direct_debit_unanswered_<entry>`** — the card prices a direct-debit
  payer differently and this entry has no stored answer, which an entry
  created before the question existed does not. The credit is left out
  rather than guessed, so answer it in the options to have it billed.
- **`brussels_power_term_missing_<entry>`** — the card prints only the
  metering half of Sibelga's fixed charge and Brugel's sheet, which supplies
  the power half, could not be read. About 50 EUR a year is left out rather
  than guessed; the sheet is retried every six hours.
- **`register_pair_incomplete_<entry>`** — one register of a day/night pair
  records nothing, or stopped while its twin carries on (a rename, an
  integration swap, a meter replacement). The pair is billed only on the
  days both registers report, so the running cost reads low until it is
  rewired; the card names the sensor. A register that merely started late
  is not reported, since it records to date.

  The first five of those eight are not failures either: each clears when the
  supplier prints the missing row again, the direct-debit one clears as soon
  as the question is answered, the Brussels one as soon as Brugel's sheet
  can be read, and the register one as soon as both halves report again.

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
| `duration_hours` | _required_ | Window length in whole hours (1-48). On a 15-minute contract (Cociter / EBEM / Ecofix / Ecopower Dynamische Burgerstroom / energie.be / Energy Knights Agilior Online / EnergyVision / Engie / OCTA+ / Trevion, and Bolt or Frank Energie with the quarter-hour box ticked) the window aligns to quarter-hour boundaries. |
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
predates the entry's first live update tick. On an entry that recorded
a supplier switch, each hour is priced on the contract that supplied it,
and the running bill carries on across the switch.

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
land. The backfill lets Home Assistant carry on between each day of
hours, so the rest of the system stays responsive while it runs.
Response is a `{rows_written, sensors, range}` object you can
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
rather than quietly billing what it was given. Four messages are worth
searching for:

- **"negative change"** — Home Assistant restarts the running total behind
  a sensor after an outage longer than `purge_keep_days`, and the restart
  lands as one large negative hour. Adding that up cancels real energy
  elsewhere in the year: a meter that moved 4687 kWh was billed for 942.
  Those hours are now ignored and the sensor is named. The figure corrects
  itself on the next refresh, since the year is recomputed from scratch
  each time.
- **"returned no statistics"** — one half of a day/night register pair is
  wired but produces nothing, so the pair cannot be billed and the running
  cost falls to the fixed fees, unless a totals sensor is wired on the same
  side, which is then billed instead. A sensor with `device_class: energy` but
  no `state_class` compiles no long-term statistics at all, and neither does
  `state_class: measurement`; both look perfectly normal in the UI.
- **"has diverged"** — both halves of the pair report, but not on the same
  days: one stopped (a rename, an integration swap, a meter replacement) or
  started late. Only the days both report are billed, feed-in included, and
  `days_seen` (or `hours_seen`) says how many, rather than the surviving band
  being billed alone as though the other used nothing. A totals sensor wired
  on the same side is billed instead, since it covers both bands on every
  day.
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
every Bolt fixed and variable card, Engie Empower Flextime, and Trevion Groene
Energie Vast on a bi-hourly or dynamic meter); a flat or
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
OCTA+, Trevion, and TotalEnergies in Wallonia and Brussels), falling back to the
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
pip install -r requirements-dev.txt
ruff check .
ruff format --check .
mypy --strict custom_components/be_electricity_prices
pytest tests/
python scripts/live_check.py    # hits real supplier endpoints
python scripts/archive_cards.py --out tmp/archive   # stores today's cards there
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
runs three phases against the live supplier endpoints, taking the text of
any card the cards repository already holds from there so only a card that
changed since the morning's archive walk is rendered again:

- **Extractor phase** — every (contract, region) tuple is fetched and
  parsed; each fetch retries transient network errors up to three times,
  and the CI workflow re-runs the whole check up to seven times with
  escalating backoff. Only a check that fails in *every* one of those
  runs opens or updates a GitHub issue titled
  `[live-check] supplier extractor broken …`, so a slow runner timing
  out on a different supplier each time stays quiet, and a supplier that
  stays broken is commented on once a week rather than once a day.
- **Catalog phase** — the `discover()` of every supplier that implements one
  (all but energie.be, Trevion and the expert custom supplier) is run against its
  public listing page; any product visible at the supplier but missing
  from the registry opens a separate issue
  `[live-check] new supplier products detected …` so a parser regression
  and a catalogue addition stay in distinct threads. A discovery that
  sees no product at all files in the same thread, as
  `[live-check] supplier product discovery failed …`, since it means that
  supplier's new products would go unseen.
- **Freshness phase** — for the eight supplier-families that pick a card
  from several advertised ones, the card actually resolved is compared
  against the newest one the supplier advertises. A superseded card still
  downloads and still parses, so without this a stale URL looks identical
  to a healthy run. Suppliers that construct a single URL per contract are
  excluded: there is no candidate set to choose wrongly from, so a bad
  resolution fails loudly on its own.

### The card archive

Most suppliers keep their past cards somewhere the integration can fetch
them from, which is what the year-to-date cost and the signing-cohort
pricing walk (the suppliers listed under `current_year_cost` in
[Sensors](#sensors)). Some do not
(TotalEnergies, Ecofix), and some archive only part of their range (Bolt's
variable cards are named by version rather than by month, Frank skips a
month now and then), so a past month on those was billed on today's card.

A second daily workflow
([`.github/workflows/archive_cards.yml`](./.github/workflows/archive_cards.yml),
running [`scripts/archive_cards.py`](./scripts/archive_cards.py)) closes that
gap: it has run every morning since September 2026. It fetches every
registered (supplier, contract, region) card exactly as the integration
would, and commits what it parsed to
[`be_price_cards`](https://github.com/renaudallard/be_price_cards), a
repository shared with be_water_prices in which this integration owns the
`electricity/` directory, as
`electricity/cards/<supplier>/<contract>/<region>/<YYYY-MM>.json`, with the
text of every page or document that parse read under
`electricity/texts/<YYYY-MM>/`, so a card can be re-read or checked by hand
later. A manual run of the same workflow can also mirror past months from
the supplier archives into it, which keeps them readable should a supplier
drop its own. That only covers suppliers walked while they were still
publishing: DATS 24 left the market before this archive existed, so none of
its months are in here and they are read from its own server for as long as
that lasts.

**Finding a stored card by hand.** Everything is addressed by the same
three ids the integration uses, which are the directory names under
`electricity/cards/`: the supplier (`totalenergies`, `bolt`, ...), the contract
(`totalenergies_electricite_fixe`, `bolt_fix`, ...) and the region
(`flanders`, `wallonia`, `brussels`). Browse the repository to see them.

1. **The parsed card** is one JSON per month at
   `electricity/cards/<supplier>/<contract>/<region>/<YYYY-MM>.json`, for example
   [`totalenergies/totalenergies_electricite_fixe/wallonia/2026-09.json`](https://github.com/renaudallard/be_price_cards/blob/main/electricity/cards/totalenergies/totalenergies_electricite_fixe/wallonia/2026-09.json).
   It holds the energy, DSO, tax and injection figures exactly as the
   integration stores them, plus `_seen_on` (the day it was captured),
   `_via` (`live` for a card captured while it was current, `archive`
   for one mirrored from the supplier's archive) and `_sources`: every
   page or document the parse read, each with its text file under
   `texts/` and, for a PDF, the digest of the file.
2. **The original PDF** is easiest through
   [`coverage.md`](https://github.com/renaudallard/be_price_cards/blob/main/electricity/coverage.md)
   in that repository, which names one sheet per supplier under
   `coverage/`: a row per contract and region, a column per month. Each
   month cell carries two links: `pdf`
   downloads the card from the cards repository's releases (`page` opens
   the text of the page instead, for a card parsed from a page) and
   `json` opens the parsed card above; a month marked `(mirror)` was
   copied from the supplier's own archive. The same sheets are published
   under
   [`electricity/`](https://github.com/renaudallard/be_price_cards/tree/main/electricity)
   in the cards repository itself, and each release's notes point there,
   so a file seen on the releases page can be named too: search that
   repository for the file's name. Behind them is `pdfs.json`, which maps a digest to
   `electricity-<YYYY-MM>/<digest>.pdf` in those releases; the digest in a
   JSON's `_sources` is the same key.
3. **The text the parser read** is under `texts/<YYYY-MM>/`, named by the
   digest of the text itself and listed in the JSON's `_sources`, for
   checking a figure against the card without opening the PDF. A card is filed under the month its
own label names, which is what a supplier publishing in arrears (Ecopower's
definitive card) or ahead needs; a month is rewritten only when the parse
changed, and months older than three years are dropped.

The cards themselves are kept as well, as the real thing a parser can be
re-run against later: every PDF the archive has not seen before is uploaded
to an `electricity-<YYYY-MM>` release of that same repository. A release
holds the cards for one month, whatever day each was captured or mirrored
on, about two hundred files named by their SHA-256. Each stored card names
its PDF by that digest under `_sources`, and `electricity/pdfs.json` says
which release holds it. A month of cards is about 100 MB, which is why they
live in releases rather than in the tree. That digest also keeps the daily
run cheap: a card whose bytes have not changed is served the text the
archive already holds instead of being rendered again. And a parser fix
reaches the stored months on its own: when the parser sources change, the
next run replays every stored month from the texts it kept, with the clock
set to the day the card was captured and no supplier contacted, and
rewrites what came out differently; a parser that now needs the card read
another way gets the kept PDF back.

The integration reads that archive first for any closed month, one small
JSON per month straight from `raw.githubusercontent.com` against a PDF
download and a parse from the supplier; the supplier's own archive answers
for a month the project's does not hold, and the current card stands in when
neither has it. The request names the supplier, contract, region and month
and nothing else, and it is only made for a month the archive can hold: a
closed one, and for a supplier with no archive of its own not before August
2026, the earliest month the daily captures reach.
The *Read past cards from the project's archive* box on the meters step,
on by default, switches it off per entry: the integration then never
contacts GitHub, and those months are priced on the current card. A row
holds what the extractor of that day parsed, and the day after a parser
change every row is re-parsed from the texts the archive kept, so a fix
reaches past months within a day.

It is not a complete record. A stored month carries no PDF bytes of its own,
only the digest of the release file that holds the card; a supplier that
blocks the GitHub runners for a day (Mega has, the live check's timeouts
show) just misses that day's capture; and a card no parser can read
(Ecofix's page images) is read by an OCR engine built for those cards and
stored as an ordinary row, flagged so an entry served one says where the
figures came from. One that even the OCR refuses is kept as a PDF and named
on the coverage sheet with no JSON beside it.

## License

BSD 2-Clause. See [LICENSE](./LICENSE).

# Provider framework

This document is the reference for the per-supplier extractor layer under
`custom_components/be_electricity_prices/providers/`. It covers the extractor
protocol (`SupplierExtractor` and its three callable contracts), every shared
dataclass that a snapshot is built from, the registry that the coordinator and
config flow read, and the shared PDF toolkit (`providers/_pdf.py`) that every
card-parsing provider builds on. The guiding invariant of the whole layer is
that no EUR value lives in Python source: every number in a `SupplierSnapshot`
comes from a live fetch of the supplier's own published tariff card
(`providers/base.py:38`).

Related docs:

- [architecture.md](architecture.md): where this layer sits in the whole integration.
- [coordinator.md](coordinator.md): who calls `fetch` / `probe` / `fetch_for_month`, and when.
- [pricing-model.md](pricing-model.md): how `compute_breakdown` consumes the dataclasses documented here.
- [config-flow.md](config-flow.md): how contracts and regions drive the setup wizard.
- [ci-and-testing.md](ci-and-testing.md): how `scripts/live_check.py` exercises every extractor weekly.
- [glossary.md](glossary.md): Belgian-energy and HA terms used throughout.
- The per-supplier docs under [providers/](providers/) each document one concrete extractor.

## Layer overview

Each supplier is a self-contained module (for example `providers/bolt.py`) that
exposes exactly one top-level name, `EXTRACTOR`, of type `SupplierExtractor`
(`providers/base.py:568`, `SupplierProtocol`). The module's job is to turn the
supplier's live publication (a PDF card, an HTML listing, or a small API) into a
`SupplierSnapshot`: the energy formula plus a network/tax/capacity overlay for
every DSO sub-area the supplier operates in. The coordinator then picks the one
DSO the user configured and hands the snapshot to `pricing.compute_breakdown`.

```
        supplier publication (PDF / HTML / API)
                     |
                     v
   providers/<supplier>.py   EXTRACTOR.fetch / probe / fetch_for_month
                     |  (uses providers/_pdf.py helpers)
                     v
             SupplierSnapshot  ── energy: EnergyRates
                               ── dsos:   {dso_key: DsoOverlay}
                               ── taxes:  TaxOverlay
                               ── injection: InjectionRates | None
                     |
                     v
   coordinator picks user's DSO + meter  ->  pricing.compute_breakdown
```

The registry (`providers/__init__.py`) is the only place that names all
suppliers; `get()` and `all_extractors()` are the two lookups the rest of the
integration uses.

## The extractor protocol

### SupplierExtractor

The registry entry for one supplier (`providers/base.py:531`). It is a frozen,
keyword-only dataclass.

```python
@dataclass(frozen=True, kw_only=True)
class SupplierExtractor:
    id: str
    label: str
    contracts: tuple[Contract, ...]
    fetch: SnapshotFetcher
    probe: SnapshotProbe | None = None
    fetch_for_month: ArchivedSnapshotFetcher | None = None
    def regions(self) -> frozenset[str]: ...
```

| Field | Type | Meaning |
| --- | --- | --- |
| `id` | `str` | Stable machine key. Stored verbatim in config entries and used as the `EXTRACTORS` dict key, so it must never change once shipped. |
| `label` | `str` | Human-facing supplier name shown in the config flow. |
| `contracts` | `tuple[Contract, ...]` | Every product this supplier sells; drives the contract-selection step. |
| `fetch` | `SnapshotFetcher` | Mandatory. Fetches and parses the current card into a `SupplierSnapshot`. |
| `probe` | `SnapshotProbe \| None` | Optional cheap freshness check; `None` means "no probe, use TTL only". |
| `fetch_for_month` | `ArchivedSnapshotFetcher \| None` | Optional historical fetch for time-correct yearly-cost billing; `None` means "no archive". |
| `deprecated_until` | `date \| None` | Set when the supplier has announced it is leaving the residential market: the date its contracts stop being supplied. Drops the supplier from the config flow's new-setup and compare pickers, and raises the `supplier_deprecated` Repairs card on every entry using it. Purely declarative -- nothing compares it to the clock, so hiding takes effect as soon as the flag ships (you cannot sign up today for a contract being transferred away), and the date is text for the card. |
| `deprecated_successor` | `str \| None` | Registry id of the supplier taking the contracts over; named in the Repairs card so the user knows what to switch to. It is only named when it has a contract in the entry's own region: a withdrawal names one successor nationally, while our coverage is per region, so an entry we cannot route anywhere gets the `supplier_deprecated_no_successor` variant instead of advice the config flow would refuse. |

A withdrawn supplier keeps working: `fetch` and `probe` are untouched, and
`providers.get()` still resolves it, so existing entries carry on pricing off the
supplier's card until it stops publishing. Only the pickers and the Repairs card
react. Do NOT filter withdrawn suppliers out of `EXTRACTORS` or
`all_extractors()` -- that would also hide them from the live-check's registry
diff and from every entry that still needs to price.

`regions()` (`providers/base.py:560`) returns the union of `Contract.regions`
across all this supplier's contracts. The config flow uses it to decide whether
a supplier should be offered for the region the user picked.

### The three callable contracts

The three callables share the first three positional arguments: an
`aiohttp.ClientSession`, plus two `str` arguments. By convention (used across
the concrete providers and the coordinator) the two strings are the user's
selected contract id and region, letting one extractor serve products that
differ per region. All three are defined as bare `Callable` type aliases so a
provider can implement them as plain module-level `async def` functions and
assign them to the dataclass fields.

#### SnapshotFetcher

```python
SnapshotFetcher = Callable[
    [aiohttp.ClientSession, str, str], Awaitable[SupplierSnapshot]
]
```

Defined at `providers/base.py:509`. The mandatory current-card fetch. It must
return a fully populated `SupplierSnapshot` or raise `ExtractorError`
(`providers/base.py:574`) on any fetch or parse failure. It never returns
`None`: a missing current card is an error, not an absence.

#### SnapshotProbe

```python
SnapshotProbe = Callable[
    [aiohttp.ClientSession, str, str], Awaitable[str | None]
]
```

Defined at `providers/base.py:517`. A cheap freshness key. The coordinator calls
it hourly and only re-runs `fetch` when the returned key changes from the cached
one. Semantics of the return value:

- A stable string that stays the same across calls means the cached snapshot is
  still valid (skip the expensive `fetch`).
- A different string means the card changed; refetch.
- `None` means the supplier has no probe path the coordinator can rely on
  (for example Engie/Luminus API endpoints, or DATS 24's one PDF per month,
  replaced in place within the month). The coordinator then falls back to its
  time-based TTL.

Most PDF providers implement this by delegating to `head_freshness_key()` (see
[_pdf.py helpers](#the-pdf-toolkit-providers_pdfpy) below).

#### ArchivedSnapshotFetcher

```python
ArchivedSnapshotFetcher = Callable[
    [aiohttp.ClientSession, str, str, "date"], Awaitable["SupplierSnapshot | None"]
]
```

Defined at `providers/base.py:525`. Fetches the card that was published for a
specific `(year, month)` (passed as a `datetime.date`), so the yearly-cost flow
can bill each past month at its own historical rate rather than proxying every
month at the current rate. Return-value semantics:

- A `SupplierSnapshot` for the requested month when the archive resolves.
- `None` when the supplier has no accessible archive for that month. This
  applies to overwrite-in-place suppliers (OCTA+, TotalEnergies), API-only
  suppliers (Engie, Luminus, DATS 24), and any month before the supplier's
  archive horizon. On `None` the coordinator falls back to the current snapshot
  as a proxy.

An extractor whose `fetch_for_month` field is itself `None` means the supplier
has no archive at all. Providers that do implement it typically cross-check the
resolved card with `archive_validity_check()` (see below) so a CDN that silently
substitutes the current card for a withdrawn archive URL does not mis-bill past
months.

## Contract and rate dataclasses

A `SupplierSnapshot.energy` is one of six `EnergyRates` variants
(`providers/base.py:257`) chosen by the contract's `kind`. All rate dataclasses
are `frozen=True, kw_only=True`. EUR values are always populated from a live
fetch, never hardcoded — the one exception is the expert **custom** supplier
(`providers/custom.py`), whose snapshot is built from the config entry the user
filled in rather than a scraped card.

### Contract

`providers/base.py:61`. One product sold by a supplier.

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `id` | `str` | required | Stable contract key, stored in the config entry. |
| `label` | `str` | required | Human-facing product name. |
| `kind` | `TariffKind` | required | One of `"fixed"`, `"variable"`, `"dynamic"`, `"tou"`, `"tou_impact"`, `"spot_monthly"` (`providers/base.py:53`). Selects which `EnergyRates` variant the snapshot carries. |
| `regions` | `frozenset[str]` | all three | Regions the product is actually published in. Defaults to `{flanders, wallonia, brussels}`; extractors override per-contract for products that 404 outside their home region (for example TotalEnergies Impact is Wallonia-only). |
| `spot_indexed_injection` | `bool` | `False` | `True` when a non-dynamic product's injection is a per-hour spot formula with no printed monthly indicative (Cociter Variable). Pricing the injection then needs an ENTSO-E spot even though the energy side is variable, so the config flow offers the API-key step on the injection regime. Dynamic contracts already collect the key via their energy formula and leave this `False`. |

`spot_indexed_injection` is a load-bearing invariant: shape (c) injection (only
Cociter Variable today) must have the spot wired through the live, backfill, and
compare paths, all gated on this flag, or the injection credit drifts.

### FixedRates

`providers/base.py:81`. Fixed energy contract: constant EUR/kWh, optionally
bi-hourly.

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `single` | `float` | required | Single (mono) meter rate. |
| `peak` | `float \| None` | `None` | Bi-hourly peak rate; `None` falls back to `single`. |
| `offpeak` | `float \| None` | `None` | Bi-hourly off-peak rate; `None` falls back to `single`. |
| `exclusive_night` | `float \| None` | `None` | Rate for a dedicated night-circuit meter. The engine routes the `exclusive_night` meter type through it, falling back to `single` when unpublished. |
| `yearly_fixed_fee` | `float` | `0.0` | Yearly standing charge. |
| `yearly_fixed_fee_exclusive_night` | `float \| None` | `None` | Dedicated yearly fee billed instead of `yearly_fixed_fee` on an exclusive-night entry when the card prints a separate one; `None` means the standard fee applies to every meter type. |

### VariableRates

`providers/base.py:103`. Variable energy contract: the current month's effective
EUR/kWh, re-published monthly.

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `current` | `float` | required | This month's single-meter effective rate; the fallback for every meter type. |
| `peak` | `float \| None` | `None` | Per-meter indicative peak rate (suppliers like Cociter publish these); `None` falls back to `current`. |
| `offpeak` | `float \| None` | `None` | Per-meter indicative off-peak rate; `None` falls back to `current`. |
| `exclusive_night` | `float \| None` | `None` | Night-circuit rate; falls back to `current`. Pairs with `DsoOverlay.distribution_exclusive_night`. |
| `yearly_fixed_fee` | `float` | `0.0` | Yearly standing charge. |
| `yearly_fixed_fee_exclusive_night` | `float \| None` | `None` | Dedicated exclusive-night yearly fee (EBEM Groen Variabel prints one); `None` means the standard fee applies. |
| `formula` | `str \| None` | `None` | Indexation expression text for diagnostics, when published. |

### DynamicRates

`providers/base.py:140`. Dynamic energy contract: `factor * spot + base` per
price slot, against the ENTSO-E BE day-ahead spot.

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `factor` | `float` | required | Multiplier on the spot price. |
| `base` | `float` | required | Additive term (EUR/kWh). |
| `yearly_fixed_fee` | `float` | `0.0` | Yearly standing charge. |
| `quarter_hourly` | `bool` | `False` | Selects the spot billing grid. `True` keeps ENTSO-E's native 15-minute slots; `False` aggregates to clock hours. |

`quarter_hourly` reflects a real billing-grid difference between suppliers.
Frank Energie (by default), Luminus, Mega, TotalEnergies and Eneco price per
clock hour, so the integration aggregates the 15-minute day-ahead curve to
hourly and these leave the flag `False`. Engie, Cociter, EBEM, Ecofix, OCTA+,
Ecopower (Dynamische Burgerstroom), Bolt (Dynamisch), energie.be and EnergyVision bill per quarter-hour (their cards multiply
the 15-minute Belpex / eSpot_15 / Epex 15 / EPEX DA spot) and set it `True`;
that keeps the live price table, current/next-slot sensors and cheapest-window
service on native 15-minute slots. Year-to-date billing stays hourly regardless,
because HA only retains hourly long-term statistics (`providers/base.py:152`).

### TimeOfUseRates and WeekendRule

`providers/base.py:194`. Time-of-use energy contract: three slots by hour-of-day
(`kind = "tou"`). Requires an SMR3 smart meter.

The weekday schedule is shared across products:

```
peak       : 07:00-11:00 + 17:00-22:00
transition : 11:00-17:00 + 22:00-01:00
offpeak    : 01:00-07:00
```

`weekend_rule` (`WeekendRule`, `providers/base.py:199`) selects the weekend
schedule:

- `weekend_offpeak` (generic CWaPE default): Saturday, Sunday and public holidays are entirely off-peak.
- `weekend_no_peak` (Engie Empower Flextime): peak never applies; transition is
  07:00-11:00 + 17:00-01:00; off-peak is 01:00-07:00 + 11:00-17:00.
- `smartflex_seasonal` (Luminus SmartFlex): seasonal bands applied every day; the
  11:00-17:00 midday window is off-peak in spring/summer (21/03-20/09) and
  transition otherwise. See `pricing.tou_slot`.

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `peak` | `float` | required | Peak-slot rate. |
| `transition` | `float` | required | Transition-slot rate. |
| `offpeak` | `float` | required | Off-peak-slot rate. |
| `yearly_fixed_fee` | `float` | `0.0` | Yearly standing charge. |
| `formula` | `str \| None` | `None` | Indexation expression, when the supplier publishes one (rates can be re-published monthly). |
| `weekend_rule` | `WeekendRule` | `"weekend_offpeak"` | Which weekend schedule applies. |

### ImpactRates

`providers/base.py:232`. Wallonia Tarif Impact energy contract: three slots on
CWaPE bands (`kind = "tou_impact"`). Distinct from `TimeOfUseRates` because the
schedule is the CWaPE-defined Impact one (every day, no weekend exception),
matching the DSO Impact tariff that gates eligibility. Requires an SMR3
quarter-hourly meter and an opt-in to the DSO Impact tariff.

```
pic    : 17:00-22:00                    (highest)
medium : 07:00-11:00 + 22:00-01:00
eco    : 01:00-07:00 + 11:00-17:00      (lowest)
```

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `pic` | `float` | required | Peak-band rate. |
| `medium` | `float` | required | Medium-band rate. |
| `eco` | `float` | required | Eco-band (lowest) rate. |
| `yearly_fixed_fee` | `float` | `0.0` | Yearly standing charge. |
| `formula` | `str \| None` | `None` | Per-band formula text for diagnostics. |
| `pic_factor` / `pic_base` | `float \| None` | `None` | PIC band's indexation coefficients. |
| `medium_factor` / `medium_base` | `float \| None` | `None` | MEDIUM band's coefficients. |
| `eco_factor` / `eco_base` | `float \| None` | `None` | ECO band's coefficients. |

The six coefficients are the numeric form of `formula`, on the same basis as the
resolved rates beside them: baked to TVAC EUR/kWh on a residential card, left ex-VAT
on a professional one, since the cards print them in c€/kWh Hors TVA. Each band is
parsed independently, so a card that prints only some of them still contributes what
it has, and `None` means "not published" rather than zero.

They are **diagnostic only**. Signing-cohort re-pricing does not use them:
`_cohort_energy_from_archived` (`cohort.py:190`) returns `None` for this shape. An
Impact contract is monthly-indexed, so re-pricing a cohort correctly needs a
three-band monthly-mean shape that resolves downstream the way `SpotMonthlyRates`
does for the single-rate case, and that shape does not exist. Freezing the archived
card's resolved bands instead would pin the signing-month index, the exact bug that
function exists to avoid. Capturing the coefficients is the prerequisite if the shape
is ever built.

### SpotMonthlyRates

`providers/base.py:163`. Monthly-indexed energy contract (`kind = "spot_monthly"`):
a single flat rate for the whole delivery month, `factor * monthly_mean(spot) +
base`, where the mean is the arithmetic average of that month's hourly ENTSO-E
day-ahead spots. Used by the expert **custom** monthly-average mode for
group-purchase products (e.g. the Mega iChoosr / Samen Overstappen
*groepsaankoop*) that index the commodity to the realized monthly average. The
coordinator threads the mean through the same `spot_eur_per_kwh` parameter
`DynamicRates` uses, so the mean is computed at pricing time, not stored in the
snapshot. Unlike `DynamicRates` the rate never varies within a month (always the
hourly grid); the current month's mean is a running estimate until the month
closes.

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `factor` | `float` | required | Multiplier on the monthly mean spot. |
| `base` | `float` | required | Additive term, EUR/kWh. |
| `yearly_fixed_fee` | `float` | `0.0` | Yearly standing charge. |

### InjectionRates

`providers/base.py:268`. Solar feed-in compensation, in EUR/kWh. Belgian
residential injection is exempt from VAT, so these values are NEVER VAT-incl
regardless of the consumption snapshot's `vat_rate`. At least one of (`current`,
`factor`+`base`) must be populated.

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `current` | `float \| None` | `None` | Supplier's monthly indicative price, used when no live spot is available. An illustrative value that appears in the source comment is Eneco Power Fix's "Maandprijs" of 4.76 c/kWh (`providers/base.py:126`; illustrative only). |
| `factor` | `float \| None` | `None` | Multiplier for the hourly formula `injection = factor * spot + base`. |
| `base` | `float \| None` | `None` | Additive term for that formula. Belgian formulas can produce negative values at low spot (the producer pays to inject) and the engine respects that. |
| `formula` | `str \| None` | `None` | Formula text for diagnostics. |
| `peak` | `float \| None` | `None` | Per-slot injection peak rate for a TOU contract whose feed-in varies by slot (Engie Empower Flextime). When set, the engine selects the slot with the same `tou_slot()` rule as the consumption side. |
| `transition` | `float \| None` | `None` | Per-slot transition injection rate. |
| `offpeak` | `float \| None` | `None` | Per-slot off-peak (super-off-peak) injection rate. |
| `floor_at_zero` | `bool` | `False` | Opt-in: clamp the resolved injection rate at 0 (the contract guarantees a never-negative feed-in tariff, e.g. the Mega groepsaankoop). Applied in both the live and historical paths. Leave `False` for every scraped card, which must respect negative formulas. |

When the per-slot triplet is set, `current` stays the single-meter fallback. The
vast majority of contracts leave the triplet `None` (one injection rate across
all hours). Note the related invariant: monthly-indexed injection (EBEM
Variabel/B@sic+, Eneco Fix/Flex, DATS24) must emit `current` only and never an
hourly-spot `factor`/`base`, or a latent mis-price is masked while the
indicative prints.

## Overlay dataclasses

### DsoOverlay

`providers/base.py:310`. Network + capacity costs for one DSO sub-area, in
EUR/kWh and EUR/kW/yr. One of these is keyed under each DSO in
`SupplierSnapshot.dsos`.

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `distribution_single` | `float` | required | Single-meter distribution rate. |
| `distribution_peak` | `float \| None` | `None` | Bi-hourly peak distribution rate. |
| `distribution_offpeak` | `float \| None` | `None` | Bi-hourly off-peak distribution rate. |
| `distribution_exclusive_night` | `float \| None` | `None` | Distribution rate for a separate exclusive-night meter circuit. `None` falls back to `distribution_offpeak` in `pricing.network_eur_per_kwh`. |
| `transport` | `float` | required | Transmission (transport) rate. |
| `data_management_per_year` | `float` | `0.0` | Yearly meter data-management fee. |
| `capacity_eur_per_kw_year` | `float \| None` | `None` | Flanders capacity tariff per kW of monthly peak per year. |
| `brussels_osp_by_tier` | `dict[str, float] \| None` | `None` | Brussels Brugel OSP annual fee keyed by connection-power tier (`le1_44` / `le6` / `le9_6` / `le13`). Only the Sibelga overlay carries it; the user's tier selects the billed value. `None` outside Brussels or when the card omits the table. |
| `prosumer_eur_per_kva_year` | `float \| None` | `None` | Prosumer (compensation-regime) tariff in EUR per kVA of inverter capacity per year. Wallonia DSOs publish it (valid until 2030 per CWaPE); Flanders digital meters do not, so it stays `None` there. |
| `distribution_pic` | `float \| None` | `None` | Tarif Impact peak-band distribution rate (Wallonia only). |
| `distribution_medium` | `float \| None` | `None` | Tarif Impact medium-band distribution rate. |
| `distribution_eco` | `float \| None` | `None` | Tarif Impact eco-band distribution rate. |

The three `distribution_pic/medium/eco` fields carry the Wallonia-only Tarif
Impact bands (`pic` 17:00-22:00, `medium` 07:00-11:00 + 22:00-01:00, `eco`
01:00-07:00 + 11:00-17:00, every day). Wallonia DSOs publish all three on every
supplier card; Brussels (Sibelga) and Flanders (Fluvius) do not, so they stay
`None` there. The canonical DSO sub-area keys used to index `dsos` live in
`const.py:49` onward (eight Fluvius keys, five Wallonia keys, Sibelga); they are
stable forever because they are stored verbatim in every user's `CONF_DSO`.

### TaxOverlay

`providers/base.py:454`. Federal and regional levies, all in EUR/kWh except the
energy fund.

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `federal_excise` | `float` | required | Federal excise (accijns). |
| `energy_contribution` | `float` | required | Federal energy contribution. |
| `flanders_renewables` | `float` | `0.0` | Flanders cogen + green-energy surcharge. |
| `wallonia_renewables` | `float` | `0.0` | Wallonia green-energy contribution. |
| `brussels_renewables` | `float` | `0.0` | Brussels green-energy levy. |
| `region_connection_fee` | `float` | `0.0` | Regional connection fee. |
| `energy_fund_eur_per_month` | `float` | `0.0` | Monthly energy-fund charge (the one field not per-kWh). |
| `vat_rate` | `float` | `0.0` | VAT convention. `0.0` means the snapshot's prices are already VAT-incl (the convention for both Eneco and Cociter today). An extractor that ships ex-VAT numbers must set this to the parsed rate explicitly. |

Regional renewables differ across the three regions; the pricing engine picks
the right one per region, and an extractor that operates in only one or two
regions leaves the others at `0`. The `vat_rate = 0.0` convention is the common
gotcha: it does not mean "no VAT", it means "prices already include VAT".

### SupplierSnapshot

`providers/base.py:478`. Everything extracted from one supplier's card, per
`(supplier, contract)`. The coordinator combines it with the user's selected DSO
to produce the all-in price.

```python
@dataclass(frozen=True, kw_only=True)
class SupplierSnapshot:
    supplier: str
    contract: str
    energy: EnergyRates
    dsos: dict[str, DsoOverlay]
    taxes: TaxOverlay
    source_url: str
    publication_label: str = ""
    injection: InjectionRates | None = None
    supplier_prosumer_eur_per_kva_year: float | None = None
    valid_until: date | None = None
```

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `supplier` | `str` | required | Supplier id (matches `SupplierExtractor.id`). |
| `contract` | `str` | required | Contract id (matches the selected `Contract.id`). |
| `energy` | `EnergyRates` | required | One of the six rate variants above. |
| `dsos` | `dict[str, DsoOverlay]` | required | Network/capacity overlay keyed by canonical DSO sub-area key. |
| `taxes` | `TaxOverlay` | required | Federal + regional levies. |
| `source_url` | `str` | required | URL the snapshot was parsed from (surfaced in diagnostics). |
| `publication_label` | `str` | `""` | Human-readable publication marker (for example the card's month label) for diagnostics. |
| `injection` | `InjectionRates \| None` | `None` | Solar feed-in compensation, or `None` when the contract has no injection. |
| `supplier_prosumer_eur_per_kva_year` | `float \| None` | `None` | Supplier-side compensation-regime prosumer forfait in EUR per kVA per year, billed ON TOP OF the DSO prosumer tariff. Cociter Variable publishes one; most cards do not. Already TVAC, never VAT-scaled. |
| `valid_until` | `date \| None` | `None` | Last calendar day the rates apply to (typically the last day of the pricing month). `None` when the extractor could not parse a validity period; consumers treat `None` as "assume available". |

`valid_until` feeds the `tomorrow_prices_available` binary sensor, which checks
`date.today() <= valid_until`; `None` means "we do not know", so callers fall
back to treating tomorrow's rates as available (`providers/base.py:506`).

## The registry (providers/__init__.py)

The registry is the single list of suppliers the integration knows about
(`providers/__init__.py:65`).

```python
EXTRACTORS: dict[str, SupplierExtractor] = {
    _ENECO.id: _ENECO,
    _ENGIE.id: _ENGIE,
    _TOTALENERGIES.id: _TOTALENERGIES,
    _LUMINUS.id: _LUMINUS,
    _MEGA.id: _MEGA,
    _BOLT.id: _BOLT,
    _COCITER.id: _COCITER,
    _DATS24.id: _DATS24,
    _EBEM.id: _EBEM,
    _ECOFIX.id: _ECOFIX,
    _ECOPOWER.id: _ECOPOWER,
    _FRANK.id: _FRANK,
    _OCTAPLUS.id: _OCTAPLUS,
}
```

Each value's `EXTRACTOR` is imported under a private alias and keyed by its own
`.id`. The id is the stable key: it is what gets stored in every user's config
entry, so it must never change after a supplier ships.

Two lookups are exported (`providers/__init__.py:87` and `:96`):

| Function | Signature | Behaviour |
| --- | --- | --- |
| `get` | `get(supplier_id: str) -> SupplierExtractor` | Returns the registered extractor or raises `ExtractorError` (`no extractor registered for supplier ...`) on an unknown id. |
| `all_extractors` | `all_extractors() -> tuple[SupplierExtractor, ...]` | Returns every registered extractor, in insertion order. Used by the config flow (to list suppliers) and by `scripts/live_check.py` (to exercise all of them). |

The `__init__.py` also re-exports the core dataclasses (`Contract`,
`DsoOverlay`, `DynamicRates`, `EnergyRates`, `ExtractorError`, `FixedRates`,
`SupplierExtractor`, `SupplierSnapshot`, `TaxOverlay`, `VariableRates`) for
convenient importing from `providers`.

Registering a new supplier is two edits: add the module (`providers/foo.py`
exposing a top-level `EXTRACTOR`), then add `_FOO.id: _FOO` to `EXTRACTORS`
(with the matching `from .foo import EXTRACTOR as _FOO`).

## The PDF toolkit (providers/_pdf.py)

Every PDF-based provider builds on `providers/_pdf.py`. It centralises fetching,
PDF text extraction (three strategies), Belgian number parsing, VAT and validity
parsing, and a couple of Belgium-specific table parsers. Concentrating the
network and error handling here keeps roughly six lines of boilerplate out of
every provider and makes the transient-vs-permanent error distinction
consistent.

### Fetch and validation

| Symbol | Signature (async unless noted) | Solves |
| --- | --- | --- |
| `USER_AGENT` | module constant | `Home Assistant be_electricity_prices/<version>`, read from `manifest.json` (`_pdf.py:62`). Sent on every request. |
| `fetch_text` | `fetch_text(session, url, *, timeout=20) -> str` | GET an HTML listing / index / plain-text source. Raises `ExtractorError` on non-2xx or network error (`_pdf.py:444`). |
| `fetch_pdf_text` | `fetch_pdf_text(session, url, *, timeout=30) -> str` | Download a PDF and return concatenated pypdf text; parsing runs in a worker thread so a multi-page card never stalls the HA event loop (`_pdf.py:182`). |
| `fetch_pdf_text_layout` | `fetch_pdf_text_layout(session, url, *, timeout=30) -> str` | Layout-preserving pdfplumber variant (`_pdf.py:337`). |
| `fetch_pdf_text_aligned` | `fetch_pdf_text_aligned(session, url, x_join_threshold=0.0, *, timeout=30) -> str` | Word-coordinate aligned pdfplumber variant (`_pdf.py:323`). |
| `flanders_tax_overlay` | `flanders_tax_overlay(text, *, supplier, excise, renewables, contribution=None, fund=None) -> TaxOverlay` | The tax block of a Flanders-only, VAT-inclusive card. Callers pass their own compiled anchors; this holds the POLICY, which is what drifted: excise mandatory (patterns tried in order, so a flat row wins over the tiered one being phased out), renewables mandatory and all summed, contribution optional (absent = the levy abolished on 2026-08-01, not a layout drift), fund optional and in EUR/month so unscaled. Used by Frank, energie.be and EnergyVision. |
| `head_freshness_key` | `head_freshness_key(session, url, *, prefer=("Last-Modified", "ETag")) -> str \| None` | Cheap `SnapshotProbe` implementation: HEAD the card and return the first present preferred header, else `None`. Bolt prefers `ETag` first (its `Last-Modified` flips per CDN edge); everyone else prefers `Last-Modified` (`_pdf.py:350`). |

Internals worth knowing:

- `_fetch_validated_pdf_bytes` (`_pdf.py:143`) is shared by the three
  `fetch_pdf_text*` variants. It catches both `aiohttp.ClientError` and
  `TimeoutError` (aiohttp's `ClientTimeout` fires `asyncio.TimeoutError`, which
  is not a `ClientError`) and wraps them with the exact prefix
  `network error fetching`. That prefix is load-bearing: `is_transient_fetch_error`
  keys on it. HTTP >= 400 is wrapped as `HTTP <status> fetching <url>`.
- `error_text` (`_pdf.py:89`) renders the wrapped exception as `str(err)` or, when
  that is empty, its class name. aiohttp raises its timeouts argless, so without
  it the message ended in a bare colon -- and that message is user-visible on the
  `snapshot_stale` Repairs card, the `last_error` sensor attribute, and in
  diagnostics. The ENTSO-E client uses it for the same reason (`api.py:127`).
- `_is_pdf_payload` (`_pdf.py:129`) validates by magic bytes (`%PDF`, allowing a
  leading UTF-8 BOM that OCTA+ prepends), not Content-Type, because some CDNs
  return 200 + text/html for a missing PDF, and Engie's API returns
  octet-stream for valid PDFs.
- `_read_pdf_bytes` (`_pdf.py:112`) refuses a body whose declared Content-Length
  exceeds `_MAX_PDF_BYTES` (64 MiB, about 12x the largest real card), bounding
  what a broken or hostile CDN can pull into coordinator memory.
- `is_transient_fetch_error(message: str) -> bool` (`_pdf.py:62`) classifies an
  `ExtractorError` message: `network error fetching` is always transient; among
  HTTP statuses, 5xx plus 408/429/403 are transient (Cloudflare-fronted
  suppliers intermittently answer with a 403 anti-bot challenge or a retryable
  429), while 404/410 are permanent (card renamed or withdrawn) and must fail
  fast.

### Text extraction strategies

Three extraction strategies exist because Belgian cards render tables three
incompatible ways. All three fail loud (raise `ExtractorError`) when a PDF has
pages but no decodable text, rather than returning blank text that every
downstream regex would miss silently.

| Symbol | Signature (sync) | Solves |
| --- | --- | --- |
| `extract_pdf_text` | `extract_pdf_text(payload: bytes) -> str` | Default pypdf extraction. Logs and skips pages pypdf returns `None` for (undecodable fonts); raises only if every page fails (`_pdf.py:192`). |
| `extract_pdf_text_layout` | `extract_pdf_text_layout(payload: bytes) -> str` | pdfplumber layout mode for cards with rotated DSO/tax columns that pypdf drops (TotalEnergies). Runs `dedupe_chars()` first to drop stacked duplicate glyphs (for example `55,,09` rendered instead of `5,09`) (`_pdf.py:247`). |
| `extract_pdf_text_aligned` | `extract_pdf_text_aligned(payload, y_tolerance=3, x_join_threshold=0.0) -> str` | Re-groups `extract_words()` output into visual rows by y-coordinate for column-major cards (OCTA+). `x_join_threshold` is opt-in: leave `0.0` to keep words separate; pass ~1.0pt to glue sub-point-gap glyphs (`5 ,0 3 2 9` into `5,0329`). Pages joined with form-feeds (`_pdf.py:271`). |

### Number, sign and VAT parsing

| Symbol | Signature (sync) | Solves |
| --- | --- | --- |
| `to_float` | `to_float(text: str) -> float` | Parse a Belgian/French decimal (`15,93` or `0.102`). Strips every Unicode space variant used as a thousands separator (NBSP, thin space, NNBSP, line separator) before swapping comma for dot, so `5 029` does not raise (`_pdf.py:551`). |
| `parse_sign` | `parse_sign(char: str) -> float` | Return `-1.0` for any hyphen/dash/Unicode-minus, `+1.0` otherwise. Use as `base = parse_sign(m.group(N)) * to_float(m.group(N+1))` so a card that swaps to U+2212 or flips polarity does not silently break the parser (`_pdf.py:596`). |
| `SIGN_CHARS` | module constant | Character-class string `+\-` plus six dash variants, to drop into a regex as `[` + `SIGN_CHARS` + `]` (`_pdf.py:592`). Supplier PDFs flip silently between these on re-renders. |
| `fold_accents` | `fold_accents(text: str) -> str` | Lowercase and strip Latin diacritics, so a literal test for `août` still matches an extraction that lost the accent to `aout`. Fold both haystack and needle (`_pdf.py:494`). |
| `vat_multiplier` | `vat_multiplier(text, *patterns, default=1.06) -> float` | Read the VAT percentage from a card header (each supplier phrases it differently) and return `1 + N/100` via `to_float` (so `21,5%` works). Falls back to `default` (1.06, illustrative current Belgian residential rate) when no pattern matches (`_pdf.py:551`). |

### Belgium-specific table and date parsers

| Symbol | Signature (sync) | Solves |
| --- | --- | --- |
| `parse_brussels_osp` | `parse_brussels_osp(text: str) -> dict[str, float] \| None` | Parse the Brussels Brugel OSP annual-fee table off a Sibelga card. Anchors on the `Obligations de Service` block (case-insensitive: Bolt lowercases `s`) and each `<bound> kVA <value>` row, returning the four residential tiers (`le1_44`/`le6`/`le9_6`/`le13`) or `None` when absent. Populates `DsoOverlay.brussels_osp_by_tier` (`_pdf.py:617`). |
| `parse_valid_until` | `parse_valid_until(text: str) -> date \| None` | Best-effort parse of the card's validity date, anchored within ~200 chars after a validity keyword (`geldig`/`valable`/`validit`/`valid `). Tries spelled-out `<day> <month> <year>`, numeric `DD/MM/YYYY` (or `DD/MM/YY`), then bare `<month> <year>` (last day of month). Clamps candidates to a symmetric 5-year horizon around Brussels-local today so a corrupted footer date does not produce a bogus year. Returns the latest match or `None`. Populates `SupplierSnapshot.valid_until` (`_pdf.py:858`). |
| `text_mentions_month` | `text_mentions_month(text, year_month: date, month_names: tuple[str, ...]) -> bool` | Heuristic that `text` references the requested month+year inside an anchored window (first 1000 chars where the card title prints, plus validity-keyword windows). Accent-folds both sides and collapses whitespace so `mei\n2026` matches (`_pdf.py:199`). |
| `archive_validity_check` | `archive_validity_check(snap, text, year_month, *, month_names=None) -> SupplierSnapshot \| None` | Confirm an archived snapshot actually covers `year_month`. Returns `snap` on pass, `None` otherwise, so a provider's `fetch_for_month` can fall back to the proxy rather than mis-bill. Two tiers: authoritative `snap.valid_until` month check when present; otherwise require a textual month mention (when `month_names` given, as eneco/cociter do) or accept on the URL resolver alone (when `None`, as ebem does) (`_pdf.py:819`). |

Two supporting internals back these date parsers: `_MONTH_NAMES` (`_pdf.py:682`)
maps Dutch, French (with and without accents) and English month names to their
1-12 index, and `_validity_windows` (`_pdf.py:757`) returns the ~200-char
context after each validity keyword so a retrospective month mention elsewhere
in the PDF does not masquerade as a validity statement. `_OSP_BOUND_TO_TIER`
(`_pdf.py:545`) maps kVA upper bounds to the shared tier keys and is kept as
literals so this low-level helper stays decoupled from `const.py` (the keys must
match `const.CONNECTION_KVA_TIER_*`).

## How to implement a new provider

Grounded in the protocol above, a minimal new PDF provider looks like this:

1. Create `providers/<supplier>.py`.
2. Declare the products as `Contract` instances, one per product, with the right
   `kind`. Set `regions` if the product is not sold in all three regions; set
   `spot_indexed_injection=True` only for the Cociter-Variable shape (variable
   energy but per-hour spot injection).
3. Implement `async def fetch(session, contract_id, region) -> SupplierSnapshot`.
   Use the `_pdf.py` helpers: `fetch_pdf_text` (or the layout/aligned variants
   for rotated or column-major cards), `to_float` / `parse_sign` / `SIGN_CHARS`
   for numbers, `vat_multiplier` for VAT, `parse_valid_until` for `valid_until`,
   `parse_brussels_osp` for a Sibelga card. Build one `DsoOverlay` per DSO
   sub-area the card covers, keyed by the canonical `const.py` DSO keys. Raise
   `ExtractorError` on any parse failure; never invent a EUR value.
4. Populate `TaxOverlay`. Remember `vat_rate=0.0` means "prices are already
   VAT-incl"; only set a non-zero rate if the card ships ex-VAT numbers.
5. Populate `injection` (`InjectionRates`) if the contract has feed-in. Emit
   `current` only for monthly-indexed injection; emit `factor`/`base` only for a
   genuine hourly-spot formula; use the per-slot triplet only for a TOU contract
   whose feed-in varies by slot. Injection values are never VAT-incl.
6. Optionally implement `probe` (usually `functools.partial(head_freshness_key, ...)`
   or a listing GET) returning a stable string, `None` when there is no reliable
   signal.
7. Optionally implement `fetch_for_month` for a supplier with an accessible
   archive; return `None` for months you cannot serve, and wrap the resolved
   snapshot in `archive_validity_check` so a CDN substitution does not mis-bill.
8. Expose a top-level `EXTRACTOR = SupplierExtractor(id=..., label=..., contracts=..., fetch=..., probe=..., fetch_for_month=...)`.
   The `id` is permanent once shipped.
9. Register it: add the import and the `EXTRACTORS` entry in `providers/__init__.py`.
10. Add a fixture-driven test under `tests/` (see [ci-and-testing.md](ci-and-testing.md))
    and a provider doc under [providers/](providers/). `scripts/live_check.py`
    will exercise the real card weekly via `all_extractors()`.

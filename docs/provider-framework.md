# Provider framework

This document is the reference for the per-supplier extractor layer under
`custom_components/be_electricity_prices/providers/`. It covers the extractor
protocol (`SupplierExtractor` and its three callable contracts), every shared
dataclass that a snapshot is built from, the registry that the coordinator and
config flow read, and the shared PDF toolkit (`providers/_pdf.py`) that every
card-parsing provider builds on. The guiding invariant of the whole layer is
that no EUR value lives in Python source: every number in a `SupplierSnapshot`
comes from a live fetch of the supplier's own published tariff card
(`providers/base.py`).

Related docs:

- [architecture.md](architecture.md): where this layer sits in the whole integration.
- [coordinator.md](coordinator.md): who calls `fetch` / `probe` / `fetch_for_month`, and when.
- [pricing-model.md](pricing-model.md): how `compute_breakdown` consumes the dataclasses documented here.
- [config-flow.md](config-flow.md): how contracts and regions drive the setup wizard.
- [ci-and-testing.md](ci-and-testing.md): how `scripts/live_check.py` exercises every extractor daily.
- [glossary.md](glossary.md): Belgian-energy and HA terms used throughout.
- The per-supplier docs under [providers/](providers/) each document one concrete extractor.

## Layer overview

Each supplier is a self-contained module (for example `providers/bolt.py`) that
exposes exactly one top-level name, `EXTRACTOR`, of type `SupplierExtractor`
(`providers/base.py`). The module's job is to turn the
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

The registry entry for one supplier (`providers/base.py`). It is a frozen,
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
| `settles_on_next_card` | `bool` | Set where `fetch_for_month` settles a month-indexed month on the card that follows it and flags it provisional until then (Aspiravi, Cociter, Eneco, EBEM, Elegant, Trevion, Mega, Engie), or on the month's own card where the live capture of it is unsettled (Ecopower, whose definitive card prints the month's settled index and SPP). Luminus, TotalEnergies and Bolt set it too, beside `settle_month`, which settles it in place instead. Only these are asked again about a closed month the card archive caught while it ran, at runtime and by the daily archive run; any other supplier answers with the same card, and asking cost the compare page a supplier fetch per candidate after every restart. |
| `settle_month` | `MonthSettler \| None` | Set where a month is settled IN PLACE (Luminus, TotalEnergies, Bolt): the card held for a closed month, live capture or archive answer, keeps every field and only its month-indexed rates are re-priced on the index the next card names (TotalEnergies' names none, and its rates are solved for it; Bolt's variable cards are re-priced a quarter at a time, each register on the index the next quarter's card prints for it, `settled_registers`), with `index_realised` recorded (`providers/_settle.py`). `fetch_for_month` is then never asked to replace a held card, at runtime or by the daily archive run, because Luminus's archive serves a month without its campaign. TotalEnergies has no `fetch_for_month` at all: the next card is only ever its live one, and the daily archive run settles the rows it caught while that card is served. Provisional while the next card is not out, and at TotalEnergies while its feed-in card still names the month before, a card's monthly block is not read, or no other card of the range is printed at the energy's index or two disagree (`_range_agrees`), the other leg settling meanwhile. |
| `deprecated_until` | `date \| None` | Set when the supplier has announced it is leaving the residential market: the date its contracts stop being supplied. Drops the supplier from the config flow's new-setup and compare pickers, and raises the `supplier_deprecated` Repairs card on every entry using it. Hiding takes effect as soon as the flag ships, ahead of the date, because you cannot sign up today for a contract being transferred away. The date itself is compared to the clock in two places once it passes: the coordinator stops asking the supplier for a card and swaps the Repairs card for the supply-ended variant, and the archive walk drops the supplier from its targets. |
| `deprecated_successor` | `str \| None` | Registry id of the supplier taking the contracts over; named in the Repairs card so the user knows what to switch to. It is only named when it has a contract in the entry's own region: a withdrawal names one successor nationally, while our coverage is per region, so an entry we cannot route anywhere gets the `supplier_deprecated_no_successor` variant instead of advice the config flow would refuse. |

A withdrawn supplier keeps working: `fetch` and `probe` are untouched, and
`providers.get()` still resolves it, so existing entries carry on pricing off the
supplier's card until it stops publishing. Only the pickers and the Repairs card
react. Do NOT filter withdrawn suppliers out of `EXTRACTORS` or
`all_extractors()` -- that would also hide them from the live-check's registry
diff and from every entry that still needs to price.

`regions()` (`providers/base.py`) returns the union of `Contract.regions`
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

Defined at `providers/base.py`. The mandatory current-card fetch. It must
return a fully populated `SupplierSnapshot` or raise `ExtractorError`
(`providers/base.py`) on any fetch or parse failure. It never returns
`None`: a missing current card is an error, not an absence.

#### SnapshotProbe

```python
SnapshotProbe = Callable[
    [aiohttp.ClientSession, str, str], Awaitable[str | None]
]
```

Defined at `providers/base.py`. A cheap freshness key. The coordinator calls
it hourly and only re-runs `fetch` when the returned key changes from the cached
one. Semantics of the return value:

- A stable string that stays the same across calls means the cached snapshot is
  still valid (skip the expensive `fetch`).
- A different string means the card changed; refetch.
- `None` means the supplier has no probe path the coordinator can rely on
  (for example DATS 24's one PDF per month, or energie.be/Engie/Luminus API
  endpoints, replaced in place within the month). The coordinator then falls
  back to its time-based TTL.

Most PDF providers implement this by delegating to `head_freshness_key()` (see
[_pdf.py helpers](#the-pdf-toolkit-providers_pdfpy) below).

#### ArchivedSnapshotFetcher

```python
ArchivedSnapshotFetcher = Callable[
    [aiohttp.ClientSession, str, str, "date"], Awaitable["SupplierSnapshot | None"]
]
```

Defined at `providers/base.py`. Fetches the card that was published for a
specific `(year, month)` (passed as a `datetime.date`), so the yearly-cost flow
can bill each past month at its own historical rate rather than proxying every
month at the current rate. Return-value semantics:

- A `SupplierSnapshot` for the requested month when the archive resolves.
- An `ExtractorError` for a transient fetch failure (a timeout, a reset, a 5xx,
  an anti-bot 403, per `is_transient_fetch_error`). The month cache
  (`_snapshot_for_month`, `snapshot_months.py`) writes its retry marker only
  when the fetcher RAISES; a `None` is cached as the month's answer for the
  provisional TTL and, through a backfill, can reach the recorder. Every
  provider re-raises such a failure around its network calls, the way
  `dats24.py` always did.
- `None` when the supplier has no accessible archive for that month. This
  applies to the suppliers that overwrite their cards in place and keep no
  archive (Ecofix, TotalEnergies; OCTA+ overwrites its live cards too, but its
  site's archive answers for past months), the
  running month on a supplier that publishes in arrears (energie.be), and any
  month before the supplier's archive horizon (Engie's 2023 cards predate the
  layout its parser reads, energie.be's mid-2025 cards are page images). The
  month cache asks the repository's own card archive first for a closed month
  (`snapshot_months._archived_card_from_github`, fed daily by
  `scripts/archive_cards.py` and mirroring these archives, see
  [ci-and-testing.md](ci-and-testing.md)); `fetch_for_month` answers for a
  month the archive does not hold, and on `None` from both the current
  snapshot stands in as a proxy. Where the extractor sets
  `settles_on_next_card`, it is also asked over an archive row caught
  live on a card indexed on its own month (`_awaits_settlement`), since that
  row holds the estimate the card printed and the supplier's path is what
  settles the month on the next card: a settled answer replaces the row, one
  still provisional or a failure leaves the row billing but provisional, and
  `None` leaves it as it is. An extractor with `settle_month` is never asked
  for a replacement: whichever tier gave the month's card, it is re-priced in
  place until its month-indexed legs carry `index_realised` (`is_settled`).

An extractor whose `fetch_for_month` field is itself `None` means the supplier
has no archive at all; its past months come from the repository archive alone.
Providers that do implement it typically cross-check the
resolved card with `archive_validity_check()` (see below) so a CDN that silently
substitutes the current card for a withdrawn archive URL does not mis-bill past
months.

## Contract and rate dataclasses

A `SupplierSnapshot.energy` is one of six `EnergyRates` variants
(`providers/_rates.py`) chosen by the contract's `kind`. All rate dataclasses
are `frozen=True, kw_only=True`. EUR values are always populated from a live
fetch, never hardcoded — the one exception is the expert **custom** supplier
(`providers/custom.py`), whose snapshot is built from the config entry the user
filled in rather than a scraped card.

### Contract

`providers/_rates.py`. One product sold by a supplier.

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `id` | `str` | required | Stable contract key, stored in the config entry. |
| `label` | `str` | required | Human-facing product name. |
| `kind` | `TariffKind` | required | One of `"fixed"`, `"variable"`, `"dynamic"`, `"tou"`, `"tou_impact"`, `"spot_monthly"` (`providers/_rates.py`). Selects which `EnergyRates` variant the snapshot carries. |
| `regions` | `frozenset[str]` | all three | Regions the product is actually published in. Defaults to `{flanders, wallonia, brussels}`; extractors override per-contract for products that 404 outside their home region (for example TotalEnergies Impact is Wallonia-only). |
| `professional` | `bool` | `False` | `True` when the supplier sells this product to businesses: its card is published excluding VAT and may band the federal excise by annual volume, so the config flow shows a **professional** step asking for the VAT treatment and the yearly consumption. Nothing else keys off it; a professional contract is otherwise an ordinary contract. |
| `spot_indexed_injection` | `bool` | `False` | `True` when a non-dynamic product's feed-in is index-linked and its energy leg fetches no spots, so pricing the injection needs an ENTSO-E key the energy side never asks for. Both index resolutions count: per hour (`_injection_needs_spot`) and a delivery-month mean (`_injection_needs_month_spot`). The config flow reads it to offer the API-key step on the injection regime. Dynamic contracts already collect the key via their energy formula and leave this `False`. The README states the current count and a test derives it from the registry, so it is not repeated here. |
| `month_indexed_energy` | `bool` | `False` | `True` when a non-spot-priced product's ENERGY is indexed on the delivery month's mean and its card prints last month's figure, so the config flow offers the optional ENTSO-E key on every solar regime. The registry twin of the parser's `month_indexed`; the live check holds each fetched card to it (`_expect_month_indexed_registry`). |
| `quarter_hourly_option` | `bool` | `False` | `True` when the supplier sells this product on both settlements, so the flow shows a **settlement** step and `resolve_settlement_grid` applies the answer. Two cases, and they differ in how far the answer reaches. Frank Energie's Dynamisch tiers are `dynamic` either way and only `DynamicRates.quarter_hourly` moves. Bolt's four variable cards, in both segments, settle the same printed formula against the RLP-weighted month or per quarter-hour, which are different `EnergyRates` kinds, so `effective_kind` moves the CONTRACT KIND with the answer and everything the flow decides before it has fetched a card (meter list, mandatory ENTSO-E key, signing-rate boxes, ranking cell) reads it through that. Not for a supplier that sells the two grids as two products (Energy Knights Agilior against Agilis), nor for one whose card fixes the grid and whose parser sets `quarter_hourly` itself. |
| `withdrawn` | `date \| None` | `None` | The first day the supplier no longer sold this product, the contract-level twin of `SupplierExtractor.deprecated_until`. Hides the product from the config flow's contract picker (kept, through `keep=`, on an entry already on it, or its edit form would refuse its own default) and from both compare pages, and raises the `contract_withdrawn` Repairs card on every entry using it. `fetch` is untouched, so such an entry keeps pricing on the last card published for the product, and the live check reports that card's failures as expected rather than filing them. Nothing compares the date to the clock in the integration. OCTA+'s Fixed, Fixed Impact, Eco Fixed, Flux and Eco Flux and Trevion's LifePowr carry 2026-10-01. |
| `direct_debit_discount` | `bool` | `False` | `True` when this product's card prices a direct-debit payer differently, so the flow shows a **direct_debit** step and `resolve_direct_debit` applies the answer. Two uses: Brusol's Groene stroom takes a reduction off the standing charge (`SupplierSnapshot.direct_debit_discount_eur`: 250 EUR/yr, 230 on domiciliëring), and Mega's cards make the first-year ristourne larger for a direct-debit payer, or grant it to nobody else (`welcome_credit_direct_debit_eur`, `welcome_credit_requires_direct_debit`). The euros are read off the card like every other figure, never written here; this flag only decides whether the household is asked. Same registry-versus-parser agreement as the two flags above, and the same failure if they disagree: unset, no step asks, nothing is stored and a parsed reduction is billed to nobody. |

`spot_indexed_injection` is a load-bearing invariant: shape (c) and shape (d)
injection must have the spot wired through the live, backfill, and compare paths,
all gated on this flag, or the injection credit drifts. It started as the Cociter
Variable shape and now covers most of the static range across a dozen suppliers,
so do not read it as a one-supplier edge case.

### FixedRates

`providers/_rates.py`. Fixed energy contract: constant EUR/kWh, optionally
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

`providers/_rates.py`. Variable energy contract: the current month's effective
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
| `index_realised` | `float \| None` | `None` | The value the card's index settled at for the card's OWN month, in EUR/kWh, once the supplier published it. Eneco footnotes each month's realised Belpex-RLP-M on the next card, as Aspiravi, Cociter, EBEM, Elegant, Engie and Trevion print theirs there, and Ecopower's definitive card prints its own month's; `fetch_for_month` settles the archived month on it (Luminus in place, `settle_month`, as TotalEnergies does on the index its next card's rates solve to, since its cards name none; Mega's next card states the month's figures, which are that card's own formulas at the index recorded, see `settled_as_stated`) and `current` is then that figure. `None` while the next card is not out. |
| `settled_as_stated` | `bool` | `False` | The rates are the figures the supplier states the month settled at, on formulas other than this card's: Mega's next card states them, its own formulas at the month's `index_realised`. The month's own billing takes them as they stand, keyed or not (`_month_indexed_leg` returns `None`), while the coefficients stay this card's, which a cohort signed in its month locks and bills later months on at their `index_realised`; its own month bills the stated rates for that cohort too (`_effective_snapshot_for_month`). |
| `rlp_indexed` | `bool` | `False` | The index is an RLP-weighted month mean, not the plain one; the coordinator resolves the coefficients against Synergrid's residential load profile. Meaningful only with `month_indexed` or `quarter_indexed`. |
| `rlp_blend` | `RlpBlend` | `"distinct"` | Which DSO reduction of that profile: `"distinct"` (the equal mean of the three regional curves, Eneco's), `"columns"` (the column-weighted mean of energie.be, Mega, Luminus BasicFlex, Ecopower and TotalEnergies), `"flanders"` (the Fluvius curve of Energy Knights, Trevion, EBEM and EnergyVision), or `"wallonia"` (Bolt's, the Walloon DSOs' curve). Read only with `rlp_indexed`. |
| `quarter_indexed` | `bool` | `False` | The index is the delivery QUARTER's, not the month's: Bolt's variable cards print "Belpex Q3 2026" and bill the quarter. The monthly leg resolves on the quarter's mean, quarter to date while it runs, each register keeping the spread the card prints (`cohort_legs._quarter_leg`). The re-price keys on it as on `month_indexed`, which such a card leaves `False` so a version without this flag bills the printed rates. A closed quarter's month settles in place on the next quarter's card (`bolt.settle_month`), which records the mono index as `index_realised`. |

### DynamicRates

`providers/_rates.py`. Dynamic energy contract: `factor * spot + base` per
price slot, against the ENTSO-E BE day-ahead spot.

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `factor` | `float` | required | Multiplier on the spot price. |
| `base` | `float` | required | Additive term (EUR/kWh). |
| `yearly_fixed_fee` | `float` | `0.0` | Yearly standing charge. |
| `quarter_hourly` | `bool` | `False` | Selects the spot billing grid. `True` keeps ENTSO-E's native 15-minute slots; `False` aggregates to clock hours. |

`quarter_hourly` reflects a real billing-grid difference between suppliers.
Eneco, Energy Knights (Agilis Online), Frank Energie (by default), Luminus, Mega
and TotalEnergies price per clock hour, so the integration aggregates the
15-minute day-ahead curve to hourly and these leave the flag `False`. Cociter,
EBEM, Ecofix, Ecopower (Dynamische Burgerstroom), energie.be, Energy Knights
(Agilior Online), EnergyVision, Engie, OCTA+ and Trevion bill per quarter-hour
(their cards multiply the 15-minute Belpex / eSpot_15 / Epex 15 / EPEX DA spot)
and set it `True`; Bolt and Frank sell one card on both settlements, and
`resolve_settlement_grid` sets the flag from the entry's answer;
that keeps the live price table, current/next-slot sensors and cheapest-window
service on native 15-minute slots. Year-to-date billing stays hourly regardless,
because HA only retains hourly long-term statistics (`providers/_rates.py`).

### TimeOfUseRates and WeekendRule

`providers/_rates.py`. Time-of-use energy contract: three slots by hour-of-day
(`kind = "tou"`). Requires an SMR3 smart meter.

The weekday schedule is shared across products:

```
peak       : 07:00-11:00 + 17:00-22:00
transition : 11:00-17:00 + 22:00-01:00
offpeak    : 01:00-07:00
```

`weekend_rule` (`WeekendRule`, `providers/_rates.py`) selects the weekend
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

`providers/_rates.py`. Wallonia Tarif Impact energy contract: three slots on
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
| `rlp_indexed` / `rlp_blend` | `bool` / `RlpBlend` | `False` / `"distinct"` | Same as on `VariableRates`: the bands' index is the RLP-weighted month mean, in that blend (Mega Off-peak Impact, `columns`); Cociter's trihoraire BELIX keeps the plain mean. |
| `index_realised` | `float \| None` | `None` | Same as on `VariableRates`: the value the card's own month settled at, once the next card names it. Cociter's trihoraire card records the BELIX `settled_energy` rebuilds the three bands at, and the month splice hands it to the banded `SpotMonthlyRates` leg. Mega's Off-peak Impact records the index its next card's stated bands solve to through that card's own formulas, with `settled_as_stated`. |
| `settled_as_stated` | `bool` | `False` | Same as on `VariableRates`. |

The six coefficients are the numeric form of `formula`, on the same basis as the
resolved rates beside them: baked to TVAC EUR/kWh on a residential card, left ex-VAT
on a professional one, since the cards print them in c€/kWh Hors TVA. Each band is
parsed independently, so a card that prints only some of them still contributes what
it has, and `None` means "not published" rather than zero.

With `month_indexed` set they are what the contract bills. Cociter's trihoraire
card prints its bands at the previous month's BELIX and its note (7) settles the
delivery month on its own, so `_cohort_energy_from_archived` (`cohort_legs.py`)
turns the three pairs into a `SpotMonthlyRates` leg carrying `factor_pic` /
`factor_medium` / `factor_eco` and the per-band ceilings, which `energy_eur_per_kwh`
routes by `dso_impact_band`; every month-mean gate then prices it like the variable
card's mono pair. Without the flag (a card that prints resolved bands with no monthly
formula behind them, or one whose formula rows did not all parse) the function
returns `None` and the printed bands bill, since freezing coefficients the card does
not index monthly would pin the signing-month index, the exact bug it exists to avoid.

### SpotMonthlyRates

`providers/_rates.py`. Monthly-indexed energy contract (`kind = "spot_monthly"`):
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

`providers/_rates.py`. Solar feed-in compensation, in EUR/kWh. Belgian
residential injection is exempt from VAT, so these values are NEVER VAT-incl
regardless of the consumption snapshot's `vat_rate`. At least one of (`current`,
`factor`+`base`) must be populated.

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `current` | `float \| None` | `None` | Supplier's monthly indicative price, used when no live spot is available. An illustrative value that appears in the source comment is Eneco Power Fix's "Maandprijs" of 4.76 c/kWh (`providers/_rates.py`; illustrative only). |
| `factor` | `float \| None` | `None` | Multiplier for the hourly formula `injection = factor * spot + base`. |
| `base` | `float \| None` | `None` | Additive term for that formula. Belgian formulas can produce negative values at low spot (the producer pays to inject) and the engine respects that. |
| `formula` | `str \| None` | `None` | Formula text for diagnostics. |
| `peak` | `float \| None` | `None` | Per-slot injection peak rate for a TOU contract whose feed-in varies by slot (Engie Empower Flextime). When set, the engine selects the slot with the same `tou_slot()` rule as the consumption side. |
| `transition` | `float \| None` | `None` | Per-slot transition injection rate. |
| `offpeak` | `float \| None` | `None` | Per-slot off-peak (super-off-peak) injection rate. |
| `bi_hourly` | `bool` | `False` | The `peak` / `offpeak` pair is the day and night register rate of a bi-hourly meter, not TOU slots (Trevion Groene Energie Vast). The engine credits a two-register meter by register on the entry's day/night schedule (`day_start_for`, `day_night.py`) and a single-register meter at `current`; a pair without the flag on a fixed or variable card is not read. |
| `index_realised` | `float \| None` | `None` | The value the credit's index settled at for the card's OWN month, in EUR/kWh, once the supplier published it. EBEM names the month just closed on the next card ("de SPP0 vorige maand bedroeg 79,11"), Trevion, Elegant and Eneco ("Belpex-injectie (09/2026: €156,4069/MWh)") name the month outright, and Ecopower's definitive card prints its own month's; Mega's next card states the month's credit, its own formula at the index recorded, see `settled_as_stated`. `fetch_for_month` settles the archived month on it, `current` is rebuilt from the card's own coefficients, and the engine bills that figure instead of computing a weighted mean. The `VariableRates` field of the same name does this for the consumption leg; the two indices differ and settle independently. |
| `settled_as_stated` | `bool` | `False` | `current` is the credit the supplier states the month settled at, on a formula other than this card's (Mega's next card, at the month's `index_realised`). The month's own billing credits it as it stands (`_historical_injection_rate`), while `factor` and `base` stay this card's, which a cohort signed in its month locks for later months; the cohort's leg drops the flag. |
| `floor_at_zero` | `bool` | `False` | Opt-in: clamp the resolved injection rate at 0 (the contract guarantees a never-negative feed-in tariff: a custom entry such as the Mega groepsaankoop, or a card stating it, as Ecopower's Groene Burgerstroom has since July 2026). Applied in both the live and historical paths. Leave `False` for a card that does not state the guarantee, which must respect negative formulas. |

When the per-slot triplet is set, `current` stays the single-meter fallback. The
vast majority of contracts leave the triplet `None` (one injection rate across
all hours). A card whose slots are each a monthly formula (Engie Empower Flextime)
sets the three `factor_*` / `base_*` pairs beside the triplet and `month_indexed`;
the triplet is then last month's figure and the coordinator bakes the pairs on the
delivery month's mean. Note the related invariant: a monthly-indexed injection (EBEM
Variabel/B@sic+, Eneco Fix/Fix One/Flex/Flex One, DATS 24, EnergyVision fixed) keeps the
printed indicative as `current`, carries the month formula as `factor`/`base`, and sets
the `month_indexed` or `spp_indexed` flag its card names, so `_injection_needs_spot` and
`_injection_is_spot_formula` (`injection.py`) never price it per hour. Without the flag
the formula would be read as an hourly-spot one, a latent mis-price the indicative masks
while it prints; without the formula the delivery-month re-price is lost.

## Overlay dataclasses

### DsoOverlay

`providers/base.py`. Network + capacity costs for one DSO sub-area, in
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
`const.py` onward (eight Fluvius keys, five Wallonia keys, Sibelga); they are
stable forever because they are stored verbatim in every user's `CONF_DSO`.

### TaxOverlay

`providers/base.py`. Federal and regional levies, all in EUR/kWh except the
energy fund.

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `federal_excise` | `float` | required | Federal excise (accijns). |
| `energy_contribution` | `float` | required | Federal energy contribution. |
| `flanders_renewables` | `float` | `0.0` | Flanders cogen + green-energy surcharge. |
| `wallonia_renewables` | `float` | `0.0` | Wallonia green-energy contribution. |
| `brussels_renewables` | `float` | `0.0` | Brussels green-energy levy. |
| `region_connection_fee` | `float` | `0.0` | Regional connection fee. |
| `energy_fund_eur_per_month` | `float` | `0.0` | Monthly energy-fund charge (the one field not per-kWh). It is a Flemish levy, and `fees.py` bills 12 x it with no region check of its own (`fees.py`), so an extractor serving more than one region must return `0.0` outside Flanders. |
| `vat_rate` | `float` | `0.0` | VAT convention. `0.0` means the snapshot's prices are already VAT-incl (the convention for both Cociter and Eneco today). An extractor that ships ex-VAT numbers must set this to the parsed rate explicitly. |
| `card_vat_rate` | `float \| None` | `None` | The VAT rate the card states for its own customers, as a fraction (`0.06` on a residential card printing "6% TVA"), `None` where it states none. Left out of a stored row while `None`. |
| `assumed_vat_rate` | `float \| None` | `None` | The rate a parser grossed an energy formula by because the card states none (the residential `VAT_RATE_REDUCED`): Bolt's variable cards, energie.be's dynamic and variable ones, OCTA+ in the months its header is missing. On a card priced excluding VAT (Ecopower), the rate `vat_rate` was set to for want of one stated. `None` where nothing was put on a guess. Left out of a stored row while `None`. |

Regional renewables differ across the three regions; the pricing engine picks
the right one per region, and an extractor that operates in only one or two
regions leaves the others at `0`. The `vat_rate = 0.0` convention is the common
gotcha: it does not mean "no VAT", it means "prices already include VAT".

### SupplierSnapshot

`providers/base.py`. Everything extracted from one supplier's card, per
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
    provisional: bool = False
```

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `supplier` | `str` | required | Supplier id (matches `SupplierExtractor.id`). |
| `contract` | `str` | required | Contract id (matches the selected `Contract.id`). |
| `energy` | `EnergyRates` | required | One of the six rate variants above. |
| `dsos` | `dict[str, DsoOverlay]` | required | Network/capacity overlay keyed by canonical DSO sub-area key. |
| `taxes` | `TaxOverlay` | required | Federal + regional levies. |
| `source_url` | `str` | required | URL the snapshot was parsed from (kept in the snapshot cache and the card archive rows, not shown in diagnostics or attributes). |
| `publication_label` | `str` | `""` | Human-readable publication marker (for example the card's month label) for diagnostics. |
| `injection` | `InjectionRates \| None` | `None` | Solar feed-in compensation, or `None` when the contract has no injection. |
| `supplier_prosumer_eur_per_kva_year` | `float \| None` | `None` | Supplier-side compensation-regime prosumer forfait in EUR per kVA per year, billed ON TOP OF the DSO prosumer tariff. Cociter Variable publishes one; most cards do not. Already TVAC, never VAT-scaled. |
| `valid_until` | `date \| None` | `None` | Last calendar day the rates apply to (typically the last day of the pricing month). `None` when the extractor could not parse a validity period; consumers treat `None` as "assume available". |
| `direct_debit_discount_eur` | `float \| None` | `None` | What the card takes off the YEARLY STANDING CHARGE for a household paying by direct debit, or `None` where it offers none, which is every card but Brusol's Groene stroom. Held as the REDUCTION rather than the reduced fee, so it cannot silently disagree with `yearly_fixed_fee` beside it and the extractor can check its reading against the total the same sentence prints. Whether this household pays that way is not on the card: `resolve_direct_debit` applies the config flow's answer and clears this field as it goes, so no later reader can apply it twice. |
| `provisional` | `bool` | `False` | The extractor knows this ARCHIVED month's figures can still change (an Eneco month whose next card, carrying the realised index, is not out yet). The monthly snapshot cache re-fetches such a row after its TTL instead of keeping it as a closed month's fact. Never set on a live card. |

The `welcome_credit_*` fields carry a first-year credit the card grants a new
customer (its amount and kind, a per-kWh or percentage leg, a cap, the wait, a
feed-in bonus and the conditions on it); they
are described with the credit itself in
[pricing-model.md](pricing-model.md#welcome-credits).

`valid_until` feeds the `tomorrow_prices_available` binary sensor, which checks
`date.today() < valid_until`. On `None` it falls back to the month the
`publication_label` names (`card_valid_until`, `_validity.py`), and only a card
dated by neither is treated as available (`providers/base.py`).

## The registry (providers/__init__.py)

The registry is the single list of suppliers the integration knows about
(`providers/__init__.py`).

```python
EXTRACTORS: dict[str, SupplierExtractor] = {
    _ENECO.id: _ENECO,
    _ENGIE.id: _ENGIE,
    _TOTALENERGIES.id: _TOTALENERGIES,
    _TREVION.id: _TREVION,
    _LUMINUS.id: _LUMINUS,
    _MEGA.id: _MEGA,
    _BOLT.id: _BOLT,
    _COCITER.id: _COCITER,
    _DATS24.id: _DATS24,
    _EBEM.id: _EBEM,
    _ECOFIX.id: _ECOFIX,
    _ECOPOWER.id: _ECOPOWER,
    _FRANK.id: _FRANK,
    _ENERGIEBE.id: _ENERGIEBE,
    _ENERGYVISION.id: _ENERGYVISION,
    _ENERGYKNIGHTS.id: _ENERGYKNIGHTS,
    _OCTAPLUS.id: _OCTAPLUS,
    _ASPIRAVI.id: _ASPIRAVI,
    _SOCIAL.id: _SOCIAL,
    _ELEGANT.id: _ELEGANT,
    # Expert escape hatch, listed last so it sorts to the bottom of the
    # supplier dropdown.
    _CUSTOM.id: _CUSTOM,
}
```

Each value's `EXTRACTOR` is imported under a private alias and keyed by its own
`.id`. The id is the stable key: it is what gets stored in every user's config
entry, so it must never change after a supplier ships.

Two lookups are exported (`providers/__init__.py`):

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
| `USER_AGENT` | module constant | `Home Assistant be_electricity_prices/<version>`, read from `manifest.json` (`_pdf.py`). Sent on every request. |
| `fetch_text` | `fetch_text(session, url, *, timeout=20) -> str` | GET an HTML listing / index / plain-text source. Raises `ExtractorError` on non-2xx or network error (`_pdf.py`). |
| `fetch_pdf_text` | `fetch_pdf_text(session, url, *, timeout=30) -> str` | Download a PDF and return concatenated pypdf text; parsing runs in a worker thread so a multi-page card never stalls the HA event loop (`_pdf.py`). |
| `fetch_pdf_text_layout` | `fetch_pdf_text_layout(session, url, *, timeout=30) -> str` | The same download read through `extract_pdf_text_layout` (`_pdf.py`). |
| `fetch_pdf_text_aligned` | `fetch_pdf_text_aligned(session, url, x_join_threshold=0.0, *, timeout=30) -> str` | Word-coordinate aligned pdfplumber variant (`_pdf.py`). |
| `regional_tax_overlay` | `regional_tax_overlay(text, *, supplier, region, excise, renewables, contribution=None, fund=None) -> TaxOverlay` | The tax block of a single-region, VAT-inclusive card. `region` picks which of the three regional renewables fields the levy lands in and the other two stay zero, which is what such a card actually says; an unknown region raises rather than zeroing all three. Callers pass their own compiled anchors; this holds the POLICY, which is what drifted: excise mandatory (patterns tried in order, so a flat row wins over the tiered one being phased out), renewables mandatory and all summed, contribution optional (absent = the levy abolished on 2026-08-01, not a layout drift), fund optional and in EUR/month so unscaled. Used by Aspiravi, Elegant, energie.be, Energy Knights, EnergyVision (for Flanders and for Brussels) and Frank. |
| `head_freshness_key` | `head_freshness_key(session, url, *, prefer=("Last-Modified", "ETag")) -> str \| None` | Cheap `SnapshotProbe` implementation: HEAD the card and return the first present preferred header, else `None`. EnergyVision prefers `ETag` first on its listing pages; everyone else prefers `Last-Modified` (`_pdf.py`). |
| `head_or_raise` | `head_or_raise(session, url, *, timeout=10) -> str \| None` | HEAD the URL and raise `ExtractorError` the way the fetch helpers do (`HTTP <status>` at 400 and above, `network error fetching` on a client error or timeout), so `is_transient_fetch_error` can tell a card that is not there from a supplier that is down. Returns the answer's `Last-Modified`, or `None` when it sends none, for a lookup that has to tell one upload of a file from a later one. For a lookup that probes before it downloads (Eneco's five volumes a month). `head_ok` is the `bool` form of it for `discover()` paths (`_pdf.py`). |

Internals worth knowing:

- `_pdf_text` (`_pdf.py`) sits between the three `fetch_pdf_text*` variants and
  the download, holding the one thing they share besides the bytes: an opt-in
  memo. `memoise_text_fetches` covers `fetch_text` and the PDF readers alike,
  so a block that will read the same document twice reads it once. That is the
  compare sweep: it already collapsed the per-supplier listing GETs, and where
  two products share one card it now also skips the second parse. On a tariff
  card the parse is the expensive half, not the download (a 2,4 MB Bolt
  variable card is about 38 s through pdfplumber on a Pi against well under a
  second to fetch), and Bolt sells each of its four variable cards on both
  settlements. What is memoised is the extracted TEXT, a few kB, not the
  payload; the key carries the variant (and the aligned threshold) because the
  same document read two ways is two different strings and handing one back for
  the other would feed a parser a shape it has no regexes for.
- `_fetch_validated_pdf_bytes` (`_pdf.py`) is shared by the three
  `fetch_pdf_text*` variants. It catches both `aiohttp.ClientError` and
  `TimeoutError` (aiohttp's `ClientTimeout` fires `asyncio.TimeoutError`, which
  is not a `ClientError`) and wraps them with the exact prefix
  `network error fetching`. That prefix is load-bearing: `is_transient_fetch_error`
  keys on it. HTTP >= 400 is wrapped as `HTTP <status> fetching <url>`.
- `error_text` (`_pdf.py`) renders the wrapped exception as `str(err)` or, when
  that is empty, its class name. aiohttp raises its timeouts argless, so without
  it the message ended in a bare colon -- and that message is user-visible on the
  `snapshot_stale` Repairs card, the `last_error` sensor attribute, and in
  diagnostics. The ENTSO-E client uses it for the same reason (`api.py`).
- `_is_pdf_payload` (`_pdf.py`) validates by magic bytes (`%PDF`, allowing a
  leading UTF-8 BOM that OCTA+ prepends and blank space after it, which some
  servers emit), not Content-Type, because some CDNs return 200 + text/html for
  a missing PDF, and Engie's API returns octet-stream for valid PDFs.
  Tolerating the prefix is only half the job, so `_fetch_validated_pdf_bytes`
  and OCTA+'s archive **strip it** (`_strip_pdf_prefix`) before returning: those
  bytes still have to parse, and pdfplumber cannot read them -- it fails such a
  file with `No /Root object! - Is this really a PDF?`, which reads like a
  corrupt card rather than a few stray bytes. pypdf recovers on its own, so this
  is what keeps the `aligned` and `layout` variants working on such a card.
- `_storage_error_code` (`_pdf.py`) splits one case back out of that
  catch-all. An object store refusing the read answers the proxy in front of it
  with its own XML error document, which the proxy passes through as a 200, so
  the payload is not a PDF and used to report as a permanent parse failure.
  Azure Blob and S3 both shape it as a root `<Error>` carrying a `<Code>`; the
  match is anchored at the start of the payload so an HTML page that merely
  mentions such a block is not caught. A hit is wrapped as
  `storage error fetching <url>: <Code>` and classifies as transient, because no
  code change can fix it. Luminus switched anonymous access off on the storage
  account behind `api-next/get-pricelist` on 2026-08-10 and every card came back
  as a 248-byte `PublicAccessNotPermitted` document, which raised the
  `extractor_failed` card on every Luminus entry, asking users to report a layout
  change that had not happened.
- `read_capped` (`_pdf.py`) reads every body, card or page, and refuses one
  past `MAX_RESPONSE_BYTES` (64 MiB, about 12x the largest real card), bounding
  what a broken or hostile server can pull into coordinator memory. The cap
  holds on the bytes as they stream, not only on a declared Content-Length,
  which a chunked answer leaves out and a server can lie about.
  `read_text_capped` decodes a page under the same cap, in the charset it
  declares or UTF-8, replacing what does not decode so a mislabelled page is
  refused by its parser as an `ExtractorError` rather than raised as a
  `UnicodeDecodeError`. The ENTSO-E and energy-charts answers, the Brugel
  sheets, the CREG rate file and Synergrid's download page are read through
  them too.
- The decode bound (`_pdf_bound.py`, pdfminer's wrappers in
  `_pdf_bound_pdfminer.py`) holds what a card's streams may decode
  to, counted on each reader as it reads the card. `read_capped` bounds the
  download, not what it inflates to. pypdf limits each stream (from 6.0.0) but
  not what a whole document decodes to, and pdfminer decompresses with no
  limit at all, both in a worker thread the fetch timeout cannot cancel: a
  20 kB file inflating to 20 MB took 26 s on a Raspberry Pi, and one of 200 kB
  ran away. The total is `_MAX_DECODED_STREAM_BYTES` (32 MiB), and each reader
  is held to it while it reads the card, `bounded_pypdf` around the plain
  reader, from opening the file, and `bounded_pdfminer` around the layout and
  aligned ones: every read of a stream counts, each time it is read, so a
  content stream counts once for each page drawing it, a form once for each
  time it is drawn and a ToUnicode map or font file each time a font is loaded
  from it, since the readers interpret each again (a 2,7 kB file whose one
  stream served 16 pages kept pdfplumber busy for 100 s); each inflate counts,
  every step of a stream inflated more than once, and is held to what is left
  of the bound before it is held, the cross-reference and object streams the
  readers inflate opening the file included; and a stream counts what its
  decoding hands back past its last inflate. A check made beforehand, on a
  reading of the file, missed what the readers do: pypdf counted nothing for a
  page's content written into the page (a 175 kB file decoded 60 MB), a form
  once however often it was drawn (a 934 B file took 77 s) and a ToUnicode map
  pages share once, and pdfminer finds pages and objects its own way (a page
  tree only one reader walks, two cross-reference tables, a font file tagged
  as an image, a filter after DCTDecode, a predictor between two inflates),
  each letting it decode 40 MiB and more uncounted. pypdf also loads every font
  a page or form names each time it is drawn, reading widths and encodings no
  stream holds (a 3 kB file naming one font 200 times in a form drawn 200
  times took 10,8 s), so a font load counts `_FONT_LOAD_BYTES` (1 kB) and
  pypdf loads the same dictionary to a font once a read. pdfminer keeps a
  font it finds by reference, but built one written into the resources
  again for each page or form drawn, its widths with it (100 000 widths
  drawn 400 times took 46,7 s), so it too builds a dictionary into a font
  once a read, and the font it made for each name the resources lack
  (naming one cost 146 us a token on a Raspberry Pi) once a read too. A
  damaged FlateDecode
  stream, cut short or failing its checksum, counts again once for every
  `_DAMAGED_FLATE_UNIT` (16 kB) of the stream under pdfminer, which inflates
  one a byte at a time in a time quadratic in its size. pypdf reads one cut
  short at once, but goes on past a failure, or a gzip header, a byte at a
  time to the end of the stream, so there such a one counts again, its
  length as well. The costliest such stream in the archive, on Bolt's
  professional Plenty fixed card of June 2026, takes 12,6 s of CPU on a
  Raspberry Pi, and under the bound one can cost 52 to 105 s, where before
  it was unbounded. Across the shipped fixtures pdfminer counts at most 9,9
  MB and pypdf 7,4 MB, and across the 2468 cards the archive held on
  2026-10-14 the most is 16,6 MB, pdfminer reading that Bolt card. LZW, run
  length and CCITT fax are refused while a card is read, and JBIG2, which
  pypdf hands to an outside program, under pypdf: no reader decodes an image
  to read text, pdfminer decodes the first three in pure Python with no
  limit (400 bytes of CCITT took 3,4 s), and no card carries one. An
  ASCII85 stream over `_MAX_ASCII85_BYTES` (1 MiB) is refused on both,
  whatever filter made it, since decoding it takes
  about 90 bytes of memory for each (16 MiB of it peaked at 1,7 GiB) and the
  largest a card carries is 2,2 kB. An RC4 decryption, which pdfminer runs a
  byte at a time (1 MiB took 365 s), counts its length times itself over
  `_RC4_UNIT` (16 kB) before it runs, as a damaged inflate does; every
  encrypted card is AES. A PNG predictor whose row is wider than both its
  stream and 16 kB is refused under pdfminer, which holds a row of that width
  before it reads any (64 Mi columns took 581 MiB). What pdfminer lays out
  and holds is held too, since it grows with what the bytes draw rather than
  with the bytes: a 6,5 kB card drawing 488 000 glyphs on one page held
  1,1 GB, pdfplumber's pass dropping duplicate glyphs took 303 s for 40 000
  on a page, 2 MB of saved graphics states held 552 MB, 400 kB of operands
  left waiting took 157 s, and 20 000 empty pages 302 s. A card lists at
  most `_MAX_PAGES` (50) pages, on both readers; pdfminer builds at most
  `_MAX_PAGE_OBJECTS` (120 000) glyphs, forms and images on a page and
  `_MAX_CARD_OBJECTS` (180 000) on the card; no stack of its interpreter or
  content parser holds more than `_MAX_STACK` (5 000) at once, arrays and
  dictionaries nest at most `_MAX_NESTING` (20) deep, and a path not yet
  painted has at most `_MAX_PATH_SEGMENTS` (450 000) segments. Each is about
  ten times the most any shipped fixture or any of the 2488 cards the
  archive held on 2026-10-15 reaches: 5 pages, 11 213 objects on a page of
  a Luminus card and 17 374 on a Cociter card, 407 on a stack, 2 deep and
  42 987 segments on a page of a Bolt card; a page of 119 000 glyphs holds
  437 MB and takes 56 s on a Raspberry Pi. No path is painted while a card
  is read, its text being read off its glyphs alone (a page of a Bolt card
  held 123 000 segments), pdfplumber lets go of each page once read rather
  than with the document, and its `dedupe_chars` finds each glyph it keeps
  at once rather than by looking it up in the page's list. What a reader
  parses is held too, counted as it parses, since what the bytes draw costs
  even when it builds nothing: 78 kB inflating to 32 MB of numbers no
  operator takes held pypdf 238 s and 1,2 GB, and 3 MB of paths pdfminer
  does not paint took 45 s. A reader parses at most `_MAX_PARSED`
  (3 000 000) objects of a card: under pdfminer each token but a comment
  of the file, of a content stream for each page drawing it and of a form
  for each draw, and of a font's maps and programs, and each font, colour
  space, form or image and procedure set of the resources it walks for a
  page or a form drawn, the kinds it reads of them, each reference it
  resolves (a 700-byte card naming an object that refers to itself had it
  resolve that for good) and each line it reads backwards from the end of
  the file (2 MB of line ends there took 4,7 s); under pypdf each operand
  and operator of a content stream each time it is interpreted, and each
  element of an array or dictionary. The most a card reaches is 935 796
  under pdfminer and 912 313 under pypdf, both on a Bolt card of 2,8 MB,
  and this is
  about three times that, since what it lets through is CPU: a token of a
  Bolt card costs pdfminer about 40 us on a Raspberry Pi, and of 30 kinds
  of operator made up to cost more none costs over a twentieth more but
  one showing a glyph for every four tokens, half as much again, which the
  object bound holds to 180 000 glyphs, so the most it lets through takes
  about 140 s.
  A comment counts nothing: pdfminer passes over one in a time linear in
  its length, the decode bound and the download cap holding how many
  bytes it passes over. A token pdfminer gathers, a comment included, and
  the data of an inline image count once at most but are grown a byte at a
  time, in a time quadratic in their length (1 MB of inline image data
  took 352 s on a Raspberry Pi), so pdfminer gathers none longer than
  `_MAX_TOKEN_BYTES` (16 kB), checked each time it reads through its
  buffer. The longest any card reaches is 1815 bytes, on a TotalEnergies
  card, and no card carries an inline image; 8 MB of strings 16 kB long
  took 29 s on a Raspberry Pi, where the same bytes in strings of 1 kB took
  22 s. pypdf gathers both into growing buffers, in a time linear in their
  length. pdfminer grows a line a buffer at a time the same way, reading
  it forwards as it looks for the end of a stream whose length is wrong
  or for the objects of a file whose table is broken (a stream saying it
  is empty carrying 16 MB with no line end took 47 s), so a line is held
  to `_MAX_TOKEN_BYTES` too, the longest a card reaches being 140 bytes;
  the lines it reads backwards from the end of a file, looking for its
  table, which it grew the same way (16 MB with no line end after its end
  took 46,8 s), are found in the file whole, in a time linear in it.
  pdfminer takes at most `_MAX_STEPS` (8 000 000) steps, a step each time a parser looks at its
  buffer again: most make or end a token, but a NUL byte, a lone `>`,
  each escape in a string or a name and each line a parser reads take one
  each, which the parse count does not see
  (2 MB of NUL bytes took 7,5 s on a Raspberry Pi, in the file itself held
  only by the download cap). The most a card takes is 2 612 279, on a Bolt
  card of 2,8 MB, and this is about three times that: a step costs at most
  about 8,5 us, a line of a stream read to its end, so the most it lets
  through take about 68 s. pypdf reads at most `_MAX_LINES_BACK` (4096) lines
  backwards from the end of a file, looking for its end marker and the
  pointer to its table, a block of 8 kB for each (a file of short lines
  with no end marker took about 9 s a MB on a Raspberry Pi). A card reads
  4 at most, and pypdf itself reads up to 1000 more recovering a broken
  pointer past up to 1000 end markers repeated; 4096 take about 70 ms. pdfminer
  loads at most `_MAX_CMAPS` (4) of the CMaps it ships for a card, each a
  font's encoding or a map it names, counted by name whether it loaded it
  for this card or an earlier one: a load costs up to 7,9 MB, the
  ToUnicode map of Adobe-GB1, and 0,15 s, and a 3 kB card naming all 144
  took 2,9 s and held 135 MB. pdfminer keeps each it loads for good, under
  the name the card gives, which it reads the same file for whatever NUL
  bytes or path it carries, so what a read added to its caches is dropped
  once the read ends. No card names one, every font of theirs naming an identity
  encoding pdfminer makes without loading anything. pdfminer makes each
  name and keyword it reads once and keeps it for good, so that one read
  twice is the same object, which it compares them by: each card naming
  new ones grew its tables, 100 000 of them by about 20 MB, refused or
  not, and 2,9 million by about 500 MB. While a card is read, one its tables lack
  is made once for the read and kept with it rather than in the tables,
  which hold only what pdfminer made importing its modules, and it makes
  at most `_MAX_SYMBOLS` (10 000) for a card. The most a card makes is
  893, all but 35 of them names, on energie.be's dynamic card for Flanders
  of September 2026, and at most 17 are named by no other card; each held
  costs about 200 bytes. A reader builds at most `_MAX_FONT_ENTRIES`
  (1 000 000) entries of a card's fonts, each code a range of a font's
  ToUnicode map, encoding or widths gives, or its program's own tables,
  counted before the range is gone through, and under pypdf each code of
  a ToUnicode map once it is built; a code listed on its own is a token
  parsed. A range takes a line, and
  2,8 kB mapping 2^32 codes ran pdfminer out of memory after 52 s, while
  pypdf held 970 MB for 48 fonts of the 100 000 codes it maps at most for
  one. The most a card reaches is 196 659 under pdfminer and 198 676 under
  pypdf, on Energy Knights' cards mapping three fonts of 65 536 codes
  each, and this is about five times that, pypdf holding about 180 bytes a
  code. A refusal ends the read where it is made, raised as an error no
  reader catches: pypdf
  catches any error a form raises and goes on to its next draw, logging
  each, 99 984 lines for one file. pypdf reaching one of its own limits is
  refused too, under pypdf's own words for it. The readers are wrapped
  once per process: a second copy of the two modules, as the live check
  loads under a package of its own, finds the wrappers in place by a
  marker they carry and reads through them, held to its own bound, where
  it wrapped them again and had each read counted once for every copy.
  The checks only count, and what they change (a font a reader builds once,
  the paths pdfminer no longer paints, a page let go of once read, a glyph
  found at once, a name made for the read alone) leaves the text as it was, so a card under the bound
  renders to the same text as before, and only these refuse: a file a
  reader cannot open is left to it to fail on.
  The manifest asks for pypdf 6.7.1 or later, the first release that maps
  at most 100 000 codes for a font, which the bound counts once the map is
  built; 6.6.0 was the first whose text extraction loads its fonts through
  `Font.from_font_resource`, which the bound counts too. pypdf 6.20.0 moved
  its font module into its `generic` package, and the bound takes it from
  either place.
- `_require_https` (`_pdf.py`) refuses to fetch a card or page whose address
  is not https, before any request. Most links are read off a supplier's page
  and only energie.be checked their scheme, so a plain http link was fetched
  and nothing could tell a redirect from it to a LAN address from the
  supplier's own answer. Every card, listing and API the extractors read is
  served over https. The HEAD probes (`head_freshness_key`, `head_or_raise`,
  `head_ok`) are held to it and to `guard_redirect` too: Aspiravi probes the
  links its listing prints. `head_freshness_key` answers `None` there, no
  signal, `head_or_raise` raises an `ExtractorError` and `head_ok` answers
  `False`.
- `guard_redirect` (`_pdf.py`) refuses a card or page that a redirect carried
  off https, whatever scheme the chain started on. aiohttp follows a redirect
  wherever it points, plain http included, and from there to an address only
  the Home Assistant host can reach, whose first bytes would then land in
  `last_error`. The redirect is not held to the requested site,
  since a supplier legitimately leaves it: energie.be's document API on
  azurewebsites.net redirects to its storage on blob.core.windows.net. Every
  redirect the live cards, listings and APIs answered on 2026-10-08 stayed on
  https. The target goes to the debug log only.
- `is_transient_fetch_error(message: str) -> bool` (`_pdf.py`) classifies an
  `ExtractorError` message: `network error fetching` and `storage error fetching`
  are always transient; among HTTP statuses, 5xx plus 408/429/403 are transient
  (Cloudflare-fronted suppliers intermittently answer with a 403 anti-bot
  challenge or a retryable 429), while 404/410 are permanent (card renamed or
  withdrawn) and must fail fast. A message opening with `OUT_OF_STEP` is
  transient too: a card refused because the card it is read with came from
  another month (Bolt Plenty Online and the Online card), which the next
  fetch pairs again.

### Text extraction strategies

Three extraction strategies exist because Belgian cards render tables three
incompatible ways. All three fail loud (raise `ExtractorError`) when a PDF has
pages but no decodable text, rather than returning blank text that every
downstream regex would miss silently.

| Symbol | Signature (sync) | Solves |
| --- | --- | --- |
| `extract_pdf_text` | `extract_pdf_text(payload: bytes) -> str` | Default pypdf extraction. Logs and skips pages pypdf returns `None` for (undecodable fonts); raises only if every page fails (`_pdf.py`). |
| `extract_pdf_text_layout` | `extract_pdf_text_layout(payload: bytes) -> str` | pdfplumber's default `extract_text()`, which rebuilds each row from glyph coordinates (it is not called with `layout=True`), for cards with rotated DSO/tax columns that pypdf drops (TotalEnergies). Runs `dedupe_chars()` first to drop stacked duplicate glyphs (for example `55,,09` rendered instead of `5,09`) (`_pdf.py`). |
| `extract_pdf_text_aligned` | `extract_pdf_text_aligned(payload, y_tolerance=3, x_join_threshold=0.0) -> str` | Re-groups `extract_words()` output into visual rows by y-coordinate for column-major cards (OCTA+). `x_join_threshold` is opt-in: leave `0.0` to keep words separate; pass ~1.0pt to glue sub-point-gap glyphs (`5 ,0 3 2 9` into `5,0329`). Pages joined with form-feeds (`_pdf.py`). |

### Number, sign and VAT parsing

| Symbol | Signature (sync) | Solves |
| --- | --- | --- |
| `to_float` | `to_float(text: str) -> float` | Parse a Belgian/French decimal (`15,93` or `0.102`). Strips every Unicode space variant used as a thousands separator (NBSP, thin space, NNBSP, line separator) before swapping comma for dot, so `5 029` does not raise (`_parse.py`). |
| `parse_sign` | `parse_sign(char: str) -> float` | Return `-1.0` for any hyphen/dash/Unicode-minus, `+1.0` otherwise. Use as `base = parse_sign(m.group(N)) * to_float(m.group(N+1))` so a card that swaps to U+2212 or flips polarity does not silently break the parser (`_parse.py`). |
| `SIGN_CHARS` | module constant | Character-class string `+\-` plus six dash variants, to drop into a regex as `[` + `SIGN_CHARS` + `]` (`_parse.py`). Supplier PDFs flip silently between these on re-renders. |
| `fold_accents` | `fold_accents(text: str) -> str` | Lowercase and strip Latin diacritics, so a literal test for `août` still matches an extraction that lost the accent to `aout`. Fold both haystack and needle (`_parse.py`). |
| `vat_multiplier` | `vat_multiplier(text, *patterns, default=1.06) -> float` | Read the VAT percentage from a card header (each supplier phrases it differently) and return `1 + N/100` via `to_float` (so `21,5%` works). Falls back to `default` (1.06, illustrative current Belgian residential rate) when no pattern matches (`_pdf.py`). |
| `printed_vat_rate` | `printed_vat_rate(text, *patterns) -> float \| None` | The same read without the fallback: the stated rate as a fraction, or `None` (`_pdf.py`). |
| `with_vat_basis` | `with_vat_basis(snapshot, printed, *, professional=False, grossed=None) -> SupplierSnapshot` | Record `card_vat_rate` and `assumed_vat_rate` on the snapshot (`base.py`, through `_rates.vat_basis`). `grossed` says whether the parser grosses this leg's formula by the stated rate; it defaults to whether the leg has formula coefficients (`_rates.vat_grossed_fields`), and a parser passes `False` for a formula printed VAT-inclusive or carrying its own multiplier. |

### Belgium-specific table and date parsers

| Symbol | Signature (sync) | Solves |
| --- | --- | --- |
| `parse_sibelga_row` | `parse_sibelga_row(text: str) -> DsoOverlay \| None` | Build the Brussels overlay from the eight-column Sibelga row, which Engie's French cards and EnergyVision's Dutch Brusol cards print identically: distribution Normal/Pleines/Creuses/Excl Nuit, metering fee, power term at or below 13 kVA, power term above it, transport. Folds the metering fee into each power band, since a connection pays both, and calls `parse_brussels_osp` for the OSP table. `None` when the row is absent, leaving the caller to decide whether that is a card without a Brussels table or a drift to refuse. Bolt, Mega and TotalEnergies print other layouts and keep their own readers (`_parse.py`). |
| `parse_brussels_osp` | `parse_brussels_osp(text: str) -> dict[str, float] \| None` | Parse the Brussels Brugel OSP annual-fee table off a Sibelga card. Anchors on the public-service-obligation block in either language (`Obligations de Service` or the Dutch `Taks openbare dienstverplichtingen`; case-insensitive, since Bolt lowercases the `s`) and each `<bound> kVA <value>` row, returning every band the card prints (`le1_44` through `gt56`) or `None` when absent. The open-ended top row is told apart by the `>` in front of its bound, since `> 36 et <= 56 kVA` and `> 56 kVA` both end in `56 kVA`. Populates `DsoOverlay.brussels_osp_by_tier` (`_parse.py`). |
| `require_every_area` | `require_every_area(supplier, out, keys, *, block=None) -> None` | Raise `ExtractorError` naming the areas a parsed network table lacks. A partial table is worse than none: a card adopted without one area leaves every entry on it failing each tick while the last good card is gone from the cache, and an empty one lets the backfill skip whole months. Aspiravi, Cociter, EBEM, Elegant, Eneco and OCTA+ ask it in `fetch` alone, so a past month's card keeps a short table: its energy is still what the month and a signing cohort are billed, and the month cache bills the missing area on the nearest card of the same calendar year that prints it, never another year's, whose network tariffs differ (`_with_nearest_area`, `snapshot_areas.py`). Ecofix and Ecopower ask it in their readers (`_parse.py`). |
| `running_month_short_of_an_area` | `running_month_short_of_an_area(out, keys, year_month) -> bool` | Whether `year_month` is still running and its card's network table lacks one of `keys`. The month walk reads the running month through `fetch_for_month`, which is the live card again: Aspiravi, Cociter, EBEM, Elegant, Eneco and OCTA+ answer `None` on it, so the current card stands in, as it does when `fetch` refuses the same card. A closed month keeps its short table (`_parse.py`). |
| `parse_valid_until` | `parse_valid_until(text: str) -> date \| None` | Best-effort parse of the card's validity date, anchored within ~200 chars after a validity keyword (`geldig`/`valable`/`validit`/`valid `). Tries spelled-out `<day> <month> <year>`, numeric `DD/MM/YYYY` (or `DD/MM/YY`), then bare `<month> <year>` (last day of month). Clamps candidates to a symmetric 5-year horizon around Brussels-local today so a corrupted footer date does not produce a bogus year. Returns the latest match or `None`. Populates `SupplierSnapshot.valid_until` (`_validity.py`). |
| `text_mentions_month` | `text_mentions_month(text, year_month: date, month_names: tuple[str, ...]) -> bool` | Heuristic that `text` references the requested month+year inside an anchored window (first 1000 chars where the card title prints, plus validity-keyword windows). Accent-folds both sides and collapses whitespace so `mei\n2026` matches (`_validity.py`). |
| `archive_validity_check` | `archive_validity_check(snap, text, year_month, *, month_names=None) -> SupplierSnapshot \| None` | Confirm an archived snapshot actually covers `year_month`. Returns `snap` on pass, `None` otherwise, so a provider's `fetch_for_month` can fall back to the proxy rather than mis-bill. Two tiers: authoritative `snap.valid_until` month check when present; otherwise require a textual month mention (when `month_names` given, as eneco/cociter do) or accept on the URL resolver alone (when `None`, as ebem does) (`_validity.py`). |

Two supporting internals back these date parsers: `_MONTH_NAMES` (`_pdf.py`)
maps Dutch, French (with and without accents) and English month names to their
1-12 index, and `_validity_windows` (`_validity.py`) returns the ~200-char
context after each validity keyword so a retrospective month mention elsewhere
in the PDF does not masquerade as a validity statement. `_OSP_BOUND_TO_TIER`
(`_parse.py`) maps kVA upper bounds to the shared tier keys and is kept as
literals so this low-level helper stays decoupled from `const.py` (the keys must
match `const.CONNECTION_KVA_TIER_*`).

## How to implement a new provider

Grounded in the protocol above, a minimal new PDF provider looks like this:

1. Create `providers/<supplier>.py`.
2. Declare the products as `Contract` instances, one per product, with the right
   `kind`. Set `regions` if the product is not sold in all three regions; set
   `spot_indexed_injection=True` whenever the energy leg fetches no spots but the
   feed-in is index-linked, whether the index is per-hour (Cociter Variable) or a
   delivery-month mean (most of the static range).
3. Implement `async def fetch(session, contract_id, region) -> SupplierSnapshot`.
   Use the `_pdf.py` helpers: `fetch_pdf_text` (or the layout/aligned variants
   for rotated or column-major cards), `to_float` / `parse_sign` / `SIGN_CHARS`
   for numbers, `vat_multiplier` for VAT, `parse_valid_until` for `valid_until`,
   `parse_brussels_osp` for a Sibelga card. Build one `DsoOverlay` per DSO
   sub-area the card covers, keyed by the canonical `const.py` DSO keys. Raise
   `ExtractorError` on any parse failure; never invent a EUR value.
4. Populate `TaxOverlay`. Remember `vat_rate=0.0` means "prices are already
   VAT-incl"; only set a non-zero rate if the card ships ex-VAT numbers. Return
   the snapshot through `with_vat_basis(snapshot, printed_vat_rate(text,
   <your pattern>))`, reading the rate with the same pattern the formula is
   grossed by, so the archive can build its VAT consensus from the card and a
   formula grossed on an assumed rate is marked as such.
5. Populate `injection` (`InjectionRates`) if the contract has feed-in. For a
   monthly-indexed injection emit the printed `current` beside the month formula's
   `factor`/`base` and set `month_indexed` or `spp_indexed`, whichever index the
   card names, so it is never priced per hour; emit `factor`/`base` without either
   flag only for a genuine hourly-spot formula; use the per-slot triplet only for a TOU contract
   whose feed-in varies by slot, adding the per-slot coefficient pairs and
   `month_indexed` when each slot is itself a monthly formula. Injection values
   are never VAT-incl.
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
    will exercise the real card daily via `all_extractors()`.

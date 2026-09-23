# CI and testing

This document covers how the Belgian Electricity Prices integration is tested and released: the
fixture-driven pytest suite (`tests/`), the daily live extractor harness
(`scripts/live_check.py`) that fetches every supplier's real tariff card and asserts the
extractors still parse, the daily card archiver (`scripts/archive_cards.py`) that stores what
every extractor parsed in the `be_price_cards` repository, the six GitHub Actions workflows (`test.yml`,
`validate.yml`, `live_check.yml`, `archive_cards.yml`, `endpoint_probe.yml`, `autorelease.yml`),
and the exact local commands a contributor runs before committing. It also spells out the
version-bump policy that gates a release.

Related docs:

- [architecture.md](architecture.md) - module map and end-to-end data flow the tests exercise.
- [provider-framework.md](provider-framework.md) - the `SupplierExtractor` protocol and
  dataclasses (`FixedRates`, `VariableRates`, `DynamicRates`, `TimeOfUseRates`, `ImpactRates`,
  `InjectionRates`, `DsoOverlay`, `TaxOverlay`) that both the suite and the live harness validate.
- [coordinator.md](coordinator.md) - the refresh lifecycle and cache the runtime tests drive.
- [pricing-model.md](pricing-model.md) - `compute_breakdown` and the tax/injection/capacity math
  that `test_pricing.py` covers.
- [data-sources.md](data-sources.md) - the ENTSO-E spot client and recorder backfill covered by
  `test_api.py` and `test_backfill.py`.
- Per-provider docs under [providers/](providers/) - each names the fixtures its extractor is
  tested against.

## The test suite

Tests live under `tests/` and run with `pytest`. Configuration is in `pyproject.toml`: pytest is
in `asyncio_mode = "auto"` with `asyncio_default_fixture_loop_scope = "function"`
(`pyproject.toml`), so `async def test_*` functions run without an explicit `@pytest.mark.asyncio`
decorator and each test gets a fresh event loop.

The integration is a Home Assistant custom component, so the suite depends on
`pytest-homeassistant-custom-component` (which supplies the `hass` fixture and `MockConfigEntry`)
and, for time-pinned tests, `pytest-freezer` (the `freezer` fixture). `tests/conftest.py`
inserts the repo root onto `sys.path` (`tests/conftest.py`) so
`custom_components.be_electricity_prices` imports without an installed package, and an autouse
fixture enables custom-component loading for every test that requests `hass`
(`tests/conftest.py`).

### Layout: one module per provider plus cross-cutting modules

There is roughly one test module per extractor, plus a set of modules covering the shared
machinery.

| Test module | Covers |
| --- | --- |
| `tests/test_bolt.py` | Bolt extractor (`providers/bolt.py`) |
| `tests/test_cociter.py` | Cociter extractor |
| `tests/test_dats24.py` | DATS 24 extractor |
| `tests/test_ebem.py` | EBEM extractor |
| `tests/test_ecofix.py` | Ecofix extractor |
| `tests/test_ecopower.py` | Ecopower extractor |
| `tests/test_eneco.py` | Eneco extractor |
| `tests/test_energiebe.py` | energie.be extractor |
| `tests/test_energyknights.py` | Energy Knights extractor |
| `tests/test_energyvision.py` | EnergyVision extractor |
| `tests/test_engie.py` | Engie extractor |
| `tests/test_frank.py` | Frank Energie extractor |
| `tests/test_luminus.py` | Luminus extractor |
| `tests/test_mega.py` | Mega extractor |
| `tests/test_octaplus.py` | OCTA+ extractor |
| `tests/test_totalenergies.py` | TotalEnergies extractor |
| `tests/test_trevion.py` | Trevion extractor |
| `tests/test_pricing.py` | `pricing.compute_breakdown` and its helpers (energy, network, taxes, TOU/offpeak, holidays, impact bands, meter fixed fee) |
| `tests/test_coordinator_runtime.py` | `BePricesCoordinator` force-refresh, stale-snapshot Repairs issue, shared caches |
| `tests/test_coordinator_helpers.py` | Coordinator helper functions in isolation |
| `tests/test_config_flow_energy_defaults.py` | Config-flow wizard defaults |
| `tests/test_options_flow.py` | Options-flow reconfiguration |
| `tests/test_sensor_helpers.py` | Sensor value/attribute helpers |
| `tests/test_binary_sensor.py` | Binary sensor (offpeak-window) entity |
| `tests/test_button.py` | Force-refresh button entity |
| `tests/test_api.py` | ENTSO-E spot client (`api.py`) |
| `tests/test_backfill.py` | Recorder cost-statistics backfill |
| `tests/test_diagnostics.py` | Diagnostics dump |
| `tests/test_discover.py` | Every supplier's `discover()` against a frozen listing snippet |
| `tests/test_window_service.py` | The offpeak/window service |
| `tests/test_pdf_helpers.py` | Shared PDF text extraction (`providers/_pdf.py`) |

Shared helpers live in `tests/__init__.py`:

- `make_snapshot(...)` builds a `SupplierSnapshot` with sensible defaults (a canonical Wallonia
  fixed-rate snapshot under ORES) so a pricing or coordinator test can override just the one field
  it cares about (`tests/__init__.py`).
- `fixture_text(name, *, layout=False)` reads a fixture PDF and runs it through the real
  extractor, `extract_pdf_text` (pypdf) by default or `extract_pdf_text_layout` (pdfplumber) when
  `layout=True` for the column-positional cards (Bolt, DATS 24, Ecopower, TotalEnergies)
  (`tests/__init__.py`). It is `lru_cache`d for the process lifetime because PDF extraction
  dominates suite runtime (the comment notes roughly 10s per fixture, and the cache cuts a full
  run from about 190s to about 30s). The cache is process-scoped, so if you rewrite a fixture
  mid-session call `fixture_text.cache_clear()` or restart pytest.

### The fixture-driven pattern

Extractor tests do not hit the network. Each supplier's real published card is captured once and
committed under `tests/fixtures/`:

- `tests/fixtures/*.pdf` are frozen copies of real tariff-card PDFs (for example
  `eneco_fix.pdf`, `bolt_variable.pdf`, `cociter_var_2604.pdf`, `frank_dynamic_slim_may.pdf`).
  Multiple dated fixtures per supplier capture format changes and historical-bug reproductions
  (for example the several `ecopower_burgerstroom_*` cards and `cociter_var_2512.pdf` versus
  `cociter_var_2604.pdf`).
- `tests/fixtures/discover/*.html` are frozen snippets of each supplier's listing page, one per
  supplier (`bolt.html`, `mega.html`, `octaplus.html`, and so on), used by `tests/test_discover.py`.
  A supplier that advertises its cards on more than one page gets one snippet per page:
  EnergyVision has `energyvision.html` for its own listing and `brusol_gs1800v.html` /
  `brusol_grs.html` for the Brussels products, which are published on another site entirely.

A provider test feeds the fixture text through the same parse path the coordinator uses and
asserts the resulting `SupplierSnapshot` (energy rates, per-DSO overlays, taxes, injection,
publication label). This is why the tests are deterministic and fast while the extractor logic is
still exercised end-to-end against genuine supplier output.

`tests/test_discover.py` drives each supplier's `discover()` against its saved listing snippet
through a minimal `aiohttp.ClientResponse` stand-in, `_FakeResponse` (`tests/test_discover.py`),
and asserts the discovered product set matches the registry exactly. A regex regression that drops
a product, or a fixture refresh that grows the catalogue, fails fast.

### Europe/Brussels timezone pin

`tests/conftest.py` has an autouse fixture `_force_brussels_timezone` that pins every test to
`Europe/Brussels` (`tests/conftest.py`). This is load-bearing, not cosmetic. The
`pytest-homeassistant-custom-component` `hass` fixture defaults to `US/Pacific`; for a
Belgian-electricity integration that default hides DST transitions, off-peak window boundaries,
and per-month archive bugs that would surface in production. The fixture has two branches:

- If the test requests `hass`, it sets the timezone on the running Home Assistant instance via
  `hass.config.async_set_time_zone("Europe/Brussels")` on the loop `hass` owns
  (`tests/conftest.py`).
- Otherwise it swaps `homeassistant.util.dt`'s default timezone for the duration of the test and
  restores it afterwards (`tests/conftest.py`).

The fixture stays synchronous on purpose: pytest-asyncio auto mode would otherwise wrap an async
autouse fixture in a second `asyncio.Runner` that cannot run while the `hass` loop is already up.
The comment at `tests/conftest.py` documents this constraint. When you write time-sensitive
tests (offpeak windows, YTD backfill boundaries, monthly archive keys), assume Brussels local
time and do not reintroduce a US default.

### `tests/recorder/`: tests that need a real recorder database

Both autouse fixtures above resolve `hass`, and `recorder_mock` cannot run under them: its
`recorder_db_url` dependency asserts `hass_fixture_setup` is still empty, i.e. the database must
be built *before* `hass` exists. `tests/recorder/conftest.py` therefore overrides both by name,
as no-ops, for that directory only.

Do **not** instead make the parent fixtures resolve lazily via `getfixturevalue`. That was tried:
it changes fixture setup order so `hass` is no longer in `request.fixturenames` when the timezone
pin runs, which silently unpinned Brussels and broke six slot/DST tests. Keeping the override
local leaves the rest of the suite byte-identical.

Almost every backfill test mocks the recorder (`tests/test_backfill.py` patches
`recorder.get_instance`), which is right when the assertion is about *what we write*. Use
`tests/recorder/` only when the assertion is about *how HA compiles what we wrote* — currently
just the backfill→live `sum` seam, which is invisible to a mock.

### mypy test-stub convention

Production code is type-checked with `mypy --strict`, but tests and helper scripts are checked
non-strict (see the workflows below). Several entity and coordinator tests substitute a
`types.SimpleNamespace` for the real coordinator or config entry when only a couple of attributes
are read (for example `entry.runtime_data = SimpleNamespace(data=...)` in
`tests/test_diagnostics.py`). That stub does not match the production signature, so the call
site is annotated with `# type: ignore[arg-type]` to keep the non-strict mypy pass clean, as in
`tests/test_button.py` and `tests/test_ecopower.py`. The convention is to suppress at the
call site with `# type: ignore[arg-type]`, never to relax the production function signature to
accept the stub. `pyproject.toml` sets `explicit_package_bases = true` (`pyproject.toml`) so
mypy treats `custom_components/be_electricity_prices` and `tests/` as separate package roots
(neither carries a root `__init__.py`), matching how pytest collects them, and it silences
missing stubs for `pypdf`, `pdfplumber`, `pyxlsb`, and `pytest_homeassistant_custom_component`
(`pyproject.toml`).

## scripts/doc_ref_check.py

The docs name files and the symbols inside them. They used to pin a line number as well, and a
line number is wrong the moment anything above it moves, which is most commits: 2630 of them
had to be re-derived on any branch that touched code, the sweep that did it went wrong more
than once in a single day, and a stale pin is worse than no pin because it sends a reader to a
line that now says something else. The numbers are gone. A file path and a symbol name are what
a reader greps for, and neither moves when a function grows.

What is left is what a RENAME breaks, which is the only way these can still rot:

| Check | Fails CI | Why |
| --- | --- | --- |
| a file a doc names does not exist | yes | the name is provably wrong: it was renamed or removed and the prose did not follow |
| a `file.md#anchor` names no heading | yes | same, for the pins into README the glossary carries |
| a markdown link names a `.md` file, or an anchor in one, that is not there | yes | the docs cross-link without backticks, so a renamed doc used to break every link into it while the check stayed green; resolved relative to the linking doc first, as a browser reads it |
| a symbol named beside a file that this tree does not define | no | printed for a human; most are prose words, Home Assistant's own names and service ids, and no rule separates those from a rename |

The third is deliberately not gated. Gating a count of it would put the docs back to needing an
edit whenever they grow, which is the thing this replaced.

It runs in the `test` job (`.github/workflows/test.yml`), before the suite, needs no network or
fixtures, and reports in under a second. `NOT_OURS` lists the names that are not files of this
repository and never will be: what a workflow writes into the cards repository or as a job
artefact, the framework doc's `providers/foo.py` placeholder, and Home Assistant's own
`sensor/recorder.py`. A doc naming one of those is correct, and resolving it is not the point.

## scripts/live_check.py

The test suite proves the extractors parse the frozen fixtures. It cannot prove that a supplier
has not silently reworked its live card. `scripts/live_check.py` closes that gap: it walks every
registered `(supplier, contract, region)` tuple, fetches the supplier's real publication over the
network, parses it, and asserts the resulting snapshot is structurally sane (energy populated,
expected DSO keys present, taxes populated, rates inside loose plausibility bounds). It prints a
markdown report to stdout and encodes the outcome in its exit code. It is run daily by
`.github/workflows/live_check.yml` (`scripts/live_check.py`).

The script deliberately does not import Home Assistant. `_load_providers()`
(`scripts/live_check.py`) synthesises a `be_pkg.providers` package and loads each provider
module by file path, so it can import `providers/*.py` and `providers/base.py` without pulling HA
into scope. It binds the rate classes (`FixedRates`, `VariableRates`, `DynamicRates`,
`TimeOfUseRates`, `ImpactRates`, `SpotMonthlyRates`) for the `isinstance`-based energy
validation (`scripts/live_check.py`), taking them from the one `providers/_rates.py` module that
`base` and every provider import, so class identity matches. The VREG ceiling reader comes from
`providers/_parse.py` the same way. `test_the_loader_binds_what_the_providers_use` runs the real
loader, because a module split that moves one of these names otherwise surfaces only as a
nightly run dying before its first card.

### Structure and main functions

```
main()                       scripts/live_check.py  asyncio.run(_run()); rc=8 on harness crash
  _run()                     scripts/live_check.py  load providers, gather checks, render, exit code
    _load_providers()        scripts/live_check.py    file-path import of every provider (no HA)
    _attributed_check(...)   scripts/live_check.py   per-supplier wait_for + trace attribution
      _check_eneco ...       scripts/live_check.py   one _check_<supplier> per registered extractor
      _check_frank                                       (cociter, dats24, ebem, ecofix, ecopower,
      _check_bolt                                         engie, luminus, mega, totalenergies,
      ...                                                 bolt, octaplus, frank, energiebe,
                                                          energyvision, energyknights)
    _check_catalogs(...)     scripts/live_check.py   run each discover(), flag new product ids
    _check_card_freshness()  scripts/live_check.py   resolved card == newest advertised
    _fetch_with_retry(...)   scripts/live_check.py   transient-only retry with backoff
    _validate_snapshot(...)  scripts/live_check.py  energy + injection shape gates
    _drift_warnings(...)     scripts/live_check.py  latency / byte budget checks
    _render_report(...)      scripts/live_check.py  markdown pass/fail report
```

### Rendering only what changed

With the cards repository checked out beside it (`--texts DIR`, `scripts/live_check.py`),
the harness installs the archive's texts as the readers' render cache (`scripts/card_texts.py`,
the same `StoredTexts` the archiver builds on): a card whose bytes the archive walked an hour
earlier and still holds is downloaded, timed, counted and parsed as before, but its text comes
from the archive instead of a pdfplumber pass. Only a card that changed since is rendered. The
report ends with how many cards were served that way and how many rendered, and the retry loop's
seven attempts on a bad morning no longer cost seven full renders. A fork's pull request has no
stored text to read and renders everything, which is the behaviour before the cache existed.

### Card freshness

`_check_card_freshness` (`scripts/live_check.py`) asks a question no other check here asks:
not "did the fetch work" but "is this the card the supplier is currently advertising". A superseded
card downloads, parses and validates exactly like a current one, so a stale URL reads as a green
run -- Bolt billed June's variable formula for ten weeks behind a passing board, and Ecopower served
January's tax block for eleven days after renaming its dynamic card to `YYYYMMDD`.

The mechanism is a deliberate asymmetry: `_expect_newest_card` (`scripts/live_check.py`) scans
the same listing page the extractor does, but with a **looser** pattern. When a supplier changes the
filename shape, the extractor's strict pattern stops seeing the new file and keeps resolving the old
one; the loose pattern still sees it, and the mismatch fails the run.

### The federal tax block

`_check_federal_tax_consensus` (`scripts/live_check.py`) asks the freshness question about a
figure rather than a file. The federal excise and the federal energy contribution are set by law,
so for one month and one region every residential card in the country carries the same pair. A
supplier whose generator renders the tax block from a template and does not update it therefore
prints last quarter's figures while parsing perfectly: healthy fetch, healthy parse, wrong money,
and nothing else here would see it.

It reads the card archive the run already clones rather than the network, so it costs nothing and
compares what installations are actually served; a fork's run has no archive and the check is
skipped. The month compared is the one most residential cards are filed under, not the
newest present: a card is filed under the month its label names and a label ahead of the
calendar is allowed, so one supplier publishing October's card on the 28th would otherwise move
the comparison to a month with a single row and silence every disagreement of the month everyone
is still on. Professional cards are measured separately, because a degressive excise legitimately
blends far below the residential rate -- every `_pro_` contract in the September 2026 archive
carries 0,01421 against the residential 0,04876. A tie is not reported: with two suppliers
disagreeing there is no consensus, and guessing which is right files issues against the correct card.

Measured on that archive when the check was added, fourteen residential suppliers agreed exactly
and three did not: Ecofix and TotalEnergies in every region they sell, Cociter in Wallonia, each
still printing the separate energy contribution the others had folded into the excise on
1 August 2026. It is the supplier's card that is wrong, so the check reports rather than corrects.

### The keyless day-ahead fallback

`_check_spot_fallback` (`scripts/live_check.py`) asks whether energy-charts still serves the
Belgian day-ahead. It is the one source that has to work on the day ENTSO-E does not, so leaving it
unexercised until then is how it rots unnoticed -- the same reasoning as the freshness gate above,
applied to a source rather than a card.

It checks shape, not values: a full local day of hourly slots and every price inside the publishable
band. The 23/24/25 tolerance is deliberate -- a DST boundary day is a real Belgian day, not a fault.

It exercises **our** `EnergyChartsClient`, not the endpoint alone, so an upstream that changes shape
under the parser fails here rather than silently at a user's next ENTSO-E outage. That client is
reached through the same synthetic `be_pkg` loader every other integration module uses
(`_load("be_pkg.api", ...)`): a plain `import custom_components...` raises `ModuleNotFoundError` in
this script, and the first version of this check did exactly that and never ran. Because it was also
folded into the freshness `try` at the time, the crash was filed as `_freshness: probe crashed`,
naming a check that had in fact completed -- so it now has its own `try` and reports under its own
label. Loading `api.py` is why the workflow installs `defusedxml`, which that module imports at
import time.

Its rows are extractor rows, as a card fetch's are, so an outage is retried and only one that lasts
the whole retry loop is filed, under the extractor issue. They were catalog rows at first: never
retried, and filed on the first failure as "new supplier products detected". A phase of the run
that crashes, this one or the catalog, federal or freshness phase, is an extractor row for the same
reason, and because a crash is a bug here that a pull request should fail on.

The window is anchored on `ZoneInfo("Europe/Brussels")` explicitly rather than through
`dt_util.start_of_local_day()`. Run standalone, this script has no Home Assistant instance and
`dt_util`'s default time zone is **UTC**, so asking it for local midnight slices 22 hours out of the
middle of two Belgian days -- which is exactly what the check reported the first time it ran. Every
other date-sensitive spot in this script pins Brussels the same way, for the same reason.

### Which suppliers are covered, and which deliberately are not

The gate covers the **eight supplier-families that pick a card from a set of several advertised
ones** -- Bolt, Cociter (variable + trihoraire + dynamic), EBEM, Ecopower (definitive + dynamic),
Eneco, EnergyVision and Frank (one row per product code, and for EnergyVision per region too,
since its Brussels cards are advertised on a different site), and Mega. That shape is the one
that can silently resolve an older card, because the older card is still there and still parses.
The row count is deliberately not written down here: it is one row per advertised card and it
moves whenever a supplier adds a product or a region, which is not worth a doc edit to restate.

Engie, Luminus, OCTA+ and TotalEnergies get no freshness ROW, because each constructs one URL per
contract from static constants or a parameter-only API query: there is no candidate set to choose
wrongly from, so a wrong resolution 404s loudly and the extractor phase reports it. They are
covered instead by the card-period check below, which asks a different question.

### Asking the card itself (`_expect_card_period`)

The rows above ask whether a *newer* card exists somewhere. That question has no answer when the
supplier overwrites one fixed URL in place - and the failure it hides is real: a supplier that
simply stopped updating that file would serve a year-old card behind a green board forever.

So `_expect_card_period` asks the card instead, from the snapshot `_validate_snapshot` already
holds, at no extra fetch. Two assertions: `valid_until` must not have passed, and the publication
label must not name a month earlier than this one. A label *newer* than the current month passes -
publishing early is not staleness - and it runs for **every** supplier, not just those four, since
the mechanism is the same everywhere.

Measured across all 251 contract-regions before it was enabled: 202 of the 206 non-exempt ones
already carried a current-month label, and the four that did not were the Bolt bug this whole gate
was built for. So this check would have caught the original defect directly.

Where some lag is legitimate, `_PERIOD_MAX_LAG_MONTHS` records **how much**, and that is a ceiling
rather than a skip. Ecopower's *definitive* card publishes in arrears, landing at the end of the
month it covers, so through August the newest definitive card is July's: exactly one month, every
month, measured against a live page that carries `202604..202607` contiguously. Two months behind
is therefore not arrears - it is Ecopower having stopped - and still fails. A plain exemption would
have hidden that forever, which is the difference between "this supplier is allowed to be a month
behind" and "stop checking this supplier".

The entry is keyed on the **contract**, not the supplier, so exempting Ecopower wholesale is never
on the table: that would re-hide the bug fixed in 0.12.5.

The dynamic card is not held to the calendar at all, though, and a ceiling is the wrong instrument
for it. It is republished only when a rate changes, not monthly: the page carries four of them in
twenty months and the extractor says the same (*"Dynamic cards don't rotate monthly"*), so the same
card was the billing one for nine, three and seven consecutive months. Any ceiling honest enough to
cover that would be the skip the tests forbid. `_PERIOD_NO_ROTATION` names it instead, and its
freshness is asserted **against the listing**: `_expect_newest_listed_card` re-reads the page with a
pattern written independently of the extractor's and fails when the page carries a card newer than
the one resolved. That is strictly stronger than the calendar was against the failure it exists for.
0.12.5 was exactly that shape - the filename gained a day component, the extractor's six-digit
pattern stopped matching it, and the resolver fell back to a January card that downloaded and parsed
clean - and the listing check catches it while the calendar check only caught it by accident of
timing.

That allowance rests on a publishing convention rather than a date the supplier declares, so
nothing can expire it the way `deprecated_until` does. It carries `_PERIOD_LAG_REVIEW_BY` instead,
enforced by a **test** rather than at runtime: a runtime expiry would start failing on perfectly
normal arrears, which is a false alarm by construction. When that date passes, CI fails with an
instruction to re-verify how the supplier publishes and then move the date. Same convention as the
re-verify note in `providers/bolt.py`.

The expiry half of the check carries the same grace window as the lag half. Without it, on the 1st
of every month every card written to run to the end of the previous month failed `card has not
expired` before its supplier had published the new one, which is a monthly false alarm rather than a
signal (seven rows on 2026-09-01: Cociter, EnergyVision, Mega pro). The allowance is scoped to a card
that lapsed at the **end of last month**, so one that expired earlier keeps failing and cannot hide
behind the first five days.

A supplier on its way out is handled differently, and not by name. `_DEPRECATED_UNTIL` is bound
from each `EXTRACTOR.deprecated_until` at load, so the withdrawal date is declared once, on the
supplier, and this check reads it:

- **up to that date** the supplier is still selling and an older card proves nothing, so the check
  records no row at all;
- **after it** the supplier has left the market. Its final card stays up and stays stale forever,
  which is real and worth showing, but no change to this repository can fix it - so the row fails
  and is marked `expected`, which keeps it in the report without setting the extractor bit or
  refiling an issue every night. Exactly the treatment an unreadable card already gets.

That allowance used to cover the staleness question only, and everything else still filed. DATS 24
left residential on 2026-08-31 and its August card 404ed the next morning, which opened an
extractor-broken issue naming a supplier that no longer sells electricity (issue #78). `_record`
(`scripts/live_check.py`) now applies the marker to **any** failing check whose supplier is past
its date, so the rule lives in one place rather than at each of the six fetch call sites. It keys on
the supplier segment of the label, so a check with no supplier prefix is untouched, and the day of
the date itself still fails normally, since the supplier is trading until the end of it.

The report separates the two kinds of expected failure, because they need different explanations: a
page-image card can come back, a supplier that has left cannot. Folding a withdrawal under
"unreadable cards" would send the reader looking for a text layer that was never the problem.

The point of deriving it is that the allowance ends when the withdrawal does. A supplier name
listed in `_PERIOD_EXEMPT` would go on suppressing the check long after the reason for it expired,
and nobody would notice.

An unreadable label is reported but does not fail: unknown is not evidence of staleness. The label
parser is unicode-aware on purpose - a character class that forgets the `u` in `août` silently
fails to read 104 of the 236 live labels, and since an unreadable label is skipped, the check would
have covered almost nothing while looking green.

Mega's *professional* contracts get a **different** check, `_check_mega_professional`, because
they have no advertised set at all: Mega never links the B2B cards from any page, so there is no
"newest advertised" to compare against. The CDN answers for itself instead - a published month
returns `application/pdf`, an unpublished one a `text/html` stub under the same 200 - so the check
is one HEAD per (contract, region), downloading nothing.

What makes it worth checking is that `fetch` silently rolls back one month when the current card is
missing. On the professional contracts that are variable or dynamic, last month's card carries
last month's index: the prices are *wrong*, not merely old, and nothing else in the run would say
so.

Early in a month that rollback is correct behaviour rather than a defect, so the check only fails
past `_PRO_PUBLICATION_GRACE_DAYS`. Mega does not publish ahead - next month's URL is a stub today,
measured - so failing without that grace would file an issue every single month. Within the grace
the row passes and records how many cards are not yet up, which is the honest report: the fallback
is in use, and that is fine for now.

### The stamps do not sort

Every one of the eight formats fails a naive `max()`, each at a different boundary, so each has its
own key (`scripts/live_check.py` onwards): Mega's `MMYYYY` is month-major (`122026` outranks
`012027`), Eneco's issue is volume-major (an April re-issue `022604` outranks May's first issue
`012605`), EBEM's `MM-YYYY` sorts lexically wrong (`12-2025` over `08-2026`) and may be a Dutch
month *name*, Frank names its cards with a month word (`December 2026` over `April 2027`), and
EnergyVision's `MMYY` is month-major like Mega's. Left naive, most of these would call a fresh
January card stale every year end.

A stamp that does not fit the expected shape scores `_UNREADABLE_STAMP`, **above** every real one,
so an unknown ADVERTISED shape becomes the newest and fails the comparison. That sentinel must not
be reused for the SERVED side: there it would read as "newer than anything advertised" and pass. A
served URL the gate's own pattern cannot read fails the row instead.

### What an unreadable page means differs per supplier

It depends on how that supplier's resolver fails, and it is read out of the resolver, never assumed.
Most of them -- Cociter, EBEM, Ecopower, Eneco, EnergyVision, Frank, Mega -- **raise** when the page
will not load or carries no card, so their own extractor row already reports the breakage and this
gate records a pass rather than duplicating it. Bolt's `_resolve_variable_suffix` instead falls back
to `_VARIABLE_SUFFIX_FALLBACK`: the card still downloads, still parses, and `_check_bolt` stays
green while that constant quietly becomes the pin this whole gate exists to prevent. So for Bolt an
unreadable *or* reshaped listing is a **failure** (`resolver_falls_back=True`), and it is the only
signal that would report it. Two suppliers in this repo have lost or blocked their listing for
weeks, so this is not hypothetical; a transient blip still files nothing, because the workflow only
opens an issue for a check that fails every retry.

That rule governs whether an issue is filed; it now governs what the issue *shows* as well. The body
carries the **last** attempt's report, which on a slow runner also lists checks that failed only that
once, so a one-off supplier timeout used to sit in the failures table beside the real regression and
read as part of it. The table is filtered to the persistent set, and a footer states how many rows
were dropped so nothing disappears silently. The pass/fail counts in the report heading stay raw, and
the run log still holds every attempt.

Matching is case-**insensitive**. Every provider module compiles its card patterns with
`re.IGNORECASE`, so a case-sensitive gate is stricter than the extractor it audits in that one
dimension and goes blind exactly where the extractor still sees.

The gate catches only the exceptions a provider raises for a failed fetch. Anything else -- a
renamed symbol, a changed signature -- propagates and is recorded as a failure, because swallowing
those made the gate report green in exactly the case where it had stopped working.

Each `_check_<supplier>` derives its contract list from the runtime registry (for example
`for cid in (c.id for c in eneco.EXTRACTOR.contracts)`, `scripts/live_check.py`) so adding a
product to `EXTRACTOR.contracts` gets it validated here without editing the harness. Every check
asserts the publication label is non-empty, the expected DSO keys for the region are present
(`_FLUVIUS_KEYS`, `_WALLONIA_DSO_KEYS`, or `sibelga` for Brussels), the relevant taxes are
positive, and then calls `_validate_snapshot`.

The federal energy contribution is the exception to "taxes are positive". It is bounds-checked by
`_expect_energy_contribution` (`scripts/live_check.py`) instead, which accepts
`[0, 0.01]` EUR/kWh. A `> 0` gate on four suppliers used to enforce it, but the levy was abolished
on 2026-08-01: EBEM's August card failed CI three times over for reporting the zero it actually
prints (issue #49). The upper bound is what the gate was really protecting against — a unit slip
that reads the value 100x too large — and that part still holds.

`_validate_snapshot` (`scripts/live_check.py`) is the funnel every supplier check goes through.
Besides `_expect_card_period` and `_expect_energy_contribution`, described above, and
`_expect_welcome_credit` and `_validate_dsos`, whose bounds are listed under `_validate_energy`, it
runs these gates:

- `_expect_month_indexed_registry` holds the parsed energy's `month_indexed` against the
  registry's `Contract.month_indexed_energy`. The flow offers the optional ENTSO-E key from
  the registry flag before any card is fetched and the coordinator re-prices from the parsed
  one, so a card that gains or loses its monthly formula changes what the key buys, and this
  is where that shows; a contract the harness does not know is left alone.

- `_expect_quarter_hourly_registry` holds a contract carrying `quarter_hourly_option` to a card
  the entry's answer can actually move, which is two shapes. A `DynamicRates` leg must still be
  on the hourly grid (Frank Energie): a card that started printing the quarter-hourly index in a
  shape the parser accepted would leave a ticked box flipping a leg already there and an unticked
  one claiming a grid the supplier no longer sells. A `VariableRates` leg must still carry
  `formula_factor` (Bolt): losing those coefficients silently disables the box, and the entry
  keeps billing the printed monthly rate.

- `_expect_direct_debit_registry` holds a card that prices a direct-debit payer (a welcome credit
  conditional on it, a direct-debit supplement or a standing-charge discount) to a contract whose
  registry entry carries `direct_debit_discount`. The flow asks the question from that flag before
  any card is fetched, so an unflagged card that prices it is the direction that costs money: the
  stored answer can only be no, and the household has no box to tick to be credited. The reverse
  is common and not asserted, since a product that printed the supplement in some months prints
  nothing in others.

- `_validate_energy` (`scripts/live_check.py`) dispatches on the energy dataclass type and
  bounds-checks the rate(s). Fixed/variable/TOU/Impact rates must sit in a loose plausibility band
  (the source uses `[0.05, 0.50]` EUR/kWh as an illustrative sanity range); dynamic contracts
  check `factor` in `[0.5, 3.0]` and `base` in `[-0.10, 0.10]` (illustrative); TOU and Impact
  additionally assert band ordering (peak >= transition >= offpeak; pic >= medium >= eco). Every
  populated figure is bounded, not one per shape: the bi-hourly and exclusive-night registers
  (`_expect_registers`), every month-indexed coefficient pair a variable, TOU or Impact card
  carries, which must also come whole (`_expect_coefficient_pair`), the Impact bands and price
  ceilings, every per-kWh network rate, the transport rate, the prosumer tariff, the VREG ceiling
  and the Sibelga power term on each DSO overlay (`_validate_dsos`), the per-slot feed-in formula
  and the printed pair or triplet (`_validate_injection`), the welcome credit's five amounts,
  its share and its wait (`_expect_welcome_credit`, bounded at BOTH ends because the slip this
  repository actually had was a figure a thousand times too small that every ceiling accepted,
  with zero exempt since a card granting nothing does not always say so with a null, plus a cap
  that may not sit under the amount it caps), and a ceiling on the federal excise and
  the regional levies (`_expect_region_basics`). The bands are sized on the unit slip they catch,
  measured over the September 2026 archive, never on tariff economics: a figure read in c/kWh
  lands ten times above a ceiling, one read in EUR/MWh a hundred below a floor, while every real
  card sits well inside. The federal energy contribution is bounded for every supplier from
  `_validate_snapshot` rather than by the three checks that used to ask for it. An
  unrecognised energy class is a failure. Spot-monthly cards are bounded on the same axis as
  dynamic ones, and on each per-meter pair the card populates: every bi-hourly or
  night-circuit coefficient carries its own bounds, the bi-hourly pair has to be complete or
  absent (pricing routes onto the bands only when both halves are set, so half a pair silently
  sends both back to the mono formula), and the peak coefficient must not sit BELOW the
  off-peak one, which is the row swap the bounds alone cannot see. That last one is deliberately
  not strict: this supplier's predecessor printed one formula in all four registers for
  nineteen consecutive months, so a flattened card is normal publishing and must not gate CI.
  Only Energy Knights Essentia prints those pairs today; energie.be Variabel and the custom
  supplier publish one formula for every meter and are unaffected.
- `_expect_regional_levies` (`scripts/live_check.py`) judges the levies nothing else read, by the
  region the card was fetched for, which `_validate_snapshot` takes as a required keyword so a new
  check cannot skip it. The Flemish energy fund must be zero or within five times of the 10,07 EUR a
  month every card that prints it states, and only on a Flemish card; on a Flemish card of a
  contract the registry knows it must be printed exactly where the contract owes it, a
  professional one or the non-domiciled Engie Empty House, since every reader returns 0 when its
  pattern misses and a zero allowed everywhere let a dropped fund through; the Walloon connection fee
  zero or within five times of 0,00075 EUR/kWh, and a Walloon card must carry it or set
  `region_connection_fee_unavailable`, which is allowed nowhere else and only with no fee;
  `published_vat_rate` is stamped by the resolver alone, so an extractor must leave it at zero.

- `_validate_injection` (`scripts/live_check.py`) gates that the feed-in credit parsed and
  kept the right shape. This exists because the coordinator drops the credit entirely when
  `injection` is None, so a relabelled injection row silently zeroes a solar user's credit and
  used to pass CI green (issues #31, F53). The `shape` argument pins expectations: `"none"`
  (region pays no feed-in, injection must be absent), `"monthly"` (`current` set, `factor`/`base`
  None), `"spot"` (`factor`/`base` set), `"spp"` (a formula indexed on the solar-weighted monthly
  mean: `current`, `factor`/`base` AND `spp_indexed` all set, with the coefficients bounds-checked),
  `"month"` (the same on the plain arithmetic monthly mean, flagged `month_indexed`),
  `"triplet"` (a per-slot peak/transition/offpeak feed-in tariff with its three month coefficient
  pairs and `month_indexed`, which only Empower Flextime has),
  or `"present"` (`factor`/`base` set). Per-contract
  expectations live in `_INJECTION_SHAPE`; the DATS 24 check passes
  `injection_shape` explicitly because its Wallonia card pays no feed-in while its Flanders card
  carries the BE_spotSPP formula.

  `"monthly"` and its two indexed siblings pull in opposite directions, so which one a card gets
  is a statement about the contract, not a formality. `"monthly"` says the card's printed figure
  IS the rate and no coefficient may ever reach the pricing engine. `"spp"` / `"month"` say the
  printed figure is last month's estimate and the coefficients are what settles the bill, so
  losing them is the mis-credit. Five cards moved from the first to the second over this batch
  (DATS 24 Groen Variabel, EBEM Variabel / B@sic+, Eneco Power Fix / Flex), and the two
  EnergyVision fixed cards followed.

  The shape assertion runs whatever else the card prints. It used to hang off the `else` of the
  indicative's range check, so on the several dynamic cards that publish BOTH a formula and an
  indicative it was only tested while the indicative happened to be absent: a redesign that dropped
  the formula and kept the indicative passed green while the credit silently went flat. TOU and
  Impact cards are derived as `"monthly"` rather than `"present"` for the same reason - a time-of-use
  ENERGY leg does not make the feed-in credit per-slot, and asserting a `factor`/`base` they never
  carry would have been noise rather than a gate.

- `_expect_feed_in_fixed_for_term` (`scripts/live_check.py`) holds `InjectionRates.fixed_for_term`
  to the cards that fix their printed feed-in price for the term: every Mega fixed card (by the
  registry kind), Trevion Groene Energie Vast and EnergyVision's fixed-injection card. The flag is
  read off a sentence and is what makes a signing cohort keep its own card's feed-in price, so a
  redesign that drops the sentence unlocks every such cohort silently, and the flag on any other
  card would lock a credit the contract lets move.

### Per-supplier byte and wallclock budgets, and drift issues

An aiohttp `TraceConfig` (`scripts/live_check.py`) tags every request with the supplier
currently being checked (via a `ContextVar` set by the `_attributed()` context manager,
`scripts/live_check.py`) and accumulates per-supplier fetch count, summed request duration,
body bytes, and failed-attempt count / duration into `METRICS`. These metrics
surface silent slowdowns and PDF-size jumps, both leading indicators that a supplier reworked its
publication, and are appended to the daily report by `_render_metrics`
(`scripts/live_check.py`).

Reading a row correctly needs three facts about which hook feeds which column:

- **Fetches / Fetch time** come from `on_request_end`, which fires once per request that reached
  its final response headers, after the redirect chain and **before** the body is read. So the
  latency figure is time-to-headers, and a 302-to-CDN fetch counts as one.
- **Bytes received** are summed in `_on_response_chunk_received` (`scripts/live_check.py`)
  rather than read from `Content-Length`, because that header is None on chunked responses and
  would silently count as zero. `ClientResponse.read()` fires that hook once with the whole body,
  so the count is all-or-nothing: a fetch with a counted request but `-` bytes got its headers and
  then stalled mid-body.
- **Failed (n / s)** comes from `_on_request_exception` (`scripts/live_check.py`), which is the
  only hook a request that never produced a response fires. Failures are kept out of the success
  columns deliberately, so the latency budgets below stay calibrated on successful fetches; before
  this counter existed a supplier whose every attempt timed out reported 0 fetches and 0 s and read
  as though it had barely been tried. The hook also prints a `warning:` line naming the exception
  and the url of the hop that actually failed -- which the wrapped `ExtractorError` cannot, since
  providers pass the original url to the fetch helper and a redirected fetch therefore reports only
  that first url whichever hop died.

Two safety caps bound runtime. Each supplier check runs under
`asyncio.wait_for(..., timeout=_SUPPLIER_HARD_TIMEOUT_S)` (`scripts/live_check.py`) with a 600s
hard cap (`scripts/live_check.py`, raised from 240s when the professional editions roughly
doubled Engie's and Mega's sequential fetch counts), recorded as an extractor failure rather than
propagating so one hung supplier cannot starve the `gather()`. Every latency budget below must stay
under that cap, or the supplier is killed before it can report the drift the budget exists to catch.
The session-level `aiohttp.ClientTimeout(total=60)` (`scripts/live_check.py`) bounds individual
requests.

`_drift_warnings` (`scripts/live_check.py`) compares each supplier's summed fetch time and
total bytes against a budget. The global defaults are `LATENCY_WARN_THRESHOLD_S = 90.0` and
`BYTES_WARN_THRESHOLD = 5_000_000` (`scripts/live_check.py`), with per-supplier overrides in
`_BYTES_BUDGET_OVERRIDES` (`scripts/live_check.py`) for the known-large catalogues (Bolt,
Ecofix, Engie, Mega, OCTA+, TotalEnergies) and `_LATENCY_BUDGET_OVERRIDES`
(`scripts/live_check.py`) for those same multi-fetch suppliers plus EBEM, Eneco, Energy Knights and
Luminus, which are slow per fetch rather than large. That last group is the
recurring one: each answers a residential line in seconds and a GitHub runner
in minutes, for a byte-identical payload, so the alert reports where the run
executed rather than anything about the cards. Energy Knights joined it on
2026-08-31 (issue #75) purely because it was added after the others and
inherited the 90s default. Note that `elapsed_s` is the sum of per-request
durations, not true wallclock, so a supplier that fetches concurrently (Bolt fetches its six PDFs
with `asyncio.gather`, `scripts/live_check.py`) records the sum of its parallel fetches; the
budgets are sized around that. The synthetic `_catalog` bucket is skipped in drift analysis because
it aggregates every supplier's discovery fetch under one name (`scripts/live_check.py`). When a
budget is blown, `live_check.yml` opens or updates a dedicated drift issue (see below). Tuning a
false-firing drift alert means adjusting the override, not the code.

A supplier whose extractor already failed this run is skipped too (`scripts/live_check.py`,
against the set `_failed_suppliers` reads off the check labels, `scripts/live_check.py`). The
failure is both the louder signal and the usual cause of the numbers: a supplier that reworks its
cards changes their size, and because bit 0 makes the workflow retry the whole run for an hour,
every other supplier gets several more rolls against its budget with drift judged on whichever
attempt landed last. Issue #55 is the worked example: Ecofix rasterised its August 2026 cards, which
pushed each PDF from ~1.1 MB to ~1.76 MB and blew the byte budget, while six retried attempts gave
Eneco enough rolls to land one 96.4s outlier against the then-90s default. One supplier-side event,
two suppliers named, and it would have refiled every day for as long as Ecofix stayed broken. The
skipped measurement is printed to stderr so a budget can still be tuned from the run log without a
rerun.

### The catalog baseline only counts what the listing shows

`_check_catalogs` diffs each supplier's `discover()` output against
`_CATALOG_BASELINES` (`scripts/live_check.py`), one lambda per supplier deriving the
registered identifier set from the provider module, so the baseline cannot drift away from
the code. The rule is that it must cover exactly what that supplier's discovery surface
enumerates, no more.

Mega and Bolt advertise only their **residential** cards -- Bolt's `discover` filters on the
`res` segment and Mega's listing carries only `-B2C-` hrefs -- while a professional edition
reuses the residential product name (Mega) or `folder/slug` (Bolt). Counting the professional
contracts let a B2B card vouch for a product that had left the residential listing: Mega
dropped Zen Fixed from the listing for the August 2026 card and put it back in September, and
the diff stayed quiet through both because `mega_pro_zen_fixed` carried the name the whole
time. Engie is deliberately not filtered: its surface is the public sitemap, which does not
split by segment.

`test_catalog_baseline_ignores_editions_the_listing_never_shows`
(`tests/test_live_check_harness.py`) pins the rule against stub contracts rather than the
registry, because every product Mega sells to businesses it also sells residentially today --
the two baselines are identical until the day they are not.

A `discover()` that comes back empty fails its row (`<supplier>/catalog: discover() returned no
ids`), since a supplier that still trades always has a catalog. A `discover()` whose listing fails
to load or no longer matches usually returns an empty set rather than raising, so this row is often
the only sign that a supplier's new products have gone unseen. It used to pass with
a warning on stderr, and Luminus's sitemap turning into an index kept its products unseen for at
least the first three weeks of September 2026. A withdrawn supplier's empty discovery is marked
expected and files nothing (`test_a_discovery_that_sees_nothing_fails`).

### Exit codes and the two report side-channels

`_run()` (`scripts/live_check.py`) splits checks into `extractor`, `catalog` and `tax` kinds. The
extractor report (with the metrics block) is printed to stdout, which the workflow captures. The
catalog diff is written to `catalog_report.md`, the tax rows to `tax_report.md` and the drift
warnings to `drift_report.md` at the repo root (`scripts/live_check.py`), each a side-channel the
workflow reads to file a separate issue so the failure modes never conflate in one thread. Beside
the drift report goes `drift_fingerprint.txt`, one `<supplier> latency` or `<supplier> bytes`
line per blown budget with no measurement in it, and beside the other two go
`catalog_failures.txt` and `tax_failures.txt`, their failing labels; those three files are what
the issues are fingerprinted on.

The exit code is bit-encoded (`scripts/live_check.py`):

| Bit | Value | Meaning | Retried by workflow? |
| --- | --- | --- | --- |
| 0 | 1 | extractor **regression** (fetch or parse, the spot fallback included) or a phase of the run that crashed, excluding unreadable cards | yes |
| 1 | 2 | catalog signal: a new product appeared at a supplier or its discovery failed, or a supplier's federal tax block disagrees with the month's consensus. One bit for two kinds of news, because neither is a regression in what this repository bills and neither should fail a pull request; `catalog_report.md` and `tax_report.md` carry one kind each, and the workflow files whichever has failures under its own title | no |
| 2 | 4 | drift alert (latency or byte budget blown) | no |
| - | 8 | harness crash: a top-level Python exception in the script, or, set by the workflow's loop itself, any exit above 7 and any odd exit with no `extractor_failures.txt` written (a module-level ImportError exits 1, an OOM kill 137, a runner SIGTERM 143) | no |

`rc=8` is deliberately outside the 1/2/4 bit space (`scripts/live_check.py`) so the workflow
does not open a "supplier extractor broken" issue for what is actually a bug in the harness.

Both halves of bit 1 run their step and then look at their own report: each begins with a
`grep -q '^## Failures'` on it and exits quietly when its half is clean. Without that guard a
run whose fifteen discovery rows all passed still opened an issue titled "new supplier products
detected", carrying six rows about a stale tax block (issue #101).

### Unreadable cards do not gate bit 0

A supplier that publishes its tariff card as page images fails every check against it, forever,
and no change to this repository can fix it. Counting that as a regression meant one such supplier
set bit 0 on every run, drove the workflow's retry loop to exhaustion, and refiled a fresh issue
each time the previous one was closed (issues #53, #56 and #58 all carried the same six Ecofix
rows). It also handed every other supplier seven rolls of the dice at a transient timeout, which
is where the collateral rows in those issues came from.

`_record` (`scripts/live_check.py`) marks such a check `expected`, and `_extractor_regressions`
(`scripts/live_check.py`) is the single definition of what gates CI. The classification reads
the exception type the fetch sites already write into the detail string
(`CardNotReadableError`, raised by `providers/_pdf.py`), so it follows the card actually
published rather than a hardcoded supplier list: a supplier that goes back to publishing text
starts gating again by itself, with no edit here. The rows still appear in the report, under their
own heading and counted separately in the headline, so a green run never hides them.

### Telling a slow supplier from a blocked runner

`scripts/probe_endpoint.py` and the `Endpoint probe` workflow exist for one
question the live-check report cannot answer: a fetch that times out in CI and
succeeds from a workstation is a **different fault** from one that is simply
slow, and both arrive as `TimeoutError`. Mega has already done the first kind to
this repository, serving a residential connection normally while timing out from
Actions runners for weeks.

The probe reports DNS, connect (TCP + TLS), time-to-first-byte and total, per
attempt, using the integration's own `User-Agent` and the same 30 s ceiling
`fetch_pdf_text_layout` applies — so what it measures is what the extractor
would experience. Run it locally and dispatch the workflow, then compare:

| both vantage points | reading |
| --- | --- |
| similar, near the ceiling | the supplier is slow; the timeout is the number to argue about |
| fast locally, slow or failing from CI | the runner's egress is being treated differently, and no timeout value fixes it |

Two details that make the numbers mean something. The connector sets
`force_close`, so every attempt pays the full handshake rather than reusing a
warm connection and hiding the cost this is here to time. And the trace hook is
attached to the **session**: a `TraceConfig` built per request and attached to
nothing is never called, which reports every connect as `None`.

It never gates. A supplier being slow today is not a reason to fail a workflow
someone ran to find out whether it is.

### The transient-only retry helper

`_fetch_with_retry(factory, *, attempts=3)` (`scripts/live_check.py`) calls `factory()`, and on
a transient network failure retries with a short backoff (`_RETRY_BACKOFF_S = (1.0, 3.0)`). A
failure is "transient" only if it is a bare `TimeoutError` or an `ExtractorError` whose message the
shared `providers/_pdf.is_transient_fetch_error` predicate classifies as transient (a wrapped
"network error fetching", a "storage error fetching" from an object store refusing the read, or an
HTTP 5xx/408/429/403). A 404/410 (card renamed or withdrawn) or any parse error propagates
immediately so a real regression is not masked by retries. A fresh awaitable
is built via `factory()` per attempt (awaitables are single-use), which is why callers pass
`functools.partial(...)` rather than a pre-created coroutine. The transient predicate is imported
from `providers/_pdf.py` (`scripts/live_check.py`) so the harness and the coordinator classify
errors identically and cannot drift apart.

This retry helper is CI-only. Do not port it into `coordinator.py`: the coordinator has its own
retry/backoff behaviour, and duplicating this logic there would create two divergent policies.

### Coverage-gap caveat

The live check only flags what it asserts. A nullable field that no gate covers can pass green even
when it is wrong. This is exactly the class of bug `_validate_injection` was added to catch (a
relabelled injection row that zeroed a solar credit passed CI for all but two suppliers before the
shape gate existed, issues #31/F53). When you add a field to a snapshot, add a gate for it here;
green does not mean covered.

## scripts/archive_cards.py

The live check asks whether today's card still parses. `scripts/archive_cards.py` keeps the
answer: it walks the same registry, fetches every (supplier, contract, region) card through
`extractor.fetch` exactly as the coordinator does, and writes the parsed snapshot to
`<out>/cards/<supplier>/<contract>/<region>/<YYYY-MM>.json` (`scripts/archive_cards.py`). The
dict is `_snapshot_to_dict`, the same codec the integration's own Store uses for a month row,
round-tripped through Home Assistant's JSON encoder so the file holds exactly the types
`_snapshot_from_dict` reads back, plus `_seen_on` and `_sources`. The run happens daily against
the `be_price_cards` repository (see `archive_cards.yml` below) and the month cache reads the result first for
any closed month, before the supplier's own archive (`snapshot_store._archived_card_from_github`,
see [coordinator.md](coordinator.md)).

Three design points:

- **A card is filed under the month its label names**, read by `label_month`
  (`scripts/live_check.py`), the same function the freshness gate uses. Filing by
  capture date would put Ecopower's definitive card, which lands at the end of the month it
  covers, under the wrong month, and a supplier publishing ahead would overwrite the running
  month with next month's card. Only a label the function cannot read files under the day's
  month.
- **What each parse read is kept too.** The run shares one text memo
  (`memoise_text_fetches`) so a listing page or a shared card is fetched and parsed once, and a
  small recording dict (`_RecordingMemo`, `scripts/archive_cards.py`) notes which memo
  entries each fetch touched. Those texts are stored content-addressed under
  `texts/<YYYY-MM>/<sha256>.txt` and listed in the card's `_sources`, so a stored month can be
  re-read against a later parser or checked by hand. Bytes are not kept: a month of PDFs is
  tens of megabytes.
- **A quiet day writes nothing.** A month file is rewritten only when the parse differs from
  what is on disk, ignoring the two timestamps (`_write_card`, `scripts/archive_cards.py`),
  so the archive gains a commit only when a card changed. Months older than `--keep-months`
  (36) are removed on every run (`_prune`, `scripts/archive_cards.py`), and so is every text no
  row names (`_drop_unnamed_texts`): the day's copy of a listing page that carries a nonce, or
  the text a rewritten row no longer reads. Nothing reaches a text except through a row, and on
  22 September 2026 such copies were 84 of the store's 1732 texts, 10,2 MB of 29,3.

The cards themselves are kept too, and the same mechanism is what keeps the daily walk cheap.
The readers in `providers/_pdf.py` expose one seam, `render_through` (`_pdf.py`): inside that
block a downloaded card's validated bytes go to a hook instead of straight to the renderer. The
archiver installs `_Cards.render` (`scripts/archive_cards.py`) there. It hashes the bytes, and
for a (variant, digest) pair some stored row already names it serves that row's text from the
branch instead of rendering, so a card that has not changed since it was last stored costs one
download and no pdfplumber pass; on a Raspberry Pi the render is the 20 minutes of the walk, the
downloads are seconds. Bytes the archive has not recorded yet are written to
`--pdfs DIR/electricity-<YYYY-MM>/<sha256>.pdf`, the month being the one the row that read it is
for (so a mirrored March card is filed under March, and bytes no row names, a card whose parse
failed, under the month of the run), and the row's `_sources` entry names its PDF by that digest
alone. A card that never passed through a reader is seen there too: OCTA+'s archive hands the
card over base64 inside a JSON answer and renders it through `render_pdf`, the same seam, so the
cache records the call and the archiver adds the card to the row's sources with its text and
digest (`_sources_of`). The 112 OCTA+ months mirrored before that seam existed had no kept card
and no link; the replay that followed the change kept them. The workflow uploads the directory as release assets of the separate cards
repository (see `archive_cards.yml` below) and records where each one landed in
`<out>/pdfs.json`, the manifest the next run seeds `_Cards` from and the one place that maps a
digest to a release path: a digest the manifest does not list is written again until an upload
succeeds, so a day without the token loses nothing for good. `_prune` drops manifest entries
older than the retention alongside the rows.

A parser fix reaches the stored months on its own. After the live walk the script compares a
digest of the parser sources (`providers/*.py`, `const.py` and the codec in `snapshot_codec.py`,
plus the installed `pypdf`, `pdfplumber` and OCR engine versions, the last with the git commit
it was installed from, since a reader release lays a card out
differently and a stored text is served to every replay and to the live check for as long as the
card's bytes stand; `_parser_digest`, `scripts/archive_cards.py`) with the one stamped in the
archive's `parser.txt`; when they differ it replays every stored row (`_replay_row`,
`scripts/archive_cards.py`): the texts the row's `_sources` name are seeded into the memo,
then, behind them and never over them, the texts the FOLLOWING month's row names. A month that
settles on the next month's card (EBEM's index, Trevion's two) reads a text its own row never
did, and without it the settlement reaches the offline session, is refused as a network error and
comes back provisional, which the replay reports as no longer settling: 16 EBEM rows and 9
Trevion ones. Behind, because the memo is keyed by URL and several suppliers publish every month
at one unchanging address: seeded first, the follower replayed Ecofix's six August 2026 rows as
September cards, label, validity and price alike. The clock is pinned with freezegun to the row's
`_seen_on` at noon Brussels (ticking, so the
loop's timers and the render threads keep working; some extractors choose a card by today's
date), and the row is re-run through `fetch`, or `fetch_for_month` for a backfilled row, with a
`_ReplaySession` (`scripts/archive_cards.py`) in place of aiohttp. That session reaches no
supplier: the only request it honours is for a kept PDF, which a parser that now reads a card
with another PDF reader asks for, served from the `--pdfs` directory or downloaded from the
cards releases (`--pdf-base-url`), with a download kept on disk for the sibling rows that read
the same card; a HEAD answers 200 for a kept card and 404 for anything else, so an extractor
that probes candidate URLs before choosing one (Eneco's archive walks issue numbers) lands on the
card the row was parsed from; anything else is refused as a network error, and the row is
reported as not replayable and left as it was, as is a row whose text file is gone or whose
archive path no longer settles it. The run reports rows `reparsed`, whose parse came out
differently, apart from rows `restamped`, rewritten only to stamp the running schema, which every
schema bump does to every replayable row. A kept card that does not download for a reason unrelated to
the card (a network error, a 5xx, a 429) leaves `parser.txt` as it was, so the next run replays
the rows again: stamping over them had them never looked at again until an unrelated parser
edit, while the integration reads a closed month from here first. A row whose parse came out differently is rewritten,
keeping its capture day; `_cached_at` moves. The replay is a regex pass per row, no download
and no render, so it is minutes for the whole branch, and a day without a code change replays
nothing. `--reparse` forces it; `--rerender` also leaves the PDF texts out of the seed, so every
card is fetched back from its kept copy and rendered afresh, at the cost of downloading every
kept card. A pdfplumber, pypdf or OCR engine upgrade takes that path by itself: the reader
versions are stamped beside the digest in `parser.txt`, a run that finds one of them moved renders
rather than re-reads (a reader the stamp never named is recorded, not counted as moved), and the workflow gives that run the six-hour budget a dispatched re-render gets
(`Size the walk's budget`). A row is rewritten only
when what a source was or what it parsed to changed: the path of the text it was read from is
not compared, because a listing page with a nonce or a render that is not byte-stable would
otherwise rewrite the row every day for nothing. A fresh archive only stamps the digest: it holds nothing older
than the parser that wrote it.

Every run also rewrites the listing for people at the archive root, in a fixed order so a day
that changed nothing rewrites it to the same bytes. `_write_coverage`
(`scripts/archive_cards.py`) writes one sheet per supplier under `coverage/`, a table with a
row per contract and region and a column per month the archive holds, and `coverage.md`, the index
naming the sheets; a sheet whose supplier has no rows left is removed. Each cell links what the month was parsed from and what
came out of it: `pdf` is the card in the cards repository's releases, once the manifest says
where it landed (until then the bare word), `page` the text of the page a page-parsed row read,
in the archive, and `json` the row itself; a month copied from the supplier's archive carries
`(mirror)`. That is the answer to "is my month covered, where is the card, and what did we read
off it". A card that downloaded and would not parse gets a cell too, `pdf` and `(not parsed)`
with no `json`: some months a supplier publishes its card as page images that no reader can
read, and those bytes are uploaded like any other card, so something has to say which card they
are. `_write_unparsed` keeps that list in `unparsed.json`, by the row the card would have become,
merged with what earlier runs saw so a run over one supplier does not forget the others, and an
entry whose month has a row is dropped, so a month that starts parsing leaves by itself.
`_write_listings` writes the sheets and removes the `pdfs.md` index earlier versions wrote, since the sheets link every file now.
The workflow rewrites them once more after the upload step (`--index-only`, no fetch) so the
day's new files are linked the day they are uploaded, writes the two READMEs itself (`Write the
two READMEs`: the root one names the namespaces, the `electricity/` one the layout) and pushes
the lot with the day's rows (`Commit and push what changed`), so a person on that repository's
releases page is one click from the names; each release's notes point there, and a search of
that repository for a file's digest finds its sheet.

A card whose reader refuses it -- a supplier publishing page images, which Ecofix has done
since August 2026 -- is handed to `ocr_price_cards` (`_ocr_text`, `scripts/archive_cards.py`)
before it is given up on. That engine knows the fonts those cards are set in glyph by glyph and
refuses a mark it cannot place; the reading is taken from `trusted_text`, which drops any line
carrying a refused mark, and is held to the same floor a text layer is (`_MIN_TEXT_LAYER_CHARS`),
so a mostly-refused page is no row rather than a row of silent misses. The row it produces
marks the SOURCE it read that way, as `"ocr"` on the `_sources` entry beside the pdf digest it
describes; the key is absent otherwise, so every row already stored is unchanged. An installation
asks the sources (`_row_read_by_ocr`, `snapshot_months.py`) to know whether to tell its user the
figures came from a reading.

The mark belongs to the document, not to the row, because a row can read two and only one of them
need be the unreadable one: **137 of the archive's 1.686 rows read two cards**, and a row-level
mark makes the readable one look unreadable for as long as it is read beside the other. It also
has to outlive the run that discovers it. The reader is what finds a card has no text layer, and a
card whose bytes have not changed is served its stored text without the reader running, so
`StoredTexts` reads the mark back off the rows to keep it.

The row used to carry a derived copy as `"_ocr"` as well. Nothing kept it in step with the
sources, and it was **never once written**: not one of the 1.686 rows had it, the Ecofix months
read off page images included, so the Repairs card it drives had never appeared for anybody.
It is gone rather than migrated, because nothing was ever reading a value that never existed.
Rows captured before the mark worked were set by measurement; a supplier that starts rasterizing
now is marked on its first walk.
The engine is installed by this workflow alone, which runs 3.13 for everything else and 3.14 for
it: no installation ever decodes a card, it reads the row.

A card that even it cannot read is named in `unparsed.json`, by the row it would have become,
with the url it was downloaded from beside each digest. That url is the half that matters later:
an extractor asks for a card by url and cannot be told to want a digest, so it is what lets
`_retry_unparsed` hand the kept bytes back. The retry runs on the same trigger the row replay
does, a changed `parser.txt`, and for the same reason -- such a card cannot be fetched again,
since a supplier serving one url overwrites it the next month, so the kept bytes are the only
copy there will ever be and the reader is the one thing that can change the answer. The day a
reader learns to read them they become rows and their entries disappear. The clock is pinned to
the middle of the month the card was captured in, the way a row's replay is pinned to its
capture day, so a parse that reads "valid until" against today does not decide a card from two
months ago is expired.

`--backfill N` runs a second walk after the live one: every supplier that keeps an archive of its
own is asked, through the same `fetch_for_month` the integration uses, for each of the N closed
months before the current one that the archive does not hold yet, and each answer is stored under
the month asked for with `_via` set to `archive` (a live capture carries `live`). A month the
supplier answers None for is left absent, as is a card still flagged provisional (Eneco's
estimate before the next card prints the settled index), so a later backfill fills it once it has
settled, and a month already on disk is never asked again. The branch thus mirrors the supplier
archives: insurance against a supplier dropping its own, and a cheap read for
any month a supplier's own path cannot serve. Only for suppliers walked while they were still
publishing, though: DATS 24 left before the archive existed and holds no rows here at all. The daily schedule runs with `--backfill 0`; the
workflow's manual dispatch takes the number as an input.

Per card, transient failures are retried three times with the live check's own classification
(`is_transient_fetch_error` plus a bare `TimeoutError`) and a permanent one is recorded and
skipped, so one supplier never stops the walk. The custom supplier has no card and a supplier
past its `deprecated_until` has left the market, so neither is asked (`_targets`,
`scripts/archive_cards.py`). The script exits 0 when at least one card was stored or
confirmed unchanged and 1 when none was, which is a runner-wide problem rather than a
supplier's; it files no issues, the live check already does that.

`tests/test_archive_cards.py` drives it with a stub extractor and a canned page: filing by
label, the shared-page attribution, the no-op repeat run, the digest-keyed render skip and PDF
retention, the replay on a parser change (and only then), the rows a replay must leave alone, a
reader that changed variant getting its kept PDF back, the retry split, the skip rules, the
backfill's absent and provisional months, the retention and the exit code.

## GitHub workflows

Six workflows live under `.github/workflows/`.

### test.yml - Tests

Runs on push to `main`, on every pull request, and on manual dispatch (`.github/workflows/test.yml`).
It pins Python 3.13 and installs the toolchain from `requirements-dev.txt`
(`.github/workflows/test.yml`), where every version is pinned: `homeassistant==2026.2.3`,
`pytest-homeassistant-custom-component==0.13.316`, `ruff==0.16.0`, `pytest-freezer==0.4.9`,
`pypdf`, `pdfplumber`, `defusedxml` and `pyxlsb`, so an upstream HA-core, test-shim or linter
release cannot silently turn the suite red on `main`. The live check and the endpoint probe take
their versions from that same file with pip's `-c`, so a version is written down once. The lint
rule set is itself
pinned in `pyproject.toml` (`[tool.ruff.lint] select`), so a ruff upgrade cannot expand what is
linted for; `[tool.ruff.format] exclude` keeps the formatter off Markdown. The steps are:

| Step | Command | Notes |
| --- | --- | --- |
| Lint | `ruff check .` then `ruff format --check .` | `.github/workflows/test.yml` |
| Type check (production) | `mypy --strict custom_components/be_electricity_prices` | strict; production code must be strict-clean (`.github/workflows/test.yml`) |
| Type check (tests + scripts) | `mypy custom_components/ tests/ scripts/` | non-strict; covers `live_check.py` so a regression surfaces on PR rather than in the next daily scheduled run (`.github/workflows/test.yml`) |
| Tests | `pytest tests/ -q -n auto --dist loadfile` | `.github/workflows/test.yml` |

`concurrency` cancels a stale push/PR run when a new commit lands (`.github/workflows/test.yml`).
The non-strict pass over `tests/` and `scripts/` is why the `# type: ignore[arg-type]` convention
described above exists.

### validate.yml - Validate

Runs on push to `main`, on pull requests, on a daily `cron: "17 6 * * *"`, and on manual dispatch
(`.github/workflows/validate.yml`). Two independent jobs:

- `hacs` runs `hacs/action@main` with `category: integration` (HACS repository requirements).
- `hassfest` runs `home-assistant/actions/hassfest@master` (Home Assistant's manifest/brands/services
  validation).

These validate packaging and manifest conformance, not runtime behaviour.

### live_check.yml - Live extractor check

Runs on the daily `cron: "17 6 * * *"` (06:17 UTC, after suppliers' overnight publication), on
manual dispatch, and on pull requests that touch `providers/**`,
`scripts/live_check.py`, or the workflow itself (`.github/workflows/live_check.yml`). It needs
`issues: write` to file drift/catalog/extractor issues (`.github/workflows/live_check.yml`).

The odd minute is deliberate. GitHub documents the start of every hour as a high-load slot for the
`schedule` event and says queued runs may be dropped when the load is high enough, which is exactly
what happened on 2026-08-27: neither this workflow nor `validate.yml` produced a run from the 06:00
slot, no delayed run and no `startup_failure`, just nothing. A dropped slot is not recovered
automatically, so the day's check has to be dispatched by hand:
`gh workflow run live_check.yml --ref main`.

The minute in the cron is a request, not a start time. Measured over 7 to 14 September 2026 this
check started between 10:55 and 12:59 UTC, four to seven hours after the slot it asks for, and the
card archiver (`cron: "41 5 * * *"`) between 09:34 and 10:58. The 36 minutes between the two crons
therefore decide nothing. Having the archiver go first is worth something, since this check then
takes that day's renders from the archive rather than repeating them, and the queue has kept that
order so far; when it does not, this check renders more cards and runs longer, which is the entire
cost. That is why neither workflow is chained to the other with `workflow_run`: an archive run
GitHub sheds would otherwise take the day's supplier check down with it.

The single `check` job installs the pinned HA version (needed because `providers/_pdf.py` imports
`homeassistant.util.dt`), clones `be_price_cards` shallow under `tmp/cards` so the harness can
take its renders from the archive's texts (`Rendering only what changed` above; the clone is
public, so it needs no token and works on a fork's pull request, and a clone that fails just means
everything is rendered), and
runs `scripts/live_check.py` inside a two-tier retry loop
(`.github/workflows/live_check.yml`). The retry exists so an issue is filed only when a supplier
is still broken roughly an hour after first detection, not for a transient CDN blip (issue #30):
seven attempts with delays `10 30 60 120 300 3000` seconds, bounded by a 5400s wall-clock deadline
so the job always reaches the issue-creation steps before the 120-minute job timeout. Only a bit-0
(extractor) failure is retried; catalog and drift signals are stable and break out immediately.

Breaking out on a green attempt is not enough on its own. On a slow runner every attempt times out
on a *different* random subset of suppliers, so no attempt is ever green and the loop filed
whichever hosts were unlucky on the last one: issue #61 ran six attempts and produced 21 failures,
all `TimeoutError`, all transient, and every card fetched fine off-runner. So the loop also
intersects the per-attempt failures and only a check that failed in **every** attempt is filed.
`scripts/live_check.py` writes each attempt's failing check labels to `extractor_failures.txt`
alongside `report.md`, and the loop folds them with `comm -12` under `LC_ALL=C`. When the
intersection empties it stops retrying (there is nothing persistent left to confirm) and clears
bit 0 from `rc`, leaving the catalog and drift bits alone. A real regression - a parse error, a
withdrawn card - fails the same checks every attempt and still files. The trade-off is that a
genuinely intermittent regression, say a CDN serving two card layouts round-robin, is suppressed
until it becomes consistent.

The extractor issue body leads with those persistent failures, because the report under them is the
last attempt's and on a slow runner also lists checks that failed only that once.

The job then branches on the captured `rc` to open or update four distinct issues through
`scripts/file_ci_issue.sh`, which finds the one open issue by its label (never by a title substring,
so a manually opened issue cannot catch these comments) and posts a comment only when the failure
changed or the last post is older than a week. Each body ends in a hidden fingerprint marker: for
the extractor issue it is a hash of `persistent_failures.txt`, for the catalog and tax issues a
hash of `catalog_failures.txt` and `tax_failures.txt` (each report also lists every passing row
under its pass count, and a tax row's detail counts the other suppliers, so hashing the report
posted the same open failure again whenever an unrelated row changed), for the drift issue a hash
of `drift_fingerprint.txt` (the report itself carries the measured seconds and bytes, so hashing it
never matched the previous day and a supplier over its budget for weeks got a comment a day), and
the script compares the marker on the issue's latest post with the one it is about to write. A supplier that stays broken therefore gets one issue and one comment a week
rather than one a day, which was the last open piece of the live check's noise problem, while a
failure that changes shape is still posted the same morning. `tests/test_file_ci_issue.py` drives
the script through a fake `gh`.

| rc bits set | Step | Label | Issue title prefix |
| --- | --- | --- | --- |
| bit 0 (rc 1/3/5/7) | Open or update extractor-broken issue | `live-check-extractor` | `[live-check] supplier extractor broken` |
| bit 2 (rc 4/5/6/7) | Open or update drift issue | `live-check-drift` | `[live-check] supplier drift detected` |
| bit 1 (rc 2/3/6/7) | Open or update new-products issue | `live-check-catalog` | `[live-check] new supplier products detected`, or `[live-check] supplier product discovery failed` when no new product is among the failures |
| bit 1 (rc 2/3/6/7) | Open or update tax-block issue | `live-check-tax` | `[live-check] a supplier's federal tax block disagrees` |

The tax report carries four kinds of row: a supplier whose federal block disagrees with the
month's consensus (`_check_federal_tax_consensus`), the same for the VREG network ceiling
(`_check_vreg_ceiling_consensus`), and the standing requests to extend each window before it
lapses (`_check_excise_window` and `_check_vreg_ceiling_window`, silent until eight weeks out).
All are supplier- or maintainer-side rather than a break here, which is why they share a thread
and fail no pull request.

A disagreement that has been looked at and decided goes in `_KNOWN_TAX_BLOCKS`
(`scripts/live_check.py`) and is then reported in its own section without filing: the
integration bills both federal levies from the law rather than from the card, so no household is
billed those figures, and an issue a day about a card already read is noise that closing only
makes worse, since the filing script looks for an OPEN issue and opens a new one when it finds
none. Two properties keep the allowance from becoming a mute button. It is keyed on the exact
(supplier, excise, contribution) triple, so a card that moves either figure by a digit files
again, which is how Bolt and Trevion were seen correcting themselves between August and
September. And each entry carries an expiry, all of them 2027-01-01, when the excise steps down
and every card in the country has to be reprinted anyway.

`_KNOWN_VREG_CEILINGS` is the same mechanism for the same reason, and shipped a day later than
the check it quiets: Bolt prints 0,2035480 where the three other suppliers whose cards the
shared reader matches in that month print 0,3472738 (five cards state the figure in all, but
DATS 24 and Ecopower state it as a table column this reader cannot see), and
`resolve_vreg_network_ceiling` bills the regulator's figure regardless, so the card is worth
reporting and not worth failing a run over daily. Keyed on the exact (supplier, ceiling) pair
and expiring with `VREG_NETWORK_CEILING_KNOWN_UNTIL`, which is the month the constant stops
being applied at all and every allowance under it has to be looked at again. A MAJORITY
disagreeing is never allowed: that says the regulator moved and the constant is stale, so it
stays a hard failure naming the constant rather than the cards.

That verdict needs a fleet to be a majority of, so it is held behind a quorum
(`_VREG_CONSENSUS_QUORUM`, two cards PRINTING the sentence, not two agreeing
with the constant: a fleet-wide move leaves nothing in agreement, so a gate on
the agreeing side cannot open in the one case worth reporting). Only four suppliers print the sentence the
shared reader matches and two of them are missing from some months, so without it a month that
captured one stale card and none of the agreeing ones would report that the fleet had moved to
that card, and point the maintainer at Bolt's figure: about 1,7 times too tight, and worth up to
59,43 EUR a year of under-billing if it were adopted. Below the quorum the per-supplier rows
still file, which is the claim the evidence supports. `_check_federal_tax_consensus` guards the
same way, by refusing to rank a top pair only as large as its runner-up.

A figure `_KNOWN_VREG_CEILINGS` allows is left out of that vote and of the quorum's count: it is a
card error somebody has looked at, not a sign that the regulator moved. Counted, Bolt's figure made
one moved card a majority against one agreeing card whenever a third was unread that month, and the
run asked for the constant to be moved instead of naming the card that had changed. A fleet-wide
move still reports without it, three of three.

The extractor issue body keeps only the failures table and the per-supplier metrics block, dropping
the `## All checks` checklist: the full report outgrew GitHub's 65,536-character issue body limit,
which made `gh issue create` fail and file nothing (`.github/workflows/live_check.yml`). A
defensive cap truncates the body at a line boundary near 60,000 bytes in case a mass failure
inflates the failures table itself. The full report is always in the run log.

On `pull_request` events the issue-creation steps are skipped; instead a final step fails the PR
check if any bit other than the catalog-only bit is set (`rc & ~2`), since a new-product signal is
informational, not a regression (`.github/workflows/live_check.yml`). A separate step fails the
run on `rc=8` (harness crash) so a top-level traceback shows red on the Actions tab instead of
ending green (`.github/workflows/live_check.yml`). The retry loop maps a harness that died
before writing its failure list onto that code too: an odd exit used to take the extractor
branch with an empty attempt list and end green as "transient", and an even one broke out as
if the sweep were green and landed on an rc no step matched. `tests/test_live_check_harness.py`
runs the step's own shell out of the workflow file with `python` stubbed to prove it.

### endpoint_probe.yml - Endpoint probe

Manual dispatch only (`.github/workflows/endpoint_probe.yml`), with `urls`, `attempts` and
`timeout` inputs; an empty `urls` probes the suppliers that have timed out in recent live runs. It
runs `scripts/probe_endpoint.py` from a runner and prints the runner's egress address beside the
timings, so the same command run on a workstation answers the question the live-check report cannot:
whether a supplier is slow or the runner is being treated differently (see *Telling a slow supplier
from a blocked runner* above for how to read the two against each other).

Nothing gates on it, and it is deliberately not scheduled. It answers a question someone is asking,
and a supplier being slow today is not a reason to fail a workflow.

### archive_cards.yml - Archive tariff cards

Runs on the daily `cron: "41 5 * * *"` (before the live check, off the hour for the same reason)
and on manual dispatch (`.github/workflows/archive_cards.yml`), with `contents: write` because
it pushes. The dispatch takes three inputs: `backfill_months`, passed to the script as `--backfill`, and
the two booleans `reparse` and `rerender`, passed as the flags of the same names. The schedule
runs with none of them. A dispatch asking for a backfill or a re-render gets a six-hour job
timeout instead of the usual one hour, since either is far more work than the daily walk: a
backfill is one archived card per supplier, contract, region and month, a re-render downloads
and renders every kept card. The install line adds `freezegun` for the replay's clock. It checks out `main` for the script and
clones `be_price_cards` shallow under `tmp/cards`, which `.gitignore` covers, using the
`BE_ELECTRICITY_CARDS` token since this clone is written back to
(`.github/workflows/archive_cards.yml`). It then runs `scripts/archive_cards.py --out
tmp/cards/electricity`, and commits and pushes to that repository only when its tree changed.

The `Keep the cards themselves` step (`.github/workflows/archive_cards.yml`) uploads the
PDFs the script wrote under `tmp/pdfs` to releases of `renaudallard/be_price_cards`, a repository
shared with be_water_prices in which this integration owns the `electricity-` namespace: one
release per month of cards (`electricity-YYYY-MM`, the month the card is for, whatever day it
was captured or mirrored on) with each file named by its SHA-256. Where every file landed is
recorded in the archive's `pdfs.json` before the commit step runs. GitHub caps a release at a thousand assets,
which the first backfill hit when every month's cards were filed under the month of the run;
a month's own cards are about two hundred, so the cap is now far away, but the step still looks
at every release of the month that exists, uploads into the last one while it has room and opens
`electricity-YYYY-MM-2` should it ever fill; a file already present in any of them is not
uploaded again, only recorded. Retention deletes only `electricity-` releases; the water
integration's are not this job's. Where each file landed is merged into the manifest once per month
directory rather than once per file: a backfill day uploads a thousand files, and rewriting the
whole manifest for each took longer than some of the uploads.

The `Warn before the upload token expires` step asks GitHub for the token's expiry (a fine-grained
token reports it in the `github-authentication-token-expiration` response header) and, from two
weeks before it, files an issue under the same `archive-cards` label through
`scripts/file_ci_issue.sh`, fingerprinted on the expiry date so it repeats once a week until the
secret is replaced. A token that reports no expiry, or none at all, files nothing; the failure
issue still covers a token that has already expired. They cannot live in the cards repository's tree: one walk downloads about 100 MB of PDFs
(214 distinct files, measured), so three years would be around 3.5 GB in a repository every clone
of `main` also pulls; and a release on this repository would be offered to HACS users as an update.
The step needs a fine-grained personal access token with contents read and write on the cards
repository in the `BE_ELECTRICITY_CARDS` secret. A release needs a commit to tag, so a repository created
empty is given a first commit by the step itself, once. Without the secret the step says so and exits green: the parsed
cards and their texts still land in the archive, and the PDFs of that day are offered again by the
next run that has the token. Releases older than the retention are deleted on the same cutoff the
script uses for the rows.

The archive lives in its own repository on purpose: three years of daily commits would bury
`main`'s history, race the maintainer's own pushes, and land in every HACS download. The push
goes through the `BE_ELECTRICITY_CARDS` token to `be_price_cards`, so nothing here is started
by it and this repository's own token stays read-only. Concurrency is queued rather than cancelled
(`cancel-in-progress: false`): a manual run overlapping the schedule would otherwise push the
same day twice and lose the second push as non-fast-forward.

Mega has blocked the GitHub runner address range before (its listing fetch timed out only from
Actions, from 2026-07-06 on). On such a day the script gives the supplier up after three network
failures in a row (`_GIVE_UP_AFTER`, `scripts/archive_cards.py`) and skips the rest of its
cards, live and backfill alike, because every further card would cost the same three timeouts and
two sleeps and sixty of them would run the job into its timeout with nothing committed; a parse
failure does not count. The first run, on 2026-09-11, stored all 61 Mega cards, so the block is
not permanent; either way Mega has its own archive and the month cache rarely needs the
repository's copy for it.

A failed run files an issue (`File the failure as an issue`, `.github/workflows/archive_cards.yml`),
which is why the job also has `issues: write`: nobody watches the Actions tab, and a walk that
stored nothing, a refused push or an expired upload token (fine-grained tokens live a year at
most) would otherwise end the archive quietly. The same `scripts/file_ci_issue.sh` the live check
uses, label `archive-cards`: one open issue per problem, the failed step's name as the fingerprint
so the same step failing again within the week adds nothing and a different one is posted at once,
and a body that names that step so the token case is told from the others. A job cancelled
by its timeout runs no further step, which is what the give-up rule above is for.

### autorelease.yml - Autorelease

Runs only on push to `main` that changes
`custom_components/be_electricity_prices/manifest.json` (`.github/workflows/autorelease.yml`),
with `contents: write`. It gates on the Tests + Validate checks so a manifest bump on a red branch
can never publish: the `verify` job **calls** `test.yml` via `workflow_call`
(`.github/workflows/autorelease.yml`), and separate `hacs`/`hassfest` jobs mirror `validate.yml`.
`verify` used to be a hand-copy of `test.yml` carrying a note that the two must be kept in sync;
calling it removes the chance to forget. `test.yml`'s concurrency group includes `github.workflow`
(the **caller's** name) for that reason — without it the standalone Tests run and the one
autorelease calls would share a group on a push to `main` and `cancel-in-progress` would kill the
release's own gate. The `release` job needs all three
(`.github/workflows/autorelease.yml`), then:

1. Extracts the version from `manifest.json` via `jq` and derives `tag=v<version>`
   (`.github/workflows/autorelease.yml`).
2. Skips if the release already exists (`gh release view`, `.github/workflows/autorelease.yml`),
   making the workflow idempotent against re-pushes. It asks for the release rather than the tag
   because the tag is pushed first: when every `gh release create` attempt below fails, the tag
   is already on origin, and a re-run that tested it skipped the release and ended green with
   nothing published. Re-running the failed job now finishes the release
   (`tests/test_autorelease.py` runs the check and release steps out of the workflow against a
   scratch origin and a fake `gh`).
3. Builds `dist/be_electricity_prices.zip` from the component directory, excluding `*.pyc` and
   `__pycache__` (`.github/workflows/autorelease.yml`).
4. Tags, pushes the tag, and runs `gh release create --generate-notes` with the zip attached,
   retrying up to five times with exponential backoff (a past release, v0.5.28, was lost to a 504
   from GitHub's REST API) and treating an already-created release as success
   (`.github/workflows/autorelease.yml`).

The practical consequence: tagging and publishing a GitHub release is fully automatic once a
manifest version bump lands on `main`. Do not tag or create releases by hand.

### Version-bump policy

The release trigger is a change to `manifest.json`'s `version`, so bumping the manifest is
equivalent to shipping a release. Bump it only when a user sees a runtime change: a parser fix, a
new supplier or contract, a pricing correction, a coordinator behaviour change. Even a new feature
takes a PATCH bump; the maintainer sets the exact number. CI-only, test-only, and
documentation-only changes must not touch the manifest version. When in doubt, do not bump; ask the
maintainer.

## Local dev commands before committing

`scripts/gate.sh` runs all of them in one go and is what to use before a push:

```
scripts/gate.sh                     # the whole suite, about 22 minutes on a Pi
scripts/gate.sh tests/test_ebem.py  # one file, for a quick pass
```

It checks a throwaway git worktree of HEAD rather than the working tree. The gate is what protects
a push and takes twenty minutes, so it reads files that are still being edited: two runs were
voided that way on 2026-09-17, one by a test file changing under pytest and one by a commit
shifting the lines an `inspect`-based test reads, and both looked green until the next run
disagreed. Editing while it runs now cannot reach it, and what it verifies is what a push would
publish. The consequence is that uncommitted work is NOT tested: commit first, then gate. The
interpreter, `tmp/actionlint` and the installed plugins come from the real tree, since the
worktree holds tracked files only; without the actionlint binary that one check is skipped and
says so.

The individual invocations, derived from `.github/workflows/test.yml`, if you would rather run
one by hand:

```
ruff check .
ruff format .          # workflow uses `ruff format --check .`; run the formatter locally
mypy --strict custom_components/be_electricity_prices
mypy custom_components/ tests/ scripts/
python scripts/doc_ref_check.py
pytest tests/ -q -n auto --dist loadfile
```

Notes:

- The workflow runs `ruff format --check .` (verify only). Run `ruff format .` locally to apply the
  formatting, then commit; a stray unformatted file fails the check step.
- Both mypy passes matter: `--strict` on production code, and the non-strict pass over
  `tests/`/`scripts/` that also type-checks `live_check.py`.
- `doc_ref_check.py` needs no network or fixtures and reports in seconds, so run it after any doc
  edit: it fails on a pin past the end of its file or on a dead line, and on a rise in the
  rewritable or unanchored-markdown counts. `--verbose` adds the moved-symbol suspects, and
  `--write` repins only what an AST symbol can resolve.
- The suite is split across cores BY FILE, so a file's fixtures are built once in one worker
  rather than scattered across several. Measured on a Raspberry Pi 5 over 2087 tests: 21:52
  serial, 13:27 on two workers, 9:58 on four. Each worker pays one Home Assistant import, which
  is why this is not in `addopts`: on a single file that fixed cost dominates and four workers
  take 2,4x as long as none (310 s against 128 s over 101 tests).
- To iterate on a single provider, target its module WITHOUT the split, for example
  `pytest tests/test_bolt.py -q`.
- The full live check is network-bound and can be run locally with
  `python scripts/live_check.py`; it writes `catalog_report.md`, `tax_report.md`,
  `drift_report.md` and their fingerprint and failure-label files to the repo root and prints the
  extractor report to stdout. It is not part of the pre-commit gate.
- `python scripts/archive_cards.py --out tmp/archive` stores today's cards under `tmp/archive`
  the way the daily workflow stores them in `be_price_cards`. A full walk asks about 250
  cards and takes 21 minutes on a Raspberry Pi, most of it Bolt's and Mega's PDF parses, and
  writes about 7 MB. Not part of the gate either.
- Per the repository conventions, ensure `__pycache__` contents are cleared before committing and
  keep `README.md` and any man page in step with runtime-affecting changes.

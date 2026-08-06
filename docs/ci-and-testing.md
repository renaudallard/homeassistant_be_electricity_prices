# CI and testing

This document covers how the Belgian Electricity Prices integration is tested and released: the
fixture-driven pytest suite (`tests/`), the weekly live extractor harness
(`scripts/live_check.py`) that fetches every supplier's real tariff card and asserts the
extractors still parse, the four GitHub Actions workflows (`test.yml`, `validate.yml`,
`live_check.yml`, `autorelease.yml`), and the exact local commands a contributor runs before
committing. It also spells out the version-bump policy that gates a release.

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
(`pyproject.toml:1`), so `async def test_*` functions run without an explicit `@pytest.mark.asyncio`
decorator and each test gets a fresh event loop.

The integration is a Home Assistant custom component, so the suite depends on
`pytest-homeassistant-custom-component` (which supplies the `hass` fixture and `MockConfigEntry`)
and, for time-pinned tests, `pytest-freezer` (the `freezer` fixture). `tests/conftest.py`
inserts the repo root onto `sys.path` (`tests/conftest.py:40`) so
`custom_components.be_electricity_prices` imports without an installed package, and an autouse
fixture enables custom-component loading for every test that requests `hass`
(`tests/conftest.py:44`).

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
| `tests/test_energyvision.py` | EnergyVision extractor |
| `tests/test_engie.py` | Engie extractor |
| `tests/test_frank.py` | Frank Energie extractor |
| `tests/test_luminus.py` | Luminus extractor |
| `tests/test_mega.py` | Mega extractor |
| `tests/test_octaplus.py` | OCTA+ extractor |
| `tests/test_totalenergies.py` | TotalEnergies extractor |
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
  it cares about (`tests/__init__.py:85`).
- `fixture_text(name, *, layout=False)` reads a fixture PDF and runs it through the real
  extractor, `extract_pdf_text` (pypdf) by default or `extract_pdf_text_layout` (pdfplumber) when
  `layout=True` for the column-positional cards (Bolt, DATS 24, Ecopower, TotalEnergies)
  (`tests/__init__.py:57`). It is `lru_cache`d for the process lifetime because PDF extraction
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

A provider test feeds the fixture text through the same parse path the coordinator uses and
asserts the resulting `SupplierSnapshot` (energy rates, per-DSO overlays, taxes, injection,
publication label). This is why the tests are deterministic and fast while the extractor logic is
still exercised end-to-end against genuine supplier output.

`tests/test_discover.py` drives each supplier's `discover()` against its saved listing snippet
through a minimal `aiohttp.ClientResponse` stand-in, `_FakeResponse` (`tests/test_discover.py:68`),
and asserts the discovered product set matches the registry exactly. A regex regression that drops
a product, or a fixture refresh that grows the catalogue, fails fast.

### Europe/Brussels timezone pin

`tests/conftest.py` has an autouse fixture `_force_brussels_timezone` that pins every test to
`Europe/Brussels` (`tests/conftest.py:52`). This is load-bearing, not cosmetic. The
`pytest-homeassistant-custom-component` `hass` fixture defaults to `US/Pacific`; for a
Belgian-electricity integration that default hides DST transitions, off-peak window boundaries,
and per-month archive bugs that would surface in production. The fixture has two branches:

- If the test requests `hass`, it sets the timezone on the running Home Assistant instance via
  `hass.config.async_set_time_zone("Europe/Brussels")` on the loop `hass` owns
  (`tests/conftest.py:68`).
- Otherwise it swaps `homeassistant.util.dt`'s default timezone for the duration of the test and
  restores it afterwards (`tests/conftest.py:71`).

The fixture stays synchronous on purpose: pytest-asyncio auto mode would otherwise wrap an async
autouse fixture in a second `asyncio.Runner` that cannot run while the `hass` loop is already up.
The comment at `tests/conftest.py:58` documents this constraint. When you write time-sensitive
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
`tests/test_diagnostics.py:81`). That stub does not match the production signature, so the call
site is annotated with `# type: ignore[arg-type]` to keep the non-strict mypy pass clean, as in
`tests/test_button.py:62` and `tests/test_ecopower.py:409`. The convention is to suppress at the
call site with `# type: ignore[arg-type]`, never to relax the production function signature to
accept the stub. `pyproject.toml` sets `explicit_package_bases = true` (`pyproject.toml:12`) so
mypy treats `custom_components/be_electricity_prices` and `tests/` as separate package roots
(neither carries a root `__init__.py`), matching how pytest collects them, and it silences
missing stubs for `pypdf`, `pdfplumber`, and `pytest_homeassistant_custom_component`
(`pyproject.toml:14`).

## scripts/live_check.py

The test suite proves the extractors parse the frozen fixtures. It cannot prove that a supplier
has not silently reworked its live card. `scripts/live_check.py` closes that gap: it walks every
registered `(supplier, contract, region)` tuple, fetches the supplier's real publication over the
network, parses it, and asserts the resulting snapshot is structurally sane (energy populated,
expected DSO keys present, taxes populated, rates inside loose plausibility bounds). It prints a
markdown report to stdout and encodes the outcome in its exit code. It is run daily by
`.github/workflows/live_check.yml` (`scripts/live_check.py:35`).

The script deliberately does not import Home Assistant. `_load_providers()`
(`scripts/live_check.py:62`) synthesises a `be_pkg.providers` package and loads each provider
module by file path, so it can import `providers/*.py` and `providers/base.py` without pulling HA
into scope. It binds the base rate classes (`FixedRates`, `VariableRates`, `DynamicRates`,
`TimeOfUseRates`, `ImpactRates`) for the `isinstance`-based energy validation
(`scripts/live_check.py:88`); class identity matches because every provider imports from the same
loaded `base` module.

### Structure and main functions

```
main()                       scripts/live_check.py:1756  asyncio.run(_run()); rc=8 on harness crash
  _run()                     scripts/live_check.py:1537  load providers, gather checks, render, exit code
    _load_providers()        scripts/live_check.py:62    file-path import of every provider (no HA)
    _attributed_check(...)   scripts/live_check.py:322   per-supplier wait_for + trace attribution
      _check_eneco ...       scripts/live_check.py:437   one _check_<supplier> per registered extractor
      _check_frank                                       (cociter, dats24, ebem, ecofix, ecopower,
      _check_bolt                                         engie, luminus, mega, totalenergies,
      ...                                                 bolt, octaplus, frank, energiebe,
                                                          energyvision)
    _check_catalogs(...)     scripts/live_check.py:1188   run each discover(), flag new product ids
    _fetch_with_retry(...)   scripts/live_check.py:398   transient-only retry with backoff
    _validate_snapshot(...)  scripts/live_check.py:1365  energy + injection shape gates
    _drift_warnings(...)     scripts/live_check.py:1721  latency / byte budget checks
    _render_report(...)      scripts/live_check.py:1508  markdown pass/fail report
```

Each `_check_<supplier>` derives its contract list from the runtime registry (for example
`for cid in (c.id for c in eneco.EXTRACTOR.contracts)`, `scripts/live_check.py:441`) so adding a
product to `EXTRACTOR.contracts` gets it validated here without editing the harness. Every check
asserts the publication label is non-empty, the expected DSO keys for the region are present
(`_FLUVIUS_KEYS`, `_WALLONIA_DSO_KEYS`, or `sibelga` for Brussels), the relevant taxes are
positive, and then calls `_validate_snapshot`.

The federal energy contribution is the exception to "taxes are positive". It is bounds-checked by
`_expect_energy_contribution` (`scripts/live_check.py:506`) instead, which accepts
`[0, 0.01]` EUR/kWh. A `> 0` gate on four suppliers used to enforce it, but the levy was abolished
on 2026-08-01: EBEM's August card failed CI three times over for reporting the zero it actually
prints (issue #49). The upper bound is what the gate was really protecting against — a unit slip
that reads the value 100x too large — and that part still holds.

`_validate_snapshot` (`scripts/live_check.py:1315`) runs two gates:

- `_validate_energy` (`scripts/live_check.py:1375`) dispatches on the energy dataclass type and
  bounds-checks the rate(s). Fixed/variable/TOU/Impact rates must sit in a loose plausibility band
  (the source uses `[0.05, 0.50]` EUR/kWh as an illustrative sanity range); dynamic contracts
  check `factor` in `[0.5, 3.0]` and `base` in `[0, 0.10]` (illustrative); TOU and Impact
  additionally assert band ordering (peak >= transition >= offpeak; pic >= medium >= eco). An
  unrecognised energy class is a failure.
- `_validate_injection` (`scripts/live_check.py:1173`) gates that the feed-in credit parsed and
  kept the right shape. This exists because the coordinator drops the credit entirely when
  `injection` is None, so a relabelled injection row silently zeroes a solar user's credit and
  used to pass CI green (issues #31, F53). The `shape` argument pins expectations: `"none"`
  (region pays no feed-in, injection must be absent), `"monthly"` (`current` set, `factor`/`base`
  None), `"spot"` (`factor`/`base` set), or `"present"` (present, shape unconstrained). Per-contract
  expectations live in `_INJECTION_SHAPE` (`scripts/live_check.py:1241`); the DATS 24 check passes
  `injection_shape` explicitly because its Wallonia card pays no feed-in while its Flanders card is
  monthly-indexed.

### Per-supplier byte and wallclock budgets, and drift issues

An aiohttp `TraceConfig` (`scripts/live_check.py:288`) tags every request with the supplier
currently being checked (via a `ContextVar` set by the `_attributed()` context manager,
`scripts/live_check.py:298`) and accumulates per-supplier fetch count, summed request duration,
body bytes, and failed-attempt count / duration into `METRICS`. These metrics
surface silent slowdowns and PDF-size jumps, both leading indicators that a supplier reworked its
publication, and are appended to the daily report by `_render_metrics`
(`scripts/live_check.py:1476`).

Reading a row correctly needs three facts about which hook feeds which column:

- **Fetches / Fetch time** come from `on_request_end`, which fires once per request that reached
  its final response headers, after the redirect chain and **before** the body is read. So the
  latency figure is time-to-headers, and a 302-to-CDN fetch counts as one.
- **Bytes received** are summed in `_on_response_chunk_received` (`scripts/live_check.py:262`)
  rather than read from `Content-Length`, because that header is None on chunked responses and
  would silently count as zero. `ClientResponse.read()` fires that hook once with the whole body,
  so the count is all-or-nothing: a fetch with a counted request but `-` bytes got its headers and
  then stalled mid-body.
- **Failed (n / s)** comes from `_on_request_exception` (`scripts/live_check.py:286`), which is the
  only hook a request that never produced a response fires. Failures are kept out of the success
  columns deliberately, so the latency budgets below stay calibrated on successful fetches; before
  this counter existed a supplier whose every attempt timed out reported 0 fetches and 0 s and read
  as though it had barely been tried. The hook also prints a `warning:` line naming the exception
  and the url of the hop that actually failed -- which the wrapped `ExtractorError` cannot, since
  providers pass the original url to the fetch helper and a redirected fetch therefore reports only
  that first url whichever hop died.

Two safety caps bound runtime. Each supplier check runs under
`asyncio.wait_for(..., timeout=_SUPPLIER_HARD_TIMEOUT_S)` (`scripts/live_check.py:349`) with a 600s
hard cap (`scripts/live_check.py:327`, raised from 240s when the professional editions roughly
doubled Engie's and Mega's sequential fetch counts), recorded as an extractor failure rather than
propagating so one hung supplier cannot starve the `gather()`. Every latency budget below must stay
under that cap, or the supplier is killed before it can report the drift the budget exists to catch.
The session-level `aiohttp.ClientTimeout(total=60)` (`scripts/live_check.py:1679`) bounds individual
requests.

`_drift_warnings` (`scripts/live_check.py:1713`) compares each supplier's summed fetch time and
total bytes against a budget. The global defaults are `LATENCY_WARN_THRESHOLD_S = 90.0` and
`BYTES_WARN_THRESHOLD = 5_000_000` (`scripts/live_check.py:1622`), with per-supplier overrides in
`_BYTES_BUDGET_OVERRIDES` (`scripts/live_check.py:1639`) for the known-large catalogues (Bolt,
TotalEnergies, Engie, Ecofix, Mega, OCTA+) and `_LATENCY_BUDGET_OVERRIDES`
(`scripts/live_check.py:1669`) for those same multi-fetch suppliers plus Luminus and Eneco, which
are slow per fetch rather than large. Note that `elapsed_s` is the sum of per-request durations,
not true wallclock, so a supplier that fetches concurrently (Bolt fetches its six PDFs with
`asyncio.gather`, `scripts/live_check.py:946`) records the sum of its parallel fetches; the budgets
are sized around that. The synthetic `_catalog` bucket is skipped in drift analysis because it
aggregates every supplier's discovery fetch under one name (`scripts/live_check.py:1720`). When a
budget is blown, `live_check.yml` opens or updates a dedicated drift issue (see below). Tuning a
false-firing drift alert means adjusting the override, not the code.

A supplier whose extractor already failed this run is skipped too (`scripts/live_check.py:1727`,
against the set `_failed_suppliers` reads off the check labels, `scripts/live_check.py:1700`). The
failure is both the louder signal and the usual cause of the numbers: a supplier that reworks its
cards changes their size, and because bit 0 makes the workflow retry the whole run for an hour,
every other supplier gets several more rolls against its budget with drift judged on whichever
attempt landed last. Issue #55 is the worked example: Ecofix rasterised its August 2026 cards, which
pushed each PDF from ~1.1 MB to ~1.76 MB and blew the byte budget, while six retried attempts gave
Eneco enough rolls to land one 96.4s outlier against the then-90s default. One supplier-side event,
two suppliers named, and it would have refiled every day for as long as Ecofix stayed broken. The
skipped measurement is printed to stderr so a budget can still be tuned from the run log without a
rerun.

### Exit codes and the two report side-channels

`_run()` (`scripts/live_check.py:1537`) splits checks into `extractor` and `catalog` kinds. The
extractor report (with the metrics block) is printed to stdout, which the workflow captures. The
catalog diff is written to `catalog_report.md` and the drift warnings to `drift_report.md` at the
repo root (`scripts/live_check.py:1638`), each a side-channel the workflow reads to file a separate
issue so the three failure modes never conflate in one thread.

The exit code is bit-encoded (`scripts/live_check.py:1648`):

| Bit | Value | Meaning | Retried by workflow? |
| --- | --- | --- | --- |
| 0 | 1 | extractor failure (fetch or parse regression) | yes |
| 1 | 2 | catalog signal (a new product appeared at a supplier) | no |
| 2 | 4 | drift alert (latency or byte budget blown) | no |
| - | 8 | harness crash (top-level Python exception in the script) | no |

`rc=8` is deliberately outside the 1/2/4 bit space (`scripts/live_check.py:1764`) so the workflow
does not open a "supplier extractor broken" issue for what is actually a bug in the harness.

### The transient-only retry helper

`_fetch_with_retry(factory, *, attempts=3)` (`scripts/live_check.py:398`) calls `factory()`, and on
a transient network failure retries with a short backoff (`_RETRY_BACKOFF_S = (1.0, 3.0)`). A
failure is "transient" only if it is a bare `TimeoutError` or an `ExtractorError` whose message the
shared `providers/_pdf.is_transient_fetch_error` predicate classifies as transient (a wrapped
"network error fetching" or an HTTP 5xx/408/429/403). A 404/410 (card renamed or withdrawn) or any
parse error propagates immediately so a real regression is not masked by retries. A fresh awaitable
is built via `factory()` per attempt (awaitables are single-use), which is why callers pass
`functools.partial(...)` rather than a pre-created coroutine. The transient predicate is imported
from `providers/_pdf.py` (`scripts/live_check.py:95`) so the harness and the coordinator classify
errors identically and cannot drift apart.

This retry helper is CI-only. Do not port it into `coordinator.py`: the coordinator has its own
retry/backoff behaviour, and duplicating this logic there would create two divergent policies.

### Coverage-gap caveat

The live check only flags what it asserts. A nullable field that no gate covers can pass green even
when it is wrong. This is exactly the class of bug `_validate_injection` was added to catch (a
relabelled injection row that zeroed a solar credit passed CI for all but two suppliers before the
shape gate existed, issues #31/F53). When you add a field to a snapshot, add a gate for it here;
green does not mean covered.

## GitHub workflows

Four workflows live under `.github/workflows/`.

### test.yml - Tests

Runs on push to `main`, on every pull request, and on manual dispatch (`.github/workflows/test.yml:3`).
It pins Python 3.13 and installs a pinned toolchain: `homeassistant==2026.2.3`,
`pytest-homeassistant-custom-component==0.13.316` and `ruff==0.16.0` (plus `pytest-freezer==0.4.9`,
`pypdf`, `pdfplumber`, `defusedxml`), so an upstream HA-core, test-shim or linter release cannot
silently turn the suite red on `main` (`.github/workflows/test.yml:31`). The lint rule set is itself
pinned in `pyproject.toml` (`[tool.ruff.lint] select`), so a ruff upgrade cannot expand what is
linted for; `[tool.ruff.format] exclude` keeps the formatter off Markdown. The steps are:

| Step | Command | Notes |
| --- | --- | --- |
| Lint | `ruff check .` then `ruff format --check .` | `.github/workflows/test.yml:42` |
| Type check (production) | `mypy --strict custom_components/be_electricity_prices` | strict; production code must be strict-clean (`.github/workflows/test.yml:47`) |
| Type check (tests + scripts) | `mypy custom_components/ tests/ scripts/` | non-strict; covers `live_check.py` so a regression surfaces on PR rather than in the next 06:00 UTC scheduled run (`.github/workflows/test.yml:48`) |
| Tests | `pytest tests/ -q` | `.github/workflows/test.yml:56` |

`concurrency` cancels a stale push/PR run when a new commit lands (`.github/workflows/test.yml:11`).
The non-strict pass over `tests/` and `scripts/` is why the `# type: ignore[arg-type]` convention
described above exists.

### validate.yml - Validate

Runs on push to `main`, on pull requests, on a daily `cron: "0 6 * * *"`, and on manual dispatch
(`.github/workflows/validate.yml:3`). Two independent jobs:

- `hacs` runs `hacs/action@main` with `category: integration` (HACS repository requirements).
- `hassfest` runs `home-assistant/actions/hassfest@master` (Home Assistant's manifest/brands/services
  validation).

These validate packaging and manifest conformance, not runtime behaviour.

### live_check.yml - Live extractor check

Runs on the daily `cron: "0 6 * * *"` (06:00 UTC is 07:00/08:00 Belgian local, after suppliers'
overnight publication), on manual dispatch, and on pull requests that touch `providers/**`,
`scripts/live_check.py`, or the workflow itself (`.github/workflows/live_check.yml:3`). It needs
`issues: write` to file drift/catalog/extractor issues (`.github/workflows/live_check.yml:14`).

The single `check` job installs the pinned HA version (needed because `providers/_pdf.py` imports
`homeassistant.util.dt`) and runs `scripts/live_check.py` inside a two-tier retry loop
(`.github/workflows/live_check.yml:52`). The retry exists so an issue is filed only when a supplier
is still broken roughly an hour after first detection, not for a transient CDN blip (issue #30):
seven attempts with delays `10 30 60 120 300 3000` seconds, bounded by a 5400s wall-clock deadline
so the job always reaches the issue-creation steps before the 120-minute job timeout. Only a bit-0
(extractor) failure is retried; catalog and drift signals are stable and break out immediately.

The job then branches on the captured `rc` to open or update three distinct, label-deduplicated
issues (dedup is on a deterministic label, not a title substring, so a manually opened issue cannot
catch these comments):

| rc bits set | Step | Label | Issue title prefix |
| --- | --- | --- | --- |
| bit 0 (rc 1/3/5/7) | Open or update extractor-broken issue | `live-check-extractor` | `[live-check] supplier extractor broken` |
| bit 2 (rc 4/5/6/7) | Open or update drift issue | `live-check-drift` | `[live-check] supplier drift detected` |
| bit 1 (rc 2/3/6/7) | Open or update new-products issue | `live-check-catalog` | `[live-check] new supplier products detected` |

The extractor issue body keeps only the failures table and the per-supplier metrics block, dropping
the `## All checks` checklist: the full report outgrew GitHub's 65,536-character issue body limit,
which made `gh issue create` fail and file nothing (`.github/workflows/live_check.yml:134`). A
defensive cap truncates the body at a line boundary near 60,000 bytes in case a mass failure
inflates the failures table itself. The full report is always in the run log.

On `pull_request` events the issue-creation steps are skipped; instead a final step fails the PR
check if any bit other than the catalog-only bit is set (`rc & ~2`), since a new-product signal is
informational, not a regression (`.github/workflows/live_check.yml:272`). A separate step fails the
run on `rc=8` (harness crash) so a top-level traceback shows red on the Actions tab instead of
ending green (`.github/workflows/live_check.yml:281`).

### autorelease.yml - Autorelease

Runs only on push to `main` that changes
`custom_components/be_electricity_prices/manifest.json` (`.github/workflows/autorelease.yml:3`),
with `contents: write`. It gates on the Tests + Validate checks so a manifest bump on a red branch
can never publish: the `verify` job **calls** `test.yml` via `workflow_call`
(`.github/workflows/autorelease.yml:25`), and separate `hacs`/`hassfest` jobs mirror `validate.yml`.
`verify` used to be a hand-copy of `test.yml` carrying a note that the two must be kept in sync;
calling it removes the chance to forget. `test.yml`'s concurrency group includes `github.workflow`
(the **caller's** name) for that reason — without it the standalone Tests run and the one
autorelease calls would share a group on a push to `main` and `cancel-in-progress` would kill the
release's own gate. The `release` job needs all three
(`.github/workflows/autorelease.yml:72`), then:

1. Extracts the version from `manifest.json` via `jq` and derives `tag=v<version>`
   (`.github/workflows/autorelease.yml:81`).
2. Skips if the tag already exists (`.github/workflows/autorelease.yml:92`), making the workflow
   idempotent against re-pushes.
3. Builds `dist/be_electricity_prices.zip` from the component directory, excluding `*.pyc` and
   `__pycache__` (`.github/workflows/autorelease.yml:101`).
4. Tags, pushes the tag, and runs `gh release create --generate-notes` with the zip attached,
   retrying up to five times with exponential backoff (a past release, v0.5.28, was lost to a 504
   from GitHub's REST API) and treating an already-created release as success
   (`.github/workflows/autorelease.yml:111`).

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

Run the same four gates the `test.yml` job runs, from the repo root, before committing. These are
the exact invocations derived from `.github/workflows/test.yml`:

```
ruff check .
ruff format .          # workflow uses `ruff format --check .`; run the formatter locally
mypy --strict custom_components/be_electricity_prices
mypy custom_components/ tests/ scripts/
pytest tests/ -q
```

Notes:

- The workflow runs `ruff format --check .` (verify only). Run `ruff format .` locally to apply the
  formatting, then commit; a stray unformatted file fails the check step.
- Both mypy passes matter: `--strict` on production code, and the non-strict pass over
  `tests/`/`scripts/` that also type-checks `live_check.py`.
- To iterate on a single provider, target its module, for example
  `pytest tests/test_bolt.py -q`.
- The full live check is network-bound and can be run locally with
  `python scripts/live_check.py`; it writes `catalog_report.md` and `drift_report.md` to the repo
  root and prints the extractor report to stdout. It is not part of the pre-commit gate.
- Per the repository conventions, ensure `__pycache__` contents are cleared before committing and
  keep `README.md` and any man page in step with runtime-affecting changes.

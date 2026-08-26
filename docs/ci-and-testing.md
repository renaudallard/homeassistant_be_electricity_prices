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

## scripts/doc_ref_check.py

The docs pin roughly 760 `file.py:line` references. They rot whenever a module grows, and a
stale pin is worse than no pin: it sends a reader to a line that now says something else.
`scripts/doc_ref_check.py` resolves each one by CONTENT, never by offset arithmetic. The docs
almost always name the symbol a reference points at, in backticks, on the same line
(``_extract_energy`` (`_mega_cards.py:254`)), so the checker resolves that symbol's real definition
line from an AST index and, with `--write`, repins it.

It runs in the `test` job (`.github/workflows/test.yml:62`), before the suite, and needs no
network or fixtures.

**It fails the build on a reference that is provably broken** (past the end of the file it
names, or landing on a line that holds nothing) **and on any rise in the rewritable or
unanchored-markdown counts**. The rest is printed and left to a human, because each has a
legitimate form the checker cannot distinguish:

| Report | Fails CI | Why not automatic |
| --- | --- | --- |
| past EOF (plain, range, continuation, or markdown) | yes | the file has no such line; nothing to argue about |
| `markdown BROKEN` | yes | the pin lands on a blank line, a bare fence, or a table rule, so it points at nothing whatever the prose claims |
| `anchored+rewritable` | only above the baseline | the anchor heuristic takes the nearest preceding backticked identifier, which on a dense sentence is often not the subject. Read the list before running `--write` |
| `markdown unanchored` | only above the baseline | a markdown target has no symbol to resolve, so the word score is a smoke test rather than a proof; a claim can also have no good passage to point at |
| `range pins unanchored` | only above the baseline | the span holds none of the identifiers its sentence names, which is usually rot but is also how a deliberate cross-file or sub-region pin looks |
| `moved-symbol suspects` | no | the same shape is also a correct reference to a USE site (`CONF_CONTRACT` (`config_flow.py:163`) is where the flow reads it, not where `const` defines it). ~63 of these are expected; `--verbose` lists them |

No single rewritable reference is provably stale, so none can gate on its own: four of the five
on `main` sit on a line that does not even mention the symbol the prose names, being deliberate
pins at the code implementing the behaviour. The COUNT still gates, because it is stable for
that class - an ambiguous pin stays ambiguous however the file moves - and rises only when pins
that used to resolve stop resolving. `_REWRITABLE_BASELINE` (`scripts/doc_ref_check.py:55`)
freezes it. Without this, a branch that inserted three import lines shifted 98 pins (764 correct
down to 666) and the checker still exited 0. Raise the baseline only for a pin deliberately
aimed at an implementation site, naming it in the commit message; lower it when one is resolved,
which the checker prints a note asking for.

Five reference forms exist and all five are checked. That matters because for a long time only
the first was, and the other four rotted invisibly behind a clean run: 28 stale ranges (21 of
them predating the refactor that exposed them), 4 stale continuations, 31 references to a
symbol that had moved module while its old line still landed inside the now-shorter file, and
12 of the glossary's 15 `README.md:line` pins, some off by over 100 lines. Ranges
and continuations are never auto-rewritten: the end of a span is not derivable from an anchor
symbol, and inventing one is worse than leaving a visible stale pin.

Markdown pins are the fifth form, and they need a different anchor: `README.md` has no symbols
to index, so `MDREF` (`scripts/doc_ref_check.py:81`) scores each pin on the DISTINCTIVE words its
claim shares with the passage it lands on. A word is distinctive when it appears on at most 3% of
the target file's lines, which drops "the" and "energy" and keeps "energiefonds" and "picker";
the passage is the paragraph or list item around the pinned line
(`_md_passage` (`scripts/doc_ref_check.py:228`)), because README prose is hard-wrapped and the
claim rarely fits on one line. Fewer than
`_MD_MIN_SHARED` (`scripts/doc_ref_check.py:100`) shared words is reported, never rewritten: there
is no AST truth to rewrite to. The threshold was measured against the pins as they stood before
the 2026-08-17 repin, where all 12 stale ones scored 3 or less and the three that still resolved
scored 4, 7 and 14.
`_MD_UNANCHORED_BASELINE` (`scripts/doc_ref_check.py:107`) tolerates the one pin on `main` with no
better target: the glossary's TSO row defines Elia and the transmission charge, which README never
states.

`_RANGE_UNANCHORED_BASELINE` covers the 36 range pins whose span legitimately holds none of
the identifiers around them. Two shapes account for all of them: a pin that crosses files on
purpose (the prose names a function in one module and points the reader at the constants it
reads in another, as `_resolve_daily_kwh` does with the wiring block in `const.py`), and a pin
that targets part of a block rather than the whole definition. Until the 2026-08 sweep the only
thing checked about a range was that neither end ran past EOF, so a span could sit on entirely
unrelated code and pass: 154 of 676 did, including five of the eight into `const.py`. The sweep
repinned 154 of them, 113 mechanically where the sentence's symbol had exactly one definition
and 41 by hand.

A table row whose reference is a bare number in a "Line" column (`| `_store` | ... | 859 |`)
is invisible to all of this. Those are checked by hand when the file they point into changes.

## scripts/live_check.py

The test suite proves the extractors parse the frozen fixtures. It cannot prove that a supplier
has not silently reworked its live card. `scripts/live_check.py` closes that gap: it walks every
registered `(supplier, contract, region)` tuple, fetches the supplier's real publication over the
network, parses it, and asserts the resulting snapshot is structurally sane (energy populated,
expected DSO keys present, taxes populated, rates inside loose plausibility bounds). It prints a
markdown report to stdout and encodes the outcome in its exit code. It is run daily by
`.github/workflows/live_check.yml` (`scripts/live_check.py:35`).

The script deliberately does not import Home Assistant. `_load_providers()`
(`scripts/live_check.py:89`) synthesises a `be_pkg.providers` package and loads each provider
module by file path, so it can import `providers/*.py` and `providers/base.py` without pulling HA
into scope. It binds the base rate classes (`FixedRates`, `VariableRates`, `DynamicRates`,
`TimeOfUseRates`, `ImpactRates`) for the `isinstance`-based energy validation
(`scripts/live_check.py:89`); class identity matches because every provider imports from the same
loaded `base` module.

### Structure and main functions

```
main()                       scripts/live_check.py:1760  asyncio.run(_run()); rc=8 on harness crash
  _run()                     scripts/live_check.py:1537  load providers, gather checks, render, exit code
    _load_providers()        scripts/live_check.py:62    file-path import of every provider (no HA)
    _attributed_check(...)   scripts/live_check.py:322   per-supplier wait_for + trace attribution
      _check_eneco ...       scripts/live_check.py:437   one _check_<supplier> per registered extractor
      _check_frank                                       (cociter, dats24, ebem, ecofix, ecopower,
      _check_bolt                                         engie, luminus, mega, totalenergies,
      ...                                                 bolt, octaplus, frank, energiebe,
                                                          energyvision)
    _check_catalogs(...)     scripts/live_check.py:1188   run each discover(), flag new product ids
    _check_card_freshness()  scripts/live_check.py:1240   resolved card == newest advertised
    _fetch_with_retry(...)   scripts/live_check.py:398   transient-only retry with backoff
    _validate_snapshot(...)  scripts/live_check.py:1365  energy + injection shape gates
    _drift_warnings(...)     scripts/live_check.py:1725  latency / byte budget checks
    _render_report(...)      scripts/live_check.py:1508  markdown pass/fail report
```

### Card freshness

`_check_card_freshness` (`scripts/live_check.py:1623`) asks a question no other check here asks:
not "did the fetch work" but "is this the card the supplier is currently advertising". A superseded
card downloads, parses and validates exactly like a current one, so a stale URL reads as a green
run -- Bolt billed June's variable formula for ten weeks behind a passing board, and Ecopower served
January's tax block for eleven days after renaming its dynamic card to `YYYYMMDD`.

The mechanism is a deliberate asymmetry: `_expect_newest_card` (`scripts/live_check.py:1274`) scans
the same listing page the extractor does, but with a **looser** pattern. When a supplier changes the
filename shape, the extractor's strict pattern stops seeing the new file and keeps resolving the old
one; the loose pattern still sees it, and the mismatch fails the run.

### Which suppliers are covered, and which deliberately are not

The gate covers the **eight supplier-families that pick a card from a set of several advertised
ones** -- Bolt, Ecopower (definitive + dynamic), Mega, Eneco, EBEM, Cociter (variable + dynamic),
Frank and EnergyVision (one row per product code), twelve rows in all. That shape is the one that
can silently resolve an older card, because the older card is still there and still parses.

OCTA+, TotalEnergies, Engie and Luminus get no freshness ROW, because each constructs one URL per
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

The entry is keyed on the **contract**, not the supplier, so Ecopower's dynamic card is still held
to the current month - exempting Ecopower wholesale would have re-hidden the bug fixed in 0.12.5.

That allowance rests on a publishing convention rather than a date the supplier declares, so
nothing can expire it the way `deprecated_until` does. It carries `_PERIOD_LAG_REVIEW_BY` instead,
enforced by a **test** rather than at runtime: a runtime expiry would start failing on perfectly
normal arrears, which is a false alarm by construction. When that date passes, CI fails with an
instruction to re-verify how the supplier publishes and then move the date. Same convention as the
re-verify note in `providers/bolt.py`.

A supplier on its way out is handled differently, and not by name. `_DEPRECATED_UNTIL` is bound
from each `EXTRACTOR.deprecated_until` at load, so the withdrawal date is declared once, on the
supplier, and this check reads it:

- **up to that date** the supplier is still selling and an older card proves nothing, so the check
  records no row at all;
- **after it** the supplier has left the market. Its final card stays up and stays stale forever,
  which is real and worth showing, but no change to this repository can fix it - so the row fails
  and is marked `expected`, which keeps it in the report without setting the extractor bit or
  refiling an issue every night. Exactly the treatment an unreadable card already gets.

The point of deriving it is that the allowance ends when the withdrawal does. A supplier name
listed in `_PERIOD_EXEMPT` would go on suppressing the check long after the reason for it expired,
and nobody would notice.

An unreadable label is reported but does not fail: unknown is not evidence of staleness. The label
parser is unicode-aware on purpose - a character class that forgets the `u` in `août` silently
fails to read 104 of the 236 live labels, and since an unreadable label is skipped, the check would
have covered almost nothing while looking green.

Mega's nine *professional* contracts get a **different** check, `_check_mega_professional`, because
they have no advertised set at all: Mega never links the B2B cards from any page, so there is no
"newest advertised" to compare against. The CDN answers for itself instead - a published month
returns `application/pdf`, an unpublished one a `text/html` stub under the same 200 - so the check
is one HEAD per (contract, region), 27 in all, downloading nothing.

What makes it worth checking is that `fetch` silently rolls back one month when the current card is
missing. Four of the nine professional contracts are variable or dynamic, so last month's card
carries last month's index: the prices are *wrong*, not merely old, and nothing else in the run
would say so.

Early in a month that rollback is correct behaviour rather than a defect, so the check only fails
past `_PRO_PUBLICATION_GRACE_DAYS`. Mega does not publish ahead - next month's URL is a stub today,
measured - so failing without that grace would file an issue every single month. Within the grace
the row passes and records how many cards are not yet up, which is the honest report: the fallback
is in use, and that is fine for now.

### The stamps do not sort

Every one of the eight formats fails a naive `max()`, each at a different boundary, so each has its
own key (`scripts/live_check.py:1257` onwards): Mega's `MMYYYY` is month-major (`122026` outranks
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
Most of them -- Ecopower, Mega, Eneco, EBEM, Cociter, EnergyVision, Frank -- **raise** when the page
will not load or carries no card, so their own extractor row already reports the breakage and this
gate records a pass rather than duplicating it. Bolt's `_resolve_variable_suffix` instead falls back
to `_VARIABLE_SUFFIX_FALLBACK`: the card still downloads, still parses, and `_check_bolt` stays
green while that constant quietly becomes the pin this whole gate exists to prevent. So for Bolt an
unreadable *or* reshaped listing is a **failure** (`resolver_falls_back=True`), and it is the only
signal that would report it. Two suppliers in this repo have lost or blocked their listing for
weeks, so this is not hypothetical; a transient blip still files nothing, because the workflow only
opens an issue for a check that fails every retry.

Matching is case-**insensitive**. Every provider module compiles its card patterns with
`re.IGNORECASE`, so a case-sensitive gate is stricter than the extractor it audits in that one
dimension and goes blind exactly where the extractor still sees.

The gate catches only the exceptions a provider raises for a failed fetch. Anything else -- a
renamed symbol, a changed signature -- propagates and is recorded as a failure, because swallowing
those made the gate report green in exactly the case where it had stopped working.

Each `_check_<supplier>` derives its contract list from the runtime registry (for example
`for cid in (c.id for c in eneco.EXTRACTOR.contracts)`, `scripts/live_check.py:610`) so adding a
product to `EXTRACTOR.contracts` gets it validated here without editing the harness. Every check
asserts the publication label is non-empty, the expected DSO keys for the region are present
(`_FLUVIUS_KEYS`, `_WALLONIA_DSO_KEYS`, or `sibelga` for Brussels), the relevant taxes are
positive, and then calls `_validate_snapshot`.

The federal energy contribution is the exception to "taxes are positive". It is bounds-checked by
`_expect_energy_contribution` (`scripts/live_check.py:599`) instead, which accepts
`[0, 0.01]` EUR/kWh. A `> 0` gate on four suppliers used to enforce it, but the levy was abolished
on 2026-08-01: EBEM's August card failed CI three times over for reporting the zero it actually
prints (issue #49). The upper bound is what the gate was really protecting against — a unit slip
that reads the value 100x too large — and that part still holds.

`_validate_snapshot` (`scripts/live_check.py:2371`) runs two gates:

- `_validate_energy` (`scripts/live_check.py:2432`) dispatches on the energy dataclass type and
  bounds-checks the rate(s). Fixed/variable/TOU/Impact rates must sit in a loose plausibility band
  (the source uses `[0.05, 0.50]` EUR/kWh as an illustrative sanity range); dynamic contracts
  check `factor` in `[0.5, 3.0]` and `base` in `[0, 0.10]` (illustrative); TOU and Impact
  additionally assert band ordering (peak >= transition >= offpeak; pic >= medium >= eco). An
  unrecognised energy class is a failure.
- `_validate_injection` (`scripts/live_check.py:1886`) gates that the feed-in credit parsed and
  kept the right shape. This exists because the coordinator drops the credit entirely when
  `injection` is None, so a relabelled injection row silently zeroes a solar user's credit and
  used to pass CI green (issues #31, F53). The `shape` argument pins expectations: `"none"`
  (region pays no feed-in, injection must be absent), `"monthly"` (`current` set, `factor`/`base`
  None), `"spot"` (`factor`/`base` set), `"spp"` (a formula indexed on the solar-weighted monthly
  mean: `current`, `factor`/`base` AND `spp_indexed` all set, with the coefficients bounds-checked),
  `"month"` (the same on the plain arithmetic monthly mean, flagged `month_indexed`),
  `"triplet"` (a per-slot peak/transition/offpeak feed-in tariff, which only Empower Flextime has),
  or `"present"` (`factor`/`base` set). Per-contract
  expectations live in `_INJECTION_SHAPE`; the DATS 24 check passes
  `injection_shape` explicitly because its Wallonia card pays no feed-in while its Flanders card
  carries the BE_spotSPP formula.

  `"monthly"` and its two indexed siblings pull in opposite directions, so which one a card gets
  is a statement about the contract, not a formality. `"monthly"` says the card's printed figure
  IS the rate and no coefficient may ever reach the pricing engine. `"spp"` / `"month"` say the
  printed figure is last month's estimate and the coefficients are what settles the bill, so
  losing them is the mis-credit. Five cards moved from the first to the second over this batch
  (Eneco Power Fix / Flex, EBEM Variabel / B@sic+, DATS 24 Groen Variabel), and the two
  EnergyVision fixed cards followed.

  The shape assertion runs whatever else the card prints. It used to hang off the `else` of the
  indicative's range check, so on the several dynamic cards that publish BOTH a formula and an
  indicative it was only tested while the indicative happened to be absent: a redesign that dropped
  the formula and kept the indicative passed green while the credit silently went flat. TOU and
  Impact cards are derived as `"monthly"` rather than `"present"` for the same reason - a time-of-use
  ENERGY leg does not make the feed-in credit per-slot, and asserting a `factor`/`base` they never
  carry would have been noise rather than a gate.

### Per-supplier byte and wallclock budgets, and drift issues

An aiohttp `TraceConfig` (`scripts/live_check.py:288`) tags every request with the supplier
currently being checked (via a `ContextVar` set by the `_attributed()` context manager,
`scripts/live_check.py:347`) and accumulates per-supplier fetch count, summed request duration,
body bytes, and failed-attempt count / duration into `METRICS`. These metrics
surface silent slowdowns and PDF-size jumps, both leading indicators that a supplier reworked its
publication, and are appended to the daily report by `_render_metrics`
(`scripts/live_check.py:1476`).

Reading a row correctly needs three facts about which hook feeds which column:

- **Fetches / Fetch time** come from `on_request_end`, which fires once per request that reached
  its final response headers, after the redirect chain and **before** the body is read. So the
  latency figure is time-to-headers, and a 302-to-CDN fetch counts as one.
- **Bytes received** are summed in `_on_response_chunk_received` (`scripts/live_check.py:305`)
  rather than read from `Content-Length`, because that header is None on chunked responses and
  would silently count as zero. `ClientResponse.read()` fires that hook once with the whole body,
  so the count is all-or-nothing: a fetch with a counted request but `-` bytes got its headers and
  then stalled mid-body.
- **Failed (n / s)** comes from `_on_request_exception` (`scripts/live_check.py:329`), which is the
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
The session-level `aiohttp.ClientTimeout(total=60)` (`scripts/live_check.py:2153`) bounds individual
requests.

`_drift_warnings` (`scripts/live_check.py:2864`) compares each supplier's summed fetch time and
total bytes against a budget. The global defaults are `LATENCY_WARN_THRESHOLD_S = 90.0` and
`BYTES_WARN_THRESHOLD = 5_000_000` (`scripts/live_check.py:1672`), with per-supplier overrides in
`_BYTES_BUDGET_OVERRIDES` (`scripts/live_check.py:2751`) for the known-large catalogues (Bolt,
TotalEnergies, Engie, Ecofix, Mega, OCTA+) and `_LATENCY_BUDGET_OVERRIDES`
(`scripts/live_check.py:1720`) for those same multi-fetch suppliers plus Luminus, Eneco and EBEM,
which are slow per fetch rather than large. Note that `elapsed_s` is the sum of per-request
durations, not true wallclock, so a supplier that fetches concurrently (Bolt fetches its six PDFs
with `asyncio.gather`, `scripts/live_check.py:966`) records the sum of its parallel fetches; the
budgets are sized around that. The synthetic `_catalog` bucket is skipped in drift analysis because
it aggregates every supplier's discovery fetch under one name (`scripts/live_check.py:1810`). When a
budget is blown, `live_check.yml` opens or updates a dedicated drift issue (see below). Tuning a
false-firing drift alert means adjusting the override, not the code.

A supplier whose extractor already failed this run is skipped too (`scripts/live_check.py:2370`,
against the set `_failed_suppliers` reads off the check labels, `scripts/live_check.py:2851`). The
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
repo root (`scripts/live_check.py:2432`), each a side-channel the workflow reads to file a separate
issue so the three failure modes never conflate in one thread.

The exit code is bit-encoded (`scripts/live_check.py:1648`):

| Bit | Value | Meaning | Retried by workflow? |
| --- | --- | --- | --- |
| 0 | 1 | extractor **regression** (fetch or parse), excluding unreadable cards | yes |
| 1 | 2 | catalog signal (a new product appeared at a supplier) | no |
| 2 | 4 | drift alert (latency or byte budget blown) | no |
| - | 8 | harness crash (top-level Python exception in the script) | no |

`rc=8` is deliberately outside the 1/2/4 bit space (`scripts/live_check.py:1768`) so the workflow
does not open a "supplier extractor broken" issue for what is actually a bug in the harness.

### Unreadable cards do not gate bit 0

A supplier that publishes its tariff card as page images fails every check against it, forever,
and no change to this repository can fix it. Counting that as a regression meant one such supplier
set bit 0 on every run, drove the workflow's retry loop to exhaustion, and refiled a fresh issue
each time the previous one was closed (issues #53, #56 and #58 all carried the same six Ecofix
rows). It also handed every other supplier seven rolls of the dice at a transient timeout, which
is where the collateral rows in those issues came from.

`_record` (`scripts/live_check.py:478`) marks such a check `expected`, and `_extractor_regressions`
(`scripts/live_check.py:2371`) is the single definition of what gates CI. The classification reads
the exception type the fetch sites already write into the detail string
(`CardNotReadableError`, raised by `providers/_pdf.py`), so it follows the card actually
published rather than a hardcoded supplier list: a supplier that goes back to publishing text
starts gating again by itself, with no edit here. The rows still appear in the report, under their
own heading and counted separately in the headline, so a green run never hides them.

### The transient-only retry helper

`_fetch_with_retry(factory, *, attempts=3)` (`scripts/live_check.py:551`) calls `factory()`, and on
a transient network failure retries with a short backoff (`_RETRY_BACKOFF_S = (1.0, 3.0)`). A
failure is "transient" only if it is a bare `TimeoutError` or an `ExtractorError` whose message the
shared `providers/_pdf.is_transient_fetch_error` predicate classifies as transient (a wrapped
"network error fetching", a "storage error fetching" from an object store refusing the read, or an
HTTP 5xx/408/429/403). A 404/410 (card renamed or withdrawn) or any parse error propagates
immediately so a real regression is not masked by retries. A fresh awaitable
is built via `factory()` per attempt (awaitables are single-use), which is why callers pass
`functools.partial(...)` rather than a pre-created coroutine. The transient predicate is imported
from `providers/_pdf.py` (`scripts/live_check.py:135`) so the harness and the coordinator classify
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

Run the same five gates the `test.yml` job runs, from the repo root, before committing. These are
the exact invocations derived from `.github/workflows/test.yml`:

```
ruff check .
ruff format .          # workflow uses `ruff format --check .`; run the formatter locally
mypy --strict custom_components/be_electricity_prices
mypy custom_components/ tests/ scripts/
python scripts/doc_ref_check.py
pytest tests/ -q
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
- To iterate on a single provider, target its module, for example
  `pytest tests/test_bolt.py -q`.
- The full live check is network-bound and can be run locally with
  `python scripts/live_check.py`; it writes `catalog_report.md` and `drift_report.md` to the repo
  root and prints the extractor report to stdout. It is not part of the pre-commit gate.
- Per the repository conventions, ensure `__pycache__` contents are cleared before committing and
  keep `README.md` and any man page in step with runtime-affecting changes.

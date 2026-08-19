#!/usr/bin/env python3
# Copyright (c) 2026, Renaud Allard <renaud@allard.it>
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# 1. Redistributions of source code must retain the above copyright notice,
#    this list of conditions and the following disclaimer.
#
# 2. Redistributions in binary form must reproduce the above copyright notice,
#    this list of conditions and the following disclaimer in the documentation
#    and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

"""Live end-to-end check of every supplier extractor.

Walks every (supplier, contract) tuple, hits the supplier's real
publication, parses the result, and verifies the snapshot is structurally
sane: energy populated, expected DSO keys present, taxes populated, rates
inside loose plausibility bounds. Prints a markdown report to stdout and
exits non-zero on the first failure.

Run by ``.github/workflows/live_check.yml`` daily; on persistent failure
the workflow opens or updates a GitHub issue with this report attached.
"""

from __future__ import annotations

import asyncio
import importlib.util as iu
import re
import sys
import time
import traceback
import types
from collections.abc import Awaitable, Callable, Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date, datetime
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import TypeVar
from zoneinfo import ZoneInfo

import aiohttp

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "custom_components" / "be_electricity_prices"


# Every supplier the live check covers, in report order. The single list the
# loader, the module map and the gather are all driven from: these used to be
# five parallel enumerations, and _load_providers returned a bare 15-tuple that
# _run destructured BY POSITION. Inserting a supplier into one list and not the
# other silently rebound every module after the insertion point, so the check
# would fetch one supplier's card and assert it under another's name while
# reporting green.
_SUPPLIERS: tuple[str, ...] = (
    "eneco",
    "cociter",
    "dats24",
    "ebem",
    "ecofix",
    "ecopower",
    "engie",
    "luminus",
    "mega",
    "totalenergies",
    "bolt",
    "octaplus",
    "frank",
    "energiebe",
    "energyvision",
)


def _load_providers() -> dict[str, types.ModuleType]:
    """Load the providers package without dragging Home Assistant into scope.

    Keyed by supplier id: ``_check_catalogs`` indexes it by name, and a dict
    cannot be destructured into the wrong variables the way the old positional
    tuple could.
    """
    parent = types.ModuleType("be_pkg")
    parent.__path__ = [str(PKG)]
    sys.modules["be_pkg"] = parent
    prov = types.ModuleType("be_pkg.providers")
    prov.__path__ = [str(PKG / "providers")]
    sys.modules["be_pkg.providers"] = prov

    def _load(name: str, path: Path) -> types.ModuleType:
        spec = iu.spec_from_file_location(name, str(path))
        assert spec is not None and spec.loader is not None
        mod = iu.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        return mod

    const = _load("be_pkg.const", PKG / "const.py")
    global _FLUVIUS_KEYS, _WALLONIA_DSO_KEYS, _BRUSSELS_DSO_KEYS
    _FLUVIUS_KEYS = const.FLUVIUS_KEYS
    _WALLONIA_DSO_KEYS = const.WALLONIA_DSO_KEYS
    _BRUSSELS_DSO_KEYS = const.BRUSSELS_DSO_KEYS
    _EXPECTED_DSOS.update(
        {
            "flanders": const.FLUVIUS_KEYS,
            "wallonia": const.WALLONIA_DSO_KEYS,
            "brussels": const.BRUSSELS_DSO_KEYS,
        }
    )

    base = _load("be_pkg.providers.base", PKG / "providers" / "base.py")
    # Bind the rate classes for isinstance-based validation in
    # _validate_energy. Class identity matches because every provider
    # imports from this same loaded module via ``from ..base import``.
    global _RATE_FIXED, _RATE_VARIABLE, _RATE_DYNAMIC, _RATE_TOU, _RATE_IMPACT
    global _RATE_SPOT_MONTHLY
    _RATE_FIXED = base.FixedRates
    _RATE_VARIABLE = base.VariableRates
    _RATE_DYNAMIC = base.DynamicRates
    _RATE_TOU = base.TimeOfUseRates
    _RATE_IMPACT = base.ImpactRates
    _RATE_SPOT_MONTHLY = base.SpotMonthlyRates
    pdf = _load("be_pkg.providers._pdf", PKG / "providers" / "_pdf.py")
    global _is_transient_fetch_error, _fetch_text, _EXTRACTOR_ERROR
    _is_transient_fetch_error = pdf.is_transient_fetch_error
    _fetch_text = pdf.fetch_text
    _EXTRACTOR_ERROR = base.ExtractorError
    loaded = {
        supplier: _load(
            f"be_pkg.providers.{supplier}", PKG / "providers" / f"{supplier}.py"
        )
        for supplier in _SUPPLIERS
    }
    # Every module must be the supplier it is filed under. The old positional
    # unpack could bind a module to the wrong name and still report green, so
    # make that impossible rather than merely unlikely: the extractor knows its
    # own id, and a mismatch means the list and the filenames have diverged.
    mismatched = {
        supplier: mod.EXTRACTOR.id
        for supplier, mod in loaded.items()
        if mod.EXTRACTOR.id != supplier
    }
    if mismatched:
        raise RuntimeError(f"provider module / supplier id mismatch: {mismatched}")
    _DEPRECATED_UNTIL.update(
        {
            supplier: mod.EXTRACTOR.deprecated_until
            for supplier, mod in loaded.items()
            if getattr(mod.EXTRACTOR, "deprecated_until", None) is not None
        }
    )
    return loaded


@dataclass
class Check:
    label: str
    ok: bool
    detail: str = ""
    # "extractor" -> a fetch / parse regression; opens the existing issue
    # "catalog"   -> a new product detected at the supplier; opens a
    #                separate issue so the two failure modes don't get
    #                conflated in one thread.
    kind: str = "extractor"
    # A failure that is real but KNOWN and unactionable: the supplier
    # publishes its card as page images, so no parser change can read it.
    # Still reported, still visibly failing in the table, but it does not
    # set the extractor bit -- otherwise one such supplier fails every run
    # forever, exhausts the workflow's retry loop, and refiles a fresh
    # issue each time the last one is closed. Set by _record from the
    # exception type, so it tracks the current card rather than a
    # hardcoded supplier list.
    expected: bool = False


CHECKS: list[Check] = []

# Populated from custom_components.be_electricity_prices.const at startup.
# Declared here so module-load doesn't fail before _load_providers() runs.
_FLUVIUS_KEYS: frozenset[str] = frozenset()
_WALLONIA_DSO_KEYS: frozenset[str] = frozenset()
_BRUSSELS_DSO_KEYS: frozenset[str] = frozenset()

# The DSO set and the renewables field a region's card must carry. Seven
# checks each restated these as local literals, in two arity groups, and the
# only thing separating a two-region supplier's map from a three-region one
# was whether the Brussels row was present -- so a supplier that gained
# Brussels had to have its map remembered as well as its region tuple.
_EXPECTED_DSOS: dict[str, frozenset[str]] = {}
_RENEWABLES_FIELD: dict[str, str] = {
    "flanders": "flanders_renewables",
    "wallonia": "wallonia_renewables",
    "brussels": "brussels_renewables",
}

# Rate-class references bound by _load_providers; ``object`` placeholder
# until startup so isinstance() in _validate_energy still type-checks
# pre-load (it runs only after _load_providers, but mypy walks both
# paths). Bound to the actual base.FixedRates / VariableRates /
# DynamicRates / TimeOfUseRates / ImpactRates / SpotMonthlyRates classes
# once the providers package is loaded.
_RATE_FIXED: type = object
_RATE_VARIABLE: type = object
_RATE_DYNAMIC: type = object
_RATE_TOU: type = object
_RATE_IMPACT: type = object
_RATE_SPOT_MONTHLY: type = object


# Bound by _load_providers to providers/_pdf.is_transient_fetch_error so
# _fetch_with_retry shares the coordinator's transient/permanent split
# rather than duplicating it. Placeholder until the providers package loads.
def _is_transient_fetch_error(_message: str) -> bool:
    return False


# Bound by _load_providers to providers/_pdf.fetch_text, so the freshness
# probe can read a supplier's listing page directly rather than reaching
# into whichever provider module happens to re-export the helper.
async def _fetch_text(_session: aiohttp.ClientSession, _url: str) -> str:
    raise RuntimeError("providers not loaded")


# supplier -> its own EXTRACTOR.deprecated_until, bound by _load_providers.
# Read from the registry rather than restated here so a withdrawal date is
# declared in exactly one place and the checks below expire with it.
_DEPRECATED_UNTIL: dict[str, date] = {}


# Bound by _load_providers to providers/base.ExtractorError. The freshness
# probe catches ONLY this, so a page that will not load stays a pass while a
# renamed symbol or a changed signature surfaces as a failure.
_EXTRACTOR_ERROR: type[Exception] = RuntimeError


# Per-supplier fetch-time (summed per-request durations) + bytes-received
# accounting. Populated via an aiohttp TraceConfig that tags every
# request with whichever supplier is currently being checked (set by the
# _attributed() context manager). Surfaces silent slowdowns and PDF-size
# jumps in the report; both are leading indicators that a supplier
# reworked its tariff publication.
METRICS: dict[str, dict[str, float]] = {}
_CURRENT_SUPPLIER: ContextVar[str | None] = ContextVar(
    "be_live_check_supplier", default=None
)


def _metrics_bucket(supplier: str) -> dict[str, float]:
    return METRICS.setdefault(
        supplier,
        {
            "fetches": 0.0,
            "elapsed_s": 0.0,
            "bytes": 0.0,
            "failed": 0.0,
            "failed_s": 0.0,
        },
    )


async def _on_request_start(
    _session: aiohttp.ClientSession,
    ctx: SimpleNamespace,
    _params: aiohttp.TraceRequestStartParams,
) -> None:
    ctx.start = time.monotonic()


async def _on_request_end(
    _session: aiohttp.ClientSession,
    ctx: SimpleNamespace,
    _params: aiohttp.TraceRequestEndParams,
) -> None:
    supplier = _CURRENT_SUPPLIER.get()
    if supplier is None:
        return
    bucket = _metrics_bucket(supplier)
    bucket["fetches"] += 1.0
    bucket["elapsed_s"] += time.monotonic() - getattr(ctx, "start", time.monotonic())
    # Two things this hook does NOT cover, both counted elsewhere:
    #  - Body bytes. `response.content_length` is just the ``Content-Length``
    #    header verbatim and is None on ``Transfer-Encoding: chunked``
    #    (aiohttp.helpers.HeadersMixin.content_length), so bytes are summed
    #    in `_on_response_chunk_received` instead. Note this hook fires
    #    BEFORE the body is read, so `elapsed_s` is time-to-headers only.
    #  - Failed requests. aiohttp fires `on_request_exception` for those, so
    #    they never reach here: see `_on_request_exception`.


async def _on_response_chunk_received(
    _session: aiohttp.ClientSession,
    _ctx: SimpleNamespace,
    params: aiohttp.TraceResponseChunkReceivedParams,
) -> None:
    """Accumulate actual response body bytes per supplier.

    `on_response_chunk_received` fires regardless of whether the server set
    Content-Length or used Transfer-Encoding: chunked, so summing
    `len(chunk)` gives an honest byte total even for chunked responses,
    which the previous Content-Length-only path silently counted as zero.

    It is not one call per network chunk: `ClientResponse.read()` buffers
    the whole body and then fires this hook ONCE with all of it. So a
    transfer that stalls halfway records zero bytes, not a partial count --
    which is why an all-or-nothing byte total plus a counted fetch means
    "headers arrived, body never finished".
    """
    supplier = _CURRENT_SUPPLIER.get()
    if supplier is None:
        return
    _metrics_bucket(supplier)["bytes"] += float(len(params.chunk))


async def _on_request_exception(
    _session: aiohttp.ClientSession,
    ctx: SimpleNamespace,
    params: aiohttp.TraceRequestExceptionParams,
) -> None:
    """Count a request that never produced a response.

    Without this hook a failed request is invisible in the metrics table: it
    fires neither `on_request_end` nor `on_response_chunk_received`, so it
    contributes 0 fetches, 0 s and 0 bytes, and a supplier whose every
    attempt died read as though it had barely been tried. Failures are kept
    in their OWN counters rather than folded into `fetches` / `elapsed_s`,
    because the drift budgets in `_LATENCY_BUDGET_OVERRIDES` are calibrated
    against successful-fetch latency and must not start tripping on time
    burnt by an unreachable endpoint.

    ``params.url`` is the url of the hop that actually failed, which the
    wrapped ExtractorError cannot tell us: the providers pass the original
    url to the fetch helper, so a supplier fetched through a redirect (for
    example energie.be's document API, which 302s to an Azure blob) reports
    only that first url no matter which hop timed out.
    """
    supplier = _CURRENT_SUPPLIER.get()
    if supplier is None:
        return
    elapsed = time.monotonic() - getattr(ctx, "start", time.monotonic())
    bucket = _metrics_bucket(supplier)
    bucket["failed"] += 1.0
    bucket["failed_s"] += elapsed
    print(
        f"warning: {supplier}: request failed after {elapsed:.2f}s: "
        f"{type(params.exception).__name__} on {params.url}",
        file=sys.stderr,
    )


def _trace_config() -> aiohttp.TraceConfig:
    tc = aiohttp.TraceConfig()
    tc.on_request_start.append(_on_request_start)
    tc.on_request_end.append(_on_request_end)
    tc.on_response_chunk_received.append(_on_response_chunk_received)
    tc.on_request_exception.append(_on_request_exception)
    return tc


@contextmanager
def _attributed(supplier: str) -> Iterator[None]:
    """Attribute every aiohttp request inside this block to ``supplier``.

    Wrapping each ``_check_<supplier>`` call lets the trace hooks tag
    timing + Content-Length without each check function having to
    thread the supplier id through. Re-entry is safe via ContextVar.
    """
    token = _CURRENT_SUPPLIER.set(supplier)
    try:
        yield
    finally:
        _CURRENT_SUPPLIER.reset(token)


# Hard cap for a single supplier's whole check. aiohttp's session-level
# total=60 s only bounds individual requests, so a check that issues many
# sequential requests can still drag for minutes if one of them is slow
# but not stalled. wait_for cuts that off so a single broken supplier can
# never block the gather() and starve the rest of the run.
#
# INVARIANT: this must stay ABOVE every latency budget in
# _LATENCY_BUDGET_OVERRIDES. Those budgets warn that a supplier has got
# slow; this cap kills it. A budget at or above the cap can never fire,
# because the supplier is killed before it can report the drift.
#
# Raised from 240 s when the professional editions roughly doubled the
# sequential fetch counts (engie 29 -> 53, mega 28 -> 52): those two walk
# their catalogue one card at a time, so their wallclock scales with it
# and they were landing on the old cap on a slow-CDN day.
_SUPPLIER_HARD_TIMEOUT_S = 600.0


async def _attributed_check(
    supplier: str,
    fn: Callable[..., Awaitable[None]],
    *args: object,
) -> None:
    """asyncio.gather-friendly wrapper around an `_attributed()` block.

    asyncio.create_task copies the parent context, so each gather'd
    task gets its own ContextVar slot; setting `_CURRENT_SUPPLIER`
    inside the task only affects that task's view, which is what the
    TraceConfig hooks observe when aiohttp issues a request.

    A per-supplier wait_for caps total wallclock so one hung supplier
    can't starve the gather. Timeouts surface as a recorded extractor
    failure instead of propagating, mirroring how individual fetch
    errors are handled inside each `_check_*`.
    """
    with _attributed(supplier):
        try:
            await asyncio.wait_for(fn(*args), timeout=_SUPPLIER_HARD_TIMEOUT_S)
        except TimeoutError:
            _record(
                f"{supplier}: hard timeout",
                False,
                f"exceeded {_SUPPLIER_HARD_TIMEOUT_S:.0f}s wallclock",
            )
        except asyncio.CancelledError:
            # wait_for cancels the inner coroutine when the cap expires and
            # normally re-raises that as TimeoutError. It cannot when the
            # coroutine is parked on something uncancellable: the PDF parse
            # runs under asyncio.to_thread, and a thread cannot be
            # interrupted, so the CancelledError surfaces here instead. It
            # is a BaseException, so the clause below never saw it, and one
            # slow supplier took the whole run down with it -- no report
            # printed at all, and the workflow filed an issue with an empty
            # body (2026-08-03, totalenergies at the 240 s cap).
            #
            # Only swallow the cancellation we caused ourselves. A pending
            # cancel request on this task means the run is being torn down
            # from outside, which must keep propagating.
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
            _record(
                f"{supplier}: hard timeout",
                False,
                f"exceeded {_SUPPLIER_HARD_TIMEOUT_S:.0f}s wallclock "
                "(cancelled inside an uncancellable parse)",
            )
        except Exception as err:  # noqa: BLE001
            # Record any other failure as this supplier's row instead of
            # letting it propagate out of the top-level gather, which would
            # abort every other supplier's check and mis-report a real data
            # regression as a harness crash (rc=8).
            _record(
                f"{supplier}: unexpected error",
                False,
                f"{type(err).__name__}: {err}",
            )


# Every fetch call site records its failure detail as
# f"{type(err).__name__}: {err}", so the exception type is machine-written
# at the front of the string. Matching on it is exact, not prose matching.
_UNREADABLE_MARKER = "CardNotReadableError"
# A supplier past its own deprecated_until has left the market; its final
# card stays up and stays stale forever. Real, visible, and not actionable
# by any change here -- the same class as an unreadable card.
_WITHDRAWN_MARKER = "SupplierWithdrawn"


def _record(label: str, ok: bool, detail: str = "", kind: str = "extractor") -> None:
    CHECKS.append(
        Check(
            label=label,
            ok=ok,
            detail=detail,
            kind=kind,
            expected=not ok
            and detail.startswith((_UNREADABLE_MARKER, _WITHDRAWN_MARKER)),
        )
    )


def _expect(label: str, condition: bool, detail: str = "") -> bool:
    _record(label, condition, detail if not condition else "")
    return condition


# Upper bound for the federal "bijdrage op de energie" / "cotisation sur
# l'energie". It was 0,0020417 EUR/kWh until it was abolished on 2026-08-01,
# so anything above a cent per kWh is a unit slip rather than a real rate.
_MAX_ENERGY_CONTRIBUTION = 0.01


def _expect_professional_basis(prefix: str, contract: object, snap: object) -> None:
    """Assert what a professional card must carry, and a residential one
    must not.

    The whole professional lane rests on the card being published
    excluding VAT: if a supplier quietly switched to VAT-inclusive
    printing, every price would come out 21% high and nothing else here
    would notice, because each individual field would still parse. Gate
    the basis itself, in both directions, so the asymmetry can't hide.
    """
    professional = bool(getattr(contract, "professional", False))
    taxes = snap.taxes  # type: ignore[attr-defined]
    injection = snap.injection  # type: ignore[attr-defined]
    if professional:
        _expect(
            f"{prefix}: professional card is ex-VAT",
            taxes.vat_rate == 0.21,
            detail=f"vat_rate={taxes.vat_rate}",
        )
        if injection is not None:
            _expect(
                f"{prefix}: professional injection is taxed",
                injection.vat_applies,
            )
    else:
        _expect(
            f"{prefix}: residential card is VAT-inclusive",
            taxes.vat_rate == 0.0,
            detail=f"vat_rate={taxes.vat_rate}",
        )
        if injection is not None:
            _expect(
                f"{prefix}: residential injection is exempt",
                not injection.vat_applies,
            )


def _expect_excise_bands(prefix: str, taxes: object) -> None:
    """A degressive excise schedule must be ordered and plausible.

    Only asserted where the card prints one (Engie and Mega professional
    cards); Bolt prints the first tranche alone, so an absent schedule is
    not a failure.
    """
    bands = taxes.federal_excise_bands  # type: ignore[attr-defined]
    if not bands:
        return
    uppers = [b[0] for b in bands]
    rates = [b[1] for b in bands]
    _expect(
        f"{prefix}: excise bands ascend by volume",
        uppers == sorted(uppers) and len(set(uppers)) == len(uppers),
        detail=f"uppers={uppers}",
    )
    _expect(
        f"{prefix}: excise bands are degressive",
        all(a >= b for a, b in zip(rates, rates[1:], strict=False)),
        detail=f"rates={rates}",
    )
    _expect(
        f"{prefix}: excise band bounds are whole kWh",
        all(u >= 1000.0 for u in uppers),
        detail=f"uppers={uppers} (a thousands-separator slip reads 20.000 as 20)",
    )


def _expect_energy_contribution(prefix: str, taxes: object) -> None:
    """Bounds-check the federal energy contribution on a fetched snapshot.

    This used to assert ``> 0`` on four suppliers. The levy fell to zero on
    2026-08-01, so EBEM's August card failed CI three times over for
    reporting the rate the card actually prints (issue #49). Zero is now a
    valid value; the useful assertion is that the number is non-negative and
    still of a plausible magnitude, which keeps catching the unit slip the
    original gate was really there for.
    """
    value = getattr(taxes, "energy_contribution", None)
    _expect(
        f"{prefix}: energy contribution in [0, {_MAX_ENERGY_CONTRIBUTION}] EUR/kWh",
        value is not None and 0.0 <= value <= _MAX_ENERGY_CONTRIBUTION,
        detail=str(taxes),
    )


_RETRY_BACKOFF_S: tuple[float, ...] = (1.0, 3.0)
_RetryT = TypeVar("_RetryT")


async def _fetch_with_retry(
    factory: Callable[[], Awaitable[_RetryT]],
    *,
    attempts: int = 3,
) -> _RetryT:
    """Call ``factory()`` up to ``attempts`` times, retrying transient
    network failures with a short backoff between attempts.

    A "transient" failure is either a bare ``TimeoutError`` or an
    ``ExtractorError``-shaped exception whose message starts with
    ``"network error fetching"`` or ``"HTTP "`` -- the two strings the
    shared PDF helpers in ``providers/_pdf.py`` use to wrap aiohttp
    surface errors. Any other exception (parse error, regex miss, ...)
    propagates immediately so a real regression isn't masked by retries.

    A fresh awaitable is created via ``factory()`` for every attempt
    because awaitables can only be awaited once.
    """
    last_err: BaseException | None = None
    for i in range(attempts):
        try:
            return await factory()
        except Exception as err:
            # Same transient classification the coordinator uses: a bare
            # timeout, a wrapped "network error fetching", or a 5xx / 408 /
            # 429 / 403 status. A 404 / 410 (card renamed or withdrawn) is
            # not transient and fails fast. The string predicate is shared
            # from providers/_pdf.py so the two paths can't drift apart.
            transient = isinstance(err, TimeoutError) or _is_transient_fetch_error(
                str(err)
            )
            if not transient or i == attempts - 1:
                raise
            last_err = err
            await asyncio.sleep(_RETRY_BACKOFF_S[min(i, len(_RETRY_BACKOFF_S) - 1)])
    assert last_err is not None  # unreachable
    raise last_err


async def _check_eneco(session: aiohttp.ClientSession, eneco: types.ModuleType) -> None:
    expected_dso_keys = _WALLONIA_DSO_KEYS | _FLUVIUS_KEYS
    # Derive the contracts from the runtime registry so a product added
    # to EXTRACTOR.contracts is validated here without editing this list.
    for cid in (c.id for c in eneco.EXTRACTOR.contracts):
        prefix = f"eneco/{cid}"
        try:
            # Eneco's PDF carries every region; any one is fine.
            snap = await _fetch_with_retry(
                partial(eneco.fetch, session, cid, "flanders")
            )
        except Exception as err:
            _record(f"{prefix}: fetch", False, f"{type(err).__name__}: {err}")
            continue
        _expect(f"{prefix}: publication label", bool(snap.publication_label))
        _expect(
            f"{prefix}: all DSO keys present",
            expected_dso_keys <= set(snap.dsos),
            detail=f"missing: {sorted(expected_dso_keys - set(snap.dsos))}",
        )
        _expect(
            f"{prefix}: federal excise > 0",
            snap.taxes.federal_excise > 0,
            detail=str(snap.taxes),
        )
        _expect(
            f"{prefix}: flanders renewables > 0",
            snap.taxes.flanders_renewables > 0,
            detail=str(snap.taxes),
        )
        _expect(
            f"{prefix}: wallonia renewables > 0",
            snap.taxes.wallonia_renewables > 0,
            detail=str(snap.taxes),
        )
        _validate_snapshot(prefix, cid, snap, require_capacity=_CAPACITY_REQUIRED)


async def _check_cociter(
    session: aiohttp.ClientSession, cociter: types.ModuleType
) -> None:
    expected_dso_keys = _WALLONIA_DSO_KEYS
    for cid in ("cociter_variable", "cociter_dynamic"):
        prefix = f"cociter/{cid}"
        try:
            snap = await _fetch_with_retry(
                partial(cociter.fetch, session, cid, "wallonia")
            )
        except Exception as err:
            _record(f"{prefix}: fetch", False, f"{type(err).__name__}: {err}")
            continue
        _expect(f"{prefix}: publication label", bool(snap.publication_label))
        _expect(
            f"{prefix}: wallonia DSO keys present",
            expected_dso_keys <= set(snap.dsos),
            detail=f"missing: {sorted(expected_dso_keys - set(snap.dsos))}",
        )
        _expect(
            f"{prefix}: federal excise > 0",
            snap.taxes.federal_excise > 0,
            detail=str(snap.taxes),
        )
        _expect(
            f"{prefix}: wallonia renewables > 0",
            snap.taxes.wallonia_renewables > 0,
            detail=str(snap.taxes),
        )
        _validate_snapshot(prefix, cid, snap)


async def _check_dats24(
    session: aiohttp.ClientSession, dats24: types.ModuleType
) -> None:
    expected = {"flanders": _FLUVIUS_KEYS, "wallonia": _WALLONIA_DSO_KEYS}
    cid = "dats24_groen_variabel"
    for region in ("flanders", "wallonia"):
        prefix = f"dats24/{cid}/{region}"
        try:
            snap = await _fetch_with_retry(partial(dats24.fetch, session, cid, region))
        except Exception as err:
            _record(f"{prefix}: fetch", False, f"{type(err).__name__}: {err}")
            continue
        _expect(f"{prefix}: publication label", bool(snap.publication_label))
        _expect(
            f"{prefix}: expected DSOs present",
            expected[region] <= set(snap.dsos),
            detail=f"missing: {sorted(expected[region] - set(snap.dsos))}",
        )
        _expect(
            f"{prefix}: federal excise > 0",
            snap.taxes.federal_excise > 0,
            detail=str(snap.taxes),
        )
        if region == "flanders":
            _expect(
                f"{prefix}: flanders renewables > 0",
                snap.taxes.flanders_renewables > 0,
                detail=str(snap.taxes),
            )
        else:
            _expect(
                f"{prefix}: wallonia renewables > 0",
                snap.taxes.wallonia_renewables > 0,
                detail=str(snap.taxes),
            )
        # DATS 24's teruglevering is reserved to Flemish digital-meter
        # customers, so the Wallonia card pays no feed-in; the Flanders
        # card is monthly-indexed (current only, no spot factor/base).
        _validate_snapshot(
            prefix,
            cid,
            snap,
            injection_shape="monthly" if region == "flanders" else "none",
        )


async def _check_ebem(session: aiohttp.ClientSession, ebem: types.ModuleType) -> None:
    # EBEM only sells residential electricity in Flanders. The 'elek' card
    # carries both Groen Variabel and Groen B@sic+ in one PDF; the
    # 'dynamic' card carries Groen Dyn@mic. Walk every contract — they
    # all hit the same listing-page resolver but parse different blocks.
    expected_dsos = _FLUVIUS_KEYS
    for contract in ebem._CONTRACTS:
        cid = contract.contract_id
        prefix = f"ebem/{cid}/flanders"
        try:
            snap = await _fetch_with_retry(
                partial(ebem.fetch, session, cid, "flanders")
            )
        except Exception as err:
            _record(f"{prefix}: fetch", False, f"{type(err).__name__}: {err}")
            continue
        _expect(f"{prefix}: publication label", bool(snap.publication_label))
        _expect(
            f"{prefix}: all eight Fluvius DSOs present",
            expected_dsos <= set(snap.dsos),
            detail=f"missing: {sorted(expected_dsos - set(snap.dsos))}",
        )
        _expect(
            f"{prefix}: flanders renewables > 0",
            snap.taxes.flanders_renewables > 0,
            detail=str(snap.taxes),
        )
        _expect(
            f"{prefix}: federal excise > 0",
            snap.taxes.federal_excise > 0,
            detail=str(snap.taxes),
        )
        _expect_energy_contribution(prefix, snap.taxes)
        _validate_snapshot(prefix, cid, snap)


async def _check_two_region_supplier(
    session: aiohttp.ClientSession, mod: types.ModuleType, supplier: str
) -> None:
    """Walk every (contract, region) pair of a Flanders + Wallonia supplier.

    Ecofix, Luminus and OCTA+ each sell into those two regions only, and
    checked them with the same loop and the same assertions in the same order.
    Only OCTA+ carried the per-contract region guard, which all three want: a
    contract restricted to one region must not have the other region's card
    parsed against it.

    That guard reads ``regions`` defensively because only OCTA+'s private
    ``_ContractDef`` declares the field -- Ecofix's and Luminus's do not, so a
    plain attribute access raises AttributeError. Their public registry
    ``Contract`` does have it, but that is a different object from the
    ``_CONTRACTS`` entries walked here.
    """
    for contract in mod._CONTRACTS:
        cid = contract.contract_id
        for region_key in ("flanders", "wallonia"):
            regions = getattr(contract, "regions", None)
            if regions and region_key not in regions:
                continue
            prefix = f"{supplier}/{cid}/{region_key}"
            try:
                snap = await _fetch_with_retry(
                    partial(mod.fetch, session, cid, region_key)
                )
            except Exception as err:
                _record(f"{prefix}: fetch", False, f"{type(err).__name__}: {err}")
                continue
            _expect(f"{prefix}: publication label", bool(snap.publication_label))
            _expect_region_basics(prefix, region_key, snap)
            _expect_energy_contribution(prefix, snap.taxes)
            _validate_snapshot(prefix, cid, snap)


async def _check_ecofix(
    session: aiohttp.ClientSession, ecofix: types.ModuleType
) -> None:
    # Ecofix sells residential electricity in Flanders and Wallonia
    # (no Brussels offering). The same PDF carries both regions; the
    # parser narrows the snapshot down to the requested region.
    await _check_two_region_supplier(session, ecofix, "ecofix")


async def _check_ecopower(
    session: aiohttp.ClientSession, ecopower: types.ModuleType
) -> None:
    expected_dso_keys = _FLUVIUS_KEYS
    # Ecopower sells the static "Groene burgerstroom" and the dynamic
    # "Dynamische burgerstroom"; both are Flanders-only HTVA cards with
    # the same tax/DSO shape, so the assertions are shared. Derive the
    # contracts from the runtime registry so a third product is validated
    # here without editing this list.
    for cid in (c.id for c in ecopower.EXTRACTOR.contracts):
        prefix = f"ecopower/{cid}"
        try:
            snap = await _fetch_with_retry(
                partial(ecopower.fetch, session, cid, "flanders")
            )
        except Exception as err:
            _record(f"{prefix}: fetch", False, f"{type(err).__name__}: {err}")
            continue
        _expect(f"{prefix}: publication label", bool(snap.publication_label))
        _expect(
            f"{prefix}: all eight Fluvius DSOs present",
            expected_dso_keys <= set(snap.dsos),
            detail=f"missing: {sorted(expected_dso_keys - set(snap.dsos))}",
        )
        _expect(
            f"{prefix}: federal excise > 0",
            snap.taxes.federal_excise > 0,
            detail=str(snap.taxes),
        )
        _expect(
            f"{prefix}: flanders renewables > 0",
            snap.taxes.flanders_renewables > 0,
            detail=str(snap.taxes),
        )
        # Ecopower publishes HTVA values; vat_rate must be 0.06.
        _expect(
            f"{prefix}: vat_rate is 0.06",
            snap.taxes.vat_rate == 0.06,
            detail=str(snap.taxes),
        )
        _validate_snapshot(prefix, cid, snap, require_capacity=_CAPACITY_REQUIRED)


async def _check_flanders_card(
    session: aiohttp.ClientSession,
    mod: types.ModuleType,
    supplier: str,
    cid: str,
) -> None:
    """One Flanders-only card: all eight Fluvius rows, the federal and
    regional levies, and a VAT-inclusive card.

    Frank and energie.be checked exactly this, in two functions that differed
    only in the supplier token. ``cid`` stays a parameter rather than looping
    ``mod.EXTRACTOR.contracts``: Frank sells five tiers off one card and this
    deliberately checks the default one, so iterating them would multiply
    Frank's wallclock and byte draw by five and trip the drift budgets.

    Nothing here is dynamic-specific -- the energy leg is validated by shape in
    _validate_energy -- so energie.be's spot-monthly variable card reuses it.
    """
    prefix = f"{supplier}/{cid}"
    try:
        snap = await _fetch_with_retry(partial(mod.fetch, session, cid, "flanders"))
    except Exception as err:
        _record(f"{prefix}: fetch", False, f"{type(err).__name__}: {err}")
        return
    _expect(f"{prefix}: publication label", bool(snap.publication_label))
    _expect(
        f"{prefix}: all eight Fluvius DSOs present",
        _FLUVIUS_KEYS <= set(snap.dsos),
        detail=f"missing: {sorted(_FLUVIUS_KEYS - set(snap.dsos))}",
    )
    _expect(
        f"{prefix}: federal excise > 0",
        snap.taxes.federal_excise > 0,
        detail=str(snap.taxes),
    )
    _expect(
        f"{prefix}: flanders renewables > 0",
        snap.taxes.flanders_renewables > 0,
        detail=str(snap.taxes),
    )
    _expect(
        f"{prefix}: vat_rate is 0.0 (VAT-inclusive card)",
        snap.taxes.vat_rate == 0.0,
        detail=str(snap.taxes),
    )
    _validate_snapshot(prefix, cid, snap, require_capacity=_CAPACITY_REQUIRED)


async def _check_frank(session: aiohttp.ClientSession, frank: types.ModuleType) -> None:
    # Own coroutine so the per-supplier byte / latency attribution keeps its
    # own bucket; same for energie.be below.
    await _check_flanders_card(session, frank, "frank", "frank_dynamic")


async def _check_energiebe(
    session: aiohttp.ClientSession, energiebe: types.ModuleType
) -> None:
    # Two products, two independently published PDFs, two fetches: the
    # variable card is not a section of the dynamic one and drifts on its own.
    for cid in ("energiebe_dynamic", "energiebe_variable"):
        await _check_flanders_card(session, energiebe, "energiebe", cid)


async def _check_energyvision(
    session: aiohttp.ClientSession, energyvision: types.ModuleType
) -> None:
    # Each product is published for exactly one region (the Flemish cards in
    # Dutch, the Walloon one in French), so walk the contract's own regions
    # rather than assuming Flanders: fetching a Walloon contract as flanders
    # raises, and its card carries Walloon DSOs and CV instead of GSC/WKC.
    for contract in energyvision.EXTRACTOR.contracts:
        cid = contract.id
        for region in sorted(contract.regions):
            prefix = f"energyvision/{cid}/{region}"
            flanders = region == "flanders"
            expected_dso_keys = _FLUVIUS_KEYS if flanders else _WALLONIA_DSO_KEYS
            try:
                snap = await _fetch_with_retry(
                    partial(energyvision.fetch, session, cid, region)
                )
            except Exception as err:
                _record(f"{prefix}: fetch", False, f"{type(err).__name__}: {err}")
                continue
            _expect(f"{prefix}: publication label", bool(snap.publication_label))
            _expect(
                f"{prefix}: expected DSOs present",
                expected_dso_keys <= set(snap.dsos),
                detail=f"missing: {sorted(expected_dso_keys - set(snap.dsos))}",
            )
            _expect(
                f"{prefix}: federal excise > 0",
                snap.taxes.federal_excise > 0,
                detail=str(snap.taxes),
            )
            _expect(
                f"{prefix}: regional renewables > 0",
                (
                    snap.taxes.flanders_renewables
                    if flanders
                    else snap.taxes.wallonia_renewables
                )
                > 0,
                detail=str(snap.taxes),
            )
            _expect(
                f"{prefix}: vat_rate is 0.0 (VAT-inclusive card)",
                snap.taxes.vat_rate == 0.0,
                detail=str(snap.taxes),
            )
            _validate_snapshot(prefix, cid, snap, require_capacity=_CAPACITY_REQUIRED)
            if "ores" in snap.dsos:
                # The Walloon card prints the CWaPE Impact bands cheapest
                # first, the reverse of the DATS 24 layout: a positional
                # mis-map silently swaps peak and off-peak distribution.
                o = snap.dsos["ores"]
                _expect(
                    f"{prefix}: impact bands ordered eco < medium < pic",
                    None not in (o.distribution_eco, o.distribution_medium)
                    and o.distribution_pic is not None
                    and o.distribution_eco < o.distribution_medium
                    and o.distribution_medium < o.distribution_pic,
                    detail=str(o),
                )


async def _check_luminus(
    session: aiohttp.ClientSession, luminus: types.ModuleType
) -> None:
    # Luminus serves Flanders and Wallonia for every market product
    # (Brussels carries only the regulated Social tariff, which is
    # excluded from the registry).
    await _check_two_region_supplier(session, luminus, "luminus")


async def _check_bolt(session: aiohttp.ClientSession, bolt: types.ModuleType) -> None:
    # Bolt's PDFs are nationwide (one file per contract, all 3 regions
    # in one document), so we walk every (contract, region) pair just to
    # verify the parsing path for each region works.
    # Fetch all six contract PDFs concurrently. Sequential fetches
    # turned the 240 s per-supplier wallclock cap into the binding
    # constraint on slow-CDN days (issue #13: 5 contracts each at
    # 30 s timeout already hit the cap). Concurrent gather makes
    # max(individual time) the wallclock instead of the sum, so a
    # 60 s per-request budget still fits comfortably under the cap.
    fetch_results = await asyncio.gather(
        *(
            _fetch_with_retry(partial(bolt._fetch_pdf_text, session, c))
            for c in bolt._CONTRACTS
        ),
        return_exceptions=True,
    )
    for contract, result in zip(bolt._CONTRACTS, fetch_results, strict=True):
        cid = contract.contract_id
        if isinstance(result, BaseException):
            for region_key in ("flanders", "wallonia", "brussels"):
                _record(
                    f"bolt/{cid}/{region_key}: fetch",
                    False,
                    f"{type(result).__name__}: {result}",
                )
            continue
        url, text = result
        for region_key in ("flanders", "wallonia", "brussels"):
            prefix = f"bolt/{cid}/{region_key}"
            try:
                snap = bolt.parse_snapshot(cid, text, region_key, url)
            except Exception as err:
                _record(f"{prefix}: parse", False, f"{type(err).__name__}: {err}")
                continue
            _expect_professional_basis(prefix, contract, snap)
            _expect_excise_bands(prefix, snap.taxes)
            _expect_region_basics(prefix, region_key, snap)
            _expect(
                f"{prefix}: publication label",
                bool(snap.publication_label),
                detail=f"label={snap.publication_label!r}",
            )
            _validate_snapshot(prefix, cid, snap)


async def _check_totalenergies(
    session: aiohttp.ClientSession, totalenergies: types.ModuleType
) -> None:
    # TotalEnergies serves all 3 regions for every product. Walk every
    # (contract, region) pair against the real /latest/ PDFs.
    for contract in totalenergies._CONTRACTS:
        cid = contract.contract_id
        for region_key in ("flanders", "wallonia", "brussels"):
            if region_key not in contract.regions:
                continue
            prefix = f"totalenergies/{cid}/{region_key}"
            try:
                snap = await _fetch_with_retry(
                    partial(totalenergies.fetch, session, cid, region_key)
                )
            except Exception as err:
                _record(f"{prefix}: fetch", False, f"{type(err).__name__}: {err}")
                continue
            _expect_region_basics(prefix, region_key, snap)
            _expect(
                f"{prefix}: publication label",
                bool(snap.publication_label),
                detail=f"label={snap.publication_label!r}",
            )
            _validate_snapshot(prefix, cid, snap)


async def _check_mega(session: aiohttp.ClientSession, mega: types.ModuleType) -> None:
    # Mega serves all 3 regions for every contract and resolves the URL
    # by scraping mega.be/fr/energie/cartes-tarifaires; walk every (contract,
    # region) pair to verify both the listing scrape and the PDF parse.
    #
    # Mega's fetch() pulls the listing on every call (~342KB x 33 pairs
    # = ~11MB redundant traffic + 33 round-trips). Pre-fetch the
    # listing once and override _fetch_listing_html for the duration of
    # this check so the fetch path is still exercised end-to-end while
    # the harness stops paying for the same HTML 33 times.
    try:
        listing_html = await _fetch_with_retry(
            partial(mega._fetch_listing_html, session)
        )
    except Exception as err:
        _record("mega: listing fetch", False, f"{type(err).__name__}: {err}")
        return

    async def _cached_listing(_session: aiohttp.ClientSession) -> str:
        return listing_html

    # mypy treats module-attribute assignment as widening; setattr keeps
    # the patch contained without forcing a stub for the private helper.
    original_fetch_listing = mega._fetch_listing_html
    setattr(mega, "_fetch_listing_html", _cached_listing)  # noqa: B010
    try:
        await _check_mega_pairs(session, mega)
    finally:
        setattr(mega, "_fetch_listing_html", original_fetch_listing)  # noqa: B010


async def _check_mega_pairs(
    session: aiohttp.ClientSession, mega: types.ModuleType
) -> None:
    for contract in mega._CONTRACTS:
        cid = contract.contract_id
        for region_key in ("flanders", "wallonia", "brussels"):
            if region_key not in contract.regions:
                continue
            prefix = f"mega/{cid}/{region_key}"
            try:
                snap = await _fetch_with_retry(
                    partial(mega.fetch, session, cid, region_key)
                )
            except Exception as err:
                _record(f"{prefix}: fetch", False, f"{type(err).__name__}: {err}")
                continue
            _expect_professional_basis(prefix, contract, snap)
            _expect_excise_bands(prefix, snap.taxes)
            _expect_region_basics(prefix, region_key, snap)
            _expect(
                f"{prefix}: publication label",
                bool(snap.publication_label),
                detail=f"label={snap.publication_label!r}",
            )
            _validate_snapshot(prefix, cid, snap)


async def _check_octaplus(
    session: aiohttp.ClientSession, octaplus: types.ModuleType
) -> None:
    # OCTA+ only sells residential electricity in Flanders and Wallonia
    # (Brussels offerings are professional-only). One PDF per (contract,
    # region) at https://files.octaplus.be/tariffs/E_OCTA_<SLUG>_RE_<VL|WL>_FR.pdf
    # octaplus_fixed_impact is Wallonia-only (CWaPE bands); the shared
    # loop's region guard keeps the Flanders Fixed card out of it.
    await _check_two_region_supplier(session, octaplus, "octaplus")


async def _check_engie(session: aiohttp.ClientSession, engie: types.ModuleType) -> None:
    # Engie now fetches one PDF per (contract, region) on demand, so the
    # check walks every supported region per contract instead of asking
    # for a single merged snapshot. If a region fetch ever stops working
    # the report flags the specific (contract, region) pair.
    region_letter = {"flanders": "V", "wallonia": "W", "brussels": "B"}
    for contract in engie._CONTRACTS:
        cid = contract.contract_id
        for region_key, letter in region_letter.items():
            if letter not in contract.months_per_region:
                continue
            prefix = f"engie/{cid}/{region_key}"
            try:
                snap = await _fetch_with_retry(
                    partial(engie.fetch, session, cid, region_key)
                )
            except Exception as err:
                _record(f"{prefix}: fetch", False, f"{type(err).__name__}: {err}")
                continue
            _expect_professional_basis(prefix, contract, snap)
            _expect_excise_bands(prefix, snap.taxes)
            _expect(f"{prefix}: publication label", bool(snap.publication_label))
            _expect_region_basics(prefix, region_key, snap)
            _validate_snapshot(prefix, cid, snap)


async def _check_catalogs(
    session: aiohttp.ClientSession, modules: dict[str, types.ModuleType]
) -> None:
    """Run each supplier's ``discover()`` and surface any new product ids.

    ``known`` is each supplier's registered identifier set, derived from
    the provider module in the same shape ``discover()`` returns, so the
    CI baseline can't silently drift away from the code.
    """
    known: dict[str, set[str]] = {
        "mega": {c.product_name for c in modules["mega"]._CONTRACTS},
        "bolt": {f"{c.folder}/{c.slug}" for c in modules["bolt"]._CONTRACTS},
        "engie": {c.family for c in modules["engie"]._CONTRACTS},
        "luminus": {c.slug for c in modules["luminus"]._CONTRACTS},
        "eneco": set(modules["eneco"]._CONTRACT_SLUGS),
        "totalenergies": {c.slug for c in modules["totalenergies"]._CONTRACTS},
        "octaplus": {c.slug for c in modules["octaplus"]._CONTRACTS},
        "cociter": set(modules["cociter"]._DISCOVER_FAMILIES.values()),
        "ebem": {c.contract_id for c in modules["ebem"]._CONTRACTS},
        "ecofix": {c.contract_id for c in modules["ecofix"]._CONTRACTS},
        "ecopower": set(modules["ecopower"].DISCOVER_IDS),
        "dats24": {modules["dats24"]._CONTRACT_ID},
        "frank": {t[0] for t in modules["frank"]._TIERS},
        "energyvision": set(modules["energyvision"].DISCOVER_IDS),
    }
    for name, mod in modules.items():
        discover = getattr(mod, "discover", None)
        if discover is None:
            continue
        try:
            discovered = await discover(session)
        except Exception as err:
            _record(
                f"{name}/catalog: discovery raised",
                False,
                f"{type(err).__name__}: {err}",
                kind="catalog",
            )
            continue
        if not discovered:
            # discover() returned an empty set: either a transient listing
            # fetch failure or a discovery surface that changed shape.
            # Catalog signals aren't retried and we can't open an issue on
            # a transient blip, but a persistently empty result means all
            # new-product coverage for this supplier is silently gone. Log
            # it on stderr AND record a tracked (non-failing) catalog Check
            # so the emptiness shows up in the structured results and the
            # run history, not only buried in the CI log.
            print(
                f"warning: {name}/catalog: discover() returned no ids "
                "(listing fetch failed or discovery surface changed)",
                file=sys.stderr,
            )
            _record(
                f"{name}/catalog: discover() returned no ids",
                True,
                "listing fetch failed or discovery surface changed",
                kind="catalog",
            )
            continue
        new_ids = sorted(discovered - known.get(name, set()))
        _record(
            f"{name}/catalog: no new products at supplier",
            not new_ids,
            detail=", ".join(new_ids) if new_ids else "",
            kind="catalog",
        )


def _expect_newest_card(
    label: str,
    html: str,
    pattern: str,
    served: str,
    key: Callable[[str], int],
    exclude: str = "",
    empty_is_ok: bool = True,
) -> None:
    """Assert ``served`` is the highest card stamp ``html`` advertises.

    ``pattern`` is deliberately looser than the extractor's own: that is
    the whole mechanism. When a supplier changes the filename SHAPE the
    strict pattern stops seeing the new file and keeps resolving the old
    one, which still exists, still returns 200 and still parses -- so
    every other check in this script passes. The loose pattern still sees
    it, and the two disagree.

    ``empty_is_ok`` decides what a page carrying no recognisable card
    means, and that differs per supplier. Where the extractor RAISES when
    it cannot resolve a card, the extractor phase already reports the
    breakage and a redesigned page reported here too would be noise. Where
    the extractor instead falls back to a hardcoded version, nothing else
    fails, and a pass here would leave that pin serving silently.

    Matching is case-INSENSITIVE, and not as a convenience: every provider
    module compiles its card patterns with ``re.IGNORECASE``, so a gate
    matching case-sensitively is stricter than the extractor it audits in
    that one dimension, and goes blind exactly where the extractor still
    sees. A supplier recasing a filename would keep resolving fine while
    this scan found nothing and, for a supplier whose empty case is a
    pass, reported green.
    """
    advertised = [
        m.group(1)
        for m in re.finditer(pattern, html, re.IGNORECASE)
        if not exclude or exclude not in m.group(0).lower()
    ]
    if not advertised:
        _record(
            label,
            empty_is_ok,
            "page advertises no card in the expected shape"
            + ("" if empty_is_ok else "; extractor is serving its hardcoded fallback"),
        )
        return
    newest = max(advertised, key=key)
    if key(served) >= key(newest):
        _record(label, True)
        return
    _expect(label, False, f"supplier advertises {newest}, extractor resolves {served}")


def _version_key(stamp: str) -> int:
    """Order a Bolt variable-card version.

    Plain numbers, so "9" must not outrank "13". A version that is NOT
    purely numeric sorts above every real one on purpose: the extractor
    matches \\d+ and cannot resolve such a card at all, so it must become
    the "newest advertised" and fail the comparison. Scoring it low
    instead made the filename-shape change this gate exists to catch pass
    green, which is the exact failure mode it was written for.
    """
    return int(stamp) if stamp.isdigit() else 10**9


# Every stamp format below fails a naive max(), each at a different
# boundary, so each supplier needs its own key. The shared rule: a stamp
# that does not fit the expected shape scores _UNREADABLE_STAMP, ABOVE
# every real one, so it becomes the "newest advertised" and fails the
# comparison. Scoring an unparseable stamp low is what let Bolt's
# filename-shape case pass green.
_UNREADABLE_STAMP = 10**9

# Dutch month names, 3-letter prefixes, as they appear in EBEM and Frank
# filenames. "maa"/"mrt" both occur for March.
_NL_MONTH_PREFIX = {
    "jan": 1,
    "feb": 2,
    "maa": 3,
    "mrt": 3,
    "apr": 4,
    "mei": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "okt": 10,
    "nov": 11,
    "dec": 12,
}


def _ecopower_date_key(stamp: str) -> int:
    """Order an Ecopower ``YYYYMM`` or ``YYYYMMDD`` stamp.

    Year-major, so it sorts once both widths are padded to eight: a dated
    reissue then outranks the bare month card it replaces.
    """
    return int(stamp.ljust(8, "0")) if stamp.isdigit() else _UNREADABLE_STAMP


def _mega_month_key(stamp: str) -> int:
    """Order a Mega ``MMYYYY`` stamp.

    Month-major, so it sorts neither numerically nor lexically: 122026
    (December 2026) outranks 012027 (January 2027) both ways. Left naive,
    the gate would call a fresh January card stale every year end.
    """
    if len(stamp) == 6 and stamp.isdigit():
        return int(stamp[2:]) * 100 + int(stamp[:2])
    return _UNREADABLE_STAMP


def _eneco_issue_key(stamp: str) -> int:
    """Order an Eneco ``BC_<form>_<VOLYYMM>`` stamp.

    The issue is volume-major (``01`` = first publication of that month,
    ``02``+ a re-issue), so a plain int mis-orders: an April re-issue
    022604 outranks May's first issue 012605, and a genuinely stale April
    card would pass green.
    """
    issue = stamp.rsplit("_", 1)[-1]
    if len(issue) != 6 or not issue.isdigit():
        return _UNREADABLE_STAMP
    return int(issue[2:6]) * 100 + int(issue[0:2])


def _month_year_key(stamp: str) -> int:
    """Order an EBEM ``MM-YYYY`` / ``<maand>-YYYY`` / ``YYYY-MM`` stamp.

    "12-2025" outranks "08-2026" lexically, and a month NAME does not sort
    at all. Accepts all three shapes the page has carried and normalises
    to YYYYMM.
    """
    parts = re.split(r"[-_]", stamp, maxsplit=1)
    if len(parts) != 2:
        return _UNREADABLE_STAMP
    first, second = parts
    if not second.isdigit():
        return _UNREADABLE_STAMP
    if first.isdigit():
        # YYYY-MM when the first half is the wider one, else MM-YYYY.
        if len(first) == 4:
            return int(first) * 100 + int(second)
        return int(second) * 100 + int(first)
    month = _NL_MONTH_PREFIX.get(first[:3].lower())
    return int(second) * 100 + month if month else _UNREADABLE_STAMP


def _frank_month_key(stamp: str) -> int:
    """Order a Frank ``<maand> YYYY`` label, or a numeric ``YYYY-MM``.

    Frank names its cards with a Dutch month WORD, which sorts wrong in
    every direction: "April 2026" > "Augustus 2026" and "December 2026" <
    "Januari 2027" lexically.
    """
    stamp = stamp.strip().lower()
    numeric = re.fullmatch(r"(20\d{2})[-_ ]?(0[1-9]|1[0-2])", stamp)
    if numeric:
        return int(numeric.group(1)) * 100 + int(numeric.group(2))
    worded = re.fullmatch(r"([a-z]+)\s+(20\d{2})", stamp)
    if not worded:
        return _UNREADABLE_STAMP
    month = _NL_MONTH_PREFIX.get(worded.group(1)[:3])
    return int(worded.group(2)) * 100 + month if month else _UNREADABLE_STAMP


def _mmyy_key(stamp: str) -> int:
    """Order an EnergyVision ``MMYY`` stamp: 0826, 1226, 0127.

    Month-major like Mega's, one digit shorter.
    """
    if len(stamp) == 4 and stamp.isdigit():
        return int(stamp[2:]) * 100 + int(stamp[:2])
    return _UNREADABLE_STAMP


def _yymm_key(stamp: str) -> int:
    """Order a Cociter ``YYMM`` stamp, ignoring any trailing tail.

    Year-major, so the leading four digits do sort -- but the captured
    stamp carries the language token and a WordPress duplicate counter
    ("2608-fr-1"), which must not join the comparison.
    """
    head = re.match(r"(\d{4})", stamp)
    return int(head.group(1)) if head else _UNREADABLE_STAMP


async def _freshness_row(
    label: str,
    session: aiohttp.ClientSession,
    page: str,
    pattern: str,
    served_of: Callable[[str], Awaitable[list[str | None]]],
    key: Callable[[str], int],
    exclude: str = "",
    resolver_falls_back: bool = False,
) -> None:
    """One supplier-family freshness row.

    ``served_of`` receives the page HTML (several resolvers are pure and
    take the already-fetched document) and returns every stamp the
    extractor resolves for that family. The NEWEST is compared, not the
    oldest. Comparing the oldest looks stricter and is actually wrong: a
    supplier that rolls one product's or one region's card ahead of its
    siblings then fails the row and gets named broken for its own
    publication schedule. Mega's listing is demonstrably not atomic. The
    defect this gate exists for -- a resolver that cannot see the new
    filename shape -- takes out every card in the family at once, so the
    newest resolved stamp still falls behind the newest advertised and the
    row still fails. Where a break really can hit one member at a time,
    the caller emits a row per member instead of collapsing them.

    ``resolver_falls_back`` says what an unreadable or unrecognisable page
    means for THIS supplier, and it is read out of the resolver, never
    assumed. A resolver that RAISES is already reported by the extractor
    phase, so this row passes and does not duplicate it. A resolver that
    falls back to a constant or an older card leaves every other check
    green, so this row is the only thing that can report it and must fail.
    """
    try:
        html = await _fetch_text(session, page)
        served = await served_of(html)
    except _EXTRACTOR_ERROR as err:
        # Only the provider fetch error. A renamed symbol or a changed
        # signature propagates to the caller and is recorded as a failure:
        # catching those made the gate pass green precisely when it had
        # stopped working.
        detail = f"page unreadable: {type(err).__name__}: {err}"
        if resolver_falls_back:
            _expect(label, False, f"{detail}; extractor is serving its fallback")
        else:
            _record(label, True, detail)
        return
    if not served:
        _expect(label, False, "extractor resolved no card at all")
        return
    if any(stamp is None for stamp in served):
        # The gate's own pattern cannot read a URL the extractor resolved,
        # so there is nothing to compare. That is a finding: it means the
        # two have diverged, which is the whole point of the row.
        _expect(label, False, "cannot read a card stamp out of a resolved URL")
        return
    _expect_newest_card(
        label,
        html,
        pattern,
        max([s for s in served if s is not None], key=key),
        key=key,
        exclude=exclude,
        empty_is_ok=not resolver_falls_back,
    )


def _stamps_from(pattern: str, *urls: str | None) -> list[str | None]:
    """Pull the stamp out of each resolved URL with the gate's own pattern.

    Using the same pattern on both sides keeps the comparison honest: a
    URL the loose pattern cannot read is a finding, not something to skip.
    ``None`` marks exactly that, and the caller fails the row on it.

    The unreadable SENTINEL cannot be reused here. It sorts above every
    real stamp so an unknown ADVERTISED shape becomes the newest and fails
    the comparison -- but applied to the SERVED side that same score reads
    as "newer than anything advertised" and PASSES. The two sides need
    opposite treatment of the same unknown.
    """
    out: list[str | None] = []
    for url in urls:
        if url is None:
            continue
        m = re.search(pattern, url, re.IGNORECASE)
        out.append(m.group(1) if m else None)
    return out


# Days into a month after which Mega's professional card for that month is
# expected to exist. fetch() falls back to the previous month when it does
# not, and mega.py's own comment puts the lag at "a day or two"; the extra
# margin keeps a normal publication delay from filing an issue every month.
_PRO_PUBLICATION_GRACE_DAYS = 5


async def _check_mega_professional(
    session: aiohttp.ClientSession, mega: types.ModuleType
) -> None:
    """Assert Mega's B2B cards for the current month exist.

    These cannot use a freshness ROW: Mega never links the professional
    cards from any page, so there is no advertised set to compare against.
    The CDN answers for itself instead -- a published month returns
    ``application/pdf`` and an unpublished one a ``text/html`` stub under
    the same 200 -- so HEAD is the whole check, and no card is downloaded.

    What makes this worth a check at all is that ``fetch`` silently rolls
    back one month when the current card is missing. Four of the nine
    professional contracts are variable or dynamic, so last month's card
    carries last month's index: the prices are WRONG, not merely old, and
    nothing else in this run would say so.

    Early in a month the rollback is correct behaviour, not a defect, so
    this only fails past :data:`_PRO_PUBLICATION_GRACE_DAYS`. Mega does not
    publish ahead -- next month's URL is a stub today -- so failing without
    that grace would file an issue every month.
    """
    today = mega.dt_util.now().date()
    label = "mega/freshness: professional cards published for the current month"
    missing: list[str] = []
    for contract in mega._CONTRACTS:
        if not contract.professional:
            continue
        for region, code in mega._REGION_TO_CODE.items():
            if region not in contract.regions:
                continue
            url = mega._pro_pdf_url(contract, code, today)
            try:
                async with session.head(url, allow_redirects=True) as resp:
                    ctype = resp.headers.get("Content-Type", "")
            except aiohttp.ClientError as err:
                # A transport failure is not a publication signal, and the
                # supplier's own extractor rows already report a real break.
                _record(label, True, f"HEAD failed: {type(err).__name__}: {err}")
                return
            if "pdf" not in ctype.lower():
                missing.append(f"{contract.contract_id}/{region}")
    if not missing:
        _record(label, True)
        return
    if today.day <= _PRO_PUBLICATION_GRACE_DAYS:
        _record(
            label,
            True,
            f"day {today.day} of the month, still within the publication "
            f"grace; {len(missing)} card(s) not yet up, extractor is serving "
            f"last month's",
        )
        return
    _expect(
        label,
        False,
        f"{len(missing)} professional card(s) missing for {today:%Y-%m} well "
        f"past publication; the extractor is silently serving last month's, "
        f"which carries last month's index on the variable and dynamic "
        f"contracts: {', '.join(sorted(missing)[:6])}"
        + (" ..." if len(missing) > 6 else ""),
    )


async def _check_card_freshness(
    session: aiohttp.ClientSession, modules: dict[str, types.ModuleType]
) -> None:
    """Assert every listing-resolved supplier serves its NEWEST card.

    Every other check here asks whether the fetch succeeded and the parse
    made sense. Both are true of a superseded card, so a pinned or
    unmatched URL reads as a perfectly healthy run: Bolt served June's
    formula for ten weeks behind a green board, and Ecopower served
    January's tax block once its dynamic card was renamed to YYYYMMDD.

    Covered: the eight supplier-families that pick a card from a set of
    several advertised ones, which is the shape that can silently resolve
    an older card. OCTA+, TotalEnergies, Engie and Luminus construct one
    URL per contract with no candidate set, so a wrong resolution 404s
    loudly instead; a row here would be noise, not coverage.
    """
    bolt = modules.get("bolt")
    if bolt is not None:

        async def _bolt_served(_html: str) -> list[str | None]:
            contract = bolt._CONTRACTS_BY_ID["bolt_variable"]
            return [await bolt._resolve_variable_suffix(session, contract)]

        await _freshness_row(
            "bolt/freshness: serving the newest advertised variable card",
            session,
            bolt._LISTING_URL,
            # \w+ where the extractor demands \d+: a version that grows a
            # letter must fail loudly, not resolve to the old number.
            r"pricelists/var/bolt_res_el_fr_(\w+)\.pdf",
            _bolt_served,
            key=_version_key,
            # Bolt's resolver returns _VARIABLE_SUFFIX_FALLBACK when the
            # listing will not load, so the card still downloads and parses
            # and nothing else fails. Through such an outage that constant
            # IS the hardcoded pin this gate exists to prevent.
            resolver_falls_back=True,
        )

    ecopower = modules.get("ecopower")
    if ecopower is not None:
        for family, page, resolver, exclude in (
            ("dynamic", ecopower._DBS_PAGE, ecopower._resolve_latest_dbs_pdf, ""),
            (
                "definitive",
                ecopower._PRICE_PAGE,
                ecopower._resolve_latest_pdf,
                "inschatting",
            ),
        ):
            # Spans the whole filename, not just the stamp: ``exclude``
            # tests the matched text and "inschatting" sits AFTER the stamp.
            pattern = (
                rf"/(\d{{4,8}})[a-z]?_{'dbs' if family == 'dynamic' else 'gbs'}_"
                r"[^\"\s]*?\.pdf"
            )

            async def _eco_served(
                _html: str, _r: object = resolver, _p: str = pattern
            ) -> list[str | None]:
                url, _label = await _r(session)  # type: ignore[operator]
                return _stamps_from(_p, url)

            await _freshness_row(
                f"ecopower/freshness: serving the newest advertised {family} card",
                session,
                page,
                pattern,
                _eco_served,
                key=_ecopower_date_key,
                exclude=exclude,
            )

    mega = modules.get("mega")
    if mega is not None:
        # Drops the data-product-element anchor, the product name and the
        # per-region pin the extractor requires, and widens FR/B2C/\d{6} to
        # \w+. Keeps the literal -EL- so the gas cards sharing the page
        # cannot match. "prepaid" is advertised but is not a billed card.
        pattern = r"/tarif/Mega-\w+-EL-\w+-(?:BX|VL|WL)-(\w+)-[^\"'\s]*\.pdf"

        async def _mega_served(html: str) -> list[str | None]:
            urls = [
                mega._resolve_pdf_url(html, c.product_name, code)
                for c in mega._CONTRACTS
                if not getattr(c, "professional", False)
                for region, code in mega._REGION_TO_CODE.items()
                if region in c.regions
            ]
            return _stamps_from(pattern, *urls)

        await _freshness_row(
            "mega/freshness: serving the newest advertised card",
            session,
            mega._LISTING_URL,
            pattern,
            _mega_served,
            key=_mega_month_key,
            exclude="prepaid",
        )
        await _check_mega_professional(session, mega)

    eneco = modules.get("eneco")
    if eneco is not None:
        # POWER_ keeps the gas cards out; the stamp group spans the form
        # number and the issue, which _eneco_issue_key splits.
        pattern = r"BC_([\w.-]+?)_NL_ENECO_POWER_"

        async def _eneco_served(html: str) -> list[str | None]:
            urls = [eneco._resolve_url(html, cid) for cid in eneco._CONTRACT_SLUGS]
            return _stamps_from(pattern, *urls)

        await _freshness_row(
            "eneco/freshness: serving the newest advertised card",
            session,
            eneco._LISTING_URL,
            pattern,
            _eneco_served,
            key=_eneco_issue_key,
        )

    ebem = modules.get("ebem")
    if ebem is not None:
        # Tolerates a trailing _web/_v2 suffix, an underscore separator and
        # an ISO flip, none of which the extractor's pattern allows.
        pattern = (
            r"tariefkaart[-_][a-z]*[-_]?"
            r"([a-z]{3,10}[-_]\d{4}|\d{2}[-_]\d{4}|\d{4}[-_]\d{2})"
            r"[^\"'/]*\.pdf"
        )

        async def _ebem_served(_html: str) -> list[str | None]:
            out = []
            for kind in ("elek", "dynamic"):
                _url, label = await ebem._find_latest(session, kind)
                out.append(label)  # already YYYY-MM, which the key accepts
            return out

        await _freshness_row(
            "ebem/freshness: serving the newest advertised card",
            session,
            ebem._LISTING_URL,
            pattern,
            _ebem_served,
            key=_month_year_key,
            # The kind token is matched loosely so a renamed electricity
            # card still registers, and that same looseness swallows "gas".
            # EBEM publishes 42 gas cards on this page and has twice rolled
            # them days ahead of the electricity ones, which would fail this
            # row for a card the integration does not even parse. Every
            # other multi-fuel supplier here pins its fuel in the pattern
            # (mega -EL-, eneco POWER_, frank dynamis); EBEM cannot, because
            # the token it would pin is the one being kept loose.
            exclude="gas",
        )

    cociter = modules.get("cociter")
    if cociter is not None:
        for family, contract_id in (
            ("variable", "cociter_variable"),
            ("dynamic", "cociter_dynamic"),
        ):
            head = "RCVar_YMR" if family == "variable" else "RCDyn_SM3"
            # \d{2,8} with optional dashed groups, so a dashed or widened
            # date is visible; the extractor pins an exact 4-digit YYMM.
            pattern = (
                rf"{head}[^\"\s]*?[-_]"
                r"(\d{2,8}(?:[-_]\d{1,2}){0,2}[^\"/\s]*)\.pdf"
            )

            async def _coc_served(
                _html: str, _cid: str = contract_id, _p: str = pattern
            ) -> list[str | None]:
                url, _label = await cociter._find_latest(
                    session, cociter._CONTRACT_PATTERNS[_cid]
                )
                return _stamps_from(_p, url)

            await _freshness_row(
                f"cociter/freshness: serving the newest advertised {family} card",
                session,
                cociter._INDEX_URL,
                pattern,
                _coc_served,
                key=_yymm_key,
            )

    frank = modules.get("frank")
    if frank is not None:
        # Frank has no listing page: its cards live in a Sanity CMS and the
        # resolved URL is a content hash carrying no month at all, so the
        # served stamp is the parsed LABEL. The advertised side is a
        # deliberately loose GROQ with no filename predicate, scanned for a
        # Dutch month word -- the "Elektriciteit" token the extractor keys
        # on is exactly the layer that went blind before.
        pattern = (
            r"(?m)^(?=.*dynamis).*?((?:januari|februari|maart|april|mei|juni|juli|"
            r"augustus|september|oktober|november|december)\s+20\d{2}"
            r"|20\d{2}[-_ ]?(?:0[1-9]|1[0-2]))"
        )
        try:
            rows = await frank._sanity_query(
                session,
                '*[_type=="sanity.fileAsset"]{originalFilename,_createdAt}'
                " | order(_createdAt desc)[0..59]",
            )
            blob = "\n".join(str(r.get("originalFilename", "")) for r in rows)
            served = {}
            for tier_id, _tier_label, _suffix in frank._TIERS:
                _url, card_label = await frank._resolve_pdf_url(session, tier_id)
                served[tier_id] = card_label
        except _EXTRACTOR_ERROR as err:
            _record(
                "frank/freshness: serving the newest advertised dynamic card",
                True,
                f"CMS unreadable: {type(err).__name__}: {err}",
            )
        else:
            # One row PER TIER, not one collapsed row. The break that
            # actually happened hit the four suffixed tiers while the base
            # tier stayed fine, so a family-wide comparison would have gone
            # green on it: the base tier's correct stamp is the newest and
            # hides the other four.
            for tier_id, card_label in served.items():
                label = f"frank/freshness: newest advertised {tier_id} card"
                if _frank_month_key(card_label) == _UNREADABLE_STAMP:
                    _expect(label, False, f"cannot read a month out of {card_label!r}")
                    continue
                _expect_newest_card(
                    label,
                    blob,
                    pattern,
                    card_label,
                    key=_frank_month_key,
                    exclude="combi",
                )

    energyvision = modules.get("energyvision")
    if energyvision is not None:
        for contract in energyvision._CONTRACTS:
            # The trailing literal hyphen after the code keeps the GS1JVG
            # gas card out of the GS1JV row.
            pattern = (
                rf"inline-files/EV-([^\"]*?)-{re.escape(contract.code)}-[^\"]*\.pdf"
            )

            async def _ev_served(
                _html: str, _c: object = contract, _p: str = pattern
            ) -> list[str | None]:
                url = await energyvision._resolve_card_url(session, _c)
                return _stamps_from(_p, url)

            await _freshness_row(
                f"energyvision/freshness: newest advertised {contract.code} card",
                session,
                energyvision._LISTING_URL,
                pattern,
                _ev_served,
                key=_mmyy_key,
            )


def _validate_injection(prefix: str, snap: object, shape: str = "present") -> None:
    """Gate that injection parsed AND kept the right shape (issues #31, F53).

    The coordinator drops the feed-in credit entirely when ``injection``
    is None, so a relabelled injection row silently zeroes a solar user's
    credit and used to pass CI green for all but two suppliers. ``shape``
    additionally pins the expected shape so a 0.6.7-class regression
    (monthly-indexed card flipping to a spot factor/base, applied to the
    hourly spot) fails loud instead of shipping green:

      * ``"none"``    - the region/contract pays no feed-in (DATS 24 in
        Wallonia); injection must be absent.
      * ``"monthly"`` - a realized monthly indicative; ``current`` set and
        ``factor``/``base`` must be None.
      * ``"spot"``    - a per-hour spot formula; ``factor``/``base`` set.
      * ``"spp"``     - a formula indexed on the SOLAR-weighted monthly mean,
        with the card's printed indicative kept as the fallback: ``current``,
        ``factor``/``base`` AND ``spp_indexed`` all set. The flag is what stops
        the coordinator resolving the formula against the energy leg's mean,
        which is a different index and roughly doubles the credit in a sunny
        month, so losing it is a silent mis-credit rather than a failure.
      * ``"present"`` - present, shape unconstrained (default).

    Monthly indicatives can settle slightly negative (a producer pays to
    inject at very low spot), so the magnitude lower bound allows a small
    negative floor; the upper bound catches a column-index misread.
    """
    injection = getattr(snap, "injection", None)
    if shape == "none":
        _expect(
            f"{prefix}: injection absent (region/contract pays no feed-in)",
            injection is None,
            detail=f"injection={injection}",
        )
        return
    _expect(
        f"{prefix}: injection rates present",
        injection is not None,
        detail="injection is None",
    )
    if injection is None:
        return
    current = getattr(injection, "current", None)
    factor = getattr(injection, "factor", None)
    base = getattr(injection, "base", None)
    if shape == "monthly":
        _expect(
            f"{prefix}: monthly-indexed injection (current set, no spot factor/base)",
            current is not None and factor is None and base is None,
            detail=f"current={current}, factor={factor}, base={base}",
        )
    elif shape == "spot":
        _expect(
            f"{prefix}: spot-indexed injection (factor + base present)",
            factor is not None and base is not None,
            detail=f"factor={factor}, base={base}",
        )
    elif shape == "spp":
        _expect(
            f"{prefix}: SPP-indexed injection (formula + indicative + flag)",
            current is not None
            and factor is not None
            and base is not None
            and bool(getattr(injection, "spp_indexed", False)),
            detail=f"current={current}, factor={factor}, base={base}, "
            f"spp_indexed={getattr(injection, 'spp_indexed', None)}",
        )
    if current is not None:
        _expect(
            f"{prefix}: injection credit in [-0.10, 0.20] EUR/kWh",
            -0.10 <= current <= 0.20,
            detail=f"current={current}",
        )
    elif shape == "present":
        _expect(
            f"{prefix}: dynamic injection factor + base present",
            factor is not None and base is not None,
            detail=f"factor={factor}, base={base}",
        )


# Explicit injection-shape overrides per contract id (see _validate_injection).
# Anything not listed is derived from contract metadata by
# _expected_injection_shape, so a new card is covered without editing this map.
_INJECTION_SHAPE: dict[str, str] = {
    "power_fix": "monthly",
    "power_flex": "monthly",
    "ebem_variable": "monthly",
    "ebem_basic_plus": "monthly",
    "ecofix_flexy": "monthly",
    # Cociter Variable's injection is itself spot-indexed (factor x BELPEX
    # + base, current None at parse time). Pinning it to "spot" asserts
    # factor+base unconditionally; the "present" default only checks them
    # while current happens to be None, so a regression that set current
    # would slip the shape check.
    "cociter_variable": "spot",
    # energie.be Variabel indexes its injection on Belpex_SPP while its energy
    # indexes on Belpex_RLP, so it carries the formula, the card's printed
    # indicative as a fallback, AND the flag that keeps the two indices apart.
    # Dropping any of the three is a silent mis-credit: without the flag the
    # formula resolves against the energy leg's mean and roughly doubles the
    # credit in a sunny month, and without the indicative there is nothing to
    # credit while the Synergrid profile is unavailable.
    "energiebe_variable": "spp",
}

# contract id -> Contract, populated in _run once the providers are loaded so
# _expected_injection_shape can derive a shape for every card.
_CONTRACTS_BY_ID: dict[str, object] = {}


def _expected_injection_shape(contract_id: str) -> str:
    """Expected injection shape for a contract.

    Explicit _INJECTION_SHAPE entries win; otherwise derive from the
    contract's own metadata so a fixed/variable (monthly-indexed) card can't
    silently gain a spot factor/base -- the 0.6.7-class latent mispricing --
    without failing here. ``spot_indexed_injection`` marks the one variable
    card whose injection is a spot formula (Cociter Variable); dynamic and TOU
    cards carry factor/base or per-slot rates, so they stay presence-only."""
    if contract_id in _INJECTION_SHAPE:
        return _INJECTION_SHAPE[contract_id]
    contract = _CONTRACTS_BY_ID.get(contract_id)
    if contract is None:
        return "present"
    if getattr(contract, "spot_indexed_injection", False):
        return "spot"
    if getattr(contract, "kind", "") in ("fixed", "variable"):
        return "monthly"
    return "present"


def _expect_region_basics(prefix: str, region_key: str, snap: object) -> None:
    """The three assertions every per-(contract, region) check makes.

    Expected DSOs present, that region's renewables levy above zero, and the
    federal excise above zero. Seven checks carried these three byte-identical
    _expect calls plus their own copy of the region maps, so a new
    region-level invariant had to be added seven times and the copies had
    already fallen into two arity groups.

    Deliberately only these three. The publication-label assertion differs
    between checks (some pass a detail=), and the per-supplier extras --
    professional VAT basis, excise bands, energy contribution, the ORES band
    ordering -- stay at their call sites where their reasons live.
    """
    taxes = getattr(snap, "taxes", None)
    dsos = getattr(snap, "dsos", None) or {}
    expected = _EXPECTED_DSOS[region_key]
    _expect(
        f"{prefix}: expected DSOs present",
        expected <= set(dsos),
        detail=f"missing: {sorted(expected - set(dsos))}",
    )
    _expect(
        f"{prefix}: regional renewables > 0",
        getattr(taxes, _RENEWABLES_FIELD[region_key]) > 0,
        detail=str(taxes),
    )
    _expect(
        f"{prefix}: federal excise > 0",
        getattr(taxes, "federal_excise", 0) > 0,
        detail=str(taxes),
    )


# Suppliers whose card legitimately is NOT for the current month, with the
# reason. Everything else is expected to serve a card labelled for the month
# it is being billed in; measured across all 251 contract-regions, 202 of 206
# non-exempt ones did, and the four that did not were the Bolt bug this gate
# was built for.
# How many months behind the current one a contract's card may legitimately
# be. Zero for almost everything: a supplier bills the month it is in. An
# entry here is an ALLOWANCE WITH A CEILING, not a skip -- a card further
# behind than its entry still fails, so a supplier that stops publishing
# altogether is caught even where some lag is expected.
#
# A supplier winding down is NOT handled here; its date lives on its own
# EXTRACTOR (deprecated_until) and is read below, so that allowance expires
# with the withdrawal.
_PERIOD_MAX_LAG_MONTHS: dict[str, int] = {
    # Ecopower's DEFINITIVE card publishes in arrears, landing at the end of
    # the month it covers, so through August the newest definitive card is
    # July's: exactly one month, every month. Measured on the live page,
    # which carries 202604..202607 contiguously with no gap. Two months
    # behind is therefore not arrears, it is Ecopower having stopped, and
    # still fails. Keyed on the CONTRACT: the dynamic sibling publishes
    # normally and exempting the supplier would re-hide the 0.12.5 bug.
    "ecopower_burgerstroom": 1,
}

# The arrears allowance above rests on a publishing convention, not on a
# date the supplier declares, so nothing can expire it automatically the way
# deprecated_until does. It is pinned to a review date instead, enforced by
# a test rather than at runtime: a runtime expiry would start failing on
# perfectly normal arrears, which is a false alarm by construction. Same
# convention as the Bolt re-verify note in providers/bolt.py.
_PERIOD_LAG_REVIEW_BY = date(2027, 2, 1)

# Days into a month before a card still labelled for the previous month is
# treated as stale rather than as a supplier publishing a little late. Same
# reasoning as _PRO_PUBLICATION_GRACE_DAYS.
_PERIOD_GRACE_DAYS = 5

_FR_MONTH_NAMES: dict[str, int] = {
    "janvier": 1,
    "février": 2,
    "fevrier": 2,
    "mars": 3,
    "avril": 4,
    "mai": 5,
    "juin": 6,
    "juillet": 7,
    "août": 8,
    "aout": 8,
    "aôut": 8,
    "septembre": 9,
    "octobre": 10,
    "novembre": 11,
    "décembre": 12,
    "decembre": 12,
}
_NL_MONTH_NAMES: dict[str, int] = {
    "januari": 1,
    "februari": 2,
    "maart": 3,
    "april": 4,
    "mei": 5,
    "juni": 6,
    "juli": 7,
    "augustus": 8,
    "september": 9,
    "oktober": 10,
    "november": 11,
    "december": 12,
}


def _label_month(label: str) -> tuple[int, int] | None:
    """Read ``(year, month)`` out of a card's publication label.

    Every shape the suppliers actually print: ``08/2026``, ``2026-08`` and a
    French or Dutch month name with a year. The name match is unicode-aware
    on purpose -- a class that forgets the ``u`` in ``août`` silently fails to
    read 104 of the 236 live labels, and an unreadable label is skipped, so
    the check would have quietly covered almost nothing.

    Returns None when the shape is unknown, which the caller reports without
    failing: an unrecognised label is not evidence of staleness.
    """
    s = label.strip().lower()
    m = re.fullmatch(r"(\d{2})/(\d{4})", s)
    if m:
        return int(m.group(2)), int(m.group(1))
    m = re.fullmatch(r"(\d{4})-(\d{2})", s)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.fullmatch(r"([^\W\d_]+)\s+(\d{4})", s, re.UNICODE)
    if m:
        num = _FR_MONTH_NAMES.get(m.group(1)) or _NL_MONTH_NAMES.get(m.group(1))
        if num:
            return int(m.group(2)), num
    return None


def _expect_card_period(prefix: str, contract_id: str, snap: object) -> None:
    """Assert the card being billed says it is for the current month.

    The other freshness checks ask whether a NEWER card exists somewhere.
    This asks the card itself, which is the only question available when the
    supplier overwrites one fixed URL in place: OCTA+, TotalEnergies, Engie
    and Luminus each construct a single URL per contract, so no comparison
    against an advertised set is possible, and a supplier that simply stopped
    updating that file would serve a year-old card behind a green board.

    Two assertions, both from the snapshot the caller already fetched:
    ``valid_until`` must not have passed, and the publication label must not
    name a month earlier than this one. A label NEWER than the current month
    passes -- publishing early is not staleness.
    """
    supplier = prefix.split("/", 1)[0]
    today = datetime.now(ZoneInfo("Europe/Brussels")).date()

    # A supplier on its way out keeps publishing until its last day, so an
    # older card is not yet evidence of anything. Past that date it has left
    # the market: its final card stays up and stays stale forever, which is
    # real and worth showing but cannot be fixed here, so it reports without
    # setting the extractor bit or filing an issue every night. The date is
    # the supplier's own deprecated_until, so this allowance ends when the
    # withdrawal does, with nothing here to remember to remove.
    withdrawn = _DEPRECATED_UNTIL.get(supplier)
    if withdrawn is not None and today <= withdrawn:
        return
    marker = f"{_WITHDRAWN_MARKER}: " if withdrawn is not None else ""

    valid_until = getattr(snap, "valid_until", None)
    if valid_until is not None and valid_until < today:
        _expect(
            f"{prefix}: card has not expired",
            False,
            f"{marker}valid_until {valid_until} passed on {today}",
        )

    label = str(getattr(snap, "publication_label", "") or "")
    parsed = _label_month(label)
    if parsed is None:
        # Not a failure: an unknown label shape is not evidence of staleness.
        # Reported so a new shape is visible rather than silently uncovered.
        _record(f"{prefix}: card period readable", True, f"unparsed label {label!r}")
        return
    year, month = parsed
    lag = (today.year - year) * 12 + (today.month - month)
    allowed = _PERIOD_MAX_LAG_MONTHS.get(contract_id, 0)
    if today.day <= _PERIOD_GRACE_DAYS:
        # A supplier publishing a few days late is not a stale card.
        allowed += 1
    # Recorded pass or fail, like every other assertion here. A check that
    # emits a row only when it fails cannot be told apart from one that never
    # ran, which is the shape this whole gate exists to stamp out.
    _expect(
        f"{prefix}: card is recent enough",
        lag <= allowed,
        f"{marker}card is labelled {label!r} on {today}: {lag} month(s) behind, "
        f"at most {allowed} expected",
    )


def _validate_snapshot(
    prefix: str,
    contract_id: str,
    snap: object,
    *,
    injection_shape: str | None = None,
    require_capacity: frozenset[str] = frozenset(),
) -> None:
    """Validate the energy rates and the injection coverage/shape of one
    fetched snapshot. Called by every ``_check_*`` after its
    supplier-specific DSO / tax assertions. ``injection_shape`` overrides
    the per-contract default (used for region-dependent cases like
    DATS 24, whose Wallonia card pays no feed-in)."""
    _expect_card_period(prefix, contract_id, snap)
    _validate_energy(prefix, contract_id, getattr(snap, "energy", None))
    shape = injection_shape or _expected_injection_shape(contract_id)
    _validate_injection(prefix, snap, shape)
    _validate_dsos(prefix, snap, require_capacity=require_capacity)


# The Fluvius row five Flanders checks spot-checked by hand for a PRESENT
# capacity tariff. Kept as exactly the one key they used: widening it to all
# eight areas is a real coverage increase, not a refactor, and would want a
# look at a live report first.
_CAPACITY_REQUIRED = frozenset({"fluvius_antwerpen"})


def _validate_dsos(
    prefix: str, snap: object, *, require_capacity: frozenset[str] = frozenset()
) -> None:
    """Bounds-check the capacity tariff on every DSO overlay a supplier
    populates, so a column-index misread fails CI instead of shipping
    silently (the capaciteitstarief is the dominant cost for a Flemish
    digital-meter user). It's a regulated, supplier-independent rate; the
    field is None on non-Flemish overlays, which are skipped.

    ``require_capacity`` names DSO keys whose capacity must additionally be
    PRESENT, not merely in range. The loop below cannot catch a None: it has
    nothing to bounds-check. Five suppliers spot-checked exactly that on
    fluvius_antwerpen with their own pasted block, each one sitting directly
    after the _validate_snapshot call that already runs this function, so the
    same bound was recorded twice per run. Those blocks are the leftovers this
    parameter replaces: a key absent from the snapshot is still not asserted,
    which is what they did."""
    dsos = getattr(snap, "dsos", None) or {}
    for key, overlay in dsos.items():
        capacity = getattr(overlay, "capacity_eur_per_kw_year", None)
        if key in require_capacity:
            _expect(
                f"{prefix}: {key} publishes a capacity tariff",
                capacity is not None,
                detail=str(overlay),
            )
        if capacity is not None:
            _expect(
                f"{prefix}: {key} capacity tariff in [20, 200] EUR/kW/yr",
                20.0 <= capacity <= 200.0,
                detail=f"capacity={capacity}",
            )


def _validate_energy(prefix: str, contract_id: str, energy: object) -> None:  # noqa: ARG001 - contract_id reserved for richer validation
    if isinstance(energy, _RATE_FIXED):
        rate = getattr(energy, "single", None)
        _expect(
            f"{prefix}: fixed rate in [0.05, 0.50] EUR/kWh",
            rate is not None and 0.05 <= rate <= 0.50,
            detail=f"single={rate}",
        )
    elif isinstance(energy, _RATE_VARIABLE):
        current = getattr(energy, "current", None)
        _expect(
            f"{prefix}: variable rate in [0.05, 0.50] EUR/kWh",
            current is not None and 0.05 <= current <= 0.50,
            detail=f"current={current}",
        )
    elif isinstance(energy, _RATE_DYNAMIC):
        factor = getattr(energy, "factor", None)
        base = getattr(energy, "base", None)
        # factor is in EUR/kWh per spot in EUR/kWh; ~1.0-1.2 today.
        _expect(
            f"{prefix}: dynamic factor in [0.5, 3.0]",
            factor is not None and 0.5 <= factor <= 3.0,
            detail=f"factor={factor}",
        )
        _expect(
            f"{prefix}: dynamic base in [0, 0.10] EUR/kWh",
            base is not None and 0.0 <= base <= 0.10,
            detail=f"base={base}",
        )
    elif isinstance(energy, _RATE_SPOT_MONTHLY):
        factor = getattr(energy, "factor", None)
        base = getattr(energy, "base", None)
        # Same units as the dynamic leg: the monthly mean spot is substituted
        # for the slot price, so the coefficients live on the same scale.
        _expect(
            f"{prefix}: spot-monthly factor in [0.5, 3.0]",
            factor is not None and 0.5 <= factor <= 3.0,
            detail=f"factor={factor}",
        )
        _expect(
            f"{prefix}: spot-monthly base in [0, 0.10] EUR/kWh",
            base is not None and 0.0 <= base <= 0.10,
            detail=f"base={base}",
        )
    elif isinstance(energy, _RATE_TOU):
        peak = getattr(energy, "peak", None)
        transition = getattr(energy, "transition", None)
        offpeak = getattr(energy, "offpeak", None)
        for label, rate in (
            ("peak", peak),
            ("transition", transition),
            ("offpeak", offpeak),
        ):
            _expect(
                f"{prefix}: TOU {label} in [0.05, 0.50] EUR/kWh",
                rate is not None and 0.05 <= rate <= 0.50,
                detail=f"{label}={rate}",
            )
        # peak should be the most expensive band, offpeak the cheapest.
        if peak is not None and transition is not None and offpeak is not None:
            _expect(
                f"{prefix}: TOU bands ordered peak >= transition >= offpeak",
                peak >= transition >= offpeak,
                detail=f"peak={peak}, transition={transition}, offpeak={offpeak}",
            )
    elif isinstance(energy, _RATE_IMPACT):
        pic = getattr(energy, "pic", None)
        medium = getattr(energy, "medium", None)
        eco = getattr(energy, "eco", None)
        for label, rate in (
            ("pic", pic),
            ("medium", medium),
            ("eco", eco),
        ):
            _expect(
                f"{prefix}: Impact {label} in [0.05, 0.50] EUR/kWh",
                rate is not None and 0.05 <= rate <= 0.50,
                detail=f"{label}={rate}",
            )
        # pic should be the most expensive band, eco the cheapest.
        if pic is not None and medium is not None and eco is not None:
            _expect(
                f"{prefix}: Impact bands ordered pic >= medium >= eco",
                pic >= medium >= eco,
                detail=f"pic={pic}, medium={medium}, eco={eco}",
            )
    else:
        _record(
            f"{prefix}: energy type",
            False,
            f"unknown energy class {type(energy).__name__}",
        )


def _render_metrics(metrics: dict[str, dict[str, float]]) -> str:
    """Per-supplier fetch-time + bytes-received block for the report.

    The time column is the SUM of per-request durations (not true
    wallclock): concurrent fetches add up even though they overlap in
    real time. Empty when nothing was traced (e.g. the catalog-only
    report). Emits a leading blank line so it slots cleanly between
    sections without collapsing into an adjacent table.
    """
    if not metrics:
        return ""
    rows = ["", "## Per-supplier latency / size", ""]
    rows.append(
        "| Supplier | Fetches | Fetch time (s) | Bytes received | Failed (n / s) |"
    )
    rows.append("| --- | ---: | ---: | ---: | ---: |")
    for supplier, m in sorted(metrics.items()):
        bytes_str = f"{int(m['bytes']):,}" if m["bytes"] else "-"
        # Failed attempts are counted separately from Fetches: a request that
        # raised produced no response, so folding it into the success columns
        # would corrupt the latency the drift budgets are calibrated on. A
        # supplier with fetches but no bytes and no failures got its headers
        # and then stalled mid-body.
        failed = m.get("failed", 0.0)
        failed_str = f"{int(failed)} / {m.get('failed_s', 0.0):.2f}" if failed else "-"
        rows.append(
            f"| `{supplier}` | {int(m['fetches'])} | "
            f"{m['elapsed_s']:.2f} | {bytes_str} | {failed_str} |"
        )
    return "\n".join(rows) + "\n"


def _render_report(
    checks: Iterable[Check], metrics: dict[str, dict[str, float]] | None = None
) -> str:
    rows: list[str] = []
    checks = list(checks)
    pass_count = sum(1 for c in checks if c.ok)
    regressions = [c for c in checks if not c.ok and not c.expected]
    expected = [c for c in checks if not c.ok and c.expected]
    headline = f"# Live extractor check — {pass_count} pass, {len(regressions)} fail"
    if expected:
        # Say it in the headline. A run that reads "0 fail" while the table
        # below lists failing rows reads like a bug in the harness.
        headline += f", {len(expected)} unreadable (expected)"
    rows.append(headline)
    rows.append("")
    if regressions:
        rows.append("## Failures")
        rows.append("")
        rows.append("| Check | Detail |")
        rows.append("| --- | --- |")
        for c in regressions:
            detail = (c.detail or "").replace("|", "\\|").replace("\n", " ")
            rows.append(f"| `{c.label}` | {detail} |")
        rows.append("")
    if expected:
        rows.append("## Unreadable cards (expected, not a regression)")
        rows.append("")
        rows.append(
            "These suppliers publish their tariff card as page images, so there "
            "is no text layer to parse. No change to this repository can fix "
            "them; affected entries raise the `extractor_unreadable` Repairs "
            "card pointing at the custom-supplier workaround. These rows do "
            "not fail the run, and they disappear on their own if the supplier "
            "goes back to publishing text."
        )
        rows.append("")
        rows.append("| Check | Detail |")
        rows.append("| --- | --- |")
        for c in expected:
            detail = (c.detail or "").replace("|", "\\|").replace("\n", " ")
            rows.append(f"| `{c.label}` | {detail} |")
        rows.append("")
    rows.append("## All checks")
    rows.append("")
    for c in checks:
        marker = "[x]" if c.ok else "[ ]"
        rows.append(f"- {marker} {c.label}")
    out = "\n".join(rows) + "\n"
    if metrics:
        out += _render_metrics(metrics)
    return out


# supplier id -> its check coroutine. Defined here because every _check_* has
# to exist first. The keys must equal _SUPPLIERS exactly, which the assert
# below pins: a supplier added to one and not the other is the failure this
# whole arrangement exists to make impossible.
_CHECKS_BY_SUPPLIER: dict[
    str, Callable[[aiohttp.ClientSession, types.ModuleType], Awaitable[None]]
] = {
    "eneco": _check_eneco,
    "cociter": _check_cociter,
    "dats24": _check_dats24,
    "ebem": _check_ebem,
    "ecofix": _check_ecofix,
    "ecopower": _check_ecopower,
    "engie": _check_engie,
    "luminus": _check_luminus,
    "mega": _check_mega,
    "totalenergies": _check_totalenergies,
    "bolt": _check_bolt,
    "octaplus": _check_octaplus,
    "frank": _check_frank,
    "energiebe": _check_energiebe,
    "energyvision": _check_energyvision,
}
assert set(_CHECKS_BY_SUPPLIER) == set(_SUPPLIERS), (
    "live check supplier list and check registry disagree: "
    f"{set(_SUPPLIERS) ^ set(_CHECKS_BY_SUPPLIER)}"
)


async def _run() -> int:
    modules = _load_providers()
    # Index every contract so _expected_injection_shape can derive a shape
    # for cards not explicitly listed in _INJECTION_SHAPE.
    for _mod in modules.values():
        for _contract in _mod.EXTRACTOR.contracts:
            _CONTRACTS_BY_ID[_contract.id] = _contract
    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(
        timeout=timeout, trace_configs=[_trace_config()]
    ) as session:
        # Run the per-supplier checks concurrently. ContextVars are
        # copy-on-write per asyncio.Task, so the _attributed() ContextVar
        # set inside each task only changes that task's view; aiohttp's
        # TraceConfig hooks run in the same task that issued the request,
        # so per-supplier byte / latency attribution stays correct.
        await asyncio.gather(
            *(
                _attributed_check(
                    supplier, _CHECKS_BY_SUPPLIER[supplier], session, modules[supplier]
                )
                for supplier in _SUPPLIERS
            )
        )
        # Catalog probes fan out across suppliers; attribute them
        # to a synthetic bucket so they don't double-count against
        # any one supplier's per-card timing.
        with _attributed("_catalog"):
            try:
                await _check_catalogs(session, modules)
            except Exception as err:  # noqa: BLE001
                # The catalog phase dereferences provider-internal attributes
                # (renamed by a refactor -> AttributeError). Record it as a
                # catalog failure instead of letting it escape and discard the
                # extractor report that was already computed above.
                _record(
                    "_catalog: probe crashed",
                    False,
                    f"{type(err).__name__}: {err}",
                    kind="catalog",
                )
            # Its own try: a catalog crash must not swallow the freshness
            # gate, which is the one check that sees a supplier superseding
            # a card we still resolve.
            try:
                await _check_card_freshness(session, modules)
            except Exception as err:  # noqa: BLE001
                _record(
                    "_freshness: probe crashed",
                    False,
                    f"{type(err).__name__}: {err}",
                    kind="catalog",
                )

    extractor_checks = [c for c in CHECKS if c.kind == "extractor"]
    catalog_checks = [c for c in CHECKS if c.kind == "catalog"]
    # Stdout = extractor report (existing workflow consumes this).
    # Metrics piggyback on the extractor report so silent slowdowns and
    # PDF-size jumps surface daily without a separate pipeline.
    print(_render_report(extractor_checks, METRICS))
    # Side-channel: catalog report goes to a known file the workflow
    # picks up to open / update its own issue, separate from the
    # extractor-broken issue so the two failure modes don't conflate.
    # Anchor side-channel reports on the repo root rather than the
    # process CWD so a developer running this script from any directory
    # gets the file alongside the existing logs (CI happens to invoke
    # from repo root, so behaviour there is unchanged).
    (ROOT / "catalog_report.md").write_text(_render_report(catalog_checks))
    drift_warnings = _drift_warnings(METRICS, _failed_suppliers(extractor_checks))
    (ROOT / "drift_report.md").write_text(_render_drift(drift_warnings))
    regressions = _extractor_regressions(extractor_checks)
    # Side-channel: this attempt's failing check labels for the workflow's
    # retry loop to intersect across attempts.
    _write_failure_labels(ROOT / "extractor_failures.txt", regressions)
    extractor_failed = bool(regressions)
    catalog_failed = any(not c.ok for c in catalog_checks)
    drift_alert = bool(drift_warnings)
    # Bit-encoded exit codes:
    #   bit 0 (1) = extractor failure
    #   bit 1 (2) = catalog signal
    #   bit 2 (4) = drift alert
    return (
        (1 if extractor_failed else 0)
        | (2 if catalog_failed else 0)
        | (4 if drift_alert else 0)
    )


# Static drift thresholds. The default (5 MB / 90 s) catches a fresh
# regression at any supplier; per-supplier overrides cover known-large
# catalogs whose total over the full check is honestly above the
# default but stable. The override is the budget we expect plus ~25%
# headroom; cross it and something genuinely changed.
LATENCY_WARN_THRESHOLD_S = 90.0
BYTES_WARN_THRESHOLD = 5_000_000

# Per-supplier byte budgets (override the global). Picked off the
# steady-state metrics table after the bolt 3x-refetch fix:
#   * bolt: ~32 MB (6 contracts x ~5 MB PDF, parsed once for all 3
#     regions). Allow 50 MB for headroom and a possible new product.
#   * totalenergies: ~11 MB (7 contracts x 3 regions, ~0.45 MB each).
#     Allow 15 MB.
#   * engie: ~5.4 MB (sitemap discovery + ~24 region PDFs). Allow 8 MB
#     so we don't fire on a slow day.
#   * ecofix: ~6.6 MB (3 contracts x 2 regions, ~1.1 MB each PDF).
#     Allow 8 MB.
#   * mega: ~5.3 MB (~342KB listing fetched once via the harness's
#     listing-cache + 33 region PDFs at ~150KB each). Allow 7 MB so a
#     slow CI day or a slightly larger PDF batch doesn't fire.
#   * octaplus: ~17.6 MB (8 contracts x 2 regions at ~1 MB each after the
#     2026 card redesign; fixed_impact is Wallonia-only). Allow 22 MB.
_BYTES_BUDGET_OVERRIDES: dict[str, int] = {
    # bolt, engie and mega each gained a professional edition of most of
    # their catalogue (August 2026), which roughly doubles the number of
    # cards fetched: bolt 7 -> 14 of its ~5 MB PDFs, engie ~24 -> ~48
    # region PDFs, mega 33 -> ~57. Budgets doubled to match, still with
    # the same slack the residential-only figures carried.
    "bolt": 100_000_000,
    "totalenergies": 15_000_000,
    "engie": 16_000_000,
    "ecofix": 8_000_000,
    "mega": 14_000_000,
    "octaplus": 22_000_000,
}

# Per-supplier latency budgets (override the global). NOTE: elapsed_s
# is the SUM of per-request durations (accumulated in _on_request_end),
# not true wallclock -- so a supplier that fetches concurrently records
# the sum of its parallel fetches even though they overlap in real
# time. Sized to "observed slow-day summed fetch time + ~20-25%
# headroom" so the retry helper's per-PDF overhead (1-3s per fired
# retry, see _fetch_with_retry) doesn't push a normal slow day over
# budget. bolt fetches its six ~5 MB PDFs concurrently (see _check_bolt),
# so on a slow-CDN day (issue #13) the six 20-30 s fetches sum to ~180 s
# even though real wallclock stays well under the 240 s hard cap and the
# snapshot succeeds; budget it accordingly so it doesn't false-fire.
# The same professional editions double the fetch count for bolt, engie
# and mega. elapsed_s is the SUM of per-request durations, so it scales
# with the count even where the fetches overlap. Every value here must
# stay under _SUPPLIER_HARD_TIMEOUT_S, or the supplier is killed before
# it can ever report the drift these budgets exist to catch.
_LATENCY_BUDGET_OVERRIDES: dict[str, float] = {
    "bolt": 400.0,
    # EBEM and Luminus (below) are slow only from GitHub runners, the same
    # shape as eneco: from a residential line their 6 and 20 fetches sum to
    # 0.7s and 1.4s for byte-identical payloads. Sized off 63 attempts
    # across 11 runs, where ebem ran 41s median / 112s max and luminus 111s
    # median / 169s max. The old 90s default and 150s budget sat on their
    # p95, so the alert fired on whichever attempt the extractor retry loop
    # happened to end on rather than on any change to the cards (issue #60).
    "ebem": 130.0,
    # eneco.be answers slowly from GitHub runners and fast from anywhere
    # else: the listing page it times out on in CI serves in ~1.3 s and
    # ~180 KB from a residential line. Its six small fetches summed to
    # 35-96 s across the six attempts of one run, with a TimeoutError on
    # every attempt, so the 90 s default fires on host latency rather
    # than on any change to the cards. Same shape as the bolt CDN and
    # the mega runner-IP block.
    "eneco": 150.0,
    "engie": 260.0,
    # See the EBEM note above.
    "luminus": 210.0,
    "mega": 240.0,
    # TotalEnergies and OCTA+ fetch every (contract, region) PDF
    # sequentially (25 and 21 fetches), so their summed elapsed_s blows
    # the 90 s default on a slow day even though each fetch is small and
    # the snapshot succeeds. Budget like the other multi-fetch suppliers,
    # well under the 240 s hard cap.
    "totalenergies": 150.0,
    "octaplus": 130.0,
}


def _bytes_budget(supplier: str) -> int:
    return _BYTES_BUDGET_OVERRIDES.get(supplier, BYTES_WARN_THRESHOLD)


def _latency_budget(supplier: str) -> float:
    return _LATENCY_BUDGET_OVERRIDES.get(supplier, LATENCY_WARN_THRESHOLD_S)


def _extractor_regressions(checks: Iterable[Check]) -> list[Check]:
    """Failures that should gate CI.

    An unreadable card is a real failure but not a regression: the supplier
    publishes page images, and no change to this repository can read them.
    Letting it set the extractor bit meant one such supplier failed every
    run forever, exhausted the workflow's retry loop, and refiled a fresh
    issue each time the last was closed. It still shows in the report, in
    its own table, so it is visible without being actionable noise.
    """
    return [c for c in checks if not c.ok and not c.expected]


def _write_failure_labels(path: Path, checks: Iterable[Check]) -> None:
    """Write one check label per line, sorted, for the workflow's retry loop.

    The loop intersects this file across its attempts and only files an
    issue for what failed in EVERY attempt. On a slow runner each attempt
    times out on a different random subset of suppliers, so no attempt is
    green and the loop used to file whichever hosts were unlucky on the
    last one (issue #61); a parse error or a withdrawn card fails the same
    checks every attempt and still files.

    Only pass regressions here. An unreadable card fails every attempt by
    definition, so feeding it in would intersect with itself and refile
    daily - the exact noise _extractor_regressions exists to drop.
    """
    path.write_text("".join(f"{label}\n" for label in sorted(c.label for c in checks)))


def _failed_suppliers(checks: Iterable[Check]) -> frozenset[str]:
    """Suppliers carrying at least one failed check.

    Labels are `<supplier>: <what>` or `<supplier>/<contract>[/<region>]:
    <what>`, so the supplier is whatever precedes the first separator.
    """
    return frozenset(
        check.label.split(":", 1)[0].split("/", 1)[0].strip()
        for check in checks
        if not check.ok
    )


def _drift_warnings(
    metrics: dict[str, dict[str, float]],
    failed: frozenset[str] = frozenset(),
) -> list[str]:
    """Static-threshold drift signals: latency or byte budgets blown."""
    warnings: list[str] = []
    for supplier, m in sorted(metrics.items()):
        if supplier == "_catalog":
            # The catalog pass aggregates every supplier's discovery
            # listing fetch under one synthetic bucket, so its bytes and
            # wallclock blow any single-supplier budget by design. It is
            # not a per-supplier regression signal; skip it rather than
            # auto-open a spurious drift issue.
            continue
        if supplier in failed:
            # This supplier's extractor already failed, which is both the
            # louder signal and the likely cause of the numbers: a
            # supplier that reworks its cards changes their size, and the
            # workflow then retries the whole run for an hour, so every
            # other supplier gets several more rolls against its budget
            # and drift is judged on whichever attempt happened to be
            # last. Reporting drift as well splits one supplier-side
            # event across two issues and refiles it daily for as long as
            # the extractor stays broken. Keep the measurement in the log
            # so tuning a budget later does not need a rerun.
            print(
                f"drift: {supplier} skipped, extractor failed "
                f"(elapsed {m['elapsed_s']:.1f}s, {int(m['bytes']):,} bytes)",
                file=sys.stderr,
            )
            continue
        latency_budget = _latency_budget(supplier)
        if m["elapsed_s"] > latency_budget:
            warnings.append(
                f"`{supplier}` fetch time {m['elapsed_s']:.1f}s "
                f"exceeds {latency_budget:.0f}s budget"
            )
        budget = _bytes_budget(supplier)
        if m["bytes"] > budget:
            warnings.append(
                f"`{supplier}` received {int(m['bytes']):,} bytes "
                f"exceeds {budget:,} byte budget"
            )
    return warnings


def _render_drift(warnings: list[str]) -> str:
    if not warnings:
        return "# Live-check drift — no warnings\n"
    rows = ["# Live-check drift — alerts", ""]
    for w in warnings:
        rows.append(f"- {w}")
    return "\n".join(rows) + "\n"


def main() -> int:
    try:
        return asyncio.run(_run())
    except Exception:
        # Harness crash. Use rc=8 (outside the documented 1/2/4 bit
        # space) so the workflow doesn't open a "supplier extractor
        # broken" issue for what's actually a bug in this script.
        traceback.print_exc()
        return 8


if __name__ == "__main__":
    sys.exit(main())

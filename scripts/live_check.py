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
import sys
import time
import traceback
import types
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import aiohttp

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "custom_components" / "be_electricity_prices"


def _load_providers() -> tuple[types.ModuleType, ...]:
    """Load the providers package without dragging Home Assistant into scope."""
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

    _load("be_pkg.providers.base", PKG / "providers" / "base.py")
    _load("be_pkg.providers._pdf", PKG / "providers" / "_pdf.py")
    eneco = _load("be_pkg.providers.eneco", PKG / "providers" / "eneco.py")
    cociter = _load("be_pkg.providers.cociter", PKG / "providers" / "cociter.py")
    dats24 = _load("be_pkg.providers.dats24", PKG / "providers" / "dats24.py")
    ebem = _load("be_pkg.providers.ebem", PKG / "providers" / "ebem.py")
    ecofix = _load("be_pkg.providers.ecofix", PKG / "providers" / "ecofix.py")
    ecopower = _load("be_pkg.providers.ecopower", PKG / "providers" / "ecopower.py")
    engie = _load("be_pkg.providers.engie", PKG / "providers" / "engie.py")
    luminus = _load("be_pkg.providers.luminus", PKG / "providers" / "luminus.py")
    mega = _load("be_pkg.providers.mega", PKG / "providers" / "mega.py")
    totalenergies = _load(
        "be_pkg.providers.totalenergies", PKG / "providers" / "totalenergies.py"
    )
    bolt = _load("be_pkg.providers.bolt", PKG / "providers" / "bolt.py")
    octaplus = _load("be_pkg.providers.octaplus", PKG / "providers" / "octaplus.py")
    return (
        eneco,
        cociter,
        dats24,
        ebem,
        ecofix,
        ecopower,
        engie,
        luminus,
        mega,
        totalenergies,
        bolt,
        octaplus,
    )


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


CHECKS: list[Check] = []


# Per-supplier wallclock + bytes-received accounting. Populated via an
# aiohttp TraceConfig that tags every request with whichever supplier
# is currently being checked (set by the _attributed() context
# manager). Surfaces silent slowdowns and PDF-size jumps in the
# report; both are leading indicators that a supplier reworked its
# tariff publication.
METRICS: dict[str, dict[str, float]] = {}
_CURRENT_SUPPLIER: ContextVar[str | None] = ContextVar(
    "be_live_check_supplier", default=None
)


def _metrics_bucket(supplier: str) -> dict[str, float]:
    return METRICS.setdefault(
        supplier, {"fetches": 0.0, "elapsed_s": 0.0, "bytes": 0.0}
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
    params: aiohttp.TraceRequestEndParams,
) -> None:
    supplier = _CURRENT_SUPPLIER.get()
    if supplier is None:
        return
    bucket = _metrics_bucket(supplier)
    bucket["fetches"] += 1.0
    bucket["elapsed_s"] += time.monotonic() - getattr(ctx, "start", time.monotonic())
    cl = params.response.headers.get("Content-Length")
    if cl is not None:
        try:
            bucket["bytes"] += float(cl)
        except ValueError:
            # Non-numeric header is upstream noise, not our problem.
            pass


def _trace_config() -> aiohttp.TraceConfig:
    tc = aiohttp.TraceConfig()
    tc.on_request_start.append(_on_request_start)
    tc.on_request_end.append(_on_request_end)
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


def _record(label: str, ok: bool, detail: str = "", kind: str = "extractor") -> None:
    CHECKS.append(Check(label=label, ok=ok, detail=detail, kind=kind))


def _expect(label: str, condition: bool, detail: str = "") -> bool:
    _record(label, condition, detail if not condition else "")
    return condition


async def _check_eneco(session: aiohttp.ClientSession, eneco: types.ModuleType) -> None:
    expected_dso_keys = {
        # Wallonia
        "aieg",
        "aiesh",
        "ores",
        "resa",
        "rew",
        # Fluvius sub-areas (Flanders)
        "fluvius_antwerpen",
        "fluvius_halle_vilvoorde",
        "fluvius_imewo",
        "fluvius_intergem",
        "fluvius_iveka",
        "fluvius_limburg",
        "fluvius_west",
        "fluvius_zenne_dijle",
    }
    for cid in ("power_fix", "power_flex", "power_dynamic"):
        prefix = f"eneco/{cid}"
        try:
            # Eneco's PDF carries every region; any one is fine.
            snap = await eneco.fetch(session, cid, "flanders")
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
        _validate_energy(prefix, cid, snap.energy)
        # Pick one Fluvius row to bounds-check the digital meter parser.
        if "fluvius_antwerpen" in snap.dsos:
            antwerpen = snap.dsos["fluvius_antwerpen"]
            _expect(
                f"{prefix}: fluvius capacity tariff in [20, 200] EUR/kW/yr",
                antwerpen.capacity_eur_per_kw_year is not None
                and 20.0 <= antwerpen.capacity_eur_per_kw_year <= 200.0,
                detail=str(antwerpen),
            )


async def _check_cociter(
    session: aiohttp.ClientSession, cociter: types.ModuleType
) -> None:
    expected_dso_keys = {"aieg", "aiesh", "ores", "resa", "rew"}
    for cid in ("cociter_variable", "cociter_dynamic"):
        prefix = f"cociter/{cid}"
        try:
            snap = await cociter.fetch(session, cid, "wallonia")
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
        _validate_energy(prefix, cid, snap.energy)


async def _check_dats24(
    session: aiohttp.ClientSession, dats24: types.ModuleType
) -> None:
    expected = {
        "flanders": {
            "fluvius_antwerpen",
            "fluvius_halle_vilvoorde",
            "fluvius_imewo",
            "fluvius_intergem",
            "fluvius_iveka",
            "fluvius_limburg",
            "fluvius_west",
            "fluvius_zenne_dijle",
        },
        "wallonia": {"aieg", "aiesh", "ores", "resa", "rew"},
    }
    cid = "dats24_groen_variabel"
    for region in ("flanders", "wallonia"):
        prefix = f"dats24/{cid}/{region}"
        try:
            snap = await dats24.fetch(session, cid, region)
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
        _validate_energy(prefix, cid, snap.energy)


async def _check_ebem(session: aiohttp.ClientSession, ebem: types.ModuleType) -> None:
    # EBEM only sells residential electricity in Flanders. The 'elek' card
    # carries both Groen Variabel and Groen B@sic+ in one PDF; the
    # 'dynamic' card carries Groen Dyn@mic. Walk every contract — they
    # all hit the same listing-page resolver but parse different blocks.
    expected_dsos = {
        "fluvius_antwerpen",
        "fluvius_halle_vilvoorde",
        "fluvius_imewo",
        "fluvius_intergem",
        "fluvius_iveka",
        "fluvius_limburg",
        "fluvius_west",
        "fluvius_zenne_dijle",
    }
    for contract in ebem._CONTRACTS:
        cid = contract.contract_id
        prefix = f"ebem/{cid}/flanders"
        try:
            snap = await ebem.fetch(session, cid, "flanders")
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
        _expect(
            f"{prefix}: energy contribution > 0",
            snap.taxes.energy_contribution > 0,
            detail=str(snap.taxes),
        )
        _validate_energy(prefix, cid, snap.energy)


async def _check_ecofix(
    session: aiohttp.ClientSession, ecofix: types.ModuleType
) -> None:
    # Ecofix sells residential electricity in Flanders and Wallonia
    # (no Brussels offering). The same PDF carries both regions; the
    # parser narrows the snapshot down to the requested region. Walk
    # every (contract, region) pair to verify both code paths.
    expected_dsos = {
        "flanders": {
            "fluvius_antwerpen",
            "fluvius_halle_vilvoorde",
            "fluvius_imewo",
            "fluvius_intergem",
            "fluvius_iveka",
            "fluvius_limburg",
            "fluvius_west",
            "fluvius_zenne_dijle",
        },
        "wallonia": {"aieg", "aiesh", "ores", "resa", "rew"},
    }
    renewables_field = {
        "flanders": "flanders_renewables",
        "wallonia": "wallonia_renewables",
    }
    for contract in ecofix._CONTRACTS:
        cid = contract.contract_id
        for region_key in ("flanders", "wallonia"):
            prefix = f"ecofix/{cid}/{region_key}"
            try:
                snap = await ecofix.fetch(session, cid, region_key)
            except Exception as err:
                _record(f"{prefix}: fetch", False, f"{type(err).__name__}: {err}")
                continue
            _expect(f"{prefix}: publication label", bool(snap.publication_label))
            _expect(
                f"{prefix}: expected DSOs present",
                expected_dsos[region_key] <= set(snap.dsos),
                detail=f"missing: {sorted(expected_dsos[region_key] - set(snap.dsos))}",
            )
            _expect(
                f"{prefix}: regional renewables > 0",
                getattr(snap.taxes, renewables_field[region_key]) > 0,
                detail=str(snap.taxes),
            )
            _expect(
                f"{prefix}: federal excise > 0",
                snap.taxes.federal_excise > 0,
                detail=str(snap.taxes),
            )
            _expect(
                f"{prefix}: energy contribution > 0",
                snap.taxes.energy_contribution > 0,
                detail=str(snap.taxes),
            )
            _validate_energy(prefix, cid, snap.energy)


async def _check_ecopower(
    session: aiohttp.ClientSession, ecopower: types.ModuleType
) -> None:
    expected_dso_keys = {
        "fluvius_antwerpen",
        "fluvius_halle_vilvoorde",
        "fluvius_imewo",
        "fluvius_intergem",
        "fluvius_iveka",
        "fluvius_limburg",
        "fluvius_west",
        "fluvius_zenne_dijle",
    }
    cid = "ecopower_burgerstroom"
    prefix = f"ecopower/{cid}"
    try:
        snap = await ecopower.fetch(session, cid, "flanders")
    except Exception as err:
        _record(f"{prefix}: fetch", False, f"{type(err).__name__}: {err}")
        return
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
    _validate_energy(prefix, cid, snap.energy)
    if "fluvius_antwerpen" in snap.dsos:
        a = snap.dsos["fluvius_antwerpen"]
        _expect(
            f"{prefix}: fluvius capacity tariff in [20, 200] EUR/kW/yr",
            a.capacity_eur_per_kw_year is not None
            and 20.0 <= a.capacity_eur_per_kw_year <= 200.0,
            detail=str(a),
        )


async def _check_luminus(
    session: aiohttp.ClientSession, luminus: types.ModuleType
) -> None:
    # Luminus serves Flanders and Wallonia for every market product
    # (Brussels carries only the regulated Social tariff, which is
    # excluded from the registry). Walk every (contract, region) pair.
    expected_dsos = {
        "flanders": {
            "fluvius_antwerpen",
            "fluvius_halle_vilvoorde",
            "fluvius_imewo",
            "fluvius_intergem",
            "fluvius_iveka",
            "fluvius_limburg",
            "fluvius_west",
            "fluvius_zenne_dijle",
        },
        "wallonia": {"aieg", "aiesh", "ores", "resa", "rew"},
    }
    renewables_field = {
        "flanders": "flanders_renewables",
        "wallonia": "wallonia_renewables",
    }
    for contract in luminus._CONTRACTS:
        cid = contract.contract_id
        for region_key in ("flanders", "wallonia"):
            prefix = f"luminus/{cid}/{region_key}"
            try:
                snap = await luminus.fetch(session, cid, region_key)
            except Exception as err:
                _record(f"{prefix}: fetch", False, f"{type(err).__name__}: {err}")
                continue
            _expect(f"{prefix}: publication label", bool(snap.publication_label))
            _expect(
                f"{prefix}: expected DSOs present",
                expected_dsos[region_key] <= set(snap.dsos),
                detail=f"missing: {sorted(expected_dsos[region_key] - set(snap.dsos))}",
            )
            _expect(
                f"{prefix}: regional renewables > 0",
                getattr(snap.taxes, renewables_field[region_key]) > 0,
                detail=str(snap.taxes),
            )
            _expect(
                f"{prefix}: federal excise > 0",
                snap.taxes.federal_excise > 0,
                detail=str(snap.taxes),
            )
            _expect(
                f"{prefix}: energy contribution > 0",
                snap.taxes.energy_contribution > 0,
                detail=str(snap.taxes),
            )
            _validate_energy(prefix, cid, snap.energy)


async def _check_bolt(session: aiohttp.ClientSession, bolt: types.ModuleType) -> None:
    # Bolt's PDFs are nationwide (one file per contract, all 3 regions
    # in one document), so we walk every (contract, region) pair just to
    # verify the parsing path for each region works.
    expected_dsos = {
        "flanders": {
            "fluvius_antwerpen",
            "fluvius_halle_vilvoorde",
            "fluvius_imewo",
            "fluvius_intergem",
            "fluvius_iveka",
            "fluvius_limburg",
            "fluvius_west",
            "fluvius_zenne_dijle",
        },
        "wallonia": {"aieg", "aiesh", "ores", "resa", "rew"},
        "brussels": {"sibelga"},
    }
    renewables_field = {
        "flanders": "flanders_renewables",
        "wallonia": "wallonia_renewables",
        "brussels": "brussels_renewables",
    }
    for contract in bolt._CONTRACTS:
        cid = contract.contract_id
        # Bolt's PDF covers every region, so fetch once per contract
        # and re-parse for each region from the cached text. Without
        # this the daily check downloads the same 5 MB PDF three
        # times (once per region) and trips the byte budget.
        try:
            url, text = await bolt._fetch_pdf_text(session, contract)
        except Exception as err:
            for region_key in ("flanders", "wallonia", "brussels"):
                _record(
                    f"bolt/{cid}/{region_key}: fetch",
                    False,
                    f"{type(err).__name__}: {err}",
                )
            continue
        for region_key in ("flanders", "wallonia", "brussels"):
            prefix = f"bolt/{cid}/{region_key}"
            try:
                snap = bolt.parse_snapshot(cid, text, region_key, url)
            except Exception as err:
                _record(f"{prefix}: parse", False, f"{type(err).__name__}: {err}")
                continue
            _expect(
                f"{prefix}: expected DSOs present",
                expected_dsos[region_key] <= set(snap.dsos),
                detail=f"missing: {sorted(expected_dsos[region_key] - set(snap.dsos))}",
            )
            _expect(
                f"{prefix}: regional renewables > 0",
                getattr(snap.taxes, renewables_field[region_key]) > 0,
                detail=str(snap.taxes),
            )
            _expect(
                f"{prefix}: federal excise > 0",
                snap.taxes.federal_excise > 0,
                detail=str(snap.taxes),
            )
            _validate_energy(prefix, cid, snap.energy)


async def _check_totalenergies(
    session: aiohttp.ClientSession, totalenergies: types.ModuleType
) -> None:
    # TotalEnergies serves all 3 regions for every product. Walk every
    # (contract, region) pair against the real /latest/ PDFs.
    expected_dsos = {
        "flanders": {
            "fluvius_antwerpen",
            "fluvius_halle_vilvoorde",
            "fluvius_imewo",
            "fluvius_intergem",
            "fluvius_iveka",
            "fluvius_limburg",
            "fluvius_west",
            "fluvius_zenne_dijle",
        },
        "wallonia": {"aieg", "aiesh", "ores", "resa", "rew"},
        "brussels": {"sibelga"},
    }
    renewables_field = {
        "flanders": "flanders_renewables",
        "wallonia": "wallonia_renewables",
        "brussels": "brussels_renewables",
    }
    for contract in totalenergies._CONTRACTS:
        cid = contract.contract_id
        for region_key in ("flanders", "wallonia", "brussels"):
            if region_key not in contract.regions:
                continue
            prefix = f"totalenergies/{cid}/{region_key}"
            try:
                snap = await totalenergies.fetch(session, cid, region_key)
            except Exception as err:
                _record(f"{prefix}: fetch", False, f"{type(err).__name__}: {err}")
                continue
            _expect(
                f"{prefix}: expected DSOs present",
                expected_dsos[region_key] <= set(snap.dsos),
                detail=f"missing: {sorted(expected_dsos[region_key] - set(snap.dsos))}",
            )
            _expect(
                f"{prefix}: regional renewables > 0",
                getattr(snap.taxes, renewables_field[region_key]) > 0,
                detail=str(snap.taxes),
            )
            _expect(
                f"{prefix}: federal excise > 0",
                snap.taxes.federal_excise > 0,
                detail=str(snap.taxes),
            )
            _validate_energy(prefix, cid, snap.energy)


async def _check_mega(session: aiohttp.ClientSession, mega: types.ModuleType) -> None:
    # Mega serves all 3 regions for every contract and resolves the URL
    # by scraping mega.be/fr/cartes-tarifaires; walk every (contract,
    # region) pair to verify both the listing scrape and the PDF parse.
    expected_dsos = {
        "flanders": {
            "fluvius_antwerpen",
            "fluvius_halle_vilvoorde",
            "fluvius_imewo",
            "fluvius_intergem",
            "fluvius_iveka",
            "fluvius_limburg",
            "fluvius_west",
            "fluvius_zenne_dijle",
        },
        "wallonia": {"aieg", "aiesh", "ores", "resa", "rew"},
        "brussels": {"sibelga"},
    }
    renewables_field = {
        "flanders": "flanders_renewables",
        "wallonia": "wallonia_renewables",
        "brussels": "brussels_renewables",
    }
    for contract in mega._CONTRACTS:
        cid = contract.contract_id
        for region_key in ("flanders", "wallonia", "brussels"):
            prefix = f"mega/{cid}/{region_key}"
            try:
                snap = await mega.fetch(session, cid, region_key)
            except Exception as err:
                _record(f"{prefix}: fetch", False, f"{type(err).__name__}: {err}")
                continue
            _expect(
                f"{prefix}: expected DSOs present",
                expected_dsos[region_key] <= set(snap.dsos),
                detail=f"missing: {sorted(expected_dsos[region_key] - set(snap.dsos))}",
            )
            _expect(
                f"{prefix}: regional renewables > 0",
                getattr(snap.taxes, renewables_field[region_key]) > 0,
                detail=str(snap.taxes),
            )
            _expect(
                f"{prefix}: federal excise > 0",
                snap.taxes.federal_excise > 0,
                detail=str(snap.taxes),
            )
            _validate_energy(prefix, cid, snap.energy)


async def _check_octaplus(
    session: aiohttp.ClientSession, octaplus: types.ModuleType
) -> None:
    # OCTA+ only sells residential electricity in Flanders and Wallonia
    # (Brussels offerings are professional-only). One PDF per (contract,
    # region) at https://files.octaplus.be/tariffs/E_OCTA_<SLUG>_RE_<VL|WL>_FR.pdf
    expected_dsos = {
        "flanders": {
            "fluvius_antwerpen",
            "fluvius_halle_vilvoorde",
            "fluvius_imewo",
            "fluvius_intergem",
            "fluvius_iveka",
            "fluvius_limburg",
            "fluvius_west",
            "fluvius_zenne_dijle",
        },
        "wallonia": {"aieg", "aiesh", "ores", "resa", "rew"},
    }
    renewables_field = {
        "flanders": "flanders_renewables",
        "wallonia": "wallonia_renewables",
    }
    for contract in octaplus._CONTRACTS:
        cid = contract.contract_id
        for region_key in ("flanders", "wallonia"):
            prefix = f"octaplus/{cid}/{region_key}"
            try:
                snap = await octaplus.fetch(session, cid, region_key)
            except Exception as err:
                _record(f"{prefix}: fetch", False, f"{type(err).__name__}: {err}")
                continue
            _expect(f"{prefix}: publication label", bool(snap.publication_label))
            _expect(
                f"{prefix}: expected DSOs present",
                expected_dsos[region_key] <= set(snap.dsos),
                detail=f"missing: {sorted(expected_dsos[region_key] - set(snap.dsos))}",
            )
            _expect(
                f"{prefix}: regional renewables > 0",
                getattr(snap.taxes, renewables_field[region_key]) > 0,
                detail=str(snap.taxes),
            )
            _expect(
                f"{prefix}: federal excise > 0",
                snap.taxes.federal_excise > 0,
                detail=str(snap.taxes),
            )
            _expect(
                f"{prefix}: energy contribution > 0",
                snap.taxes.energy_contribution > 0,
                detail=str(snap.taxes),
            )
            _validate_energy(prefix, cid, snap.energy)


async def _check_engie(session: aiohttp.ClientSession, engie: types.ModuleType) -> None:
    # Engie now fetches one PDF per (contract, region) on demand, so the
    # check walks every supported region per contract instead of asking
    # for a single merged snapshot. If a region fetch ever stops working
    # the report flags the specific (contract, region) pair.
    expected_dsos = {
        "flanders": {
            "fluvius_antwerpen",
            "fluvius_halle_vilvoorde",
            "fluvius_imewo",
            "fluvius_intergem",
            "fluvius_iveka",
            "fluvius_limburg",
            "fluvius_west",
            "fluvius_zenne_dijle",
        },
        "wallonia": {"aieg", "aiesh", "ores", "resa", "rew"},
        "brussels": {"sibelga"},
    }
    region_letter = {"flanders": "V", "wallonia": "W", "brussels": "B"}
    renewables_field = {
        "flanders": "flanders_renewables",
        "wallonia": "wallonia_renewables",
        "brussels": "brussels_renewables",
    }
    for contract in engie._CONTRACTS:
        cid = contract.contract_id
        for region_key, letter in region_letter.items():
            if letter not in contract.months_per_region:
                continue
            prefix = f"engie/{cid}/{region_key}"
            try:
                snap = await engie.fetch(session, cid, region_key)
            except Exception as err:
                _record(f"{prefix}: fetch", False, f"{type(err).__name__}: {err}")
                continue
            _expect(f"{prefix}: publication label", bool(snap.publication_label))
            _expect(
                f"{prefix}: expected DSOs present",
                expected_dsos[region_key] <= set(snap.dsos),
                detail=f"missing: {sorted(expected_dsos[region_key] - set(snap.dsos))}",
            )
            _expect(
                f"{prefix}: regional renewables > 0",
                getattr(snap.taxes, renewables_field[region_key]) > 0,
                detail=str(snap.taxes),
            )
            _expect(
                f"{prefix}: federal excise > 0",
                snap.taxes.federal_excise > 0,
                detail=str(snap.taxes),
            )
            _validate_energy(prefix, cid, snap.energy)


async def _check_catalogs(
    session: aiohttp.ClientSession, modules: dict[str, types.ModuleType]
) -> None:
    """Run each supplier's ``discover()`` and surface any new product ids.

    ``known_ids_for(module)`` extracts the registry's identifier set in
    the same shape ``discover()`` returns. discover() that returns an
    empty set is treated as "discovery not implemented" and skipped
    (Engie, Luminus today).
    """
    known: dict[str, set[str]] = {
        "mega": {c.product_name for c in modules["mega"]._CONTRACTS},
        "bolt": {f"{c.folder}/{c.slug}" for c in modules["bolt"]._CONTRACTS},
        "engie": {c.family for c in modules["engie"]._CONTRACTS},
        "luminus": {c.slug for c in modules["luminus"]._CONTRACTS},
        "eneco": set(modules["eneco"]._CONTRACT_SLUGS),
        "totalenergies": {c.slug for c in modules["totalenergies"]._CONTRACTS},
        "octaplus": {c.slug for c in modules["octaplus"]._CONTRACTS},
        "cociter": {"cociter_variable", "cociter_dynamic"},
        "ebem": {c.contract_id for c in modules["ebem"]._CONTRACTS},
        "ecofix": {c.contract_id for c in modules["ecofix"]._CONTRACTS},
        "ecopower": {"ecopower_burgerstroom"},
        "dats24": {"dats24_groen_variabel"},
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
            # No discovery surface (Engie / Luminus) or transient
            # failure — no signal either way.
            continue
        new_ids = sorted(discovered - known.get(name, set()))
        _record(
            f"{name}/catalog: no new products at supplier",
            not new_ids,
            detail=", ".join(new_ids) if new_ids else "",
            kind="catalog",
        )


def _validate_energy(prefix: str, contract_id: str, energy: object) -> None:
    kind = type(energy).__name__
    if kind == "FixedRates":
        rate = getattr(energy, "single", None)
        _expect(
            f"{prefix}: fixed rate in [0.05, 0.50] EUR/kWh",
            rate is not None and 0.05 <= rate <= 0.50,
            detail=f"single={rate}",
        )
    elif kind == "VariableRates":
        current = getattr(energy, "current", None)
        _expect(
            f"{prefix}: variable rate in [0.05, 0.50] EUR/kWh",
            current is not None and 0.05 <= current <= 0.50,
            detail=f"current={current}",
        )
    elif kind == "DynamicRates":
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
    elif kind == "TimeOfUseRates":
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
    else:
        _record(
            f"{prefix}: energy type",
            False,
            f"unknown energy class {kind}",
        )


def _render_metrics(metrics: dict[str, dict[str, float]]) -> str:
    """Per-supplier wallclock + bytes-received block for the report.

    Empty when nothing was traced (e.g. the catalog-only report).
    Emits a leading blank line so it slots cleanly between sections
    without collapsing into an adjacent table.
    """
    if not metrics:
        return ""
    rows = ["", "## Per-supplier latency / size", ""]
    rows.append("| Supplier | Fetches | Wallclock (s) | Bytes received |")
    rows.append("| --- | ---: | ---: | ---: |")
    for supplier, m in sorted(metrics.items()):
        bytes_str = f"{int(m['bytes']):,}" if m["bytes"] else "-"
        rows.append(
            f"| `{supplier}` | {int(m['fetches'])} | "
            f"{m['elapsed_s']:.2f} | {bytes_str} |"
        )
    return "\n".join(rows) + "\n"


def _render_report(
    checks: Iterable[Check], metrics: dict[str, dict[str, float]] | None = None
) -> str:
    rows: list[str] = []
    pass_count = sum(1 for c in checks if c.ok)
    fail_count = sum(1 for c in checks if not c.ok)
    rows.append(f"# Live extractor check — {pass_count} pass, {fail_count} fail")
    rows.append("")
    if fail_count:
        rows.append("## Failures")
        rows.append("")
        rows.append("| Check | Detail |")
        rows.append("| --- | --- |")
        for c in checks:
            if not c.ok:
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


async def _run() -> int:
    (
        eneco,
        cociter,
        dats24,
        ebem,
        ecofix,
        ecopower,
        engie,
        luminus,
        mega,
        totalenergies,
        bolt,
        octaplus,
    ) = _load_providers()
    modules = {
        "eneco": eneco,
        "cociter": cociter,
        "dats24": dats24,
        "ebem": ebem,
        "ecofix": ecofix,
        "ecopower": ecopower,
        "engie": engie,
        "luminus": luminus,
        "mega": mega,
        "totalenergies": totalenergies,
        "bolt": bolt,
        "octaplus": octaplus,
    }
    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(
        timeout=timeout, trace_configs=[_trace_config()]
    ) as session:
        with _attributed("eneco"):
            await _check_eneco(session, eneco)
        with _attributed("cociter"):
            await _check_cociter(session, cociter)
        with _attributed("dats24"):
            await _check_dats24(session, dats24)
        with _attributed("ebem"):
            await _check_ebem(session, ebem)
        with _attributed("ecofix"):
            await _check_ecofix(session, ecofix)
        with _attributed("ecopower"):
            await _check_ecopower(session, ecopower)
        with _attributed("engie"):
            await _check_engie(session, engie)
        with _attributed("luminus"):
            await _check_luminus(session, luminus)
        with _attributed("mega"):
            await _check_mega(session, mega)
        with _attributed("totalenergies"):
            await _check_totalenergies(session, totalenergies)
        with _attributed("bolt"):
            await _check_bolt(session, bolt)
        with _attributed("octaplus"):
            await _check_octaplus(session, octaplus)
        # Catalog probes fan out across suppliers; attribute them
        # to a synthetic bucket so they don't double-count against
        # any one supplier's per-card timing.
        with _attributed("_catalog"):
            await _check_catalogs(session, modules)

    extractor_checks = [c for c in CHECKS if c.kind == "extractor"]
    catalog_checks = [c for c in CHECKS if c.kind == "catalog"]
    # Stdout = extractor report (existing workflow consumes this).
    # Metrics piggyback on the extractor report so silent slowdowns and
    # PDF-size jumps surface daily without a separate pipeline.
    print(_render_report(extractor_checks, METRICS))
    # Side-channel: catalog report goes to a known file the workflow
    # picks up to open / update its own issue, separate from the
    # extractor-broken issue so the two failure modes don't conflate.
    Path("catalog_report.md").write_text(_render_report(catalog_checks))
    drift_warnings = _drift_warnings(METRICS)
    Path("drift_report.md").write_text(_render_drift(drift_warnings))
    extractor_failed = any(not c.ok for c in extractor_checks)
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


# Static drift thresholds. The default (5 MB / 60 s) catches a fresh
# regression at any supplier; per-supplier overrides cover known-large
# catalogs whose total over the full check is honestly above the
# default but stable. The override is the budget we expect plus ~25%
# headroom; cross it and something genuinely changed.
LATENCY_WARN_THRESHOLD_S = 60.0
BYTES_WARN_THRESHOLD = 5_000_000

# Per-supplier byte budgets (override the global). Picked off the
# steady-state metrics table after the bolt 3x-refetch fix:
#   * bolt: ~32 MB (6 contracts x ~5 MB PDF, parsed once for all 3
#     regions). Allow 50 MB for headroom and a possible new product.
#   * totalenergies: ~11 MB (7 contracts x 3 regions, ~0.45 MB each).
#     Allow 15 MB.
#   * engie: ~5.4 MB (sitemap discovery + ~24 region PDFs). Allow 8 MB
#     so we don't fire on a slow day.
_BYTES_BUDGET_OVERRIDES: dict[str, int] = {
    "bolt": 50_000_000,
    "totalenergies": 15_000_000,
    "engie": 8_000_000,
}


def _bytes_budget(supplier: str) -> int:
    return _BYTES_BUDGET_OVERRIDES.get(supplier, BYTES_WARN_THRESHOLD)


def _drift_warnings(metrics: dict[str, dict[str, float]]) -> list[str]:
    """Static-threshold drift signals: latency or byte budgets blown."""
    warnings: list[str] = []
    for supplier, m in sorted(metrics.items()):
        if m["elapsed_s"] > LATENCY_WARN_THRESHOLD_S:
            warnings.append(
                f"`{supplier}` wallclock {m['elapsed_s']:.1f}s "
                f"exceeds {LATENCY_WARN_THRESHOLD_S:.0f}s budget"
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
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())

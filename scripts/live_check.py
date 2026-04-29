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
import traceback
import types
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "custom_components" / "be_electricity_prices"


def _load_providers() -> tuple[types.ModuleType, types.ModuleType]:
    """Load the providers package without dragging Home Assistant into scope."""
    parent = types.ModuleType("be_pkg")
    parent.__path__ = [str(PKG)]  # type: ignore[attr-defined]
    sys.modules["be_pkg"] = parent
    prov = types.ModuleType("be_pkg.providers")
    prov.__path__ = [str(PKG / "providers")]  # type: ignore[attr-defined]
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
    engie = _load("be_pkg.providers.engie", PKG / "providers" / "engie.py")
    return eneco, cociter, engie


@dataclass
class Check:
    label: str
    ok: bool
    detail: str = ""


CHECKS: list[Check] = []


def _record(label: str, ok: bool, detail: str = "") -> None:
    CHECKS.append(Check(label=label, ok=ok, detail=detail))


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
            snap = await eneco.fetch(session, cid)
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
            snap = await cociter.fetch(session, cid)
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


async def _check_engie(session: aiohttp.ClientSession, engie: types.ModuleType) -> None:
    # Three contracts span every parsing path: a fixed rate, an indexed
    # variable rate and a hourly dynamic formula. The eight other Engie
    # products share these parsers, so passing the trio is a strong
    # signal that nothing has shifted on the supplier side.
    expected_flanders = {
        "fluvius_antwerpen",
        "fluvius_halle_vilvoorde",
        "fluvius_imewo",
        "fluvius_intergem",
        "fluvius_iveka",
        "fluvius_limburg",
        "fluvius_west",
        "fluvius_zenne_dijle",
    }
    expected_wallonia = {"aieg", "aiesh", "ores", "resa", "rew"}
    expected_brussels = {"sibelga"}
    for cid in ("engie_easy_fixed", "engie_easy_variable", "engie_dynamic"):
        prefix = f"engie/{cid}"
        try:
            snap = await engie.fetch(session, cid)
        except Exception as err:
            _record(f"{prefix}: fetch", False, f"{type(err).__name__}: {err}")
            continue
        _expect(f"{prefix}: publication label", bool(snap.publication_label))
        _expect(
            f"{prefix}: all three regions present",
            expected_flanders <= set(snap.dsos)
            and expected_wallonia <= set(snap.dsos)
            and expected_brussels <= set(snap.dsos),
            detail=f"got DSOs: {sorted(snap.dsos)}",
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
        _expect(
            f"{prefix}: brussels renewables > 0",
            snap.taxes.brussels_renewables > 0,
            detail=str(snap.taxes),
        )
        _validate_energy(prefix, cid, snap.energy)


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
    else:
        _record(
            f"{prefix}: energy type",
            False,
            f"unknown energy class {kind}",
        )


def _render_report(checks: Iterable[Check]) -> str:
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
    return "\n".join(rows) + "\n"


async def _run() -> int:
    eneco, cociter, engie = _load_providers()
    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        await _check_eneco(session, eneco)
        await _check_cociter(session, cociter)
        await _check_engie(session, engie)
    print(_render_report(CHECKS))
    return 1 if any(not c.ok for c in CHECKS) else 0


def main() -> int:
    try:
        return asyncio.run(_run())
    except Exception:
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())

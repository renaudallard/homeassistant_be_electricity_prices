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


def _load_providers() -> tuple[
    types.ModuleType,
    types.ModuleType,
    types.ModuleType,
    types.ModuleType,
    types.ModuleType,
    types.ModuleType,
    types.ModuleType,
    types.ModuleType,
]:
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
    engie = _load("be_pkg.providers.engie", PKG / "providers" / "engie.py")
    luminus = _load("be_pkg.providers.luminus", PKG / "providers" / "luminus.py")
    mega = _load("be_pkg.providers.mega", PKG / "providers" / "mega.py")
    totalenergies = _load(
        "be_pkg.providers.totalenergies", PKG / "providers" / "totalenergies.py"
    )
    bolt = _load("be_pkg.providers.bolt", PKG / "providers" / "bolt.py")
    octaplus = _load("be_pkg.providers.octaplus", PKG / "providers" / "octaplus.py")
    return eneco, cociter, engie, luminus, mega, totalenergies, bolt, octaplus


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
        for region_key in ("flanders", "wallonia", "brussels"):
            prefix = f"bolt/{cid}/{region_key}"
            try:
                snap = await bolt.fetch(session, cid, region_key)
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
        "eneco": set(modules["eneco"]._CONTRACT_URLS),
        "totalenergies": {c.slug for c in modules["totalenergies"]._CONTRACTS},
        "octaplus": {c.slug for c in modules["octaplus"]._CONTRACTS},
        "cociter": {"cociter_variable", "cociter_dynamic"},
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
    eneco, cociter, engie, luminus, mega, totalenergies, bolt, octaplus = (
        _load_providers()
    )
    modules = {
        "eneco": eneco,
        "cociter": cociter,
        "engie": engie,
        "luminus": luminus,
        "mega": mega,
        "totalenergies": totalenergies,
        "bolt": bolt,
        "octaplus": octaplus,
    }
    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        await _check_eneco(session, eneco)
        await _check_cociter(session, cociter)
        await _check_engie(session, engie)
        await _check_luminus(session, luminus)
        await _check_mega(session, mega)
        await _check_totalenergies(session, totalenergies)
        await _check_bolt(session, bolt)
        await _check_octaplus(session, octaplus)
        await _check_catalogs(session, modules)

    extractor_checks = [c for c in CHECKS if c.kind == "extractor"]
    catalog_checks = [c for c in CHECKS if c.kind == "catalog"]
    # Stdout = extractor report (existing workflow consumes this).
    print(_render_report(extractor_checks))
    # Side-channel: catalog report goes to a known file the workflow
    # picks up to open / update its own issue, separate from the
    # extractor-broken issue so the two failure modes don't conflate.
    Path("catalog_report.md").write_text(_render_report(catalog_checks))
    extractor_failed = any(not c.ok for c in extractor_checks)
    catalog_failed = any(not c.ok for c in catalog_checks)
    # Distinct exit codes so the workflow knows which issue to open:
    #   0 = all green
    #   1 = extractor failure (existing behaviour)
    #   2 = catalog-only (extractor green, but new products at suppliers)
    #   3 = both
    return (1 if extractor_failed else 0) | (2 if catalog_failed else 0)


def main() -> int:
    try:
        return asyncio.run(_run())
    except Exception:
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())

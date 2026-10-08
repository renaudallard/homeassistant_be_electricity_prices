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

"""Shared test helpers."""

from __future__ import annotations

import ast
import hashlib
import os
from collections.abc import AsyncIterator
from datetime import date
from functools import cache
from importlib.metadata import version
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.be_electricity_prices.const import (
    DOMAIN,
    WELCOME_CREDIT_PRO_RATA,
)
from custom_components.be_electricity_prices.providers._pdf import (
    extract_pdf_text,
    extract_pdf_text_aligned,
    extract_pdf_text_layout,
)
from custom_components.be_electricity_prices.providers._rates import (
    EnergyRates,
    FixedRates,
    InjectionRates,
)
from custom_components.be_electricity_prices.providers.base import (
    DsoOverlay,
    SupplierExtractor,
    SupplierSnapshot,
    TaxOverlay,
)

FIXTURES = Path(__file__).parent / "fixtures"
PACKAGE = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / ("be_electricity_prices")
)


def _text_cache_dir() -> Path:
    """Where ``fixture_text`` keeps what it extracted, across runs.

    One directory per extractor: the digest covers ``providers/_pdf.py`` and
    the pypdf and pdfplumber versions, so a change to either reads every card
    afresh instead of serving text the current code would not produce. Under
    ``tmp/`` by default, which git ignores; ``BE_FIXTURE_TEXT_CACHE`` moves
    it, which the gate does so its throwaway worktree reuses the main one's.
    """
    extractor = hashlib.sha256((PACKAGE / "providers" / "_pdf.py").read_bytes())
    for dist in ("pypdf", "pdfplumber"):
        extractor.update(f"{dist} {version(dist)}".encode())
    base = os.environ.get("BE_FIXTURE_TEXT_CACHE") or (
        Path(__file__).resolve().parent.parent / "tmp" / "fixture_text"
    )
    return Path(base).resolve() / extractor.hexdigest()[:16]


_TEXT_CACHE = _text_cache_dir()


def compare_page_sources() -> dict[str, str]:
    """The source of every ``compare_*.py`` module, keyed by file name.

    The compare page's source guards read the whole family through this
    rather than a list of modules: a list goes stale the moment a split moves
    code into a new file, which the 0.27.5 split did with compare_inputs.py,
    and a guard that stops reading a file keeps passing while it covers less.
    """
    return {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(PACKAGE.glob("compare_*.py"))
    }


def compare_page_calls(function: str) -> list[tuple[str, ast.Call]]:
    """Every call to ``function`` in the compare modules, with its file name.

    Read off the syntax tree, so the function's own definition and a docstring
    that names it are not taken for calls, while a call inside an f-string is.
    A call through a name the module imported it under, or wrapped in
    ``partial``, is a call too: matching the bare name alone let either one
    through a guard written to see every call. Any other use of the function,
    held in a variable or handed to another as an argument, is a call this
    cannot see, so it fails every guard built on it rather than pass one.
    """

    def _named(node: ast.expr) -> str | None:
        return getattr(node, "id", getattr(node, "attr", None))

    calls: list[tuple[str, ast.Call]] = []
    unfollowed: list[str] = []
    for name, source in compare_page_sources().items():
        tree = ast.parse(source)
        names = {function} | {
            alias.asname
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
            if alias.name == function and alias.asname
        }
        followed: set[int] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if _named(node.func) in names:
                calls.append((name, node))
                followed.add(id(node.func))
            elif (
                _named(node.func) == "partial"
                and node.args
                and _named(node.args[0]) in names
            ):
                calls.append((name, node))
                followed.add(id(node.args[0]))
        unfollowed += [
            f"{name} line {node.lineno}"
            for node in ast.walk(tree)
            if isinstance(node, ast.Name | ast.Attribute)
            and isinstance(node.ctx, ast.Load)
            and _named(node) in names
            and id(node) not in followed
        ]
    assert not unfollowed, f"{function} used other than by a call: {unfollowed}"
    return calls


@cache
def fixture_text(name: str, *, layout: bool = False, aligned: bool = False) -> str:
    """Read ``tests/fixtures/<name>`` and run it through the PDF extractor.

    ``layout=True`` routes through ``extract_pdf_text_layout`` for
    suppliers whose tariff cards rely on column positions (Bolt,
    DATS 24, Ecopower, TotalEnergies, Trevion), and ``aligned=True`` through
    ``extract_pdf_text_aligned`` with OCTA+'s word joining. Default is
    ``extract_pdf_text`` (pypdf), which is fine for the rest.

    The text is also kept on disk (``_text_cache_dir``), keyed on the PDF's
    own digest and the mode, because reading the cards is most of what the
    suite spends: every card once, in both modes, is about 2900 CPU seconds
    on a Raspberry Pi 5, a single Bolt card over a minute of layout. A card
    that fails to read raises as before and leaves nothing behind. Stored as
    bytes, so the text comes back exactly, carriage returns included.

    Also cached in memory for the life of the process, so a worker reads a
    file off the disk once. Tests must not mutate the returned string. A
    fixture rewritten mid-session (``pytest-watch``, ``--looponfail``) keeps
    its old text until the process restarts or ``fixture_text.cache_clear()``
    runs; the copy on disk is keyed on the new bytes and needs nothing.
    """
    if layout and aligned:
        raise ValueError("pick one of layout and aligned")
    payload = (FIXTURES / name).read_bytes()
    mode = "aligned" if aligned else "layout" if layout else "plain"
    kept = _TEXT_CACHE / f"{hashlib.sha256(payload).hexdigest()}.{mode}.txt"
    try:
        return kept.read_bytes().decode("utf-8", "surrogatepass")
    except FileNotFoundError:
        pass
    if aligned:
        text = extract_pdf_text_aligned(payload, x_join_threshold=1.0)
    elif layout:
        text = extract_pdf_text_layout(payload)
    else:
        text = extract_pdf_text(payload)
    # Written aside and renamed into place: the xdist workers read and write
    # the same directory, and a reader must never see half a file.
    kept.parent.mkdir(parents=True, exist_ok=True)
    partial = kept.with_name(f"{kept.name}.{os.getpid()}")
    partial.write_bytes(text.encode("utf-8", "surrogatepass"))
    os.replace(partial, kept)
    return text


def make_snapshot(
    *,
    supplier: str = "test",
    contract: str = "test",
    energy: EnergyRates | None = None,
    dsos: dict[str, DsoOverlay] | None = None,
    taxes: TaxOverlay | None = None,
    source_url: str = "test://",
    publication_label: str = "",
    injection: InjectionRates | None = None,
    valid_until: date | None = None,
    supplier_prosumer_eur_per_kva_year: float | None = None,
    welcome_credit_eur: float | None = None,
    welcome_credit_kind: str = WELCOME_CREDIT_PRO_RATA,
    welcome_credit_eur_per_kwh: float | None = None,
    welcome_credit_cap_eur: float | None = None,
    welcome_credit_direct_debit_eur: float | None = None,
    welcome_credit_requires_direct_debit: bool = False,
    welcome_credit_after_months: int = 12,
    welcome_credit_pct_of_energy: float | None = None,
    welcome_credit_kwh: float | None = None,
    welcome_credit_excludes_night_meter: bool = False,
    welcome_credit_injection_eur_per_kwh: float | None = None,
) -> SupplierSnapshot:
    """SupplierSnapshot with sensible defaults for tests.

    Defaults are a canonical Wallonia fixed-rate snapshot under ORES;
    override any field a test cares about. ``dsos={}`` is preserved (the
    factory only fills in defaults when the kwarg is ``None``).
    """
    if energy is None:
        energy = FixedRates(single=0.18)
    if dsos is None:
        dsos = {"ores": DsoOverlay(distribution_single=0.10, transport=0.0145)}
    if taxes is None:
        taxes = TaxOverlay(federal_excise=0.05, energy_contribution=0.002)
    return SupplierSnapshot(
        supplier=supplier,
        contract=contract,
        energy=energy,
        dsos=dsos,
        taxes=taxes,
        source_url=source_url,
        publication_label=publication_label,
        injection=injection,
        valid_until=valid_until,
        supplier_prosumer_eur_per_kva_year=supplier_prosumer_eur_per_kva_year,
        welcome_credit_eur=welcome_credit_eur,
        welcome_credit_kind=welcome_credit_kind,
        welcome_credit_eur_per_kwh=welcome_credit_eur_per_kwh,
        welcome_credit_cap_eur=welcome_credit_cap_eur,
        welcome_credit_direct_debit_eur=welcome_credit_direct_debit_eur,
        welcome_credit_requires_direct_debit=welcome_credit_requires_direct_debit,
        welcome_credit_after_months=welcome_credit_after_months,
        welcome_credit_pct_of_energy=welcome_credit_pct_of_energy,
        welcome_credit_kwh=welcome_credit_kwh,
        welcome_credit_excludes_night_meter=welcome_credit_excludes_night_meter,
        welcome_credit_injection_eur_per_kwh=welcome_credit_injection_eur_per_kwh,
    )


def make_entry(
    *,
    supplier: str = "eneco",
    contract: str = "power_fix",
    region: str = "wallonia",
    dso: str = "ores",
    meter: str = "mono",
    title: str = "Eneco - Eneco Zon & Wind Vast (Wallonia)",
    options: dict[str, object] | None = None,
    **extra: object,
) -> MockConfigEntry:
    """MockConfigEntry with the canonical Eneco / Wallonia / mono base.

    Override any of the five base fields; pass extra entry-data keys as
    keyword arguments (e.g. ``solar_regime="none"``) and ``options`` for
    the entry options mapping.
    """
    data: dict[str, object] = {
        "supplier": supplier,
        "contract": contract,
        "region": region,
        "dso": dso,
        "meter": meter,
        **extra,
    }
    if options is None:
        return MockConfigEntry(domain=DOMAIN, data=data, title=title)
    return MockConfigEntry(domain=DOMAIN, data=data, options=options, title=title)


def make_stub_extractor(
    *, extractor_id: str = "test", label: str = "Test", fetch: Any = None
) -> SupplierExtractor:
    """A no-op SupplierExtractor for tests that only need a registry entry.

    ``fetch`` defaults to a fresh ``AsyncMock``; pass a coroutine function
    to control what fetch does (e.g. raise).
    """
    return SupplierExtractor(
        id=extractor_id,
        label=label,
        contracts=(),
        fetch=fetch or AsyncMock(),
    )


__all__ = [
    "FIXTURES",
    "FakeBody",
    "fixture_text",
    "make_entry",
    "make_snapshot",
    "make_stub_extractor",
]


class FakeBody:
    """What a fetch helper streams a response body from, over bytes in hand."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    async def iter_chunked(self, _size: int) -> AsyncIterator[bytes]:
        yield self._body


def make_text_session(body: str) -> Any:
    """A minimal stand-in for an aiohttp session that serves ``body``.

    Three provider test modules pasted the same _Resp / _Session pair
    (md5-identical) to exercise a listing fetch. Do NOT fold in
    test_discover.py's stub: that one is a deliberate superset with a
    configurable status, a headers dict and a head() method.
    """

    class _Resp:
        status = 200
        content_length = None
        charset = None
        history = ()
        content = FakeBody(body.encode("utf-8"))

        async def __aenter__(self) -> _Resp:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

    class _Session:
        def get(self, *_args: Any, **_kwargs: Any) -> _Resp:
            return _Resp()

    return _Session()

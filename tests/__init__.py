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

from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.be_electricity_prices.const import DOMAIN
from custom_components.be_electricity_prices.providers._pdf import (
    extract_pdf_text,
    extract_pdf_text_layout,
)
from custom_components.be_electricity_prices.providers.base import (
    DsoOverlay,
    EnergyRates,
    FixedRates,
    InjectionRates,
    SupplierExtractor,
    SupplierSnapshot,
    TaxOverlay,
)

FIXTURES = Path(__file__).parent / "fixtures"


@lru_cache(maxsize=None)
def fixture_text(name: str, *, layout: bool = False) -> str:
    """Read ``tests/fixtures/<name>`` and run it through the PDF extractor.

    ``layout=True`` routes through ``extract_pdf_text_layout`` for
    suppliers whose tariff cards rely on column positions (Bolt,
    DATS 24, Ecopower, TotalEnergies). Default is ``extract_pdf_text``
    (pypdf), which is fine for the rest.

    Cached for the lifetime of the Python process: PDF extraction is
    the dominant cost in the test suite (~10s per fixture), and every
    call with the same arguments returns the same string. The cache
    cuts the full suite from ~190s to ~30s.

    Constraints, because the cache is process-scoped, not session-
    scoped:
      * tests must not mutate the returned string (they don't today),
      * a developer rewriting a fixture file mid-session (e.g. under
        ``pytest-watch`` / ``--looponfail``) keeps seeing the old
        text until the Python process restarts. Call
        ``fixture_text.cache_clear()`` when iterating on a fixture, or
        re-run pytest from scratch.
    """
    payload = (FIXTURES / name).read_bytes()
    if layout:
        return extract_pdf_text_layout(payload)
    return extract_pdf_text(payload)


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
    "fixture_text",
    "make_entry",
    "make_snapshot",
    "make_stub_extractor",
]


def make_text_session(body: str) -> Any:
    """A minimal stand-in for an aiohttp session that serves ``body``.

    Three provider test modules pasted the same _Resp / _Session pair
    (md5-identical) to exercise a listing fetch. Do NOT fold in
    test_discover.py's stub: that one is a deliberate superset with a
    configurable status, a headers dict and a head() method.
    """

    class _Resp:
        status = 200

        async def text(self) -> str:
            return body

        async def __aenter__(self) -> "_Resp":
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

    class _Session:
        def get(self, *_args: Any, **_kwargs: Any) -> _Resp:
            return _Resp()

    return _Session()

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

"""Catalog-discovery tests.

Each supplier's ``discover()`` is exercised against a frozen snippet
of the supplier's listing page (saved under ``fixtures/discover/``).
The tests assert the discovered set matches the registry exactly —
so a regex regression that drops a product, or a fixture refresh
that grows the catalogue, fails fast.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from custom_components.be_electricity_prices.providers import bolt as bolt_mod
from custom_components.be_electricity_prices.providers import cociter as cociter_mod
from custom_components.be_electricity_prices.providers import ecopower as ecopower_mod
from custom_components.be_electricity_prices.providers import eneco as eneco_mod
from custom_components.be_electricity_prices.providers import engie as engie_mod
from custom_components.be_electricity_prices.providers import luminus as luminus_mod
from custom_components.be_electricity_prices.providers import mega as mega_mod
from custom_components.be_electricity_prices.providers import octaplus as octaplus_mod
from custom_components.be_electricity_prices.providers import (
    totalenergies as totalenergies_mod,
)

FIX = Path(__file__).parent / "fixtures" / "discover"


class _FakeResponse:
    """Minimal aiohttp.ClientResponse stand-in for discover() tests."""

    def __init__(self, body: str, status: int = 200) -> None:
        self.status = status
        self._body = body
        self.headers = {"content-type": "text/html"}

    async def text(self) -> str:
        return self._body

    async def __aenter__(self) -> "_FakeResponse":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None


class _FakeSession:
    """ClientSession stand-in: returns a fixed body regardless of URL."""

    def __init__(self, body: str, status: int = 200) -> None:
        self._body = body
        self._status = status

    def get(self, *_args: Any, **_kwargs: Any) -> _FakeResponse:
        return _FakeResponse(self._body, self._status)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _read(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


# ---- per-supplier discover() tests -------------------------------------------


def test_mega_discover_matches_registry() -> None:
    session = _FakeSession(_read("mega.html"))
    discovered = _run(mega_mod.discover(session))
    expected = {c.product_name for c in mega_mod._CONTRACTS}
    assert discovered == expected


def test_bolt_discover_matches_registry() -> None:
    session = _FakeSession(_read("bolt.html"))
    discovered = _run(bolt_mod.discover(session))
    expected = {f"{c.folder}/{c.slug}" for c in bolt_mod._CONTRACTS}
    assert discovered == expected


def test_eneco_discover_matches_registry() -> None:
    session = _FakeSession(_read("eneco.html"))
    discovered = _run(eneco_mod.discover(session))
    expected = set(eneco_mod._CONTRACT_SLUGS)
    assert discovered == expected


def test_totalenergies_discover_matches_registry() -> None:
    session = _FakeSession(_read("totalenergies.html"))
    discovered = _run(totalenergies_mod.discover(session))
    expected = {c.slug for c in totalenergies_mod._CONTRACTS}
    assert discovered == expected


def test_octaplus_discover_matches_registry() -> None:
    session = _FakeSession(_read("octaplus.html"))
    discovered = _run(octaplus_mod.discover(session))
    expected = {c.slug for c in octaplus_mod._CONTRACTS}
    assert discovered == expected


def test_cociter_discover_returns_known_family_ids() -> None:
    session = _FakeSession(_read("cociter.html"))
    discovered = _run(cociter_mod.discover(session))
    # Cociter maps known family prefixes back to registry contract ids.
    assert discovered == {"cociter_variable", "cociter_dynamic"}


def test_engie_discover_returns_only_known_families_no_noise() -> None:
    # The fixture mixes legitimate product URLs (dynamic-tarief,
    # empower-vast, flow-contract, ...) with marketing slugs that share
    # the suffix pattern (uw-contract, vragen-faq, ...). Discovery
    # should map known URL tokens to family ids and drop the noise via
    # _NOISE_TOKENS — never surface "uw" or "vragen" as new products.
    session = _FakeSession(_read("engie.html"))
    discovered = _run(engie_mod.discover(session))
    known = {c.family for c in engie_mod._CONTRACTS}
    # Every discovered token must be a known family — no false positives.
    assert discovered <= known
    # And the fixture must surface the families whose product pages
    # actually appear under the discoverable URL patterns.
    assert {"DYNAMIC", "EASY", "EMPOWER", "FLOW"} <= discovered


def test_engie_discover_surfaces_unknown_family() -> None:
    body = _read("engie.html") + "\n/nl/newproduct-tarief\n"
    session = _FakeSession(body)
    discovered = _run(engie_mod.discover(session))
    known = {c.family for c in engie_mod._CONTRACTS}
    assert "newproduct" in discovered - known


def test_luminus_discover_drops_excluded_social_tariff() -> None:
    # The /tarifs-energie/ sitemap directory carries tarif-social/ for
    # the regulated CREG-set protected-customer rate, which is not user-
    # selectable and excluded from the registry. Discovery must skip it.
    session = _FakeSession(_read("luminus.html"))
    discovered = _run(luminus_mod.discover(session))
    assert "tarif-social" not in discovered
    assert "sociaal-tarief" not in discovered


def test_luminus_discover_matches_registry() -> None:
    # All Luminus residential market products on the sitemap are now
    # registered. Discovery must return the registry's slug set
    # exactly (minus the excluded social-tariff slug, which is not a
    # market product).
    session = _FakeSession(_read("luminus.html"))
    discovered = _run(luminus_mod.discover(session))
    known = {c.slug for c in luminus_mod._CONTRACTS}
    assert discovered == known


def test_luminus_discover_surfaces_new_slug() -> None:
    body = _read("luminus.html") + "\n/fr/particuliers/tarifs-energie/newproduct/\n"
    session = _FakeSession(body)
    discovered = _run(luminus_mod.discover(session))
    known = {c.slug for c in luminus_mod._CONTRACTS}
    assert "newproduct" in discovered - known


# ---- behaviour: surfacing new products ---------------------------------------


def test_mega_discover_surfaces_new_product() -> None:
    body = _read("mega.html") + '\ndata-product-element="Mega Future Plan"\n'
    session = _FakeSession(body)
    discovered = _run(mega_mod.discover(session))
    known = {c.product_name for c in mega_mod._CONTRACTS}
    assert "Mega Future Plan" in discovered - known


def test_cociter_discover_surfaces_new_family() -> None:
    body = _read("cociter.html") + "\nRCNew_FAM_Coop-2604-fr.pdf\n"
    session = _FakeSession(body)
    discovered = _run(cociter_mod.discover(session))
    known = {"cociter_variable", "cociter_dynamic"}
    # The unmapped family is surfaced verbatim.
    assert "RCNew_FAM" in discovered - known


# ---- error handling ---------------------------------------------------------


def test_ecopower_discover_skips_inschatting_preview() -> None:
    """The next-month *_gbs_inschatting_tariefkaart_ecopower.pdf preview
    is not a separate product - the parser deliberately ignores it
    and the discover handler must too. Otherwise live-check files a
    spurious 'new product' issue every time the preview is published."""
    session = _FakeSession(_read("ecopower.html"))
    discovered = _run(ecopower_mod.discover(session))
    assert discovered == {ecopower_mod._CONTRACT_ID}


def test_ecopower_discover_surfaces_genuinely_new_family() -> None:
    body = (
        _read("ecopower.html")
        + '\n<a href="https://example/202605_zakelijk_stroom_tariefkaart.pdf">x</a>\n'
    )
    session = _FakeSession(body)
    discovered = _run(ecopower_mod.discover(session))
    assert "ecopower_zakelijk_stroom" in discovered - {ecopower_mod._CONTRACT_ID}


def test_discover_returns_empty_on_http_error() -> None:
    session = _FakeSession("", status=503)
    assert _run(mega_mod.discover(session)) == set()
    assert _run(bolt_mod.discover(session)) == set()
    assert _run(eneco_mod.discover(session)) == set()
    assert _run(totalenergies_mod.discover(session)) == set()
    assert _run(octaplus_mod.discover(session)) == set()
    assert _run(cociter_mod.discover(session)) == set()
    assert _run(engie_mod.discover(session)) == set()
    assert _run(luminus_mod.discover(session)) == set()
    assert _run(ecopower_mod.discover(session)) == set()

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
import json
import logging
from typing import Any, cast

import aiohttp
import pytest

from custom_components.be_electricity_prices.providers import bolt as bolt_mod
from custom_components.be_electricity_prices.providers import cociter as cociter_mod
from custom_components.be_electricity_prices.providers import ebem as ebem_mod
from custom_components.be_electricity_prices.providers import ecofix as ecofix_mod
from custom_components.be_electricity_prices.providers import ecopower as ecopower_mod
from custom_components.be_electricity_prices.providers import eneco as eneco_mod
from custom_components.be_electricity_prices.providers import (
    energyknights as energyknights_mod,
)
from custom_components.be_electricity_prices.providers import (
    energyvision as energyvision_mod,
)
from custom_components.be_electricity_prices.providers import engie as engie_mod
from custom_components.be_electricity_prices.providers import frank as frank_mod
from custom_components.be_electricity_prices.providers import luminus as luminus_mod
from custom_components.be_electricity_prices.providers import mega as mega_mod
from custom_components.be_electricity_prices.providers import octaplus as octaplus_mod
from custom_components.be_electricity_prices.providers import (
    totalenergies as totalenergies_mod,
)

from tests import FIXTURES

FIX = FIXTURES / "discover"


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


class _FakeSessionImpl:
    """ClientSession stand-in: returns a fixed body regardless of URL.

    Provides ``get`` for listing-page-based discovery and ``head`` for
    HEAD-probe-based discovery (Ecofix).
    """

    def __init__(self, body: str, status: int = 200) -> None:
        self._body = body
        self._status = status

    def get(self, *_args: Any, **_kwargs: Any) -> _FakeResponse:
        return _FakeResponse(self._body, self._status)

    def head(self, *_args: Any, **_kwargs: Any) -> _FakeResponse:
        return _FakeResponse(self._body, self._status)


def _FakeSession(body: str, status: int = 200) -> aiohttp.ClientSession:
    """Return a duck-typed ClientSession for discover() calls.

    The returned object only implements .get / .head / async-context
    semantics; cast lets every call site treat it as a real
    ClientSession without type errors at the discover() boundary.
    """
    return cast(aiohttp.ClientSession, _FakeSessionImpl(body, status))


class _UrlFakeSessionImpl:
    """ClientSession stand-in that serves a different body per URL.

    Maps a URL substring to a (body, status) pair; used by the suppliers
    whose discover() scrapes more than one page (Ecopower).
    """

    def __init__(self, by_fragment: dict[str, tuple[str, int]]) -> None:
        self._by_fragment = by_fragment

    def get(self, url: str, *_args: Any, **_kwargs: Any) -> _FakeResponse:
        for frag, (body, status) in self._by_fragment.items():
            if frag in url:
                return _FakeResponse(body, status)
        return _FakeResponse("", 404)

    def head(self, url: str, *_args: Any, **_kwargs: Any) -> _FakeResponse:
        return self.get(url)


def _UrlFakeSession(by_fragment: dict[str, tuple[str, int]]) -> aiohttp.ClientSession:
    return cast(aiohttp.ClientSession, _UrlFakeSessionImpl(by_fragment))


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _read(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


# ---- per-supplier discover() tests -------------------------------------------


def test_mega_discover_matches_registry() -> None:
    session = _FakeSession(_read("mega.html"))
    discovered = _run(mega_mod.discover(session))
    # discover() scrapes the listing, which advertises every residential
    # product plus the professional SME pair; the other B2B editions are
    # unlisted and addressed by built URL, so they are not part of the
    # baseline.
    expected = {c.product_name for c in mega_mod._CONTRACTS if c.advertised}
    assert discovered == expected


def test_energyvision_discover_matches_registry() -> None:
    session = _FakeSession(_read("energyvision.html"))
    discovered = _run(energyvision_mod.discover(session))
    # discover() returns every residential electricity code on the listing,
    # across BOTH language tokens: the Flemish cards are published only as
    # -nl and the Walloon ones only as -WAL-fr, so matching one token would
    # silently drop a whole region's catalogue from the drift check. The
    # registry baseline is DISCOVER_IDS (the full catalogue, so only a
    # genuinely new code flags).
    assert discovered == set(energyvision_mod.DISCOVER_IDS)


def test_energyknights_discover_matches_registry() -> None:
    session = _FakeSession(_read("energyknights.html"))
    discovered = _run(energyknights_mod.discover(session))
    # The listing carries all eight products; only three are modelled, so the
    # baseline is DISCOVER_IDS (the full catalogue) and a genuinely new
    # product is the only thing that flags.
    assert discovered == set(energyknights_mod.DISCOVER_IDS)


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
    # Cociter maps known family prefixes back to registry contract ids. The
    # trihoraire family is one letter from the variable one (RCVaI / RCVar),
    # so a fold that treated them as the same would show up right here.
    assert discovered == {
        "cociter_variable",
        "cociter_variable_impact",
        "cociter_dynamic",
    }


def test_ebem_discover_matches_registry() -> None:
    """EBEM discover() maps PDF kinds to contract ids: every 'elek' PDF
    surfaces both Variabel + B@sic+ (they share the card), and every
    'dynamic' PDF surfaces Dyn@mic. The fixture lists all three kinds,
    so the registry's full contract id set must be returned."""
    session = _FakeSession(_read("ebem.html"))
    discovered = _run(ebem_mod.discover(session))
    expected = {c.contract_id for c in ebem_mod._CONTRACTS}
    assert discovered == expected


def test_ebem_discover_surfaces_unknown_pdf_kind() -> None:
    """Ebem stopped offering fixed contracts ('Ebem biedt voorlopig
    geen vaste contracten meer aan'), so a future revival is a real
    new-product signal: a third PDF kind in the listing should surface
    verbatim so live_check files a tracking issue."""
    body = _read("ebem.html") + (
        '\n<a href="/media/abc12345/ebem_tariefkaart-fix-06-2026.pdf">x</a>\n'
    )
    session = _FakeSession(body)
    discovered = _run(ebem_mod.discover(session))
    known = {c.contract_id for c in ebem_mod._CONTRACTS}
    assert "fix" in discovered - known


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


def _frank_cms_body(filenames: list[str]) -> str:
    """Build a Sanity CMS JSON response body for the given filenames."""
    return json.dumps({"result": [{"originalFilename": f} for f in filenames]})


def test_frank_discover_matches_registry() -> None:
    # Frank scrapes the Sanity CMS rather than an HTML listing; one PDF
    # per tier maps back to its contract id (a bare month is the standard
    # tier, the suffix words HV/VT/JN/SL the others).
    session = _FakeSession(
        _frank_cms_body(
            [
                f"Tariefkaart Elektriciteit Dynamisch{sfx} Januari 2026.pdf"
                for sfx in ("", " HV", " VT", " JN", " SL")
            ]
        )
    )
    discovered = _run(frank_mod.discover(session))
    expected = {t[0] for t in frank_mod._TIERS}
    assert discovered == expected


def test_frank_discover_surfaces_new_tier() -> None:
    session = _FakeSession(
        _frank_cms_body(["Tariefkaart Elektriciteit Dynamisch XL Januari 2026.pdf"])
    )
    discovered = _run(frank_mod.discover(session))
    known = {t[0] for t in frank_mod._TIERS}
    assert "frank_dynamic_xl" in discovered - known


# ---- error handling ---------------------------------------------------------


def test_ecopower_discover_skips_inschatting_preview() -> None:
    """The next-month *_gbs_inschatting_tariefkaart_ecopower.pdf preview
    is not a separate product - the parser deliberately ignores it
    and the discover handler must too. Otherwise live-check files a
    spurious 'new product' issue every time the preview is published."""
    session = _FakeSession(_read("ecopower.html"))
    discovered = _run(ecopower_mod.discover(session))
    assert discovered == {ecopower_mod._CONTRACT_ID}


def test_ecopower_discover_sees_a_family_named_with_a_full_date() -> None:
    """Ecopower moved the dynamic card to a YYYYMMDD filename. Pinned at
    six digits, discover() cannot see a NEW family published under that
    naming either, so the one check meant to notice a new Ecopower product
    would stay silent on it."""
    page = (
        '<a href="https://cdn.example/20260801_dbs_tariefkaart.pdf">dbs</a>'
        '<a href="https://cdn.example/20260901_groenestroom_tariefkaart.pdf">new</a>'
    )
    discovered = _run(ecopower_mod.discover(_FakeSession(page)))
    assert ecopower_mod._DBS_DISCOVER_ID in discovered
    assert "ecopower_groenestroom" in discovered


def test_ecopower_discover_finds_dynamic_card() -> None:
    """The dynamic "Dynamische burgerstroom" card lives on its own page,
    not the gbs price page, so discover() must scrape that page too.
    Otherwise the dbs family is invisible to the catalog drift detector -
    the reason Ecopower's dynamic tariff was never flagged as new."""
    body = (
        _read("ecopower.html")
        + '\n<a href="https://example/202601_dbs_tariefkaart.pdf">x</a>\n'
    )
    session = _FakeSession(body)
    discovered = _run(ecopower_mod.discover(session))
    assert discovered == {ecopower_mod._CONTRACT_ID, ecopower_mod._DBS_DISCOVER_ID}


def test_ecopower_discover_surfaces_new_family_on_dbs_page() -> None:
    """A new family linked only from the dynamic page (not the gbs price
    page) must still be surfaced: discover() matches every detector over
    both page bodies, not just the gbs page."""
    session = _UrlFakeSession(
        {
            ecopower_mod._PRICE_PAGE: (
                '<a href="https://example/202601_gbs_tariefkaart.pdf">x</a>',
                200,
            ),
            ecopower_mod._DBS_PAGE: (
                '<a href="https://example/202601_dbs_tariefkaart.pdf">x</a>'
                '<a href="https://example/202605_zakelijk_stroom_tariefkaart.pdf">y</a>',
                200,
            ),
        }
    )
    discovered = _run(ecopower_mod.discover(session))
    assert discovered == {
        ecopower_mod._CONTRACT_ID,
        ecopower_mod._DBS_DISCOVER_ID,
        "ecopower_zakelijk_stroom",
    }


def test_ecopower_discover_logs_partial_page_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When one page fetches and the other fails, discover() returns the
    reachable result but logs the failure - otherwise the missing family
    would slip past live_check's empty-result warning (the result is still
    non-empty)."""
    session = _UrlFakeSession(
        {
            ecopower_mod._PRICE_PAGE: (
                '<a href="https://example/202601_gbs_tariefkaart.pdf">x</a>',
                200,
            ),
            ecopower_mod._DBS_PAGE: ("", 503),
        }
    )
    with caplog.at_level(logging.WARNING):
        discovered = _run(ecopower_mod.discover(session))
    assert discovered == {ecopower_mod._CONTRACT_ID}
    assert ecopower_mod._DBS_DISCOVER_ID not in discovered
    assert ecopower_mod._DBS_PAGE in caplog.text


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
    # Ecofix uses HEAD-probe discovery; a 5xx on every URL means every
    # contract is dropped.
    assert _run(ecofix_mod.discover(session)) == set()
    # EBEM scrapes the listing page; an HTTP error must yield set().
    assert _run(ebem_mod.discover(session)) == set()
    # Frank queries the Sanity CMS; a 5xx raises ExtractorError -> set().
    assert _run(frank_mod.discover(session)) == set()
    # Energy Knights and EnergyVision both scrape a listing page.
    assert _run(energyknights_mod.discover(session)) == set()
    assert _run(energyvision_mod.discover(session)) == set()


def test_readme_counts_the_spot_indexed_injection_contracts_correctly() -> None:
    """The README's index-linked feed-in figure must match the registry.

    Same class of failure as the discover tests above, and it has bitten:
    the number went from 61 to 60 in the commit that ADDED a contract
    carrying the flag, so it was wrong by two the moment it was written and
    nothing noticed. Derived here rather than trusted, since the sentence is
    what tells a reader whether an ENTSO-E key buys them anything.
    """
    import re
    from pathlib import Path

    from custom_components.be_electricity_prices.providers import all_extractors

    suppliers = {
        extractor.label: ids
        for extractor in all_extractors()
        if (ids := [c.id for c in extractor.contracts if c.spot_indexed_injection])
    }
    contracts = sum(len(ids) for ids in suppliers.values())

    readme = (Path(__file__).parent.parent / "README.md").read_text(encoding="utf-8")
    found = re.search(r"(\d+) contracts across (\d+) suppliers", readme)
    assert found is not None, "the README sentence naming the counts is gone"
    assert (int(found[1]), int(found[2])) == (contracts, len(suppliers)), (
        f"README says {found[0]}; the registry has {contracts} contracts "
        f"across {len(suppliers)} suppliers: {sorted(suppliers)}"
    )


def _issue_form_options(field_id: str) -> list[str]:
    """The options a dropdown offers on the bug report form."""
    # PyYAML ships no stubs and homeassistant pulls it in anyway, so the
    # CI type pass needs the marker rather than another pinned dependency.
    import yaml  # type: ignore[import-untyped]
    from pathlib import Path

    form = yaml.safe_load(
        (
            Path(__file__).parent.parent
            / ".github"
            / "ISSUE_TEMPLATE"
            / "bug_report.yml"
        ).read_text(encoding="utf-8")
    )
    for field in form["body"]:
        if field.get("id") == field_id:
            return list(field["attributes"]["options"])
    raise AssertionError(f"the bug report form has no {field_id!r} field any more")


def test_issue_form_offers_every_registered_supplier() -> None:
    """The form's supplier list must hold every label the flow offers.

    The field is required and has no "other", so a supplier missing here
    does not merely annoy: it forces the reporter to name a supplier they
    are not on, and the report then reads as a bug in that supplier's
    extractor. Issue #85 arrived that way, filed against Eneco by an Energy
    Knights customer, because the list had gone four suppliers stale.
    """
    from custom_components.be_electricity_prices.providers import all_extractors

    offered = set(_issue_form_options("supplier"))
    registered = {extractor.label for extractor in all_extractors()}
    assert offered == registered, (
        f"missing from the form: {sorted(registered - offered)}; "
        f"offered but not a supplier: {sorted(offered - registered)}"
    )


def test_issue_form_offers_every_dso() -> None:
    """Same for the DSO list, down to the spelling.

    The reporter picks the name they were shown in the options flow, so a
    label that only nearly matches sends them hunting for their own DSO.
    """
    from custom_components.be_electricity_prices.const import DSO_CHOICES

    offered = set(_issue_form_options("dso"))
    known = {label for region in DSO_CHOICES.values() for _, label in region}
    assert offered == known, (
        f"missing from the form: {sorted(known - offered)}; "
        f"offered but not a DSO: {sorted(offered - known)}"
    )

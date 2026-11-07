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

"""A card link read off a supplier's page or API answer is followed only
where that supplier's cards sit.

Every card Cociter, Aspiravi and Trevion have linked sits on their own
site, and every card Ecopower, Eneco, energie.be and Frank have named sits
on one file host of theirs, in the card archive and live alike. A link to
any other host, even over https and for a newer month, is one a changed
page or answer carries. The real links are the archived cards' addresses;
the other host is the attack.
"""

from __future__ import annotations

import json
from datetime import date
from unittest.mock import AsyncMock

import pytest

from custom_components.be_electricity_prices.providers import (
    aspiravi,
    cociter,
    ecopower,
    eneco,
    energiebe,
    frank,
    trevion,
)
from custom_components.be_electricity_prices.providers._links import (
    on_hosts,
    on_page_site,
)
from custom_components.be_electricity_prices.providers.base import ExtractorError

_ELSEWHERE = (
    "https://cards.example",
    "https://www.cociter.be.cards.example",
    "https://aspiravi-energy.be@cards.example",
    "https://trevion.be:8443",
)
# The same attacks on a file host: another host, one named after it, one
# behind its name as a user, another port and plain http.
_OFF_HOST = (
    "https://cards.example",
    "https://{}.cards.example",
    "https://{}@cards.example",
    "https://{}:8443",
    "http://{}",
)


@pytest.mark.parametrize("host", _ELSEWHERE)
async def test_a_cociter_card_off_its_site_is_not_followed(
    monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    real = "https://www.cociter.be/wp-content/uploads/RCVaI_YMR_Coop-2611-fr.pdf"
    crafted = f'<a href="{host}/wp-content/uploads/RCVaI_YMR_Coop-2612-fr.pdf">x</a>'
    page = f'{crafted}<a href="{real}">Novembre 2026</a>'
    monkeypatch.setattr(cociter, "fetch_text", AsyncMock(return_value=page))
    assert await cociter._find_latest(None, cociter._VAI_RE) == (real, "2026-11")  # type: ignore[arg-type]

    monkeypatch.setattr(cociter, "fetch_text", AsyncMock(return_value=crafted))
    with pytest.raises(ExtractorError, match="no matching tariff card"):
        await cociter._find_latest(None, cociter._VAI_RE)  # type: ignore[arg-type]


@pytest.mark.parametrize("host", _ELSEWHERE)
async def test_an_aspiravi_card_off_its_site_is_neither_read_nor_probed(
    monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    """The freshness probe HEADs the card the downloads page names, so it is
    held to the site as the fetch is."""
    real = (
        "https://aspiravi-energy.be/wp-content/uploads/2026/11/"
        "900027_Eco_Plus_flex_huishoudelijk-formule-2026.pdf"
    )

    def link(url: str) -> str:
        return f'<a class="btn" href="{url}"><span>Huidige tariefkaart</span></a>'

    crafted = link(f"{host}/wp-content/uploads/2026/12/900027_card.pdf")
    stray = '<a href="https://xn--zz.example/brochure.pdf">brochure</a>'
    monkeypatch.setattr(
        aspiravi, "fetch_text", AsyncMock(return_value=crafted + stray + link(real))
    )
    assert await aspiravi._current_card_url(None) == real  # type: ignore[arg-type]

    probed = AsyncMock(return_value="stamp")
    monkeypatch.setattr(aspiravi, "head_freshness_key", probed)
    monkeypatch.setattr(aspiravi, "fetch_text", AsyncMock(return_value=crafted))
    assert await aspiravi.probe(None, aspiravi._CONTRACT_ID, "flanders") is None  # type: ignore[arg-type]
    probed.assert_not_awaited()


@pytest.mark.parametrize("host", _ELSEWHERE)
def test_a_trevion_card_off_its_site_is_not_followed(host: str) -> None:
    contract = next(c for c in trevion._CONTRACTS if c.id == "groene_stroom_flex")
    name = "Tariefkaart-Groene-Stroom-Flex-Particulier"
    real = f"https://trevion.be/tariefkaarten/{name}-202610.pdf"
    crafted = f'<a href="{host}/tariefkaarten/{name}-202611.pdf">x</a>'
    page = f'{crafted}<a href="{real}">x</a><a href="/tariefkaarten/{name}-202609.pdf">'
    assert trevion._resolve_card(page, contract) == (real, "2026-10")
    # A link from the site's root reads as it always did.
    assert trevion._resolve_card(page, contract, date(2026, 9, 1))[0] == (
        f"https://trevion.be/tariefkaarten/{name}-202609.pdf"
    )
    with pytest.raises(ExtractorError, match="no card found"):
        trevion._resolve_card(crafted, contract)


@pytest.mark.parametrize(
    ("href", "url"),
    [
        ("/a/card.pdf", "https://www.cociter.be/a/card.pdf"),
        ("card.pdf", "https://www.cociter.be/electricite/card.pdf"),
        ("https://www.cociter.be/b.pdf", "https://www.cociter.be/b.pdf"),
        ("//cards.example/b.pdf", None),
        ("http://www.cociter.be/b.pdf", None),
        ("https://WWW.COCITER.BE/b.pdf", "https://WWW.COCITER.BE/b.pdf"),
    ],
)
def test_a_link_is_read_against_the_page_it_is_on(href: str, url: str | None) -> None:
    assert on_page_site("https://www.cociter.be/electricite/x", href) == url


@pytest.mark.parametrize(
    ("url", "kept"),
    [
        ("https://cdn.nimbu.io/s/a/202607_gbs_tariefkaart.pdf?dl=1", True),
        ("https://CDN.NIMBU.IO/a.pdf", True),
        ("https://cdn.nimbu.io:443/a.pdf", True),
        ("https://cdn.nimbu.io:8443/a.pdf", False),
        ("http://cdn.nimbu.io/a.pdf", False),
        ("https://nimbu.io/a.pdf", False),
        ("https://cdn.nimbu.io.cards.example/a.pdf", False),
        ("https://cdn.nimbu.io@cards.example/a.pdf", False),
        ("https://[cdn.nimbu.io/a.pdf", False),
        # A label no IDNA decoder reads: refused, not raised on.
        ("https://xn--zz.example/a.pdf", False),
        ("/a.pdf", False),
    ],
)
def test_a_link_is_held_to_the_hosts_given(url: str, kept: bool) -> None:
    assert on_hosts(url, frozenset({"cdn.nimbu.io"})) == (url if kept else None)


@pytest.mark.parametrize("host", _OFF_HOST)
async def test_an_ecopower_card_off_its_host_is_not_followed(
    monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    base = host.format("cdn.nimbu.io")
    files = "https://cdn.nimbu.io/s/4tn7vz5/channelentries"
    gbs = f"{files}/vxvv2p3/files/1785485811994/202607_gbs_tariefkaart.pdf?dl=1"
    dbs = f"{files}/w19ql4o/files/1747643844883/202501b_dbs_tariefkaart.pdf?dl=1"
    crafted = (
        f'<a href="{base}/x/202611_gbs_tariefkaart.pdf">x</a>'
        f'<a href="{base}/x/20261101_dbs_tariefkaart.pdf">x</a>'
    )
    # A partner link whose host does not decode sits on the page as well.
    stray = '<a href="https://xn--zz.example/">partner</a>'
    page = f'{crafted}{stray}<a href="{gbs}">x</a><a href="{dbs}">x</a>'
    monkeypatch.setattr(ecopower, "fetch_text", AsyncMock(return_value=page))
    assert await ecopower._resolve_latest_pdf(None) == (gbs, "2026-07")  # type: ignore[arg-type]
    assert await ecopower._resolve_latest_dbs_pdf(None) == (dbs, "2025-01")  # type: ignore[arg-type]

    monkeypatch.setattr(ecopower, "fetch_text", AsyncMock(return_value=crafted))
    with pytest.raises(ExtractorError, match="no Ecopower tariefkaart"):
        await ecopower._resolve_latest_pdf(None)  # type: ignore[arg-type]
    with pytest.raises(ExtractorError, match="no Ecopower dbs tariefkaart"):
        await ecopower._resolve_latest_dbs_pdf(None)  # type: ignore[arg-type]


@pytest.mark.parametrize("host", _OFF_HOST)
def test_an_eneco_card_off_its_host_is_not_followed(host: str) -> None:
    """A full link elsewhere is passed over; the card's name it carries is
    read as a bare one, on Eneco's own host."""
    name = "BC_032_012611_NL_ENECO_POWER_FLEX.pdf"
    real = f"https://cdn.eneco.be/downloads/nl/general/tk/{name}"
    crafted = f'<a href="{host.format("cdn.eneco.be")}/tk/{name}">x</a>'
    assert eneco._resolve_url(crafted + f'<a href="{real}">', "power_flex") == real
    assert eneco._resolve_url(crafted, "power_flex") == real


@pytest.mark.parametrize("host", _OFF_HOST)
async def test_an_energiebe_card_off_its_host_is_not_followed(
    monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    """Both of energie.be's APIs name the card; neither is followed off the
    blob host every card has sat on."""
    real = "https://energie.blob.core.windows.net/cms/assets/Variabel_EL_Res.pdf"
    crafted = f"{host.format('energie.blob.core.windows.net')}/cms/assets/x.pdf"

    def contract(url: str) -> dict[str, object]:
        return {"tariffType": "Variable", "contractTypeElRes": {"tariffDocument": url}}

    answer = {"contracts": [contract(crafted), contract(real)]}
    monkeypatch.setattr(
        energiebe, "fetch_text", AsyncMock(return_value=json.dumps(answer))
    )
    assert await energiebe._resolve_card_url(None, "Variable") == real  # type: ignore[arg-type]
    answer = {"contracts": [contract(crafted)]}
    monkeypatch.setattr(
        energiebe, "fetch_text", AsyncMock(return_value=json.dumps(answer))
    )
    with pytest.raises(ExtractorError, match="no residential Variable card"):
        await energiebe._resolve_card_url(None, "Variable")  # type: ignore[arg-type]

    listing = {"tariffCards": [{"date": "2026/07", "electricityDocument": crafted}]}
    monkeypatch.setattr(
        energiebe, "fetch_text", AsyncMock(return_value=json.dumps(listing))
    )
    month = date(2026, 7, 1)
    assert await energiebe._resolve_archive_url(None, "Variable", month) is None  # type: ignore[arg-type]


@pytest.mark.parametrize("host", _OFF_HOST)
async def test_a_frank_card_off_its_host_is_not_followed(
    monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    """Sanity names the file it holds; one off Sanity's file host is refused,
    not swapped for an older card."""
    files = "https://cdn.sanity.io/files/8navd656/production-be"
    name = "Frank Energie Tariefkaart Elektriciteit Dynamisch JN November 2026.pdf"
    rows = [
        {
            "originalFilename": name,
            "url": f"{files}/fb5d894eee5c67b9d424ad51ba7d83e166a54720.pdf",
            "_createdAt": "2026-10-30T12:38:23Z",
        }
    ]
    monkeypatch.setattr(frank, "_sanity_query", AsyncMock(return_value=rows))
    assert await frank._resolve_pdf_url(None, "frank_dynamic_jn") == (  # type: ignore[arg-type]
        rows[0]["url"],
        "november 2026",
    )
    newer = {
        "originalFilename": name.replace(".pdf", " (2).pdf"),
        "url": f"{host.format('cdn.sanity.io')}/files/x.pdf",
        "_createdAt": "2026-10-31T12:38:23Z",
    }
    monkeypatch.setattr(frank, "_sanity_query", AsyncMock(return_value=[newer, *rows]))
    with pytest.raises(ExtractorError, match="not on Sanity's file host"):
        await frank._resolve_pdf_url(None, "frank_dynamic_jn")  # type: ignore[arg-type]

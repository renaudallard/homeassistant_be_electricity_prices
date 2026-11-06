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

"""Every hop of a redirect is checked before it is requested.

Driven over real sockets. One TLS server on 127.0.0.1 answers for every
name the fetches ask, under a throwaway authority only the client trusts,
and a plain listener on 127.0.0.1 stands in for a device on the LAN: it
records every connection it is offered, so a test can tell that a refused
hop was never even connected to, not only that its answer went unread.
"""

from __future__ import annotations

import asyncio
import socket
import ssl
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from aiohttp import web
from aiohttp.abc import AbstractResolver, ResolveResult

from custom_components.be_electricity_prices import (
    api,
    brugel,
    creg_ev,
    cwape,
    fluvius,
    synergrid,
)
from custom_components.be_electricity_prices.providers._pdf import (
    _fetch_validated_pdf_bytes,
    fetch_pdf_text,
    fetch_text,
    head_freshness_key,
    head_ok,
    head_or_raise,
)
from custom_components.be_electricity_prices.providers.base import ExtractorError

# Every name the fetches below ask for, on the one TLS server. The LAN
# device has a name too, so a redirect can reach it by name as well as by
# address.
_NAMES = (
    "supplier.test",
    "storage.test",
    "web-api.tp.entsoe.eu",
    "api.energy-charts.info",
    "www.smard.de",
    "brugel.brussels",
    "www.creg.be",
    "www.synergrid.be",
    "www.cwape.be",
    "www.fluvius.be",
    "lan.test",
)

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


def _contexts(directory: Path) -> tuple[ssl.SSLContext, ssl.SSLContext]:
    """A server context for ``_NAMES`` and a client context trusting only
    the authority that signed it."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    now = datetime.now(UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "redirect test CA")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    key = ec.generate_private_key(ec.SECP256R1())
    leaf = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, _NAMES[0])]))
        .issuer_name(ca_name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(name) for name in _NAMES]),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )
    cert_file = directory / "leaf.pem"
    key_file = directory / "leaf.key"
    cert_file.write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    server = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    server.load_cert_chain(cert_file, key_file)
    client = ssl.create_default_context(
        cadata=ca.public_bytes(serialization.Encoding.PEM).decode("ascii")
    )
    return server, client


@pytest.fixture(scope="module")
def contexts(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[ssl.SSLContext, ssl.SSLContext]:
    return _contexts(tmp_path_factory.mktemp("tls"))


class _Resolver(AbstractResolver):
    """Every test name at 127.0.0.1, port 443 being the TLS server's."""

    def __init__(self, port: int) -> None:
        self.port = port

    async def resolve(
        self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET
    ) -> list[ResolveResult]:
        if host not in _NAMES:
            raise OSError(f"{host} is not on the test network")
        return [
            {
                "hostname": host,
                "host": "127.0.0.1",
                "port": self.port if port == 443 else port,
                "family": socket.AF_INET,
                "proto": 0,
                "flags": socket.AI_NUMERICHOST,
            }
        ]

    async def close(self) -> None:
        return None


class _Net:
    """What the TLS server answers, what it was asked, and what the LAN
    device was offered."""

    def __init__(self) -> None:
        self.routes: dict[str, Handler] = {}
        self.asked: list[str] = []
        self.lan: list[bytes] = []
        self.lan_port = 0
        self.session: aiohttp.ClientSession

    def redirect(self, where: str, location: str, status: int = 302) -> None:
        async def answer(_request: web.Request) -> web.StreamResponse:
            return web.Response(status=status, headers={"Location": location})

        self.routes[where] = answer

    def answer(self, where: str, body: bytes, content_type: str = "text/html") -> None:
        async def answer(_request: web.Request) -> web.StreamResponse:
            return web.Response(body=body, content_type=content_type)

        self.routes[where] = answer

    async def serve(self, request: web.Request) -> web.StreamResponse:
        where = f"{request.host.split(':')[0]}{request.path}"
        self.asked.append(f"{request.method} {where}")
        handler = self.routes.get(where)
        if handler is None:
            return web.Response(status=404, text="not here")
        return await handler(request)

    async def lan_device(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            opening = await asyncio.wait_for(reader.read(512), 1)
        except TimeoutError:
            opening = b""
        self.lan.append(opening)
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: 39\r\n"
            b"Connection: close\r\n\r\n<html>router admin: secret page</html>\n"
        )
        try:
            await writer.drain()
        finally:
            writer.close()


@pytest.fixture
async def net(
    socket_enabled: None, contexts: tuple[ssl.SSLContext, ssl.SSLContext]
) -> AsyncIterator[_Net]:
    server_context, client_context = contexts
    net = _Net()
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", net.serve)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    listening = socket.create_server(("127.0.0.1", 0))
    site = web.SockSite(runner, listening, ssl_context=server_context)
    await site.start()
    lan = await asyncio.start_server(net.lan_device, "127.0.0.1", 0)
    net.lan_port = lan.sockets[0].getsockname()[1]
    connector = aiohttp.TCPConnector(
        resolver=_Resolver(listening.getsockname()[1]), ssl=client_context
    )
    net.session = aiohttp.ClientSession(connector=connector)
    try:
        yield net
    finally:
        await net.session.close()
        await runner.cleanup()
        lan.close()
        await lan.wait_closed()


# Where a supplier's answer could send Home Assistant on the LAN: by address
# over plain http and over https, IPv4 written as IPv6, by name over plain
# http, and by name with a login aiohttp would send.
_LAN_TARGETS = (
    "http://127.0.0.1:{port}/cgi-bin/reboot.cgi?now=1",
    "https://127.0.0.1:{port}/",
    "https://[::ffff:127.0.0.1]:{port}/",
    "http://lan.test:{port}/admin",
    "https://admin:hunter2@lan.test:{port}/",
)


@pytest.mark.parametrize("target", _LAN_TARGETS)
@pytest.mark.parametrize("reader", ["text", "pdf", "head", "probe"])
async def test_a_redirect_to_the_lan_is_never_requested(
    net: _Net, target: str, reader: str
) -> None:
    """aiohttp followed a redirect wherever it pointed and the answer was
    only checked once it had come back, so the LAN device had been sent the
    request, its path, query and login included, by the time the integration
    refused to read what it said."""
    location = target.format(port=net.lan_port)
    net.redirect("supplier.test/a", location)
    url = "https://supplier.test/a"
    if reader == "probe":
        assert await head_freshness_key(net.session, url) is None
    else:
        read: Callable[[aiohttp.ClientSession, str], Awaitable[Any]] = {
            "text": fetch_text,
            "pdf": fetch_pdf_text,
            "head": head_or_raise,
        }[reader]
        with pytest.raises(ExtractorError) as raised:
            await read(net.session, url)
        # The redirect's address can be a LAN device's: it stays out of
        # last_error.
        assert "127.0.0.1" not in str(raised.value)
        assert "lan.test" not in str(raised.value)
    assert net.lan == []
    method = "HEAD" if reader in ("head", "probe") else "GET"
    assert net.asked == [f"{method} supplier.test/a"]


@pytest.mark.parametrize("source", ["entsoe", "energy-charts", "smard"])
async def test_a_day_ahead_redirect_to_the_lan_is_never_requested(
    net: _Net, source: str
) -> None:
    """The three day-ahead clients had no check at all, and two of them
    copied the LAN device's answer into the error last_error shows."""
    location = f"http://127.0.0.1:{net.lan_port}/cgi-bin/reboot.cgi?now=1"
    for where in (
        "web-api.tp.entsoe.eu/api",
        "api.energy-charts.info/price",
        "www.smard.de/app/chart_data/4996/DE/index_quarterhour.json",
    ):
        net.redirect(where, location)
    clients: dict[str, api.EntsoeClient | api.EnergyChartsClient | api.SmardClient] = {
        "entsoe": api.EntsoeClient("not-a-key", net.session),
        "energy-charts": api.EnergyChartsClient(net.session),
        "smard": api.SmardClient(net.session),
    }
    client = clients[source]
    with pytest.raises(api.EntsoeError) as raised:
        await client.fetch_day_ahead(
            datetime(2026, 11, 4, 23, tzinfo=UTC), datetime(2026, 11, 5, 23, tzinfo=UTC)
        )
    assert "secret" not in str(raised.value)
    assert net.lan == []


async def test_the_regulator_downloads_never_request_a_redirect_to_the_lan(
    net: _Net,
) -> None:
    """Brugel's sheets, the CREG's rate file, Synergrid's page and
    workbooks, and the CWaPE and Fluvius workbooks were fetched with no check
    either: the last two let aiohttp follow and looked at the chain only once
    it had come back."""
    location = f"http://127.0.0.1:{net.lan_port}/cgi-bin/reboot.cgi?now=1"
    sheet = brugel._URL.format(published=2025, year=2026)
    workbook = f"{synergrid._BASE_URL}/2026/SPP_ex-ante_and_ex-post_2026.xlsx"
    grid = f"{cwape._SITE}/sites/default/files/ORES_2026.xlsx"
    tariffs = f"{fluvius._SITE}/sites/fluvius/files/tarieven-2026.xlsx"
    net.answer(
        "www.fluvius.be/publication",
        f'<a href="{tariffs[len(fluvius._SITE) :]}">XLSX</a>'.encode(),
    )
    for url in (sheet, creg_ev._URL, synergrid._PAGE_URL, workbook, grid, tariffs):
        net.redirect(url.removeprefix("https://"), location)
    assert await brugel._pdf_text(net.session, sheet) is None
    assert await creg_ev._csv_text(net.session) is None
    assert (
        await synergrid._listed_url(net.session, 2026, synergrid._SPP_TOKENS, ".xlsx")
        is None
    )
    with pytest.raises(ValueError, match="refusing"):
        await synergrid._download(net.session, workbook)
    with pytest.raises(ExtractorError, match="its redirect is not an https"):
        await cwape._download(net.session, grid)
    with pytest.raises(ExtractorError, match="its redirect is not an https"):
        await fluvius._fetch_workbook(
            net.session, 'href="/publication"', 'href="([^"]+)"'
        )
    assert net.lan == []


async def test_a_redirect_to_another_site_over_https_is_followed(net: _Net) -> None:
    """energie.be's document API on azurewebsites.net answers with a redirect
    to its storage on blob.core.windows.net, so a redirect may leave the site
    it was asked of."""
    net.redirect("supplier.test/document", "https://storage.test/blob/card.pdf")
    net.answer("storage.test/blob/card.pdf", b"%PDF-1.7 card", "application/pdf")
    payload = await _fetch_validated_pdf_bytes(
        net.session, "https://supplier.test/document"
    )
    assert payload == b"%PDF-1.7 card"
    assert net.asked == ["GET supplier.test/document", "GET storage.test/blob/card.pdf"]


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
async def test_every_redirect_status_is_followed_where_it_points(
    net: _Net, status: int
) -> None:
    """A Location is read against the address that answered it, as aiohttp
    reads one, and HEAD stays a HEAD."""
    net.redirect("supplier.test/old", "/new/listing", status)
    net.answer("supplier.test/new/listing", b"moved here")
    assert await fetch_text(net.session, "https://supplier.test/old") == "moved here"
    assert await head_ok(net.session, "https://supplier.test/old")
    assert net.asked == [
        "GET supplier.test/old",
        "GET supplier.test/new/listing",
        "HEAD supplier.test/old",
        "HEAD supplier.test/new/listing",
    ]


async def test_a_redirect_loop_is_cut_short(net: _Net) -> None:
    """aiohttp's own limit was ten hops; no source sends more than one."""
    from custom_components.be_electricity_prices.providers._pdf import MAX_REDIRECTS

    for hop in range(12):
        net.redirect(f"supplier.test/{hop}", f"/{hop + 1}")
    with pytest.raises(ExtractorError, match="network error fetching"):
        await fetch_text(net.session, "https://supplier.test/0")
    assert len(net.asked) == MAX_REDIRECTS + 1


async def test_the_hops_share_the_fetch_timeout(net: _Net) -> None:
    """The timeout bounded the whole chain while aiohttp followed it, and
    still does now that each hop is asked on its own: five hops of 0,4 s
    cannot fit in one second."""

    def slow(hop: int) -> Handler:
        async def answer(_request: web.Request) -> web.StreamResponse:
            await asyncio.sleep(0.4)
            return web.Response(status=302, headers={"Location": f"/{hop + 1}"})

        return answer

    for hop in range(5):
        net.routes[f"supplier.test/{hop}"] = slow(hop)
    net.answer("supplier.test/5", b"too late")
    with pytest.raises(ExtractorError, match="TimeoutError"):
        await fetch_text(net.session, "https://supplier.test/0", timeout=1)
    assert len(net.asked) <= 3


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("https://www.energie.be/api/v1/data/contracts", None),
        ("https://STORE.blob.core.windows.net/c.pdf", None),
        ("https://8.8.8.8/", None),
        ("https://[2a00:1450:4001::1]/", None),
        ("https://123.example.com/", None),
        ("http://www.energie.be/", "not an https address"),
        ("ftp://www.energie.be/", "not an https address"),
        ("https://admin:hunter2@www.energie.be/", "an address carrying credentials"),
        ("https://admin@www.energie.be/", "an address carrying credentials"),
        ("https://10.0.0.1/", "not a public address"),
        ("https://172.16.0.1/", "not a public address"),
        ("https://192.168.1.1/", "not a public address"),
        ("https://127.0.0.1/", "not a public address"),
        ("https://127.0.0.1./", "not a public address"),
        ("https://169.254.169.254/latest/meta-data/", "not a public address"),
        ("https://100.64.0.1/", "not a public address"),
        ("https://0.0.0.0/", "not a public address"),
        ("https://224.0.0.251/", "not a public address"),
        ("https://255.255.255.255/", "not a public address"),
        ("https://240.0.0.1/", "not a public address"),
        # The forms the C library reads as an address too.
        ("https://2130706433/", "not a public address"),
        ("https://0x7f000001/", "not a public address"),
        ("https://0177.0.0.1/", "not a public address"),
        ("https://10.1/", "not a public address"),
        ("https://[::1]/", "not a public address"),
        ("https://[::]/", "not a public address"),
        ("https://[fe80::1%25eth0]/", "not a public address"),
        ("https://[fc00::1]/", "not a public address"),
        ("https://[fec0::1]/", "not a public address"),
        ("https://[ff02::1]/", "not a public address"),
        ("https://[::ffff:192.168.1.1]/", "not a public address"),
        ("https://[::ffff:7f00:1]/", "not a public address"),
        ("https://[64:ff9b::a00:1]/", "not a public address"),
        ("https://[2002:c0a8:101::1]/", "not a public address"),
        ("https:///no-host", "not a public address"),
    ],
)
def test_an_address_is_refused_for_what_it_is(url: str, reason: str | None) -> None:
    from yarl import URL

    from custom_components.be_electricity_prices.providers._pdf import _refusal

    assert _refusal(URL(url)) == reason

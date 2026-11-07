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

"""Shared helpers for fetching and reading PDF tariff cards."""

from __future__ import annotations

import asyncio
import codecs
import ipaddress
import json
import logging
import re
import socket
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from pathlib import Path

import aiohttp
from yarl import URL

from ._parse import (
    to_float,
)
from ._pdf_render import (
    extract_pdf_text,
    extract_pdf_text_aligned,
    extract_pdf_text_layout,
)
from ._render_thread import render_in_thread
from .base import (
    ExtractorError,
)

_LOGGER = logging.getLogger(__name__)


def _read_version() -> str:
    manifest = Path(__file__).resolve().parent.parent / "manifest.json"
    try:
        return str(json.loads(manifest.read_text(encoding="utf-8")).get("version", "0"))
    except (OSError, ValueError):
        return "0"


USER_AGENT = f"Home Assistant be_electricity_prices/{_read_version()}"

# Opens the message of a card refused because a card it is read with came
# from another month: a listing read that failed between the two fetches, or
# one card republished before the other. The next fetch pairs them again.
OUT_OF_STEP = "cards out of step"


def is_transient_fetch_error(message: str) -> bool:
    """Whether an ExtractorError message describes a transient fetch
    failure a later refresh is likely to recover, rather than a permanent
    one (parse error, 404, non-PDF payload) that needs a code fix.

    The fetch helpers in this module wrap aiohttp surface errors with three
    stable prefixes: ``network error fetching`` (timeout / reset / DNS),
    ``storage error fetching`` (the object store behind the url refused the
    read) and ``HTTP <status>``. A bare ``network error`` is always
    transient. Among HTTP statuses, 5xx plus 408 / 429 / 403 are transient
    (the Cloudflare-fronted suppliers intermittently answer an otherwise
    healthy resource with a 403 anti-bot challenge or a 429 that succeeds
    on retry); 404 / 410 mean the card was renamed or withdrawn and must
    fail fast. A pair of cards caught in different months (``OUT_OF_STEP``)
    is transient too: it is no layout change, and the next fetch heals it.
    """
    if message.startswith(("network error fetching", OUT_OF_STEP)):
        return True
    if message.startswith("storage error fetching"):
        return True
    if message.startswith("HTTP "):
        head = message[len("HTTP ") :].split(None, 1)[0]
        if head.isdigit():
            status = int(head)
            return status >= 500 or status in (403, 408, 429)
    return False


# How _fetch_validated_pdf_bytes's message opens when the payload opens with
# a tag past any blank space: a web page came in the card's place. Said
# before the address, so the check reads the message's start, which a cut
# of the message keeps.
_A_WEB_PAGE = "expected a PDF, got a web page"
_WEB_PAGE_RE = re.compile(rb"[ \t\r\n]*<")


def is_missing_card_error(message: str) -> bool:
    """Whether an ExtractorError message says there is no card at the
    address.

    Permanent for :func:`is_transient_fetch_error`, since a retry will not
    bring the card back, but not a layout change either: the supplier has no
    card at that address, because it has not published this month's yet,
    withdrew the product or moved its cards. That is a 404 or a 410, read
    off the same ``HTTP <status>`` prefix the fetch helpers here write, or a
    web page served in the card's place: Mega's CDN answers an unpublished
    card with 200 and an HTML page, and a redirect to a homepage lands on
    one too.
    """
    if message.startswith("HTTP "):
        return message[len("HTTP ") :].split(None, 1)[0] in ("404", "410")
    return message.startswith(_A_WEB_PAGE)


def error_text(err: BaseException) -> str:
    """The exception's message, or its class name when it carries none.

    aiohttp raises its timeouts argless, and ``str()`` of an argless
    exception is ``""``. Interpolated into a message that ends in ``": "``
    that produced a user-facing sentence trailing off after the colon, on
    all three surfaces that show ``last_error``: the ``snapshot_stale``
    Repairs card, the ``current_price`` sensor attribute, and diagnostics.
    Naming the class is the smallest thing that stays informative: the
    caller's own prefix already says what was being attempted.

    Also used by the ENTSO-E client, whose ``EntsoeError`` reaches the same
    three surfaces through ``ENTSO-E: {err}``. Cut like an address
    (:func:`shown_text`), since aiohttp's errors spell out the one they failed on.
    """
    return shown_text(str(err) or type(err).__name__)


# How much of an address, or of an error, a message shows. Most links are
# read off a supplier's page, so one is as long as the page makes it, and a
# parser's message can quote what it read, as long as the card makes it. A
# message reaches last_error, the Repairs card and every sensor's attributes,
# so it is cut here and again where last_error stores it. The longest real
# address, Elegant's archive, was 366 characters on 2026-11-06.
_SHOWN = 500


def shown_text(text: str) -> str:
    """``text`` cut to :data:`_SHOWN` characters, for a message."""
    return text if len(text) <= _SHOWN else f"{text[:_SHOWN]}..."


# 64 MiB: ~12x the largest real tariff card (Bolt's ~5 MiB PDFs), so it
# never trips on a legitimate card or workbook while bounding what a broken
# or hostile server can pull into the coordinator's memory in one fetch.
MAX_RESPONSE_BYTES = 64 * 1024 * 1024

# 8 MiB: the cap on a page, an API answer or a table. Each is decoded and
# parsed whole, mostly on the event loop, at many times its size in memory:
# 8 MiB of JSON took 1,4 s and 185 MiB to load on a Raspberry Pi. Measured
# 2026-11-06, the largest real ones are energy-charts' answer from 2015,
# where its Belgian prices start, to November 2026 at 2,3 MB (0,6 MB more a
# year), the CWaPE document listing at 1,9 MB and an OCTA+ archive sheet, a
# card in base64, at 1,6 MB. A Justel law page is 0,6 MB, Luminus's largest
# sitemap 0,4 MB, every other listing and API answer under 0,35 MB, the
# archive's tables under 80 kB and the CREG rate file 1,3 kB. ENTSO-E's
# largest, a week of both products, is about 0,1 MB by its layout.
MAX_TEXT_BYTES = 8 * 1024 * 1024


async def read_capped(
    resp: aiohttp.ClientResponse, url: str, limit: int = MAX_RESPONSE_BYTES
) -> bytes:
    """Read a response body, refusing one past ``limit`` bytes: a card's
    or a workbook's :data:`MAX_RESPONSE_BYTES`, or the :data:`MAX_TEXT_BYTES`
    :func:`read_text_capped` passes for a page.

    Content-Length is only a hint: a chunked answer carries none and a
    server can lie, so the cap holds on the bytes read as well. The bytes
    counted are the decoded ones, so a compressed answer is held to it too.
    """
    declared = resp.content_length
    if declared is not None and declared > limit:
        raise ExtractorError(
            f"refusing {shown_text(url)}: declared {declared} bytes (limit {limit})"
        )
    payload = bytearray()
    async for chunk in resp.content.iter_chunked(65536):
        payload.extend(chunk)
        if len(payload) > limit:
            raise ExtractorError(f"refusing {shown_text(url)}: more than {limit} bytes")
    return bytes(payload)


# The charsets a body is read in when it declares one; any other, or none,
# reads as UTF-8. Python decodes in every codec it ships, some slowly enough
# to stall the event loop on a page a server labels with them: punycode is
# quadratic, 100 kB of it took 1,8 s on a Raspberry Pi and 200 kB 4,4 s. On
# 2026-11-06 every source declared utf-8, Justel iso-8859-1, or nothing.
_CHARSETS = frozenset(
    codecs.lookup(name).name for name in ("utf-8", "latin-1", "cp1252", "ascii")
)


def _charset(declared: str | None) -> str:
    """The codec a body declaring ``declared`` is read in."""
    try:
        name = codecs.lookup(declared or "utf-8").name
    except LookupError:
        # A charset Python does not know, "utf8mb4" for one.
        return "utf-8"
    return name if name in _CHARSETS else "utf-8"


async def read_text_capped(
    resp: aiohttp.ClientResponse, url: str, *, limit: int = MAX_TEXT_BYTES
) -> str:
    """Read a text body under ``limit`` bytes, in the charset it declares or
    UTF-8. The limit is :data:`MAX_TEXT_BYTES` unless the caller knows its
    document to be smaller.

    Not strict: a page in another charset than it declares is then refused
    by its parser as an ExtractorError rather than raised as a
    UnicodeDecodeError.
    """
    payload = await read_capped(resp, url, limit)
    return payload.decode(_charset(resp.charset), errors="replace")


def _internal_address(host: str) -> bool:
    """Whether ``host`` is an address literal off the public internet.

    Private, loopback, link-local, reserved, multicast and unspecified
    addresses are, IPv4 or IPv6, an IPv4 address written as IPv6 included,
    and so is the shared range carrier-grade NAT and Tailscale number
    devices in. The short and numeric spellings of IPv4 the C library reads
    as an address ("10.1", "0x7f.1", "2130706433") count as the address
    they spell. A name is not looked up: over https the host it leads to
    still has to show a certificate the client trusts for that name, so a
    redirect by name reaches only a host holding a public certificate for
    it.
    """
    name = host.removesuffix(".")
    address: ipaddress.IPv4Address | ipaddress.IPv6Address
    try:
        address = ipaddress.ip_address(name)
    except ValueError:
        try:
            address = ipaddress.IPv4Address(socket.inet_aton(name))
        except (OSError, ValueError):
            return False
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        elif address.is_site_local:
            return True
    return (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
        or not address.is_global
    )


def _refusal(url: URL) -> str | None:
    """Why ``url`` is not fetched, or None when it may be.

    Every card, listing and API the integration reads is served over https
    by a public name, so this refuses nothing real. Most links are read off
    a supplier's page, and a plain http one could be answered by anything
    on the way; a redirect to one, or to a LAN address, is how a host the
    integration trusts could send Home Assistant into the LAN. An address
    carrying a login is refused too, since aiohttp would send it along.
    """
    if url.scheme != "https":
        return "not an https address"
    if url.user is not None or url.password is not None:
        return "an address carrying credentials"
    if not url.host or _internal_address(url.host):
        return "not a public address"
    return None


# How many redirects one fetch follows; aiohttp's own limit was ten. On
# 2026-11-06 no listing, API or card the integration reads answered with
# more than one: energie.be's document API sends each card on to its
# storage, and Eneco's, EBEM's and Brusol's pages moved. Five leaves room
# for a CDN in between.
MAX_REDIRECTS = 5
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


@asynccontextmanager
async def checked_request(
    session: aiohttp.ClientSession,
    url: str,
    *,
    timeout: float,
    head: bool = False,
    params: Mapping[str, str] | None = None,
    headers: Mapping[str, str] | None = None,
) -> AsyncIterator[aiohttp.ClientResponse]:
    """GET ``url``, or HEAD it, and yield the answer, following a redirect
    only once :func:`_refusal` has let its address through.

    aiohttp sends the request on to wherever a redirect points before its
    caller can see where that is, so checking the answer afterwards was too
    late: the LAN device had already been asked, path, query and login
    included. Each hop is therefore asked on its own, with aiohttp's
    redirects off, and the next one is checked before it is asked. A
    redirect may leave the site it came from: energie.be's document API on
    azurewebsites.net answers with one to its storage on
    blob.core.windows.net.

    The hops share ``timeout``, as they did when aiohttp followed them, and
    a chain longer than :data:`MAX_REDIRECTS` raises aiohttp's
    TooManyRedirects as its own limit did, so a caller's handling of a
    timeout or a client error holds as it was. A refused address raises an
    ExtractorError naming ``url``; where the redirect pointed goes to the
    debug log only, since that can be a device on the LAN.
    """
    try:
        refused = _refusal(URL(url))
    except ValueError:
        refused = "not an address"
    if refused is not None:
        raise ExtractorError(f"refusing {shown_text(url)}: {refused}")
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    send = session.head if head else session.get
    target: str | URL = url
    history: list[aiohttp.ClientResponse] = []
    while True:
        left = deadline - loop.time()
        if left <= 0:
            raise TimeoutError
        async with send(
            target,
            params=params,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=left),
            allow_redirects=False,
        ) as resp:
            location = (
                resp.headers.get("Location")
                if resp.status in _REDIRECT_STATUSES
                else None
            )
            if not location:
                yield resp
                return
            history.append(resp)
            if len(history) > MAX_REDIRECTS:
                raise aiohttp.TooManyRedirects(history[0].request_info, tuple(history))
            try:
                # Read against the address that answered, as aiohttp reads
                # a relative Location.
                hop = URL(location)
                if not hop.scheme:
                    hop = resp.url.join(hop)
                refused = _refusal(hop)
            except ValueError:
                refused = "not an address"
        if refused is not None:
            _LOGGER.debug("%s redirected to %s", url, location)
            raise ExtractorError(
                f"refusing {shown_text(url)}: its redirect is {refused}"
            )
        # The query was the first address's; the redirect carries its own.
        target, params = hop, None


def _strip_pdf_prefix(payload: bytes) -> bytes:
    """The bytes without a UTF-8 BOM, then blank space, ahead of ``%PDF``.

    Some publishers prepend the BOM (\\ufeff = 3 bytes EF BB BF): OCTA+'s
    tariff PDFs do this, and some servers a newline. Either has to come off
    rather than merely be tolerated: pdfplumber fails such a file with "No
    /Root object! - Is this really a PDF?", which reads like a corrupt card
    rather than a few stray bytes. pypdf recovers on its own, so the two
    aligned/layout variants are the ones this protects.
    """
    return payload.removeprefix(b"\xef\xbb\xbf").lstrip(b"\r\n\t ")


def _is_pdf_payload(payload: bytes) -> bool:
    """Return True if the bytes look like a PDF: ``%PDF`` once
    :func:`_strip_pdf_prefix` has taken off what may precede it."""
    return _strip_pdf_prefix(payload).startswith(b"%PDF")


# An object store that refuses the read answers the proxy in front of it
# with its own XML error document, and the proxy passes that through as a
# 200. Azure Blob and S3 both shape it as a root <Error> carrying a <Code>.
_STORAGE_ERROR_RE = re.compile(
    rb"^(?:\xef\xbb\xbf)?\s*(?:<\?xml[^>]*\?>\s*)?<Error>\s*<Code>([^<]{1,64})</Code>",
    re.IGNORECASE,
)


def _storage_error_code(payload: bytes) -> str | None:
    """The error code if ``payload`` is an object-store error document.

    Luminus switched anonymous access off on the storage account behind
    ``api-next/get-pricelist`` on 2026-08-10, and every card came back as
    a 248-byte ``PublicAccessNotPermitted`` document under a 200. Read as
    a plain non-PDF payload that reports as a permanent parse failure, so
    every Luminus entry raised the ``extractor_failed`` card asking the
    user to report a layout change that had not happened. The supplier's
    own download button was broken the same way, and nothing here could
    fix it, which is the definition of transient in this taxonomy.
    """
    match = _STORAGE_ERROR_RE.match(payload)
    if match is None:
        return None
    return match.group(1).decode("ascii", "replace").strip() or None


async def _fetch_validated_pdf_bytes(
    session: aiohttp.ClientSession, url: str, *, timeout: int = 30
) -> bytes:
    """Download ``url`` and return its bytes once validated as a PDF.

    Shared by the three ``fetch_pdf_text*`` variants. Catches TimeoutError
    alongside ClientError: aiohttp's ClientTimeout fires
    asyncio.TimeoutError (== builtins.TimeoutError on 3.11+), which is NOT
    a ClientError subclass, so a slow supplier endpoint would otherwise
    bubble a bare TimeoutError out of discover/fetch and crash the
    live-check. The ``network error fetching`` prefix is load-bearing -
    :func:`is_transient_fetch_error` keys on it to decide whether to retry -
    so only this function and :func:`fetch_text` may write it.
    """
    try:
        async with checked_request(
            session, url, timeout=timeout, headers={"User-Agent": USER_AGENT}
        ) as resp:
            if resp.status >= 400:
                raise ExtractorError(f"HTTP {resp.status} fetching {shown_text(url)}")
            payload = await read_capped(resp, url, MAX_RESPONSE_BYTES)
    except (aiohttp.ClientError, TimeoutError) as err:
        raise ExtractorError(
            f"network error fetching {shown_text(url)}: {error_text(err)}"
        ) from err

    if not _is_pdf_payload(payload):
        # What came instead is the server's to choose, and the message
        # reaches last_error, the Repairs card and the sensors' attributes,
        # so it says what kind of thing came and the bytes go to the log.
        _LOGGER.debug("%s answered %r instead of a PDF", url, payload[:80])
        # An object store refusing the read is the one non-PDF payload no
        # code change can fix, so it gets its own prefix and lands on the
        # transient side of is_transient_fetch_error rather than asking
        # the user to report a layout change.
        code = _storage_error_code(payload)
        if code is not None:
            _LOGGER.debug("%s answered the storage error %s", url, code)
            raise ExtractorError(
                f"storage error fetching {shown_text(url)}: the store refused the read"
            )
        # Some CDNs return 200 + text/html for missing PDFs (a 404
        # disguised as success). Engie's API returns octet-stream
        # for valid PDFs, so checking the magic bytes is more
        # reliable than the Content-Type header.
        got = (
            _A_WEB_PAGE
            if _WEB_PAGE_RE.match(payload)
            else "expected a PDF, got something else"
        )
        raise ExtractorError(f"{got}, at {shown_text(url)}")
    return _strip_pdf_prefix(payload)


async def _pdf_text(
    session: aiohttp.ClientSession,
    url: str,
    *,
    variant: str,
    timeout: int,
    render: Callable[[bytes], str],
) -> str:
    """Download ``url`` and extract its text, or serve both from the memo.

    The three ``fetch_pdf_text*`` variants differ only in ``render``, so the
    memo lookup lives here rather than three times over. Keyed by variant as
    well as URL: the same document read aligned and read layout-preserving are
    different strings, and handing one back for the other would feed a parser
    a shape it has no regexes for.

    Extraction is offloaded to a worker thread: every variant is pure-Python
    parsing, and a multi-page tariff card would otherwise stall Home
    Assistant's event loop.
    """
    memo = _TEXT_MEMO.get()
    key = f"{variant}\0{url}"
    if memo is not None and key in memo:
        return memo[key]
    downloads = _DOWNLOADS.get()
    payload = None if downloads is None else downloads.get(url)
    if payload is None:
        payload = await _fetch_validated_pdf_bytes(session, url, timeout=timeout)
        if downloads is not None:
            downloads[url] = payload
    text = await render_pdf(variant, url, payload, render)
    if memo is not None:
        memo[key] = text
    return text


async def render_pdf(
    variant: str, url: str, payload: bytes, render: Callable[[bytes], str]
) -> str:
    """Turn validated PDF bytes into text, through the render hook when one
    is installed and in a worker thread otherwise.

    The readers call it after their download. A provider that receives a
    card some other way calls it too: OCTA+'s archive hands the card over
    base64 inside a JSON answer, and rendering those bytes directly would
    keep the card archiver, which listens on the hook, from ever seeing
    them. ``url`` is whatever names the card for that provider.
    """
    hook = _RENDER_HOOK.get()
    if hook is None:
        return await render_in_thread(render, payload)
    return await hook(variant, url, payload, render)


async def fetch_pdf_text(
    session: aiohttp.ClientSession, url: str, *, timeout: int = 30
) -> str:
    """Download ``url`` and return the concatenated extracted text."""
    return await _pdf_text(
        session, url, variant="plain", timeout=timeout, render=extract_pdf_text
    )


async def fetch_pdf_text_aligned(
    session: aiohttp.ClientSession,
    url: str,
    x_join_threshold: float = 0.0,
    *,
    timeout: int = 30,
) -> str:
    """Word-coordinate aligned variant of :func:`fetch_pdf_text`."""
    return await _pdf_text(
        session,
        url,
        # The threshold changes the output, so it belongs in the memo key: two
        # callers reading one card at different thresholds want two strings.
        variant=f"aligned:{x_join_threshold}",
        timeout=timeout,
        render=lambda payload: extract_pdf_text_aligned(payload, 3, x_join_threshold),
    )


async def fetch_pdf_text_layout(
    session: aiohttp.ClientSession, url: str, *, timeout: int = 30
) -> str:
    """Layout-preserving variant of :func:`fetch_pdf_text`.

    Some CDNs return HTTP 200 with ``text/html`` for missing PDFs (404
    pages disguised as success). We treat those as fetch failures so the
    parser never tries to read a PDF that isn't.
    """
    return await _pdf_text(
        session, url, variant="layout", timeout=timeout, render=extract_pdf_text_layout
    )


async def head_freshness_key(
    session: aiohttp.ClientSession,
    url: str,
    *,
    prefer: tuple[str, ...] = ("Last-Modified", "ETag"),
) -> str | None:
    """HEAD ``url`` and return the first present header from ``prefer``.

    Used as a cheap freshness probe by suppliers whose tariff cards live
    behind a CDN that honours ``If-Modified-Since`` / ``If-None-Match``.
    Returns ``None`` on any 4xx/5xx, network error, or when none of the
    preferred headers are populated; the coordinator treats ``None`` as
    "no signal" and falls back to its time-based TTL.

    EnergyVision passes ``prefer=("ETag", "Last-Modified")`` for the
    listing page it probes; every other supplier keeps the default order.

    Checked hop by hop like the fetches (:func:`checked_request`): an
    address refused there, the first or one a redirect points to, is no
    signal either. Aspiravi probes the links its listing prints.
    """
    try:
        async with checked_request(
            session, url, head=True, timeout=10, headers={"User-Agent": USER_AGENT}
        ) as resp:
            if resp.status >= 400:
                return None
            for key in prefer:
                value = resp.headers.get(key)
                if value:
                    return value
            return None
    except (aiohttp.ClientError, TimeoutError, ExtractorError):
        # aiohttp's ClientTimeout fires asyncio.TimeoutError (==
        # builtins.TimeoutError on 3.11+), which is NOT a ClientError;
        # without this a slow HEAD would break the documented
        # None-on-failure contract and bubble out of the probe path.
        # ExtractorError is checked_request refusing an address.
        return None


async def head_or_raise(
    session: aiohttp.ClientSession, url: str, *, timeout: int = 10
) -> str | None:
    """HEAD ``url`` and raise the way the fetch helpers do when it fails.

    Returns the answer's ``Last-Modified``, or None when it sends none, for
    a lookup that has to tell one upload of a file from a later one.

    ``HTTP <status>`` at 400 and above, ``network error fetching`` on a
    client error or a timeout (the bare ``TimeoutError`` aiohttp's total
    ``ClientTimeout`` raises included), so ``is_transient_fetch_error`` can
    tell a card that is not there from a supplier that is down. For a lookup
    that probes before it downloads: Eneco tries five volumes a month, and
    a probe folding both cases into "not there" had a CDN blip cached as a
    month with no archive.

    Checked hop by hop like the fetches, by :func:`checked_request`.
    """
    try:
        async with checked_request(
            session,
            url,
            head=True,
            timeout=timeout,
            headers={"User-Agent": USER_AGENT},
        ) as resp:
            if resp.status >= 400:
                raise ExtractorError(f"HTTP {resp.status} fetching {shown_text(url)}")
            return resp.headers.get("Last-Modified")
    except (aiohttp.ClientError, TimeoutError) as err:
        raise ExtractorError(
            f"network error fetching {shown_text(url)}: {error_text(err)}"
        ) from err


async def head_ok(
    session: aiohttp.ClientSession, url: str, *, timeout: int = 10
) -> bool:
    """Whether ``url`` answers a HEAD probe with a non-error status.

    A small existence check for ``discover()`` paths that only need to
    know the card still resolves. ``False`` on any HTTP >= 400, on a
    network failure or off https, which is every case
    :func:`head_or_raise` raises for.
    """
    try:
        await head_or_raise(session, url, timeout=timeout)
    except ExtractorError:
        return False
    return True


def vat_multiplier(
    text: str,
    *patterns: str | re.Pattern[str],
    default: float = 1.06,
) -> float:
    """Read the VAT percentage from a card header and return 1 + N/100.

    Each supplier's tariff card prints the rate in a different phrasing
    ("Tarifs 6% TVAC", "TVA 6%", "6% de TVA comprise", ...); every
    extractor passes its own ``patterns``. The helper picks the first
    match across the list and converts the captured group via
    :func:`to_float` so cards that print a fractional rate (e.g.
    ``"21,5%"``) work without per-provider parsing.

    Falls back to ``default`` (1.06, the current Belgian residential rate)
    when none of the patterns match. Most cards ship VAT-inclusive figures
    or print the rate, so the value rarely matters; where a card prints
    ex-VAT coefficients and no rate at all (every residential Bolt card) the
    fallback is what the entry bills on, and such a caller names it.
    """
    rate = printed_vat_rate(text, *patterns)
    return default if rate is None else 1.0 + rate


def printed_vat_rate(text: str, *patterns: str | re.Pattern[str]) -> float | None:
    """The VAT rate a card states, as a fraction, or ``None`` where none of
    ``patterns`` matches.

    What :func:`vat_multiplier` reads, without its fallback: a parser records
    whether the rate it grossed by was read or assumed (``TaxOverlay``'s
    ``card_vat_rate`` and ``assumed_vat_rate``), and the default hides that.
    Group 1 of the first pattern that matches is the percentage.
    """
    for pattern in patterns:
        match = (
            re.search(pattern, text)
            if isinstance(pattern, str)
            else pattern.search(text)
        )
        if match:
            return to_float(match.group(1)) / 100.0
    return None


# A caller that will read the same document many times in quick succession can
# ask fetch_text and the PDF readers to serve repeats from memory. Off by
# default: every existing caller wants a live read, and a global time-based
# cache would quietly hand a coordinator tick a stale page. This is opt-in,
# explicit, and scoped to the block that entered it.
_TEXT_MEMO: ContextVar[dict[str, str] | None] = ContextVar("_TEXT_MEMO", default=None)


@contextmanager
def memoise_text_fetches(store: dict[str, str]) -> Iterator[None]:
    """Serve repeat reads of one URL from ``store`` inside this block.

    Two kinds of repeat, both of them a sweep pricing several products of one
    supplier:

    Listing pages. Nine providers resolve a per-supplier listing inside
    ``fetch()`` and then pick one product out of it, so pricing a whole
    supplier re-downloads the same page once per contract: a Flanders static
    sweep pulls Mega's listing nine times, Engie's eight and Luminus's eight,
    about 3 MB and 25 round trips that buy nothing.

    Tariff cards. Where two products share one document the sweep parsed it
    twice, and the parse is the expensive half, not the download: a Bolt
    variable card is 2,4 MB and takes about 38 s through pdfplumber on a
    Raspberry Pi against well under a second to fetch. Bolt sells each of its
    four variable cards on both settlements, so a Flanders sweep was spending
    four of those parses on text it already held, against a 120 s budget.

    What is memoised is the EXTRACTED TEXT, not the bytes. It is the parse
    that is worth skipping, and a card's text is a few kB against a couple of
    MB of payload, so a whole sweep's worth stays small enough to hold.

    ``store`` is passed in rather than created here so a caller can share one
    memo across several tasks - an ``asyncio.Task`` copies the context at
    creation, which copies the reference and not the dict, so every candidate
    in a sweep sees what the first one fetched.

    Deliberately not a TTL cache inside the fetchers: the coordinator and the
    one-off quote both want a live read, and the failure mode of guessing a
    TTL for them is a stale card nobody asked for.
    """
    token = _TEXT_MEMO.set(store)
    try:
        yield
    finally:
        _TEXT_MEMO.reset(token)


# The bytes of each card downloaded inside a ``download_each_card_once``
# block, by URL: opt-in and scoped like the memo above.
_DOWNLOADS: ContextVar[dict[str, bytes] | None] = ContextVar("_DOWNLOADS", default=None)


@contextmanager
def download_each_card_once() -> Iterator[None]:
    """Download each card once inside this block, however many ways it is
    read.

    energie.be's November 2026 template needs two readings of every card,
    laid out for its tables and plain for its sentences, and each reading
    downloaded the card again, about a megabyte more on every fetch. The
    text memo above cannot spare that: it holds text, not bytes, and a
    coordinator tick runs outside it. A reading the memo serves downloads
    nothing, as before.
    """
    token = _DOWNLOADS.set({})
    try:
        yield
    finally:
        _DOWNLOADS.reset(token)


# How a downloaded card becomes text, when someone other than the readers
# wants a say. The card archiver (scripts/archive_cards.py) downloads every
# card daily but keys the render on the bytes' hash, so a card that has not
# changed since it was last stored is neither rendered nor parsed again, and
# it keeps the bytes it has not seen before. None, the default everywhere in
# Home Assistant, renders in a worker thread as always.
RenderHook = Callable[[str, str, bytes, Callable[[bytes], str]], Awaitable[str]]
_RENDER_HOOK: ContextVar[RenderHook | None] = ContextVar("_RENDER_HOOK", default=None)


@contextmanager
def render_through(hook: RenderHook) -> Iterator[None]:
    """Route every PDF render inside this block through ``hook``.

    The hook receives the reader variant, the URL, the validated bytes and
    the renderer the reader would have used, and returns the text. Scoped
    to the block, like the text memo above, so nothing in the integration
    itself can end up on this path.
    """
    token = _RENDER_HOOK.set(hook)
    try:
        yield
    finally:
        _RENDER_HOOK.reset(token)


async def fetch_text(
    session: aiohttp.ClientSession,
    url: str,
    *,
    params: dict[str, str] | None = None,
    timeout: int = 20,
    limit: int = MAX_TEXT_BYTES,
) -> str:
    """GET ``url`` and return the response body as text.

    Raises :class:`ExtractorError` on any HTTP non-2xx, network error,
    or aiohttp client failure. Use for HTML listing / index pages and
    other plain-text sources; reach for :func:`fetch_pdf_text` (or its
    layout / aligned variants) when the body is expected to be a PDF.

    ``params`` is passed straight to :meth:`aiohttp.ClientSession.get`
    for endpoints that carry their query in the URL string (e.g. a
    Sanity CMS ``query``). ``limit`` caps the body
    (:func:`read_text_capped`): a caller whose document is known to be
    small passes a tighter one, as the card archive's rows and tables do.

    Callers that prefer a soft None-on-failure can wrap this in a
    ``try / except ExtractorError`` block; concentrating the network /
    HTTP error handling here keeps the ~6 lines of boilerplate out of
    every provider, and routes every fetch through the one
    :func:`is_transient_fetch_error` taxonomy so transient failures
    retry uniformly.
    """
    memo = _TEXT_MEMO.get()
    # Keyed on the full request, not the bare URL: one endpoint answers
    # different questions by query string (Frank's Sanity GROQ, for one).
    memo_key = url if not params else f"{url}?{sorted(params.items())}"
    if memo is not None and memo_key in memo:
        return memo[memo_key]
    try:
        async with checked_request(
            session,
            url,
            timeout=timeout,
            params=params,
            headers={"User-Agent": USER_AGENT},
        ) as resp:
            if resp.status >= 400:
                raise ExtractorError(f"HTTP {resp.status} fetching {shown_text(url)}")
            body = await read_text_capped(resp, url, limit=limit)
            # Only a success is memoised. A failure is re-attempted by the
            # next caller, which is what the negative cache one layer up is
            # for; caching it here would give it a second, untracked lifetime.
            if memo is not None:
                memo[memo_key] = body
            return body
    except (aiohttp.ClientError, TimeoutError) as err:
        raise ExtractorError(
            f"network error fetching {shown_text(url)}: {error_text(err)}"
        ) from err


# One capture group around a plain decimal, with NO thousands separator.
# Named for that constraint on purpose: a card whose values run into four
# digits needs eneco's wider pattern instead, and reusing this one there
# truncates the value to its first digits (recorded beside eneco's _NUM).
NUM_NO_THOUSANDS = r"([\d]+(?:[.,][\d]+)?)"


# Full month names in calendar order (index 0 == January). The single
# source of truth for the per-supplier archive-validity checks, which
# match a card's spelled-out month against ``month_names[month - 1]``.
# Suppliers import the tuple for their card's language rather than
# re-listing the twelve names (and drifting on accents); dict-shaped
# lookups derive from these with ``enumerate(.., 1)``.
NL_MONTHS: tuple[str, ...] = (
    "januari",
    "februari",
    "maart",
    "april",
    "mei",
    "juni",
    "juli",
    "augustus",
    "september",
    "oktober",
    "november",
    "december",
)
FR_MONTHS: tuple[str, ...] = (
    "janvier",
    "février",
    "mars",
    "avril",
    "mai",
    "juin",
    "juillet",
    "août",
    "septembre",
    "octobre",
    "novembre",
    "décembre",
)


# Month names recognised in publication strings, mapped to their 1-12
# index. Each language's full name + a few common abbreviations Belgian
# tariff cards use. The lookup key is lowercase, accent-stripped not
# guaranteed (we accept both forms explicitly).
_MONTH_NAMES: dict[str, int] = {
    # Dutch
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
    # French (with and without accents)
    "janvier": 1,
    "fevrier": 2,
    "février": 2,
    "mars": 3,
    "avril": 4,
    "mai": 5,
    "juin": 6,
    "juillet": 7,
    "aout": 8,
    "août": 8,
    "septembre": 9,
    "octobre": 10,
    "novembre": 11,
    "decembre": 12,
    "décembre": 12,
    # English (some cards mix languages on cross-region documents).
    # april / september / november / december share their spelling
    # with Dutch and are already registered above; repeating them
    # here would trip ruff's F601 (duplicate dict key literal), so
    # the English block lists only the names that actually differ.
    "january": 1,
    "february": 2,
    "march": 3,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "october": 10,
}


_MONTH_YEAR_RE = re.compile(r"\b([A-Za-z]+)\s+(20\d{2})\b")


_VALID_KEYWORDS = ("geldig", "valable", "validit", "valid ")

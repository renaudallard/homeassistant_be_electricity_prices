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

"""Tests for the PDF parsing helpers in providers/_pdf.py."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Sequence
from datetime import date
from types import SimpleNamespace

import aiohttp
import pytest

from custom_components.be_electricity_prices.providers import _pdf, _pdf_bound
from custom_components.be_electricity_prices.providers._parse import (
    numeric_row,
    parse_brussels_osp,
    parse_sign,
)
from custom_components.be_electricity_prices.providers._pdf import (
    _MIN_TEXT_LAYER_CHARS,
    MAX_RESPONSE_BYTES,
    _fetch_validated_pdf_bytes,
    extract_pdf_text,
    extract_pdf_text_aligned,
    extract_pdf_text_layout,
    fetch_pdf_text,
    fetch_text,
    head_freshness_key,
    head_ok,
    head_or_raise,
    is_missing_card_error,
    is_transient_fetch_error,
    vat_multiplier,
)
from custom_components.be_electricity_prices.providers._rates import FixedRates
from custom_components.be_electricity_prices.providers._validity import (
    archive_validity_check,
    parse_valid_until,
    text_mentions_month,
)
from custom_components.be_electricity_prices.providers.base import (
    CardNotReadableError,
    DsoOverlay,
    ExtractorError,
    SupplierSnapshot,
    TaxOverlay,
)
from tests import FakeBody


class _PdfResponse:
    status = 200
    content_length = None
    history = ()

    def __init__(self, payload: bytes) -> None:
        self.content = FakeBody(payload)

    async def __aenter__(self) -> _PdfResponse:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None


class _PdfSession:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def get(self, _url: str, **_kw: object) -> _PdfResponse:
        return _PdfResponse(self.payload)


async def test_render_pdf_is_the_seam_for_bytes_that_came_another_way() -> None:
    """A provider handed a card outside a reader (OCTA+'s archive, base64 in
    JSON) renders it through the same hook the readers use; without a hook
    it renders in a thread like them."""
    seen: list[tuple[str, str, bytes]] = []

    async def hook(
        variant: str, url: str, payload: bytes, renderer: Callable[[bytes], str]
    ) -> str:
        seen.append((variant, url, payload))
        return "hooked"

    with _pdf.render_through(hook):
        assert (
            await _pdf.render_pdf("aligned", "u", b"%PDF x", lambda p: "rendered")
            == "hooked"
        )
    assert seen == [("aligned", "u", b"%PDF x")]
    assert (
        await _pdf.render_pdf("aligned", "u", b"%PDF x", lambda p: "rendered")
        == "rendered"
    )


async def test_render_hook_sees_the_bytes_and_decides_the_text() -> None:
    """Inside render_through the reader hands the validated bytes and its
    own renderer to the hook and takes the hook's text; outside it renders
    as always. Either way the memo stores what came back."""
    rendered: list[bytes] = []

    def render(payload: bytes) -> str:
        rendered.append(payload)
        return "rendered"

    session = _PdfSession(b"%PDF-1.4 card")
    seen: list[tuple[str, str, bytes]] = []

    async def hook(
        variant: str, url: str, payload: bytes, renderer: Callable[[bytes], str]
    ) -> str:
        seen.append((variant, url, payload))
        assert renderer is render
        return "from the hook"

    store: dict[str, str] = {}
    with _pdf.memoise_text_fetches(store), _pdf.render_through(hook):
        text = await _pdf._pdf_text(
            session,  # type: ignore[arg-type]
            "https://acme.test/card.pdf",
            variant="plain",
            timeout=5,
            render=render,
        )
    assert text == "from the hook"
    assert seen == [("plain", "https://acme.test/card.pdf", b"%PDF-1.4 card")]
    assert rendered == []
    assert store == {"plain\0https://acme.test/card.pdf": "from the hook"}
    # Outside the block the reader renders itself.
    text = await _pdf._pdf_text(
        session,  # type: ignore[arg-type]
        "https://acme.test/card.pdf",
        variant="plain",
        timeout=5,
        render=render,
    )
    assert text == "rendered"
    assert rendered == [b"%PDF-1.4 card"]


def test_parse_valid_until_dutch_geldig_van_tem() -> None:
    """Eneco / Mega Dutch wording -- date inside a 'Geldig...' window."""
    text = "Geldig van 1 april 2026 t.e.m 30 april 2026."
    assert parse_valid_until(text) == date(2026, 4, 30)


def test_parse_valid_until_french_valable_jusqu_au() -> None:
    text = "Cette carte tarifaire est valable jusqu'au 30 avril 2026."
    assert parse_valid_until(text) == date(2026, 4, 30)


def test_parse_valid_until_french_mai() -> None:
    """Regression: French May (mai) must be in _MONTH_NAMES so a card
    whose validity is spelled out only in French ("valable jusqu'au
    31 mai 2026") parses its actual end date instead of falling
    through to None."""
    text = "Cette carte tarifaire est valable jusqu'au 31 mai 2026."
    assert parse_valid_until(text) == date(2026, 5, 31)


def test_parse_valid_until_numeric_dd_mm_yyyy() -> None:
    text = "Validité: du 01/04/2026 au 30/04/2026."
    assert parse_valid_until(text) == date(2026, 4, 30)


def test_parse_valid_until_numeric_accepts_dash_and_dot_separators() -> None:
    """Legal-style date publications occasionally use dashes or dots
    instead of slashes (DD-MM-YYYY, DD.MM.YYYY). No supplier in the
    registry currently does, but the cost of supporting them is one
    regex character class and avoids a future fragility."""
    assert parse_valid_until("validité: 30-04-2026") == date(2026, 4, 30)
    assert parse_valid_until("validité: 30.04.2026") == date(2026, 4, 30)


def test_parse_valid_until_picks_latest_in_window() -> None:
    """Both validity-start and validity-end appear in one statement;
    the helper must return the end (latest)."""
    text = "valable du 1 mars 2026 au 30 avril 2026."
    assert parse_valid_until(text) == date(2026, 4, 30)


def test_parse_valid_until_ignores_unrelated_dates() -> None:
    """Dates that don't sit inside a validity-keyword window must be
    ignored. Without this gate the parser used to mistake long-term
    contract dates (e.g. compensation-regime end 2030-12-31) for
    pricing validity."""
    text = (
        "Le regime de compensation reste applicable jusqu'au 2030-12-31. "
        "Le tarif est valable du 1 avril 2026 au 30 avril 2026."
    )
    assert parse_valid_until(text) == date(2026, 4, 30)


def test_parse_valid_until_accepts_bare_month_year_inside_window() -> None:
    """Card with no day in the validity statement -- treat the month
    as fully covered."""
    text = "Tariefkaart april 2026, geldig in april 2026."
    assert parse_valid_until(text) == date(2026, 4, 30)


def test_parse_valid_until_returns_none_when_no_keyword_present() -> None:
    """The card lacks any validity wording entirely -- caller must
    fall back to "treat as available", so we return None."""
    assert parse_valid_until("Eneco residential 100% green energy.") is None


def test_parse_valid_until_returns_none_for_keyword_without_date() -> None:
    text = "Cette tarification est valable conformement aux conditions generales."
    assert parse_valid_until(text) is None


def test_parse_valid_until_clamps_implausible_far_future_year() -> None:
    """A captured 4-digit year that's centuries away (typically a
    chunk of a corrupted phone number / fax that happens to look
    like DD/MM/YYYY inside a validity window) must not bubble out as
    a real validity date.
    """
    text = "Cette carte est valable du 01/04/2026 au 30/04/2625."
    # The 2026 candidate is the only one that survives the year clamp.
    assert parse_valid_until(text) == date(2026, 4, 1)


def test_parse_valid_until_rejects_retrospective_only_window() -> None:
    """A pathological card whose validity window mentions only dates
    older than today.year - 5 must not surface a misleading valid_until
    -- archive_validity_check would otherwise accept the snapshot for
    the wrong month. Synthesise a window with only a 2010 date range
    and confirm None is returned."""
    text = "valable du 1 janvier 2010 au 31 decembre 2010."
    assert parse_valid_until(text) is None


def test_parse_valid_until_accepts_archive_year_within_5_years() -> None:
    """fetch_for_month walks back through Eneco/Cociter archives; a
    card from year-3 (well within the legitimate archive horizon)
    must still parse its printed validity rather than fall through
    to the textual-mention fallback."""
    text = "valable du 1 juin 2023 au 30 juin 2023."
    # Today (test run) - 3 years is in range; assert an actual date,
    # not None. Exact date matches the second numeric of the window.
    assert parse_valid_until(text) == date(2023, 6, 30)


# ---- network-error normalization ----------------------------------------------


class _FakeSession:
    """Minimal aiohttp.ClientSession stand-in whose .get() raises on entry."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def get(self, *_args: object, **_kwargs: object) -> _FakeCtx:
        return _FakeCtx(self._exc)


class _FakeCtx:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def __aenter__(self) -> object:  # pragma: no cover - re-raises immediately
        raise self._exc

    async def __aexit__(self, *_exc: object) -> None:
        return None


async def test_fetch_text_converts_timeout_to_extractor_error() -> None:
    """aiohttp's ClientTimeout fires asyncio.TimeoutError, which is NOT
    a ClientError. Without explicit catching the bare TimeoutError
    bubbles out of every supplier's discover/fetch and crashes the
    live-check (regression: engie/catalog 2026-05-05)."""
    session = _FakeSession(TimeoutError())
    with pytest.raises(ExtractorError, match="network error"):
        await fetch_text(session, "https://example.com/")  # type: ignore[arg-type]


async def test_fetch_pdf_text_converts_timeout_to_extractor_error() -> None:
    session = _FakeSession(TimeoutError())
    with pytest.raises(ExtractorError, match="network error"):
        await fetch_pdf_text(session, "https://example.com/x.pdf")  # type: ignore[arg-type]


async def test_fetch_text_still_converts_aiohttp_client_errors() -> None:
    """Regression guard: the broader except tuple must still catch
    plain ClientError so a network-down case doesn't bubble either."""
    session = _FakeSession(aiohttp.ClientConnectionError("dns failed"))
    with pytest.raises(ExtractorError, match="network error"):
        await fetch_text(session, "https://example.com/")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "fetcher",
    [
        lambda s: fetch_text(s, "https://example.com/x"),
        lambda s: fetch_pdf_text(s, "https://example.com/x"),
    ],
)
async def test_blank_stringifying_exception_is_named_not_dropped(
    fetcher: object,
) -> None:
    """aiohttp raises its timeouts argless, so ``str(err)`` is empty. Both
    wrap sites must name the class instead of ending in a bare colon: this
    message is shown to users on the snapshot_stale Repairs card, as the
    current_price ``last_error`` attribute, and in diagnostics.

    Asserted by exact equality, not a prefix match, so the tail cannot
    silently regress to empty again.
    """
    session = _FakeSession(TimeoutError())
    with pytest.raises(ExtractorError) as excinfo:
        await fetcher(session)  # type: ignore[operator]
    assert str(excinfo.value) == (
        "network error fetching https://example.com/x: TimeoutError"
    )
    # The "network error fetching" prefix is load-bearing: the coordinator and
    # the live-check both key retry behaviour on it.
    assert is_transient_fetch_error(str(excinfo.value))


class _FakeBodySession:
    """Session stand-in whose .get() answers 200 with a fixed body."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    def get(self, *_args: object, **_kwargs: object) -> _FakeBodyCtx:
        return _FakeBodyCtx(self._body)


class _FakeBodyCtx:
    def __init__(self, body: bytes) -> None:
        self._body = body

    async def __aenter__(self) -> _FakeBodyResp:
        return _FakeBodyResp(self._body)

    async def __aexit__(self, *_exc: object) -> None:
        return None


class _FakeBodyResp:
    def __init__(self, body: bytes) -> None:
        self.status = 200
        self.content_length = len(body)
        self.history = ()
        self.content = FakeBody(body)


_AZURE_ERROR_BODY = (
    b'\xef\xbb\xbf<?xml version="1.0" encoding="utf-8"?><Error>'
    b"<Code>PublicAccessNotPermitted</Code><Message>Public access is not "
    b"permitted on this storage account.\nRequestId:09de2d10\n</Message></Error>"
)

_S3_ERROR_BODY = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n'
    b"<Error><Code>AccessDenied</Code><Message>Access Denied</Message></Error>"
)


@pytest.mark.parametrize(
    ("body", "code"),
    [
        (_AZURE_ERROR_BODY, "PublicAccessNotPermitted"),
        (_S3_ERROR_BODY, "AccessDenied"),
    ],
)
async def test_object_store_error_document_is_transient_not_a_parse_failure(
    body: bytes, code: str
) -> None:
    """An object store that refuses the read answers through the proxy as a
    200 carrying its own XML error document. Read as a plain non-PDF payload
    it reported as a permanent parse failure, so every entry on that supplier
    raised the extractor_failed card asking the user to report a layout change
    that had not happened (Luminus, 2026-08-10). No code change can fix it,
    which is what transient means in this taxonomy.
    """
    session = _FakeBodySession(body)
    with pytest.raises(ExtractorError) as excinfo:
        await _fetch_validated_pdf_bytes(session, "https://x/card.pdf")  # type: ignore[arg-type]
    assert str(excinfo.value) == f"storage error fetching https://x/card.pdf: {code}"
    assert is_transient_fetch_error(str(excinfo.value))


async def test_a_disguised_404_page_is_still_a_permanent_parse_failure() -> None:
    """Narrowness guard for the check above: a CDN serving an HTML page in
    place of a withdrawn PDF must keep failing fast. Classing every non-PDF
    payload as transient would hide a card that was renamed or pulled.
    """
    session = _FakeBodySession(b"<!DOCTYPE html><html><title>404</title></html>")
    with pytest.raises(ExtractorError) as excinfo:
        await _fetch_validated_pdf_bytes(session, "https://x/card.pdf")  # type: ignore[arg-type]
    assert str(excinfo.value).startswith("expected a PDF at https://x/card.pdf")
    assert not is_transient_fetch_error(str(excinfo.value))


@pytest.mark.parametrize(
    ("body", "missing"),
    [
        # Mega's CDN past a professional card's grace days, 2026-10-06.
        (b'<!DOCTYPE html>\n<html lang="fr"><head>', True),
        (b"\n\n  <html><body>Page not found</body></html>", True),
        # Not a page: a broken download is not an absent card.
        (b"GIF89a\x01\x00\x01\x00", False),
    ],
)
async def test_a_web_page_where_the_card_should_be_is_a_missing_card(
    body: bytes, missing: bool
) -> None:
    """Mega answers an unpublished card with 200 and an HTML page rather than
    a 404. Read as a layout change it raised the card asking the user to
    report one, where the card was simply not there.
    """
    session = _FakeBodySession(body)
    with pytest.raises(ExtractorError) as excinfo:
        await _fetch_validated_pdf_bytes(session, "https://x/card.pdf")  # type: ignore[arg-type]
    assert is_missing_card_error(str(excinfo.value)) is missing


async def test_an_error_element_inside_an_html_page_is_not_a_storage_error() -> None:
    """The match is anchored at the start of the payload: a page that merely
    mentions an <Error><Code> block is not the store refusing the read.
    """
    session = _FakeBodySession(b"<html><body><Error><Code>x</Code></body></html>")
    with pytest.raises(ExtractorError, match="expected a PDF"):
        await _fetch_validated_pdf_bytes(session, "https://x/card.pdf")  # type: ignore[arg-type]


async def test_non_empty_exception_message_is_passed_through() -> None:
    """The class name is a fallback only; a real message must survive
    verbatim so a DNS or reset reason is not replaced by its type."""
    session = _FakeSession(aiohttp.ClientConnectionError("dns failed"))
    with pytest.raises(ExtractorError) as excinfo:
        await fetch_text(session, "https://example.com/x")  # type: ignore[arg-type]
    assert str(excinfo.value) == (
        "network error fetching https://example.com/x: dns failed"
    )


# ---- vat_multiplier ----------------------------------------------------------


def test_vat_multiplier_single_pattern_matches() -> None:
    """Single-pattern call: the helper returns 1 + N/100 from the
    capture group when the pattern matches."""
    assert vat_multiplier(
        "Tarifs 6% TVAC", r"Tarifs\s+(\d+)\s*%\s*TVAC"
    ) == pytest.approx(1.06)
    assert vat_multiplier("TVA 21%", r"TVA\s*(\d+)\s*%") == pytest.approx(1.21)


def test_vat_multiplier_returns_default_when_pattern_misses() -> None:
    """No match falls back to the default (1.06 unless overridden)."""
    assert vat_multiplier("nothing relevant", r"TVA\s*(\d+)\s*%") == pytest.approx(1.06)
    assert vat_multiplier(
        "nothing relevant", r"TVA\s*(\d+)\s*%", default=1.21
    ) == pytest.approx(1.21)


def test_vat_multiplier_multi_pattern_falls_back_to_second() -> None:
    """Luminus-style two-regex fallback: if the primary doesn't match,
    the secondary is tried before the default. Verify both branches."""
    primary = re.compile(r"TVA\s*sur\s*les\s*prix.+?(\d+)\s*%", re.S)
    secondary = r"TVA\s*(\d+)\s*%"
    # Primary matches: secondary irrelevant.
    assert vat_multiplier(
        "TVA sur les prix de l'energie 21 %", primary, secondary
    ) == pytest.approx(1.21)
    # Only secondary matches.
    assert vat_multiplier("TVA 6%", primary, secondary) == pytest.approx(1.06)
    # Neither matches: default fires.
    assert vat_multiplier("nothing", primary, secondary) == pytest.approx(1.06)


def test_vat_multiplier_handles_decimal_capture() -> None:
    """Octa+'s pattern allows a fractional rate (e.g. ``21,5%``).
    The helper routes the capture through to_float so a card with a
    decimal VAT parses correctly."""
    pattern = r"Tarifs\s+(\d+(?:[.,]\d+)?)\s*%\s*TVAC"
    assert vat_multiplier("Tarifs 21,5% TVAC", pattern) == pytest.approx(1.215)


# ---- archive_validity_check --------------------------------------------------


def _stub_snapshot(valid_until: date | None) -> SupplierSnapshot:
    return SupplierSnapshot(
        supplier="test",
        contract="test",
        energy=FixedRates(single=0.18),
        dsos={"ores": DsoOverlay(distribution_single=0.10, transport=0.0145)},
        taxes=TaxOverlay(federal_excise=0.05, energy_contribution=0.002),
        source_url="test://",
        valid_until=valid_until,
    )


def test_archive_validity_check_valid_until_in_requested_month_is_accepted() -> None:
    """When valid_until parses cleanly and falls in the requested
    year-month, the snapshot is surfaced as-is."""
    snap = _stub_snapshot(date(2025, 12, 31))
    out = archive_validity_check(snap, "anything", date(2025, 12, 1))
    assert out is snap


def test_archive_validity_check_valid_until_in_other_month_is_rejected() -> None:
    """When valid_until parses but doesn't intersect the requested
    month, the snapshot is rejected even with a textual fallback
    available."""
    snap = _stub_snapshot(date(2025, 12, 31))
    text = "december 2025"  # mentions the wrong month deliberately
    out = archive_validity_check(
        snap, text, date(2025, 3, 1), month_names=("january", "february", "march")
    )
    assert out is None


def test_archive_validity_check_falls_back_to_text_when_valid_until_missing() -> None:
    """No parsed validity, but month_names is provided: require a
    textual mention of the requested month inside the validity window."""
    snap = _stub_snapshot(None)
    months = ("januari", "februari", "maart", "april", "mei", "juni")
    text = "Geldig van 1 mei 2026 t.e.m 31 mei 2026."
    assert (
        archive_validity_check(snap, text, date(2026, 5, 1), month_names=months) is snap
    )
    # Wrong month: the validity window mentions May, not March.
    assert (
        archive_validity_check(snap, text, date(2026, 3, 1), month_names=months) is None
    )


def test_archive_validity_check_no_textual_fallback_when_month_names_none() -> None:
    """EBEM-style: month_names=None means trust the URL resolver. With
    valid_until=None and no textual cross-check available, accept the
    snapshot as-is rather than rejecting on missing validity."""
    snap = _stub_snapshot(None)
    out = archive_validity_check(snap, "no validity here", date(2026, 5, 1))
    assert out is snap


class _FakeResp:
    charset = None

    def __init__(self, content_length: int | None, body: bytes) -> None:
        self.content_length = content_length
        self.content = FakeBody(body)


def test_read_capped_rejects_oversize_declared_length() -> None:
    resp = _FakeResp(MAX_RESPONSE_BYTES + 1, b"%PDF-fake")
    with pytest.raises(ExtractorError, match="declared"):
        asyncio.run(_pdf.read_capped(resp, "https://x/big.pdf"))  # type: ignore[arg-type]


def test_read_capped_allows_normal_and_unknown_length() -> None:
    within = _FakeResp(1024, b"%PDF-1.7 ok")
    assert asyncio.run(_pdf.read_capped(within, "u")) == b"%PDF-1.7 ok"  # type: ignore[arg-type]
    # No Content-Length (streamed) still reads through.
    unknown = _FakeResp(None, b"%PDF-stream")
    assert asyncio.run(_pdf.read_capped(unknown, "u")) == b"%PDF-stream"  # type: ignore[arg-type]


@pytest.mark.parametrize("pdf", [True, False])
async def test_a_body_past_the_cap_is_refused_as_it_streams(
    socket_enabled: None, monkeypatch: pytest.MonkeyPatch, pdf: bool
) -> None:
    """Only a declared Content-Length was checked, and on a PDF alone: a
    chunked answer, or a page of text, was read whole whatever its size."""
    from aiohttp import web
    from aiohttp.test_utils import TestServer

    monkeypatch.setattr(_pdf, "MAX_RESPONSE_BYTES", 1000)
    # The local test server speaks plain http.
    monkeypatch.setattr(_pdf, "_require_https", lambda _url: None)

    async def chunked(request: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse()
        resp.enable_chunked_encoding()
        await resp.prepare(request)
        for _ in range(3):
            await resp.write(b"%PDF" + b"x" * 496)
        await resp.write_eof()
        return resp

    app = web.Application()
    app.router.add_get("/", chunked)
    async with TestServer(app) as server, aiohttp.ClientSession() as session:
        url = str(server.make_url("/"))
        fetch = fetch_pdf_text(session, url) if pdf else fetch_text(session, url)
        with pytest.raises(ExtractorError, match="more than 1000 bytes"):
            await fetch


_BLANK_PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]>>endobj\n"
    b"xref\n0 4\n0000000000 65535 f \n0000000009 00000 n \n"
    b"0000000052 00000 n \n0000000101 00000 n \n"
    b"trailer<</Size 4/Root 1 0 R>>\nstartxref\n164\n%%EOF"
)


def test_pdfplumber_extractors_fail_loud_on_textless_pdf() -> None:
    # A valid PDF with a page but no decodable text must raise rather than
    # return "" and let every downstream regex miss silently (only
    # mandatory fields fail loud; nullable ones would zero).
    #
    # It now raises the more specific CardNotReadableError: zero decoded
    # characters is the extreme of "published as page images", so the two
    # conditions were folded into one check rather than kept as near
    # duplicates. CardNotReadableError IS an ExtractorError, so every
    # existing caller still catches it.
    with pytest.raises(CardNotReadableError, match="no text layer"):
        extract_pdf_text_layout(_BLANK_PDF)
    with pytest.raises(CardNotReadableError, match="no text layer"):
        extract_pdf_text_aligned(_BLANK_PDF)
    assert issubclass(CardNotReadableError, ExtractorError)


def _pdf_from(objects: Sequence[bytes], *, sections: int = 1) -> bytes:
    """A PDF of ``objects``, numbered from 1, the first its catalog, with
    its last object listed again in each of ``sections`` - 1 later
    cross-reference sections chained by /Prev."""
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = 0
    for section in range(sections):
        previous, xref = xref, len(out)
        if section == 0:
            out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
            out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
        else:
            out += b"xref\n%d 1\n%010d 00000 n \n" % (len(objects), offsets[-1])
        out += b"trailer<</Size %d/Root 1 0 R" % (len(objects) + 1)
        if section:
            out += b"/Prev %d" % previous
        out += b">>\n"
    out += b"startxref\n%d\n%%%%EOF\n" % xref
    return bytes(out)


# A font for the page to name: pypdf reads nothing of a page without
# resources.
_FONT_RESOURCES = (
    b"/Resources<</Font<</F1<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>>>>>"
)


def _pdf_of(
    stream: bytes,
    *extra: bytes,
    attrs: bytes = b"/Filter/FlateDecode",
    pages: int = 1,
    contents: bytes = b"3 0 R",
    tree: bool = True,
) -> bytes:
    """A PDF of ``pages`` pages that all draw ``stream``, with ``attrs`` in
    its dictionary, as their content, named ``contents``, and name a font,
    followed by the
    ``extra`` objects, which nothing refers to. Without ``tree`` the catalog
    names no page tree."""
    kids = b" ".join(b"%d 0 R" % (4 + page) for page in range(pages))
    return _pdf_from(
        (
            b"<</Type/Catalog/Pages 2 0 R>>" if tree else b"<</Type/Catalog>>",
            b"<</Type/Pages/Kids[%s]/Count %d>>" % (kids, pages),
            b"<<%s/Length %d>>\nstream\n" % (attrs, len(stream))
            + stream
            + b"\nendstream",
            *(
                b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents %s%s>>"
                % (contents, _FONT_RESOURCES),
            )
            * pages,
            *extra,
        )
    )


def _comment(size: int) -> bytes:
    """A content stream that is one comment ``size`` bytes long, which
    inflates from a few kB."""
    return b"%" + b"A" * size + b"\n"


def _inflating_pdf(
    size: int,
    *extra: bytes,
    attrs: bytes = b"/Filter/FlateDecode",
    pages: int = 1,
    contents: bytes = b"3 0 R",
    tree: bool = True,
) -> bytes:
    """A PDF whose content stream, a comment, inflates to ``size`` bytes."""
    import zlib

    return _pdf_of(
        zlib.compress(_comment(size), 9),
        *extra,
        attrs=attrs,
        pages=pages,
        contents=contents,
        tree=tree,
    )


_EVERY_READER = [extract_pdf_text, extract_pdf_text_layout, extract_pdf_text_aligned]


@pytest.mark.parametrize("extract", _EVERY_READER)
def test_streams_that_inflate_past_the_cap_are_refused_before_the_readers(
    monkeypatch: pytest.MonkeyPatch, extract: Callable[[bytes], str]
) -> None:
    """pdfminer decompresses a stream with no limit, in a worker thread the
    fetch timeout cannot cancel: a 20 kB file inflating to 20 MB took 26 s
    of CPU on a Raspberry Pi, and the download cap does not see it. pypdf
    limits each stream but not their total."""
    monkeypatch.setattr(_pdf_bound, "_MAX_DECODED_STREAM_BYTES", 1 << 20)
    payload = _inflating_pdf(2 << 20)
    assert len(payload) < 10_000
    with pytest.raises(
        ExtractorError, match=r"^PDF streams decode to more than 1048576 bytes"
    ):
        extract(payload)
    # Under the cap the same document goes on to the reader as before.
    with pytest.raises(CardNotReadableError, match="no text layer"):
        extract(_inflating_pdf(1000))


def test_pypdf_reaching_its_own_limit_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pypdf.filters
    from pypdf.errors import LimitReachedError

    def limited(_data: bytes) -> bytes:
        raise LimitReachedError("Limit reached while decompressing.")

    monkeypatch.setattr(pypdf.filters, "decompress", limited)
    with pytest.raises(ExtractorError, match="refusing to read them"):
        extract_pdf_text(_inflating_pdf(1000))


@pytest.mark.parametrize("extract", _EVERY_READER)
def test_a_content_stream_counts_whatever_its_subtype(
    monkeypatch: pytest.MonkeyPatch, extract: Callable[[bytes], str]
) -> None:
    """Images were left out by their /Subtype, and a page's content stream
    tagged /Image escaped the count while pdfminer inflated it all the
    same. An image no page draws as its content is still left out."""
    import zlib

    monkeypatch.setattr(_pdf_bound, "_MAX_DECODED_STREAM_BYTES", 1 << 20)
    image = b"/Subtype/Image/Filter/FlateDecode"
    with pytest.raises(ExtractorError, match=r"^PDF streams decode to more than"):
        extract(_inflating_pdf(2 << 20, attrs=image))
    picture = zlib.compress(b"\0" * (2 << 20), 9)
    unread = b"<<%s/Length %d>>\nstream\n" % (image, len(picture))
    with pytest.raises(CardNotReadableError, match="no text layer"):
        extract(_inflating_pdf(1000, unread + picture + b"\nendstream"))


@pytest.mark.parametrize("extract", _EVERY_READER)
def test_a_content_stream_counts_once_for_each_page_drawing_it(
    monkeypatch: pytest.MonkeyPatch, extract: Callable[[bytes], str]
) -> None:
    """Each page interprets its content again: a 2,7 kB file whose one
    stream served 16 pages kept pdfplumber busy for 100 s, and the stream
    was counted once."""
    monkeypatch.setattr(_pdf_bound, "_MAX_DECODED_STREAM_BYTES", 1 << 20)
    with pytest.raises(ExtractorError, match=r"^PDF streams decode to more than"):
        extract(_inflating_pdf(300_000, pages=4))
    with pytest.raises(CardNotReadableError, match="no text layer"):
        extract(_inflating_pdf(300_000, pages=3))


def _written_into_pages_pdf(stream: bytes, pages: int) -> bytes:
    """``pages`` pages, each with ``stream``, a FlateDecode stream, written
    into it as its content rather than referred to."""
    content = b"<</Filter/FlateDecode/Length %d>>\nstream\n" % len(stream)
    content += stream + b"\nendstream"
    kids = b" ".join(b"%d 0 R" % (3 + page) for page in range(pages))
    return _pdf_from(
        (
            b"<</Type/Catalog/Pages 2 0 R>>",
            b"<</Type/Pages/Kids[%s]/Count %d>>" % (kids, pages),
            *(
                b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents %s%s>>"
                % (content, _FONT_RESOURCES),
            )
            * pages,
        )
    )


def test_a_content_stream_written_into_its_page_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Counted by the objects the cross-reference table lists, a stream
    written into its page counted nothing: a 175 kB file had pypdf decode
    60 MB for its text. pdfminer does not read one."""
    import zlib

    monkeypatch.setattr(_pdf_bound, "_MAX_DECODED_STREAM_BYTES", 1 << 20)
    stream = zlib.compress(_comment(300_000), 9)
    with pytest.raises(ExtractorError, match=r"^PDF streams decode to more than"):
        extract_pdf_text(_written_into_pages_pdf(stream, 4))
    with pytest.raises(CardNotReadableError, match="no text layer"):
        extract_pdf_text(_written_into_pages_pdf(stream, 3))


def _form_pdf(form: bytes, draws: int, fonts: int = 0) -> bytes:
    """One page drawing ``draws`` times a form, ``form`` a FlateDecode
    stream, whose resources name one font ``fonts`` times."""
    names = b"".join(b"/F%d 5 0 R" % name for name in range(fonts))
    return _pdf_from(
        (
            b"<</Type/Catalog/Pages 2 0 R>>",
            b"<</Type/Pages/Kids[4 0 R]/Count 1>>",
            b"<</Length %d>>\nstream\n%s\nendstream"
            % (len(b"/X0 Do\n" * draws), b"/X0 Do\n" * draws),
            b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents 3 0 R"
            b"/Resources<</XObject<</X0 6 0 R>>>>>>",
            b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>",
            b"<</Type/XObject/Subtype/Form/BBox[0 0 612 792]"
            b"/Resources<</Font<<%s>>>>/Filter/FlateDecode/Length %d>>\nstream\n"
            % (names, len(form))
            + form
            + b"\nendstream",
        )
    )


@pytest.mark.parametrize("extract", _EVERY_READER)
def test_a_form_counts_once_for_each_time_it_is_drawn(
    monkeypatch: pytest.MonkeyPatch, extract: Callable[[bytes], str]
) -> None:
    """pypdf interprets a form again each time a page draws it, and it was
    counted once: a 934 B file kept pypdf busy for 77 s. A refusal in a
    form, which pypdf logs and goes on from, still refuses."""
    import zlib

    monkeypatch.setattr(_pdf_bound, "_MAX_DECODED_STREAM_BYTES", 1 << 20)
    form = zlib.compress(_comment(300_000), 9)
    with pytest.raises(ExtractorError, match=r"^PDF streams decode to more than"):
        extract(_form_pdf(form, 4))
    with pytest.raises(CardNotReadableError, match="no text layer"):
        extract(_form_pdf(form, 3))


def _fonts_pdf(pages: int, to_unicode: bytes) -> bytes:
    """``pages`` pages, each with a font of its own, all of them with
    ``to_unicode``, a FlateDecode stream, as their ToUnicode map."""
    kids = b" ".join(b"%d 0 R" % (5 + 2 * page) for page in range(pages))
    return _pdf_from(
        (
            b"<</Type/Catalog/Pages 2 0 R>>",
            b"<</Type/Pages/Kids[%s]/Count %d>>" % (kids, pages),
            b"<</Length 35>>\nstream\nBT /F1 12 Tf 10 50 Td (Hello) Tj ET\nendstream",
            b"<</Filter/FlateDecode/Length %d>>\nstream\n" % len(to_unicode)
            + to_unicode
            + b"\nendstream",
            *(
                part
                for page in range(pages)
                for part in (
                    b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]"
                    b"/Contents 3 0 R/Resources<</Font<</F1 %d 0 R>>>>>>"
                    % (6 + 2 * page),
                    b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica/ToUnicode 4 0 R>>",
                )
            ),
        )
    )


@pytest.mark.parametrize("extract", _EVERY_READER)
def test_a_tounicode_map_counts_each_time_a_font_is_loaded_from_it(
    monkeypatch: pytest.MonkeyPatch, extract: Callable[[bytes], str]
) -> None:
    """A ToUnicode map the fonts of several pages share was counted once and
    parsed for each of them."""
    import zlib

    monkeypatch.setattr(_pdf_bound, "_MAX_DECODED_STREAM_BYTES", 1 << 20)
    to_unicode = zlib.compress(_comment(300_000), 9)
    with pytest.raises(ExtractorError, match=r"^PDF streams decode to more than"):
        extract(_fonts_pdf(4, to_unicode))
    with pytest.raises(CardNotReadableError, match="no text layer"):
        extract(_fonts_pdf(3, to_unicode))


def test_a_font_counts_each_time_pypdf_loads_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pypdf loads every font a form names each time the form is drawn,
    which no stream holds: a 3 kB file naming one font 200 times in a form
    drawn 200 times took 10,8 s, and counted nothing. A load counts, and
    the same dictionary is read to a font once."""
    import zlib

    from pypdf import _font

    monkeypatch.setattr(_pdf_bound, "_MAX_DECODED_STREAM_BYTES", 1 << 20)
    form = zlib.compress(b" ")
    with pytest.raises(ExtractorError, match=r"^PDF streams decode to more than"):
        extract_pdf_text(_form_pdf(form, 11, fonts=100))
    encode = _font.get_encoding
    loads: list[object] = []

    def counted(font: object) -> object:
        loads.append(font)
        return encode(font)  # type: ignore[arg-type]

    monkeypatch.setattr(_font, "get_encoding", counted)
    with pytest.raises(CardNotReadableError, match="no text layer"):
        extract_pdf_text(_form_pdf(form, 9, fonts=100))
    assert len(loads) == 1


def test_pypdf_stops_inflating_at_the_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pypdf inflates a stream up to 75 MB before anything could count it.
    Its inflate stops at what is left of the bound."""
    import tracemalloc

    monkeypatch.setattr(_pdf_bound, "_MAX_DECODED_STREAM_BYTES", 1 << 20)
    payload = _inflating_pdf(32 << 20)
    tracemalloc.start()
    try:
        with pytest.raises(ExtractorError, match=r"^PDF streams decode to more than"):
            extract_pdf_text(payload)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 8 << 20


@pytest.mark.parametrize("extract", [extract_pdf_text_layout, extract_pdf_text_aligned])
def test_a_page_naming_its_content_by_another_generation_counts(
    monkeypatch: pytest.MonkeyPatch, extract: Callable[[bytes], str]
) -> None:
    """pdfminer resolves a reference by its number whatever generation it
    names. Counted by generation and number, a page naming its content
    ``3 1 R`` drew nothing: a content stream tagged /Image escaped the
    count, 200 MiB inflated from a 204 kB file, and a stream four pages
    shared counted once."""
    monkeypatch.setattr(_pdf_bound, "_MAX_DECODED_STREAM_BYTES", 1 << 20)
    image = b"/Subtype/Image/Filter/FlateDecode"
    with pytest.raises(ExtractorError, match=r"^PDF streams decode to more than"):
        extract(_inflating_pdf(2 << 20, attrs=image, contents=b"3 1 R"))
    with pytest.raises(ExtractorError, match=r"^PDF streams decode to more than"):
        extract(_inflating_pdf(300_000, pages=4, contents=b"3 1 R"))
    with pytest.raises(CardNotReadableError, match="no text layer"):
        extract(_inflating_pdf(300_000, pages=3, contents=b"3 1 R"))


def _orphan_page_pdf(stream: bytes, attrs: bytes) -> bytes:
    """A tree pypdf walks to one page and pdfminer to none, its nodes being
    untyped, so pdfminer reads the one object typed as a page instead, which
    draws ``stream``."""
    return _pdf_from(
        (
            b"<</Type/Catalog/Pages 2 0 R>>",
            b"<</Kids[3 0 R]/Count 1>>",
            b"<</Parent 2 0 R/MediaBox[0 0 612 792]/Contents 4 0 R>>",
            b"<</Length 22>>\nstream\nBT /F1 12 Tf (hi) Tj ET\nendstream",
            b"<<%s/Length %d>>\nstream\n" % (attrs, len(stream))
            + stream
            + b"\nendstream",
            b"<</Type/Page/MediaBox[0 0 612 792]/Contents 5 0 R>>",
        )
    )


def _listed_page_pdf(stream: bytes, sections: int) -> bytes:
    """No page tree, and the one page, drawing ``stream``, listed in
    ``sections`` cross-reference sections, each of which pdfminer reads it
    from."""
    return _pdf_from(
        (
            b"<</Type/Catalog>>",
            b"<</Filter/FlateDecode/Length %d>>\nstream\n" % len(stream)
            + stream
            + b"\nendstream",
            b"<</Type/Page/MediaBox[0 0 612 792]/Contents 2 0 R>>",
        ),
        sections=sections,
    )


@pytest.mark.parametrize("extract", [extract_pdf_text_layout, extract_pdf_text_aligned])
def test_pdfplumber_is_bounded_on_the_pages_pdfminer_lists(
    monkeypatch: pytest.MonkeyPatch, extract: Callable[[bytes], str]
) -> None:
    """pdfminer finds pages its own way: with no page tree, or none it can
    walk, every object typed as a page, once for each cross-reference
    section listing it. Imitated off pypdf's walk, a page pdfminer found
    and pypdf did not escaped the count: an image-tagged stream it drew
    inflated 48 MiB, and one page listed in forty sections cost 43 s."""
    import zlib

    monkeypatch.setattr(_pdf_bound, "_MAX_DECODED_STREAM_BYTES", 1 << 20)
    image = b"/Subtype/Image/Filter/FlateDecode"
    with pytest.raises(ExtractorError, match=r"^PDF streams decode to more than"):
        extract(_orphan_page_pdf(zlib.compress(_comment(2 << 20), 9), image))
    with pytest.raises(ExtractorError, match=r"^PDF streams decode to more than"):
        extract(_inflating_pdf(300_000, pages=4, tree=False))
    with pytest.raises(CardNotReadableError, match="no text layer"):
        extract(_inflating_pdf(300_000, pages=3, tree=False))
    content = zlib.compress(_comment(300_000), 9)
    with pytest.raises(ExtractorError, match=r"^PDF streams decode to more than"):
        extract(_listed_page_pdf(content, 4))
    with pytest.raises(CardNotReadableError, match="no text layer"):
        extract(_listed_page_pdf(content, 3))


def _hidden_stream_pdf(hidden: bytes, shape: str) -> bytes:
    """A one-page PDF whose content, object 4, pypdf reads as a few bytes of
    text and pdfminer as ``hidden``, a FlateDecode stream it finds elsewhere:
    ``rebuilt``, a table header spaced twice that pdfminer rebuilds by
    scanning the file, the later definition winning; ``fallthrough``, the
    newest table naming a mislabelled object pypdf takes and pdfminer skips
    for the older table's entry, which pypdf's own repair cannot find."""
    text = b"BT /F1 12 Tf 10 50 Td (Hello) Tj ET"
    out = bytearray(b"%PDF-1.4\n")
    at = {}

    def put(key: object, head: bytes, body: bytes) -> None:
        at[key] = len(out)
        out.extend(head + body + b"\nendobj\n")

    put(1, b"1 0 obj\n", b"<</Type/Catalog/Pages 2 0 R>>")
    put(2, b"2 0 obj\n", b"<</Type/Pages/Kids[3 0 R]/Count 1>>")
    put(
        3,
        b"3 0 obj\n",
        b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents 4 0 R>>",
    )
    small = b"<</Length %d>>\nstream\n" % len(text) + text + b"\nendstream"
    big = b"<</Filter/FlateDecode/Length %d>>\nstream\n" % len(hidden) + hidden
    big += b"\nendstream"

    def rows(*keys: object) -> bytes:
        return b"".join(b"%010d 00000 n \n" % at[k] for k in keys)

    if shape == "rebuilt":
        put(4, b"4 0 obj\n", small)
        put("hidden", b"4 0 obj\n", big)
        xref = len(out)
        out += b"xref\n0  5\n0000000000 65535 f \n" + rows(1, 2, 3, 4)
        out += b"trailer<</Size 5/Root 1 0 R>>\n"
    else:
        put("hidden", b"4 0 %c\nobj\n", big)
        put(4, b"5 0 obj\n", small)
        older = len(out)
        out += b"xref\n0 5\n0000000000 65535 f \n" + rows(1, 2, 3, "hidden")
        out += b"trailer<</Size 5/Root 1 0 R>>\n"
        xref = len(out)
        out += b"xref\n4 1\n" + rows(4)
        out += b"trailer<</Size 5/Root 1 0 R/Prev %d>>\n" % older
    out += b"startxref\n%d\n%%%%EOF\n" % xref
    return bytes(out)


@pytest.mark.parametrize("shape", ["rebuilt", "fallthrough"])
@pytest.mark.parametrize("extract", [extract_pdf_text_layout, extract_pdf_text_aligned])
def test_pdfplumber_is_bounded_on_what_pdfminer_decodes(
    monkeypatch: pytest.MonkeyPatch, extract: Callable[[bytes], str], shape: str
) -> None:
    """Counted on pypdf's reading of the file, every way of having the two
    readers find different objects let pdfminer decode what pypdf never
    counted: two tables, an entry listed twice, a shifted pointer, a table
    rebuilt by scanning, an entry pdfminer skips for an older one, each 40
    MiB and more. What pdfminer decodes is measured on pdfminer itself."""
    import zlib

    monkeypatch.setattr(_pdf_bound, "_MAX_DECODED_STREAM_BYTES", 1 << 20)
    hidden = zlib.compress(_comment(2 << 20), 9)
    with pytest.raises(ExtractorError, match=r"^PDF streams decode to more than"):
        extract(_hidden_stream_pdf(hidden, shape))
    with pytest.raises(CardNotReadableError, match="no text layer"):
        extract(_hidden_stream_pdf(zlib.compress(_comment(1000), 9), shape))


def _object_stream_pdf(padding: int) -> bytes:
    """A PDF whose catalog, page tree and page sit in an object stream padded
    with ``padding`` blanks, behind a cross-reference stream, so pdfminer
    inflates it opening the file."""
    import struct
    import zlib

    objects = (
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents 4 0 R>>",
    )
    body = b""
    offsets = []
    for obj in objects:
        offsets.append(len(body))
        body += obj + b"\n"
    header = b" ".join(b"%d %d" % (n, o) for n, o in enumerate(offsets, 1))
    packed = zlib.compress(header + b"\n" + body + b" " * padding, 9)
    text = b"BT /F1 12 Tf 72 720 Td (Hello) Tj ET"
    out = bytearray(b"%PDF-1.5\n")
    at = {4: len(out)}
    out += b"4 0 obj\n<</Length %d>>\nstream\n%s\nendstream\nendobj\n" % (
        len(text),
        text,
    )
    at[5] = len(out)
    out += b"5 0 obj\n<</Type/ObjStm/N 3/First %d/Filter/FlateDecode/Length %d>>" % (
        len(header) + 1,
        len(packed),
    )
    out += b"\nstream\n" + packed + b"\nendstream\nendobj\n"
    at[6] = len(out)
    rows = struct.pack(">BIH", 0, 0, 65535)
    rows += b"".join(struct.pack(">BIH", 2, 5, i) for i in range(3))
    rows += b"".join(struct.pack(">BIH", 1, at[n], 0) for n in (4, 5, 6))
    out += b"6 0 obj\n<</Type/XRef/Size 7/W[1 4 2]/Root 1 0 R/Length %d>>" % len(rows)
    out += b"\nstream\n" + rows + b"\nendstream\nendobj\n"
    out += b"startxref\n%d\n%%%%EOF\n" % at[6]
    return bytes(out)


@pytest.mark.parametrize("extract", [extract_pdf_text_layout, extract_pdf_text_aligned])
def test_what_pdfminer_inflates_opening_a_file_is_measured(
    monkeypatch: pytest.MonkeyPatch, extract: Callable[[bytes], str]
) -> None:
    """pdfminer inflates the cross-reference and object streams as it opens
    a file, before anything could be counted: 300 MB from a 306 kB file. Its
    decoding is measured as it goes, so it stops at the bound."""
    monkeypatch.setattr(_pdf_bound, "_MAX_DECODED_STREAM_BYTES", 1 << 20)
    with pytest.raises(ExtractorError, match=r"^PDF streams decode to more than"):
        extract(_object_stream_pdf(2 << 20))
    with pytest.raises(CardNotReadableError, match="no text layer"):
        extract(_object_stream_pdf(1000))


@pytest.mark.parametrize("extract", _EVERY_READER)
def test_a_ccitt_stream_outside_an_image_is_refused(
    extract: Callable[[bytes], str],
) -> None:
    """pdfminer decodes CCITT fax in pure Python: 400 bytes took 3,4 s on a
    Raspberry Pi. No card carries one, and an image may."""
    ccitt = b"/Filter/CCITTFaxDecode/DecodeParms<</K -1/Columns 100>>"
    with pytest.raises(ExtractorError, match=r"^PDF carries a CCITT"):
        extract(_pdf_of(b"\xff" * 20, attrs=ccitt))
    fax = b"<</Subtype/Image/Width 100/Height 1%s/Length 20>>\nstream\n" % ccitt
    with pytest.raises(CardNotReadableError, match="no text layer"):
        extract(_inflating_pdf(1000, fax + b"\xff" * 20 + b"\nendstream"))


def _damaged(size: int, *, how: str) -> bytes:
    """A PDF whose content stream, ``size`` bytes of comment that compress
    poorly, is cut short, inside an ASCII85 wrapper too (``wrapped``),
    whole but failing its checksum (``checksum``), broken halfway
    (``broken``), or cut short under a gzip header (``gzip``)."""
    import base64
    import gzip
    import random
    import zlib

    letters = random.Random(0).choices(b"abcdefghijklmnopqrstuvwxyz", k=size)
    whole = zlib.compress(b"%" + bytes(letters) + b"\n", 9)
    if how == "gzip":
        return _pdf_of(gzip.compress(b"%" + bytes(letters) + b"\n")[:-4])
    if how == "checksum":
        return _pdf_of(whole[:-1] + bytes([whole[-1] ^ 1]))
    if how == "broken":
        half = len(whole) // 2
        return _pdf_of(whole[:half] + b"\xff" * 8 + whole[half + 8 :])
    if how == "cut":
        return _pdf_of(whole[:-4])
    return _pdf_of(
        base64.a85encode(whole[:-4]) + b"~>",
        attrs=b"/Filter[/ASCII85Decode/FlateDecode]",
    )


@pytest.mark.parametrize("how", ["cut", "wrapped", "checksum"])
@pytest.mark.parametrize("extract", [extract_pdf_text_layout, extract_pdf_text_aligned])
def test_a_damaged_flate_stream_counts_many_times_over(
    monkeypatch: pytest.MonkeyPatch, extract: Callable[[bytes], str], how: str
) -> None:
    """zlib refuses a FlateDecode stream cut short, and pdfminer then
    inflates it again a byte at a time, in a time quadratic in its size.
    A small one, like those on some Bolt and Frank cards, still reads."""
    monkeypatch.setattr(_pdf_bound, "_MAX_DECODED_STREAM_BYTES", 1 << 20)
    monkeypatch.setattr(_pdf_bound, "_DAMAGED_FLATE_UNIT", 4096)
    with pytest.raises(ExtractorError, match=r"^PDF streams decode to more than"):
        extract(_damaged(128 << 10, how=how))
    with pytest.raises(CardNotReadableError, match="no text layer"):
        extract(_damaged(1000, how=how))


def test_pypdf_counts_again_a_flate_stream_zlib_fails_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pypdf reads a stream cut short as far as it goes, at once, and goes
    on past a failure, or a gzip header, a byte at a time to the end of the
    stream. Counted again like pdfminer's, the 216 kB stream Bolt's
    professional Plenty fixed card of June 2026 cuts short counted 8,8 MB
    more for nothing."""
    monkeypatch.setattr(_pdf_bound, "_MAX_DECODED_STREAM_BYTES", 1 << 20)
    monkeypatch.setattr(_pdf_bound, "_DAMAGED_FLATE_UNIT", 4096)
    for how in ("broken", "checksum", "gzip"):
        with pytest.raises(ExtractorError, match=r"^PDF streams decode to more than"):
            extract_pdf_text(_damaged(128 << 10, how=how))
    for how in ("cut", "wrapped"):
        with pytest.raises(CardNotReadableError, match="no text layer"):
            extract_pdf_text(_damaged(128 << 10, how=how))


@pytest.mark.parametrize("extract", [extract_pdf_text_layout, extract_pdf_text_aligned])
def test_the_bound_is_the_only_reason_pdfplumber_is_refused(
    extract: Callable[[bytes], str],
) -> None:
    """A stream with a filter no reader knows used to be refused as a parse
    error before pdfplumber, which read such cards before the check, saw
    them, and a file pdfminer cannot open is its own parse error."""
    unknown_filter = b"<</Length 4/Filter/NoSuchDecode>>\nstream\nabcd\nendstream"
    # pdfplumber reads the page, which carries no text: the card reached it.
    with pytest.raises(CardNotReadableError, match="no text layer"):
        extract(_inflating_pdf(1000, unknown_filter))
    with pytest.raises(ExtractorError, match="parse error") as unreadable:
        extract(b"%PDF-1.4\nnothing a reader can open\n")
    assert "refusing" not in str(unreadable.value)


def _font_pdf(pages: int, to_unicode: bytes, attrs: bytes = b"") -> bytes:
    """``pages`` pages sharing resources whose font is written into them,
    with ``to_unicode``, a FlateDecode stream with ``attrs``, as its
    ToUnicode map, which pdfminer reads again for each page."""
    kids = b" ".join(b"%d 0 R" % (6 + page) for page in range(pages))
    return _pdf_from(
        (
            b"<</Type/Catalog/Pages 2 0 R>>",
            b"<</Type/Pages/Kids[%s]/Count %d>>" % (kids, pages),
            b"<</Length 35>>\nstream\nBT /F1 12 Tf 10 50 Td (Hello) Tj ET\nendstream",
            b"<</Font<</F1<</Type/Font/Subtype/Type1/BaseFont/Helvetica"
            b"/ToUnicode 5 0 R>>>>>>",
            b"<<%s/Filter/FlateDecode/Length %d>>\nstream\n" % (attrs, len(to_unicode))
            + to_unicode
            + b"\nendstream",
            *(
                b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]"
                b"/Contents 3 0 R/Resources 4 0 R>>",
            )
            * pages,
        )
    )


@pytest.mark.parametrize("extract", [extract_pdf_text_layout, extract_pdf_text_aligned])
def test_pdfminer_is_held_to_the_bound_on_every_stream_it_reads(
    monkeypatch: pytest.MonkeyPatch, extract: Callable[[bytes], str]
) -> None:
    """A check made on a reading of the file beforehand missed what pdfminer
    itself reads: a font's ToUnicode map tagged as an image, a FlateDecode
    step after DCTDecode, a second inflate behind a predictor, each 256 MiB
    from a file of a few hundred kB. It is held to the bound as it reads."""
    import zlib

    monkeypatch.setattr(_pdf_bound, "_MAX_DECODED_STREAM_BYTES", 1 << 20)
    big = zlib.compress(_comment(2 << 20), 9)
    small = zlib.compress(_comment(1000), 9)
    for stream in (big, small):
        refused = stream is big
        rows = b"".join(
            b"\0" + stream[i : i + 100].ljust(100, b"\0")
            for i in range(0, len(stream), 100)
        )
        shapes = (
            _font_pdf(1, stream, b"/Subtype/Image"),
            _pdf_of(stream, attrs=b"/Filter[/DCTDecode/FlateDecode]"),
            _pdf_of(
                zlib.compress(rows, 9),
                attrs=b"/Filter[/FlateDecode/FlateDecode]"
                b"/DecodeParms[<</Predictor 10/Columns 100>>null]",
            ),
        )
        for payload in shapes:
            if refused:
                with pytest.raises(
                    ExtractorError, match=r"^PDF streams decode to more than"
                ):
                    extract(payload)
            else:
                with pytest.raises(CardNotReadableError, match="no text layer"):
                    extract(payload)


def test_pdfminer_stops_inflating_at_the_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refused once inflated, a stream would already be held: 300 MB from a
    306 kB file. pdfminer's inflate stops at what is left of the bound."""
    from pdfminer.pdftypes import PDFStream

    monkeypatch.setattr(_pdf_bound, "_MAX_DECODED_STREAM_BYTES", 1 << 20)
    decode = PDFStream.decode
    held: list[int] = []

    def decoded(stream: PDFStream) -> None:
        decode(stream)
        held.append(len(stream.data or b""))

    monkeypatch.setattr(PDFStream, "decode", decoded)
    with pytest.raises(ExtractorError, match=r"^PDF streams decode to more than"):
        extract_pdf_text_layout(_object_stream_pdf(8 << 20))
    with pytest.raises(ExtractorError, match=r"^PDF streams decode to more than"):
        extract_pdf_text_layout(_inflating_pdf(8 << 20))
    assert max(held, default=0) <= 1 << 20


@pytest.mark.parametrize("extract", [extract_pdf_text_layout, extract_pdf_text_aligned])
def test_a_font_loaded_on_each_page_counts_for_each(
    monkeypatch: pytest.MonkeyPatch, extract: Callable[[bytes], str]
) -> None:
    """pdfminer keeps a font only when it is referenced, so one written into
    the resources pages share has its ToUnicode map parsed again on every
    page: a 1 MiB map on ten pages took 130 s and was counted once."""
    import zlib

    monkeypatch.setattr(_pdf_bound, "_MAX_DECODED_STREAM_BYTES", 1 << 20)
    to_unicode = zlib.compress(_comment(300_000), 9)
    with pytest.raises(ExtractorError, match=r"^PDF streams decode to more than"):
        extract(_font_pdf(4, to_unicode))
    with pytest.raises(CardNotReadableError, match="no text layer"):
        extract(_font_pdf(3, to_unicode))


@pytest.mark.parametrize("extract", [extract_pdf_text_layout, extract_pdf_text_aligned])
def test_pdfminer_filters_decoded_in_pure_python_are_refused(
    extract: Callable[[bytes], str],
) -> None:
    """pdfminer decodes LZW and run length in pure Python with no limit; no
    card carries either outside an image, which pdfminer never decodes."""
    lzw = b"\x80\x0b\x60\x50\x22\x0c\x0c\x85\x01"
    for name, stream in ((b"LZWDecode", lzw), (b"RunLengthDecode", b"\x00A\x80")):
        with pytest.raises(
            ExtractorError, match=r"^PDF carries a [A-Za-z -]* stream outside an image"
        ):
            extract(_pdf_of(stream, attrs=b"/Filter/" + name))
        image = b"<</Subtype/Image/Width 1/Height 1/Filter/%s/Length %d>>\nstream\n" % (
            name,
            len(stream),
        )
        with pytest.raises(CardNotReadableError, match="no text layer"):
            extract(_inflating_pdf(1000, image + stream + b"\nendstream"))


@pytest.mark.parametrize("extract", [extract_pdf_text_layout, extract_pdf_text_aligned])
def test_each_inflate_of_a_chain_counts(
    monkeypatch: pytest.MonkeyPatch, extract: Callable[[bytes], str]
) -> None:
    """A stream inflated over and over, each step as large as the last,
    counted only its final output: 51 inflates of 4 MiB from a 7,6 kB file
    counted 4 MiB."""
    import zlib

    monkeypatch.setattr(_pdf_bound, "_MAX_DECODED_STREAM_BYTES", 1 << 20)

    def chained(steps: int) -> bytes:
        stream = _comment(300_000)
        for _ in range(steps):
            stream = zlib.compress(stream, 0)
        return _pdf_of(stream, attrs=b"/Filter[%s]" % (b"/FlateDecode" * steps))

    with pytest.raises(ExtractorError, match=r"^PDF streams decode to more than"):
        extract(chained(4))
    with pytest.raises(CardNotReadableError, match="no text layer"):
        extract(chained(3))


def test_an_rc4_decryption_counts_before_it_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pdfminer decrypts RC4 a byte at a time, appending each to what it
    holds: 1 MiB took 365 s, and twenty 64 kB strings 8 s, none counted.
    Each counts its length times itself, before it runs. Every encrypted
    card is AES."""
    from pdfminer.arcfour import Arcfour

    monkeypatch.setattr(_pdf_bound, "_MAX_DECODED_STREAM_BYTES", 1 << 20)
    with _pdf_bound.bounded_pdfminer():
        for _ in range(3):
            assert len(Arcfour(b"key").decrypt(b"\0" * (64 << 10))) == 64 << 10
    with (
        pytest.raises(ExtractorError, match=r"^PDF streams decode to more than"),
        _pdf_bound.bounded_pdfminer(),
    ):
        for _ in range(5):
            Arcfour(b"key").decrypt(b"\0" * (64 << 10))
    big = 2 << 20
    assert len(Arcfour(b"key").decrypt(b"\0" * (64 << 10))) == 64 << 10
    with (
        pytest.raises(ExtractorError, match=r"^PDF streams decode to more than"),
        _pdf_bound.bounded_pdfminer(),
    ):
        Arcfour(b"key").decrypt(b"\0" * big)


@pytest.mark.parametrize("extract", _EVERY_READER)
def test_a_large_ascii85_stream_is_refused(
    monkeypatch: pytest.MonkeyPatch, extract: Callable[[bytes], str]
) -> None:
    """Decoding ASCII85 takes about 90 bytes of memory for each: 16 MiB of
    it peaked at 1,7 GiB, before anything was counted. The largest a card
    carries is 2,2 kB."""
    import base64

    monkeypatch.setattr(_pdf_bound, "_MAX_ASCII85_BYTES", 4096)
    a85 = b"/Filter/ASCII85Decode"
    with pytest.raises(ExtractorError, match=r"^PDF carries an ASCII85 stream"):
        extract(_pdf_of(base64.a85encode(_comment(4096)) + b"~>", attrs=a85))
    with pytest.raises(CardNotReadableError, match="no text layer"):
        extract(_pdf_of(base64.a85encode(_comment(1000)) + b"~>", attrs=a85))


@pytest.mark.parametrize("extract", _EVERY_READER)
def test_ascii85_behind_another_filter_is_held_to_what_it_decodes(
    monkeypatch: pytest.MonkeyPatch, extract: Callable[[bytes], str]
) -> None:
    """Behind an inflate, an ASCII85 step decodes what the inflate made of a
    few bytes, which no size of the stream itself bounds: it is held to the
    ASCII85 it is handed."""
    import base64
    import zlib

    monkeypatch.setattr(_pdf_bound, "_MAX_ASCII85_BYTES", 4096)
    attrs = b"/Filter[/FlateDecode/ASCII85Decode]"
    big = zlib.compress(base64.a85encode(_comment(4096)) + b"~>")
    with pytest.raises(ExtractorError, match=r"^PDF carries an ASCII85 stream"):
        extract(_pdf_of(big, attrs=attrs))
    small = zlib.compress(base64.a85encode(_comment(1000)) + b"~>")
    with pytest.raises(CardNotReadableError, match="no text layer"):
        extract(_pdf_of(small, attrs=attrs))


@pytest.mark.parametrize("extract", [extract_pdf_text_layout, extract_pdf_text_aligned])
def test_what_a_decode_hands_back_past_its_last_inflate_counts(
    monkeypatch: pytest.MonkeyPatch, extract: Callable[[bytes], str]
) -> None:
    """ASCII85 behind an inflate makes four bytes of each ``z``: a 38 kB file
    had pdfminer handed 125,8 MB with 31,5 MB counted."""
    import zlib

    monkeypatch.setattr(_pdf_bound, "_MAX_DECODED_STREAM_BYTES", 1 << 20)
    attrs = b"/Filter[/FlateDecode/ASCII85Decode]"
    with pytest.raises(ExtractorError, match=r"^PDF streams decode to more than"):
        extract(_pdf_of(zlib.compress(b"z" * 300_000 + b"~>"), attrs=attrs))
    with pytest.raises(CardNotReadableError, match="no text layer"):
        extract(_pdf_of(zlib.compress(b"z" * 200_000 + b"~>"), attrs=attrs))


def _filter_in_object_stream_pdf(stream: bytes) -> bytes:
    """A PDF whose one page draws ``stream``, unfiltered, through a /Filter
    kept in an object stream, which pdfminer inflates in the middle of
    decoding ``stream``."""
    import struct
    import zlib

    head = b"5 0 "
    packed = zlib.compress(head + b"[]")
    objects = {
        1: b"<</Type/Catalog/Pages 2 0 R>>",
        2: b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        3: b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents 4 0 R>>",
        4: b"<</Filter 5 0 R/Length %d>>\nstream\n" % len(stream)
        + stream
        + b"\nendstream",
        6: b"<</Type/ObjStm/N 1/First %d/Filter/FlateDecode/Length %d>>\nstream\n"
        % (len(head), len(packed))
        + packed
        + b"\nendstream",
    }
    out = bytearray(b"%PDF-1.5\n")
    rows = struct.pack(">BIH", 0, 0, 65535)
    at = {}
    for number, body in objects.items():
        at[number] = len(out)
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    for number in range(1, 7):
        if number == 5:
            rows += struct.pack(">BIH", 2, 6, 0)
        else:
            rows += struct.pack(">BIH", 1, at[number], 0)
    xref = len(out)
    rows += struct.pack(">BIH", 1, xref, 0)
    out += b"7 0 obj\n<</Type/XRef/Size 8/W[1 4 2]/Root 1 0 R/Length %d>>" % len(rows)
    out += b"\nstream\n" + rows + b"\nendstream\nendobj\n"
    out += b"startxref\n%d\n%%%%EOF\n" % xref
    return bytes(out)


@pytest.mark.parametrize("extract", [extract_pdf_text_layout, extract_pdf_text_aligned])
def test_a_stream_counts_whatever_its_filter_inflates_to_resolve(
    monkeypatch: pytest.MonkeyPatch, extract: Callable[[bytes], str]
) -> None:
    """A stream whose /Filter sits in an object stream not yet read has
    pdfminer inflate that one while decoding it: 40 MB of content counted
    202 bytes."""
    monkeypatch.setattr(_pdf_bound, "_MAX_DECODED_STREAM_BYTES", 1 << 20)
    with pytest.raises(ExtractorError, match=r"^PDF streams decode to more than"):
        extract(_filter_in_object_stream_pdf(_comment(1 << 20)))
    with pytest.raises(CardNotReadableError, match="no text layer"):
        extract(_filter_in_object_stream_pdf(_comment(1000)))


@pytest.mark.parametrize("extract", [extract_pdf_text_layout, extract_pdf_text_aligned])
def test_a_predictor_row_wider_than_its_stream_is_refused(
    extract: Callable[[bytes], str],
) -> None:
    """pdfminer holds a PNG predictor row of the width named before it reads
    any: a 468-byte file naming 64 Mi columns took 581 MiB. A row as wide as
    its stream, or no wider than 16 kB, is read."""
    import zlib

    def predicted(row: int, columns: int) -> bytes:
        return _pdf_of(
            zlib.compress(b"\x00" + b" " * row),
            attrs=b"/Filter/FlateDecode/DecodeParms<</Predictor 12/Columns %d>>"
            % columns,
        )

    with pytest.raises(ExtractorError, match=r"^PDF names a predictor row wider"):
        extract(predicted(20, 1 << 20))
    for row, columns in ((20, 1000), (20_000, 20_000)):
        with pytest.raises(CardNotReadableError, match="no text layer"):
            extract(predicted(row, columns))


def _swallowed_refusal_pdf() -> bytes:
    """A card whose text reads, with an /Info title in an LZW object stream:
    pdfplumber reads the metadata and logs what fails there instead of
    raising it."""
    import struct

    text = (
        b"BT /F1 12 Tf 72 720 Td "
        + b"(Hello world, a card with text) Tj 0 -14 Td " * 30
    )
    text += b"ET"
    lzw = b"\x80\x0b\x60\x50\x22\x0c\x0c\x85\x01"
    objects = {
        1: b"<</Type/Catalog/Pages 2 0 R>>",
        2: b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        3: b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents 4 0 R"
        b"/Resources<</Font<</F1<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>>>>>>>",
        4: b"<</Length %d>>\nstream\n%s\nendstream" % (len(text), text),
        5: b"<</Type/ObjStm/N 1/First 4/Filter/LZWDecode/Length 9>>\nstream\n"
        + lzw
        + b"\nendstream",
        7: b"<</Title 8 0 R>>",
    }
    out = bytearray(b"%PDF-1.5\n")
    at = {}
    for number, body in objects.items():
        at[number] = len(out)
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    at[6] = len(out)
    rows = struct.pack(">BIH", 0, 0, 65535)
    for number in range(1, 9):
        if number == 8:
            rows += struct.pack(">BIH", 2, 5, 0)
        else:
            rows += struct.pack(">BIH", 1, at.get(number, 0), 0)
    out += (
        b"6 0 obj\n<</Type/XRef/Size 9/W[1 4 2]/Root 1 0 R/Info 7 0 R/Length %d>>"
        % (len(rows))
    )
    out += b"\nstream\n" + rows + b"\nendstream\nendobj\n"
    out += b"startxref\n%d\n%%%%EOF\n" % at[6]
    return bytes(out)


def test_a_refusal_pdfplumber_swallows_still_refuses() -> None:
    """pdfplumber logs what fails while it reads the metadata and goes on,
    and the card's text came back as if the refusal had not been made."""
    with pytest.raises(ExtractorError, match=r"^PDF carries a LZW stream"):
        extract_pdf_text_layout(_swallowed_refusal_pdf())


def test_text_mentions_month_tolerates_split_whitespace() -> None:
    months = (
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
    # PDF extraction split the month and year across a newline / extra spaces.
    assert text_mentions_month("Tariefkaart mei\n2026 ...", date(2026, 5, 1), months)
    assert text_mentions_month("Tariefkaart mei   2026 ...", date(2026, 5, 1), months)
    # A different month must still not match.
    assert not text_mentions_month(
        "Tariefkaart juni 2026 ...", date(2026, 5, 1), months
    )


def test_parse_sign_covers_canonical_hyphens() -> None:
    # PDF typesetters emit U+2010 / U+2011 for a minus; both must read as
    # negative so a hyphen re-render can't drop a formula's sign.
    for neg in ("-", "‐", "‑", "‒", "–", "—", "−"):
        assert parse_sign(neg) == -1.0
    assert parse_sign("+") == 1.0


# Reads a card of every format; 108 s with none cached on a loaded Raspberry Pi 4.
@pytest.mark.timeout(300)
def test_parse_brussels_osp_across_extractor_formats() -> None:
    # The Brussels providers render the OSP table several different ways
    # (label above vs. beside the value; et/Entre/<= phrasings), but the shared
    # parser must return the same eight bands for each. The open-ended top row
    # is the one that needs care: "Entre 36,01 et 56,00 kVA" and "> 56,01 kVA"
    # both end in a bound near 56, so keying on the number alone made the top
    # row overwrite the one below it.
    from custom_components.be_electricity_prices.providers._parse import (
        parse_brussels_osp,
    )
    from tests import fixture_text

    expected = {
        "le1_44": 0.0,
        "le6": 13.36,
        "le9_6": 21.37,
        "le13": 26.71,
        "le18": 39.94,
        "le36": 53.30,
        "le56": 106.59,
        "gt56": 173.25,
    }
    for name, layout in (
        ("mega_smart_fixed_b.pdf", False),
        ("engie_dynamic_b.pdf", False),
        ("totalenergies_dynamic_b.pdf", True),
        # Bolt prints a lowercase "Obligations de service publique" header,
        # so the block regex must be case-insensitive.
        ("bolt_fix.pdf", True),
        ("bolt_variable.pdf", True),
    ):
        assert parse_brussels_osp(fixture_text(name, layout=layout)) == expected
    # A card without the OSP block (non-Brussels) yields None.
    assert parse_brussels_osp(fixture_text("mega_smart_fixed_w.pdf")) is None


def _blank_one_page_pdf() -> bytes:
    """A single-page PDF with NO text layer, generated rather than checked in.

    Deliberately takes no argument. The first version accepted a ``text``
    parameter and never drew it (it merged the blank page with itself), so a
    later "short but non-empty card" test would have passed vacuously while
    believing it exercised a real text layer.
    """
    import io

    import pypdf

    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=595, height=842)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def test_card_with_no_text_layer_raises_a_distinct_error() -> None:
    """An image-only card must raise CardNotReadableError, not the generic
    parse error.

    The distinction is what routes the user to the workaround instead of to
    "the layout changed, please report it", and it has to come from the
    document rather than from a per-supplier flag: a flag encodes one
    month's observation permanently, and Ecofix overwrites its card in
    place every month, so a revert to text would leave the flag lying.
    """
    blank = _blank_one_page_pdf()
    with pytest.raises(CardNotReadableError) as excinfo:
        extract_pdf_text(blank)
    assert "no text layer" in str(excinfo.value)
    # It is still an ExtractorError, so every existing handler keeps working.
    assert isinstance(excinfo.value, ExtractorError)


# Reads every fixture card; 382 s with none cached on a loaded Raspberry Pi 4.
@pytest.mark.timeout(900)
def test_a_real_card_is_never_mistaken_for_an_image_only_one() -> None:
    """The threshold must clear every card we actually ship.

    Measured across all fixtures the least texty readable card, a one-page
    TotalEnergies feed-in card, carries 2435 characters against 172 and 342
    for the rasterized Ecofix ones. The CREG's social tariff cards are one
    table on one page and carry about 1550, which still leaves the
    600-character line a factor of two and a half of headroom.
    This asserts the margin rather than the constant, so shrinking the
    threshold toward real data fails here.

    Read through fixture_text, which runs the same extract_pdf_text and keeps
    what it read: extracting every fixture here, uncached, was nine minutes
    of a twenty-minute suite on one worker.
    """
    from tests import FIXTURES, fixture_text

    smallest = min(
        len(fixture_text(path.name).strip()) for path in sorted(FIXTURES.glob("*.pdf"))
    )
    assert smallest > 2.5 * _MIN_TEXT_LAYER_CHARS


async def test_bom_prefixed_pdf_is_stripped_before_parsing() -> None:
    """`_is_pdf_payload` tolerates a leading UTF-8 BOM so a publisher that
    prepends one is not rejected as "not a PDF". Tolerating it is only half
    the job: pdfplumber cannot parse those bytes and fails them with
    "No /Root object! - Is this really a PDF?", which reads like a corrupt
    card rather than three stray bytes. pypdf recovers on its own, so the
    aligned/layout variants are the ones that need this.
    """
    body = b"\xef\xbb\xbf%PDF-1.7\nrest of the document"
    session = _FakeBodySession(body)
    out = await _fetch_validated_pdf_bytes(session, "https://x/card.pdf")  # type: ignore[arg-type]
    assert out.startswith(b"%PDF"), "the BOM must not reach the parser"
    assert out == body[3:]


@pytest.mark.parametrize("prefix", [b"\n", b"\r\n", b" \t\n", b"\xef\xbb\xbf\r\n"])
async def test_a_pdf_whose_signature_follows_blank_lines_is_read(
    prefix: bytes,
) -> None:
    """A server that emits a newline ahead of the card had it refused as
    "expected a PDF"; past the BOM, blank space before %PDF is stripped the
    same way, since pdfplumber looks for the signature at byte zero."""
    body = b"%PDF-1.7\nrest of the document"
    session = _FakeBodySession(prefix + body)
    assert (
        await _fetch_validated_pdf_bytes(session, "https://x/card.pdf")  # type: ignore[arg-type]
        == body
    )


async def test_a_pdf_without_a_bom_is_returned_untouched() -> None:
    """The strip must key on the BOM, not run unconditionally: taking three
    bytes off an ordinary card would corrupt every supplier at once."""
    body = b"%PDF-1.7\nrest of the document"
    session = _FakeBodySession(body)
    assert (
        await _fetch_validated_pdf_bytes(session, "https://x/card.pdf")  # type: ignore[arg-type]
        == body
    )


# ---- numeric_row: reading a table row rather than matching a literal --------

# The Ecofix Flexy card, as a page image read back by OCR. The consumption
# row's label lost a letter; every figure on the card was read exactly.
_FLEXY_OCR = """Verbruik 1,60
Maandprjs: 11,81 11,81 11,81 11,81
Wallonie
Verwachte jaarprijs: 12,16 12,16 12,16 12,16
Injectie (E cent/kWh)
Maandprijs: 4,32 4,32 4,32
"""


def test_numeric_row_reads_a_row_whose_label_lost_a_letter() -> None:
    """The measured failure: `Maandprjs` for `Maandprijs`.

    The literal this replaces walked past the row and took four figures off
    the Injectie block, which parses clean and bills consumption at the
    feed-in rate: 0,0432 where the card says 0,1181.
    """
    assert numeric_row(
        _FLEXY_OCR, "Maandprijs", 4, after="Verbruik", before="Injectie"
    ) == ["11,81", "11,81", "11,81", "11,81"]


def test_numeric_row_will_not_cross_into_the_next_block() -> None:
    """With the row gone, the answer is nothing, never the neighbouring row.

    This is the whole point. `\\s` matches a newline, so the literal was free
    to collect its four figures from wherever it landed.
    """
    gutted = _FLEXY_OCR.replace("Maandprjs: 11,81 11,81 11,81 11,81\n", "")
    assert (
        numeric_row(gutted, "Maandprijs", 4, after="Verbruik", before="Injectie")
        is None
    )


def test_numeric_row_holds_the_caller_to_the_column_count() -> None:
    """A row offering three figures is not the four-column row the card
    prints, whatever its label says."""
    short = _FLEXY_OCR.replace(
        "Maandprjs: 11,81 11,81 11,81 11,81", "Maandprijs: 11,81 11,81 11,81"
    )
    assert (
        numeric_row(short, "Maandprijs", 4, after="Verbruik", before="Injectie") is None
    )


def test_numeric_row_keeps_the_blocks_apart() -> None:
    """Both blocks carry a Maandprijs row; each lookup gets its own."""
    assert numeric_row(_FLEXY_OCR, "Maandprijs", after="Injectie") == [
        "4,32",
        "4,32",
        "4,32",
    ]


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("Maandprijs", True),
        ("Maandprjs", True),
        ("Maandpris", True),
        ("Mandprjs", True),
        # Two edits and a digit substituted for a letter is not a reading,
        # it is a guess, and a guess is what must not reach a bill.
        ("M4andprij5", False),
        ("Toeslagen", False),
    ],
)
def test_numeric_row_tolerates_damage_but_not_invention(
    label: str, expected: bool
) -> None:
    text = _FLEXY_OCR.replace("Maandprjs", label)
    got = numeric_row(text, "Maandprijs", 4, after="Verbruik", before="Injectie")
    assert (got is not None) is expected


@pytest.mark.parametrize(
    ("printed", "expected"),
    [
        ("Tarief 11,81 4,32", ["11,81", "4,32"]),
        # A thousands separator and a decimal comma in the same figure. Read
        # as two columns, this row would look three wide and be refused.
        ("Tarief 1.234,56 4,32", ["1.234,56", "4,32"]),
        ("Tarief 1.000.000 20.000", ["1.000.000", "20.000"]),
    ],
)
def test_numeric_row_reads_a_belgian_figure_as_one_column(
    printed: str, expected: list[str]
) -> None:
    assert numeric_row(printed, "Tarief", len(expected)) == expected


def test_numeric_row_without_a_column_count_still_needs_one_row() -> None:
    """Leaving the count open is for cards that vary it; it never licenses
    a match that spans two rows."""
    assert numeric_row("Label: 1,0 2,0\nOther: 3,0", "Label") == ["1,0", "2,0"]


def test_numeric_row_reads_a_label_that_wrapped_above_its_figures() -> None:
    """pdfplumber puts a label too long for its column on its own line and
    leaves the row's figures alone on the next, which is what it does to
    Ecofix's Fluvius West and Zenne-Dijle rows. Read as one row, the way it
    prints on the card."""
    card = (
        "Fluvius Midden-Vlaanderen 53,1314 5,27945 4,77831 18,92 18,92\n"
        "Fluvius West\n"
        "60,5255 6,69853 5,99282 18,92 18,92\n"
    )
    assert numeric_row(card, "Fluvius West", 5) == [
        "60,5255",
        "6,69853",
        "5,99282",
        "18,92",
        "18,92",
    ]


def test_a_bare_figure_line_borrows_no_label_from_a_row_above() -> None:
    """Only a line carrying no figures of its own can be a wrapped label. A
    row that already has its own label keeps it, so a stray figure line under
    a real row cannot be read as that row repeated."""
    card = "Fluvius West 60,5255 6,69853\n1,00 2,00\n"
    assert numeric_row(card, "Fluvius West", 2) == ["60,5255", "6,69853"]


_OSP_FEES = {
    "le1_44": 0.0,
    "le6": 13.36,
    "le9_6": 21.37,
    "le13": 26.71,
    "le18": 39.94,
    "le36": 53.30,
    "le56": 106.59,
    "gt56": 173.25,
}


def test_brussels_osp_reads_the_french_block() -> None:
    card = (
        "Taxe obligations de Service Public (OSP) - €/an\n"
        "<= 1,44 kVA 0,00\n"
        "> 1,44 et <= 6,0 kVA 13,36\n"
        "> 6,00 et <= 9,6 kVA 21,37\n"
        "> 9,60 et <= 13,0 kVA 26,71\n"
        "> 13,00 et <= 18,0 kVA 39,94\n"
        "> 18,00 et <= 36,0 kVA 53,30\n"
        "> 36,00 et <= 56,0 kVA 106,59\n"
        "> 56,00 kVA 173,25\n"
    )
    assert parse_brussels_osp(card) == _OSP_FEES


def test_brussels_osp_reads_the_dutch_block() -> None:
    """Brussels is bilingual and the Dutch cards head the same regulated
    table "Taks openbare dienstverplichtingen (ODV)". Anchored on the French
    wording alone the block went unread, so a Dutch Brussels card priced
    every connection at a zero OSP fee instead of up to 173,25 EUR a year."""
    card = (
        "Taks openbare dienstverplichtingen (ODV) - €/jaar\n"
        "<= 1,44 kVA 0,00\n"
        "> 1,44 en <= 6,0 kVA 13,36\n"
        "> 6,00 en <= 9,6 kVA 21,37\n"
        "> 9,60 en <= 13,0 kVA 26,71\n"
        "> 13,00 en <= 18,0 kVA 39,94\n"
        "> 18,00 en <= 36,0 kVA 53,30\n"
        "> 36,00 en <= 56,0 kVA 106,59\n"
        "> 56,00 kVA 173,25\n"
    )
    assert parse_brussels_osp(card) == _OSP_FEES


def test_brussels_osp_is_none_off_a_card_without_the_block() -> None:
    assert parse_brussels_osp("SIBELGA 9,96 9,96 7,53 7,53 14,73 2,27") is None


def test_the_vreg_ceiling_is_read_from_either_language() -> None:
    """Luminus and Frank state the maximumtarief in a footnote, not a column.

    The regulator caps what a digital-meter connection pays in network
    charges, and ``fees._capped_capacity_annual`` applies it. Cards that
    print it as a column of the DSO table were read; these two state it once
    below the table and it went unread, so the cap never bound on 15
    contracts. It bites where the capacity term dominates, which is a
    low-volume connection on a high peak.
    """
    from custom_components.be_electricity_prices.providers._parse import (
        parse_vreg_network_ceiling,
    )

    french = (
        "le tarif de gestion des données s’élève à 18,56 € par an. Un tarif "
        "maximal de 0,3472738 €/kWh (hors gestion des données) s’applique aux "
        "compteurs digitaux."
    )
    dutch = (
        "* Voor digitale meters geldt een maximumtarief van 0,3472738 EUR/kWh "
        "(excl. databeheer)."
    )
    assert parse_vreg_network_ceiling(french) == pytest.approx(0.3472738)
    assert parse_vreg_network_ceiling(dutch) == pytest.approx(0.3472738)

    # Wrapped across lines by the text layer, as the cards do it.
    assert parse_vreg_network_ceiling(
        "Un tarif\nmaximal de\n0,3472738 €/kWh"
    ) == pytest.approx(0.3472738)

    # A card that does not state it leaves the cap off rather than
    # inventing a ceiling.
    assert parse_vreg_network_ceiling("no ceiling on this card") is None


async def test_a_page_that_is_not_utf8_is_read_not_raised(
    socket_enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A page in another charset than it declares (a maintenance page in
    Latin-1) decoded strictly raised UnicodeDecodeError, which is no
    ExtractorError and so escaped every caller that handles one."""
    from aiohttp import web
    from aiohttp.test_utils import TestServer

    # The local test server speaks plain http.
    monkeypatch.setattr(_pdf, "_require_https", lambda _url: None)

    async def latin1(_request: web.Request) -> web.Response:
        return web.Response(body=b"Tarif \xe9lectricit\xe9", content_type="text/html")

    app = web.Application()
    app.router.add_get("/", latin1)
    async with TestServer(app) as server, aiohttp.ClientSession() as session:
        text = await fetch_text(session, str(server.make_url("/")))
    assert text == "Tarif �lectricit�"


class _RedirectedResp:
    """An answer reached through ``hops``, the last of them the final url."""

    status = 200
    content_length = None
    charset = None

    def __init__(self, hops: list[str], body: bytes) -> None:
        from yarl import URL

        self.history = tuple(SimpleNamespace(url=URL(hop)) for hop in hops[:-1])
        self.url = URL(hops[-1])
        self.content = FakeBody(body)
        self.headers = {"Last-Modified": "Thu, 01 Oct 2026 00:00:00 GMT"}

    async def __aenter__(self) -> _RedirectedResp:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None


class _RedirectingSession:
    def __init__(self, hops: list[str], body: bytes) -> None:
        self._resp = _RedirectedResp(hops, body)

    def get(self, *_args: object, **_kwargs: object) -> _RedirectedResp:
        return self._resp

    head = get


@pytest.mark.parametrize("pdf", [True, False])
@pytest.mark.parametrize(
    ("hops", "refused"),
    [
        # A validated https link sent on to plain http, a LAN address here.
        (["https://card.test/c.pdf", "http://192.168.1.1/"], True),
        (
            ["https://card.test/c.pdf", "http://card.test/c.pdf", "https://x.test/"],
            True,
        ),
        # Another site over https is followed: energie.be's documents API
        # answers with a redirect to its Azure storage.
        (["https://card.test/c.pdf", "https://store.blob.core.windows.net/c"], False),
    ],
)
async def test_a_redirect_off_https_is_refused(
    pdf: bool, hops: list[str], refused: bool
) -> None:
    session = _RedirectingSession(hops, b"%PDF-1.7 card")
    fetch = (
        _fetch_validated_pdf_bytes(session, hops[0])  # type: ignore[arg-type]
        if pdf
        else fetch_text(session, hops[0])  # type: ignore[arg-type]
    )
    if refused:
        with pytest.raises(ExtractorError, match="redirected off https") as raised:
            await fetch
        # The target can be a LAN address: it stays out of last_error.
        assert "192.168" not in str(raised.value)
    else:
        assert await fetch


class _NoRequestSession:
    def get(self, *_args: object, **_kwargs: object) -> object:
        raise AssertionError("an http address was requested")

    head = get


@pytest.mark.parametrize("pdf", [True, False])
async def test_an_http_link_is_refused_before_it_is_requested(pdf: bool) -> None:
    """Trevion and Aspiravi take the links their listing prints as they are,
    and the redirect guard only looked at a chain that started on https, so
    an http link redirected to a LAN address was read."""
    url = "http://trevion.be/a.pdf"
    session = _NoRequestSession()
    fetch = (
        _fetch_validated_pdf_bytes(session, url)  # type: ignore[arg-type]
        if pdf
        else fetch_text(session, url)  # type: ignore[arg-type]
    )
    with pytest.raises(ExtractorError, match="not an https address"):
        await fetch


@pytest.mark.parametrize(
    ("hops", "refused"),
    [
        (["https://card.test/c.pdf", "http://192.168.1.1/"], True),
        (["https://card.test/c.pdf", "https://store.blob.core.windows.net/c"], False),
    ],
)
async def test_a_probe_redirected_off_https_is_refused(
    hops: list[str], refused: bool
) -> None:
    """Aspiravi HEADs the links its listing prints, and the probes followed a
    redirect anywhere."""
    session = _RedirectingSession(hops, b"")
    key = await head_freshness_key(session, hops[0])  # type: ignore[arg-type]
    if refused:
        assert key is None
        with pytest.raises(ExtractorError, match="redirected off https"):
            await head_or_raise(session, hops[0])  # type: ignore[arg-type]
    else:
        assert key is not None
        assert await head_or_raise(session, hops[0])  # type: ignore[arg-type]


async def test_an_http_link_is_not_probed() -> None:
    url = "http://aspiravi.be/a.pdf"
    session = _NoRequestSession()
    assert await head_freshness_key(session, url) is None  # type: ignore[arg-type]
    with pytest.raises(ExtractorError, match="not an https address"):
        await head_or_raise(session, url)  # type: ignore[arg-type]
    assert not await head_ok(session, url)  # type: ignore[arg-type]


def test_the_redirect_guard_holds_whatever_scheme_the_chain_starts_on() -> None:
    from yarl import URL

    resp = SimpleNamespace(
        history=(SimpleNamespace(url=URL("http://trevion.be/a.pdf")),),
        url=URL("http://192.168.1.1/admin"),
    )
    with pytest.raises(ExtractorError, match="redirected off https"):
        _pdf.guard_redirect("http://trevion.be/a.pdf", resp)  # type: ignore[arg-type]

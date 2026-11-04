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

"""Look for a supplier's card where its extractor did not find it.

The live check runs this for a card that failed to fetch or is still the
month before's. It reads the pages the supplier lists its cards on, keeps the
addresses that name the product, and downloads each to see whether it names
the running month. It only reports: the address it gives is for a person to
check and adopt, and nothing here changes what the integration reads.

A card that moved is found where the supplier itself lists it. In November
2026 Elegant renamed its files, Engie gave Empty House a new document code,
EnergyVision put its uploads in month folders and Luminus gave BasicFlex a
new product id, and each new address stood on the supplier's own list
before any extractor read it. A sitemap is no such list: it names product
families, not cards.

For Elegant, Engie and Luminus the candidates are the integration's own
(``providers/_moved.py``), which it reads a moved card from by itself; this
reports every one of them, with where it came from, including an address
that is not the product's own card for the month by construction (a sibling
product, the month before's product id), which the integration never reads.
EnergyVision's are searched here alone: its tariefkaart page links its
files, and its uploads sit in the month's folder, the month before's or
``inline-files``. The live check asks each module's own ``fetch``, so it
keeps failing at the usual address until the extractor reads the card
there.

Usage: python scripts/card_finder.py <supplier> <contract> <region> [YYYY-MM]
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import re
import sys
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from datetime import date, datetime
from html import unescape
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import aiohttp

# The rows a missing or moved card fails: the check named after the label's
# colon, on a "<supplier>/<contract>/<region>" label.
_TRIGGERS = frozenset({"fetch", "card is recent enough", "card has not expired"})
# The candidates downloaded for one card, best first. More is a supplier
# listing something other than one card per product.
_MAX_TRIED = 4
# One card's whole search, pages and downloads.
_SEARCH_TIMEOUT_S = 120.0

# A link as markup carries it.
_HREF_RE = re.compile(r"""(?:href|src)\s*=\s*["']([^"'<>]+)["']""", re.IGNORECASE)

# EnergyVision's page that links its files, where the tariefkaart listing
# builds them in a script.
_ENERGYVISION_PAGE = "/nl-be/tariefkaart-2"

# An address, where it came from, and whether it is the product's card for
# the month by construction, as ``providers/_moved.py`` gives them.
Candidates = list[tuple[str, str, bool]]
Search = Callable[[aiohttp.ClientSession, str, str, date], Awaitable[Candidates]]


def _pdf() -> ModuleType:
    """The PDF and HTTP helpers, as the live check loaded them."""
    return importlib.import_module("be_pkg.providers._pdf")


def _moved() -> ModuleType:
    """The candidates the integration reads a moved card from, as the live
    check loaded them (``providers/_moved.py``)."""
    return importlib.import_module("be_pkg.providers._moved")


async def _energyvision(
    session: aiohttp.ClientSession, contract_id: str, region: str, month: date
) -> Candidates:
    ev = importlib.import_module("be_pkg.providers.energyvision")
    contract = ev._CONTRACTS_BY_ID[contract_id]
    card = contract.cards[region]
    stamp = f"{month.month:02d}{month.year % 100:02d}"
    named = re.compile(
        rf"/EV-{stamp}-{re.escape(contract.code)}-{re.escape(card.token)}"
        r"(?:_\d+)?\.pdf$",
        re.IGNORECASE,
    )
    page_url = card.index_url or f"{card.site}{_ENERGYVISION_PAGE}"
    found: dict[str, str] = {}
    try:
        page = unescape(await _pdf().fetch_text(session, page_url))
    except _pdf().ExtractorError:
        page = ""
    for link in _HREF_RE.findall(page):
        url = urljoin(page_url, link)
        if named.search(url):
            found.setdefault(url, "its card page")
    # Where its listing's script looks for the month's name.
    for url in ev._archive_card_urls(contract, card, month):
        found.setdefault(url, "its upload folders")
    return [(url, where, True) for url, where in found.items()]


def _searches() -> dict[str, Search]:
    """Where to look for each covered supplier's card: the integration's
    candidates, and this script's own for EnergyVision."""
    searches: dict[str, Search] = {
        supplier: candidates
        for supplier, (_read_at, candidates) in _moved().FALLBACKS.items()
    }
    searches["energyvision"] = _energyvision
    return searches


def _month_re(month: date) -> re.Pattern[str]:
    """The month as a card names it: "novembre 2026", "november 2026" or
    "11/2026"."""
    names = sorted(
        (name for name, number in _pdf()._MONTH_NAMES.items() if number == month.month),
        key=len,
        reverse=True,
    )
    return re.compile(
        rf"\b(?:{'|'.join(map(re.escape, names))})\s+{month.year}\b"
        rf"|\b{month.month:02d}/{month.year}\b",
        re.IGNORECASE,
    )


async def _reason(session: aiohttp.ClientSession, url: str, month: date) -> str | None:
    """Why the card at ``url`` is not ``month``'s, or None when it names it."""
    pdf = _pdf()
    try:
        text = await pdf.fetch_pdf_text(session, url, timeout=30)
    except pdf.ExtractorError as err:
        return f"{type(err).__name__}: {err}"
    if _month_re(month).search(text) is None:
        return f"names no {month:%Y-%m}"
    return None


async def find(
    session: aiohttp.ClientSession,
    modules: Mapping[str, ModuleType],
    supplier: str,
    contract_id: str,
    region: str,
    month: date,
) -> list[tuple[str, str, str | None]]:
    """Each candidate address for ``month``'s card, where it came from and
    why it is not that card, None when it is, best first."""
    found = await _searches()[supplier](session, contract_id, region, month)
    return [
        (
            url,
            where if exact else f"{where}, not the product's own card",
            await _reason(session, url, month),
        )
        for url, where, exact in found[:_MAX_TRIED]
    ]


def _covered(
    modules: Mapping[str, ModuleType], supplier: str, contract_id: str
) -> bool:
    module = modules.get(supplier)
    return (
        supplier in _searches()
        and module is not None
        and contract_id in getattr(module, "_CONTRACTS_BY_ID", {})
    )


async def describe(
    session: aiohttp.ClientSession,
    modules: Mapping[str, ModuleType],
    supplier: str,
    contract_id: str,
    region: str,
    month: date,
) -> str:
    """One line on what the search found, for a live-check row."""
    try:
        results = await asyncio.wait_for(
            find(session, modules, supplier, contract_id, region, month),
            _SEARCH_TIMEOUT_S,
        )
    except Exception as err:  # report only: a search that fails says so
        return f"card finder: the search failed: {type(err).__name__}: {err}"
    found = [f"{url} ({where})" for url, where, reason in results if reason is None]
    if found:
        return f"card finder: {month:%Y-%m}'s card at {', '.join(found)}"
    if not results:
        return f"card finder: {supplier}'s pages list no card for it"
    return (
        f"card finder: none of the {len(results)} addresses {supplier}'s pages "
        f"give names {month:%Y-%m}"
    )


async def annotate(
    checks: list[Any],
    session: aiohttp.ClientSession,
    modules: Mapping[str, ModuleType],
    today: date,
) -> None:
    """Append what the search found to every failing row of a card it
    covers that failed to fetch or is still the month before's. Report
    only: no row changes from failing to passing or back."""
    month = today.replace(day=1)
    rows: dict[tuple[str, str, str], list[int]] = {}
    for index, check in enumerate(checks):
        if check.ok or check.kind != "extractor" or check.expected:
            continue
        head, _, what = check.label.partition(": ")
        parts = head.split("/")
        if what not in _TRIGGERS or len(parts) != 3:
            continue
        supplier, contract_id, region = parts
        if _covered(modules, supplier, contract_id):
            rows.setdefault((supplier, contract_id, region), []).append(index)
    for (supplier, contract_id, region), indexes in rows.items():
        note = await describe(session, modules, supplier, contract_id, region, month)
        for index in indexes:
            check = checks[index]
            checks[index] = replace(
                check, detail=f"{check.detail}; {note}" if check.detail else note
            )


async def _main(args: argparse.Namespace) -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import live_check  # type: ignore[import-not-found]

    modules = live_check._load_providers()
    if not _covered(modules, args.supplier, args.contract):
        print(f"no finder for {args.supplier}/{args.contract}", file=sys.stderr)
        return 2
    if args.month:
        month = datetime.strptime(args.month, "%Y-%m").date()
    else:
        month = datetime.now(ZoneInfo("Europe/Brussels")).date().replace(day=1)
    async with aiohttp.ClientSession() as session:
        results = await find(
            session, modules, args.supplier, args.contract, args.region, month
        )
    for url, where, reason in results:
        print(f"{reason or 'names ' + format(month, '%Y-%m')}: {url} ({where})")
    return 0 if any(reason is None for _url, _where, reason in results) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("supplier")
    parser.add_argument("contract")
    parser.add_argument("region")
    parser.add_argument(
        "month", nargs="?", help="YYYY-MM; the running month by default"
    )
    return asyncio.run(_main(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())

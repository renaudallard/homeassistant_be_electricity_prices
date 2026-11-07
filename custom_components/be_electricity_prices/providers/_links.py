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


"""Where a card link read off a supplier's page or API answer may lead.

The integration fetches a card wherever the page or answer points, and
``checked_request`` holds only to https and public addresses, so a page
changed to link another host would have Home Assistant fetch whatever
that host serves. Each supplier is held to where its cards really sit.
"""

from __future__ import annotations

from collections.abc import Collection
from urllib.parse import urljoin

from yarl import URL


def on_hosts(url: str, hosts: Collection[str]) -> str | None:
    """``url`` where it is an https address on one of ``hosts``, else None.

    For a supplier whose cards sit on hosts of their own, named from where
    the card archive and the live cards found them.
    """
    try:
        parsed = URL(url)
    except ValueError:
        return None
    # raw_host is the ASCII name aiohttp connects to. host decodes an
    # "xn--" label and raises on one that does not decode, which a single
    # stray link would turn into a failed card lookup.
    if parsed.scheme != "https" or parsed.port != 443 or parsed.raw_host not in hosts:
        return None
    return url


def on_page_site(page: str, href: str) -> str | None:
    """``href`` read as a link on ``page``, or None where it leaves the
    page's own site.

    For the links a supplier's page lists. Every card such a page has
    linked sits on that page's own site: Brusol's on www.brusol.be,
    Cociter's on www.cociter.be, Aspiravi's on aspiravi-energy.be and
    Trevion's on trevion.be, in the card archive and on 2026-11-06 alike.
    A link anywhere else is one the page was changed to carry, and
    following it would fetch whatever that page points at.
    """
    try:
        host = URL(page).raw_host
    except ValueError:
        return None
    return on_hosts(urljoin(page, href), () if host is None else (host,))

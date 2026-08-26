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

"""Time a supplier endpoint from wherever this runs.

A supplier fetch that times out in CI and succeeds from a workstation is a
different fault from one that is simply slow, and the two are indistinguishable
in the live-check report: both surface as ``TimeoutError``. This repository has
already had the first kind, where Mega began timing out only from Actions
runners while serving a residential connection normally, and diagnosing it from
the report alone was not possible.

So this prints where the time goes, from whichever vantage point invokes it.
Run it locally and from ``.github/workflows/endpoint_probe.yml`` and compare:

* similar timings from both, near the timeout -> the supplier is slow, and the
  timeout is the number to argue about
* fast locally, timing out from CI -> the runner's egress is being treated
  differently, and no timeout value fixes that

It deliberately reuses the integration's own client settings, so what it
measures is what the extractor would experience: the same aiohttp session, the
same ``User-Agent``, and by default the same 30 s ceiling that
``fetch_pdf_text_layout`` applies. Probing with curl instead would answer a
question nobody asked.

Never part of CI's gating: it takes no position on pass or fail, it reports.
"""

from __future__ import annotations

import argparse
import asyncio
import socket
import ssl
import sys
import time
from urllib.parse import urlsplit

import aiohttp

sys.path.insert(0, ".")

# Imported rather than hardcoded so the probe cannot drift from the header the
# extractors actually send; a supplier filtering on it would otherwise be
# invisible here. Imported through the package, not by file path: _pdf uses
# relative imports and cannot be loaded standalone.
from custom_components.be_electricity_prices.providers._pdf import (  # noqa: E402
    USER_AGENT,
)

# The endpoints worth watching, and why each is here rather than every URL in
# the tree. energie.be timed out on every attempt of two consecutive live runs,
# which is what prompted this. Frank and Ecofix timed out once each in the same
# window and recovered, so they are the control: if all three look alike from
# one vantage point and differ from the other, the difference is the vantage
# point.
_DEFAULT_URLS: tuple[str, ...] = (
    "https://energie-production-api.azurewebsites.net"
    "/api/v1/data/document?key=DynamicTariffs",
    "https://8navd656.api.sanity.io/v2023-01-01/data/query/production-be",
    "https://portal.ecofixgp.be/docs/prices/current/EL_Ecofix_Motion_NL.pdf",
)


def _resolve(host: str) -> tuple[float, list[str]]:
    """DNS resolution time in seconds, and the addresses returned."""
    start = time.monotonic()
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except OSError as err:
        return time.monotonic() - start, [f"<{type(err).__name__}: {err}>"]
    return time.monotonic() - start, sorted({str(i[4][0]) for i in infos})


async def _attempt(
    session: aiohttp.ClientSession, url: str, timeout: int
) -> dict[str, object]:
    """One request, timed at the points that distinguish the two faults.

    ``connect`` covers TCP plus TLS, ``ttfb`` ends when the response headers
    land, and ``total`` includes reading the body. A block on the runner's
    egress shows as a connect that never completes; a slow supplier shows as a
    long ttfb with a normal connect.
    """
    row: dict[str, object] = {"url": url}
    start = time.monotonic()
    # The trace hook is attached to the SESSION (see _probe) and writes into
    # this dict, which aiohttp hands it as trace_request_ctx. Building a
    # TraceConfig here instead silently measures nothing: a config attached to
    # no session is never called, which is how the first version of this
    # reported every connect as None.
    ctx: dict[str, float] = {"start": start}

    try:
        async with session.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=aiohttp.ClientTimeout(total=timeout),
            trace_request_ctx=ctx,
        ) as resp:
            row["status"] = resp.status
            row["ttfb"] = round(time.monotonic() - start, 3)
            body = await resp.read()
            row["total"] = round(time.monotonic() - start, 3)
            row["bytes"] = len(body)
            row["content_type"] = resp.headers.get("Content-Type", "")
    except (TimeoutError, aiohttp.ClientError) as err:
        row["total"] = round(time.monotonic() - start, 3)
        row["error"] = f"{type(err).__name__}: {err}"
    connected = ctx.get("connected")
    row["connect"] = None if connected is None else round(connected - start, 3)
    return row


async def _probe(urls: list[str], attempts: int, timeout: int) -> int:
    trace = aiohttp.TraceConfig()

    async def _on_connected(_session: object, ctx: object, _params: object) -> None:
        request_ctx = getattr(ctx, "trace_request_ctx", None)
        if isinstance(request_ctx, dict):
            request_ctx["connected"] = time.monotonic()

    trace.on_connection_create_end.append(_on_connected)

    ssl_ctx = ssl.create_default_context()
    # force_close so every attempt pays the full TCP + TLS cost. Pooling would
    # hand attempts 2 and 3 a warm connection and hide exactly the handshake
    # this is here to time.
    connector = aiohttp.TCPConnector(ssl=ssl_ctx, force_close=True)
    failures = 0
    async with aiohttp.ClientSession(
        connector=connector, trace_configs=[trace]
    ) as session:
        for url in urls:
            host = urlsplit(url).netloc
            dns_s, addrs = _resolve(host)
            print(f"\n## {host}")
            print(f"    url      {url}")
            print(f"    dns      {dns_s:.3f}s -> {', '.join(addrs)}")
            for i in range(1, attempts + 1):
                row = await _attempt(session, url, timeout)
                if "error" in row:
                    failures += 1
                    print(
                        f"    try {i}    FAIL after {row['total']}s "
                        f"(connect {row['connect']}s) {row['error']}"
                    )
                else:
                    print(
                        f"    try {i}    {row['status']} "
                        f"connect {row['connect']}s  ttfb {row['ttfb']}s  "
                        f"total {row['total']}s  {row['bytes']} bytes  "
                        f"{row['content_type']}"
                    )
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "urls", nargs="*", default=list(_DEFAULT_URLS), help="URLs to probe"
    )
    parser.add_argument("--attempts", type=int, default=3)
    parser.add_argument(
        "--timeout",
        type=int,
        default=30,
        help="per-request ceiling; 30 matches fetch_pdf_text_layout's default",
    )
    args = parser.parse_args()
    print(f"user-agent: {USER_AGENT}")
    print(f"attempts:   {args.attempts}   timeout: {args.timeout}s")
    failures = asyncio.run(_probe(args.urls, args.attempts, args.timeout))
    print(f"\n{failures} failed request(s) across {args.attempts} attempt(s) each")
    # Always 0: this reports, it does not gate. A supplier being slow today is
    # not a reason to fail a workflow someone ran to find out whether it is.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

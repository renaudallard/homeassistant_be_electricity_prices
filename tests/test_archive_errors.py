"""Every provider's archive lookup raises on a transient failure.

The month cache writes its retry marker only when ``fetch_for_month`` RAISES.
A lookup that turned a timeout into ``None`` had the month cached as "no
archive has this month" for the provisional TTL, billed on the current card
as a proxy, and written into the recorder by any backfill that ran inside
that day. DATS 24 always re-raised; the others returned None.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import aiohttp
import pytest

from custom_components.be_electricity_prices.providers import all_extractors
from custom_components.be_electricity_prices.providers.base import ExtractorError


class _Down:
    """An aiohttp session whose every request fails at the socket."""

    def __init__(self) -> None:
        self.calls = 0

    def get(self, *args: Any, **kwargs: Any) -> "_Down":
        self.calls += 1
        return self

    def head(self, *args: Any, **kwargs: Any) -> "_Down":
        self.calls += 1
        return self

    def post(self, *args: Any, **kwargs: Any) -> "_Down":
        self.calls += 1
        return self

    async def __aenter__(self) -> None:
        raise aiohttp.ClientConnectionError("connection reset")

    async def __aexit__(self, *args: Any) -> None:
        return None


_ARCHIVED = [ext for ext in all_extractors() if ext.fetch_for_month is not None]


@pytest.mark.parametrize("extractor", _ARCHIVED, ids=[e.id for e in _ARCHIVED])
async def test_a_transient_failure_is_raised_not_cached_as_no_archive(
    extractor: Any,
) -> None:
    """For every contract and region the supplier archives, a lookup that
    reached the network and failed there must raise; one that never reached
    it (no archive for that product or region) may still answer None."""
    assert extractor.fetch_for_month is not None
    reached = 0
    for contract in extractor.contracts:
        for region in sorted(contract.regions):
            session = _Down()
            try:
                result = await extractor.fetch_for_month(
                    session,  # type: ignore[arg-type]
                    contract.id,
                    region,
                    date(2026, 6, 1),
                )
            except ExtractorError:
                assert session.calls, (contract.id, region)
                reached += 1
                continue
            assert not session.calls or result is not None, (
                f"{extractor.id} {contract.id} {region}: a failed fetch came back "
                f"as None after {session.calls} request(s)"
            )
    assert reached, f"{extractor.id}: no lookup reached the network"

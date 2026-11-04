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

"""The card an earlier contract of the year is priced on: the one its
supplier publishes today, the archive's reading of one published as page
images, the newest one an archive kept inside the contract's days, or the
entry's own as the last resort.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any, cast

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .compare_inputs import _QuoteEntry
from .const import CONF_CONTRACT, CONF_DSO, CONF_REGION, CONF_SUPPLIER, SUPPLIER_CUSTOM
from .providers import get as get_extractor
from .providers._pdf import is_transient_fetch_error
from .providers._resolve import without_welcome_credit
from .providers.base import CardNotReadableError, SupplierExtractor, SupplierSnapshot
from .providers.custom import build_snapshot as build_custom_snapshot
from .snapshot_months import month_card, month_card_failed
from .snapshot_resolve import _resolve_snapshot, entry_annual_kwh
from .snapshot_stand_ins import card_for_unreadable_month
from .snapshot_store import fetch_shared

if TYPE_CHECKING:
    from .contract_periods import ContractPeriod

_LOGGER = logging.getLogger(__name__)


async def _current_card(
    hass: HomeAssistant,
    session: aiohttp.ClientSession,
    extractor: SupplierExtractor,
    proxy: ConfigEntry,
) -> tuple[SupplierSnapshot | None, bool, bool]:
    """The old contract's card as its supplier publishes it today, as parsed,
    or ``None`` when the supplier cannot be reached, whether it was read off
    the archive's OCR reading, and whether a ``None`` is a read that failed
    just now (a timeout, a 5xx) rather than a card that is gone.

    A card published as page images downloads fine and no parser here can read
    it: Ecofix's, every day since August 2026. The repository's archive reads
    those off their pixels, and the live tick and the compare page price such
    a card on that row (``card_for_unreadable_month``), so an earlier contract
    on one is priced on it too rather than on another supplier's card."""
    data = proxy.data
    region = data.get(CONF_REGION, "")
    contract = data.get(CONF_CONTRACT, "")
    read_by_ocr = False
    read_failed = False
    if extractor.id == SUPPLIER_CUSTOM:
        raw: SupplierSnapshot | None = build_custom_snapshot(
            data, region, data.get(CONF_DSO, "")
        )
    else:
        fetched = await fetch_shared(
            hass,
            session,
            extractor,
            contract,
            region,
            supplier=extractor.id,
            record_failure=False,
        )
        raw = fetched.row.snapshot if fetched.row is not None else None
        # Classified as the coordinator classifies its own card's fetch.
        read_failed = isinstance(
            fetched.error, TimeoutError
        ) or is_transient_fetch_error(fetched.error_message)
        if raw is None and isinstance(fetched.error, CardNotReadableError):
            try:
                archived = await card_for_unreadable_month(
                    session, extractor.id, contract, region, dt_util.now().date(), proxy
                )
            except (
                Exception
            ) as err:  # a blip on the archive is not this period's problem
                _LOGGER.debug(
                    "card archive read failed for %s/%s: %s",
                    extractor.id,
                    contract,
                    err,
                )
                archived = None
                read_failed = True
            if archived is not None:
                raw = archived.snapshot
                read_by_ocr = archived.read_by_ocr
    if raw is None:
        return None, False, read_failed
    return raw, read_by_ocr, False


async def _latest_archived_card(
    hass: HomeAssistant,
    session: aiohttp.ClientSession,
    extractor: SupplierExtractor,
    proxy: ConfigEntry,
    period: ContractPeriod,
) -> tuple[SupplierSnapshot | None, bool]:
    """The newest card an archive holds for the old contract inside its days,
    and, when there is none, whether a month's read failed just now.

    For a supplier that has left the market and no longer publishes one: DATS
    24's cards answer 404 since September 2026, and the project's archive keeps
    the months it captured. ``month_card`` answers None for a month no archive
    holds and for a month whose read failed, which ``month_card_failed`` tells
    apart.
    """
    data = proxy.data
    contract = data.get(CONF_CONTRACT, "")
    region = data.get(CONF_REGION, "")
    month = date(period.end.year, period.end.month, 1)
    first = date(period.start.year, period.start.month, 1)
    failed = False
    while month >= first:
        card = await month_card(
            hass, session, extractor, contract, region, month, proxy, nearest_area=True
        )
        if card is not None:
            return card, False
        failed = failed or month_card_failed(
            hass, extractor.id, contract, region, month
        )
        month = (month - timedelta(days=1)).replace(day=1)
    return None, failed


async def period_card(
    hass: HomeAssistant,
    session: aiohttp.ClientSession,
    coordinator: Any,
    period: ContractPeriod,
    overrides: Mapping[str, Any] | None = None,
) -> tuple[
    ConfigEntry,
    SupplierExtractor,
    SupplierSnapshot | None,
    bool,
    bool,
    bool,
    SupplierSnapshot | None,
]:
    """The stand-in entry, extractor and card an earlier contract is priced on,
    whether that card is the entry's own standing in, whether it stands in
    because a read failed just now (``PricedPeriod.read_failed``), whether
    it was read off the archive's OCR reading, and the card it was resolved
    from.

    The last is what the walk resolves for a month no archive holds, which
    owes that month's federal levies rather than today's
    (``_snapshot_for_month``): the card as parsed, the entry's own
    (``_snapshot_raw``) where that stands in, and ``None`` for an archived
    card, which was resolved for its own month.

    The card is the one its supplier publishes today, resolved for this
    household, and each month still bills on its own archived card through the
    walk: this one only stands in for a month no archive holds, exactly as the
    entry's current card does for its own contract. A card published as page
    images is the archive's reading of it (``_current_card``). A supplier that
    no longer publishes one falls back to the newest card an archive kept
    inside the period, and one with neither to the entry's current card, the
    last resort. ``None`` only when the entry has no card of its own either.

    A stand-in prices the days and nothing the card offers a new customer: it
    is walked with the old contract's start date, so a welcome credit left on
    it was credited to a contract that never signed that card, 259 EUR of Mega
    ristourne on a DATS 24 year.

    Raises ``ExtractorError`` for a supplier the registry no longer knows.
    """
    proxy = cast(
        ConfigEntry,
        _QuoteEntry(
            data={**period.data, **(overrides or {})}, runtime_data=coordinator
        ),
    )
    extractor = get_extractor(str(period.data.get(CONF_SUPPLIER, "")))
    fallback = getattr(coordinator, "_snapshot", None)
    raw, read_by_ocr, read_failed = await _current_card(hass, session, extractor, proxy)
    card = (
        None
        if raw is None
        else _resolve_snapshot(proxy, raw, annual_kwh=entry_annual_kwh(proxy))
    )
    if card is None and fallback is not None:
        card, archive_failed = await _latest_archived_card(
            hass, session, extractor, proxy, period
        )
        read_failed = read_failed or archive_failed
    if card is None:
        stand_in = None if fallback is None else without_welcome_credit(fallback)
        own_raw = getattr(coordinator, "_snapshot_raw", None)
        return proxy, extractor, stand_in, True, read_failed, False, own_raw
    return proxy, extractor, card, False, False, read_by_ocr, raw

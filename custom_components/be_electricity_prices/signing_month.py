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

"""The month a household signed its contract, and what a card read for a
later month owes it from the one it signed.

Split out of ``cohort`` so the resolver can ask it: ``cohort`` imports the
resolver, and the signing month is a fact about the entry like the others
``_resolve_snapshot`` bakes in.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from typing import Any

from homeassistant.config_entries import ConfigEntry

from .const import (
    CONF_CONTRACT,
    CONF_CONTRACT_START_DATE,
    CONF_SUPPLIER,
    CONF_TARIFF_CARD_DATE,
)
from .providers import feed_in_floor_from
from .providers.base import SupplierSnapshot


def _parse_iso_date(value: Any) -> date | None:
    """Parse a stored ISO ``YYYY-MM-DD`` date string, or ``None``.

    Accepts the DateSelector return value used for the contract lifecycle
    fields; returns ``None`` for a missing / malformed value.
    """
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _tariff_card_month(entry: ConfigEntry) -> date | None:
    """First-of-month of the card this contract is billed on, or ``None``.

    A fixed or dynamic contract is locked to the card in force when it was
    SIGNED, which is not always the month supply began: a supplier switch
    takes about a month to go through, so a customer who signed in June is
    supplied from July on June's card, and energie.be goes as far as printing
    that month in the product name ("... 06/26"). Reading the start date as
    the card month therefore billed a switcher one card too late, which on
    Eneco's fixed card between July and August 2026 is 0,1681 against 0,2028
    EUR/kWh, 121 EUR/yr at 3500 kWh (issue #96).

    So the card month is its own optional field, and the start date is the
    fallback for every entry that does not set one, which is the behaviour
    they already had. The day within the month is irrelevant to which monthly
    card applies, so normalise to the first.
    """
    d = _parse_iso_date(entry.data.get(CONF_TARIFF_CARD_DATE)) or _parse_iso_date(
        entry.data.get(CONF_CONTRACT_START_DATE)
    )
    if d is None:
        return None
    return date(d.year, d.month, 1)


def resolve_signed_feed_in_floor(
    entry: ConfigEntry, snap: SupplierSnapshot
) -> SupplierSnapshot:
    """``snap`` without the feed-in floor where the card the household signed
    did not grant one.

    A product read off a later card than the one signed takes its terms, and
    a guaranteed minimum is not always among the earlier issue's: EnergyVision's
    Goedkope stroom put "garanderen wij in elk geval 1 €cent/kWh" on its June
    2025 card (``Contract.feed_in_floor_from``), and the November 2024 to
    March 2025 issues print no floor. A signing month before that drops the
    floor; one from it, or none known, keeps the card read's terms.

    Only on the entry's own contract, the one the signing month is about: a
    proxy carrying this entry's dates and another contract's card keeps that
    card's floor.
    """
    since = feed_in_floor_from(snap.supplier, snap.contract)
    inj = snap.injection
    if since is None or inj is None or inj.minimum is None:
        return snap
    data = entry.data
    if (data.get(CONF_SUPPLIER), data.get(CONF_CONTRACT)) != (
        snap.supplier,
        snap.contract,
    ):
        return snap
    signed = _tariff_card_month(entry)
    if signed is None or signed >= since:
        return snap
    return replace(snap, injection=replace(inj, minimum=None))

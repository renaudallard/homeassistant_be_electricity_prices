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

"""The contract's dates and the supplier switch, as the flow asks for them.

The start, end and tariff-card dates the contract step offers and checks,
and the options menu's record-a-switch and remove-the-last-switch pair:
the one date a switch asks for, the checks it has to pass, and what
recording or removing it does to the entry's data. Split out of
``flow_schemas.py``; ``config_flow.py`` keeps the step handlers.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any

import voluptuous as vol
from homeassistant.helpers.selector import (
    BooleanSelector,
    DateSelector,
)
from homeassistant.util import dt as dt_util

from .const import (
    CONF_CONTRACT,
    CONF_CONTRACT_END_DATE,
    CONF_CONTRACT_START_DATE,
    CONF_MANUAL_ENERGY_BASE,
    CONF_MANUAL_ENERGY_EXCLUSIVE_NIGHT,
    CONF_MANUAL_ENERGY_FACTOR,
    CONF_MANUAL_ENERGY_OFFPEAK,
    CONF_MANUAL_ENERGY_PEAK,
    CONF_MANUAL_ENERGY_SINGLE,
    CONF_MANUAL_YEARLY_FEE,
    CONF_PREVIOUS_CONTRACTS,
    CONF_QUARTER_HOURLY,
    CONF_REGION,
    CONF_SUPPLIER,
    CONF_SWITCH_DATE,
    CONF_TARIFF_CARD_DATE,
    CONF_YTD_FROM_CONTRACT_START,
)

# The three optional dates the contract step asks.
_CONTRACT_DATE_KEYS: tuple[str, ...] = (
    CONF_CONTRACT_START_DATE,
    CONF_TARIFF_CARD_DATE,
    CONF_CONTRACT_END_DATE,
)


def _add_contract_date_fields(fields: dict[Any, Any], defaults: dict[str, Any]) -> None:
    """Append the optional contract start / tariff card / end date pickers.

    Pre-filled with the stored value as a *suggestion* (not a default) on the
    options / reconfigure pass, so blanking the picker truly omits the key from
    ``user_input``: the step handler then pops it, which is how a date is
    cleared. A ``default`` would re-inject the stored value on a blank submit,
    making the date unclearable.
    """
    date_selector = DateSelector()
    for key in _CONTRACT_DATE_KEYS:
        stored = defaults.get(key)
        if stored:
            fields[vol.Optional(key, description={"suggested_value": stored})] = (
                date_selector
            )
        else:
            fields[vol.Optional(key)] = date_selector
    # Sits with the dates because it is meaningless without a start date, and
    # this is the only step that collects one. A plain default (rather than a
    # suggested_value) is right here: an unticked box DOES reach user_input as
    # False, so there is nothing to clear and nothing to re-inject.
    fields[
        vol.Optional(
            CONF_YTD_FROM_CONTRACT_START,
            default=bool(defaults.get(CONF_YTD_FROM_CONTRACT_START, False)),
        )
    ] = BooleanSelector()


def _validate_contract_dates(
    user_input: dict[str, Any], data: Mapping[str, Any] | None = None
) -> dict[str, str]:
    """Reject a future start or card date, an end date not after the start, or
    a start before the last recorded supplier switch.

    All three fields are independently optional: an end date without a start
    date is fine (a bare renewal reminder), so the ordering check only fires
    when both are present.

    The card date is checked only against today. It is NOT required to fall on
    or before the start date, which looks like the obvious guard and is wrong:
    a renewal re-signs a supply that began years ago onto this month's card, so
    a card date after the start date is as ordinary as one before it.

    ``data`` is the entry's settings, for its recorded switches. The contract
    configured is the one supplying since the last of them, so it cannot have
    started before it: the year would price the contract left only from that
    start, and the cohort would look the signing card up a month too early.
    """
    from .cohort import _parse_iso_date
    from .contract_periods import recorded_contracts

    errors: dict[str, str] = {}
    start = _parse_iso_date(user_input.get(CONF_CONTRACT_START_DATE))
    card = _parse_iso_date(user_input.get(CONF_TARIFF_CARD_DATE))
    end = _parse_iso_date(user_input.get(CONF_CONTRACT_END_DATE))
    today = dt_util.now().date()
    records = recorded_contracts(data or {})
    if start is not None and start > today:
        errors[CONF_CONTRACT_START_DATE] = "start_date_in_future"
    elif start is not None and records and start < records[-1][0]:
        errors[CONF_CONTRACT_START_DATE] = "start_date_before_switch"
    if card is not None and card > today:
        errors[CONF_TARIFF_CARD_DATE] = "card_date_in_future"
    if start is not None and end is not None and end <= start:
        errors[CONF_CONTRACT_END_DATE] = "end_before_start"
    return errors


_MANUAL_RATE_KEYS: tuple[str, ...] = (
    CONF_MANUAL_ENERGY_SINGLE,
    CONF_MANUAL_ENERGY_PEAK,
    CONF_MANUAL_ENERGY_OFFPEAK,
    CONF_MANUAL_ENERGY_EXCLUSIVE_NIGHT,
    CONF_MANUAL_ENERGY_FACTOR,
    CONF_MANUAL_ENERGY_BASE,
    CONF_MANUAL_YEARLY_FEE,
)


def _another_contract(stored: Mapping[str, Any], data: Mapping[str, Any]) -> bool:
    """Whether ``data`` has moved off the stored entry's supplier, contract or
    region, so the stored contract's card and typed signing rate no longer
    describe the one being set up."""
    return any(
        data.get(key) != stored.get(key)
        for key in (CONF_SUPPLIER, CONF_CONTRACT, CONF_REGION)
    )


def _drop_left_contract_answers(
    stored: Mapping[str, Any], data: dict[str, Any]
) -> None:
    """Forget the stored contract's dates and settlement once an edit has moved
    to another supplier or region, so their steps start from nothing.

    Run on the entry step, before a contract is picked, where moving off the
    stored supplier or region is what ``_another_contract`` reads. Pre-filled,
    the dates were submitted as the new contract's own, and the card month put
    it on the card of the month the old one was signed, which a newly typed
    start date does not override (``_tariff_card_month``). A supply that
    starts on 1 January cannot be recorded as a switch, so this edit is how
    such a change is made. The box billing the year from the start date goes
    with them, as ``_record_switch`` drops it. So does the settlement answer,
    which is the household's arrangement with the supplier it leaves: ticked
    for Frank's quarter-hour grid and submitted as shown, it made a Bolt
    variable card dynamic. Only an answer still holding the stored value goes:
    the start date a recorded switch has just moved to the switch day is the
    new contract's own. So is a stored start date on the day of the last
    switch recorded this year (``_removable_switch``), whichever supplier the
    edit moves to: it is the day the contract after the switch began, so
    correcting the supplier picked at the switch keeps it.
    """
    if not _another_contract(stored, data):
        return
    switch = _removable_switch(stored, dt_util.now().date())
    starts_on_switch = switch is not None and (
        stored.get(CONF_CONTRACT_START_DATE) == switch[0].isoformat()
    )
    for key in (
        *_CONTRACT_DATE_KEYS,
        CONF_YTD_FROM_CONTRACT_START,
        CONF_QUARTER_HOURLY,
    ):
        if key == CONF_CONTRACT_START_DATE and starts_on_switch:
            continue
        if key in data and data[key] == stored.get(key):
            del data[key]


def _carried_card_month(
    stored: Mapping[str, Any] | None,
    data: Mapping[str, Any],
    user_input: Mapping[str, Any],
) -> bool:
    """Whether the contract step came back on another contract with the stored
    contract's card month as the form showed it.

    The step's schema is built before the contract is picked, so on an edit
    that keeps the supplier and region the box offers the month the contract
    being left was signed, and that month wins over the start date. A month
    typed differently is the new contract's own, and so is any month once the
    box has been asked again empty (``data`` then holds none).
    """
    if stored is None or user_input.get(CONF_CONTRACT) == stored.get(CONF_CONTRACT):
        return False
    month = user_input.get(CONF_TARIFF_CARD_DATE)
    return month is not None and month == data.get(CONF_TARIFF_CARD_DATE)


def _switch_schema(today: date) -> vol.Schema:
    """The one question a supplier switch asks: the new contract's first day."""
    return vol.Schema(
        {vol.Required(CONF_SWITCH_DATE, default=today.isoformat()): DateSelector()}
    )


def _validate_switch_date(
    data: Mapping[str, Any], user_input: dict[str, Any]
) -> dict[str, str]:
    """Refuse a switch date that prices nothing, or that overlaps a recorded one.

    It has to fall after 1 January, so the contract being left has at least one
    day this year, and not after today, since the day is when the new contract
    started supplying, not when it will. And after the last switch recorded,
    because each contract ends where the next begins, and after the start date
    of the contract being left, which cannot end before it began.
    """
    from .cohort import _parse_iso_date
    from .contract_periods import recorded_contracts

    until = _parse_iso_date(user_input.get(CONF_SWITCH_DATE))
    today = dt_util.now().date()
    if until is None or until > today or until <= date(today.year, 1, 1):
        return {CONF_SWITCH_DATE: "switch_date_outside_year"}
    records = recorded_contracts(data)
    if records and until <= records[-1][0]:
        return {CONF_SWITCH_DATE: "switch_date_before_last"}
    # The contract being left cannot end before it began.
    started = _parse_iso_date(data.get(CONF_CONTRACT_START_DATE))
    if started is not None and until <= started:
        return {CONF_SWITCH_DATE: "switch_date_before_start"}
    return {}


def _record_switch(data: Mapping[str, Any], until: date) -> dict[str, Any]:
    """The entry's settings with its current contract kept as the one held until
    the day before ``until``, ready for the new contract to be picked.

    The settings are kept whole, so the old contract is priced on exactly what
    was configured for it. A switch from an earlier year prices nothing this
    year and is dropped. The start date moves to the switch day, since the
    contract now being set up is the new one, and the answers that belonged to
    the old contract go: its card month, a typed signing rate and its end date,
    none of which describe the new one. So does the box that bills the year
    from the contract start, which would leave the old contract out of the year.
    The meter's day hours are not kept with the old contract: every contract
    reads the entry's (``_ENTRY_FACTS``), so a later correction reaches it.
    """
    from .contract_periods import _ENTRY_FACTS, recorded_contracts

    held = {
        key: value
        for key, value in data.items()
        if key != CONF_PREVIOUS_CONTRACTS and key not in _ENTRY_FACTS
    }
    kept = [
        {"until": when.isoformat(), "data": dict(settings)}
        for when, settings in recorded_contracts(data)
        if when > date(until.year, 1, 1)
    ]
    out = {
        **data,
        CONF_PREVIOUS_CONTRACTS: [*kept, {"until": until.isoformat(), "data": held}],
        CONF_CONTRACT_START_DATE: until.isoformat(),
    }
    for key in (
        CONF_TARIFF_CARD_DATE,
        CONF_CONTRACT_END_DATE,
        CONF_YTD_FROM_CONTRACT_START,
        *_MANUAL_RATE_KEYS,
    ):
        out.pop(key, None)
    return out


def _removable_switch(
    data: Mapping[str, Any], today: date
) -> tuple[date, Mapping[str, Any]] | None:
    """The last recorded switch, when it falls in ``today``'s year.

    Only such a switch prices anything, so only it can be a mistake worth
    undoing. One recorded in an earlier year is a real change of supplier
    long settled, kept until the next switch drops it, and removing it would
    put back a contract the household left before the year began.
    """
    from .contract_periods import recorded_contracts

    records = recorded_contracts(data)
    if not records or records[-1][0] <= date(today.year, 1, 1):
        return None
    return records[-1]


def _remove_last_switch(data: Mapping[str, Any]) -> dict[str, Any]:
    """The entry's settings as they stood before its last switch was recorded.

    ``_record_switch`` keeps those settings whole as the contract held until the
    switch, so they are put back as they were: the contract being left, its
    start date, card month, signing rate, end date and year-to-date box. The
    switches recorded before it stay. A switch recorded with the wrong date, or
    that never happened, has no other way out: a new one must be later than the
    last, and it would keep the contract set up since as the one left. The one
    exception is a question added after the switch was recorded: the copy holds
    no answer to it, so the entry's answer is kept rather than lost, the way
    the earlier contract's period reads it while the switch stands. The
    meter's day hours are the entry's whatever the copy holds, as they are
    while the switch stands.
    """
    from .contract_periods import (
        _unasked_facts,
        _with_entry_facts,
        recorded_contracts,
    )

    records = recorded_contracts(data)
    if not records:
        return dict(data)
    held = _with_entry_facts(records[-1][1], data)
    held.update(_unasked_facts(held, data))
    held.pop(CONF_PREVIOUS_CONTRACTS, None)
    kept = [
        {"until": when.isoformat(), "data": dict(settings)}
        for when, settings in records[:-1]
    ]
    if kept:
        held[CONF_PREVIOUS_CONTRACTS] = kept
    return held

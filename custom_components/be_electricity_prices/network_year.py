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

"""A card billed in a year after its own: the network of the new year.

A card prints the network tariffs of the year it prices. Held into January, a
December card billed the new year's months on the old table until the
supplier's next card was out, though the operators' tariffs change on 1
January whatever a supplier publishes. Once the regulator's sheet of the
delivery year itself is read, such a month takes its network off it, as a
withdrawn product's months past its last card do
(``snapshot_resolve.past_last_card``). Until then the card's table stands:
the sheet of the year before would bill what the card already prints.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import date
from typing import Any

from .brugel import own_network_terms
from .const import (
    CONF_DSO,
    CONF_REGION,
    REGION_BRUSSELS,
    REGION_FLANDERS,
    REGION_WALLONIA,
)
from .cwape import cached_network as cached_cwape_network
from .fluvius import held_area_terms
from .providers._resolve import on_card_basis
from .providers._validity import card_valid_until
from .providers.base import DsoOverlay, SupplierSnapshot


def card_year(snap: SupplierSnapshot) -> int | None:
    """The year a card prices, by the last day it states or else the month
    its title names, or None when it names neither."""
    end = card_valid_until(snap.valid_until, snap.publication_label)
    return None if end is None else end.year


def billed_after_its_year(snap: SupplierSnapshot, month: date) -> bool:
    """Whether ``month`` falls in a calendar year after the one ``snap``
    prices. Never for a card that cannot be dated."""
    year = card_year(snap)
    return year is not None and month.year > year


def own_year_sheet(region: str, month: date, dso: str) -> dict[str, float | None]:
    """The overlay fields ``region``'s sheet of ``month``'s own year gives
    ``dso``, excluding VAT, or none while that sheet is not read: Fluvius's
    area sheet, Brugel's final sheet, or the CWaPE's operator and transport
    grids that began in that year, each of those standing on its own."""
    if region == REGION_FLANDERS:
        held = held_area_terms(month.year, dso)
        if held is None or held[0] != month.year:
            return {}
        return held[1].overlay_fields()
    if region == REGION_BRUSSELS:
        terms = own_network_terms(month.year)
        return {} if terms is None else terms.overlay_fields()
    read: dict[str, float | None] = {}
    if region == REGION_WALLONIA:
        for grid in cached_cwape_network(month, dso):
            if grid is not None and grid.valid_from.year == month.year:
                read.update(grid.overlay_fields())
    return read


def on_sheet(
    snap: SupplierSnapshot,
    month: date,
    overlay: DsoOverlay,
    fields: Mapping[str, float | None],
) -> DsoOverlay:
    """``overlay`` with the fields a regulator's sheet sets, each put on the
    basis the entry typed on, and the typed figure dropped where the sheet
    leaves a field ``None``."""
    changes: dict[str, Any] = {
        field: None if value is None else on_card_basis(snap, month, value)
        for field, value in fields.items()
    }
    return replace(overlay, **changes)


def new_year_network(
    data: Mapping[str, Any], snap: SupplierSnapshot, month: date, region: str
) -> SupplierSnapshot:
    """``snap`` with the entry's own area billed on the sheet of ``month``'s
    year, for a month past the year the card prices, once that sheet is read
    (:func:`own_year_sheet`); ``snap`` itself otherwise. The other areas keep
    the card's figures: only the entry's own is billed."""
    if data.get(CONF_REGION) != region or not billed_after_its_year(snap, month):
        return snap
    dso = str(data.get(CONF_DSO, ""))
    overlay = snap.dsos.get(dso)
    fields = own_year_sheet(region, month, dso)
    if overlay is None or not fields:
        return snap
    return replace(
        snap, dsos={**snap.dsos, dso: on_sheet(snap, month, overlay, fields)}
    )

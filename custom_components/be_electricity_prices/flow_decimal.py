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

"""Figures typed from a bill or a card, read with a decimal comma.

A browser's number field refuses the decimal comma Belgian bills and cards
print, or sends it on as empty, so these figures are text fields, read here
when the form is submitted. The whole kWh volumes stay number fields, checked
here too: one holds a value to its bounds, and a NaN passes both.
"""

from __future__ import annotations

import functools
import math
import re
from collections.abc import Callable, Coroutine
from decimal import Decimal
from typing import Any, TypeVar, cast

import voluptuous as vol
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.helpers.selector import (
    NumberSelector,
    TextSelector,
    TextSelectorConfig,
)

# Digits with a decimal comma or point, and a minus where the field allows it.
_DECIMAL_TEXT = re.compile(r"-?\d+(?:[.,]\d+)?", re.ASCII)

_S = TypeVar("_S", bound=Callable[..., Coroutine[Any, Any, ConfigFlowResult]])


class DecimalSelector(TextSelector):
    """A text field for a figure, from ``low`` to ``high`` when given."""

    def __init__(self, low: float | None = None, high: float | None = None) -> None:
        super().__init__(TextSelectorConfig())
        self.low = low
        self.high = high


def decimal_text(value: Any) -> str:
    """A stored figure as its field shows it, never in the exponent form
    str() gives a small one, which the field would then refuse."""
    return format(Decimal(repr(float(value))), "f")


def _decimal(text: str, low: float | None, high: float | None) -> float:
    if _DECIMAL_TEXT.fullmatch(text) is None:
        raise ValueError(text)
    number = float(text.replace(",", "."))
    if (low is not None and number < low) or (high is not None and number > high):
        raise ValueError(text)
    return number


def _is_nan(value: Any) -> bool:
    return isinstance(value, float) and math.isnan(value)


def read_decimals(schema: vol.Schema, user_input: dict[str, Any]) -> dict[str, str]:
    """Read in ``user_input`` each figure of ``schema``'s decimal fields:
    a number, or gone when left blank, as ha-form leaves out a blanked
    number field. Returns an error for each that is not a number within
    its field's bounds, and for a number field holding a NaN, which the
    frontend never sends and another client may."""
    errors: dict[str, str] = {}
    for marker, selector in schema.schema.items():
        key = str(marker)
        if key not in user_input:
            continue
        if isinstance(selector, NumberSelector):
            if _is_nan(user_input[key]):
                errors[key] = "invalid_number"
            continue
        if not isinstance(selector, DecimalSelector):
            continue
        text = str(user_input[key]).strip()
        if not text:
            del user_input[key]
            continue
        try:
            user_input[key] = _decimal(text, selector.low, selector.high)
        except ValueError:
            errors[key] = "invalid_number"
    return errors


def reads_decimals(
    step_id: str,
    build: Callable[[dict[str, Any]], vol.Schema],
    placeholders: Callable[[Any], dict[str, str] | None] | None = None,
) -> Callable[[_S], _S]:
    """Read a step's figures before it runs; a figure refused brings its
    form, built by ``build`` off the flow's data, back as typed."""

    def decorate(step: _S) -> _S:
        @functools.wraps(step)
        async def wrapper(
            flow: Any, user_input: dict[str, Any] | None = None
        ) -> ConfigFlowResult:
            if user_input is not None:
                schema = build(flow._data)
                typed = dict(user_input)
                errors = read_decimals(schema, user_input)
                if errors:
                    # A NaN is not shown back: its field keeps what it held.
                    shown = {k: v for k, v in typed.items() if not _is_nan(v)}
                    return flow.async_show_form(  # type: ignore[no-any-return]
                        step_id=step_id,
                        data_schema=flow.add_suggested_values_to_schema(schema, shown),
                        errors=errors,
                        description_placeholders=(
                            None if placeholders is None else placeholders(flow)
                        ),
                    )
            return await step(flow, user_input)

        return cast(_S, wrapper)

    return decorate

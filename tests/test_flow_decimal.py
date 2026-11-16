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

"""Figures typed from a bill or a card, read with a decimal comma."""

from __future__ import annotations

from typing import Any

import pytest
import voluptuous as vol

from custom_components.be_electricity_prices.flow_decimal import (
    DecimalSelector,
    decimal_text,
    read_decimals,
)


def _read(text: str, low: float | None = 0.0, high: float | None = 50.0) -> Any:
    typed: dict[str, Any] = {"figure": text}
    schema = vol.Schema({vol.Optional("figure"): DecimalSelector(low, high)})
    errors = read_decimals(schema, typed)
    return errors.get("figure", typed.get("figure"))


@pytest.mark.parametrize(
    ("text", "read"),
    [
        ("4,6", 4.6),
        ("4.6", 4.6),
        (" 0,25 ", 0.25),
        ("50", 50.0),
        ("0", 0.0),
        ("", None),
        ("  ", None),
    ],
)
def test_a_figure_reads_with_a_comma_or_a_point(text: str, read: Any) -> None:
    """A blank figure is gone, as ha-form leaves out a blanked number field."""
    assert _read(text) == read


@pytest.mark.parametrize(
    "text", ["4,6 kVA", "50,01", "-1", "1e1", "nan", "inf", "+4", "1_0", "4,6,0", ",5"]
)
def test_a_figure_that_is_not_one_is_refused(text: str) -> None:
    assert _read(text) == "invalid_number"


def test_a_minus_only_where_the_field_takes_one() -> None:
    assert _read("-0,02", -0.5, 0.5) == -0.02
    assert _read("-0,6", -0.5, 0.5) == "invalid_number"
    assert _read("-1234,5", None, None) == -1234.5


def test_other_fields_are_left_as_they_are() -> None:
    typed: dict[str, Any] = {"figure": "1,5", "regime": "none", "box": True}
    schema = vol.Schema(
        {
            vol.Optional("figure"): DecimalSelector(0.0, 2.0),
            vol.Optional("regime"): str,
            vol.Optional("box"): bool,
        }
    )
    assert read_decimals(schema, typed) == {}
    assert typed == {"figure": 1.5, "regime": "none", "box": True}


@pytest.mark.parametrize(
    ("stored", "shown"),
    [
        (0.000075, "0.000075"),
        (2.5, "2.5"),
        (0, "0.0"),
        (0.06, "0.06"),
        (-0.02, "-0.02"),
    ],
)
def test_a_stored_figure_is_shown_as_its_field_reads_it(
    stored: float, shown: str
) -> None:
    """str() writes 0.000075 as 7.5e-05, which the field would refuse."""
    assert decimal_text(stored) == shown
    assert _read(shown, None, None) == float(stored)

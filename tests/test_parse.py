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

"""The shared card readers in ``providers/_parse.py`` on crafted card text."""

from __future__ import annotations

import re
import time
from collections.abc import Callable

import pytest

from custom_components.be_electricity_prices.providers._parse import (
    LINE_BREAK,
    excise_tier_bands,
    ores_sub_areas_split,
    parse_brussels_osp,
)


# Card text a host could serve instead, 100,000 characters or more, on which a
# pattern was tried again from every place it could start: the OSP heading
# followed by blank lines and no footnote mark, its rows by a number with no
# unit, an ORES row by blanks and no figure, a column of ORES labels with no
# closing bracket (180,000, since each costs less), and an excise band whose
# upper bound runs on in thousands groups with no unit after them.
@pytest.mark.timeout(60)
@pytest.mark.parametrize(
    ("read", "expected"),
    [
        pytest.param(
            lambda: parse_brussels_osp(
                "Obligations de Service public\n" + "\n" * 100_000 + "x"
            ),
            None,
            id="osp blank lines",
        ),
        pytest.param(
            lambda: parse_brussels_osp(
                "Obligations de Service public " + "1" * 100_000
            ),
            None,
            id="osp digits",
        ),
        pytest.param(
            lambda: ores_sub_areas_split("ORES (Est) 1" + "\xa0" * 100_000 + "x"),
            False,
            id="ores blanks",
        ),
        pytest.param(
            lambda: ores_sub_areas_split("ORES (\n" * 30_000),
            False,
            id="ores labels",
        ),
        pytest.param(
            lambda: excise_tier_bands("0 - 0." + "000 " * 25_000, None),
            None,
            id="excise bands",
        ),
    ],
)
def test_a_crafted_card_is_read_in_time_linear_in_its_length(
    read: Callable[[], object], expected: object
) -> None:
    started = time.perf_counter()
    assert read() == expected
    assert time.perf_counter() - started < 2.0


def test_a_line_break_reads_what_two_blank_runs_around_it_read() -> None:
    two_runs = re.compile(r"a\s*\n\s*b")
    one_way = re.compile(rf"a{LINE_BREAK}b")
    for blanks in ("\n", " \n ", "\n\n", "\t\n \xa0\n", " ", "", "\n x"):
        text = f"a{blanks}b"
        assert bool(one_way.fullmatch(text)) == bool(two_runs.fullmatch(text)), text

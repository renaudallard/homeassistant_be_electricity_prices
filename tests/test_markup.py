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

"""The tags taken out of a fetched page (``providers/_markup.py``)."""

from __future__ import annotations

import re
import time
from pathlib import Path

import pytest

from custom_components.be_electricity_prices.providers._markup import strip_tags

_FIXTURES = Path(__file__).resolve().parent / "fixtures"
_PAGES = sorted(
    path.relative_to(_FIXTURES).as_posix() for path in _FIXTURES.rglob("*.html")
)


@pytest.mark.parametrize("name", _PAGES)
def test_a_real_page_loses_its_tags_as_the_plain_pattern_takes_them(
    name: str,
) -> None:
    """Every page the suite holds as served, the Justel law and Aspiravi's
    downloads among them, reads the same as through ``<[^>]+>`` alone."""
    page = (_FIXTURES / name).read_bytes().decode("iso-8859-1")
    assert strip_tags(page) == re.sub(r"<[^>]+>", " ", page)


def test_what_follows_the_last_tag_is_kept() -> None:
    assert strip_tags("a <b>c</b> d < e") == "a  c  d < e"
    assert strip_tags("<<a>") == " "
    assert strip_tags("<>") == "<>"
    assert strip_tags("no tag") == "no tag"


def test_a_page_of_stray_brackets_costs_its_length() -> None:
    """A ``<`` with no ``>`` after it took the plain pattern to the end of
    the page, from each of them in turn."""
    started = time.perf_counter()
    assert strip_tags("<" * 100_000) == "<" * 100_000
    assert strip_tags("<a" * 50_000 + ">") == " "
    assert time.perf_counter() - started < 2.0

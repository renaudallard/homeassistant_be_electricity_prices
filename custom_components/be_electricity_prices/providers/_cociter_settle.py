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

"""Cociter: the BELIX the next card prints for a month, which settles it."""

from __future__ import annotations

import re
from datetime import date

from ._parse import to_float
from ._pdf import FR_MONTHS

# The BELIX column of the variable and trihoraire cards, and the month it
# belongs to: "Compteur monohoraire (0,075 x BELIX + 5) + 6% TVA 156,41
# 17,7346 c€/kWh", then "* L'indice BELIX indiqué est celui du mois
# précédent, dans ce cas-ci septembre 2026." Every row prints the same value.
_BELIX_VALUE_RE = re.compile(
    r"x\s*BELIX\s*[^)]*\)\s*\+\s*\d+\s*%\s*TVA\s+(\d+(?:,\d+)?)\s"
)
_BELIX_MONTH_RE = re.compile(r"dans\s+ce\s+cas-ci\s+(\w+)\s+(\d{4})", re.IGNORECASE)


def published_belix(text: str) -> tuple[date, float] | None:
    """The BELIX a card prints and the month it names, as ``(month, EUR/kWh)``.

    The card for a month prints the BELIX of the month before, which is that
    month's settled index: note (7) bills the delivery month on its own BELIX.
    """
    value = _BELIX_VALUE_RE.search(text)
    month = _BELIX_MONTH_RE.search(text)
    if value is None or month is None:
        return None
    name = month.group(1).lower()
    if name not in FR_MONTHS:
        return None
    return (
        date(int(month.group(2)), FR_MONTHS.index(name) + 1, 1),
        to_float(value.group(1)) / 1000.0,
    )

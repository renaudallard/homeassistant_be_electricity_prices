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

"""The fixture text cache on disk, which every later run trusts."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import tests
from tests import fixture_text

_CARD = "ecopower_dynamische_burgerstroom_nov.pdf"


def test_a_text_is_synced_before_it_takes_the_card_name(tmp_path: Path) -> None:
    """A crash after an unsynced rename left an empty file under the card's
    name, read from then on as the card's text: the write is synced first."""
    calls: list[str] = []
    real_fsync, real_replace = tests.os.fsync, tests.os.replace

    def fsync(fd: int) -> None:
        calls.append("fsync")
        real_fsync(fd)

    def replace(src: str, dst: str) -> None:
        calls.append("replace")
        real_replace(src, dst)

    with (
        patch.object(tests, "_TEXT_CACHE", tmp_path),
        patch.object(tests.os, "fsync", fsync),
        patch.object(tests.os, "replace", replace),
    ):
        text = fixture_text.__wrapped__(_CARD)
    assert calls == ["fsync", "replace"]
    (kept,) = tmp_path.iterdir()
    assert kept.read_bytes().decode("utf-8", "surrogatepass") == text
    assert text.strip()

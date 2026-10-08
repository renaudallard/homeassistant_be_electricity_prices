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

"""The declared Home Assistant floor has to be one CI actually runs.

hacs.json is what stops an install on an older core, so a number nobody
tests claims support for releases no run ever exercised, and the README
repeats it to the user.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def _declared() -> str:
    hacs = json.loads((_ROOT / "hacs.json").read_text(encoding="utf-8"))
    return str(hacs["homeassistant"])


def test_hacs_floor_matches_the_tested_home_assistant() -> None:
    requirements = (_ROOT / "requirements-dev.txt").read_text(encoding="utf-8")
    pinned = re.search(r"^homeassistant==([0-9.]+)$", requirements, re.MULTILINE)
    assert pinned is not None, "requirements-dev.txt no longer pins homeassistant"
    assert _declared() == pinned.group(1), (
        f"hacs.json declares {_declared()} but CI installs {pinned.group(1)}"
    )


def test_the_docs_quote_the_same_floor() -> None:
    declared = _declared()
    readme = (_ROOT / "README.md").read_text(encoding="utf-8")
    assert f"**{declared} or newer**" in readme
    assert f"Home%20Assistant-{declared}%2B" in readme
    architecture = (_ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")
    assert f"Home Assistant {declared} or newer" in architecture

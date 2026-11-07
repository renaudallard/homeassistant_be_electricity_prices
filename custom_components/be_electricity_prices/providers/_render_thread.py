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

"""Running a card's render in a worker thread, with what pypdf logs about
the card sent to the debug log."""

from __future__ import annotations

import asyncio
import logging
import pkgutil
import threading
from collections.abc import Callable

import pypdf

_LOGGER = logging.getLogger(__name__)


# pypdf logs at WARNING what it skips or repairs while it reads a card: a
# ToUnicode entry it cannot decode (every energie.be card from November
# 2026 on), a header behind a byte order mark, its advice to install
# fontTools. The text comes out as the tests pin it either way and nothing
# there is for the user to act on, so while a render of ours runs, its
# pypdf records go to this module's debug log instead. pypdf reads made by
# anything else in Home Assistant log as before.
_RENDERING = threading.local()


class _PypdfToDebug(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not getattr(_RENDERING, "active", False):
            return True
        _LOGGER.debug("%s: %.300s", record.name, record.getMessage())
        return False


# pypdf logs to the logger of the module that met the problem.
_TO_DEBUG = _PypdfToDebug()
for _module in pkgutil.walk_packages(pypdf.__path__, "pypdf."):
    logging.getLogger(_module.name).addFilter(_TO_DEBUG)


def _render_quietly(render: Callable[[bytes], str], payload: bytes) -> str:
    _RENDERING.active = True
    try:
        return render(payload)
    finally:
        _RENDERING.active = False


async def render_in_thread(render: Callable[[bytes], str], payload: bytes) -> str:
    """Run ``render`` on ``payload`` in a worker thread, the pypdf records
    it causes logged at debug."""
    return await asyncio.to_thread(_render_quietly, render, payload)

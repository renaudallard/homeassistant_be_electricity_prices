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

"""Text from outside the integration, made safe to place in markdown.

Home Assistant renders the description of a Repairs card, of a fix flow and
of an options-flow step, and an abort reason, as markdown, and the frontend's
sanitiser lets a link or an image with an https address through. An error
message carries what a supplier's server answered: a status line, a storage
error code, the first bytes of a body that was not a PDF. Whoever controls a
card's address could put a phishing link in front of the administrator, or an
image that loads as soon as the dialog opens and tells its host the
administrator's address and when they looked. No script runs either way.
"""

from __future__ import annotations

import re

# Every ASCII punctuation character. Markdown, an autolink and HTML can only
# begin with one of them, and CommonMark lets any of them be escaped with a
# backslash, which then shows the character itself and begins nothing. The
# colon and the full stop are among them, so a bare https:// or www. address
# does not turn into a link either.
_PUNCTUATION = re.compile(r"([!-/:-@\[-`{-~])")


def escape_markdown(text: str) -> str:
    """``text`` as plain characters in markdown: no link, image, emphasis,
    code span, heading, list, table, autolink or HTML tag can form out of it.

    It is put on one line too, every run of whitespace a single space: the
    text sits inside a sentence, and a new line could open a block of its
    own. The water integration escapes its Repair text the same way.
    """
    return _PUNCTUATION.sub(r"\\\1", " ".join(text.split()))

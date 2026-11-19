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

"""What a downloaded payload is: a PDF, or a storage error in its place."""

from __future__ import annotations

import re

# How much of a payload is read to tell what it is: the BOM and blank space
# ahead of its first byte, then an error document's root. Acrobat looks for
# "%PDF" in a file's first 1024 bytes, and Luminus's error document was 248.
# The payload is judged on the event loop and can be 64 MiB of blanks, which
# the storage error pattern took 5 s to give up on.
_HEAD_BYTES = 1024


def _prefix_length(payload: bytes) -> int:
    """How many bytes of a UTF-8 BOM, then blank space, ``payload`` starts
    with, :data:`_HEAD_BYTES` at most."""
    head = payload[:_HEAD_BYTES]
    return len(head) - len(head.removeprefix(b"\xef\xbb\xbf").lstrip(b"\r\n\t "))


def _strip_pdf_prefix(payload: bytes) -> bytes:
    """The bytes without a UTF-8 BOM, then blank space, ahead of ``%PDF``.

    Some publishers prepend the BOM (\\ufeff = 3 bytes EF BB BF): OCTA+'s
    tariff PDFs do this, and some servers a newline. Either has to come off
    rather than merely be tolerated: pdfplumber fails such a file with "No
    /Root object! - Is this really a PDF?", which reads like a corrupt card
    rather than a few stray bytes. pypdf recovers on its own, so the two
    aligned/layout variants are the ones this protects.
    """
    return payload[_prefix_length(payload) :]


def _is_pdf_payload(payload: bytes) -> bool:
    """Return True if the bytes look like a PDF: ``%PDF`` once
    :func:`_strip_pdf_prefix` has taken off what may precede it."""
    return payload.startswith(b"%PDF", _prefix_length(payload))


def _is_web_page(payload: bytes) -> bool:
    """Whether ``payload`` opens with a tag once :func:`_strip_pdf_prefix`
    has taken off what may precede it."""
    return payload.startswith(b"<", _prefix_length(payload))


# An object store that refuses the read answers the proxy in front of it
# with its own XML error document, and the proxy passes that through as a
# 200. Azure Blob and S3 both shape it as a root <Error> carrying a <Code>.
# Matched past the payload's prefix and within the kilobyte after it.
_STORAGE_ERROR_RE = re.compile(
    rb"(?:<\?xml[^>]*\?>\s*)?<Error>\s*<Code>([^<]{1,64})</Code>",
    re.IGNORECASE,
)


def _storage_error_code(payload: bytes) -> str | None:
    """The error code if ``payload`` is an object-store error document.

    Luminus switched anonymous access off on the storage account behind
    ``api-next/get-pricelist`` on 2026-08-10, and every card came back as
    a 248-byte ``PublicAccessNotPermitted`` document under a 200. Read as
    a plain non-PDF payload that reports as a permanent parse failure, so
    every Luminus entry raised the ``extractor_failed`` card asking the
    user to report a layout change that had not happened. The supplier's
    own download button was broken the same way, and nothing here could
    fix it, which is the definition of transient in this taxonomy.
    """
    start = _prefix_length(payload)
    match = _STORAGE_ERROR_RE.match(payload, start, start + _HEAD_BYTES)
    if match is None:
        return None
    return match.group(1).decode("ascii", "replace").strip() or None

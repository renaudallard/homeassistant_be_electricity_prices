"""The archive branch's texts as a render cache.

A card's bytes hash to a digest, and every row on the archive branch names,
per reader variant, the text that digest rendered to. Installed as the
readers' render hook (``providers/_pdf.render_through``), a downloaded card
whose bytes are already known is served that text instead of being rendered
again. The download still happens, so whoever installs the cache keeps
measuring timing, bytes, status codes and freshness; only the render, which
is the cost (about twenty minutes of a full walk on a Raspberry Pi, most of
the nine on a runner), is skipped when nothing changed.

Shared by the card archiver, which also keeps bytes it has not seen, and the
live check, which reads the branch and renders only what is new. Imports
nothing from the integration so the live check's own module loader can use
it as is.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from pathlib import Path


def read_text(path: Path) -> str:
    """A stored text exactly as it was fetched: no newline translation, so
    a replay hands the parser the very bytes it read the first time."""
    with path.open("r", encoding="utf-8", newline="") as handle:
        return handle.read()


def digest_of(pdf: str) -> str:
    """The digest a row names its PDF by. Rows written before the manifest
    existed carry the release path instead; the file name is the digest
    either way."""
    return Path(pdf).stem


class StoredTexts:
    """What the branch already knows about card bytes."""

    def __init__(self, archive: Path, *, serve: bool = True) -> None:
        self.archive = archive
        # False when every card must be rendered afresh, which is how a
        # reader upgrade reaches the stored months.
        self.serve = serve
        # (variant, digest) -> text path on the branch, from every stored row.
        self.texts: dict[tuple[str, str], str] = {}
        for row in archive.glob("*/*/*/????-??.json"):
            try:
                sources = json.loads(row.read_text(encoding="utf-8")).get(
                    "_sources", []
                )
            except ValueError:
                continue
            for source in sources:
                if "pdf" in source:
                    self.texts[(source["variant"], digest_of(source["pdf"]))] = source[
                        "text"
                    ]
        # What this run rendered, so a second card on the same bytes is
        # served too.
        self.fresh: dict[tuple[str, str], str] = {}
        # url -> digest, for whoever needs to name the PDF behind a URL.
        self.digests: dict[str, str] = {}
        self.rendered = 0
        self.unrendered = 0

    def digest_for(self, url: str) -> str | None:
        """The digest of the PDF behind ``url``, once its bytes were seen."""
        return self.digests.get(url)

    def keep(self, digest: str, payload: bytes) -> None:
        """Called once per downloaded card; the archiver stores unseen bytes."""

    async def render(
        self, variant: str, url: str, payload: bytes, renderer: Callable[[bytes], str]
    ) -> str:
        digest = hashlib.sha256(payload).hexdigest()
        self.digests[url] = digest
        self.keep(digest, payload)
        key = (variant, digest)
        text = self.fresh.get(key)
        if text is None and self.serve:
            stored = self.texts.get(key)
            if stored is not None and (self.archive / stored).exists():
                text = read_text(self.archive / stored)
        if text is not None:
            self.unrendered += 1
            return text
        text = await asyncio.to_thread(renderer, payload)
        self.rendered += 1
        self.fresh[key] = text
        return text

"""The card archive's texts as a render cache.

A card's bytes hash to a digest, and every row in the card archive names,
per reader variant, the text that digest rendered to. Installed as the
readers' render hook (``providers/_pdf.render_through``), a downloaded card
whose bytes are already known is served that text instead of being rendered
again. The download still happens, so whoever installs the cache keeps
measuring timing, bytes, status codes and freshness; only the render, which
is the cost (about twenty minutes of a full walk on a Raspberry Pi, most of
the nine on a runner), is skipped when nothing changed.

Shared by the card archiver, which also keeps bytes it has not seen, and the
live check, which reads the archive and renders only what is new. Imports
nothing from the integration so the live check's own module loader can use
it as is.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata
import json
from collections.abc import Callable
from pathlib import Path

# The two PDF text readers, pdfplumber standing for the pdfminer.six it pins,
# and the code that drives them. Each PDF source of a row names what rendered
# its text, and a stored text is served again only to the same versions and
# code: a reader upgrade or a render fix reads every card afresh, in the
# archive and in the live check alike.
_READERS = ("pypdf", "pdfplumber")
_RENDER_CODE = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "be_electricity_prices"
    / "providers"
    / "_pdf.py"
)
# The engine that reads a card published as page images, and what it reads
# with: pypdfium2 renders the pages, numpy matches the glyphs and pdfplumber
# lays out the words, all installed beside it unpinned, so one of them moving
# is the engine moving. A source it read names them in place of the readers.
OCR_ENGINE = "ocr-price-cards"
_ENGINE_LIBRARIES = ("pypdfium2", "numpy", "pdfplumber")


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


def _version(name: str) -> str:
    """One package's version, with the commit when it was installed from
    git: the OCR engine is installed from its main branch, where a fix does
    not have to move the version number. ``absent`` when it is not
    installed."""
    try:
        version = importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return "absent"
    try:
        direct = importlib.metadata.distribution(name).read_text("direct_url.json")
        commit = json.loads(direct or "{}").get("vcs_info", {}).get("commit_id")
    except (importlib.metadata.PackageNotFoundError, ValueError, AttributeError):
        commit = None
    return f"{version}+{commit[:12]}" if isinstance(commit, str) else version


def readers_line() -> str:
    """The text readers' versions and a digest of the render code, as a PDF
    source records them."""
    versions = " ".join(f"{name}=={_version(name)}" for name in _READERS)
    render = hashlib.sha256(_RENDER_CODE.read_bytes()).hexdigest()[:16]
    return f"{versions} render={render}"


def engine_version() -> str:
    """The OCR engine's version and those of the libraries it reads with, as
    a source it read records them."""
    libraries = " ".join(f"{name}=={_version(name)}" for name in _ENGINE_LIBRARIES)
    return f"{_version(OCR_ENGINE)} {libraries}"


class StoredTexts:
    """What the archive already knows about card bytes."""

    def __init__(self, archive: Path, *, serve: bool = True) -> None:
        self.archive = archive
        # False when every card must be rendered afresh (--rerender).
        self.serve = serve
        # (variant, digest) -> text path in the archive, from every stored row
        # whose text the installed readers and render code made. The OCR
        # engine's readings are served as they are: the live check installs
        # no engine, and the archiver drops those another engine made.
        self.texts: dict[tuple[str, str], str] = {}
        # Digest -> the engine that read the card, for the cards a PREVIOUS
        # run had to read off their pixels. Carried in the rows beside the
        # text, because serving a stored text skips the reader that would
        # otherwise discover it again: without this the fact lasted exactly
        # one day and every row after the first said the card was read
        # normally.
        self.ocr: dict[str, str] = {}
        readers = readers_line()
        # The rows sit under cards/ in the cards repository; see _ROWS there.
        for row in archive.glob("cards/*/*/*/????-??.json"):
            try:
                sources = json.loads(row.read_text(encoding="utf-8")).get(
                    "_sources", []
                )
            except ValueError:
                continue
            for source in sources:
                if "pdf" not in source:
                    continue
                digest = digest_of(source["pdf"])
                if source.get("ocr"):
                    self.ocr[digest] = str(source["ocr"])
                elif source.get("readers") != readers:
                    continue
                self.texts[(source["variant"], digest)] = source["text"]
        # What this run rendered, so a second card on the same bytes is
        # served too.
        self.fresh: dict[tuple[str, str], str] = {}
        # url -> digest, for whoever needs to name the PDF behind a URL.
        self.digests: dict[str, str] = {}
        # Every card this run was handed, in order: a provider that gets a
        # card some other way than through a reader (OCTA+'s archive, base64
        # inside JSON) leaves nothing in the text memo, and this is how the
        # archiver still learns what it read. Cleared per fetch by the caller.
        self.calls: list[tuple[str, str, str, str]] = []
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
            self.calls.append((variant, url, digest, text))
            return text
        text = await asyncio.to_thread(renderer, payload)
        self.rendered += 1
        self.fresh[key] = text
        self.calls.append((variant, url, digest, text))
        return text

"""scripts/card_texts.py: the archive branch's texts as a render cache."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

# scripts/ is not a package, so it is added to sys.path above rather than
# imported by dotted path; mypy cannot follow that.
from card_texts import StoredTexts, digest_of, read_text  # type: ignore[import-not-found]  # noqa: E402


def _branch(tmp_path: Path, payload: bytes, variant: str, text: str) -> Path:
    """A branch holding one row that read ``payload`` in ``variant``."""
    digest = hashlib.sha256(payload).hexdigest()
    text_path = tmp_path / "texts/2026-09/abc.txt"
    text_path.parent.mkdir(parents=True)
    text_path.write_text(text)
    row = tmp_path / "acme/acme_fix/wallonia/2026-09.json"
    row.parent.mkdir(parents=True)
    row.write_text(
        json.dumps(
            {
                "_sources": [
                    {
                        "url": "https://acme.test/card.pdf",
                        "variant": variant,
                        "text": "texts/2026-09/abc.txt",
                        "pdf": digest,
                    }
                ]
            }
        )
    )
    return tmp_path


def test_known_bytes_are_served_and_unknown_ones_rendered(tmp_path: Path) -> None:
    cache = StoredTexts(_branch(tmp_path, b"%PDF v1", "plain", "stored text"))
    renders: list[bytes] = []

    def render(payload: bytes) -> str:
        renders.append(payload)
        return "fresh text"

    async def run() -> tuple[str, str, str, str]:
        a = await cache.render(
            "plain", "https://acme.test/card.pdf", b"%PDF v1", render
        )
        # Another reader variant of the same bytes has no stored text.
        b = await cache.render(
            "layout", "https://acme.test/card.pdf", b"%PDF v1", render
        )
        # The same bytes under another URL, already rendered this run.
        c = await cache.render(
            "layout", "https://acme.test/other.pdf", b"%PDF v1", render
        )
        d = await cache.render("plain", "https://acme.test/new.pdf", b"%PDF v2", render)
        return a, b, c, d

    assert asyncio.run(run()) == (
        "stored text",
        "fresh text",
        "fresh text",
        "fresh text",
    )
    assert renders == [b"%PDF v1", b"%PDF v2"]
    assert (cache.unrendered, cache.rendered) == (2, 2)
    assert (
        cache.digest_for("https://acme.test/new.pdf")
        == hashlib.sha256(b"%PDF v2").hexdigest()
    )


def test_serving_can_be_switched_off_for_a_reader_upgrade(tmp_path: Path) -> None:
    cache = StoredTexts(
        _branch(tmp_path, b"%PDF v1", "plain", "stored text"), serve=False
    )
    renders: list[bytes] = []

    def render(payload: bytes) -> str:
        renders.append(payload)
        return "fresh"

    async def run() -> str:
        return await cache.render(
            "plain", "https://acme.test/card.pdf", b"%PDF v1", render
        )

    assert asyncio.run(run()) == "fresh"
    assert renders == [b"%PDF v1"]


def test_keep_sees_every_download_once_per_url(tmp_path: Path) -> None:
    """The archiver's hook: called with the digest and the bytes of every
    card downloaded, before any lookup, so unseen bytes can be stored."""
    kept: list[tuple[str, int]] = []

    class Keeping(StoredTexts):
        def keep(self, digest: str, payload: bytes) -> None:
            kept.append((digest[:8], len(payload)))

    cache = Keeping(_branch(tmp_path, b"%PDF v1", "plain", "stored text"))
    asyncio.run(
        cache.render("plain", "https://acme.test/card.pdf", b"%PDF v1", lambda p: "x")
    )
    assert kept == [(hashlib.sha256(b"%PDF v1").hexdigest()[:8], 7)]


def test_helpers_keep_bytes_and_digests_intact(tmp_path: Path) -> None:
    path = tmp_path / "t.txt"
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write("a\r\nb")
    assert read_text(path) == "a\r\nb"
    assert digest_of("cards-2026-09/abc.pdf") == "abc"
    assert digest_of("abc") == "abc"

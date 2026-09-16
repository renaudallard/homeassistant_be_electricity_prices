"""The doc reference checker, on a docs tree of its own.

It gates CI on a file or anchor a doc names that is not there. The docs
cross-link with markdown links rather than backticks, and those were never
looked at: a renamed doc broke every link into it while the check stayed
green.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import doc_ref_check as drc  # type: ignore[import-not-found]  # noqa: E402


def _tree(tmp_path: Path, *docs: tuple[str, str]) -> Path:
    root = tmp_path / "repo"
    (root / "docs" / "providers").mkdir(parents=True)
    (root / "README.md").write_text("# Readme\n\n## Setup\n", encoding="utf-8")
    for name, body in docs:
        (root / "docs" / name).write_text(body, encoding="utf-8")
    return root


def _run(monkeypatch: pytest.MonkeyPatch, root: Path) -> int:
    monkeypatch.setattr(drc, "ROOT", root)
    monkeypatch.setattr(drc, "DOCS", root / "docs")
    monkeypatch.setattr(sys, "argv", ["doc_ref_check.py"])
    return drc.main()


def test_links_that_resolve_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _tree(
        tmp_path,
        (
            "guide.md",
            "# Guide\n\n## The map\n\nSee [providers](providers/bolt.md#rows) and [readme](../README.md#setup).\n",
        ),
        (
            "providers/bolt.md",
            "# Bolt\n\n## Rows\n\nBack to [the guide](../guide.md#the-map).\n",
        ),
    )
    assert _run(monkeypatch, root) == 0
    assert "MISSING" not in capsys.readouterr().out


def test_a_dangling_link_or_link_anchor_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _tree(
        tmp_path,
        (
            "guide.md",
            "# Guide\n\nSee [old](old-architecture.md#the-map) and [rows](providers/bolt.md#no-such-heading).\n",
        ),
        ("providers/bolt.md", "# Bolt\n\n## Rows\n"),
    )
    assert _run(monkeypatch, root) == 1
    out = capsys.readouterr().out
    assert "MISSING FILE   guide.md:3 (old-architecture.md)" in out
    assert "MISSING ANCHOR guide.md:3 providers/bolt.md#no-such-heading" in out


def test_a_link_a_browser_cannot_follow_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The link resolver fell back to the path-token bases, so a link that
    only resolves from the repository root passed while the browser answered
    404; a same-page anchor was never looked at, and a link to a source file
    was not a link at all."""
    root = _tree(
        tmp_path,
        (
            "guide.md",
            "# Guide\n\n## The map\n\nSee [arch](docs/architecture.md), [tool](../scripts/tool.py), "
            "[gone](../scripts/gone.py), [me](#the-map) and [nowhere](#nowhere).\n",
        ),
        ("architecture.md", "# Architecture\n"),
        ("providers/bolt.md", "# Bolt\n\nBack to the [readme](README.md).\n"),
    )
    (root / "scripts").mkdir()
    (root / "scripts" / "tool.py").write_text("", encoding="utf-8")
    assert _run(monkeypatch, root) == 1
    out = capsys.readouterr().out
    assert "MISSING FILE   guide.md:5 (docs/architecture.md)" in out
    assert "MISSING FILE   guide.md:5 (../scripts/gone.py)" in out
    assert "MISSING ANCHOR guide.md:5 #nowhere" in out
    assert "MISSING FILE   bolt.md:3 (README.md)" in out
    assert "tool.py" not in out
    assert "#the-map" not in out

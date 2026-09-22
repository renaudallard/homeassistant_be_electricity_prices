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


def test_an_anchor_with_an_underscore_is_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A heading named after a function keeps its underscores in the slug,
    and the anchor classes read letters, digits and hyphens only, so
    `#fetch_for_month` was never looked at and a cross-doc link carrying one
    matched no pattern at all, file check included."""
    root = _tree(
        tmp_path,
        (
            "guide.md",
            "# Guide\n\n## fetch_for_month\n\nSee [own](#fetch_for_month), [bolt](providers/bolt.md#fetch_for_month) "
            "and [typo](#fetch_for_months).\n",
        ),
        ("providers/bolt.md", "# Bolt\n\n## fetch_for_month\n"),
    )
    assert _run(monkeypatch, root) == 1
    out = capsys.readouterr().out
    assert "MISSING ANCHOR guide.md:5 #fetch_for_months" in out
    assert out.count("MISSING") == 1


def test_no_comment_in_the_code_pins_a_line_number() -> None:
    """The docs dropped their file:line pins because a line number is wrong
    the moment anything above it moves, and this script checks what is left.
    Comments in the code kept theirs, out of its reach, and the 0.27.5 split
    left six of them pointing at the wrong line or past the end of the file.
    Name the symbol instead."""
    import re

    root = Path(__file__).resolve().parent.parent
    pin = re.compile(r"\b[\w/]+\.py:\d+")
    found = [
        f"{path.relative_to(root)} line {number}"
        for folder in ("custom_components", "scripts", "tests")
        for path in sorted((root / folder).rglob("*.py"))
        for number, line in enumerate(path.read_text(encoding="utf-8").split("\n"), 1)
        if pin.search(line)
    ]
    assert not found, found

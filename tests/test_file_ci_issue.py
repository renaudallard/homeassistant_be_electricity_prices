"""scripts/file_ci_issue.sh: one issue per problem, one comment per cooldown."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "file_ci_issue.sh"

# A gh that answers from files the test writes and logs every call it gets.
FAKE_GH = r"""#!/usr/bin/env bash
echo "$*" >> "$FAKE_GH_LOG"
case "$1 $2" in
  "label create") exit 0 ;;
  "issue list") cat "$FAKE_GH_OPEN" 2>/dev/null; exit 0 ;;
  "issue view") jq -r "${@: -1}" "$FAKE_GH_ISSUE"; exit 0 ;;
  "issue comment"|"issue create") cp "${@: -1}" "$FAKE_GH_POSTED"; exit 0 ;;
esac
echo "fake gh: unexpected $*" >&2
exit 1
"""


@pytest.fixture
def gh(tmp_path: Path) -> dict[str, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gh").write_text(FAKE_GH)
    (bin_dir / "gh").chmod(0o755)
    return {
        "bin": bin_dir,
        "log": tmp_path / "gh.log",
        "open": tmp_path / "open.txt",
        "issue": tmp_path / "issue.json",
        "posted": tmp_path / "posted.md",
    }


def _run(gh: dict[str, Path], *args: str) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "PATH": f"{gh['bin']}:{os.environ['PATH']}",
        "FAKE_GH_LOG": str(gh["log"]),
        "FAKE_GH_OPEN": str(gh["open"]),
        "FAKE_GH_ISSUE": str(gh["issue"]),
        "FAKE_GH_POSTED": str(gh["posted"]),
    }
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def _calls(gh: dict[str, Path]) -> list[str]:
    """The gh subcommands the script ran, in order (a multi-line jq
    expression logs extra lines, which are not calls)."""
    return [
        line.split(" ")[1]
        for line in gh["log"].read_text().splitlines()
        if line.split(" ")[0] in ("label", "issue")
    ]


def _issue_with(body: str, comments: list[tuple[str, str]]) -> str:
    return json.dumps(
        {
            "body": body,
            "createdAt": "2026-08-01T06:00:00Z",
            "comments": [{"body": b, "createdAt": at} for at, b in comments],
        }
    )


def test_a_new_problem_opens_an_issue_carrying_its_fingerprint(
    gh: dict[str, Path], tmp_path: Path
) -> None:
    body = tmp_path / "body.md"
    body.write_text("## Run\n\nhttps://x\n")
    fp = tmp_path / "fp.txt"
    fp.write_text("mega/x: fetch\n")
    result = _run(
        gh,
        "--label",
        "l",
        "--title",
        "t",
        "--body",
        str(body),
        "--fingerprint",
        str(fp),
    )
    assert result.returncode == 0, result.stderr
    assert _calls(gh) == ["create", "list", "create"]
    posted = gh["posted"].read_text()
    assert posted.startswith("## Run")
    assert "<!-- ci-fingerprint: " in posted


def test_the_same_failure_within_the_cooldown_posts_nothing(
    gh: dict[str, Path], tmp_path: Path
) -> None:
    body = tmp_path / "body.md"
    body.write_text("report\n")
    fp = tmp_path / "fp.txt"
    fp.write_text("mega/x: fetch\n")
    # Find the fingerprint the script computes by letting it create once.
    _run(
        gh,
        "--label",
        "l",
        "--title",
        "t",
        "--body",
        str(body),
        "--fingerprint",
        str(fp),
    )
    marker = [
        line
        for line in gh["posted"].read_text().splitlines()
        if "ci-fingerprint" in line
    ][0]
    gh["open"].write_text("42\n")
    yesterday = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    gh["issue"].write_text(
        _issue_with("old body", [(yesterday, f"a comment\n{marker}")])
    )
    gh["log"].write_text("")
    gh["posted"].unlink()
    body.write_text("report\n")
    result = _run(
        gh,
        "--label",
        "l",
        "--title",
        "t",
        "--body",
        str(body),
        "--fingerprint",
        str(fp),
    )
    assert result.returncode == 0, result.stderr
    assert "not commenting" in result.stdout
    assert _calls(gh) == ["create", "list", "view"]
    assert not gh["posted"].exists()

    # The same failure past the cooldown is posted again.
    long_ago = (datetime.now(UTC) - timedelta(days=8)).strftime("%Y-%m-%dT%H:%M:%SZ")
    gh["issue"].write_text(
        _issue_with("old body", [(long_ago, f"a comment\n{marker}")])
    )
    gh["log"].write_text("")
    result = _run(
        gh,
        "--label",
        "l",
        "--title",
        "t",
        "--body",
        str(body),
        "--fingerprint",
        str(fp),
    )
    assert result.returncode == 0, result.stderr
    assert _calls(gh) == ["create", "list", "view", "comment"]
    assert gh["posted"].exists()

    # A different failure is posted at once, whatever the age.
    fp.write_text("bolt/y: parse\n")
    gh["issue"].write_text(
        _issue_with("old body", [(yesterday, f"a comment\n{marker}")])
    )
    gh["log"].write_text("")
    gh["posted"].unlink()
    result = _run(
        gh,
        "--label",
        "l",
        "--title",
        "t",
        "--body",
        str(body),
        "--fingerprint",
        str(fp),
    )
    assert result.returncode == 0, result.stderr
    assert _calls(gh) == ["create", "list", "view", "comment"]


def test_an_open_issue_without_a_marker_gets_a_comment(
    gh: dict[str, Path], tmp_path: Path
) -> None:
    """An issue from before the fingerprint existed, or opened by hand under
    the label, has no marker to compare against and is commented on."""
    body = tmp_path / "body.md"
    body.write_text("report\n")
    gh["open"].write_text("7\n")
    gh["issue"].write_text(_issue_with("hand-written", []))
    result = _run(gh, "--label", "l", "--title", "t", "--body", str(body))
    assert result.returncode == 0, result.stderr
    assert _calls(gh) == ["create", "list", "view", "comment"]
    assert "<!-- ci-fingerprint: " in gh["posted"].read_text()


def test_missing_arguments_fail_loudly(gh: dict[str, Path]) -> None:
    result = _run(gh, "--label", "l")
    assert result.returncode == 2
    assert "required" in result.stderr

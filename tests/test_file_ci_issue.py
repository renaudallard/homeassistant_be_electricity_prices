"""scripts/file_ci_issue.sh: one issue per problem, one comment per cooldown."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

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


def _comment(at: str, body: str, login: str = "github-actions") -> dict[str, Any]:
    """A comment as gh shows it, by the bot that files these unless
    ``login`` says otherwise."""
    return {"author": {"login": login}, "body": body, "createdAt": at}


def _issue_with(
    body: str,
    comments: list[dict[str, Any]],
    author: str = "app/github-actions",
    opened: str = "2026-08-01T06:00:00Z",
) -> str:
    """The issue as gh shows it, opened by the bot unless ``author`` says
    otherwise: gh names the bot app/github-actions as an issue's author and
    github-actions as a comment's."""
    return json.dumps(
        {
            "author": {"is_bot": author.startswith("app/"), "login": author},
            "body": body,
            "createdAt": opened,
            "comments": comments,
        }
    )


def _marker(fingerprint: Path) -> str:
    """The marker the script writes for that fingerprint file."""
    digest = hashlib.sha256(fingerprint.read_bytes()).hexdigest()[:16]
    return f"<!-- ci-fingerprint: {digest} -->"


def _ago(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


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
    marker = next(
        line
        for line in gh["posted"].read_text().splitlines()
        if "ci-fingerprint" in line
    )
    gh["open"].write_text("42\n")
    yesterday = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    gh["issue"].write_text(
        _issue_with("old body", [_comment(yesterday, f"a comment\n{marker}")])
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
        _issue_with("old body", [_comment(long_ago, f"a comment\n{marker}")])
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
        _issue_with("old body", [_comment(yesterday, f"a comment\n{marker}")])
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


def _post_again(gh: dict[str, Path], tmp_path: Path, issue: str) -> list[str]:
    """Run the script with ``issue`` open as #7 and ``tmp_path/fp.txt`` as
    the fingerprint file: the gh calls it made."""
    body = tmp_path / "body.md"
    body.write_text("report\n")
    fp = tmp_path / "fp.txt"
    gh["open"].write_text("7\n")
    gh["issue"].write_text(issue)
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
    return _calls(gh)


def test_only_the_bot_s_own_posts_hold_a_marker(
    gh: dict[str, Path], tmp_path: Path
) -> None:
    """Anyone can comment on the issue, and a comment repeating the marker of
    the bot's last post kept the same failure quiet for another week, and
    for good when repeated every six days. Only the bot's posts count, and
    an issue a person opened is none of them."""
    fp = tmp_path / "fp.txt"
    fp.write_text("mega/x: fetch\n")
    marker = _marker(fp)
    stale = _comment(_ago(8), f"again\n{marker}")
    echoed = _comment(_ago(1), f"nice\n{marker}", "some-outsider")
    posted = ["create", "list", "view", "comment"]
    issue = _issue_with("old body", [stale, echoed])
    assert _post_again(gh, tmp_path, issue) == posted
    by_hand = _issue_with(f"by hand\n{marker}", [], "renaudallard", _ago(1))
    assert _post_again(gh, tmp_path, by_hand) == posted
    # The control: the bot's own post of yesterday still holds it back.
    fresh = _issue_with(f"report\n{marker}", [echoed], opened=_ago(1))
    assert _post_again(gh, tmp_path, fresh) == ["create", "list", "view"]


def test_only_the_marker_the_script_writes_counts(
    gh: dict[str, Path], tmp_path: Path
) -> None:
    """A report can quote a marker, from a supplier's page as much as
    anywhere, and the first marker of a post was the one read: the same
    failure then read as another one and was posted every day. The marker
    the script writes is the last of the post."""
    fp = tmp_path / "fp.txt"
    fp.write_text("mega/x: fetch\n")
    quoted = "| `mega/x: fetch` | `Zen <!-- ci-fingerprint: 0000000000000000 -->` |"
    posted = _comment(_ago(1), f"{quoted}\n\n{_marker(fp)}")
    issue = _issue_with("old body", [posted])
    assert _post_again(gh, tmp_path, issue) == ["create", "list", "view"]


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

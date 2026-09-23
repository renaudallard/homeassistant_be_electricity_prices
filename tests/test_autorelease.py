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

"""The release job of autorelease.yml, run from the workflow itself against a
scratch origin and a fake gh."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

WORKFLOW = Path(__file__).resolve().parent.parent / ".github/workflows/autorelease.yml"
TAG = "v9.9.9"

# gh as the release job calls it. A release is the JSON gh would answer
# with, under releases/, and view runs the job's --jq filter over it with the
# real jq. create fails first as many times as the failures file says, and
# as many times again as the drafts file says, leaving a draft each time: the
# publish call and the cleanup of the draft both failing.
_RELEASE = '{"isDraft": %s, "assets": [{"name": "be_electricity_prices.zip"}]}'
FAKE_GH = rf"""#!/bin/sh
state=$(dirname "$0")/..
echo "$*" >> "$state/gh.log"
case "$1 $2" in
  "release view")
    tag=$3
    [ -e "$state/releases/$tag" ] || exit 1
    shift 3
    while [ $# -gt 0 ] && [ "$1" != "--jq" ]; do shift; done
    [ $# -gt 1 ] || exit 0
    jq -r "$2" "$state/releases/$tag" ;;
  "release create")
    n=$(cat "$state/failures")
    if [ "$n" -gt 0 ]; then
      echo $((n - 1)) > "$state/failures"
      exit 1
    fi
    d=$(cat "$state/drafts")
    if [ "$d" -gt 0 ]; then
      echo $((d - 1)) > "$state/drafts"
      echo '{_RELEASE % "true"}' > "$state/releases/$3"
      exit 1
    fi
    echo '{_RELEASE % "false"}' > "$state/releases/$3" ;;
  "release delete") rm "$state/releases/$3" ;;
  *) exit 2 ;;
esac
"""


def _job() -> dict[str, Any]:
    import yaml  # type: ignore[import-untyped]

    job: dict[str, Any] = yaml.safe_load(WORKFLOW.read_text())["jobs"]["release"]
    return job


def _steps() -> dict[str, dict[str, Any]]:
    return {s.get("id") or s.get("name", ""): s for s in _job()["steps"]}


def _scratch(root: Path) -> dict[str, str]:
    """A bare origin holding one commit, the fake gh and a no-op sleep, and
    the environment that runs a step against them."""
    bin_dir = root / "bin"
    bin_dir.mkdir()
    (bin_dir / "gh").write_text(FAKE_GH)
    (bin_dir / "sleep").write_text("#!/bin/sh\nexit 0\n")
    for stub in bin_dir.iterdir():
        stub.chmod(0o755)
    (root / "releases").mkdir()
    (root / "failures").write_text("0")
    (root / "drafts").write_text("0")
    env = {
        **os.environ,
        **_job().get("env", {}),
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
    }
    seed = root / "seed"
    for args in (
        ["init", "-q", "--bare", "-b", "main", str(root / "origin.git")],
        ["init", "-q", "-b", "main", str(seed)],
        ["-C", str(seed), "commit", "-q", "--allow-empty", "-m", "bump"],
        ["-C", str(seed), "push", "-q", str(root / "origin.git"), "main"],
    ):
        subprocess.run(["git", *args], env=env, check=True)
    return env


def _run_job(root: Path, env: dict[str, str]) -> int | None:
    """Check origin out as the job does, run its check step, then its
    release step when the check says to. The release step's exit, or None
    when the job skipped it."""
    steps = _steps()
    work = root / "work"
    shutil.rmtree(work, ignore_errors=True)
    subprocess.run(
        ["git", "clone", "-q", str(root / "origin.git"), str(work)], env=env, check=True
    )
    (work / "dist").mkdir()
    (work / "dist" / "be_electricity_prices.zip").write_bytes(b"")
    output = root / "github_output"
    output.write_text("")

    def _script(step: dict[str, Any]) -> str:
        return str(step["run"]).replace("${{ steps.version.outputs.tag }}", TAG)

    subprocess.run(
        ["bash", "-c", _script(steps["check"])],
        cwd=work,
        env={**env, "GITHUB_OUTPUT": str(output)},
        check=True,
    )
    if "exists=false" not in output.read_text():
        return None
    return subprocess.run(
        ["bash", "-c", _script(steps["Tag and release"])],
        cwd=work,
        env=env,
        capture_output=True,
        check=False,
    ).returncode


def test_a_rerun_publishes_a_release_whose_tag_is_already_pushed(
    tmp_path: Path,
) -> None:
    """The tag is pushed before the release is created, so when all five
    attempts failed the tag was on origin, and the job's re-run, which
    tested the tag, skipped the release and ended green with nothing
    published."""
    env = _scratch(tmp_path)
    (tmp_path / "failures").write_text("5")
    assert _run_job(tmp_path, env) == 1
    tags = subprocess.run(
        ["git", "-C", str(tmp_path / "origin.git"), "tag"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert tags == [TAG]
    assert not (tmp_path / "releases" / TAG).exists()

    assert _run_job(tmp_path, env) == 0
    assert (tmp_path / "releases" / TAG).exists()


def test_a_published_release_is_left_alone(tmp_path: Path) -> None:
    """The control: once the release exists, a re-run publishes nothing."""
    env = _scratch(tmp_path)
    assert _run_job(tmp_path, env) == 0
    creates = (tmp_path / "gh.log").read_text().count("release create")
    assert _run_job(tmp_path, env) is None
    assert (tmp_path / "gh.log").read_text().count("release create") == creates


def test_a_draft_left_by_a_failed_publish_is_not_the_release(tmp_path: Path) -> None:
    """gh release create makes a draft, uploads, then publishes. When the
    publish call and the cleanup of the draft both fail, the draft stays, and
    the lookup by tag finds it: the retry took it for the release and ended
    green with nothing HACS can install. It is dropped and the create run
    again."""
    env = _scratch(tmp_path)
    (tmp_path / "drafts").write_text("1")
    assert _run_job(tmp_path, env) == 0
    assert json.loads((tmp_path / "releases" / TAG).read_text())["isDraft"] is False
    assert (tmp_path / "gh.log").read_text().count("release create") == 2


def test_a_rerun_publishes_over_a_draft_an_earlier_run_left(tmp_path: Path) -> None:
    """A draft an earlier run could not clean up is found by the check's
    lookup too, and a re-run must not take it for the release and skip the
    job, green, with nothing published."""
    env = _scratch(tmp_path)
    (tmp_path / "releases" / TAG).write_text(_RELEASE % "true")
    assert _run_job(tmp_path, env) == 0
    assert json.loads((tmp_path / "releases" / TAG).read_text())["isDraft"] is False

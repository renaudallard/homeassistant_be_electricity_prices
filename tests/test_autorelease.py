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

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

WORKFLOW = Path(__file__).resolve().parent.parent / ".github/workflows/autorelease.yml"
TAG = "v9.9.9"

# gh as the release job calls it. A release is a file under releases/, and
# create fails first as many times as the failures file says.
FAKE_GH = r"""#!/bin/sh
state=$(dirname "$0")/..
echo "$*" >> "$state/gh.log"
case "$1 $2" in
  "release view") [ -e "$state/releases/$3" ] ;;
  "release create")
    n=$(cat "$state/failures")
    if [ "$n" -gt 0 ]; then
      echo $((n - 1)) > "$state/failures"
      exit 1
    fi
    touch "$state/releases/$3" ;;
  *) exit 2 ;;
esac
"""


def _steps() -> dict[str, dict[str, Any]]:
    import yaml  # type: ignore[import-untyped]

    workflow = yaml.safe_load(WORKFLOW.read_text())
    return {
        s.get("id") or s.get("name", ""): s
        for s in workflow["jobs"]["release"]["steps"]
    }


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
    env = {
        **os.environ,
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

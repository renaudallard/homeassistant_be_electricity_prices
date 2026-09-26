#!/usr/bin/env bash
# Run the push gate against a SNAPSHOT of HEAD, not against the working tree.
#
# The gate is the thing that protects a push. Run in place, it reads the files
# it is checking while they are still being edited: two runs were voided that
# way on 2026-09-17, one by a test file changing under pytest and one by a
# commit shifting the lines an inspect-based test reads. Both looked green
# until the next run disagreed.
#
# So the checks run in a throwaway git worktree of HEAD. Editing the real tree
# while this runs cannot reach it, and what is verified is exactly what a push
# would publish: commits, never uncommitted work. Commit first, then gate.
#
# Every check starts at once. pytest spreads over all the cores; the others
# run on one core each, and one after the other they left three of a Pi's
# four idle for four minutes. When the GATE_REMOTE host (maci7 by default)
# answers, pytest runs there instead, on the same snapshot shipped with git
# archive, while the rest runs here: about 6 minutes against 15 on a Pi alone.
# Set GATE_REMOTE= to keep everything local. The remote needs a venv at
# ~/be_gate/.venv, refreshed here with uv whenever requirements-dev.txt moves,
# and GNU date first in its PATH for the CI issue script's tests. A remote
# that cannot be reached, or drops mid-run, costs a local pytest, never a
# gate result; on a Mac the suite runs under caffeinate so the machine does
# not sleep under it.
#
# Usage: scripts/gate.sh [pytest args...]
#   scripts/gate.sh                     the whole suite, as test.yml runs it
#   scripts/gate.sh tests/test_ebem.py  one file, for a quick pass
#
# The interpreter, the workflow linter and the pytest plugins come from the
# real tree: the worktree holds the repository's tracked files and nothing
# else, so .venv and tmp/actionlint are addressed absolutely.
set -u

ROOT=$(git rev-parse --show-toplevel) || exit 1
cd "$ROOT" || exit 1
PYTHON="$ROOT/.venv/bin/python"
ACTIONLINT="$ROOT/tmp/actionlint"
SHA=$(git rev-parse --short HEAD)
WORKTREE="$ROOT/tmp/gate/$SHA.$$"
LOGS="$ROOT/tmp/gate/$SHA.$$.logs"
REMOTE=${GATE_REMOTE-maci7}
REMOTE_DIR="be_gate/gate-$SHA.$$"
# The keepalive notices a remote that went away without closing the
# connection, a Mac put to sleep for one, within a minute; without it ssh
# waits on the dead peer and the local fallback never starts.
SSH=(ssh -o BatchMode=yes -o ConnectTimeout=5 -o ServerAliveInterval=15
  -o ServerAliveCountMax=4)
shipped=""

[ -x "$PYTHON" ] || { echo "no interpreter at $PYTHON" >&2; exit 1; }

cleanup() {
  cd "$ROOT" || return
  git worktree remove --force "$WORKTREE" >/dev/null 2>&1
  rm -rf "$LOGS"
  [ -n "$shipped" ] && "${SSH[@]}" "$REMOTE" "rm -rf $REMOTE_DIR" >/dev/null 2>&1
}
trap cleanup EXIT INT TERM

mkdir -p "$LOGS"
git worktree add --detach --quiet "$WORKTREE" HEAD || exit 1
echo "gating $SHA in $WORKTREE"

# Ship the snapshot and bring the remote venv in step with the pins. Any
# failure leaves pytest local.
remote_ready() {
  [ -n "$REMOTE" ] || return 1
  "${SSH[@]}" "$REMOTE" "test -x be_gate/.venv/bin/python" 2>/dev/null || return 1
  git archive HEAD |
    "${SSH[@]}" "$REMOTE" "mkdir -p $REMOTE_DIR && tar -xf - -C $REMOTE_DIR" ||
    return 1
  shipped=1
  local want have
  want=$(sha256sum "$WORKTREE/requirements-dev.txt" | cut -d' ' -f1)
  have=$("${SSH[@]}" "$REMOTE" "cat be_gate/.venv/requirements.sha256" 2>/dev/null)
  [ "$want" = "$have" ] && return 0
  echo "refreshing the $REMOTE venv from requirements-dev.txt"
  "${SSH[@]}" "$REMOTE" "~/.local/bin/uv pip install -q \
    --python be_gate/.venv/bin/python -r $REMOTE_DIR/requirements-dev.txt &&
    echo $want > be_gate/.venv/requirements.sha256"
}

names=()
pids=()
# Start one check in the background, its output in LOGS under its position.
start() {
  local n=${#names[@]}
  names+=("$1")
  shift
  ( cd "$WORKTREE" && "$@" ) > "$LOGS/$n.log" 2>&1 &
  pids+=($!)
}

PYTEST_ARGS=("${@:-tests/}")
if remote_ready; then
  where="$REMOTE"
  remote_pytest="../.venv/bin/python -m pytest $(printf '%q ' "${PYTEST_ARGS[@]}")-q -n auto --dist loadfile"
  # caffeinate -i keeps a Mac from idle sleep for as long as the suite runs;
  # a remote without it runs the suite plainly.
  start "pytest (on $REMOTE)" "${SSH[@]}" -n "$REMOTE" \
    "cd $REMOTE_DIR && { command -v caffeinate >/dev/null && exec caffeinate -i $remote_pytest; exec $remote_pytest; }"
else
  where=local
  start "pytest" "$PYTHON" -m pytest "${PYTEST_ARGS[@]}" -q -n auto --dist loadfile
fi
echo "pytest runs on $where"
echo

start "ruff check"  "$PYTHON" -m ruff check .
start "ruff format" "$PYTHON" -m ruff format --check .
# One cache each: two mypy processes writing one cache can corrupt it.
start "mypy strict" "$PYTHON" -m mypy --cache-dir .mypy_cache_strict \
  --strict custom_components/be_electricity_prices
start "mypy all"    "$PYTHON" -m mypy --cache-dir .mypy_cache_all \
  custom_components/ tests/ scripts/
start "doc refs"    "$PYTHON" scripts/doc_ref_check.py
if [ -x "$ACTIONLINT" ]; then
  # Globbed, not listed: a seventh workflow should not be able to slip past
  # this by not being named. Expanded inside the worktree, which holds the
  # same tracked files.
  start "actionlint" bash -c "\"$ACTIONLINT\" .github/workflows/*.yml"
else
  echo "== actionlint"
  echo "   skipped: no binary at $ACTIONLINT"
  echo
fi

failed=""
# pytest started first and is reported last, after the quick checks.
order=("${!names[@]}")
order=("${order[@]:1}" 0)
for n in "${order[@]}"; do
  wait "${pids[$n]}"
  rc=$?
  name=${names[$n]}
  if [ "$rc" -eq 255 ] && [ "$where" != local ] && [ "$n" -eq 0 ]; then
    # ssh's own failure, not the suite's: rerun it here.
    echo "== $name"
    cat "$LOGS/$n.log"
    echo "   lost $REMOTE, rerunning pytest locally"
    name="pytest"
    ( cd "$WORKTREE" && "$PYTHON" -m pytest "${PYTEST_ARGS[@]}" -q -n auto \
      --dist loadfile ) > "$LOGS/$n.log" 2>&1
    rc=$?
  fi
  echo "== $name"
  cat "$LOGS/$n.log"
  if [ "$rc" -eq 0 ]; then
    echo "   ok"
  else
    echo "   FAILED"
    failed="$failed ${name// /-}"
  fi
  echo
done
if [ -n "$failed" ]; then
  echo "GATE FAILED:$failed"
  exit 1
fi
echo "gate passed on $SHA"

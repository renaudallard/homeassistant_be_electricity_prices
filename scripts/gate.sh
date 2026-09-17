#!/usr/bin/env bash
# Run the push gate against a SNAPSHOT of HEAD, not against the working tree.
#
# The gate is the thing that protects a push, and it takes about 22 minutes on
# a Raspberry Pi. Run in place, it reads the files it is checking while they
# are still being edited: two runs were voided that way on 2026-09-17, one by a
# test file changing under pytest and one by a commit shifting the lines an
# inspect-based test reads. Both looked green until the next run disagreed.
#
# So the checks run in a throwaway git worktree of HEAD. Editing the real tree
# while this runs cannot reach it, and what is verified is exactly what a push
# would publish: commits, never uncommitted work. Commit first, then gate.
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

[ -x "$PYTHON" ] || { echo "no interpreter at $PYTHON" >&2; exit 1; }

cleanup() {
  cd "$ROOT" || return
  git worktree remove --force "$WORKTREE" >/dev/null 2>&1
}
trap cleanup EXIT INT TERM

mkdir -p "$ROOT/tmp/gate"
git worktree add --detach --quiet "$WORKTREE" HEAD || exit 1
echo "gating $SHA in $WORKTREE"
echo

failed=""
run() {
  local name=$1; shift
  echo "== $name"
  if (cd "$WORKTREE" && "$@"); then
    echo "   ok"
  else
    echo "   FAILED"
    failed="$failed $name"
  fi
  echo
}

run "ruff check"    "$PYTHON" -m ruff check .
run "ruff format"   "$PYTHON" -m ruff format --check .
run "mypy strict"   "$PYTHON" -m mypy --strict custom_components/be_electricity_prices
run "mypy all"      "$PYTHON" -m mypy custom_components/ tests/ scripts/
run "doc refs"      "$PYTHON" scripts/doc_ref_check.py
if [ -x "$ACTIONLINT" ]; then
  # Globbed, not listed: a seventh workflow should not be able to slip past
  # this by not being named. Expanded here and resolved inside the worktree,
  # which holds the same tracked files.
  run "actionlint" "$ACTIONLINT" .github/workflows/*.yml
else
  echo "== actionlint"
  echo "   skipped: no binary at $ACTIONLINT"
  echo
fi
# As test.yml runs it. The suite is the long pole, so it goes last: a lint
# failure should not cost twenty minutes before it is reported.
run "pytest" "$PYTHON" -m pytest "${@:-tests/}" -q -n auto --dist loadfile

if [ -n "$failed" ]; then
  echo "GATE FAILED:$failed"
  exit 1
fi
echo "gate passed on $SHA"

#!/usr/bin/env bash
# Run the suite on both ends of what an installation reads its cards with,
# as test.yml's two jobs do: the newest requirements the manifest allows,
# installed in the venv, then their floor, installed in a directory of its
# own put ahead of the venv. Run by scripts/gate.sh, here or on its remote,
# with the arguments test.yml runs pytest with.
#
# usage: gate_pytest.sh python floor-dir pytest-args...
set -u

python=${1-}
floor=${2-}
[ -n "$python" ] && [ -n "$floor" ] && [ $# -gt 2 ] || {
  echo "usage: gate_pytest.sh python floor-dir pytest-args..." >&2
  exit 2
}
shift 2

scripts/install_requirements.sh newest "$python" --quiet || exit 1
# The floor only moves with the manifest, so it is installed again only
# then: what pip resolves for it is the same every other day.
want=$(cat custom_components/*/manifest.json | sha256sum | cut -d' ' -f1)
if [ "$(cat "$floor/.manifest.sha256" 2>/dev/null)" != "$want" ]; then
  rm -rf -- "$floor"
  scripts/install_requirements.sh floor "$python" --quiet --target "$floor" || exit 1
  echo "$want" > "$floor/.manifest.sha256"
fi

rc=0
echo "-- newest"
"$python" -m pytest "$@" || rc=1
echo "-- floor"
PYTHONPATH="$floor${PYTHONPATH:+:$PYTHONPATH}" "$python" -m pytest "$@" || rc=1
exit "$rc"

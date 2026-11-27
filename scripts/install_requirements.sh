#!/usr/bin/env bash
# Install the integration's own requirements the way Home Assistant does,
# so the suite, the live check and the card archive read cards with what an
# installation reads them with.
#
# Home Assistant installs a requirement the manifest names only when no
# installed release meets it, with "uv pip install --upgrade <requirement>
# --constraint package_constraints.txt": a new installation gets the newest
# release the manifest allows, and an older one keeps what it had, down to
# the manifest's floor. So there are two ends to run on:
#
#   newest  each requirement as the manifest writes it, upgraded under the
#           constraints of the Home Assistant installed here, if any
#   floor   each requirement at its ">=" floor
#
# usage: install_requirements.sh newest|floor [python] [pip arguments...]
#
# python is the interpreter whose pip installs (default: python); the pip
# arguments follow the requirements, so --target DIR puts the floor beside
# a venv rather than in it.
set -eu

mode=${1-}
python=${2:-python}
[ $# -gt 2 ] && shift 2 || set --
manifests=(custom_components/*/manifest.json)
[ ${#manifests[@]} -eq 1 ] && [ -f "${manifests[0]}" ] ||
  { echo "not one manifest under custom_components/" >&2; exit 1; }
manifest=${manifests[0]}

case $mode in
  newest | floor) ;;
  *) echo "usage: install_requirements.sh newest|floor [python] [pip arguments...]" >&2; exit 2 ;;
esac

mapfile -t requirements < <("$python" -c '
import json, sys
for line in json.load(open(sys.argv[1], encoding="utf-8"))["requirements"]:
    print(line)
' "$manifest")
[ ${#requirements[@]} -gt 0 ] || { echo "no requirements in $manifest" >&2; exit 1; }

if [ "$mode" = floor ]; then
  for i in "${!requirements[@]}"; do
    case ${requirements[$i]} in
      *'>='*[,\;\ ]* | *'>='*'>='*)
        echo "cannot take the floor of ${requirements[$i]}" >&2; exit 1 ;;
      *'>='*) requirements[$i]=${requirements[$i]/>=/==} ;;
      *) echo "${requirements[$i]} names no floor" >&2; exit 1 ;;
    esac
  done
fi

# A job that reads cards without importing Home Assistant has none to take
# the constraints from; none of them names a requirement of ours today.
constrain=()
if constraints=$("$python" -c '
import os, homeassistant
print(os.path.join(os.path.dirname(homeassistant.__file__), "package_constraints.txt"))
' 2>/dev/null); then
  [ -f "$constraints" ] || { echo "no $constraints" >&2; exit 1; }
  constrain=(--constraint "$constraints")
else
  echo "no Home Assistant here: installing without its constraints"
fi

# Not quiet: what pip reports installing is the record of what was run on.
echo "installing the $mode requirements: ${requirements[*]}"
"$python" -m pip install --upgrade --disable-pip-version-check \
  ${constrain[@]+"${constrain[@]}"} "${requirements[@]}" "$@"

#!/usr/bin/env bash
# Print a file inside a fenced code block no line of it can close.
#
# The live check's reports are written by the job that runs the card
# readers on what the suppliers serve, and posted by the job that writes
# issues: posted as markdown, a report that job was made to write could
# mention, link and embed as the bot. The control characters go first,
# since a CR ends a line and one dropped later would join two runs of
# backticks, and the fence is one backtick longer than the longest run
# left, three at least. Every line, the last included, ends in a newline,
# so the closing fence stands on a line of its own.
#
# usage: code_block.sh FILE
set -euo pipefail

[ $# -eq 1 ] || { echo "usage: code_block.sh FILE" >&2; exit 2; }

clean() {
  LC_ALL=C tr -d '\000-\010\013-\037\177' < "$1"
}

fence=$(clean "$1" | LC_ALL=C awk '
  { line = $0
    while (match(line, /`+/)) {
      if (RLENGTH > n) n = RLENGTH
      line = substr(line, RSTART + RLENGTH)
    } }
  END { for (n = (n < 3 ? 3 : n + 1); n > 0; n--) printf "`"; print "" }')
printf '%s\n' "$fence"
clean "$1" | LC_ALL=C awk '{ print }'
printf '%s\n' "$fence"

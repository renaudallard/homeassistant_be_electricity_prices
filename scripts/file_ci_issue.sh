#!/usr/bin/env bash
# Open or update the one issue a CI job files for a problem, without
# re-posting the same problem every day.
#
# The issue is found by its label, never by a title substring, so a
# manually opened "Re: ..." issue can never catch a job's comments. The
# body carries a fingerprint of what failed; when the open issue's latest
# post carries the same fingerprint and is younger than the cooldown, the
# run says so and posts nothing. A permanent upstream outage therefore
# gets one issue and one comment per cooldown period, not one per day, and
# a failure that changes shape is posted at once.
#
# usage: file_ci_issue.sh --label L --title T --body FILE [--fingerprint FILE]
#                         [--cooldown-days N] [--workflow NAME]
#
# --fingerprint names a file whose content identifies the failure (the
# persistent-failures list, a step name); without it the body itself is
# the fingerprint. --workflow is only used in the label's description.
set -eu

label='' title='' body='' fingerprint_file='' cooldown_days=7 workflow=''
while [ $# -gt 0 ]; do
  case "$1" in
    --label) label=$2; shift 2 ;;
    --title) title=$2; shift 2 ;;
    --body) body=$2; shift 2 ;;
    --fingerprint) fingerprint_file=$2; shift 2 ;;
    --cooldown-days) cooldown_days=$2; shift 2 ;;
    --workflow) workflow=$2; shift 2 ;;
    *) echo "file_ci_issue.sh: unknown argument $1" >&2; exit 2 ;;
  esac
done
[ -n "$label" ] && [ -n "$title" ] && [ -n "$body" ] || {
  echo "file_ci_issue.sh: --label, --title and --body are required" >&2
  exit 2
}

fingerprint=$(sha256sum "${fingerprint_file:-$body}" | cut -c1-16)
marker="<!-- ci-fingerprint: $fingerprint -->"

gh label create "$label" \
  --color FBCA04 \
  --description "Auto-filed by ${workflow:-CI}" \
  --force >/dev/null 2>&1 || true

existing=$(gh issue list \
  --state open \
  --label "$label" \
  --json number \
  --jq '.[0].number // empty')

if [ -n "$existing" ]; then
  # The latest post that carries a marker: a comment if there is one, else
  # the body. A post older than the cooldown, or with another fingerprint,
  # is a different situation and gets a fresh comment.
  latest=$(gh issue view "$existing" --json body,createdAt,comments --jq '
    ([{body: .body, createdAt: .createdAt}] + [.comments[] | {body: .body, createdAt: .createdAt}])
    | map(select(.body | test("<!-- ci-fingerprint: [0-9a-f]+ -->")))
    | last // empty
    | "\(.createdAt) \(.body | capture("<!-- ci-fingerprint: (?<f>[0-9a-f]+) -->").f)"')
  if [ -n "$latest" ]; then
    posted_at=${latest%% *}
    posted_fingerprint=${latest##* }
    age_days=$(( ( $(date -u +%s) - $(date -u -d "$posted_at" +%s) ) / 86400 ))
    if [ "$posted_fingerprint" = "$fingerprint" ] && [ "$age_days" -lt "$cooldown_days" ]; then
      echo "issue #$existing already reports this failure ($age_days day(s) ago); not commenting"
      exit 0
    fi
  fi
  printf '\n%s\n' "$marker" >> "$body"
  echo "appending comment to issue #$existing"
  gh issue comment "$existing" --body-file "$body"
else
  printf '\n%s\n' "$marker" >> "$body"
  gh issue create \
    --title "$title ($(date -u +%Y-%m-%d))" \
    --label "$label" \
    --body-file "$body"
fi

#!/usr/bin/env bash
# Next release version of ONE component (the API or the web app), each with its
# own VERSION file -- api/VERSION, app/VERSION -- so the two release
# independently (#173). Mirrors ris-api's backend/frontend scheme.
#
# Format: YYYY.MM.DD.NNN -- the UTC date, zero-padded (so versions sort as
# text), then that component's build number for the day: 001 for its first
# release of the day, incrementing on each further one, back to 001 the next day.
#
#   scripts/bump-version.sh api/VERSION --dry-run   # print the next version
#   scripts/bump-version.sh app/VERSION             # write it, and print it
#
# Deploy workflows compute it ONCE per run (dry run), build with it, and commit
# it back only after a successful deploy -- so an image and its tag never
# disagree. Portable: BSD date (macOS) and GNU date (CI) both support %Y%m%d.
set -euo pipefail

file="${1:?usage: bump-version.sh <component>/VERSION [--dry-run]}"
dry=false
[ "${2:-}" = "--dry-run" ] && dry=true

current="$(tr -d '[:space:]' < "$file" 2>/dev/null || true)"
current="${current:-0000.00.00.000}"
today="$(date -u +%Y.%m.%d)"

IFS='.' read -r y m d build <<< "$current"
if [ "$y.$m.$d" = "$today" ]; then
  # 10#: "008" is not an (invalid) octal literal to bash arithmetic.
  next="$today.$(printf '%03d' $((10#${build:-0} + 1)))"
else
  next="$today.001"
fi

$dry || printf '%s\n' "$next" > "$file"
printf '%s\n' "$next"

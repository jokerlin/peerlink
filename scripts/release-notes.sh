#!/bin/sh
# Print the CHANGELOG.md section for one version (without its heading), for the GitHub release notes.
# usage: sh scripts/release-notes.sh <version>     prints nothing when the version has no section
set -eu
v="${1:?usage: sh scripts/release-notes.sh <version>}"
cd "$(dirname "$0")/.."
awk -v v="$v" '
  /^## \[/ { if (on) exit; on = (index($0, "## [" v "]") == 1); next }
  /^\[[^]]+\]: / { next }
  on { print }
' CHANGELOG.md | sed -e '/./,$!d' | awk '{ lines[NR] = $0 } END { n = NR; while (n > 0 && lines[n] == "") n--; for (i = 1; i <= n; i++) print lines[i] }'

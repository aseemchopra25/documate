#!/usr/bin/env bash
# Personal-info deny-list scan. The patterns arrive via $PII_PATTERNS (one
# extended regex per line) and are deliberately never stored in the repo — a
# committed deny-list in a public repo would disclose the very strings it
# guards. CI feeds it from an Actions secret; `make pii` from a file outside
# the repo. Prints matching FILE PATHS only — matched text never reaches a log.
#
# Usage: PII_PATTERNS="$(cat ...)" scan_pii.sh [dir]   (default: .)
set -euo pipefail

dir="${1:-.}"
if [ -z "${PII_PATTERNS:-}" ]; then
  echo "pii: PII_PATTERNS unset — nothing scanned (set the Actions secret, or the local pattern file)" >&2
  exit 0
fi

status=0
hits="$(grep -rIlE -f <(printf '%s\n' "$PII_PATTERNS") "$dir" \
        --exclude-dir=.git --exclude-dir=.venv --exclude-dir=.documate \
        --exclude-dir=coverage --exclude-dir=dist)" || status=$?
if [ "$status" -eq 0 ]; then
  printf 'pii: a deny-list pattern matched — inspect these files:\n%s\n' "$hits" >&2
  exit 1
elif [ "$status" -ne 1 ]; then
  echo "pii: grep failed (exit $status) — refusing to pass on an error" >&2
  exit 2
fi
echo "pii: clean — no deny-list pattern matches under $dir"

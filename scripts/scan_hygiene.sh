#!/usr/bin/env bash
# Hygiene gate for the built site: refuse to publish pages that leak local
# filesystem paths, email addresses, or IP-looking strings. One copy of the
# patterns, shared by `make hygiene` and the pages workflow.
#
# Prints matching FILE PATHS only — the matched text itself never reaches a log.
# Usage: scan_hygiene.sh [site-dir]   (default: site)
set -euo pipefail

dir="${1:-site}"
[ -d "$dir" ] || { echo "hygiene: '$dir' not found — build it first (make site)" >&2; exit 2; }

pat='(/Users/|/home/)[A-Za-z]|[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}|\b([0-9]{1,3}\.){3}[0-9]{1,3}\b'

status=0
hits="$(grep -rIlE "$pat" "$dir")" || status=$?
if [ "$status" -eq 0 ]; then
  printf 'hygiene: possible personal info in the built site — inspect these files:\n%s\n' "$hits" >&2
  exit 1
elif [ "$status" -ne 1 ]; then
  echo "hygiene: grep failed (exit $status) — refusing to pass on an error" >&2
  exit 2
fi
echo "hygiene: clean — no local paths, emails, or IP-like strings in $dir"

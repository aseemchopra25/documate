<!-- generated documentation — edit the source, not this file -->
# `scripts/scan_hygiene.sh`

Hygiene gate for the built site: refuse to publish pages that leak local
filesystem paths, email addresses, or IP-looking strings. One copy of the
patterns, shared by `make hygiene` and the pages workflow.
Prints matching FILE PATHS only — the matched text itself never reaches a log.
Usage: scan_hygiene.sh [site-dir]   (default: site)

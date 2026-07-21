<!-- generated documentation — edit the source, not this file -->
# `scripts/scan_pii.sh`

Personal-info deny-list scan. The patterns arrive via $PII_PATTERNS (one
extended regex per line) and are deliberately never stored in the repo — a
committed deny-list in a public repo would disclose the very strings it
guards. CI feeds it from an Actions secret; `make pii` from a file outside
the repo. Prints matching FILE PATHS only — matched text never reaches a log.
Usage: PII_PATTERNS="$(cat ...)" scan_pii.sh [dir]   (default: .)

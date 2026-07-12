"""coverage_report.py — render coverage.py JSON as a colored per-file table.

`make coverage` runs the suite under coverage.py, dumps JSON, and pipes the
path here: one row per source file — hue-coded percentage (green >= 80,
yellow >= 50, red below, dim zero), a 22-cell bar, covered/total statements —
grouped by directory, with a TOTAL line. Colors turn off when stdout isn't a
tty, so redirecting to a file stays clean. Stdlib only.
"""

from __future__ import annotations

import json
import sys

BARW = 22  # bar cells — wide enough to read, narrow enough for a split pane


def _c(code: str, s: str) -> str:
    """Wrap `s` in an ANSI color unless stdout is not a tty (pipes stay plain)."""
    return f"\033[{code}m{s}\033[0m" if sys.stdout.isatty() else s


def _hue(p: float) -> str:
    """The ANSI hue for a percentage: dim zero, green/yellow/red by threshold."""
    return "2;90" if p == 0 else "32" if p >= 80 else "33" if p >= 50 else "31"


def _bar(p: float) -> str:
    """A BARW-cell block bar, filled proportionally and colored by `_hue`."""
    f = round(p / 100 * BARW)
    return _c(_hue(p), "█" * f) + _c("2;90", "░" * (BARW - f))


def main(argv: list[str]) -> int:
    """Read a coverage.py JSON report (argv[0]) and print the grouped table."""
    data = json.load(open(argv[0]))
    groups: dict[str, list] = {}
    for rel, f in data["files"].items():
        s = f["summary"]
        d, _, name = rel.rpartition("/")
        groups.setdefault(d, []).append(
            (s["percent_covered"], s["covered_lines"], s["num_statements"], name)
        )
    print()
    print(" " + _c("1", "documate coverage"))
    print(" " + _c("2;90", "─" * 58))
    for d in sorted(groups):
        print(" " + _c("1;36", d + "/"))
        for p, cov, tot, name in sorted(groups[d], key=lambda r: (-r[0], r[3])):
            pct = _c(_hue(p), f"{p:5.1f}%")
            cnt = _c("2;90", f"{cov:>4}/{tot:<4}")
            print(f"   {pct}  {_bar(p)}  {cnt}  {name}")
        print()
    t = data["totals"]
    tp = t["percent_covered"]
    print(" " + _c("2;90", "─" * 58))
    print(
        f"   {_c('1', 'TOTAL')}  {_c(_hue(tp), f'{tp:5.1f}%')}  {_bar(tp)}  "
        + _c("2;90", f"{t['covered_lines']}/{t['num_statements']} statements")
    )
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

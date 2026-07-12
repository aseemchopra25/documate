# Origin

This package began as a copy of the indexer core from **code-review-graph**
(`github.com/tirth8205/code-review-graph`, v2.3.6, commit `b72413c`), licensed
MIT — Copyright (c) 2026 Tirth Kanani. MIT requires that copyright and
permission notice to stay with the code, so the `LICENSE` file in this
directory is permanent even as the code diverges.

Since July 2026 this is **first-party documate code**: no upstream is tracked,
there is no re-pull story, edit it like any other module. Two rules survive
from the old boundary because they're good architecture, not because the code
is foreign:

- `graphdb.py` stays the only importer — the engine API and its sqlite schema
  are referenced nowhere else, so engine refactors never leak.
- The engine ships no tests of its own; documate's suite (`RealGraph`-style
  fixtures in `tests/test_documate.py`) is the safety net — cover engine
  changes there.

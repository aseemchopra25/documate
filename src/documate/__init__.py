"""documate — generate docs from your code and keep them honest.

One command: bare `documate` writes the documentation (structure from the code
graph, prose from your docstrings) and then gates it; `documate --check` runs the
gate alone for CI — failing when the docs go stale, name dead code, or lie about
code that changed. Repo-agnostic: plugs into any codebase via
an optional documate.config.json.

Public surface is the CLI; the modules (core, docs, check, anchors, resolve, drift,
extract, graphdb, config) are importable for embedding.
"""

__version__ = "0.2.0"

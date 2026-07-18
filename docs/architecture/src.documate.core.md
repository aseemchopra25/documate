<!-- generated documentation — edit the source, not this file -->
# `src/documate/core.py`

core.py — the per-invocation Context: root + config + graph adapter.

A documate command runs against one repo (or one sub-tree of a monorepo). The CLI
resolves the root, loads its config, wires the graph adapter, and hands this Context to
every command. No import-time globals — the same process can point at different roots,
and nothing is hard-bound to one checkout.

**depends on** [`src/documate/config.py`](src.documate.config.md), [`src/documate/graphdb.py`](src.documate.graphdb.md)  ·  **used by** [`src/documate/anchors.py`](src.documate.anchors.md), [`src/documate/briefs.py`](src.documate.briefs.md), [`src/documate/check.py`](src.documate.check.md), [`src/documate/cli.py`](src.documate.cli.md), [`src/documate/docs.py`](src.documate.docs.md), [`src/documate/drift.py`](src.documate.drift.md), [`src/documate/prose.py`](src.documate.prose.md), [`src/documate/resolve.py`](src.documate.resolve.md), [`src/documate/site.py`](src.documate.site.md), [`src/documate/stats.py`](src.documate.stats.md)

## API

### `resolve_root(root: str | Path | None=None) -> Path`
`src/documate/core.py:32`

Explicit path wins; else the git toplevel of cwd; else cwd (non-git trees).

**called by** `Context.make`

### `class Context`
`src/documate/core.py:48`

Per-invocation bundle: resolved root, loaded Config, wired graph adapter. No import-time globals.

#### `Context.make(cls, root: str | Path | None=None) -> 'Context'`
`src/documate/core.py:56`

Resolve the root, load its config, and wire a GraphDB into a ready Context.

**calls** `resolve_root`

#### `Context.rel(self, abspath: str) -> str`
`src/documate/core.py:62`

Strip the absolute root off a graph path (file or `<abspath>::scope`),
returning a repo-relative POSIX path. Idempotent on already-relative input.

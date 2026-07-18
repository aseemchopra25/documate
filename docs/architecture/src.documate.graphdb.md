<!-- generated documentation — edit the source, not this file -->
# `src/documate/graphdb.py`

graphdb.py — documate's only door to the code graph.

Wraps the indexing engine (`._engine`). Every other documate module talks to THIS,
never to the engine internals or the sqlite schema. Refactor or swap the engine and
only this file moves — that's the decoupling the whole layering is about.

Two halves:
  index()   drives the engine to (re)build the graph at config.graph_db.
  reads     name lookup / callees / reverse-deps over a read-only connection. Reads
            DEGRADE: a missing or locked db returns empty, never raises — a sym: check
            soft-passes when the graph isn't there. It's an ephemeral artifact; never
            gate on its absence.

The sqlite schema (nodes: name/kind/qualified_name/file_path/line_start; edges:
kind/source_qualified/target_qualified) is referenced ONLY here.

**used by** [`src/documate/core.py`](src.documate.core.md)  ·  **discussed in** [`CONTRIBUTING.md`](../../CONTRIBUTING.md)

## API

### `class GraphDB`
`src/documate/graphdb.py:30`

The sole adapter over the indexing engine and its sqlite graph; every other module talks only to this.

#### `GraphDB.__init__(self, root: Path, db_path: Path, skip: tuple[str, ...]=())`
`src/documate/graphdb.py:33`

Bind the adapter to a repo root and the sqlite graph path (nothing is
opened yet). `skip` — config.skip_dirs — keeps vendored/generated trees
out of the graph itself, not just off the pages.

#### `GraphDB.index(self, incremental: bool=False) -> dict`
`src/documate/graphdb.py:43`

(Re)build the graph. Engine imports are lazy so the read path (and -h) work
without the heavy tree-sitter deps installed.

`incremental` re-parses only what changed since the graph was last built, diffing
against the HEAD sha we stored on that build — NOT the engine's default HEAD~1, which
would silently skip every commit made between two indexings and leave a stale graph
(the exact rot documate exists to stop). If that sha is gone (rebase, shallow clone,
a graph from an older documate), we can't trust the delta, so we full-build.

**calls** `GraphDB._engine_ignores`, `GraphDB._incremental_base`, `GraphDB._self_ignore`, `GraphDB.exists`

#### `GraphDB._engine_ignores(self) -> list[str]`
`src/documate/graphdb.py:70`

config.skip_dirs as engine fnmatch patterns. A skip entry is a path
substring ("/vendor/"); the engine matches fnmatch patterns against
root-relative paths, and its `*` crosses `/` — so each entry becomes a
root form and an anywhere form ("vendor/*", "*/vendor/*").

**called by** `GraphDB.index`

#### `GraphDB._self_ignore(self) -> None`
`src/documate/graphdb.py:81`

Make the graph directory ignore itself (`.gitignore` containing `*`,
the .mypy_cache convention) so the db never lands in a commit and the
user's root .gitignore stays untouched. Written only when the directory
holds nothing but documate's own artifacts — a custom graph_db pointing
into a directory the user owns must not get their files ignored.

**called by** `GraphDB.index`  ·  **calls** `GraphDB.exists`

#### `GraphDB._incremental_base(self, store) -> str | None`
`src/documate/graphdb.py:96`

The commit the graph was last built at, iff it's still a real reachable commit.
None means "don't trust an incremental delta, full-build instead": an unreachable
base makes `git diff` find nothing and re-parse nothing, silently freezing the graph
at its old state.

**called by** `GraphDB.index`

#### `GraphDB.exists(self) -> bool`
`src/documate/graphdb.py:111`

True when the graph database file is present on disk.

**called by** `GraphDB._connect`, `GraphDB._self_ignore`, `GraphDB.changed_symbols`, `GraphDB.index`

#### `GraphDB.changed_symbols(self, base: str) -> list[dict]`
`src/documate/graphdb.py:115`

Symbols (Function/Class/Test) whose line ranges overlap a git/svn diff vs
`base`. The token magic: a diff -> the exact functions that changed, not files.
Returns plain dicts (engine GraphNode kept behind this boundary). Empty without
a graph.

No CLI caller today: kept deliberately for the Claude prose layer on the
roadmap (diff -> changed symbols -> only the stale sections get rewritten).

**calls** `GraphDB.exists`

#### `GraphDB._code_parser(self)`
`src/documate/graphdb.py:155`

Lazy engine parser for fingerprinting (custom languages loaded once). Returns
None if tree-sitter isn't importable — the read path stays usable without the
heavy deps, fingerprints just degrade.

**called by** `GraphDB.fingerprint_source`, `GraphDB.fingerprint_symbol`

#### `GraphDB.fingerprint_source(self, path, source) -> str | None`
`src/documate/graphdb.py:168`

16-hex AST fingerprint of a source snippet in `path`'s language; None (degrade)
when it can't be parsed. Formatting-invariant, literal-sensitive.

**calls** `GraphDB._code_parser`

#### `GraphDB.fingerprint_symbol(self, path, source, name: str) -> str | None`
`src/documate/graphdb.py:174`

16-hex AST fingerprint of the symbol `name`, located structurally in `source`
(line-shift-proof, for the base blob). None when absent/ambiguous/unparseable.

**calls** `GraphDB._code_parser`

#### `GraphDB._connect(self)`
`src/documate/graphdb.py:181`

Read-only sqlite connection, or None when the db is absent/locked (reads degrade).

**called by** `GraphDB._chunked`, `GraphDB.call_edges`, `GraphDB.files`, `GraphDB.import_edges`, `GraphDB.nodes_by_name`, `GraphDB.symbols`, `GraphDB.tested_by`  ·  **calls** `GraphDB.exists`

#### `GraphDB.nodes_by_name(self, name: str) -> list[tuple] | None`
`src/documate/graphdb.py:190`

(qualified_name, file_path, line_start, line_end) for every non-File node
named `name`.

Returns None when the graph is absent or locked (caller DEGRADES — soft pass),
vs an empty list when the graph is readable but has no such symbol (caller HARD
fails — the doc names a ghost). That distinction is the whole point of the
None-vs-[] split here.

**calls** `GraphDB._connect`

#### `GraphDB.symbols(self, kinds=('Function', 'Class')) -> list[dict]`
`src/documate/graphdb.py:212`

Every non-test symbol of the given kinds: {name, kind, qualified, file, line}.

The structure half of the docs generator — what exists and where. Prose isn't
here (the graph stores no docstrings, on purpose: source stays truth). Empty list
when the graph is absent/locked.

**calls** `GraphDB._connect`

#### `GraphDB.files(self) -> list[str]`
`src/documate/graphdb.py:237`

Absolute path of every non-test source file the engine parsed (its File
nodes). Lets the docs generator notice symbol-free files — a Go doc.go, a
re-export __init__.py — whose whole content is module prose. Empty when the
graph is absent/locked.

**calls** `GraphDB._connect`

#### `GraphDB.call_edges(self) -> list[tuple]`
`src/documate/graphdb.py:255`

(source_qualified, target_qualified) for every CALLS edge. The docs xref
side; callers filter to the targets they recognise. Empty without a graph.

**calls** `GraphDB._connect`

#### `GraphDB.tested_by(self) -> list[tuple]`
`src/documate/graphdb.py:271`

(production_target, test_qualified) for every TESTED_BY edge — the engine
draws one whenever a test function calls a production function. The test side
is always qualified (it's the test's own name in its own file); the production
side is qualified only for a same-file call — cross-file (the normal case) it's
a bare name the caller must resolve, and drop when ambiguous. Empty without a
graph.

**calls** `GraphDB._connect`

#### `GraphDB.import_edges(self) -> list[tuple]`
`src/documate/graphdb.py:291`

(source_file, target_file) for every file-level IMPORTS_FROM edge the engine
resolved. The dependency map's source of truth for non-Python modules (JS/TS
and friends); Python is re-scanned with ast in docs — the engine truncates its
multi-name imports. Only file->file rows (no `::`): symbol-level import edges
are Python's, and unresolved externals never made it into the table as pairs
of real paths anyway. Empty without a graph.

**calls** `GraphDB._connect`

#### `GraphDB.symbols_in_files(self, abs_files) -> list[tuple]`
`src/documate/graphdb.py:314`

(name, qualified_name) for every symbol defined in the given absolute files.

**calls** `GraphDB._chunked`

#### `GraphDB.reverse_sources(self, names) -> list[str]`
`src/documate/graphdb.py:322`

source_qualified of dependency edges whose target is in `names` — the callers
side, for drift's ripple tier.

**calls** `GraphDB._chunked`

#### `GraphDB._chunked(self, sql_tmpl: str, values: list, prefix=()) -> list[tuple]`
`src/documate/graphdb.py:337`

Run an IN()-query over `values` in <1000-var chunks, return all rows.

**called by** `GraphDB.reverse_sources`, `GraphDB.symbols_in_files`  ·  **calls** `GraphDB._connect`

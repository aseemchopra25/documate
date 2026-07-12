"""graphdb.py — documate's only door to the code graph.

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
"""

from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

# Edge kinds that mean "depends on" — a change to the callee can stale a doc about the
# caller. CONTAINS/TESTED_BY are structural, not dependencies.
RIPPLE_KINDS = ("CALLS", "REFERENCES", "INHERITS", "IMPORTS_FROM")
_VAR_CHUNK = 400  # under sqlite's 999-variable IN() cap


class GraphDB:
    """The sole adapter over the indexing engine and its sqlite graph; every other module talks only to this."""

    def __init__(self, root: Path, db_path: Path, skip: tuple[str, ...] = ()):
        """Bind the adapter to a repo root and the sqlite graph path (nothing is
        opened yet). `skip` — config.skip_dirs — keeps vendored/generated trees
        out of the graph itself, not just off the pages."""
        self.root = root
        self.db_path = db_path
        self.skip = skip
        self._parser = None  # lazy CodeParser for AST fingerprints; False = unavailable

    # ---- build ----
    def index(self, incremental: bool = False) -> dict:
        """(Re)build the graph. Engine imports are lazy so the read path (and -h) work
        without the heavy tree-sitter deps installed.

        `incremental` re-parses only what changed since the graph was last built, diffing
        against the HEAD sha we stored on that build — NOT the engine's default HEAD~1, which
        would silently skip every commit made between two indexings and leave a stale graph
        (the exact rot documate exists to stop). If that sha is gone (rebase, shallow clone,
        a graph from an older documate), we can't trust the delta, so we full-build."""
        from ._engine.graph import GraphStore
        from ._engine.incremental import full_build, incremental_update

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._self_ignore()
        store = GraphStore(self.db_path)
        extra = self._engine_ignores()
        try:
            if incremental and self.db_path.exists():
                base = self._incremental_base(store)
                if base is not None:
                    return incremental_update(
                        self.root, store, base=base, extra_ignore=extra
                    )
            return full_build(self.root, store, extra_ignore=extra)
        finally:
            store.close()

    def _engine_ignores(self) -> list[str]:
        """config.skip_dirs as engine fnmatch patterns. A skip entry is a path
        substring ("/vendor/"); the engine matches fnmatch patterns against
        root-relative paths, and its `*` crosses `/` — so each entry becomes a
        root form and an anywhere form ("vendor/*", "*/vendor/*")."""
        out: list[str] = []
        for s in self.skip:
            body = s.lstrip("/")
            out += [f"{body}*", f"*/{body}*"]
        return out

    def _self_ignore(self) -> None:
        """Make the graph directory ignore itself (`.gitignore` containing `*`,
        the .mypy_cache convention) so the db never lands in a commit and the
        user's root .gitignore stays untouched. Written only when the directory
        holds nothing but documate's own artifacts — a custom graph_db pointing
        into a directory the user owns must not get their files ignored."""
        d = self.db_path.parent
        if d == self.root or (d / ".gitignore").exists():
            return
        ours = (self.db_path.name, "briefs", "stats.jsonl", "spend.jsonl")
        if all(
            p.name.startswith(self.db_path.name) or p.name in ours for p in d.iterdir()
        ):
            (d / ".gitignore").write_text("*\n")

    def _incremental_base(self, store) -> str | None:
        """The commit the graph was last built at, iff it's still a real reachable commit.
        None means "don't trust an incremental delta, full-build instead": an unreachable
        base makes `git diff` find nothing and re-parse nothing, silently freezing the graph
        at its old state."""
        sha = store.get_metadata("git_head_sha")
        if not sha:
            return None
        ok = subprocess.run(
            ["git", "-C", str(self.root), "cat-file", "-e", f"{sha}^{{commit}}"],
            capture_output=True,
        )
        return sha if ok.returncode == 0 else None

    @property
    def exists(self) -> bool:
        """True when the graph database file is present on disk."""
        return self.db_path.exists()

    def changed_symbols(self, base: str) -> list[dict]:
        """Symbols (Function/Class/Test) whose line ranges overlap a git/svn diff vs
        `base`. The token magic: a diff -> the exact functions that changed, not files.
        Returns plain dicts (engine GraphNode kept behind this boundary). Empty without
        a graph.

        No CLI caller today: kept deliberately for the Claude prose layer on the
        roadmap (diff -> changed symbols -> only the stale sections get rewritten)."""
        if not self.db_path.exists():
            return []
        from ._engine.changes import map_changes_to_nodes, parse_diff_ranges
        from ._engine.graph import GraphStore

        ranges = parse_diff_ranges(str(self.root), base)  # {rel_path: [(start,end)]}
        if not ranges:
            return []
        abs_ranges = {str(self.root / k): v for k, v in ranges.items()}
        try:  # degrade contract: a foreign/locked db reads as empty, never raises
            store = GraphStore(self.db_path)
        except sqlite3.Error:
            return []
        try:
            nodes = map_changes_to_nodes(store, abs_ranges)
        except sqlite3.Error:
            return []
        finally:
            store.close()
        return [
            {
                "name": n.name,
                "qualified": n.qualified_name,
                "kind": n.kind,
                "file": n.file_path,
                "line": n.line_start,
            }
            for n in nodes
            if n.kind in ("Function", "Class", "Test")
        ]

    # ---- AST fingerprints (drift gate; engine-driven, degradable) ----
    def _code_parser(self):
        """Lazy engine parser for fingerprinting (custom languages loaded once). Returns
        None if tree-sitter isn't importable — the read path stays usable without the
        heavy deps, fingerprints just degrade."""
        if self._parser is None:
            try:
                from ._engine.parser import CodeParser

                self._parser = CodeParser(self.root)
            except Exception:
                self._parser = False
        return self._parser or None

    def fingerprint_source(self, path, source) -> str | None:
        """16-hex AST fingerprint of a source snippet in `path`'s language; None (degrade)
        when it can't be parsed. Formatting-invariant, literal-sensitive."""
        cp = self._code_parser()
        return cp.fingerprint_source(path, source) if cp else None

    def fingerprint_symbol(self, path, source, name: str) -> str | None:
        """16-hex AST fingerprint of the symbol `name`, located structurally in `source`
        (line-shift-proof, for the base blob). None when absent/ambiguous/unparseable."""
        cp = self._code_parser()
        return cp.fingerprint_symbol(path, source, name) if cp else None

    # ---- reads (all degradable) ----
    def _connect(self):
        """Read-only sqlite connection, or None when the db is absent/locked (reads degrade)."""
        if not self.db_path.exists():
            return None
        try:
            return sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=2)
        except sqlite3.Error:
            return None

    def nodes_by_name(self, name: str) -> list[tuple] | None:
        """(qualified_name, file_path, line_start, line_end) for every non-File node
        named `name`.

        Returns None when the graph is absent or locked (caller DEGRADES — soft pass),
        vs an empty list when the graph is readable but has no such symbol (caller HARD
        fails — the doc names a ghost). That distinction is the whole point of the
        None-vs-[] split here."""
        con = self._connect()
        if con is None:
            return None
        try:
            return con.execute(
                "SELECT qualified_name, file_path, line_start, line_end FROM nodes "
                "WHERE name = ? AND kind != 'File'",
                (name,),
            ).fetchall()
        except sqlite3.Error:
            return None
        finally:
            con.close()

    def symbols(self, kinds=("Function", "Class")) -> list[dict]:
        """Every non-test symbol of the given kinds: {name, kind, qualified, file, line}.

        The structure half of the docs generator — what exists and where. Prose isn't
        here (the graph stores no docstrings, on purpose: source stays truth). Empty list
        when the graph is absent/locked."""
        con = self._connect()
        if con is None:
            return []
        try:
            marks = ",".join("?" * len(kinds))
            rows = con.execute(
                f"SELECT name, kind, qualified_name, file_path, line_start FROM nodes "
                f"WHERE kind IN ({marks}) AND is_test = 0",
                tuple(kinds),
            ).fetchall()
            return [
                {"name": n, "kind": k, "qualified": q, "file": f, "line": ln}
                for n, k, q, f, ln in rows
            ]
        except sqlite3.Error:
            return []
        finally:
            con.close()

    def files(self) -> list[str]:
        """Absolute path of every non-test source file the engine parsed (its File
        nodes). Lets the docs generator notice symbol-free files — a Go doc.go, a
        re-export __init__.py — whose whole content is module prose. Empty when the
        graph is absent/locked."""
        con = self._connect()
        if con is None:
            return []
        try:
            rows = con.execute(
                "SELECT file_path FROM nodes WHERE kind = 'File' AND is_test = 0"
            ).fetchall()
            return [r[0] for r in rows]
        except sqlite3.Error:
            return []
        finally:
            con.close()

    def call_edges(self) -> list[tuple]:
        """(source_qualified, target_qualified) for every CALLS edge. The docs xref
        side; callers filter to the targets they recognise. Empty without a graph."""
        con = self._connect()
        if con is None:
            return []
        try:
            return con.execute(
                "SELECT source_qualified, target_qualified FROM edges "
                "WHERE kind = 'CALLS'"
            ).fetchall()
        except sqlite3.Error:
            return []
        finally:
            con.close()

    def tested_by(self) -> list[tuple]:
        """(production_target, test_qualified) for every TESTED_BY edge — the engine
        draws one whenever a test function calls a production function. The test side
        is always qualified (it's the test's own name in its own file); the production
        side is qualified only for a same-file call — cross-file (the normal case) it's
        a bare name the caller must resolve, and drop when ambiguous. Empty without a
        graph."""
        con = self._connect()
        if con is None:
            return []
        try:
            return con.execute(
                "SELECT source_qualified, target_qualified FROM edges "
                "WHERE kind = 'TESTED_BY'"
            ).fetchall()
        except sqlite3.Error:
            return []
        finally:
            con.close()

    def import_edges(self) -> list[tuple]:
        """(source_file, target_file) for every file-level IMPORTS_FROM edge the engine
        resolved. The dependency map's source of truth for non-Python modules (JS/TS
        and friends); Python is re-scanned with ast in docs — the engine truncates its
        multi-name imports. Only file->file rows (no `::`): symbol-level import edges
        are Python's, and unresolved externals never made it into the table as pairs
        of real paths anyway. Empty without a graph."""
        con = self._connect()
        if con is None:
            return []
        try:
            rows = con.execute(
                "SELECT source_qualified, target_qualified FROM edges "
                "WHERE kind = 'IMPORTS_FROM'"
            ).fetchall()
            return [
                (s, t) for s, t in rows if s and t and "::" not in s and "::" not in t
            ]
        except sqlite3.Error:
            return []
        finally:
            con.close()

    def symbols_in_files(self, abs_files) -> list[tuple]:
        """(name, qualified_name) for every symbol defined in the given absolute files."""
        return self._chunked(
            "SELECT DISTINCT name, qualified_name FROM nodes "
            "WHERE kind != 'File' AND file_path IN ({marks})",
            list(abs_files),
        )

    def reverse_sources(self, names) -> list[str]:
        """source_qualified of dependency edges whose target is in `names` — the callers
        side, for drift's ripple tier."""
        names = list(names)
        if not names:
            return []
        kmarks = ",".join("?" * len(RIPPLE_KINDS))
        rows = self._chunked(
            f"SELECT DISTINCT source_qualified FROM edges "
            f"WHERE kind IN ({kmarks}) AND target_qualified IN ({{marks}})",
            names,
            prefix=RIPPLE_KINDS,
        )
        return [r[0] for r in rows if r[0]]

    def _chunked(self, sql_tmpl: str, values: list, prefix=()) -> list[tuple]:
        """Run an IN()-query over `values` in <1000-var chunks, return all rows."""
        con = self._connect()
        if con is None or not values:
            return []
        out: list[tuple] = []
        try:
            for i in range(0, len(values), _VAR_CHUNK):
                chunk = values[i : i + _VAR_CHUNK]
                marks = ",".join("?" * len(chunk))
                out.extend(
                    con.execute(
                        sql_tmpl.format(marks=marks), (*prefix, *chunk)
                    ).fetchall()
                )
            return out
        except sqlite3.Error:
            return out
        finally:
            con.close()

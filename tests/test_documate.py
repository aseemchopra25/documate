"""test_documate.py — documate's own layer on throwaway fixture repos.

documate's Context takes an explicit root, so tests just point it at a tmp git repo.
Two fixture styles: `Base` hand-builds a minimal graph.db (no tree-sitter, fully
deterministic) for the resolve/anchors/drift/check layer; `RealGraph` runs the real
engine for what the fixture can't fake (symbols, call edges, docstrings).

Run: `python -m unittest tests.test_documate`  (or pytest).
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from documate.core import GENERATED_STAMP, Context
from documate import anchors as A
from documate import briefs as BR
from documate import check as CK
from documate import docs as DOCS
from documate import drift as D
from documate import extract as EX
from documate import cli as CLI
from documate import prose as P
from documate import resolve as R
from documate import site as SITE
from documate import stats as ST
from documate import ui as UI
from documate import undo as U


def _w(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


#: fixture git must not read the machine's config — a global commit.gpgsign=true
#: adds ~150ms of gpg per commit (and serializes the whole suite behind gpg-agent)
_GIT_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
}


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t", *args],
        check=True,
        capture_output=True,
        env=_GIT_ENV,
    )


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="documate_fix_")).resolve()
        root = self.dir
        _w(root / "src" / "key.c", "int verify_key(void){return 1;}\n")
        _w(root / "src" / "app.c", "int do_unlock(void){return verify_key();}\n")
        _w(root / "src" / "misc.c", "int unrelated_fn(void){return 0;}\n")
        _w(
            root / "docs" / "guides" / "01-key.md",
            "## Key\n<!-- documents: sym:verify_key -->\n",
        )
        _w(
            root / "docs" / "guides" / "02-unlock.md",
            "## Unlock\n<!-- documents: sym:do_unlock -->\n",
        )
        self._graph(root)
        _git(root, "init", "-q")
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "fix")
        self.ctx = Context.make(root)

    def _graph(self, root: Path) -> None:
        db = root / ".documate" / "graph.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(db)
        con.execute(
            "CREATE TABLE nodes(name TEXT,kind TEXT,qualified_name TEXT,file_path TEXT,line_start INT,line_end INT,is_test INT DEFAULT 0)"
        )
        con.execute(
            "CREATE TABLE edges(kind TEXT,source_qualified TEXT,target_qualified TEXT)"
        )
        key, app, misc = (str(root / "src" / f) for f in ("key.c", "app.c", "misc.c"))
        con.executemany(
            "INSERT INTO nodes(name,kind,qualified_name,file_path,line_start,line_end) VALUES(?,?,?,?,?,?)",
            [
                ("verify_key", "Function", f"{key}::verify_key", key, 1, 1),
                ("do_unlock", "Function", f"{app}::do_unlock", app, 1, 1),
                ("unrelated_fn", "Function", f"{misc}::unrelated_fn", misc, 1, 1),
            ],
        )
        con.execute(
            "INSERT INTO edges VALUES('CALLS',?,?)", (f"{app}::do_unlock", "verify_key")
        )
        con.commit()
        con.close()

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.dir, ignore_errors=True)

    def _touch(self, rel: str) -> None:
        # appends a comment *below* the 1..1 symbol span — an unrelated in-file edit
        # that changes the file but not the documented symbol's fingerprint.
        p = self.dir / rel
        p.write_text(p.read_text() + "\n// x\n")

    def _edit_symbol(self, rel: str = "src/key.c") -> None:
        # a real semantic change to the documented symbol's body (verify_key), so its
        # AST fingerprint differs from base — genuine DIRECT drift (contrast _touch).
        p = self.dir / rel
        p.write_text(p.read_text().replace("return 1", "return 42"))

    def _fresh_docs(self) -> None:
        """Generate the generated tier and commit everything — a green starting state."""
        self.assertEqual(DOCS.run(self.ctx), 0)
        _git(self.dir, "add", "-A")
        _git(self.dir, "commit", "-q", "-m", "docs")


class TestResolve(Base):
    def test_sym(self):
        r = R.resolve(self.ctx, "sym:do_unlock")
        self.assertTrue(r.ok)
        self.assertEqual(r.targets[0]["file"], "src/app.c")

    def test_dangling(self):
        self.assertFalse(R.resolve(self.ctx, "sym:ghost").ok)

    def test_unknown_namespace(self):
        self.assertFalse(R.resolve(self.ctx, "opcode:0x42").ok)

    def test_degraded_without_graph(self):
        (self.dir / ".documate" / "graph.db").unlink()
        ctx = Context.make(self.dir)
        r = R.resolve(ctx, "sym:do_unlock")
        self.assertTrue(r.ok and r.degraded)

    def _add_node(self, name: str, rel: str) -> None:
        con = sqlite3.connect(self.dir / ".documate" / "graph.db")
        q = str(self.dir / rel)
        con.execute(
            "INSERT INTO nodes(name,kind,qualified_name,file_path,line_start,line_end)"
            " VALUES(?,?,?,?,?,?)",
            (name, "Function", f"{q}::{name}", q, 1, 1),
        )
        con.commit()
        con.close()

    def test_ambiguous_bare_sym_asks_for_path(self):
        # a second production site for verify_key -> resolve can't choose; must ask @path,
        # and the @path form disambiguates it.
        self._add_node("verify_key", "src/key2.c")
        r = R.resolve(self.ctx, "sym:verify_key")
        self.assertFalse(r.ok)
        self.assertIn("ambiguous", r.error)
        self.assertIn("@path", r.error)
        self.assertTrue(R.resolve(self.ctx, "sym:verify_key@src/key.c").ok)

    def test_production_site_beats_test_mock(self):
        # a same-named symbol in a test file (path carries a test marker) must not
        # shadow the production definition.
        self._add_node("verify_key", "src/key.test.c")
        r = R.resolve(self.ctx, "sym:verify_key")
        self.assertTrue(r.ok)
        self.assertEqual(r.targets[0]["file"], "src/key.c")


class TestConfig(Base):
    def test_unknown_key(self):
        (self.dir / "documate.config.json").write_text('{"bogus":1}')
        with self.assertRaises(ValueError):
            Context.make(self.dir)

    def test_project_name_overrides_derived_name(self):
        (self.dir / "documate.config.json").write_text('{"project_name":"krypton"}')
        ctx = Context.make(self.dir)
        self.assertEqual(DOCS._repo_name(ctx), "krypton")


class TestAnchors(Base):
    def test_build_index(self):
        idx = A.build_index(self.ctx)
        self.assertEqual(idx["sym:verify_key"], ["docs/guides/01-key.md"])
        self.assertEqual(idx["sym:do_unlock"], ["docs/guides/02-unlock.md"])

    def test_validate_clean(self):
        failed, degraded = A.validate(self.ctx)
        self.assertEqual((failed, degraded), ([], []))

    def test_validate_dangling(self):
        _w(
            self.dir / "docs" / "guides" / "99.md",
            "## B\n<!-- documents: sym:ghost -->\n",
        )
        failed, _ = A.validate(self.ctx)
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0][0], "sym:ghost")

    def test_generated_pages_are_not_scanned(self):
        # A generated page can quote a docstring that *contains example anchors* (the
        # dogfood repo hit this: anchors.py's own module doc). The scanner must skip
        # anything carrying the generated stamp, or documate flags its own output.
        from documate.core import GENERATED_STAMP

        _w(
            self.dir / "docs" / "architecture" / "x.md",
            GENERATED_STAMP + "\n# x\n<!-- documents: sym:ghost -->\n",
        )
        self.assertNotIn("sym:ghost", A.build_index(self.ctx))

    def test_validate_degrades_without_graph(self):
        (self.dir / ".documate" / "graph.db").unlink()
        ctx = Context.make(self.dir)
        failed, degraded = A.validate(ctx)
        self.assertEqual(failed, [])
        self.assertEqual(len(degraded), 2)

    def test_sig_before_sym_is_reported_bad(self):
        # a sig with no preceding sym: can't bind — must be flagged, not silently dropped.
        _w(
            self.dir / "docs" / "guides" / "99.md",
            "## B\n<!-- documents: sig:0f3a9c1b2d4e5f60 sym:verify_key -->\n",
        )
        failed, _ = A.validate(self.ctx)
        self.assertTrue(any("no sym: before it" in (e or "") for _, e, _ in failed))

    def test_conflicting_sigs_are_reported_bad(self):
        # two different sigs pinning the same sym on one page would bind one wrong and
        # never flag; scan must call it out.
        _w(
            self.dir / "docs" / "guides" / "99.md",
            "## B\n<!-- documents: sym:verify_key sig:0000000000000000 -->\n"
            "<!-- documents: sym:verify_key sig:1111111111111111 -->\n",
        )
        failed, _ = A.validate(self.ctx)
        self.assertTrue(any("conflicting" in (e or "") for _, e, _ in failed))


class TestFingerprint(Base):
    """The AST fingerprint (Weakness-1): formatting-invariant, semantics- and literal-
    sensitive, comment-agnostic, degrades to None. Driven through the graphdb adapter —
    the only door to the engine's parser — so it covers every language the engine does."""

    def fp(self, src, path="x.py"):
        return self.ctx.graph.fingerprint_source(path, src)

    def test_spacing_only_is_invariant(self):
        self.assertEqual(self.fp("x=1"), self.fp("x = 1"))
        self.assertEqual(self.fp("f( a,b )"), self.fp("f(a, b)"))

    def test_reindent_and_rewrap_are_invariant(self):
        self.assertEqual(
            self.fp("def g():\n    if x:\n        return 1"),
            self.fp("def g():\n  if x:\n            return 1"),
        )

    def test_blank_lines_and_trailing_ws_are_invariant(self):
        self.assertEqual(self.fp("a = 1\nb = 2"), self.fp("a = 1   \n\n\nb = 2\n"))

    def test_string_literal_contents_are_sensitive(self):
        # the false-negative this fixes: str.split() collapsed "a  b" == "a b".
        self.assertNotEqual(self.fp('y = "a  b"'), self.fp('y = "a b"'))

    def test_numeric_literal_digits_are_sensitive(self):
        # decision: a literal's exact characters matter -> 1_000 != 1000.
        self.assertNotEqual(self.fp("n = 1_000"), self.fp("n = 1000"))

    def test_comment_only_edit_is_invariant(self):
        # documented default policy: comment/trivia nodes are dropped.
        self.assertEqual(self.fp("z = 1  # hi"), self.fp("z = 1  # BYE, totally"))
        self.assertEqual(self.fp("z = 1  # hi"), self.fp("z = 1"))

    def test_signature_changes_are_sensitive(self):
        base = "def f(x): pass"
        self.assertNotEqual(self.fp(base), self.fp("def f(x, y): pass"))  # add param
        self.assertNotEqual(self.fp(base), self.fp("def f(x: int): pass"))  # annotation
        self.assertNotEqual(self.fp(base), self.fp("def f(x=1): pass"))  # default

    def test_body_logic_changes_are_sensitive(self):
        self.assertNotEqual(self.fp("r = a + b"), self.fp("r = a - b"))  # operator
        self.assertNotEqual(self.fp("r = foo()"), self.fp("r = bar()"))  # called name

    def test_degrades_to_none_never_raises(self):
        self.assertIsNone(self.fp("code", path="x.unknownext"))  # no language
        self.assertIsNone(D.fingerprint(self.ctx, "nope.c", 1, 1))  # unreadable file
        self.assertIsNone(
            D.fingerprint(self.ctx, "src/key.c", 5, 9)
        )  # span out of range

    def test_language_awareness_go(self):
        # not Python: a second grammar the engine supports proves the fingerprint is
        # AST-level, not text-level. Reflow invariant, operator sensitive.
        base = self.fp("func Add(a int) int {\n\treturn a + 1\n}", path="a.go")
        self.assertEqual(
            base, self.fp("func Add(a int) int { return a  +  1 }", "a.go")
        )
        self.assertNotEqual(
            base, self.fp("func Add(a int) int { return a - 1 }", "a.go")
        )

    def test_fingerprint_symbol_locates_by_name_not_line(self):
        # base-blob path: a symbol shifted down by an inserted function above still
        # fingerprints identically to its isolated form; absent name degrades to None.
        shifted = b"int other(void){return 9;}\nint verify_key(void){return 1;}\n"
        self.assertEqual(
            self.ctx.graph.fingerprint_symbol("src/key.c", shifted, "verify_key"),
            self.fp("int verify_key(void){return 1;}", path="src/key.c"),
        )
        self.assertIsNone(
            self.ctx.graph.fingerprint_symbol("src/key.c", shifted, "ghost")
        )


class TestDrift(Base):
    def test_direct_when_documented_symbol_changes(self):
        # the documented symbol's *body* changed -> DIRECT drift (sig-less, per-symbol).
        self._edit_symbol()
        direct, _, _, _ = D.find_drift(self.ctx, "HEAD", 0, 500)
        self.assertTrue(any(d["module"] == "docs/guides/01-key.md" for d in direct))

    def test_no_direct_when_only_other_part_of_file_changes(self):
        # Weakness-2: an unrelated edit elsewhere in verify_key's file (a comment
        # appended below its 1..1 span) must NOT flag the doc — file membership used to.
        self._touch("src/key.c")
        direct, _, _, _ = D.find_drift(self.ctx, "HEAD", 0, 500)
        self.assertFalse(any(d["module"] == "docs/guides/01-key.md" for d in direct))

    def test_no_direct_on_formatter_only_change(self):
        # sig-less DIRECT is an AST compare: reformatting verify_key (spacing only) is
        # not drift, even though the file changed.
        (self.dir / "src" / "key.c").write_text(
            "int   verify_key( void ){  return  1 ; }\n"
        )
        direct, _, _, _ = D.find_drift(self.ctx, "HEAD", 0, 500)
        self.assertFalse(any(d["module"] == "docs/guides/01-key.md" for d in direct))

    def test_renamed_symbol_is_a_ghost_via_validate_not_drift(self):
        # rename verify_key in source AND drop it from the graph (what a reindex does):
        # anchors.validate must fail the anchor; drift must NOT manufacture a fingerprint
        # finding for an anchor that no longer resolves.
        con = sqlite3.connect(self.dir / ".documate" / "graph.db")
        con.execute("DELETE FROM nodes WHERE name='verify_key'")
        con.commit()
        con.close()
        (self.dir / "src" / "key.c").write_text("int checks_key(void){return 1;}\n")
        failed, _ = A.validate(self.ctx)
        self.assertTrue(any(a == "sym:verify_key" for a, _, _ in failed))
        direct, _, notes, _ = D.find_drift(self.ctx, "HEAD", 0, 500)
        self.assertFalse(any(d["module"] == "docs/guides/01-key.md" for d in direct))
        self.assertTrue(any("sym:verify_key" in n for n in notes))

    def test_no_graph_degrades_to_no_gate(self):
        # missing graph: even a real symbol change can't be verified -> no gate, notes.
        (self.dir / ".documate" / "graph.db").unlink()
        ctx = Context.make(self.dir)
        self._edit_symbol()
        direct, rippled, notes, _ = D.find_drift(ctx, "HEAD", 1, 500)
        self.assertEqual((direct, rippled), ([], []))
        self.assertTrue(notes)

    def test_clean(self):
        direct, rippled, _, _ = D.find_drift(self.ctx, "HEAD", 1, 500)
        self.assertEqual((direct, rippled), ([], []))

    def test_ripple_advisory(self):
        # key.c changed AND its guide updated -> no DIRECT. The unlock guide documents
        # do_unlock, which CALLS verify_key (in key.c) -> RIPPLE.
        self._touch("src/key.c")
        self._touch("docs/guides/01-key.md")
        direct, rippled, _, _ = D.find_drift(self.ctx, "HEAD", 1, 500)
        self.assertFalse(direct)
        self.assertTrue(any("02-unlock.md" in d["module"] for d in rippled))

    def _pin(self) -> str:
        """Re-anchor the key guide with the current sig for verify_key."""
        sig = D.fingerprint(self.ctx, "src/key.c", 1, 1)
        _w(
            self.dir / "docs" / "guides" / "01-key.md",
            f"## Key\n<!-- documents: sym:verify_key sig:{sig} -->\n",
        )
        return sig

    def test_sig_match_ignores_unrelated_change_in_same_file(self):
        # the whole point of the pin: another symbol's edit in the same file is
        # not drift for this anchor (file-level git would have flagged it).
        self._pin()
        self._touch("src/key.c")  # appends a line BELOW verify_key's 1..1 span
        direct, _, _, _ = D.find_drift(self.ctx, "HEAD", 0, 500)
        self.assertFalse(any(d["module"] == "docs/guides/01-key.md" for d in direct))

    def test_sig_mismatch_gates_and_reports_current_sig(self):
        self._pin()
        (self.dir / "src" / "key.c").write_text("int verify_key(void){return 2;}\n")
        direct, _, _, _ = D.find_drift(self.ctx, "HEAD", 0, 500)
        row = next(d for d in direct if d["module"] == "docs/guides/01-key.md")
        self.assertEqual(row["sig"], D.fingerprint(self.ctx, "src/key.c", 1, 1))

    def test_sig_is_whitespace_insensitive(self):
        self._pin()
        # same tokens, different spacing runs + indent: formatting, not drift.
        (self.dir / "src" / "key.c").write_text(
            "  int  verify_key(void){return   1;}\n"
        )
        direct, _, _, _ = D.find_drift(self.ctx, "HEAD", 0, 500)
        self.assertFalse(any(d["module"] == "docs/guides/01-key.md" for d in direct))

    def test_malformed_sig_fails_validation(self):
        _w(
            self.dir / "docs" / "guides" / "01-key.md",
            "## Key\n<!-- documents: sym:verify_key sig:nothex -->\n",
        )
        failed, _ = A.validate(self.ctx)
        self.assertTrue(any("malformed sig" in (err or "") for _, err, _ in failed))


class TestCheck(Base):
    """The one gate, end to end on the deterministic fixture."""

    def test_clean_passes(self):
        self._fresh_docs()
        self.assertEqual(CK.run(self.ctx, "HEAD"), 0)

    def test_stale_generated_page_gates(self):
        self._fresh_docs()
        readme = self.ctx.config.docs_dir / "README.md"
        readme.write_text(readme.read_text() + "\nhand edit\n")
        self.assertEqual(CK.run(self.ctx, "HEAD"), 1)

    def test_missing_generated_page_gates(self):
        self._fresh_docs()
        next(iter((self.ctx.config.docs_dir / "architecture").glob("*.md"))).unlink()
        self.assertEqual(CK.run(self.ctx, "HEAD"), 1)

    def test_orphan_generated_page_gates(self):
        self._fresh_docs()
        _w(
            self.ctx.config.docs_dir / "architecture" / "zzz.md",
            GENERATED_STAMP + "\nghost page\n",
        )
        self.assertEqual(CK.run(self.ctx, "HEAD"), 1)

    def test_dangling_anchor_gates(self):
        self._fresh_docs()
        _w(
            self.dir / "docs" / "guides" / "99.md",
            "## B\n<!-- documents: sym:ghost -->\n",
        )
        self.assertEqual(CK.run(self.ctx, "HEAD"), 1)

    def test_direct_drift_gates(self):
        self._fresh_docs()
        self._edit_symbol()  # verify_key's body changes; graph (hence docs) stays fresh
        self.assertEqual(CK.run(self.ctx, "HEAD"), 1)

    def test_ripple_does_not_gate(self):
        self._fresh_docs()
        self._touch("src/key.c")
        self._touch("docs/guides/01-key.md")  # the direct doc was updated
        self.assertEqual(CK.run(self.ctx, "HEAD"), 0)


class TestBriefs(Base):
    """check --briefs: the gate's findings as self-contained work orders. The drift
    kind runs on the deterministic fixture; the undocumented kind needs the real
    engine — see TestUndocumentedBriefs."""

    def _emit(self) -> tuple[Path, list]:
        out = self.dir / ".documate" / "briefs"
        direct, _, _, _ = D.find_drift(self.ctx, "HEAD", 0, 500)
        return out, BR.emit(self.ctx, "HEAD", direct, out)

    def test_drift_brief_packs_page_source_and_new_sig(self):
        sig = D.fingerprint(self.ctx, "src/key.c", 1, 1)
        _w(
            self.dir / "docs" / "guides" / "01-key.md",
            f"## Key\n<!-- documents: sym:verify_key sig:{sig} -->\n",
        )
        (self.dir / "src" / "key.c").write_text("int verify_key(void){return 2;}\n")
        out, index = self._emit()
        row = next(r for r in index if r["kind"] == "drift")
        text = (out / row["brief"]).read_text()
        new_sig = D.fingerprint(self.ctx, "src/key.c", 1, 1)
        self.assertIn("int verify_key(void){return 2;}", text)  # current source
        self.assertIn("documents: sym:verify_key", text)  # the page as committed
        self.assertIn(f"sig:{new_sig}", text)  # the re-pin instruction
        self.assertEqual(row["sig"], new_sig)

    def test_green_repo_emits_empty_index(self):
        out, index = self._emit()
        self.assertEqual(index, [])
        self.assertEqual(json.loads((out / "briefs.json").read_text()), [])
        self.assertFalse(list(out.glob("*.md")))

    def test_fixed_finding_clears_its_stale_brief(self):
        self._edit_symbol()
        out, _ = self._emit()
        self.assertTrue(list(out.glob("*.md")))
        _git(self.dir, "add", "-A")
        _git(self.dir, "commit", "-q", "-m", "fix")
        out, index = self._emit()
        self.assertEqual(index, [])
        self.assertFalse(list(out.glob("*.md")))

    def test_check_run_emits_briefs_and_keeps_exit_code(self):
        # --briefs is a side channel: same exit code as without it, findings on disk.
        self._fresh_docs()
        out = self.dir / ".documate" / "briefs"
        self.assertEqual(CK.run(self.ctx, "HEAD", briefs_dir=out), 0)
        self.assertEqual(json.loads((out / "briefs.json").read_text()), [])
        self._edit_symbol()
        self.assertEqual(CK.run(self.ctx, "HEAD", briefs_dir=out), 1)
        index = json.loads((out / "briefs.json").read_text())
        self.assertEqual([r["kind"] for r in index], ["drift"])
        self.assertTrue((out / index[0]["brief"]).exists())

    def test_bottom_up_orders_callees_first(self):
        rows = [{"qualified": "a"}, {"qualified": "b"}, {"qualified": "c"}]
        order = BR._bottom_up(rows, {"a": {"b"}, "b": {"c"}})  # a calls b calls c
        self.assertEqual([r["qualified"] for r in order], ["c", "b", "a"])

    def test_bottom_up_breaks_cycles(self):
        rows = [{"qualified": "a"}, {"qualified": "b"}]
        order = BR._bottom_up(rows, {"a": {"b"}, "b": {"a"}})
        self.assertEqual(len(order), 2)  # terminates, keeps every row


class TestProseFix(Base):
    """`check --fix`: the model loop with a scripted stand-in for the claude CLI
    (the `cmd` seam) — drift brief in, page edit out, gate green after."""

    def _fake(self, body: str) -> list[str]:
        """Write a stand-in model script (brief on stdin, edits the repo) and
        return the cmd list prose should run instead of the claude CLI."""
        import sys

        script = self.dir / "fake_model.py"
        script.write_text(body)
        return [sys.executable, str(script)]

    def test_fix_check_repairs_drift_and_repins(self):
        self._fresh_docs()
        sig = D.fingerprint(self.ctx, "src/key.c", 1, 1)
        _w(
            self.dir / "docs" / "guides" / "01-key.md",
            f"## Key\n<!-- documents: sym:verify_key sig:{sig} -->\n",
        )
        _git(self.dir, "add", "-A")
        _git(self.dir, "commit", "-q", "-m", "pin")
        (self.dir / "src" / "key.c").write_text("int verify_key(void){return 2;}\n")
        cmd = self._fake(
            "import re, sys, pathlib\n"
            "brief = sys.stdin.read()\n"
            'page = re.search(r"^`([^`]+)` documents", brief, re.M).group(1)\n'
            'sym = re.search(r"documents `([^`]+)`", brief).group(1)\n'
            'sig = re.search(r"pin to `sig:([0-9a-f]{16})`", brief).group(1)\n'
            "pathlib.Path(page).write_text(\n"
            '    f"## Key\\n<!-- documents: sym:{sym} sig:{sig} -->\\nReverified.\\n"\n'
            ")\n"
        )
        rc = P.fix_check(self.ctx, "HEAD", "fake", cmd=cmd, yes=True)
        self.assertEqual(rc, 0)
        page = (self.dir / "docs" / "guides" / "01-key.md").read_text()
        self.assertIn(f"sig:{D.fingerprint(self.ctx, 'src/key.c', 1, 1)}", page)

    def test_fix_check_timeout_leaves_gate_red(self):
        self._fresh_docs()
        self._edit_symbol()
        cmd = self._fake("import time\ntime.sleep(10)\n")
        rc = P.fix_check(self.ctx, "HEAD", "fake", timeout=1, cmd=cmd, yes=True)
        self.assertEqual(rc, 1)  # nothing was drafted; drift still gates

    def test_fix_check_quiet_collapses_a_passing_gate_to_one_line(self):
        # bare --ai path: the leading gate is internal plumbing — a pass is one
        # ✓ line, not the three per-check verdicts
        import contextlib
        import io

        self._fresh_docs()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = P.fix_check(self.ctx, "HEAD", "fake", yes=True, quiet=True)
        self.assertEqual(rc, 0)
        shown = out.getvalue()
        self.assertIn("gate passed", shown)
        self.assertNotIn("docs fresh", shown)
        self.assertNotIn("drift", shown)


class TestBudget(Base):
    """--budget: no new model call starts once the measured spend (the calls' own
    cost reports, never a price table) reaches the cap; the remainder is
    reported and left for a re-run, not marked failed."""

    def test_agentic_path_stops_at_the_cap(self):
        import contextlib
        import io
        import sys

        bdir = self.dir / "briefs"
        bdir.mkdir()
        for name in ("b1.md", "b2.md"):
            (bdir / name).write_text("# work order\n")
        marker = self.dir / "calls.txt"
        script = self.dir / "costly_model.py"
        script.write_text(
            "import json\n"
            f"open({str(marker)!r}, 'a').write('x')\n"
            "print(json.dumps({'total_cost_usd': 2.0, 'result': 'ok'}))\n"
        )
        rows = [
            {
                "kind": "drift",
                "file": "src/key.c",
                "symbol": "verify_key",
                "brief": "b1.md",
            },
            {
                "kind": "drift",
                "file": "src/app.c",
                "symbol": "do_unlock",
                "brief": "b2.md",
            },
        ]
        spend = P._Spend()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            failures = P._draft(
                self.ctx,
                rows,
                bdir,
                "fake",
                30,
                [sys.executable, str(script)],
                spend,
                budget=1.0,
            )
        self.assertEqual(failures, 0)
        self.assertEqual(marker.read_text(), "x")  # exactly one call ran
        self.assertIn(
            "--budget $1.00 reached — 1 work order(s) not drafted", buf.getvalue()
        )
        self.assertEqual(spend.spent(), 2.0)


class TestForeignDb(Base):
    """The degrade contract at the engine door: a graph.db documate didn't build
    (here: the hand-made fixture schema) reads as empty — never a traceback."""

    def test_changed_symbols_degrades_on_foreign_schema(self):
        self._touch("src/key.c")  # a real diff, so the engine path is actually taken
        self.assertEqual(self.ctx.graph.changed_symbols("HEAD"), [])

    def test_failed_graphstore_init_closes_its_connection(self):
        # the fix behind the degrade: a raising __init__ must not leak its sqlite
        # connection (the caller never gets a handle to close).
        import gc
        import warnings

        from documate._engine.graph import GraphStore

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            with self.assertRaises(sqlite3.Error):
                GraphStore(self.dir / ".documate" / "graph.db")
            gc.collect()
        self.assertFalse([w for w in caught if issubclass(w.category, ResourceWarning)])


class TestCliBriefsFlag(unittest.TestCase):
    """`documate --briefs` argument shapes: absent -> off, bare -> default
    dir sentinel, explicit -> that DIR."""

    def _parse(self, argv):
        return CLI.build_parser().parse_args(argv)

    def test_absent_means_off(self):
        self.assertIsNone(self._parse(["--check"]).briefs)

    def test_bare_flag_means_default_dir(self):
        self.assertEqual(self._parse(["--check", "--briefs"]).briefs, "")

    def test_explicit_dir(self):
        self.assertEqual(
            self._parse(["--check", "--briefs", "out/briefs"]).briefs, "out/briefs"
        )


class TestCliOneCommand(unittest.TestCase):
    """The consolidated surface: one command, the modes are flags. --ai defaults
    to haiku; nonsense combinations and the retired verbs are refused (exit 2)."""

    def _parse(self, argv):
        return CLI.build_parser().parse_args(argv)

    def _rc(self, argv):
        import contextlib
        import io

        with contextlib.redirect_stderr(io.StringIO()):
            return CLI.main(argv)

    def test_ai_flag_shapes(self):
        self.assertIsNone(self._parse([]).ai)
        self.assertEqual(self._parse(["--ai"]).ai, "haiku")
        self.assertEqual(self._parse(["--ai", "sonnet"]).ai, "sonnet")

    def test_watch_with_ai_is_refused(self):
        self.assertEqual(self._rc(["--watch", "--ai"]), 2)

    def test_rewrite_requires_ai_and_rejects_check(self):
        self.assertFalse(self._parse([]).rewrite)
        self.assertTrue(self._parse(["--ai", "sonnet", "--rewrite"]).rewrite)
        self.assertEqual(self._rc(["--rewrite"]), 2)  # --rewrite needs --ai
        self.assertEqual(self._rc(["--check", "--ai", "--rewrite"]), 2)

    def test_check_with_watch_or_html_is_refused(self):
        self.assertEqual(self._rc(["--check", "--watch"]), 2)
        self.assertEqual(self._rc(["--check", "--html"]), 2)

    def test_retired_verbs_are_refused_with_a_hint(self):
        import contextlib
        import io

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = CLI.main(["docs"])
        self.assertEqual(rc, 2)
        self.assertIn("verb is gone", err.getvalue())


class RealGraph(unittest.TestCase):
    """A real engine-built graph (not the hand-built fixture) — the docs generator
    wants real tree-sitter parsing (symbols, call edges, docstrings) the fixture can't fake."""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="documate_gen_")).resolve()
        root = self.dir
        _w(
            root / "pkg" / "core.py",
            'def helper():\n    """Return one."""\n    return 1\n\n'
            'def entry():\n    """Entry doubles the helper."""\n    return helper() + helper()\n',
        )
        _w(
            root / "pkg" / "more.py",
            'from pkg.core import entry\n\ndef driver():\n    """Drive the entry point."""\n    return entry()\n',
        )
        _git(root, "init", "-q")
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "x")
        self.ctx = Context.make(root)
        self.ctx.graph.index()  # real tree-sitter build

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.dir, ignore_errors=True)


class TestIncrementalIndex(RealGraph):
    """The fast path: a second index re-parses only what changed, diffed against the sha the
    graph was built at — and falls back to a full rebuild when that sha is gone, so it can't
    silently freeze the graph in the past."""

    def test_incremental_reflects_a_new_symbol(self):
        # setUp built the graph at HEAD; now add a symbol (uncommitted is fine — the base is a
        # real commit, so `git diff <base>` sees working-tree edits too).
        _w(
            self.dir / "pkg" / "core.py",
            "def helper():\n    return 1\n\ndef sparkle():\n    return helper()\n",
        )
        stats = self.ctx.graph.index(incremental=True)
        self.assertIn("files_updated", stats)  # the incremental path ran, not full
        self.assertTrue(
            self.ctx.graph.nodes_by_name("sparkle")
        )  # new symbol is in the graph

    def test_stale_base_sha_falls_back_to_full(self):
        # A build-sha that no longer resolves (rebase / shallow clone) must NOT be trusted — an
        # unreachable base diffs to nothing and would re-parse nothing. We full-build instead.
        from documate._engine.graph import GraphStore

        store = GraphStore(self.ctx.graph.db_path)
        store.set_metadata("git_head_sha", "0" * 40)
        store.commit()
        store.close()
        stats = self.ctx.graph.index(incremental=True)
        self.assertIn("files_parsed", stats)  # full rebuild, not a no-op incremental


class TestStoreBatch(unittest.TestCase):
    """graph.py batches a file's nodes/edges into one executemany each. Guard the
    edge de-dup the batch relies on: exact duplicates collapse, distinct call
    sites (same target, different line) survive, and re-storing a file replaces
    its data rather than doubling it."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="documate_store_")).resolve()

    def tearDown(self):
        import shutil

        shutil.rmtree(self.dir, ignore_errors=True)

    def _store(self):
        from documate._engine.graph import GraphStore

        return GraphStore(self.dir / "graph.db")

    def test_dedups_edges_but_keeps_distinct_call_sites(self):
        from documate._engine.parser import EdgeInfo, NodeInfo

        store = self._store()
        f = "/x/a.py"
        nodes = [
            NodeInfo("File", "a.py", f, 1, 9),
            NodeInfo("Function", "caller", f, 1, 9),
        ]
        edges = [
            EdgeInfo("CALLS", f + "::caller", "callee", f, 3),
            EdgeInfo("CALLS", f + "::caller", "callee", f, 3),  # exact dup -> collapses
            EdgeInfo("CALLS", f + "::caller", "callee", f, 5),  # distinct site -> kept
        ]
        store.store_file_nodes_edges(f, nodes, edges, fhash="h1")
        out = store.get_edges_by_source(f + "::caller")
        self.assertEqual(sorted(e.line for e in out), [3, 5])  # dup gone, both sites
        self.assertEqual(len(store.get_nodes_by_file(f)), 2)
        store.close()

    def test_restore_replaces_file_data(self):
        from documate._engine.parser import EdgeInfo, NodeInfo

        store = self._store()
        f = "/x/a.py"
        store.store_file_nodes_edges(
            f,
            [NodeInfo("Function", "old", f, 1, 2)],
            [EdgeInfo("CALLS", f + "::old", "g", f, 1)],
            fhash="h1",
        )
        store.store_file_nodes_edges(
            f, [NodeInfo("Function", "new", f, 1, 2)], [], fhash="h2"
        )
        nodes = store.get_nodes_by_file(f)
        self.assertEqual({n.name for n in nodes}, {"new"})  # old symbol replaced
        self.assertEqual(nodes[0].file_hash, "h2")
        self.assertEqual(store.get_edges_by_source(f + "::old"), [])  # old edge gone
        store.close()


class TestDocsModel(RealGraph):
    def test_prose_comes_from_docstrings(self):
        model = DOCS.build_model(self.ctx)
        core = next(p for p in model.pages if p.rel.endswith("core.py"))
        entry = next(s for s in core.symbols if s.name == "entry")
        self.assertEqual(
            entry.doc, "Entry doubles the helper."
        )  # source docstring, verbatim
        self.assertEqual(entry.signature, "entry()")  # rebuilt from ast
        self.assertEqual(model.coverage["percent"], 100)

    def test_exposes_and_depends(self):
        # more.py does `from pkg.core import entry` -> core exposes entry, more depends on core.
        model = DOCS.build_model(self.ctx)
        core = next(p for p in model.pages if p.rel.endswith("core.py"))
        more = next(p for p in model.pages if p.rel.endswith("more.py"))
        self.assertIn("entry", core.exposes)
        self.assertTrue(any(d.endswith("core.py") for d in more.depends_on))
        self.assertTrue(any(u.endswith("more.py") for u in core.used_by))
        self.assertIn(("entry", "helper"), core.flow)  # the page diagram edge

    def test_undocumented_symbol_not_faked(self):
        # a symbol with no docstring is counted but never gets an invented paragraph.
        _w(
            self.dir / "pkg" / "core.py",
            'def helper():\n    """Return one."""\n    return 1\n\ndef naked():\n    return 0\n',
        )
        self.ctx.graph.index()
        model = DOCS.build_model(self.ctx)
        pages = DOCS.render(model)
        text = "\n".join(pages.values())
        self.assertIn("Undocumented (1)", text)  # the honest fold
        self.assertIn("`naked`", text)
        self.assertGreaterEqual(model.coverage["undocumented"], 1)

    def test_render_links_overview_to_pages(self):
        pages = DOCS.render(DOCS.build_model(self.ctx))
        self.assertIn("architecture/pkg.core.md", pages["README.md"])
        self.assertIn("architecture/pkg.more.md", pages["README.md"])
        self.assertIn(
            "pkg.core.md", pages["architecture/pkg.more.md"]
        )  # depends-on link


class TestMethodGrouping(RealGraph):
    """Methods are first-class: prose is keyed by the dotted qualified tail
    (`Class.method`), so two classes with a same-named method keep their own
    docstrings, and both renderers nest methods under their class."""

    def setUp(self) -> None:
        super().setUp()
        _w(
            self.dir / "pkg" / "shapes.py",
            'class Circle:\n    """A circle."""\n\n'
            '    def area(self):\n        """Pi r squared."""\n        return 3\n\n\n'
            'class Square:\n    """A square."""\n\n'
            '    def area(self):\n        """Side squared."""\n        return 4\n\n'
            "    def _hidden(self):\n        return 0\n",
        )
        _git(self.dir, "add", "-A")
        self.ctx.graph.index()
        self.model = DOCS.build_model(self.ctx)

    def test_same_named_methods_keep_their_own_docs(self):
        # the whole point of dotted keys: bare-name keying would clobber one area().
        page = next(p for p in self.model.pages if p.rel.endswith("shapes.py"))
        docs = {s.name: s.doc for s in page.symbols}
        self.assertEqual(docs["Circle.area"], "Pi r squared.")
        self.assertEqual(docs["Square.area"], "Side squared.")

    def test_methods_nest_under_their_class(self):
        md = DOCS.render(self.model)["architecture/pkg.shapes.md"]
        self.assertIn("### `class Circle`", md)
        self.assertIn("#### `Circle.area(self)`", md)  # h4 under the class h3
        self.assertLess(md.index("### `class Circle`"), md.index("#### `Circle.area"))

    def test_undocumented_fold_names_the_class(self):
        md = DOCS.render(self.model)["architecture/pkg.shapes.md"]
        self.assertIn("`Square._hidden`", md)

    def test_site_marks_method_entries(self):
        page = SITE.render(self.model)["pkg.shapes.html"]
        self.assertIn('class="api-entry method"', page)


class TestDocsRun(RealGraph):
    def test_writes_and_prunes(self):
        _w(
            self.ctx.config.docs_dir / "architecture" / "zzz.md",
            GENERATED_STAMP + "\norphan\n",
        )
        self.assertEqual(DOCS.run(self.ctx), 0)
        ddir = self.ctx.config.docs_dir
        self.assertTrue((ddir / "README.md").exists())
        self.assertTrue((ddir / "architecture" / "pkg.core.md").exists())
        self.assertFalse((ddir / "architecture" / "zzz.md").exists())  # pruned

    def test_authored_pages_untouched(self):
        guide = self.ctx.config.docs_dir / "guides" / "why.md"
        _w(guide, "hand-written\n")
        self.assertEqual(DOCS.run(self.ctx), 0)
        self.assertEqual(guide.read_text(), "hand-written\n")

    def test_check_green_after_docs(self):
        self.assertEqual(DOCS.run(self.ctx), 0)
        self.assertEqual(CK.run(self.ctx, "HEAD"), 0)

    def test_architecture_page_stitches_the_subsystems(self):
        # one page, every subsystem: module prose in context, heading linking the
        # per-module reference, neighbours linked too.
        pages = DOCS.render(DOCS.build_model(self.ctx))
        arch = pages["ARCHITECTURE.md"]
        self.assertTrue(arch.startswith(DOCS._STAMP))
        self.assertIn("(architecture/pkg.core.md)", arch)  # heading link
        for p in DOCS.build_model(self.ctx).pages:  # nothing left out
            self.assertIn(f"`{p.rel}`", arch)

    def test_reading_order_starts_at_the_entry_point(self):
        # more.py imports core.py, nothing imports more.py: more is the door, so it
        # reads first on ARCHITECTURE.md (alphabetical would put core first) and the
        # overview says where to start.
        out = DOCS.render(DOCS.build_model(self.ctx))
        arch = out["ARCHITECTURE.md"]
        self.assertLess(
            arch.index("## [`pkg/more.py`]"), arch.index("## [`pkg/core.py`]")
        )
        self.assertIn(
            "**Start here:** [`pkg/more.py`](architecture/pkg.more.md)",
            out["README.md"],
        )


class TestTour(unittest.TestCase):
    """The reading order is a pure graph fact: breadth-first from the entry points
    (nothing imports them), unreached modules appended most-used-first, every tie
    alphabetical — never a judgment call."""

    def _pages(self, *rels):
        return [DOCS.Page(r, DOCS._slug(r), None, [], {}, [], [], []) for r in rels]

    def test_walks_from_the_entry_through_its_deps(self):
        order, entries = DOCS._tour(
            self._pages("util.py", "cli.py", "core.py"),
            [("cli.py", "core.py"), ("core.py", "util.py")],
        )
        self.assertEqual(entries, ["cli.py"])
        self.assertEqual(order, ["cli.py", "core.py", "util.py"])

    def test_no_doors_falls_back_to_most_used_then_name(self):
        # a <-> b import each other (a cycle has no door); hub is used by both.
        order, entries = DOCS._tour(
            self._pages("a.py", "b.py", "hub.py"),
            [
                ("a.py", "b.py"),
                ("b.py", "a.py"),
                ("a.py", "hub.py"),
                ("b.py", "hub.py"),
            ],
        )
        self.assertEqual(entries, [])
        self.assertEqual(order, ["hub.py", "a.py", "b.py"])

    def test_no_edges_reads_alphabetically(self):
        order, entries = DOCS._tour(self._pages("b.py", "a.py"), [])
        self.assertEqual(entries, [])
        self.assertEqual(order, ["a.py", "b.py"])


class TestGroupedDocs(RealGraph):
    """Past _GROUP_AT pages (in more than one directory), the overview lists
    directories instead of a phone-book of modules, and architecture/ nests one
    folder per directory — each with its own index."""

    def setUp(self):
        super().setUp()
        _w(
            self.dir / "lib" / "util.py",
            "from pkg.core import entry\n\n"
            'def use():\n    """Use the entry point."""\n    return entry()\n',
        )
        _git(self.dir, "add", "-A")  # the engine only sees git-tracked files
        self.ctx.graph.index()
        self._cap = DOCS._GROUP_AT
        DOCS._GROUP_AT = 2  # 3 pages in 2 directories: grouped layout
        self.addCleanup(setattr, DOCS, "_GROUP_AT", self._cap)

    def test_overview_lists_directories_not_modules(self):
        pages = DOCS.render(DOCS.build_model(self.ctx))
        readme = pages["README.md"]
        self.assertIn("architecture/pkg/README.md", readme)
        self.assertIn("architecture/lib/README.md", readme)
        self.assertNotIn("architecture/pkg.core.md", readme)  # no module rows
        self.assertIn("lib --> pkg", readme)  # the map aggregates to directories

    def test_group_index_lists_its_modules(self):
        pages = DOCS.render(DOCS.build_model(self.ctx))
        idx = pages["architecture/pkg/README.md"]
        self.assertIn("(core.md)", idx)
        self.assertIn("(more.md)", idx)
        self.assertNotIn("lib/util", idx)  # scoped to its own directory

    def test_cross_directory_links_climb(self):
        pages = DOCS.render(DOCS.build_model(self.ctx))
        self.assertIn("(../pkg/core.md)", pages["architecture/lib/util.md"])  # dep
        self.assertIn("(more.md)", pages["architecture/pkg/core.md"])  # same dir
        self.assertIn("(../lib/util.md)", pages["architecture/pkg/core.md"])  # used by

    def test_architecture_page_nests_by_directory(self):
        pages = DOCS.render(DOCS.build_model(self.ctx))
        arch = pages["ARCHITECTURE.md"]
        self.assertIn("## `pkg/`", arch)  # directory sections
        self.assertIn("(architecture/pkg/core.md)", arch)  # nested links
        self.assertIn("lib --> pkg", arch)  # map aggregates to directories

    def test_grouped_reading_order_inside_and_across_directories(self):
        # within pkg/, the door (more.py) reads before core.py — alphabetical would
        # invert it — and the grouped overview still points at the doors.
        out = DOCS.render(DOCS.build_model(self.ctx))
        arch = out["ARCHITECTURE.md"]
        self.assertLess(
            arch.index("### [`pkg/more.py`]"),
            arch.index("### [`pkg/core.py`]"),
        )
        self.assertIn("**Start here:**", out["README.md"])
        self.assertIn("(architecture/lib/util.md)", out["README.md"])

    def test_layout_flip_prunes_and_check_stays_green(self):
        # write the flat layout first (a repo just under the threshold), then cross
        # it: the flat pages must be pruned, the nested ones written, check green.
        DOCS._GROUP_AT = 99
        self.assertEqual(DOCS.run(self.ctx), 0)
        flat = self.ctx.config.docs_dir / "architecture" / "pkg.core.md"
        self.assertTrue(flat.exists())
        DOCS._GROUP_AT = 2
        self.assertEqual(DOCS.run(self.ctx), 0)
        self.assertFalse(flat.exists())  # old layout gone
        nested = self.ctx.config.docs_dir / "architecture" / "pkg" / "core.md"
        self.assertTrue(nested.exists())
        self.assertEqual(CK.run(self.ctx, "HEAD"), 0)  # freshness/orphans agree


class TestAgentPointer(RealGraph):
    """`documate` maintains a marked pointer block in AGENTS.md / CLAUDE.md so
    coding agents read the generated map instead of re-crawling the repo — but only
    in files that already exist, and only between its own markers."""

    def test_block_appended_to_existing_files(self):
        _w(self.dir / "AGENTS.md", "# Agents\n\nHouse rules.\n")
        self.assertEqual(DOCS.run(self.ctx), 0)
        text = (self.dir / "AGENTS.md").read_text()
        self.assertIn("House rules.", text)  # existing prose untouched
        self.assertIn("<!-- code-map:begin -->", text)
        self.assertIn("docs/README.md", text)
        self.assertNotIn("documate", text)  # nothing we write names the tool
        self.assertFalse((self.dir / "CLAUDE.md").exists())  # never created

    def test_stale_block_rewritten_in_place(self):
        # legacy markers from an older release upgrade in place, not duplicate
        _w(
            self.dir / "CLAUDE.md",
            "intro\n\n<!-- documate:begin -->\nold pointer\n<!-- documate:end -->\n\ntail\n",
        )
        self.assertEqual(DOCS.run(self.ctx), 0)
        text = (self.dir / "CLAUDE.md").read_text()
        self.assertNotIn("old pointer", text)
        self.assertNotIn("documate:begin", text)
        self.assertIn("<!-- code-map:begin -->", text)
        self.assertIn("docs/README.md", text)
        self.assertTrue(text.startswith("intro"))
        self.assertIn("tail", text)  # everything outside the markers survives

    def test_idempotent(self):
        _w(self.dir / "AGENTS.md", "# Agents\n")
        self.assertEqual(DOCS.run(self.ctx), 0)
        first = (self.dir / "AGENTS.md").read_text()
        self.assertEqual(DOCS.run(self.ctx), 0)
        self.assertEqual((self.dir / "AGENTS.md").read_text(), first)


class TestSite(RealGraph):
    """site.render consumes the exact model docs.render does — the HTML can't disagree
    with the markdown tier — and site.run writes a flat, prunable build artifact."""

    def test_renders_the_same_model(self):
        pages = SITE.render(DOCS.build_model(self.ctx))
        self.assertEqual(
            set(pages),
            {
                "index.html",
                "architecture.html",
                "style.css",
                "nav.js",
                "pkg.core.html",
                "pkg.more.html",
            },
        )
        self.assertIn("Entry doubles the helper.", pages["pkg.core.html"])  # prose
        self.assertIn('href="pkg.core.html"', pages["index.html"])  # index links pages
        self.assertIn('href="pkg.core.html"', pages["pkg.more.html"])  # depends-on chip
        self.assertIn("flowchart TD", pages["pkg.core.html"])  # the page diagram
        self.assertIn("flowchart LR", pages["index.html"])  # the module map

    def test_nav_is_shared_not_inlined(self):
        # one nav.js for the whole site: a page's size must not grow with the number
        # of pages (inlining the sidebar everywhere made a 2,000-page site O(pages²)).
        pages = SITE.render(DOCS.build_model(self.ctx))
        self.assertNotIn('class="page', pages["pkg.core.html"])  # no inlined nav links
        self.assertIn('data-active="pkg.core"', pages["pkg.core.html"])  # highlight key
        self.assertIn('"pkg.more"', pages["nav.js"])  # the one copy lists every page

    def test_docstring_html_is_escaped(self):
        _w(
            self.dir / "pkg" / "core.py",
            'def helper():\n    """Careful with <b> & friends."""\n    return 1\n',
        )
        self.ctx.graph.index()
        pages = SITE.render(DOCS.build_model(self.ctx))
        self.assertIn("Careful with &lt;b&gt; &amp; friends.", pages["pkg.core.html"])
        self.assertNotIn("<b> &", pages["pkg.core.html"])

    def test_run_writes_and_prunes(self):
        _w(self.ctx.config.site_dir / "zzz.html", "orphan\n")
        self.assertEqual(SITE.run(self.ctx), 0)
        sdir = self.ctx.config.site_dir
        self.assertTrue((sdir / "index.html").exists())
        self.assertTrue((sdir / "style.css").exists())
        self.assertTrue((sdir / "nav.js").exists())
        self.assertFalse((sdir / "zzz.html").exists())  # pruned

    def test_site_is_not_gated(self):
        # the site is a build artifact: absent, stale, or orphaned, check stays green.
        self.assertEqual(DOCS.run(self.ctx), 0)
        _w(self.ctx.config.site_dir / "index.html", "<html>ancient</html>\n")
        self.assertEqual(CK.run(self.ctx, "HEAD"), 0)

    def test_authored_guide_gets_a_page(self):
        # the hand-written tier rides along: converted from markdown, in the nav,
        # anchor comments stripped (they're for check, not readers).
        _w(
            self.dir / "docs" / "guides" / "why.md",
            "# The why\n\n<!-- documents: sym:entry -->\n\n"
            "Prose with `code` and **weight**.\n\n- one\n- two\n",
        )
        self.assertEqual(DOCS.run(self.ctx), 0)  # generated tier alongside
        self.assertEqual(SITE.run(self.ctx), 0)
        page = (self.ctx.config.site_dir / "guides.why.html").read_text()
        self.assertIn("<h1>The why</h1>", page)
        self.assertIn("<code>code</code> and <strong>weight</strong>", page)
        self.assertIn("<li>one</li>", page)
        self.assertNotIn("documents:", page)  # anchor comment invisible
        nav = (self.ctx.config.site_dir / "nav.js").read_text()
        self.assertIn('"guides.why"', nav)  # in the shared nav

    def test_generated_pages_are_not_guides(self):
        # the stamped tier must not show up twice: docs/README.md and architecture/*
        # are already the model's pages, not authored guides.
        self.assertEqual(DOCS.run(self.ctx), 0)
        self.assertEqual(SITE.run(self.ctx), 0)
        names = {p.name for p in self.ctx.config.site_dir.glob("*.html")}
        self.assertNotIn("README.html", names)
        self.assertEqual(
            names,
            {"index.html", "architecture.html", "pkg.core.html", "pkg.more.html"},
        )

    def test_guide_links_resolve_at_render_time(self):
        # sibling .md links become site pages; links out of the docs tree become
        # blob URLs on the remote at default_base; nothing ships as-written.
        _git(self.dir, "remote", "add", "origin", "git@github.com:acme/proj.git")
        _w(
            self.dir / "docs" / "guides" / "a.md",
            "# A\n\nSee [B](b.md), the [map](../README.md), "
            "and [the code](../../pkg/core.py).\n\n"
            "```md\na fenced [example](untouched.md) stays literal\n```\n",
        )
        _w(self.dir / "docs" / "guides" / "b.md", "# B\n\nHello.\n")
        self.assertEqual(SITE.run(self.ctx), 0)
        page = (self.ctx.config.site_dir / "guides.a.html").read_text()
        self.assertIn('href="guides.b.html"', page)
        self.assertIn('href="index.html"', page)  # the generated overview's page
        self.assertIn(
            'href="https://github.com/acme/proj/blob/main/pkg/core.py"', page
        )
        self.assertIn("untouched.md", page)  # fenced example left alone

    def test_dead_guide_link_fails_the_build(self):
        import contextlib
        import io

        _w(
            self.dir / "docs" / "guides" / "a.md",
            "# A\n\nSee [ghost](nothing-there.md).\n",
        )
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            rc = SITE.run(self.ctx)
        self.assertEqual(rc, 1)
        self.assertIn("nothing-there.md", buf.getvalue())
        self.assertIn("dead link", buf.getvalue())
        self.assertFalse((self.ctx.config.site_dir / "guides.a.html").exists())

    def test_guide_tables_render_as_tables(self):
        _w(
            self.dir / "docs" / "guides" / "t.md",
            "# T\n\n| flag | effect |\n|---|---|\n| `-x` | stops |\n| `-y` | goes |\n",
        )
        self.assertEqual(SITE.run(self.ctx), 0)
        page = (self.ctx.config.site_dir / "guides.t.html").read_text()
        self.assertIn("<table>", page)
        self.assertIn("<th>flag</th>", page)
        self.assertIn("<td><code>-x</code></td>", page)
        self.assertNotIn("| flag |", page)  # no raw pipes

    def test_doxygen_docstrings_render_structured(self):
        html_out = SITE._prose(
            "@brief Sums two values.\n@param a the left value\n"
            "@param b the right value\n@return the total"
        )
        self.assertIn("<p>Sums two values.</p>", html_out)
        self.assertIn('<dl class="params">', html_out)
        self.assertIn("<dt><code>a</code></dt><dd>the left value</dd>", html_out)
        self.assertIn("<dt>returns</dt><dd>the total</dd>", html_out)
        self.assertNotIn("@brief", html_out)  # markers never print raw
        # plain prose keeps the paragraph path
        self.assertEqual(SITE._prose("Just words."), "<p>Just words.</p>")

    def test_getting_started_guide_headlines_the_landing_page(self):
        # a getting-started guide is featured: its opening line becomes the hero lede
        # and its install prose is inlined on index.html, so the front page shows how
        # to start — not just a module map. It is not also listed as a plain card.
        _w(
            self.dir / "docs" / "guides" / "getting-started.md",
            "# Get started\n\nInstall it and point it at your repo.\n\n"
            "## Install\n\n```bash\npip install thing\n```\n",
        )
        self.assertEqual(SITE.run(self.ctx), 0)
        idx = (self.ctx.config.site_dir / "index.html").read_text()
        self.assertIn('class="hero"', idx)  # the landing hero
        self.assertIn("Install it and point it at your repo.", idx)  # lede from guide
        self.assertIn("pip install thing", idx)  # install prose inlined
        self.assertIn('href="guides.getting-started.html">Get started', idx)  # CTA
        self.assertEqual(
            idx.count("guides.getting-started.html"), 1
        )  # not double-listed
        # still its own page in the nav, like any guide
        self.assertTrue(
            (self.ctx.config.site_dir / "guides.getting-started.html").exists()
        )


class TestDocsDiff(RealGraph):
    """docs.run(diff=True) — the --watch live view: a changed page prints as a unified
    diff, an untouched run says so, and unchanged pages are not rewritten."""

    def test_changed_page_prints_a_diff(self):
        import contextlib
        import io

        self.assertEqual(DOCS.run(self.ctx), 0)
        _w(
            self.dir / "pkg" / "core.py",
            'def helper():\n    """Return one."""\n    return 1\n\n'
            'def entry():\n    """Entry now triples the helper."""\n    return helper() * 3\n',
        )
        self.ctx.graph.index()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(DOCS.run(self.ctx, diff=True), 0)
        text = out.getvalue()
        self.assertIn("~ architecture/pkg.core.md", text)  # the changed page, flagged
        self.assertIn("+Entry now triples the helper.", text)  # new line, + prefixed
        self.assertIn("-Entry doubles the helper.", text)  # old line, - prefixed
        self.assertIn("1 page(s) changed", text)

    def test_idle_run_says_no_change(self):
        import contextlib
        import io

        self.assertEqual(DOCS.run(self.ctx), 0)
        readme = self.ctx.config.docs_dir / "README.md"
        mtime = readme.stat().st_mtime_ns
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(DOCS.run(self.ctx, diff=True), 0)
        self.assertIn("no page(s) changed", out.getvalue())
        self.assertEqual(readme.stat().st_mtime_ns, mtime)  # untouched, not rewritten


class TestWatchSnapshot(RealGraph):
    """cli._snapshot — the --watch change detector: tracked source in, the docs and
    site trees out (regeneration writes there; watching them would retrigger forever)."""

    def test_tracks_source_and_skips_output_trees(self):
        _w(self.ctx.config.docs_dir / "README.md", "generated\n")
        _w(self.ctx.config.site_dir / "index.html", "<html></html>\n")
        _git(self.dir, "add", "-A")
        snap = CLI._snapshot(self.ctx)
        self.assertIn("pkg/core.py", snap)
        self.assertNotIn("docs/README.md", snap)
        self.assertNotIn("site/index.html", snap)

    def test_edit_and_delete_both_move_the_snapshot(self):
        before = CLI._snapshot(self.ctx)
        _w(self.dir / "pkg" / "core.py", "def helper():\n    return 2\n")
        edited = CLI._snapshot(self.ctx)
        self.assertNotEqual(before, edited)
        (self.dir / "pkg" / "more.py").unlink()
        self.assertNotIn("pkg/more.py", CLI._snapshot(self.ctx))


class TestChangedSymbols(RealGraph):
    """graphdb.changed_symbols: the diff->symbol map kept for the prose-layer roadmap."""

    def test_changed_symbol_maps_to_node(self):
        (self.dir / "pkg" / "core.py").write_text(
            "def helper():\n    return 2\n\ndef entry():\n    return helper()\n"
        )
        names = {s["name"] for s in self.ctx.graph.changed_symbols("HEAD")}
        self.assertIn("helper", names)


class TestUndocumentedBriefs(RealGraph):
    """The 'make documentation' half of --briefs: a changed symbol with no docstring
    gets a work order; its documented neighbors don't, but their docs are quoted."""

    def test_changed_undocumented_symbol_gets_brief(self):
        _w(
            self.dir / "pkg" / "core.py",
            'def helper():\n    """Return one."""\n    return 1\n\n'
            'def entry():\n    """Entry doubles the helper."""\n    return helper() + helper()\n\n'
            "def sparkle():\n    return helper()\n",
        )
        self.ctx.graph.index(incremental=True)
        out = self.dir / ".documate" / "briefs"
        index = BR.emit(self.ctx, "HEAD", [], out)
        row = next(r for r in index if r["symbol"] == "sparkle")
        self.assertEqual(row["kind"], "undocumented")
        self.assertNotIn("helper", {r["symbol"] for r in index})  # documented: no brief
        text = (out / row["brief"]).read_text()
        self.assertIn("def sparkle():", text)
        self.assertIn("Return one.", text)  # callee helper's doc, quoted for context

    def test_list_undocumented_is_pure_json_on_stdout(self):
        import contextlib
        import io

        _w(
            self.dir / "pkg" / "core.py",
            (self.dir / "pkg" / "core.py").read_text()
            + "\ndef sparkle():\n    return helper()\n",
        )
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = CLI.main(["--list-undocumented", str(self.dir)])
        self.assertEqual(rc, 0)
        rows = json.loads(buf.getvalue())  # stdout parses whole: no chatter mixed in
        undoc = {(r["kind"], r["file"], r["symbol"]) for r in rows}
        self.assertIn(("undocumented", "pkg/core.py", "sparkle"), undoc)
        # setUp's files carry no module prose — the module tier is listed too
        self.assertIn(("module", "pkg/core.py", "module"), undoc)
        # documented symbols stay out
        self.assertNotIn(("undocumented", "pkg/core.py", "helper"), undoc)


class TestProseSeed(RealGraph):
    """`docs --fix`: the fresh-repo seeding pass — undocumented Python symbols go
    through the batched single-turn path (one scripted-model call, marked blocks
    out, documate inserts), and the pages regenerate from the drafts."""

    def _sparkle(self):
        # module docstrings included: these tests are about the symbol tier,
        # so the module tier must not add work orders of its own
        _w(
            self.dir / "pkg" / "core.py",
            '"""Core helpers."""\n\n'
            'def helper():\n    """Return one."""\n    return 1\n\n'
            'def entry():\n    """Entry doubles the helper."""\n    return helper() + helper()\n\n'
            "def sparkle():\n    return helper()\n",
        )
        _w(
            self.dir / "pkg" / "more.py",
            '"""More drivers."""\n\nfrom pkg.core import entry\n\n'
            'def driver():\n    """Drive the entry point."""\n    return entry()\n',
        )
        self.ctx.graph.index(incremental=True)

    def _fix(self, script_body: str):
        import contextlib
        import io
        import sys

        script = self.dir / "fake_model.py"
        script.write_text(script_body)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            rc = P.fix_docs(
                self.ctx, "fake", cmd=[sys.executable, str(script)], yes=True
            )
        return rc, buf.getvalue()

    def test_only_aims_the_run_at_one_file(self):
        import contextlib
        import io
        import sys

        self._sparkle()
        _w(
            self.dir / "pkg" / "more.py",
            (self.dir / "pkg" / "more.py").read_text()
            + "\ndef plain():\n    return 0\n",
        )
        self.ctx.graph.index(incremental=True)
        script = self.dir / "fake_model.py"
        script.write_text(
            "import re, sys\n"
            "prompt = sys.stdin.read()\n"
            'for n, _ in re.findall(r"## Work order (\\d+): `([^`]+)`", prompt):\n'
            '    print(f"<<<doc {n}>>>")\n'
            '    print("Drafted.")\n'
            '    print("<<<end>>>")\n'
        )
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            rc = P.fix_docs(
                self.ctx,
                "fake",
                cmd=[sys.executable, str(script)],
                yes=True,
                only="pkg/more*",
            )
        self.assertEqual(rc, 0)
        self.assertIn("keeps 1 of 2 work order(s)", buf.getvalue())
        self.assertIn('"""Drafted."""', (self.dir / "pkg" / "more.py").read_text())
        self.assertNotIn("Drafted", (self.dir / "pkg" / "core.py").read_text())

    def test_dry_run_plans_without_calling_the_model(self):
        import contextlib
        import io
        import sys

        self._sparkle()
        script = self.dir / "boom_model.py"
        script.write_text("import sys; sys.exit(3)\n")  # any call would fail loudly
        before = (self.dir / "pkg" / "core.py").read_text()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            rc = P.fix_docs(
                self.ctx, "fake", cmd=[sys.executable, str(script)], dry=True
            )
        self.assertEqual(rc, 0)
        shown = buf.getvalue()
        self.assertIn("--ai plan", shown)  # the same plan a real run confirms
        self.assertIn("--dry-run", shown)
        self.assertEqual((self.dir / "pkg" / "core.py").read_text(), before)
        # a glob matching nothing is an explicit clean no-op, not silence
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            rc = P.fix_docs(
                self.ctx,
                "fake",
                cmd=[sys.executable, str(script)],
                yes=True,
                only="nowhere/*",
            )
        self.assertEqual(rc, 0)
        self.assertIn("nothing to draft", buf.getvalue())

    def test_format_cmd_runs_over_touched_files(self):
        import contextlib
        import io
        import shlex
        import sys

        self._sparkle()
        fmt = self.dir / "fake_fmt.py"
        fmt.write_text(
            "import sys\n"
            "for p in sys.argv[1:]:\n"
            "    s = open(p).read()\n"
            "    open(p, 'w').write('# formatted\\n' + s)\n"
        )
        (self.dir / "documate.config.json").write_text(
            json.dumps(
                {"format_cmd": f"{shlex.quote(sys.executable)} {shlex.quote(str(fmt))}"}
            )
        )
        self.ctx = Context.make(self.dir)
        script = self.dir / "fake_model.py"
        script.write_text(
            "import re, sys\n"
            "prompt = sys.stdin.read()\n"
            'for n, _ in re.findall(r"## Work order (\\d+): `([^`]+)`", prompt):\n'
            '    print(f"<<<doc {n}>>>")\n'
            '    print("Drafted.")\n'
            '    print("<<<end>>>")\n'
        )
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            rc = P.fix_docs(
                self.ctx, "fake", cmd=[sys.executable, str(script)], yes=True
            )
        self.assertEqual(rc, 0)
        core = (self.dir / "pkg" / "core.py").read_text()
        self.assertIn('"""Drafted."""', core)  # the draft landed
        self.assertTrue(core.startswith("# formatted\n"))  # then the formatter ran
        # untouched files are not formatted
        self.assertNotIn("# formatted", (self.dir / "pkg" / "more.py").read_text())
        self.assertIn("format_cmd ran over 1 file(s)", buf.getvalue())

    def test_fix_docs_seeds_missing_docstrings(self):
        self._sparkle()
        # the stand-in model speaks the batch protocol: prompt on stdin (one
        # numbered work order per symbol), one <<<doc n>>> block per order out.
        rc, shown = self._fix(
            "import re, sys\n"
            "prompt = sys.stdin.read()\n"
            'assert "## Work order 1:" in prompt\n'
            'for n, _ in re.findall(r"## Work order (\\d+): `([^`]+)`", prompt):\n'
            '    print(f"<<<doc {n}>>>")\n'
            '    print("Drafted.")\n'
            '    print("<<<end>>>")\n'
        )
        self.assertEqual(rc, 0)
        self.assertIn('"""Drafted."""', (self.dir / "pkg" / "core.py").read_text())
        # the pre-flight plan is on record even under --yes (a CI log shows the spend)
        self.assertIn("--ai plan", shown)
        self.assertIn("1 work order(s) · 1 fake call(s)", shown)
        self.assertIn("tokens", shown)
        # each draft announces itself once: file + symbol + its summary sentence;
        # the full text is git diff's job, so no diff dump in the run output
        self.assertIn("pkg/core.py  sparkle — Drafted.", shown)
        self.assertNotIn("~ pkg/core.py", shown)
        self.assertIn("file(s) touched", shown)
        self.assertIn("review with git diff", shown)
        # the post-draft regeneration is quiet — one coverage line per run,
        # the baseline printed before the plan
        self.assertEqual(shown.count("coverage"), 1)
        # nothing left to seed: a fresh all-scope emit is empty
        out = self.dir / ".documate" / "briefs"
        self.assertEqual(BR.emit(self.ctx, "HEAD", [], out, undocumented="all"), [])

    def test_fix_docs_seeds_go_doc_comments(self):
        # Go rides the same batched path: the draft lands as a `//` comment
        # block directly above the declaration, no agent call per symbol
        _w(
            self.dir / "pkg" / "lib.go",
            "package pkg\n\n"
            "// Helper returns one.\n"
            "func Helper() int { return 1 }\n\n"
            "func Glimmer() int { return Helper() }\n",
        )
        _git(self.dir, "add", "-A")
        self.ctx.graph.index(incremental=True)
        rc, shown = self._fix(
            "import re, sys\n"
            "prompt = sys.stdin.read()\n"
            # lanes are file-disjoint: only the lane holding the .go file
            # carries the Go-convention instruction
            'assert "lib.go" not in prompt or "Go doc convention" in prompt\n'
            'for n, _ in re.findall(r"## Work order (\\d+): `([^`]+)`", prompt):\n'
            '    print(f"<<<doc {n}>>>")\n'
            '    print("Glimmer drafts.")\n'
            '    print("<<<end>>>")\n'
        )
        self.assertEqual(rc, 0)
        src = (self.dir / "pkg" / "lib.go").read_text()
        self.assertIn("// Glimmer drafts.\nfunc Glimmer()", src)
        self.assertIn("pkg/lib.go  Glimmer — Glimmer drafts.", shown)
        self.assertNotIn("in-place repairs", shown)  # batched, not agentic
        # the insert shifted every decl below it — the post-draft re-index means
        # line-anchored doc reads see the comment, not a stale line number
        self.assertNotIn("still undocumented", shown)
        out = self.dir / ".documate" / "briefs"
        self.assertEqual(BR.emit(self.ctx, "HEAD", [], out, undocumented="all"), [])

    def test_insert_go_refuses_a_documented_declaration(self):
        _w(
            self.dir / "pkg" / "lib.go",
            "package pkg\n\n// Helper returns one.\nfunc Helper() int { return 1 }\n",
        )
        row = {"kind": "undocumented", "file": "pkg/lib.go", "symbol": "Helper"}
        self.assertEqual(P._insert(self.ctx, row, "New text."), "already documented")

    def test_fix_docs_seeds_module_docs(self):
        # 100% symbol coverage still leaves the architecture page's sections
        # reading "No module docstring" — seeding drafts the module tier too:
        # a Go package comment above the package clause, a Python module
        # docstring as the file's first statement
        self._sparkle()
        _w(
            self.dir / "pkg" / "lib.go",
            "package pkg\n\n// Helper returns one.\nfunc Helper() int { return 1 }\n",
        )
        _w(
            self.dir / "pkg" / "naked.py",
            'def lone():\n    """Alone."""\n    return 0\n',
        )
        _git(self.dir, "add", "-A")
        self.ctx.graph.index(incremental=True)
        rc, shown = self._fix(
            "import re, sys\n"
            "prompt = sys.stdin.read()\n"
            # the What-to-write section survives _context's batch trimming
            # (checked only in the lane that holds the .go file)
            'assert "lib.go" not in prompt or "Go package comment" in prompt\n'
            'for n, _ in re.findall(r"## Work order (\\d+): `([^`]+)`", prompt):\n'
            '    print(f"<<<doc {n}>>>")\n'
            '    print("Drafted module.")\n'
            '    print("<<<end>>>")\n'
        )
        self.assertEqual(rc, 0)
        go = (self.dir / "pkg" / "lib.go").read_text()
        self.assertTrue(go.startswith("// Drafted module.\npackage pkg"), go[:60])
        py = (self.dir / "pkg" / "naked.py").read_text()
        self.assertTrue(py.startswith('"""Drafted module."""\n\ndef lone'), py[:60])
        self.assertIn("pkg/lib.go  module — Drafted module.", shown)
        # nothing left: the re-emit sees the fresh module prose
        out = self.dir / ".documate" / "briefs"
        self.assertEqual(BR.emit(self.ctx, "HEAD", [], out, undocumented="all"), [])

    def test_fix_docs_seeds_c_doc_comments(self):
        # C (and every other doc-above comment language) rides the batched
        # path too — no per-symbol agent calls. Two undocumented functions in
        # one file prove the shift tracking: the second insert lands on its
        # line corrected for the first; the module comment tops the file.
        self._sparkle()
        _w(
            self.dir / "src" / "util.c",
            "#include <stdio.h>\n\n"
            "int alpha(void) { return 1; }\n\n"
            "int beta(void) { return alpha(); }\n",
        )
        _git(self.dir, "add", "-A")
        self.ctx.graph.index(incremental=True)
        rc, shown = self._fix(
            "import re, sys\n"
            "prompt = sys.stdin.read()\n"
            'for n, _ in re.findall(r"## Work order (\\d+): `([^`]+)`", prompt):\n'
            '    print(f"<<<doc {n}>>>")\n'
            '    print(f"Drafted {n}.")\n'
            '    print("<<<end>>>")\n'
        )
        self.assertEqual(rc, 0)
        src = (self.dir / "src" / "util.c").read_text()
        # C is documented with Doxygen blocks, never `//` runs: Doxygen ignores
        # line comments, so a `//` draft leaves the symbol undocumented in the one
        # tool the language documents with.
        self.assertRegex(src, r"/\*\*\n \* Drafted \d+\.\n \*/\nint alpha")
        self.assertRegex(src, r"/\*\*\n \* Drafted \d+\.\n \*/\nint beta")
        self.assertTrue(src.startswith("/**\n * @file util.c\n * Drafted"), src[:60])
        self.assertNotIn("// Drafted", src)
        self.assertNotIn("in-place repairs", shown)  # batched, not agentic
        self.assertNotIn("failed", shown)
        out = self.dir / ".documate" / "briefs"
        self.assertEqual(BR.emit(self.ctx, "HEAD", [], out, undocumented="all"), [])

    def test_lanes_are_file_disjoint_and_keep_a_file_in_order(self):
        rows = [
            {"file": f, "symbol": s}
            for f, s in [
                ("a.c", "one"),
                ("b.c", "two"),
                ("a.c", "three"),
                ("c.c", "four"),
                ("a.c", "module"),
            ]
        ]
        lanes = P._lanes(rows)
        self.assertLessEqual(len(lanes), P._WORKERS)
        per_file = {}
        for i, lane in enumerate(lanes):
            for r in lane:
                per_file.setdefault(r["file"], i)
                self.assertEqual(per_file[r["file"]], i)  # never split across lanes
        a = [r["symbol"] for lane in lanes for r in lane if r["file"] == "a.c"]
        self.assertEqual(a, ["one", "three", "module"])  # module still last
        self.assertEqual(P._lanes([]), [])

    def test_insert_module_refuses_documented_files(self):
        _w(
            self.dir / "pkg" / "lib.go",
            "// Package pkg is documented.\npackage pkg\n\nfunc Helper() int { return 1 }\n",
        )
        row = {"kind": "module", "file": "pkg/lib.go", "symbol": "module"}
        self.assertEqual(P._insert(self.ctx, row, "New."), "already documented")
        _w(self.dir / "pkg" / "told.py", '"""Documented."""\n\nX = 1\n')
        row = {"kind": "module", "file": "pkg/told.py", "symbol": "module"}
        self.assertEqual(P._insert(self.ctx, row, "New."), "already documented")

    def test_fix_docs_stream_json_deltas_split_mid_marker(self):
        # the real CLI protocol: text arrives as stream-json text_delta events,
        # sliced without regard for block markers — the incremental parser must
        # still find the completed block and insert.
        self._sparkle()
        rc, shown = self._fix(
            "import json\n"
            "import sys\n"
            "sys.stdin.read()\n"
            'for piece in ["<<<doc 1", ">>>\\nStreamed", " draft.\\n<<<", "end>>>"]:\n'
            "    ev = {\n"
            '        "type": "stream_event",\n'
            '        "event": {\n'
            '            "type": "content_block_delta",\n'
            '            "delta": {"type": "text_delta", "text": piece},\n'
            "        },\n"
            "    }\n"
            "    print(json.dumps(ev))\n"
        )
        self.assertEqual(rc, 0)
        self.assertIn(
            '"""Streamed draft."""', (self.dir / "pkg" / "core.py").read_text()
        )
        self.assertIn("pkg/core.py  sparkle — Streamed draft.", shown)

    def test_fix_docs_meters_exact_spend_from_usage_events(self):
        # the CLI reports what a call actually cost (usage events + the result
        # payload's total_cost_usd); the run's totals line carries the measured
        # figure — never a price table
        self._sparkle()
        rc, shown = self._fix(
            "import json, sys\n"
            "sys.stdin.read()\n"
            "def ev(e):\n"
            '    print(json.dumps({"type": "stream_event", "event": e}))\n'
            'ev({"type": "message_start", "message": {"usage": {"input_tokens": 90, "cache_read_input_tokens": 10}}})\n'
            'ev({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "<<<doc 1>>>\\nMetered.\\n<<<end>>>"}})\n'
            'ev({"type": "message_delta", "usage": {"output_tokens": 50}})\n'
            "print(json.dumps({\n"
            '    "type": "result", "result": "",\n'
            '    "usage": {"input_tokens": 90, "cache_read_input_tokens": 10, "output_tokens": 50},\n'
            '    "total_cost_usd": 0.0123,\n'
            "}))\n"
        )
        self.assertEqual(rc, 0)
        self.assertIn("150 tok · $0.0123", shown)

    def test_fix_docs_malformed_reply_fails_loud(self):
        self._sparkle()
        rc, shown = self._fix('print("no blocks here, just chatter")\n')
        self.assertEqual(rc, 1)  # nothing inserted -> failures reported
        self.assertIn("no draft in the reply", shown)
        self.assertNotIn(
            '"""', (self.dir / "pkg" / "core.py").read_text().split("helper()\n\n")[-1]
        )

    def _fix_unconsented(self, confirm=None):
        """Run fix_docs without yes=True, optionally stubbing ui.confirm — the
        stand-in model marks a file if it ever runs, so 'never called' is
        checkable."""
        import contextlib
        import io
        import sys
        from unittest import mock

        self._sparkle()
        script = self.dir / "fake_model.py"
        script.write_text("import pathlib\npathlib.Path('model_ran').write_text('x')\n")
        buf = io.StringIO()
        patch = (
            mock.patch.object(UI, "confirm", return_value=confirm)
            if confirm is not None
            else contextlib.nullcontext()
        )
        with (
            patch,
            contextlib.redirect_stdout(buf),
            contextlib.redirect_stderr(buf),
        ):
            rc = P.fix_docs(self.ctx, "fake", cmd=[sys.executable, str(script)])
        return rc, buf.getvalue()

    def test_fix_docs_refuses_unattended_without_yes(self):
        # captured output = no terminal to ask: --ai must refuse, not spend
        rc, shown = self._fix_unconsented()
        self.assertEqual(rc, 1)
        self.assertIn("--yes", shown)
        self.assertFalse((self.dir / "model_ran").exists())  # model never ran

    def test_fix_docs_declined_is_a_clean_noop(self):
        rc, shown = self._fix_unconsented(confirm=False)
        self.assertEqual(rc, 0)  # saying no is a choice, not an error
        self.assertIn("declined", shown)
        self.assertFalse((self.dir / "model_ran").exists())

    def test_fix_docs_ctrl_c_exits_130_with_accounting(self):
        import contextlib
        import io
        import sys
        from unittest import mock

        self._sparkle()
        script = self.dir / "fake_model.py"
        script.write_text("pass\n")
        buf = io.StringIO()
        with (
            mock.patch.object(P, "_stream", side_effect=KeyboardInterrupt),
            contextlib.redirect_stdout(buf),
            contextlib.redirect_stderr(buf),
        ):
            rc = P.fix_docs(
                self.ctx, "fake", cmd=[sys.executable, str(script)], yes=True
            )
        self.assertEqual(rc, 130)  # conventional SIGINT code, no traceback
        self.assertIn("interrupted", buf.getvalue())
        self.assertIn("git diff", buf.getvalue())  # partial drafts stay reviewable


class TestProseRewrite(RealGraph):
    """`--ai --rewrite`: re-emit every C/C++ doc comment as Doxygen. The batched
    path drafts a `@brief`/`@param` body; documate replaces the existing comment
    block (or seeds an undocumented symbol) with a `/** */` block — the marker
    Doxygen reads, unlike the plain `//` seeding writes."""

    def _crc(self):
        _w(
            self.dir / "src" / "crc.c",
            "#include <stdint.h>\n\n"
            "// old summary\n"
            "int crc32(const uint8_t *buf) {\n"
            "    return 0;\n"
            "}\n\n"
            "int seedless(int x) {\n"
            "    return x;\n"
            "}\n",
        )
        _git(self.dir, "add", "-A")
        self.ctx.graph.index(incremental=True)

    # a scripted stand-in model speaking the batch protocol: one Doxygen `@brief`
    # body per numbered work order (the `{body}` is spliced in per test)
    _MODEL = (
        "import re, sys\n"
        "prompt = sys.stdin.read()\n"
        'assert "Doxygen" in prompt\n'
        'for n, _ in re.findall(r"## Work order (\\d+): `([^`]+)`", prompt):\n'
        '    print(f"<<<doc {n}>>>")\n'
        "__BODY__"
        '    print("<<<end>>>")\n'
    )

    def _rewrite(self, body: str | None = None):
        import contextlib
        import io
        import sys

        if body is None:
            body = (
                '    print("@brief Computes a checksum.")\n'
                '    print("@param buf input bytes")\n'
                '    print("@return the checksum")\n'
            )
        script = self.dir / "fake_model.py"
        script.write_text(self._MODEL.replace("__BODY__", body))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            rc = P.fix_rewrite(
                self.ctx, "fake", cmd=[sys.executable, str(script)], yes=True
            )
        return rc, buf.getvalue()

    def test_rewrite_briefs_scope_is_c_family_only(self):
        self._crc()
        out = self.dir / ".documate" / "briefs"
        index = BR.emit(self.ctx, "HEAD", [], out, rewrite=True)
        syms = {r["symbol"] for r in index}
        self.assertEqual(syms, {"crc32", "seedless"})  # C only
        self.assertNotIn("helper", syms)  # the Python fixtures are out of scope
        self.assertTrue(all(r["kind"] == "rewrite" for r in index))
        # the current doc is quoted so the model improves, not invents
        crc_brief = next(r for r in index if r["symbol"] == "crc32")
        self.assertIn("old summary", (out / crc_brief["brief"]).read_text())

    def test_rewrite_replaces_and_seeds_as_doxygen(self):
        self._crc()
        rc, shown = self._rewrite()
        self.assertEqual(rc, 0)
        src = (self.dir / "src" / "crc.c").read_text()
        self.assertEqual(src.count("/**"), 2)  # both symbols now Doxygen
        self.assertIn(" * @brief Computes a checksum.", src)
        self.assertIn(" */\nint crc32(const uint8_t *buf)", src)  # block above decl
        self.assertNotIn("// old summary", src)  # replaced, not duplicated
        self.assertIn("src/crc.c  crc32", shown)
        self.assertNotIn("in-place repairs", shown)  # batched, not agentic

    def test_rewrite_replaces_existing_block_and_strips_stray_markers(self):
        # the common real input: an existing multi-line /* */ block (old style, so
        # the resume filter keeps it as a candidate), and a model that (against
        # instructions) wraps its reply in /** */ markers
        _w(
            self.dir / "src" / "blk.c",
            "#include <stddef.h>\n\n"
            "/*\n * old brief\n * @param n count\n */\n"
            "size_t tally(int n) {\n    return n;\n}\n",
        )
        _git(self.dir, "add", "-A")
        self.ctx.graph.index(incremental=True)
        rc, _ = self._rewrite(
            '    print("/**")\n'
            '    print(" * @brief New tally brief.")\n'
            '    print(" * @param n the count")\n'
            '    print(" */")\n'
        )
        self.assertEqual(rc, 0)
        src = (self.dir / "src" / "blk.c").read_text()
        self.assertEqual(src.count("/**"), 1)  # no doubled wrapper
        self.assertEqual(src.count("*/"), 1)
        self.assertIn(" * @brief New tally brief.", src)
        self.assertNotIn("old brief", src)  # the whole old block was swapped out
        self.assertIn(" */\nsize_t tally(int n)", src)

    def test_rewrite_no_c_sources_is_a_clean_noop(self):
        rc, shown = self._rewrite()  # setUp built only Python files
        self.assertEqual(rc, 0)
        self.assertIn("no C/C++ symbols", shown)

    def test_rewrite_is_resumable_skips_already_doxygen(self):
        # a converged symbol emits no work order, so re-running a capped rewrite
        # continues through the remainder instead of redoing the same first briefs
        self._crc()
        rc, _ = self._rewrite()
        self.assertEqual(rc, 0)
        out = self.dir / ".documate" / "briefs"
        index = BR.emit(self.ctx, "HEAD", [], out, rewrite=True)
        self.assertEqual(index, [])  # everything is /** @brief */ now — nothing left

    def test_rewrite_repairs_wedged_comment(self):
        # damage from an older insert: a /** */ block wedged between the return
        # type and the name, where Doxygen and the extractor both stop seeing it.
        # The rewrite must delete the wedge and land the fresh block above the
        # whole declaration, replacing the old-style comment sitting up there.
        _w(
            self.dir / "src" / "wedge.c",
            "/* old summary */\n"
            "static int\n"
            "/**\n"
            " * @brief wedged\n"
            " */\n"
            "victim(int n)\n"
            "{\n"
            "    return n;\n"
            "}\n",
        )
        _git(self.dir, "add", "-A")
        self.ctx.graph.index(incremental=True)
        rc, _ = self._rewrite()
        self.assertEqual(rc, 0)
        src = (self.dir / "src" / "wedge.c").read_text()
        self.assertIn(" */\nstatic int\nvictim(int n)", src)  # decl is whole again
        self.assertNotIn("wedged", src)  # the wedge was deleted, not upgraded
        self.assertNotIn("old summary", src)  # the top comment was the one replaced
        self.assertEqual(src.count("/**"), 1)

    def test_rewrite_header_owns_the_contract(self):
        # a doc-less definition whose sibling-header prototype is Doxygen-documented
        # emits nothing (Doxygen merges decl/def); a definition duplicating that
        # contract gets a @brief-only order; an undocumented-header symbol stays a
        # normal full-contract order.
        _w(
            self.dir / "src" / "mod.h",
            "/**\n * @brief Alpha does things.\n * @param v the input\n"
            " * @return the result\n */\nint alpha(int v);\n\n"
            "/**\n * @brief Beta.\n * @param v the input\n */\nint beta(int v);\n",
        )
        _w(
            self.dir / "src" / "mod.c",
            '#include "mod.h"\n\n'
            "int alpha(int v)\n{\n    return v;\n}\n\n"
            "/**\n * @brief Beta local.\n * @param v dup contract\n */\n"
            "int beta(int v)\n{\n    return v;\n}\n\n"
            "// old gamma\n"
            "int gamma(int v)\n{\n    return v;\n}\n",
        )
        _git(self.dir, "add", "-A")
        self.ctx.graph.index(incremental=True)
        out = self.dir / ".documate" / "briefs"
        index = BR.emit(self.ctx, "HEAD", [], out, rewrite=True)
        syms = {r["symbol"] for r in index}
        self.assertNotIn("alpha", syms)  # the prototype already carries the contract
        self.assertIn("beta", syms)
        self.assertIn("gamma", syms)
        beta = next(r for r in index if r["symbol"] == "beta")
        self.assertIn("## Contract", (out / beta["brief"]).read_text())
        gamma = next(r for r in index if r["symbol"] == "gamma")
        self.assertNotIn("## Contract", (out / gamma["brief"]).read_text())


class TestUndo(RealGraph):
    """--ai runs leave a manifest (mode, model, writes, before-images) so the run
    is attributable and reversible without in-file markers; `documate --undo`
    restores exactly the recorded files, refusing any edited since."""

    def _seed(self):
        """Two undocumented symbols in two files, drafted by a scripted model."""
        import contextlib
        import io
        import sys

        _w(
            self.dir / "pkg" / "core.py",
            (self.dir / "pkg" / "core.py").read_text()
            + "\ndef sparkle():\n    return helper()\n",
        )
        _w(
            self.dir / "pkg" / "more.py",
            (self.dir / "pkg" / "more.py").read_text()
            + "\ndef plain():\n    return 0\n",
        )
        self.ctx.graph.index(incremental=True)
        script = self.dir / "fake_model.py"
        script.write_text(
            "import re, sys\n"
            "prompt = sys.stdin.read()\n"
            'for n, _ in re.findall(r"## Work order (\\d+): `([^`]+)`", prompt):\n'
            '    print(f"<<<doc {n}>>>")\n'
            '    print("Drafted.")\n'
            '    print("<<<end>>>")\n'
        )
        self.originals = {
            rel: (self.dir / rel).read_text() for rel in ("pkg/core.py", "pkg/more.py")
        }
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            rc = P.fix_docs(
                self.ctx, "fake", cmd=[sys.executable, str(script)], yes=True
            )
        self.assertEqual(rc, 0)
        return buf.getvalue()

    def test_run_writes_a_manifest(self):
        shown = self._seed()
        self.assertIn("documate --undo", shown)  # the run says how to revert
        data = json.loads((self.dir / ".documate" / "last-run.json").read_text())
        self.assertEqual(data["mode"], "seed")
        self.assertEqual(data["model"], "fake")
        self.assertEqual(set(data["files"]), {"pkg/core.py", "pkg/more.py"})
        wrote = {(w["file"], w["symbol"]) for w in data["writes"]}
        self.assertIn(("pkg/core.py", "sparkle"), wrote)
        self.assertIn(("pkg/more.py", "plain"), wrote)
        # before-images are the true pre-run text
        self.assertEqual(
            data["files"]["pkg/core.py"]["before"], self.originals["pkg/core.py"]
        )

    def test_undo_restores_pre_run_state(self):
        import contextlib
        import io

        self._seed()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            rc = U.undo_last(self.ctx)
        self.assertEqual(rc, 0)
        for rel, text in self.originals.items():
            self.assertEqual((self.dir / rel).read_text(), text)
        self.assertFalse((self.dir / ".documate" / "last-run.json").exists())
        # a second --undo has nothing left and says so
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            self.assertEqual(U.undo_last(self.ctx), 1)

    def test_undo_refuses_files_edited_after_the_run(self):
        import contextlib
        import io

        self._seed()
        edited = (self.dir / "pkg" / "more.py").read_text() + "\n# hand edit\n"
        (self.dir / "pkg" / "more.py").write_text(edited)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            rc = U.undo_last(self.ctx)
        self.assertEqual(rc, 1)  # something was refused
        # the untouched file was restored, the edited one left exactly as edited
        self.assertEqual(
            (self.dir / "pkg" / "core.py").read_text(), self.originals["pkg/core.py"]
        )
        self.assertEqual((self.dir / "pkg" / "more.py").read_text(), edited)
        # the refused file keeps its manifest entry for a later --undo
        data = json.loads((self.dir / ".documate" / "last-run.json").read_text())
        self.assertEqual(set(data["files"]), {"pkg/more.py"})


class TestCReferenceNodes(RealGraph):
    """A C type *reference* (`struct x *p` in a signature, a local, an external
    libc type) must not become a Class node — only the definition, the specifier
    carrying a body, is a documentable symbol. References were counting a type as
    undocumented in every file that used it, making full coverage unreachable."""

    def test_only_definitions_become_class_nodes(self):
        _w(
            self.dir / "src" / "a.h",
            "/**\n * @brief A 2D point.\n */\nstruct point {\n    int x;\n};\n",
        )
        _w(
            self.dir / "src" / "b.c",
            '#include "a.h"\n#include <time.h>\n\nstruct point g;\n\n'
            "int use_point(struct point *p)\n{\n"
            "    struct timespec ts;\n    (void)ts;\n    return p->x;\n}\n",
        )
        _git(self.dir, "add", "-A")
        self.ctx.graph.index(incremental=True)
        classes = [s for s in self.ctx.graph.symbols() if s["kind"] == "Class"]
        where = {(s["name"], self.ctx.rel(s["file"])) for s in classes}
        self.assertIn(("point", "src/a.h"), where)  # the definition, once
        self.assertNotIn(("point", "src/b.c"), where)  # the uses
        self.assertNotIn("timespec", {s["name"] for s in classes})  # not ours
        # the documented definition is what coverage counts
        model = DOCS.build_model(self.ctx)
        page = next(p for p in model.pages if p.rel == "src/a.h")
        self.assertTrue(any(s.name == "point" and s.doc for s in page.symbols))


class TestWorktree(RealGraph):
    """A linked worktree must generate byte-identical pages to the main checkout —
    the page title comes from the common git dir's parent, not the worktree's own
    dirname — so `--check` (freshness) passes there instead of forcing a skip."""

    def test_check_passes_in_a_linked_worktree(self):
        self.assertEqual(DOCS.run(self.ctx), 0)
        _git(self.dir, "add", "-A")
        _git(self.dir, "commit", "-q", "-m", "docs")
        wt = self.dir.parent / (self.dir.name + "_wt")
        _git(self.dir, "worktree", "add", "--detach", str(wt))
        self.addCleanup(__import__("shutil").rmtree, wt, True)
        ctx2 = Context.make(wt)
        ctx2.graph.index()
        model = DOCS.build_model(ctx2)
        self.assertEqual(model.root_name, self.dir.name)  # not the worktree's name
        self.assertEqual(CK.run(ctx2, "HEAD"), 0)


class TestMultiLanguage(unittest.TestCase):
    """documate is language-agnostic — the engine parses Go/C/Swift the same as Python.
    A non-Python smoke proves the diff->symbol->doc chain isn't Python-only."""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="documate_go_")).resolve()
        root = self.dir
        _w(
            root / "main.go",
            "package main\nfunc helper() int { return 1 }\n"
            "func Entry() int { return helper() + helper() }\n",
        )
        _w(
            root / "docs" / "guides" / "entry.md",
            "## Entry\n<!-- documents: sym:Entry -->\nDescribes Entry.\n",
        )
        _git(root, "init", "-q")
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "x")
        self.ctx = Context.make(root)
        self.ctx.graph.index()

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.dir, ignore_errors=True)

    def test_go_symbol_resolves(self):
        r = R.resolve(self.ctx, "sym:Entry")
        self.assertTrue(r.ok)
        self.assertEqual(r.targets[0]["file"], "main.go")

    def test_go_diff_drifts_the_doc(self):
        (self.dir / "main.go").write_text(
            "package main\nfunc helper() int { return 2 }\n"
            "func Entry() int { return helper() }\n"
        )
        direct, _, _, _ = D.find_drift(self.ctx, "HEAD", 0, 500)
        self.assertTrue(any(d["module"] == "docs/guides/entry.md" for d in direct))

    def test_go_package_comment_is_the_page_lead(self):
        # non-Python module prose: the file-top comment block leads the page, and the
        # symbol's own adjacent comment still lands on the symbol — no double-claim.
        _w(
            self.dir / "main.go",
            "// Package main wires the demo together.\n"
            "package main\n\n"
            "// Entry runs the whole show.\n"
            "func Entry() int { return 1 }\n",
        )
        _git(self.dir, "add", "-A")
        self.ctx.graph.index()
        model = DOCS.build_model(self.ctx)
        page = DOCS.render(model)["architecture/main.go.md"]
        self.assertIn("Package main wires the demo together.", page)
        self.assertIn("Entry runs the whole show.", page)
        main = next(p for p in model.pages if p.rel == "main.go")
        self.assertEqual(main.summary, "Package main wires the demo together.")

    def test_ts_import_edge_draws_the_dependency(self):
        # the engine resolves JS/TS path imports to file->file edges; the model turns
        # them into depends_on/used_by so a polyglot monorepo gets a real map.
        _w(
            self.dir / "web" / "util.ts",
            "/** Format a price in cents. */\n"
            "export function fmt(c: number): string { return String(c); }\n",
        )
        _w(
            self.dir / "web" / "cart.ts",
            'import { fmt } from "./util";\n\n'
            "/** Total, formatted. */\n"
            "export function total(): string { return fmt(0); }\n",
        )
        _git(self.dir, "add", "-A")
        self.ctx.graph.index()
        model = DOCS.build_model(self.ctx)
        self.assertIn(("web/cart.ts", "web/util.ts"), model.module_edges)
        cart = next(p for p in model.pages if p.rel == "web/cart.ts")
        self.assertIn("web/util.ts", cart.depends_on)
        util = next(p for p in model.pages if p.rel == "web/util.ts")
        self.assertIn("web/cart.ts", util.used_by)

    def test_ts_barrel_reexport_wires_the_graph(self):
        # `export ... from` is how a TS library's index.ts wires its public API.
        # The barrel defines no symbols but must still be a module in the map —
        # and the door into the package (zod dogfood: dropping barrels left the
        # real entry points looking like unimported leaves).
        _w(
            self.dir / "lib" / "impl.ts",
            "/** Add two ints. */\n"
            "export function add(a: number, b: number): number { return a + b; }\n",
        )
        _w(
            self.dir / "lib" / "index.ts",
            'export { add } from "./impl.js";\n'  # ESM style: .js names the .ts
            'export * as util from "./impl.js";\n',
        )
        _git(self.dir, "add", "-A")
        self.ctx.graph.index()
        model = DOCS.build_model(self.ctx)
        self.assertIn(("lib/index.ts", "lib/impl.ts"), model.module_edges)
        barrel = next(p for p in model.pages if p.rel == "lib/index.ts")
        self.assertIn("lib/impl.ts", barrel.depends_on)
        impl = next(p for p in model.pages if p.rel == "lib/impl.ts")
        self.assertIn("lib/index.ts", impl.used_by)
        # `export function` is an export_statement too — it must still fall
        # through to declaration extraction, not vanish into the import branch.
        self.assertTrue(any(s.name == "add" for s in impl.symbols))
        _, entries = DOCS._tour(model.pages, model.module_edges)
        self.assertIn("lib/index.ts", entries)  # the barrel is the door

    def test_bench_and_colocated_tests_are_not_source(self):
        # benchmarks measure the code, .test./.spec. files exercise it — neither
        # is public surface; left in, bench scripts become the doors (zod dogfood).
        _w(
            self.dir / "bench" / "speed.ts",
            "export function bench(): number { return 1; }\n",
        )
        _w(
            self.dir / "pkg" / "api.test.ts",
            "export function t(): number { return 1; }\n",
        )
        _git(self.dir, "add", "-A")
        self.ctx.graph.index()
        rels = {p.rel for p in DOCS.build_model(self.ctx).pages}
        self.assertNotIn("bench/speed.ts", rels)
        self.assertNotIn("pkg/api.test.ts", rels)

    def test_go_doc_comment_is_extracted(self):
        # the doc lives in a // comment ABOVE the func (Go/C/Rust convention), not inside it
        # like Python. The generator must harvest it, so coverage counts a non-Python doc.
        _w(
            self.dir / "main.go",
            "package main\n"
            "// Entry runs the whole show.\n"
            "func Entry() int { return helper() }\n"
            "func helper() int { return 1 }\n",
        )
        _git(self.dir, "add", "-A")
        self.ctx.graph.index()
        model = DOCS.build_model(self.ctx)
        page = DOCS.render(model)["architecture/main.go.md"]
        self.assertIn("Entry runs the whole show.", page)
        self.assertGreaterEqual(model.coverage["documented"], 1)

    def test_symbol_free_doc_go_gets_a_page(self):
        # Go's convention: the package doc lives in a doc.go with no symbols in it.
        # A symbol-free file with module prose still deserves a page.
        _w(self.dir / "doc.go", "// Package main is the demo package.\npackage main\n")
        _git(self.dir, "add", "-A")
        self.ctx.graph.index()
        page = DOCS.render(DOCS.build_model(self.ctx))["architecture/doc.go.md"]
        self.assertIn("Package main is the demo package.", page)

    def test_c_typedef_is_documented_once(self):
        # `typedef struct x y;` yields two graph nodes on one line (alias + nested
        # tag); the page must not document the same thing twice.
        _w(
            self.dir / "ring.h",
            "/** Opaque ring handle. */\ntypedef struct ring ring_t;\n",
        )
        _git(self.dir, "add", "-A")
        self.ctx.graph.index()
        page = DOCS.render(DOCS.build_model(self.ctx))["architecture/ring.h.md"]
        self.assertEqual(page.count("Opaque ring handle."), 1)


class TestGoEdges(unittest.TestCase):
    """Go's unresolved edges, re-qualified at our layer: the engine stores a Go
    cross-file call as a bare name and an import as a package path, so `_go_edges`
    must recover the dependency map — and refuse to invent one (a method call on
    some other type's value shares its bare name with package functions)."""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="documate_goedges_")).resolve()
        root = self.dir
        _w(
            root / "main.go",
            "package main\n\n"
            'import (\n\t"fmt"\n\n\t"example.com/edges/krypto"\n'
            '\t"example.com/edges/protector"\n'
            '\t"example.com/edges/server"\n\t"example.com/edges/session"\n'
            '\t"example.com/edges/store"\n)\n\n'
            "func main() {\n"
            "\tprotector.Protect()\n"
            "\tkrypto.Derive()\n"
            "\tfmt.Println(krypto.Encrypt(nil))\n"
            "\tserver.Read()\n"
            "\tdb.Open()\n"
            "\t_ = session.Pair{}\n"
            "}\n",
        )
        _w(
            root / "krypto" / "krypto.go",
            "package krypto\n\n"
            "// Derive derives keys.\nfunc Derive() {\n\thelper()\n}\n\n"
            "// Encrypt seals data.\nfunc Encrypt(b []byte) []byte { return b }\n\n"
            "func helper() {}\n",
        )
        _w(
            root / "server" / "server.go",
            "package server\n\n"
            'import "example.com/edges/krypto"\n\n'
            "// Read reads a record.\nfunc Read() {\n\tkrypto.Derive()\n}\n\n"
            "// Println collides with fmt's on purpose.\nfunc Println() {}\n",
        )
        _w(
            root / "session" / "session.go",
            "package session\n\n// Pair is a key pair.\ntype Pair struct{}\n",
        )
        # dir "store", declared `package db`: the call site can only read db.Open(
        _w(
            root / "store" / "store.go",
            "package db\n\n// Open opens the store.\nfunc Open() {}\n",
        )
        # aliased import: the call site reads kk.Encrypt(, never krypto.Encrypt(
        _w(
            root / "report" / "report.go",
            "package report\n\n"
            'import kk "example.com/edges/krypto"\n\n'
            "// Report prints a digest.\nfunc Report() { kk.Encrypt(nil) }\n",
        )
        # build-tag twins: one Protect per platform, same package dir
        _w(
            root / "protector" / "protector.go",
            "package protector\n\n"
            "// Protect locks the process down.\nfunc Protect() {}\n",
        )
        _w(
            root / "protector" / "protector_openbsd.go",
            "//go:build openbsd\n\npackage protector\n\nfunc Protect() {}\n",
        )
        # stringer-style generated file: nothing imports it because nobody reads it
        _w(
            root / "krypto" / "keytype_string.go",
            '// Code generated by "stringer -type=keyType"; DO NOT EDIT.\n\n'
            "package krypto\n\nfunc keyString() { helper() }\n",
        )
        _w(
            root / "server" / "server_test.go",
            'package server\n\nimport "testing"\n\n'
            "func plainHelper() {}\n"
            "func TestRead(t *testing.T) { Read() }\n",
        )
        _git(root, "init", "-q")
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "x")
        self.ctx = Context.make(root)
        self.ctx.graph.index()
        self.model = DOCS.build_model(self.ctx)

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.dir, ignore_errors=True)

    def _page(self, rel):
        return next(p for p in self.model.pages if p.rel == rel)

    def test_cross_package_call_draws_edge_xref_and_flow(self):
        main = self._page("main.go")
        self.assertEqual(main.depends_on.get("krypto/krypto.go"), ["Derive", "Encrypt"])
        derive = next(
            s for s in self._page("krypto/krypto.go").symbols if s.name == "Derive"
        )
        self.assertIn("main", derive.callers)
        self.assertIn(("main", "Derive"), main.flow)
        # unresolved stdlib/builtin targets stay out of the diagram
        self.assertNotIn(("main", "Println"), main.flow)

    def test_method_call_cannot_fabricate_a_symbol_edge(self):
        # main.go calls fmt.Println — bare target "Println" — and server exports a
        # Println too; without the literal `server.Println(` in main.go, no edge.
        prints = next(
            s for s in self._page("server/server.go").symbols if s.name == "Println"
        )
        self.assertEqual(prints.callers, [])
        self.assertNotIn(
            "Println", self._page("main.go").depends_on.get("server/server.go", [])
        )

    def test_types_only_import_still_draws_the_module_edge(self):
        # session is imported for a struct, never called: the single-file package
        # import alone is unambiguous, so the dependency map keeps the edge.
        self.assertIn("session/session.go", self._page("main.go").depends_on)
        self.assertEqual(self._page("main.go").depends_on["session/session.go"], [])

    def test_go_test_files_are_not_subsystems(self):
        rels = {p.rel for p in self.model.pages}
        self.assertNotIn("server/server_test.go", rels)
        # nor do their helpers count against coverage
        rendered = "".join(DOCS.render(self.model).values())
        self.assertNotIn("plainHelper", rendered)

    def test_declared_package_name_beats_directory_name(self):
        # Go never promises the package name matches its directory (fzf's src/
        # declares `package fzf`) — verification must use the declared name.
        self.assertEqual(
            self._page("main.go").depends_on.get("store/store.go"), ["Open"]
        )

    def test_aliased_import_verifies_the_call(self):
        self.assertEqual(
            self._page("report/report.go").depends_on.get("krypto/krypto.go"),
            ["Encrypt"],
        )

    def test_build_tag_twins_both_keep_the_edge(self):
        # two owners in one package dir are platform variants of one implementation,
        # not ambiguity — both keep the edge (owners in *different* dirs still drop)
        deps = self._page("main.go").depends_on
        self.assertEqual(deps.get("protector/protector.go"), ["Protect"])
        self.assertEqual(deps.get("protector/protector_openbsd.go"), ["Protect"])

    def test_generated_file_is_skip_tier(self):
        # banner-carrying source is nobody's reading: no page, no coverage debt
        self.assertNotIn("krypto/keytype_string.go", {p.rel for p in self.model.pages})
        _, entries = DOCS._tour(self.model.pages, self.model.module_edges)
        self.assertNotIn("krypto/keytype_string.go", entries)
        self.assertIn("main.go", entries)

    def test_go_test_call_becomes_evidence_on_the_symbol(self):
        # TestRead calls Read() -> the engine's TESTED_BY edge (bare production name,
        # resolved because exactly one owned symbol bears it) lands as evidence.
        read = next(
            s for s in self._page("server/server.go").symbols if s.name == "Read"
        )
        self.assertEqual(read.tested, ["read"])


class TestEvidence(Base):
    """Mined evidence for the no-docstring repo: what a symbol's tests assert (off
    the engine's TESTED_BY edges) and why a module exists (off the subject of the
    commit that created it) — labeled as evidence, shown only where docstrings are
    missing, never invented."""

    def _edge(self, prod: str, test_q: str) -> None:
        con = sqlite3.connect(self.dir / ".documate" / "graph.db")
        con.execute("INSERT INTO edges VALUES('TESTED_BY',?,?)", (prod, test_q))
        con.commit()
        con.close()

    def test_test_evidence_attaches_and_renders_in_the_fold(self):
        tst = str(self.dir / "tests" / "test_key.c")
        self._edge("verify_key", f"{tst}::test_key_verifies_smartcard")
        model = DOCS.build_model(self.ctx)
        key = next(p for p in model.pages if p.rel == "src/key.c")
        sym = next(s for s in key.symbols if s.name == "verify_key")
        self.assertEqual(sym.tested, ["key verifies smartcard"])
        md = DOCS.render(model)[f"architecture/{key.slug}.md"]
        self.assertIn("- `verify_key` — tested: key verifies smartcard", md)

    def test_ambiguous_test_target_attaches_nowhere(self):
        # two owned symbols named verify_key: a bare TESTED_BY target must not guess.
        con = sqlite3.connect(self.dir / ".documate" / "graph.db")
        misc = str(self.dir / "src" / "misc.c")
        con.execute(
            "INSERT INTO nodes(name,kind,qualified_name,file_path,line_start) "
            "VALUES('verify_key','Function',?,?,9)",
            (f"{misc}::verify_key", misc),
        )
        con.commit()
        con.close()
        self._edge("verify_key", f"{self.dir}/tests/test_key.c::test_something")
        model = DOCS.build_model(self.ctx)
        for p in model.pages:
            for s in p.symbols:
                self.assertEqual(s.tested, [], f"{p.rel}::{s.name}")

    def test_origin_fills_the_docstringless_gap(self):
        # Base's C modules have no docstrings; its whole tree landed in one commit
        # whose subject is "fix" — the mined line every surface shows, labeled.
        model = DOCS.build_model(self.ctx)
        key = next(p for p in model.pages if p.rel == "src/key.c")
        self.assertIsNone(key.module_doc)
        self.assertEqual(key.origin, "fix")
        out = DOCS.render(model)
        self.assertIn('*first commit: "fix"*', out["README.md"])
        self.assertIn(
            '*No module docstring. First commit: "fix".*',
            out[f"architecture/{key.slug}.md"],
        )
        self.assertIn(
            '*No module docstring. First commit: "fix".*', out["ARCHITECTURE.md"]
        )

    def test_no_git_history_degrades_to_no_origin(self):
        import shutil

        shutil.rmtree(self.dir / ".git")
        model = DOCS.build_model(self.ctx)
        self.assertTrue(all(p.origin is None for p in model.pages))
        self.assertNotIn("First commit", "".join(DOCS.render(model).values()))

    def test_bulk_add_is_not_an_origin(self):
        # a commit adding a pile of modules at once (a tree move, a vendor import)
        # has a subject that describes none of them — skip it, like hotspots do
        bulk = {f"bulk/f{i}.c" for i in range(DOCS._BULK_CAP + 1)}
        for r in sorted(bulk):
            _w(self.dir / r, "int x;\n")
        _git(self.dir, "add", "-A")
        _git(self.dir, "commit", "-q", "-m", "import vendor tree")
        got = DOCS._origins(self.ctx, bulk | {"src/key.c"})
        self.assertEqual(got, {"src/key.c": "fix"})


class TestIncludeEdges(Base):
    """C-family includes arrive in the graph exactly as written in the source —
    `compile.h` bare, `wolfssl/wolfcrypt/aes.h` path-form — and resolve the way a
    compiler's include search would: next to the includer, from the repo root, by
    unique suffix or name. System headers own nothing and drop out; a vendor
    snapshot can't capture includers outside its own top-level tree."""

    def _include(self, src_rel: str, target: str) -> None:
        con = sqlite3.connect(self.dir / ".documate" / "graph.db")
        con.execute(
            "INSERT INTO edges VALUES('IMPORTS_FROM',?,?)",
            (str(self.dir / src_rel), target),
        )
        con.commit()
        con.close()

    def _module(self, rel: str) -> None:
        """A minimal owned module at `rel` — file on disk plus its graph node."""
        p = self.dir / rel
        _w(p, "int fn(void){return 0;}\n")
        con = sqlite3.connect(self.dir / ".documate" / "graph.db")
        con.execute(
            "INSERT INTO nodes(name,kind,qualified_name,file_path,line_start) "
            "VALUES('fn','Function',?,?,1)",
            (f"{p}::fn", str(p)),
        )
        con.commit()
        con.close()

    def _edges(self):
        return DOCS.build_model(self.ctx).module_edges

    def test_bare_include_resolves_onto_the_only_owner(self):
        self._include("src/app.c", "key.c")
        self._include("src/app.c", "stdio.h")  # system header: no owner, no edge
        model = DOCS.build_model(self.ctx)
        self.assertIn(("src/app.c", "src/key.c"), model.module_edges)
        app = next(p for p in model.pages if p.rel == "src/app.c")
        self.assertIn("src/key.c", app.depends_on)
        md = DOCS.render(model)["architecture/src.app.c.md"]
        self.assertIn("**depends on** [`src/key.c`](src.key.c.md)", md)

    def test_diagram_collapses_a_c_h_pair_without_self_loops(self):
        # bytecode.c -> bytecode.h is one node on the overview diagram: no
        # self-loop, and its outgoing edges dedup
        self.assertEqual(
            DOCS._stem_edges(
                [("src/a.c", "src/a.h"), ("src/a.c", "src/b.h"), ("src/a.h", "src/b.h")]
            ),
            [("a", "b")],
        )

    def test_sibling_beats_repo_wide_ambiguity(self):
        # two key.c exist, but `#include "key.c"` from src/ means the sibling —
        # that's exactly where the compiler looks first
        self._module("lib/key.c")
        self._include("src/app.c", "key.c")
        edges = self._edges()
        self.assertIn(("src/app.c", "src/key.c"), edges)
        self.assertNotIn(("src/app.c", "lib/key.c"), edges)

    def test_ambiguous_bare_include_without_a_sibling_resolves_nowhere(self):
        self._module("lib/key.c")
        self._module("tools/gen.c")  # third tree: no sibling, no unique owner
        self._include("tools/gen.c", "key.c")
        edges = self._edges()
        self.assertNotIn(("tools/gen.c", "src/key.c"), edges)
        self.assertNotIn(("tools/gen.c", "lib/key.c"), edges)

    def test_path_include_resolves_from_the_repo_root(self):
        self._module("wolfssl/aes.h")
        self._include("src/app.c", "wolfssl/aes.h")
        self.assertIn(("src/app.c", "wolfssl/aes.h"), self._edges())

    def test_path_include_resolves_relative_to_the_includer(self):
        self._module("src/port/ti.h")
        self._include("src/app.c", "port/ti.h")
        self.assertIn(("src/app.c", "src/port/ti.h"), self._edges())

    def test_path_include_resolves_by_unique_suffix(self):
        # the -Iinclude layout: `#include "crypt/foo.h"` finds include/crypt/foo.h
        self._module("include/crypt/foo.h")
        self._include("src/app.c", "crypt/foo.h")
        self.assertIn(("src/app.c", "include/crypt/foo.h"), self._edges())

    def test_ambiguous_path_suffix_resolves_nowhere(self):
        self._module("include/crypt/foo.h")
        self._module("third/crypt/foo.h")
        self._include("src/app.c", "crypt/foo.h")
        edges = self._edges()
        self.assertNotIn(("src/app.c", "include/crypt/foo.h"), edges)
        self.assertNotIn(("src/app.c", "third/crypt/foo.h"), edges)

    def test_vendor_snapshot_cannot_capture_the_tree(self):
        # wolfssl's poison: the repo's only tracked config.h lives in an IDE
        # vendor corner while 140 includers mean the build-generated one
        self._module("IDE/mdk/config.h")
        self._module("IDE/other/x.c")
        self._include("src/app.c", "config.h")
        self._include("IDE/other/x.c", "config.h")
        edges = self._edges()
        self.assertNotIn(("src/app.c", "IDE/mdk/config.h"), edges)
        # an includer in the same top-level tree keeps the edge
        self.assertIn(("IDE/other/x.c", "IDE/mdk/config.h"), edges)

    def test_bare_include_unique_below_the_includer_wins(self):
        # ESP-IDF layout: main/main.c includes "conf.h" living in main/include/,
        # while another conf.h exists elsewhere in the repo
        self._module("main/main.c")
        self._module("main/include/conf.h")
        self._module("other/conf.h")
        self._include("main/main.c", "conf.h")
        edges = self._edges()
        self.assertIn(("main/main.c", "main/include/conf.h"), edges)
        self.assertNotIn(("main/main.c", "other/conf.h"), edges)

    def test_root_includer_sees_the_whole_repo(self):
        self._module("main.c")
        self._module("util/util.h")
        self._include("main.c", "util.h")
        self.assertIn(("main.c", "util/util.h"), self._edges())


class TestTourDoors(unittest.TestCase):
    """Doors rank by reach: in a repo full of leaf example programs, "Start here"
    must point at the door that opens the codebase, not the first alphabetically."""

    def test_doors_rank_by_reach_not_alphabet(self):
        def pg(r):
            return DOCS.Page(
                rel=r,
                slug=r,
                module_doc=None,
                exposes=[],
                depends_on={},
                used_by=[],
                symbols=[],
                flow=[],
            )

        pages = [pg(r) for r in ("aaa.c", "big.c", "lib1.c", "lib2.c", "x.c")]
        edges = [
            ("aaa.c", "x.c"),
            ("big.c", "lib1.c"),
            ("lib1.c", "lib2.c"),
        ]
        order, entries = DOCS._tour(pages, edges)
        self.assertEqual(entries, ["big.c", "aaa.c"])
        self.assertEqual(order[0], "big.c")  # the reading order starts at the big door


class TestMermaidLines(unittest.TestCase):
    """Mermaid node ids must be parse-safe: `(`/`[` open shape syntax, so a
    Next.js route dir (`app/(doc)/[[...slug]]`) used verbatim kills the whole
    overview diagram (zod dogfood — verified against the real mermaid parser)."""

    def test_plain_ids_pass_through_with_no_decls(self):
        self.assertEqual(
            DOCS._mermaid_lines([("src.docs", "src.core")]),
            ["  src.docs --> src.core"],
        )

    def test_route_dir_labels_get_safe_ids(self):
        lines = DOCS._mermaid_lines([("app.(doc).[[...slug]]", "app.components")])
        self.assertIn("  app._doc_.__...slug__ --> app.components", lines)
        self.assertIn('  app._doc_.__...slug__["app.(doc).[[...slug]]"]', lines)

    def test_distinct_labels_never_share_an_id(self):
        lines = DOCS._mermaid_lines([("a(b)", "a[b]")])  # both sanitize to a_b_
        self.assertIn("  a_b_ --> a_b__2", lines)
        self.assertIn('  a_b_["a(b)"]', lines)
        self.assertIn('  a_b__2["a[b]"]', lines)


class TestHotspots(Base):
    """Churn × co-change mined from `git log`, pinned to one commit so the numbers
    are reproducible: `docs` mines at HEAD and prints the pin, `check` re-mines at
    the printed pin — history growing under unchanged docs is never staleness."""

    def _commit(self, *rels: str) -> None:
        for rel in rels:
            self._touch(rel)
        _git(self.dir, "add", "-A")
        _git(self.dir, "commit", "-q", "-m", "work")

    def _model(self):
        return DOCS.build_model(self.ctx, hot_rev=DOCS._head_rev(self.ctx))

    def test_hot_modules_and_hidden_coupling_render_on_the_overview(self):
        for _ in range(3):
            self._commit("src/key.c", "src/app.c")
        model = self._model()
        hs = model.hotspots
        # 4 commits each (setUp's + 3); misc.c has 1 — below the hot bar
        self.assertEqual(hs.hot, [("src/app.c", 4), ("src/key.c", 4)])
        self.assertEqual(hs.coupled, [("src/app.c", "src/key.c", 4)])
        md = DOCS.render(model)["README.md"]
        self.assertIn("## Hotspots", md)
        self.assertIn(f"*Mined from git history as of `{hs.rev}`.*", md)
        self.assertIn("[`src/app.c`](architecture/src.app.c.md) (4 commits)", md)
        self.assertIn(
            "↔ [`src/key.c`](architecture/src.key.c.md) (4 shared commits)", md
        )

    def test_an_import_edge_makes_co_change_expected_not_coupling(self):
        con = sqlite3.connect(self.dir / ".documate" / "graph.db")
        app, key = (str(self.dir / "src" / f) for f in ("app.c", "key.c"))
        con.execute("INSERT INTO edges VALUES('IMPORTS_FROM',?,?)", (app, key))
        con.commit()
        con.close()
        for _ in range(3):
            self._commit("src/key.c", "src/app.c")
        hs = self._model().hotspots
        self.assertEqual(hs.coupled, [])
        self.assertEqual(hs.hot[0], ("src/app.c", 4))

    def test_pin_keeps_freshness_green_while_history_grows(self):
        for _ in range(3):
            self._commit("src/key.c", "src/app.c")
        self._fresh_docs()  # mined at this HEAD, pin printed, all committed
        self._commit("src/key.c", "src/app.c")  # history grows under the docs
        ddir = self.ctx.config.docs_dir
        pin = DOCS.pinned_rev(ddir)
        self.assertIsNotNone(pin)
        self.assertEqual(CK.run(self.ctx, base="HEAD"), 0)
        # mining at the new HEAD would have moved the counts — the pin is what
        # keeps the committed page a pure function of itself
        head = DOCS.render(self._model())["README.md"]
        self.assertNotEqual((ddir / "README.md").read_text(), head)

    def test_regen_after_a_churn_neutral_commit_keeps_the_pin(self):
        for _ in range(3):
            self._commit("src/key.c", "src/app.c")
        self._fresh_docs()  # README mined + pinned at this HEAD, committed
        ddir = self.ctx.config.docs_dir
        pin = DOCS.pinned_rev(ddir)
        readme = (ddir / "README.md").read_text()
        # HEAD moves but no file's churn changes — the treadmill trigger
        _git(self.dir, "commit", "-q", "--allow-empty", "-m", "empty")
        self.assertEqual(DOCS.run(self.ctx), 0)
        # the pin didn't chase HEAD: the page is byte-identical, so committing it
        # can't re-trigger the regeneration next time (no treadmill)
        self.assertEqual(DOCS.pinned_rev(ddir), pin)
        self.assertEqual((ddir / "README.md").read_text(), readme)

    def test_an_orphaned_pin_self_heals_to_a_reachable_rev(self):
        for _ in range(3):
            self._commit("src/key.c", "src/app.c")
        self._fresh_docs()
        ddir = self.ctx.config.docs_dir
        readme = ddir / "README.md"
        # simulate an amend/rebase orphaning the pinned commit: point it at a sha
        # that doesn't resolve. A churn-neutral regen must NOT keep it.
        text = DOCS._PIN_RE.sub(
            "*Mined from git history as of `deadbeef`.*", readme.read_text()
        )
        readme.write_text(text)
        _git(self.dir, "commit", "-q", "--allow-empty", "-am", "orphan the pin")
        self.assertEqual(DOCS.run(self.ctx), 0)
        healed = DOCS.pinned_rev(ddir)
        self.assertNotEqual(healed, "deadbeef")
        self.assertTrue(DOCS._rev_exists(self.ctx, healed))

    def test_no_rev_means_no_section(self):
        model = DOCS.build_model(self.ctx)
        self.assertIsNone(model.hotspots)
        self.assertNotIn("## Hotspots", DOCS.render(model)["README.md"])
        import shutil

        shutil.rmtree(self.dir / ".git")
        self.assertIsNone(DOCS._head_rev(self.ctx))


class TestClobberGuard(Base):
    """documate never overwrites or deletes a file it didn't stamp — a repo whose
    docs/ predates documate gets a refusal (nothing written), never data loss."""

    def test_existing_unstamped_docs_refuse_to_be_overwritten(self):
        import contextlib
        import io

        readme = self.dir / "docs" / "README.md"
        _w(readme, "# their docs index\n")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(DOCS.run(self.ctx), 1)
        self.assertEqual(readme.read_text(), "# their docs index\n")
        self.assertFalse((self.dir / "docs" / "ARCHITECTURE.md").exists())
        self.assertIn("weren't generated by documate", err.getvalue())
        self.assertIn("docs/README.md", err.getvalue())

    def test_legacy_stamped_page_is_still_ours(self):
        # pages stamped by an older release (old wording) overwrite, not refuse
        from documate.core import STAMPS

        readme = self.dir / "docs" / "README.md"
        _w(readme, STAMPS[-1] + "\nold page\n")
        self.assertEqual(DOCS.run(self.ctx), 0)
        text = readme.read_text()
        self.assertTrue(text.startswith(GENERATED_STAMP))
        self.assertNotIn("old page", text)

    def test_interactive_rescue_asks_for_a_new_docs_dir_and_continues(self):
        # a terminal turns the refusal into a prompt: a bad answer is
        # re-asked, a good one is persisted to config and the run continues
        import contextlib
        import io
        from unittest import mock

        readme = self.dir / "docs" / "README.md"
        _w(readme, "# their docs index\n")
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(DOCS.ui, "ask", side_effect=["../outside", "docs/code"]),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            self.assertEqual(DOCS.run(self.ctx), 0)
        self.assertEqual(readme.read_text(), "# their docs index\n")
        self.assertTrue((self.dir / "docs" / "code" / "README.md").exists())
        self.assertIn("outside the repo", out.getvalue())
        cfg = json.loads((self.dir / "documate.config.json").read_text())
        self.assertEqual(cfg["docs_dir"], "docs/code")

    def test_rescue_declined_keeps_the_refusal(self):
        import contextlib
        import io
        from unittest import mock

        _w(self.dir / "docs" / "README.md", "# their docs index\n")
        err = io.StringIO()
        with (
            mock.patch.object(DOCS.ui, "ask", return_value=None),
            contextlib.redirect_stderr(err),
        ):
            self.assertEqual(DOCS.run(self.ctx), 1)
        self.assertIn("weren't generated by documate", err.getvalue())
        self.assertFalse((self.dir / "documate.config.json").exists())

    def test_prune_leaves_their_files_under_architecture_alone(self):
        self.assertEqual(DOCS.run(self.ctx), 0)
        theirs = self.dir / "docs" / "architecture" / "design-notes.md"
        _w(theirs, "# hand-written\n")
        ours = self.dir / "docs" / "architecture" / "src.gone.c.md"
        _w(ours, DOCS._STAMP + "\n# `src/gone.c`\n")
        self.assertEqual(DOCS.run(self.ctx), 0)
        self.assertTrue(theirs.exists())  # not ours to delete
        self.assertFalse(ours.exists())  # stamped orphan: pruned as before

    def test_check_does_not_flag_their_files_as_orphans(self):
        self._fresh_docs()
        _w(self.dir / "docs" / "architecture" / "design-notes.md", "# hand-written\n")
        _git(self.dir, "add", "-A")
        _git(self.dir, "commit", "-q", "-m", "their notes")
        self.assertEqual(CK.run(self.ctx, base="HEAD"), 0)


class TestSelfIgnore(Base):
    """The graph directory ignores itself (`.gitignore` = `*`) so the db never
    lands in a commit — but only when the directory is documate's alone."""

    def test_graph_dir_gets_a_self_ignoring_gitignore(self):
        self.ctx.graph._self_ignore()
        gi = self.dir / ".documate" / ".gitignore"
        self.assertEqual(gi.read_text(), "*\n")

    def test_a_dir_with_user_files_is_left_alone(self):
        _w(self.dir / ".documate" / "notes.txt", "mine\n")
        self.ctx.graph._self_ignore()
        self.assertFalse((self.dir / ".documate" / ".gitignore").exists())

    def test_an_existing_gitignore_is_not_rewritten(self):
        gi = self.dir / ".documate" / ".gitignore"
        _w(gi, "graph.db\n")
        self.ctx.graph._self_ignore()
        self.assertEqual(gi.read_text(), "graph.db\n")


class TestDocMentions(Base):
    """The repo's existing documentation is linked from the generated pages, not
    ignored: a tracked doc file naming a module by path lands on that module's
    page as "discussed in"."""

    def test_a_doc_naming_a_module_lands_on_its_page(self):
        _w(self.dir / "docs" / "design.md", "The key check lives in src/key.c.\n")
        _w(self.dir / "NOTES.md", "See key.c for the verifier.\n")  # bare, unique
        _git(self.dir, "add", "-A")
        model = DOCS.build_model(self.ctx)
        page = next(p for p in model.pages if p.rel == "src/key.c")
        self.assertEqual(page.mentions, ["NOTES.md", "docs/design.md"])
        md = DOCS.render(model)["architecture/src.key.c.md"]
        self.assertIn(
            "**discussed in** [`NOTES.md`](../../NOTES.md), "
            "[`docs/design.md`](../design.md)",
            md,
        )

    def test_stamped_untracked_and_longer_paths_never_count(self):
        _w(self.dir / "gen.md", GENERATED_STAMP + "\nsrc/key.c everywhere\n")
        _w(self.dir / "vendored.md", "copied from vendor/src/key.c upstream\n")
        _git(self.dir, "add", "-A")
        _w(self.dir / "untracked.md", "src/key.c\n")
        model = DOCS.build_model(self.ctx)
        page = next(p for p in model.pages if p.rel == "src/key.c")
        self.assertEqual(page.mentions, [])

    def test_ambiguous_bare_filename_needs_the_full_path(self):
        _w(self.dir / "doc.md", "key.c does the check; src/key.c is the real one\n")
        _git(self.dir, "add", "-A")
        got = DOCS._doc_mentions(self.ctx, {"src/key.c", "lib/key.c"})
        self.assertEqual(got, {"src/key.c": ["doc.md"]})  # bare `key.c` claimed nothing


class TestPyEvidence(RealGraph):
    """The Python path end-to-end through the real engine: a test in tests/ calling a
    production function yields a TESTED_BY edge whose bare target resolves onto the
    owned symbol."""

    def setUp(self) -> None:
        super().setUp()
        _w(
            self.dir / "tests" / "test_core.py",
            "from pkg.core import entry\n\n"
            "def test_entry_doubles_the_helper():\n    assert entry() == 2\n",
        )
        _git(self.dir, "add", "-A")
        self.ctx.graph.index()

    def test_python_test_call_becomes_evidence(self):
        model = DOCS.build_model(self.ctx)
        core = next(p for p in model.pages if p.rel.endswith("core.py"))
        entry = next(s for s in core.symbols if s.name == "entry")
        self.assertEqual(entry.tested, ["entry doubles the helper"])


class TestDocExtract(unittest.TestCase):
    """The non-Python harvester is pure string work — the graph gives the symbol's line,
    this reads the comment above it. No engine, no tmp repo, just lines in / prose out."""

    def _doc(self, src: str, decl_line_1indexed: int):
        return EX.doc_above(src.splitlines(), decl_line_1indexed - 1)

    def test_line_comment_run(self):
        self.assertEqual(
            self._doc("// does a thing\n// over two lines\nfunc f() {}", 3),
            "does a thing\nover two lines",
        )

    def test_triple_slash_rustdoc(self):
        self.assertEqual(self._doc("/// the answer\nfn f() {}", 2), "the answer")

    def test_block_javadoc(self):
        src = "/**\n * Greets.\n * @param n name\n */\nvoid g(String n) {}"
        self.assertEqual(self._doc(src, 5), "Greets.\n@param n name")

    def test_single_line_block(self):
        self.assertEqual(self._doc("/** quick. */\nint h() {}", 2), "quick.")

    def test_skips_rust_attribute_between_doc_and_fn(self):
        self.assertEqual(
            self._doc("/// inline add\n#[inline]\nfn add() {}", 3), "inline add"
        )

    def test_skips_java_annotation_between_doc_and_method(self):
        self.assertEqual(
            self._doc("/** Overridden. */\n@Override\npublic void m() {}", 3),
            "Overridden.",
        )

    def test_no_comment_is_none(self):
        self.assertIsNone(self._doc("func bare() {}", 1))

    def test_blank_gap_breaks_the_claim(self):
        # blank-separated comment = the file's lead prose, not the symbol's doc
        self.assertIsNone(self._doc("// file header\n\nfunc f() {}", 3))

    def test_plain_code_above_is_not_a_doc(self):
        self.assertIsNone(self._doc("x = 1\nfunc f() {}", 2))

    def test_signature_joins_wrapped_params(self):
        src = "func Route(\n\tctx Context,\n\tid string,\n) error {\n\treturn nil\n}"
        self.assertEqual(
            EX.signature_at(src.splitlines(), 0),
            "func Route(ctx Context, id string) error",
        )

    def test_signature_skips_attribute_lines(self):
        # the graph often points at the attribute, not the decl below it
        src = "@discardableResult\nfunc add(_ x: Int) -> Int {"
        self.assertEqual(
            EX.signature_at(src.splitlines(), 0), "func add(_ x: Int) -> Int"
        )

    def test_signature_cuts_the_body_and_prototype_semicolon(self):
        self.assertEqual(EX.signature_at(["int h() { return 1; }"], 0), "int h()")
        self.assertEqual(
            EX.signature_at(["ring_t *ring_new(size_t n);"], 0),
            "ring_t *ring_new(size_t n)",
        )


class TestModuleDoc(unittest.TestCase):
    """extract.module_doc — the page's lead prose: the docstring for Python, the
    file-top comment block for everything else, never double-claimed with the doc of
    the first symbol."""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="documate_md_"))

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.dir, ignore_errors=True)

    def _md(self, name: str, src: str, first_line=None):
        p = self.dir / name
        p.write_text(src)
        return EX.module_doc(p, first_line)

    def test_go_package_comment(self):
        src = (
            "// Package store keeps orders.\npackage store\n\n"
            "// Save writes.\nfunc Save() {}\n"
        )
        self.assertEqual(
            self._md("store.go", src, first_line=5), "Package store keeps orders."
        )

    def test_adjacent_to_first_symbol_is_the_symbols_doc(self):
        src = "/** Format a price. */\nexport function fmt() {}\n"
        self.assertIsNone(self._md("util.ts", src, first_line=2))

    def test_block_comment_header(self):
        src = "/* math helpers */\n\nint add(int a, int b) { return a + b; }\n"
        self.assertEqual(self._md("lib.c", src, first_line=3), "math helpers")

    def test_python_docstring_unchanged(self):
        self.assertEqual(
            self._md("m.py", '"""Module prose."""\nx = 1\n'), "Module prose."
        )

    def test_license_header_is_not_the_module_prose(self):
        src = (
            "// Copyright 2026 Corp.\n// SPDX-License-Identifier: MIT\n\n"
            "// Package gadget wires the demo.\npackage gadget\n"
        )
        self.assertEqual(self._md("g.go", src), "Package gadget wires the demo.")

    def test_license_only_file_has_no_prose(self):
        self.assertIsNone(self._md("l.c", "/* Copyright 2026 Corp. */\n\nint x;\n"))

    def test_stacked_vendor_boilerplate_is_skipped_block_by_block(self):
        # dtoa.c-style header: an author/copyright block, then a bug-reports block,
        # buried anywhere in the block (not just its first lines) — the first real
        # prose block wins
        src = (
            "/* The author of this software is D. Gay.\n"
            " *\n"
            " * Copyright (c) 1991 by Lucent. */\n\n"
            "/* Please send bug reports to D. Gay. */\n\n"
            "/* strtod for IEEE machines. */\n\n"
            "double strtod(const char *s) { return 0; }\n"
        )
        self.assertEqual(self._md("dtoa.c", src), "strtod for IEEE machines.")


class TestShellExtract(unittest.TestCase):
    """Shell scripts document themselves with `#` runs — honored for .sh/.bash/.zsh
    and only there: elsewhere `#` is a directive (#include), and Python's `#` never
    reaches this path (its docs come via ast)."""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="documate_sh_"))

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.dir, ignore_errors=True)

    def _f(self, name: str, src: str) -> Path:
        p = self.dir / name
        p.write_text(src)
        return p

    def test_hash_header_is_module_prose_shebang_excluded(self):
        p = self._f(
            "key-bindings.bash",
            "#!/usr/bin/env bash\n"
            "# Key bindings for the shell.\n"
            "# - CTRL-T\n\n"
            "bind_all() { :; }\n",
        )
        self.assertEqual(EX.module_doc(p), "Key bindings for the shell.\n- CTRL-T")

    def test_ascii_art_banner_lines_drop_out(self):
        p = self._f(
            "completion.zsh",
            "#     ____\n#    /_/\\_\\\n# Sets up completion.\n\nx=1\n",
        )
        self.assertEqual(EX.module_doc(p), "Sets up completion.")

    def test_function_comment_above(self):
        p = self._f(
            "select.sh",
            "# Selects a file with fzf.\n__fzf_select__() {\n  :\n}\n",
        )
        syms = [
            {"qualified": "select.sh::__fzf_select__", "line": 2, "kind": "Function"}
        ]
        self.assertEqual(
            EX.comment_symbols(p, syms),
            {"__fzf_select__": ("__fzf_select__()", "Selects a file with fzf.")},
        )

    def test_c_hash_is_a_directive_not_a_comment(self):
        p = self._f("m.c", "#include <stdio.h>\n\nint x;\n")
        self.assertIsNone(EX.module_doc(p))


class TestDeclDefMerge(unittest.TestCase):
    """comment_symbols looks past the node's own line when nothing sits above it: the
    doc may legitimately live on the C header prototype (doxygen's decl/def merge) or
    on the `class` shadowed by an `extension` (the engine keeps one node per name)."""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="documate_merge_"))

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.dir, ignore_errors=True)

    def test_anonymous_typedef_keeps_its_name_not_the_cut_decl(self):
        # `typedef struct { ... } jv;` — the decl cut at `{` reads "typedef struct",
        # which doesn't even name the symbol: better no signature than that
        _w(
            self.dir / "jv.h",
            "/* All fields private. */\ntypedef struct {\n  int x;\n} jv;\n",
        )
        out = EX.comment_symbols(
            self.dir / "jv.h", [{"qualified": "jv.h::jv", "line": 2, "kind": "Class"}]
        )
        self.assertEqual(out["jv"], (None, "All fields private."))

    def test_c_definition_borrows_the_header_doc(self):
        _w(
            self.dir / "ring.h",
            "/** Allocate a ring holding n slots. */\nring_t *ring_new(size_t n);\n",
        )
        _w(self.dir / "ring.c", '#include "ring.h"\nring_t *ring_new(size_t n)\n{\n}\n')
        out = EX.comment_symbols(
            self.dir / "ring.c",
            [{"qualified": "ring.c::ring_new", "line": 2, "kind": "Function"}],
        )
        self.assertEqual(out["ring_new"][1], "Allocate a ring holding n slots.")

    def test_swift_class_doc_survives_an_extension_node(self):
        src = "/// A cart.\nfinal class Cart {\n}\n\nextension Cart {\n}\n"
        p = self.dir / "Cart.swift"
        p.write_text(src)
        out = EX.comment_symbols(
            p, [{"qualified": "Cart.swift::Cart", "line": 5, "kind": "Class"}]
        )
        self.assertEqual(out["Cart"], ("final class Cart", "A cart."))

    def test_prose_mentioning_the_name_is_not_a_declaration(self):
        # a comment line containing "class Cart" must not be mistaken for the decl —
        # the rescue must come up empty here, not claim the stray prose
        src = "// intro\n// about the class Cart here\n\nextension Cart {\n}\n"
        p = self.dir / "C.swift"
        p.write_text(src)
        out = EX.comment_symbols(
            p, [{"qualified": "C.swift::Cart", "line": 4, "kind": "Class"}]
        )
        self.assertIsNone(out["Cart"][1])


class TestMonorepo(unittest.TestCase):
    """`--root` points the same binary at any sub-tree. Two packages under one git repo,
    each with its own docs, must resolve independently against its own sub-tree."""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="documate_mono_")).resolve()
        root = self.dir
        _w(root / "pkg-a" / "a.py", "def alpha():\n    return 1\n")
        _w(
            root / "pkg-a" / "docs" / "guides" / "a.md",
            "## Alpha\n<!-- documents: sym:alpha -->\n",
        )
        _w(root / "pkg-b" / "b.py", "def beta():\n    return 2\n")
        _w(
            root / "pkg-b" / "docs" / "guides" / "b.md",
            "## Beta\n<!-- documents: sym:beta -->\n",
        )
        _git(root, "init", "-q")
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "x")
        self.ctx_a = Context.make(root / "pkg-a")
        self.ctx_b = Context.make(root / "pkg-b")
        self.ctx_a.graph.index()
        self.ctx_b.graph.index()

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.dir, ignore_errors=True)

    def test_subtree_resolves_own_symbol(self):
        self.assertTrue(R.resolve(self.ctx_a, "sym:alpha").ok)
        self.assertTrue(R.resolve(self.ctx_b, "sym:beta").ok)

    def test_subtree_isolated_from_sibling(self):
        # pkg-a's graph must not see pkg-b's symbol — the sub-tree is the world.
        self.assertFalse(R.resolve(self.ctx_a, "sym:beta").ok)
        self.assertFalse(R.resolve(self.ctx_b, "sym:alpha").ok)


class TestCliHelp(unittest.TestCase):
    """The CLI front door: `-h` (and a bare `documate` outside any repo, where
    there is nothing to act on) lands on one exhaustive help screen (exit 0, not
    an argparse error) showing every command with every one of its options."""

    def setUp(self) -> None:
        self._nowhere = Path(tempfile.mkdtemp(prefix="documate_norepo_"))

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self._nowhere, ignore_errors=True)

    def _screen(self, argv):
        import contextlib
        import io

        buf = io.StringIO()
        old = os.getcwd()
        os.chdir(self._nowhere)  # outside any git repo: bare documate = help
        try:
            with contextlib.redirect_stdout(buf):
                rc = CLI.main(argv)
        finally:
            os.chdir(old)
        self.assertEqual(rc, 0)
        return buf.getvalue()

    def test_bare_invocation_and_dash_h_show_every_option(self):
        out = self._screen([])
        self.assertEqual(out, self._screen(["-h"]))
        self.assertIn("Usage: documate", out)
        for token in (
            "path",
            "--check",
            "--watch",
            "--ai [MODEL]",
            "--root PATH",
            "--full",
            "--html",
            "--base REF",
            "--briefs [DIR]",
            "--yes",
        ):
            self.assertIn(token, out)


class TestInit(RealGraph):
    """`documate --init`: scaffold a root config, then run the normal job."""

    def _quiet(self, fn):
        import contextlib
        import io

        with (
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            return fn()

    def test_writes_a_valid_config_and_generates_docs(self):
        cfg = self.dir / "documate.config.json"
        self.assertFalse(cfg.exists())
        rc = self._quiet(lambda: CLI.main([str(self.dir), "--init"]))
        self.assertEqual(rc, 0)
        self.assertTrue((self.dir / "docs" / "README.md").exists())  # normal job ran
        body = json.loads(cfg.read_text())
        self.assertEqual(body["skip_dirs"], [])  # empty = defaults, honest extend
        self.assertIn("docs_dir", body)
        # the scaffold round-trips through the loader (no unknown-key error) and
        # is behaviourally the default config
        self.assertEqual(Context.make(self.dir).config.default_base, "main")

    def test_never_clobbers_an_existing_config(self):
        cfg = self.dir / "documate.config.json"
        cfg.write_text('{"default_base": "develop"}')
        self._quiet(lambda: CLI.main([str(self.dir), "--init"]))
        self.assertEqual(json.loads(cfg.read_text()), {"default_base": "develop"})

    def test_scaffold_unit_returns_none_when_present(self):
        from documate import config as CFG

        self.assertIsNotNone(CFG.scaffold(self.dir))  # first time writes
        self.assertIsNone(CFG.scaffold(self.dir))  # second time refuses

    def test_init_refuses_other_mode_flags(self):
        import contextlib
        import io

        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(CLI.main(["--init", "--check", str(self.dir)]), 2)


class TestBareInvocation(RealGraph):
    """Bare `documate` (and `documate PATH`) is the zero-decision front door:
    it refreshes the docs and then gates them — docs, then check, one command."""

    def _quiet(self, fn):
        import contextlib
        import io

        with (
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            return fn()

    def test_path_argument_runs_docs_then_check(self):
        rc = self._quiet(lambda: CLI.main([str(self.dir)]))
        self.assertEqual(rc, 0)
        self.assertTrue((self.dir / "docs" / "README.md").exists())
        self.assertTrue((self.dir / "docs" / "ARCHITECTURE.md").exists())

    def test_bare_inside_repo_runs_the_job(self):
        old = os.getcwd()
        os.chdir(self.dir)
        try:
            rc = self._quiet(lambda: CLI.main([]))
        finally:
            os.chdir(old)
        self.assertEqual(rc, 0)
        self.assertTrue((self.dir / "docs" / "README.md").exists())

    def test_check_mode_gates_without_writing(self):
        rc = self._quiet(lambda: CLI.main([str(self.dir), "--check"]))
        self.assertEqual(rc, 1)  # no docs yet: freshness must fail
        self.assertFalse((self.dir / "docs").exists())  # and nothing was written

    def test_ai_mode_seeds_then_repairs(self):
        from unittest import mock

        calls = []
        with (
            mock.patch.object(
                CLI.prose,
                "fix_docs",
                side_effect=lambda ctx, m, yes=False, **kw: (
                    calls.append(("seed", m, yes)) or 0
                ),
            ),
            mock.patch.object(
                CLI.prose,
                "fix_check",
                side_effect=lambda ctx, b, m, yes=False, quiet=False, **kw: (
                    calls.append(("repair", m, yes, quiet)) or 0
                ),
            ),
        ):
            rc = self._quiet(lambda: CLI.main([str(self.dir), "--ai", "--yes"]))
            self.assertEqual(rc, 0)
            # --yes rides through to both phases: the pre-flight won't block CI;
            # the composed repair pass keeps its internal gate quiet
            self.assertEqual(
                calls, [("seed", "haiku", True), ("repair", "haiku", True, True)]
            )
            calls.clear()
            rc = self._quiet(
                lambda: CLI.main([str(self.dir), "--check", "--ai", "sonnet"])
            )
            self.assertEqual(rc, 0)
            # never seeds; the user asked for the gate, so it stays loud
            self.assertEqual(calls, [("repair", "sonnet", False, False)])

    def test_ai_mode_stops_when_seeding_fails(self):
        from unittest import mock

        with (
            mock.patch.object(CLI.prose, "fix_docs", return_value=1),
            mock.patch.object(CLI.prose, "fix_check") as repair,
        ):
            rc = self._quiet(lambda: CLI.main([str(self.dir), "--ai"]))
        self.assertEqual(rc, 1)
        repair.assert_not_called()  # a failed seed reports; it doesn't cascade


class TestUi(unittest.TestCase):
    """The presentation layer's captured-output contract: piped/CI output is the
    same words as the terminal's, plain — no ANSI, no wrapping, streams intact."""

    def _capture(self, fn):
        import contextlib
        import io

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            fn()
        return out.getvalue(), err.getvalue()

    def test_plain_output_no_ansi_streams_preserved(self):
        long = "x" * 300  # far past any default console width — must not wrap
        out, err = self._capture(lambda: (UI.ok(long), UI.fail("broken")))
        self.assertIn(f"✓ {long}\n", out)  # one line, unwrapped
        self.assertNotIn("\x1b", out)  # no ANSI when captured
        self.assertIn("✗ broken", err)  # failures stay on stderr

    def test_tracker_plain_mode_is_a_transcript(self):
        def run():
            with UI.tracker(2) as t:
                t.working("one")
                t.done("drafted  one")
                t.working("two")
                t.failed("timeout  two")

        out, err = self._capture(run)
        self.assertIn("→ one …", out)  # announces what it is currently doing
        self.assertIn("✓ drafted  one", out)
        self.assertIn("✗ timeout  two", err)


class TestSkipDirs(unittest.TestCase):
    """skip_dirs is one knob for graph and docs alike: vendored trees never
    enter the graph (real engine), and config overrides extend the defaults."""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="documate_skip_")).resolve()
        _w(self.dir / "src" / "main.c", "int main_fn(void){return 1;}\n")
        _w(self.dir / "third_party" / "lib.c", "int vend_fn(void){return 2;}\n")
        _w(self.dir / "deps" / "dep.c", "int dep_fn(void){return 3;}\n")
        _w(self.dir / "src" / "testdata" / "fix.c", "int fix_fn(void){return 4;}\n")
        _git(self.dir, "init", "-q")
        _git(self.dir, "add", "-A")
        _git(self.dir, "commit", "-q", "-m", "x")

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.dir, ignore_errors=True)

    def test_vendored_trees_never_enter_the_graph(self):
        ctx = Context.make(self.dir)
        ctx.graph.index()
        rels = {ctx.rel(s["file"]) for s in ctx.graph.symbols()}
        self.assertEqual(rels, {"src/main.c"})

    def test_override_extends_defaults_and_bang_drops_one(self):
        (self.dir / "documate.config.json").write_text(
            '{"skip_dirs": ["/mystuff/", "!/vendor/"]}'
        )
        cfg = Context.make(self.dir).config
        self.assertIn("/mystuff/", cfg.skip_dirs)
        self.assertIn("/third_party/", cfg.skip_dirs)  # defaults survive an override
        self.assertNotIn("/vendor/", cfg.skip_dirs)  # "!" un-skips a default


class TestStats(Base):
    """`documate --stats`: the dashboard and its two jsonl ledgers."""

    def _run(self) -> tuple[int, str]:
        import contextlib
        import io

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = ST.run(self.ctx)
        return rc, out.getvalue()

    def test_dashboard_renders_and_records_a_snapshot(self):
        rc, out = self._run()
        self.assertEqual(rc, 0)
        self.assertIn("0/3 · 0%", out)  # the fixture's three undocumented C symbols
        self.assertIn("first snapshot recorded", out)
        self.assertIn("model spend: none recorded", out)
        led = self.dir / ".documate" / "stats.jsonl"
        self.assertEqual(len(led.read_text().splitlines()), 1)
        # unchanged repo: nothing appended, no delta faked against ourselves
        rc, out = self._run()
        self.assertEqual(len(led.read_text().splitlines()), 1)
        self.assertNotIn("deltas vs", out)

    def test_deltas_compare_to_the_previous_distinct_snapshot(self):
        self._run()  # baseline
        guide = self.dir / "docs" / "guides" / "01-key.md"
        guide.write_text(guide.read_text() + "a\nb\nc\n")
        rc, out = self._run()
        self.assertIn("+3", out)  # page_lines moved
        self.assertIn("deltas vs", out)
        led = self.dir / ".documate" / "stats.jsonl"
        self.assertEqual(len(led.read_text().splitlines()), 2)

    def test_spend_ledger_sums_across_runs_and_models(self):
        ST.add_spend(self.ctx, "haiku", 24100, 0.0547)
        ST.add_spend(self.ctx, "sonnet", 9000, 0.11)
        ST.add_spend(self.ctx, "haiku", 0, 0.0)  # unmeasured run: never recorded
        led = self.dir / ".documate" / "spend.jsonl"
        self.assertEqual(len(led.read_text().splitlines()), 2)
        rc, out = self._run()
        self.assertIn("33.1k tok", out)
        self.assertIn("$0.16", out)
        self.assertIn("2 --ai run(s)", out)
        self.assertIn("sonnet", out)

    def test_garbled_ledgers_degrade_to_no_history(self):
        (self.dir / ".documate" / "spend.jsonl").write_text("not json\n")
        (self.dir / ".documate" / "stats.jsonl").write_text("{]\n")
        rc, out = self._run()
        self.assertEqual(rc, 0)
        self.assertIn("none recorded", out)

    def test_stats_refuses_other_mode_flags(self):
        import contextlib
        import io

        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(CLI.main(["--stats", "--check", str(self.dir)]), 2)

    def test_docs_run_records_a_snapshot(self):
        self.assertEqual(DOCS.run(self.ctx), 0)
        led = self.dir / ".documate" / "stats.jsonl"
        self.assertEqual(len(led.read_text().splitlines()), 1)


class CFamilyInsertTest(unittest.TestCase):
    """Doc comments written into C/C++ must be Doxygen blocks, must sit above the
    whole declaration, and must never land on a use of the symbol's name."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="documate_cfam_")).resolve()
        self.ctx = SimpleNamespace(root=self.dir)

    def _write(self, name: str, src: str) -> Path:
        path = self.dir / name
        path.write_text(src, encoding="utf-8")
        return path

    def _insert(self, name: str, src: str, symbol: str, line: int, text="Does a thing."):
        path = self._write(name, src)
        row = {"kind": "undocumented", "symbol": symbol, "file": name, "line": line}
        err = P._insert_above(self.ctx, row, text, {})
        return err, path.read_text(encoding="utf-8")

    def test_c_gets_a_doxygen_block_not_a_line_comment(self):
        err, out = self._insert("a.c", "int add(int x)\n{\n\treturn x;\n}\n", "add", 1)
        self.assertIsNone(err)
        self.assertIn("/**", out)
        self.assertNotIn("// Does a thing.", out)

    def test_return_type_on_its_own_line_keeps_the_declaration_intact(self):
        # Linux/Zephyr house style: the graph records the symbol at its name, which
        # is the second line. Inserting there would split the declaration.
        src = "static enum err\nparse_attr(struct attr *a)\n{\n\treturn 0;\n}\n"
        err, out = self._insert("b.c", src, "parse_attr", 2)
        self.assertIsNone(err)
        self.assertTrue(
            out.startswith("/**"), f"comment must precede the return type:\n{out}"
        )
        self.assertNotIn("static enum err\n/**", out)

    def test_pointer_return_with_a_lone_star_line(self):
        src = "struct msg\n\t*\n\tbuild_m3(struct session *s)\n{\n\treturn 0;\n}\n"
        err, out = self._insert("c.c", src, "build_m3", 3)
        self.assertIsNone(err)
        self.assertTrue(out.startswith("/**"), out)

    def test_cpp_qualified_method_definition(self):
        src = "CHIP_ERROR\nReader::GetSubId(MutableByteSpan &out)\n{\n\treturn 0;\n}\n"
        err, out = self._insert("d.cpp", src, "GetSubId", 2)
        self.assertIsNone(err)
        self.assertTrue(out.startswith("/**"), out)

    def test_refuses_to_write_inside_a_parameter_list(self):
        # `attr` also names a type used as a parameter; the name search can land there.
        src = "int parse(const char *name,\n\t  struct attr *attr)\n{\n\treturn 0;\n}\n"
        err, out = self._insert("e.c", src, "attr", 2)
        self.assertEqual(err, "landing line is not a definition")
        self.assertNotIn("/**", out)

    def test_refuses_to_write_onto_a_local_variable(self):
        src = "void run(void)\n{\n\tstruct caps *caps = get();\n\t(void)caps;\n}\n"
        err, out = self._insert("f.c", src, "caps", 3)
        self.assertEqual(err, "landing line is not a definition")
        self.assertNotIn("/**", out)

    def test_brace_on_the_signature_line_is_still_a_function_body(self):
        # The body brace shares its line with `struct` (the return type): that
        # must read as a function body, not a member scope.
        src = (
            "static struct caps *get(void) {\n"
            "\tstruct caps *caps = init();\n"
            "\treturn caps;\n}\n"
        )
        err, out = self._insert("k.c", src, "caps", 2)
        self.assertEqual(err, "landing line is not a definition")
        self.assertNotIn("/**", out)

    def test_struct_member_is_still_documentable(self):
        src = "struct cfg {\n\tint slot_bitmask;\n};\n"
        err, out = self._insert("g.c", src, "slot_bitmask", 2)
        self.assertIsNone(err)
        self.assertIn("/**", out)

    def test_already_documented_is_left_alone(self):
        src = "/** Adds. */\nstatic int\nadd(int x)\n{\n\treturn x;\n}\n"
        err, out = self._insert("h.c", src, "add", 3)
        self.assertEqual(err, "already documented")
        self.assertEqual(out.count("/**"), 1)

    def test_non_c_languages_keep_line_comments(self):
        err, out = self._insert("i.rs", "fn add(x: i32) -> i32 { x }\n", "add", 1)
        self.assertIsNone(err)
        self.assertIn("// Does a thing.", out)
        self.assertNotIn("/**", out)

    def test_rewrite_replaces_above_the_whole_declaration(self):
        src = "// old\nstatic enum err\nparse_attr(struct attr *a)\n{\n\treturn 0;\n}\n"
        path = self._write("j.c", src)
        row = {"kind": "rewrite", "symbol": "parse_attr", "file": "j.c", "line": 3}
        err = P._rewrite_above(self.ctx, row, "@brief Parses.", {})
        out = path.read_text(encoding="utf-8")
        self.assertIsNone(err)
        self.assertNotIn("// old", out)
        self.assertTrue(out.startswith("/**"), out)
        self.assertIn("static enum err\nparse_attr", out)



if __name__ == "__main__":
    unittest.main(verbosity=2)

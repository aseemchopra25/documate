"""docs.py — `documate`: generate the committed documentation from code.

The generated tier. One overview page (`docs/README.md`) plus one architecture page per
subsystem (`docs/architecture/<slug>.md`), built from two honest sources:

  structure  the graph — which symbols exist, who calls whom, which module imports which
  prose      your docstrings/doc-comments, via `extract` — never invented

Output is committed (it's the documentation people read on the repo) but never
hand-edited: `documate` rewrites it, `documate --check` fails CI when it's stale.
A symbol with no docstring lands in an "Undocumented" fold instead of a faked
paragraph, so the coverage number on the overview is honest and ratchets up as you
write docstrings.

The build is split model -> render on purpose: `build_model` returns plain dataclasses
(no markdown), `render` turns them into markdown strings. A future HTML renderer plugs
into the same model. Output via `ui`, logic stdlib only; graph needed (the CLI
indexes before calling in).
"""

from __future__ import annotations

import ast
import functools
import posixpath
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import extract, stats, ui
from .core import GENERATED_STAMP, STAMPS, Context

_XREF_CAP = 8  # callers/callees listed per symbol — keep a page skimmable
_FLOW_CAP = 12  # callees drawn in a subsystem flow — a flow, not a hairball
_EDGE_CAP = 60  # edges drawn on the overview map
_GROUP_AT = 40  # more pages than this (in >1 directory): group the docs by directory
_HOT_CAP = 5  # hotspot modules / coupled pairs listed on the overview
_BULK_CAP = 8  # a commit touching more modules than this is a bulk change: skip it
_MENTION_CAP = 5  # existing doc files linked per module page ("discussed in")
_DOC_EXTS = (".md", ".rst")  # what counts as existing documentation worth linking


def _slug(rel: str) -> str:
    """Flatten a repo-relative source path into a page filename stem (`src/a/b.py` -> `src.a.b`)."""
    return rel.replace("/", ".").removesuffix(".py") or "root"


def _dir(rel: str) -> str:
    """The directory holding a module ("" at the repo root) — the grouping key when a
    repo is too big for one flat page list."""
    return rel.rpartition("/")[0]


def _tail(d: str, p: Page) -> str:
    """A page's filename stem inside its directory's folder (`src/a/b.py` -> `b`)."""
    return p.slug if not d else p.slug[len(_slug(d)) + 1 :]


def _skip(ctx: Context, rel: str) -> bool:
    """True for paths the docs must not treat as source: skip_dirs = not-our-source
    (vendored/build), test_markers = test code — the docs describe the public
    surface, not the suite that exercises it. Markers are substrings of the
    "/"-prefixed rel path: directories slash-wrapped ("/tests/", and the prefix "/"
    is why a top-level `tests/` matches) or filename suffixes ("_test.go")."""
    probe = "/" + rel
    return any(m in probe for m in (*ctx.config.skip_dirs, *ctx.config.test_markers))


def _xref_maps(ctx: Context, owned: set[str], extra=()) -> tuple[dict, dict]:
    """(callers, callees) keyed by qualified_name, values = sets of qualified_names.

    ONLY qualified targets that name an owned symbol count. A bare target is a builtin /
    stdlib / unresolved call; matching it by short name conflates collisions. Drop the
    bare half: a missing xref beats a wrong one. `extra` is more (src, tgt) qualified
    pairs recovered elsewhere (Go's re-qualified cross-file calls), same rules."""
    callers: dict[str, set] = {}
    callees: dict[str, set] = {}
    for src, tgt in (*ctx.graph.call_edges(), *extra):
        if src in owned and tgt in owned and src != tgt:
            callees.setdefault(src, set()).add(tgt)
            callers.setdefault(tgt, set()).add(src)
    return callers, callees


def _humanize(test_q: str) -> str:
    """A test's name read as the behavior it asserts: drop the test prefix, split
    snake/camel words ("TestReadRejectsShortRecord" -> "read rejects short record").
    Mined from the name, never invented — the evidence line for a symbol whose only
    documentation is its test suite."""
    name = extract.short(test_q).rsplit(".", 1)[-1]
    name = re.sub(r"^[Tt]est_?", "", name)
    words = re.split(r"_+|(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", name)
    return " ".join(w.lower() for w in words if w)


def _tested(ctx: Context, syms: list[dict]) -> dict[str, list[str]]:
    """qualified production symbol -> humanized names of the tests that call it.

    The engine's TESTED_BY edges carry a qualified test but usually a bare production
    name (tests live in other files). A bare name attaches only when exactly one owned
    symbol bears it — evidence pinned to the wrong symbol is worse than none."""
    by_name: dict[str, list[str]] = {}
    for s in syms:
        by_name.setdefault(s["name"], []).append(s["qualified"])
    owned = {s["qualified"] for s in syms}
    out: dict[str, set[str]] = {}
    for prod, test in ctx.graph.tested_by():
        if "::" in prod:
            q = prod if prod in owned else None
        else:
            cands = by_name.get(prod, ())
            q = cands[0] if len(cands) == 1 else None
        if q and (h := _humanize(test)):
            out.setdefault(q, set()).add(h)
    return {q: sorted(hs)[:_XREF_CAP] for q, hs in out.items()}


def _origins(ctx: Context, rels: set[str]) -> dict[str, str]:
    """rel source path -> subject of the oldest commit that added it.

    One `git log` pass over the whole history (newest first; later, older adds
    overwrite, so the original introduction wins). For a module with no docstring
    that subject is the only human prose in the repo about why the file exists —
    mined and labeled as a commit subject, never passed off as documentation.
    A commit adding more than _BULK_CAP owned modules is a bulk event (a tree
    move, a vendor import) whose subject describes no single file — skipped, same
    spirit as the hotspot bulk filter ("Move source files to src/" on 25 jq pages
    is what this rule exists to prevent). Empty on any git failure (shallow or
    absent history just means no evidence); note a shallow CI clone sees
    different history than a full one, so freshness checking needs
    `fetch-depth: 0` — same as the drift gate."""
    try:
        log = subprocess.run(
            [
                "git",
                "-C",
                str(ctx.root),
                "log",
                "--no-renames",
                "--relative",
                "--diff-filter=A",
                "--format=%x00%s",
                "--name-only",
                "--",
                ".",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    origins: dict[str, str] = {}
    for block in log.split("\0"):
        lines = [ln.strip() for ln in block.splitlines()]
        if not lines or not lines[0]:
            continue
        owned = [f for f in lines[1:] if f in rels]
        if not owned or len(owned) > _BULK_CAP:
            continue
        for f in owned:
            origins[f] = lines[0]
    return origins


def _doc_mentions(ctx: Context, rels: set[str]) -> dict[str, list[str]]:
    """module rel -> tracked doc files (.md/.rst, unstamped) that mention it by path.

    The repo's existing documentation is evidence too: a design note or an old
    docs site that names a module gets linked from that module's generated page
    ("discussed in"), so the map points into the prose humans already wrote
    instead of ignoring it. Matching is by repo-relative path — or bare filename
    when exactly one module carries it — never by symbol name (too collision-
    prone). Untracked files are invisible (same rule as the indexer); empty on
    any git failure."""
    try:
        out = subprocess.run(
            ["git", "-C", str(ctx.root), "ls-files", "-z", "--", "."],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    doc_files = [
        f for f in out.split("\0") if f.endswith(_DOC_EXTS) and not _skip(ctx, f)
    ]
    base_count: dict[str, int] = {}
    for r in rels:
        name = r.rsplit("/", 1)[-1]
        base_count[name] = base_count.get(name, 0) + 1
    pats = []
    for r in sorted(rels):
        alts = [re.escape(r)]
        name = r.rsplit("/", 1)[-1]
        if name != r and base_count[name] == 1:
            alts.append(re.escape(name))
        # boundaries: `src/key.c` must not fire inside `vendor/src/key.c`, and the
        # bare `key.c` must not fire inside `src/key.c` (the full path already does)
        pats.append(
            (r, re.compile(r"(?<![\w./-])(?:" + "|".join(alts) + r")(?![\w-])"))
        )
    mentions: dict[str, set[str]] = {}
    for f in doc_files:
        try:
            text = (ctx.root / f).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if text.startswith(STAMPS):
            continue  # our own generated tier quoting paths isn't discussion
        for r, pat in pats:
            if pat.search(text):
                mentions.setdefault(r, set()).add(f)
    return {r: sorted(fs)[:_MENTION_CAP] for r, fs in mentions.items()}


@dataclass
class Hotspots:
    """Change-frequency evidence for the overview, pinned to one commit.

    `rev` is the pin: the rendered section prints it, and `check` re-mines at that
    same commit (via `pinned_rev`) instead of HEAD — so history growing under
    committed docs never makes them "stale". `hot` is (module, commits touching
    it); `coupled` is (a, b, shared commits) for module pairs that usually change
    together yet share no import edge — coupling the dependency map can't show."""

    rev: str
    hot: list[tuple[str, int]]
    coupled: list[tuple[str, str, int]]


def _repo_name(ctx: Context) -> str:
    """The name the generated pages call this repo.

    Config `project_name` wins. Otherwise, when the root is a whole checkout, the
    name comes from the git common dir's parent — the main checkout's directory —
    so a linked worktree titles its pages exactly like the checkout that committed
    them and `--check` stays green there (the worktree's own dirname would differ
    on every page). A monorepo sub-tree root keeps its own basename, as does any
    non-git tree."""
    if ctx.config.project_name:
        return ctx.config.project_name
    try:
        out = subprocess.run(
            ["git", "-C", str(ctx.root), "rev-parse", "--show-toplevel", "--git-common-dir"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()
        top, common = Path(out[0]), Path(out[1])
    except (OSError, subprocess.SubprocessError, IndexError):
        return ctx.root.name
    if not common.is_absolute():
        common = (ctx.root / common).resolve()
    if top == ctx.root:
        return common.parent.name
    return ctx.root.name


def _head_rev(ctx: Context) -> str | None:
    """Current HEAD's short hash — the pin `documate` mines hotspots at.
    None (no hotspots) without git or before the first commit."""
    try:
        out = subprocess.run(
            ["git", "-C", str(ctx.root), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return out or None


def _rev_exists(ctx: Context, rev: str) -> bool:
    """True when `rev` still resolves to a commit. A hotspot pin orphaned by an
    amend/rebase does not — and a pin the gate can no longer mine at must be
    re-pinned rather than preserved."""
    return (
        subprocess.run(
            ["git", "-C", str(ctx.root), "cat-file", "-e", f"{rev}^{{commit}}"],
            capture_output=True,
        ).returncode
        == 0
    )


def _hotspots(
    ctx: Context, rev: str, rels: set[str], edges: list[tuple]
) -> Hotspots | None:
    """Mine churn and co-change from `git log <rev>`, filtered to owned modules.

    One pass over history as of the pin. Merge commits and bulk changes (more
    than _BULK_CAP modules in one commit — a reformat, not a change) are skipped.
    A module is hot with >= 2 commits; a pair is coupled when it shares >= 3
    commits, that is at least half of the quieter side's total, and no import
    edge links the two (an edge makes co-change expected, not hidden). None on
    any git failure (a missing pin just means no evidence) or when nothing
    clears the hot bar."""
    try:
        log = subprocess.run(
            [
                "git",
                "-C",
                str(ctx.root),
                "log",
                rev,
                "--no-merges",
                "--no-renames",
                "--relative",
                "--name-only",
                "--format=%x00",
                "--",
                ".",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    churn: dict[str, int] = {}
    shared: dict[tuple[str, str], int] = {}
    for block in log.split("\0"):
        touched = sorted(
            {f for f in (ln.strip() for ln in block.splitlines()) if f in rels}
        )
        if not touched or len(touched) > _BULK_CAP:
            continue
        for f in touched:
            churn[f] = churn.get(f, 0) + 1
        for i, a in enumerate(touched):
            for b in touched[i + 1 :]:
                shared[(a, b)] = shared.get((a, b), 0) + 1
    linked = {(min(a, b), max(a, b)) for a, b in edges}
    hot = sorted(
        ((f, n) for f, n in churn.items() if n >= 2), key=lambda x: (-x[1], x[0])
    )[:_HOT_CAP]
    coupled = sorted(
        (
            (a, b, n)
            for (a, b), n in shared.items()
            if n >= 3 and 2 * n >= min(churn[a], churn[b]) and (a, b) not in linked
        ),
        key=lambda x: (-x[2], x[0], x[1]),
    )[:_HOT_CAP]
    return Hotspots(rev=rev, hot=hot, coupled=coupled) if hot else None


def _go_edges(ctx: Context, syms: list[dict]) -> tuple[list[tuple], list[tuple]]:
    """(re-qualified call pairs, module edges) recovered from Go's unresolved edges.

    The engine leaves two Go gaps: IMPORTS_FROM targets are package paths
    ("example.com/mod/krypto"), not files, and a call is only qualified when caller
    and callee share a file — `krypto.Derive()` or a package-sibling `helper()` is
    stored as a bare name. Both are resolvable with what's already on disk:

    - an import path maps to the owned package dir it ends with;
    - a bare call target maps to the one file that (a) owns that name, (b) sits in
      the caller's own package dir or one it imports, and (c) is *literally called
      in the caller's source* — `krypto.Derive(` cross-package, an undotted
      `helper(` in-package — so a method call on some other type's value
      (`conn.Read()`) can't fabricate a dependency. The cross-package prefix is the
      package's *declared* name (Go never promises it matches the directory — fzf's
      `src/` declares `package fzf`) or an alias the caller's import line gives the
      path. Survivors in several package dirs → no edge: a missing xref beats a
      wrong one. Survivors sharing one dir are build-tag twins (`protector.go` /
      `protector_openbsd.go`), one implementation per platform — all keep the edge.

    Call pairs come back qualified ("<abs file>::Name", the graph's own format) for
    `_xref_maps`; module edges carry the symbol, plus a symbol-less edge for an
    imported single-file package that's never called (a types-only structs package
    still belongs on the dependency map)."""
    rel_of = {s["file"]: ctx.rel(s["file"]) for s in syms if s["file"].endswith(".go")}
    if not rel_of:
        return [], []
    abs_of = {r: f for f, r in rel_of.items()}
    dirname = lambda rel: rel.rsplit("/", 1)[0] if "/" in rel else ""  # noqa: E731
    owners: dict[str, set[str]] = {}  # bare name -> rel files defining it
    for s in syms:
        if s["file"] in rel_of:
            owners.setdefault(s["name"], set()).add(rel_of[s["file"]])
    dirs: dict[str, set[str]] = {}  # rel package dir -> rel files in it
    for rel in rel_of.values():
        dirs.setdefault(dirname(rel), set()).add(rel)

    # which owned package dirs each file imports (path-suffix match, longest wins)
    imports: dict[str, set[str]] = {}
    for src_abs, tgt in ctx.graph.import_edges():
        rs = rel_of.get(src_abs)
        if rs is None or tgt in abs_of or "\n" in tgt:
            continue
        hit = max(
            (d for d in dirs if d and (tgt == d or tgt.endswith("/" + d))),
            key=len,
            default=None,
        )
        if hit is not None:
            imports.setdefault(rs, set()).add(hit)

    texts: dict[str, str] = {}

    def text(rel: str) -> str:
        """Source of `rel`, read once, "" when unreadable."""
        if rel not in texts:
            try:
                texts[rel] = Path(abs_of[rel]).read_text(
                    encoding="utf-8", errors="ignore"
                )
            except OSError:
                texts[rel] = ""
        return texts[rel]

    pkg_of: dict[str, str] = {}  # package dir -> declared `package X` name

    def prefixes(rs: str, d: str) -> set[str]:
        """Call-site prefixes that can mean package dir `d` inside file `rs`: the
        package's declared name, plus any alias `rs`'s import line gives a path
        ending in the dir (`kk "example.com/mod/krypto"` -> `kk`)."""
        if d not in pkg_of:
            m = re.search(r"^package\s+(\w+)", text(min(dirs[d])), re.M)
            pkg_of[d] = m.group(1) if m else d.rsplit("/", 1)[-1]
        last = re.escape(d.rsplit("/", 1)[-1])
        return {pkg_of[d]} | set(re.findall(rf'(\w+)\s+"[^"\n]*/{last}"', text(rs)))

    pairs: list[tuple] = []
    mod_edges: list[tuple] = []
    seen: set[tuple] = set()
    for src_q, tgt in ctx.graph.call_edges():
        if "::" not in src_q or not tgt or "::" in tgt or "." in tgt:
            continue
        rs = rel_of.get(src_q.split("::", 1)[0])
        if rs is None or (rs, src_q, tgt) in seen or rs in owners.get(tgt, ()):
            continue  # own-file owner means the engine already had its chance
        seen.add((rs, src_q, tgt))
        cand_dirs = {dirname(rs)} | imports.get(rs, set())
        verified = []
        for cand in owners.get(tgt, ()):
            if cand == rs or dirname(cand) not in cand_dirs:
                continue
            pats = (
                [rf"(?<![.\w]){re.escape(tgt)}\s*\("]
                if dirname(cand) == dirname(rs)
                else [
                    rf"\b{re.escape(p)}\.{re.escape(tgt)}\s*\("
                    for p in prefixes(rs, dirname(cand))
                ]
            )
            if any(re.search(p, text(rs)) for p in pats):
                verified.append(cand)
        if verified and len({dirname(c) for c in verified}) == 1:
            for dst in sorted(verified):
                pairs.append((src_q, f"{abs_of[dst]}::{tgt}"))
                mod_edges.append((rs, dst, tgt))

    # an imported single-file package never called: the import alone is unambiguous
    called = {(rs, dst) for rs, dst, _ in mod_edges}
    for rs, ds in sorted(imports.items()):
        for d in sorted(ds):
            if len(dirs[d]) == 1:
                (dst,) = dirs[d]
                if dst != rs and (rs, dst) not in called:
                    mod_edges.append((rs, dst, None))
    return sorted(pairs), sorted(mod_edges, key=lambda e: (e[0], e[1], e[2] or ""))


def _module_edges(ctx: Context, syms: list[dict]) -> list[tuple]:
    """(src_module, dst_module, symbol|None) module-dependency edges.

    Two sources, one per language family. Python is scanned with stdlib `ast` (the
    engine truncates a multi-name `from . import a, b, c` to its first name
    and lumps stdlib in, so its IMPORTS_FROM can't draw a faithful Python graph):
      - `from .core import Context, load_config`  -> edges to core.py, symbols {Context, ...}
      - `from . import drift, docs`               -> edges to each module, no symbol
      - `import pkg.drift`                         -> edge to drift.py, no symbol
    A target resolves only to an owned module (by file stem); stdlib/third-party drop out.
    Everything else comes from the engine's IMPORTS_FROM edges (`graphdb.import_edges`):
    file->file rows resolve directly (JS/TS-style path imports); a C-family include
    target — `compile.h` bare, `wolfssl/wolfcrypt/aes.h` path-form, arriving exactly
    as written in the source — is found the way a compiler's include search would
    (`resolve_include`). No symbol names on either kind.

    The universe is every parsed non-skipped file, not just symbol owners: a barrel
    (index.ts of pure re-exports, a `from .x import y` __init__.py) defines nothing
    but is the hub the whole API surface routes through — drop it and a library's
    dependency map falls apart (zod dogfood)."""
    rel_of = {s["file"]: ctx.rel(s["file"]) for s in syms}  # abs file -> rel
    for f in ctx.graph.files():
        rel = ctx.rel(f)
        if f not in rel_of and not _skip(ctx, rel):
            rel_of[f] = rel
    stem_of: dict[str, str] = {}  # module stem -> rel file
    owned = set(rel_of.values())
    by_base: dict[str, list[str]] = {}  # bare filename -> every rel carrying it
    for f, rel in rel_of.items():
        stem_of.setdefault(Path(f).stem, rel)  # first wins on a stem collision
        by_base.setdefault(rel.rsplit("/", 1)[-1], []).append(rel)

    def resolve_include(rs: str, dst: str) -> str | None:
        """Find include target `dst` from module `rs` the way a compiler would.

        Path-form (`wolfssl/aes.h`): relative to the includer, then to the repo
        root (the ubiquitous -I<root>), then a unique path-suffix match (the
        -Iinclude layout). Bare (`config.h`): the includer's sibling, then unique
        below the includer's dir (ESP-IDF's main/include), then repo-unique — but
        only inside the includer's own top-level tree, so one vendor snapshot of
        `config.h` can't capture every module that means the build-generated one.
        System headers own nothing and drop out; a missing edge beats a wrong one."""
        at = rs.rsplit("/", 1)[0] if "/" in rs else ""
        if "/" in dst:
            rel = posixpath.normpath(f"{at}/{dst}") if at else dst
            if rel in owned:
                return rel
            if dst in owned:
                return dst
            hits = [
                r
                for r in by_base.get(dst.rsplit("/", 1)[-1], ())
                if r.endswith("/" + dst)
            ]
            return hits[0] if len(hits) == 1 else None
        sib = f"{at}/{dst}" if at else dst
        if sib in owned:
            return sib
        hits = by_base.get(dst, [])
        under = [r for r in hits if at and r.startswith(at + "/")]
        if len(under) == 1:
            return under[0]
        if len(hits) == 1 and (
            not at or hits[0].split("/", 1)[0] == rs.split("/", 1)[0]
        ):
            return hits[0]
        return None

    edges: list[tuple] = []
    for f, rs in rel_of.items():
        if not f.endswith(".py"):
            continue
        try:
            tree = ast.parse(Path(f).read_text(encoding="utf-8", errors="ignore"))
        except (OSError, SyntaxError, ValueError):
            continue
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom) and node.module
            ):  # from x.core import A, B
                rt = stem_of.get(node.module.split(".")[-1])
                if rt and rt != rs:
                    edges += [(rs, rt, a.name) for a in node.names]
            elif isinstance(node, ast.ImportFrom):  # from . import drift, docs
                edges += [
                    (rs, stem_of[a.name], None)
                    for a in node.names
                    if stem_of.get(a.name) and stem_of[a.name] != rs
                ]
            elif isinstance(node, ast.Import):  # import pkg.drift
                edges += [
                    (rs, stem_of[a.name.split(".")[-1]], None)
                    for a in node.names
                    if stem_of.get(a.name.split(".")[-1])
                    and stem_of[a.name.split(".")[-1]] != rs
                ]
    for src_f, dst_f in ctx.graph.import_edges():  # the non-Python half
        rs = rel_of.get(src_f)
        if rs is None or src_f.endswith(".py"):
            continue
        rt = rel_of.get(dst_f) or resolve_include(rs, dst_f)
        if rt and rt != rs:
            edges.append((rs, rt, None))
    return edges


@dataclass
class Symbol:
    """One function/class on a page: identity + prose + owned xrefs.

    `name` is the qualified-name tail — dotted for a class member (`GraphDB.index`),
    bare for a top-level symbol — so renderers can group methods under their class."""

    name: str
    kind: str
    line: int
    signature: str | None
    doc: str | None
    callers: list[str] = field(default_factory=list)
    callees: list[str] = field(default_factory=list)
    tested: list[str] = field(default_factory=list)  # humanized names of calling tests

    @property
    def owner(self) -> str | None:
        """The class a method belongs to (its dotted prefix); None for top-level symbols."""
        return self.name.rsplit(".", 1)[0] if "." in self.name else None


@dataclass
class Page:
    """One subsystem (= source module): its API surface, edges, and symbols."""

    rel: str  # repo-relative source path, e.g. "src/documate/cli.py"
    slug: str  # "src.documate.cli"
    module_doc: str | None  # module-level prose (Python), full text
    exposes: list[str]  # symbols other modules import — the API surface
    depends_on: dict[str, list[str]]  # rel module -> symbols it pulls from it
    used_by: list[str]  # rel modules that import this one
    symbols: list[Symbol]  # source order
    flow: list[tuple[str, str]]  # (entry, callee) edges for the page diagram
    origin: str | None = (
        None  # creating-commit subject; mined only when module_doc is absent
    )
    mentions: list[str] = field(
        default_factory=list
    )  # tracked doc files that mention this module by path ("discussed in")

    @property
    def summary(self) -> str:
        """First sentence-ish line of the module prose — the overview table cell."""
        if not self.module_doc:
            return ""
        head = self.module_doc.strip().splitlines()[0].strip()
        return head[:120]


#: Go's formal generated-code banner (stringer, protoc, mockgen all emit it);
#: by convention it sits before the package clause — the first lines cover it.
_GEN_RE = re.compile(r"^// Code generated .* DO NOT EDIT\.$")


@functools.lru_cache(maxsize=None)
def _machine_generated(path: Path) -> bool:
    """True when the file carries the generated-code banner in its first lines.
    Skip tier, same as skip_dirs: nobody reads the file, so it gets no page,
    no coverage debt, no --ai work order. Cached — the model and briefs both
    probe it per symbol."""
    try:
        head = path.read_text(encoding="utf-8", errors="ignore").splitlines()[:5]
    except OSError:
        return False
    return any(_GEN_RE.match(line) for line in head)


@dataclass
class Model:
    """Everything `render` (markdown today, HTML later) needs, no markup in it."""

    root_name: str
    pages: list[Page]
    module_edges: list[tuple[str, str]]  # dedup'd (src_rel, dst_rel)
    coverage: dict  # documented / undocumented / total / percent
    hotspots: Hotspots | None = None  # None: no pin, no git, or nothing hot
    docs_rel: str = "docs"  # docs_dir relative to the repo root — page->repo links


def build_model(ctx: Context, hot_rev: str | None = None) -> Model:
    """Read the graph + source into the page model. Pure: writes nothing.

    `hot_rev` pins the hotspot mining to one commit — `docs` passes HEAD, `check`
    passes the pin already printed in the committed overview (`pinned_rev`), so
    the freshness diff never moves just because history grew. None skips mining."""
    syms = [
        s
        for s in ctx.graph.symbols(("Function", "Class"))
        if not _skip(ctx, ctx.rel(s["file"]))
        and not _machine_generated(Path(s["file"]))
    ]
    owned = {s["qualified"] for s in syms}
    go_calls, go_mod_edges = _go_edges(ctx, syms)
    callers, callees = _xref_maps(ctx, owned, go_calls)
    tested = _tested(ctx, syms)

    by_file: dict[str, list] = {}
    for s in syms:
        by_file.setdefault(s["file"], []).append(s)

    edges = _module_edges(ctx, syms) + go_mod_edges
    member_names = {ctx.rel(f): {s["name"] for s in mem} for f, mem in by_file.items()}
    depends: dict[str, dict] = {}  # mod -> {dep_mod: {symbols}}
    exposed: dict[str, set] = {}  # mod -> {real symbols others import}
    used_by: dict[str, set] = {}  # mod -> {importers}
    for src, dst, sym in edges:
        depends.setdefault(src, {}).setdefault(dst, set())
        used_by.setdefault(dst, set()).add(src)
        if sym:
            depends[src][dst].add(sym)
            if sym in member_names.get(dst, ()):
                exposed.setdefault(dst, set()).add(sym)

    pages: list[Page] = []
    documented = undocumented = 0
    for abs_file in sorted(by_file):
        rel = ctx.rel(abs_file)
        members = sorted(by_file[abs_file], key=lambda r: r["line"])
        # a C `typedef struct x y;` yields two nodes on one line (alias + nested tag);
        # keep the outer one so the page doesn't document the same thing twice.
        at = {extract.short(s["qualified"]): s["line"] for s in members}
        members = [
            s
            for s in members
            if not (
                "." in (t := extract.short(s["qualified"]))
                and at.get(t.rsplit(".", 1)[0]) == s["line"]
            )
        ]
        prose = extract.extract(Path(abs_file), members)

        entries: list[Symbol] = []
        for s in members:
            disp = extract.short(s["qualified"])  # dotted for methods: Class.method
            sig, doc = prose.get(disp, (None, None))
            if doc:
                documented += 1
            else:
                undocumented += 1
            entries.append(
                Symbol(
                    name=disp,
                    kind=s["kind"],
                    line=s["line"],
                    signature=sig,
                    doc=doc,
                    callers=sorted(
                        extract.short(q) for q in callers.get(s["qualified"], ())
                    )[:_XREF_CAP],
                    callees=sorted(
                        extract.short(q) for q in callees.get(s["qualified"], ())
                    )[:_XREF_CAP],
                    tested=tested.get(s["qualified"], []),
                )
            )

        # the page diagram: the most-exposed (else first public, else first) symbol and
        # its real callees — a flow the reader can trust because the graph drew it.
        exposes = sorted(exposed.get(rel, set()))
        names = sorted(s["name"] for s in members)
        publics = [n for n in names if not n.startswith("_")]
        top = exposes[0] if exposes else (publics[0] if publics else names[0])
        top_q = next((s["qualified"] for s in members if s["name"] == top), None)
        flow = (
            [
                (top, c)
                for c in sorted({extract.short(q) for q in callees.get(top_q, ())})[
                    :_FLOW_CAP
                ]
            ]
            if top_q
            else []
        )

        pages.append(
            Page(
                rel=rel,
                slug=_slug(rel),
                module_doc=extract.module_doc(Path(abs_file), members[0]["line"]),
                exposes=exposes,
                depends_on={
                    m: sorted(v) for m, v in sorted(depends.get(rel, {}).items())
                },
                used_by=sorted(used_by.get(rel, set())),
                symbols=entries,
                flow=flow,
            )
        )

    # files the engine parsed but that own no symbols still get a page when they carry
    # module prose (Go's doc.go convention puts the package doc in a symbol-free file)
    # or sit in the import graph (a barrel — the API surface routes through it).
    for abs_file in sorted(set(ctx.graph.files()) - set(by_file)):
        rel = ctx.rel(abs_file)
        if _skip(ctx, rel) or _machine_generated(Path(abs_file)):
            continue
        doc = extract.module_doc(Path(abs_file))
        if doc or rel in depends or rel in used_by:
            pages.append(
                Page(
                    rel=rel,
                    slug=_slug(rel),
                    module_doc=doc,
                    exposes=[],
                    depends_on={
                        m: sorted(v) for m, v in sorted(depends.get(rel, {}).items())
                    },
                    used_by=sorted(used_by.get(rel, set())),
                    symbols=[],
                    flow=[],
                )
            )
    pages.sort(key=lambda p: p.rel)

    # evidence for the gap pages: a module with no docstring at least shows the
    # subject of the commit that created it — mined only when something needs it,
    # so a fully documented repo never pays the git call.
    if any(not p.module_doc for p in pages):
        origins = _origins(ctx, {p.rel for p in pages})
        for p in pages:
            if not p.module_doc:
                p.origin = origins.get(p.rel)

    # the repo's existing documentation joins the map: each page links the tracked
    # doc files that mention its module by path.
    mentioned = _doc_mentions(ctx, {p.rel for p in pages})
    for p in pages:
        p.mentions = mentioned.get(p.rel, [])

    total = documented + undocumented
    dedup = sorted({(s, d) for s, d, _ in edges})
    return Model(
        root_name=_repo_name(ctx),
        pages=pages,
        module_edges=dedup,  # renderers cap what they draw (_EDGE_CAP)
        docs_rel=ctx.rel(str(ctx.config.docs_dir)),
        hotspots=(
            _hotspots(ctx, hot_rev, {p.rel for p in pages}, dedup) if hot_rev else None
        ),
        coverage={
            "documented": documented,
            "undocumented": undocumented,
            "total": total,
            "percent": documented * 100 // total if total else 0,
        },
    )


_STAMP = GENERATED_STAMP  # emitted atop every generated page; detection matches STAMPS


def _tour(
    pages: list[Page], edges: list[tuple[str, str]]
) -> tuple[list[str], list[str]]:
    """(page rels in reading order, the entry points the tour starts from).

    Entry points are modules nothing else imports but that import something — the
    doors into the codebase. (Machine-generated files can't become doors: build_model
    drops them before a page exists.) Doors rank by *reach* — how many modules their
    dependency walk opens —
    so in a repo with hundreds of leaf example programs (wolfssl's IDE/ trees) the
    door that actually opens the library outranks whatever sorts first. The order
    walks breadth-first from the doors through
    their dependencies, so a reader meets each module after the code that drives it;
    whatever the walk can't reach (import cycles, isolated modules) is appended
    most-used-first. Pure graph fact — no salience is invented — and every tie
    breaks alphabetically, so the tour is deterministic."""
    rels = {p.rel for p in pages}
    dep: dict[str, list[str]] = {}
    fan_in: dict[str, int] = {r: 0 for r in rels}
    for a, b in sorted(set(edges)):
        if a in rels and b in rels and a != b:
            dep.setdefault(a, []).append(b)
            fan_in[b] += 1

    def reach(r: str) -> int:
        """How many modules `r`'s dependency walk opens (itself included)."""
        seen: set[str] = set()
        queue = [r]
        while queue:
            n = queue.pop()
            if n not in seen:
                seen.add(n)
                queue += dep.get(n, [])
        return len(seen)

    entries = sorted(
        (r for r in rels if not fan_in[r] and dep.get(r)),
        key=lambda r: (-reach(r), r),
    )
    order: list[str] = []
    seen: set[str] = set()
    queue = list(entries)
    while queue:
        r = queue.pop(0)
        if r in seen:
            continue
        seen.add(r)
        order.append(r)
        queue += dep.get(r, [])
    rest = sorted(rels - seen, key=lambda r: (-fan_in[r], r))
    return order + rest, entries


def _start_here(entries: list[str], href) -> list[str]:
    """The overview's "start here" line: link the entry points (capped at 3), or
    nothing when the graph has no clear door. `href` maps a rel to its page link."""
    if not entries:
        return []
    links = ", ".join(f"[`{r}`]({href(r)})" for r in entries[:3])
    tail = (
        "the doors into the codebase (nothing else imports them)"
        if len(entries) > 1
        else "the door into the codebase (nothing else imports it)"
    )
    return [f"**Start here:** {links} — {tail}.", ""]


def _about(p: Page) -> str:
    """A page's one-line description for overview tables: the module prose's first
    line when there is one, else the mined creating-commit subject — italicized and
    labeled as what it is, so evidence never masquerades as documentation."""
    if p.summary:
        return p.summary
    if p.origin:
        return f'*first commit: "{p.origin}"*'
    return ""


def _mermaid_lines(
    edges: list[tuple[str, str]],
    classes: dict[str, str] | None = None,
    clusters: dict[str, list[str]] | None = None,
) -> list[str]:
    """Mermaid edge lines with parse-safe node ids. `(`/`[` open shape syntax in
    a bare mermaid id, so a Next.js route dir (`app/(doc)/[[...slug]]`) silently
    becomes a mislabeled shape or a parse error (zod dogfood). Ids keep word
    chars/dots/dashes; a node whose id lost characters is declared once as
    `id["label"]` so the diagram still shows the real name. Distinct labels never
    share an id (collisions suffix `_2`) — merging two nodes would draw a lie.
    `classes` (label -> mermaid class name) adds one `class a,b,c name` line per
    class, ids resolved through the same table; the matching `classDef` colors are
    the renderer's job (the site injects them per theme), so the committed
    markdown carries no styling. `clusters` (box label -> member node labels)
    wraps members in labeled `subgraph` blocks, in order; cluster i is minted id
    `c<i>` (collision-suffixed like node ids) and classed `h<i>` so the renderer
    can tint the box to match its members' hue."""
    ids: dict[str, str] = {}
    taken: set[str] = set()

    def nid(label: str) -> str:
        """The parse-safe id for `label`, minted on first sight and stable after."""
        if label not in ids:
            base = re.sub(r"[^\w.-]", "_", label) or "_"
            cand, n = base, 1
            while cand in taken:
                n += 1
                cand = f"{base}_{n}"
            taken.add(cand)
            ids[label] = cand
        return ids[label]

    lines = [f"  {nid(a)} --> {nid(b)}" for a, b in edges]
    decls = [
        '  {}["{}"]'.format(i, label.replace('"', "'"))
        for label, i in sorted(ids.items())
        if i != label
    ]
    sub: list[str] = []
    boxed: list[str] = []
    for i, (label, members) in enumerate((clusters or {}).items()):
        mids = sorted(ids[m] for m in members if m in ids)
        if not mids:
            continue
        cid = f"c{i}"
        while cid in taken:
            cid += "_"
        taken.add(cid)
        sub.append('  subgraph {}["{}"]'.format(cid, label.replace('"', "'")))
        sub += [f"    {m}" for m in mids]
        sub.append("  end")
        boxed.append(f"  class {cid} h{i}")
    marks: dict[str, list[str]] = {}
    for label, cls in (classes or {}).items():
        if label in ids:
            marks.setdefault(cls, []).append(ids[label])
    tail = [
        "  class {} {}".format(",".join(sorted(marks[cls])), cls)
        for cls in sorted(marks)
    ]
    return decls + sub + lines + tail + boxed


def _stem_edges(edges: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Module edges collapsed to file stems for the overview diagram: a .c/.h pair
    is one node there, so its internal edge becomes a self-loop and its outgoing
    edges become duplicates — drop both, the diagram shows modules, not files."""
    stems = sorted({(Path(a).stem, Path(b).stem) for a, b in edges})
    return [(a, b) for a, b in stems if a != b]


_PIN_RE = re.compile(r"^\*Mined from git history as of `([0-9a-f]{4,40})`\.\*$", re.M)


def pinned_rev(ddir: Path) -> str | None:
    """The hotspot pin recorded in the committed overview, if any.

    `check` re-mines at this commit instead of HEAD, so freshness stays a pure
    function of the committed tree — new commits don't shift the counts under
    the diff. None when the overview is absent or carries no hotspot section."""
    try:
        text = (ddir / "README.md").read_text(encoding="utf-8")
    except OSError:
        return None
    m = _PIN_RE.search(text)
    return m.group(1) if m else None


def _hotspot_lines(hs: Hotspots | None, href) -> list[str]:
    """The overview's Hotspots section: the most-changed modules, then pairs that
    change together without an import edge between them. Mined evidence, labeled —
    and the label line doubles as the pin `pinned_rev` reads back."""
    if not hs:
        return []
    lines = ["## Hotspots", "", f"*Mined from git history as of `{hs.rev}`.*", ""]
    most = ", ".join(f"[`{f}`]({href(f)}) ({n} commits)" for f, n in hs.hot)
    lines += [f"**Most-changed:** {most}.", ""]
    if hs.coupled:
        lines += ["**Change together without importing each other:**", ""]
        lines += [
            f"- [`{a}`]({href(a)}) ↔ [`{b}`]({href(b)}) ({n} shared commits)"
            for a, b, n in hs.coupled
        ]
        lines.append("")
    return lines


def _overview(model: Model) -> str:
    """The docs/README.md: what the system is made of, drawn from the graph."""
    cov = model.coverage
    lines = [
        _STAMP,
        f"# {model.root_name}",
        "",
        f"**{len(model.pages)} subsystems · "
        f"{cov['documented']}/{cov['total']} symbols documented ({cov['percent']}%)**",
        "",
    ]
    _, entries = _tour(model.pages, model.module_edges)
    lines += _start_here(entries, lambda r: f"architecture/{_slug(r)}.md")
    if drawn := _stem_edges(model.module_edges):
        lines += ["```mermaid", "flowchart LR"]
        lines += _mermaid_lines(drawn[:_EDGE_CAP])
        lines += ["```", ""]
    lines += ["## Subsystems", "", "| subsystem | about |", "|---|---|"]
    for p in model.pages:
        about = _about(p).replace("|", "\\|")
        lines.append(f"| [`{p.rel}`](architecture/{p.slug}.md) | {about} |")
    lines.append("")
    lines += _hotspot_lines(model.hotspots, lambda r: f"architecture/{_slug(r)}.md")
    return "\n".join(lines)


def _grouped_overview(model: Model, groups: dict[str, list[Page]]) -> str:
    """The monorepo overview: directories, not a phone book of modules.

    Same header and honesty as `_overview`, but the map and the table aggregate to
    directory level and each row links that directory's own index page — a
    2,000-module repo gets a readable front page instead of a 2,000-row table."""
    cov = model.coverage
    lines = [
        _STAMP,
        f"# {model.root_name}",
        "",
        f"**{len(model.pages)} subsystems in {len(groups)} directories · "
        f"{cov['documented']}/{cov['total']} symbols documented ({cov['percent']}%)**",
        "",
    ]
    tail_of = {p.rel: _tail(d, p) for d, ps in groups.items() for p in ps}
    _, entries = _tour(model.pages, model.module_edges)
    lines += _start_here(
        entries, lambda r: f"architecture/{_slug(_dir(r))}/{tail_of[r]}.md"
    )
    gedges = sorted(
        {(_dir(a), _dir(b)) for a, b in model.module_edges if _dir(a) != _dir(b)}
    )
    if gedges:
        lines += ["```mermaid", "flowchart LR"]
        lines += _mermaid_lines([(_slug(a), _slug(b)) for a, b in gedges[:_EDGE_CAP]])
        lines += ["```", ""]
    lines += [
        "## Directories",
        "",
        "| directory | subsystems | documented |",
        "|---|---|---|",
    ]
    for d in sorted(groups):
        ps = groups[d]
        docd = sum(1 for p in ps for s in p.symbols if s.doc)
        tot = sum(len(p.symbols) for p in ps)
        pct = docd * 100 // tot if tot else 0
        lines.append(
            f"| [`{d or '.'}/`](architecture/{_slug(d)}/README.md)"
            f" | {len(ps)} | {docd}/{tot} ({pct}%) |"
        )
    lines.append("")
    lines += _hotspot_lines(
        model.hotspots, lambda r: f"architecture/{_slug(_dir(r))}/{tail_of[r]}.md"
    )
    return "\n".join(lines)


def _group_index(d: str, pages: list[Page]) -> str:
    """One directory's index page: the same subsystem table the small-repo overview
    has, scoped to this directory's modules."""
    lines = [_STAMP, f"# `{d or '.'}/`", "", "| subsystem | about |", "|---|---|"]
    for p in pages:
        about = _about(p).replace("|", "\\|")
        lines.append(f"| [`{p.rel}`]({_tail(d, p)}.md) | {about} |")
    lines.append("")
    return "\n".join(lines)


def _page(p: Page, href=None, at: str = "docs/architecture") -> str:
    """One architecture page: module prose, edges, flow, then the per-symbol API.

    `href` maps a sibling module's rel path to a link relative to THIS page — the
    grouped layout passes one that climbs directories (`../other.dir/mod.md`);
    default is the flat layout's same-folder link. `at` is this page's own folder
    relative to the repo root, so "discussed in" can link doc files anywhere in
    the repo."""
    href = href or (lambda m: f"{_slug(m)}.md")
    lines = [_STAMP, f"# `{p.rel}`", ""]
    if p.module_doc:
        lines += [p.module_doc.strip(), ""]
    elif p.origin:
        lines += [f'*No module docstring. First commit: "{p.origin}".*', ""]

    refs = []
    if p.depends_on:
        refs.append(
            "**depends on** " + ", ".join(f"[`{m}`]({href(m)})" for m in p.depends_on)
        )
    if p.used_by:
        refs.append(
            "**used by** " + ", ".join(f"[`{m}`]({href(m)})" for m in p.used_by)
        )
    if p.mentions:
        refs.append(
            "**discussed in** "
            + ", ".join(f"[`{m}`]({posixpath.relpath(m, at)})" for m in p.mentions)
        )
    if refs:
        lines += ["  ·  ".join(refs), ""]

    if p.flow:
        lines += ["```mermaid", "flowchart TD"]
        for a, b in p.flow:
            lines.append(f"  {a} --> {b}")
        lines += ["```", ""]

    documented = [s for s in p.symbols if s.doc]
    missing = [s for s in p.symbols if not s.doc]
    if documented:
        lines += ["## API", ""]
    for s in documented:
        # methods nest one level under their class (source order keeps them adjacent)
        lines += [
            f"{'####' if s.owner else '###'} `{s.signature or s.name}`",
            f"`{p.rel}:{s.line}`",
            "",
            s.doc.strip(),
        ]
        xref = []
        if s.callers:
            xref.append("**called by** " + ", ".join(f"`{n}`" for n in s.callers))
        if s.callees:
            xref.append("**calls** " + ", ".join(f"`{n}`" for n in s.callees))
        if xref:
            lines += ["", "  ·  ".join(xref)]
        lines.append("")
    if missing:
        # no docstring, but the fold still shows what the graph knows: the behaviors
        # the symbol's own tests assert, read off their names.
        lines += [f"<details><summary>Undocumented ({len(missing)})</summary>", ""]
        for s in missing:
            ev = f" — tested: {'; '.join(s.tested)}" if s.tested else ""
            lines.append(f"- `{s.name}`{ev}")
        lines += ["", "</details>", ""]
    return "\n".join(lines)


def _architecture(model: Model, groups: dict[str, list[Page]], grouped: bool) -> str:
    """docs/ARCHITECTURE.md — the whole system stitched onto one page.

    The read-it-top-to-bottom companion to the per-module reference: the dependency
    map, then every subsystem's full module prose in context, its API surface, and
    its neighbours — each heading and neighbour linking into `architecture/`. What
    the overview's table names and the architecture/ pages detail, this narrates.
    Sections come in `_tour` reading order (entry points first), not alphabetical;
    in the grouped (monorepo) layout they nest under their directory, directories
    ordered by their best-ranked page."""
    lines = [
        _STAMP,
        f"# {model.root_name} — architecture",
        "",
        "Every subsystem on one page, in reading order: entry points (nothing "
        "imports them) first, then the machinery they drive. Each section is the "
        "subsystem's own prose, what it exposes, and how the pieces depend on each "
        "other; headings link to the full per-module reference under "
        "[`architecture/`](architecture/).",
        "",
    ]
    order, _ = _tour(model.pages, model.module_edges)
    rank = {r: i for i, r in enumerate(order)}
    if grouped:
        where = {
            p.rel: f"architecture/{_slug(d)}/{_tail(d, p)}.md"
            for d, ps in groups.items()
            for p in ps
        }
        gedges = sorted(
            {(_dir(a), _dir(b)) for a, b in model.module_edges if _dir(a) != _dir(b)}
        )
        if gedges:
            lines += ["```mermaid", "flowchart LR"]
            lines += _mermaid_lines(
                [(_slug(a), _slug(b)) for a, b in gedges[:_EDGE_CAP]]
            )
            lines += ["```", ""]
    else:
        where = {p.rel: f"architecture/{p.slug}.md" for p in model.pages}
        if drawn := _stem_edges(model.module_edges):
            lines += ["```mermaid", "flowchart LR"]
            lines += _mermaid_lines(drawn[:_EDGE_CAP])
            lines += ["```", ""]

    dirs = (
        sorted(groups, key=lambda d: (min(rank[p.rel] for p in groups[d]), d))
        if grouped
        else [None]
    )
    for d in dirs:
        pages = sorted(groups[d] if grouped else model.pages, key=lambda p: rank[p.rel])
        if grouped:
            lines += [f"## `{d or '.'}/`", ""]
        for p in pages:
            lines += [f"{'###' if grouped else '##'} [`{p.rel}`]({where[p.rel]})", ""]
            if p.module_doc:
                lines += [p.module_doc.strip(), ""]
            elif p.origin:
                lines += [f'*No module docstring. First commit: "{p.origin}".*', ""]
            facts = []
            if p.exposes:
                facts.append("**exposes** " + ", ".join(f"`{s}`" for s in p.exposes))
            for label, mods in (("depends on", p.depends_on), ("used by", p.used_by)):
                if mods:
                    facts.append(
                        f"**{label}** "
                        + ", ".join(
                            f"[`{m}`]({where[m]})" if m in where else f"`{m}`"
                            for m in mods
                        )
                    )
            if facts:
                lines += ["  ·  ".join(facts), ""]
    return "\n".join(lines)


def render(model: Model) -> dict[str, str]:
    """Model -> {path-under-docs_dir: markdown}. Deterministic: same model, same bytes.

    Two layouts, one threshold: up to _GROUP_AT pages (or a single directory), the
    flat layout — overview table of modules, pages directly under `architecture/`.
    Past it, the grouped layout — overview table of directories, one folder per
    directory under `architecture/` holding its index (README.md) and its pages —
    so a monorepo's front page and directory listing stay readable."""
    groups: dict[str, list[Page]] = {}
    for p in model.pages:
        groups.setdefault(_dir(p.rel), []).append(p)
    if len(model.pages) <= _GROUP_AT or len(groups) < 2:
        out = {
            "README.md": _overview(model),
            "ARCHITECTURE.md": _architecture(model, groups, False),
        }
        for p in model.pages:
            out[f"architecture/{p.slug}.md"] = _page(
                p, at=f"{model.docs_rel}/architecture"
            )
        return out

    where = {
        p.rel: (_slug(d), _tail(d, p)) for d, ps in groups.items() for p in ps
    }  # rel -> (folder, stem)
    out = {
        "README.md": _grouped_overview(model, groups),
        "ARCHITECTURE.md": _architecture(model, groups, True),
    }
    for d in sorted(groups):
        g = _slug(d)

        def href(m: str, _g: str = g) -> str:
            """Link to a sibling module's page from inside this directory's folder."""
            g2, t2 = where[m]
            return f"{t2}.md" if g2 == _g else f"../{g2}/{t2}.md"

        out[f"architecture/{g}/README.md"] = _group_index(d, groups[d])
        for p in groups[d]:
            out[f"architecture/{g}/{_tail(d, p)}.md"] = _page(
                p, href, at=f"{model.docs_rel}/architecture/{g}"
            )
    return out


def _print_diff(rel: str, old: str, new: str) -> None:
    """A compact colored unified diff of one regenerated page — what `--watch` shows
    so every doc change is visible the moment it happens, straight in the terminal."""
    ui.diff(rel, old, new)


_AGENT_BEGIN = "<!-- code-map:begin -->"
_AGENT_END = "<!-- code-map:end -->"
# Every marker pair ever written, newest first — older pairs are recognized so an
# existing block upgrades in place instead of duplicating.
_AGENT_MARKERS = (
    (_AGENT_BEGIN, _AGENT_END),
    ("<!-- documate:begin -->", "<!-- documate:end -->"),
)


def _agent_pointer(ctx: Context) -> list[str]:
    """Maintain the agent-pointer block in AGENTS.md / CLAUDE.md — whichever already
    exist at the root (never created uninvited).

    The generated docs are a token-cheap map of the repo; this block tells coding
    agents to read that map before crawling source, which is the whole token-economy
    point. Idempotent: the block lives between documate markers and is rewritten in
    place; everything outside the markers is untouched. Returns the files changed.
    `check` never calls this — the gate stays read-only."""
    ddir = ctx.rel(str(ctx.config.docs_dir))
    block = (
        f"{_AGENT_BEGIN}\n"
        f"## Code map (generated)\n\n"
        f"Before crawling the source, read `{ddir}/README.md` — a generated,\n"
        f"always-current map of this repo: subsystems, module dependencies, docstring\n"
        f"coverage. Per-module API references (signatures, docstrings, callers and\n"
        f"callees) live in `{ddir}/architecture/`. Those pages are regenerated\n"
        f"automatically; never hand-edit them.\n"
        f"{_AGENT_END}"
    )
    changed: list[str] = []
    for name in ("AGENTS.md", "CLAUDE.md"):
        path = ctx.root / name
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue  # absent (or unreadable): not ours to create
        for begin, end in _AGENT_MARKERS:
            if begin in text and end in text:
                head, _, rest = text.partition(begin)
                _, _, tail = rest.partition(end)
                new = head + block + tail
                break
        else:
            new = text.rstrip("\n") + "\n\n" + block + "\n"
        if new != text:
            path.write_text(new)
            changed.append(name)
    return changed


def _rescue_docs_dir(ctx: Context, want: dict[str, str]) -> bool:
    """Interactive way out of the clobber refusal: ask where generated docs
    should go instead, validate the answer (inside the repo, no foreign files
    where our pages would land), persist it to the config file, and point
    ctx.config there. Returns True when a new docs_dir was accepted; False
    (no terminal, decline, or three bad answers) falls back to the refusal.
    The caller re-runs so the page model rebuilds with the new docs_rel."""
    import json

    root = ctx.root.resolve()
    for _ in range(3):
        ans = ui.ask(
            "docs/ has files documate didn't write — where should generated "
            "docs go instead?",
            default="docs/code",
        )
        if ans is None:
            return False
        cand = (root / ans).resolve()
        if Path(ans).is_absolute() or not cand.is_relative_to(root):
            ui.warn(f"docs: {ans} is outside the repo — pick a repo-relative path")
            continue
        if cand == ctx.config.docs_dir.resolve() or cand == root:
            ui.warn(f"docs: {ans} is where the conflict is — pick somewhere else")
            continue
        if cand.is_file():
            ui.warn(f"docs: {ans} is a file, not a directory")
            continue
        clash = next(
            (
                rel
                for rel in sorted(want)
                if (p := cand / rel).is_file()
                and not p.read_text(encoding="utf-8", errors="replace").startswith(
                    STAMPS
                )
            ),
            None,
        )
        if clash:
            ui.warn(f"docs: {ans}/{clash} exists and isn't documate's either")
            continue
        rel = cand.relative_to(root).as_posix()
        cfg = ctx.config.source or ctx.root / "documate.config.json"
        data = json.loads(cfg.read_text()) if cfg.is_file() else {}
        data["docs_dir"] = rel
        cfg.write_text(json.dumps(data, indent=2) + "\n")
        ctx.config.docs_dir = cand
        ui.ok(f"docs: docs_dir → {rel}, saved to {ctx.rel(str(cfg))} — commit it")
        return True
    return False


def run(ctx: Context, diff: bool = False, quiet: bool = False) -> int:
    """Write the generated tier under docs_dir, pruning orphaned pages of ours.

    Never touches a file it didn't stamp: a docs/ tree that predates documate makes
    this refuse (nothing written) rather than clobber — the fix is pointing docs_dir
    elsewhere, not a --force. Only pages whose content actually changed are
    rewritten. With diff=True (the `--watch` live view) every new/changed/pruned
    page is printed as a colored unified diff, so you watch the documentation move
    as you edit the code. With quiet=True (the --ai post-draft refresh) the summary
    line stays unprinted — refusals and failures still speak."""
    if not ctx.graph.exists:
        ui.fail("docs: graph absent — indexing failed?")
        return 1
    model = build_model(ctx, hot_rev=_head_rev(ctx))
    want = render(model)
    ddir = ctx.config.docs_dir
    theirs = [
        rel
        for rel in sorted(want)
        if (p := ddir / rel).exists()
        and not p.read_text(encoding="utf-8", errors="replace").startswith(STAMPS)
    ]
    if theirs:
        for rel in theirs:
            ui.detail(f"THEIRS {ctx.rel(str(ddir / rel))}", err=True, style="red")
        if _rescue_docs_dir(ctx, want):
            return run(ctx, diff=diff, quiet=quiet)
        ui.fail(
            "docs: those files exist but weren't generated by documate — refusing "
            "to overwrite. Point docs_dir elsewhere in documate.config.json "
            '(e.g. {"docs_dir": "docs/code"}) or move the files.'
        )
        return 1
    changed = 0
    for rel, text in sorted(want.items()):
        path = ddir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        old = path.read_text() if path.exists() else ""
        if old == text:
            continue
        # A regeneration after new commits re-mines Hotspots at HEAD, advancing the
        # pin rev even when the churn data is byte-for-byte identical — a no-op diff
        # that, once committed, moves HEAD and re-triggers next time (the pin chases
        # HEAD forever). Break the loop: when the ONLY change is the pin line, keep
        # the committed page — its existing pin still reproduces these same numbers.
        # But only if that pin still resolves: an amend/rebase can orphan it, and a
        # pin the gate can't mine at must advance to HEAD (self-heal). (No-op on
        # pages without a pin: they got here by differing for a real reason.)
        m = _PIN_RE.search(old)
        if (
            m
            and _PIN_RE.sub("", text) == _PIN_RE.sub("", old)
            and _rev_exists(ctx, m.group(1))
        ):
            continue
        changed += 1
        if diff:
            _print_diff(rel, old, text)
        path.write_text(text)
    # a renamed/deleted source file (or a layout flip across _GROUP_AT) must not leave
    # old pages behind. Prune only stamped pages — a file of theirs that happens to
    # live under architecture/ is no more ours to delete than to overwrite.
    adir = ddir / "architecture"
    for stale in sorted(adir.rglob("*.md")):
        rel = stale.relative_to(ddir).as_posix()
        if rel in want:
            continue
        try:
            ours = stale.read_text(encoding="utf-8", errors="replace").startswith(
                STAMPS
            )
        except OSError:
            ours = False
        if not ours:
            continue
        changed += 1
        if diff:
            ui.line(f"- {rel} (source gone)", style="bold red")
        stale.unlink()
    for sub in sorted((p for p in adir.rglob("*") if p.is_dir()), reverse=True):
        if not any(sub.iterdir()):
            sub.rmdir()
    for name in _agent_pointer(ctx):
        ui.ok(f"docs: agent pointer refreshed in {name}")
    stats.record(ctx, model)  # the --stats ledger: every state change gets a snapshot
    if quiet:
        return 0
    cov = model.coverage
    hue = (
        "green" if cov["percent"] >= 80 else "yellow" if cov["percent"] >= 50 else "red"
    )
    ui.result(
        (f"docs: {len(model.pages)} subsystem page(s) -> {ctx.rel(str(ddir))}", None),
        ("  |  ", "dim"),
        (f"{changed or 'no'} page(s) changed", "cyan" if changed else "dim"),
        ("  |  ", "dim"),
        (f"coverage {cov['documented']}/{cov['total']} ({cov['percent']}%)", hue),
    )
    return 0

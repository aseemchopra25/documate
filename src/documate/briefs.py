"""briefs.py — O(diff) work orders for a prose-writing model (or a human).

`documate --briefs` turns the gate's findings into self-contained files an
LLM can act on without exploring the repo — the integration surface is *a file you
hand to a model*, never a server (see notes/v2-direction.md). Three kinds:

  drift         an authored page's anchored code changed: re-verify the prose,
                edit only what the change falsified, re-pin the sig.
  undocumented  a symbol changed vs --base and has no docstring/doc-comment:
                draft one.
  module        a file has no module-level prose (the architecture page's
                section lead) — seeding scope only, never diff-driven.

Each brief packs everything the task needs: the symbol's current source, the diff
vs base, the page as committed (drift kind), the docstrings of direct callers and
callees (how the thing is *used*), and what its tests assert. Undocumented briefs
are ordered callees-first so drafted summaries compose instead of being guessed.
A `briefs.json` index beside the briefs is the machine-readable half: the wrapper
reads it, feeds each brief to the model, then re-runs `documate --check` — the gate
itself is the verification loop. Emission is O(diff): a quiet repo writes an empty
index and nothing else. Stdlib only.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import docs, drift, extract
from .core import Context
from .resolve import resolve

_SRC_CAP = 400  # lines of symbol source in a brief — a work order, not a repo pack
_DIFF_CAP = 200  # lines of diff context
_XREF_CAP = 8  # callers/callees quoted per brief, matching the docs pages

#: C-family suffixes the --rewrite scope targets. Defined in `extract` because the
#: default insert path needs the same list: Doxygen ignores plain `//`, so every
#: write into these files is a `/** */` block, rewrite or not.
_CFAMILY = extract.CFAMILY


def _slug(text: str) -> str:
    """A filename-safe slug for a page/symbol path (`docs/guides/a.md` -> `docs-guides-a.md`)."""
    return re.sub(r"[^\w.-]", "-", text)


def _fence(lang: str, text: str) -> str:
    """A 4-backtick fenced block — authored pages legitimately contain 3-backtick fences."""
    return f"````{lang}\n{text}\n````"


def _span(ctx: Context, rel: str, a, b) -> str | None:
    """The symbol's current source (1-indexed inclusive lines), capped at _SRC_CAP;
    None when the span can't be read — the brief then simply omits the section."""
    try:
        lines = (ctx.root / rel).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    if not a or not b or not (1 <= a <= b <= len(lines)):
        return None
    out = lines[a - 1 : b]
    if len(out) > _SRC_CAP:
        out = out[:_SRC_CAP] + [f"... (truncated at {_SRC_CAP} lines)"]
    return "\n".join(out)


def _diff(ctx: Context, base: str, rel: str) -> str | None:
    """Unified diff of `rel` vs merge-base(base, HEAD) — branch delta plus uncommitted,
    same change window drift uses. None when git has nothing (a sig can drift with no
    diff vs base: the code changed long ago, the pin is older still)."""
    mb = drift._git(ctx, "merge-base", base, "HEAD")
    out = drift._git(ctx, "diff", mb[0] if mb else base, "--", rel)
    if len(out) > _DIFF_CAP:
        out = out[:_DIFF_CAP] + [f"... (truncated at {_DIFF_CAP} lines)"]
    return "\n".join(out) or None


def _xrefs(ctx: Context) -> tuple[dict, dict]:
    """(callers, callees) keyed by engine-qualified name, qualified endpoints only —
    a bare target is an unresolved/stdlib call and matching it by short name
    conflates collisions (same rule as the docs pages' xref maps)."""
    callers: dict[str, set] = {}
    callees: dict[str, set] = {}
    for s, t in ctx.graph.call_edges():
        if s and t and "::" in s and "::" in t and s != t:
            callees.setdefault(s, set()).add(t)
            callers.setdefault(t, set()).add(s)
    return callers, callees


def _doc_of(ctx: Context, qualified: str) -> str | None:
    """First line of one symbol's docstring, by engine-qualified name; None when it
    has none. Quoted in a brief's used-by section so the model sees the contract of
    each neighbor, not its whole body."""
    if "::" not in qualified:
        return None
    path, tail = qualified.split("::", 1)
    rows = ctx.graph.nodes_by_name(tail.rsplit(".", 1)[-1]) or []
    line = next((ls for qn, fp, ls, le in rows if qn == qualified), None)
    if line is None:
        return None
    got = extract.extract(
        Path(path), [{"qualified": qualified, "line": line, "kind": ""}]
    )
    pair = got.get(extract.short(qualified))
    doc = pair[1] if pair else None
    return doc.strip().splitlines()[0] if doc else None


def _used(ctx: Context, qualified: str, callers: dict, callees: dict) -> list[str]:
    """The used-by bullet lines for one symbol: direct callers then callees, each
    with its doc's first line when it has one. Empty when the graph knows nothing."""
    out: list[str] = []
    for label, nbrs in (("called by", callers), ("calls", callees)):
        for q in sorted(nbrs.get(qualified, ()))[:_XREF_CAP]:
            doc = _doc_of(ctx, q)
            out.append(
                f"- {label} `{extract.short(ctx.rel(q))}`"
                + (f" — {doc}" if doc else "")
            )
    return out


def _tail_sections(
    ctx: Context, qualified: str, xrefs: tuple, tested: dict
) -> list[str]:
    """The shared trailing sections of every brief: how the symbol is used, and what
    its tests assert — evidence sections, omitted entirely when empty."""
    parts: list[str] = []
    used = _used(ctx, qualified, *xrefs)
    if used:
        parts.append("## How the symbol is used\n\n" + "\n".join(used))
    tests = tested.get(qualified, [])
    if tests:
        parts.append(
            "## What its tests assert\n\n" + "\n".join(f"- {t}" for t in tests)
        )
    return parts


def _abs_q(ctx: Context, tgt: dict) -> str:
    """A resolve target's engine-qualified (absolute-path) name, for the xref maps."""
    rq = tgt.get("qualified", "")
    tail = rq.split("::", 1)[1] if "::" in rq else tgt.get("symbol", "")
    return f"{ctx.root / tgt['file']}::{tail}"


def _drift_brief(
    ctx: Context, base: str, row: dict, xrefs: tuple, tested: dict
) -> tuple[str, dict] | None:
    """One drift work order (text, index row) from a DIRECT drift row, or None when
    the anchor no longer resolves (the anchors gate owns that failure)."""
    r = resolve(ctx, row["anchor"])
    if not r.ok or r.degraded or not r.targets:
        return None
    tgt = r.targets[0]
    page, f = row["module"], tgt["file"]
    sig = row.get("sig")
    head = [
        "# Work order: the doc may now be lying",
        "",
        f"`{page}` documents `{tgt['symbol']}` (anchor `{row['anchor']}`), and that "
        "code changed. Re-read the page against the current source below. Edit only "
        "the sentences the change falsified; keep every other line byte-identical.",
    ]
    if sig:
        head.append(
            f"Then update that anchor's pin to `sig:{sig}` on the page. Finish by "
            "running `documate --check` — it must pass."
        )
    else:
        head.append("Finish by running `documate --check` — it must pass.")
    parts = ["\n".join(head)]
    try:
        parts.append(
            f"## The page as committed ({page})\n\n"
            + _fence("markdown", (ctx.root / page).read_text(encoding="utf-8"))
        )
    except (OSError, UnicodeDecodeError):
        pass
    src = _span(ctx, f, tgt.get("line"), tgt.get("line_end"))
    if src:
        parts.append(
            f"## The code it documents ({f} lines {tgt['line']}-{tgt['line_end']})\n\n"
            + _fence(Path(f).suffix.lstrip("."), src)
        )
    diff = _diff(ctx, base, f)
    if diff:
        parts.append(f"## What changed vs {base}\n\n" + _fence("diff", diff))
    parts += _tail_sections(ctx, _abs_q(ctx, tgt), xrefs, tested)
    meta = {
        "kind": "drift",
        "page": page,
        "anchor": row["anchor"],
        "symbol": tgt["symbol"],
        "file": f,
        "line": tgt.get("line"),
        "line_end": tgt.get("line_end"),
    }
    if sig:
        meta["sig"] = sig
    return "\n\n".join(parts) + "\n", meta


def _bottom_up(rows: list[dict], callees: dict) -> list[dict]:
    """Order changed symbols callees-first, so a caller's brief is drafted after the
    summaries it composes over already exist. Cycles break deterministically."""
    order: list[dict] = []
    placed: set[str] = set()
    pending = sorted(rows, key=lambda r: r["qualified"])
    quals = {r["qualified"] for r in pending}
    while pending:
        ready = [
            r
            for r in pending
            if not (callees.get(r["qualified"], set()) & (quals - placed))
            - {r["qualified"]}
        ]
        if not ready:
            ready = [pending[0]]
        for r in ready:
            order.append(r)
            placed.add(r["qualified"])
        pending = [r for r in pending if r["qualified"] not in placed]
    return order


def _no_doc(ctx: Context, rows: list[dict]) -> list[dict]:
    """The subset of symbol rows that carry no docstring/doc-comment, checked
    per file through the same extractor the docs pages read."""
    by_file: dict[str, list[dict]] = {}
    for s in rows:
        by_file.setdefault(s["file"], []).append(s)
    out: list[dict] = []
    for abs_file, members in by_file.items():
        prose = extract.extract(Path(abs_file), members)
        for s in members:
            pair = prose.get(extract.short(s["qualified"]))
            if not (pair and pair[1]):
                out.append(s)
    return out


def _undoc_briefs(
    ctx: Context, base: str, xrefs: tuple, tested: dict, scope: str
) -> list[tuple[str, dict]]:
    """(text, index row) per undocumented Function/Class — the 'make documentation'
    half. scope='changed' keys on the diff vs base (`check --fix`, O(diff));
    scope='all' walks every graph symbol (`docs --fix`, the fresh-repo seeding
    pass, where a diff section would be noise and is omitted). Empty without a
    graph."""
    if scope == "all":
        cand = ctx.graph.symbols()
    else:
        cand = [
            s
            for s in ctx.graph.changed_symbols(base)
            if s["kind"] in ("Function", "Class")
        ]
    cand = [
        s
        for s in cand
        if not docs._skip(ctx, ctx.rel(s["file"]))
        and not docs._machine_generated(Path(s["file"]))
    ]
    undocumented = _no_doc(ctx, cand)
    out: list[tuple[str, dict]] = []
    for s in _bottom_up(undocumented, xrefs[1]):
        rel = ctx.rel(s["file"])
        node = next(
            (
                r
                for r in ctx.graph.nodes_by_name(s["name"]) or []
                if r[0] == s["qualified"]
            ),
            None,
        )
        line, line_end = (node[2], node[3]) if node else (s["line"], None)
        why = (
            "is undocumented"
            if scope == "all"
            else f"changed vs {base} and has no docstring/doc-comment"
        )
        parts = [
            "# Work order: document a symbol\n\n"
            f"`{extract.short(s['qualified'])}` ({s['kind']}) in `{rel}` {why}. "
            f"Write its docstring/doc-comment at line {line}, in the language's "
            "own convention: what it does and any contract a caller must know — "
            "only what the source below shows, never invented."
        ]
        src = _span(ctx, rel, line, line_end)
        if src:
            parts.append(
                f"## The code ({rel} lines {line}-{line_end})\n\n"
                + _fence(Path(rel).suffix.lstrip("."), src)
            )
        if scope != "all":
            diff = _diff(ctx, base, rel)
            if diff:
                parts.append(f"## What changed vs {base}\n\n" + _fence("diff", diff))
        parts += _tail_sections(ctx, s["qualified"], xrefs, tested)
        meta = {
            "kind": "undocumented",
            "symbol": extract.short(s["qualified"]),
            "file": rel,
            "line": line,
            "line_end": line_end,
        }
        out.append(("\n\n".join(parts) + "\n", meta))
    return out


def _rewrite_briefs(ctx: Context, xrefs: tuple, tested: dict) -> list[tuple[str, dict]]:
    """(text, index row) per C-family Function/Class — the `--rewrite` scope. Every
    C/C++ symbol gets a work order to (re)write its doc comment as Doxygen: the
    current doc (when any) and the source are quoted so the model improves on what's
    there rather than inventing. Rows sort by (file, line) so the inserter, running a
    file's symbols top-to-bottom, keeps its line-shift bookkeeping coherent. Empty
    without a graph or without C sources."""
    cand = [
        s
        for s in ctx.graph.symbols()
        if Path(s["file"]).suffix in _CFAMILY
        and not docs._skip(ctx, ctx.rel(s["file"]))
        and not docs._machine_generated(Path(s["file"]))
    ]
    out: list[tuple[str, dict]] = []
    for s in sorted(cand, key=lambda s: (s["file"], s["line"] or 0)):
        rel = ctx.rel(s["file"])
        node = next(
            (
                r
                for r in ctx.graph.nodes_by_name(s["name"]) or []
                if r[0] == s["qualified"]
            ),
            None,
        )
        line, line_end = (node[2], node[3]) if node else (s["line"], None)
        prose = extract.extract(
            Path(s["file"]),
            [{"qualified": s["qualified"], "line": line, "kind": s["kind"]}],
        )
        pair = prose.get(extract.short(s["qualified"]))
        cur = pair[1] if pair else None
        parts = [
            "# Work order: rewrite as Doxygen documentation\n\n"
            f"`{extract.short(s['qualified'])}` ({s['kind']}) in `{rel}`. (Re)write its "
            f"doc comment at line {line} as Doxygen — a `@brief` line, then one "
            "`@param <name>` per parameter and a `@return` when it returns a value — "
            "improving on any current doc below; only what the source proves, never invented."
        ]
        if cur:
            parts.append("## Current documentation\n\n" + _fence("text", cur))
        src = _span(ctx, rel, line, line_end)
        if src:
            parts.append(
                f"## The code ({rel} lines {line}-{line_end})\n\n"
                + _fence(Path(rel).suffix.lstrip("."), src)
            )
        parts += _tail_sections(ctx, s["qualified"], xrefs, tested)
        meta = {
            "kind": "rewrite",
            "symbol": extract.short(s["qualified"]),
            "file": rel,
            "line": line,
            "line_end": line_end,
        }
        out.append(("\n\n".join(parts) + "\n", meta))
    return out


def _module_briefs(ctx: Context) -> list[tuple[str, dict]]:
    """(text, index row) per module with no module-level prose — the top-of-file
    doc each architecture-page section leads with. Seeding-scope only: module
    prose is repo furniture, not diff work, so the check path never drafts it.
    These sort after the symbol orders, so in a batched prompt the model has
    just written the file's docstrings when it summarizes the file."""
    by_file: dict[str, list[dict]] = {}
    for s in ctx.graph.symbols():
        if not docs._skip(ctx, ctx.rel(s["file"])) and not docs._machine_generated(
            Path(s["file"])
        ):
            by_file.setdefault(s["file"], []).append(s)
    out: list[tuple[str, dict]] = []
    for abs_file, members in sorted(by_file.items()):
        members.sort(key=lambda s: s["line"] or 0)
        path = Path(abs_file)
        if extract.module_doc(path, members[0]["line"]):
            continue
        rel = ctx.rel(abs_file)
        prose = extract.extract(path, members)
        api = []
        for s in members:
            pair = prose.get(extract.short(s["qualified"]))
            sig = (pair and pair[0]) or extract.short(s["qualified"])
            doc = ((pair and pair[1]) or "").strip().splitlines()
            api.append(f"- `{sig}` — {doc[0] if doc else '(undocumented)'}")
        if rel.endswith(".go"):
            m = re.search(
                r"^package\s+(\w+)",
                path.read_text(encoding="utf-8", errors="replace"),
                re.M,
            )
            hint = (
                f"Go package comment: the first line begins `Package {m.group(1)} ` "
                "and the block sits directly above the `package` clause."
                if m
                else "Go package comment, directly above the `package` clause."
            )
        else:
            hint = (
                "Top-of-file module documentation in the language's own "
                "convention (Python: a docstring as the first statement)."
            )
        parts = [
            "# Work order: document a module\n\n"
            f"`{rel}` has no module-level documentation — the architecture page "
            "has nothing to lead its section with. Write it at the top of the "
            "file: 1-3 sentences on what the module is for and how its pieces "
            "fit — only what the API below proves, never invented.",
            f"## What to write\n\n{hint}",
            "## The module's API\n\n" + "\n".join(api),
        ]
        # line = the first symbol's, so the insert side can re-run the same
        # module_doc disambiguation (top comment adjacent to it is the
        # symbol's doc, not the module's)
        meta = {
            "kind": "module",
            "symbol": "module",
            "file": rel,
            "line": members[0]["line"],
        }
        out.append(("\n\n".join(parts) + "\n", meta))
    return out


def emit(
    ctx: Context,
    base: str,
    direct: list[dict],
    out_dir: Path,
    undocumented: str = "changed",
    rewrite: bool = False,
) -> list[dict]:
    """Write one work-order file per finding into `out_dir` plus a `briefs.json`
    index (the machine-readable half), clearing briefs from earlier runs first so a
    fixed finding can't linger as stale work. `undocumented` picks the doc-drafting
    scope: 'changed' (vs base — the check path) or 'all' (every graph symbol — the
    fresh-repo seeding path). `rewrite` swaps all of that for the C-family rewrite
    scope: one work order per C/C++ symbol to re-emit its doc comment as Doxygen.
    Returns the index rows; a green repo returns [] and the directory holds only an
    empty index — the wrapper's definitive 'nothing to do'."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("*.md"):
        old.unlink()
    xrefs = _xrefs(ctx)
    tested = docs._tested(ctx, ctx.graph.symbols())
    orders: list[tuple[str, dict]] = []
    if rewrite:
        orders += _rewrite_briefs(ctx, xrefs, tested)
    else:
        for row in direct:
            got = _drift_brief(ctx, base, row, xrefs, tested)
            if got:
                orders.append(got)
        orders += _undoc_briefs(ctx, base, xrefs, tested, undocumented)
        if undocumented == "all":  # module prose is seeded, never diff-driven
            orders += _module_briefs(ctx)
    index: list[dict] = []
    for text, meta in orders:
        name = f"{meta['kind']}--{_slug(meta.get('page') or meta['file'])}--{_slug(meta['symbol'])}.md"
        (out_dir / name).write_text(text, encoding="utf-8")
        index.append({"brief": name, **meta})
    (out_dir / "briefs.json").write_text(
        json.dumps(index, indent=2) + "\n", encoding="utf-8"
    )
    return index

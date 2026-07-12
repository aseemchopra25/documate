"""drift.py — flag docs that describe code which just changed.

The engine behind `check`'s third gate. The anchor index says which authored page
documents which symbol; the resolver maps each anchor to its file; git says what
changed. Intersect: a documented file changed but its page didn't → the prose may now
be lying.

    changed = (branch vs base) ∪ (working tree + staged)

Two tiers:
  DIRECT  the documented *symbol's* code changed. Gates.
  RIPPLE  the documented symbol didn't change, but it calls a symbol defined in a file
          that did (graph-backed, bounded). Advisory only — never gates, silent without
          a graph. A weaker signal shouldn't block a push.

Both tiers share ONE oracle: an AST fingerprint of the symbol's source (formatting-
invariant, literal-sensitive — see `fingerprint`). A sig-less anchor compares that
fingerprint between the merge-base and the working tree, so pure formatter churn and
edits to *other* symbols in the same file never flag — only the documented symbol
changing does. An anchor pinned with `sig:` compares the same fingerprint against an
author-verified value instead of the base, and a mismatch is DIRECT drift whose message
carries the current sig so the author can re-verify the prose and re-pin. The idea is
fiberplane/drift's AST fingerprint; the sig lives inline in the anchor, not a lock file.

git supplies the cheap pre-filter (which files differ from base) and the base blob;
the gate *decision* for sig-less anchors is the fingerprint compare, not file
membership. `sym:` needs the graph and degrades without it. Stdlib only.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .anchors import scan
from .core import Context
from .resolve import resolve


def fingerprint(ctx: Context, rel: str, line_start, line_end) -> str | None:
    """16-hex AST fingerprint of the symbol spanning lines `line_start..line_end` of `rel`.

    Reads the working-tree file, slices the span, and hands it to the graph adapter,
    which parses it with the indexing engine and serialises the syntax tree. The digest
    is invariant to reindentation, re-wrapping, blank lines, trailing whitespace, and
    spacing-only edits (`x=1` == `x = 1`, `f( a,b )` == `f(a, b)`), but sensitive to
    signatures, control flow, operators, called names, and the exact contents of
    string/char/numeric literals (`"a  b"` != `"a b"`, `1_000` != `1000`). A comment-only
    edit does NOT change it (the engine drops comment/trivia nodes by default). None when
    the span can't be read or parsed — the caller degrades to a note and never gates on a
    broken oracle.

    Same 16-hex shape as before, so committed `sig:` pins keep their *format*. The
    algorithm changed (was a whitespace-collapsed line hash, which both over-flagged
    `x=1`->`x = 1` and silently missed edits inside string literals), so existing pin
    *values* changed once — re-pin from the sig `documate --check` prints."""
    try:
        lines = (ctx.root / rel).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    if (
        not line_start
        or not line_end
        or not (1 <= line_start <= line_end <= len(lines))
    ):
        return None
    span = "\n".join(lines[line_start - 1 : line_end])
    return ctx.graph.fingerprint_source(rel, span)


def _git(ctx: Context, *args: str) -> list[str]:
    """Run git against the repo root, returning non-blank stdout lines (empty on error)."""
    out = subprocess.run(
        ["git", "-C", str(ctx.root), *args], capture_output=True, text=True
    )
    return [ln for ln in out.stdout.splitlines() if ln.strip()]


def _merge_base(ctx: Context, base: str) -> str:
    """The base<->HEAD merge-base — the "before" snapshot the change set diffs against,
    and where the sig-less fingerprint compare reads the symbol's old source. Falls back
    to HEAD when there is no merge-base (base is an ancestor of / equals HEAD)."""
    mb = _git(ctx, "merge-base", base, "HEAD")
    return mb[0] if mb else "HEAD"


def _show(ctx: Context, ref: str, rel: str) -> bytes | None:
    """Raw bytes of `rel` at `ref` (`git show ref:rel`), or None when it didn't exist
    there (a file newly added since base) or git errors."""
    out = subprocess.run(
        ["git", "-C", str(ctx.root), "show", f"{ref}:{rel}"], capture_output=True
    )
    return out.stdout if out.returncode == 0 else None


def changed_files(ctx: Context, base: str) -> set[str]:
    """Repo-relative paths differing from `base`: branch delta ∪ uncommitted."""
    files: set[str] = set()
    mb = _git(ctx, "merge-base", base, "HEAD")
    if mb:
        files.update(_git(ctx, "diff", "--name-only", mb[0], "HEAD"))
    files.update(_git(ctx, "diff", "--name-only", "HEAD"))
    files.update(_git(ctx, "diff", "--name-only", "--cached"))
    return files


def dependent_files(
    ctx: Context, changed_rel: set[str], hops: int, cap: int
) -> tuple[set[str], bool]:
    """Files whose symbols (up to `hops`) call/reference a symbol defined in a changed
    file — the ripple set. Bounded by hops + cap with a truncated flag. Empty + False
    without a graph (ripple degrades to nothing, never blocks)."""
    if hops <= 0 or not changed_rel or not ctx.graph.exists:
        return set(), False
    abs_changed = [str(ctx.root / p) for p in changed_rel]
    frontier: set[str] = set()
    for name, qual in ctx.graph.symbols_in_files(abs_changed):
        if name:
            frontier.add(name)
        if qual:
            frontier.add(qual)
    seen = set(frontier)
    dep: set[str] = set()
    truncated = False
    for _ in range(hops):
        if not frontier or truncated:
            break
        nxt: set[str] = set()
        for sq in ctx.graph.reverse_sources(frontier):
            abspath = sq.split("::", 1)[0]
            if not abspath.startswith(str(ctx.root)):
                continue
            rel = Path(abspath).relative_to(ctx.root).as_posix()
            if rel not in changed_rel:
                dep.add(rel)
            if sq not in seen:
                seen.add(sq)
                nxt.add(sq)
            if "::" in sq:
                bare = sq.rsplit("::", 1)[1]
                if bare not in seen:
                    seen.add(bare)
                    nxt.add(bare)
            if len(dep) >= cap:
                truncated = True
                break
        frontier = nxt
    return dep, truncated


def _collapse(rows: list[dict]) -> list[dict]:
    """Dedup drift rows to one per (page, file) pair, sorted by page."""
    seen, out = set(), []
    for d in rows:
        k = (d["module"], d["file"])
        if k not in seen:
            seen.add(k)
            out.append(d)
    return sorted(out, key=lambda r: r["module"])


def _symbol_drifted(
    ctx: Context,
    mb: str,
    base: str,
    rel: str,
    name,
    line,
    line_end,
    anchor: str,
    notes: list[str],
) -> bool:
    """Did the documented symbol's code change between `mb` and the working tree?

    Compares the symbol's AST fingerprint on each side: the working span (by its current
    line range) against the same symbol located *by name* in the base blob (line-shift
    proof). Formatter-only churn and edits to other symbols in the file therefore don't
    flag. Degrades to a note (returns False) when either side can't be read/parsed — a
    broken oracle never gates."""
    cur = fingerprint(ctx, rel, line, line_end)
    if cur is None:
        notes.append(f"{anchor}: working span unreadable ({rel})")
        return False
    blob = _show(ctx, mb, rel)
    if blob is None:
        notes.append(
            f"{anchor}: {name} has no source at {base} (new since base) — not compared"
        )
        return False
    old = ctx.graph.fingerprint_symbol(rel, blob, name)
    if old is None:
        notes.append(
            f"{anchor}: {name} absent/unparseable in {rel} at {base} — not compared"
        )
        return False
    return old != cur


def find_drift(ctx: Context, base: str, ripple_hops: int, ripple_cap: int):
    """Return (direct rows, ripple rows, notes, truncated), deduped by (page, file)."""
    changed = changed_files(ctx, base)
    mb = _merge_base(ctx, base)  # the "before" snapshot for sig-less symbol compares
    index, sigs, _ = scan(ctx)  # bad sig tokens are anchors.validate's failure
    ripple, truncated = dependent_files(ctx, changed, ripple_hops, ripple_cap)
    direct: list[dict] = []
    rippled: list[dict] = []
    notes: list[str] = []
    for anchor, modules in index.items():
        r = resolve(ctx, anchor)
        if r.degraded:
            notes.append(f"{anchor}: {r.error}")
            continue
        if not r.ok:
            notes.append(
                f"{anchor}: unresolved ({r.error}) — `documate --check` flags it"
            )
            continue
        for tgt in r.targets:
            f = tgt.get("file")
            if not f:
                continue
            for m in modules:
                sig = sigs.get((anchor, m))
                if sig:
                    # pinned: the fingerprint is the oracle — git, ripple, and
                    # whether the page itself changed are all irrelevant.
                    cur = fingerprint(ctx, f, tgt.get("line"), tgt.get("line_end"))
                    if cur is None:
                        notes.append(
                            f"{anchor}: sig unverifiable ({f} span unreadable)"
                        )
                    elif cur != sig:
                        direct.append(
                            {"anchor": anchor, "file": f, "module": m, "sig": cur}
                        )
                    continue
                if m in changed:
                    continue  # author touched the doc alongside the code — not drift
                if f in changed:
                    # sig-less DIRECT: compare the documented *symbol* base->worktree,
                    # not the whole file — formatter churn and edits to other symbols
                    # in the same file don't flag.
                    if _symbol_drifted(
                        ctx,
                        mb,
                        base,
                        f,
                        tgt.get("symbol"),
                        tgt.get("line"),
                        tgt.get("line_end"),
                        anchor,
                        notes,
                    ):
                        direct.append({"anchor": anchor, "file": f, "module": m})
                elif f in ripple:
                    rippled.append({"anchor": anchor, "file": f, "module": m})
    return _collapse(direct), _collapse(rippled), notes, truncated

"""drift.py — flag docs that describe code which just changed.

The engine behind `check`'s third gate. The anchor index says which authored page
documents which symbol; the resolver maps each anchor to its file; git says what
changed. Intersect: a documented file changed but its page didn't → the prose may now
be lying.

    changed = (branch vs base) ∪ (working tree + staged)

Two tiers:
  DIRECT  the documented file itself changed. Gates.
  RIPPLE  the documented file didn't change, but it calls a symbol defined in one that
          did (graph-backed, bounded). Advisory only — never gates, silent without a
          graph. A weaker signal shouldn't block a push.

git is the change oracle for sig-less anchors — no stored hashes. An anchor pinned
with `sig:` opts out of git entirely: its verdict is a fingerprint comparison against
the symbol's current source (per-symbol, base-ref-free, indifferent to unrelated edits
in the same file), and a mismatch is DIRECT drift whose message carries the current
sig so the author can re-verify the prose and re-pin. The idea is fiberplane/drift's
AST fingerprint; the sig lives inline in the anchor instead of a lock file.
`sym:` needs the graph and degrades without it. Stdlib only.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from .anchors import scan
from .core import Context
from .resolve import resolve


def fingerprint(ctx: Context, rel: str, line_start, line_end) -> str | None:
    """16-hex fingerprint of a symbol's source span, whitespace-run-insensitive.

    Collapsing whitespace runs makes re-indents and re-wraps hash-stable while
    keeping token boundaries, so `"a  b"` inside a string still differs from
    `"a b"`. Spacing-only formatter churn (`x=1` -> `x = 1`) does flag — the
    tool over-flags rather than silently passes a lying doc; a true syntax-tree
    hash at index time is the upgrade path. None when the span can't be read
    (caller degrades to a note, never gates on a broken oracle)."""
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
    norm = " ".join("\n".join(lines[line_start - 1 : line_end]).split())
    return hashlib.blake2b(norm.encode("utf-8"), digest_size=8).hexdigest()


def _git(ctx: Context, *args: str) -> list[str]:
    """Run git against the repo root, returning non-blank stdout lines (empty on error)."""
    out = subprocess.run(
        ["git", "-C", str(ctx.root), *args], capture_output=True, text=True
    )
    return [ln for ln in out.stdout.splitlines() if ln.strip()]


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


def find_drift(ctx: Context, base: str, ripple_hops: int, ripple_cap: int):
    """Return (direct rows, ripple rows, notes, truncated), deduped by (page, file)."""
    changed = changed_files(ctx, base)
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
                if f in changed:
                    bucket = direct
                elif f in ripple:
                    bucket = rippled
                else:
                    continue
                if m not in changed:
                    bucket.append({"anchor": anchor, "file": f, "module": m})
    return _collapse(direct), _collapse(rippled), notes, truncated

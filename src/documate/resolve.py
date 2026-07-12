"""resolve.py — turn a doc anchor into the concrete code it names, or fail loudly.

A doc module declares what code it describes with anchors. This resolves one anchor to
its real target, or fails when the target is gone (renamed/deleted = the doc now lies).
Keystone the anchor validation and the drift gate both hang off.

One namespace:

  sym:NAME              a function/class, resolved against the graph (via the adapter).
  sym:NAME@repo/rel.c   ~10% of names collide; add @repo-rel-path to disambiguate.

`sym:` DEGRADES (soft pass) when the graph is absent/locked — you can't gate on an
ephemeral artifact. Stdlib only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .core import Context

ANCHOR_RE = re.compile(r"^sym:(.+)$")
SYM_RE = re.compile(r"^([^@]+?)(?:@(.+))?$")


@dataclass
class Resolution:
    """Outcome of resolving one anchor.

    ok        resolved to exactly what it should.
    degraded  a sym: couldn't be verified (graph absent/locked) — soft pass, never a
              real failure.
    targets   the concrete code named (repo-relative).
    error     one-line reason when ok is False.
    """

    anchor: str
    kind: str = ""
    ok: bool = False
    degraded: bool = False
    targets: list[dict] = field(default_factory=list)
    error: str | None = None


def _skip(ctx: Context, path: str) -> bool:
    """True when a resolved path sits in a skip_dir — never source-of-truth for an anchor."""
    return any(m in path for m in ctx.config.skip_dirs)


def resolve_sym(ctx: Context, value: str) -> Resolution:
    """Resolve a sym: anchor through the graph; soft-pass (degraded) when the graph is absent or locked."""
    anchor = f"sym:{value}"
    m = SYM_RE.match(value)
    if not m or not m.group(1).strip():
        return Resolution(anchor, "sym", error="malformed sym anchor (empty name)")
    name, path_q = m.group(1).strip(), (m.group(2) or "").strip()

    rows = ctx.graph.nodes_by_name(name)
    if rows is None:
        return Resolution(
            anchor,
            "sym",
            ok=True,
            degraded=True,
            error="graph absent/locked — sym not verified (run `documate`)",
        )

    cands = []
    for qn, fp, line, line_end in rows:
        rel_file = ctx.rel(fp)
        if _skip(ctx, rel_file):
            continue
        cands.append(
            {
                "symbol": name,
                "qualified": ctx.rel(qn),
                "file": rel_file,
                "line": line,
                "line_end": line_end,
            }
        )

    # Prefer a production site over a test-file mock when both carry the name.
    prod = [
        c for c in cands if not any(t in c["file"] for t in ctx.config.test_markers)
    ]
    if prod:
        cands = prod
    if path_q:
        cands = [
            c for c in cands if c["file"] == path_q or c["file"].endswith("/" + path_q)
        ]

    if not cands:
        hint = f" at {path_q}" if path_q else ""
        return Resolution(
            anchor, "sym", error=f"no symbol named '{name}'{hint} (renamed/deleted?)"
        )
    if len(cands) > 1:
        files = ", ".join(sorted(c["file"] for c in cands)[:6])
        return Resolution(
            anchor,
            "sym",
            error=f"'{name}' is ambiguous ({len(cands)} sites: {files}) — add @path",
        )
    return Resolution(anchor, "sym", ok=True, targets=cands)


def resolve(ctx: Context, anchor: str) -> Resolution:
    """Resolve one sym: anchor, or fail on unknown syntax."""
    m = ANCHOR_RE.match(anchor.strip())
    if not m:
        return Resolution(anchor, error=f"unknown anchor syntax: '{anchor}'")
    return resolve_sym(ctx, m.group(1))


def resolve_many(ctx: Context, anchors) -> list[Resolution]:
    """Resolve a batch of anchors, preserving order."""
    return [resolve(ctx, a) for a in anchors]

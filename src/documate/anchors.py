"""anchors.py — scan authored docs for `documents:` anchors and validate them.

An authored page (hand-written markdown under docs/) declares what code it describes:

    <!-- documents: sym:unlock -->               (prose, invisible HTML comment)
    <!-- documents: sym:unlock sig:0f3a9c1b2d4e5f60 -->   (pinned to a fingerprint)
    %% documents: sym:ble_handler                 (inside a mermaid block)

`build_index` scans every .md under docs_dir into `{anchor: [pages]}` — graph-free and
deterministic, computed fresh each run (nothing to commit or keep in sync). Generated
pages carry no anchors (their freshness is checked by regeneration instead), so this
is effectively the authored tier's map. `validate` resolves each anchor to confirm the
code it names still exists; a sym: against a missing graph degrades to a warning.

A `sig:` token pins the *preceding* sym: to the fingerprint of the code the author
verified the prose against (drift prints the current value). With a sig, drift for
that page/anchor is decided by fingerprint comparison instead of file-level git diff —
per-symbol and base-ref-free. The sig lives inline in the anchor, never in a lock
file: it travels with the prose it protects and updates in the same edit.
Stdlib only.
"""

from __future__ import annotations

import re

from .core import STAMPS, Context
from .resolve import resolve

HTML_MARK_RE = re.compile(r"<!--\s*documents:\s*(.*?)\s*-->", re.DOTALL)
MERMAID_MARK_RE = re.compile(r"%%\s*documents:\s*(.*)$", re.MULTILINE)
ANCHOR_TOKEN_RE = re.compile(r"sym:\S+")
SIG_TOKEN_RE = re.compile(r"^sig:([0-9a-f]{16})$")


def scan(ctx: Context) -> tuple[dict[str, list[str]], dict[tuple, str], list[tuple]]:
    """One pass over every authored page → (index, sigs, bad).

    index  {anchor: sorted[pages]} — as `build_index` always returned.
    sigs   {(anchor, page): sig} — the fingerprint pins, one per sym token.
    bad    [(page, token, reason)] — malformed/orphaned/conflicting sig tokens;
           loud, because a sig that silently never matches would flag forever
           and one that silently binds wrong would never flag."""
    index: dict[str, set[str]] = {}
    sigs: dict[tuple, str] = {}
    bad: list[tuple] = []
    for md in sorted(ctx.config.docs_dir.rglob("*.md")):
        text = md.read_text()
        if text.startswith(STAMPS):
            continue  # generated tier: freshness-checked by regeneration, never scanned
        rel = md.relative_to(ctx.root).as_posix()
        for g in HTML_MARK_RE.findall(text) + MERMAID_MARK_RE.findall(text):
            last_sym = None
            for token in g.split():
                if ANCHOR_TOKEN_RE.fullmatch(token):
                    index.setdefault(token, set()).add(rel)
                    last_sym = token
                elif token.startswith("sig:"):
                    m = SIG_TOKEN_RE.match(token)
                    if not m:
                        bad.append((rel, token, "malformed sig (want 16 hex chars)"))
                    elif last_sym is None:
                        bad.append((rel, token, "sig with no sym: before it"))
                    elif sigs.get((last_sym, rel), m.group(1)) != m.group(1):
                        bad.append(
                            (
                                rel,
                                token,
                                f"conflicting sigs for {last_sym} on this page",
                            )
                        )
                    else:
                        sigs[(last_sym, rel)] = m.group(1)
    return {a: sorted(mods) for a, mods in sorted(index.items())}, sigs, bad


def build_index(ctx: Context) -> dict[str, list[str]]:
    """Scan every doc page for anchor comments → `{anchor: sorted[pages]}`. Graph-free,
    deterministic: same .md tree in, same dict out, anywhere."""
    return scan(ctx)[0]


def validate(ctx: Context) -> tuple[list[tuple], list[tuple]]:
    """Resolve every declared anchor against real code.

    Returns (failed, degraded): lists of (anchor, error, pages). A failed anchor names
    a ghost (renamed/deleted code — the doc lies) or carries a broken sig token; a
    degraded one just couldn't be verified (graph absent/locked) and must never gate."""
    index, _, bad = scan(ctx)
    failed = [(token, reason, [page]) for page, token, reason in bad]
    degraded = []
    for anchor, pages in index.items():
        r = resolve(ctx, anchor)
        if r.ok and not r.degraded:
            continue
        (degraded if r.degraded else failed).append((anchor, r.error, pages))
    return failed, degraded

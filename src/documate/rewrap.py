"""rewrap.py — reflow doc comments already in the source to `doc_width`.

`documate --rewrap-docs` is the no-model half of the wrapping story. `--ai` wraps
what it writes, but docs drafted by an earlier version (or by hand, or by another
tool) sit there one long line each, and a repo with a column limit rejects them.
This pass fixes them with pure text work: no model, no tokens, no network.

Only a doc comment carrying a line over the limit is touched — one already
inside it comes out byte-identical, so a sweep can't quietly reformat a repo's
hand-written prose. Two shapes are understood, the same two `prose` writes:
a `/** ... */` block (C family) and a run of line comments (`//`, `--`, `#`).
A Lua `--[[ long comment ]]` ends a run rather than joining it: reflowing one
would move its delimiters. The pass is idempotent, and every write goes into the
run manifest, so `documate --undo` takes it back. Stdlib only.
"""

from __future__ import annotations

import re
from pathlib import Path

from . import docs, extract, ui, undo
from .core import Context
from .prose import _comment_prefix, _only, _rewrap


def _fits(span: list[str], width: int) -> bool:
    """True when every line already fits — tabs counted as the 8 columns a format
    gate counts them as, not as one character."""
    return all(len(ln.expandtabs(8)) <= width for ln in span)


def _room(ind: str, width: int, lead: int) -> int:
    """Columns left for text after the indentation and a `lead`-wide marker."""
    return max(width - len(ind.expandtabs(8)) - lead, 20)


def _blocks(lines: list[str]) -> list[tuple[int, int]]:
    """(start, end) of every `/** ... */` doc block, 0-indexed inclusive."""
    out: list[tuple[int, int]] = []
    i = 0
    while i < len(lines):
        if lines[i].strip().startswith("/**"):
            j = i
            while j < len(lines) and "*/" not in lines[j]:
                j += 1
            if j < len(lines):
                out.append((i, j))
            i = j + 1
        else:
            i += 1
    return out


def _in_run(stripped: str, prefix: str) -> bool:
    """True when the line continues a line-comment doc run. A Lua long-comment
    opener ends the run instead of joining it."""
    return stripped.startswith(prefix) and not stripped.startswith("--[[")


def _runs(lines: list[str], prefix: str) -> list[tuple[int, int]]:
    """(start, end) of every contiguous line-comment run, 0-indexed inclusive."""
    out: list[tuple[int, int]] = []
    i = 0
    while i < len(lines):
        if _in_run(lines[i].strip(), prefix):
            j = i
            while j + 1 < len(lines) and _in_run(lines[j + 1].strip(), prefix):
                j += 1
            out.append((i, j))
            i = j + 1
        else:
            i += 1
    return out


def _rewrap_blocks(lines: list[str], width: int) -> int:
    """Reflow every over-long `/** */` block in place. Returns how many changed."""
    changed = 0
    for start, end in reversed(_blocks(lines)):
        span = lines[start : end + 1]
        if _fits(span, width):
            continue
        ind = re.match(r"[ \t]*", lines[start]).group(0)
        body: list[str] = []
        for ln in span:
            t = ln.strip()
            t = t.removeprefix("/**").removeprefix("/*").removesuffix("*/").strip()
            body.append(t[1:].strip() if t.startswith("*") else t)
        while body and not body[0]:
            body.pop(0)
        while body and not body[-1]:
            body.pop()
        new = [f"{ind}/**"]
        new += [f"{ind} * {ln}".rstrip() for ln in _rewrap(body, _room(ind, width, 3))]
        new.append(f"{ind} */")
        if new != span:
            lines[start : end + 1] = new
            changed += 1
    return changed


def _rewrap_runs(lines: list[str], width: int, prefix: str) -> int:
    """Reflow every over-long line-comment run in place. Returns how many changed."""
    changed = 0
    for start, end in reversed(_runs(lines, prefix)):
        span = lines[start : end + 1]
        if _fits(span, width) or span[0].strip().startswith("#!"):
            continue  # a shebang is a directive, not prose
        ind = re.match(r"[ \t]*", lines[start]).group(0)
        body = [ln.strip().removeprefix(prefix).strip() for ln in span]
        room = _room(ind, width, len(prefix) + 1)
        new = [f"{ind}{prefix} {ln}".rstrip() for ln in _rewrap(body, room)]
        if new != span:
            lines[start : end + 1] = new
            changed += 1
    return changed


def rewrap_file(path: Path, width: int) -> tuple[str | None, int]:
    """(new text, comments changed) for one file; (None, 0) when it can't be read
    or nothing overflows — an unreadable file is skipped, never half-written."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None, 0
    lines = text.splitlines()
    # C family documents in `/** */` blocks whatever `_comment_prefix` says;
    # every other doc-above language documents in a run of its line marker.
    if path.suffix in extract.CFAMILY:
        changed = _rewrap_blocks(lines, width)
    else:
        prefix = _comment_prefix(path.name)
        changed = _rewrap_runs(lines, width, prefix) if prefix else 0
    if not changed:
        return None, 0
    return "\n".join(lines) + ("\n" if text.endswith("\n") else ""), changed


def run(ctx: Context, only: str | None = None, dry: bool = False) -> int:
    """`documate --rewrap-docs`: reflow over-long doc comments in every tracked
    source file documate would document, then regenerate the pages the change
    moves. `only` narrows to a file glob, `dry` reports without writing. Always
    exit 0 — nothing here can fail a build; a repo with nothing to reflow says so
    and stops."""
    width = ctx.config.doc_width
    rels = sorted(
        {
            ctx.rel(s["file"])
            for s in ctx.graph.symbols()
            if not docs._skip(ctx, ctx.rel(s["file"]))
            and not docs._machine_generated(Path(s["file"]))
        }
    )
    rels = [r["file"] for r in _only([{"file": rel} for rel in rels], only)]
    before: dict[str, str] = {}
    total = 0
    for rel in rels:
        path = ctx.root / rel
        new, n = rewrap_file(path, width)
        if not n or new is None:
            continue
        total += n
        if not dry:
            before[rel] = path.read_text(encoding="utf-8")
            path.write_text(new, encoding="utf-8")
        ui.note(f"{'would rewrap' if dry else 'rewrapped'}  {rel}  ({n} comment(s))")
    if not total:
        ui.ok(f"rewrap: every doc comment already fits {width} columns")
        return 0
    if dry:
        ui.ok(
            f"rewrap: --dry-run — {total} comment(s) in {len(before) or 'the'} "
            "file(s) would be reflowed; nothing written"
        )
        return 0
    undo.record(ctx, before, [], "rewrap", "-")
    ui.ok(f"rewrap: {total} comment(s) in {len(before)} file(s) -> {width} columns")
    ctx.graph.index(incremental=True)  # docstring text moved: the pages quote it
    return docs.run(ctx, quiet=True)

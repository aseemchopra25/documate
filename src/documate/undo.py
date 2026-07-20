"""undo.py — the --ai run manifest, and `documate --undo` to revert it.

Model output is indistinguishable from hand-written prose once it lands, which is
what makes reviewing (and unpicking) a big run slow. Two answers, neither of which
marks the files themselves — nothing documate writes into a repo names the tool:

  record   every --ai run leaves `.documate/last-run.json`: mode, model, which
           file:symbol pairs were drafted, and per touched file the full
           before-text plus a hash of what the run left behind.
  undo     `documate --undo` restores each recorded file's before-text — but only
           when its current content still hashes to what the run left. A file
           edited since is refused, file by file, and stays in the manifest; git
           remains the real undo once drafts are committed, this one works before
           any commit exists.

Records from the same process merge (bare `--ai` chains a seeding pass into a
repair pass — one invocation, one manifest); a new invocation replaces the
manifest, so `--undo` always means "the last `--ai` run". Stdlib only.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from . import ui
from .core import Context


def _path(ctx: Context) -> Path:
    """The manifest's home, next to the graph and the briefs."""
    return ctx.root / ".documate" / "last-run.json"


def _sha(text: str) -> str:
    """Content hash used to check a file hasn't moved on since the run."""
    return hashlib.sha256(text.encode("utf-8", "surrogateescape")).hexdigest()[:16]


def _read(ctx: Context, rel: str) -> str:
    """Current content of `rel`, "" when unreadable — matching how a vanished
    file diffs against its snapshot."""
    try:
        return (ctx.root / rel).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def snapshot(ctx: Context, index: list[dict]) -> dict[str, str]:
    """{rel: content} of every file the run's work orders can touch (the source
    file, and the authored page for drift rows), taken before the first model
    call — the before-images `record` diffs against."""
    out: dict[str, str] = {}
    for row in index:
        for key in ("file", "page"):
            rel = row.get(key)
            if rel and rel not in out:
                out[rel] = _read(ctx, rel)
    return out


def record(
    ctx: Context, before: dict[str, str], index: list[dict], mode: str, model: str
) -> None:
    """Write the run manifest for every snapshotted file the run actually changed;
    a run that changed nothing writes nothing (and never clobbers the previous
    manifest). A manifest written earlier by this same process is merged into —
    bare `--ai` is one invocation in two passes — keeping the older before-images,
    which are the true pre-run state."""
    changed = {rel: old for rel, old in before.items() if _read(ctx, rel) != old}
    if not changed:
        return
    files = {
        rel: {"before": old, "after_sha": _sha(_read(ctx, rel))}
        for rel, old in changed.items()
    }
    writes = [
        {"file": r["file"], "symbol": r["symbol"], "kind": r["kind"]}
        for r in index
        if r["file"] in changed or r.get("page") in changed
    ]
    path = _path(ctx)
    try:
        prev = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        prev = None
    if prev and prev.get("pid") == os.getpid():  # same invocation: merge passes
        for rel, entry in prev.get("files", {}).items():
            if rel in files:
                files[rel]["before"] = entry["before"]  # the older image is truer
            else:
                files[rel] = entry
        writes = prev.get("writes", []) + writes
    data = {
        "when": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "pid": os.getpid(),
        "mode": mode,
        "model": model,
        "writes": writes,
        "files": files,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    ui.note(
        f"fix: recorded {len(files)} file(s) in {ctx.rel(str(path))} — "
        "`documate --undo` reverts this run"
    )


def undo_last(ctx: Context) -> int:
    """`documate --undo`: restore every file the last --ai run touched to its
    before-image — skipping, loudly, any file whose content no longer matches
    what the run left (it was edited since; reverting would eat that edit).
    Restored files leave the manifest; refused ones stay, so a second --undo
    after you've dealt with the edit still works. Nonzero when nothing could
    be restored, or anything was refused."""
    path = _path(ctx)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        ui.fail("undo: no --ai run recorded — nothing to revert")
        return 1
    files: dict = data.get("files", {})
    restored: list[str] = []
    refused: list[str] = []
    for rel, entry in sorted(files.items()):
        now = _read(ctx, rel)
        if _sha(now) == entry["after_sha"]:
            (ctx.root / rel).write_text(entry["before"], encoding="utf-8")
            restored.append(rel)
        elif now == entry["before"]:
            restored.append(rel)  # already back — count it done
        else:
            refused.append(rel)
            ui.warn(f"undo: {rel} was edited after the run — left alone")
    for rel in restored:
        files.pop(rel, None)
    if not files:
        path.unlink(missing_ok=True)
    else:
        data["files"] = files
        data["writes"] = [w for w in data.get("writes", []) if w["file"] in files]
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    if restored:
        ui.ok(
            f"undo: {len(restored)} file(s) restored to their pre-run state — "
            "run `documate` to refresh the generated pages"
        )
    if refused:
        ui.fail(
            f"undo: {len(refused)} file(s) refused — revert or commit their edits, "
            "then re-run --undo (or use git)"
        )
        return 1
    return 0 if restored else 1

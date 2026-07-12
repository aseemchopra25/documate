"""check.py — `documate --check`: the one gate. Are the docs fresh, real, and honest?

Three checks behind one gate (CI and the pre-commit hook run exactly this):

  freshness  the generated tier matches what `documate` would write right now
             (regenerate in memory, diff against disk — no timestamps, no hashes)
  anchors    every `sym:` anchor in an authored page still resolves to real code
  drift      an authored page describes code that changed since --base but wasn't
             itself updated. DIRECT (the documented file changed) gates; RIPPLE
             (a dependency of the documented code changed) is advisory only.

Degrade contract: a missing graph never gates (the CLI indexes before calling in, so
this only bites on exotic setups); RIPPLE never gates. Exit 1 = a doc is stale, a doc
names a ghost, or a doc is lying. With --briefs the findings are additionally emitted
as work-order files (see `briefs`) — emission never changes the exit code. All
output goes through `ui` (rich); the logic is stdlib only.
"""

from __future__ import annotations

from pathlib import Path

from . import anchors, briefs, docs, drift, ui
from .core import GENERATED_STAMP, Context

_RIPPLE_HOPS = 1
_RIPPLE_CAP = 500


def run(
    ctx: Context,
    base: str | None = None,
    briefs_dir: Path | None = None,
    quiet: bool = False,
) -> int:
    """Run all three checks against `base` (default: config default_base); nonzero on any gate.
    With `briefs_dir`, also emit work-order files there for the drift findings and for
    changed-but-undocumented symbols — never affecting the exit code. With `quiet`
    (the --ai internal re-verify) the per-check success lines stay unprinted — the
    caller owns the one-line verdict — while failures and advisories print as ever."""
    base = base or ctx.config.default_base
    failures = 0

    # 1) freshness — the generated tier is a pure function of the tree; diff it.
    # Hotspots are re-mined at the pin the committed overview printed (not HEAD),
    # so history growing under unchanged docs can never read as staleness.
    if ctx.graph.exists:
        ddir = ctx.config.docs_dir
        want = docs.render(docs.build_model(ctx, hot_rev=docs.pinned_rev(ddir)))
        stale = [
            rel
            for rel, text in sorted(want.items())
            if not (ddir / rel).exists() or (ddir / rel).read_text() != text
        ]
        # stamped pages only: a file of theirs under architecture/ isn't an orphan —
        # `docs` won't prune it, so gating on it would block forever.
        orphans = sorted(
            p.relative_to(ddir).as_posix()
            for p in (ddir / "architecture").rglob("*.md")
            if p.relative_to(ddir).as_posix() not in want
            and p.read_text(encoding="utf-8", errors="replace").startswith(
                GENERATED_STAMP
            )
        )
        if stale or orphans:
            failures += 1
            for rel in stale:
                ui.detail(f"STALE  {ctx.rel(str(ddir / rel))}", err=True, style="red")
            for rel in orphans:
                ui.detail(f"ORPHAN {ctx.rel(str(ddir / rel))}", err=True, style="red")
            ui.fail("generated docs out of date — run `documate`")
        elif not quiet:
            ui.ok(f"docs fresh ({len(want)} generated page(s))")
    else:
        ui.warn("graph absent — freshness not verified")

    # 2) anchors — an authored page must not name a ghost.
    failed, degraded = anchors.validate(ctx)
    for anchor, err, pages in degraded:
        ui.detail(f"warn  {anchor}  ->  {err}  [{pages[0]}]", style="yellow")
    for anchor, err, pages in failed:
        ui.detail(
            f"FAIL  {anchor}  ->  {err}  [{', '.join(pages)}]", err=True, style="red"
        )
    if failed:
        failures += 1
        ui.fail(f"{len(failed)} dangling anchor(s) — fix the doc or the anchor")
    elif not quiet:
        n = len(anchors.build_index(ctx))
        ui.ok(
            f"{n} anchor(s) resolve"
            if n
            else "no anchors declared — nothing to resolve"
        )

    # 3) drift — an authored page describing changed code must change too.
    direct, rippled, notes, truncated = drift.find_drift(
        ctx, base, _RIPPLE_HOPS, _RIPPLE_CAP
    )
    if direct:
        failures += 1
        ui.fail(
            f"{len(direct)} doc(s) describe code changed since {base} "
            f"but weren't updated:"
        )
        for d in direct:
            sig_hint = (
                f" — re-verify the prose, then pin sig:{d['sig']}" if "sig" in d else ""
            )
            ui.detail(
                f"{d['module']}  <-  {d['file']}  (anchor {d['anchor']}){sig_hint}",
                err=True,
                style="red",
            )
        ui.note(
            "  update the doc, or commit anyway with --no-verify if it's still correct.",
            err=True,
        )
    if rippled:
        cap = " (truncated)" if truncated else ""
        ui.warn(
            f"{len(rippled)} doc(s) describe a CALLER of changed code (advisory{cap}):"
        )
        for d in rippled:
            ui.detail(
                f"~ {d['module']}  <-  {d['file']}  (anchor {d['anchor']})",
                style="yellow",
            )
    if not direct and not rippled and not quiet:
        ui.ok(
            f"no doc drift vs {base}"
            + (f" ({len(notes)} anchor(s) unverified)" if notes else "")
        )

    if briefs_dir is not None:
        orders = briefs.emit(ctx, base, direct, briefs_dir)
        if orders:  # a clean run stays quiet — zero work orders isn't news
            ui.header(
                f"briefs: {len(orders)} work order(s) -> {ctx.rel(str(briefs_dir))}"
            )

    return 1 if failures else 0

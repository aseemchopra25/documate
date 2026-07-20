"""cli.py — one command. Bare `documate` does the whole job; flags pick a mode.

  documate            index → write/refresh the docs → gate the result
  documate --check    gate only, writes nothing: generated docs fresh, anchors
                      real, no authored page lying about changed code (CI/hooks)
  documate --watch    keep running: regenerate whenever a tracked file changes
  documate --ai       the opt-in model layer: draft every missing docstring and
                      repair drifted pages via the claude CLI, then re-verify
                      through the gate (default model haiku; `--ai sonnet` upgrades)
  documate --stats    the dashboard: coverage bars, doc lines +/−, and the
                      all-time --ai bill (ledgers live next to the graph)

  documate --init     scaffold a documate.config.json (defaults, ready to edit),
                      then run the normal job — first-time setup in one command

Everything else is an override: `documate PATH` (or --root) points the same
binary at any repo or monorepo sub-tree, --base picks the drift ref, --full
re-indexes from scratch, --html adds the static site, --briefs emits work
orders whenever the gate runs. --only/--dry-run/--budget aim, preview, and
cap an --ai run; --undo reverts the last one from its recorded manifest;
--list-undocumented prints the missing-docs map as JSON. One Context per
invocation, no import-time globals. `--watch --ai` is refused: a model call
on every save is a token faucet — run --ai as a deliberate one-shot.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import subprocess
import sys
import time
from pathlib import Path

from rich_argparse import RawDescriptionRichHelpFormatter

from . import briefs, check, config, docs, prose, site, stats, ui, undo
from .core import Context


def _index(ctx: Context, full: bool) -> None:
    """Refresh the graph before anything reads it. Default is incremental-when-a-
    graph-exists: re-parse only what changed since the last build (fast on big repos,
    the whole point). --full forces a from-scratch rebuild; a first index (no db yet)
    is always full regardless. index() decides the safe path. A spinner covers the
    wait (this is the one silent-slow step), then one line says what it did."""
    mode = "incremental" if not full and ctx.graph.exists else "full"
    with ui.status(f"indexing ({mode})"):
        stats = ctx.graph.index(incremental=not full)
    if "files_parsed" in stats:
        ui.note(f"graph: {stats['files_parsed']} file(s) parsed")
    elif stats.get("files_updated"):
        ui.note(f"graph: {stats['files_updated']} file(s) re-parsed")
    else:
        ui.note("graph: up to date")


def _snapshot(ctx: Context) -> dict[str, float]:
    """{tracked file: mtime} — everything that can change the generated docs.

    Tracked files only (`git ls-files`), matching what the indexer sees, minus the
    docs and site trees: regeneration writes there, so watching them would retrigger
    forever. A vanished file drops out of the dict, which is itself a change."""
    proc = subprocess.run(
        ["git", "-C", str(ctx.root), "ls-files", "-z"],
        capture_output=True,
        text=True,
    )
    skip = (
        f"{ctx.rel(str(ctx.config.docs_dir))}/",
        f"{ctx.rel(str(ctx.config.site_dir))}/",
    )
    snap: dict[str, float] = {}
    for rel in proc.stdout.split("\0"):
        if not rel or rel.startswith(skip):
            continue
        try:
            snap[rel] = (ctx.root / rel).stat().st_mtime
        except OSError:
            continue  # deleted since ls-files — absence from the dict records it
    return snap


def _watch(ctx: Context, html: bool) -> int:
    """`documate --watch` — poll for source changes, regenerate on each one.

    A dumb 1s mtime poll, no watcher dependency: an idle cycle is one `git ls-files`
    + stats, and a regeneration is incremental, so the loop stays sub-second even on
    large repos. A brand-new file starts triggering once it's git-tracked (`git add`)
    — untracked files are invisible to the poll and the indexer alike. Ctrl-C stops.

    Each cycle prints which source files triggered it, then a colored unified diff of
    every doc page that changed in response — the live terminal view of the docs."""
    seen = _snapshot(ctx)
    ui.header("docs: watching for changes — Ctrl-C to stop")
    try:
        while True:
            time.sleep(1.0)
            now = _snapshot(ctx)
            if now == seen:
                continue
            edited = sorted(
                set(now) ^ set(seen)
                | {f for f in now.keys() & seen.keys() if now[f] != seen[f]}
            )
            seen = now
            head = ", ".join(edited[:4]) + (" …" if len(edited) > 4 else "")
            ui.line("")
            ui.line(f"[{time.strftime('%H:%M:%S')}] {head}", style="bold magenta")
            _index(ctx, full=False)
            docs.run(ctx, diff=True)
            if html:
                site.run(ctx)
    except KeyboardInterrupt:
        ui.line("")
        return 0


def _repo_here() -> bool:
    """Is the cwd inside a git work tree? Decides whether a bare `documate` can act
    (there's a repo to document) or should explain itself (the help screen)."""
    r = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"], capture_output=True
    )
    return r.returncode == 0


def build_parser() -> argparse.ArgumentParser:
    """Build the one flat parser: no subcommands, so `documate -h` is the whole
    surface on one screen (rendered through rich-argparse, the one cosmetic
    dependency). The three modes come first, overrides after."""
    p = argparse.ArgumentParser(
        prog="documate",
        description="Generate documentation from your code, and keep it honest.\n"
        "Bare `documate` does the whole job: write/refresh docs/, then gate it.",
        epilog="examples:\n"
        "  documate --init          scaffold config, then the whole job (first run)\n"
        "  documate                 the whole job — inside any repo, zero decisions\n"
        "  documate --check         gate only, writes nothing (CI / pre-commit)\n"
        "  documate --watch         regenerate on every save while you develop\n"
        "  documate --ai            let a model draft what's missing, then verify\n"
        "  documate --stats         coverage, doc lines +/−, model spend so far",
        formatter_class=RawDescriptionRichHelpFormatter,
    )
    p.add_argument(
        "-v",
        "--version",
        action="version",
        version=f"documate {importlib.metadata.version('documate')}",
    )
    p.add_argument(
        "path",
        nargs="?",
        default=None,
        help="repo to document (default: the repo you're in)",
    )
    p.add_argument(
        "--check",
        action="store_true",
        help="gate only, write nothing: docs fresh, anchors real, no drift (CI/hooks)",
    )
    p.add_argument(
        "--watch",
        action="store_true",
        help="keep running: regenerate whenever a tracked source file changes",
    )
    p.add_argument(
        "--ai",
        nargs="?",
        const="haiku",
        default=None,
        metavar="MODEL",
        help="let MODEL (default: haiku) draft missing docstrings — and with "
        "--check, repair the gate's findings — via the claude CLI, then "
        "re-verify; drafts land uncommitted for review",
    )
    p.add_argument(
        "--init",
        action="store_true",
        help="scaffold a documate.config.json (keys at their defaults), then do "
        "the normal job — first-time setup in one command",
    )
    p.add_argument(
        "--stats",
        action="store_true",
        help="show the documentation dashboard: coverage, doc lines +/−, and "
        "what --ai has cost so far",
    )
    p.add_argument(
        "--yes",
        action="store_true",
        help="skip the --ai pre-flight confirmation (unattended runs / CI)",
    )
    p.add_argument(
        "--rewrite",
        action="store_true",
        help="with --ai: re-emit every C/C++ doc comment as Doxygen (/** */ + "
        "@brief/@param/@return) — the marker Doxygen reads; drafts land uncommitted",
    )
    p.add_argument(
        "--list-undocumented",
        action="store_true",
        help="print every undocumented symbol/module as JSON on stdout (nothing "
        "else prints there) and exit — the machine-readable ask",
    )
    p.add_argument(
        "--undo",
        action="store_true",
        help="revert the last --ai run from its recorded manifest "
        "(.documate/last-run.json); files edited since are refused, not clobbered",
    )
    p.add_argument(
        "--only",
        default=None,
        metavar="GLOB",
        help="with --ai: draft only work orders whose file matches GLOB "
        "(repo-relative fnmatch, e.g. 'src/modules/*') — aim a run without "
        "re-rooting the tool",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="with --ai: show the plan (work orders, calls, token estimate), "
        "then stop — no model called, no source edited",
    )
    p.add_argument(
        "--budget",
        type=float,
        default=None,
        metavar="USD",
        help="with --ai: stop starting model calls once the run's measured spend "
        "reaches USD (in-flight calls finish; the rest is left for a re-run)",
    )
    p.add_argument(
        "--html",
        action="store_true",
        help="also render the static HTML site (default: site/, gitignored)",
    )
    p.add_argument(
        "--base",
        default=None,
        metavar="REF",
        help="git ref the drift gate diffs against (default: config default_base)",
    )
    p.add_argument(
        "--briefs",
        nargs="?",
        const="",
        default=None,
        metavar="DIR",
        help="emit per-finding work-order files for an LLM/agent when the gate "
        "runs (default DIR: .documate/briefs)",
    )
    p.add_argument(
        "--root",
        default=None,
        metavar="PATH",
        help="repo root (default: git toplevel / cwd)",
    )
    p.add_argument(
        "--full",
        action="store_true",
        help="re-index from scratch (default: incremental when a graph exists)",
    )
    return p


def _scaffold(ctx: Context) -> None:
    """`--init`: write the starter config, then teach the ignore defaults so the
    user knows what's already skipped before they add to it. Never clobbers an
    existing config — that path just says the file is already there. Behaviourally
    a no-op on this run (the scaffold is defaults only), so the docs + gate that
    follow see exactly what they would have without it."""
    path = config.scaffold(ctx.root)
    if path is None:
        ui.note(f"init: {ctx.rel(str(ctx.config.source))} already exists — leaving it")
        return
    ui.ok(f"init: wrote {ctx.rel(str(path))} — edit it to customize")
    ui.note(
        "skipped by default (add to skip_dirs, or prefix ! to un-skip): "
        + ", ".join(ctx.config.skip_dirs)
    )


def _dispatch(args) -> int:
    """Route one parsed invocation: index first (every mode reads the graph), then
    the mode — gate only (--check), the model layer (--ai), or the default job:
    docs, site if asked, then either the watch loop or the gate. --watch skips the
    gate on purpose: it's a dev loop, and gating every save would be noise."""
    ctx = Context.make(args.path or args.root)
    if args.undo:  # reverts files about to be re-read — no point indexing first
        return undo.undo_last(ctx)
    if args.list_undocumented:  # machine-readable stdout: index silently, JSON only
        ctx.graph.index(incremental=not args.full)
        print(json.dumps(briefs.undocumented(ctx), indent=2))
        return 0
    _index(ctx, args.full)
    if args.stats:
        return stats.run(ctx)
    if args.init:
        _scaffold(ctx)  # then fall through to the normal docs + gate job
    bdir = None
    if args.briefs is not None:
        bdir = (
            Path(args.briefs).resolve()
            if args.briefs
            else ctx.root / ".documate" / "briefs"
        )
    if args.check:
        if args.ai:
            return prose.fix_check(
                ctx,
                args.base,
                args.ai,
                yes=args.yes,
                only=args.only,
                dry=args.dry_run,
                budget=args.budget,
            )
        return check.run(ctx, args.base, briefs_dir=bdir)
    if args.ai:
        if args.rewrite:  # re-emit C/C++ docs as Doxygen; no seed/gate chaining
            return prose.fix_rewrite(
                ctx,
                args.ai,
                yes=args.yes,
                only=args.only,
                dry=args.dry_run,
                budget=args.budget,
            )
        # Seed every missing docstring first; a clean pass hands the gate to
        # fix_check, which repairs any drift findings and returns the re-run
        # gate's verdict. A failed seed stops here — its rc is the honest one.
        return prose.fix_docs(
            ctx,
            args.ai,
            yes=args.yes,
            only=args.only,
            dry=args.dry_run,
            budget=args.budget,
        ) or prose.fix_check(
            ctx,
            args.base,
            args.ai,
            yes=args.yes,
            quiet=True,
            only=args.only,
            dry=args.dry_run,
            budget=args.budget,
        )
    rc = docs.run(ctx)
    if rc == 0 and args.html:
        rc = site.run(ctx)
    if rc != 0:
        return rc
    if args.watch:
        return _watch(ctx, args.html)
    return check.run(ctx, args.base, briefs_dir=bdir)


def main(argv=None) -> int:
    """Console entry point: parse argv, dispatch, return the exit code.

    Bare `documate` inside a repo does the whole job; outside any repo (nothing to
    act on) it lands on the help screen instead of an error. -h/--help and argparse
    errors are converted from SystemExit to plain return codes so embedders (and
    the tests) get a value, not an exit."""
    argv = sys.argv[1:] if argv is None else list(argv)
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        if args.path in ("docs", "check"):
            parser.error(
                f"the `{args.path}` verb is gone — run bare `documate` for the "
                "whole job, or `documate --check` for the gate alone"
            )
        if args.path and not Path(args.path).is_dir():
            parser.error(f"{args.path}: not a directory")
        if args.check and (args.watch or args.html):
            parser.error("--check writes nothing — drop --watch/--html")
        if args.stats and (args.check or args.watch or args.ai or args.html):
            parser.error("--stats only reads — drop the other mode flags")
        if args.init and (args.check or args.watch or args.ai or args.stats):
            parser.error(
                "--init scaffolds then runs the default job — "
                "drop --check/--watch/--ai/--stats"
            )
        if args.watch and args.ai:
            parser.error(
                "--watch --ai would call the model on every save — "
                "run --ai as a deliberate one-shot instead"
            )
        if args.rewrite and (not args.ai or args.check):
            parser.error(
                "--rewrite drives the model over your whole repo — run it as "
                "`documate --ai <model> --rewrite`, not with --check"
            )
        if (args.only or args.dry_run or args.budget is not None) and not args.ai:
            parser.error("--only/--dry-run/--budget steer the model layer — add --ai")
        if args.undo and (
            args.check or args.watch or args.ai or args.stats or args.init or args.html
        ):
            parser.error("--undo only reverts the last --ai run — drop the other flags")
        if args.list_undocumented and (
            args.check or args.watch or args.ai or args.stats or args.init or args.html
        ):
            parser.error("--list-undocumented only reads — drop the other mode flags")
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 0
    if not argv and not _repo_here():
        parser.print_help()
        return 0
    try:
        return _dispatch(args)
    except KeyboardInterrupt:
        ui.line("")
        return 130  # clean SIGINT exit — the phases already printed their state


if __name__ == "__main__":
    sys.exit(main())

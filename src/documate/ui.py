"""ui.py — one voice for everything documate says.

Every human-facing line the tool prints goes through here: a fixed glyph
vocabulary (✓ ok, ✗ fail, ! warn, → doing), consistent colors, a spinner for
the slow silent parts, and a live progress bar while the model layer drafts.
On a real terminal the output is dynamic and colored; captured or piped
(tests, CI, the pre-commit hook) the exact same words come out as plain
single-line text — rich resolves sys.stdout/stderr lazily and drops styling
for non-terminals, and soft_wrap keeps messages greppable at any width.

Stream contract is preserved from the print() era: successes and advisories
go to stdout, gate failures to stderr — CI redirects keep meaning.
"""

from __future__ import annotations

import contextlib
import difflib

from rich.console import Console
from rich.text import Text

_out = Console(highlight=False, markup=False, soft_wrap=True)
_err = Console(stderr=True, highlight=False, markup=False, soft_wrap=True)


def _emit(console: Console, glyph: str, gstyle: str, msg: str, style=None) -> None:
    """One glyph-prefixed line on `console` — the shape every helper shares."""
    t = Text()
    if glyph:
        t.append(glyph + " ", style=gstyle)
    t.append(msg, style=style)
    console.print(t)


def ok(msg: str) -> None:
    """A green ✓ line on stdout — a step succeeded or a summary is healthy."""
    _emit(_out, "✓", "bold green", msg)


def fail(msg: str) -> None:
    """A red ✗ line on stderr — a gate failed or a step went wrong."""
    _emit(_err, "✗", "bold red", msg, style="red")


def warn(msg: str) -> None:
    """A yellow ! line on stdout — degraded or advisory, never a failure."""
    _emit(_out, "!", "bold yellow", msg)


def header(msg: str) -> None:
    """A bold cyan line announcing a phase (e.g. what --fix is about to do)."""
    _emit(_out, "→", "bold cyan", msg, style="bold cyan")


def note(msg: str, err: bool = False) -> None:
    """A dim context line — explanation under a finding, next-step hints."""
    _emit(_err if err else _out, "", "", msg, style="dim")


def detail(msg: str, err: bool = False, style: str | None = None) -> None:
    """A two-space-indented per-item row under a header (STALE/FAIL/~ lines)."""
    _emit(_err if err else _out, "", "", "  " + msg, style=style)


def line(msg: str, style: str | None = None) -> None:
    """A raw styled line on stdout — the diff view's building block."""
    _emit(_out, "", "", msg, style=style)


def diff(label: str, old: str, new: str) -> tuple[int, int]:
    """A compact colored unified diff of one file's change — `~ label` header,
    green +/red − body, two-space indent. What `--watch` shows for every doc
    change and `--fix` shows for every model edit, so nothing lands invisibly.
    Returns (added, removed) line counts so callers can total a batch."""
    line(f"~ {label}", style="bold cyan")
    added = removed = 0
    for ln in difflib.unified_diff(
        old.splitlines(), new.splitlines(), lineterm="", n=2
    ):
        if ln.startswith(("+++", "---")):
            continue
        if ln.startswith("@@"):
            line(f"  {ln}", style="cyan")
        elif ln.startswith("+"):
            added += 1
            line(f"  {ln}", style="green")
        elif ln.startswith("-"):
            removed += 1
            line(f"  {ln}", style="red")
        else:
            line(f"  {ln}")
    return added, removed


def result(*parts: tuple[str, str | None]) -> None:
    """A ✓ summary line built from (text, style) segments, so one line can
    carry differently-colored facts (pages cyan, coverage green) and still
    read as a single greppable string when styling is stripped."""
    t = Text()
    t.append("✓ ", style="bold green")
    for text, style in parts:
        t.append(text, style=style)
    _out.print(t)


def plan(title: str, lines: list[tuple[str, str | None]]) -> None:
    """The pre-flight card: a framed panel on a terminal; the same facts as
    plain indented lines when captured, so a CI log still records what a run
    was about to spend before --yes let it through."""
    if not _out.is_terminal:
        header(title)
        for msg, style in lines:
            if msg:
                detail(msg, style=style)
        return
    from rich.panel import Panel

    body = Text()
    for i, (msg, style) in enumerate(lines):
        if i:
            body.append("\n")
        body.append(msg, style=style)
    _out.print(
        Panel(
            body,
            title=Text(f" {title} ", style="bold cyan"),
            title_align="left",
            border_style="cyan",
            padding=(0, 1),
            expand=False,  # a card sized to its facts, not a terminal-wide box
        ),
        soft_wrap=False,  # let long plan lines wrap inside the frame, not crop
    )


def card(title: str, rows: list[list[tuple[str, str | None]]]) -> None:
    """A framed dashboard card: each row is (text, style) segments composing
    one line — the plan panel's shape, but multi-colored within a line (a
    coverage bar next to its +/− delta). Captured/CI gets the same words as
    plain indented lines, so a log still records what the dashboard said."""
    if not _out.is_terminal:
        header(title)
        for row in rows:
            msg = "".join(t for t, _ in row)
            if msg.strip():
                detail(msg)
        return
    from rich.panel import Panel

    body = Text()
    for i, row in enumerate(rows):
        if i:
            body.append("\n")
        for text, style in row:
            body.append(text, style=style)
    _out.print(
        Panel(
            body,
            title=Text(f" {title} ", style="bold cyan"),
            title_align="left",
            border_style="cyan",
            padding=(0, 1),
            expand=False,
        ),
        soft_wrap=False,
    )


class _Spin:
    """A stoppable spinner line for a wait of unknown length (the gap between
    sending a model call and its first visible token). Animated on a terminal,
    one dim note when captured; update() retitles it, stop() is idempotent and
    must run before printing anything else."""

    def __init__(self, msg: str):
        """Start spinning (or print the one captured-mode note)."""
        self._status = None
        if _out.is_terminal:
            self._status = _out.status(Text(msg, style="cyan"), spinner="dots")
            self._status.start()
        else:
            note(f"{msg} …")

    def update(self, msg: str) -> None:
        """Change the spinner's text (no-op when captured or stopped)."""
        if self._status:
            self._status.update(Text(msg, style="cyan"))

    def stop(self) -> None:
        """Clear the spinner so normal printing can resume; safe to call twice."""
        if self._status:
            self._status.stop()
            self._status = None


def spinner(msg: str) -> _Spin:
    """Start a stoppable spinner — for waits that end on an event (first model
    token) rather than a scope, where the `status` context manager can't fit."""
    return _Spin(msg)


def confirm(msg: str) -> bool | None:
    """A yes/no on the terminal — Enter alone means yes, so consenting is one
    keypress; n / no / q (or Ctrl-C) declines. Returns None when there is no
    terminal to ask (captured/CI) — the caller owns that policy."""
    if not _out.is_terminal:
        return None
    _out.print(
        Text.assemble(
            ("→ ", "bold cyan"), (msg, "bold"), ("  [Enter = yes · n = no] ", "dim")
        ),
        end="",
    )
    try:
        ans = input()
    except (EOFError, KeyboardInterrupt):
        _out.print()
        return False
    return ans.strip().lower() not in ("n", "no", "q", "quit")


def ask(msg: str, default: str) -> str | None:
    """A one-line free-text prompt — Enter alone takes the default. Returns
    None when there is no terminal to ask (captured/CI) or on Ctrl-C — the
    caller owns that policy."""
    if not _out.is_terminal:
        return None
    _out.print(
        Text.assemble(("→ ", "bold cyan"), (msg, "bold"), (f"  [{default}] ", "dim")),
        end="",
    )
    try:
        ans = input()
    except (EOFError, KeyboardInterrupt):
        _out.print()
        return None
    return ans.strip() or default


@contextlib.contextmanager
def status(msg: str):
    """Show what's happening during a slow, otherwise-silent step: an animated
    spinner on a terminal, a plain dim line everywhere else (so CI logs still
    say what the pause was)."""
    if _out.is_terminal:
        with _out.status(Text(msg, style="cyan"), spinner="dots"):
            yield
    else:
        note(f"{msg} …")
        yield


class _PlainTracker:
    """Non-terminal tracker: one line when an item starts, one when it ends —
    a CI log reads as a faithful transcript of what the run was doing."""

    def working(self, label: str) -> None:
        """Announce the item now being worked on."""
        _emit(_out, "→", "cyan", f"{label} …", style="dim")

    def done(self, msg: str) -> None:
        """Record one item finished successfully."""
        ok(msg)

    def failed(self, msg: str) -> None:
        """Record one item failed (stderr, like every failure)."""
        fail(msg)


class _LiveTracker:
    """Terminal tracker: a live bar (spinner, count, elapsed) whose description
    is the item in flight; finished items print as ✓/✗ lines above the bar."""

    def __init__(self, progress, task):
        """Bind to an already-started rich Progress and its single task."""
        self._progress = progress
        self._task = task

    def working(self, label: str) -> None:
        """Put the in-flight item on the live bar."""
        self._progress.update(self._task, description=label)

    def done(self, msg: str) -> None:
        """Advance the bar and log the finished item above it."""
        self._progress.console.print(Text.assemble(("✓ ", "bold green"), msg))
        self._progress.advance(self._task)

    def failed(self, msg: str) -> None:
        """Advance the bar and log the failure above it, in red."""
        self._progress.console.print(Text.assemble(("✗ ", "bold red"), (msg, "red")))
        self._progress.advance(self._task)


@contextlib.contextmanager
def tracker(total: int):
    """Track a batch of slow items (the --fix drafting loop): a transient live
    progress bar on a terminal, started/finished lines otherwise. Yields an
    object with working(label) / done(msg) / failed(msg)."""
    if not _out.is_terminal:
        yield _PlainTracker()
        return
    from rich.progress import (
        BarColumn,
        MofNCompleteColumn,
        Progress,
        SpinnerColumn,
        TextColumn,
        TimeElapsedColumn,
    )

    progress = Progress(
        SpinnerColumn(style="cyan"),
        TextColumn("{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=_out,
        transient=True,
    )
    with progress:
        yield _LiveTracker(progress, progress.add_task("", total=total))

<!-- generated documentation — edit the source, not this file -->
# `src/documate/ui.py`

ui.py — one voice for everything documate says.

Every human-facing line the tool prints goes through here: a fixed glyph
vocabulary (✓ ok, ✗ fail, ! warn, → doing), consistent colors, a spinner for
the slow silent parts, and a live progress bar while the model layer drafts.
On a real terminal the output is dynamic and colored; captured or piped
(tests, CI, the pre-commit hook) the exact same words come out as plain
single-line text — rich resolves sys.stdout/stderr lazily and drops styling
for non-terminals, and soft_wrap keeps messages greppable at any width.

Stream contract is preserved from the print() era: successes and advisories
go to stdout, gate failures to stderr — CI redirects keep meaning.

**used by** [`src/documate/check.py`](src.documate.check.md), [`src/documate/cli.py`](src.documate.cli.md), [`src/documate/docs.py`](src.documate.docs.md), [`src/documate/prose.py`](src.documate.prose.md), [`src/documate/site.py`](src.documate.site.md), [`src/documate/stats.py`](src.documate.stats.md)

## API

### `_emit(console: Console, glyph: str, gstyle: str, msg: str, style=None) -> None`
`src/documate/ui.py:27`

One glyph-prefixed line on `console` — the shape every helper shares.

**called by** `_PlainTracker.working`, `detail`, `fail`, `header`, `line`, `note`, `ok`, `warn`

### `ok(msg: str) -> None`
`src/documate/ui.py:36`

A green ✓ line on stdout — a step succeeded or a summary is healthy.

**called by** `_PlainTracker.done`  ·  **calls** `_emit`

### `fail(msg: str) -> None`
`src/documate/ui.py:41`

A red ✗ line on stderr — a gate failed or a step went wrong.

**called by** `_PlainTracker.failed`  ·  **calls** `_emit`

### `warn(msg: str) -> None`
`src/documate/ui.py:46`

A yellow ! line on stdout — degraded or advisory, never a failure.

**calls** `_emit`

### `header(msg: str) -> None`
`src/documate/ui.py:51`

A bold cyan line announcing a phase (e.g. what --fix is about to do).

**called by** `card`, `plan`  ·  **calls** `_emit`

### `note(msg: str, err: bool=False) -> None`
`src/documate/ui.py:56`

A dim context line — explanation under a finding, next-step hints.

**called by** `_Spin.__init__`, `status`  ·  **calls** `_emit`

### `detail(msg: str, err: bool=False, style: str | None=None) -> None`
`src/documate/ui.py:61`

A two-space-indented per-item row under a header (STALE/FAIL/~ lines).

**called by** `card`, `plan`  ·  **calls** `_emit`

### `line(msg: str, style: str | None=None) -> None`
`src/documate/ui.py:66`

A raw styled line on stdout — the diff view's building block.

**called by** `diff`  ·  **calls** `_emit`

### `diff(label: str, old: str, new: str) -> tuple[int, int]`
`src/documate/ui.py:71`

A compact colored unified diff of one file's change — `~ label` header,
green +/red − body, two-space indent. What `--watch` shows for every doc
change and `--fix` shows for every model edit, so nothing lands invisibly.
Returns (added, removed) line counts so callers can total a batch.

**calls** `line`

### `result(*parts: tuple[str, str | None]) -> None`
`src/documate/ui.py:96`

A ✓ summary line built from (text, style) segments, so one line can
carry differently-colored facts (pages cyan, coverage green) and still
read as a single greppable string when styling is stripped.

### `plan(title: str, lines: list[tuple[str, str | None]]) -> None`
`src/documate/ui.py:107`

The pre-flight card: a framed panel on a terminal; the same facts as
plain indented lines when captured, so a CI log still records what a run
was about to spend before --yes let it through.

**calls** `detail`, `header`

### `card(title: str, rows: list[list[tuple[str, str | None]]]) -> None`
`src/documate/ui.py:137`

A framed dashboard card: each row is (text, style) segments composing
one line — the plan panel's shape, but multi-colored within a line (a
coverage bar next to its +/− delta). Captured/CI gets the same words as
plain indented lines, so a log still records what the dashboard said.

**calls** `detail`, `header`

### `class _Spin`
`src/documate/ui.py:170`

A stoppable spinner line for a wait of unknown length (the gap between
sending a model call and its first visible token). Animated on a terminal,
one dim note when captured; update() retitles it, stop() is idempotent and
must run before printing anything else.

**called by** `spinner`

#### `_Spin.__init__(self, msg: str)`
`src/documate/ui.py:176`

Start spinning (or print the one captured-mode note).

**calls** `note`, `status`

#### `_Spin.update(self, msg: str) -> None`
`src/documate/ui.py:185`

Change the spinner's text (no-op when captured or stopped).

**called by** `_LiveTracker.working`

#### `_Spin.stop(self) -> None`
`src/documate/ui.py:190`

Clear the spinner so normal printing can resume; safe to call twice.

### `spinner(msg: str) -> _Spin`
`src/documate/ui.py:197`

Start a stoppable spinner — for waits that end on an event (first model
token) rather than a scope, where the `status` context manager can't fit.

**calls** `_Spin`

### `confirm(msg: str) -> bool | None`
`src/documate/ui.py:203`

A yes/no on the terminal — Enter alone means yes, so consenting is one
keypress; n / no / q (or Ctrl-C) declines. Returns None when there is no
terminal to ask (captured/CI) — the caller owns that policy.

### `ask(msg: str, default: str) -> str | None`
`src/documate/ui.py:223`

A one-line free-text prompt — Enter alone takes the default. Returns
None when there is no terminal to ask (captured/CI) or on Ctrl-C — the
caller owns that policy.

### `status(msg: str)`
`src/documate/ui.py:242`

Show what's happening during a slow, otherwise-silent step: an animated
spinner on a terminal, a plain dim line everywhere else (so CI logs still
say what the pause was).

**called by** `_Spin.__init__`  ·  **calls** `note`

### `class _PlainTracker`
`src/documate/ui.py:254`

Non-terminal tracker: one line when an item starts, one when it ends —
a CI log reads as a faithful transcript of what the run was doing.

**called by** `tracker`

#### `_PlainTracker.working(self, label: str) -> None`
`src/documate/ui.py:258`

Announce the item now being worked on.

**calls** `_emit`

#### `_PlainTracker.done(self, msg: str) -> None`
`src/documate/ui.py:262`

Record one item finished successfully.

**calls** `ok`

#### `_PlainTracker.failed(self, msg: str) -> None`
`src/documate/ui.py:266`

Record one item failed (stderr, like every failure).

**calls** `fail`

### `class _LiveTracker`
`src/documate/ui.py:271`

Terminal tracker: a live bar (spinner, count, elapsed) whose description
is the item in flight; finished items print as ✓/✗ lines above the bar.

**called by** `tracker`

#### `_LiveTracker.__init__(self, progress, task)`
`src/documate/ui.py:275`

Bind to an already-started rich Progress and its single task.

#### `_LiveTracker.working(self, label: str) -> None`
`src/documate/ui.py:280`

Put the in-flight item on the live bar.

**calls** `_Spin.update`

#### `_LiveTracker.done(self, msg: str) -> None`
`src/documate/ui.py:284`

Advance the bar and log the finished item above it.

#### `_LiveTracker.failed(self, msg: str) -> None`
`src/documate/ui.py:289`

Advance the bar and log the failure above it, in red.

### `tracker(total: int)`
`src/documate/ui.py:296`

Track a batch of slow items (the --fix drafting loop): a transient live
progress bar on a terminal, started/finished lines otherwise. Yields an
object with working(label) / done(msg) / failed(msg).

**calls** `_LiveTracker`, `_PlainTracker`

<!-- generated documentation — edit the source, not this file -->
# `src/documate/prose.py`

prose.py — the opt-in model layer: drive Claude over the work orders.

documate itself never calls a model API; this module shells out to the `claude`
CLI (Haiku by default), feeding it one self-contained brief per finding and
letting it edit the repo directly. The gate is the verifier: after drafting,
docs regenerate and `check` re-runs — a draft that doesn't survive the gate is
a failure, not a doc. Two entry points, one per mode:

  fix_check  `documate --check --ai` — surgical, O(diff): re-verify drifted
             authored pages (re-pinning their sigs) and draft docstrings for
             changed-but-undocumented symbols.
  fix_docs   `documate --ai` — the fresh-repo seeding pass: draft a
             docstring for every undocumented symbol, callees first, then
             regenerate the pages from them.

Two drafting paths, chosen per work order. Undocumented symbols and modules in
Python and every doc-above comment language (Go, the C family, Rust, JS/TS,
shell, …) take the token-optimal batched path: one single-turn claude call per
_BATCH briefs, no tools — the model only outputs doc text in marked blocks, and
documate inserts each at the symbol's known line itself (one system-prompt
overhead per batch, zero Read/Edit turns). Drift repairs and anything else
keep the agentic path: one call per brief with Read+Edit, editing in place.
Either way a _Spend meter rides the run: exact tokens tick on the spinner as
the CLI streams usage, exact dollars settle from each call's result payload.

Guardrails: a hard per-call timeout, a per-run cap (_CAP — re-running resumes,
and the callees-first order makes iterative runs compose), and no commits —
drafts land as uncommitted edits for a human to review, which is also why the
layer can never trigger on its own output. The model dependency stays behind
the subprocess boundary; output goes through `ui` (a live progress bar on a
terminal, a plain transcript in CI).

**depends on** [`src/documate/briefs.py`](src.documate.briefs.md), [`src/documate/check.py`](src.documate.check.md), [`src/documate/core.py`](src.documate.core.md), [`src/documate/docs.py`](src.documate.docs.md), [`src/documate/extract.py`](src.documate.extract.md), [`src/documate/stats.py`](src.documate.stats.md), [`src/documate/ui.py`](src.documate.ui.md), [`src/documate/undo.py`](src.documate.undo.md)  ·  **used by** [`src/documate/cli.py`](src.documate.cli.md)  ·  **discussed in** [`notes/v2-direction.md`](../../notes/v2-direction.md)

## API

### `_agent(name: str, prompt: str, tools: list[str]) -> list[str]`
`src/documate/prose.py:56`

The custom-agent flags every model call runs under. A default claude -p
call ships ~22k tokens of system prompt and tool schemas before the brief
even starts; an agent declaring only the tools the job needs measures ~3.7k
(no tools) / ~5.5k (Read+Edit) — the single biggest per-call token cut.

**called by** `_cmd`, `_cmd_text`

### `_cmd(model: str) -> list[str]`
`src/documate/prose.py:65`

The claude CLI invocation for one brief: print mode, edits auto-accepted,
an agent carrying only Read+Edit — a work order needs nothing else. JSON
output so the reply carries its own usage and cost for the meter.

**called by** `_draft`  ·  **calls** `_agent`

### `_cmd_text(model: str) -> list[str]`
`src/documate/prose.py:86`

The claude CLI invocation for a batched call: print mode, a tool-less
agent — the model writes text, documate does the editing. Streaming JSON
output so every word is rendered (and every completed block inserted) the
moment it's generated; --verbose is what print-mode streaming requires.

**called by** `_lane`  ·  **calls** `_agent`

### `class _Spend`
`src/documate/prose.py:119`

The run's exact token/dollar meter, as the CLI reports it — measured,
never a price table (prices go stale; `total_cost_usd` doesn't). Tokens
tick live while a call streams (message_start bills the input, each
message_delta the growing output); dollars settle per finished call from
its result payload. `on_change` re-renders whatever line displays it.

**called by** `fix_check`, `fix_docs`, `fix_rewrite`

#### `_Spend.__init__(self)`
`src/documate/prose.py:126`

Start at zero, displaying nowhere until `on_change` is bound.
Calls can stream concurrently (the lanes), so each in-flight call
keeps its own live counters, keyed by the token `_stream` mints.

#### `_Spend.message(self, key, usage: dict) -> None`
`src/documate/prose.py:137`

A message_start: bill its input (cache included) and fold the
call's previous message output into its running count.

**called by** `_stream`

#### `_Spend.delta(self, key, usage: dict) -> None`
`src/documate/prose.py:146`

A message_delta: the message's cumulative output tokens so far.

**called by** `_stream`

#### `_Spend.settle(self, payload: dict, key=None) -> None`
`src/documate/prose.py:153`

A call finished: fold its authoritative usage and cost (the result
event's `usage`/`total_cost_usd`) and clear its live counters.

**called by** `_draft`, `_stream`

#### `_Spend.spent(self) -> float`
`src/documate/prose.py:167`

Settled dollars so far — what a --budget check compares against.
Measured from finished calls' own cost reports, never a price table.

**called by** `_draft`, `_lane`

#### `_Spend.measured(self) -> bool`
`src/documate/prose.py:174`

Whether any usage was ever reported (the test seam's scripted
models report none — their runs stay meter-silent).

#### `_Spend.label(self) -> str`
`src/documate/prose.py:180`

The running figure, spinner-sized: '4.4k tok · $0.0182'. Dollars
join once a call has reported cost — the CLI bills per finished call,
so until then $0.0000 would read as free, not as pending.

**called by** `_draft`, `_show`, `_totals_line`  ·  **calls** `_tok`

### `_context(text: str) -> str`
`src/documate/prose.py:190`

A brief's evidence sections only — everything from the first `## ` heading
on. The lead paragraph is editing instructions written for the agentic path;
the batch prompt replaces them with its own output-only instructions.

**called by** `_batch_prompt`

### `_batch_prompt(rows: list[dict], briefs_dir: Path) -> str`
`src/documate/prose.py:198`

One prompt covering a chunk of work orders: shared output-only instructions,
then each brief's evidence under a numbered heading. A rewrite chunk (C-family)
asks for a Doxygen body instead; either reply format is rigid so parsing can be
too.

**called by** `_lane`, `_preflight`  ·  **calls** `_context`

### `_clean(text: str) -> str`
`src/documate/prose.py:243`

Normalize one reply block into bare docstring prose: strip the fences and
triple quotes models add despite instructions. What survives is inserted
verbatim, so anything unusable must fail loudly later, not pass quietly.

**called by** `_absorb`

### `_def_re(name: str) -> re.Pattern`
`src/documate/prose.py:257`

The definition-line pattern for a Python symbol (def/async def/class).

**called by** `_insert_py`

### `_go_def_re(name: str) -> re.Pattern`
`src/documate/prose.py:262`

The declaration-line pattern for a Go symbol (func, method with receiver,
type, var, const).

**called by** `_insert_go`

### `_comment_prefix(file: str) -> str | None`
`src/documate/prose.py:296`

The doc-comment line marker for a doc-above language, None otherwise.

**called by** `_insert_above`, `_insert_module`, `_split`

### `_locate(lines: list[str], pat: re.Pattern, at) -> tuple[int, str | None]`
`src/documate/prose.py:305`

(index, error) of the definition line: trust the index row's recorded
line, else re-locate by pattern nearest to it — earlier insertions in the
same file shift everything below them down.

**called by** `_insert_go`, `_insert_py`

### `_insert(ctx: Context, row: dict, text: str, shifts: dict | None=None) -> str | None`
`src/documate/prose.py:320`

Insert `text` as the work order's documentation, deterministically — the
model never touches the file on this path. Dispatches on kind and language:
module prose at the top of the file, a doc comment above the declaration
(Go by pattern; other doc-above languages by the indexed line, corrected
through `shifts` for earlier inserts in the same run), a Python docstring
under the signature. Returns an error string (nothing written) or None on
success.

**called by** `_absorb`  ·  **calls** `_insert_above`, `_insert_go`, `_insert_module`, `_insert_py`, `_rewrite_above`

### `_comment(text: str, ind: str, prefix: str='//') -> str`
`src/documate/prose.py:341`

`text` as a `prefix` comment block at `ind`entation — markers a model
added despite instructions stripped, so they can't double up. Any draft
embeds safely: a line marker has no closing delimiter to collide with.

**called by** `_insert_above`, `_insert_go`, `_insert_module`

### `_doxygen_block(text: str, ind: str) -> str | None`
`src/documate/prose.py:354`

`text` as a Doxygen `/** ... */` block at `ind`entation — the marker Doxygen
reads, unlike the plain `//` other doc-above inserts use. Any wrapper the model
added despite instructions (`/**`, `*/`, a leading `*`) is stripped so markers
can't double up; a blank interior line becomes a lone ` *`. None when nothing
but markers survives — an empty `/** */` would read as undocumented.

**called by** `_insert_above`, `_insert_module`, `_rewrite_above`

### `_decl_start(lines: list[str], i: int) -> int`
`src/documate/prose.py:403`

Index of the first line of the declaration whose name sits on line `i`.

C and C++ routinely break a declaration after the return type, which is the
house style across the Linux kernel and Zephyr:

    static enum uwb_err
    parse_session_attribute(struct uwb_msg_attribute *attr, ...)

Inserting directly above the name line puts the comment *inside* the
declaration, where Doxygen and `extract.doc_above` both stop seeing it — the
symbol reads as undocumented however many times it is written. Walk up over
bare type/qualifier/`*` lines to the real start. A comment block found
mid-walk with a type/qualifier line directly above it is exactly that damage
(an older insert wedged it inside the declaration): hop it and keep
climbing, so a rewrite lands above the whole declaration — `_wedged_spans`
finds the hopped block for removal. A comment with anything else above it is
the symbol's doc, not damage, and ends the walk as before.

**called by** `_insert_above`, `_rewrite_above`

### `_wedged_spans(lines: list[str], decl: int, name: int) -> list[tuple[int, int]]`
`src/documate/prose.py:445`

(start, end) 0-indexed inclusive spans of comment blocks sitting between a
declaration's first line and its name line — the damage an older insert left
wedged inside the declaration. `_decl_start`'s hop walks above them; deleting
them is the other half of the repair.

**called by** `_rewrite_above`

### `_find_decl(lines: list[str], at: int, word: re.Pattern) -> int | None`
`src/documate/prose.py:468`

0-index of the line carrying the symbol's name, from its recorded line
`at` (1-indexed, already shift-corrected), or None.

The graph records a C declaration at its first line, so the name sits on it
or 1-2 lines below (return type on its own line), occasionally one above —
the original probe window. When a comment block sits wedged inside the
declaration (older-insert damage), the name is further down than the window
reaches: from a type/qualifier first line, walk forward over type, blank and
comment lines — never matching the name against comment text, which may
legitimately mention it — until the first other code line.

**called by** `_insert_above`, `_rewrite_above`

### `_at_definition(lines: list[str], i: int) -> bool`
`src/documate/prose.py:500`

True when line `i` is at a scope where a definition can live: not inside a
parameter list, and not inside a function body.

The landing line is found by matching the symbol's name, which also matches
the name's *uses* — a parameter of that type, a local of that type. Writing
there wedges a doc comment into a signature (which corrupts how the
declaration renders) or onto a local variable (which documents nothing).
Member scopes are allowed through: a struct or class body is where a member's
doc belongs. A `{` straight after `)` opens a function body even when the
signature names a struct return type (`static struct s *get(void) {`).
Delimiters inside strings, chars and comments do not count.

**called by** `_insert_above`, `_rewrite_above`

### `_rewrite_above(ctx: Context, row: dict, text: str, shifts: dict) -> str | None`
`src/documate/prose.py:540`

Replace (or, when absent, insert) the Doxygen doc comment above a C-family
declaration. Locates the decl by its recorded line — shift-corrected for earlier
rewrites in this run — confirmed by the symbol's name on (or within two lines of)
the landing line; swaps the existing doc block found by `_doc_span` for a fresh
`/** */` block, or inserts one when there's no doc there yet (a symbol documented
only on its header prototype). A comment block an older insert wedged inside the
declaration is deleted on the way — the rewrite self-heals that damage instead of
upgrading it in place. Nothing is written when the decl can't be located or the
draft is empty.

**called by** `_insert`  ·  **calls** `_at_definition`, `_decl_start`, `_doxygen_block`, `_find_decl`, `_wedged_spans`

### `_insert_module(ctx: Context, row: dict, text: str, shifts: dict | None=None) -> str | None`
`src/documate/prose.py:589`

Insert `text` as the module's top-of-file prose: a comment block directly
above a Go `package` clause, a docstring as a Python file's first statement
(after any leading `#!`/`#` comment lines), a comment block at the top of
any other doc-above file (after a shebang).

**called by** `_insert`  ·  **calls** `_comment`, `_comment_prefix`, `_doxygen_block`

### `_insert_go(ctx: Context, row: dict, text: str) -> str | None`
`src/documate/prose.py:656`

Insert `text` as a Go doc comment: find the declaration (the recorded
line, re-located by name if it shifted), write the `//` block directly
above.

**called by** `_insert`  ·  **calls** `_comment`, `_go_def_re`, `_locate`

### `_insert_above(ctx: Context, row: dict, text: str, shifts: dict) -> str | None`
`src/documate/prose.py:677`

Insert `text` as a doc comment above the declaration in any doc-above
language (C family, Rust, JS/TS, shell, …). There is no per-language
declaration grammar here: the graph's recorded line is trusted, corrected
through `shifts` — the lines earlier inserts in this run added above it —
and the symbol's name must appear on (or within two lines of) the landing
line, else nothing is written.

**called by** `_insert`  ·  **calls** `_at_definition`, `_comment`, `_comment_prefix`, `_decl_start`, `_doxygen_block`, `_find_decl`

### `_insert_py(ctx: Context, row: dict, text: str) -> str | None`
`src/documate/prose.py:718`

Insert `text` as a Python docstring: find the def line, walk to the end
of the signature, indent to the body, write.

**called by** `_insert`  ·  **calls** `_def_re`, `_locate`

### `_stream(argv: list[str], prompt: str, cwd, timeout: int, on_text, on_think=None, meter: _Spend | None=None, procs: set | None=None) -> tuple[int, str, str, bool]`
`src/documate/prose.py:760`

Run one model call, delivering its reply incrementally: each stdout line
that is a stream-json `text_delta` event (or any non-JSON line — the test
seam's plain output) is passed to `on_text` as it arrives; `thinking_delta`
events (never rendered) tick `on_think`, so a caller can show that the
silence before the first word is the model reasoning, not a hang. With a
`meter`, usage events feed it live and the final result settles it. A timer
kills the process at `timeout`; Ctrl-C kills it too, then propagates so
callers can account for what already landed. Returns (returncode, full
text, stderr, timed_out); a final `result` event is the text fallback when
no deltas were seen.

Thinking is disabled for the call: this path is single-turn transcription —
the evidence is in the prompt and the output format is rigid, and thinking
tokens bill as output (measured ~2-8k per batch, dwarfing the drafts). The
agentic path keeps thinking; repairs are judgment work.

**called by** `_lane`  ·  **calls** `_Spend.delta`, `_Spend.message`, `_Spend.settle`

### `_kill()`
`src/documate/prose.py:798`

Timer callback: mark the call timed out and kill the model process.

### `_lanes(rows: list[dict]) -> list[list[dict]]`
`src/documate/prose.py:853`

File-disjoint lanes for concurrent drafting: rows grouped by file (a
file's symbol orders and its trailing module order stay together, in
order), groups packed onto up to _WORKERS lanes, least-loaded first. Two
lanes never touch the same file, so parallel inserts can't fight over
line numbers.

**called by** `_draft_batch`, `_preflight`

### `_draft_batch(ctx: Context, rows: list[dict], briefs_dir: Path, model: str, timeout: int, cmd: list[str] | None, spend: _Spend, touched: set | None=None, budget: float | None=None) -> int`
`src/documate/prose.py:870`

The batched single-turn path for undocumented symbols and modules: one
model call per _BATCH briefs, up to _WORKERS calls in flight at once over
file-disjoint lanes. With a `budget`, no new call starts once the settled
spend reaches it (in-flight calls finish, so the stop can overshoot by up
to one call per lane) — the remainder is reported, not failed. The run reads as one ✓ line per docstring, printed
the moment its block completes — file, symbol, drafted summary; a single
spinner covers the rest (waiting, thinking, then a running n/m count over
the whole run), carrying the live token/dollar spend on the same line.
The full text is for `git diff` — the terminal shows each draft once.
Ends with a run total. Returns the failure count. Ctrl-C kills every
in-flight call, accounts for what landed, then propagates.

**called by** `fix_check`, `fix_docs`, `fix_rewrite`  ·  **calls** `_lanes`, `_show`, `_tally`, `_totals_line`

### `_show() -> None`
`src/documate/prose.py:905`

Re-render the spinner: current phase, then the live spend.

**called by** `_absorb`, `_draft_batch`, `_think`  ·  **calls** `_Spend.label`

### `_lane(lane_rows: list[dict]) -> None`
`src/documate/prose.py:912`

Drive one lane: its chunks run serially, so this lane's files only
ever see one writer, and the per-file line shifts stay coherent.

**calls** `_Spend.spent`, `_absorb`, `_batch_prompt`, `_cmd_text`, `_stream`, `_tally`

### `_think() -> None`
`src/documate/prose.py:934`

First thinking delta: the silence is reasoning, say so.

**calls** `_show`

### `_absorb(text_chunk: str) -> None`
`src/documate/prose.py:940`

Fold in the chunk; insert and announce any completed block.

**called by** `_lane`  ·  **calls** `_clean`, `_insert`, `_show`

### `_totals_line(totals: dict, spend: _Spend) -> None`
`src/documate/prose.py:1040`

The run-total header every drafting path (and its interrupt) ends with —
what landed is always accounted for, even when the run didn't finish. The
spend joins it when measured, so the spinner's meter outlives the spinner.

**called by** `_draft`, `_draft_batch`  ·  **calls** `_Spend.label`

### `_split(index: list[dict]) -> tuple[list[dict], list[dict]]`
`src/documate/prose.py:1053`

(batchable, agentic) work orders: undocumented symbols, modules, and
C-family rewrites in any language documate can insert into deterministically
(Python, Go, and every doc-above comment language) take the single-turn batched
path; drift repairs and anything else keep the in-place agent.

**called by** `_preflight`, `fix_check`, `fix_docs`  ·  **calls** `_comment_prefix`

### `_snapshot(ctx: Context, row: dict) -> dict[str, str]`
`src/documate/prose.py:1068`

{relpath: content} of the work order's target files (the source file, and
the authored page for drift rows) before the model runs — the baseline the
live draft view diffs against. A brief names its targets, so this is where
every edit is expected to land.

**called by** `_draft`

### `_tally(ctx: Context, before: dict[str, str], totals: dict) -> None`
`src/documate/prose.py:1085`

Fold what the model just changed in the snapshotted files into the run
totals (counted even for a failed/timed-out/interrupted call — a partial
edit must not go unaccounted). Accounting only: the terminal shows each
draft once, as its ✓ line; the full text belongs to `git diff`.

**called by** `_draft`, `_draft_batch`, `_lane`

### `_draft(ctx: Context, index: list[dict], briefs_dir: Path, model: str, timeout: int, cmd: list[str] | None, spend: _Spend, touched: set | None=None, budget: float | None=None) -> int`
`src/documate/prose.py:1110`

Feed each work order to the model (brief on stdin, repo as cwd), showing
live progress. With a `budget`, no new call starts once the settled spend
reaches it — the remainder is reported, not failed. — the in-flight brief on the bar (with the run's spend so
far), one ✓/✗ line per outcome. Each call's result JSON settles the meter.
Ends with a run total. Returns the number of failures; a missing claude CLI
fails every order with one clear hint instead of a stack trace, and Ctrl-C
shows any partial edit of the interrupted order before propagating.

**called by** `fix_check`, `fix_docs`  ·  **calls** `_Spend.label`, `_Spend.settle`, `_Spend.spent`, `_cmd`, `_snapshot`, `_tally`, `_totals_line`

### `_tok(n: int) -> str`
`src/documate/prose.py:1192`

A token count as a compact human number (874 → '874', 1934 → '1.9k').

**called by** `_Spend.label`, `_preflight`

### `_preflight(ctx: Context, index: list[dict], briefs_dir: Path, model: str, yes: bool, budget: float | None=None) -> bool | None`
`src/documate/prose.py:1197`

No model call starts unannounced: show exactly what --ai is about to do
— every symbol, every file, how many calls, and a token estimate measured
over the very prompts it would send (chars/4; the agentic path's number is
a floor, since the agent may Read more) — then ask. Returns True to
proceed, False on an explicit decline (a clean no-op), and None when there
is no terminal to ask and no --yes (refused: unattended spend needs to be
opted into).

**called by** `_dry_run`, `fix_check`, `fix_docs`, `fix_rewrite`  ·  **calls** `_batch_prompt`, `_lanes`, `_split`, `_tok`

### `_only(index: list[dict], pattern: str | None) -> list[dict]`
`src/documate/prose.py:1276`

The work orders whose repo-relative file (or authored page, for drift rows)
matches the `--only` glob — fnmatch, so `*` crosses directories — leaving the
context root alone: aiming a run at one subtree no longer means pointing the
whole tool (and its `.documate/`, its `docs/`) at that subtree. Says what the
filter dropped, so a too-narrow glob is visible instead of a silent no-op.

**called by** `fix_check`, `fix_docs`, `fix_rewrite`

### `_dry_run(ctx: Context, capped: list[dict], bdir: Path, model: str, budget: float | None=None) -> None`
`src/documate/prose.py:1295`

`--dry-run`: show exactly what the run would do — the same pre-flight plan a
real run confirms, work orders already on disk under `bdir` for inspection —
then stop. No model call, no source edit.

**called by** `fix_check`, `fix_docs`, `fix_rewrite`  ·  **calls** `_preflight`

### `_capped(index: list[dict]) -> list[dict]`
`src/documate/prose.py:1312`

The first _CAP work orders; prints how many remain when truncated.

**called by** `fix_check`, `fix_docs`, `fix_rewrite`

### `fix_check(ctx: Context, base: str | None, model: str, timeout: int=_TIMEOUT, cmd: list[str] | None=None, yes: bool=False, quiet: bool=False, only: str | None=None, dry: bool=False, budget: float | None=None) -> int`
`src/documate/prose.py:1323`

`documate --check --ai`: run the gate, show the pre-flight plan and get
consent, draft every emitted work order, regenerate the docs (drafted
docstrings change the generated tier), then re-run the gate — its verdict
is the exit code. Declining leaves the first gate's verdict untouched.
`only` narrows the orders to one file glob; `dry` stops after the plan.
With `quiet` (bare `--ai`, where seeding already reported) the leading gate
is internal plumbing: a pass collapses to one line, failures stay loud.

**calls** `_Spend`, `_capped`, `_draft`, `_draft_batch`, `_dry_run`, `_interrupted`, `_only`, `_preflight`

### `_run_format(ctx: Context, touched: set[str]) -> None`
`src/documate/prose.py:1383`

Run the repo's configured `format_cmd` over the source files a run touched,
before the re-index reads them — inserted doc comments then land already
conforming to the repo's formatter (a pinned clang-format CI gate would
otherwise fail on every long `@brief` line the model wrote). The command is
split shell-style with the repo-relative paths appended; any failure warns
and never sinks the run — the drafts are already on disk.

**called by** `fix_check`, `fix_docs`, `fix_rewrite`

### `_reindex(ctx: Context) -> None`
`src/documate/prose.py:1406`

Best-effort incremental re-index after drafting: inserted doc lines
shifted every declaration below them, and line-anchored extraction (the
doc_above languages — Go, C) must read the files the drafts produced. A
graph that won't rebuild (a foreign db) costs only re-emit freshness; it
must never crash a run whose tokens are already spent.

**called by** `fix_check`, `fix_docs`, `fix_rewrite`

### `_interrupted() -> int`
`src/documate/prose.py:1418`

The Ctrl-C epilogue: partial drafts were already shown and stay as
uncommitted edits; say how to review, discard, or resume, and exit 130
(the conventional SIGINT code) without a traceback.

**called by** `fix_check`, `fix_docs`, `fix_rewrite`

### `fix_docs(ctx: Context, model: str, timeout: int=_TIMEOUT, cmd: list[str] | None=None, yes: bool=False, only: str | None=None, dry: bool=False, budget: float | None=None) -> int`
`src/documate/prose.py:1430`

`documate --ai`: the fresh-repo seeding pass. Generate the pages, show
the pre-flight plan and get consent, draft a docstring for every
undocumented symbol (callees first), then regenerate so the new docstrings
land on the pages. `only` narrows the orders to one file glob; `dry` stops
after the plan. Nonzero when any draft failed (or consent was
impossible unattended); declining is a clean exit-0 no-op. The drafts
themselves are uncommitted edits awaiting review.

**calls** `_Spend`, `_capped`, `_draft`, `_draft_batch`, `_dry_run`, `_interrupted`, `_only`, `_preflight`

### `fix_rewrite(ctx: Context, model: str, timeout: int=_TIMEOUT, cmd: list[str] | None=None, yes: bool=False, only: str | None=None, dry: bool=False, budget: float | None=None) -> int`
`src/documate/prose.py:1497`

`documate --ai <model> --rewrite`: re-emit every C/C++ symbol's doc comment
as Doxygen (`/** */` with `@brief`/`@param`/`@return`) — the marker Doxygen reads,
unlike the plain `//` seeding writes. Generate the pages, show the pre-flight plan
and get consent, draft each rewrite (existing docs replaced in place, undocumented
ones seeded), then regenerate so the pages reflect the new prose. `only` narrows
the orders to one file glob; `dry` stops after the plan. Nonzero when any
draft failed (or consent was impossible unattended); declining is a clean exit-0
no-op. The drafts are uncommitted edits awaiting review.

**calls** `_Spend`, `_capped`, `_draft_batch`, `_dry_run`, `_interrupted`, `_only`, `_preflight`, `_reindex`

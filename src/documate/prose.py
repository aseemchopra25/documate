"""prose.py — the opt-in model layer: drive Claude over the work orders.

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
"""

from __future__ import annotations

import difflib
import fnmatch
import json
import os
import re
import concurrent.futures
import shlex
import subprocess
import threading
from pathlib import Path

from . import briefs, check, docs, extract, stats, ui, undo
from .core import Context

_CAP = 200  # briefs per run — bounds one run's spend; re-run to continue
_TIMEOUT = 180  # seconds per model call — a draft is a draft, not a research project
_BATCH = 40  # undocumented briefs bundled into one single-turn call
_WORKERS = 3  # concurrent batch calls — lanes are file-disjoint, so inserts can't fight


def _agent(name: str, prompt: str, tools: list[str]) -> list[str]:
    """The custom-agent flags every model call runs under. A default claude -p
    call ships ~22k tokens of system prompt and tool schemas before the brief
    even starts; an agent declaring only the tools the job needs measures ~3.7k
    (no tools) / ~5.5k (Read+Edit) — the single biggest per-call token cut."""
    spec = {name: {"description": prompt, "prompt": prompt, "tools": tools}}
    return ["--agents", json.dumps(spec), "--agent", name]


def _cmd(model: str) -> list[str]:
    """The claude CLI invocation for one brief: print mode, edits auto-accepted,
    an agent carrying only Read+Edit — a work order needs nothing else. JSON
    output so the reply carries its own usage and cost for the meter."""
    return [
        "claude",
        "-p",
        "--model",
        model,
        *_agent(
            "fixer",
            "You repair documentation in place, following the work order given.",
            ["Read", "Edit"],
        ),
        "--permission-mode",
        "acceptEdits",
        "--output-format",
        "json",
    ]


def _cmd_text(model: str) -> list[str]:
    """The claude CLI invocation for a batched call: print mode, a tool-less
    agent — the model writes text, documate does the editing. Streaming JSON
    output so every word is rendered (and every completed block inserted) the
    moment it's generated; --verbose is what print-mode streaming requires."""
    return [
        "claude",
        "-p",
        "--model",
        model,
        *_agent(
            "scribe",
            "You write documentation text exactly in the output format the "
            "work orders request — nothing else.",
            [],
        ),
        "--output-format",
        "stream-json",
        "--include-partial-messages",
        "--verbose",
    ]


_BLOCK = re.compile(r"<<<doc\s+(\d+)>>>\s*\n(.*?)<<<end>>>", re.S)

_USAGE_KEYS = (
    "input_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "output_tokens",
)


class _Spend:
    """The run's exact token/dollar meter, as the CLI reports it — measured,
    never a price table (prices go stale; `total_cost_usd` doesn't). Tokens
    tick live while a call streams (message_start bills the input, each
    message_delta the growing output); dollars settle per finished call from
    its result payload. `on_change` re-renders whatever line displays it."""

    def __init__(self):
        """Start at zero, displaying nowhere until `on_change` is bound.
        Calls can stream concurrently (the lanes), so each in-flight call
        keeps its own live counters, keyed by the token `_stream` mints."""
        self.tokens = 0  # settled: finished calls, by their result usage
        self.usd = 0.0
        self._billed = False  # a call reported its cost at least once
        self._lk = threading.Lock()
        self._calls: dict = {}  # in-flight: key -> (billed so far, current out)
        self.on_change = lambda: None

    def message(self, key, usage: dict) -> None:
        """A message_start: bill its input (cache included) and fold the
        call's previous message output into its running count."""
        with self._lk:
            b, o = self._calls.get(key, (0, 0))
            b += o + sum(usage.get(k) or 0 for k in _USAGE_KEYS[:3])
            self._calls[key] = (b, usage.get("output_tokens") or 0)
        self.on_change()

    def delta(self, key, usage: dict) -> None:
        """A message_delta: the message's cumulative output tokens so far."""
        with self._lk:
            b, o = self._calls.get(key, (0, 0))
            self._calls[key] = (b, usage.get("output_tokens") or o)
        self.on_change()

    def settle(self, payload: dict, key=None) -> None:
        """A call finished: fold its authoritative usage and cost (the result
        event's `usage`/`total_cost_usd`) and clear its live counters."""
        with self._lk:
            b, o = self._calls.pop(key, (0, 0))
            u = payload.get("usage") or {}
            total = sum(u.get(k) or 0 for k in _USAGE_KEYS)
            self.tokens += total or (b + o)
            usd = payload.get("total_cost_usd")
            if isinstance(usd, (int, float)):
                self.usd += usd
                self._billed = True
        self.on_change()

    def spent(self) -> float:
        """Settled dollars so far — what a --budget check compares against.
        Measured from finished calls' own cost reports, never a price table."""
        with self._lk:
            return self.usd

    @property
    def measured(self) -> bool:
        """Whether any usage was ever reported (the test seam's scripted
        models report none — their runs stay meter-silent)."""
        with self._lk:
            return bool(self.tokens or self._calls or self.usd)

    def label(self) -> str:
        """The running figure, spinner-sized: '4.4k tok · $0.0182'. Dollars
        join once a call has reported cost — the CLI bills per finished call,
        so until then $0.0000 would read as free, not as pending."""
        with self._lk:
            live = sum(b + o for b, o in self._calls.values())
            usd = f" · ${self.usd:.4f}" if self._billed else ""
            return f"{_tok(self.tokens + live)} tok{usd}"


def _context(text: str) -> str:
    """A brief's evidence sections only — everything from the first `## ` heading
    on. The lead paragraph is editing instructions written for the agentic path;
    the batch prompt replaces them with its own output-only instructions."""
    i = text.find("\n## ")
    return text[i + 1 :] if i >= 0 else text


def _batch_prompt(rows: list[dict], briefs_dir: Path) -> str:
    """One prompt covering a chunk of work orders: shared output-only instructions,
    then each brief's evidence under a numbered heading. A rewrite chunk (C-family)
    asks for a Doxygen body instead; either reply format is rigid so parsing can be
    too."""
    if any(r.get("kind") == "rewrite" for r in rows):
        head = (
            "# Work orders: rewrite as Doxygen documentation\n\n"
            f"Below are {len(rows)} work orders, one per C/C++ symbol. For each, write "
            "an improved Doxygen doc-comment BODY: first line "
            "`@brief <one-sentence summary>`; then, when the code shows them, one "
            "`@param <name> <description>` per parameter and a `@return <description>` "
            "— only what the shown code and current doc prove, never invented. Output "
            "the body as PLAIN TEXT: no `/**`, no `*/`, no leading `*`, no code fences "
            "— the caller wraps it in a /** */ block. Reply with exactly one block per "
            "work order, in order, in this exact format and nothing else:\n\n"
            "<<<doc 1>>>\n@brief ...\n<<<end>>>"
        )
    else:
        head = (
            "# Work orders: write docstrings\n\n"
            f"Below are {len(rows)} work orders, one per undocumented symbol or module. "
            "For each, "
            "write the docstring text: what it does and any contract a caller must know "
            "— only what the shown code proves, never invented. First line = one-sentence "
            "summary; add more lines only when a caller needs them. Output PLAIN TEXT "
            "only — no quotes, no code fences, no comment markers; the caller inserts it "
            "into the file. Reply with exactly one block per work order, in order, in "
            "this exact format and nothing else:\n\n"
            "<<<doc 1>>>\n<docstring text for work order 1>\n<<<end>>>"
        )
        if any(r["file"].endswith(".go") for r in rows):
            head += (
                "\n\nFor symbols in .go files, begin the first line with the symbol's "
                "name (Go doc convention)."
            )
    parts = [head]
    for n, row in enumerate(rows, 1):
        body = _context((briefs_dir / row["brief"]).read_text(encoding="utf-8"))
        parts.append(
            f"## Work order {n}: `{row['symbol']}` in `{row['file']}`\n\n{body}"
        )
    return "\n\n".join(parts)


def _clean(text: str) -> str:
    """Normalize one reply block into bare docstring prose: strip the fences and
    triple quotes models add despite instructions. What survives is inserted
    verbatim, so anything unusable must fail loudly later, not pass quietly."""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else ""
        t = t.rsplit("```", 1)[0].strip()
    for q in ('"""', "'''"):
        if t.startswith(q) and t.endswith(q) and len(t) >= 2 * len(q):
            t = t[len(q) : -len(q)].strip()
    return t


def _def_re(name: str) -> re.Pattern:
    """The definition-line pattern for a Python symbol (def/async def/class)."""
    return re.compile(rf"^\s*(?:async\s+def|def|class)\s+{re.escape(name)}\b")


def _go_def_re(name: str) -> re.Pattern:
    """The declaration-line pattern for a Go symbol (func, method with receiver,
    type, var, const)."""
    return re.compile(
        rf"^\s*(?:func\b\s*(?:\([^)]*\))?\s*|(?:type|var|const)\s+){re.escape(name)}\b"
    )


# languages whose doc convention is a comment block directly above the
# declaration — the styles extract.doc_above reads back. `//` for the C
# family / Rust / Java-Kotlin-Swift / JS-TS, `#` for shell (extract._HASH_DOCS).
_SLASH_DOCS = (
    ".c",
    ".h",
    ".cc",
    ".cpp",
    ".hpp",
    ".hh",
    ".cxx",
    ".m",
    ".mm",
    ".rs",
    ".java",
    ".kt",
    ".swift",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".mjs",
    ".cjs",
)


def _comment_prefix(file: str) -> str | None:
    """The doc-comment line marker for a doc-above language, None otherwise."""
    if file.endswith(_SLASH_DOCS) or file.endswith(".go"):
        return "//"
    if file.endswith(extract._HASH_DOCS):
        return "#"
    return None


def _locate(lines: list[str], pat: re.Pattern, at) -> tuple[int, str | None]:
    """(index, error) of the definition line: trust the index row's recorded
    line, else re-locate by pattern nearest to it — earlier insertions in the
    same file shift everything below them down."""
    i = at - 1 if isinstance(at, int) else -1
    if 0 <= i < len(lines) and pat.match(lines[i]):
        return i, None
    hits = [j for j, ln in enumerate(lines) if pat.match(ln)]
    if not hits:
        return -1, "definition not found"
    if len(hits) > 1 and not isinstance(at, int):
        return -1, "definition ambiguous"
    return (min(hits, key=lambda j: abs(j - i)) if i >= 0 else hits[0]), None


def _insert(
    ctx: Context, row: dict, text: str, shifts: dict | None = None
) -> str | None:
    """Insert `text` as the work order's documentation, deterministically — the
    model never touches the file on this path. Dispatches on kind and language:
    module prose at the top of the file, a doc comment above the declaration
    (Go by pattern; other doc-above languages by the indexed line, corrected
    through `shifts` for earlier inserts in the same run), a Python docstring
    under the signature. Returns an error string (nothing written) or None on
    success."""
    if row["kind"] == "module":
        return _insert_module(ctx, row, text, shifts or {})
    if row["kind"] == "rewrite":
        return _rewrite_above(ctx, row, text, shifts if shifts is not None else {})
    if row["file"].endswith(".go"):
        return _insert_go(ctx, row, text)
    if row["file"].endswith(".py"):
        return _insert_py(ctx, row, text)
    return _insert_above(ctx, row, text, shifts if shifts is not None else {})


def _comment(text: str, ind: str, prefix: str = "//") -> str:
    """`text` as a `prefix` comment block at `ind`entation — markers a model
    added despite instructions stripped, so they can't double up. Any draft
    embeds safely: a line marker has no closing delimiter to collide with."""
    block = ""
    for ln in text.splitlines():
        ln = ln.strip()
        if ln.startswith(prefix):
            ln = ln[len(prefix) :].strip()
        block += f"{ind}{prefix} {ln}".rstrip() + "\n"
    return block


def _doxygen_block(text: str, ind: str) -> str | None:
    """`text` as a Doxygen `/** ... */` block at `ind`entation — the marker Doxygen
    reads, unlike the plain `//` other doc-above inserts use. Any wrapper the model
    added despite instructions (`/**`, `*/`, a leading `*`) is stripped so markers
    can't double up; a blank interior line becomes a lone ` *`. None when nothing
    but markers survives — an empty `/** */` would read as undocumented."""
    body: list[str] = []
    for ln in text.splitlines():
        ln = ln.strip()
        if ln in ("/**", "/*", "*/"):
            continue
        if ln.startswith("/**"):
            ln = ln[3:].strip()
        ln = ln.removeprefix("/*").removesuffix("*/").strip()
        if ln.startswith("*"):
            ln = ln[1:].strip()
        body.append(ln)
    while body and not body[0]:
        body.pop(0)
    while body and not body[-1]:
        body.pop()
    if not body:
        return None
    block = f"{ind}/**\n"
    for ln in body:
        block += f"{ind} * {ln}".rstrip() + "\n"
    block += f"{ind} */\n"
    return block


#: how far above the name line a declaration's type/qualifier prefix may run
_DECL_PREFIX_MAX = 4

#: a line that is nothing but type/qualifier words and pointer stars, e.g.
#: `static enum uwb_err`, `struct session_ctx *`, `CHIP_ERROR`, `*`.
#: Anything carrying punctuation that opens or ends a statement is excluded, so
#: the walk can never cross `;`, `{`, `}`, `(`, `=`, a comment or a directive.
_DECL_PREFIX = re.compile(r"^[A-Za-z_][A-Za-z0-9_ \t*]*$|^\*+$")

#: opens a member scope rather than a function body — documenting inside one of
#: these is legitimate, documenting inside a function body is not.
_MEMBER_SCOPE = re.compile(r"\b(struct|class|enum|union|namespace|extern)\b")


#: how far a comment block wedged inside a declaration may run before the walk
#: refuses to treat it as part of the declaration
_WEDGE_MAX = 20


def _decl_start(lines: list[str], i: int) -> int:
    """Index of the first line of the declaration whose name sits on line `i`.

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
    the symbol's doc, not damage, and ends the walk as before."""
    j = i
    for _ in range(_DECL_PREFIX_MAX):
        if j == 0:
            break
        s = lines[j - 1].strip()
        if s.endswith("*/") or s.startswith("//"):
            top = j - 1
            if s.endswith("*/"):
                while top >= 0 and "/*" not in lines[top]:
                    top -= 1
            else:
                while top > 0 and lines[top - 1].strip().startswith("//"):
                    top -= 1
            above = lines[top - 1].strip() if top > 0 else ""
            if top >= 0 and j - top <= _WEDGE_MAX and above and _DECL_PREFIX.match(above):
                j = top  # wedged inside the declaration: hop it, keep climbing
                continue
            break
        if not s or not _DECL_PREFIX.match(s):
            break
        j -= 1
    return j


def _wedged_spans(lines: list[str], decl: int, name: int) -> list[tuple[int, int]]:
    """(start, end) 0-indexed inclusive spans of comment blocks sitting between a
    declaration's first line and its name line — the damage an older insert left
    wedged inside the declaration. `_decl_start`'s hop walks above them; deleting
    them is the other half of the repair."""
    spans: list[tuple[int, int]] = []
    k = decl
    while k < name:
        s = lines[k].strip()
        if s.startswith("/*"):
            start = k
            while k < name and "*/" not in lines[k]:
                k += 1
            spans.append((start, min(k, name - 1)))
        elif s.startswith("//"):
            start = k
            while k + 1 < name and lines[k + 1].strip().startswith("//"):
                k += 1
            spans.append((start, k))
        k += 1
    return spans


def _find_decl(lines: list[str], at: int, word: re.Pattern) -> int | None:
    """0-index of the line carrying the symbol's name, from its recorded line
    `at` (1-indexed, already shift-corrected), or None.

    The graph records a C declaration at its first line, so the name sits on it
    or 1-2 lines below (return type on its own line), occasionally one above —
    the original probe window. When a comment block sits wedged inside the
    declaration (older-insert damage), the name is further down than the window
    reaches: from a type/qualifier first line, walk forward over type, blank and
    comment lines — never matching the name against comment text, which may
    legitimately mention it — until the first other code line."""
    for j in (at - 1, at, at - 2, at + 1):
        if 0 <= j < len(lines) and word.search(lines[j]):
            return j
    j = at - 1
    if not (0 <= j < len(lines)) or not _DECL_PREFIX.match(lines[j].strip()):
        return None
    k, hops = j + 1, 0
    while k < len(lines) and hops < _WEDGE_MAX:
        s = lines[k].strip()
        if s.startswith("/*"):
            while k < len(lines) and "*/" not in lines[k]:
                k, hops = k + 1, hops + 1
        elif s and not s.startswith("//"):
            if word.search(lines[k]):
                return k
            if not _DECL_PREFIX.match(s):
                return None
        k, hops = k + 1, hops + 1
    return None


def _at_definition(lines: list[str], i: int) -> bool:
    """True when line `i` is at a scope where a definition can live: not inside a
    parameter list, and not inside a function body.

    The landing line is found by matching the symbol's name, which also matches
    the name's *uses* — a parameter of that type, a local of that type. Writing
    there wedges a doc comment into a signature (which corrupts how the
    declaration renders) or onto a local variable (which documents nothing).
    Member scopes are allowed through: a struct or class body is where a member's
    doc belongs. A `{` straight after `)` opens a function body even when the
    signature names a struct return type (`static struct s *get(void) {`).
    Delimiters inside strings, chars and comments do not count."""
    paren = 0
    scopes: list[bool] = []  # per open brace: True when it opened a member scope
    in_block = False
    last = ""  # last non-space code char before the brace being classified
    for ln in lines[:i]:
        if in_block:
            if "*/" not in ln:
                continue
            ln = ln.split("*/", 1)[1]
            in_block = False
        s = re.sub(r'"(\\.|[^"\\])*"', '""', ln)
        s = re.sub(r"'(\\.|[^'\\])*'", "''", s)
        s = re.sub(r"/\*.*?\*/", "", s)
        if "/*" in s:
            s, in_block = s.split("/*", 1)[0], True
        s = re.sub(r"//.*", "", s)
        member = bool(_MEMBER_SCOPE.search(s))
        paren += s.count("(") - s.count(")")
        for ch in s:
            if ch == "{":
                scopes.append(member and last != ")")
            elif ch == "}" and scopes:
                scopes.pop()
            if not ch.isspace():
                last = ch
    return paren <= 0 and all(scopes)


def _rewrite_above(ctx: Context, row: dict, text: str, shifts: dict) -> str | None:
    """Replace (or, when absent, insert) the Doxygen doc comment above a C-family
    declaration. Locates the decl by its recorded line — shift-corrected for earlier
    rewrites in this run — confirmed by the symbol's name on (or within two lines of)
    the landing line; swaps the existing doc block found by `_doc_span` for a fresh
    `/** */` block, or inserts one when there's no doc there yet (a symbol documented
    only on its header prototype). A comment block an older insert wedged inside the
    declaration is deleted on the way — the rewrite self-heals that damage instead of
    upgrading it in place. Nothing is written when the decl can't be located or the
    draft is empty."""
    name = row["symbol"].split(".")[-1]
    path = ctx.root / row["file"]
    try:
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    except (OSError, UnicodeDecodeError) as e:
        return str(e)
    at = row.get("line")
    if not isinstance(at, int):
        return "no recorded line"
    off = sum(n for pos, n in shifts.get(row["file"], ()) if pos <= at)
    word = re.compile(rf"\b{re.escape(name)}\b")
    i = _find_decl(lines, at + off, word)
    if i is None:
        return "definition not found"
    # no suffix gate: rewrite briefs are already C-family only (scoped in briefs)
    if not _at_definition(lines, i):
        return "landing line is not a definition"
    decl = _decl_start(lines, i)
    delta = 0
    for a, b in reversed(_wedged_spans(lines, decl, i)):
        del lines[a : b + 1]
        delta -= b - a + 1
    block = _doxygen_block(text, re.match(r"\s*", lines[decl]).group(0))
    if block is None:
        return "empty draft"
    span = extract.doc_span(lines, decl)
    if span is not None:
        start, end = span
        removed = end - start + 1
        lines[start : end + 1] = [block]
        delta += block.count("\n") - removed
    else:
        lines.insert(decl, block)
        delta += block.count("\n")
    path.write_text("".join(lines), encoding="utf-8")
    shifts.setdefault(row["file"], []).append((at, delta))
    return None


def _insert_module(
    ctx: Context, row: dict, text: str, shifts: dict | None = None
) -> str | None:
    """Insert `text` as the module's top-of-file prose: a comment block directly
    above a Go `package` clause, a docstring as a Python file's first statement
    (after any leading `#!`/`#` comment lines), a comment block at the top of
    any other doc-above file (after a shebang)."""
    path = ctx.root / row["file"]
    try:
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    except (OSError, UnicodeDecodeError) as e:
        return str(e)
    prefix = _comment_prefix(row["file"])
    if row["file"].endswith(".go"):
        i = next(
            (j for j, ln in enumerate(lines) if re.match(r"package\s+\w+", ln)), None
        )
        if i is None:
            return "package clause not found"
        prev = lines[i - 1].strip() if i else ""
        if prev.startswith("//") or prev.endswith("*/"):
            return "already documented"
        lines.insert(i, _comment(text, ""))
    elif prefix:
        # first-symbol line (shift-corrected) disambiguates exactly as the
        # brief's emitter did: a top comment adjacent to it is the symbol's
        at = row.get("line")
        if isinstance(at, int) and shifts:
            at += sum(n for pos, n in shifts.get(row["file"], ()) if pos <= at)
        if extract.module_doc(path, at if isinstance(at, int) else None):
            return "already documented"
        i = 1 if lines and lines[0].startswith("#!") else 0
        # Doxygen only reads a file's lead prose out of a `/** */` block, and only
        # credits it to the file when it carries `@file`; a `//` run at the top of
        # a .c is invisible to it.
        if row["file"].endswith(extract.CFAMILY):
            # the name stays alone on its line: `\file` takes a single word, and
            # anything glued to it would change the file Doxygen credits.
            block = _doxygen_block(f"@file {Path(row['file']).name}\n{text}", "")
            if block is None:
                return "empty draft"
        else:
            block = _comment(text, "", prefix)
        lines.insert(i, block)
    else:
        if '"""' in text:
            return "draft contains a docstring delimiter"
        if extract.module_doc(path):
            return "already documented"
        i = 0
        while i < len(lines) and (
            not lines[i].strip() or lines[i].lstrip().startswith("#")
        ):
            i += 1
        doc = text.splitlines()
        if len(doc) == 1:
            block = f'"""{doc[0]}"""\n'
        else:
            body = "".join(ln.rstrip() + "\n" for ln in doc[1:])
            block = f'"""{doc[0]}\n{body}"""\n'
        if i < len(lines) and lines[i].strip():
            block += "\n"
        lines.insert(i, block)
    path.write_text("".join(lines), encoding="utf-8")
    return None


def _insert_go(ctx: Context, row: dict, text: str) -> str | None:
    """Insert `text` as a Go doc comment: find the declaration (the recorded
    line, re-located by name if it shifted), write the `//` block directly
    above."""
    name = row["symbol"].split(".")[-1]
    path = ctx.root / row["file"]
    try:
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    except (OSError, UnicodeDecodeError) as e:
        return str(e)
    i, err = _locate(lines, _go_def_re(name), row.get("line"))
    if err:
        return err
    prev = lines[i - 1].strip() if i else ""
    if prev.startswith("//") or prev.endswith("*/"):
        return "already documented"
    lines.insert(i, _comment(text, re.match(r"\s*", lines[i]).group(0)))
    path.write_text("".join(lines), encoding="utf-8")
    return None


def _insert_above(ctx: Context, row: dict, text: str, shifts: dict) -> str | None:
    """Insert `text` as a doc comment above the declaration in any doc-above
    language (C family, Rust, JS/TS, shell, …). There is no per-language
    declaration grammar here: the graph's recorded line is trusted, corrected
    through `shifts` — the lines earlier inserts in this run added above it —
    and the symbol's name must appear on (or within two lines of) the landing
    line, else nothing is written."""
    name = row["symbol"].split(".")[-1]
    prefix = _comment_prefix(row["file"]) or "//"
    path = ctx.root / row["file"]
    try:
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    except (OSError, UnicodeDecodeError) as e:
        return str(e)
    at = row.get("line")
    if not isinstance(at, int):
        return "no recorded line"
    off = sum(n for pos, n in shifts.get(row["file"], ()) if pos <= at)
    word = re.compile(rf"\b{re.escape(name)}\b")
    i = _find_decl(lines, at + off, word)
    if i is None:
        return "definition not found"
    cfamily = row["file"].endswith(extract.CFAMILY)
    if cfamily:
        if not _at_definition(lines, i):
            return "landing line is not a definition"
        i = _decl_start(lines, i)
    if extract.doc_above(lines, i, hash_ok=prefix == "#"):
        return "already documented"
    ind = re.match(r"\s*", lines[i]).group(0)
    # Doxygen reads `/** */`, never a `//` run: a line comment here would leave
    # the symbol undocumented in the very tool the language documents with.
    block = _doxygen_block(text, ind) if cfamily else _comment(text, ind, prefix)
    if block is None:
        return "empty draft"
    lines.insert(i, block)
    path.write_text("".join(lines), encoding="utf-8")
    shifts.setdefault(row["file"], []).append((at, block.count("\n")))
    return None


def _insert_py(ctx: Context, row: dict, text: str) -> str | None:
    """Insert `text` as a Python docstring: find the def line, walk to the end
    of the signature, indent to the body, write."""
    if '"""' in text:
        return "draft contains a docstring delimiter"
    name = row["symbol"].split(".")[-1]
    path = ctx.root / row["file"]
    try:
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    except (OSError, UnicodeDecodeError) as e:
        return str(e)
    i, err = _locate(lines, _def_re(name), row.get("line"))
    if err:
        return err
    depth, end = 0, None
    for j in range(i, len(lines)):
        code = lines[j].split("#", 1)[0]
        depth += sum(code.count(c) for c in "([") - sum(code.count(c) for c in ")]")
        if depth <= 0 and code.rstrip().endswith(":"):
            end = j
            break
    if end is None:
        return "signature end not found"
    nxt = next((ln for ln in lines[end + 1 :] if ln.strip()), "")
    if nxt.lstrip()[:1] in ("'", '"'):
        return "already documented"
    ind = (
        re.match(r"\s*", nxt).group(0)
        if nxt
        else re.match(r"\s*", lines[i]).group(0) + "    "
    )
    doc = text.splitlines()
    if len(doc) == 1:
        block = f'{ind}"""{doc[0]}"""\n'
    else:
        body = "".join((f"{ind}{ln}").rstrip() + "\n" for ln in doc[1:])
        block = f'{ind}"""{doc[0]}\n{body}{ind}"""\n'
    lines.insert(end + 1, block)
    path.write_text("".join(lines), encoding="utf-8")
    return None


def _stream(
    argv: list[str],
    prompt: str,
    cwd,
    timeout: int,
    on_text,
    on_think=None,
    meter: _Spend | None = None,
    procs: set | None = None,
) -> tuple[int, str, str, bool]:
    """Run one model call, delivering its reply incrementally: each stdout line
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
    agentic path keeps thinking; repairs are judgment work."""
    proc = subprocess.Popen(
        argv,
        env={**os.environ, "MAX_THINKING_TOKENS": "0"},
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
    )
    if procs is not None:
        procs.add(proc)
    key = object()  # this call's identity in the shared meter
    timed_out = False

    def _kill():
        """Timer callback: mark the call timed out and kill the model process."""
        nonlocal timed_out
        timed_out = True
        proc.kill()

    timer = threading.Timer(timeout, _kill)
    timer.start()
    parts: list[str] = []
    final: str | None = None
    try:
        try:
            proc.stdin.write(prompt.encode())
            proc.stdin.close()
        except BrokenPipeError:
            pass  # the process died first; its exit code tells the story
        for raw in proc.stdout:
            line = raw.decode(errors="replace")
            chunk = None
            try:
                ev = json.loads(line)
            except ValueError:
                chunk = line  # not stream-json: a scripted model's plain reply
            else:
                if ev.get("type") == "stream_event":
                    e = ev.get("event", {})
                    d = e.get("delta", {})
                    if d.get("type") == "text_delta":
                        chunk = d.get("text", "")
                    elif d.get("type") == "thinking_delta" and on_think:
                        on_think()
                    elif meter and e.get("type") == "message_start":
                        meter.message(key, e.get("message", {}).get("usage") or {})
                    elif meter and e.get("type") == "message_delta":
                        meter.delta(key, e.get("usage") or {})
                elif ev.get("type") == "result":
                    if isinstance(ev.get("result"), str):
                        final = ev["result"]
                    if meter:
                        meter.settle(ev, key)
            if chunk:
                parts.append(chunk)
                on_text(chunk)
        err = proc.stderr.read().decode(errors="replace")
        rc = proc.wait()
    except KeyboardInterrupt:
        proc.kill()
        raise
    finally:
        timer.cancel()
        if procs is not None:
            procs.discard(proc)
    return rc, "".join(parts) or (final or ""), err, timed_out


def _lanes(rows: list[dict]) -> list[list[dict]]:
    """File-disjoint lanes for concurrent drafting: rows grouped by file (a
    file's symbol orders and its trailing module order stay together, in
    order), groups packed onto up to _WORKERS lanes, least-loaded first. Two
    lanes never touch the same file, so parallel inserts can't fight over
    line numbers."""
    if not rows:
        return []
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(r["file"], []).append(r)
    lanes: list[list[dict]] = [[] for _ in range(min(_WORKERS, len(groups)))]
    for g in groups.values():
        min(lanes, key=len).extend(g)
    return [lane for lane in lanes if lane]


def _draft_batch(
    ctx: Context,
    rows: list[dict],
    briefs_dir: Path,
    model: str,
    timeout: int,
    cmd: list[str] | None,
    spend: _Spend,
    touched: set | None = None,
    budget: float | None = None,
) -> int:
    """The batched single-turn path for undocumented symbols and modules: one
    model call per _BATCH briefs, up to _WORKERS calls in flight at once over
    file-disjoint lanes. With a `budget`, no new call starts once the settled
    spend reaches it (in-flight calls finish, so the stop can overshoot by up
    to one call per lane) — the remainder is reported, not failed. The run reads as one ✓ line per docstring, printed
    the moment its block completes — file, symbol, drafted summary; a single
    spinner covers the rest (waiting, thinking, then a running n/m count over
    the whole run), carrying the live token/dollar spend on the same line.
    The full text is for `git diff` — the terminal shows each draft once.
    Ends with a run total. Returns the failure count. Ctrl-C kills every
    in-flight call, accounts for what landed, then propagates."""
    totals: dict = {"files": set(), "added": 0, "removed": 0}
    lanes = _lanes(rows)
    if not lanes:
        return 0
    lock = threading.Lock()  # guards the terminal, counters, and tallies
    state = {"done": 0, "failures": 0}
    phase = {"msg": f"{model}: waiting for the first tokens"}
    procs: set = set()  # every in-flight model process — Ctrl-C kills them all
    stop = threading.Event()
    over = threading.Event()  # the --budget cap was hit; lanes drain, none restart
    inflight: list[dict] = []  # before-snapshots of chunks still drafting
    spin = ui.spinner(phase["msg"])

    def _show() -> None:
        """Re-render the spinner: current phase, then the live spend."""
        tail = f"  ·  {spend.label()}" if spend.measured else ""
        spin.update(phase["msg"] + tail)

    spend.on_change = lambda: (lock.acquire(), _show(), lock.release()) and None

    def _lane(lane_rows: list[dict]) -> None:
        """Drive one lane: its chunks run serially, so this lane's files only
        ever see one writer, and the per-file line shifts stay coherent."""
        shifts: dict = {}
        for at in range(0, len(lane_rows), _BATCH):
            if stop.is_set():
                return
            if budget is not None and spend.spent() >= budget:
                over.set()
                return
            chunk = lane_rows[at : at + _BATCH]
            prompt = _batch_prompt(chunk, briefs_dir)
            before = {
                row["file"]: (ctx.root / row["file"]).read_text(
                    encoding="utf-8", errors="replace"
                )
                for row in chunk
            }
            with lock:
                inflight.append(before)
            buf, seen = "", set()

            def _think() -> None:
                """First thinking delta: the silence is reasoning, say so."""
                with lock:
                    phase["msg"] = f"{model} is thinking — drafts land next"
                    _show()

            def _absorb(text_chunk: str) -> None:
                """Fold in the chunk; insert and announce any completed block."""
                nonlocal buf
                buf += text_chunk
                for m in _BLOCK.finditer(buf):
                    n = int(m.group(1))
                    if n in seen or not (1 <= n <= len(chunk)):
                        continue
                    seen.add(n)
                    row = chunk[n - 1]
                    text = _clean(m.group(2))
                    err = _insert(ctx, row, text, shifts) if text else "empty draft"
                    with lock:
                        if err:
                            state["failures"] += 1
                            label = f"{row['kind']}  {row['file']}  ({row['symbol']})"
                            ui.fail(f"failed   {label}  — {err}")
                        else:
                            first = text.splitlines()[0]
                            ui.ok(f"{row['file']}  {row['symbol']} — {first}")
                        state["done"] += 1
                        phase["msg"] = (
                            f"{model} is writing — {state['done']}/{len(rows)} drafted"
                        )
                        _show()

            rc, text, stderr, timed_out = _stream(
                cmd or _cmd_text(model),
                prompt,
                ctx.root,
                timeout,
                _absorb,
                on_think=_think,
                meter=spend,
                procs=procs,
            )
            if stop.is_set():  # interrupted: the main thread owns the epilogue
                return
            if buf != text:  # deltas missed (e.g. result-only reply): parse whole
                _absorb(text[len(buf) :] if text.startswith(buf) else text)
            with lock:
                if timed_out:
                    ui.fail(
                        f"timeout  batch of {len(chunk)} docstring(s)  (>{timeout}s)"
                    )
                elif rc != 0:
                    tail = stderr.strip().splitlines()
                    ui.fail(
                        f"failed   batch of {len(chunk)} docstring(s)"
                        + (f"  — {tail[-1]}" if tail else "")
                    )
                for n, row in enumerate(chunk, 1):
                    if n not in seen:
                        state["failures"] += 1
                        label = f"{row['kind']}  {row['file']}  ({row['symbol']})"
                        ui.fail(f"failed   {label}  — no draft in the reply")
                inflight.remove(before)
                _tally(ctx, before, totals)

    ex = concurrent.futures.ThreadPoolExecutor(max_workers=len(lanes))
    futs = [ex.submit(_lane, lane) for lane in lanes]
    try:
        for f in concurrent.futures.as_completed(futs):
            f.result()
    except FileNotFoundError:
        stop.set()
        for p in list(procs):
            p.kill()
        ex.shutdown(wait=True)
        spin.stop()
        ui.fail("fix: `claude` CLI not found — install Claude Code to use --fix")
        return state["failures"] + len(rows) - state["done"]
    except KeyboardInterrupt:
        stop.set()
        for p in list(procs):
            p.kill()
        ex.shutdown(wait=True)
        spin.stop()
        ui.fail(
            f"interrupted — {state['done']} draft(s) had landed; "
            "they stay as uncommitted edits"
        )
        for before in inflight:
            _tally(ctx, before, totals)
        _totals_line(totals, spend)
        raise
    ex.shutdown(wait=True)
    spin.stop()
    _totals_line(totals, spend)
    if over.is_set():
        left = len(rows) - state["done"] - state["failures"]
        ui.warn(
            f"fix: --budget ${budget:.2f} reached — {left} work order(s) not "
            "drafted; re-run to continue"
        )
    if touched is not None:  # every batch target is a source file
        touched.update(totals["files"])
    return state["failures"]


def _totals_line(totals: dict, spend: _Spend) -> None:
    """The run-total header every drafting path (and its interrupt) ends with —
    what landed is always accounted for, even when the run didn't finish. The
    spend joins it when measured, so the spinner's meter outlives the spinner."""
    if totals["files"]:
        cost = f", {spend.label()}" if spend.measured else ""
        ui.header(
            f"fix: {len(totals['files'])} file(s) touched, "
            f"+{totals['added']} −{totals['removed']} line(s){cost} — drafts are "
            "uncommitted, review with git diff"
        )


def _split(index: list[dict]) -> tuple[list[dict], list[dict]]:
    """(batchable, agentic) work orders: undocumented symbols, modules, and
    C-family rewrites in any language documate can insert into deterministically
    (Python, Go, and every doc-above comment language) take the single-turn batched
    path; drift repairs and anything else keep the in-place agent."""
    batch = [
        r
        for r in index
        if r["kind"] in ("undocumented", "module", "rewrite")
        and (r["file"].endswith(".py") or _comment_prefix(r["file"]) is not None)
    ]
    keep = {id(r) for r in batch}
    return batch, [r for r in index if id(r) not in keep]


def _snapshot(ctx: Context, row: dict) -> dict[str, str]:
    """{relpath: content} of the work order's target files (the source file, and
    the authored page for drift rows) before the model runs — the baseline the
    live draft view diffs against. A brief names its targets, so this is where
    every edit is expected to land."""
    snap: dict[str, str] = {}
    for key in ("file", "page"):
        rel = row.get(key)
        if not rel:
            continue
        try:
            snap[rel] = (ctx.root / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            snap[rel] = ""
    return snap


def _tally(ctx: Context, before: dict[str, str], totals: dict) -> None:
    """Fold what the model just changed in the snapshotted files into the run
    totals (counted even for a failed/timed-out/interrupted call — a partial
    edit must not go unaccounted). Accounting only: the terminal shows each
    draft once, as its ✓ line; the full text belongs to `git diff`."""
    for rel, old in before.items():
        try:
            new = (ctx.root / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            new = ""
        if new == old:
            continue
        added = removed = 0
        for ln in difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm=""):
            if ln.startswith(("+++", "---")):
                continue
            if ln.startswith("+"):
                added += 1
            elif ln.startswith("-"):
                removed += 1
        totals["files"].add(rel)
        totals["added"] += added
        totals["removed"] += removed


def _draft(
    ctx: Context,
    index: list[dict],
    briefs_dir: Path,
    model: str,
    timeout: int,
    cmd: list[str] | None,
    spend: _Spend,
    touched: set | None = None,
    budget: float | None = None,
) -> int:
    """Feed each work order to the model (brief on stdin, repo as cwd), showing
    live progress. With a `budget`, no new call starts once the settled spend
    reaches it — the remainder is reported, not failed. — the in-flight brief on the bar (with the run's spend so
    far), one ✓/✗ line per outcome. Each call's result JSON settles the meter.
    Ends with a run total. Returns the number of failures; a missing claude CLI
    fails every order with one clear hint instead of a stack trace, and Ctrl-C
    shows any partial edit of the interrupted order before propagating."""
    failures = 0
    totals: dict = {"files": set(), "added": 0, "removed": 0}
    with ui.tracker(len(index)) as track:
        for n, row in enumerate(index):
            if budget is not None and spend.spent() >= budget:
                ui.warn(
                    f"fix: --budget ${budget:.2f} reached — {len(index) - n} work "
                    "order(s) not drafted; re-run to continue"
                )
                break
            label = (
                f"{row['kind']}  {row.get('page') or row['file']}  ({row['symbol']})"
            )
            track.working(label + (f"  ·  {spend.label()}" if spend.measured else ""))
            before = _snapshot(ctx, row)
            brief = briefs_dir / row["brief"]
            try:
                with brief.open("rb") as fh:
                    r = subprocess.run(
                        cmd or _cmd(model),
                        stdin=fh,
                        cwd=ctx.root,
                        capture_output=True,
                        timeout=timeout,
                    )
            except FileNotFoundError:
                ui.fail(
                    "fix: `claude` CLI not found — install Claude Code to use --fix"
                )
                return failures + len(index)
            except subprocess.TimeoutExpired:
                failures += 1
                track.failed(f"timeout  {label}  (>{timeout}s)")
                _tally(ctx, before, totals)
                continue
            except KeyboardInterrupt:
                ui.fail(f"interrupted — {label} was in flight; checking for edits")
                _tally(ctx, before, totals)
                _totals_line(totals, spend)
                raise
            try:  # -p --output-format json: one result object, usage + cost
                payload = json.loads(r.stdout.decode(errors="replace"))
            except ValueError:
                payload = None  # scripted stand-ins print plain text
            if isinstance(payload, dict):
                spend.settle(payload)
            if r.returncode != 0:
                failures += 1
                tail = r.stderr.decode(errors="replace").strip().splitlines()
                track.failed(f"failed   {label}" + (f"  — {tail[-1]}" if tail else ""))
            else:
                track.done(f"drafted  {label}")
            _tally(ctx, before, totals)
    _totals_line(totals, spend)
    if touched is not None:  # source targets only — drift rows also edit .md pages
        touched.update(
            f for f in totals["files"] if any(r.get("file") == f for r in index)
        )
    return failures


_OUT_EST = 35  # ≈ tokens one drafted docstring costs on the way out


def _tok(n: int) -> str:
    """A token count as a compact human number (874 → '874', 1934 → '1.9k')."""
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def _preflight(
    ctx: Context,
    index: list[dict],
    briefs_dir: Path,
    model: str,
    yes: bool,
    budget: float | None = None,
) -> bool | None:
    """No model call starts unannounced: show exactly what --ai is about to do
    — every symbol, every file, how many calls, and a token estimate measured
    over the very prompts it would send (chars/4; the agentic path's number is
    a floor, since the agent may Read more) — then ask. Returns True to
    proceed, False on an explicit decline (a clean no-op), and None when there
    is no terminal to ask and no --yes (refused: unattended spend needs to be
    opted into)."""
    batch, agentic = _split(index)
    chunks = [
        lane[i : i + _BATCH]
        for lane in _lanes(batch)
        for i in range(0, len(lane), _BATCH)
    ]
    calls = len(chunks) + len(agentic)
    tok_in = sum(len(_batch_prompt(c, briefs_dir)) for c in chunks) // 4
    ag_in = (
        sum(len((briefs_dir / r["brief"]).read_text(encoding="utf-8")) for r in agentic)
        // 4
    )
    lines: list[tuple[str, str | None]] = [
        (f"{len(index)} work order(s) · {calls} {model} call(s)", "bold"),
    ]
    if batch:
        by_file: dict[str, list[str]] = {}
        for r in batch:
            by_file.setdefault(r["file"], []).append(r["symbol"].split(".")[-1])
        if agentic:  # only label the section when there's another one to tell apart
            lines.append(("docstring drafts (model writes text only):", None))
        for f, syms in sorted(by_file.items()):
            shown = ", ".join(syms[:8]) + (
                f", +{len(syms) - 8} more" if len(syms) > 8 else ""
            )
            lines.append((f"  {f}  ({len(syms)}): {shown}", "cyan"))
    if agentic:
        lines.append(("in-place repairs (agent edits your files, Read+Edit):", None))
        for r in agentic:
            lines.append(
                (
                    f"  {r['kind']}  {r.get('page') or r['file']}  ({r['symbol']})",
                    "yellow",
                )
            )
    est = f"tokens  ≈ {_tok(tok_in + ag_in)} in"
    if batch:
        est += f" · ≈ {_tok(_OUT_EST * len(batch))} out"
    if agentic:
        est += "  (floor — the agent may Read more)"
    lines.append((est, None))
    safety = (
        f"safety  drafts land uncommitted · {_TIMEOUT}s/call · "
        f"cap {_CAP}/run · gate re-verifies"
    )
    if budget is not None:
        safety += f" · budget ${budget:.2f}"
    lines.append((safety, "dim"))
    ui.plan("--ai plan", lines)
    if yes:
        return True
    ok = ui.confirm(f"run {calls} {model} call(s)?")
    if ok is None:
        ui.fail(
            "fix: --ai asks before spending and there's no terminal to ask — "
            "pass --yes to run unattended"
        )
        return None
    if not ok:
        ui.note("fix: declined — no model was called, nothing changed")
        return False
    return True


def _only(index: list[dict], pattern: str | None) -> list[dict]:
    """The work orders whose repo-relative file (or authored page, for drift rows)
    matches the `--only` glob — fnmatch, so `*` crosses directories — leaving the
    context root alone: aiming a run at one subtree no longer means pointing the
    whole tool (and its `.documate/`, its `docs/`) at that subtree. Says what the
    filter dropped, so a too-narrow glob is visible instead of a silent no-op."""
    if not pattern:
        return index
    kept = [
        r
        for r in index
        if fnmatch.fnmatch(r["file"], pattern)
        or (r.get("page") and fnmatch.fnmatch(r["page"], pattern))
    ]
    if len(kept) < len(index):
        ui.note(f"fix: --only {pattern} keeps {len(kept)} of {len(index)} work order(s)")
    return kept


def _dry_run(
    ctx: Context,
    capped: list[dict],
    bdir: Path,
    model: str,
    budget: float | None = None,
) -> None:
    """`--dry-run`: show exactly what the run would do — the same pre-flight plan a
    real run confirms, work orders already on disk under `bdir` for inspection —
    then stop. No model call, no source edit."""
    _preflight(ctx, capped, bdir, model, yes=True, budget=budget)
    ui.note(
        f"fix: --dry-run — no model called, nothing edited; work orders are in "
        f"{ctx.rel(str(bdir))}"
    )


def _capped(index: list[dict]) -> list[dict]:
    """The first _CAP work orders; prints how many remain when truncated."""
    if len(index) > _CAP:
        ui.warn(
            f"fix: capping at {_CAP} of {len(index)} work order(s) — "
            "re-run to continue (callees draft first, so runs compose)"
        )
        return index[:_CAP]
    return index


def fix_check(
    ctx: Context,
    base: str | None,
    model: str,
    timeout: int = _TIMEOUT,
    cmd: list[str] | None = None,
    yes: bool = False,
    quiet: bool = False,
    only: str | None = None,
    dry: bool = False,
    budget: float | None = None,
) -> int:
    """`documate --check --ai`: run the gate, show the pre-flight plan and get
    consent, draft every emitted work order, regenerate the docs (drafted
    docstrings change the generated tier), then re-run the gate — its verdict
    is the exit code. Declining leaves the first gate's verdict untouched.
    `only` narrows the orders to one file glob; `dry` stops after the plan.
    With `quiet` (bare `--ai`, where seeding already reported) the leading gate
    is internal plumbing: a pass collapses to one line, failures stay loud."""
    base = base or ctx.config.default_base
    bdir = ctx.root / ".documate" / "briefs"
    rc = check.run(ctx, base, briefs_dir=bdir, quiet=quiet)
    index = _only(json.loads((bdir / "briefs.json").read_text()), only)
    if not index:
        if quiet and rc == 0:
            ui.ok("fix: gate passed — drafts verified")
        return rc
    capped = _capped(index)
    if dry:
        _dry_run(ctx, capped, bdir, model, budget)
        return rc
    go = _preflight(ctx, capped, bdir, model, yes, budget)
    if go is None:
        return 1
    if not go:
        return rc
    ui.header(f"fix: drafting {len(capped)} work order(s) with {model}")
    batch, agentic = _split(capped)
    spend = _Spend()  # one meter across both paths: the run's whole bill
    touched: set = set()
    shots = undo.snapshot(ctx, capped)
    try:
        if batch:
            _draft_batch(ctx, batch, bdir, model, timeout, cmd, spend, touched, budget)
        if agentic:
            _draft(ctx, agentic, bdir, model, timeout, cmd, spend, touched, budget)
    except KeyboardInterrupt:
        return _interrupted()
    finally:  # the --stats bill: even an interrupted run's tokens were spent
        stats.add_spend(ctx, model, spend.tokens, spend.usd)
        _run_format(ctx, touched)
        undo.record(ctx, shots, capped, "repair", model)
    _reindex(ctx)
    docs.run(ctx, quiet=True)
    rc = check.run(ctx, base, briefs_dir=bdir, quiet=True)
    if rc == 0:
        ui.ok("fix: gate passed — drafts verified")
    return rc


def _run_format(ctx: Context, touched: set[str]) -> None:
    """Run the repo's configured `format_cmd` over the source files a run touched,
    before the re-index reads them — inserted doc comments then land already
    conforming to the repo's formatter (a pinned clang-format CI gate would
    otherwise fail on every long `@brief` line the model wrote). The command is
    split shell-style with the repo-relative paths appended; any failure warns
    and never sinks the run — the drafts are already on disk."""
    cmd = ctx.config.format_cmd
    if not cmd or not touched:
        return
    argv = shlex.split(cmd) + sorted(touched)
    try:
        r = subprocess.run(argv, cwd=ctx.root, capture_output=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as e:
        ui.warn(f"fix: format_cmd failed — {e}")
        return
    if r.returncode != 0:
        tail = r.stderr.decode(errors="replace").strip().splitlines()
        ui.warn("fix: format_cmd exited nonzero" + (f" — {tail[-1]}" if tail else ""))
    else:
        ui.note(f"fix: format_cmd ran over {len(touched)} file(s)")


def _reindex(ctx: Context) -> None:
    """Best-effort incremental re-index after drafting: inserted doc lines
    shifted every declaration below them, and line-anchored extraction (the
    doc_above languages — Go, C) must read the files the drafts produced. A
    graph that won't rebuild (a foreign db) costs only re-emit freshness; it
    must never crash a run whose tokens are already spent."""
    try:
        ctx.graph.index(incremental=True)
    except Exception:
        ui.warn("fix: re-index failed — regenerated pages may lag the drafts")


def _interrupted() -> int:
    """The Ctrl-C epilogue: partial drafts were already shown and stay as
    uncommitted edits; say how to review, discard, or resume, and exit 130
    (the conventional SIGINT code) without a traceback."""
    ui.warn(
        "fix: interrupted — drafts so far are uncommitted (review: git diff · "
        "discard: git checkout -- <file> · resume: re-run, it skips what's done); "
        "run documate to refresh the pages"
    )
    return 130


def fix_docs(
    ctx: Context,
    model: str,
    timeout: int = _TIMEOUT,
    cmd: list[str] | None = None,
    yes: bool = False,
    only: str | None = None,
    dry: bool = False,
    budget: float | None = None,
) -> int:
    """`documate --ai`: the fresh-repo seeding pass. Generate the pages, show
    the pre-flight plan and get consent, draft a docstring for every
    undocumented symbol (callees first), then regenerate so the new docstrings
    land on the pages. `only` narrows the orders to one file glob; `dry` stops
    after the plan. Nonzero when any draft failed (or consent was
    impossible unattended); declining is a clean exit-0 no-op. The drafts
    themselves are uncommitted edits awaiting review."""
    rc = docs.run(ctx)
    if rc != 0:
        return rc
    bdir = ctx.root / ".documate" / "briefs"
    index = _only(
        briefs.emit(ctx, ctx.config.default_base, [], bdir, undocumented="all"), only
    )
    if not index:
        ui.ok("fix: nothing to draft" + ("" if only else " — every symbol is documented"))
        return 0
    capped = _capped(index)
    if dry:
        _dry_run(ctx, capped, bdir, model, budget)
        return 0
    go = _preflight(ctx, capped, bdir, model, yes, budget)
    if go is None:
        return 1
    if not go:
        return 0
    ui.header(f"fix: drafting {len(capped)} docstring(s) with {model}")
    batch, agentic = _split(capped)
    spend = _Spend()  # one meter across both paths: the run's whole bill
    failures = 0
    touched: set = set()
    shots = undo.snapshot(ctx, capped)
    try:
        if batch:
            failures += _draft_batch(
                ctx, batch, bdir, model, timeout, cmd, spend, touched, budget
            )
        if agentic:
            failures += _draft(
                ctx, agentic, bdir, model, timeout, cmd, spend, touched, budget
            )
    except KeyboardInterrupt:
        return _interrupted()
    finally:  # the --stats bill: even an interrupted run's tokens were spent
        stats.add_spend(ctx, model, spend.tokens, spend.usd)
        _run_format(ctx, touched)
        undo.record(ctx, shots, capped, "seed", model)
    _reindex(ctx)
    docs.run(ctx, quiet=True)
    # Re-emit so briefs.json reflects what still needs drafting (next run's map);
    # drift rows aren't this verb's business, hence the empty direct list.
    remaining = briefs.emit(ctx, ctx.config.default_base, [], bdir, undocumented="all")
    if remaining:
        ui.warn(f"fix: {len(remaining)} symbol(s) still undocumented")
    return 1 if failures else 0


def fix_rewrite(
    ctx: Context,
    model: str,
    timeout: int = _TIMEOUT,
    cmd: list[str] | None = None,
    yes: bool = False,
    only: str | None = None,
    dry: bool = False,
    budget: float | None = None,
) -> int:
    """`documate --ai <model> --rewrite`: re-emit every C/C++ symbol's doc comment
    as Doxygen (`/** */` with `@brief`/`@param`/`@return`) — the marker Doxygen reads,
    unlike the plain `//` seeding writes. Generate the pages, show the pre-flight plan
    and get consent, draft each rewrite (existing docs replaced in place, undocumented
    ones seeded), then regenerate so the pages reflect the new prose. `only` narrows
    the orders to one file glob; `dry` stops after the plan. Nonzero when any
    draft failed (or consent was impossible unattended); declining is a clean exit-0
    no-op. The drafts are uncommitted edits awaiting review."""
    rc = docs.run(ctx)
    if rc != 0:
        return rc
    bdir = ctx.root / ".documate" / "briefs"
    index = _only(briefs.emit(ctx, ctx.config.default_base, [], bdir, rewrite=True), only)
    if not index:
        ui.ok(
            "fix: no C/C++ symbols to rewrite"
            + (" (everything is already Doxygen, or --only matched nothing)" if only else "")
        )
        return 0
    capped = _capped(index)
    if dry:
        _dry_run(ctx, capped, bdir, model, budget)
        return 0
    go = _preflight(ctx, capped, bdir, model, yes, budget)
    if go is None:
        return 1
    if not go:
        return 0
    ui.header(f"fix: rewriting {len(capped)} doc comment(s) with {model}")
    spend = _Spend()
    failures = 0
    touched: set = set()
    shots = undo.snapshot(ctx, capped)
    try:
        failures += _draft_batch(
            ctx, capped, bdir, model, timeout, cmd, spend, touched, budget
        )
    except KeyboardInterrupt:
        return _interrupted()
    finally:  # the --stats bill: even an interrupted run's tokens were spent
        stats.add_spend(ctx, model, spend.tokens, spend.usd)
        _run_format(ctx, touched)
        undo.record(ctx, shots, capped, "rewrite", model)
    _reindex(ctx)
    docs.run(ctx, quiet=True)
    return 1 if failures else 0

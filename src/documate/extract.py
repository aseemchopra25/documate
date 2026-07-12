"""extract.py — pull the prose out of source, per language.

The prose in the generated docs is the doc you already wrote next to the code — never
invented. Python docstrings come via stdlib `ast`; everything else (C/C++/Go/Rust/JS/TS/
Java/...) via the doc-comment block above each symbol (the graph hands us the line,
source hands us the comment), plus the file-top comment block as the module's lead prose. It's the same leading `//`/`/** */` convention doxygen and
friends read, lifted with string ops — no external doc tool to install. Stdlib only.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path


def short(qualified: str) -> str:
    """The bare symbol name at the tail of a `path::name` qualified name."""
    return qualified.rsplit("::", 1)[-1] if "::" in qualified else qualified


def py_symbols(path: Path) -> dict:
    """{dotted name: (signature, docstring|None)} for every def/class in a Python file.

    Keys mirror the graph's qualified-name tails: a method is `Class.method` (so two
    classes with an `__init__` don't clobber each other's docstring), a def nested in a
    def stays bare — exactly how the engine qualifies them. The signature is rebuilt
    from the AST (the graph doesn't store one) and carries the same dotted name; the
    docstring is read straight from source — that's the truth the reference hangs off."""
    out: dict = {}
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
    except (OSError, SyntaxError, ValueError):
        return out

    def visit(node, prefix: str) -> None:
        """Walk children; only a ClassDef extends the dotted prefix for its members."""
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = prefix + child.name
                ret = f" -> {ast.unparse(child.returns)}" if child.returns else ""
                out[name] = (
                    f"{name}({ast.unparse(child.args)}){ret}",
                    ast.get_docstring(child),
                )
                visit(child, "")  # a def inside a def is bare in the graph too
            elif isinstance(child, ast.ClassDef):
                name = prefix + child.name
                bases = ", ".join(ast.unparse(b) for b in child.bases)
                head = f"class {name}({bases})" if bases else f"class {name}"
                out[name] = (head, ast.get_docstring(child))
                visit(child, name + ".")
            else:
                visit(child, prefix)  # defs under if/try keep the enclosing prefix

    visit(tree, "")
    return out


def doc_above(lines: list[str], decl_idx: int, hash_ok: bool = False) -> str | None:
    """The doc-comment block sitting immediately above a declaration (0-indexed line).

    Harvests a contiguous `//` / `///` / `//!` run or a `/* ... */` / `/** ... */` block —
    the convention every C-family / Go / Rust / JS-TS doc tool reads — skipping annotation
    and attribute lines (`@Override`, `#[inline]`) that wedge between the comment and the
    decl. With `hash_ok` (shell files, where `#` would otherwise be a directive) a `#` run
    counts too, minus the shebang and minus letter-free ASCII-art banner lines.
    A blank line breaks the claim (godoc/rustdoc/JSDoc all require adjacency —
    a blank-separated comment is the file's, not the symbol's; see `module_doc`).
    Markers stripped. None when there's nothing up there. Heuristic, not a parser:
    the graph already told us *where* the symbol is, so this is just "read the lines above"."""
    i = decl_idx - 1
    while i >= 0:  # hop annotations/attrs wedged between doc and decl
        s = lines[i].strip()
        if not s:
            return None  # blank gap: whatever is above belongs to the file, not us
        if s.startswith("@") or s.startswith("#["):
            i -= 1
            continue
        break
    if i < 0:
        return None
    out: list[str] = []
    if lines[i].strip().endswith("*/"):  # /* ... */ or /** ... */ block
        while i >= 0:
            t = lines[i].strip()
            body = t
            if "/*" in body:
                body = body[body.index("/*") + 2 :]
                body = body.lstrip("*")  # the doxygen/rustdoc /** lead star
            body = body.removesuffix("*/").strip().lstrip("*").strip()
            if body:
                out.append(body)
            if "/*" in t:
                break
            i -= 1
        out.reverse()
    elif lines[i].strip().startswith("//"):  # // /// //! line run
        while i >= 0 and lines[i].strip().startswith("//"):
            out.append(lines[i].strip().lstrip("/").lstrip("!").strip())
            i -= 1
        out.reverse()
    elif hash_ok and lines[i].strip().startswith("#"):  # shell `#` run
        while i >= 0 and lines[i].strip().startswith("#"):
            s = lines[i].strip()
            i -= 1
            if s.startswith("#!"):
                break  # the shebang is a directive, not prose
            body = s.lstrip("#").strip()
            if re.search(r"[A-Za-z]", body):  # letter-free lines are banner art
                out.append(body)
        out.reverse()
    else:
        return None
    text = "\n".join(x for x in out if x).strip()
    return text or None


def signature_at(lines: list[str], idx: int) -> str | None:
    """The full declaration starting at line `idx` (0-indexed), rejoined when it wraps.

    Skips leading attribute lines (`@discardableResult`, `#[inline]` — the graph often
    points at them, not the decl), then reads until the parameter parens balance,
    cutting the body (`{`) or a C prototype's `;` off. Capped at 8 lines: a signature
    is a signature, not a file."""
    while idx < len(lines) and lines[idx].strip().startswith(("@", "#[")):
        idx += 1
    parts: list[str] = []
    depth = 0
    for raw in lines[idx : idx + 8]:
        buf: list[str] = []
        stop = False
        for ch in raw:
            if depth == 0 and ch in "{;":
                stop = True
                break
            buf.append(ch)
            if ch in "([":
                depth += 1
            elif ch in ")]":
                depth -= 1
        parts.append("".join(buf))
        if stop or depth <= 0:
            break
    sig = " ".join(" ".join(parts).split())
    sig = sig.replace("( ", "(").replace(" )", ")").replace(",)", ")")
    return sig or None


#: implementation suffix -> sibling header suffixes, for the decl/def doc merge
_HEADERS = {
    ".c": (".h",),
    ".cc": (".h", ".hh", ".hpp"),
    ".cpp": (".h", ".hh", ".hpp"),
    ".cxx": (".h", ".hh", ".hpp"),
    ".m": (".h",),
}

_TYPE_KEYWORDS = "class|struct|enum|actor|protocol|interface|trait|type"

#: suffixes whose doc comments are `#` runs — elsewhere `#` is a directive (#include)
_HASH_DOCS = (".sh", ".bash", ".zsh")


def _sibling_header(path: Path) -> list[str]:
    """Lines of the header next to a C/C++/ObjC implementation file, [] when none.

    C splits declaration from definition and the doc usually sits on the header
    prototype, so a doc-less definition gets one more place to look — doxygen's
    decl/def merge, done with string ops."""
    for ext in _HEADERS.get(path.suffix, ()):
        try:
            return (
                path.with_suffix(ext)
                .read_text(encoding="utf-8", errors="ignore")
                .splitlines()
            )
        except OSError:
            continue
    return []


def _decl_line(lines: list[str], name: str, kind: str, avoid: int = -1) -> int | None:
    """The line index of another *documented* declaration of `name`, or None.

    The doc a reader wrote isn't always above the node the graph kept: C documents
    the header prototype, not the definition, and Swift's `extension Foo` can shadow
    `class Foo` (one node per qualified name). Functions match `name(`, types match a
    type keyword + name; comment lines never match (prose mentioning the name isn't a
    declaration), and `avoid` excludes the node's own line."""
    if kind == "Function":
        pat = re.compile(rf"\b{re.escape(name)}\s*\(")
    else:
        pat = re.compile(rf"\b(?:{_TYPE_KEYWORDS})\s+{re.escape(name)}\b")
    for i, line in enumerate(lines):
        s = line.strip()
        if i == avoid or s.startswith(("//", "/*", "*")):
            continue
        if pat.search(s) and doc_above(lines, i) is not None:
            return i
    return None


def comment_symbols(path: Path, syms: list) -> dict:
    """{dotted name: (signature, doc|None)} for a non-Python file: the graph gives each
    symbol's line, source gives the declaration (`signature_at`) and the comment above
    it (the doc). Keyed by the qualified-name tail (`Cart.total`, like `py_symbols`) so
    same-named methods on two classes keep their own docs.

    Unlike `py_symbols` (docstring lives *inside* the def) these languages put the doc
    *above*, so we lean on `line` from the graph rather than re-parsing the file. When
    nothing sits above the node's line, the doc may still exist somewhere else the
    author legitimately put it: on another declaration of the same name in this file
    (Swift class vs extension), or on the prototype in the sibling header (C) — both
    checked via `_decl_line` before giving up."""
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return {}
    hlines: list[str] | None = None  # sibling header, loaded on first miss
    hash_ok = path.suffix in _HASH_DOCS
    out: dict = {}
    for s in syms:
        idx = s["line"] - 1  # graph line_start is 1-indexed
        if not (0 <= idx < len(lines)):
            continue
        sig, doc = signature_at(lines, idx), doc_above(lines, idx, hash_ok)
        name = short(s["qualified"]).rsplit(".", 1)[-1]  # bare name, for text search
        if doc is None and s.get("kind") != "Function":
            alt = _decl_line(lines, name, s.get("kind", ""), avoid=idx)
            if alt is not None:  # the documented twin's decl is the better signature
                sig, doc = signature_at(lines, alt), doc_above(lines, alt)
        if doc is None:
            if hlines is None:
                hlines = _sibling_header(path)
            alt = _decl_line(hlines, name, s.get("kind", ""))
            if alt is not None:
                doc = doc_above(hlines, alt)
        if sig and name not in sig:
            sig = None  # decl cut at `{` lost the name (anonymous typedef struct):
            # a "signature" that doesn't name the symbol is worse than the bare name
        out[short(s["qualified"])] = (sig, doc)
    return out


def extract(path: Path, syms: list) -> dict:
    """Per-language doc extraction: Python through stdlib `ast`, everything else through the
    comment-above-declaration harvester. The one place that knows a file's language."""
    if path.suffix == ".py":
        return py_symbols(path)
    return comment_symbols(path, syms)


_BOILER = re.compile(
    r"copyright|licen[cs]e|spdx|warranty|all rights reserved|bug reports|do not edit",
    re.I,
)


def module_doc(path: Path, first_line: int | None = None) -> str | None:
    """The module-level prose of a file: Python's module docstring; any other language's
    comment block at the top of the file (Go's `// Package x ...`, a doxygen `@file`
    header, a shell script's `#` header, a licence-free lead comment).

    Two disambiguations. A copyright/license/SPDX block is boilerplate, not prose —
    skipped, and the next comment block gets its turn. And a top comment sitting
    *directly above* the first symbol (`first_line`, 1-indexed, from the graph) is
    that symbol's doc — `doc_above` will claim it — so it is not also the module's.
    Anything else blank-separated from the code below it is module prose."""
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return None
    if path.suffix == ".py":
        try:
            return ast.get_docstring(ast.parse("\n".join(lines)))
        except (SyntaxError, ValueError):
            return None
    hash_ok = path.suffix in _HASH_DOCS
    i = 1 if lines and lines[0].startswith("#!") else 0
    while True:
        while i < len(lines) and not lines[i].strip():
            i += 1
        if i >= len(lines):
            return None
        head = lines[i].strip()
        if head.startswith("//"):
            end = i
            while end + 1 < len(lines) and lines[end + 1].strip().startswith("//"):
                end += 1
        elif head.startswith("/*"):
            end = i
            while end < len(lines) and "*/" not in lines[end]:
                end += 1
            if end >= len(lines):
                return None
        elif hash_ok and head.startswith("#"):
            end = i
            while end + 1 < len(lines) and lines[end + 1].strip().startswith("#"):
                end += 1
        else:
            return None
        text = doc_above(lines, end + 1, hash_ok)
        if text and _BOILER.search(text):
            i = end + 1  # boilerplate: skip the block, try the next one
            continue
        if first_line is not None and end == first_line - 2:
            return None  # adjacent to the first symbol: its doc, not the module's
        return text

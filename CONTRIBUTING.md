# Contributing to documate

Small tool, simple rules. The whole point is binding docs to code so they can't quietly
rot, so the bar is: don't let documate's own docs rot, and don't break the gate.

## Setup

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e .                 # stdlib tool + the vendored tree-sitter engine
make test                        # or: python -m unittest discover -s tests -v
```

`tree-sitter-language-pack` is pinned `<1` on purpose — 1.x reshuffled the grammar API and
breaks the vendored parser. Don't bump it past that.

## The tool is one command

- `documate` — index the repo, (re)write `docs/`, then gate the result.
- `documate --check` — the gate alone, writes nothing (CI and the pre-commit hook).

Everything else is a flag; `documate --help` is the whole surface on one screen.

## Layout

```
src/documate/            the tool — repo-agnostic logic, stdlib-only
src/documate/_engine/    the tree-sitter → sqlite indexer (MIT; see _engine/ORIGIN.md)
src/documate/graphdb.py  the ONLY module that touches the engine / sqlite schema
tests/test_documate.py   one file, stdlib unittest, throwaway git fixtures
```

The engine is first-party code (it began as a copy of code-review-graph — provenance and
license in `_engine/ORIGIN.md`). Adapt to it through `graphdb.py`; never edit `_engine/*`
to make documate work — put that logic behind the adapter instead.

## Rules

- **Anchor what you hand-write.** New authored prose about a symbol (under `docs/guides/`)
  gets a `<!-- documents: sym:NAME -->` anchor, or the drift gate can't protect it.
  `documate --check` prints a `sig:` fingerprint you can add to pin the anchor exactly.
- **Never hand-edit generated pages.** `docs/README.md`, `docs/ARCHITECTURE.md`, and
  `docs/architecture/*` are generated (each opens with a stamp line). Fix the source — a
  docstring, or an anchor — then regenerate with `documate`.
- **Run the gate before you commit.** `documate --check`, or install the hook:
  `git config core.hooksPath hooks` (or use `.pre-commit-hooks.yaml`). The pre-commit
  hook is self-healing — it regenerates the generated pages and stages them, then gates.
  Skip it with `--no-verify`; override the base ref with `DOCUMATE_BASE=<ref>`.
- **Stdlib for the tool.** documate's own modules import only the stdlib (plus rich for
  output, and the `_engine` adapter). Heavy deps (tree-sitter, networkx) live behind
  `_engine`.

## Tests

TDD-ish: a failing test first when you can. Cover the degraded-graph path — a missing or
locked `graph.db` must soft-pass, never raise; that's the contract the whole design leans
on. The engine ships no tests of its own, so cover engine changes with a `RealGraph`-style
fixture here. `make`-free: just `python -m unittest discover -s tests`.

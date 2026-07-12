# v2 direction: docs engine, two verbs (2026-07-08)

Recorded so a future session doesn't re-litigate the pivot.

## The reframe

v0.1 grew checker-first: 13 CLI verbs (index/build/check/validate/drift/plan/seed/
outline/seams/reference/mermaid/suggest/mcp/serve) wrapped in jargon — anchors, sidecar,
ripple, seams, bundles. The owner's verdict: opaque, and the surface was the problem, not
the core. Direction reset with the owner in the loop:

> documate is a **documentation engine**. Point it at a repo (large monorepo,
> multi-framework) → get simple, beautiful, current docs → they stay current
> automatically. Two user-facing verbs; everything else is internal plumbing.

- `documate docs` — index (incremental) → page model → committed markdown under `docs/`.
- `documate check` — one gate: generated tier fresh (regenerate + diff), anchors resolve,
  authored pages not lying vs `--base` (DIRECT gates, RIPPLE advisory).

## What was cut, and why

| cut | why |
|---|---|
| `mcp` server | owner: "I don't want this to be an MCP" — the integration surface is the CLI (and later, files) |
| `seams`, `opcode:`/`kconfig:` anchors | niche systems-repo flavours from a demo phase; concept-count without users |
| `suggest`, `mermaid`, `ripple_engine` | marginal features; generated diagrams are correct by construction now |
| `serve`/viz | already deleted upstream; the path was a dead ImportError |
| committed sidecar (`docs/.anchors.json`) | a cache that had to be kept fresh — pure overhead; anchors are scanned fresh each run |
| `plan`/`seed`/`outline`/`reference` as verbs | folded into `docs.py` + `extract.py` as internals |
| `hooks/pre-push` Claude loop, `.claude/skills` | drove removed commands; return with the prose layer below |

This supersedes `notes/generation-plan.md` (deleted with this note): its O(diff) thesis —
the graph turns a diff into exact changed symbols, so an LLM needs bundle-sized context,
never the repo — survives and is why `graphdb.changed_symbols` is kept despite having no
caller today.

## Repomix, considered

repomix (github.com/yamadashy/repomix) packs a whole repo into one AI-friendly file
(tree-sitter `--compress`, token counts). Evaluated as a dependency: no — it's O(repo) by
design, has no anchors/drift/incremental story, and duplicates our vendored engine. Kept
as UX inspiration: the LLM integration surface should be *a file you hand to a model*,
not a server.

## Roadmap layers (design seams already in place)

1. **HTML renderer** — ~~`docs.build_model` returns plain dataclasses; `render` is the only
   markdown-aware step. A static-site renderer consumes the same model.~~ **Shipped
   2026-07-08** as `documate docs --html` → `site.py`: same model, static site under
   `site_dir` (build artifact, gitignored, not gated — the committed markdown stays the
   gate's subject). Diagrams are mermaid rendered client-side (CDN import, `<pre>` text
   as the offline fallback) — deliberately no bundled layout engine.
2. **Claude prose layer** — opt-in, human-approved: `changed_symbols(base)` × anchor index
   → O(diff) work-order files (repomix-style packing of our bundles) → `claude -p` drafts
   the narrative a generator can't. Brings back the pre-push loop and skills, rebuilt on
   the two-verb tool. **Shipped 2026-07-11 in two halves.** Emitter: `documate check
   --briefs` (`briefs.py`): one self-contained brief per drift finding / undocumented
   symbol (source span, diff, page, caller/callee docstrings, test evidence,
   callees-first) + a `briefs.json` index. Driver: `prose.py` — `check --fix`
   (surgical O(diff)) and `docs --fix` (fresh-repo seeding, all-scope briefs), both
   shelling to the `claude` CLI (Haiku default), re-verified by the gate, capped per
   run, drafts left uncommitted for review. Dogfood proof: a pinned-anchor drift was
   repaired by Haiku touching only the sig line; a fresh 0%-coverage repo seeded to
   100% with accurate contracts. Deferred guardrails (for a future hook/CI trigger,
   not the manual flags): min-lines threshold, skip marker, self-commit loop
   prevention.

## Claude layer: design constraints (borrowed, 2026-07-08)

Surveyed langchain-ai/openwiki, OpenBMB/RepoAgent, textcortex/claude-code-pr-autodoc-action
for reusable ideas. Shipped immediately (zero-LLM): the agent-pointer block in
AGENTS.md/CLAUDE.md (openwiki's one great idea — generated docs are only a token saving if
agents are told to read them) and the docs-refresh-PR workflow recipe in the README.
Rejected: chat-with-repo (the MCP shape, again), per-merged-PR LLM docs (O(every change)
spend — the anti-thesis), RepoAgent's `.project_doc_record` state file (regenerate+diff
can't desync; a state file can). What the future layer inherits:

- **Guardrails before any model call** (from the autodoc action): min-lines/min-files
  thresholds, an explicit skip label/marker, loop prevention (never trigger on commits the
  layer itself made), a hard timeout. In the design from day one.
- **Xref-enriched bundles** (from RepoAgent): a work order for one symbol = its source +
  docstring + the docstrings of direct callers/callees (`callees()`/`reverse_sources()`
  already serve this). Still O(diff), but the model sees how the thing is *used*.
- **Bottom-up order** (ditto): draft callees before callers so summaries compose instead
  of being guessed.

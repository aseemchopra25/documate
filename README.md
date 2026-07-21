<p align="center">
  <img src="https://raw.githubusercontent.com/aseemchopra25/documate/main/.github/documate-intro.webp" alt="documate generating a repository's docs and gating them from the terminal" width="640">
</p>

<h1 align="center">documate</h1>

<p align="center"><b>Docs that regenerate themselves. A gate that catches them lying.</b></p>

<p align="center">
  <a href="pyproject.toml"><img src="https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white" alt="python 3.10+"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green" alt="MIT license"></a>
  <a href="docs/README.md"><img src="https://img.shields.io/badge/docs-self--generated_·_100%25-8A2BE2" alt="docs, self-generated, 100% documented"></a>
</p>

One command reads your code graph and docstrings, writes an overview, an architecture
page, and a reference page per module, then gates the result: the moment code changes
and the docs don't, CI goes red. Missing docstrings? A model drafts them, and the same
gate verifies the drafts.

## Get started

```bash
uv tool install documate      # or: pipx install documate / pip install documate

cd your-repo
documate --init               # first run: scaffold config, generate docs
documate                      # every run after: refresh + gate
```

That is the entire workflow. The core is stdlib-only; `--ai` just needs the `claude`
CLI on your PATH.

## One command, six moods

| Command | What it does |
| --- | --- |
| `documate` | index, write or refresh `docs/`, then gate it |
| `documate --check` | gate only, writes nothing (CI and pre-commit) |
| `documate --watch` | regenerate on every save |
| `documate --html` | also render a static site into `site/` |
| `documate --ai` | draft missing docstrings with a model, then re-verify |
| `documate --stats` | coverage, doc lines +/−, model spend so far |

`documate path/to/repo` documents any repo; `documate --help` has the full list.

## What it writes

- `docs/README.md`: overview with a dependency map, coverage, entry points, and hotspots.
- `docs/ARCHITECTURE.md`: the whole system on one page.
- `docs/architecture/<module>.md`: one page per module (docstring, dependencies, call-flow, symbols).

Committed, always current, never hand-edited.

## The gate

`documate --check` exits non-zero when:

1. Generated pages are out of date (fix: run `documate`).
2. A hand-written page points at code that no longer exists.
3. A hand-written page describes code that changed since `--base` but was not updated.

Install the pre-commit hook once and stale docs heal themselves on every commit:

```bash
git config core.hooksPath hooks
```

In CI, run `documate --check` with `fetch-depth: 0` on `actions/checkout`.

## The AI layer, kept on a leash

Deterministic generation covers structure; a model covers prose. Both flows end at the
same gate, so nothing a model writes ships unverified.

```bash
documate --ai                 # draft every missing docstring (default model: haiku)
documate --check --ai         # surgical mode: repair exactly what the gate flagged
```

- **Consent first.** A pre-flight shows the symbols, call count, and a measured token
  estimate before anything is spent; Enter proceeds, decline is a no-op. Unattended
  runs refuse without `--yes`.
- **Aim, preview, cap.** `--only 'src/api/*'` scopes a run, `--dry-run` shows the plan
  without calling a model, `--budget 2` stops new calls at 2 USD of measured spend.
- **Nothing lands silently.** Drafts stay uncommitted for review, every run records a
  manifest, and `documate --undo` reverts the last run while refusing files you have
  edited since.
- **`--rewrite`** re-emits existing C/C++ doc comments as Doxygen (`/** */` with
  `@brief`/`@param`/`@return`). It is idempotent, so capped runs compose.
- **No API keys, no SDK.** It drives the `claude` CLI you already have.

`documate --list-undocumented` prints the missing-docs map as JSON for your own agents.

## Authored pages

Hand-written Markdown under `docs/` joins the gate too. Anchor a page to the code it
documents:

```markdown
<!-- documents: sym:build_model -->
```

`documate --check` then fails if that code changes and the page does not. Add the
`sig:` fingerprint it prints to pin the page to the exact source, no base ref needed.

## Configuration

`documate --init` writes `documate.config.json`. Override only what you need:

```json
{
  "docs_dir": "docs",
  "site_dir": "site",
  "skip_dirs": ["/generated/"],
  "default_base": "main"
}
```

## License

[MIT](LICENSE). The engine in `src/documate/_engine/` is vendored from code-review-graph (MIT).

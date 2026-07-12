<p align="center">
  <img src="https://raw.githubusercontent.com/aseemchopra25/documate/main/.github/documate-intro.webp" alt="documate generating a repository's docs and gating them from the terminal" width="640">
</p>

# documate

documate generates documentation from your code (an overview, one page per module, and an API
reference from your docstrings) and fails CI when those docs stop matching the code.

[![python](https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white)](pyproject.toml)
[![license](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![docs](https://img.shields.io/badge/docs-self--generated_·_100%25_documented-8A2BE2)](docs/README.md)

## Install

```bash
uv tool install documate     # or: pipx install documate  /  pip install documate
```

`--ai` additionally needs the `claude` CLI on your PATH. Everything else is stdlib.

## Use

```bash
cd your-repo
documate --init    # first run: create a config, then generate the docs
documate           # generate or refresh docs/, then gate them
```

| Command | What it does |
| --- | --- |
| `documate` | index, write or refresh `docs/`, then gate it |
| `documate --check` | gate only, writes nothing (for CI and pre-commit) |
| `documate --watch` | regenerate on every save |
| `documate --html` | also render a static site into `site/` |
| `documate --ai [MODEL]` | draft missing docstrings with a model, then re-verify |
| `documate --stats` | show coverage, documentation lines, and model spend |

Run `documate path/to/repo` to document any repo. Run `documate --help` for the full list.

## What it writes

- `docs/README.md`: an overview with a dependency map, coverage, entry points, and hotspots.
- `docs/ARCHITECTURE.md`: the whole system on one page.
- `docs/architecture/<module>.md`: one page per module (docstring, dependencies, call-flow, symbols).

## The gate

`documate --check` exits non-zero when:

1. Generated pages are out of date. Run `documate` to refresh them.
2. A hand-written page points at code that no longer exists.
3. A hand-written page describes code that changed since `--base` but was not updated.

Install the pre-commit hook once:

```bash
git config core.hooksPath hooks
```

It regenerates and stages the generated pages on every commit. In CI, run `documate --check`
with `fetch-depth: 0` on `actions/checkout`.

## Authored pages

Write Markdown under `docs/` and anchor it to the code it documents:

```markdown
<!-- documents: sym:build_model -->
```

`documate --check` then fails if that code changes and the page does not. Add a `sig:`
fingerprint after the symbol to pin the page to the exact source.

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

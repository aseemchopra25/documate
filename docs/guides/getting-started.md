# Get started

documate generates documentation from your code — an overview, one page per module, and
an API reference from your docstrings — and then fails CI when those docs stop matching
the code. One command writes the docs; the same tool gates them.

## Install

```bash
uv tool install documate     # or: pipx install documate  /  pip install documate
```

Everything runs on the standard library. The optional `--ai` mode is the only extra: it
shells out to the `claude` CLI, so that needs to be on your `PATH` too.

## First run

```bash
cd your-repo
documate --init    # write a config, index the code, generate docs/
documate           # regenerate or refresh docs/, then gate them
```

`--init` drops a small `documate.config.json` you can leave untouched. From then on, bare
`documate` is the whole job: it re-indexes, writes `docs/README.md` (an overview with a
dependency map, coverage, and hotspots), `docs/ARCHITECTURE.md` (the system on one page),
and one page per module under `docs/architecture/`, then checks them.

## The commands

- `documate` — index, write or refresh `docs/`, then gate it.
- `documate --check` — gate only, writes nothing; this is what CI and the pre-commit hook run.
- `documate --watch` — regenerate on every save while you work.
- `documate --html` — also render this static site into `site/`.
- `documate --ai [MODEL]` — draft missing docstrings with a model, then re-verify.
- `documate --stats` — show coverage, documentation lines, and any model spend.

Point it anywhere with `documate path/to/repo`, and see the full list with `documate --help`.

## Keep the docs honest

`documate --check` exits non-zero when a generated page is stale, when a hand-written page
points at code that no longer exists, or when a hand-written page describes code that
changed but was not updated. Nothing to remember day to day — install the hook once and it
regenerates and stages the pages on every commit:

```bash
git config core.hooksPath hooks
```

In CI, run `documate --check` with `fetch-depth: 0` on the checkout step, so the history
the overview's hotspots and evidence are mined from is fully available.

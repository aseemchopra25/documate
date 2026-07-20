"""config.py — everything project-specific about the repo documate is pointed at.

documate's logic is repo-agnostic; this is where a given repo's specifics live, behind
neutral defaults plus an optional JSON override. A repo with the default layout
(docs under `docs/`, graph at `.documate/graph.db`) needs no config. Anything else
drops a `documate.config.json` and overrides only the keys it cares about. Search order:

    $DOCUMATE_CONFIG (absolute path)   ->  explicit, wins
    <root>/documate.config.json        ->  the conventional home
    <root>/.documate.config.json       ->  repo-root dotfile alternative

Keys (all optional; omitted keeps the default):

    docs_dir       where the docs live: generated pages are written here, authored
                   pages are scanned for anchors here                 ("docs")
    site_dir       where `docs --html` writes the static site (a build
                   artifact — gitignore it)                            ("site")
    graph_db       where the indexer writes its graph             (".documate/graph.db")
    skip_dirs      path substrings never treated as source-of-truth: not indexed,
                   no pages, no briefs (vendored/generated/build trees)
    test_markers   path substrings marking test code — directories ("/tests/") or
                   filename suffixes ("_test.go")  (prefer prod over these)

The two list keys EXTEND their defaults rather than replace them — adding your
one vendored tree must not cost you the stock list. Prefix an entry with `!` to
drop a default (`"!/vendor/"` un-skips vendor/).
    default_base   git ref `check` compares against                ("main")
    project_name   the name the generated pages carry (default: derived from the
                   checkout, worktree-safe via the git common dir)
    format_cmd     command --ai runs over the source files it touched, paths
                   appended ("clang-format -i"); None skips formatting

Unknown config keys are a hard error — a typo silently doing nothing is the exact rot
documate exists to stop. Stdlib only.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

_DEFAULTS = {
    "docs_dir": "docs",
    "site_dir": "site",
    "graph_db": ".documate/graph.db",
    # Not-our-source trees a generic monorepo carries: build output, package
    # managers, and vendoring-by-copy under its usual names (third_party/ et al —
    # the wolfssl/chromium shape). testdata/ is the Go fixture convention.
    # Git submodules need no entry: `git ls-files` lists a submodule as one
    # gitlink, so the indexer never sees its contents.
    "skip_dirs": [
        "/build/",
        "/dist/",
        "/out/",
        "/node_modules/",
        "/vendor/",
        "/third_party/",
        "/external/",
        "/extern/",
        "/deps/",
        "/testdata/",
        "/.git/",
        "/.documate/",
    ],
    # ".test." / ".spec." are the JS/TS colocated conventions (api.test.ts);
    # benchmarks are the suite that measures the code, not its public surface —
    # same tier as tests (zod dogfood: bench scripts were becoming the doors).
    "test_markers": [
        "/tests/",
        "/test/",
        "_test.go",
        ".test.",
        ".spec.",
        "/bench/",
        "/benchmarks/",
    ],
    "default_base": "main",
    # None = derive from the checkout (the git common dir's parent, so linked
    # worktrees title their pages like the main checkout); set to pin the name.
    "project_name": None,
    # shell-style command --ai runs over the files it touched (paths appended),
    # e.g. "clang-format -i" — inserted docs then meet the repo's format gate.
    "format_cmd": None,
}

_CONFIG_NAMES = ("documate.config.json", ".documate.config.json")


@dataclass
class Config:
    """Resolved config. Paths absolute."""

    docs_dir: Path
    site_dir: Path
    graph_db: Path
    skip_dirs: tuple[str, ...]
    test_markers: tuple[str, ...]
    default_base: str
    project_name: str | None = field(default=None)
    format_cmd: str | None = field(default=None)
    source: Path | None = field(default=None)


def _config_path(root: Path) -> Path | None:
    """The config file to honour: $DOCUMATE_CONFIG, else the first repo-root name that exists."""
    env = os.environ.get("DOCUMATE_CONFIG")
    if env:
        p = Path(env)
        return p if p.is_file() else None
    for name in _CONFIG_NAMES:
        p = root / name
        if p.is_file():
            return p
    return None


def scaffold(root: Path) -> Path | None:
    """Write a starter `documate.config.json` at the repo root for the user to
    edit, and return its path — or None when a config already exists (never
    clobbered). The layout keys carry their real defaults so the file names
    every knob; the two list keys start empty because they EXTEND the built-in
    lists (an empty list adds nothing — it's the honest "no overrides yet")."""
    if _config_path(root) is not None:
        return None
    path = root / _CONFIG_NAMES[0]
    body = {
        "docs_dir": _DEFAULTS["docs_dir"],
        "site_dir": _DEFAULTS["site_dir"],
        "default_base": _DEFAULTS["default_base"],
        "skip_dirs": [],
        "test_markers": [],
    }
    path.write_text(json.dumps(body, indent=2) + "\n")
    return path


def load_config(root: Path) -> Config:
    """Merge the override file (if any) over the defaults and resolve to a Config."""
    raw = dict(_DEFAULTS)
    src = _config_path(root)
    if src is not None:
        override = json.loads(src.read_text())
        unknown = set(override) - set(_DEFAULTS)
        if unknown:
            raise ValueError(
                f"{src}: unknown config key(s): {', '.join(sorted(unknown))}. "
                f"Valid keys: {', '.join(sorted(_DEFAULTS))}."
            )
        # list keys extend the defaults ("!entry" drops one); scalars replace
        for key in ("skip_dirs", "test_markers"):
            if key in override:
                drops = {e[1:] for e in override[key] if e.startswith("!")}
                adds = [e for e in override[key] if not e.startswith("!")]
                override[key] = [e for e in _DEFAULTS[key] if e not in drops] + [
                    e for e in adds if e not in _DEFAULTS[key]
                ]
        raw.update(override)

    return Config(
        docs_dir=root / raw["docs_dir"],
        site_dir=root / raw["site_dir"],
        graph_db=root / raw["graph_db"],
        skip_dirs=tuple(raw["skip_dirs"]),
        test_markers=tuple(raw["test_markers"]),
        default_base=raw["default_base"],
        project_name=raw["project_name"],
        format_cmd=raw["format_cmd"],
        source=src,
    )

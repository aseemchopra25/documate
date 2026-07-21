<!-- generated documentation — edit the source, not this file -->
# `src/documate/docs.py`

docs.py — `documate`: generate the committed documentation from code.

The generated tier. One overview page (`docs/README.md`) plus one architecture page per
subsystem (`docs/architecture/<slug>.md`), built from two honest sources:

  structure  the graph — which symbols exist, who calls whom, which module imports which
  prose      your docstrings/doc-comments, via `extract` — never invented

Output is committed (it's the documentation people read on the repo) but never
hand-edited: `documate` rewrites it, `documate --check` fails CI when it's stale.
A symbol with no docstring lands in an "Undocumented" fold instead of a faked
paragraph, so the coverage number on the overview is honest and ratchets up as you
write docstrings.

The build is split model -> render on purpose: `build_model` returns plain dataclasses
(no markdown), `render` turns them into markdown strings. A future HTML renderer plugs
into the same model. Output via `ui`, logic stdlib only; graph needed (the CLI
indexes before calling in).

**depends on** [`src/documate/core.py`](src.documate.core.md), [`src/documate/extract.py`](src.documate.extract.md), [`src/documate/stats.py`](src.documate.stats.md), [`src/documate/ui.py`](src.documate.ui.md)  ·  **used by** [`src/documate/briefs.py`](src.documate.briefs.md), [`src/documate/check.py`](src.documate.check.md), [`src/documate/cli.py`](src.documate.cli.md), [`src/documate/prose.py`](src.documate.prose.md), [`src/documate/site.py`](src.documate.site.md), [`src/documate/stats.py`](src.documate.stats.md)  ·  **discussed in** [`notes/v2-direction.md`](../../notes/v2-direction.md)

## API

### `_slug(rel: str) -> str`
`src/documate/docs.py:44`

Flatten a repo-relative source path into a page filename stem (`src/a/b.py` -> `src.a.b`).

**called by** `_architecture`, `_grouped_overview`, `_overview`, `_page`, `_tail`, `build_model`, `render`

### `_dir(rel: str) -> str`
`src/documate/docs.py:49`

The directory holding a module ("" at the repo root) — the grouping key when a
repo is too big for one flat page list.

**called by** `_architecture`, `_grouped_overview`, `render`

### `_tail(d: str, p: Page) -> str`
`src/documate/docs.py:55`

A page's filename stem inside its directory's folder (`src/a/b.py` -> `b`).

**called by** `_architecture`, `_group_index`, `_grouped_overview`, `render`  ·  **calls** `_slug`

### `_skip(ctx: Context, rel: str) -> bool`
`src/documate/docs.py:60`

True for paths the docs must not treat as source: skip_dirs = not-our-source
(vendored/build), test_markers = test code — the docs describe the public
surface, not the suite that exercises it. Markers are substrings of the
"/"-prefixed rel path: directories slash-wrapped ("/tests/", and the prefix "/"
is why a top-level `tests/` matches) or filename suffixes ("_test.go").

**called by** `_doc_mentions`, `_module_edges`, `build_model`

### `_xref_maps(ctx: Context, owned: set[str], extra=()) -> tuple[dict, dict]`
`src/documate/docs.py:70`

(callers, callees) keyed by qualified_name, values = sets of qualified_names.

ONLY qualified targets that name an owned symbol count. A bare target is a builtin /
stdlib / unresolved call; matching it by short name conflates collisions. Drop the
bare half: a missing xref beats a wrong one. `extra` is more (src, tgt) qualified
pairs recovered elsewhere (Go's re-qualified cross-file calls), same rules.

**called by** `build_model`

### `_humanize(test_q: str) -> str`
`src/documate/docs.py:86`

A test's name read as the behavior it asserts: drop the test prefix, split
snake/camel words ("TestReadRejectsShortRecord" -> "read rejects short record").
Mined from the name, never invented — the evidence line for a symbol whose only
documentation is its test suite.

**called by** `_tested`

### `_tested(ctx: Context, syms: list[dict]) -> dict[str, list[str]]`
`src/documate/docs.py:97`

qualified production symbol -> humanized names of the tests that call it.

The engine's TESTED_BY edges carry a qualified test but usually a bare production
name (tests live in other files). A bare name attaches only when exactly one owned
symbol bears it — evidence pinned to the wrong symbol is worse than none.

**called by** `build_model`  ·  **calls** `_humanize`

### `_origins(ctx: Context, rels: set[str]) -> dict[str, str]`
`src/documate/docs.py:119`

rel source path -> subject of the oldest commit that added it.

One `git log` pass over the whole history (newest first; later, older adds
overwrite, so the original introduction wins). For a module with no docstring
that subject is the only human prose in the repo about why the file exists —
mined and labeled as a commit subject, never passed off as documentation.
A commit adding more than _BULK_CAP owned modules is a bulk event (a tree
move, a vendor import) whose subject describes no single file — skipped, same
spirit as the hotspot bulk filter ("Move source files to src/" on 25 jq pages
is what this rule exists to prevent). Empty on any git failure (shallow or
absent history just means no evidence); note a shallow CI clone sees
different history than a full one, so freshness checking needs
`fetch-depth: 0` — same as the drift gate.

**called by** `build_model`  ·  **calls** `run`

### `_doc_mentions(ctx: Context, rels: set[str]) -> dict[str, list[str]]`
`src/documate/docs.py:169`

module rel -> tracked doc files (.md/.rst, unstamped) that mention it by path.

The repo's existing documentation is evidence too: a design note or an old
docs site that names a module gets linked from that module's generated page
("discussed in"), so the map points into the prose humans already wrote
instead of ignoring it. Matching is by repo-relative path — or bare filename
when exactly one module carries it — never by symbol name (too collision-
prone). Untracked files are invisible (same rule as the indexer); empty on
any git failure.

**called by** `build_model`  ·  **calls** `_skip`, `run`

### `class Hotspots`
`src/documate/docs.py:223`

Change-frequency evidence for the overview, pinned to one commit.

`rev` is the pin: the rendered section prints it, and `check` re-mines at that
same commit (via `pinned_rev`) instead of HEAD — so history growing under
committed docs never makes them "stale". `hot` is (module, commits touching
it); `coupled` is (a, b, shared commits) for module pairs that usually change
together yet share no import edge — coupling the dependency map can't show.

**called by** `_hotspots`

### `_repo_name(ctx: Context) -> str`
`src/documate/docs.py:237`

The name the generated pages call this repo.

Config `project_name` wins. Otherwise, when the root is a whole checkout, the
name comes from the git common dir's parent — the main checkout's directory —
so a linked worktree titles its pages exactly like the checkout that committed
them and `--check` stays green there (the worktree's own dirname would differ
on every page). A monorepo sub-tree root keeps its own basename, as does any
non-git tree.

**called by** `build_model`  ·  **calls** `run`

### `_head_rev(ctx: Context) -> str | None`
`src/documate/docs.py:265`

Current HEAD's short hash — the pin `documate` mines hotspots at.
None (no hotspots) without git or before the first commit.

**called by** `run`  ·  **calls** `run`

### `_rev_exists(ctx: Context, rev: str) -> bool`
`src/documate/docs.py:280`

True when `rev` still resolves to a commit. A hotspot pin orphaned by an
amend/rebase does not — and a pin the gate can no longer mine at must be
re-pinned rather than preserved.

**called by** `run`  ·  **calls** `run`

### `_hotspots(ctx: Context, rev: str, rels: set[str], edges: list[tuple]) -> Hotspots | None`
`src/documate/docs.py:293`

Mine churn and co-change from `git log <rev>`, filtered to owned modules.

One pass over history as of the pin. Merge commits and bulk changes (more
than _BULK_CAP modules in one commit — a reformat, not a change) are skipped.
A module is hot with >= 2 commits; a pair is coupled when it shares >= 3
commits, that is at least half of the quieter side's total, and no import
edge links the two (an edge makes co-change expected, not hidden). None on
any git failure (a missing pin just means no evidence) or when nothing
clears the hot bar.

**called by** `build_model`  ·  **calls** `Hotspots`, `run`

### `_go_edges(ctx: Context, syms: list[dict]) -> tuple[list[tuple], list[tuple]]`
`src/documate/docs.py:357`

(re-qualified call pairs, module edges) recovered from Go's unresolved edges.

The engine leaves two Go gaps: IMPORTS_FROM targets are package paths
("example.com/mod/krypto"), not files, and a call is only qualified when caller
and callee share a file — `krypto.Derive()` or a package-sibling `helper()` is
stored as a bare name. Both are resolvable with what's already on disk:

- an import path maps to the owned package dir it ends with;
- a bare call target maps to the one file that (a) owns that name, (b) sits in
  the caller's own package dir or one it imports, and (c) is *literally called
  in the caller's source* — `krypto.Derive(` cross-package, an undotted
  `helper(` in-package — so a method call on some other type's value
  (`conn.Read()`) can't fabricate a dependency. The cross-package prefix is the
  package's *declared* name (Go never promises it matches the directory — fzf's
  `src/` declares `package fzf`) or an alias the caller's import line gives the
  path. Survivors in several package dirs → no edge: a missing xref beats a
  wrong one. Survivors sharing one dir are build-tag twins (`protector.go` /
  `protector_openbsd.go`), one implementation per platform — all keep the edge.

Call pairs come back qualified ("<abs file>::Name", the graph's own format) for
`_xref_maps`; module edges carry the symbol, plus a symbol-less edge for an
imported single-file package that's never called (a types-only structs package
still belongs on the dependency map).

**called by** `build_model`  ·  **calls** `prefixes`, `text`

### `text(rel: str) -> str`
`src/documate/docs.py:410`

Source of `rel`, read once, "" when unreadable.

**called by** `_go_edges`, `prefixes`

### `prefixes(rs: str, d: str) -> set[str]`
`src/documate/docs.py:423`

Call-site prefixes that can mean package dir `d` inside file `rs`: the
package's declared name, plus any alias `rs`'s import line gives a path
ending in the dir (`kk "example.com/mod/krypto"` -> `kk`).

**called by** `_go_edges`  ·  **calls** `text`

### `_module_edges(ctx: Context, syms: list[dict]) -> list[tuple]`
`src/documate/docs.py:474`

(src_module, dst_module, symbol|None) module-dependency edges.

Two sources, one per language family. Python is scanned with stdlib `ast` (the
engine truncates a multi-name `from . import a, b, c` to its first name
and lumps stdlib in, so its IMPORTS_FROM can't draw a faithful Python graph):
  - `from .core import Context, load_config`  -> edges to core.py, symbols {Context, ...}
  - `from . import drift, docs`               -> edges to each module, no symbol
  - `import pkg.drift`                         -> edge to drift.py, no symbol
A target resolves only to an owned module (by file stem); stdlib/third-party drop out.
Everything else comes from the engine's IMPORTS_FROM edges (`graphdb.import_edges`):
file->file rows resolve directly (JS/TS-style path imports); a C-family include
target — `compile.h` bare, `wolfssl/wolfcrypt/aes.h` path-form, arriving exactly
as written in the source — is found the way a compiler's include search would
(`resolve_include`). No symbol names on either kind.

The universe is every parsed non-skipped file, not just symbol owners: a barrel
(index.ts of pure re-exports, a `from .x import y` __init__.py) defines nothing
but is the hub the whole API surface routes through — drop it and a library's
dependency map falls apart (zod dogfood).

**called by** `build_model`  ·  **calls** `_skip`, `resolve_include`

### `resolve_include(rs: str, dst: str) -> str | None`
`src/documate/docs.py:506`

Find include target `dst` from module `rs` the way a compiler would.

Path-form (`wolfssl/aes.h`): relative to the includer, then to the repo
root (the ubiquitous -I<root>), then a unique path-suffix match (the
-Iinclude layout). Bare (`config.h`): the includer's sibling, then unique
below the includer's dir (ESP-IDF's main/include), then repo-unique — but
only inside the includer's own top-level tree, so one vendor snapshot of
`config.h` can't capture every module that means the build-generated one.
System headers own nothing and drop out; a missing edge beats a wrong one.

**called by** `_module_edges`

### `class Symbol`
`src/documate/docs.py:581`

One function/class on a page: identity + prose + owned xrefs.

`name` is the qualified-name tail — dotted for a class member (`GraphDB.index`),
bare for a top-level symbol — so renderers can group methods under their class.

**called by** `build_model`

#### `Symbol.owner(self) -> str | None`
`src/documate/docs.py:597`

The class a method belongs to (its dotted prefix); None for top-level symbols.

### `class Page`
`src/documate/docs.py:603`

One subsystem (= source module): its API surface, edges, and symbols.

**called by** `build_model`

#### `Page.summary(self) -> str`
`src/documate/docs.py:622`

First sentence-ish line of the module prose — the overview table cell.

### `_machine_generated(path: Path) -> bool`
`src/documate/docs.py:636`

True when the file carries the generated-code banner in its first lines.
Skip tier, same as skip_dirs: nobody reads the file, so it gets no page,
no coverage debt, no --ai work order. Cached — the model and briefs both
probe it per symbol.

**called by** `build_model`

### `class Model`
`src/documate/docs.py:649`

Everything `render` (markdown today, HTML later) needs, no markup in it.

**called by** `build_model`

### `build_model(ctx: Context, hot_rev: str | None=None) -> Model`
`src/documate/docs.py:660`

Read the graph + source into the page model. Pure: writes nothing.

`hot_rev` pins the hotspot mining to one commit — `docs` passes HEAD, `check`
passes the pin already printed in the committed overview (`pinned_rev`), so
the freshness diff never moves just because history grew. None skips mining.

**called by** `run`  ·  **calls** `Model`, `Page`, `Symbol`, `_doc_mentions`, `_go_edges`, `_hotspots`, `_machine_generated`, `_module_edges`

### `_tour(pages: list[Page], edges: list[tuple[str, str]]) -> tuple[list[str], list[str]]`
`src/documate/docs.py:832`

(page rels in reading order, the entry points the tour starts from).

Entry points are modules nothing else imports but that import something — the
doors into the codebase. (Machine-generated files can't become doors: build_model
drops them before a page exists.) Doors rank by *reach* — how many modules their
dependency walk opens —
so in a repo with hundreds of leaf example programs (wolfssl's IDE/ trees) the
door that actually opens the library outranks whatever sorts first. The order
walks breadth-first from the doors through
their dependencies, so a reader meets each module after the code that drives it;
whatever the walk can't reach (import cycles, isolated modules) is appended
most-used-first. Pure graph fact — no salience is invented — and every tie
breaks alphabetically, so the tour is deterministic.

**called by** `_architecture`, `_grouped_overview`, `_overview`  ·  **calls** `reach`

### `reach(r: str) -> int`
`src/documate/docs.py:856`

How many modules `r`'s dependency walk opens (itself included).

**called by** `_tour`

### `_start_here(entries: list[str], href) -> list[str]`
`src/documate/docs.py:885`

The overview's "start here" line: link the entry points (capped at 3), or
nothing when the graph has no clear door. `href` maps a rel to its page link.

**called by** `_grouped_overview`, `_overview`  ·  **calls** `href`

### `_about(p: Page) -> str`
`src/documate/docs.py:899`

A page's one-line description for overview tables: the module prose's first
line when there is one, else the mined creating-commit subject — italicized and
labeled as what it is, so evidence never masquerades as documentation.

**called by** `_group_index`, `_overview`

### `_mermaid_lines(edges: list[tuple[str, str]], classes: dict[str, str] | None=None, clusters: dict[str, list[str]] | None=None) -> list[str]`
`src/documate/docs.py:910`

Mermaid edge lines with parse-safe node ids. `(`/`[` open shape syntax in
a bare mermaid id, so a Next.js route dir (`app/(doc)/[[...slug]]`) silently
becomes a mislabeled shape or a parse error (zod dogfood). Ids keep word
chars/dots/dashes; a node whose id lost characters is declared once as
`id["label"]` so the diagram still shows the real name. Distinct labels never
share an id (collisions suffix `_2`) — merging two nodes would draw a lie.
`classes` (label -> mermaid class name) adds one `class a,b,c name` line per
class, ids resolved through the same table; the matching `classDef` colors are
the renderer's job (the site injects them per theme), so the committed
markdown carries no styling. `clusters` (box label -> member node labels)
wraps members in labeled `subgraph` blocks, in order; cluster i is minted id
`c<i>` (collision-suffixed like node ids) and classed `h<i>` so the renderer
can tint the box to match its members' hue.

**called by** `_architecture`, `_grouped_overview`, `_overview`  ·  **calls** `nid`

### `nid(label: str) -> str`
`src/documate/docs.py:931`

The parse-safe id for `label`, minted on first sight and stable after.

**called by** `_mermaid_lines`

### `_stem_edges(edges: list[tuple[str, str]]) -> list[tuple[str, str]]`
`src/documate/docs.py:974`

Module edges collapsed to file stems for the overview diagram: a .c/.h pair
is one node there, so its internal edge becomes a self-loop and its outgoing
edges become duplicates — drop both, the diagram shows modules, not files.

**called by** `_architecture`, `_overview`

### `pinned_rev(ddir: Path) -> str | None`
`src/documate/docs.py:985`

The hotspot pin recorded in the committed overview, if any.

`check` re-mines at this commit instead of HEAD, so freshness stays a pure
function of the committed tree — new commits don't shift the counts under
the diff. None when the overview is absent or carries no hotspot section.

### `_hotspot_lines(hs: Hotspots | None, href) -> list[str]`
`src/documate/docs.py:999`

The overview's Hotspots section: the most-changed modules, then pairs that
change together without an import edge between them. Mined evidence, labeled —
and the label line doubles as the pin `pinned_rev` reads back.

**called by** `_grouped_overview`, `_overview`  ·  **calls** `href`

### `_overview(model: Model) -> str`
`src/documate/docs.py:1018`

The docs/README.md: what the system is made of, drawn from the graph.

**called by** `render`  ·  **calls** `_about`, `_hotspot_lines`, `_mermaid_lines`, `_slug`, `_start_here`, `_stem_edges`, `_tour`

### `_grouped_overview(model: Model, groups: dict[str, list[Page]]) -> str`
`src/documate/docs.py:1044`

The monorepo overview: directories, not a phone book of modules.

Same header and honesty as `_overview`, but the map and the table aggregate to
directory level and each row links that directory's own index page — a
2,000-module repo gets a readable front page instead of a 2,000-row table.

**called by** `render`  ·  **calls** `_dir`, `_hotspot_lines`, `_mermaid_lines`, `_slug`, `_start_here`, `_tail`, `_tour`

### `_group_index(d: str, pages: list[Page]) -> str`
`src/documate/docs.py:1093`

One directory's index page: the same subsystem table the small-repo overview
has, scoped to this directory's modules.

**called by** `render`  ·  **calls** `_about`, `_tail`

### `_page(p: Page, href=None, at: str='docs/architecture') -> str`
`src/documate/docs.py:1104`

One architecture page: module prose, edges, flow, then the per-symbol API.

`href` maps a sibling module's rel path to a link relative to THIS page — the
grouped layout passes one that climbs directories (`../other.dir/mod.md`);
default is the flat layout's same-folder link. `at` is this page's own folder
relative to the repo root, so "discussed in" can link doc files anywhere in
the repo.

**called by** `render`  ·  **calls** `_slug`, `href`

### `_architecture(model: Model, groups: dict[str, list[Page]], grouped: bool) -> str`
`src/documate/docs.py:1173`

docs/ARCHITECTURE.md — the whole system stitched onto one page.

The read-it-top-to-bottom companion to the per-module reference: the dependency
map, then every subsystem's full module prose in context, its API surface, and
its neighbours — each heading and neighbour linking into `architecture/`. What
the overview's table names and the architecture/ pages detail, this narrates.
Sections come in `_tour` reading order (entry points first), not alphabetical;
in the grouped (monorepo) layout they nest under their directory, directories
ordered by their best-ranked page.

**called by** `render`  ·  **calls** `_dir`, `_mermaid_lines`, `_slug`, `_stem_edges`, `_tail`, `_tour`

### `render(model: Model) -> dict[str, str]`
`src/documate/docs.py:1250`

Model -> {path-under-docs_dir: markdown}. Deterministic: same model, same bytes.

Two layouts, one threshold: up to _GROUP_AT pages (or a single directory), the
flat layout — overview table of modules, pages directly under `architecture/`.
Past it, the grouped layout — overview table of directories, one folder per
directory under `architecture/` holding its index (README.md) and its pages —
so a monorepo's front page and directory listing stay readable.

**called by** `run`  ·  **calls** `_architecture`, `_dir`, `_group_index`, `_grouped_overview`, `_overview`, `_page`, `_slug`, `_tail`

### `href(m: str, _g: str=g) -> str`
`src/documate/docs.py:1282`

Link to a sibling module's page from inside this directory's folder.

**called by** `_hotspot_lines`, `_page`, `_start_here`

### `_print_diff(rel: str, old: str, new: str) -> None`
`src/documate/docs.py:1295`

A compact colored unified diff of one regenerated page — what `--watch` shows
so every doc change is visible the moment it happens, straight in the terminal.

**called by** `run`

### `_agent_pointer(ctx: Context) -> list[str]`
`src/documate/docs.py:1311`

Maintain the agent-pointer block in AGENTS.md / CLAUDE.md — whichever already
exist at the root (never created uninvited).

The generated docs are a token-cheap map of the repo; this block tells coding
agents to read that map before crawling source, which is the whole token-economy
point. Idempotent: the block lives between documate markers and is rewritten in
place; everything outside the markers is untouched. Returns the files changed.
`check` never calls this — the gate stays read-only.

**called by** `run`

### `_rescue_docs_dir(ctx: Context, want: dict[str, str]) -> bool`
`src/documate/docs.py:1352`

Interactive way out of the clobber refusal: ask where generated docs
should go instead, validate the answer (inside the repo, no foreign files
where our pages would land), persist it to the config file, and point
ctx.config there. Returns True when a new docs_dir was accepted; False
(no terminal, decline, or three bad answers) falls back to the refusal.
The caller re-runs so the page model rebuilds with the new docs_rel.

**called by** `run`

### `run(ctx: Context, diff: bool=False, quiet: bool=False) -> int`
`src/documate/docs.py:1405`

Write the generated tier under docs_dir, pruning orphaned pages of ours.

Never touches a file it didn't stamp: a docs/ tree that predates documate makes
this refuse (nothing written) rather than clobber — the fix is pointing docs_dir
elsewhere, not a --force. Only pages whose content actually changed are
rewritten. With diff=True (the `--watch` live view) every new/changed/pruned
page is printed as a colored unified diff, so you watch the documentation move
as you edit the code. With quiet=True (the --ai post-draft refresh) the summary
line stays unprinted — refusals and failures still speak.

**called by** `_doc_mentions`, `_head_rev`, `_hotspots`, `_origins`, `_repo_name`, `_rev_exists`  ·  **calls** `_agent_pointer`, `_head_rev`, `_print_diff`, `_rescue_docs_dir`, `_rev_exists`, `build_model`, `render`

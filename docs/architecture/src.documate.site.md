<!-- generated documentation — edit the source, not this file -->
# `src/documate/site.py`

site.py — `documate --html`: the same docs, rendered as a static site.

The second consumer of the model/render seam: `docs.build_model` builds one Model, and
this module renders it as HTML the way `docs.render` renders it as markdown — same
structure, same docstring prose, so the site can never say something the committed
pages don't. It adds two things the markdown tier can't carry: an overview and an
Architecture page as real, navigable HTML, and client-side search / theming / diagrams.

The output is a build artifact, not a third doc tier: it lands in `site_dir`
(gitignored, like the graph), is regenerated wholesale by `documate --html`, and
is NOT gated by `documate --check` — the committed markdown stays the single source the
gate protects. Host it like any static site (GitHub Pages, `python -m http.server`).
A `.nojekyll` marker ships alongside so GitHub Pages serves the files verbatim, and
every link is relative so the site works under a `user.github.io/repo/` subpath.

Self-contained: one stylesheet, one script, no build tooling, no framework. The only
network fetch is Mermaid from a CDN to draw the diagrams client-side; offline, the
flowchart text stays readable in its `<pre>`. Output via `ui`, logic stdlib only.

**depends on** [`src/documate/core.py`](src.documate.core.md), [`src/documate/docs.py`](src.documate.docs.md), [`src/documate/ui.py`](src.documate.ui.md)  ·  **used by** [`src/documate/cli.py`](src.documate.cli.md)  ·  **discussed in** [`notes/v2-direction.md`](../../notes/v2-direction.md)

## API

### `_inline(text: str) -> str`
`src/documate/site.py:344`

Escape a prose line for HTML, keeping `backtick` spans as <code>.

**called by** `_md_inline`, `_overview`, `_prose`

### `_prose(text: str) -> str`
`src/documate/site.py:349`

Docstring text -> HTML blocks: blank-line-separated paragraphs, with chunks whose
every line is indented (the docstring convention for tables/diagrams/examples, and
what survives `ast.get_docstring`'s dedent) kept verbatim in a <pre>.

**called by** `_architecture`, `_page`  ·  **calls** `_inline`

### `class Guide`
`src/documate/site.py:364`

One authored page picked up for the site: its nav identity + markdown source.

**called by** `_guides`

### `_md_inline(text: str) -> str`
`src/documate/site.py:372`

`_inline` plus the guide-markdown spans: **bold** and [link](url).

**called by** `_markdown`, `flush`  ·  **calls** `_inline`

### `_markdown(text: str) -> str`
`src/documate/site.py:378`

Authored-guide markdown -> HTML: the subset guides actually use — headings,
paragraphs, fenced code (```mermaid fences become live diagrams), flat lists,
inline code/bold/links. Anchor comments (and any other HTML comment) vanish:
they're for `check`, not for readers. Not a full markdown engine on purpose; the
committed .md stays the canonical rendering.

**called by** `_guide`, `_overview`  ·  **calls** `_md_inline`, `flush`

### `flush() -> None`
`src/documate/site.py:389`

Close the open paragraph, if any.

**called by** `_markdown`  ·  **calls** `_md_inline`

### `_guides(ctx: Context) -> list[Guide]`
`src/documate/site.py:432`

Every authored page under docs_dir — any *.md without the generated stamp, the
same rule the anchor scanner uses — so the site carries the hand-written why
alongside the generated what.

**called by** `run`  ·  **calls** `Guide`

### `_mermaid(kind: str, edges) -> str`
`src/documate/site.py:453`

A client-rendered flowchart: the mermaid text itself is the offline fallback.

**called by** `_architecture`, `_overview`, `_page`

### `_nav_labels(pages: list[Page]) -> dict[str, str]`
`src/documate/site.py:459`

{slug: sidebar label} — the file's basename; the directory groups it in the tree.

**called by** `_groups`

### `_groups(pages: list[Page]) -> list[tuple[str, list[list[str]]]]`
`src/documate/site.py:464`

Pages bucketed by directory, order preserved: [(dir, [[slug, filename], …]), …].
The tree renders one collapsible group per directory.

**called by** `_nav_js`  ·  **calls** `_nav_labels`

### `_search_index(model: Model, guides: list[Guide]) -> list[list[str]]`
`src/documate/site.py:475`

Everything the palette can jump to: [kind, name, context, href]. Modules and
the two headline pages, plus every documented symbol at its `page.html#name`.

**called by** `_nav_js`

### `_nav_js(model: Model, guides: list[Guide]) -> str`
`src/documate/site.py:493`

The shared client: sidebar data (doc links + directory groups) + the search
index, injected into the app template. One copy for the whole site.

**called by** `render`  ·  **calls** `_groups`, `_search_index`

### `_crumb(*parts: str) -> str`
`src/documate/site.py:508`

A breadcrumb: last part bold, joined by faint slashes.

**called by** `_architecture`, `_guide`, `_overview`, `_page`

### `_layout(model: Model, title: str, active: str, crumb: str, body: str, body_class: str='') -> str`
`src/documate/site.py:518`

Wrap a page body in the shared shell: sidebar (brand + coverage, nav filled by
nav.js), sticky top bar (breadcrumb + search + theme), the reading column, and the
search palette. `active` (the page's slug) marks its nav link; everything else is
one shared nav.js and style.css, so a page's size never grows with the page count.

**called by** `_architecture`, `_guide`, `_overview`, `_page`

### `_links(mods) -> str`
`src/documate/site.py:570`

Chip links to sibling subsystem pages, monospace like everywhere else.

**called by** `_architecture`, `_page`

### `_featured(guides) -> Guide | None`
`src/documate/site.py:582`

The guide to headline on the landing page — the first getting-started/install
page. None -> the landing page stays a plain docs index (nothing to inline).

**called by** `_overview`

### `_split_intro(text: str) -> tuple[str, str]`
`src/documate/site.py:592`

A featured guide's markdown -> (lede, rest): drop the leading `# ` title, take the
first paragraph as the hero lede, hand back the remainder for the Getting started
section — so the opening line isn't printed twice.

**called by** `_overview`

### `_overview(model: Model, guides=()) -> str`
`src/documate/site.py:611`

index.html — the landing page: a hero (name, lede, stat badges, calls to action),
the Getting started guide inlined when the repo ships one, then the subsystem map and
any remaining guides. Still the same model the markdown overview is built from.

**called by** `render`  ·  **calls** `_crumb`, `_featured`, `_inline`, `_layout`, `_markdown`, `_mermaid`, `_split_intro`

### `_architecture(model: Model) -> str`
`src/documate/site.py:682`

architecture.html — docs/ARCHITECTURE.md as a site page: every subsystem in
reading order (entry points first), each linking into its per-module page.

**called by** `render`  ·  **calls** `_crumb`, `_layout`, `_links`, `_mermaid`, `_prose`

### `_guide(model: Model, g: Guide) -> str`
`src/documate/site.py:740`

One authored page, converted from its markdown, in the same shell as the rest.

**called by** `render`  ·  **calls** `_crumb`, `_layout`, `_markdown`

### `_page(model: Model, p: Page) -> str`
`src/documate/site.py:747`

One subsystem page: module prose, edge chips, flow diagram, per-symbol API.

**called by** `render`  ·  **calls** `_crumb`, `_layout`, `_links`, `_mermaid`, `_prose`

### `render(model: Model, guides: list[Guide]=()) -> dict[str, str]`
`src/documate/site.py:808`

Model (+ authored guides) -> {filename: html/css/js}. Deterministic, flat:
index (overview) + architecture + one page per subsystem + one per guide + the
shared stylesheet and script — the whole site, ready for any static host.

**called by** `run`  ·  **calls** `_architecture`, `_guide`, `_nav_js`, `_overview`, `_page`

### `run(ctx: Context) -> int`
`src/documate/site.py:825`

Write the site under site_dir, pruning orphaned pages of ours (same contract as
`docs.run`: a renamed source file must not leave its old page behind). Drops a
`.nojekyll` so GitHub Pages serves the files verbatim.

**calls** `_guides`, `render`

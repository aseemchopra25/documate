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
`src/documate/site.py:680`

Escape a prose line for HTML, keeping `backtick` spans as <code>.

**called by** `_doxygen`, `_md_inline`, `_overview`, `_page`, `_prose`

### `_doxygen(text: str) -> str | None`
`src/documate/site.py:688`

A Doxygen-marked doc (`@brief`/`@param`/`@return` lines, what --rewrite
emits for C) as structured HTML — the brief as the lead paragraph, the
contract as a definition list — or None when the text carries no markers
(the plain-prose path renders it). Continuation lines fold into the tag
above them; unmarked lines stay ordinary paragraphs.

**called by** `_prose`  ·  **calls** `_inline`

### `_prose(text: str) -> str`
`src/documate/site.py:743`

Docstring text -> HTML blocks: a Doxygen-marked doc renders structured
(`_doxygen`); otherwise blank-line-separated paragraphs, with chunks whose
every line is indented (the docstring convention for tables/diagrams/examples, and
what survives `ast.get_docstring`'s dedent) kept verbatim in a <pre>.

**called by** `_architecture`, `_page`  ·  **calls** `_doxygen`, `_inline`

### `class Guide`
`src/documate/site.py:762`

One authored page picked up for the site: its nav identity + markdown source.

**called by** `_guides`

### `_md_inline(text: str) -> str`
`src/documate/site.py:771`

`_inline` plus the guide-markdown spans: **bold**, *em* and [link](url).
Bold substitutes first so the em pass only ever sees single asterisks.

**called by** `_markdown`, `flush`  ·  **calls** `_inline`

### `_markdown(text: str) -> str`
`src/documate/site.py:779`

Authored-guide markdown -> HTML: the subset guides actually use — headings,
paragraphs, fenced code (```mermaid fences become live diagrams), flat lists,
inline code/bold/links, and standalone image lines (consecutive ones share a
figure; a `-light.`/`-dark.` pair renders as one theme-following image).
Anchor comments (and any other HTML comment) vanish: they're for `check`, not
for readers. Not a full markdown engine on purpose; the committed .md stays
the canonical rendering.

**called by** `_guide`, `_overview`  ·  **calls** `_md_inline`, `cls`, `flush`

### `flush() -> None`
`src/documate/site.py:792`

Close the open paragraph, if any.

**called by** `_markdown`  ·  **calls** `_md_inline`

### `cls(src: str) -> str`
`src/documate/site.py:870`

The theme class for one image of a light/dark pair.

**called by** `_markdown`

### `_guide_rank(g: Guide) -> int`
`src/documate/site.py:906`

The guide's priority bucket — index into _GUIDE_RANKS, 3 when nothing hits.

**called by** `_feats`, `_overview`

### `_guides(ctx: Context) -> list[Guide]`
`src/documate/site.py:915`

Every authored page under docs_dir — any *.md without the generated stamp, the
same rule the anchor scanner uses — so the site carries the hand-written why
alongside the generated what. Ordered for a reader (setup first, process last),
stable within a bucket, and the sidebar/search follow the same order.

**called by** `run`  ·  **calls** `Guide`

### `_remote_base(ctx: Context) -> str | None`
`src/documate/site.py:938`

The https base of the `origin` remote (git@/ssh/https forms normalized) —
where guide links that point outside the docs tree land, as blob URLs.
None without a usable remote; the caller then treats such links as dead.

**called by** `_resolve_links`  ·  **calls** `run`

### `_page_hrefs(model: Model, guides: list[Guide]) -> dict[str, str]`
`src/documate/site.py:958`

{docs-relative .md path: site .html file} for everything the site renders —
guides, the two headline pages, and each subsystem page under both committed
layouts (flat and grouped), so a guide's link works whichever one is on disk.

**called by** `_resolve_links`

### `_resolve_links(ctx: Context, model: Model, guides: list[Guide]) -> tuple[list[str], dict[str, Path]]`
`src/documate/site.py:976`

Rewrite every relative link in the authored guides to its real site target
— a sibling .md becomes its .html page, a repo file becomes the remote's blob
URL at default_base, a standalone image line points at a flat copy the caller
ships into site_dir — and return (dead, assets): a line per link or image that
resolves to nothing (the caller fails the build: a shipped dead link is doc
rot, the exact thing the tool exists to stop) and {site filename: source file}
for every image the site needs. Scheme-carrying links (http, mailto) and bare
#anchors pass through; fenced code blocks are left untouched.

**called by** `run`  ·  **calls** `_page_hrefs`, `_remote_base`

### `swap(m: re.Match, _g: Guide=g, _at: str=at) -> str`
`src/documate/site.py:995`

One matched markdown link, rewritten to its site target — or kept
as written, recording it dead when nothing resolves.

### `_mermaid(kind: str, edges, classes: dict[str, str] | None=None, clusters: dict[str, list[str]] | None=None, caption: str | None=None, tall: bool=False, open_max: bool=False) -> str`
`src/documate/site.py:1047`

A client-rendered flowchart: the mermaid text itself is the offline fallback.
`classes`/`clusters` mark nodes and directory boxes without colors — nav.js
injects the matching theme-aware `classDef` lines at render time. `tall` lets
the shell grow toward the viewport, `open_max` makes the figure present itself
full-screen on first load (nav.js honors it on wide screens only), and
`caption` renders as the figcaption carrying the interaction hints.

**called by** `_architecture`, `_overview`, `_page`

### `_map_marks(edges: list[tuple[str, str]]) -> tuple[dict[str, str], dict[str, list[str]], str]`
`src/documate/site.py:1077`

(classes, clusters, caption) for a module map, nodes keyed by stem (the
map's node label). Spanning several directories, each directory becomes a
labeled cluster box and one hue class (`g0`, `g1`, … in first-seen order,
box classed `h0`, `h1`, … to match) — the map then reads as subsystems.
A single-directory map colors by graph role instead: `gentry` for modules
nothing imports (where reading starts), `gleaf` for modules importing
nothing else on the map.

**called by** `_architecture`, `_overview`

### `_nav_labels(pages: list[Page]) -> dict[str, str]`
`src/documate/site.py:1121`

{slug: sidebar label} — the file's basename; the directory groups it in the tree.

**called by** `_groups`

### `_groups(pages: list[Page]) -> list[tuple[str, list[list[str]]]]`
`src/documate/site.py:1126`

Pages bucketed by directory, order preserved: [(dir, [[slug, filename], …]), …].
The tree renders one collapsible group per directory.

**called by** `_nav_js`  ·  **calls** `_nav_labels`

### `_search_index(model: Model, guides: list[Guide]) -> list[list[str]]`
`src/documate/site.py:1137`

Everything the palette can jump to: [kind, name, context, href]. Modules and
the two headline pages, plus every documented symbol at its `page.html#name`.

**called by** `_nav_js`

### `_nav_js(model: Model, guides: list[Guide]) -> str`
`src/documate/site.py:1155`

The shared client: sidebar data (doc links + directory groups) + the search
index, injected into the app template. One copy for the whole site.

**called by** `render`  ·  **calls** `_groups`, `_search_index`

### `_crumb(*parts: str) -> str`
`src/documate/site.py:1170`

A breadcrumb: last part bold, joined by faint slashes.

**called by** `_architecture`, `_guide`, `_overview`, `_page`

### `_layout(model: Model, title: str, active: str, crumb: str, body: str, body_class: str='', hero: str='', desc: str='') -> str`
`src/documate/site.py:1180`

Wrap a page body in the shared shell: sidebar (brand + coverage, nav filled by
nav.js), sticky top bar (breadcrumb + search + theme), an optional full-width hero
band, the reading column, and the search palette. `active` (the page's slug) marks
its nav link; everything else is one shared nav.js and style.css, so a page's size
never grows with the page count. `desc` feeds the description/OpenGraph meta, so
a shared link unfurls as the page's own summary instead of bare markup.

**called by** `_architecture`, `_guide`, `_overview`, `_page`

### `_links(mods) -> str`
`src/documate/site.py:1255`

Chip links to sibling subsystem pages, monospace like everywhere else.

**called by** `_architecture`, `_page`

### `_featured(guides) -> Guide | None`
`src/documate/site.py:1267`

The guide to headline on the landing page — the first getting-started/install
page. None -> the landing page stays a plain docs index (nothing to inline).

**called by** `_overview`

### `_split_intro(text: str) -> tuple[str, str]`
`src/documate/site.py:1277`

A featured guide's markdown -> (lede, rest): drop the leading `# ` title, take the
first paragraph as the hero lede, hand back the remainder for the Getting started
section — so the opening line isn't printed twice.

**called by** `_overview`

### `_plain(text: str) -> str`
`src/documate/site.py:1296`

Markdown spans flattened to plain text — for one-line descriptions.

**called by** `_guide_desc`, `_overview`, `_repo_lede`

### `_clip(text: str, limit: int) -> str`
`src/documate/site.py:1301`

Text cut at a word boundary near `limit`, with an ellipsis when clipped.

**called by** `_feats`, `_guide_desc`, `_overview`, `_repo_lede`

### `_repo_lede(root) -> str`
`src/documate/site.py:1308`

The repo README's opening paragraph, as the landing-page lede when no
getting-started guide provides one. Headings, badges, images, HTML, lists and
fenced code are skipped; empty when nothing usable is found.

**called by** `run`  ·  **calls** `_clip`, `_plain`

### `_guide_desc(text: str) -> str`
`src/documate/site.py:1328`

A guide's first plain paragraph, clipped to one row-description line.
Headings, anchors, lists, tables and fences are skipped — a guide that opens
with `## Abstract` describes itself by the prose after it, not the heading.

**called by** `_guide`, `_overview`  ·  **calls** `_clip`, `_plain`

### `_shell_block(root) -> list[str]`
`src/documate/site.py:1344`

The README's first shell-fenced block, as terminal lines for the landing
hero — the project's own quick start, never invented copy. Empty when the
README carries no such block.

**called by** `run`

### `_rows(items) -> str`
`src/documate/site.py:1368`

An Apple-docs style topic list: linked name + one-line description per row.
`items` yields (href, name, description-html-or-empty, mono?) tuples.

**called by** `_overview`

### `_feats(model: Model, guides) -> str`
`src/documate/site.py:1390`

The explore-card grid under the hero: the fixed destinations (architecture,
reference) plus the repo's hardware and troubleshooting guides when it has
them. Card copy is the guide's own title — nothing invented.

**called by** `_overview`  ·  **calls** `_clip`, `_guide_rank`

### `_overview(model: Model, guides=(), lede: str='', cmds: list[str]=()) -> str`
`src/documate/site.py:1425`

index.html — the landing page: a split product hero (name, lede, copyable
quick-start command, and the README's own setup session in a terminal window),
explore cards, the Getting started guide inlined when the repo ships one, then
the guides and the per-directory reference as described topic rows — a front
page a newcomer can read, not a wall of file paths.

**called by** `render`  ·  **calls** `_clip`, `_crumb`, `_feats`, `_featured`, `_guide_desc`, `_guide_rank`, `_inline`, `_layout`

### `_architecture(model: Model) -> str`
`src/documate/site.py:1579`

architecture.html — docs/ARCHITECTURE.md as a site page: every subsystem in
reading order (entry points first), each linking into its per-module page.

**called by** `render`  ·  **calls** `_crumb`, `_layout`, `_links`, `_map_marks`, `_mermaid`, `_prose`

### `_guide(model: Model, g: Guide) -> str`
`src/documate/site.py:1641`

One authored page: its title in the hero band, its markdown below (minus the
title line the band already carries).

**called by** `render`  ·  **calls** `_crumb`, `_guide_desc`, `_layout`, `_markdown`

### `_page(model: Model, p: Page) -> str`
`src/documate/site.py:1664`

One subsystem page: hero band (name, summary, path), Overview prose, edge
chips, flow diagram, then the per-symbol API with kind badges.

**called by** `render`  ·  **calls** `_crumb`, `_inline`, `_layout`, `_links`, `_mermaid`, `_prose`

### `render(model: Model, guides: list[Guide]=(), lede: str='', cmds: list[str]=()) -> dict[str, str]`
`src/documate/site.py:1744`

Model (+ authored guides) -> {filename: html/css/js}. Deterministic, flat:
index (overview) + architecture + one page per subsystem + one per guide + the
shared stylesheet and script — the whole site, ready for any static host.
`lede` is the landing-page fallback tagline and `cmds` its hero terminal
session, both mined from the repo README.

**called by** `run`  ·  **calls** `_architecture`, `_guide`, `_nav_js`, `_overview`, `_page`

### `run(ctx: Context) -> int`
`src/documate/site.py:1765`

Write the site under site_dir, pruning orphaned pages of ours (same contract as
`docs.run`: a renamed source file must not leave its old page behind). Drops a
`.nojekyll` so GitHub Pages serves the files verbatim.

**called by** `_remote_base`  ·  **calls** `_guides`, `_repo_lede`, `_resolve_links`, `_shell_block`, `render`

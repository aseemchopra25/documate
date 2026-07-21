# How documate keeps these docs honest

<!-- documents: sym:build_model sig:181d888bbcc5d000 sym:find_drift sig:03f90c8a71972d8f -->

Everything under `docs/architecture/` (plus the overview and `ARCHITECTURE.md`, the
one-page stitch of every subsystem) is **generated**: `documate`
reads the code graph (built by documate's own in-repo tree-sitter engine) and your
docstrings through `build_model` and writes markdown — and
it looks wherever the doc legitimately lives (inside a Python def, above a Go func, on the
C header prototype rather than the definition, in a symbol-free `doc.go`, in a shell
script's `#` header).

## Edges are earned, not invented

Where the graph
leaves an edge unresolved (Go stores a cross-package call as a bare name), `build_model`
re-qualifies it only when the call is literally in the caller's source, under the
package's *declared* name or import alias — Go never promises a package matches its
directory — and a missing edge
always beats an invented one.

A C include is found the way the compiler would look for
it: next to the includer first, then from the repo root, then elsewhere only when the
name is unique *and* plausibly the includer's — one vendor snapshot of `config.h` in an
IDE corner must not read as what a hundred modules depend on.

## What reads first

**Start here** points
at the doors that open the most of the codebase, not the first three alphabetically —
a repo with two hundred example programs has two hundred doors, and almost all of them
are broom closets.

`ARCHITECTURE.md` reads in
dependency order, not alphabetically: the entry points nothing else imports come first,
then the machinery they drive — the same graph walk that puts a **Start here** line on
the overview (a machine-generated file, stringer output say, gets no page at all —
`build_model` drops banner-carrying source the same way it drops `skip_dirs` trees:
nobody reads it, so it owes no documentation).

## Evidence, never padding

Where a docstring is missing outright, the pages fall back
to *evidence* the repo already contains — the subject of the commit that created the
module, and what a symbol's tests assert, read off their names — always labeled as what
it is, and never counted as documentation coverage. (A commit that adds a whole pile of
modules at once — a tree move, a vendor import — is skipped as an origin: its subject
describes none of them.)

Documentation the repo already has
joins in the same spirit: a tracked markdown or reStructuredText file that names a
module by path gets linked from that module's page (*discussed in*).

The overview's **Hotspots** section is more mined evidence — the
most-changed modules, and the pairs that change together without an import edge between
them — pinned to the commit it was mined at: `documate --check` re-mines at that printed
pin rather than at HEAD, so the counts stay a pure function of the committed page and
history growing never reads as staleness.

## Regenerate, then gate

Nobody
edits those pages — if the code changes, the pages are regenerated, and `documate --check`
fails CI whenever the committed pages differ from what regeneration would produce. (Past
~40 modules the same pages regroup by directory — a front page of directories, one folder
per directory — but they're still 100% recomputed on every run.)

The stamp on every
generated page's first line is also a property line: `documate` refuses to overwrite
and won't prune any file that doesn't carry it, so a docs tree that predates documate is
never clobbered.

## Authored pages

Pages like this one are **authored**: hand-written prose for the things a generator can't
know (the why, the trade-offs). An authored page declares what code it describes with an
invisible anchor comment — this page anchors `build_model` (the docs generator's core) and
`find_drift` (the staleness detector). When either function's *code* changes without this
page changing too, `find_drift` reports it and `documate --check` blocks the commit: the doc
is presumed lying until a human looks. That check is per-symbol, not per-file: it compares an
**AST fingerprint** of the symbol between the merge-base and your working tree, so reindenting
it, reformatting it, or editing an unrelated function in the same file is never mistaken for
drift — while a changed operator, a new parameter, or even a different string literal inside
the body always is.

An anchor can go one step further and pin the exact code the author
verified the prose against — that same fingerprint written inline as a `sig:` token (this
page's anchors carry them). A pinned anchor stops caring about git deltas entirely: it drifts
exactly when *that symbol's* fingerprint differs from what was verified, however long ago and
however the file around it churns, and the failure message hands you the new sig to re-pin
once you've re-read the prose.

That's the whole contract. Generated pages can't rot because they're recomputed;
authored pages can't rot silently because they're anchored.

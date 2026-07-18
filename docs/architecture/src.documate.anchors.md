<!-- generated documentation — edit the source, not this file -->
# `src/documate/anchors.py`

anchors.py — scan authored docs for `documents:` anchors and validate them.

An authored page (hand-written markdown under docs/) declares what code it describes:

    <!-- documents: sym:unlock -->               (prose, invisible HTML comment)
    <!-- documents: sym:unlock sig:0f3a9c1b2d4e5f60 -->   (pinned to a fingerprint)
    %% documents: sym:ble_handler                 (inside a mermaid block)

`build_index` scans every .md under docs_dir into `{anchor: [pages]}` — graph-free and
deterministic, computed fresh each run (nothing to commit or keep in sync). Generated
pages carry no anchors (their freshness is checked by regeneration instead), so this
is effectively the authored tier's map. `validate` resolves each anchor to confirm the
code it names still exists; a sym: against a missing graph degrades to a warning.

A `sig:` token pins the *preceding* sym: to the fingerprint of the code the author
verified the prose against (drift prints the current value). With a sig, drift for
that page/anchor is decided by fingerprint comparison instead of file-level git diff —
per-symbol and base-ref-free. The sig lives inline in the anchor, never in a lock
file: it travels with the prose it protects and updates in the same edit.
Stdlib only.

**depends on** [`src/documate/core.py`](src.documate.core.md), [`src/documate/resolve.py`](src.documate.resolve.md)  ·  **used by** [`src/documate/check.py`](src.documate.check.md), [`src/documate/drift.py`](src.documate.drift.md)

## API

### `scan(ctx: Context) -> tuple[dict[str, list[str]], dict[tuple, str], list[tuple]]`
`src/documate/anchors.py:36`

One pass over every authored page → (index, sigs, bad).

index  {anchor: sorted[pages]} — as `build_index` always returned.
sigs   {(anchor, page): sig} — the fingerprint pins, one per sym token.
bad    [(page, token, reason)] — malformed/orphaned/conflicting sig tokens;
       loud, because a sig that silently never matches would flag forever
       and one that silently binds wrong would never flag.

**called by** `build_index`, `validate`

### `build_index(ctx: Context) -> dict[str, list[str]]`
`src/documate/anchors.py:77`

Scan every doc page for anchor comments → `{anchor: sorted[pages]}`. Graph-free,
deterministic: same .md tree in, same dict out, anywhere.

**calls** `scan`

### `validate(ctx: Context) -> tuple[list[tuple], list[tuple]]`
`src/documate/anchors.py:83`

Resolve every declared anchor against real code.

Returns (failed, degraded): lists of (anchor, error, pages). A failed anchor names
a ghost (renamed/deleted code — the doc lies) or carries a broken sig token; a
degraded one just couldn't be verified (graph absent/locked) and must never gate.

**calls** `scan`

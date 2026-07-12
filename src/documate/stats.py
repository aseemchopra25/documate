"""stats.py — `documate --stats`: the documentation dashboard.

What the repo's documentation looks like right now (coverage bars, doc-line
counts, page sizes), how it moved (+/− deltas), and what the model layer has
cost so far. Two append-only jsonl ledgers next to the graph carry the
history: `stats.jsonl` gets a snapshot whenever a docs run or --stats sees
the numbers change, `spend.jsonl` gets one line per --ai run (prose appends
it even on Ctrl-C — spent tokens must never vanish from the bill). Reading
either ledger degrades: absent or garbled lines render as "no history yet",
never a crash — the dashboard is a viewer, not a gate.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from . import ui
from .core import GENERATED_STAMP, Context

#: The snapshot fields deltas compare (everything but the timestamp) — two
#: snapshots agreeing on all of these are the same state, so record() skips.
_METRICS = (
    "symbols_documented",
    "symbols_total",
    "modules_documented",
    "modules_total",
    "files_documented",
    "files_total",
    "doc_lines",
    "pages_generated",
    "pages_authored",
    "page_lines",
)


def _dir(ctx: Context) -> Path:
    """The ledgers' home — the graph's directory, so one place holds every
    documate artifact (and one .gitignore keeps them all out of commits)."""
    return ctx.config.graph_db.parent


def _read(path: Path) -> list[dict]:
    """Parse a jsonl ledger; a missing file or a garbled line reads as absent."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for ln in lines:
        try:
            row = json.loads(ln)
        except ValueError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def snapshot(ctx: Context, model=None) -> dict:
    """Measure the repo's documentation right now, as one flat dict.

    Symbols/modules come from the docs model (pass the one docs.run just built
    to avoid a rebuild); doc_lines counts docstring/doc-comment lines living in
    source; the page numbers count the docs tree itself, split by stamp into
    generated and authored."""
    from . import docs  # runtime import: docs imports stats to record snapshots

    model = model or docs.build_model(ctx)
    doc_lines = sum(
        len(s.doc.splitlines()) for p in model.pages for s in p.symbols if s.doc
    ) + sum(len(p.module_doc.splitlines()) for p in model.pages if p.module_doc)
    gen = auth = page_lines = 0
    for md in sorted(ctx.config.docs_dir.rglob("*.md")):
        try:
            text = md.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if text.startswith(GENERATED_STAMP):
            gen += 1
        else:
            auth += 1
        page_lines += len(text.splitlines())
    return {
        "ts": time.strftime("%Y-%m-%d %H:%M"),
        "symbols_documented": model.coverage["documented"],
        "symbols_total": model.coverage["total"],
        "modules_documented": sum(1 for p in model.pages if p.module_doc),
        "modules_total": len(model.pages),
        "files_documented": sum(
            1 for p in model.pages if p.module_doc and all(s.doc for s in p.symbols)
        ),
        "files_total": len(model.pages),
        "doc_lines": doc_lines,
        "pages_generated": gen,
        "pages_authored": auth,
        "page_lines": page_lines,
    }


def _changed(a: dict, b: dict) -> bool:
    """True when two snapshots differ on any metric (timestamps don't count)."""
    return any(a.get(k) != b.get(k) for k in _METRICS)


def record(ctx: Context, model=None, snap: dict | None = None) -> None:
    """Append the current snapshot to stats.jsonl — only when the numbers moved,
    so the ledger is a history of states, not of invocations."""
    snap = snap or snapshot(ctx, model)
    path = _dir(ctx) / "stats.jsonl"
    hist = _read(path)
    if hist and not _changed(hist[-1], snap):
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(snap) + "\n")


def add_spend(ctx: Context, model: str, tokens: int, usd: float) -> None:
    """Append one --ai run's measured bill to spend.jsonl; an unmeasured run
    (scripted stand-ins, a declined pre-flight) writes nothing."""
    if not tokens and not usd:
        return
    path = _dir(ctx) / "spend.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "ts": time.strftime("%Y-%m-%d %H:%M"),
        "model": model,
        "tokens": tokens,
        "usd": round(usd, 6),
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


def _tok(n: int) -> str:
    """A token count as a compact human number (874 → '874', 1934 → '1.9k')."""
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def _usd(x: float) -> str:
    """Dollars with enough precision to show a sub-cent run without noise."""
    return f"${x:.2f}" if x >= 0.05 else f"${x:.4f}"


def _bar(done: int, total: int, width: int = 22) -> str:
    """A █/░ gauge; a started-but-unfinished count always shows ≥1 filled cell."""
    if total <= 0:
        return "░" * width
    fill = done * width // total
    if done and not fill:
        fill = 1
    return "█" * fill + "░" * (width - fill)


def _delta(n: int) -> tuple[str, str | None]:
    """A +N/−N/±0 segment, colored by direction (documentation up = green)."""
    if n > 0:
        return (f"  +{n}", "green")
    if n < 0:
        return (f"  −{-n}", "red")
    return ("  ±0", "dim")


def _hue(pct: int) -> str:
    """The coverage traffic light shared with the docs summary line."""
    return "green" if pct >= 80 else "yellow" if pct >= 50 else "red"


def _coverage_rows(snap: dict, prev: dict | None) -> list[list[tuple]]:
    """The dashboard's top half: one gauge per coverage axis, one counted line
    per volume metric, each with its +/− vs the previous distinct snapshot."""
    rows: list[list[tuple]] = []
    for label, dkey, tkey in (
        ("symbols", "symbols_documented", "symbols_total"),
        ("modules", "modules_documented", "modules_total"),
        ("files", "files_documented", "files_total"),
    ):
        d, t = snap[dkey], snap[tkey]
        pct = d * 100 // t if t else 0
        row = [
            (f"{label:<10}", "bold"),
            (_bar(d, t), _hue(pct)),
            (f"  {d}/{t} · {pct}%", None),
        ]
        if prev is not None:
            row.append(_delta(d - prev.get(dkey, 0)))
        rows.append(row)
    rows.append([("", None)])
    for label, key, unit in (
        ("doc lines", "doc_lines", "docstring/doc-comment line(s) in source"),
        ("pages", "pages_generated", "generated + authored page(s) in docs"),
        ("page lines", "page_lines", "line(s) across the docs tree"),
    ):
        n = snap[key] + (snap["pages_authored"] if key == "pages_generated" else 0)
        row = [(f"{label:<10}", "bold"), (f"{n:>6}", "cyan"), (f"  {unit}", "dim")]
        if prev is not None:
            base = prev.get(key, 0) + (
                prev.get("pages_authored", 0) if key == "pages_generated" else 0
            )
            row.append(_delta(n - base))
        rows.append(row)
    return rows


def _spend_rows(spends: list[dict]) -> list[list[tuple]]:
    """The bill: all-time totals, then a per-model line when several ran."""
    tokens = sum(s.get("tokens") or 0 for s in spends)
    usd = sum(s.get("usd") or 0.0 for s in spends)
    rows = [
        [
            ("all time  ", "bold"),
            (f"{_tok(tokens)} tok · {_usd(usd)}", "cyan"),
            (
                f"  across {len(spends)} --ai run(s), last {spends[-1].get('ts', '?')}",
                "dim",
            ),
        ]
    ]
    by_model: dict[str, list[dict]] = {}
    for s in spends:
        by_model.setdefault(str(s.get("model") or "?"), []).append(s)
    if len(by_model) > 1:
        for name, runs in sorted(by_model.items()):
            t = sum(r.get("tokens") or 0 for r in runs)
            u = sum(r.get("usd") or 0.0 for r in runs)
            rows.append(
                [
                    (f"  {name:<8}", None),
                    (f"{_tok(t)} tok · {_usd(u)}", "cyan"),
                    (f"  {len(runs)} run(s)", "dim"),
                ]
            )
    return rows


def run(ctx: Context) -> int:
    """`documate --stats`: render the dashboard and record today's snapshot.

    Deltas compare against the most recent ledger snapshot that differs from
    now — i.e. what the latest round of work changed — and the first ever run
    says so instead of faking a zero delta. Read-only apart from the ledger
    append; never gates, always exits 0."""
    snap = snapshot(ctx)
    hist = _read(_dir(ctx) / "stats.jsonl")
    prev = next((h for h in reversed(hist) if _changed(h, snap)), None)
    ui.card(f"documentation · {ctx.root.name}", _coverage_rows(snap, prev))
    spends = _read(_dir(ctx) / "spend.jsonl")
    if spends:
        ui.card("model spend", _spend_rows(spends))
    else:
        ui.note("model spend: none recorded — --ai runs will show up here")
    if prev is not None:
        ui.note(f"deltas vs the {prev.get('ts', '?')} snapshot")
    elif not hist:
        ui.note("first snapshot recorded — future runs will show +/− deltas")
    record(ctx, snap=snap)
    pct = (
        snap["symbols_documented"] * 100 // snap["symbols_total"]
        if snap["symbols_total"]
        else 0
    )
    spent = (
        f" · {_usd(sum(s.get('usd') or 0.0 for s in spends))} spent" if spends else ""
    )
    ui.ok(
        f"stats: {snap['symbols_documented']}/{snap['symbols_total']} symbols "
        f"documented ({pct}%) · {snap['doc_lines']} doc line(s) in source{spent}"
    )
    return 0

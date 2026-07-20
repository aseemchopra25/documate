"""site.py — `documate --html`: the same docs, rendered as a static site.

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
"""

from __future__ import annotations

import html
import json
import os
import posixpath
import re
import subprocess
from dataclasses import dataclass

from . import ui
from .core import STAMPS, Context
from .docs import (
    _EDGE_CAP,
    Model,
    Page,
    _dir,
    _mermaid_lines,
    _slug,
    _tail,
    _tour,
    build_model,
)

_CODE_RE = re.compile(r"`([^`]+)`")
_BOLD_RE = re.compile(r"\*\*([^*]+)\*\*")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")

# The landing page draws the module map only while it stays legible; past this many
# edges it reads as a hairball, and the architecture page still carries the full one.
_MAP_CAP = 24

# Mermaid, loaded as a classic (UMD) script so it renders from `file://` too — the ESM
# dynamic import silently failed there and left raw `flowchart` text on the page. Both
# tags are deferred so they never block first paint; offline, the <pre> text stays.
_HEAD_SCRIPTS = (
    '<script defer src="https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js"></script>\n'
    '<script defer src="nav.js"></script>'
)

_CSS = r""":root{
  --ground:#f7f8fa; --surface:#ffffff; --raise:#eff1f5; --card:#ffffff;
  --ink:#1d2129; --strong:#0e1116; --muted:#5c6470; --faint:#8b93a0;
  --line:rgba(18,26,38,.12); --hairline:rgba(18,26,38,.075);
  --accent:#0a69da; --accent-ink:#085ec2; --tint:rgba(10,105,218,.085); --tint-line:rgba(10,105,218,.28);
  --ok:#189a4e;
  --mono:ui-monospace,"SF Mono","JetBrains Mono","Cascadia Code",Menlo,Consolas,monospace;
  --sans:-apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  --shadow:0 1px 2px rgba(14,20,30,.05),0 10px 30px -18px rgba(14,20,30,.18);
  --shadow-lg:0 28px 70px -24px rgba(14,20,30,.4);
  --article:46rem; --side:16.5rem;
}
@media (prefers-color-scheme:dark){:root{
  --ground:#0b0c0f; --surface:#121419; --raise:#1a1d24; --card:#14171d;
  --ink:#d8dde5; --strong:#f4f6fa; --muted:#8f98a5; --faint:#5f6875;
  --line:rgba(226,235,248,.13); --hairline:rgba(226,235,248,.07);
  --accent:#3f97f5; --accent-ink:#7cb5f9; --tint:rgba(77,159,255,.12); --tint-line:rgba(77,159,255,.38);
  --ok:#3ecf7a;
  --shadow:0 1px 2px rgba(0,0,0,.5),0 14px 34px -16px rgba(0,0,0,.6);
  --shadow-lg:0 34px 80px -24px rgba(0,0,0,.75);
}}
:root[data-theme="light"]{
  --ground:#f7f8fa; --surface:#ffffff; --raise:#eff1f5; --card:#ffffff;
  --ink:#1d2129; --strong:#0e1116; --muted:#5c6470; --faint:#8b93a0;
  --line:rgba(18,26,38,.12); --hairline:rgba(18,26,38,.075);
  --accent:#0a69da; --accent-ink:#085ec2; --tint:rgba(10,105,218,.085); --tint-line:rgba(10,105,218,.28);
  --ok:#189a4e;
  --shadow:0 1px 2px rgba(14,20,30,.05),0 10px 30px -18px rgba(14,20,30,.18);
  --shadow-lg:0 28px 70px -24px rgba(14,20,30,.4);
}
:root[data-theme="dark"]{
  --ground:#0b0c0f; --surface:#121419; --raise:#1a1d24; --card:#14171d;
  --ink:#d8dde5; --strong:#f4f6fa; --muted:#8f98a5; --faint:#5f6875;
  --line:rgba(226,235,248,.13); --hairline:rgba(226,235,248,.07);
  --accent:#3f97f5; --accent-ink:#7cb5f9; --tint:rgba(77,159,255,.12); --tint-line:rgba(77,159,255,.38);
  --ok:#3ecf7a;
  --shadow:0 1px 2px rgba(0,0,0,.5),0 14px 34px -16px rgba(0,0,0,.6);
  --shadow-lg:0 34px 80px -24px rgba(0,0,0,.75);
}
*{box-sizing:border-box}
html{color-scheme:light dark}
:root[data-theme="light"]{color-scheme:light}
:root[data-theme="dark"]{color-scheme:dark}
body{margin:0;background:var(--ground);color:var(--ink);font-family:var(--sans);
  font-size:16px;line-height:1.65;-webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}
a{color:var(--accent-ink);text-decoration:none}
code{font-family:var(--mono)}
::selection{background:var(--tint)}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:4px}
.app{display:grid;grid-template-columns:var(--side) minmax(0,1fr);min-height:100vh}

.side{position:sticky;top:0;height:100vh;display:flex;flex-direction:column;background:var(--surface);
  border-right:1px solid var(--hairline);z-index:40;transition:transform .28s cubic-bezier(.4,0,.2,1)}
.side-head{padding:1.2rem 1.2rem .95rem;border-bottom:1px solid var(--hairline)}
.brand{display:flex;align-items:baseline;gap:.55rem}
.brand b{font-family:var(--mono);font-weight:650;font-size:1rem;letter-spacing:-.02em;color:var(--strong)}
.brand .tag{font-size:.58rem;font-weight:650;letter-spacing:.14em;text-transform:uppercase;color:var(--faint);
  border:1px solid var(--line);border-radius:99px;padding:.14rem .45rem}
.cov{margin-top:.9rem}
.cov-row{display:flex;justify-content:space-between;align-items:baseline;font-size:.68rem;color:var(--faint);margin-bottom:.35rem}
.cov-row b{color:var(--ok);font-variant-numeric:tabular-nums;font-family:var(--mono);font-weight:600;font-size:.72rem}
.cov-bar{height:4px;border-radius:99px;background:var(--raise);overflow:hidden}
.cov-bar span{display:block;height:100%;border-radius:99px;background:var(--ok);
  transform-origin:left;animation:grow 1s cubic-bezier(.2,.7,.2,1) both}
@keyframes grow{from{transform:scaleX(0)}}
.search-btn{margin:.85rem 1.2rem;display:flex;align-items:center;gap:.5rem;width:calc(100% - 2.4rem);
  padding:.5rem .7rem;border:1px solid var(--line);border-radius:9px;background:var(--ground);
  color:var(--faint);font-family:var(--sans);font-size:.8rem;cursor:pointer;transition:border-color .15s,color .15s}
.search-btn:hover{border-color:var(--tint-line);color:var(--muted)}
.search-btn kbd{margin-left:auto;font-family:var(--mono);font-size:.64rem;color:var(--faint);
  border:1px solid var(--line);border-radius:5px;padding:.06rem .32rem;background:var(--surface)}
.tree{flex:1;overflow-y:auto;padding:.3rem .75rem 1.6rem;scrollbar-width:thin;scrollbar-color:var(--line) transparent}
.tree::-webkit-scrollbar{width:8px}
.tree::-webkit-scrollbar-thumb{background:var(--line);border-radius:9px;border:2px solid var(--surface)}
.doclinks{display:flex;flex-direction:column;gap:1px;padding:.2rem 0 .5rem}
.doclink{display:flex;align-items:center;gap:.55rem;padding:.42rem .6rem;border-radius:8px;
  font-size:.85rem;color:var(--ink);font-weight:500;transition:background .12s}
.doclink svg{width:.95rem;height:.95rem;color:var(--faint);flex:none}
.doclink:hover{background:var(--raise)}
.doclink.on{background:var(--tint);color:var(--accent-ink);font-weight:600}
.doclink.on svg{color:var(--accent)}
.tree-cap{padding:1.1rem .6rem .35rem;font-size:.62rem;font-weight:650;letter-spacing:.13em;
  text-transform:uppercase;color:var(--faint)}
.group{margin-top:.15rem}
.group-h{display:flex;align-items:center;gap:.45rem;width:100%;background:none;border:0;cursor:pointer;
  padding:.45rem .6rem;border-radius:8px;color:var(--muted);font-family:var(--mono);font-size:.72rem;
  font-weight:600;text-align:left;overflow-wrap:anywhere;line-height:1.4;transition:background .12s}
.group-h:hover{background:var(--raise)}
.group-h .chev{transition:transform .2s;font-size:.85rem;line-height:1;color:var(--faint);flex:none}
.group.collapsed .chev{transform:rotate(-90deg)}
.group.collapsed .items{display:none}
.items{display:flex;flex-direction:column;gap:1px;padding-left:.55rem}
.item{display:block;padding:.3rem .6rem;border-radius:7px;font-family:var(--mono);font-size:.76rem;
  color:var(--muted);overflow-wrap:anywhere;position:relative;transition:background .12s,color .12s}
.item:hover{background:var(--raise);color:var(--ink)}
.item.on{background:var(--tint);color:var(--accent-ink);font-weight:600}
.item-g{display:block;padding:.36rem .6rem;border-radius:7px;font-size:.8rem;color:var(--muted);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;transition:background .12s,color .12s}
.item-g:hover{background:var(--raise);color:var(--ink)}
.item-g.on{background:var(--tint);color:var(--accent-ink);font-weight:600}

.content{display:flex;flex-direction:column;min-width:0}
.topbar{position:sticky;top:0;z-index:30;display:flex;align-items:center;gap:.75rem;height:3.25rem;
  padding:0 1.5rem;background:color-mix(in srgb,var(--ground) 72%,transparent);
  backdrop-filter:saturate(1.6) blur(14px);-webkit-backdrop-filter:saturate(1.6) blur(14px);
  border-bottom:1px solid var(--hairline)}
.crumb{display:flex;align-items:center;gap:.45rem;font-family:var(--mono);font-size:.76rem;color:var(--faint);min-width:0}
.crumb .sep{color:var(--line)}
.crumb b{color:var(--ink);font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.spacer{flex:1}
.icon-btn{display:grid;place-items:center;width:2rem;height:2rem;border:1px solid transparent;
  border-radius:8px;background:none;color:var(--muted);cursor:pointer;transition:background .15s,color .15s}
.icon-btn:hover{color:var(--ink);background:var(--raise)}
.icon-btn svg{width:1rem;height:1rem}
.menu-btn{display:none}
.sun{display:none}
:root[data-theme="dark"] .sun{display:block}
:root[data-theme="dark"] .moon{display:none}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]) .sun{display:block}
  :root:not([data-theme="light"]) .moon{display:none}}

.mid{display:grid;grid-template-columns:1fr minmax(0,var(--article)) 1fr;column-gap:2.75rem}
.mid:has(.doc.reading){--article:42.5rem}
.doc{grid-column:2;min-width:0;padding:3rem 0 7rem;line-height:1.72;animation:rise .45s cubic-bezier(.2,.7,.2,1) both}
.doc.reading p{margin:1.1rem 0}
@keyframes rise{from{opacity:0;transform:translateY(8px)}}
.doc h1{font-size:1.85rem;font-weight:650;letter-spacing:-.021em;color:var(--strong);
  margin:.15rem 0 1.05rem;text-wrap:balance;line-height:1.18}
.doc h1.mono{font-family:var(--mono);font-size:1.6rem;letter-spacing:-.01em;overflow-wrap:anywhere}
.eyebrow{font-size:.7rem;font-weight:650;letter-spacing:.12em;text-transform:uppercase;color:var(--accent);margin-bottom:.65rem}
.eyebrow code{font-size:.95em;text-transform:none;letter-spacing:.02em;color:var(--muted)}
.doc h2{font-size:1.25rem;font-weight:650;letter-spacing:-.014em;margin:2.6rem 0 .9rem;color:var(--strong)}
.doc h3{font-size:1.02rem;font-weight:650;margin:1.7rem 0 .45rem;color:var(--strong)}
.doc h4{font-size:.92rem;font-weight:650;margin:1.2rem 0 .3rem}
.doc p{margin:.9rem 0}
.doc p a,.doc li a,.doc td a{text-decoration:underline;
  text-decoration-color:color-mix(in srgb,var(--accent) 30%,transparent);text-underline-offset:3px}
.doc p a:hover,.doc li a:hover,.doc td a:hover{text-decoration-color:var(--accent)}
.doc .cards a,.doc .chips a{text-decoration:none}
p code,li code,.chips code,.xref code,dd code{background:var(--raise);
  padding:.1em .36em;border-radius:5px;font-size:.84em;color:var(--ink)}
pre{background:var(--raise);border:1px solid var(--hairline);border-radius:12px;
  padding:1rem 1.2rem;overflow-x:auto;font-family:var(--mono);font-size:.82rem;line-height:1.6;color:var(--ink)}
.prewrap{position:relative;margin:1.15rem 0}
.prewrap pre{margin:0}
.copy{position:absolute;top:.55rem;right:.55rem;border:1px solid var(--line);background:var(--surface);
  color:var(--muted);border-radius:7px;font-family:var(--sans);font-size:.68rem;font-weight:550;
  padding:.26rem .6rem;opacity:0;cursor:pointer;transition:opacity .15s,color .15s,border-color .15s}
.prewrap:hover .copy,.copy:focus-visible{opacity:1}
.copy.done{color:var(--ok);border-color:var(--ok);opacity:1}
.section-h{display:flex;align-items:center;gap:.75rem;margin:3rem 0 1.15rem}
.section-h h2{font-size:.7rem;letter-spacing:.14em;text-transform:uppercase;color:var(--faint);margin:0;font-weight:650}
.section-h .rule{flex:1;height:1px;background:var(--hairline)}
.chips{color:var(--muted);font-size:.85rem;line-height:2}
.chips a{color:var(--accent-ink)}
.stat{font-family:var(--mono);font-size:.85rem;color:var(--muted)}

.graph-shell{border:1px solid var(--hairline);border-radius:16px;margin:1.5rem 0;background:
  radial-gradient(circle at 1px 1px,var(--hairline) 1px,transparent 0) 0 0/22px 22px,var(--surface);
  overflow:auto;max-height:33rem;display:flex;justify-content:safe center;
  align-items:safe center;box-shadow:var(--shadow)}
pre.mermaid{margin:0;padding:1.8rem;background:none;border:0;border-radius:0;overflow:visible;
  font-size:.78rem;color:var(--faint)}

.cards{list-style:none;padding:0;margin:1.1rem 0;display:grid;gap:.85rem;
  grid-template-columns:repeat(auto-fill,minmax(15.5rem,1fr))}
.cards a{display:block;height:100%;border:1px solid var(--hairline);border-radius:14px;padding:1rem 1.1rem;
  background:var(--card);transition:border-color .16s,box-shadow .16s}
.cards a:hover{border-color:var(--tint-line);box-shadow:var(--shadow)}
.cards .name{display:block;font-family:var(--mono);font-size:.82rem;font-weight:600;
  color:var(--strong);overflow-wrap:anywhere;transition:color .16s}
.cards a:hover .name{color:var(--accent-ink)}
.cards .about{display:block;font-size:.84rem;color:var(--muted);margin-top:.4rem;line-height:1.55}
.arch{display:flex;flex-direction:column}
.arch-sec{border-top:1px solid var(--hairline);padding:1.8rem 0 .5rem}
.arch-sec h2{font-family:var(--mono);font-size:1.02rem;margin:0 0 .55rem}
.arch-sec h2 a{color:var(--strong)}
.arch-sec h2 a:hover{color:var(--accent-ink)}

.hero{padding:.7rem 0 1.2rem}
.hero h1{font-size:clamp(2.1rem,4.5vw,2.7rem);line-height:1.08;letter-spacing:-.025em;font-weight:700;margin:.4rem 0 .7rem}
.lede{font-size:1.12rem;color:var(--muted);max-width:36rem;margin:.2rem 0 1.6rem;line-height:1.6;text-wrap:pretty}
.stats{display:flex;flex-wrap:wrap;gap:2.4rem;margin:0 0 1.8rem}
.stat-b b{display:block;font-size:1.45rem;font-weight:650;color:var(--strong);
  font-variant-numeric:tabular-nums;letter-spacing:-.02em;line-height:1.2}
.stat-b span{font-size:.72rem;font-weight:600;letter-spacing:.09em;text-transform:uppercase;color:var(--faint)}
.cta{display:flex;flex-wrap:wrap;gap:.7rem}
.btn{display:inline-flex;align-items:center;text-decoration:none;padding:.55rem 1.25rem;border-radius:99px;
  border:1px solid var(--line);background:var(--surface);color:var(--ink);font-size:.9rem;font-weight:550;
  transition:border-color .15s,background .15s,color .15s}
.btn:hover{border-color:var(--tint-line);color:var(--accent-ink)}
.btn-primary{background:var(--accent);border-color:var(--accent);color:#fff}
.btn-primary:hover{background:var(--accent-ink);border-color:var(--accent-ink);color:#fff}
:root[data-theme="dark"] .btn-primary{color:#0b0c0f}
:root[data-theme="dark"] .btn-primary:hover{color:#0b0c0f}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]) .btn-primary{color:#0b0c0f}
  :root:not([data-theme="light"]) .btn-primary:hover{color:#0b0c0f}}

.api{display:flex;flex-direction:column;margin-top:.2rem}
.api-entry{position:relative;padding:1.55rem 0 .9rem;border-top:1px solid var(--hairline)}
.api-entry:first-child{border-top:0;padding-top:.4rem}
.api-entry.method{margin-left:1.5rem}
.api-entry h3{font-weight:600;margin:0}
.api-entry h3 code{display:block;background:var(--raise);border:1px solid var(--hairline);border-radius:10px;
  padding:.72rem .95rem;font-size:.85rem;line-height:1.55;color:var(--ink);overflow-x:auto;overflow-wrap:anywhere}
.api-entry h3 code b{font-weight:650;color:var(--accent-ink)}
.api-entry.method h3 code{font-size:.82rem}
.src{font-family:var(--mono);font-size:.68rem;color:var(--faint);margin:.5rem 0 .65rem}
.api-entry p{margin:.55rem 0;font-size:.92rem}
.xref{display:flex;flex-wrap:wrap;gap:.4rem;row-gap:.55rem;margin-top:.75rem;font-size:.74rem;color:var(--faint)}
.xref code{font-size:.92em}
.doc table{border-collapse:collapse;margin:1.2rem 0;font-size:.88rem;display:block;overflow-x:auto}
.doc th,.doc td{border:1px solid var(--hairline);padding:.45rem .75rem;text-align:left;vertical-align:top}
.doc th{background:var(--raise);font-weight:600}
dl.params{margin:.75rem 0 .2rem;font-size:.88rem}
dl.params dt{font-family:var(--mono);font-size:.82rem;color:var(--strong);margin:.6rem 0 .1rem}
dl.params dd{margin:0 0 .4rem;padding-left:1rem;border-left:2px solid var(--raise);color:var(--muted)}
details{margin-top:1.6rem;border:1px dashed var(--line);border-radius:12px;padding:.2rem 1rem;color:var(--muted)}
summary{cursor:pointer;padding:.65rem 0;font-size:.85rem;font-weight:600}
ul{padding-left:1.2rem}

.toc-rail{grid-column:3;justify-self:start;width:12.5rem;padding:3.3rem 1rem 2rem 0;font-size:.78rem}
.toc-in{position:sticky;top:4.4rem;max-height:calc(100vh - 6rem);overflow-y:auto;scrollbar-width:thin;
  scrollbar-color:var(--line) transparent}
.toc-cap{font-size:.62rem;font-weight:650;letter-spacing:.12em;text-transform:uppercase;color:var(--faint);margin:0 0 .5rem}
.toc-in nav{border-left:1px solid var(--hairline)}
.toc-link{display:block;padding:.24rem .8rem;color:var(--muted);border-left:1px solid transparent;
  margin-left:-1px;line-height:1.45;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
  transition:color .12s,border-color .12s}
.toc-link:hover{color:var(--ink)}
.toc-link.on{color:var(--accent-ink);border-left-color:var(--accent);font-weight:600}
.toc-link.mono{font-family:var(--mono);font-size:.72rem}

.scrim{position:fixed;inset:0;background:rgba(8,10,15,.45);backdrop-filter:blur(2px);opacity:0;
  pointer-events:none;transition:opacity .2s;z-index:60}
.scrim.on{opacity:1;pointer-events:auto}
.palette{position:fixed;top:13vh;left:50%;transform:translateX(-50%) scale(.98);width:min(37rem,92vw);
  background:color-mix(in srgb,var(--surface) 92%,transparent);backdrop-filter:blur(18px);
  -webkit-backdrop-filter:blur(18px);border:1px solid var(--line);border-radius:16px;box-shadow:var(--shadow-lg);
  z-index:70;opacity:0;pointer-events:none;transition:opacity .18s,transform .18s;overflow:hidden}
.palette.on{opacity:1;pointer-events:auto;transform:translateX(-50%) scale(1)}
.palette input{width:100%;border:0;border-bottom:1px solid var(--hairline);background:none;color:var(--ink);
  font-family:var(--sans);font-size:1rem;padding:1.05rem 1.3rem;outline:none}
.palette input::placeholder{color:var(--faint)}
.results{max-height:min(52vh,26rem);overflow-y:auto;padding:.5rem;scrollbar-width:thin;
  scrollbar-color:var(--line) transparent}
.res{display:flex;align-items:center;gap:.7rem;padding:.55rem .75rem;border-radius:9px;cursor:pointer;text-decoration:none}
.res:hover{background:var(--raise)}
.res.sel{background:var(--tint)}
.res .kind{font-family:var(--mono);font-size:.56rem;text-transform:uppercase;letter-spacing:.06em;color:var(--faint);
  border:1px solid var(--line);border-radius:5px;padding:.12rem .3rem;flex:none;width:3.6rem;text-align:center}
.res.sel .kind{color:var(--accent-ink);border-color:var(--tint-line)}
.res .nm{font-family:var(--mono);font-size:.84rem;color:var(--ink);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.res.sel .nm{color:var(--accent-ink)}
.res .ctx{margin-left:auto;font-size:.7rem;color:var(--faint);white-space:nowrap}
.res-empty{padding:1.6rem;text-align:center;color:var(--faint);font-size:.88rem}
.pal-foot{display:flex;gap:1rem;padding:.55rem 1.1rem;border-top:1px solid var(--hairline);font-size:.66rem;color:var(--faint)}
.pal-foot kbd{font-family:var(--mono);border:1px solid var(--line);border-radius:4px;padding:0 .3rem;margin-right:.25rem}

@media (max-width:1279px){
  .mid{grid-template-columns:1fr minmax(0,var(--article)) 1fr;column-gap:0}
  .toc-rail{display:none}
  .doc{padding-left:2rem;padding-right:2rem}
}
@media (max-width:900px){
  .app{grid-template-columns:1fr}
  .side{position:fixed;top:0;left:0;width:min(19rem,86vw);transform:translateX(-101%)}
  .side.open{transform:none;box-shadow:var(--shadow-lg)}
  .menu-btn{display:grid}
  .doc{padding:2rem 1.3rem 5rem}
  .stats{gap:1.6rem}
}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
"""

# The whole client: builds the sidebar from NAV (kept out of every page so a page's
# size doesn't grow with the page count), then wires search, theming, collapse, the
# mobile drawer, and Mermaid. A plain template with a __DATA__ hole — no f-string, so
# the JS braces stay readable.
_APP_JS = r"""const NAV=__DATA__;
const root=document.documentElement;
const store={get(k){try{return localStorage.getItem(k)}catch(e){return null}},set(k,v){try{localStorage.setItem(k,v)}catch(e){}}};
const ICON={home:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M3 10.5 12 3l9 7.5"/><path d="M5 9.5V21h14V9.5"/></svg>',arch:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"><path d="M12 3 2 8.5 12 14l10-5.5L12 3z"/><path d="M2 15.5 12 21l10-5.5"/></svg>',book:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"><path d="M4 5.5A2.5 2.5 0 0 1 6.5 3H20v15H6.5A2.5 2.5 0 0 0 4 20.5z"/><path d="M20 18v3H6.5A2.5 2.5 0 0 1 4 18.5"/></svg>'};
function el(t,c,x){const e=document.createElement(t);if(c)e.className=c;if(x!=null)e.textContent=x;return e}
const side=document.querySelector(".side"),tree=document.getElementById("tree");
const active=side?(side.dataset.active||""):"";
// one collapse-state set for the whole site, so the tree looks the same page to page
const folded=new Set((store.get("dm-folded")||"").split("\n").filter(Boolean));
if(tree){
  const dl=el("div","doclinks");
  const primary=NAV.docs.slice(0,2),guides=NAV.docs.slice(2);
  for(const [key,label,icon] of primary){
    const a=el("a","doclink"+(key===active?" on":""));a.href=key+".html";
    a.innerHTML=ICON[icon]||"";a.appendChild(document.createTextNode(label));dl.appendChild(a);
  }
  tree.appendChild(dl);
  if(guides.length){
    tree.appendChild(el("div","tree-cap","Guides"));
    const gbox=el("div","items");gbox.style.paddingLeft="0";
    for(const [key,label] of guides){
      const a=el("a","item-g"+(key===active?" on":""),label);a.href=key+".html";a.title=label;gbox.appendChild(a);
    }
    tree.appendChild(gbox);
  }
  if(NAV.groups.length)tree.appendChild(el("div","tree-cap","Reference"));
  for(const [dir,items] of NAV.groups){
    const g=el("div","group"),h=el("button","group-h");
    h.innerHTML='<span class="chev">&#8964;</span>';h.appendChild(document.createTextNode(dir));
    const box=el("div","items");
    for(const [slug,label] of items){const a=el("a","item"+(slug===active?" on":""),label);a.href=slug+".html";box.appendChild(a)}
    if(folded.has(dir))g.classList.add("collapsed");
    h.onclick=()=>{
      g.classList.toggle("collapsed");
      g.classList.contains("collapsed")?folded.add(dir):folded.delete(dir);
      store.set("dm-folded",[...folded].join("\n"));
    };
    g.appendChild(h);g.appendChild(box);tree.appendChild(g);
  }
  const on=tree.querySelector(".on");
  if(on)tree.scrollTop=Math.max(0,on.offsetTop-tree.clientHeight/2);
}
// theme
const saved=store.get("dm-theme");if(saved)root.dataset.theme=saved;
const isDark=()=>root.dataset.theme?root.dataset.theme==="dark":matchMedia("(prefers-color-scheme:dark)").matches;
const themeBtn=document.getElementById("themeBtn");
if(themeBtn)themeBtn.onclick=()=>{root.dataset.theme=isDark()?"light":"dark";store.set("dm-theme",root.dataset.theme);drawMermaid()};
// mobile drawer
const scrim=document.getElementById("scrim");
const openSide=v=>{side&&side.classList.toggle("open",v);scrim&&scrim.classList.toggle("on",v)};
const menuBtn=document.getElementById("menuBtn");if(menuBtn)menuBtn.onclick=()=>openSide(true);
// mermaid, themed off the site palette; useMaxWidth:false keeps big graphs at natural
// size inside the scrollable shell instead of squashing them to fit
const MTHEME={
  light:{background:"transparent",primaryColor:"#ffffff",primaryBorderColor:"#c3ccd8",primaryTextColor:"#1d2129",
    lineColor:"#8b93a0",secondaryColor:"#eff1f5",tertiaryColor:"#f7f8fa",fontSize:"13px"},
  dark:{background:"transparent",primaryColor:"#1a1d24",primaryBorderColor:"#3a4250",primaryTextColor:"#d8dde5",
    lineColor:"#5f6875",secondaryColor:"#14171d",tertiaryColor:"#121419",fontSize:"13px"}};
function drawMermaid(){
  if(!window.mermaid)return;
  const nodes=document.querySelectorAll("pre.mermaid");
  nodes.forEach(n=>{if(!n.dataset.src)n.dataset.src=n.textContent;n.removeAttribute("data-processed");n.innerHTML=n.dataset.src});
  try{mermaid.initialize({startOnLoad:false,securityLevel:"loose",theme:"base",
    themeVariables:MTHEME[isDark()?"dark":"light"],flowchart:{useMaxWidth:false},
    fontFamily:'ui-monospace,SFMono-Regular,Menlo,monospace'});
    mermaid.run({querySelector:"pre.mermaid"}).then(()=>{
      document.querySelectorAll(".graph-shell").forEach(s=>{
        s.scrollLeft=(s.scrollWidth-s.clientWidth)/2;s.scrollTop=(s.scrollHeight-s.clientHeight)/2});
    })}catch(e){}
}
if(window.mermaid)drawMermaid();else addEventListener("load",drawMermaid);
// copy buttons: each code block gets a wrapper so the button stays put while the
// code scrolls under it
document.querySelectorAll(".doc pre:not(.mermaid)").forEach(p=>{
  const w=el("div","prewrap");p.parentNode.insertBefore(w,p);w.appendChild(p);
  const b=el("button","copy","Copy");
  b.onclick=()=>{const c=p.querySelector("code");
    navigator.clipboard&&navigator.clipboard.writeText((c||p).innerText.trim()).then(()=>{
      b.textContent="Copied";b.classList.add("done");
      setTimeout(()=>{b.textContent="Copy";b.classList.remove("done")},1600)});};
  w.appendChild(b);
});
// on-this-page rail: section heads + documented symbols, scrollspy via observer
const rail=document.getElementById("toc");
if(rail){
  const heads=[...document.querySelectorAll(".doc h2, .doc .api-entry[id]")]
    .filter(n=>!n.closest(".arch-sec"));
  const seen=new Set();
  const entries=heads.map(n=>{
    const sym=n.classList.contains("api-entry");
    const label=sym?n.id:n.textContent.trim();
    if(!n.id){let s=label.toLowerCase().replace(/[^a-z0-9]+/g,"-").replace(/^-+|-+$/g,"")||"s";
      while(seen.has(s))s+="-";n.id=s}
    seen.add(n.id);
    return {n,label,sym};
  });
  if(entries.length>=2){
    const inWrap=el("div","toc-in");
    inWrap.appendChild(el("p","toc-cap","On this page"));
    const nav=el("nav");
    const links=entries.map(({n,label,sym})=>{
      const a=el("a","toc-link"+(sym?" mono":""),label);a.href="#"+n.id;a.title=label;
      nav.appendChild(a);return a;
    });
    inWrap.appendChild(nav);rail.appendChild(inWrap);
    const mark=id=>links.forEach((a,i)=>a.classList.toggle("on",entries[i].n.id===id));
    if("IntersectionObserver" in window){
      let cur=entries[0].n.id;
      const io=new IntersectionObserver(es=>{
        for(const e of es)if(e.isIntersecting)cur=e.target.id;
        mark(cur);
      },{rootMargin:"-15% 0px -70% 0px"});
      entries.forEach(({n})=>io.observe(n));
    }
  }else rail.remove();
}
// search palette
const pal=document.getElementById("palette"),inp=document.getElementById("palInput"),box=document.getElementById("results");
let sel=0,shown=[];
const openPal=()=>{scrim&&scrim.classList.add("on");pal.classList.add("on");inp.value="";list("");inp.focus()};
const closePal=()=>{scrim&&scrim.classList.remove("on");pal.classList.remove("on")};
function list(q){
  q=q.trim().toLowerCase();
  shown=(q?NAV.search.filter(r=>(r[1]+" "+r[2]).toLowerCase().includes(q)):NAV.search).slice(0,50);
  sel=0;
  box.innerHTML=shown.length?shown.map((r,i)=>
    `<a class="res${i===0?" sel":""}" href="${r[3]}" data-i="${i}"><span class="kind">${r[0]}</span><span class="nm">${r[1]}</span><span class="ctx">${r[2]}</span></a>`
  ).join(""):`<div class="res-empty">No matches</div>`;
}
function move(d){if(!shown.length)return;sel=(sel+d+shown.length)%shown.length;
  [...box.querySelectorAll(".res")].forEach((e,i)=>e.classList.toggle("sel",i===sel));
  box.querySelector(".res.sel")?.scrollIntoView({block:"nearest"})}
if(inp){inp.oninput=e=>list(e.target.value);}
document.querySelectorAll(".js-search").forEach(b=>b.onclick=openPal);
if(scrim)scrim.onclick=()=>{closePal();openSide(false)};
addEventListener("keydown",e=>{
  if((e.metaKey||e.ctrlKey)&&e.key==="k"){e.preventDefault();openPal()}
  else if(e.key==="/"&&!/INPUT|TEXTAREA/.test(document.activeElement.tagName)){e.preventDefault();openPal()}
  else if(pal&&pal.classList.contains("on")){
    if(e.key==="Escape")closePal();
    else if(e.key==="ArrowDown"){e.preventDefault();move(1)}
    else if(e.key==="ArrowUp"){e.preventDefault();move(-1)}
    else if(e.key==="Enter"){const a=box.querySelector(".res.sel");if(a)location.href=a.getAttribute("href")}
  }
});
"""


def _inline(text: str) -> str:
    """Escape a prose line for HTML, keeping `backtick` spans as <code>."""
    return _CODE_RE.sub(r"<code>\1</code>", html.escape(text))


_DOXY_TAG = re.compile(r"^[@\\](brief|param|return|returns|retval)\b\s*(.*)$")


def _doxygen(text: str) -> str | None:
    """A Doxygen-marked doc (`@brief`/`@param`/`@return` lines, what --rewrite
    emits for C) as structured HTML — the brief as the lead paragraph, the
    contract as a definition list — or None when the text carries no markers
    (the plain-prose path renders it). Continuation lines fold into the tag
    above them; unmarked lines stay ordinary paragraphs."""
    lines = text.strip().splitlines()
    if not any(_DOXY_TAG.match(ln.strip()) for ln in lines):
        return None
    brief: list[str] = []
    params: list[list[str]] = []
    ret: list[str] = []
    rest: list[str] = []
    cur: list[str] | None = None
    for raw in lines:
        s = raw.strip()
        m = _DOXY_TAG.match(s)
        if m:
            tag, val = m.group(1), m.group(2)
            if tag == "brief":
                brief.append(val)
                cur = brief
            elif tag == "param":
                val = re.sub(r"^\[[^\]]*\]\s*", "", val)  # @param[in] direction
                bits = val.split(None, 1)
                params.append([bits[0] if bits else "", bits[1] if len(bits) > 1 else ""])
                cur = params[-1]
            else:  # return / returns / retval
                ret.append(val)
                cur = ret
        elif s and cur is not None:
            if params and cur is params[-1]:
                cur[1] += " " + s  # a param description wrapping to the next line
            else:
                cur.append(s)
        elif s:
            rest.append(s)
        else:
            cur = None
    parts: list[str] = []
    if brief:
        parts.append(f"<p>{_inline(' '.join(brief))}</p>")
    if rest:
        parts.append(f"<p>{_inline(' '.join(rest))}</p>")
    if params or ret:
        rows = "".join(
            f"<dt><code>{html.escape(n)}</code></dt><dd>{_inline(d)}</dd>"
            for n, d in params
        )
        if ret:
            rows += f"<dt>returns</dt><dd>{_inline(' '.join(ret))}</dd>"
        parts.append(f'<dl class="params">{rows}</dl>')
    return "\n".join(parts)


def _prose(text: str) -> str:
    """Docstring text -> HTML blocks: a Doxygen-marked doc renders structured
    (`_doxygen`); otherwise blank-line-separated paragraphs, with chunks whose
    every line is indented (the docstring convention for tables/diagrams/examples, and
    what survives `ast.get_docstring`'s dedent) kept verbatim in a <pre>."""
    structured = _doxygen(text)
    if structured is not None:
        return structured
    blocks = []
    for chunk in re.split(r"\n\s*\n", text.strip()):
        lines = chunk.splitlines()
        if all(ln.startswith((" ", "\t")) for ln in lines if ln.strip()):
            blocks.append(f"<pre>{html.escape(chunk)}</pre>")
        else:
            blocks.append(f"<p>{_inline(chunk)}</p>")
    return "\n".join(blocks)


@dataclass
class Guide:
    """One authored page picked up for the site: its nav identity + markdown source."""

    slug: str  # "guides.how-it-works"
    title: str  # first `# ` heading, else the rel path
    text: str  # raw markdown, anchors and all
    rel: str = ""  # path under docs_dir ("guides/how-it-works.md") — link resolution


def _md_inline(text: str) -> str:
    """`_inline` plus the guide-markdown spans: **bold** and [link](url)."""
    s = _BOLD_RE.sub(r"<strong>\1</strong>", _inline(text))
    return _LINK_RE.sub(r'<a href="\2">\1</a>', s)


def _markdown(text: str) -> str:
    """Authored-guide markdown -> HTML: the subset guides actually use — headings,
    paragraphs, fenced code (```mermaid fences become live diagrams), flat lists,
    inline code/bold/links. Anchor comments (and any other HTML comment) vanish:
    they're for `check`, not for readers. Not a full markdown engine on purpose; the
    committed .md stays the canonical rendering."""
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    lines = text.splitlines()
    out: list[str] = []
    para: list[str] = []

    def flush() -> None:
        """Close the open paragraph, if any."""
        if para:
            out.append(f"<p>{_md_inline(' '.join(para))}</p>")
            para.clear()

    i = 0
    while i < len(lines):
        s = lines[i].strip()
        if s.startswith("```"):
            flush()
            lang = s[3:].strip().lower()
            code = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            body = chr(10).join(code)
            if lang == "mermaid":
                out.append(f'<pre class="mermaid">{html.escape(body)}</pre>')
            else:
                out.append(f"<pre><code>{html.escape(body)}</code></pre>")
        elif s.startswith("#"):
            flush()
            level = min(len(s) - len(s.lstrip("#")), 4)
            out.append(f"<h{level}>{_md_inline(s.lstrip('#').strip())}</h{level}>")
        elif s.startswith(("- ", "* ")):
            flush()
            items = []
            while i < len(lines) and lines[i].strip().startswith(("- ", "* ")):
                items.append(f"<li>{_md_inline(lines[i].strip()[2:])}</li>")
                i += 1
            i -= 1
            out.append("<ul>" + "".join(items) + "</ul>")
        elif s.startswith("|"):
            flush()
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = lines[i].strip().strip("|").split("|")
                rows.append([c.strip() for c in cells])
                i += 1
            i -= 1
            # a |---|---| second row marks the first as the header
            head: list[str] | None = None
            if (
                len(rows) > 1
                and any(rows[1])
                and all(re.fullmatch(r":?-+:?", c) for c in rows[1] if c)
            ):
                head, rows = rows[0], rows[2:]
            tbl = ["<table>"]
            if head:
                tbl.append(
                    "<thead><tr>"
                    + "".join(f"<th>{_md_inline(c)}</th>" for c in head)
                    + "</tr></thead>"
                )
            tbl.append("<tbody>")
            for r in rows:
                tbl.append(
                    "<tr>" + "".join(f"<td>{_md_inline(c)}</td>" for c in r) + "</tr>"
                )
            tbl.append("</tbody></table>")
            out.append("".join(tbl))
        elif not s:
            flush()
        else:
            para.append(s)
        i += 1
    flush()
    return "\n".join(out)


def _guides(ctx: Context) -> list[Guide]:
    """Every authored page under docs_dir — any *.md without the generated stamp, the
    same rule the anchor scanner uses — so the site carries the hand-written why
    alongside the generated what."""
    found: list[Guide] = []
    ddir = ctx.config.docs_dir
    for md in sorted(ddir.rglob("*.md")):
        try:
            text = md.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if text.startswith(STAMPS):
            continue
        rel = md.relative_to(ddir).as_posix()
        title = next(
            (ln[2:].strip() for ln in text.splitlines() if ln.startswith("# ")), rel
        )
        found.append(Guide(rel.replace("/", ".").removesuffix(".md"), title, text, rel))
    return found


def _remote_base(ctx: Context) -> str | None:
    """The https base of the `origin` remote (git@/ssh/https forms normalized) —
    where guide links that point outside the docs tree land, as blob URLs.
    None without a usable remote; the caller then treats such links as dead."""
    try:
        url = subprocess.run(
            ["git", "-C", str(ctx.root), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.match(r"(?:ssh://)?git@([^:/]+)[:/](.+?)(?:\.git)?/?$", url)
    if m:
        return f"https://{m.group(1)}/{m.group(2)}"
    m = re.match(r"(https?://.+?)(?:\.git)?/?$", url)
    return m.group(1) if m else None


def _page_hrefs(model: Model, guides: list[Guide]) -> dict[str, str]:
    """{docs-relative .md path: site .html file} for everything the site renders —
    guides, the two headline pages, and each subsystem page under both committed
    layouts (flat and grouped), so a guide's link works whichever one is on disk."""
    hrefs = {"README.md": "index.html", "ARCHITECTURE.md": "architecture.html"}
    for p in model.pages:
        hrefs[f"architecture/{p.slug}.md"] = f"{p.slug}.html"
        d = _dir(p.rel)
        hrefs[f"architecture/{_slug(d)}/{_tail(d, p)}.md"] = f"{p.slug}.html"
        hrefs.setdefault(f"architecture/{_slug(d)}/README.md", "index.html")
    for g in guides:
        hrefs[g.rel] = f"{g.slug}.html"
    return hrefs


_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")


def _resolve_links(ctx: Context, model: Model, guides: list[Guide]) -> list[str]:
    """Rewrite every relative link in the authored guides to its real site target
    — a sibling .md becomes its .html page, a repo file becomes the remote's blob
    URL at default_base — and return a line per link that resolves to nothing
    (the caller fails the build: a shipped dead link is doc rot, the exact thing
    the tool exists to stop). Scheme-carrying links (http, mailto) and bare
    #anchors pass through; fenced code blocks are left untouched."""
    hrefs = _page_hrefs(model, guides)
    remote = _remote_base(ctx)
    branch = ctx.config.default_base
    dead: list[str] = []
    for g in guides:
        at = posixpath.dirname(g.rel)

        def swap(m: re.Match, _g: Guide = g, _at: str = at) -> str:
            """One matched markdown link, rewritten to its site target — or kept
            as written, recording it dead when nothing resolves."""
            label, target = m.group(1), m.group(2)
            base, _, frag = target.partition("#")
            if _SCHEME.match(target) or not base:
                return m.group(0)
            tail = f"#{frag}" if frag else ""
            nd = posixpath.normpath(posixpath.join(_at, base))
            if nd in hrefs:
                return f"[{label}]({hrefs[nd]}{tail})"
            rp = posixpath.normpath(posixpath.join(model.docs_rel, _at, base))
            if not rp.startswith("..") and (ctx.root / rp).exists():
                if remote:
                    return f"[{label}]({remote}/blob/{branch}/{rp})"
                dead.append(f"{_g.rel}: ({target}) — repo file, but no git remote to link it")
                return m.group(0)
            dead.append(f"{_g.rel}: ({target}) — no site page or repo file there")
            return m.group(0)

        out, fenced = [], False
        for ln in g.text.splitlines():
            if ln.strip().startswith("```"):
                fenced = not fenced
                out.append(ln)
            else:
                out.append(ln if fenced else _LINK_RE.sub(swap, ln))
        g.text = "\n".join(out)
    return dead


def _mermaid(kind: str, edges) -> str:
    """A client-rendered flowchart: the mermaid text itself is the offline fallback."""
    rows = "\n".join(html.escape(ln) for ln in _mermaid_lines(list(edges)))
    return (
        '<div class="graph-shell">'
        f'<pre class="mermaid">flowchart {kind}\n{rows}</pre></div>'
    )


def _nav_labels(pages: list[Page]) -> dict[str, str]:
    """{slug: sidebar label} — the file's basename; the directory groups it in the tree."""
    return {p.slug: os.path.basename(p.rel) for p in pages}


def _groups(pages: list[Page]) -> list[tuple[str, list[list[str]]]]:
    """Pages bucketed by directory, order preserved: [(dir, [[slug, filename], …]), …].
    The tree renders one collapsible group per directory."""
    labels = _nav_labels(pages)
    buckets: dict[str, list[list[str]]] = {}
    for p in pages:
        d = os.path.dirname(p.rel) or "/"
        buckets.setdefault(d, []).append([p.slug, labels[p.slug]])
    return list(buckets.items())


def _search_index(model: Model, guides: list[Guide]) -> list[list[str]]:
    """Everything the palette can jump to: [kind, name, context, href]. Modules and
    the two headline pages, plus every documented symbol at its `page.html#name`."""
    idx: list[list[str]] = [
        ["page", "Overview", "", "index.html"],
        ["page", "Architecture", "", "architecture.html"],
    ]
    for g in guides:
        idx.append(["guide", g.title, "", f"{g.slug}.html"])
    for p in model.pages:
        idx.append(["module", p.rel, "", f"{p.slug}.html"])
        base = os.path.basename(p.rel)
        for s in p.symbols:
            if s.doc:
                idx.append([s.kind.lower(), s.name, base, f"{p.slug}.html#{s.name}"])
    return idx


def _nav_js(model: Model, guides: list[Guide]) -> str:
    """The shared client: sidebar data (doc links + directory groups) + the search
    index, injected into the app template. One copy for the whole site."""
    docs = [["index", "Overview", "home"], ["architecture", "Architecture", "arch"]]
    docs += [[g.slug, g.title, "book"] for g in guides]
    data = json.dumps(
        {
            "docs": docs,
            "groups": _groups(model.pages),
            "search": _search_index(model, guides),
        }
    )
    return _APP_JS.replace("__DATA__", data)


def _crumb(*parts: str) -> str:
    """A breadcrumb: last part bold, joined by faint slashes."""
    if not parts:
        return ""
    lead = "".join(
        f'<span>{html.escape(p)}</span><span class="sep">/</span>' for p in parts[:-1]
    )
    return f'<div class="crumb">{lead}<b>{html.escape(parts[-1])}</b></div>'


def _layout(
    model: Model, title: str, active: str, crumb: str, body: str, body_class: str = ""
) -> str:
    """Wrap a page body in the shared shell: sidebar (brand + coverage, nav filled by
    nav.js), sticky top bar (breadcrumb + search + theme), the reading column, and the
    search palette. `active` (the page's slug) marks its nav link; everything else is
    one shared nav.js and style.css, so a page's size never grows with the page count."""
    cov = model.coverage
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<link rel="stylesheet" href="style.css">
</head>
<body>
<div class="app">
<aside class="side" data-active="{html.escape(active)}">
<div class="side-head">
<a class="brand" href="index.html"><b>{html.escape(model.root_name)}</b><span class="tag">docs</span></a>
<div class="cov"><div class="cov-row"><span>coverage</span><b>{cov["documented"]}/{cov["total"]} · {cov["percent"]}%</b></div>
<div class="cov-bar"><span style="width:{cov["percent"]}%"></span></div></div>
</div>
<button class="search-btn js-search"><svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="7"/><path d="m20 20-3-3"/></svg>Search docs<kbd>&#8984;K</kbd></button>
<nav class="tree" id="tree"></nav>
</aside>
<div class="content">
<header class="topbar">
<button class="icon-btn menu-btn" id="menuBtn" aria-label="Menu"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 6h18M3 12h18M3 18h18"/></svg></button>
{crumb}
<div class="spacer"></div>
<button class="icon-btn js-search" aria-label="Search"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="7"/><path d="m20 20-3-3"/></svg></button>
<button class="icon-btn" id="themeBtn" aria-label="Toggle theme"><svg class="moon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg><svg class="sun" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="4.5"/><path d="M12 2v2M12 20v2M4 12H2m20 0h-2M5 5 3.5 3.5M20.5 20.5 19 19M19 5l1.5-1.5M3.5 20.5 5 19"/></svg></button>
</header>
<div class="mid">
<main class="doc {body_class}">
{body}
</main>
<aside class="toc-rail" id="toc" aria-label="On this page"></aside>
</div>
</div>
</div>
<div class="scrim" id="scrim"></div>
<div class="palette" id="palette" role="dialog" aria-label="Search">
<input id="palInput" placeholder="Jump to a module or symbol…" autocomplete="off" spellcheck="false">
<div class="results" id="results"></div>
<div class="pal-foot"><span><kbd>&#8593;&#8595;</kbd>navigate</span><span><kbd>&#8629;</kbd>open</span><span><kbd>esc</kbd>close</span></div>
</div>
{_HEAD_SCRIPTS}
</body>
</html>
"""


def _links(mods) -> str:
    """Chip links to sibling subsystem pages, monospace like everywhere else."""
    return ", ".join(
        f'<a href="{_slug(m)}.html"><code>{html.escape(m)}</code></a>' for m in mods
    )


# A guide whose filename reads like one of these headlines the landing page: its prose
# (install, first run) is inlined so the front page shows how to start, not just a map.
_INTRO_SLUGS = ("start", "install", "quick", "setup", "getting", "intro")


def _featured(guides) -> Guide | None:
    """The guide to headline on the landing page — the first getting-started/install
    page. None -> the landing page stays a plain docs index (nothing to inline)."""
    for g in guides:
        base = g.slug.rsplit(".", 1)[-1].lower()
        if base.startswith(_INTRO_SLUGS):
            return g
    return None


def _split_intro(text: str) -> tuple[str, str]:
    """A featured guide's markdown -> (lede, rest): drop the leading `# ` title, take the
    first paragraph as the hero lede, hand back the remainder for the Getting started
    section — so the opening line isn't printed twice."""
    lines = text.splitlines()
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i < len(lines) and lines[i].lstrip().startswith("#"):
        i += 1
    while i < len(lines) and not lines[i].strip():
        i += 1
    lede = []
    while i < len(lines) and lines[i].strip():
        lede.append(lines[i].strip())
        i += 1
    return " ".join(lede), "\n".join(lines[i:])


def _overview(model: Model, guides=()) -> str:
    """index.html — the landing page: a hero (name, lede, stat badges, calls to action),
    the Getting started guide inlined when the repo ships one, then the subsystem map and
    any remaining guides. Still the same model the markdown overview is built from."""
    cov = model.coverage
    featured = _featured(guides)
    others = [g for g in guides if g is not featured]
    lede, rest = _split_intro(featured.text) if featured else ("", "")
    if not lede:
        lede = f"Generated documentation for {model.root_name}."

    stats = "".join(
        f'<div class="stat-b"><b>{v}</b><span>{label}</span></div>'
        for v, label in (
            (len(model.pages), "subsystems"),
            (f"{cov['percent']}%", "documented"),
            (cov["documented"], "symbols"),
        )
    )
    href, cta = (
        (f"{featured.slug}.html", "Get started")
        if featured
        else ("#subsystems", "Browse subsystems")
    )
    parts = [
        '<section class="hero">',
        '<div class="eyebrow">documentation</div>',
        f"<h1>{html.escape(model.root_name)}</h1>",
        f'<p class="lede">{_inline(lede)}</p>',
        f'<div class="stats">{stats}</div>',
        f'<div class="cta"><a class="btn btn-primary" href="{href}">{cta}</a>'
        f'<a class="btn" href="architecture.html">Architecture</a></div>',
        "</section>",
    ]
    if featured and rest.strip():
        parts.append(
            '<div class="section-h"><h2>Getting started</h2><span class="rule"></span></div>'
        )
        parts.append(_markdown(rest))

    parts.append(
        '<div class="section-h" id="subsystems"><h2>Subsystems</h2><span class="rule"></span></div>'
    )
    if model.module_edges and len(model.module_edges) <= _MAP_CAP:
        from pathlib import Path

        stems = [
            (Path(a).stem, Path(b).stem) for a, b in model.module_edges[:_EDGE_CAP]
        ]
        parts.append(_mermaid("LR", stems))
    cards = "\n".join(
        f'<li><a href="{p.slug}.html"><span class="name">{html.escape(p.rel)}</span>'
        f'<span class="about">{_inline(p.summary)}</span></a></li>'
        for p in model.pages
    )
    parts.append(f'<ul class="cards">\n{cards}\n</ul>')

    if others:
        parts.append(
            '<div class="section-h"><h2>Guides</h2><span class="rule"></span></div>'
        )
        gcards = "\n".join(
            f'<li><a href="{g.slug}.html"><span class="name">{html.escape(g.title)}</span></a></li>'
            for g in others
        )
        parts.append(f'<ul class="cards">\n{gcards}\n</ul>')
    return _layout(
        model, model.root_name, "index", _crumb("Overview"), "\n".join(parts)
    )


def _architecture(model: Model) -> str:
    """architecture.html — docs/ARCHITECTURE.md as a site page: every subsystem in
    reading order (entry points first), each linking into its per-module page."""
    order, _ = _tour(model.pages, model.module_edges)
    rank = {r: i for i, r in enumerate(order)}
    pages = sorted(model.pages, key=lambda p: rank.get(p.rel, len(rank)))
    parts = [
        f'<div class="eyebrow">{html.escape(model.root_name)}</div>',
        "<h1>Architecture</h1>",
        "<p>Every subsystem on one page, in reading order: entry points (nothing "
        "imports them) first, then the machinery they drive. Each heading links to the "
        "full per-module reference.</p>",
    ]
    if model.module_edges:
        from pathlib import Path

        stems = [
            (Path(a).stem, Path(b).stem) for a, b in model.module_edges[:_EDGE_CAP]
        ]
        parts.append(_mermaid("LR", stems))
    parts.append('<div class="arch">')
    for p in pages:
        sec = [
            f'<section class="arch-sec"><h2><a href="{p.slug}.html">'
            f"<code>{html.escape(p.rel)}</code></a></h2>"
        ]
        if p.module_doc:
            sec.append(_prose(p.module_doc))
        elif p.origin:
            sec.append(
                f"<p><em>No module docstring. First commit: "
                f'"{html.escape(p.origin)}".</em></p>'
            )
        facts = []
        if p.exposes:
            facts.append(
                "exposes "
                + ", ".join(f"<code>{html.escape(s)}</code>" for s in p.exposes)
            )
        if p.depends_on:
            facts.append(f"depends on {_links(p.depends_on)}")
        if p.used_by:
            facts.append(f"used by {_links(p.used_by)}")
        if facts:
            sec.append(f'<p class="chips">{" &nbsp;·&nbsp; ".join(facts)}</p>')
        sec.append("</section>")
        parts.append("\n".join(sec))
    parts.append("</div>")
    crumb = _crumb(model.root_name, "architecture")
    return _layout(
        model,
        f"{model.root_name} — architecture",
        "architecture",
        crumb,
        "\n".join(parts),
    )


def _guide(model: Model, g: Guide) -> str:
    """One authored page, converted from its markdown, in the same shell as the rest."""
    return _layout(
        model, g.title, g.slug, _crumb(g.title), _markdown(g.text), "reading"
    )


def _page(model: Model, p: Page) -> str:
    """One subsystem page: module prose, edge chips, flow diagram, per-symbol API."""
    where = os.path.dirname(p.rel)
    eyebrow = (
        f"module &middot; <code>{html.escape(where)}/</code>" if where else "module"
    )
    parts = [
        f'<div class="eyebrow">{eyebrow}</div>',
        f'<h1 class="mono">{html.escape(os.path.basename(p.rel))}</h1>',
    ]
    if p.module_doc:
        parts.append(_prose(p.module_doc))

    chips = []
    if p.depends_on:
        chips.append(f"depends on {_links(p.depends_on)}")
    if p.used_by:
        chips.append(f"used by {_links(p.used_by)}")
    if chips:
        parts.append(f'<p class="chips">{" &nbsp;·&nbsp; ".join(chips)}</p>')
    if p.flow:
        parts.append(_mermaid("TD", p.flow))

    documented = [s for s in p.symbols if s.doc]
    if documented:
        parts.append(
            '<div class="section-h"><h2>API</h2><span class="rule"></span></div>'
        )
        parts.append('<div class="api">')
    for s in documented:
        # the symbol's own name emphasized inside its declaration, once, on a word
        # boundary — so `int init(void)` bolds the call name, never a type substring
        sig = html.escape(s.signature or s.name)
        nm = html.escape(s.name)
        sig = re.sub(rf"\b{re.escape(nm)}\b", f"<b>{nm}</b>", sig, count=1)
        entry = [
            f'<section class="api-entry{" method" if s.owner else ""}" id="{html.escape(s.name)}">',
            f"<h3><code>{sig}</code></h3>",
            f'<div class="src">{html.escape(p.rel)}:{s.line}</div>',
            _prose(s.doc),
        ]
        xref = []
        if s.callers:
            xref.append(
                "called by "
                + ", ".join(f"<code>{html.escape(n)}</code>" for n in s.callers)
            )
        if s.callees:
            xref.append(
                "calls "
                + ", ".join(f"<code>{html.escape(n)}</code>" for n in s.callees)
            )
        if xref:
            entry.append(f'<div class="xref">{" &nbsp;·&nbsp; ".join(xref)}</div>')
        entry.append("</section>")
        parts.append("\n".join(entry))
    if documented:
        parts.append("</div>")

    missing = [s for s in p.symbols if not s.doc]
    if missing:
        names = ", ".join(f"<code>{html.escape(s.name)}</code>" for s in missing)
        parts.append(
            f"<details><summary>Undocumented ({len(missing)})</summary>"
            f"<p>{names}</p></details>"
        )
    crumb = _crumb(os.path.dirname(p.rel) or "/", os.path.basename(p.rel))
    return _layout(model, p.rel, p.slug, crumb, "\n".join(parts))


def render(model: Model, guides: list[Guide] = ()) -> dict[str, str]:
    """Model (+ authored guides) -> {filename: html/css/js}. Deterministic, flat:
    index (overview) + architecture + one page per subsystem + one per guide + the
    shared stylesheet and script — the whole site, ready for any static host."""
    out = {
        "index.html": _overview(model, guides),
        "architecture.html": _architecture(model),
        "style.css": _CSS,
        "nav.js": _nav_js(model, guides),
    }
    for p in model.pages:
        out[f"{p.slug}.html"] = _page(model, p)
    for g in guides:
        out[f"{g.slug}.html"] = _guide(model, g)
    return out


def run(ctx: Context) -> int:
    """Write the site under site_dir, pruning orphaned pages of ours (same contract as
    `docs.run`: a renamed source file must not leave its old page behind). Drops a
    `.nojekyll` so GitHub Pages serves the files verbatim."""
    if not ctx.graph.exists:
        ui.fail("site: graph absent — indexing failed?")
        return 1
    guides = _guides(ctx)
    model = build_model(ctx)
    dead = _resolve_links(ctx, model, guides)
    if dead:
        for d in dead:
            ui.detail(f"DEAD  {d}", err=True, style="red")
        ui.fail(
            f"site: {len(dead)} dead link(s) in authored pages — fix the doc "
            "(or add a git remote, for links to repo files)"
        )
        return 1
    want = render(model, guides)
    sdir = ctx.config.site_dir
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / ".nojekyll").write_text("")
    for name, text in sorted(want.items()):
        (sdir / name).write_text(text)
    for stale in sdir.glob("*.html"):
        if stale.name not in want:
            stale.unlink()
    pages = len(want) - 4 - len(guides)  # minus index, architecture, css, js
    extra = f" + {len(guides)} guide(s)" if guides else ""
    ui.ok(
        f"site: {pages} subsystem page(s) + index + architecture{extra}"
        f" -> {ctx.rel(str(sdir))}/index.html"
    )
    return 0

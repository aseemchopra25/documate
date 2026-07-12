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
import re
from dataclasses import dataclass

from . import ui
from .core import GENERATED_STAMP, Context
from .docs import (
    _EDGE_CAP,
    Model,
    Page,
    _mermaid_lines,
    _slug,
    _tour,
    build_model,
)

_CODE_RE = re.compile(r"`([^`]+)`")
_BOLD_RE = re.compile(r"\*\*([^*]+)\*\*")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")

# Mermaid, loaded as a classic (UMD) script so it renders from `file://` too — the ESM
# dynamic import silently failed there and left raw `flowchart` text on the page. Both
# tags are deferred so they never block first paint; offline, the <pre> text stays.
_HEAD_SCRIPTS = (
    '<script defer src="https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js"></script>\n'
    '<script defer src="nav.js"></script>'
)

_CSS = r""":root{
  --ground:#fbfbfc; --surface:#ffffff; --raise:#f5f6f9;
  --ink:#1a1d24; --strong:#0e1116; --muted:#5f6979; --faint:#8a93a3;
  --line:#e7e9ef; --line-soft:#eef0f4;
  --accent:#5b5bd6; --accent-soft:#ececfb; --accent-ink:#4a4ac2;
  --ok:#16a34a;
  --mono:ui-monospace,"SF Mono","JetBrains Mono","Cascadia Code",Menlo,Consolas,monospace;
  --sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  --shadow:0 1px 2px rgba(16,20,32,.04),0 8px 24px -12px rgba(16,20,32,.14);
  --shadow-lg:0 24px 60px -20px rgba(16,20,32,.35);
}
@media (prefers-color-scheme:dark){:root{
  --ground:#0b0e14; --surface:#11151d; --raise:#161b25;
  --ink:#e5e8ee; --strong:#f6f8fb; --muted:#8b95a7; --faint:#5f6a7d;
  --line:#212734; --line-soft:#1a202b;
  --accent:#9b9cf7; --accent-soft:#1c1f3a; --accent-ink:#b9baff; --ok:#47d18a;
  --shadow:0 1px 2px rgba(0,0,0,.4),0 12px 30px -12px rgba(0,0,0,.6);
  --shadow-lg:0 30px 70px -20px rgba(0,0,0,.8);
}}
:root[data-theme="light"]{
  --ground:#fbfbfc; --surface:#ffffff; --raise:#f5f6f9;
  --ink:#1a1d24; --strong:#0e1116; --muted:#5f6979; --faint:#8a93a3;
  --line:#e7e9ef; --line-soft:#eef0f4;
  --accent:#5b5bd6; --accent-soft:#ececfb; --accent-ink:#4a4ac2; --ok:#16a34a;
  --shadow:0 1px 2px rgba(16,20,32,.04),0 8px 24px -12px rgba(16,20,32,.14);
  --shadow-lg:0 24px 60px -20px rgba(16,20,32,.35);
}
:root[data-theme="dark"]{
  --ground:#0b0e14; --surface:#11151d; --raise:#161b25;
  --ink:#e5e8ee; --strong:#f6f8fb; --muted:#8b95a7; --faint:#5f6a7d;
  --line:#212734; --line-soft:#1a202b;
  --accent:#9b9cf7; --accent-soft:#1c1f3a; --accent-ink:#b9baff; --ok:#47d18a;
  --shadow:0 1px 2px rgba(0,0,0,.4),0 12px 30px -12px rgba(0,0,0,.6);
  --shadow-lg:0 30px 70px -20px rgba(0,0,0,.8);
}
*{box-sizing:border-box}
body{margin:0;background:var(--ground);color:var(--ink);font-family:var(--sans);
  font-size:15px;line-height:1.65;-webkit-font-smoothing:antialiased}
a{color:var(--accent-ink);text-decoration:none}
code{font-family:var(--mono)}
::selection{background:var(--accent-soft)}
.app{display:grid;grid-template-columns:17rem minmax(0,1fr);min-height:100vh}

.side{position:sticky;top:0;height:100vh;display:flex;flex-direction:column;background:var(--surface);
  border-right:1px solid var(--line);z-index:40;transition:transform .28s cubic-bezier(.4,0,.2,1)}
.side-head{padding:1.15rem 1.15rem .9rem;border-bottom:1px solid var(--line-soft)}
.brand{display:flex;align-items:baseline;gap:.5rem}
.brand b{font-family:var(--mono);font-weight:600;font-size:1.02rem;letter-spacing:-.02em;color:var(--strong)}
.brand .tag{font-size:.6rem;letter-spacing:.14em;text-transform:uppercase;color:var(--faint);
  border:1px solid var(--line);border-radius:5px;padding:.12rem .3rem}
.cov{margin-top:.85rem}
.cov-row{display:flex;justify-content:space-between;align-items:baseline;font-size:.72rem;color:var(--muted);margin-bottom:.35rem}
.cov-row b{color:var(--ok);font-variant-numeric:tabular-nums;font-family:var(--mono);font-weight:600;font-size:.76rem}
.cov-bar{height:5px;border-radius:99px;background:var(--line);overflow:hidden}
.cov-bar span{display:block;height:100%;border-radius:99px;
  background:linear-gradient(90deg,var(--ok),color-mix(in srgb,var(--ok) 70%,var(--accent)));
  transform-origin:left;animation:grow 1s cubic-bezier(.2,.7,.2,1) both}
@keyframes grow{from{transform:scaleX(0)}}
.search-btn{margin:.85rem 1.15rem;display:flex;align-items:center;gap:.5rem;width:calc(100% - 2.3rem);
  padding:.5rem .65rem;border:1px solid var(--line);border-radius:9px;background:var(--ground);
  color:var(--faint);font-family:var(--sans);font-size:.82rem;cursor:pointer;transition:.15s}
.search-btn:hover{border-color:var(--accent);color:var(--muted)}
.search-btn kbd{margin-left:auto;font-family:var(--mono);font-size:.66rem;color:var(--faint);
  border:1px solid var(--line);border-radius:5px;padding:.05rem .3rem;background:var(--surface)}
.tree{flex:1;overflow-y:auto;padding:.25rem .7rem 1.5rem}
.tree::-webkit-scrollbar{width:9px}
.tree::-webkit-scrollbar-thumb{background:var(--line);border-radius:9px;border:3px solid var(--surface)}
.doclinks{display:flex;flex-direction:column;gap:1px;padding:.15rem 0 .55rem;margin-bottom:.35rem;
  border-bottom:1px solid var(--line-soft)}
.doclink{display:flex;align-items:center;gap:.6rem;padding:.42rem .55rem;border-radius:7px;
  font-size:.86rem;color:var(--ink);font-weight:500;transition:.12s}
.doclink svg{width:1rem;height:1rem;color:var(--muted);flex:none}
.doclink:hover{background:var(--raise)}
.doclink.on{background:var(--accent-soft);color:var(--accent-ink)}
.doclink.on svg{color:var(--accent)}
.group{margin-top:.5rem}
.group-h{display:flex;align-items:center;gap:.4rem;width:100%;background:none;border:0;cursor:pointer;
  padding:.4rem .45rem;color:var(--faint);font-family:var(--sans);font-size:.66rem;font-weight:600;
  letter-spacing:.1em;text-transform:uppercase;text-align:left}
.group-h .chev{transition:transform .2s;font-size:.9rem;line-height:1;color:var(--faint)}
.group.collapsed .chev{transform:rotate(-90deg)}
.group.collapsed .items{display:none}
.items{display:flex;flex-direction:column;gap:1px}
.item{display:block;padding:.32rem .55rem;border-radius:7px;font-family:var(--mono);font-size:.79rem;
  color:var(--muted);overflow-wrap:anywhere;position:relative;transition:.12s}
.item:hover{background:var(--raise);color:var(--ink)}
.item.on{background:var(--accent-soft);color:var(--accent-ink);font-weight:600}
.item.on::before{content:"";position:absolute;left:-.7rem;top:.35rem;bottom:.35rem;width:3px;border-radius:99px;background:var(--accent)}

.content{display:flex;flex-direction:column;min-width:0}
.topbar{position:sticky;top:0;z-index:30;display:flex;align-items:center;gap:.75rem;height:3.5rem;
  padding:0 1.5rem;background:color-mix(in srgb,var(--ground) 82%,transparent);
  backdrop-filter:saturate(1.4) blur(10px);border-bottom:1px solid var(--line-soft)}
.crumb{display:flex;align-items:center;gap:.4rem;font-family:var(--mono);font-size:.8rem;color:var(--muted);min-width:0}
.crumb .sep{color:var(--faint)}
.crumb b{color:var(--ink);font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.spacer{flex:1}
.icon-btn{display:grid;place-items:center;width:2.15rem;height:2.15rem;border:1px solid var(--line);
  border-radius:9px;background:var(--surface);color:var(--muted);cursor:pointer;transition:.15s}
.icon-btn:hover{color:var(--ink);border-color:var(--accent)}
.icon-btn svg{width:1.05rem;height:1.05rem}
.menu-btn{display:none}
.sun{display:none}
:root[data-theme="dark"] .sun{display:block}
:root[data-theme="dark"] .moon{display:none}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]) .sun{display:block}
  :root:not([data-theme="light"]) .moon{display:none}}

.doc{max-width:46rem;margin:0 auto;padding:2.75rem 3.5rem 6rem;line-height:1.72;animation:rise .5s cubic-bezier(.2,.7,.2,1) both}
.doc.reading{max-width:42rem}
.doc.reading p{margin:1.05rem 0}
@keyframes rise{from{opacity:0;transform:translateY(10px)}}
.doc h1{font-size:1.55rem;font-weight:600;letter-spacing:-.01em;color:var(--strong);margin:.1rem 0 1rem;text-wrap:balance;line-height:1.25}
.doc h1.mono{font-family:var(--mono)}
.eyebrow{font-family:var(--mono);font-size:.72rem;color:var(--accent);margin-bottom:.5rem}
.doc h2{font-size:1.1rem;font-weight:600;margin:2.4rem 0 1rem;color:var(--strong)}
.doc h3{font-size:1rem;margin:1.4rem 0 .4rem}
.doc h4{font-size:.92rem;margin:1rem 0 .3rem}
.doc p{margin:.85rem 0}
.doc a{text-decoration:underline;text-decoration-color:color-mix(in srgb,var(--accent) 35%,transparent);text-underline-offset:2px}
.doc a:hover{text-decoration-color:var(--accent)}
.stat{font-family:var(--mono);font-size:.85rem;color:var(--muted)}
p code,li code,.chips code,.xref code,.doc h1 code{background:var(--raise);border:1px solid var(--line-soft);
  padding:.08em .34em;border-radius:5px;font-size:.86em;color:var(--ink)}
.doc h1 code{background:none;border:0;padding:0}
pre{background:var(--raise);border:1px solid var(--line);border-radius:10px;padding:.9rem 1.1rem;
  overflow-x:auto;font-family:var(--mono);font-size:.84rem;line-height:1.55}
.section-h{display:flex;align-items:center;gap:.7rem;margin:2.6rem 0 1.2rem}
.section-h h2{font-size:.82rem;letter-spacing:.12em;text-transform:uppercase;color:var(--muted);margin:0;font-weight:600}
.section-h .rule{flex:1;height:1px;background:var(--line)}
.chips{color:var(--muted);font-size:.88rem}
.chips a{color:var(--accent-ink)}

pre.mermaid{border:1px solid var(--line);border-radius:14px;background:
  radial-gradient(circle at 1px 1px,var(--line-soft) 1px,transparent 0) 0 0/22px 22px,var(--surface);
  padding:1.6rem;margin:1.4rem 0;display:flex;justify-content:center;box-shadow:var(--shadow);color:var(--faint)}

.cards{list-style:none;padding:0;display:grid;gap:.75rem;grid-template-columns:repeat(auto-fill,minmax(15rem,1fr))}
.cards a{display:block;height:100%;border:1px solid var(--line);border-radius:12px;padding:.9rem 1rem;
  background:var(--surface);transition:.16s}
.cards a:hover{border-color:color-mix(in srgb,var(--accent) 40%,var(--line));box-shadow:var(--shadow);transform:translateY(-1px)}
.cards .name{display:block;font-family:var(--mono);font-size:.84rem;color:var(--accent-ink);overflow-wrap:anywhere}
.cards .about{display:block;font-size:.85rem;color:var(--muted);margin-top:.35rem}
.arch{display:flex;flex-direction:column}
.arch-sec{border-top:1px solid var(--line);padding:1.6rem 0 .4rem}
.arch-sec h2{font-family:var(--mono);font-size:1rem;margin:0 0 .5rem}

.hero{padding:.4rem 0 1.4rem}
.hero h1{font-size:2.05rem;line-height:1.15;margin:.35rem 0 .55rem}
.lede{font-size:1.06rem;color:var(--muted);max-width:34rem;margin:.2rem 0 1.35rem;line-height:1.55}
.badges{display:flex;flex-wrap:wrap;gap:.55rem;margin:0 0 1.45rem}
.badge{display:inline-flex;align-items:baseline;gap:.4rem;padding:.42rem .75rem;border:1px solid var(--line);
  border-radius:10px;background:var(--surface);font-size:.76rem;color:var(--muted);box-shadow:var(--shadow)}
.badge b{font-family:var(--mono);font-size:.95rem;color:var(--strong);font-variant-numeric:tabular-nums}
.cta{display:flex;flex-wrap:wrap;gap:.65rem}
.btn{display:inline-flex;align-items:center;text-decoration:none;padding:.55rem 1.1rem;border-radius:10px;
  border:1px solid var(--line);background:var(--surface);color:var(--ink);font-size:.9rem;font-weight:500;transition:.15s}
.btn:hover{border-color:var(--accent);transform:translateY(-1px);box-shadow:var(--shadow);text-decoration:none}
.btn-primary{background:var(--accent);border-color:var(--accent);color:#fff}
.btn-primary:hover{background:var(--accent-ink);border-color:var(--accent-ink)}
:root[data-theme="dark"] .btn-primary{color:#0b0e14}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]) .btn-primary{color:#0b0e14}}

.api{display:flex;flex-direction:column;gap:.9rem;margin-top:.4rem}
.api-entry{position:relative;border:1px solid var(--line);border-radius:12px;background:var(--surface);
  padding:1rem 1.15rem;transition:.18s;overflow:hidden}
.api-entry::before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;background:var(--accent);
  transform:scaleY(0);transform-origin:top;transition:transform .2s}
.api-entry:hover{border-color:color-mix(in srgb,var(--accent) 40%,var(--line));box-shadow:var(--shadow)}
.api-entry:hover::before{transform:scaleY(1)}
.api-entry.method{margin-left:1.5rem}
.api-entry h3{font-family:var(--mono);font-size:.92rem;color:var(--strong);font-weight:600;margin:0;overflow-wrap:anywhere}
.api-entry h3 code{background:none;border:0;padding:0}
.api-entry.method h3{font-size:.88rem}
.src{font-family:var(--mono);font-size:.72rem;color:var(--faint);margin:.15rem 0 .6rem}
.api-entry p{margin:.5rem 0;font-size:.92rem}
.xref{display:flex;flex-wrap:wrap;gap:.4rem;margin-top:.7rem;font-size:.75rem;color:var(--muted)}
.xref code{background:var(--raise);border:1px solid var(--line-soft);border-radius:5px;padding:.06em .3em;color:var(--ink)}
details{margin-top:1.4rem;border:1px dashed var(--line);border-radius:10px;padding:.2rem .9rem;color:var(--muted)}
summary{cursor:pointer;padding:.6rem 0;font-size:.86rem;font-weight:600}
ul{padding-left:1.2rem}

.scrim{position:fixed;inset:0;background:rgba(10,12,18,.5);backdrop-filter:blur(2px);opacity:0;
  pointer-events:none;transition:.2s;z-index:60}
.scrim.on{opacity:1;pointer-events:auto}
.palette{position:fixed;top:14vh;left:50%;transform:translateX(-50%) scale(.97);width:min(37rem,92vw);
  background:var(--surface);border:1px solid var(--line);border-radius:16px;box-shadow:var(--shadow-lg);
  z-index:70;opacity:0;pointer-events:none;transition:.2s;overflow:hidden}
.palette.on{opacity:1;pointer-events:auto;transform:translateX(-50%) scale(1)}
.palette input{width:100%;border:0;border-bottom:1px solid var(--line);background:none;color:var(--ink);
  font-family:var(--sans);font-size:1rem;padding:1.05rem 1.25rem;outline:none}
.palette input::placeholder{color:var(--faint)}
.results{max-height:min(52vh,26rem);overflow-y:auto;padding:.5rem}
.res{display:flex;align-items:center;gap:.7rem;padding:.55rem .75rem;border-radius:9px;cursor:pointer;text-decoration:none}
.res.sel{background:var(--accent-soft)}
.res .kind{font-family:var(--mono);font-size:.58rem;text-transform:uppercase;letter-spacing:.06em;color:var(--faint);
  border:1px solid var(--line);border-radius:5px;padding:.1rem .3rem;flex:none;width:3.6rem;text-align:center}
.res.sel .kind{color:var(--accent-ink);border-color:var(--accent)}
.res .nm{font-family:var(--mono);font-size:.85rem;color:var(--ink);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.res .ctx{margin-left:auto;font-size:.72rem;color:var(--faint);white-space:nowrap}
.res-empty{padding:1.6rem;text-align:center;color:var(--faint);font-size:.88rem}
.pal-foot{display:flex;gap:1rem;padding:.55rem 1rem;border-top:1px solid var(--line-soft);font-size:.68rem;color:var(--faint)}
.pal-foot kbd{font-family:var(--mono);border:1px solid var(--line);border-radius:4px;padding:0 .3rem;margin-right:.25rem}

@media (max-width:900px){
  .app{grid-template-columns:1fr}
  .side{position:fixed;top:0;left:0;width:min(19rem,86vw);transform:translateX(-101%)}
  .side.open{transform:none;box-shadow:var(--shadow-lg)}
  .menu-btn{display:grid}
  .doc{padding:2rem 1.4rem 5rem}
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
if(tree){
  const dl=el("div","doclinks");
  for(const [key,label,icon] of NAV.docs){
    const a=el("a","doclink"+(key===active?" on":""));a.href=key+".html";
    a.innerHTML=ICON[icon]||"";a.appendChild(document.createTextNode(label));dl.appendChild(a);
  }
  tree.appendChild(dl);
  for(const [dir,items] of NAV.groups){
    const g=el("div","group"),h=el("button","group-h");
    h.innerHTML='<span class="chev">&#8964;</span>';h.appendChild(document.createTextNode(dir));
    const box=el("div","items");
    for(const [slug,label] of items){const a=el("a","item"+(slug===active?" on":""),label);a.href=slug+".html";box.appendChild(a)}
    h.onclick=()=>g.classList.toggle("collapsed");
    g.appendChild(h);g.appendChild(box);tree.appendChild(g);
  }
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
// mermaid
function drawMermaid(){
  if(!window.mermaid)return;
  const nodes=document.querySelectorAll("pre.mermaid");
  nodes.forEach(n=>{if(!n.dataset.src)n.dataset.src=n.textContent;n.removeAttribute("data-processed");n.innerHTML=n.dataset.src});
  try{mermaid.initialize({startOnLoad:false,securityLevel:"loose",theme:isDark()?"dark":"neutral",
    fontFamily:'ui-monospace,SFMono-Regular,Menlo,monospace'});mermaid.run({querySelector:"pre.mermaid"})}catch(e){}
}
if(window.mermaid)drawMermaid();else addEventListener("load",drawMermaid);
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


def _prose(text: str) -> str:
    """Docstring text -> HTML blocks: blank-line-separated paragraphs, with chunks whose
    every line is indented (the docstring convention for tables/diagrams/examples, and
    what survives `ast.get_docstring`'s dedent) kept verbatim in a <pre>."""
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
        if text.startswith(GENERATED_STAMP):
            continue
        rel = md.relative_to(ddir).as_posix()
        title = next(
            (ln[2:].strip() for ln in text.splitlines() if ln.startswith("# ")), rel
        )
        found.append(Guide(rel.replace("/", ".").removesuffix(".md"), title, text))
    return found


def _mermaid(kind: str, edges) -> str:
    """A client-rendered flowchart: the mermaid text itself is the offline fallback."""
    rows = "\n".join(html.escape(ln) for ln in _mermaid_lines(list(edges)))
    return f'<pre class="mermaid">flowchart {kind}\n{rows}</pre>'


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
<main class="doc {body_class}">
{body}
</main>
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

    badges = "".join(
        f'<span class="badge"><b>{v}</b>{label}</span>'
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
        f'<div class="badges">{badges}</div>',
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
    if model.module_edges:
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
        '<div class="eyebrow">architecture</div>',
        f"<h1>{html.escape(model.root_name)} — architecture</h1>",
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
    parts = [
        '<div class="eyebrow">module</div>',
        f'<h1 class="mono"><code>{html.escape(p.rel)}</code></h1>',
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
        entry = [
            f'<section class="api-entry{" method" if s.owner else ""}" id="{html.escape(s.name)}">',
            f"<h3><code>{html.escape(s.signature or s.name)}</code></h3>",
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
    want = render(build_model(ctx), guides)
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

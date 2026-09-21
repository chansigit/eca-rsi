"""ecarsi.index — landing pages generated from the artefacts on disk.

    python -m ecarsi.index <root | unit>       (re)write the static pages

Nothing here is told what happened: the state of a run is read back from
manifests, contract files, stats/decision files and progress.log, so the
same function renders a finished release and a run that is halfway through
round 2 (ecarsi.serve re-renders on every request, which is what makes
mid-run monitoring possible). Every step also writes the static pages when
it finishes, so a directory that is only copied around still has them.

    <root>/index.html         header card + units table (a one-unit run shows that unit inline)
    <root>/units/<u>/index.html   at-a-glance numbers, files, rounds, samples, sankey, UMAP, needs-review
"""

from __future__ import annotations

import csv
import hashlib
import html as _h
import json
import os
import re
import sys
from pathlib import Path

from .. import layout as L
from .. import mirror, review

CSS = """
/* warm paper by day, dark by night (prefers-color-scheme); everything below reads these variables */
:root{color-scheme:light;--bg:#f4efe4;--card:#fbf8f1;--ink:#3b3a33;--ink-soft:#4e4b42;--muted:#6f6a5e;--line:#e3dccb;--line-strong:#cbc2ad;
 --accent:#2a7f8f;--accent-ink:#1f6472;--accent-bg:#e2eeee;
 --ok:#3d7a4a;--ok-bg:#e3eedf;--run:#a1651a;--run-bg:#f6e9d3;--bad:#a63d3d;--bad-bg:#f5e0dc;--none:#6f6a5e;--none-bg:#ece6d8;
 /* Two palettes, deliberately separate. --ok/--run/--bad above are a *judgement*: green is good,
    amber is worth a look, red is bad -- confidence, inclusion, how well a round converged. The
    four below are a run's *lifecycle*, where finished is not "good" and running is not "warning".
    Sharing one set made a high-confidence badge and a released dataset the same green. */
 --done:#38618c;--done-bg:#e0e8f2;--live:#3d7a4a;--live-bg:#e3eedf;
 --fail:#a63d3d;--fail-bg:#f5e0dc;--wait:#8a6d1f;--wait-bg:#f4ecd4;
 --row-alt:#f7f3ea;--row-hover:#ece9dd;--tip-bg:#2c2a25;--tip-ink:#f4efe4;--tip-muted:#b9b3a5;--plot:#fdfbf6;
 /* --plot backs the kernels' PNGs, which carry their own white, so it stays light in both
    themes; --canvas is our own UMAP surface and follows the page. Pure white read as a
    lightbox cut into the paper. --umap-dim/--umap-blank recede against --canvas, so they
    cannot be baked light: the JS reads them from here. */
 --canvas:#fdfbf6;--umap-dim:#e3e6ea;--umap-blank:#bbbbbb;
 --t1:.75rem;--t2:.8125rem;--t3:.875rem;--t4:1rem;--t5:1.125rem;--t6:1.25rem;--t7:1.5rem;--t8:1.875rem;
 --s1:8px;--s2:16px;--s3:24px;--s4:32px;--s5:48px;--r:4px;
 --paper-light:rgba(255,250,230,.48);--glint:rgba(255,255,255,.65);--shade:rgba(82,65,36,.055);
 --surface:linear-gradient(145deg,var(--glint),transparent 55%),color-mix(in srgb,var(--card) 94%,transparent);
 --paper-shadow:inset 0 1px 0 var(--glint),0 3px 14px var(--shade);
 --serif:Georgia,"Iowan Old Style","Palatino Linotype",Palatino,serif;
 --sans:system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
 --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,"Liberation Mono",monospace}
@media (prefers-color-scheme:dark){:root{color-scheme:dark;--bg:#1e2124;--card:#272b30;--ink:#d8d4c8;--ink-soft:#c5c0b4;--muted:#a09a8c;--line:#3a3f45;--line-strong:#4d535b;
 --accent:#6fb3c9;--accent-ink:#8fc7d8;--accent-bg:#233740;
 --ok:#7fc28b;--ok-bg:#243a2a;--run:#d9a441;--run-bg:#3d3320;--bad:#e07070;--bad-bg:#432727;--none:#a09a8c;--none-bg:#31363c;
 --done:#7aa8d4;--done-bg:#22303f;--live:#7fc28b;--live-bg:#243a2a;
 --fail:#e07070;--fail-bg:#432727;--wait:#d2b45e;--wait-bg:#38321f;
 --row-alt:#2b3035;--row-hover:#333940;--tip-bg:#e9e4d8;--tip-ink:#1e2124;--tip-muted:#5b564c;--plot:#f2efe6;--canvas:#22262a;--umap-dim:#343a41;--umap-blank:#5d646d;--paper-light:rgba(187,155,102,.035);--glint:rgba(255,246,220,.035);--shade:rgba(0,0,0,.14)}}
*{box-sizing:border-box}
html{font-size:16px}
body{margin:0;background:radial-gradient(ellipse at 12% 0,var(--paper-light),transparent 65%),var(--bg);color:var(--ink);font:var(--t4)/1.5 var(--sans)}
a{color:var(--accent);text-decoration:underline;text-decoration-color:color-mix(in srgb,var(--accent) 40%,transparent);text-underline-offset:2px}
a:hover{text-decoration-color:var(--accent)}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
code,.path{font-family:var(--mono);font-size:.9em}
code{background:var(--none-bg);padding:.1em .35em;border-radius:4px}
.path{display:block;word-break:break-all;background:none;padding:0;color:var(--ink)}
h1,h2,h3{margin:0;line-height:1.25;letter-spacing:-.01em;font-family:var(--serif);font-weight:500}
h1{font-size:var(--t8)}h2{font-size:var(--t7)}h3{font-size:var(--t6)}
p{margin:0 0 var(--s2)}
main.page{max-width:1200px;margin:0 auto;padding:var(--s3) var(--s3) var(--s5)}
.crumb{color:var(--muted);font-size:var(--t3);margin-bottom:var(--s1)}.crumb a{color:var(--muted)}
.muted{color:var(--muted)}.num{font-variant-numeric:tabular-nums}
/* status: one set of colours for pills, dots, table cells and review groups.
   Lifecycle first -- finished is blue because it is an end state, not a grade. */
.released{--st:var(--done);--st-bg:var(--done-bg)}
.running{--st:var(--live);--st-bg:var(--live-bg)}
.failed{--st:var(--fail);--st-bg:var(--fail-bg)}
.queued,.paused{--st:var(--wait);--st-bg:var(--wait-bg)}
/* then judgement, which keeps the older green/amber/red reading */
.include,.band-good{--st:var(--ok);--st-bg:var(--ok-bg)}
.tone-warn,.band-watch{--st:var(--run);--st-bg:var(--run-bg)}
.exclude,.tone-bad,.band-high{--st:var(--bad);--st-bg:var(--bad-bg)}
.neutral,.empty-sample,.tone-none{--st:var(--none);--st-bg:var(--none-bg)}
.tone-info{--st:var(--accent);--st-bg:var(--accent-bg)}
.pill{display:inline-flex;align-items:center;gap:.45em;padding:.1em .7em .1em .6em;border-radius:999px;font-size:var(--t3);font-weight:600;
 line-height:1.6;white-space:nowrap;vertical-align:middle;color:var(--st,var(--none));background:var(--st-bg,var(--none-bg))}
.pill::before,.dot{content:"";display:inline-block;width:.5rem;height:.5rem;border-radius:50%;background:var(--st,var(--none));flex:none}
.st{color:var(--st,var(--none));font-weight:600}
/* convergence sparkline: colour is absolute (see TREND_BANDS), a hollow point is still removing.
   The plot sits in its own tinted frame so a row of them reads as a column of charts, not as ink
   floating in the table; the tint is translucent so the row's own background still shows through. */
.spark{display:block;background:color-mix(in srgb,var(--none-bg) 55%,transparent);
 border:1px solid var(--line);border-radius:var(--r);padding:1px}
.sp-line{fill:none;stroke:var(--line);stroke-width:1.5;stroke-linejoin:round}
.sp-area{pointer-events:none}
/* the reading for the point under the pointer. The chart is 84 px wide on the overview, far too
   small to letter inside, and the browser's own <title> tooltip waits about a second and is lost
   on the slightest movement -- at an 8 px spacing that means it effectively never appears. */
.sp-tip{position:fixed;z-index:60;pointer-events:none;display:none;max-width:28ch;
 padding:.25em .6em;border-radius:var(--r);font:var(--t2) var(--sans);line-height:1.5;
 background:var(--tip-bg);color:var(--tip-ink);box-shadow:0 2px 10px var(--shade)}
.sp-tip.on{display:block}.sp-tip .q{color:var(--tip-muted)}
circle.sp{fill:var(--st,var(--none));stroke:none}
circle.sp.open{fill:var(--card);stroke:var(--st,var(--none));stroke-width:1.6}
/* the pointer target: transparent but hit-testable (fill:none would not be). No cursor change --
   `help` draws a question mark over the number the reader is trying to read. The point under it
   grows instead, so it is obvious which round the tooltip belongs to. */
circle.sp-hit{fill:transparent;stroke:none}
/* the dot is painted over its target, so it must not take the pointer itself: otherwise landing
   exactly on a point hits the one element that carries no reading. */
circle.sp{pointer-events:none;transition:r .08s}
circle.sp-hit:hover+circle.sp{r:4}
.sofar{margin-left:.4em;font-size:.85em;font-weight:400;color:var(--muted)}
/* paper chapters: ink headings and neutral rules, no accent rails */
.hero{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:var(--s3);margin-bottom:var(--s3)}
.hero .title{display:flex;align-items:center;gap:var(--s2);flex-wrap:wrap}
.hero .sub{color:var(--muted);font-size:var(--t3);margin-top:4px}
dl.facts{display:flex;flex-wrap:wrap;gap:var(--s1) var(--s4);margin:var(--s2) 0 0}
dl.facts dt{font-size:var(--t2);color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
dl.facts dd{margin:0;font-size:var(--t5);font-weight:600;font-variant-numeric:tabular-nums}
.next{margin-top:var(--s2);padding:var(--s1) var(--s2);background:var(--accent-bg);color:var(--accent-ink);border-radius:var(--r);font-size:var(--t3)}
.next a{color:inherit;font-weight:600}
/* number cards */
.glance{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:var(--s2);margin-bottom:var(--s3)}
.stat{background:var(--card);border:1px solid var(--line);border-radius:var(--r);padding:var(--s2)}
.stat .v{display:block;font-size:var(--t7);font-weight:700;line-height:1.2;font-variant-numeric:tabular-nums;letter-spacing:-.01em}
.stat .k{display:block;color:var(--muted);font-size:var(--t3);margin-top:2px}
.stat .sub{display:block;color:var(--muted);font-size:var(--t2)}
.stat.tone-bad .v{color:var(--bad)}.stat.tone-warn .v{color:var(--run)}
/* sections */
section.block{scroll-margin-top:72px;background:var(--card);border:1px solid var(--line);border-radius:8px;padding:var(--s3);margin-bottom:var(--s3)}
section.block>h2{display:flex;align-items:baseline;gap:var(--s2);flex-wrap:wrap}
section.block>h2 .count{font-size:var(--t3);font-weight:400;color:var(--muted)}
p.lede{color:var(--muted);font-size:var(--t3);margin:4px 0 var(--s2);max-width:90ch}
p.empty{color:var(--muted);margin:0}
.callout{background:var(--card);border:1px solid var(--line);border-radius:var(--r);padding:var(--s2);margin:0 0 var(--s2);font-size:var(--t3)}
nav.jump{position:sticky;top:0;z-index:5;background:var(--bg);display:flex;gap:var(--s1);flex-wrap:wrap;padding:var(--s1) 0;margin:0 0 var(--s2);border-bottom:1px solid var(--line)}
nav.jump a{padding:.25em .8em;border-radius:999px;font-size:var(--t3);font-weight:600;text-decoration:none;color:var(--ink)}
nav.jump a:hover{background:var(--accent-bg);color:var(--accent-ink)}
/* files */
dl.files{display:grid;grid-template-columns:max-content 1fr;gap:var(--s1) var(--s3);margin:0;font-size:var(--t3)}
dl.files dt{font-weight:600;white-space:nowrap}dl.files dd{margin:0;min-width:0}
/* tables */
.wrap{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:var(--t3)}
th,td{padding:10px 12px;border-bottom:1px solid var(--line);text-align:left;vertical-align:middle}
th{color:var(--muted);font-weight:600;border-bottom:2px solid var(--line-strong);white-space:nowrap}
th.r,td.r,td.num{text-align:right;font-variant-numeric:tabular-nums}
tbody tr:nth-child(even){background:var(--row-alt)}tbody tr:hover{background:var(--row-hover)}
td.reason,td.note{color:var(--ink-soft);min-width:24ch;max-width:60ch}
th button{all:unset;cursor:pointer;color:inherit;font:inherit;padding-right:1.1em;position:relative}
th button::after{content:"\\21C5";position:absolute;right:0;opacity:.4}
th[aria-sort=ascending] button::after{content:"\\2191";opacity:1}th[aria-sort=descending] button::after{content:"\\2193";opacity:1}
.bar{display:inline-block;vertical-align:middle;width:64px;height:8px;background:var(--none-bg);border-radius:4px;margin-left:var(--s1);overflow:hidden}
.bar i{display:block;height:100%;background:var(--bad);opacity:.7}
.badge{display:inline-block;padding:.05em .55em;border-radius:999px;font-size:var(--t2);font-weight:600}
.conf-high{background:var(--ok-bg);color:var(--ok)}.conf-medium{background:var(--none-bg);color:var(--none)}.conf-low{background:var(--run-bg);color:var(--run)}
.act-remove{color:var(--bad);font-weight:600}
.toolbar{display:flex;align-items:center;gap:var(--s2);flex-wrap:wrap;margin-bottom:var(--s2)}
.toolbar input[type=search]{font:inherit;font-size:var(--t3);padding:8px 12px;border:1px solid var(--line-strong);border-radius:var(--r);min-width:18rem;background:var(--card)}
.toolbar label{font-size:var(--t3);color:var(--muted)}
/* exclusion reason popover (CSS-only details) */
td.why-cell{position:relative;white-space:nowrap}details.why{display:inline-block;margin-left:.35rem;vertical-align:middle}
details.why summary{list-style:none;cursor:pointer;display:inline-flex;align-items:center;justify-content:center;width:1.4rem;height:1.4rem;border-radius:999px;background:var(--bad);color:var(--card);font-size:var(--t2);font-weight:700;line-height:1}
details.why summary::-webkit-details-marker{display:none}
.why-body{position:absolute;left:0;top:100%;z-index:15;margin-top:4px;width:min(60ch,70vw);white-space:normal;background:var(--card);border:1px solid var(--line);border-radius:var(--r);box-shadow:0 4px 14px rgba(0,0,0,.12);padding:var(--s1) var(--s2);font-size:var(--t3);text-align:left}
/* figures */
figure{margin:0}figure img{max-width:100%;border:1px solid var(--line);border-radius:var(--r);background:var(--plot)}
figcaption{color:var(--muted);font-size:var(--t3);margin-top:var(--s1)}
/* sankey */
.sk-tip{position:absolute;z-index:20;background:var(--tip-bg);color:var(--tip-ink);font-size:var(--t2);line-height:1.4;padding:8px 10px;border-radius:6px;pointer-events:none;max-width:34ch;box-shadow:0 2px 8px rgba(0,0,0,.25)}
.sk-tip .m{color:var(--tip-muted)}
svg.sk{display:block;max-width:100%}svg.sk .sk-stage{font-size:13px;font-weight:600;fill:var(--ink)}
svg.sk .sk-label{font-size:12px;fill:var(--ink)}svg.sk .sk-label.rm{fill:var(--bad)}
svg.sk .sk-label.mid{font-weight:600;paint-order:stroke;stroke:var(--card);stroke-width:3px;stroke-linejoin:round}
svg.sk .sk-flow{opacity:.45;transition:opacity .12s}svg.sk .sk-node{stroke:var(--card);stroke-width:.5;cursor:pointer}
svg.sk.dim .sk-flow{opacity:.07}svg.sk.dim .sk-flow.hi{opacity:.85}
/* umap */
.umap-row{display:flex;gap:var(--s3);align-items:flex-start;flex-wrap:wrap}
.umap-panel{flex:1 1 520px;min-width:0}.umap-panel h3{margin:0 0 var(--s1);display:flex;align-items:baseline;gap:var(--s1)}
.umap-panel h3 .count{font-size:var(--t3);font-weight:400;color:var(--muted)}
.umap-panel canvas{display:block;border:1px solid var(--line);border-radius:var(--r);cursor:crosshair;background:var(--canvas);max-width:100%}
.umap-legend{margin-top:var(--s1);max-height:280px;overflow-y:auto;font-size:var(--t3);border:1px solid var(--line);border-radius:var(--r);padding:4px;columns:2;column-gap:4px}
.umap-leg{display:flex;align-items:center;gap:8px;padding:3px 8px;cursor:pointer;border-radius:4px;break-inside:avoid}
.umap-leg:hover{background:var(--none-bg)}.umap-leg.on{background:var(--accent-bg);font-weight:600}.umap-leg.off{opacity:.45}
.umap-leg i{display:inline-block;width:12px;height:12px;border-radius:3px;flex:none}.umap-leg .lab{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.umap-leg .n{color:var(--muted);font-variant-numeric:tabular-nums}.umap-status{color:var(--muted);font-size:var(--t3);margin:0 0 var(--s2)}
/* needs review */
.rv-group{border:1px solid var(--line);border-radius:var(--r);padding:var(--s2);margin-top:var(--s2)}
.rv-group h3{display:flex;align-items:baseline;gap:var(--s1);flex-wrap:wrap}
.rv-group h3 .count{font-size:var(--t3);font-weight:600;color:var(--st,var(--none));background:var(--st-bg,var(--none-bg));border-radius:999px;padding:0 .6em}
.rv-group h3 .cells{font-size:var(--t3);font-weight:400;color:var(--muted)}
.rv-group p.desc{color:var(--muted);font-size:var(--t3);margin:4px 0 var(--s1);max-width:90ch}
.rv-group table{font-size:var(--t3)}table.review td.c-cells{text-align:right;font-variant-numeric:tabular-nums}table.review td.c-label{max-width:34ch}
footer{color:var(--muted);font-size:var(--t2);margin-top:var(--s4);border-top:1px solid var(--line);padding-top:var(--s2)}
ul.warn{margin:4px 0 0 1.2rem;padding:0}
/* A little reflected light on paper; blur only on surfaces that float over content. */
.hero,.stat,section.block{background:var(--surface);box-shadow:var(--paper-shadow)}
@supports ((backdrop-filter:blur(1px)) or (-webkit-backdrop-filter:blur(1px))){
 nav.jump,.why-body{background:color-mix(in srgb,var(--card) 88%,transparent);-webkit-backdrop-filter:blur(12px);backdrop-filter:blur(12px);box-shadow:var(--paper-shadow)}
}
@media (prefers-reduced-transparency:reduce){nav.jump,.why-body{background:var(--card);-webkit-backdrop-filter:none;backdrop-filter:none}.hero,.stat,section.block{background:var(--card)}}
@media (max-width:700px){main.page{padding:var(--s2)}section.block,.hero{padding:var(--s2)}dl.files{grid-template-columns:1fr}}
"""


SANKEY_JS = r"""
(function(){
const D = SANKEY_DATA, el = document.getElementById("sankey-vis");
if (!D || !el) return;
const PAL = ["#4e79a7","#f28e2b","#59a14f","#e15759","#76b7b2","#edc948","#b07aa1","#ff9da7","#9c755f","#bab0ac",
             "#1f77b4","#aec7e8","#2ca02c","#98df8a","#9467bd","#c5b0d5","#8c564b","#c49c94","#e377c2","#17becf"];
const RED = "#c0392b", GRAY = "#9aa0a6", colorOf = {}; let ci = 0;
function color(n){ if (n.removed) return RED; if (n.name === "unlabelled") return GRAY;
  if (!(n.name in colorOf)) colorOf[n.name] = PAL[ci++ % PAL.length]; return colorOf[n.name]; }
const tip = document.createElement("div"); tip.className = "sk-tip"; tip.style.display = "none"; document.body.appendChild(tip);
function fmt(n){ return n.toLocaleString(); }
function pct(a, b){ return b ? (100 * a / b).toFixed(a / b < 0.01 ? 2 : 1) + "%" : ""; }
function showTip(ev, html){ tip.innerHTML = html; tip.style.display = "block"; moveTip(ev); }
function moveTip(ev){ const pad = 14; let x = ev.pageX + pad, y = ev.pageY + pad;
  if (x + tip.offsetWidth > window.scrollX + window.innerWidth - 8) x = ev.pageX - tip.offsetWidth - pad;
  tip.style.left = x + "px"; tip.style.top = y + "px"; }
function hideTip(){ tip.style.display = "none"; }

function render(){
  el.innerHTML = "";
  // stage titles stand vertical (rotated -90°) so ten rounds of "round N · msp / zmip" never collide
  const nS = D.stages.length, W = Math.max(el.clientWidth, 700), bottom = 14;
  const top = 16 + Math.max(...D.stages.map(s => s.length)) * 6.6, H = 620 + top - 34;
  const padL = 150, padR = 150, barW = 14, gap = 3;
  const innerW = W - padL - padR, colX = i => padL + (nS === 1 ? 0 : i * (innerW - barW) / (nS - 1));
  const byStage = D.stages.map(() => []);
  D.nodes.forEach((n, i) => { n.id = i; byStage[n.stage].push(n); });
  const maxNodes = Math.max(...byStage.map(a => a.length));
  const scale = (H - top - bottom - gap * (maxNodes - 1)) / D.total;   // px per cell, column 0 is the tallest
  byStage.forEach(nodes => { let y = top; nodes.forEach(n => { n.h = n.count * scale; n.y = y; y += n.h + gap; n.inOff = 0; n.outOff = 0; }); });
  const ns = "http://www.w3.org/2000/svg", svg = document.createElementNS(ns, "svg");
  svg.setAttribute("width", W); svg.setAttribute("height", H); svg.setAttribute("class", "sk");
  const mk = (t, a) => { const e = document.createElementNS(ns, t); for (const k in a) e.setAttribute(k, a[k]); return e; };
  // stage titles
  D.stages.forEach((t, i) => { const x = colX(i) + barW / 2 + 4, y = top - 8;
    const tx = mk("text", {x, y, transform: `rotate(-90 ${x} ${y})`, "text-anchor": "start", class: "sk-stage"}); tx.textContent = t; svg.appendChild(tx); });
  // flows (drawn first, under the bars)
  const gFlows = mk("g", {}); svg.appendChild(gFlows);
  const flowsBySrc = {}, flowsByDst = {};
  D.flows.forEach(f => { (flowsBySrc[f.src] = flowsBySrc[f.src] || []).push(f); (flowsByDst[f.dst] = flowsByDst[f.dst] || []).push(f); });
  // order flows so ribbons do not cross needlessly: by destination position at the source, by source position at the destination
  D.nodes.forEach(n => { (flowsBySrc[n.id] || []).sort((a, b) => D.nodes[a.dst].y - D.nodes[b.dst].y);
                         (flowsByDst[n.id] || []).sort((a, b) => D.nodes[a.src].y - D.nodes[b.src].y); });
  const paths = [];
  D.nodes.forEach(n => (flowsBySrc[n.id] || []).forEach(f => { f.y0 = n.y + n.outOff; n.outOff += f.count * scale; }));
  D.nodes.forEach(n => (flowsByDst[n.id] || []).forEach(f => { f.y1 = n.y + n.inOff; n.inOff += f.count * scale; }));
  D.flows.forEach(f => { const s = D.nodes[f.src], d = D.nodes[f.dst], h = f.count * scale;
    const x0 = colX(s.stage) + barW, x1 = colX(d.stage), dx = (x1 - x0) / 2;
    const p = `M${x0},${f.y0} C${x0 + dx},${f.y0} ${x1 - dx},${f.y1} ${x1},${f.y1} L${x1},${f.y1 + h} C${x1 - dx},${f.y1 + h} ${x0 + dx},${f.y0 + h} ${x0},${f.y0 + h} Z`;
    const path = mk("path", {d: p, fill: color(d.removed ? d : s), class: "sk-flow", "data-src": f.src, "data-dst": f.dst});
    path.addEventListener("mousemove", ev => { moveTip(ev); });
    path.addEventListener("mouseenter", ev => { focus(f.src, f.dst);
      showTip(ev, `<b>${esc(s.name)}</b> → <b>${esc(d.name)}</b><br>${fmt(f.count)} cells · ${pct(f.count, s.count)} of ${esc(s.name)}` +
                  (d.removed ? "" : ` · ${pct(f.count, d.count)} of ${esc(d.name)}`)); });
    path.addEventListener("mouseleave", () => { unfocus(); hideTip(); });
    gFlows.appendChild(path); paths.push(path); f.el = path; });
  // bars + labels
  const gBars = mk("g", {}); svg.appendChild(gBars);
  const minLabel = 0.012 * D.total, bigLabel = 0.08 * D.total, last = nS - 1;
  const colGap = nS === 1 ? innerW : (innerW - barW) / (nS - 1), roomy = colGap >= 220;
  D.nodes.forEach(n => { const x = colX(n.stage);
    const r = mk("rect", {x, y: n.y, width: barW, height: Math.max(n.h, 0.8), fill: color(n), class: "sk-node"});
    const stageTotal = byStage[n.stage].reduce((a, b) => a + b.count, 0);
    r.addEventListener("mousemove", moveTip);
    r.addEventListener("mouseenter", ev => { focusNode(n.id);
      const ins = (flowsByDst[n.id] || []).slice().sort((a, b) => b.count - a.count).slice(0, 6)
        .map(f => `${esc(D.nodes[f.src].name)} ${fmt(f.count)}`).join("<br>");
      const outs = (flowsBySrc[n.id] || []).slice().sort((a, b) => b.count - a.count).slice(0, 6)
        .map(f => `${esc(D.nodes[f.dst].name)} ${fmt(f.count)}`).join("<br>");
      showTip(ev, `<b>${esc(n.name)}</b><br><span class="m">${esc(D.stages[n.stage])}</span><br>${fmt(n.count)} cells · ${pct(n.count, stageTotal)} of this stage · ${pct(n.count, D.total)} of input`
        + (ins ? `<br><span class="m">from:</span><br>${ins}` : "") + (outs ? `<br><span class="m">to:</span><br>${outs}` : "")); });
    r.addEventListener("mouseleave", () => { unfocus(); hideTip(); });
    gBars.appendChild(r);
    // labels: every readable node in the first and last column (outside the drawing). In
    // between, all of them only when the columns are far apart; otherwise just the big ones
    // (≥ 8 % of input), centred on the bar with a white halo — the rest is one hover away.
    const right = n.stage === last, edge = n.stage === 0 || right;
    const show = n.count >= minLabel && (edge || roomy || (n.count >= bigLabel && colGap >= 90));
    if (show) { const mid = !edge && !roomy;
      const t = mk("text", {x: mid ? x + barW / 2 : (right ? x + barW + 6 : x - 6), y: n.y + n.h / 2 + 4,
                            "text-anchor": mid ? "middle" : (right ? "start" : "end"),
                            class: "sk-label" + (n.removed ? " rm" : "") + (mid ? " mid" : "")});
      t.textContent = `${n.name} (${fmt(n.count)})`; gBars.appendChild(t); } });
  el.appendChild(svg);
  function focus(src, dst){ svg.classList.add("dim"); D.flows.forEach(f => f.el.classList.toggle("hi", f.src === src && f.dst === dst)); }
  function focusNode(id){ svg.classList.add("dim"); D.flows.forEach(f => f.el.classList.toggle("hi", f.src === id || f.dst === id)); }
  function unfocus(){ svg.classList.remove("dim"); D.flows.forEach(f => f.el.classList.remove("hi")); }
}
function esc(s){ return String(s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c])); }
render(); let t; window.addEventListener("resize", () => { clearTimeout(t); t = setTimeout(render, 150); });
})();
"""


UMAP_JS = r"""
(function(){
const host = document.getElementById("umap-vis"); if (!host) return;
const status = host.querySelector(".umap-status"), row = host.querySelector(".umap-row");
const tip = document.createElement("div"); tip.className = "sk-tip"; tip.style.display = "none"; document.body.appendChild(tip);
const esc = s => String(s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const fmt = n => n.toLocaleString();
const GRID = 128, PAD = 12;
let D = null, panels = [], view = {k: 1, tx: 0, ty: 0}, hoverIdx = -1, drag = null, S = 480;
let X, Y, grid = null, raf = 0, lod = false, lodTimer = 0;
try { D = JSON.parse(document.getElementById("umap-data").textContent); init(); }
catch (e) { status.textContent = "Could not render embedded UMAP data (" + e + "). Regenerate this page with ecarsi.index."; }

function rgba(hex){ const h = hex.replace("#", ""); const v = parseInt(h.length === 3 ? h.split("").map(c => c + c).join("") : h, 16);
  return [(v >> 16) & 255, (v >> 8) & 255, v & 255]; }
// A palette colour as one packed ABGR pixel, read once per repaint so switching theme is enough.
function theme(name){ const c = rgba(getComputedStyle(document.documentElement).getPropertyValue(name).trim() || "#bbbbbb");
  return 0xff000000 | (c[2] << 16) | (c[1] << 8) | c[0]; }
function init(){
  X = Float32Array.from(D.x, v => v / 65535); Y = Float32Array.from(D.y, v => 1 - v / 65535);
  // spatial grid over data space for O(1) nearest-cell lookup on hover
  const cells = new Array(GRID * GRID).fill(null);
  for (let i = 0; i < D.n; i++) { const g = Math.min(GRID - 1, (X[i] * GRID) | 0) + GRID * Math.min(GRID - 1, (Y[i] * GRID) | 0); (cells[g] || (cells[g] = [])).push(i); }
  grid = cells;
  const shown = D.n_total && D.n_total > D.n ? `showing a stratified ${fmt(D.n)} of ${fmt(D.n_total)} cells (small labels kept whole; legend counts are complete)` : `${fmt(D.n)} cells`;
  status.innerHTML = `${shown} · <span class="muted">wheel = zoom · drag = pan · double-click = reset · click a legend entry to isolate a label; panels stay in sync</span>`;
  row.innerHTML = "";
  for (const key of Object.keys(D.layers)) {
    const L = D.layers[key], el = document.createElement("div"); el.className = "umap-panel";
    el.innerHTML = `<h3>${esc(key)} labels <span class="count">${L.labels.length} · ${esc(L.column)}</span></h3><canvas aria-label="UMAP, ${esc(key)} labels"></canvas><div class="umap-legend"></div>`;
    row.appendChild(el);
    const P = {key, L, cv: el.querySelector("canvas"), legend: el.querySelector(".umap-legend"), sel: null, hi: null,
               idx: Int32Array.from(L.idx), rgb: L.colors.map(rgba), base: document.createElement("canvas"), baseKey: "", medians: null};
    P.medians = medians(P);
    panels.push(P); bind(P); buildLegend(P);
  }
  window.addEventListener("resize", () => { layout(); schedule(); });
  layout(); schedule();
}
function medians(P){ // label anchor = median x/y in data space, computed once
  const by = new Map();
  for (let i = 0; i < D.n; i++) { const c = P.idx[i]; if (c < 0) continue; let a = by.get(c); if (!a) by.set(c, a = [[], []]); a[0].push(X[i]); a[1].push(Y[i]); }
  const out = [];
  for (const [c, [xs, ys]] of by) { xs.sort((a, b) => a - b); ys.sort((a, b) => a - b); out.push({c, n: xs.length, x: xs[xs.length >> 1], y: ys[ys.length >> 1]}); }
  return out;
}
function layout(){ const n = panels.length || 1; S = Math.max(360, Math.min(640, Math.floor((host.clientWidth - 24 * (n - 1)) / n) - 2));
  const dpr = window.devicePixelRatio || 1;
  for (const P of panels) { P.cv.width = S * dpr; P.cv.height = S * dpr; P.cv.style.width = S + "px"; P.cv.style.height = S + "px"; P.baseKey = ""; } clamp(); }
function clamp(){ view.tx = Math.min(0, Math.max(view.tx, S - S * view.k)); view.ty = Math.min(0, Math.max(view.ty, S - S * view.k)); }
function sx(i){ return (PAD + X[i] * (S - 2 * PAD)) * view.k + view.tx; }
function sy(i){ return (PAD + Y[i] * (S - 2 * PAD)) * view.k + view.ty; }
function schedule(){ if (!raf) raf = requestAnimationFrame(() => { raf = 0; for (const P of panels) draw(P); }); }
function interact(){ // coarse pass while the view is moving, full pass when it settles
  lod = D.n > 40000; clearTimeout(lodTimer); lodTimer = setTimeout(() => { lod = false; for (const P of panels) P.baseKey = ""; schedule(); }, 140);
  for (const P of panels) P.baseKey = ""; schedule();
}
function bind(P){ const cv = P.cv;
  cv.addEventListener("wheel", ev => { ev.preventDefault(); const r = cv.getBoundingClientRect(), mx = ev.clientX - r.left, my = ev.clientY - r.top;
    const f = ev.deltaY < 0 ? 1.2 : 1 / 1.2, k2 = Math.min(Math.max(view.k * f, 1), 60);
    view.tx = mx - (mx - view.tx) * (k2 / view.k); view.ty = my - (my - view.ty) * (k2 / view.k); view.k = k2; clamp(); interact(); }, {passive: false});
  cv.addEventListener("mousedown", ev => { drag = {x: ev.clientX, y: ev.clientY, tx: view.tx, ty: view.ty}; });
  window.addEventListener("mousemove", ev => { if (!drag) return; view.tx = drag.tx + ev.clientX - drag.x; view.ty = drag.ty + ev.clientY - drag.y; clamp(); interact(); });
  window.addEventListener("mouseup", () => { drag = null; });
  cv.addEventListener("mousemove", ev => { if (drag) return; const r = cv.getBoundingClientRect(); hover(ev.clientX - r.left, ev.clientY - r.top, ev); });
  cv.addEventListener("mouseleave", () => { if (hoverIdx >= 0) { hoverIdx = -1; schedule(); } tip.style.display = "none"; });
  cv.addEventListener("dblclick", () => { view = {k: 1, tx: 0, ty: 0}; interact(); });
}
function buildLegend(P){
  const L = P.L, order = L.labels.map((_, i) => i).sort((a, b) => L.counts[b] - L.counts[a]);
  P.legend.innerHTML = order.map(i => `<div class="umap-leg${P.sel === i ? " on" : ""}${P.sel !== null && P.sel !== i ? " off" : ""}" data-i="${i}"><i style="background:${L.colors[i]}"></i><span class="lab" title="${esc(L.labels[i])}">${esc(L.labels[i])}</span><span class="n">${fmt(L.counts[i])}</span></div>`).join("");
  P.legend.querySelectorAll(".umap-leg").forEach(el => { const i = +el.dataset.i;
    el.addEventListener("click", () => { P.sel = P.sel === i ? null : i; buildLegend(P); P.baseKey = ""; schedule(); });
    el.addEventListener("mouseenter", () => { P.hi = i; P.baseKey = ""; schedule(); }); el.addEventListener("mouseleave", () => { P.hi = null; P.baseKey = ""; schedule(); }); });
}
function renderBase(P){ // points → pixel buffer (no per-point canvas calls), cached until view/focus changes
  const dpr = window.devicePixelRatio || 1, W = Math.round(S * dpr), focus = P.hi !== null ? P.hi : P.sel;
  const key = [view.k.toFixed(3), view.tx.toFixed(1), view.ty.toFixed(1), focus, lod, W].join("|");
  if (P.baseKey === key) return;
  P.base.width = W; P.base.height = W;
  const bctx = P.base.getContext("2d"), img = bctx.createImageData(W, W), buf = new Uint32Array(img.data.buffer);
  buf.fill(0xffffffff);
  // Radius in CSS pixels: sparse datasets and larger panels need larger
  // markers. Bound both density scaling and zoom to keep dense clouds legible.
  const radius = Math.min(7, Math.max(1, Math.min(3.5, 0.16 * (S - 2 * PAD) / Math.sqrt(Math.max(1, D.n)))) * Math.sqrt(view.k));
  const r = Math.max(1, Math.round(dpr * radius));
  const stride = lod ? Math.max(1, Math.ceil(D.n / 30000)) : 1, idx = P.idx, rgb = P.rgb;
  const pack = c => 0xff000000 | (c[2] << 16) | (c[1] << 8) | c[0];
  // Grey is only grey against a known surface: on the dark theme the panel is dark, so the
  // recede-into-the-background colours have to come from the palette rather than be baked light.
  const dim = theme("--umap-dim"), blank = theme("--umap-blank");
  const cols = rgb.map(pack);
  const passes = focus === null ? [null] : [false, true];
  for (const want of passes) {
    for (let i = 0; i < D.n; i += stride) { const c = idx[i], isF = focus !== null && c === focus; if (want !== null && isF !== want) continue;
      const x = Math.round(sx(i) * dpr), y = Math.round(sy(i) * dpr); if (x < 0 || y < 0 || x >= W || y >= W) continue;
      const col = focus !== null && !isF ? dim : (c >= 0 ? cols[c] : blank);
      const y0 = Math.max(0, y - r), y1 = Math.min(W - 1, y + r);
      for (let yy = y0; yy <= y1; yy++) {
        const dx = Math.floor(Math.sqrt(r * r - (yy - y) * (yy - y)));
        const x0 = Math.max(0, x - dx), x1 = Math.min(W - 1, x + dx);
        let o = yy * W + x0; for (let xx = x0; xx <= x1; xx++) buf[o++] = col;
      } }
  }
  bctx.putImageData(img, 0, 0); P.baseKey = key;
}
function draw(P){
  renderBase(P);
  const cv = P.cv, ctx = cv.getContext("2d"), dpr = window.devicePixelRatio || 1, L = P.L, focus = P.hi !== null ? P.hi : P.sel;
  ctx.setTransform(1, 0, 0, 1, 0, 0); ctx.drawImage(P.base, 0, 0); ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.font = "600 12px system-ui, sans-serif"; ctx.textAlign = "center"; ctx.lineWidth = 3; ctx.strokeStyle = "rgba(255,255,255,.85)"; ctx.fillStyle = "#1f2328";
  const minN = P.key === "coarse" ? 1 : Math.max(30, D.n * 0.004);
  for (const m of P.medians) { if (m.n < minN || (focus !== null && m.c !== focus)) continue;
    const mx = (PAD + m.x * (S - 2 * PAD)) * view.k + view.tx, my = (PAD + m.y * (S - 2 * PAD)) * view.k + view.ty;
    if (mx < 0 || my < 0 || mx > S || my > S) continue; const t = L.labels[m.c]; ctx.strokeText(t, mx, my); ctx.fillText(t, mx, my); }
  if (hoverIdx >= 0) { ctx.beginPath(); ctx.arc(sx(hoverIdx), sy(hoverIdx), 6, 0, 2 * Math.PI); ctx.strokeStyle = "#1f2328"; ctx.lineWidth = 1.5; ctx.stroke(); }
}
function nearest(mx, my){ // data-space grid lookup within ~8 screen px
  const ux = ((mx - view.tx) / view.k - PAD) / (S - 2 * PAD), uy = ((my - view.ty) / view.k - PAD) / (S - 2 * PAD);
  const rad = 8 / (view.k * (S - 2 * PAD)), g0 = Math.max(0, ((ux - rad) * GRID) | 0), g1 = Math.min(GRID - 1, ((ux + rad) * GRID) | 0);
  const h0 = Math.max(0, ((uy - rad) * GRID) | 0), h1 = Math.min(GRID - 1, ((uy + rad) * GRID) | 0);
  let best = -1, bd = 64;
  for (let gy = h0; gy <= h1; gy++) for (let gx = g0; gx <= g1; gx++) { const cell = grid[gx + GRID * gy]; if (!cell) continue;
    for (const i of cell) { const dx = sx(i) - mx, dy = sy(i) - my, d = dx * dx + dy * dy; if (d < bd) { bd = d; best = i; } } }
  return best;
}
function hover(mx, my, ev){
  const best = nearest(mx, my);
  if (best !== hoverIdx) { hoverIdx = best; schedule(); }
  if (best < 0) { tip.style.display = "none"; return; }
  const rows = Object.entries(D.layers).map(([k, L]) => `<span class="m">${esc(k)}:</span> ${esc(L.idx[best] >= 0 ? L.labels[L.idx[best]] : "–")}`);
  for (const [k, E] of Object.entries(D.extra)) rows.push(`<span class="m">${esc(k)}:</span> ${esc(E.idx[best] >= 0 ? E.labels[E.idx[best]] : "–")}`);
  tip.innerHTML = rows.join("<br>"); tip.style.display = "block";
  const pad = 14; let x = ev.pageX + pad, y = ev.pageY + pad; if (x + tip.offsetWidth > window.scrollX + window.innerWidth - 8) x = ev.pageX - tip.offsetWidth - pad;
  tip.style.left = x + "px"; tip.style.top = y + "px";
}
})();
"""


def _json(p: Path, default=None):
    if not p.is_file():
        return default
    with open(p) as f:
        return json.load(f)


def _n_obs(h5ad: Path) -> int | None:
    try:
        import h5py

        with h5py.File(h5ad, "r") as f:
            return int(f["obs"][f["obs"].attrs["_index"]].shape[0])
    except Exception:
        return None


def fmt_elapsed(sec) -> str:
    if sec is None or sec != sec:
        return "n/a"
    sec = int(sec)
    return f"{sec // 3600}h{(sec % 3600) // 60:02d}m" if sec >= 3600 else f"{sec // 60}m{sec % 60:02d}s"


def read_stats(path: Path) -> dict:
    text = path.read_text().strip()
    if text.startswith("{"):
        return json.loads(text)
    st = dict(tok.split("=", 1) for tok in text.split())
    return {k: (float(v) if k in ("frac", "elapsed_s") else v if k == "decision" else int(v)) for k, v in st.items()}


# ---------------------------------------------------------------- unit state

def persample_state(unit: Path) -> dict:
    man = _json(L.persample_manifest(unit), {})
    samples = []
    for s in man.get("samples", []):
        d = L.sample_dir(unit, s)
        contract = L.PS_ANNOTATE_LIGHT if man.get("annotate", True) else L.PS_LIGHT  # light: renders from a mirror
        done = L.complete(d, contract)
        if man.get("schema_version") == 2:
            # Display the recorded validation; actual resume rehashes and
            # rereads outputs in osp_contract. Never hash H5AD on HTTP GET.
            state = _json(d / L.RUN_STATE, {})
            done = (done and state.get("state") == "complete" and state.get("exit_code") == 0
                    and state.get("identity") == s.get("identity")
                    and s["value"] not in man.get("failed_samples", [])
                    and all((d / f).is_file() for f in L.PS_QC_CONTRACT))
        empty = s["value"] in man.get("empty_samples", [])  # QC removed every cell; finished without outputs
        samples.append({"name": d.name, "value": s["value"], "n_cells": s["n_cells"], "dir": d,
                        "done": done or empty, "empty": empty, "report": (d / "report.html").is_file()})
    return {"manifest": bool(man), "sample_column": man.get("sample_column"), "species": man.get("species"),
            "n_excluded": sum(r["n_cells"] for r in (man.get("sample_mapping") or {}).get("exclude_cells", [])),
            "samples": samples, "n_done": sum(s["done"] for s in samples), "n": len(samples),
            "done": bool(samples) and all(s["done"] for s in samples)}


def _round_step(rdir: Path) -> str:
    """What a round without a decision is currently doing, from the light
    step markers only — the page must say the same thing on a --mirror copy,
    which carries no h5ad."""
    cdir, zdir = L.crosssample_dir(rdir), L.zoomin_dir(rdir)
    if not L.complete(cdir, L.MSP_LIGHT):
        if not L.complete(cdir, L.MSP_INTEGRATED_LIGHT):
            return "crosssample · integrate" if (cdir.is_dir() or (rdir / L.ROUND_INPUT).is_file()) else "starting"
        if not (cdir / "inspection_proposal.json").is_file():
            return "crosssample · inspect"
        return "crosssample · annotate"
    if not L.complete(zdir, L.ZMIP_LIGHT):
        plan = _json(zdir / "zmip_plan.json")
        if not plan:
            return "zoomin · plan"
        zoomed = [ln["name"] for ln in plan["lineages"] if ln["zoom"]]
        done = [n for n in zoomed if L.complete(L.lineage_dir(zdir, n), L.ZMIP_LINEAGE_LIGHT)]
        return f"zoomin · lineages {len(done)}/{len(zoomed)}"
    if not (L.ledger_dir(rdir) / "cell_ledger.csv").is_file():
        return "ledger"
    return "deciding"


def rounds_state(unit: Path) -> list[dict]:
    out = []
    for rdir in L.rounds(unit):
        n = L.round_number(rdir)
        cdir, zdir = L.crosssample_dir(rdir), L.zoomin_dir(rdir)
        st_p, dec_p = rdir / L.STATS, rdir / L.DECISION
        r = {"n": n, "dir": rdir, "stats": None, "decision": None, "step": None,
             "msp_report": (cdir / "report.html").is_file(), "zmip_report": (zdir / "report.html").is_file(),
             "sankey": (L.ledger_dir(rdir) / "sankey_coarse.png").is_file()}
        if st_p.is_file() and dec_p.is_file():
            r["stats"] = read_stats(st_p)
            r["decision"] = dec_p.read_text().strip()
        else:
            r["step"] = _round_step(rdir)
            n_in = _round_input_cells(unit, n)  # from progress.log, so a mirror copy knows it too
            if n_in is None and (cdir / "integrated.h5ad").is_file():
                n_in = _n_obs(cdir / "integrated.h5ad")
            if n_in is not None:
                r["n_in"] = n_in
        out.append(r)
    return out


def _round_input_cells(unit: Path, n: int) -> int | None:
    """Cells entering round n, as the loop logged it ('round N input prepared
    from round M (X cells)'); round 1 has no such line."""
    pat = re.compile(rf"^round {n} input prepared from round \d+ \((\d+) cells\)")
    for _, event in reversed(L.read_log(unit)):
        m = pat.match(event)
        if m:
            return int(m.group(1))
    return None


_EVENT_ORGANIZE = re.compile(r"^organize: (\d+) cells")
_EVENT_RELEASE = re.compile(r"^release rounds=\d+ final_cells=(\d+)")


def _epoch(ts: str) -> float:
    import time

    return time.mktime(time.strptime(ts, "%Y-%m-%d %H:%M:%S"))


def log_events(log: list[tuple[str, str]]) -> dict:
    """When cells entered and left a unit, as (epoch seconds, cells): the first
    'organize: N cells' line and the last 'release ... final_cells=N' line.
    Derived, never recorded: what is on disk now is the whole history."""
    org = rel = None
    for ts, event in log:
        m = _EVENT_ORGANIZE.match(event)
        if m and org is None:
            org = (_epoch(ts), int(m.group(1)))
        m = _EVENT_RELEASE.match(event)
        if m:
            rel = (_epoch(ts), int(m.group(1)))
    return {"organize": org, "release": rel}


def _round_started_after(log: list[tuple[str, str]], n: int) -> tuple[int, str] | None:
    """(round, timestamp) of the newest 'round N start' with N > n, else None."""
    pat = re.compile(r"^round (\d+) start$")
    for ts, event in reversed(log):
        m = pat.match(event)
        if m and int(m.group(1)) > n:
            return int(m.group(1)), ts
    return None



# ---------------------------------------------- generation 2 (durable control plane)
# A gen-2 run records its state in publication.json files instead of progress.log / stats.txt /
# decision.txt, and keeps its computed artefacts in the Pool's request folders; only release/ is
# copied into the unit. These readers return the state a gen-1 unit returns, so one navigator lists
# both generations and the release sections (ledger, sankey, UMAP, needs review) are shared.

def _gen2_rounds(unit: Path) -> list[dict]:
    out = []
    # A round's input is settled the moment the round opens: it is what the previous round
    # left, or what per-sample published. Waiting for cross-sample to restate it leaves the
    # column blank for the first half of every round, which reads as "unknown", not "pending".
    survivors = _json(unit / L.GEN2_PERSAMPLE / L.GEN2_PUBLICATION, {}).get("n_survived")
    for rdir in sorted((unit / L.ROUNDS).glob("round*")):
        record = _json(rdir / L.GEN2_PUBLICATION, {})
        stats = record.get("stats") or {}
        started = min((p.stat().st_mtime for p in (rdir / L.GEN2_CROSS).glob("spec.json")), default=None)
        finished = (rdir / L.GEN2_PUBLICATION).stat().st_mtime if (rdir / L.GEN2_PUBLICATION).is_file() else None
        row = {"n": record.get("round") or L.round_number(rdir), "dir": rdir, "stats": None, "decision": None,
               "step": None, "reason": stats.get("reason", ""),
               "sankey": (rdir / L.LEDGER / "sankey.json").is_file(),
               "seconds": (finished - started) if started and finished else None,
               "msp_report": (rdir / L.GEN2_CROSS / "report.html").is_file(),
               "zmip_report": (rdir / L.GEN2_ZOOM / "report.html").is_file()}
        if stats:
            row["stats"] = {k: stats.get(k) for k in ("n_in", "n_out", "removed", "frac")}
            row["decision"] = stats.get("decision")
            survivors = stats.get("n_out")
        else:
            cross = _json(rdir / L.GEN2_CROSS / L.GEN2_PUBLICATION, {})
            zoom = _json(rdir / L.GEN2_ZOOM / L.GEN2_PUBLICATION, {})
            row["step"] = ("zoom-in done, deciding" if zoom else "zoom-in" if cross
                           else "cross-sample" if (rdir / L.GEN2_CROSS).is_dir() else "starting")
            row["n_in"] = cross.get("n_input") or survivors
            if cross:
                # The round's own number arrives only when it ends. Cross-sample publishes its
                # half as soon as it is done, which is a subtotal, not a forecast: on this batch
                # cross-sample removed 61 cells of a round that went on to remove thousands.
                if cross.get("n_removed") is not None and cross.get("n_input"):
                    row["partial"] = {"stage": "cross-sample", "removed": cross["n_removed"],
                                      "frac": cross["n_removed"] / cross["n_input"]}
        out.append(row)
    return out


def _gen2_unit_state(unit: Path) -> dict:
    published = _json(unit / L.GEN2_PUBLICATION, {})
    per = _json(unit / L.GEN2_PERSAMPLE / L.GEN2_PUBLICATION, {})
    manifest = _json(unit.parent.parent / L.GEN2_ORGANIZE / L.UNITS / unit.name / L.INPUT / L.MANIFEST, {})
    rounds = _gen2_rounds(unit)
    release = L.release_dir(unit)
    released = (release / "receipt.json").is_file()
    done = [r for r in rounds if r["stats"]]
    failed_samples = per.get("failed_samples") or []
    skipped = per.get("skipped_samples") or []
    # A unit's own failure is recorded by the dataset that waited for it, not inside the unit.
    # A resumed dataset does not rewrite that record until it finishes, so a failure older
    # than the unit's newest publication has already been superseded by the work that followed.
    dataset_path = unit.parent.parent / L.GEN2_PUBLICATION
    dataset = _json(dataset_path, {})
    failure = next((f for f in dataset.get("failed_units", []) if f.get("unit") == unit.name), None)
    if failure is not None and dataset_path.is_file():
        newest = max((p.stat().st_mtime for p in [unit / L.GEN2_PERSAMPLE / L.GEN2_PUBLICATION,
                                                  *[r["dir"] / L.GEN2_PUBLICATION for r in rounds],
                                                  *(unit / L.ROUNDS).glob("round*/0*/" + L.GEN2_PUBLICATION)]
                      if p.is_file()), default=0)
        if newest > dataset_path.stat().st_mtime:
            failure = None
    if released:
        stage, cls = f"released after {len(rounds)} round(s)", "released"
    elif failure and str(failure.get("error", "")).startswith("PAUSED"):
        # A unit stopped by loop_control fails its workflow so it stays resumable; that is a
        # held run waiting on a person, not a broken one.
        stage, cls = f"paused — {failure['error'].split(':', 1)[-1].strip()}"[:120], "paused"
    elif failure:
        stage, cls = f"failed — {failure.get('error', '')}"[:120], "failed"
    elif failed_samples:
        stage, cls = f"{len(failed_samples)} sample(s) failed", "failed"
    elif rounds and rounds[-1]["stats"] is None:
        stage, cls = f"round {rounds[-1]['n']} · {rounds[-1]['step']}", "running"
    elif rounds:
        stage, cls = f"round {rounds[-1]['n']} done, next round pending", "running"
    elif per:
        stage, cls = ("per-sample done, first round pending" if per.get("state") == "complete"
                      else f"per-sample {len(per.get('samples', []))} sample(s)"), "running"
    elif any((unit / L.GEN2_PERSAMPLE).glob("*")):
        stage, cls = "per-sample running", "running"
    else:
        stage, cls = "organized, per-sample not started", "running"
    n_input = published.get("n_input") or per.get("n_input") or manifest.get("n_cells")
    final_cells = published.get("n_survived") if released else (done[-1]["stats"]["n_out"] if done else None)
    events = {"organize": None, "release": None}
    organized = unit.parent.parent / L.GEN2_ORGANIZE / L.GEN2_PUBLICATION
    if organized.is_file() and n_input:
        events["organize"] = (organized.stat().st_mtime, n_input)
    if released and final_cells is not None:
        events["release"] = ((release / "receipt.json").stat().st_mtime, final_cells)
    return {"name": unit.name, "dir": unit, "generation": 2, "n_input": n_input,
            "species": manifest.get("species") or "",
            "finished": _when(events["release"][0]) if events["release"] else None,
            "persample": {"manifest": bool(per), "n": len(per.get("samples", [])) + len(failed_samples),
                          "n_done": len(per.get("samples", [])), "done": per.get("state") == "complete",
                          "samples": [], "species": manifest.get("species"), "sample_column": None,
                          "n_excluded": per.get("n_removed"), "skipped": skipped, "failed": failed_samples},
            "rounds": rounds, "released": released, "stage": stage, "stage_class": cls,
            "last_event": published.get("reason", "") or (rounds[-1]["reason"] if rounds else ""),
            "final_cells": final_cells,
            "output_h5ad": (release / "final.h5ad") if (release / "final.h5ad").is_file() else None,
            "output_note": "final" if released else "", "sample_decisions": {},
            "forced": bool(published.get("forced_release")), "events": events}


def _gen2_unit_body(unit: Path, s: dict, base: str = "") -> str:
    e = _h.escape
    release = L.release_dir(unit)
    items = review.from_json(release / "needs_review.json") if (release / "needs_review.json").is_file() else []
    done = [r for r in s["rounds"] if r["stats"]]
    n_in, n_fin = s["n_input"], s["final_cells"]
    removed_frac = (1 - n_fin / n_in) if n_in and n_fin is not None else None
    per = s["persample"]
    glance = [
        _stat(_n(n_in) or "–", "input cells", e(str(s["species"] or ""))),
        _stat(_n(n_fin) or "–", "final cells" if s["released"] else "cells now",
              "" if s["released"] or n_fin is None else "after the last finished round"),
        _stat(_pct(removed_frac) if removed_frac is not None else "–", "removed overall",
              "QC, excluded samples and rounds", "tone-bad" if removed_frac and removed_frac > 0.3 else ""),
        _stat(str(len(done)) + (" <small>+1 running</small>" if s["rounds"] and not s["rounds"][-1]["stats"] else ""), "rounds"),
        _stat(f'{per["n_done"]}/{per["n"]}' if per["n"] else "–", "samples annotated",
              f'{len(per["skipped"])} skipped' if per["skipped"] else ""),
        _stat(str(len(items)), "needs review", "items, see below" if items else "nothing so far",
              "tone-warn" if items else ""),
    ]
    reports = sorted((unit / L.GEN2_PERSAMPLE).glob("*/report.html"))
    sections = (("files", "Files"), ("rounds", "Rounds"), ("samples", "Samples"), ("sankey", "Cell identity"),
                ("umap", "Final UMAP"), ("review", "Needs review"))
    parts = ['<div class="glance">' + "".join(glance) + "</div>",
             '<nav class="jump" aria-label="sections">'
             + "".join(f'<a href="#{k}">{t}</a>' for k, t in sections) + "</nav>"]
    files = []
    if s["released"]:
        for name, what in (("summary.json", "what happened, round by round"),
                           ("needs_review.md", "the review items below, as text"),
                           ("needs_review.json", "same, machine-readable"),
                           ("cell_ledger.csv.gz", "one row per input cell: status and labels per stage"),
                           ("cell_exclusions.csv.gz", "every removal with its reason"),
                           ("final.h5ad", "the released matrix"),
                           ("umap.json", "final embedding and labels behind the UMAP panels")):
            if (release / name).is_file():
                files.append((name, f'<a href="{base}{L.RELEASE}/{name}">{name}</a> <span class="muted">{what}</span>'))
    parts.append('<section class="block" id="files"><h2>Files</h2>'
                 "<p class=\"lede\">A generation-2 run keeps its computed artefacts in the warm pool's request folders, "
                 'each referenced by the publication that accepted it; the unit directory holds those publications and, '
                 'once released, the copied release.</p>'
                 + ('<dl class="files">' + "".join(f"<dt>{e(k)}</dt><dd>{v}</dd>" for k, v in files) + "</dl>"
                    if files else '<p class="empty">No release yet: the loop has not converged.</p>') + "</section>")
    rows = []
    for r in s["rounds"]:
        rp = f'{base}{L.ROUNDS}/{r["dir"].name}'
        links = " · ".join(x for x in [
            f'<a href="{rp}/{L.GEN2_CROSS}/report.html">msp</a>' if r.get("msp_report") else "",
            f'<a href="{rp}/{L.GEN2_ZOOM}/report.html">zmip</a>' if r.get("zmip_report") else "",
            f'<a href="{rp}/{L.LEDGER}/cell_ledger.csv.gz">ledger</a>' if r.get("sankey") else ""] if x)
        st = r["stats"]
        elapsed = fmt_elapsed(r["seconds"]) if r.get("seconds") else ""
        if st:
            dec, frac = r["decision"], st.get("frac")
            pill = ("failed" if str(r.get("reason", "")).startswith("FORCED") else
                    "released" if dec == "release" else "neutral")
            rows.append(f'<tr><td class="num">{r["n"]}</td><td class="num">{_n(st["n_in"])}</td>'
                        f'<td class="num">{_n(st["n_out"])}</td><td class="num">{_n(st["removed"])}</td>'
                        f'<td class="num">{_pct(frac) + _bar(frac) if frac is not None else ""}</td>'
                        f'<td><span class="pill {pill}">{e(str(dec))}</span></td>'
                        f'<td class="reason">{e(str(r.get("reason") or ""))}</td>'
                        f'<td class="num">{elapsed}</td><td>{links}</td></tr>')
        else:
            part = r.get("partial")
            so_far = (f'<td class="num">{_n(part["removed"])}</td>'
                      f'<td class="num">{100 * part["frac"]:.2f}%<span class="sofar" '
                      f'title="{e(part["stage"])} only; the round is still removing">so far</span></td>'
                      if part else "<td></td><td></td>")
            rows.append(f'<tr><td class="num">{r["n"]}</td><td class="num">{_n(r.get("n_in"))}</td><td></td>{so_far}'
                        f'<td><span class="pill running">running</span></td>'
                        f'<td class="reason st running">{e(str(r["step"] or ""))}</td>'
                        f'<td class="num">{elapsed}</td><td>{links}</td></tr>')
    running_round = bool(s["rounds"]) and s["rounds"][-1]["stats"] is None
    parts.append(f'<section class="block" id="rounds"><h2>Rounds <span class="count">{len(done)} finished'
                 + (", 1 running" if running_round else "") + f'</span></h2><p class="lede">{EXPLAIN["rounds"]}</p>'
                 + ('<div class="wrap"><table><thead><tr><th class="r">round</th><th class="r">cells in</th><th class="r">cells out</th>'
                    '<th class="r">removed</th><th class="r">removed %</th><th>decision</th><th>reason</th>'
                    '<th class="r">wall time</th><th>reports</th></tr></thead>'
                    f'<tbody>{"".join(rows)}</tbody></table></div>' if rows else '<p class="empty">No round started yet.</p>')
                 + "</section>")
    summary = _json(unit / L.GEN2_PERSAMPLE / "samples.json", [])
    if not summary:  # published before the summary existed: the reports still name the samples
        summary = [{"sample": r.parent.name} for r in reports]
    first = unit / L.ROUNDS / "round01" / L.GEN2_CROSS / "inclusion.json"
    decided = {d["sample"]: d for d in _json(first, {}).get("samples", [])}
    rows = []
    for row in summary:
        folder = unit / L.GEN2_PERSAMPLE / row["sample"]
        link = (f'<a href="{base}{L.GEN2_PERSAMPLE}/{row["sample"]}/report.html">osp report</a>'
                if (folder / "report.html").is_file() else '<span class="muted">–</span>')
        state = row.get("state", "")
        status = ('<span class="pill empty-sample" title="OSP QC removed every cell; see qc_removed.csv">empty</span>'
                  if state == "empty" else '<span class="pill failed">failed</span>' if state == "failed"
                  else '<span class="pill running" title="two agent sessions died; the survivors carry no annotation">unannotated</span>'
                  if state == "unannotated" else '<span class="pill released">done</span>' if state
                  else '<span class="pill running">pending</span>')
        d = decided.get(row["sample"])
        dec = "" if d is None else "include" if d.get("include") else "exclude"
        pill = f'<span class="pill {e(dec)}">{e(dec)}</span>' if dec else '<span class="muted">–</span>'
        if dec == "exclude" and d.get("reason"):
            pill += (f'<details class="why"><summary title="why excluded?" aria-label="why excluded?">?</summary>'
                     f'<div class="why-body"><b>{e(row["sample"])} excluded:</b> {e(d["reason"])}</div></details>')
        rows.append(f'<tr><td>{e(row["sample"])}</td><td class="num">{_n(row.get("n_input"))}</td>'
                    f'<td>{status}</td><td class="why-cell">{pill}</td><td>{link}</td></tr>')
    meta = [f'{per["n_done"]}/{per["n"]} done'] if per["n"] else []
    if per.get("n_excluded"):
        meta.append(f'{per["n_excluded"]:,} cells excluded before OSP')
    parts.append(f'<section class="block" id="samples"><h2>Samples <span class="count">{" · ".join(meta)}</span></h2>'
                 f'<p class="lede">{EXPLAIN["samples"]}</p>'
                 + ('<div class="wrap"><table><thead><tr><th>sample</th><th class="r">input cells</th><th>osp</th>'
                    f'<th>integration</th><th>report</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>'
                    if rows else '<p class="empty">Per-sample processing has not started.</p>') + "</section>")
    if per["skipped"] or per["failed"]:
        detail = "".join(f'<li><b>{e(str(x.get("sample", "")))}</b> {e(str(x.get("error", ""))[:200])}</li>'
                         for x in list(per["skipped"]) + list(per["failed"]))
        parts.append('<section class="block"><h2>Samples needing attention</h2><ul class="warn">' + detail + "</ul></section>")
    # Released: the final ledger. Still looping: the newest round that published one, so the
    # Sankey is there from round one instead of only after convergence.
    sankey = next((p for p in [release / "sankey.json"]
                   + [r["dir"] / L.LEDGER / "sankey.json" for r in reversed(s["rounds"])] if p.is_file()), None)
    if sankey is not None:
        data = sankey.read_text(encoding="utf-8").replace("<", "\\u003c")
        through = ("the release" if sankey.parent == release
                   else "round " + str(next(r["n"] for r in s["rounds"] if r["dir"] / L.LEDGER == sankey.parent)))
        parts.append('<section class="block" id="sankey"><h2>Cell identity across steps and rounds '
                     f'<span class="count">coarse labels · through {through}</span></h2>'
                     f'<p class="lede">{EXPLAIN["sankey"]}</p>'
                     f'<div id="sankey-vis" class="wrap"></div><script>const SANKEY_DATA = {data};{SANKEY_JS}</script></section>')
    umap = release / "umap.json"
    if umap.is_file():
        data = umap.read_text(encoding="utf-8").replace("<", "\\u003c")
        parts.append('<section class="block" id="umap"><h2>Final UMAP '
                     '<span class="count">every released cell · coarse and fine labels</span></h2>'
                     f'<p class="lede">{EXPLAIN["umap"]}</p><div id="umap-vis">'
                     f'<p class="umap-status">loading {base}{L.RELEASE}/umap.json… (JavaScript required)</p>'
                     '<div class="umap-row"></div></div>'
                     f'<script type="application/json" id="umap-data">{data}</script><script>{UMAP_JS}</script></section>')
    counts = review.counts(items)
    brief = " · ".join(f"{n} {t.lower()}" for _, t, n, _ in counts) if counts else "nothing to review"
    parts.append(f'<section class="block" id="review"><h2>Needs review <span class="count">{len(items)} items — {e(brief)}</span></h2>'
                 f'<p class="lede">{EXPLAIN["review"]}</p>' + review.to_html(items, base) + "</section>")
    return "".join(parts)


def unit_state(unit: Path) -> dict:
    """Everything the pages need, read from disk."""
    if L.is_gen2_unit(unit):
        return _gen2_unit_state(unit)
    im = _json(L.input_manifest(unit), {})
    ps = persample_state(unit)
    rounds = rounds_state(unit)
    rel = L.release_dir(unit)
    released = (rel / "summary.md").is_file()
    log = L.read_log(unit)
    last = log[-1] if log else None
    # "persample complete: N experiments; 0 failed" is the success line, not a failure
    failed = bool(last) and "failed" in last[1] and not last[1].startswith("persample complete")
    if failed:
        stage, cls = f"failed — {last[1]}", "failed"
    elif last and last[1].startswith("paused by loop_control"):
        stage, cls = f"paused after round {len(rounds)} (loop_control.json) — re-run to continue", "running"
    elif released and (not rounds or rounds[-1]["decision"] is not None):
        stage, cls = f"released after {len(rounds)} round(s)", "released"
    elif rounds and rounds[-1]["decision"] is None:
        stage, cls = f"round {rounds[-1]['n']} · {rounds[-1]['step']}", "running"
    elif rounds:
        nxt = _round_started_after(log, rounds[-1]["n"])
        if nxt:  # a mirror copy carries the log line before any file of the new round
            stage, cls = f"round {nxt[0]} · crosssample (since {nxt[1][11:16]})", "running"
        else:
            stage, cls = f"round {rounds[-1]['n']} done, next round pending", "running"
    elif ps["manifest"] and not ps["done"]:
        stage, cls = f"persample {ps['n_done']}/{ps['n']} samples", "running"
    elif ps["done"]:
        stage, cls = "persample done, loop not started", "running"
    else:
        stage, cls = "organized, persample not started", "running"
    # cells now: survivors of the last finished round (final once released)
    done = [r for r in rounds if r["stats"]]
    final_cells = done[-1]["stats"]["n_out"] if done else None
    # the h5ad a reader should take: release/final.h5ad once released, else
    # the latest finished round's survivors (still moving while the loop runs)
    output_h5ad, output_note = None, ""
    if released and (rel / "final.h5ad").is_file():
        output_h5ad, output_note = rel / "final.h5ad", "final"
    else:
        with_h5ad = [r for r in done if (L.zoomin_dir(r["dir"]) / "annotated_zmip.h5ad").is_file()]
        if with_h5ad:
            output_h5ad = L.zoomin_dir(with_h5ad[-1]["dir"]) / "annotated_zmip.h5ad"
            output_note = f"latest survivors, round {with_h5ad[-1]['n']} — not final, the loop is still running"
    dec_rows = {}
    if rounds:
        dec = L.crosssample_dir(rounds[0]["dir"]) / "sample_decisions.csv"
        if dec.is_file():
            with open(dec) as f:
                dec_rows = {r["sample"]: r for r in csv.DictReader(f)}
    finished = next((ts for ts, ev in reversed(log) if ev.startswith("release ")), None) if released else None
    return {"name": unit.name, "dir": unit, "n_input": im.get("n_cells"), "species": im.get("species") or ps["species"], "finished": finished,
            "persample": ps, "rounds": rounds, "released": released, "stage": stage, "stage_class": cls,
            "last_event": f"{last[0]} {last[1]}" if last else "", "final_cells": final_cells,
            "output_h5ad": output_h5ad, "output_note": output_note,
            "sample_decisions": dec_rows, "forced": _forced(rounds), "events": log_events(log)}


def _forced(rounds: list[dict]) -> bool:
    return bool(rounds) and bool(rounds[-1]["stats"]) and str(rounds[-1]["stats"].get("reason", "")).startswith("FORCED")


# ---------------------------------------------------------------- unit page

def _pct(x) -> str:
    return f"{100 * x:.2f}%"


def _n(x) -> str:
    return "" if x is None or x == "" else f"{int(x):,}"


def _k(x) -> str:
    """Thousands, two decimals: a column of counts is read for its size, and 201.28k compares
    at a glance where 201,278 has to be counted. Rounded, so 1,017 is 1.02k."""
    return "" if x is None or x == "" else f"{int(x) / 1000:.2f}k"


def _bar(frac: float) -> str:
    return f'<span class="bar" title="{_pct(frac)}"><i style="width:{min(100, 100 * frac):.1f}%"></i></span>'


def _stat(v, k, sub="", cls="") -> str:
    return (f'<div class="stat {cls}"><span class="v">{v}</span><span class="k">{_h.escape(k)}</span>'
            + (f'<span class="sub">{sub}</span>' if sub else "") + "</div>")


def _when(ts: float | None) -> str:
    import time

    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else ""


def collection_of(path: Path) -> str:
    """Collection for eca-pp fleet trees or direct study/standardize layouts."""
    parts = Path(path).parts
    if "eca-pp" in parts[1:]:
        return parts[parts.index("eca-pp") - 1]
    if path.name == "rsi" and (path.parent / "standardize" / "result.json").is_file():
        return path.parent.parent.name
    return ""



def collections(direct: dict) -> dict:
    """Fill in the collections a path could not give. `direct` is name -> collection_of()
    (empty where the path says nothing). A run kept outside the fleet tree -- a
    control-plane run directory, a one-off -- still belongs to the collection its name
    carries: `<collection>-<rest>` is the prefix the pages already strip from the name.
    Takes the collections rather than the paths so the fleet pages never touch disk."""
    known = sorted({c for c in direct.values() if c}, key=len, reverse=True)
    return {name: c or next((k for k in known if name.startswith(k + "-")), "")
            for name, c in direct.items()}


def display_name(root: Path) -> str:
    """'Stomach' for .../eca-pp/Stomach/rsi, else the directory name."""
    parts = Path(root).parts
    return parts[parts.index("eca-pp") + 1] if "eca-pp" in parts[:-1] else root.name


# A live run rewrites a state file every few minutes at the very worst (a long msp
# integration still checkpoints). Nothing on disk records that a run was stopped --
# a killed driver, an expired Slurm job and a terminated workflow all leave the last
# state behind -- so a "running" run whose files stopped moving this long ago is
# reported as stopped instead of pretending it is still working.
STALE_AFTER = 12 * 3600


def _stalled(cls: str, stage: str, updated: float | None) -> tuple[str, str]:
    import time

    if cls != "running" or not updated or time.time() - updated < STALE_AFTER:
        return cls, stage
    return "failed", f"stopped · {stage}"


# Where a round's removal sits: the rule releases below 1 % (round_policy.RELEASE_FRAC) and calls
# three rounds under 2 % a plateau, so a run is doing well well before it stops.
# A band is a judgement on one round, not a lifecycle state, so it has its own class names:
# reusing .released/.running here tied the dot's colour to whatever a dataset pill happened to be.
TREND_BANDS = ((0.015, "band-good"), (0.03, "band-watch"))   # under 1.5 % green, under 3 % amber, else red
# The same three colours the dots get, as variables the SVG gradient can read.
BAND_INK = {"band-good": "--ok", "band-watch": "--run", "band-high": "--bad"}


def trend_band(frac: float) -> str:
    return next((cls for edge, cls in TREND_BANDS if frac < edge), "band-high")


def round_trend(states: list[dict]) -> list[dict]:
    """One point per round: how much of what entered it the round removed. A settled point is a
    finished round; an unsettled one is the part a running round has published so far and will
    grow. The unit with the most rounds speaks for a dataset -- the others are shorter runs of
    the same decision, and a mean would hide the one still removing."""
    rounds = max((s.get("rounds") or [] for s in states), key=len, default=[])
    points = []
    for r in rounds:
        stats, partial = r.get("stats"), r.get("partial")
        if stats and stats.get("frac") is not None:
            points.append({"n": r["n"], "frac": stats["frac"], "settled": True})
        elif partial:
            points.append({"n": r["n"], "frac": partial["frac"], "settled": False})
    return points


def dataset_state(root: Path, states: list[dict] | None = None) -> dict:
    """Aggregate of a run root (or a unit bound on its own) for the fleet
    pages; `states` = unit_state() per unit when the caller already has them."""
    if states is None:
        states = [unit_state(u) for u in ([root] if L.is_unit(root) or L.is_gen2_unit(root) else L.units(root))]
    released = sum(1 for s in states if s["released"])
    final = [s["final_cells"] for s in states if s["final_cells"] is not None]
    n_in = [s["n_input"] for s in states if s["n_input"] is not None]
    if not states:
        stage, cls = "Not started", "neutral"
    elif released == len(states):
        stage, cls = "released", "released"
    elif any(s["stage_class"] == "failed" for s in states):
        stage, cls = "failed", "failed"
    elif any(s["stage_class"] == "paused" for s in states):
        # A unit held by loop_control stops the dataset too, but it is waiting on a person.
        stage, cls = (states[0]["stage"] if len(states) == 1 else "paused"), "paused"
    else:
        stage, cls = (states[0]["stage"] if len(states) == 1 else f"{released}/{len(states)} released"), "running"
    fin = [s["finished"] for s in states if s.get("finished")]
    events = {k: [s["events"][k] for s in states if s.get("events") and s["events"][k]] for k in ("organize", "release")}
    updated = state_mtime(root)
    cls, stage = _stalled(cls, stage, updated)
    return {"units": len(states), "released": released, "n_input": sum(n_in) if n_in else None, "events": events,
            "final_cells": sum(final) if final else None, "rounds": max((len(s["rounds"]) for s in states), default=0),
            "species": ", ".join(sorted({str(s["species"]) for s in states if s["species"]})),
            "finished": max(fin) if fin and released == len(states) else None,
            "updated": updated, "stage": stage, "cls": cls, "trend": round_trend(states),
            "awaiting_start": False}


TREND_CEILING = 0.10   # a first round often removes 20-36 %; drawn to scale it flattens the rest
TREND_STEP = 11        # px per round: the safety cap is 15 rounds, which still fits the frame


def sparkline(points: list[dict], width: int = 108, height: int = 22) -> str:
    """Per-round removal as a share of what entered the round, oldest left. The scale is the run's
    own worst round up to TREND_CEILING, so an early clear-out does not squash the settling that
    follows; anything above the ceiling is drawn on it and keeps its true value in the tooltip.
    The colour is absolute, so two runs compare at a glance. A hollow point is still removing."""
    if not points:
        return '<span class="muted">–</span>'
    top = min(max(max(p["frac"] for p in points), 0.03), TREND_CEILING)
    # One round is one step, left to right from the frame's edge -- not stretched to fill it.
    # A run of two rounds spread across the full width reads like a long history of two states;
    # at a fixed step its shortness is the first thing visible. Long runs compress to fit.
    step = min(TREND_STEP, (width - 6) / max(len(points) - 1, 1))
    x = lambda i: 3 + i * step
    y = lambda f: height - 3 - (height - 6) * (min(f, top) / top)
    path = " ".join(("M" if i == 0 else "L") + f"{x(i):.1f} {y(p['frac']):.1f}" for i, p in enumerate(points))
    # The area under the line, washed in the colours of the rounds above it: a horizontal ramp
    # whose stops sit under their own point, so the tint between two rounds is the blend of the
    # two, faded out downward. Ids are derived from the path so two identical charts share one
    # definition and nothing collides with a neighbouring row.
    uid = hashlib.md5(path.encode()).hexdigest()[:8]
    ramp = "".join(f'<stop offset="{(x(i) - 3) / max(width - 6, 1):.4f}" '
                   f'style="stop-color:var({BAND_INK[trend_band(p["frac"])]})"/>' for i, p in enumerate(points))
    high = min(y(p["frac"]) for p in points)
    fade = (f'<linearGradient id="f{uid}" gradientUnits="userSpaceOnUse" x1="0" y1="{high:.1f}" x2="0" y2="{height - 3}">'
            '<stop offset="0" stop-color="#fff" stop-opacity=".55"/>'
            '<stop offset="1" stop-color="#fff" stop-opacity="0"/></linearGradient>')
    area = (f'{path} L{x(len(points) - 1):.1f} {height - 3} L{x(0):.1f} {height - 3} Z'
            if len(points) > 1 else "")
    defs = (f'<defs><linearGradient id="c{uid}" x1="0" y1="0" x2="1" y2="0">{ramp}</linearGradient>{fade}'
            f'<mask id="m{uid}"><rect width="{width}" height="{height}" fill="url(#f{uid})"/></mask></defs>')
    wash = f'<path d="{area}" fill="url(#c{uid})" mask="url(#m{uid})" class="sp-area"/>' if area else ""
    # A 2.6 px dot is hard to point at, so each round gets a wide invisible target carrying the
    # reading, with its own dot drawn immediately after it -- the pair lets CSS grow the dot the
    # pointer is over (`circle.sp-hit:hover + circle.sp`) without any script.
    marks = "".join(
        f'<circle cx="{x(i):.1f}" cy="{y(p["frac"]):.1f}" r="7" class="sp-hit">'
        f'<title>round {p["n"]}: {100 * p["frac"]:.2f}% removed'
        + ("" if p["settled"] else " so far (cross-sample; the round is still removing)")
        + f'</title></circle><circle cx="{x(i):.1f}" cy="{y(p["frac"]):.1f}" r="2.6" '
          f'class="sp {trend_band(p["frac"])}' + ('"' if p["settled"] else ' open"') + "/>"
        for i, p in enumerate(points))
    last = points[-1]
    return (f'<svg class="spark" viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
            f'aria-label="removal per round, last {100 * last["frac"]:.2f}%">{defs}{wash}'
            f'<path d="{path}" class="sp-line"/>{marks}</svg>')


SPARK_JS = r"""
// One delegated listener for every sparkline on the page: the point's own <title> is the text,
// so the chart stays the single source of the reading and works without script for a screen
// reader, while a pointer gets it at once instead of after the browser's tooltip delay.
(function(){
  var tip = document.createElement("div"); tip.className = "sp-tip"; document.body.appendChild(tip);
  function place(ev){ var pad = 12, w = tip.offsetWidth, h = tip.offsetHeight;
    var x = ev.clientX + pad, y = ev.clientY - h - pad;
    if (x + w > innerWidth - 4) x = ev.clientX - w - pad;
    if (y < 4) y = ev.clientY + pad;
    tip.style.left = x + "px"; tip.style.top = y + "px"; }
  document.addEventListener("mouseover", function(ev){
    var hit = ev.target.closest && ev.target.closest("circle.sp-hit"); if (!hit) return;
    var t = hit.querySelector("title"); if (!t) return;
    var text = t.textContent, cut = text.indexOf(" so far");
    tip.innerHTML = cut < 0 ? esc(text)
      : esc(text.slice(0, cut)) + '<span class="q">' + esc(text.slice(cut)) + "</span>";
    tip.classList.add("on"); place(ev);
  });
  document.addEventListener("mousemove", function(ev){ if (tip.classList.contains("on")) place(ev); });
  document.addEventListener("mouseout", function(ev){
    if (ev.target.closest && ev.target.closest("circle.sp-hit")) tip.classList.remove("on"); });
  function esc(s){ var d = document.createElement("span"); d.textContent = s; return d.innerHTML; }
})();
"""


def _hero(s_cls: str, s_stage: str, title: str, crumb: str = "", sub: str = "", facts=(), next_: str = "") -> str:
    e = _h.escape
    return (f'<header class="hero">{crumb}<div class="title"><h1>{e(title)}</h1><span class="pill {s_cls}">{e(s_stage)}</span></div>'
            + (f'<div class="sub">{sub}</div>' if sub else "")
            + '<dl class="facts">' + "".join(f"<div><dt>{e(k)}</dt><dd>{v}</dd></div>" for k, v in facts if v) + "</dl>"
            + (f'<p class="next">{next_}</p>' if next_ else "") + "</header>")


EXPLAIN = {
    "rounds": "Each round re-embeds the surviving cells across samples (msp), re-annotates them and zooms into each lineage (zmip), "
              "then removes what the agents judged low quality. The loop stops when a round removes less than 1 % (or fewer than 100 cells).",
    "samples": "OSP runs QC, clustering and a first annotation once per sample; in round 1 an agent decides which samples enter "
               "integration. Excluded and emptied samples stay on disk untouched.",
    "sankey": "Every input cell flows left to right through per-sample QC and each round's msp and zmip step, coloured by coarse label; "
              "a ribbon ending in a red sink is the cells removed at that step. Hover a bar or a ribbon for counts.",
    "umap": "The released embedding with the final coarse and fine labels. Hover a point for its labels; scroll to zoom, drag to pan, "
            "double-click to reset; click a legend entry to isolate one label. The two panels stay in sync.",
    "review": "Everything the agents were unsure about or the host overrode, grouped by category. Nothing here stopped the loop; "
              "removals are irreversible, the rest is advisory.",
    "files": "Where the results live on the server host. The h5ad carries the final labels in obs columns "
             "<code>zmip_ann_coarse</code> and <code>zmip_ann_fine</code>.",
    "units": "One analysis unit is one merged dataset (one species) run through the loop on its own. Open a unit for its rounds, "
             "final UMAP and files.",
}


def _unit_body(unit: Path, s: dict, base: str = "") -> str:
    """Sections of a unit page. `base` prefixes every unit-relative link so the
    same body can sit inline on a one-unit root page ('units/<u>/')."""
    e = _h.escape
    rel = lambda p: base + e(str(p.relative_to(unit)))  # noqa: E731
    done_rounds = [r for r in s["rounds"] if r["stats"]]
    ps = s["persample"]
    items = review.collect(unit, [r["dir"] for r in done_rounds], [r["stats"] for r in done_rounds], s["forced"])
    umap_p = L.release_dir(unit) / "umap.json"
    umap_text = umap_p.read_text(encoding="utf-8") if umap_p.is_file() else None
    n_labels: dict[str, int] = {}
    if umap_text:
        try:
            n_labels = {k: len(v["labels"]) for k, v in json.loads(umap_text)["layers"].items()}
        except (ValueError, TypeError, KeyError, AttributeError):
            pass

    # at a glance
    n_in, n_fin = s["n_input"], s["final_cells"]
    removed_frac = (1 - n_fin / n_in) if n_in and n_fin is not None else None
    total_s = sum((r["stats"].get("elapsed_s") or 0) for r in done_rounds)
    running_round = bool(s["rounds"]) and not s["rounds"][-1]["stats"]
    n_excl = sum(1 for d in s["sample_decisions"].values() if d["decision"] == "exclude")
    glance = [
        _stat(_n(n_in) or "–", "input cells", e(str(s["species"] or ""))),
        _stat(_n(n_fin) or "–", "final cells" if s["released"] else "cells now",
              "" if s["released"] or n_fin is None else "after the last finished round"),
        _stat(_pct(removed_frac) if removed_frac is not None else "–", "removed overall",
              "QC, excluded samples and rounds", "tone-bad" if removed_frac and removed_frac > 0.3 else ""),
        _stat(str(len(done_rounds)) + (" <small>+1 running</small>" if running_round else ""), "rounds",
              f"{fmt_elapsed(total_s)} wall time" if total_s else ""),
        _stat(f'{n_labels.get("coarse", "–")} / {n_labels.get("fine", "–")}', "coarse / fine labels",
              "in the release" if n_labels else "known at release"),
        _stat(f'{ps["n_done"]}/{ps["n"]}' if ps["n"] else "–", "samples done", f"{n_excl} excluded" if n_excl else ""),
        _stat(str(len(items)), "needs review", "items, see below" if items else "nothing so far",
              "tone-warn" if items else ""),
    ]
    parts = ['<div class="glance">' + "".join(glance) + "</div>"]

    jumps = [("files", "Files"), ("rounds", "Rounds"), ("samples", "Samples"), ("sankey", "Cell identity"),
             ("umap", "Final UMAP"), ("review", "Needs review")]
    parts.append('<nav class="jump" aria-label="sections">' + "".join(f'<a href="#{k}">{t}</a>' for k, t in jumps) + "</nav>")

    # files
    files = []
    if s["output_h5ad"] is not None:
        out = s["output_h5ad"]
        files.append(("h5ad", f'<code class="path">{e(str(out))}</code><span class="muted">{e(s["output_note"])}</span> · '
                              f'<a href="{rel(out)}">download</a>'))
    rd = L.release_dir(unit)
    if s["released"]:
        files.append(("release dir", f'<code class="path">{e(str(rd))}</code>'
                      + (' <span class="pill failed">forced at the safety cap</span>' if s["forced"] else "")))
        for fn, what in (("summary.md", "what happened, round by round"), ("needs_review.md", "the review items below, as text"),
                         ("needs_review.json", "same, machine-readable"), ("cell_ledger.csv", "one row per input cell: status and labels per stage"),
                         ("umap.json", "final embedding and labels behind the UMAP panels")):
            if (rd / fn).is_file():
                files.append((fn, f'<a href="{base}{L.RELEASE}/{fn}">{fn}</a> <span class="muted">{what}</span>'))
    parts.append(f'<section class="block" id="files"><h2>Files</h2><p class="lede">{EXPLAIN["files"]}</p>'
                 + ('<dl class="files">' + "".join(f"<dt>{e(k)}</dt><dd>{v}</dd>" for k, v in files) + "</dl>"
                    if files else '<p class="empty">No output yet: the first round has not finished.</p>') + "</section>")

    # rounds
    rows = []
    for r in s["rounds"]:
        rp = rel(r["dir"])
        links = " · ".join(x for x in [
            f'<a href="{rp}/{L.CROSSSAMPLE}/report.html">msp</a>' if r["msp_report"] else "",
            f'<a href="{rp}/{L.ZOOMIN}/report.html">zmip</a>' if r["zmip_report"] else "",
            f'<a href="{rp}/{L.LEDGER}/sankey_coarse.png">sankey</a>' if r["sankey"] else ""] if x)
        st = r["stats"]
        if st:
            dec = r["decision"]
            pill = "failed" if str(st.get("reason", "")).startswith("FORCED") else "released" if dec == "release" else "neutral"
            rows.append(f'<tr><td class="num">{r["n"]}</td><td class="num">{_n(st["n_in"])}</td><td class="num">{_n(st["n_out"])}</td>'
                        f'<td class="num">{_n(st["removed"])}</td><td class="num">{_pct(st["frac"])}{_bar(st["frac"])}</td>'
                        f'<td><span class="pill {pill}">{e(str(dec))}</span></td><td class="reason">{e(str(st.get("reason", "")))}</td>'
                        f'<td class="num">{fmt_elapsed(st.get("elapsed_s"))}</td><td>{links}</td></tr>')
        else:
            rows.append(f'<tr><td class="num">{r["n"]}</td><td class="num">{_n(r.get("n_in"))}</td><td></td><td></td><td></td>'
                        f'<td><span class="pill running">running</span></td><td class="reason st running">{e(str(r["step"]))}</td>'
                        f'<td></td><td>{links}</td></tr>')
    parts.append(f'<section class="block" id="rounds"><h2>Rounds <span class="count">{len(done_rounds)} finished'
                 + (", 1 running" if running_round else "") + f'</span></h2><p class="lede">{EXPLAIN["rounds"]}</p>'
                 + ('<div class="wrap"><table><thead><tr><th class="r">round</th><th class="r">cells in</th><th class="r">cells out</th>'
                    '<th class="r">removed</th><th class="r">removed %</th><th>decision</th><th>reason</th><th class="r">wall time</th><th>reports</th></tr></thead>'
                    f'<tbody>{"".join(rows)}</tbody></table></div>' if rows else '<p class="empty">No round started yet.</p>') + "</section>")

    # per-sample
    prow = []
    for smp in ps["samples"]:
        d = s["sample_decisions"].get(smp["name"]) or s["sample_decisions"].get(smp["value"]) or {}
        link = (f'<a href="{rel(smp["dir"])}/report.html">osp report</a>' if smp["report"]
                else ('<span class="st running">running</span>' if not smp["done"] else ""))
        dec = d.get("decision", "")
        dpill = f'<span class="pill {e(dec)}">{e(dec)}</span>' if dec else '<span class="muted">–</span>'
        if dec == "exclude" and d.get("reason"):  # reason folded behind a red "?" — click opens, click again closes (CSS-only <details>)
            dpill += (f'<details class="why"><summary title="why excluded?" aria-label="why excluded?">?</summary>'
                      f'<div class="why-body"><b>{e(smp["name"])} excluded:</b> {e(d["reason"])}</div></details>')
        status_pill = ('<span class="pill empty-sample" title="OSP QC removed every cell; see qc_removed.csv">empty</span>'
                       if smp.get("empty") else '<span class="pill released">done</span>' if smp["done"]
                       else '<span class="pill running">pending</span>')
        prow.append(f'<tr><td>{e(smp["name"])}</td><td class="num">{_n(smp["n_cells"])}</td>'
                    f'<td>{status_pill}</td><td class="why-cell">{dpill}</td><td>{link}</td></tr>')
    meta = [f'{ps["n_done"]}/{ps["n"]} done']
    if ps["sample_column"]:
        meta.append(f'sample column <code>{e(str(ps["sample_column"]))}</code>')
    if ps.get("n_excluded"):
        meta.append(f'{ps["n_excluded"]:,} cells excluded before OSP by sample-map policy '
                    f'(<a href="{base}{L.PERSAMPLE}/{L.EXCLUDED_CELLS}">excluded_cells.csv</a>)')
    parts.append(f'<section class="block" id="samples"><h2>Samples <span class="count">{" · ".join(meta)}</span></h2>'
                 f'<p class="lede">{EXPLAIN["samples"]}</p>'
                 + ('<div class="wrap"><table><thead><tr><th>sample</th><th class="r">input cells</th><th>osp</th><th>integration</th>'
                    f'<th>report</th></tr></thead><tbody>{"".join(prow)}</tbody></table></div>'
                    if prow else '<p class="empty">Per-sample processing has not started.</p>') + "</section>")

    # sankey + ledger
    last_done = [r for r in s["rounds"] if r["sankey"]]
    if last_done:
        ldir = L.ledger_dir(last_done[-1]["dir"])
        ld = rel(ldir)
        data_p = ldir / "sankey_coarse.json"
        if data_p.is_file():
            data = data_p.read_text().replace("</", "<\\/")
            fig = (f'<div id="sankey-vis" class="wrap"></div><script>const SANKEY_DATA = {data};{SANKEY_JS}</script>'
                   f'<figcaption>Every label is shown, nothing pooled. Static version: <a href="{ld}/sankey_coarse.png">sankey_coarse.png</a>'
                   f' · <a href="{ld}/cell_ledger.csv">cell_ledger.csv</a> has one row per input cell with its status and labels per stage.</figcaption>')
        else:
            fig = (f'<figure><a href="{ld}/sankey_coarse.png"><img src="{ld}/sankey_coarse.png" alt="Sankey diagram of coarse cell labels across steps and rounds"></a>'
                   f'<figcaption><a href="{ld}/cell_ledger.csv">cell_ledger.csv</a> has one row per input cell with its status and labels per stage.</figcaption></figure>')
        parts.append(f'<section class="block" id="sankey"><h2>Cell identity across steps and rounds <span class="count">coarse labels · through round {last_done[-1]["n"]}</span></h2>'
                     f'<p class="lede">{EXPLAIN["sankey"]}</p>{fig}</section>')

    # final UMAP (interactive) — data extracted at release into release/umap.json
    if umap_text is not None:
        # Script elements are raw text even with application/json. Escape '<'
        # so labels and cell IDs cannot terminate the element with </script>.
        data = umap_text.replace("<", "\\u003c")
        parts.append(f'<section class="block" id="umap"><h2>Final UMAP <span class="count">every released cell · coarse and fine labels</span></h2>'
                     f'<p class="lede">{EXPLAIN["umap"]}</p>'
                     f'<div id="umap-vis"><p class="umap-status">loading {base}{e(str(umap_p.relative_to(unit)))}… (JavaScript required)</p><div class="umap-row"></div></div>'
                     f'<script type="application/json" id="umap-data">{data}</script>'
                     f'<script>{UMAP_JS}</script></section>')

    # needs review — from disk, so it exists mid-run too
    cs = review.counts(items)
    brief = " · ".join(f"{n} {t.lower()}" for _, t, n, _ in cs) if cs else "nothing to review"
    parts.append(f'<section class="block" id="review"><h2>Needs review <span class="count">{len(items)} items — {e(brief)}</span></h2>'
                 f'<p class="lede">{EXPLAIN["review"]}' + ("" if s["released"] else " The loop is still running, so this list is still growing.")
                 + "</p>" + review.to_html(items, base) + "</section>")
    return "".join(parts)


def _unit_facts(s: dict) -> list:
    arrow = f'{_n(s["n_input"]) or "–"} → {_n(s["final_cells"]) or "–"}'
    return [("species", _h.escape(str(s["species"] or ""))), ("cells in → out", arrow), ("rounds", str(len(s["rounds"]))),
            ("finished", _h.escape(s["finished"] or "")) if s["released"] else ("last event", _h.escape(s["last_event"]))]


def render_unit(unit: Path, dataset: str | None = None) -> str:
    """One analysis unit; `dataset` is the name it is served under (crumb)."""
    s = unit_state(unit)
    e = _h.escape
    root = L.root_of(unit)
    ds = dataset or (display_name(root) if root else "")
    crumb = f'<div class="crumb"><a href="../../{L.INDEX}">{e(ds)}</a> / {L.UNITS} / {e(s["name"])}</div>' if root else ""
    hero = _hero(s["stage_class"], s["stage"], s["name"], crumb, (f"analysis unit of <b>{e(ds)}</b> · " if ds else "") + f'<code class="path">{e(str(unit))}</code>',
                 _unit_facts(s), "Start with the numbers below, then the final UMAP; Needs review lists what the agents were unsure about.")
    body = _gen2_unit_body(unit, s) if s.get("generation") == 2 else _unit_body(unit, s)
    return _page(f"{s['name']} — {ds} · eca-rsi" if root else f"{s['name']} — eca-rsi unit", unit, hero + body)


# ---------------------------------------------------------------- root page

def render_root(root: Path, name: str | None = None) -> str:
    """A run root; `name` is the name it is served under. With exactly one
    unit its whole content is shown inline, so the page is never a dead end."""
    e = _h.escape
    manifest_path = L.gen2_organize_manifest(root) if L.is_gen2_root(root) else L.organize_manifest(root)
    om = _json(manifest_path, {})
    units = L.units(root)
    states = [unit_state(u) for u in units]
    ds = dataset_state(root, states)
    title = name or display_name(root)
    coll = collection_of(root)
    sub = (f"collection <b>{e(coll)}</b> · " if coll else "") + f'<code class="path">{e(str(root))}</code>'
    arrow = f'{_n(ds["n_input"]) or "–"} → {_n(ds["final_cells"]) or "–"}'
    facts = [("species", e(ds["species"])), ("cells in → out", arrow), ("rounds", str(ds["rounds"])),
             ("finished", e(ds["finished"] or "")) if ds["finished"] else ("last updated", _when(ds["updated"]))]
    if len(units) == 1:
        u, s = units[0], states[0]
        next_ = (f'This run has one analysis unit, <b>{e(u.name)}</b>, shown below in full '
                 f'(<a href="{L.UNITS}/{e(u.name)}/{L.INDEX}">open it on its own page</a>). '
                 "Start with the numbers, then the final UMAP; Needs review lists what the agents were unsure about.")
        inline = _gen2_unit_body if s.get("generation") == 2 else _unit_body
        body = _hero(ds["cls"], ds["stage"], title, "", sub, facts, next_) + inline(u, s, f"{L.UNITS}/{e(u.name)}/")
    else:
        rows = []
        for u, s in zip(units, states):
            rows.append(f'<tr><td><a href="{L.UNITS}/{e(u.name)}/{L.INDEX}"><b>{e(u.name)}</b></a></td>'
                        f'<td>{e(str(s["species"] or ""))}</td><td class="num">{_n(s["n_input"])}</td>'
                        f'<td class="num">{s["persample"]["n_done"]}/{s["persample"]["n"]}</td><td class="num">{len(s["rounds"])}</td>'
                        f'<td class="num">{_n(s["final_cells"])}</td>'
                        f'<td><span class="pill {s["stage_class"]}">{e(s["stage"])}</span></td>'
                        f'<td class="muted">{e(s["last_event"])}</td></tr>')
        next_ = (f'This run has {len(units)} analysis units; open one below for its rounds, final UMAP and files.' if units
                 else "No analysis unit has been planned yet; organize has not finished.")
        body = (_hero(ds["cls"], ds["stage"], title, "", sub, facts, next_)
                + f'<section class="block" id="units"><h2>Units <span class="count">{ds["released"]}/{len(units)} released</span></h2>'
                + f'<p class="lede">{EXPLAIN["units"]}</p>'
                + ('<div class="wrap"><table><thead><tr><th>unit</th><th>species</th><th class="r">input cells</th><th class="r">samples</th>'
                   '<th class="r">rounds</th><th class="r">final cells</th><th>stage</th><th>last event</th></tr></thead>'
                   f'<tbody>{"".join(rows)}</tbody></table></div>' if rows else '<p class="empty">No units yet.</p>') + "</section>")
    extra = []
    if om and om.get("warnings"):
        extra.append('<div class="callout tone-warn"><b>organize warnings</b><ul class="warn">'
                     + "".join(f"<li>{e(w)}</li>" for w in om["warnings"]) + "</ul></div>")
    if om:
        extra.append(f'<p class="muted">Organize plan and cell-conservation audit: <a href="{manifest_path.relative_to(root).as_posix()}">{manifest_path.relative_to(root).as_posix()}</a>'
                     + (f' · input units from eca-pp: {e(", ".join(u["name"] for u in om.get("input_units", [])))}' if om.get("input_units") else "") + "</p>")
    return _page(f"{title} — eca-rsi run", root, body + "".join(extra))


# the files a page is derived from: their newest mtime is "when the run state
# last changed" — and, since ecarsi.mirror copies with mtimes, how fresh a copy is
STATE_GLOBS = (L.PROGRESS, f"{L.UNITS}/*/{L.PROGRESS}", f"{L.ORGANIZE}/{L.MANIFEST}", f"{L.INPUT}/{L.MANIFEST}",
               f"{L.PERSAMPLE}/{L.MANIFEST}", f"{L.PERSAMPLE}/*/{L.RUN_STATE}", f"{L.ROUNDS}/*/{L.MANIFEST}",
               f"{L.ROUNDS}/*/{L.STATS}", f"{L.ROUNDS}/*/{L.DECISION}", f"{L.RELEASE}/summary.json", f"{L.RELEASE}/pruned.json",
               L.GEN2_PUBLICATION, f"{L.UNITS}/*/{L.GEN2_PUBLICATION}", f"{L.UNITS}/*/{L.ROUNDS}/*/{L.GEN2_PUBLICATION}",
               # a round publishes only when it ends; its stages publish as they finish, so a
               # long round still moves the clock
               f"{L.UNITS}/*/{L.ROUNDS}/*/*/{L.GEN2_PUBLICATION}",
               f"{L.UNITS}/*/{L.GEN2_PERSAMPLE}/{L.GEN2_PUBLICATION}", f"{L.RELEASE}/receipt.json",
               f"{L.UNITS}/*/{L.RELEASE}/receipt.json")


def state_mtime(d: Path) -> float | None:
    ts = []
    for g in STATE_GLOBS:
        for p in d.glob(g):
            try:
                ts.append(p.stat().st_mtime)
            except OSError:
                pass  # vanished between glob and stat
    return max(ts) if ts else None


def _page(title: str, where: Path, body: str) -> str:
    import time

    fmt = "%Y-%m-%d %H:%M:%S"
    src = mirror.copy_notice(where)
    origin = f"a mirror copy of {_h.escape(src)}" if src else "the run directory"
    t = state_mtime(where)
    updated = f" · run state updated {time.strftime(fmt, time.localtime(t))}" if t else ""
    return (f'<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>{_h.escape(title)}</title><style>{CSS}</style></head><body><main class=\"page\">{body}"
            f'<footer>rendered {time.strftime(fmt)} from {origin} by ecarsi.index{updated} · reload for the current state</footer></main></body></html>')


# ---------------------------------------------------------------- writers

def write_unit_index(unit: Path) -> Path:
    p = unit / L.INDEX
    if p.is_symlink():
        p.unlink()
    p.write_text(render_unit(unit))
    return p


def write_root_index(root: Path) -> Path:
    p = root / L.INDEX
    if p.is_symlink():
        p.unlink()
    p.write_text(render_root(root))
    return p


def write_all(target: Path) -> list[Path]:
    """Static pages for a unit (and its root, if it has one) or a whole root."""
    written = []
    if L.is_unit(target):
        written.append(write_unit_index(target))
        root = L.root_of(target)
        if root is not None:
            written.append(write_root_index(root))
    elif L.is_root(target):
        for u in L.units(target):
            written.append(write_unit_index(u))
        written.append(write_root_index(target))
    else:
        raise SystemExit(f"{target} is neither an organize root nor a unit dir")
    mirror.sync(target)  # no-op without <root>/mirror.json; a failure is a warning
    return written


def main(argv: list[str]) -> int:
    if len(argv) != 1 or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0 if argv and argv[0] in ("-h", "--help") else 2
    for p in write_all(Path(argv[0]).resolve()):
        print(f"[index] {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

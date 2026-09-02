"""ecarsi.index — landing pages generated from the artefacts on disk.

    python -m ecarsi.index <root | unit>       (re)write the static pages

Nothing here is told what happened: the state of a run is read back from
manifests, contract files, stats/decision files and progress.log, so the
same function renders a finished release and a run that is halfway through
round 2 (ecarsi.serve re-renders on every request, which is what makes
mid-run monitoring possible). Every step also writes the static pages when
it finishes, so a directory that is only copied around still has them.

    <root>/index.html         one row per unit: stage, cells, rounds, links
    <root>/units/<u>/index.html   per-sample reports, rounds table, sankey, needs-review
"""

from __future__ import annotations

import csv
import html as _h
import json
import sys
from pathlib import Path

from . import layout as L
from . import review

CSS = """
:root{--bg:#f4f5f7;--card:#fff;--ink:#1f2328;--muted:#656d76;--line:#e6e8eb;--accent:#3b5bdb;
 --ok:#2f9e44;--ok-bg:#e9f7ec;--warn:#d9480f;--warn-bg:#fff1e6;--bad:#c92a2a;--bad-bg:#fdecec;--gray-bg:#f1f3f5}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,Helvetica,Arial,sans-serif}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
code{font:.88em ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;background:var(--gray-bg);padding:.1em .35em;border-radius:4px}
.page{max-width:1380px;margin:0 auto;padding:1.5rem 1.5rem 4rem}
header.top{display:flex;align-items:flex-end;justify-content:space-between;flex-wrap:wrap;gap:.8rem;margin:.5rem 0 1.2rem}
header.top h1{margin:0;font-size:1.9rem;font-weight:650;letter-spacing:-.01em}
.crumb{color:var(--muted);font-size:.9rem;margin-bottom:.35rem}.crumb a{color:var(--muted)}
.event{color:var(--muted);font-size:.85rem}
.pill{display:inline-block;padding:.22em .75em;border-radius:999px;font-size:.85rem;font-weight:600;white-space:nowrap;vertical-align:middle}
.pill.running{background:var(--warn-bg);color:var(--warn)}.pill.released{background:var(--ok-bg);color:var(--ok)}
.pill.failed{background:var(--bad-bg);color:var(--bad)}.pill.neutral{background:var(--gray-bg);color:var(--muted)}
.pill.include{background:var(--ok-bg);color:var(--ok)}.pill.exclude{background:var(--bad-bg);color:var(--bad)}
.cards{display:flex;flex-wrap:wrap;gap:.8rem;margin:0 0 1.2rem}
.card{flex:1 1 150px;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:.8rem 1rem;box-shadow:0 1px 2px rgba(0,0,0,.04)}
.card .num{display:block;font-size:1.6rem;font-weight:650;line-height:1.15;letter-spacing:-.01em}
.card .lbl{display:block;color:var(--muted);font-size:.82rem;margin-top:.15rem}
.card .sub{display:block;color:var(--muted);font-size:.78rem}
a.card{color:var(--ink)}a.card:hover{text-decoration:none;border-color:var(--accent)}
.review-cards .card{flex:1 1 170px}
.callout{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--accent);border-radius:8px;padding:.7rem 1rem;margin:0 0 1.2rem;font-size:.92rem}
.callout .path{font:.86rem ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;word-break:break-all;background:none;padding:0}
.callout.warn{border-left-color:var(--warn)}.callout.bad{border-left-color:var(--bad)}
nav.jump{display:flex;gap:1.2rem;flex-wrap:wrap;font-size:.9rem;margin:0 0 1.2rem;padding:.55rem 0;border-top:1px solid var(--line);border-bottom:1px solid var(--line)}
section{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:1.1rem 1.4rem 1.3rem;margin:0 0 1.2rem;box-shadow:0 1px 2px rgba(0,0,0,.04)}
section h2{margin:0 0 .6rem;font-size:1.2rem;font-weight:650;display:flex;align-items:baseline;gap:.6rem;flex-wrap:wrap}
section h2 small{font-weight:400;color:var(--muted);font-size:.85rem}
h3{margin:1.6rem 0 .3rem;font-size:1.02rem;font-weight:650;display:flex;align-items:center;gap:.5rem}
h3 .count{background:var(--gray-bg);color:var(--muted);border-radius:999px;padding:.05em .6em;font-size:.8rem;font-weight:600}
h3.kind-convergence .count,h3.kind-removed .count{background:var(--bad-bg);color:var(--bad)}
h3.kind-sample_excluded .count,h3.kind-reassigned .count{background:var(--warn-bg);color:var(--warn)}
.card.kind-convergence .num,.card.kind-removed .num{color:var(--bad)}
.card.kind-sample_excluded .num,.card.kind-reassigned .num{color:var(--warn)}
p.desc{color:var(--muted);font-size:.88rem;margin:0 0 .5rem;max-width:90ch}
.wrap{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:.9rem}
th,td{padding:.45rem .65rem;border-bottom:1px solid var(--line);text-align:right;vertical-align:top}
th{color:var(--muted);font-weight:600;font-size:.78rem;text-transform:uppercase;letter-spacing:.03em;text-align:right;border-bottom:2px solid var(--line)}
th:first-child,td:first-child,th.l,td.l,td.note,td.reason{text-align:left}
tbody tr:hover{background:#fafbfc}
td.num{font-variant-numeric:tabular-nums}
td.note,td.reason{font-size:.85rem;color:#3a3f45;min-width:28ch;max-width:70ch}
table.review th,table.review td{text-align:left}table.review td.c-cells{text-align:right;font-variant-numeric:tabular-nums}
table.review td.c-label{max-width:34ch}
.badge{display:inline-block;padding:.08em .55em;border-radius:999px;font-size:.78rem;font-weight:600}
.conf-high{background:var(--ok-bg);color:var(--ok)}.conf-medium{background:var(--gray-bg);color:var(--muted)}.conf-low{background:var(--warn-bg);color:var(--warn)}
.act-remove{color:var(--bad);font-weight:600}.act{color:var(--ink)}
.bar{display:inline-block;vertical-align:middle;width:70px;height:7px;background:var(--gray-bg);border-radius:4px;margin-left:.5rem;overflow:hidden}
.bar i{display:block;height:100%;background:var(--bad);opacity:.75}
.muted{color:var(--muted)}.running-cell{color:var(--warn);font-weight:600}
figure{margin:0}figure img{max-width:100%;border:1px solid var(--line);border-radius:8px;background:#fff}
figcaption{color:var(--muted);font-size:.85rem;margin-top:.4rem}
p.empty{color:var(--muted)}
footer{color:var(--muted);font-size:.8rem;margin-top:2rem}
ul.warn{margin:.3rem 0 0 1.2rem;padding:0}
.sk-tip{position:absolute;z-index:20;background:#1f2328;color:#fff;font-size:.8rem;line-height:1.35;padding:.45rem .6rem;border-radius:6px;pointer-events:none;max-width:34ch;box-shadow:0 2px 8px rgba(0,0,0,.25)}
.sk-tip .m{color:#b8c0c8}
svg.sk{display:block;max-width:100%}svg.sk .sk-stage{font-size:12px;font-weight:600;fill:#1f2328}
svg.sk .sk-label{font-size:10.5px;fill:#1f2328}svg.sk .sk-label.rm{fill:#7a1f16}
svg.sk .sk-flow{opacity:.45;transition:opacity .12s}svg.sk .sk-node{stroke:#fff;stroke-width:.5;cursor:pointer}
svg.sk.dim .sk-flow{opacity:.07}svg.sk.dim .sk-flow.hi{opacity:.85}
details summary{cursor:pointer;list-style:none}details summary::-webkit-details-marker{display:none}
details summary h2{display:inline-flex;margin:0}details summary h2::before{content:"▸";color:var(--muted);margin-right:.5rem;font-size:.9rem}
details[open] summary h2::before{content:"▾"}details summary .hint{display:block;color:var(--muted);font-size:.85rem;margin:.2rem 0 0 1.4rem}
details[open] summary .hint{display:none}.details-body{margin-top:.8rem}
.umap-row{display:flex;gap:24px;align-items:flex-start;flex-wrap:wrap}
.umap-panel{flex:1 1 360px;min-width:0}.umap-panel h3{margin:.2rem 0 .4rem}
.umap-panel canvas{display:block;border:1px solid var(--line);border-radius:8px;cursor:crosshair;background:#fff;max-width:100%}
.umap-legend{margin-top:.5rem;max-height:230px;overflow-y:auto;font-size:.8rem;border:1px solid var(--line);border-radius:8px;padding:.3rem .2rem;columns:2;column-gap:.4rem}
.umap-leg{display:flex;align-items:center;gap:.4rem;padding:.1rem .45rem;cursor:pointer;border-radius:4px;break-inside:avoid}
.umap-leg:hover{background:var(--gray-bg)}.umap-leg.on{background:var(--gray-bg);font-weight:600}.umap-leg.off{opacity:.45}
.umap-leg i{display:inline-block;width:10px;height:10px;border-radius:3px;flex:none}.umap-leg .lab{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.umap-leg .n{color:var(--muted);font-variant-numeric:tabular-nums}.umap-status{color:var(--muted);font-size:.85rem;margin:0 0 .6rem}
td.why-cell{position:relative;white-space:nowrap}details.why{display:inline-block;margin-left:.35rem;vertical-align:middle}
details.why summary{list-style:none;cursor:pointer;display:inline-flex;align-items:center;justify-content:center;width:1.15rem;height:1.15rem;border-radius:999px;background:var(--bad);color:#fff;font-size:.78rem;font-weight:700;line-height:1}
details.why summary::-webkit-details-marker{display:none}details.why[open] summary{background:#8f1d1d}
.why-body{position:absolute;left:0;top:100%;z-index:15;margin-top:.3rem;width:min(60ch,70vw);white-space:normal;background:#fff;border:1px solid var(--line);border-left:4px solid var(--bad);border-radius:8px;box-shadow:0 4px 14px rgba(0,0,0,.12);padding:.6rem .8rem;font-size:.85rem;color:#3a3f45;text-align:left}
.sk-alt{font-size:.82rem;color:var(--muted);margin-top:.4rem}
@media (max-width:700px){.page{padding:1rem}section{padding:.9rem 1rem}}
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
  const nS = D.stages.length, W = Math.max(el.clientWidth, 700), H = 620, top = 34, bottom = 14;
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
  D.stages.forEach((t, i) => { const x = colX(i) + barW / 2;
    const tx = mk("text", {x, y: 18, "text-anchor": "middle", class: "sk-stage"}); tx.textContent = t; svg.appendChild(tx); });
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
  const minLabel = 0.012 * D.total, last = nS - 1;
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
    if (n.count >= minLabel) { const right = n.stage === last;
      const t = mk("text", {x: right ? x + barW + 6 : x - 6, y: n.y + n.h / 2 + 4, "text-anchor": right ? "start" : "end",
                            class: "sk-label" + (n.removed ? " rm" : "")});
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
const url = host.getAttribute("data-src"), status = host.querySelector(".umap-status"), row = host.querySelector(".umap-row");
const tip = document.createElement("div"); tip.className = "sk-tip"; tip.style.display = "none"; document.body.appendChild(tip);
const esc = s => String(s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const fmt = n => n.toLocaleString();
const GRID = 128, PAD = 12;
let D = null, panels = [], view = {k: 1, tx: 0, ty: 0}, hoverIdx = -1, drag = null, S = 480;
let X, Y, grid = null, raf = 0, lod = false, lodTimer = 0;
fetch(url).then(r => { if (!r.ok) throw new Error(r.status); return r.json(); }).then(d => { D = d; init(); })
  .catch(e => { status.textContent = "could not load " + url + " (" + e + ") — open this page through ecarsi.serve"; });

function rgba(hex){ const h = hex.replace("#", ""); const v = parseInt(h.length === 3 ? h.split("").map(c => c + c).join("") : h, 16);
  return [(v >> 16) & 255, (v >> 8) & 255, v & 255]; }
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
    el.innerHTML = `<h3>${esc(key)} <span class="count">${L.labels.length}</span> <small class="muted">${esc(L.column)}</small></h3><canvas></canvas><div class="umap-legend"></div>`;
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
function layout(){ const n = panels.length || 1; S = Math.max(360, Math.min(600, Math.floor((host.clientWidth - 24 * (n - 1)) / n) - 2));
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
  const r = Math.max(1, Math.round(dpr * Math.max(1.0, Math.min(2.6, Math.sqrt(view.k))) * (D.n > 60000 ? 0.7 : 1)));
  const stride = lod ? Math.max(1, Math.ceil(D.n / 30000)) : 1, idx = P.idx, rgb = P.rgb, dim = 0xffeae6e3; // ABGR little-endian: #e3e6ea
  const pack = c => 0xff000000 | (c[2] << 16) | (c[1] << 8) | c[0];
  const cols = rgb.map(pack);
  const passes = focus === null ? [null] : [false, true];
  for (const want of passes) {
    for (let i = 0; i < D.n; i += stride) { const c = idx[i], isF = focus !== null && c === focus; if (want !== null && isF !== want) continue;
      const x = Math.round(sx(i) * dpr), y = Math.round(sy(i) * dpr); if (x < 0 || y < 0 || x >= W || y >= W) continue;
      const col = focus !== null && !isF ? dim : (c >= 0 ? cols[c] : 0xffbbbbbb);
      const x0 = Math.max(0, x - r + 1), x1 = Math.min(W - 1, x + r - 1), y0 = Math.max(0, y - r + 1), y1 = Math.min(W - 1, y + r - 1);
      for (let yy = y0; yy <= y1; yy++) { let o = yy * W + x0; for (let xx = x0; xx <= x1; xx++) buf[o++] = col; } }
  }
  bctx.putImageData(img, 0, 0); P.baseKey = key;
}
function draw(P){
  renderBase(P);
  const cv = P.cv, ctx = cv.getContext("2d"), dpr = window.devicePixelRatio || 1, L = P.L, focus = P.hi !== null ? P.hi : P.sel;
  ctx.setTransform(1, 0, 0, 1, 0, 0); ctx.drawImage(P.base, 0, 0); ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.font = "600 11px system-ui, sans-serif"; ctx.textAlign = "center"; ctx.lineWidth = 3; ctx.strokeStyle = "rgba(255,255,255,.85)"; ctx.fillStyle = "#1f2328";
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
        contract = L.PS_ANNOTATE_CONTRACT if man.get("annotate", True) else L.PS_CONTRACT
        samples.append({"name": d.name, "value": s["value"], "n_cells": s["n_cells"], "dir": d,
                        "done": L.complete(d, contract), "report": (d / "report.html").is_file()})
    return {"manifest": bool(man), "sample_column": man.get("sample_column"), "species": man.get("species"),
            "samples": samples, "n_done": sum(s["done"] for s in samples), "n": len(samples),
            "done": bool(samples) and all(s["done"] for s in samples)}


def _round_step(rdir: Path) -> str:
    """What a round without a decision is currently doing, from its contracts."""
    cdir, zdir = L.crosssample_dir(rdir), L.zoomin_dir(rdir)
    if not L.complete(cdir, L.MSP_CONTRACT):
        if not (cdir / "integrated.h5ad").is_file():
            return "crosssample · integrate" if (cdir.is_dir() or (rdir / L.ROUND_INPUT).is_file()) else "starting"
        if not (cdir / "inspection_proposal.json").is_file():
            return "crosssample · inspect"
        return "crosssample · annotate"
    if not L.complete(zdir, L.ZMIP_CONTRACT):
        plan = _json(zdir / "zmip_plan.json")
        if not plan:
            return "zoomin · plan"
        zoomed = [ln["name"] for ln in plan["lineages"] if ln["zoom"]]
        done = [n for n in zoomed if L.complete(L.lineage_dir(zdir, n), L.ZMIP_LINEAGE_CONTRACT)]
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
            if (cdir / "integrated.h5ad").is_file():
                r["n_in"] = _n_obs(cdir / "integrated.h5ad")
        out.append(r)
    return out


def unit_state(unit: Path) -> dict:
    """Everything the pages need, read from disk."""
    im = _json(L.input_manifest(unit), {})
    ps = persample_state(unit)
    rounds = rounds_state(unit)
    rel = L.release_dir(unit)
    released = (rel / "summary.md").is_file()
    log = L.read_log(unit)
    last = log[-1] if log else None
    failed = bool(last) and "failed" in last[1]
    if failed:
        stage, cls = f"failed — {last[1]}", "failed"
    elif released and (not rounds or rounds[-1]["decision"] is not None):
        stage, cls = f"released after {len(rounds)} round(s)", "released"
    elif rounds and rounds[-1]["decision"] is None:
        stage, cls = f"round {rounds[-1]['n']} · {rounds[-1]['step']}", "running"
    elif rounds:
        stage, cls = f"round {rounds[-1]['n']} done, next round pending", "running"
    elif ps["manifest"] and not ps["done"]:
        stage, cls = f"persample {ps['n_done']}/{ps['n']} samples", "running"
    elif ps["done"]:
        stage, cls = "persample done, loop not started", "running"
    else:
        stage, cls = "organized, persample not started", "running"
    final_cells = None
    if rounds and rounds[-1]["stats"]:
        final_cells = rounds[-1]["stats"]["n_out"]
    # the h5ad a reader should take: release/final.h5ad once released, else
    # the latest finished round's survivors (still moving while the loop runs)
    output_h5ad, output_note = None, ""
    if released and (rel / "final.h5ad").is_file():
        output_h5ad, output_note = rel / "final.h5ad", "final"
    else:
        done = [r for r in rounds if r["stats"] and (L.zoomin_dir(r["dir"]) / "annotated_zmip.h5ad").is_file()]
        if done:
            output_h5ad = L.zoomin_dir(done[-1]["dir"]) / "annotated_zmip.h5ad"
            output_note = f"latest survivors, round {done[-1]['n']} — not final, the loop is still running"
    dec_rows = {}
    if rounds:
        dec = L.crosssample_dir(rounds[0]["dir"]) / "sample_decisions.csv"
        if dec.is_file():
            with open(dec) as f:
                dec_rows = {r["sample"]: r for r in csv.DictReader(f)}
    return {"name": unit.name, "dir": unit, "n_input": im.get("n_cells"), "species": im.get("species") or ps["species"],
            "persample": ps, "rounds": rounds, "released": released, "stage": stage, "stage_class": cls,
            "last_event": f"{last[0]} {last[1]}" if last else "", "final_cells": final_cells,
            "output_h5ad": output_h5ad, "output_note": output_note,
            "sample_decisions": dec_rows, "forced": _forced(rounds)}


def _forced(rounds: list[dict]) -> bool:
    return bool(rounds) and bool(rounds[-1]["stats"]) and str(rounds[-1]["stats"].get("reason", "")).startswith("FORCED")


# ---------------------------------------------------------------- unit page

def _pct(x) -> str:
    return f"{100 * x:.2f}%"


def _n(x) -> str:
    return "" if x is None or x == "" else f"{int(x):,}"


def _bar(frac: float) -> str:
    return f'<span class="bar" title="{_pct(frac)}"><i style="width:{min(100, 100 * frac):.1f}%"></i></span>'


def _card(num, label, sub="", cls="") -> str:
    return (f'<div class="card {cls}"><span class="num">{num}</span><span class="lbl">{_h.escape(label)}</span>'
            + (f'<span class="sub">{sub}</span>' if sub else "") + "</div>")


def render_unit(unit: Path) -> str:
    s = unit_state(unit)
    e = _h.escape
    root = L.root_of(unit)
    crumb = (f'<div class="crumb"><a href="../../{L.INDEX}">{e(root.name)}</a> / {L.UNITS} / {e(s["name"])}</div>'
             if root else "")
    header = (f'<header class="top"><div>{crumb}<h1>{e(s["name"])} <span class="pill {s["stage_class"]}">{e(s["stage"])}</span></h1></div>'
              + (f'<div class="event">last event · {e(s["last_event"])}</div>' if s["last_event"] else "") + "</header>")

    done_rounds = [r for r in s["rounds"] if r["stats"]]
    total_s = sum((r["stats"].get("elapsed_s") or 0) for r in done_rounds)
    removed_total = sum(r["stats"]["removed"] for r in done_rounds)
    cards = [_card(_n(s["n_input"]), "input cells", e(str(s["species"] or ""))),
             _card(_n(s["final_cells"]) or "–", "final cells" if s["released"] else "cells now",
                   f"−{_n(removed_total)} removed in rounds" if done_rounds else ""),
             _card(f'{s["persample"]["n_done"]}/{s["persample"]["n"]}' if s["persample"]["n"] else "–", "samples (osp)",
                   f'{sum(1 for d in s["sample_decisions"].values() if d["decision"] == "exclude")} excluded'
                   if s["sample_decisions"] else ""),
             _card(str(len(done_rounds)) + ("" if s["released"] else " <small>+1 running</small>" if s["rounds"] and not s["rounds"][-1]["stats"] else ""),
                   "rounds done", f"{fmt_elapsed(total_s)} wall time" if total_s else "")]
    parts = [header, '<div class="cards">' + "".join(cards) + "</div>"]

    if s["output_h5ad"] is not None:
        out = s["output_h5ad"]
        cls = "" if s["released"] else "warn"
        parts.append(f'<div class="callout {cls}"><b>Output h5ad</b> <span class="muted">({e(s["output_note"])})</span><br>'
                     f'<code class="path">{e(str(out))}</code> &nbsp;<a href="{e(str(out.relative_to(unit)))}">download</a>'
                     f'<br><span class="muted">labels: <code>zmip_ann_coarse</code> / <code>zmip_ann_fine</code></span></div>')
    if s["released"]:
        parts.append('<div class="callout"><b>Release</b>' + (' <span class="pill failed">forced at the safety cap</span>' if s["forced"] else "")
                     + f'<br><code class="path">{e(str(L.release_dir(unit)))}</code><br>'
                     f'<a href="{L.RELEASE}/summary.md">summary.md</a> · <a href="{L.RELEASE}/needs_review.md">needs_review.md</a> · '
                     f'<a href="{L.RELEASE}/needs_review.json">needs_review.json</a> · <a href="{L.RELEASE}/cell_ledger.csv">cell_ledger.csv</a></div>')

    jumps = ['<a href="#rounds">Rounds</a>', '<a href="#samples">Samples</a>', '<a href="#sankey">Cell identity</a>',
             '<a href="#umap">Final UMAP</a>', '<a href="#review">Needs review</a>']
    parts.append('<nav class="jump">' + "".join(jumps) + "</nav>")

    # rounds
    rows = []
    for r in s["rounds"]:
        rp = e(str(r["dir"].relative_to(unit)))
        links = " · ".join(x for x in [
            f'<a href="{rp}/{L.CROSSSAMPLE}/report.html">msp</a>' if r["msp_report"] else "",
            f'<a href="{rp}/{L.ZOOMIN}/report.html">zmip</a>' if r["zmip_report"] else "",
            f'<a href="{rp}/{L.LEDGER}/sankey_coarse.png">sankey</a>' if r["sankey"] else ""] if x)
        st = r["stats"]
        if st:
            dec = r["decision"]
            pill = "failed" if str(st.get("reason", "")).startswith("FORCED") else "released" if dec == "release" else "neutral"
            rows.append(f'<tr><td>{r["n"]}</td><td class="num">{_n(st["n_in"])}</td><td class="num">{_n(st["n_out"])}</td>'
                        f'<td class="num">{_n(st["removed"])}</td><td class="num">{_pct(st["frac"])}{_bar(st["frac"])}</td>'
                        f'<td class="l"><span class="pill {pill}">{e(str(dec))}</span></td><td class="reason">{e(str(st.get("reason", "")))}</td>'
                        f'<td class="num">{fmt_elapsed(st.get("elapsed_s"))}</td><td class="l">{links}</td></tr>')
        else:
            rows.append(f'<tr><td>{r["n"]}</td><td class="num">{_n(r.get("n_in"))}</td><td></td><td></td><td></td>'
                        f'<td class="l"><span class="pill running">running</span></td><td class="reason running-cell">{e(str(r["step"]))}</td>'
                        f'<td></td><td class="l">{links}</td></tr>')
    parts.append('<section id="rounds"><h2>Rounds <small>crosssample (msp) → zoomin (zmip), on the survivors each time</small></h2>'
                 + ('<div class="wrap"><table><thead><tr><th>round</th><th>cells in</th><th>cells out</th><th>removed</th>'
                    '<th>removed %</th><th class="l">decision</th><th class="l">reason</th><th>wall time</th><th class="l">reports</th></tr></thead>'
                    f'<tbody>{"".join(rows)}</tbody></table></div>' if rows else '<p class="empty">no round started yet</p>') + "</section>")

    # per-sample
    ps = s["persample"]
    prow = []
    for smp in ps["samples"]:
        d = s["sample_decisions"].get(smp["name"]) or s["sample_decisions"].get(smp["value"]) or {}
        link = (f'<a href="{e(str(smp["dir"].relative_to(unit)))}/report.html">osp report</a>' if smp["report"]
                else ('<span class="running-cell">running</span>' if not smp["done"] else ""))
        dec = d.get("decision", "")
        dpill = f'<span class="pill {dec}">{e(dec)}</span>' if dec else '<span class="muted">–</span>'
        if dec == "exclude" and d.get("reason"):  # reason folded behind a red "?" — click opens, click again closes (CSS-only <details>)
            dpill += (f'<details class="why"><summary title="why excluded?">?</summary>'
                      f'<div class="why-body"><b>{e(smp["name"])} excluded:</b> {e(d["reason"])}</div></details>')
        prow.append(f'<tr><td>{e(smp["name"])}</td><td class="num">{_n(smp["n_cells"])}</td>'
                    f'<td class="l">{"<span class=\"pill released\">done</span>" if smp["done"] else "<span class=\"pill running\">pending</span>"}</td>'
                    f'<td class="l why-cell">{dpill}</td><td class="l">{link}</td></tr>')
    parts.append(f'<section id="samples"><h2>Samples <small>osp runs once per sample · {ps["n_done"]}/{ps["n"]} done'
                 + (f' · sample column <code>{e(str(ps["sample_column"]))}</code>' if ps["sample_column"] else "") + "</small></h2>"
                 + ('<div class="wrap"><table><thead><tr><th>sample</th><th>input cells</th><th class="l">osp</th><th class="l">integration</th>'
                    f'<th class="l">report</th></tr></thead><tbody>{"".join(prow)}</tbody></table></div>'
                    if prow else '<p class="empty">persample has not started</p>') + "</section>")

    # sankey + ledger
    last_done = [r for r in s["rounds"] if r["sankey"]]
    if last_done:
        ldir = L.ledger_dir(last_done[-1]["dir"])
        ld = e(str(ldir.relative_to(unit)))
        data_p = ldir / "sankey_coarse.json"
        if data_p.is_file():
            data = data_p.read_text().replace("</", "<\\/")
            fig = (f'<div id="sankey-vis"></div><script>const SANKEY_DATA = {data};{SANKEY_JS}</script>'
                   f'<p class="sk-alt">Hover a bar or a ribbon for its identity and cell counts (every label shown, nothing pooled). '
                   f'Static version: <a href="{ld}/sankey_coarse.png">sankey_coarse.png</a>.</p>')
        else:
            fig = f'<figure><a href="{ld}/sankey_coarse.png"><img src="{ld}/sankey_coarse.png" alt="Sankey"></a></figure>'
        parts.append(f'<section id="sankey"><h2>Cell identity across steps and rounds <small>coarse labels · through round {last_done[-1]["n"]}</small></h2>'
                     + fig + f'<figcaption>Every input cell flows left to right; cells removed at a stage end in that stage\'s red sink. '
                     f'<a href="{ld}/cell_ledger.csv">cell_ledger.csv</a> — one row per input cell, status + labels per stage.</figcaption></section>')

    # final UMAP (interactive) — data extracted at release into release/umap.json
    umap_p = L.release_dir(unit) / "umap.json"
    if umap_p.is_file():
        rel = e(str(umap_p.relative_to(unit)))
        parts.append('<section id="umap"><h2>Final UMAP <small>every released cell · coarse / fine identity · hover for the cell, click a legend entry to isolate a label</small></h2>'
                     f'<div id="umap-vis" data-src="{rel}"><p class="umap-status">loading {rel}…</p><div class="umap-row"></div></div>'
                     f'<script>{UMAP_JS}</script></section>')

    # needs review — from disk, so it exists mid-run too
    items = review.collect(unit, [r["dir"] for r in done_rounds], [r["stats"] for r in done_rounds], s["forced"])
    cs = review.counts(items)
    brief = (" · ".join(f"{n} {t.lower()}" for _, t, n, _ in cs) if cs else "nothing to review")
    parts.append('<section id="review"><details><summary><h2>Needs review <small>'
                 + f'{len(items)} items — {e(brief)}' + "</small></h2>"
                 + '<span class="hint">' + ("everything the agents were unsure about or the host overrode; nothing here stopped the loop"
                                            if s["released"] else "so far — the loop is still running") + " · click to expand</span></summary>"
                 + '<div class="details-body">' + review.to_html(items) + "</div></details></section>")
    return _page(f"{s['name']} — unit of {root.name} · eca-rsi" if root else f"{s['name']} — eca-rsi unit",
                 "".join(parts))


# ---------------------------------------------------------------- root page

def render_root(root: Path) -> str:
    e = _h.escape
    om = _json(L.organize_manifest(root), {})
    units = L.units(root)
    states = [unit_state(u) for u in units]
    rows = []
    for u, s in zip(units, states):
        rows.append(f'<tr><td><a href="{L.UNITS}/{e(u.name)}/{L.INDEX}"><b>{e(u.name)}</b></a></td>'
                    f'<td class="l">{e(str(s["species"] or ""))}</td><td class="num">{_n(s["n_input"])}</td>'
                    f'<td class="num">{s["persample"]["n_done"]}/{s["persample"]["n"]}</td><td class="num">{len(s["rounds"])}</td>'
                    f'<td class="num">{_n(s["final_cells"])}</td>'
                    f'<td class="l"><span class="pill {s["stage_class"]}">{e(s["stage"])}</span></td>'
                    f'<td class="l muted">{e(s["last_event"])}</td></tr>')
    n_rel = sum(1 for s in states if s["released"])
    header = (f'<header class="top"><div><div class="crumb">ecarsi run</div><h1>{e(root.name)}</h1></div>'
              f'<div class="event">{n_rel}/{len(units)} unit(s) released</div></header>')
    cards = [_card(str(len(om.get("input_units", []))) if om else "–", "input units (eca-pp)",
                   e(", ".join(u["name"] for u in om.get("input_units", []))) if om else ""),
             _card(str(len(units)), "analysis units"),
             _card(_n(sum(s["n_input"] or 0 for s in states)) or "–", "input cells"),
             _card(_n(sum(s["final_cells"] or 0 for s in states if s["released"])) or "–", "released cells")]
    parts = [header, '<div class="cards">' + "".join(cards) + "</div>"]
    if om and om.get("warnings"):
        parts.append('<div class="callout warn"><b>organize warnings</b><ul class="warn">'
                     + "".join(f"<li>{e(w)}</li>" for w in om["warnings"]) + "</ul></div>")
    parts.append('<section><h2>Units <small>one analysis unit = one merged dataset, run independently</small></h2>'
                 + ('<div class="wrap"><table><thead><tr><th>unit</th><th class="l">species</th><th>input cells</th><th>samples</th>'
                    '<th>rounds</th><th>final cells</th><th class="l">stage</th><th class="l">last event</th></tr></thead>'
                    f'<tbody>{"".join(rows)}</tbody></table></div>' if rows else '<p class="empty">no units yet</p>') + "</section>")
    if om:
        parts.append(f'<p class="muted">organize plan and cell-conservation audit: <a href="{L.ORGANIZE}/{L.MANIFEST}">{L.ORGANIZE}/{L.MANIFEST}</a></p>')
    return _page(f"{root.name} — eca-rsi run", "".join(parts))


def _page(title: str, body: str) -> str:
    import time

    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    return (f'<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>{_h.escape(title)}</title><style>{CSS}</style></head><body><div class=\"page\">{body}"
            f'<footer>rendered {stamp} from the run directory by ecarsi.index · reload for the current state</footer></div></body></html>')


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


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

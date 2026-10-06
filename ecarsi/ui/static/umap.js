
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

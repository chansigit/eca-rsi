
(function(){
  const D = HISTORY_DATA, box = document.getElementById("hist"), tip = document.getElementById("hist-tip"),
        nEl = document.getElementById("hist-n"), q = document.getElementById("ds-q"), table = document.getElementById("ds-table");
  if (!D || !box) return;
  const fmtN = v => v >= 1e6 ? (v / 1e6).toFixed(v >= 1e7 ? 0 : 1) + "M" : v >= 1e3 ? Math.round(v / 1e3) + "k" : String(v);
  const fmtT = t => { const d = new Date(t * 1000), p = n => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`; };
  // -- which datasets count: the ones the table filter leaves visible --
  function names(){ if (!table) return Object.keys(D.datasets);
    // One table row per analysis unit, so a dataset with six units is six rows; the cards and
    // the curve count datasets, once each, like the sidebar (17 multi-unit datasets, 2026-09-24).
    return [...new Set([...table.tBodies[0].rows].filter(r => !r.hidden).map(r => decodeURIComponent(r.querySelector("a").getAttribute("href").slice(1, -1))))]; }
  function events(){ const ev = [];
    for (const nm of names()) { const d = D.datasets[nm]; if (!d) continue;
      for (const [t, n] of d.organize) ev.push({t, n, k: "in", nm});
      for (const [t, n] of d.release) ev.push({t, n, k: "rel", nm}); }
    return ev.sort((a, b) => a.t - b.t); }
  // -- state --
  let ev = events(), lo = null, hi = null, drag = null;
  const now = () => Date.now() / 1000;
  function totals(t){ let cin = 0, rel = 0, last = null; const started = new Set(), released = new Set();
    for (const e of ev) { if (e.t > t) break; if (e.k === "in") { cin += e.n; started.add(e.nm); } else { rel += e.n; released.add(e.nm); } last = e; }
    return {cin, rel, din:started.size, drel:released.size, last}; }
  function cards(){
    const rows = names().map(n => D.datasets[n]).filter(Boolean), k = totals(now());
    let pending = 0, undated = 0, releasedInput = 0, releasedOutput = 0; const counts = {};
    for(const d of rows){ counts[d.state] = (counts[d.state] || 0)+1;
      const missing = Math.max(0,d.input_cells-d.organize.reduce((s,e)=>s+e[1],0));
      if(d.awaiting_start) pending += missing; else if(d.state !== 'neutral') undated += missing;
      if(d.state === 'released'){releasedInput += d.input_cells;releasedOutput += d.final_cells;}}
    const values = {datasets:rows.length,'cells-in':k.cin,'cells-released':k.rel,'cells-queued':pending,
      kept:releasedInput ? (100*releasedOutput/releasedInput).toFixed(0)+'%' : '—'};
    for(const el of document.querySelectorAll('.stat[data-stat]')){
      const key=el.dataset.stat, value=key.startsWith('state-') ? counts[key.slice(6)] || 0 : values[key];
      el.querySelector('.v').textContent=typeof value === 'number' ?
        (['cells-in','cells-queued'].includes(key) && value>=10000000 ? (value/1000000).toFixed(2)+' M' : value.toLocaleString('en-US')) : value;}
    const note=document.getElementById('hist-undated'); if(note){note.hidden=!undated;
      note.textContent=undated.toLocaleString('en-US')+' input cells lack a recorded start time and are excluded from Cells in and the time curve.';}
  }
  // Release speed centred on t: cells released in [t - 1.5 d, t + 1.5 d] per day. Near now the
  // window has no future half, so it divides by the days it actually covers and says so.
  function rate3d(t){ const a = t - 1.5 * 86400, b = Math.min(t + 1.5 * 86400, now()), days = (b - a) / 86400;
    let c = 0; for (const e of ev) if (e.k === "rel" && e.t > a && e.t <= b) c += e.n;
    return `3-day release speed <b>${Math.round(c / days).toLocaleString()}</b> cells/day` +
      (days < 2.99 ? ` <span class="m">(only ${days.toFixed(1)} d of window so far)</span>` : ""); }
  // -- drawing --
  const W = 960, H = 200, L = 64, R = 16, T = 14, B = 30;
  let logT = false, showIn = false;
  function draw(){
    cards();
    box.innerHTML = "";
    if (!ev.length) { box.innerHTML = '<p class="empty">nothing to plot — no bound dataset has an organize line in its log</p>'; if (nEl) nEl.textContent = ""; return; }
    const t0 = lo ?? ev[0].t, t1 = hi ?? now(), span = Math.max(t1 - t0, 60);
    // Only the series actually drawn sets the axis: cells in outpaces cells released enough
    // that including a hidden "in" curve here would still flatten the one line left on screen.
    const seriesMax = k => Math.max(...ev.filter(e => e.k === k).map((e, i, a) => a.slice(0, i + 1).reduce((s, x) => s + x.n, 0)), 0);
    const yraw = Math.max(...(showIn ? ["in", "rel"] : ["rel"]).map(seriesMax), 1);
    const nice = [1, 2, 5, 10, 20, 50, 100, 200, 500].map(m => m * Math.pow(10, Math.floor(Math.log10(yraw)) - 1)).find(s => yraw / s <= 6) || yraw / 4;
    const ymax = Math.ceil(yraw / nice) * nice;
    // Log time reads backwards from the right edge: distance is age, so the newest hours get
    // most of the width and a long tail of history compresses instead of squeezing them out.
    // log(1 + age) so that age zero -- the right edge, now -- is a real position, not a pole.
    const lgT = Math.log(1 + span);
    const pos = t => logT ? 1 - Math.log(1 + Math.max(t1 - t, 0)) / lgT : (t - t0) / span;
    const un = f => logT ? t1 + 1 - Math.exp((1 - f) * lgT) : t0 + f * span;
    const x = t => L + pos(Math.min(Math.max(t, t0), t1)) * (W - L - R),
          y = v => T + (1 - v / ymax) * (H - T - B);
    const step = k => { let v = 0, d = `M${x(t0)} ${y(0)}`; for (const e of ev) { if (e.k !== k) continue; if (e.t > t1) break;
        const xx = x(e.t); d += ` H${xx.toFixed(1)}`; v += e.n; d += ` V${y(v).toFixed(1)}`; } return d + ` H${x(t1)}`; };
    const yt = [];
    for (let v = 0; v <= ymax + nice / 2; v += nice) yt.push(v);
    const AGES = [0, 3600, 3 * 3600, 6 * 3600, 12 * 3600, 86400, 2 * 86400, 7 * 86400,
                  14 * 86400, 30 * 86400, 90 * 86400, 365 * 86400];
    const ageLabel = a => a === 0 ? "now" : a < 86400 ? `${Math.round(a / 3600)}h` : `${Math.round(a / 86400)}d`;
    const days = span / 86400, stepS = days > 2 ? 86400 : days > 0.6 ? 6 * 3600 : 3600;
    // a mark every day; a label every day that fits (~40 px each), so long spans thin the labels, not the days
    const every = Math.max(1, Math.ceil(days * 40 / (W - L - R)));
    let xt, xl, xm = [];
    if (logT) { xt = AGES.filter(a => a <= span).map(a => t1 - a); xl = t => ageLabel(Math.round(t1 - t)); }
    else if (stepS >= 86400) {
      // One mark per local midnight: a day is the unit the batch is read in, and epoch multiples
      // of 86400 fall at 17:00 here, not at the day boundary.
      xt = []; const d = new Date(t0 * 1000); d.setHours(0, 0, 0, 0);
      for (; d.getTime() / 1000 <= t1; d.setDate(d.getDate() + 1)) if (d.getTime() / 1000 >= t0) xm.push(d.getTime() / 1000);
      xt = xm.filter((_, i) => (xm.length - 1 - i) % every === 0);   // count from the newest day
      xl = t => { const d = new Date(t * 1000); return `${d.getMonth() + 1}/${d.getDate()}`; }; }
    else { xt = []; for (let t = Math.ceil(t0 / stepS) * stepS; t <= t1; t += stepS) xt.push(t);
           xl = t => { const d = new Date(t * 1000); return `${String(d.getHours()).padStart(2, "0")}:00`; }; }
    box.innerHTML = `<svg class="hist-svg" viewBox="0 0 ${W} ${H}" role="img" aria-label="cells in and released over time">
      ${yt.map(v => `<line class="grid" x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}"/><text class="tick" x="${L - 8}" y="${y(v) + 4}" text-anchor="end">${fmtN(v)}</text>`).join("")}
      ${xm.map(t => `<line class="grid" x1="${x(t)}" x2="${x(t)}" y1="${H - B}" y2="${H - B + 5}"/>`).join("")}
      ${xt.map(t => `<text class="tick" x="${x(t)}" y="${H - B + 18}" text-anchor="middle">${xl(t)}</text>`).join("")}
      ${showIn ? `<path class="ser in" d="${step("in")}"/>` : ""}<path class="ser rel" d="${step("rel")}"/>
      <line class="cross" id="hist-cross" x1="0" x2="0" y1="${T}" y2="${H - B}" style="display:none"/>
      <rect class="zoom" id="hist-zoom" y="${T}" height="${H - T - B}" style="display:none"/>
      <rect class="hit" x="${L}" y="${T}" width="${W - L - R}" height="${H - T - B}" fill="transparent"/></svg>
      <div class="hist-legend">${showIn ? '<span><i class="in"></i>cells in</span>' : ""}<span><i class="rel"></i>cells released</span>${lo || hi ? '<span class="muted">zoomed · double-click to reset</span>' : ""}</div>`;
    if (nEl) { const k = totals(t1); nEl.textContent = `${names().length} datasets · ${k.din} started · ${k.drel} released`; }
    const svg = box.querySelector("svg"), hit = svg.querySelector(".hit"), cross = svg.querySelector("#hist-cross"), zoom = svg.querySelector("#hist-zoom");
    const tAt = ev_ => { const r = svg.getBoundingClientRect(); const px = (ev_.clientX - r.left) / r.width * W; return un((px - L) / (W - L - R)); };
    hit.addEventListener("mousemove", e => {
      const t = Math.min(Math.max(tAt(e), t0), t1), k = totals(t); cross.setAttribute("x1", x(t)); cross.setAttribute("x2", x(t)); cross.style.display = "";
      tip.style.display = "block"; tip.innerHTML = `<b>${fmtT(t)}</b><br>cells in <b>${k.cin.toLocaleString()}</b> · released <b>${k.rel.toLocaleString()}</b>` +
        (k.cin ? ` · released / in ${(100 * k.rel / k.cin).toFixed(0)}%` : "") + `<br>${rate3d(t)}<br><span class="m">${k.din} started · ${k.drel} released</span>` +
        (k.last ? `<br><span class="m">last: ${k.last.nm} ${k.last.k === "in" ? "started" : "released"} +${k.last.n.toLocaleString()} at ${fmtT(k.last.t).slice(5)}</span>` : "");
      // tip is a sibling of box, not a descendant, so it has no positioned ancestor to be
      // "relative to box" against -- clientX/Y minus box's rect was landing near the top of
      // the *document* instead of near the cursor. Page coordinates, and above the cursor
      // (falls below only if the viewport has no room above), like the sibling sk-tips in index.py.
      const pad = 10;
      let left = e.pageX - tip.offsetWidth / 2, top = e.pageY - tip.offsetHeight - pad;
      left = Math.max(window.scrollX + 4, Math.min(left, window.scrollX + window.innerWidth - tip.offsetWidth - 4));
      if (top < window.scrollY + 4) top = e.pageY + pad;
      tip.style.left = left + "px"; tip.style.top = top + "px";
      if (drag !== null) { const a = Math.min(x(drag), x(t)), b = Math.max(x(drag), x(t)); zoom.setAttribute("x", a); zoom.setAttribute("width", b - a); zoom.style.display = ""; } });
    hit.addEventListener("mouseleave", () => { tip.style.display = "none"; cross.style.display = "none"; });
    hit.addEventListener("mousedown", e => { drag = tAt(e); e.preventDefault(); });
    hit.addEventListener("mouseup", e => { if (drag === null) return; const t = tAt(e); if (Math.abs(t - drag) > span / 100) { lo = Math.min(drag, t); hi = Math.max(drag, t); draw(); } drag = null; });
    svg.addEventListener("dblclick", () => { lo = hi = null; setRange(0); draw(); });
  }
  const buttons = [...document.querySelectorAll(".hist-range button")];
  function setRange(days){ buttons.forEach(b => b.classList.toggle("on", Number(b.dataset.r) === days)); }
  buttons.forEach(b => b.addEventListener("click", () => { const d = Number(b.dataset.r); lo = d ? now() - d * 86400 : null; hi = null; setRange(d); draw(); }));
  const showInBtn = document.getElementById("hist-show-in");
  if (showInBtn) showInBtn.addEventListener("click", () => { showIn = !showIn;
    showInBtn.classList.toggle("on", showIn); showInBtn.setAttribute("aria-pressed", String(showIn)); draw(); });
  const logBtn = document.getElementById("hist-log");
  if (logBtn) logBtn.addEventListener("click", () => { logT = !logT;
    logBtn.classList.toggle("on", logT); logBtn.setAttribute("aria-pressed", String(logT)); draw(); });
  const more = document.getElementById("hist-more"), detail = document.getElementById("hist-detail");
  if (more && detail) more.addEventListener("click", ev => { ev.preventDefault();
    detail.hidden = !detail.hidden; more.textContent = detail.hidden ? "What is counted?" : "Hide"; });
  if (q) q.addEventListener("input", () => { ev = events(); draw(); });
  draw();
})();

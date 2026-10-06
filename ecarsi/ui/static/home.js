
(function(){
  const q = document.getElementById("ds-q"), table = document.getElementById("ds-table"), n = document.getElementById("ds-n");
  if (!q || !table) return;
  const body = table.tBodies[0], rows = [...body.rows];
  function filter(){ const t = q.value.trim().toLowerCase(); let k = 0;
    for (const r of rows) { const hit = !t || r.dataset.text.includes(t); r.hidden = !hit; k += hit; }
    n.textContent = (t ? k + " of " + rows.length : rows.length) + " units"; }
  q.addEventListener("input", filter); filter();
  // sortable columns: click a header; numbers start descending, text ascending; click again to flip
  const ths = [...table.tHead.rows[0].cells]; let col = -1, asc = true;
  ths.forEach((th, i) => { const b = th.querySelector("button"); if (!b) return;
    b.addEventListener("click", () => {
      const num = th.hasAttribute("data-num");
      asc = col === i ? !asc : !num; col = i;
      const key = r => { const c = r.cells[i], v = c.dataset.v !== undefined ? c.dataset.v : c.textContent.trim(); return num ? (Number(v) || 0) : v.toLowerCase(); };
      rows.sort((a, b) => { const x = key(a), y = key(b); return (x < y ? -1 : x > y ? 1 : 0) * (asc ? 1 : -1); });
      for (const r of rows) body.appendChild(r);
      ths.forEach((h, j) => h.setAttribute("aria-sort", j === i ? (asc ? "ascending" : "descending") : "none")); }); });
  // default order: most recently changed first (a numeric column's first click sorts descending)
  const lu = ths.findIndex(h => /last updated/i.test(h.textContent));
  if (lu >= 0) ths[lu].querySelector("button").click();
})();

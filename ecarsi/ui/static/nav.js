
(function(){
  const $ = id => document.getElementById(id);
  const items = [...document.querySelectorAll("#sb-list .item")], frame = $("frame"), crumb = $("crumb"), open = $("open"),
        q = $("nav-q"), n = $("nav-n"), msg = $("nav-msg"), empty = $("empty"), home = $("home-item"), models = $("models-item"), control = $("control-item"),
        sort = $("nav-sort"), sp = $("nav-sp"), st = $("nav-st"), groups = [...document.querySelectorAll("#sb-list details.group")];
  const names = new Set(items.map(i => i.dataset.name));
  // -- sidebar <-> main pane --
  function mark(name){ items.forEach(i => i.classList.toggle("active", i.dataset.name === name));
    if (home) home.classList.toggle("active", name === "__home__");
    if (control) control.classList.toggle("active", name === "_control");
    if (models) models.classList.toggle("active", name === "_model_pool_panel");
    const cur = items.find(i => i.dataset.name === name); if (cur) { const g = cur.closest("details.group"); if (g) g.open = true; } }
  // What the sidebar knows is enough to name the page before the server has rendered it: the
  // title, the status and the counts are already in the DOM. The rest is drawn as bars until
  // the real page arrives, so a click answers immediately instead of holding the old dataset.
  const pending = $("pending");
  function placeholder(path){
    if (!pending) return;
    const m = path.match(/^\/([^/]+)\//), item = m && items.find(i => i.dataset.name === m[1]);
    const title = path === "/_home" ? "overview" : path === "/_control/" ? "operations"
                : item ? item.querySelector(".nm").textContent : decodeURIComponent(path);
    const dot = item && item.querySelector(".dot"), cells = item && item.querySelector(".cells");
    pending.querySelector("h1").textContent = title;
    const pill = pending.querySelector(".pill");
    pill.textContent = dot ? dot.getAttribute("title") || "" : "";
    pill.className = "pill " + (item ? item.dataset.cls : "neutral");
    pill.hidden = !pill.textContent;
    pending.querySelector(".facts").innerHTML =
      (item && item.dataset.species ? "<div><dt>species</dt><dd>" + item.dataset.species + "</dd></div>" : "")
      + (cells && cells.textContent ? "<div><dt>cells</dt><dd>" + cells.textContent + "</dd></div>" : "");
    frame.style.display = "none"; pending.hidden = false;
  }
  function show(path){ window.modelMonitor.close(); open.hidden = false; if (empty) empty.style.display = "none";
    const m = path.match(/^\/([^/]+)\//); if (m) mark(m[1] === "_home" ? "__home__" : m[1]);
    crumb.textContent = path === "/_home" ? "overview" : path === "/_control/" ? "operations" : decodeURIComponent(path);
    if (frameUrl() !== path) { placeholder(path); frame.src = path; } else frame.dispatchEvent(new Event('load')); }
  function frameUrl(){ try { return frame.contentWindow.location.pathname; } catch (e) { return null; } }
  function fromHash(){
    const h = location.hash.replace(/^#/, "");
    if (h === "/__home__") return "/_home";
    if (h === "/_control/") return control ? "/_control/" : null;
    const m = h.match(/^\/([^/]+)\/(.*)$/); return m && names.has(m[1]) ? "/" + m[1] + "/" + m[2] : null; }
  frame.addEventListener("load", () => {
    if (pending) pending.hidden = true;
    if (!window.modelMonitor.isOpen()) frame.style.display = "";
    const p = frameUrl(); if (!p || window.modelMonitor.isOpen()) return;
    if (p === "/_home") {
      if (location.hash !== "#/__home__") history.replaceState(null, "", "#/__home__");
      mark("__home__"); crumb.textContent = "overview"; open.href = "/_home";
      try { document.title = frame.contentDocument.title || "Periscope"; } catch (e) {}
      return;
    }
    const m = p.match(/^\/([^/]+)\//); if (!m) return;
    if (location.hash !== "#" + p) history.replaceState(null, "", "#" + p);
    mark(m[1]); crumb.textContent = p === "/_control/" ? "operations" : decodeURIComponent(p); open.href = p;
    try { document.title = frame.contentDocument.title || "Periscope"; } catch (e) {}
  });
  window.addEventListener("hashchange", () => { const p = fromHash(); if (p) show(p); });
  items.forEach(i => i.addEventListener("click", ev => { if (ev.target.closest("input.sel")) return; ev.preventDefault(); show("/" + i.dataset.name + "/"); }));
  if (home) home.addEventListener("click", ev => { ev.preventDefault(); show("/_home"); });
  if (control) control.addEventListener("click", ev => { ev.preventDefault(); show("/_control/"); });
  async function showMonitor(monitor, name, title){
    if(!await monitor.open())return;
    frame.style.display="none";if(empty)empty.style.display="none";
    mark(name);crumb.textContent=title;open.hidden=true;document.title="Periscope";
    history.replaceState(null,"","#/__home__");
    if(matchMedia('(max-width:760px)').matches)document.body.classList.add('sb-hidden');
  }
  models.addEventListener('click',()=>showMonitor(window.modelMonitor,'_model_pool_panel','Agent Bridge'));
  const brand = $("brand"); if (brand) brand.addEventListener("click", ev => { ev.preventDefault(); show("/_home"); });
  $("sb-toggle").addEventListener("click", () => document.body.classList.toggle("sb-hidden"));
  $("sb-show").addEventListener("click", () => document.body.classList.remove("sb-hidden"));
  $("reload").addEventListener("click", () => { if(window.modelMonitor.isOpen()){models.click();return;} location.reload(); });
  // -- search + species filter (groups start collapsed; a group folds away when none of its
  //    datasets match and opens while a filter is active) --
  function apply(){ const t = q.value.trim().toLowerCase(), s = sp ? sp.value : "", w = st ? st.value : ""; let k = 0;
    const okw = c => !w || c === w;
    for (const i of items) { const hit = (!t || i.dataset.text.includes(t)) && (!s || i.dataset.species === s) && okw(i.dataset.cls); i.style.display = hit ? "" : "none"; k += hit; }
    for (const g of groups) { const any = [...g.querySelectorAll(".item")].some(i => i.style.display !== "none"); g.style.display = any ? "" : "none"; if ((t || s || w) && any) g.open = true; }
    n.textContent = (t || s || w) ? `${k} / ${items.length}` : `${items.length}`; }
  q.addEventListener("input", apply); if (sp) sp.addEventListener("change", apply); if (st) st.addEventListener("change", apply); apply();
  // -- sort (name / cells / status), within each collection --
  const STATUS_RANK = {released: 0, running: 1, queued: 2, paused: 3, neutral: 4, failed: 5};
  function applySort(){
    const mode = sort ? sort.value : "name";
    const sorted = [...items].sort((a, b) => {
      if (mode === "cells") return (Number(b.dataset.cells) || 0) - (Number(a.dataset.cells) || 0);
      if (mode === "status") {
        const r = (STATUS_RANK[a.dataset.cls] ?? 9) - (STATUS_RANK[b.dataset.cls] ?? 9);
        return r !== 0 ? r : a.dataset.name.localeCompare(b.dataset.name);
      }
      return a.dataset.name.localeCompare(b.dataset.name);
    });
    for (const i of sorted) i.parentElement.appendChild(i);
    try { localStorage.setItem("ecarsi.serve.sort", mode); } catch (e) {}
  }
  if (sort) {
    try { const s = localStorage.getItem("ecarsi.serve.sort"); if (s) sort.value = s; } catch (e) {}
    sort.addEventListener("change", applySort);
    applySort();
  }
  // -- draggable sidebar width --
  const sbEl = $("sb"), resizer = $("sb-resizer");
  function setWidth(px){ px = Math.max(240, Math.min(720, px)); sbEl.style.flexBasis = px + "px"; sbEl.style.width = px + "px"; }
  try { const w = localStorage.getItem("ecarsi.serve.sbWidth"); if (w) setWidth(parseInt(w, 10)); } catch (e) {}
  if (resizer) {
    let dragging = false;
    resizer.addEventListener("mousedown", ev => {
      dragging = true; document.body.style.cursor = "col-resize"; document.body.style.userSelect = "none";
      // the iframe is a separate document — once the cursor crosses into it,
      // window-level mousemove/mouseup here stop firing entirely; disabling
      // its pointer events for the drag keeps the parent document capturing
      frame.style.pointerEvents = "none";
      ev.preventDefault();
    });
    window.addEventListener("mousemove", ev => { if (!dragging) return; setWidth(ev.clientX); });
    window.addEventListener("mouseup", () => {
      if (!dragging) return;
      dragging = false; document.body.style.cursor = ""; document.body.style.userSelect = ""; frame.style.pointerEvents = "";
      try { localStorage.setItem("ecarsi.serve.sbWidth", parseInt(sbEl.style.width, 10)); } catch (e) {}
    });
  }
  // -- bind / unbind (edit the registry file through the server) --
  function say(text, bad){ msg.textContent = text; msg.className = "callout" + (bad ? " tone-bad" : ""); msg.style.display = "block"; }
  async function post(url, body){
    const r = await fetch(url, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
    let j; try { j = await r.json(); } catch (e) { j = {ok: false, error: r.status + " " + r.statusText}; }
    if (r.status === 403) j.error = j.error || "admin actions are refused through the public tunnel unless the server was started with --auth";
    return j; }
  const form = $("bind-form");
  $("bind-open").addEventListener("click", () => { form.style.display = form.style.display === "none" ? "" : "none"; if (form.style.display === "") $("bind-path").focus(); });
  $("bind-cancel").addEventListener("click", () => { form.style.display = "none"; });
  $("bind-go").addEventListener("click", async () => {
    const path = $("bind-path").value.trim(), name = $("bind-name").value.trim();
    if (!path) { say("enter a directory path", true); return; }
    $("bind-go").disabled = true;
    const j = await post("/_bind", {path, name: name || null});
    $("bind-go").disabled = false;
    if (j.ok) { location.hash = "#/" + j.name + "/"; location.reload(); } else say("bind failed: " + j.error, true); });
  $("bind-path").addEventListener("keydown", ev => { if (ev.key === "Enter") $("bind-go").click(); });
  const boxes = [...document.querySelectorAll("input.sel")], ub = $("unbind-go"), sb = $("sb");
  function sync(){ const k = boxes.filter(b => b.checked).length; ub.disabled = !k; ub.textContent = k ? `Unbind (${k})` : "Unbind…"; sb.classList.toggle("selecting", k > 0); }
  boxes.forEach(b => b.addEventListener("change", sync));
  ub.addEventListener("click", async () => {
    const sel = boxes.filter(b => b.checked).map(b => b.value);
    if (!sel.length) return;
    if (!confirm("Unbind " + sel.length + " dataset(s)?\n\n" + sel.join("\n") + "\n\n(Only the registry entry is removed; nothing in the run directories is touched.)")) return;
    ub.disabled = true;
    const j = await post("/_unbind", {names: sel});
    if (j.ok) { const cur = fromHash(); if (cur && sel.includes(cur.split("/")[1])) location.hash = ""; location.reload(); }
    else { ub.disabled = false; say("unbind failed: " + j.error, true); } });
  sync();
  // -- initial pane: the address in the hash, else the overview --
  const first = fromHash() || "/_home";
  if (items.length || first === "/_home") show(first); else { frame.style.display = "none"; if (empty) empty.style.display = ""; }
})();

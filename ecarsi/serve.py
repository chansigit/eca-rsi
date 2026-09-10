"""ecarsi.serve — Periscope, a stateless navigator server for eca-rsi run reports.

Periscope is the name of the web UI (page titles, the sidebar brand, the
startup line); the CLI verb stays `serve`.

    ecarsi serve [dir...] [--registry FILE] [--port 8899] [--bind 127.0.0.1]
                 [--ngrok [--domain csj.example.app]] [--auth user:pass]
    ecarsi serve scan-add <dir-or-glob>... [--name N] [--dry-run] [--registry FILE]
    ecarsi serve remove   <name>... [--registry FILE]
    ecarsi serve list     [--json] [--registry FILE]
    ecarsi serve dump     [path] [--registry FILE]
    ecarsi serve reload   <path> [--replace] [--registry FILE]

The server runs in the foreground (Ctrl-C stops it and its ngrok tunnel);
put it in nohup / tmux / an sbatch yourself if you want it in the
background — nothing here manages processes. Every dataset (an organize
root or a single unit, see ecarsi.layout) is served under its own name,
`/<name>/...`; `/` is the navigator (sidebar grouped by collection) opening
on an overview page that lists them all. Landing pages are
rendered from the run directory on every request (ecarsi.index), so a run
that is still going shows its current stage; the server never writes into
a dataset directory.

The single source of truth for what is served is the REGISTRY FILE
(default $XDG_CONFIG_HOME/ecarsi/registry.json, i.e. ~/.config/ecarsi/
registry.json), a JSON object {name: path}. The server re-reads it whenever
its mtime changes, so `scan-add` / `remove` — and the navigator's Bind /
Unbind buttons, which edit the same file — take effect within a request,
without talking to the running process. Kill and restart the server on any
host and the same list comes back. Directories given on the `serve`
command line are served in addition, for this process only.

`dump` copies the registry file elsewhere (or prints it); `reload` merges
another such file into it (`--replace` to swap the whole list) — handy for
keeping several lists, e.g. one per project.

Default: local only (http://127.0.0.1:PORT). --ngrok additionally opens ONE
ngrok tunnel covering everything (ngrok binary + authtoken are the user's
responsibility; so are account limits such as one agent session per free
account). --domain uses a reserved domain instead of a random URL;
--auth USER:PASS puts a password on the whole site (HTTP basic auth,
checked by this server on every request — local, LAN or tunnel; ngrok is
not involved). Default: no password, so day-to-day debugging is prompt-free.
The navigator's Bind / Unbind buttons (POST /_bind, /_unbind) are refused
for requests arriving through the tunnel (ngrok stamps X-Forwarded-For)
unless a password is set; local requests always may.
"""

from __future__ import annotations

import argparse
import base64
import glob as _glob
import gzip
import hmac
import html as _h
import http.server
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from functools import partial
from pathlib import Path

from . import index
from . import layout as L

SUBCOMMANDS = ("scan-add", "remove", "list", "dump", "reload")


def default_registry() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    return base / "ecarsi" / "registry.json"


# ---------------------------------------------------------------- registry


def _check_dataset(path: Path) -> Path:
    path = Path(path)
    if not (L.is_root(path) or L.is_unit(path)):
        raise ValueError(
            f"{path} is neither an organize root nor a unit dir (see ecarsi.layout)"
        )
    return path


class Registry:
    """name -> dataset dir. The registry FILE is the truth; this object is a
    cache of it that re-reads on mtime change and writes through on
    bind/unbind. `extra` are per-process additions (serve's positional
    dirs) that are never written to the file."""

    def __init__(self, path: Path, extra: dict[str, Path] | None = None) -> None:
        self.path = Path(path)
        self._extra = dict(extra or {})
        self._file: dict[str, Path] = {}
        self._stamp: tuple | None = None
        self._lock = threading.Lock()

    # -- file I/O --
    @staticmethod
    def read_file(path: Path) -> dict[str, Path]:
        if not path.is_file():
            return {}
        data = json.loads(path.read_text() or "{}")
        if not isinstance(data, dict):
            raise ValueError(f"{path}: expected a JSON object {{name: path}}")
        return {str(k): Path(v) for k, v in data.items()}

    @staticmethod
    def write_file(path: Path, items: dict[str, Path]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(
            json.dumps({k: str(v) for k, v in sorted(items.items())}, indent=2) + "\n"
        )
        os.replace(
            tmp, path
        )  # atomic: a concurrent server never sees a half-written file

    def _load_if_changed(self) -> None:
        try:
            st = self.path.stat()
            stamp = (st.st_mtime_ns, st.st_size)
        except FileNotFoundError:
            stamp = None
        if stamp == self._stamp:
            return
        try:
            self._file = self.read_file(self.path) if stamp else {}
        except (ValueError, OSError) as e:
            sys.stderr.write(
                f"[serve] registry {self.path} unreadable, keeping last good list: {e}\n"
            )
            return
        self._stamp = stamp

    # -- reads --
    def snapshot(self) -> dict[str, Path]:
        with self._lock:
            self._load_if_changed()
            return {
                **self._file,
                **self._extra,
            }  # this process's own dirs win on a name clash

    def get(self, name: str) -> Path | None:
        return self.snapshot().get(name)

    # -- writes (through to the file) --
    def bind(self, name: str, path: Path, force: bool = False) -> None:
        path = _check_dataset(path)
        if not name or "/" in name:
            raise ValueError("name must be non-empty and contain no '/'")
        with self._lock:
            self._load_if_changed()
            existing = self._file.get(name) or self._extra.get(name)
            if not force and existing is not None and existing != path:
                raise ValueError(
                    f"name {name!r} already bound to {existing} — use --name to disambiguate or remove it first"
                )
            new = dict(self._file)
            new[name] = path
            self.write_file(self.path, new)
            self._file = new
            self._stamp = None  # re-stat next time; our own write changed the mtime

    def unbind(self, names: list[str]) -> None:
        """All-or-nothing, so a typo in one name doesn't half-apply the batch."""
        with self._lock:
            self._load_if_changed()
            missing = [n for n in names if n not in self._file]
            if missing:
                raise ValueError(
                    "nothing bound as "
                    + ", ".join(repr(m) for m in missing)
                    + (
                        ""
                        if not any(n in self._extra for n in missing)
                        else " (given on the serve command line, not in the registry file)"
                    )
                )
            new = {k: v for k, v in self._file.items() if k not in names}
            self.write_file(self.path, new)
            self._file = new
            self._stamp = None


# ---------------------------------------------------------------- navigator

APP = "Periscope"
# The mark: a periscope raised above the waterline — you are outside the cluster looking in.
# Stroke-only and currentColor, so it takes the colour of wherever it is placed and scales with
# the font (see LOGO_CSS). Single-quoted attributes and no '#' so the same string can go straight
# into a data: URI for the favicon without an encoder.
LOGO_SVG = (
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' aria-hidden='true' fill='none' "
    "stroke='currentColor' stroke-width='2.2' stroke-linecap='round' stroke-linejoin='round'>"
    "<path d='M8 18.5V9.5A4.5 4.5 0 0 1 12.5 5h3.8'/><circle cx='18.5' cy='5' r='2.1'/>"
    "<path d='M2.5 21.5c1.3-1.4 2.6-1.4 3.9 0s2.6 1.4 3.9 0 2.6-1.4 3.9 0 2.6 1.4 3.9 0 2.6-1.4 3.9 0'/></svg>"
)
FAVICON = '<link rel="icon" href="data:image/svg+xml,' + LOGO_SVG.replace("currentColor", "rgb(36,86,196)") + '">'
LOGO_CSS = ".logo{display:inline-flex;vertical-align:-.12em;color:var(--accent)}.logo svg{width:1em;height:1em}h1 .logo{margin-right:.3em}"


def logo() -> str:
    return f'<span class="logo">{LOGO_SVG}</span>'


NAV_JS = r"""
(function(){
  const $ = id => document.getElementById(id);
  const items = [...document.querySelectorAll("#sb-list .item")], frame = $("frame"), crumb = $("crumb"), open = $("open"),
        q = $("nav-q"), n = $("nav-n"), msg = $("nav-msg"), empty = $("empty"), home = $("home-item"),
        sort = $("nav-sort"), sp = $("nav-sp"), groups = [...document.querySelectorAll("#sb-list details.group")];
  const names = new Set(items.map(i => i.dataset.name));
  // -- sidebar <-> main pane --
  function mark(name){ items.forEach(i => i.classList.toggle("active", i.dataset.name === name));
    if (home) home.classList.toggle("active", name === "__home__");
    const cur = items.find(i => i.dataset.name === name); if (cur) { const g = cur.closest("details.group"); if (g) g.open = true; } }
  function show(path){ if (empty) empty.style.display = "none"; frame.style.display = ""; if (frameUrl() !== path) frame.src = path; }
  function frameUrl(){ try { return frame.contentWindow.location.pathname; } catch (e) { return null; } }
  function fromHash(){
    const h = location.hash.replace(/^#/, "");
    if (h === "/__home__") return "/_home";
    const m = h.match(/^\/([^/]+)\/(.*)$/); return m && names.has(m[1]) ? "/" + m[1] + "/" + m[2] : null; }
  frame.addEventListener("load", () => {
    const p = frameUrl(); if (!p) return;
    if (p === "/_home") {
      if (location.hash !== "#/__home__") history.replaceState(null, "", "#/__home__");
      mark("__home__"); crumb.textContent = "overview"; open.href = "/_home";
      try { document.title = frame.contentDocument.title || "Periscope"; } catch (e) {}
      return;
    }
    const m = p.match(/^\/([^/]+)\//); if (!m) return;
    if (location.hash !== "#" + p) history.replaceState(null, "", "#" + p);
    mark(m[1]); crumb.textContent = decodeURIComponent(p); open.href = p;
    try { document.title = frame.contentDocument.title || "Periscope"; } catch (e) {}
  });
  window.addEventListener("hashchange", () => { const p = fromHash(); if (p) show(p); });
  items.forEach(i => i.addEventListener("click", ev => { if (ev.target.closest("input.sel")) return; ev.preventDefault(); show("/" + i.dataset.name + "/"); }));
  if (home) home.addEventListener("click", ev => { ev.preventDefault(); show("/_home"); });
  $("sb-toggle").addEventListener("click", () => document.body.classList.toggle("sb-hidden"));
  $("sb-show").addEventListener("click", () => document.body.classList.remove("sb-hidden"));
  $("reload").addEventListener("click", () => { try { frame.contentWindow.location.reload(); } catch (e) { frame.src = frame.src; } });
  // -- search + species filter (groups start collapsed; a group folds away when none of its
  //    datasets match and opens while a filter is active) --
  function apply(){ const t = q.value.trim().toLowerCase(), s = sp ? sp.value : ""; let k = 0;
    for (const i of items) { const hit = (!t || i.dataset.text.includes(t)) && (!s || i.dataset.species === s); i.style.display = hit ? "" : "none"; k += hit; }
    for (const g of groups) { const any = [...g.querySelectorAll(".item")].some(i => i.style.display !== "none"); g.style.display = any ? "" : "none"; if ((t || s) && any) g.open = true; }
    n.textContent = (t || s) ? `${k} / ${items.length}` : `${items.length}`; }
  q.addEventListener("input", apply); if (sp) sp.addEventListener("change", apply); apply();
  // -- sort (name / cells / status), within each collection --
  const STATUS_RANK = {released: 0, running: 1, neutral: 2, failed: 3};
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
"""


def _dataset_state(root: Path) -> dict:
    """Per-dataset summary read from disk (ecarsi.index), for the navigator / list."""
    blank = {"units": 0, "released": 0, "n_input": None, "final_cells": None, "rounds": 0, "species": "",
             "finished": None, "updated": None}
    if not root.is_dir():
        return {**blank, "stage": "missing on disk", "cls": "failed"}
    try:
        return index.dataset_state(root)
    except Exception as e:  # a broken run dir must not take the navigator down
        return {**blank, "stage": f"unreadable: {e}", "cls": "failed"}


class StateCache:
    """dataset_state() of every registered root, refreshed by a background
    thread. The fleet pages (`/`, `/_home`) need all of them: ~90 stat/open
    per dataset, and a cold Lustre metadata op costs ~8 ms on Oak (measured
    2026-09-07: 167 datasets = 15k ops = 0.4 s warm, minutes cold — and the
    mirror writes of running jobs keep invalidating the client cache). So the
    warmer pays that cost off the request path every `ttl` seconds and the
    pages read the last result; dataset / unit pages are still rendered live."""

    def __init__(self, registry: Registry, ttl: float = 60.0):
        self._registry, self._ttl = registry, ttl
        self._states: dict[Path, tuple[float, dict]] = {}
        self._lock = threading.Lock()

    def get(self, root: Path) -> dict:
        with self._lock:
            hit = self._states.get(root)
        if hit and time.time() - hit[0] < 3 * self._ttl:  # warmer alive → never older than ttl; 3x = it died, recompute
            return hit[1]
        return self._put(root)

    def _put(self, root: Path) -> dict:
        st = _dataset_state(root)
        with self._lock:
            self._states[root] = (time.time(), st)
        return st

    def refresh(self) -> None:
        roots = set(self._registry.snapshot().values())
        for root in roots:
            self._put(root)
        with self._lock:
            for gone in set(self._states) - roots:
                del self._states[gone]

    def start(self) -> None:
        def loop():
            while True:
                try:
                    self.refresh()
                except Exception as e:  # keep warming; a request falls back to a live read after 3*ttl
                    sys.stderr.write(f"[serve] state warmer: {e}\n")
                time.sleep(self._ttl)

        threading.Thread(target=loop, daemon=True, name="state-warmer").start()


NAV_CSS = """
html,body{height:100%}body{display:flex;overflow:hidden}
aside.sb{width:360px;flex:0 0 360px;background:var(--card);border-right:1px solid var(--line);display:flex;flex-direction:column;min-width:0;position:relative}
.sb-resizer{position:absolute;top:0;right:-3px;width:6px;height:100%;cursor:col-resize;z-index:6}
.sb-resizer:hover,.sb-resizer:active{background:var(--accent);opacity:.3}
.sb-head{padding:var(--s2) var(--s2) var(--s1);display:flex;flex-direction:column;gap:var(--s1);border-bottom:1px solid var(--line)}
.sb-head .brand{display:flex;align-items:center;justify-content:space-between;gap:var(--s1)}
.sb-head .brand b{font-size:var(--t5)}.sb-head .brand small{color:var(--muted);font-size:var(--t3);font-weight:400;margin-left:.4em}
.sb-head .brand .logo{font-size:var(--t6);margin-right:.35em}
.sb-head input[type=search]{width:100%;font:inherit;font-size:var(--t3);padding:8px 12px;border:1px solid var(--line-strong);border-radius:var(--r);background:var(--card)}
.sb-head .sort-row{display:flex;align-items:center;gap:var(--s1);font-size:var(--t3);color:var(--muted)}
.sb-head select{font:inherit;font-size:var(--t3);padding:4px 8px;border:1px solid var(--line-strong);border-radius:6px;background:var(--card);color:var(--ink)}
.sb-list{flex:1;overflow-y:auto;padding:var(--s1)}
details.group{margin-bottom:4px}details.group>summary{list-style:none;cursor:pointer;display:flex;align-items:center;gap:var(--s1);padding:8px 10px;border-radius:var(--r);font-size:var(--t3);font-weight:650;color:var(--muted)}
details.group>summary::-webkit-details-marker{display:none}details.group>summary::before{content:"";width:0;height:0;border:5px solid transparent;border-left-color:currentColor;margin-right:2px;transition:transform .1s}
details.group[open]>summary::before{transform:rotate(90deg)}details.group>summary:hover{background:var(--none-bg)}
details.group>summary .gn{margin-left:auto;font-weight:400;font-variant-numeric:tabular-nums}
.items{padding-left:var(--s1)}
.item{display:flex;align-items:center;gap:10px;padding:8px 10px;border-radius:var(--r);color:var(--ink);text-decoration:none;font-size:var(--t3)}
.item:hover{background:var(--none-bg)}.item.active{background:var(--accent-bg);color:var(--accent-ink);font-weight:600}
.item .nm{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.item .cells{color:var(--muted);font-variant-numeric:tabular-nums;font-size:var(--t2);white-space:nowrap}
.item input.sel{margin:0;flex:0 0 auto;opacity:0;transition:opacity .1s}
.item:hover input.sel,aside.selecting input.sel,.item input.sel:checked{opacity:1}
.home-item{margin:var(--s1) var(--s2) 0}
.sb-foot{padding:var(--s1) var(--s2) var(--s2);border-top:1px solid var(--line);display:flex;flex-direction:column;gap:var(--s1)}
.sb-foot .row{display:flex;gap:var(--s1)}
.sb-foot .reg{font:var(--t1) var(--mono);color:var(--muted);word-break:break-all}
main.shell{flex:1;display:flex;flex-direction:column;min-width:0;background:var(--bg)}
.mbar{display:flex;align-items:center;gap:var(--s1);padding:6px var(--s2);border-bottom:1px solid var(--line);background:var(--card);font-size:var(--t3);color:var(--muted);min-height:2.75rem}
.mbar #crumb{flex:1;font:var(--t2) var(--mono);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
iframe{flex:1;border:0;width:100%;background:var(--bg)}
#empty{padding:var(--s4);max-width:70ch}
.btn{font:inherit;font-size:var(--t3);font-weight:600;padding:6px 14px;border-radius:var(--r);border:1px solid var(--accent);background:var(--accent);color:var(--card);cursor:pointer}
.btn:disabled{opacity:.45;cursor:default}.btn.danger{background:var(--bad);border-color:var(--bad)}
.btn.plain{background:var(--card);color:var(--ink);border-color:var(--line-strong)}
.icon{background:none;border:0;cursor:pointer;color:var(--muted);font-size:var(--t5);padding:2px 8px;border-radius:6px;line-height:1}.icon:hover{background:var(--none-bg)}
a.icon{text-decoration:none}
#sb-show{display:none}body.sb-hidden aside.sb{display:none}body.sb-hidden #sb-show{display:inline-block}
#bind-form{margin:0}#bind-form input{width:100%;font:var(--t3) var(--mono);padding:6px 10px;border:1px solid var(--line-strong);border-radius:6px;margin:4px 0}
#bind-form p{margin:var(--s1) 0;color:var(--muted)}
@media (max-width:760px){aside.sb{position:fixed;inset:0 auto 0 0;z-index:5;box-shadow:0 0 0 100vw rgba(0,0,0,.35)}}
"""


def group_tally(counts: dict[str, int]) -> str:
    """'12 done · 3 working · 1 failed' for a collection; zero parts are left out.
    `neutral` (bound but not started) counts as working: it is not done and not broken."""
    done = counts.get("released", 0)
    working = counts.get("running", 0) + counts.get("neutral", 0)
    failed = counts.get("failed", 0)
    parts = [f'<span class="st released">{done} done</span>'] if done else []
    if working:
        parts.append(f'<span class="st running">{working} working</span>')
    if failed:
        parts.append(f'<span class="st failed">{failed} failed</span>')
    return " · ".join(parts)


def _navigator_html(items: dict[str, Path], registry_path: Path, state=_dataset_state) -> str:
    """Shell: datasets grouped by collection down the left, the selected
    dataset's own pages (root landing page -> its units -> ...) in an iframe on
    the right. The iframe keeps the address in the hash (#/<name>/...), so
    reload / back / bookmarks land on the same page; `/` opens the overview."""
    e = _h.escape
    groups: dict[str, list[str]] = {}
    tally: dict[str, dict[str, int]] = {}
    species: dict[str, int] = {}
    for name, p in sorted(items.items()):
        st = state(p)
        coll = index.collection_of(p) or "other"
        short = name[len(coll) + 1:] if name.startswith(coll + "-") else name
        cells = index._n(st["final_cells"])
        sp = st.get("species") or ""
        species[sp] = species.get(sp, 0) + 1
        t = tally.setdefault(coll, {})
        t[st["cls"]] = t.get(st["cls"], 0) + 1
        groups.setdefault(coll, []).append(
            f'<a class="item" href="/{e(name)}/" data-name="{e(name)}" title="{e(name)} · {e(st["stage"])} · {e(str(p))}" '
            f'data-cells="{st["final_cells"] or 0}" data-cls="{e(st["cls"])}" data-species="{e(sp)}" '
            f'data-text="{e((name + " " + coll + " " + sp + " " + str(p) + " " + st["stage"]).lower())}">'
            f'<input class="sel" type="checkbox" value="{e(name)}" aria-label="select {e(name)} for unbind">'
            f'<span class="dot {e(st["cls"])}" title="{e(st["stage"])}"></span>'
            f'<span class="nm">{e(short)}</span>'
            + (f'<span class="cells">{cells}</span>' if cells else "") + "</a>"
        )
    rows = "".join(
        f'<details class="group"><summary>{e(coll)}<span class="gn">{group_tally(tally[coll])}</span></summary><div class="items">{"".join(rs)}</div></details>'
        for coll, rs in sorted(groups.items())
    )
    sp_options = "".join(
        f'<option value="{e(sp)}">{e(sp or "unknown")} ({k})</option>' for sp, k in sorted(species.items(), key=lambda kv: (kv[0] == "", kv[0]))
    )
    hint = (
        "A bindable directory is an eca-rsi <b>organize root</b> (contains <code>organize/manifest.json</code> or a "
        "<code>units/</code> dir — e.g. <code>&lt;dataset&gt;/rsi</code>, the <code>&lt;root&gt;</code> you gave "
        "<code>eca-rsi run</code>) or a single <b>unit</b> (contains <code>input/organized.h5ad</code> or "
        "<code>input/manifest.json</code> — e.g. <code>&lt;root&gt;/units/&lt;unit&gt;</code>). "
        "Absolute path on the server host; a raw eca-pp <code>standardize/</code> dir or a bare h5ad is not bindable."
    )
    empty_note = '<p class="muted" style="padding:8px">nothing bound yet</p>'
    sidebar = (
        '<aside class="sb" id="sb" aria-label="datasets"><div class="sb-resizer" id="sb-resizer" title="drag to resize"></div>'
        '<div class="sb-head">'
        f'<div class="brand"><span>{logo()}<b>{APP}</b><small><span id="nav-n">{len(items)}</span> datasets</small></span>'
        '<button class="icon" id="sb-toggle" title="hide sidebar" aria-label="hide sidebar">&#9776;</button></div>'
        '<input id="nav-q" type="search" placeholder="Filter datasets…" aria-label="filter datasets" autocomplete="off">'
        '<div class="sort-row"><label for="nav-sort">sort</label><select id="nav-sort">'
        '<option value="name">name</option><option value="cells">cells</option>'
        '<option value="status">status</option></select>'
        f'<label for="nav-sp">species</label><select id="nav-sp"><option value="">all</option>{sp_options}</select></div>'
        "</div>"
        '<a class="item home-item" id="home-item" href="/_home" data-name="__home__">'
        '<span class="nm"><b>Overview</b> · all datasets</span></a>'
        f'<div class="sb-list" id="sb-list">{rows or empty_note}</div>'
        '<div class="sb-foot">'
        '<div class="row"><button id="bind-open" class="btn plain">+ Bind…</button><button id="unbind-go" class="btn danger" disabled>Unbind…</button></div>'
        '<div id="bind-form" class="callout" style="display:none"><b>Directory to bind</b>'
        '<input id="bind-path" type="text" placeholder="/oak/…/<dataset>/rsi" aria-label="directory path" autocomplete="off" spellcheck="false">'
        '<input id="bind-name" type="text" placeholder="name (default: directory basename)" aria-label="name" autocomplete="off">'
        f"<p>{hint}</p>"
        '<div class="row"><button id="bind-go" class="btn">Bind</button><button id="bind-cancel" class="btn plain">Cancel</button></div></div>'
        '<div id="nav-msg" class="callout" style="display:none" role="status"></div>'
        f'<div class="reg" title="registry file: bind/unbind edit it; nothing in the run directories is touched">{e(str(registry_path))}</div>'
        "</div></aside>"
    )
    main = (
        '<main class="shell"><div class="mbar"><button class="icon" id="sb-show" title="show sidebar" aria-label="show sidebar">&#9776;</button>'
        '<span id="crumb"></span><button class="icon" id="reload" title="reload page" aria-label="reload page">&#8635;</button>'
        '<a class="icon" id="open" href="/" target="_blank" title="open in a new tab" aria-label="open in a new tab">&#8599;</a></div>'
        '<iframe id="frame" name="frame" title="dataset"></iframe>'
        '<div id="empty" style="display:none"><h2>Nothing bound yet</h2><p>Use <b>+ Bind…</b> in the sidebar or, on the server host, '
        "<code>eca-rsi serve scan-add &lt;dir-or-glob&gt;</code>. The server picks up registry changes on the next request.</p>"
        f"<p>{hint}</p></div></main>"
    )
    return (
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width,initial-scale=1"><title>{APP} · ECA-RSI</title>{FAVICON}'
        f"<style>{index.CSS}{NAV_CSS}{LOGO_CSS}</style></head><body>{sidebar}{main}<script>{NAV_JS}</script></body></html>"
    )


HOME_JS = r"""
(function(){
  const q = document.getElementById("ds-q"), table = document.getElementById("ds-table"), n = document.getElementById("ds-n");
  if (!q || !table) return;
  const body = table.tBodies[0], rows = [...body.rows];
  function filter(){ const t = q.value.trim().toLowerCase(); let k = 0;
    for (const r of rows) { const hit = !t || r.dataset.text.includes(t); r.hidden = !hit; k += hit; }
    n.textContent = (t ? k + " of " + rows.length : rows.length) + " datasets"; }
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
"""


def _home_html(items: dict[str, Path], state=_dataset_state) -> str:
    """Overview: what this site is, fleet numbers, and a filterable, sortable
    table of every dataset. This is the page `/` opens."""
    import time

    e = _h.escape
    states = {name: (state(p), p) for name, p in items.items()}
    by = lambda c: sum(1 for s, _ in states.values() if s["cls"] == c)  # noqa: E731
    cells_in = sum(s["n_input"] or 0 for s, _ in states.values())
    cells_out = sum(s["final_cells"] or 0 for s, _ in states.values() if s["cls"] == "released")
    stats = [(str(len(items)), "datasets", ""), (str(by("released")), "released", "released"),
             (str(by("running")), "running", "running"), (str(by("failed")), "failed", "failed"),
             (index._n(cells_in) or "0", "cells in", ""), (index._n(cells_out) or "0", "cells released", "")]
    stat_html = "".join(f'<div class="stat"><span class="v{" st " + c if c and int(v) else ""}">{e(v)}</span><span class="k">{e(k)}</span></div>'
                        for v, k, c in stats)
    rank = {"released": 0, "running": 1, "neutral": 2, "failed": 3}
    rows = []
    for name, (s, p) in sorted(states.items()):
        coll = index.collection_of(p)
        rows.append(
            f'<tr data-text="{e((name + " " + coll + " " + s["species"] + " " + s["stage"]).lower())}">'
            f'<td><a href="/{e(name)}/"><b>{e(name)}</b></a></td><td>{e(coll)}</td><td>{e(s["species"])}</td>'
            f'<td class="num" data-v="{s["n_input"] or 0}">{index._n(s["n_input"])}</td>'
            f'<td class="num" data-v="{s["final_cells"] or 0}">{index._n(s["final_cells"])}</td>'
            f'<td class="num" data-v="{s["rounds"]}">{s["rounds"] or ""}</td>'
            f'<td data-v="{rank.get(s["cls"], 9)}"><span class="pill {e(s["cls"])}">{e(s["stage"])}</span></td>'
            f'<td class="num" data-v="{s["updated"] or 0}">{index._when(s["updated"])}</td></tr>')
    def th(t, num=False):
        attrs = ' class="r" data-num' if num else ""
        return f'<th{attrs} aria-sort="none"><button type="button">{t}</button></th>'
    table = ('<div class="wrap"><table id="ds-table"><thead><tr>' + th("dataset") + th("collection") + th("species")
             + th("cells in", True) + th("cells out", True) + th("rounds", True) + th("status", True) + th("last updated", True)
             + f'</tr></thead><tbody>{"".join(rows)}</tbody></table></div>' if rows
             else '<p class="empty">No dataset is bound yet. Use <b>+ Bind…</b> in the sidebar or <code>eca-rsi serve scan-add</code> on the server host.</p>')
    return (
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width,initial-scale=1"><title>{APP} — overview</title>{FAVICON}'
        f'<style>{index.CSS}{LOGO_CSS}</style></head><body><main class="page">'
        f'<header class="hero"><div class="title"><h1>{logo()}{APP}</h1><span class="sub">ECA-RSI runs</span></div>'
        '<p class="sub" style="max-width:80ch;margin-top:8px">Recursive self-improving annotation of single-cell atlases. Each dataset below was '
        "processed per sample (QC, clustering), integrated across samples and annotated in rounds by agents, with low-quality cells removed "
        "until the loop converged. A dataset page shows the numbers, the rounds, the final UMAP with coarse and fine labels, "
        "the cell-identity Sankey, the review items and where the result files live.</p>"
        '<p class="next">Pick a dataset in the table or the sidebar. Green = released, amber = still running, red = failed.</p></header>'
        f'<div class="glance">{stat_html}</div>'
        f'<section class="block" id="datasets"><h2>Datasets <span class="count" id="ds-n">{len(rows)} datasets</span></h2>'
        '<p class="lede">Cells in is the number of cells the run started from; cells out is what the release keeps. Click a column header to sort.</p>'
        '<div class="toolbar"><label for="ds-q">Filter</label><input id="ds-q" type="search" placeholder="name, collection, species, status…" autocomplete="off"></div>'
        f"{table}</section>"
        f'<footer>rendered {time.strftime("%Y-%m-%d %H:%M:%S")} by {APP} (ecarsi serve) from the registry · reload for the current state</footer>'
        f"</main><script>{HOME_JS}</script></body></html>"
    )


# ---------------------------------------------------------------- handler


def _render_index(root: Path, sub: str, name: str | None = None) -> str | None:
    """HTML for a landing page rendered from disk right now, or None if the
    request isn't for one. Never writes into the dataset directory (the
    pipeline steps write their own static index.html for offline use)."""
    parts = [p for p in sub.split("/") if p]
    if parts and parts[-1] == L.INDEX:
        parts = parts[:-1]
    elif parts and not sub.endswith("/"):
        return None  # a file, not a directory landing page
    if L.is_unit(root):
        return index.render_unit(root, name) if not parts else None
    if not parts:
        return index.render_root(root, name)
    if len(parts) == 2 and parts[0] == L.UNITS and L.is_unit(root / L.UNITS / parts[1]):
        return index.render_unit(root / L.UNITS / parts[1], name)
    return None


class Handler(http.server.SimpleHTTPRequestHandler):
    """Multi-tenant static files: first path segment selects a dataset from
    the registry, the rest is served from that directory (self.directory /
    self.path are recomputed per request, which is safe — translate_path
    reads them fresh on every call, not cached from __init__)."""

    def __init__(self, *a, registry: Registry, auth: str | None = None, states: StateCache | None = None, **kw):
        self._registry = registry
        self._auth = auth  # "user:pass" -> HTTP basic auth enforced here, on every request; None = open
        self._state = states.get if states else _dataset_state  # fleet pages: cached states when a warmer runs
        super().__init__(
            *a, **kw
        )  # directory defaults to cwd; do_GET always overrides it before use

    def _authorized(self) -> bool:
        """Web-level password (--auth). Checked by the server itself, so it
        covers local, LAN and tunnel access alike and needs nothing from
        ngrok. Off by default — debugging with a password prompt is a pain."""
        if not self._auth:
            return True
        hdr = self.headers.get("Authorization", "")
        if not hdr.startswith("Basic "):
            return False
        try:
            given = base64.b64decode(hdr[6:].strip()).decode("utf-8", "replace")
        except Exception:
            return False
        return hmac.compare_digest(given, self._auth)

    def _demand_auth(self) -> None:
        self.send_response(401)
        self.send_header(
            "WWW-Authenticate", 'Basic realm="ecarsi serve", charset="UTF-8"'
        )
        self.send_header("Content-Length", "0")
        self.end_headers()

    # -- admin over HTTP (the navigator's Bind / Unbind buttons) --
    # ngrok forwards from 127.0.0.1 too, so the client address can't tell a
    # local request from one arriving through the public tunnel — but ngrok
    # stamps X-Forwarded-For on everything it forwards. Forwarded requests
    # may only administer if the server has a password (--auth; the request
    # has already passed it by the time we get here); local requests always may.
    def _admin_allowed(self) -> bool:
        return bool(self._auth) or not self.headers.get("X-Forwarded-For")

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        if len(body) > 1024 and "gzip" in self.headers.get("Accept-Encoding", ""):
            body = gzip.compress(body, 5)  # rendered pages are 80-450 KB of HTML and compress ~5x; matters through the tunnel
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass  # the visitor reloaded or left while we were rendering; not worth a traceback in the log

    def _json(self, code: int, obj: dict) -> None:
        self._send(code, json.dumps(obj).encode(), "application/json")

    def _html(self, text: str) -> None:
        self._send(200, text.encode("utf-8"), "text/html; charset=utf-8")

    def do_POST(self):
        if not self._authorized():
            return self._demand_auth()
        path = self.path.split("?", 1)[0]
        if path not in ("/_bind", "/_unbind"):
            return self.send_error(404)
        if not self._admin_allowed():
            return self._json(
                403,
                {
                    "ok": False,
                    "error": "admin actions are refused through the public tunnel unless the server was started with --auth",
                },
            )
        try:
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._json(400, {"ok": False, "error": "bad JSON body"})
        try:
            if path == "/_bind":
                raw = str(req.get("path") or "").strip()
                if not raw:
                    raise ValueError("path is required")
                d = Path(raw).expanduser().resolve()
                if not d.is_dir():
                    raise ValueError(f"{d} is not a directory on the server host")
                name = (req.get("name") or d.name).strip()
                self._registry.bind(name, d)
                self.log_message("bind %s -> %s", name, d)
                return self._json(200, {"ok": True, "name": name})
            names = [str(x) for x in (req.get("names") or [])]
            if not names:
                raise ValueError("names is required")
            self._registry.unbind(names)
            self.log_message("unbind %s", ", ".join(names))
            return self._json(200, {"ok": True, "removed": names})
        except (ValueError, OSError) as e:
            return self._json(400, {"ok": False, "error": str(e)})

    def do_GET(self):
        if not self._authorized():
            return self._demand_auth()
        raw = self.path.split("?", 1)[0]
        if raw == "/_home":
            return self._html(_home_html(self._registry.snapshot(), self._state))
        parts = [p for p in raw.split("/") if p]
        if not parts:
            return self._html(
                _navigator_html(self._registry.snapshot(), self._registry.path, self._state)
            )
        name = parts[0]
        root = self._registry.get(name)
        if root is None:
            # `message` (the short arg) lands in the HTTP status line and
            # must be latin-1 — anything fancier (em dash, etc.) belongs in
            # `explain` (the body) instead, or send_error raises and the
            # connection dies with an empty reply, no 404 at all
            return self.send_error(
                404,
                "unknown dataset",
                explain=f"no dataset bound as {name!r}; see the navigator at /",
            )
        if not root.is_dir():
            return self.send_error(
                404,
                "dataset missing",
                explain=f"{root} (bound as {name!r}) is not on disk any more",
            )
        sub = (
            "/"
            + "/".join(parts[1:])
            + ("/" if raw.endswith("/") and len(parts) > 1 else "")
        )
        if not raw.endswith("/") and (
            len(parts) == 1 or (root / sub.lstrip("/")).is_dir()
        ):
            return self._redirect(
                raw + "/"
            )  # ourselves, not SimpleHTTPRequestHandler: its redirect would drop the /<name> prefix
        try:
            page = _render_index(root, sub, name)
        except Exception as e:  # a broken page must not take the server down; fall back to the static file
            sys.stderr.write(f"[serve] live render failed for {name}{sub}: {e}\n")
            page = None
        if page is not None:
            return self._html(page)
        self.directory = str(root)
        self.path = sub
        http.server.SimpleHTTPRequestHandler.do_GET(self)

    def _redirect(self, location: str) -> None:
        self.send_response(301)
        self.send_header("Location", location)
        self.end_headers()

    def log_message(self, format, *args):  # noqa: A002 - matches BaseHTTPRequestHandler's signature
        """Access log on stderr: time, visitor address, request line, user agent.
        Through the ngrok tunnel every request arrives from 127.0.0.1; the
        visitor's own address is the first hop of X-Forwarded-For."""
        headers = getattr(self, "headers", None) or {}
        who = (headers.get("X-Forwarded-For") or self.address_string()).split(",")[0].strip()
        ua = headers.get("User-Agent", "-")
        sys.stderr.write(
            f'[serve] {time.strftime("%Y-%m-%d %H:%M:%S")} {who} {format % args} "{ua}"\n'
        )


# ---------------------------------------------------------------- ngrok


def start_ngrok(port: int, domain: str | None) -> tuple[subprocess.Popen, str]:
    exe = shutil.which("ngrok")
    if not exe:
        raise SystemExit(
            "ngrok not found on PATH — install it and add your authtoken (ngrok config add-authtoken …)"
        )
    cmd = [exe, "http", str(port), "--log", "stdout", "--log-format", "json"]
    if domain:
        cmd += ["--domain", domain]
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )
    url, deadline = None, time.time() + 30
    assert proc.stdout is not None
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            break
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("msg") == "started tunnel" and ev.get("url"):
            url = ev["url"]
            break
        if ev.get("lvl") in ("eror", "crit", "error"):
            proc.terminate()
            raise SystemExit(f"ngrok failed: {ev.get('err') or ev.get('msg')}")
    if url is None:
        proc.terminate()
        raise SystemExit(
            "ngrok did not report a tunnel within 30 s (see its output above)"
        )
    # keep draining so the pipe never fills
    threading.Thread(target=lambda: [None for _ in proc.stdout], daemon=True).start()  # type: ignore[union-attr]
    return proc, url


# ---------------------------------------------------------------- serve (foreground)


def cmd_serve(args: argparse.Namespace) -> int:
    reg_path = Path(args.registry).expanduser().resolve()
    extra: dict[str, Path] = {}
    for d in args.dir:
        p = Path(d).expanduser().resolve()
        try:
            _check_dataset(p)
        except ValueError as e:
            print(f"[serve] {e}")
            return 2
        if p.name in extra and extra[p.name] != p:
            print(
                f"[serve] two command-line dirs both named {p.name!r}: {extra[p.name]} and {p} — put one in the registry under another name"
            )
            return 2
        extra[p.name] = p
    registry = Registry(reg_path, extra)
    items = registry.snapshot()
    states = StateCache(registry)
    states.start()
    httpd = http.server.ThreadingHTTPServer(
        (args.bind, args.port), partial(Handler, registry=registry, auth=args.auth, states=states)
    )
    print(
        f"[serve] {APP} on http://{args.bind}:{args.port}/  ({len(items)} dataset(s); registry {reg_path}"
        + (f", {len(extra)} from the command line)" if extra else ")")
        + (
            f"  [password-protected, user {args.auth.split(':', 1)[0]!r}]"
            if args.auth
            else "  [no password]"
        ),
        flush=True,
    )
    for name, p in sorted(items.items()):
        print(f"  /{name}/  {p}", flush=True)

    tunnel = None
    if args.ngrok or args.domain:
        tunnel, url = start_ngrok(args.port, args.domain)
        print(f"[serve] public: {url}/", flush=True)

    def shutdown(*_):
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
        if tunnel and tunnel.poll() is None:
            tunnel.terminate()
            try:
                tunnel.wait(5)
            except subprocess.TimeoutExpired:
                tunnel.kill()
    print("[serve] stopped", flush=True)
    return 0


# ---------------------------------------------------------------- registry commands

GENERIC_DIR_NAMES = {
    "rsi",
    "root",
    "run",
    "runs",
    "out",
    "output",
    "results",
    "eca-rsi",
    "ecarsi",
    "eca-pp",
    "units",
    "data",
    "sc",
}


def _scan_names(dirs: list[Path], taken: dict[str, Path]) -> dict[Path, str]:
    """Names for a batch of scanned dirs. Path components that carry no
    information (rsi, eca-pp, units, ...) are dropped; each dir starts with
    its last informative component and every dir whose name collides —
    within the batch or with an existing entry for another path — is
    qualified by one more component, symmetrically (Brain across three
    collections becomes mca1.1-Brain / mca2.0-Brain / mca3.0-Brain, not
    Brain / Brain-rsi / eca-pp-Brain)."""
    comps = {
        d: [c for c in d.parts[1:] if c not in GENERIC_DIR_NAMES] or [d.name]
        for d in dirs
    }
    depth = {d: 1 for d in dirs}
    name = lambda d: "-".join(comps[d][-depth[d] :])
    while True:
        by: dict[str, list[Path]] = {}
        for d in dirs:
            by.setdefault(name(d), []).append(d)
        clash = [
            d
            for n, ds in by.items()
            for d in ds
            if len(ds) > 1 or (n in taken and taken[n] != d)
        ]
        clash = [d for d in clash if depth[d] < len(comps[d])]  # can't qualify further
        if not clash:
            return {d: name(d) for d in dirs}
        for d in clash:
            depth[d] += 1


def cmd_scan_add(args: argparse.Namespace) -> int:
    reg = Registry(Path(args.registry).expanduser().resolve())
    matches = sorted(
        {
            Path(m).resolve()
            for pat in args.glob
            for m in _glob.glob(os.path.expandvars(os.path.expanduser(pat)))
        }
    )
    if not matches:
        print("[serve] nothing matched")
        return 1
    taken = reg.snapshot()
    new, skipped = [], []
    for d in matches:
        if not d.is_dir():
            continue
        if not (L.is_root(d) or L.is_unit(d)):
            skipped.append(d)
        elif d not in taken.values():  # already in under some name -> leave it
            new.append(d)
    if args.name:
        if len(new) != 1:
            print(f"[serve] --name needs exactly one new dataset, got {len(new)}")
            return 2
        names = {new[0]: args.name}
    else:
        names = _scan_names(new, taken)
    plan = [(names[d], d) for d in new]
    for d in skipped:
        print(f"  skip   {d}  (not an organize root / unit)")
    if not plan:
        print(
            f"[serve] nothing new to add ({len(matches)} matched, {len(skipped)} skipped, {len(matches) - len(skipped)} already in)"
        )
        return 0
    n_ok = 0
    for name, d in plan:
        if args.dry_run:
            print(f"  would  {name:24s} {d}")
            continue
        try:
            reg.bind(name, d)
            n_ok += 1
            print(f"  added  {name:24s} {d}")
        except (ValueError, OSError) as e:
            print(f"  FAILED {name:24s} {d}  ({e})")
    if args.dry_run:
        print(f"[serve] dry run: {len(plan)} to add, {len(skipped)} skipped")
        return 0
    print(f"[serve] added {n_ok}/{len(plan)}, {len(skipped)} skipped -> {reg.path}")
    return 0 if n_ok == len(plan) else 1


def cmd_remove(args: argparse.Namespace) -> int:
    reg = Registry(Path(args.registry).expanduser().resolve())
    try:
        reg.unbind(args.name)
    except (ValueError, OSError) as e:
        print(f"[serve] remove failed: {e}")
        return 1
    print(f"[serve] removed {', '.join(args.name)} from {reg.path}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    reg = Registry(Path(args.registry).expanduser().resolve())
    items = reg.snapshot()
    if args.json:
        out = {}
        for name, p in sorted(items.items()):
            st = _dataset_state(p)
            out[name] = {"path": str(p), **st}
        print(json.dumps({"registry": str(reg.path), "datasets": out}, indent=2))
        return 0
    print(f"registry {reg.path}" + ("" if items else "  (empty)"))
    for name, p in sorted(items.items()):
        st = _dataset_state(p)
        cells = index._n(st["final_cells"]) or "-"
        print(f"  {name:24s} {st['stage']:18s} {cells:>10s}  {p}")
    return 0


def cmd_dump(args: argparse.Namespace) -> int:
    reg_path = Path(args.registry).expanduser().resolve()
    items = Registry.read_file(reg_path)
    if not args.path:
        print(json.dumps({k: str(v) for k, v in sorted(items.items())}, indent=2))
        return 0
    out = Path(args.path).expanduser().resolve()
    Registry.write_file(out, items)
    print(
        f"[serve] wrote {len(items)} entr{'y' if len(items) == 1 else 'ies'} -> {out}"
    )
    return 0


def cmd_reload(args: argparse.Namespace) -> int:
    reg_path = Path(args.registry).expanduser().resolve()
    src = Path(args.path).expanduser().resolve()
    if not src.is_file():
        print(f"[serve] {src} does not exist")
        return 1
    try:
        incoming = Registry.read_file(src)
    except ValueError as e:
        print(f"[serve] {e}")
        return 1
    current = {} if args.replace else Registry.read_file(reg_path)
    merged = {**current, **incoming}  # entries from the file win on a name clash
    Registry.write_file(reg_path, merged)
    print(
        f"[serve] {'replaced with' if args.replace else 'merged'} {len(incoming)} entr{'y' if len(incoming) == 1 else 'ies'} from {src} -> {reg_path} ({len(merged)} total)"
    )
    return 0


# ---------------------------------------------------------------- cli


def main(argv: list[str]) -> int:
    def registry_arg(p):
        p.add_argument(
            "--registry",
            default=str(default_registry()),
            metavar="FILE",
            help="registry file, JSON {name: path} (default $XDG_CONFIG_HOME/ecarsi/registry.json)",
        )

    if argv and argv[0] in SUBCOMMANDS:
        ap = argparse.ArgumentParser(
            prog="ecarsi.serve",
            description=__doc__,
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        sub = ap.add_subparsers(dest="cmd", required=True)

        p = sub.add_parser(
            "scan-add",
            help="add every organize root / unit matching the globs to the registry",
        )
        p.add_argument(
            "glob",
            nargs="+",
            help="dirs or globs, e.g. '$OAK/data/sc/*/eca-pp/*/rsi' (quote it)",
        )
        p.add_argument(
            "--name",
            default=None,
            help="name for the (single) dataset instead of the derived one",
        )
        p.add_argument(
            "--dry-run",
            action="store_true",
            help="show what would be added, add nothing",
        )
        registry_arg(p)
        p.set_defaults(func=cmd_scan_add)

        p = sub.add_parser("remove", help="remove datasets from the registry by name")
        p.add_argument("name", nargs="+")
        registry_arg(p)
        p.set_defaults(func=cmd_remove)

        p = sub.add_parser(
            "list", help="list the registry, with each dataset's stage and final cells"
        )
        p.add_argument("--json", action="store_true")
        registry_arg(p)
        p.set_defaults(func=cmd_list)

        p = sub.add_parser(
            "dump", help="copy the registry file to PATH (no PATH: print it)"
        )
        p.add_argument("path", nargs="?", default=None)
        registry_arg(p)
        p.set_defaults(func=cmd_dump)

        p = sub.add_parser(
            "reload", help="merge another registry file into the registry"
        )
        p.add_argument("path")
        p.add_argument(
            "--replace",
            action="store_true",
            help="replace the whole list instead of merging",
        )
        registry_arg(p)
        p.set_defaults(func=cmd_reload)

        args = ap.parse_args(argv)
        return args.func(args)

    ap = argparse.ArgumentParser(
        prog="ecarsi.serve",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="registry subcommands: "
        + " | ".join(SUBCOMMANDS)
        + "  (ecarsi serve <sub> --help)",
    )
    ap.add_argument(
        "dir",
        nargs="*",
        help="extra dataset dirs to serve for this process only (name = basename)",
    )
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument(
        "--bind",
        default="127.0.0.1",
        help="default local only; 0.0.0.0 to expose on the LAN",
    )
    ap.add_argument(
        "--ngrok", action="store_true", help="also open an ngrok tunnel to this port"
    )
    ap.add_argument(
        "--domain", default=None, help="reserved ngrok domain (implies --ngrok)"
    )
    ap.add_argument(
        "--auth",
        default=None,
        metavar="USER:PASS",
        help="web-level password (HTTP basic auth, enforced by the server on every request, local or tunnel); default none",
    )
    registry_arg(ap)
    args = ap.parse_args(argv)
    return cmd_serve(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

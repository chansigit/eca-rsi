"""ecarsi.serve — Periscope, a stateless navigator server for eca-rsi run reports.

Periscope is the name of the web UI (page titles, the sidebar brand, the
startup line); the CLI verb stays `serve`.

    ecarsi serve [dir...] [--registry FILE] [--port 8899] [--bind 127.0.0.1]
                 [--ngrok [--domain csj.example.app]] [--auth user:pass]
                 [--control-plane RUN_DIR [--control-pool-root P] [--control-bridge-root B] [--control-temporal-root T]]
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
import hashlib
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
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path

from typing import Callable
from . import index
from .. import layout as L

SUBCOMMANDS = ("scan-add", "remove", "list", "dump", "reload")


def default_registry() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    return base / "ecarsi" / "registry.json"


# ---------------------------------------------------------------- registry


def _check_dataset(path: Path) -> Path:
    path = Path(path)
    if not (L.is_root(path) or L.is_unit(path) or L.is_gen2_unit(path)):
        raise ValueError(
            f"{path} is neither an organize root nor a unit dir (see ecarsi.layout)"
        )
    return path


class Registry:
    """name -> dataset dir. The registry FILE is the truth; this object is a
    cache of it that re-reads on mtime change and writes through on
    bind/unbind. `extra` are per-process additions (serve's positional
    dirs) that are never written to the file."""

    def __init__(self, path: Path, extra: dict[str, Path] | None = None,
                 published: "Callable[[], dict[str, Path]] | None" = None) -> None:
        self.path = Path(path)
        self._extra = dict(extra or {})
        # Runs the control plane publishes about itself (ControlVerdicts.runs): a dataset is on the
        # page from the moment it is submitted, with nobody registering anything. Lowest priority --
        # a name in the file or on the command line keeps its own path.
        self._published = published or (lambda: {})
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
                **self._published(),
                **self._file,
                **self._extra,
            }  # this process's own dirs win on a name clash

    def get(self, name: str) -> Path | None:
        return self.snapshot().get(name)

    def cached_snapshot(self) -> dict[str, Path]:
        # Readers never wait on filesystem I/O under the registry write lock.
        # _file is replaced atomically, not modified in place.
        return {**self._published(), **self._file, **self._extra}

    def start(self) -> None:
        def refresh():
            while True:
                try:
                    self.snapshot()
                except OSError as exc:
                    sys.stderr.write(f'[serve] registry refresh: {exc}\n')
                time.sleep(5)
        threading.Thread(target=refresh, daemon=True, name='registry-warmer').start()

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
"""


def _dataset_state(root: Path) -> dict:
    """Per-dataset summary read from disk (ecarsi.index), for the navigator / list."""
    blank = {"units": 0, "released": 0, "n_input": None, "final_cells": None, "rounds": 0, "species": "",
             "finished": None, "updated": None, "events": {"organize": [], "release": []}}
    if not root.is_dir():
        return {**blank, "stage": "missing on disk", "cls": "failed"}
    try:
        return index.dataset_state(root)
    except Exception as e:  # a broken run dir must not take the navigator down
        return {**blank, "stage": f"unreadable: {e}", "cls": "failed"}


TERMINAL = {"FAILED": ("failed", "failed"), "TERMINATED": ("failed", "terminated"),
            "TIMED_OUT": ("failed", "timed out"), "CANCELED": ("failed", "cancelled"),
            "CANCELLED": ("failed", "cancelled"), "COMPLETED": ("released", "completed")}


class ControlVerdicts:
    """What the control plane says about each run, read from the file it publishes.

    The fleet table is derived from the files stages write, which is as current as the last stage
    boundary and no more: a run that died inside Temporal writing nothing reads as running until the
    twelve-hour staleness rule notices, and a run just resumed reads as failed until its next
    publication. The control plane knows now, so it publishes what it knows and this reads it. The
    file carries the time it was written: a stale publisher is visible rather than silently believed,
    and a missing one simply leaves the disk-derived row alone."""

    FRESH = 120.0     # a publisher that has not written for this long is not speaking for the fleet
    RECENT = 86400.0  # a closed run stays on the page this long, unless registered or superseded

    def __init__(self, path: Path | None):
        self._path, self._mtime, self._data = path, None, {}

    def _load(self) -> None:
        if self._path is None:
            return
        try:
            mtime = self._path.stat().st_mtime
        except OSError:
            self._data = {}
            return
        if mtime == self._mtime:
            return
        try:
            self._data = json.loads(self._path.read_text())
        except (OSError, ValueError):
            return          # a half-written file is never seen (the publisher renames), so this is corruption: keep the last good one
        self._mtime = mtime

    def age(self) -> float | None:
        self._load()
        at = self._data.get("generated_at")
        return None if at is None else max(0.0, time.time() - at)

    def live(self) -> bool:
        age = self.age()
        return age is not None and age <= self.FRESH

    def of(self, run_id: str, unit: str = "") -> dict | None:
        """The verdict on a run, or on one of its units when the control plane names that unit."""
        if not run_id or not self.live():
            return None
        workflows = self._data.get("workflows") or {}
        return workflows.get(f"dataset/{run_id}{unit}")

    def runs(self) -> dict[str, Path]:
        """Every dataset run the control plane owns, by the directory it publishes for it.

        The fleet table used to list only what the registry named, so a dataset the coordinator had
        been running for ten minutes was invisible until someone ran scan-add: the two disagreed
        about what the fleet even was (2026-09-22). The plane publishes each run's output_root from
        the spec it was started with; a directory that exists is a row, named as scan-add would name
        it. Superseded runs whose directories are gone, or a silent publisher, contribute nothing."""
        if not self.live():
            return {}
        datasets = [r for r in (self._data.get("workflows") or {}).values() if r.get("kind") == "DatasetWorkflow"]
        # A run is fleet while it runs, and for a day after it closes -- long enough for a failure
        # to be seen, not long enough for last week's to keep the table. And never once the same
        # dataset has been started again: a resubmission supersedes what it replaced. The first
        # version of this rule bound everything the plane had ever owned and put 31 dead runs back
        # on the page in one refresh, fifteen of them "failed" (2026-09-22).
        newest = {}
        for r in datasets:
            key = r.get("dataset_id")
            if key and r.get("started", 0) > newest.get(key, 0):
                newest[key] = r["started"]
        out = {}
        for r in datasets:
            root = r.get("output_root")
            if not root or not Path(root).is_dir():
                continue
            if r.get("status") != "RUNNING":
                if r.get("started", 0) < newest.get(r.get("dataset_id"), 0):
                    continue    # superseded: the dataset was started again
                # A failure never quietly leaves the table (owner, 2026-09-23: "失败了就是失败了,
                # 不要隐藏问题"): it stays until a resubmission supersedes it. Completed and
                # terminated runs still age off after a day -- the released ones are registered
                # and the killed ones were meant to go.
                if r.get("status") != "FAILED" and time.time() - (r.get("closed") or 0) > self.RECENT:
                    continue
            out[Path(root).name] = Path(root)
        return out


def reconcile(row: dict, verdict: dict | None, precise: bool = True) -> dict:
    """The control plane's verdict wins over what the files imply, and says so.

    Only the verdict changes: the stage text stays, because 'per-sample running' is still what the
    run was last seen doing and the verdict cannot say it. A run the control plane calls finished is
    not running whatever its files suggest, and one it calls running is not failed however old its
    last publication is.

    `precise` is False when the verdict is the whole run's and the row is one unit of it: a finished
    run still has no unit running, but a running run says nothing about the unit that already failed
    inside it, so that direction is left to the files."""
    if not verdict:
        return row
    status = verdict.get("status", "")
    if status == "RUNNING":
        if precise and row["cls"] in {"failed", "paused", "neutral"}:
            return {**row, "cls": "running", "live": "running"}
        return {**row, "live": "running"} if row["cls"] == "running" else row
    cls, word = TERMINAL.get(status, ("", ""))
    if not cls or row["cls"] == cls:
        return {**row, "live": word or status.lower()}
    stage = f"{word} · last seen {row['stage']}" if row["cls"] == "running" else word
    return {**row, "cls": cls, "stage": stage, "live": word}


class StateCache:
    """Fleet requests only read memory, including during a slow storage refresh."""

    def __init__(self, registry: Registry, ttl: float = 60.0, cache_file: Path | None = None):
        self._registry, self._ttl = registry, ttl
        self._states: dict[Path, tuple[float, dict]] = {}
        self._lock = threading.Lock()
        self._cache_file = cache_file
        self._save_lock = threading.Lock()
        self._last_saved = 0
        if cache_file:
            try:
                stored = json.loads(cache_file.read_text())
                self._states = {Path(root): (record[0], record[1]) for root, record in stored.items()}
            except (OSError, ValueError, TypeError, AttributeError, IndexError):
                pass  # a missing/corrupt disposable cache starts with explicit loading states

    def get(self, root: Path) -> dict:
        with self._lock:
            hit = self._states.get(root)
        if hit:
            return {**hit[1], 'cached_at': hit[0]}
        return dict(units=0, released=0, n_input=None, final_cells=None, rounds=0,
                    species='', finished=None, updated=None, events={'organize': [], 'release': []},
                    stage='Loading status', cls='loading', collection='', cached_at=None)

    def _put(self, root: Path) -> dict:
        st = _dataset_state(root)
        st['collection'] = index.collection_of(root)
        with self._lock:
            self._states[root] = (time.time(), st)
        if self._cache_file and time.monotonic()-self._last_saved >= 5 and self._save_lock.acquire(blocking=False):
            try:
                self._last_saved = time.monotonic()
                with self._lock:
                    stored = {str(p): value for p, value in self._states.items()}
                from ..run_state import write_json
                write_json(self._cache_file, stored)
            except OSError as exc:
                sys.stderr.write(f'[serve] state cache write: {exc}\n')
            finally:
                self._save_lock.release()
        return st

    SETTLED = frozenset({"released"})   # the one state whose files will not move again
    FULL_EVERY = 10                     # sweeps between two rereads of the settled majority

    def refresh(self, full: bool = True) -> None:
        roots = set(self._registry.snapshot().values())
        if not full:
            # A released dataset is finished: rereading 270 of them is what pushes the handful that
            # are actually running out to a multi-minute refresh, which is exactly the lag people
            # notice. Reread the unsettled ones every sweep and the rest occasionally, in case one
            # was reopened or arrived while this process was not looking.
            with self._lock:
                settled = {root for root, (_, st) in self._states.items() if st.get("cls") in self.SETTLED}
            roots = {root for root in roots if root not in settled} or roots
        # Bound filesystem work independently of the number of browser requests.
        with ThreadPoolExecutor(max_workers=4, thread_name_prefix='dataset-warmer') as workers:
            list(workers.map(self._put, roots))
        with self._lock:
            live = set(self._registry.snapshot().values())
            for gone in set(self._states) - live:
                del self._states[gone]

    def start(self) -> None:
        def loop():
            sweep = 0
            while True:
                try:
                    self.refresh(full=sweep % self.FULL_EVERY == 0)
                except Exception as e:
                    sys.stderr.write(f"[serve] state warmer: {e}\n")
                sweep += 1
                time.sleep(self._ttl)

        threading.Thread(target=loop, daemon=True, name="state-warmer").start()


NAV_CSS = """
html,body{height:100%}body{display:flex;flex-direction:column;overflow:hidden}
.below{flex:1;display:flex;min-height:0}
/* the application bar: identity on the left, the sections on the right */
.tb{display:flex;align-items:center;gap:var(--s2);padding:0 var(--s2);min-height:3rem;
 background:var(--card);border-bottom:1px solid var(--line-strong);flex:none}
.tb-brand{display:inline-flex;align-items:center;color:inherit;text-decoration:none;margin-right:auto}
.tb-brand b{font-size:var(--t5)}.tb-brand .logo{font-size:var(--t6);margin-right:.4em}
.tb-brand:hover b{color:var(--accent)}
.tb-nav{display:flex;align-items:center;gap:4px}
.tb-link{font:inherit;font-size:var(--t3);font-weight:600;color:var(--muted);text-decoration:none;
 background:none;border:0;cursor:pointer;padding:6px 12px;border-radius:var(--r);white-space:nowrap}
.tb-link:hover{background:var(--none-bg);color:var(--ink)}
.tb-link.active{background:var(--accent-bg);color:var(--accent-ink)}
aside.sb{width:360px;flex:0 0 360px;background:var(--card);border-right:1px solid var(--line);display:flex;flex-direction:column;min-width:0;position:relative}
.sb-resizer{position:absolute;top:0;right:-3px;width:6px;height:100%;cursor:col-resize;z-index:6}
.sb-resizer:hover,.sb-resizer:active{background:var(--accent);opacity:.3}
.sb-head{padding:var(--s2) var(--s2) var(--s1);display:flex;flex-direction:column;gap:var(--s1);border-bottom:1px solid var(--line)}
.sb-head .brand{display:flex;align-items:center;justify-content:space-between;gap:var(--s1)}
.sb-head .sb-title{font-size:var(--t3);font-weight:650;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
.sb-head .sb-title small{font-size:var(--t3);font-weight:400;margin-left:.5em;text-transform:none;letter-spacing:0}
.sb-head input[type=search]{width:100%;font:inherit;font-size:var(--t3);padding:8px 12px;border:1px solid var(--line-strong);border-radius:var(--r);background:var(--card)}
.sb-head .sort-row{display:flex;flex-wrap:wrap;align-items:center;gap:var(--s1) var(--s2);font-size:var(--t3);color:var(--muted)}
.sb-head .sort-row .ctl{display:inline-flex;align-items:center;gap:6px;white-space:nowrap}
.sb-head select{font:inherit;font-size:var(--t3);padding:4px 8px;border:1px solid var(--line-strong);border-radius:6px;background:var(--card);color:var(--ink)}
.sb-list{flex:1;overflow-y:auto;padding:var(--s1)}
details.group{margin-bottom:4px}details.group>summary{list-style:none;cursor:pointer;display:flex;align-items:center;gap:var(--s1);padding:8px 10px;border-radius:var(--r);font-size:var(--t3);font-weight:650;color:var(--muted)}
details.group>summary::-webkit-details-marker{display:none}details.group>summary::before{content:"";width:0;height:0;border:5px solid transparent;border-left-color:currentColor;margin-right:2px;transition:transform .1s}
details.group[open]>summary::before{transform:rotate(90deg)}details.group>summary:hover{background:var(--none-bg)}
details.group>summary .gn{margin-left:auto;font-weight:400;font-variant-numeric:tabular-nums;display:flex;align-items:center;gap:.6em}
details.group>summary .gn .st{display:inline-flex;align-items:center;gap:.35em}
.items{padding-left:var(--s1)}
.item{display:flex;align-items:center;gap:10px;padding:8px 10px;border-radius:var(--r);color:var(--ink);text-decoration:none;font-size:var(--t3)}
.item:hover{background:var(--none-bg)}.item.active{background:var(--accent-bg);color:var(--accent-ink);font-weight:600}
.item .nm{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.item .cells{color:var(--st,var(--muted));font-variant-numeric:tabular-nums;font-size:var(--t2);white-space:nowrap}
.item input.sel{margin:0;flex:0 0 auto;opacity:0;transition:opacity .1s}
.item:hover input.sel,aside.selecting input.sel,.item input.sel:checked{opacity:1}
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
/* the pane while a dataset page is being rendered. The markup is the page's own hero and block,
   styled by index.CSS, so only the unknown part needs rules here. */
#pending{flex:1;min-height:0;overflow:auto;background:var(--bg)}
#pending .ph-bars{display:flex;flex-direction:column;gap:var(--s2);max-width:70ch}
#pending .ph-bars i{height:14px;border-radius:var(--r);background:var(--none-bg);
 background-image:linear-gradient(90deg,transparent,color-mix(in srgb,var(--card) 80%,transparent),transparent);
 background-size:200% 100%;animation:ph 1.4s linear infinite}
#pending .ph-bars i:nth-child(2){width:85%;animation-delay:.15s}#pending .ph-bars i:nth-child(3){width:60%;animation-delay:.3s}
@keyframes ph{from{background-position:200% 0}to{background-position:-200% 0}}
@media (prefers-reduced-motion:reduce){#pending .ph-bars i{animation:none}}
#bind-form{margin:0}#bind-form input{width:100%;font:var(--t3) var(--mono);padding:6px 10px;border:1px solid var(--line-strong);border-radius:6px;margin:4px 0}
#bind-form p{margin:var(--s1) 0;color:var(--muted)}
@supports ((backdrop-filter:blur(1px)) or (-webkit-backdrop-filter:blur(1px))){
 aside.sb,.mbar{background:color-mix(in srgb,var(--card) 90%,transparent);-webkit-backdrop-filter:blur(14px);backdrop-filter:blur(14px);box-shadow:var(--paper-shadow)}
}
@media (prefers-reduced-transparency:reduce){aside.sb,.mbar{background:var(--card);-webkit-backdrop-filter:none;backdrop-filter:none}}
@media (max-width:760px){aside.sb{position:fixed;inset:0 auto 0 0;z-index:5;box-shadow:0 0 0 100vw rgba(0,0,0,.35)}}
"""


def group_tally(counts: dict[str, int]) -> str:
    """A dot and a count per state (the word is the tooltip): a collection row has no room for
    '12 Completed · 3 Running · 1 Failed'. Same states and order as the overview and filters.
    The dot is the page's own status colour — emoji bring their own palette and their own
    advance widths, which a column of counts cannot line up."""
    return " ".join(f'<span class="st {cls}" title="{label}"><i class="dot"></i>{counts[cls]}</span>'
                    for cls, label in DATASET_STATES if counts.get(cls))


DATASET_STATES = (("released", "Completed"), ("running", "Running"), ("queued", "Queued"),
                  ("paused", "Paused"), ("neutral", "Not started"), ("failed", "Failed"))


def _navigator_html(items: dict[str, Path], registry_path: Path, state=_dataset_state, control: bool = False) -> str:
    """Shell: datasets grouped by collection down the left, the selected
    dataset's own pages (root landing page -> its units -> ...) in an iframe on
    the right. The iframe keeps the address in the hash (#/<name>/...), so
    reload / back / bookmarks land on the same page; `/` opens the overview."""
    from .. import model_web
    e = _h.escape
    groups: dict[str, list[str]] = {}
    tally: dict[str, dict[str, int]] = {}
    species: dict[str, int] = {}
    states = {name: state(p) for name, p in sorted(items.items())}
    colls = index.collections({name: (st['collection'] if 'collection' in st else index.collection_of(items[name]))
                               for name, st in states.items()})
    for name, p in sorted(items.items()):
        st = states[name]
        coll = colls[name] or "other"
        short = name[len(coll) + 1:] if name.startswith(coll + "-") else name
        # A released run's count is its result; a running one's is what the last finished round
        # left, which is worth reading as provisional. A failed run has no count to report.
        cells = index._k(st["final_cells"]) if st["cls"] in ("released", "running") else ""
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
            + (f'<span class="cells {e(st["cls"])}" title="{"cells released" if st["cls"] == "released" else "cells left by the last finished round"}">{cells}</span>'
               if cells else "") + "</a>"
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
    # The three destinations are the whole application; the sidebar below is one of them --
    # the dataset picker, and every control in it (filter, sorts, bind, registry path) serves
    # only that. Keeping them in one column read as an undifferentiated stack, and collapsing
    # the sidebar took the brand and the navigation away with the list.
    topbar = (
        '<header class="tb" id="tb">'
        f'<a class="tb-brand" id="brand" href="/_home" title="overview">{logo()}<b>{APP}</b></a>'
        '<nav class="tb-nav" aria-label="sections">'
        '<a class="tb-link" id="home-item" href="/_home" data-name="__home__">Overview</a>'
        + ('<a class="tb-link" id="control-item" href="/_control/" data-name="_control"'
           ' title="Temporal, warm pool and bridge of the run directory">Operations</a>' if control else '')
        + '<button class="tb-link" id="models-item" title="Primary and fallback model inventory">Agent Bridge</button>'
        '</nav></header>'
    )
    sidebar = (
        '<aside class="sb" id="sb" aria-label="datasets"><div class="sb-resizer" id="sb-resizer" title="drag to resize"></div>'
        '<div class="sb-head">'
        f'<div class="brand"><span class="sb-title">Datasets<small><span id="nav-n">{len(items)}</span> bound</small></span>'
        '<button class="icon" id="sb-toggle" title="hide sidebar" aria-label="hide sidebar">&#9776;</button></div>'
        '<input id="nav-q" type="search" placeholder="Filter datasets…" aria-label="filter datasets" autocomplete="off">'
        '<div class="sort-row"><span class="ctl"><label for="nav-sort">sort</label><select id="nav-sort">'
        '<option value="name">name</option><option value="cells">cells</option>'
        '<option value="status">status</option></select></span>'
        f'<span class="ctl"><label for="nav-sp">species</label><select id="nav-sp"><option value="">all</option>{sp_options}</select></span>'
        '<span class="ctl"><label for="nav-st">status</label><select id="nav-st"><option value="">all</option>'
        + ''.join(f'<option value="{cls}">{label}</option>' for cls, label in DATASET_STATES)
        + '</select></span></div>'
        "</div>"
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
        # Everything the sidebar already knows about the dataset, shown the instant it is clicked.
        # A dataset page is rendered from disk on every request; on a cold run directory that is
        # seconds, and until now the pane kept showing the previous dataset all the way through.
        # The same skeleton the page itself uses -- page > hero > block -- so the real page
        # replaces it in place instead of everything jumping when it arrives.
        '<div id="pending" hidden aria-live="polite"><main class="page">'
        '<header class="hero"><div class="title"><h1></h1><span class="pill"></span></div>'
        '<dl class="facts"></dl></header>'
        '<section class="block"><h2>Reading the run directory<span class="count">…</span></h2>'
        '<div class="ph-bars"><i></i><i></i><i></i></div></section></main></div>'
        f'<section id="model-panel" hidden aria-label="Agent Bridge"></section>'
        '<div id="empty" style="display:none"><h2>Nothing bound yet</h2><p>Use <b>+ Bind…</b> in the sidebar or, on the server host, '
        "<code>eca-rsi serve scan-add &lt;dir-or-glob&gt;</code>. The server picks up registry changes on the next request.</p>"
        f"<p>{hint}</p></div></main>"
    )
    return (
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width,initial-scale=1"><title>{APP} · ECA-RSI</title>{FAVICON}'
        f"<style>{index.CSS}{NAV_CSS}{LOGO_CSS}{model_web.CSS}</style></head><body>{topbar}<div class='below'>{sidebar}{main}</div>"
        f"<script>{model_web.JS}</script><script>{NAV_JS}</script></body></html>"
    )


HOME_CSS = ("td.nw{white-space:nowrap}#ds-table td{padding:7px 8px}"
            # A capsule is only right for one line. A status here wraps to two or three, and a
            # 999 px radius then curves in far enough to cut the words it is meant to hold; the
            # dot, centred on a three-line box, floats away from the line it belongs to.
            "#ds-table .pill{white-space:normal;line-height:1.35;max-width:18ch;"
            "border-radius:var(--r);padding:.25em .6em;align-items:flex-start}"
            "#ds-table .pill::before{margin-top:.42em}"
            # ten columns is a lot of table: the long text ones are capped so the numbers,
            # the sparkline and the status stay on one screen instead of behind a scrollbar.
            "#ds-table{table-layout:auto;width:100%}#ds-table th,#ds-table td{overflow-wrap:anywhere}"
            "#ds-table td:first-child{max-width:24ch}#ds-table td.unit{max-width:16ch;white-space:normal;color:var(--muted)}"
            "#ds-table td:nth-child(3){max-width:14ch;white-space:normal}"
            "#ds-table td:nth-child(4){max-width:8ch}#ds-table .spark{width:84px}"
            ".cell-glance{grid-template-columns:minmax(0,1fr) minmax(0,1fr) minmax(0,1.5fr) minmax(0,1.1fr)}.cell-glance .v{white-space:nowrap}"
            "@media(max-width:700px){.cell-glance{grid-template-columns:repeat(2,minmax(0,1fr))}}"
            ".hist{position:relative;margin-top:var(--s1)}.hist-svg{display:block;width:100%;height:auto}"
            ".hist-svg .grid{stroke:var(--line);stroke-width:1}.hist-svg .tick{font-size:11px;fill:var(--muted)}"
            ".hist-svg .ser{fill:none;stroke-width:2.25;stroke-linejoin:round}.hist-svg .ser.in{stroke:var(--muted)}.hist-svg .ser.rel{stroke:var(--done)}"
            ".hist-svg .cross{stroke:var(--accent);stroke-width:1;stroke-dasharray:3 3}.hist-svg .zoom{fill:var(--accent);opacity:.15}.hist-svg .hit{cursor:crosshair}"
            ".hist-legend{display:flex;gap:var(--s3);font-size:var(--t3);color:var(--muted);margin-top:4px}.hist-legend i{display:inline-block;width:18px;height:3px;vertical-align:middle;margin-right:6px}"
            ".hist-legend i.in{background:var(--muted)}.hist-legend i.rel{background:var(--done)}"
            ".hist-range button{font:inherit;font-size:var(--t2);padding:3px 10px;border:1px solid var(--line-strong);background:var(--card);color:var(--ink);border-radius:999px;cursor:pointer}"
            ".hist-range button.on{background:var(--accent-bg);color:var(--accent-ink);border-color:var(--accent)}"
            ".hist-range .sep{flex:1}#hist-more{color:var(--accent);text-decoration:none;border-bottom:1px dotted currentColor}")


def fleet_history(states: dict) -> dict:
    """Per-dataset organize / release events (epoch seconds, cells) for the
    curve; `states` = {name: (dataset_state, path)}. Derived from the logs of
    what is bound now, so unbinding a dataset removes it from the past too."""
    out = {}
    colls = index.collections({n: (s['collection'] if 'collection' in s else index.collection_of(p))
                               for n, (s, p) in states.items()})
    for name, (s, p) in sorted(states.items()):
        ev = s.get("events") or {}
        out[name] = {"collection": colls[name], "species": s["species"],
                     "organize": [list(e) for e in ev.get("organize", [])], "release": [list(e) for e in ev.get("release", [])],
                     "rounds": [list(e) for e in ev.get("rounds", [])],
                     "state": s["cls"], "input_cells": s.get("n_input") or 0,
                     "awaiting_start": s.get("awaiting_start", s["cls"] == "queued"),
                     "final_cells": s.get("final_cells") or 0}
    return {"datasets": out}


def history_at(hist: dict, at: float) -> dict:
    """The curve read at one moment: cells in / released and how many datasets had started / released."""
    cin = rel = din = drel = 0
    for d in hist["datasets"].values():
        o = [n for t, n in d["organize"] if t <= at]
        r = [n for t, n in d["release"] if t <= at]
        cin += sum(o); rel += sum(r); din += bool(o); drel += bool(r)
    return {"at": at, "cells_in": cin, "cells_released": rel, "datasets_started": din, "datasets_released": drel}


def fleet_totals(hist: dict) -> dict:
    """Current cards and the curve share the same dated input/release events."""
    result = history_at(hist, time.time())
    rows = list(hist["datasets"].values())
    result["cells_queued"] = sum(max(0, d["input_cells"] - sum(n for _, n in d["organize"]))
                                 for d in rows if d["awaiting_start"])
    result["undated_input"] = sum(max(0, d["input_cells"] - sum(n for _, n in d["organize"]))
                                  for d in rows if not d["awaiting_start"] and d["state"] != "neutral")
    released = [d for d in rows if d["state"] == "released"]
    denominator = sum(d["input_cells"] for d in released)
    result["kept"] = 100 * sum(d["final_cells"] for d in released) / denominator if denominator else None
    return result


def _parse_at(text: str) -> float:
    try:
        return float(text)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return time.mktime(time.strptime(text, fmt))
        except ValueError:
            continue
    raise ValueError(f"unparseable time {text!r}; use YYYY-MM-DDTHH:MM or epoch seconds")

HISTORY_JS = r"""
(function(){
  const D = HISTORY_DATA, box = document.getElementById("hist"), tip = document.getElementById("hist-tip"),
        nEl = document.getElementById("hist-n"), q = document.getElementById("ds-q"), table = document.getElementById("ds-table");
  if (!D || !box) return;
  const fmtN = v => v >= 1e6 ? (v / 1e6).toFixed(v >= 1e7 ? 0 : 1) + "M" : v >= 1e3 ? Math.round(v / 1e3) + "k" : String(v);
  const fmtT = t => { const d = new Date(t * 1000), p = n => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`; };
  // -- which datasets count: the ones the table filter leaves visible --
  function names(){ if (!table) return Object.keys(D.datasets);
    return [...table.tBodies[0].rows].filter(r => !r.hidden).map(r => decodeURIComponent(r.querySelector("a").getAttribute("href").slice(1, -1))); }
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
  // -- productivity: cell-rounds per hour, a trailing-window rate on the curve's own time axis --
  // One finished round of a unit that went in with n cells is n cell-rounds: the loop's unit of
  // work, whether it came from a big dataset's one round or a small one's many.
  const prodBox = document.getElementById("prod");
  function drawProd(t0, t1, x, xm, xt, xl){
    if (!prodBox) return;
    const rs = []; for (const nm of names()) { const d = D.datasets[nm]; if (d && d.rounds) for (const [t, n] of d.rounds) rs.push([t, n]); }
    rs.sort((a, b) => a[0] - b[0]);
    if (!rs.length) { prodBox.innerHTML = '<p class="empty">no finished round is dated yet</p>'; return; }
    const span = t1 - t0, win = span > 7 * 86400 ? 86400 : 6 * 3600, NB = 160, pts = [];
    for (let i = 0; i <= NB; i++) { const te = t0 + span * i / NB; let c = 0, k = 0;
      for (const [t, n] of rs) { if (t > te) break; if (t > te - win) { c += n; k++; } }
      pts.push([te, c / (win / 3600), k / (win / 3600)]); }
    const yraw = Math.max(...pts.map(p => p[1]), 1);
    const nice = [1, 2, 5, 10, 20, 50].map(m => m * Math.pow(10, Math.floor(Math.log10(yraw)) - 1)).find(s => yraw / s <= 5) || yraw / 4;
    const ymax = Math.ceil(yraw / nice) * nice, PH = 150, y = v => T + (1 - v / ymax) * (PH - T - B), yt = [];
    for (let v = 0; v <= ymax + nice / 2; v += nice) yt.push(v);
    const d = pts.map((p, i) => `${i ? "L" : "M"}${x(p[0]).toFixed(1)} ${y(p[1]).toFixed(1)}`).join(" ");
    prodBox.innerHTML = `<svg class="hist-svg" viewBox="0 0 ${W} ${PH}" role="img" aria-label="cell-rounds per hour">
      ${yt.map(v => `<line class="grid" x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}"/><text class="tick" x="${L - 8}" y="${y(v) + 4}" text-anchor="end">${fmtN(v)}</text>`).join("")}
      ${xm.map(t => `<line class="grid" x1="${x(t)}" x2="${x(t)}" y1="${PH - B}" y2="${PH - B + 5}"/>`).join("")}
      ${xt.map(t => `<text class="tick" x="${x(t)}" y="${PH - B + 18}" text-anchor="middle">${xl(t)}</text>`).join("")}
      <path class="ser rel" d="${d}"/><rect class="hit" x="${L}" y="${T}" width="${W - L - R}" height="${PH - T - B}" fill="transparent"/></svg>
      <div class="hist-legend"><span><i class="rel"></i>cell-rounds per hour, trailing ${win / 3600} h</span></div>`;
    const svg = prodBox.querySelector("svg");
    svg.querySelector(".hit").addEventListener("mousemove", e => { const r = svg.getBoundingClientRect();
      const f = ((e.clientX - r.left) / r.width * W - L) / (W - L - R), p = pts[Math.max(0, Math.min(NB, Math.round(f * NB)))];
      tip.style.display = "block"; tip.innerHTML = `<b>${fmtT(p[0])}</b><br><b>${Math.round(p[1]).toLocaleString()}</b> cell-rounds/h · ${p[2].toFixed(1)} rounds/h<br><span class="m">over the ${win / 3600} h before</span>`;
      tip.style.left = Math.max(window.scrollX + 4, e.pageX - tip.offsetWidth / 2) + "px"; tip.style.top = (e.pageY - tip.offsetHeight - 10) + "px"; });
    svg.querySelector(".hit").addEventListener("mouseleave", () => { tip.style.display = "none"; });
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
    drawProd(t0, t1, x, xm, xt, xl);
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
"""

HOME_JS = r"""
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
"""


def _home_html(items: dict[str, Path], state=_dataset_state, verdicts: "ControlVerdicts | None" = None) -> str:
    """Overview: what this site is, fleet numbers, and a filterable, sortable
    table of every dataset. This is the page `/` opens."""
    import time

    e = _h.escape
    states = {name: (state(p), p) for name, p in items.items()}
    cached = [s.get('cached_at') for s, _ in states.values() if 'cached_at' in s]
    freshness = ''
    if cached:
        known = [stamp for stamp in cached if stamp is not None]
        oldest = index._when(min(known)) if known else 'not yet available'
        freshness = (f'<p class="muted" role="status">Dataset summaries: {len(known)} / {len(cached)} loaded. '
                     f'Oldest refresh: {oldest}. Showing the last available data while refreshing in the background.</p>')
    if verdicts is not None:
        # Say which clock the status column is on. A reader who cannot tell a live verdict from a
        # guess made out of file dates has no way to catch the page being wrong, which is how a
        # dataset sat here reading "running" for hours after it died.
        age = verdicts.age()
        source = (f'status from the control plane, {int(age)}s old' if verdicts.live() and age is not None
                  else 'status inferred from files: the control plane is not publishing'
                  if age is None else f'status inferred from files: the control plane last published {index._when(time.time() - age)}')
        freshness += f'<p class="muted" role="status">Run status: {e(source)}.</p>'
    by = lambda c: sum(1 for s, _ in states.values() if s["cls"] == c)  # noqa: E731
    history = fleet_history(states)
    totals = fleet_totals(history)
    stats = [(str(len(items)), "datasets", "", "datasets")] + [
             (str(by(cls)), label, cls, "state-" + cls)
             for cls, label in DATASET_STATES if by(cls) or cls in {"released", "running", "queued", "failed"}]
    compact = lambda n: f'{n/1_000_000:.2f} M' if n >= 10_000_000 else f'{n:,}'
    cell_stats = [
             (compact(totals["cells_in"]), "cells in", "", "cells-in"),
             (compact(totals["cells_queued"]), "cells awaiting start", "", "cells-queued"),
             (index._n(totals["cells_released"]) or "0", "cells released", "", "cells-released"),
             (f'{totals["kept"]:.0f}%' if totals["kept"] is not None else "—", "kept in completed datasets", "", "kept")]
    def stat_html(rows):
        return "".join(f'<div class="stat" data-stat="{key}"><span class="v{" st " + c if c and int(v) else ""}">{e(v)}</span><span class="k">{e(k)}</span></div>'
                       for v, k, c, key in rows)
    rank = {cls: i for i, (cls, _) in enumerate(DATASET_STATES)}
    rows = []
    colls = index.collections({n: (s['collection'] if 'collection' in s else index.collection_of(p))
                               for n, (s, p) in states.items()})
    for name, (s, p) in sorted(states.items()):
        coll = colls[name]
        short = name[len(coll) + 1:] if coll and name.startswith(coll + "-") else name  # the collection has its own column
        # One row per analysis unit: a unit is what actually runs rounds, so it is the only row that
        # can carry an honest convergence curve and status. A dataset that has not organized yet has
        # no unit, and a state cached before this column existed has no unit_rows; both fall back to
        # the dataset aggregate so the fleet page never goes blank while the cache warms.
        units = s.get("unit_rows") or [dict(name="", stage=s["stage"], cls=s["cls"], n_input=s["n_input"],
                                            final_cells=s["final_cells"], rounds=s["rounds"],
                                            trend=s.get("trend") or [], species=s["species"], updated=s["updated"])]
        # The control plane numbers its unit workflows in plan order, which the organize publication
        # records, so a unit row usually gets the verdict on that very unit. Where the order is not
        # on disk the run's own verdict stands in, and then it may only close a row, never reopen it.
        run = s.get("run_id", "")
        run_verdict = verdicts.of(run) if verdicts else None
        rows_out = []
        for u in units:
            own = verdicts.of(run, f"/unit-{u['index']}") if verdicts and u.get("index") is not None else None
            rows_out.append(reconcile(u, own or run_verdict, precise=own is not None))
        units = rows_out
        for u in units:
            kept = 100 * u["final_cells"] / u["n_input"] if u["n_input"] and u["final_cells"] is not None else None
            species = u.get("species") or s["species"]
            rows.append(
                f'<tr data-text="{e((name + " " + u["name"] + " " + coll + " " + species + " " + u["stage"]).lower())}">'
                f'<td><a href="/{e(name)}/" title="{e(name)}"><b>{e(short)}</b></a></td>'
                f'<td class="nw unit">{e(u["name"])}</td><td class="nw">{e(coll)}</td><td>{e(species)}</td>'
                f'<td class="num" data-v="{u["n_input"] or 0}">{index._k(u["n_input"])}</td>'
                f'<td class="num" data-v="{u["final_cells"] or 0}">{index._k(u["final_cells"])}</td>'
                f'<td class="num" data-v="{kept if kept is not None else -1}">{f"{kept:.0f}%" if kept is not None else ""}</td>'
                f'<td class="num" data-v="{u["rounds"]}">{u["rounds"] or ""}</td>'
                f'<td>{index.sparkline(u["trend"])}</td>'
                f'<td data-v="{rank.get(u["cls"], 9)}"><span class="pill {e(u["cls"])}">{e(u["stage"])}</span></td>'
                f'<td class="num nw" data-v="{u["updated"] or 0}">{index._when(u["updated"])}</td></tr>')
    def th(t, num=False):
        attrs = ' class="r" data-num' if num else ""
        return f'<th{attrs} aria-sort="none"><button type="button">{t}</button></th>'
    table = ('<div class="wrap"><table id="ds-table"><thead><tr>' + th("dataset") + th("unit") + th("collection") + th("species")
             + th("cells in", True) + th("cells out", True) + th("kept", True) + th("rounds", True)
             # not sortable: the shape is the point, and one number cannot stand for it
             + '<th class="r">convergence</th>' + th("status", True) + th("last updated", True)
             + f'</tr></thead><tbody>{"".join(rows)}</tbody></table></div>' if rows
             else '<p class="empty">No dataset is bound yet. Use <b>+ Bind…</b> in the sidebar or <code>eca-rsi serve scan-add</code> on the server host.</p>')
    return (
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width,initial-scale=1"><title>{APP} — overview</title>{FAVICON}'
        f'<style>{index.CSS}{LOGO_CSS}{HOME_CSS}</style></head><body><main class="page">'
        f'<header class="hero"><div class="title"><h1>{logo()}{APP}</h1><span class="sub">ECA-RSI runs</span></div>'
        '<p class="sub" style="max-width:80ch;margin-top:8px">Recursive self-improving annotation of single-cell atlases. Each dataset below was '
        "processed per sample (QC, clustering), integrated across samples and annotated in rounds by agents, with low-quality cells removed "
        "until the loop converged. A dataset page shows the numbers, the rounds, the final UMAP with coarse and fine labels, "
        "the cell-identity Sankey, the review items and where the result files live.</p>"
        '<p class="next">Pick a dataset in the table or the sidebar. Green = released, amber = still running, red = failed.</p></header>'
        f'{freshness}<div class="glance">{stat_html(stats)}</div>'
        f'<div class="glance cell-glance" aria-label="Cell counts">{stat_html(cell_stats)}</div>'
        '<section class="block" id="history"><h2>Cells over time <span class="count" id="hist-n"></span></h2>'
        '<p class="lede">Counted at each dataset\'s recorded organize and release steps, for whatever is bound right now. '
        'Hover to read a moment, drag to zoom. <a href="#" id="hist-more">What is counted?</a></p>'
        '<p class="lede" id="hist-detail" hidden>Cells in excludes queued inputs, which are shown separately as '
        '<b>Cells awaiting start</b>; Cells released includes released units of a dataset still running. Unbind a '
        'dataset and it leaves the past too. The table filter applies to the cards and the curve; a time zoom only to the curve.</p>'
        f'<p class="muted" id="hist-undated"{" hidden" if not totals["undated_input"] else ""}>'
        f'{totals["undated_input"]:,} input cells lack a recorded start time and are excluded from Cells in and the time curve.</p>'
        '<div class="toolbar hist-range"><button type="button" data-r="1">24h</button><button type="button" data-r="7">7d</button>'
        '<button type="button" data-r="30">30d</button><button type="button" data-r="0" class="on">all</button>'
        # A batch is weeks of history in which the interesting part is the last few hours; on a
        # clock axis those hours are a sliver. Log time spaces points by age from the right edge.
        # Off by default: on it, equal horizontal distances are no longer equal durations.
        '<span class="sep"></span>'
        # Cells in climbs far faster than cells released (organize is cheap, release is the
        # whole loop), so on a shared axis its line dwarfs the released line into a flat
        # smear near zero. Hidden by default; the released curve is the one worth reading.
        '<button type="button" id="hist-show-in" aria-pressed="false" title="cells in rises much faster than cells released and compresses it on a shared axis">show cells in</button>'
        '<button type="button" id="hist-log" aria-pressed="false" title="space by age instead of by clock, so the newest hours get most of the width">log time</button></div>'
        '<div id="hist" class="hist"></div><div id="hist-tip" class="sk-tip" style="display:none"></div>'
        '<h2 style="margin-top:var(--s3)">Productivity over time</h2>'
        '<p class="lede">Cell-rounds per hour: each finished round adds the cells it started with, so one round of a '
        '100k-cell unit counts as much as ten rounds of a 10k-cell one. Follows the zoom and filter above.</p>'
        '<div id="prod" class="hist"></div></section>'
        f'<section class="block" id="datasets"><h2>Datasets <span class="count" id="ds-n">{len(rows)} units</span></h2>'
        '<p class="lede">Input counts include declared queued inputs. Cells out shows the latest output count; kept is out / in. Click a column header to sort.</p>'
        '<div class="toolbar"><label for="ds-q">Filter</label><input id="ds-q" type="search" placeholder="name, collection, species, status…" autocomplete="off"></div>'
        f"{table}</section>"
        f'<footer>rendered {time.strftime("%Y-%m-%d %H:%M:%S")} by {APP} (ecarsi serve) from the registry · reload for the current state</footer>'
        f"</main><script>const HISTORY_DATA = {json.dumps(history)};</script>"
        f"<script>{HOME_JS}</script><script>{HISTORY_JS}</script><script>{index.SPARK_JS}</script></body></html>"
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
    if L.is_unit(root) or L.is_gen2_unit(root):
        return index.render_unit(root, name) if not parts else None
    if not parts:
        return index.render_root(root, name)
    if len(parts) == 2 and parts[0] == L.UNITS and (L.is_unit(root / L.UNITS / parts[1])
                                                    or L.is_gen2_unit(root / L.UNITS / parts[1])):
        return index.render_unit(root / L.UNITS / parts[1], name)
    return None


class Handler(http.server.SimpleHTTPRequestHandler):
    """Multi-tenant static files: first path segment selects a dataset from
    the registry, the rest is served from that directory (self.directory /
    self.path are recomputed per request, which is safe — translate_path
    reads them fresh on every call, not cached from __init__)."""

    def __init__(self, *a, registry: Registry, auth: str | None = None, states: StateCache | None = None,
                 control=None, verdicts: "ControlVerdicts | None" = None, **kw):
        self._registry = registry
        self._control = control  # observatory.ControlPlane behind /_control/ when serve got --control-plane
        self._verdicts = verdicts  # the control plane's published run statuses, when it publishes them
        self._auth = auth  # "user:pass" -> HTTP basic auth enforced here, on every request; None = open
        self._state = states.get if states else _dataset_state  # fleet pages: cached states when a warmer runs
        self._states = states
        self._items = registry.cached_snapshot if states else registry.snapshot
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
        if urllib.parse.unquote(self.path).startswith("/_models"):
            self.send_header("Cache-Control", "no-store")
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
        if path.startswith("/_models/"):
            from .. import model_web
            if path not in {"/_models/access", "/_models/save", "/_models/keys"}:
                return self._json(404, {"error": "Not found"})
            if not (self._admin_allowed() or model_web.admin_matches(self.headers.get("X-Model-Admin", ""))):
                return self._json(403, {"error": "Model settings require administrator access"})
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                return self._json(400, {"error": "Expected application/json"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 0 or length > 32768:
                    return self._json(413, {"error": "Request too large"})
                req = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(req, dict):
                    raise ValueError("Expected a JSON object")
                if path.endswith("/access"):
                    return self._json(200, {"ok": True})
                if path.endswith("/keys"):
                    return self._json(200, model_web.key_presence())
                return self._json(200, model_web.save_models(req.get("models"), req.get("revision")))
            except FileExistsError:
                return self._json(409, {"error": "Configuration changed. Reload before saving."})
            except ValueError:
                return self._json(400, {"error": "Invalid configuration. Check model names, duplicate entries and HTTP(S) URLs. Never include credentials in URLs. Models on the same backend must share its URL."})
            except Exception:
                return self._json(503, {"error": "Model configuration could not be read or saved"})
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
        started = time.monotonic()
        try:
            return self._get()
        finally:
            elapsed = time.monotonic()-started
            if elapsed >= 2:
                self.log_message('slow GET %s duration=%.3fs', self.path.split('?', 1)[0], elapsed)

    def _get(self):
        if not self._authorized():
            return self._demand_auth()
        raw = self.path.split("?", 1)[0]
        if raw == "/_models/status.json":
            from .. import model_web
            try:
                data = model_web.snapshot()
                return self._json(200, {**data, "html": model_web.render(data)})
            except Exception:
                return self._json(503, {"error": "Model configuration is unavailable"})
        if raw == "/_control" or raw.startswith("/_control/"):
            if self._control is None:
                return self._json(404, {"error": "no control plane: start the server with --control-plane <run dir>"})
            if raw == "/_control":
                return self._redirect("/_control/")
            if raw == "/_control/":
                return self._send(200, self._control.page(), "text/html; charset=utf-8")
            if raw in {"/_control/api/status", "/_control/api/timeline"}:
                try:
                    query = urllib.parse.parse_qs(self.path.partition("?")[2])
                    return self._json(200, self._control.api(raw.rsplit("/", 1)[1], query))
                except (ValueError, OverflowError) as e:
                    return self._json(400, {"error": str(e)})
            return self._json(404, {"error": "Not found"})
        if raw == "/_home":
            return self._html(_home_html(self._items(), self._state, self._verdicts))
        if raw == "/_history.json":  # the curve's data; ?at=YYYY-MM-DDTHH:MM (or epoch) reads it at one moment
            hist = fleet_history({n: (self._state(p), p) for n, p in self._items().items()})
            at = urllib.parse.parse_qs(self.path.partition("?")[2]).get("at")
            if not at:
                return self._json(200, hist)
            try:
                return self._json(200, history_at(hist, _parse_at(at[0])))
            except ValueError as e:
                return self._json(400, {"error": str(e)})
        parts = [p for p in raw.split("/") if p]
        if not parts:
            return self._html(
                _navigator_html(self._items(), self._registry.path, self._state, control=self._control is not None)
            )
        name = parts[0]
        root = self._items().get(name)
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
    control, verdicts = None, ControlVerdicts(None)
    if args.control_plane:
        from ..observatory import ControlPlane
        base = Path(args.control_plane).expanduser().resolve()
        control = ControlPlane(base, pool_root=Path(args.control_pool_root or base / 'pool'),
                               bridge_root=Path(args.control_bridge_root or base / 'bridge'),
                               temporal_service_root=Path(args.control_temporal_root or base / 'durable-control')).start()
        # container/fleet-status.py publishes this beside the run; absent, the page says so and
        # falls back to what the files imply.
        verdicts = ControlVerdicts(base / 'fleet-status.json')
    # The plane's own list of runs is the third source of rows, under the file and the command line:
    # a submitted dataset is on the page at once, and nobody registers anything by hand.
    registry = Registry(reg_path, extra, published=verdicts.runs)
    items = registry.snapshot()
    registry.start()
    cache_key = hashlib.sha256(str(reg_path).encode()).hexdigest()[:16]
    states = StateCache(registry, cache_file=Path.home()/'.cache/ecarsi-periscope'/f'{cache_key}.json')
    states.start()
    httpd = http.server.ThreadingHTTPServer(
        (args.bind, args.port), partial(Handler, registry=registry, auth=args.auth, states=states, control=control, verdicts=verdicts)
    )
    print(
        f"[serve] {APP} on http://{args.bind}:{args.port}/  ({len(items)} dataset(s); registry {reg_path}"
        + (f", {len(extra)} from the command line)" if extra else ")")
        + (
            f"  [password-protected, user {args.auth.split(':', 1)[0]!r}]"
            if args.auth
            else "  [no password]"
        )
        + (f"  [control plane {control.root} at /_control/]" if control else ""),
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


def _informative(d: Path) -> list[str]:
    return [c for c in d.parts[1:] if c not in GENERIC_DIR_NAMES] or [d.name]


def _auto_depth(name: str, path: Path) -> int | None:
    """How many components of `path` this function used to make `name`, or None
    if it never would have. A name already qualified once (mca1.1-Bladder) has
    to stay recognisable as ours, or the next batch to arrive reads it as a
    hand-picked name and helps itself to the bare one — the very
    order-dependence #11 is about. The depth is what keeps requalification
    one-way: an entry may gain a component when a new collision demands it,
    never lose one because the collision that caused it is no longer in this
    batch. (Measured 2026-09-21: recomputing from scratch would have renamed
    298 of 459 live entries, every one of them to something *shorter*.)"""
    comps = _informative(path)
    return next((k for k in range(1, len(comps) + 1) if name == "-".join(comps[-k:])), None)


def _scan_names(dirs: list[Path], taken: dict[str, Path]) -> dict[Path, str]:
    """Names for a batch of scanned dirs. Path components that carry no
    information (rsi, eca-pp, units, ...) are dropped; each dir starts with
    its last informative component and every dir whose name collides —
    within the batch or with an existing entry for another path — is
    qualified by one more component, symmetrically (Brain across three
    collections becomes mca1.1-Brain / mca2.0-Brain / mca3.0-Brain, not
    Brain / Brain-rsi / eca-pp-Brain).

    Already-registered dirs that still carry their bare auto-name take part in
    the resolution, so the result does not depend on the order scan-add was
    run in: whoever arrived first does not get to keep `/Bladder/` while every
    other collection is qualified (eca-rsi#11). A bookmarked bare URL then
    stops resolving instead of quietly pointing at another batch's data. A name
    this function would never produce is a deliberate `--name` and is left
    alone — it only bars others from taking it. The caller applies the renames.
    """
    comps: dict[Path, list[str]] = {}
    fixed: dict[str, Path] = {}
    floor: dict[Path, int] = {}
    for name_, path in taken.items():
        if path in dirs or path in comps:
            continue
        held = _auto_depth(name_, path)
        if held is None:
            fixed[name_] = path
        else:
            comps[path], floor[path] = _informative(path), held
    for d in dirs:
        comps[d] = _informative(d)
    order = list(comps)
    # Sharing a last component is what makes a bare name ambiguous, whatever the others are
    # called right now. Keying off the current names instead would let a third collection walk
    # in and take `/Bladder/` simply because the two incumbents had already been qualified.
    shared = {c for c in (comps[d][-1] for d in order)
              if sum(comps[d][-1] == c for d in order) > 1}
    depth = {d: min(max(floor.get(d, 1), 2 if comps[d][-1] in shared else 1), len(comps[d]))
             for d in order}
    name = lambda d: "-".join(comps[d][-depth[d] :])
    while True:
        by: dict[str, list[Path]] = {}
        for d in order:
            by.setdefault(name(d), []).append(d)
        clash = [
            d
            for n, ds in by.items()
            for d in ds
            if len(ds) > 1 or (n in fixed and fixed[n] != d)
        ]
        clash = [d for d in clash if depth[d] < len(comps[d])]  # can't qualify further
        if not clash:
            return {d: name(d) for d in order}
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
    renames = [
        (old, names[p], p)
        for old, p in sorted(taken.items())
        if p in names and p not in new and names[p] != old
    ]
    for d in skipped:
        print(f"  skip   {d}  (not an organize root / unit)")
    if not plan:
        print(
            f"[serve] nothing new to add ({len(matches)} matched, {len(skipped)} skipped, {len(matches) - len(skipped)} already in)"
        )
        return 0
    for old, new_name, d in renames:
        if args.dry_run:
            print(f"  would  {old:24s} -> {new_name}  (bare name is now ambiguous)")
            continue
        try:
            reg.bind(new_name, d)
            reg.unbind([old])
            print(f"  renamed {old:23s} -> {new_name}")
        except (ValueError, OSError) as e:
            print(f"  FAILED rename {old} -> {new_name}  ({e})")
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
        print(f"[serve] dry run: {len(plan)} to add, {len(renames)} to rename, {len(skipped)} skipped")
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
    ap.add_argument("--control-plane", default=None, metavar="RUN_DIR",
                    help="gen-2 run directory: serve its Temporal / warm pool / bridge monitor at /_control/")
    ap.add_argument("--control-pool-root", default=None, help="pool root of the run directory (default RUN_DIR/pool)")
    ap.add_argument("--control-bridge-root", default=None, help="bridge root (default RUN_DIR/bridge)")
    ap.add_argument("--control-temporal-root", default=None, help="Temporal service root (default RUN_DIR/durable-control)")
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

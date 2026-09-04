"""ecarsi.serve — a stateless navigator server for eca-rsi run reports.

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
`/<name>/...`; `/` is a navigator page listing them all. Landing pages are
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

The navigator also shows each dataset's Slurm job (last job= in
<root>/jobs.log, see the convention above _root_job_id; status.txt is read
as a fallback), asked of squeue/sacct with a short cache — purely a
display; nothing here submits anything.
"""

from __future__ import annotations

import argparse
import base64
import glob as _glob
import hmac
import html as _h
import http.server
import json
import os
import re
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
        raise ValueError(f"{path} is neither an organize root nor a unit dir (see ecarsi.layout)")
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
        tmp.write_text(json.dumps({k: str(v) for k, v in sorted(items.items())}, indent=2) + "\n")
        os.replace(tmp, path)  # atomic: a concurrent server never sees a half-written file

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
            sys.stderr.write(f"[serve] registry {self.path} unreadable, keeping last good list: {e}\n")
            return
        self._stamp = stamp

    # -- reads --
    def snapshot(self) -> dict[str, Path]:
        with self._lock:
            self._load_if_changed()
            return {**self._file, **self._extra}  # this process's own dirs win on a name clash

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
                raise ValueError(f"name {name!r} already bound to {existing} — use --name to disambiguate or remove it first")
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
                raise ValueError("nothing bound as " + ", ".join(repr(m) for m in missing)
                                 + ("" if not any(n in self._extra for n in missing)
                                    else " (given on the serve command line, not in the registry file)"))
            new = {k: v for k, v in self._file.items() if k not in names}
            self.write_file(self.path, new)
            self._file = new
            self._stamp = None


# ---------------------------------------------------------------- slurm jobs (display only)
#
# Convention: whatever submits a run appends to <root>/jobs.log, one line per
# event, space-separated key=value tokens, every line carrying job=<slurm id>:
#     job=41888484 node=sh04-13n32 start=2026-09-03T12:00:05-0700
#     job=41888484 end=2026-09-03T15:24:36-0700 exit=0
# The navigator takes the LAST job id in the file as the run's current job and
# asks Slurm about it (squeue for queued/running, sacct for finished). The
# older per-run status.txt (same tokens, job= only on its first line) is read
# as a fallback so existing runs show up too. No squeue on PATH -> no column.

JOBS_LOG = "jobs.log"
_JOB_RE = re.compile(r"\bjob=(\d+)\b")
_slurm_cache: dict = {"at": 0.0, "ids": (), "states": {}}
_SLURM_TTL = 20.0  # s; one squeue + one sacct per page load at most this often


def _root_job_id(root: Path) -> str | None:
    for fname in (JOBS_LOG, "status.txt"):
        f = root / fname
        if f.is_file():
            ids = _JOB_RE.findall(f.read_text())
            if ids:
                return ids[-1]
    return None


def _slurm_states(ids: list[str]) -> dict[str, dict]:
    """{job id: {state, elapsed, node, reason}} via one squeue + one sacct, cached."""
    ids = sorted(set(ids))
    if not ids or not shutil.which("squeue"):
        return {}
    now = time.time()
    if tuple(ids) == _slurm_cache["ids"] and now - _slurm_cache["at"] < _SLURM_TTL:
        return _slurm_cache["states"]
    out: dict[str, dict] = {}
    try:
        q = subprocess.run(["squeue", "-j", ",".join(ids), "-h", "-o", "%i|%T|%M|%N|%r"],
                           capture_output=True, text=True, timeout=15)
        for line in q.stdout.splitlines():
            jid, state, elapsed, node, reason = (line.split("|") + [""] * 5)[:5]
            out[jid] = {"state": state, "elapsed": elapsed, "node": node, "reason": reason}
        rest = [i for i in ids if i not in out]
        if rest and shutil.which("sacct"):
            a = subprocess.run(["sacct", "-j", ",".join(rest), "-X", "-n", "-P", "-o", "JobID,State,Elapsed,NodeList,ExitCode"],
                               capture_output=True, text=True, timeout=20)
            for line in a.stdout.splitlines():
                jid, state, elapsed, node, exitcode = (line.split("|") + [""] * 5)[:5]
                out[jid] = {"state": state.split()[0] if state else "", "elapsed": elapsed, "node": node, "reason": f"exit {exitcode}"}
    except (subprocess.TimeoutExpired, OSError) as e:
        sys.stderr.write(f"[serve] slurm lookup failed: {e}\n")
    _slurm_cache.update(at=now, ids=tuple(ids), states=out)
    return out


_JOB_CLS = {"RUNNING": "running", "PENDING": "running", "COMPLETING": "running", "CONFIGURING": "running",
            "COMPLETED": "released"}


def _job_cell(jid: str | None, st: dict | None) -> tuple[str, str]:
    """(html, search text) for the navigator's job column."""
    if not jid:
        return '<span class="muted">–</span>', ""
    if not st:
        return f'<span class="pill neutral" title="job {jid}: not known to squeue/sacct">{jid} ?</span>', jid
    state = st["state"] or "?"
    cls = _JOB_CLS.get(state, "failed")
    detail = " · ".join(x for x in (st["elapsed"], st["node"], st["reason"] if state == "PENDING" or cls == "failed" else "") if x and x != "None")
    e = _h.escape
    return (f'<span class="pill {cls}" title="job {jid}{" · " + e(detail) if detail else ""}">{e(state.lower())}</span>'
            + (f' <small class="muted">{e(st["elapsed"])}</small>' if st["elapsed"] else ""),
            f"{jid} {state.lower()} {st['node']}")


# ---------------------------------------------------------------- navigator

NAV_JS = r"""
(function(){
  const $ = id => document.getElementById(id);
  const q = $("nav-q"), rows = [...document.querySelectorAll("tbody tr")], n = $("nav-n"), msg = $("nav-msg");
  function apply(){ const t = q ? q.value.trim().toLowerCase() : ""; let k = 0;
    for (const r of rows) { const hit = !t || r.dataset.text.includes(t); r.style.display = hit ? "" : "none"; k += hit; }
    if (n) n.textContent = t ? `${k} / ${rows.length}` : `${rows.length}`; }
  if (q) { q.addEventListener("input", apply); q.focus(); apply(); }
  function say(text, bad){ msg.textContent = text; msg.className = "callout" + (bad ? " bad" : ""); msg.style.display = "block"; }
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
    if (j.ok) location.reload(); else say("bind failed: " + j.error, true); });
  $("bind-path").addEventListener("keydown", ev => { if (ev.key === "Enter") $("bind-go").click(); });
  const boxes = [...document.querySelectorAll("input.sel")], ub = $("unbind-go"), all = $("sel-all");
  function sync(){ const k = boxes.filter(b => b.checked).length; ub.disabled = !k; ub.textContent = k ? `Unbind selected (${k})` : "Unbind selected"; }
  boxes.forEach(b => b.addEventListener("change", sync));
  if (all) all.addEventListener("change", () => { boxes.forEach(b => { if (b.closest("tr").style.display !== "none") b.checked = all.checked; }); sync(); });
  ub.addEventListener("click", async () => {
    const names = boxes.filter(b => b.checked).map(b => b.value);
    if (!names.length) return;
    if (!confirm("Unbind " + names.length + " dataset(s)?\n\n" + names.join("\n") + "\n\n(Only the registry entry is removed; nothing in the run directories is touched.)")) return;
    ub.disabled = true;
    const j = await post("/_unbind", {names});
    if (j.ok) location.reload(); else { ub.disabled = false; say("unbind failed: " + j.error, true); } });
  sync();
})();
"""


def _dataset_state(root: Path) -> dict:
    """Per-dataset summary read from disk (ecarsi.index), for the navigator / list."""
    if not root.is_dir():
        return {"units": 0, "released": 0, "n_input": None, "final_cells": None, "stage": "missing on disk", "cls": "failed"}
    try:
        units = [root] if L.is_unit(root) else L.units(root)
        states = [index.unit_state(u) for u in units]
    except Exception as e:  # a broken run dir must not take the navigator down
        return {"units": 0, "released": 0, "n_input": None, "final_cells": None, "stage": f"unreadable: {e}", "cls": "failed"}
    released = sum(1 for s in states if s["released"])
    final = [s["final_cells"] for s in states if s["final_cells"] is not None]
    n_in = [s["n_input"] for s in states if s["n_input"] is not None]
    if not states:
        stage, cls = "no units", "neutral"
    elif released == len(states):
        stage, cls = "released", "released"
    elif any(s["stage_class"] == "failed" for s in states):
        stage, cls = "failed", "failed"
    else:
        stage, cls = states[0]["stage"] if len(states) == 1 else f"{released}/{len(states)} released", "running"
    return {"units": len(states), "released": released, "n_input": sum(n_in) if n_in else None,
            "final_cells": sum(final) if final else None, "stage": stage, "cls": cls}


def _navigator_html(items: dict[str, Path], registry_path: Path) -> str:
    e = _h.escape
    rows = []
    jids = {name: _root_job_id(p) for name, p in items.items()}
    jstates = _slurm_states([j for j in jids.values() if j])
    for name, p in sorted(items.items()):
        st = _dataset_state(p)
        cells = index._n(st["final_cells"]) or "–"
        job_html, job_text = _job_cell(jids[name], jstates.get(jids[name] or ""))
        rows.append(f'<tr data-text="{e((name + " " + str(p) + " " + st["stage"] + " " + job_text).lower())}">'
                    f'<td class="l"><input class="sel" type="checkbox" value="{e(name)}"></td>'
                    f'<td><a href="/{e(name)}/"><b>{e(name)}</b></a></td>'
                    f'<td class="num">{index._n(st["n_input"]) or "–"}</td>'
                    f'<td class="num"><b>{cells}</b>{"" if st["cls"] == "released" else " <small class=\"muted\">so far</small>" if st["final_cells"] else ""}</td>'
                    f'<td class="num">{st["released"]}/{st["units"]}</td>'
                    f'<td class="l"><span class="pill {st["cls"]}">{e(st["stage"])}</span></td>'
                    f'<td class="l">{job_html}</td>'
                    f'<td class="l muted"><code class="path">{e(str(p))}</code></td></tr>')
    hint = ("A bindable directory is an eca-rsi <b>organize root</b> (contains <code>organize/manifest.json</code> or a "
            "<code>units/</code> dir — e.g. <code>&lt;dataset&gt;/rsi</code>, the <code>&lt;root&gt;</code> you gave "
            "<code>eca-rsi run</code>) or a single <b>unit</b> (contains <code>input/organized.h5ad</code> or "
            "<code>input/manifest.json</code> — e.g. <code>&lt;root&gt;/units/&lt;unit&gt;</code>). "
            "Absolute path on the server host; a raw eca-pp <code>standardize/</code> dir or a bare h5ad is not bindable.")
    toolbar = (
        '<div style="display:flex;gap:.6rem;align-items:center;flex-wrap:wrap;margin:0 0 .8rem">'
        '<button id="bind-open" class="btn">+ Bind dataset…</button>'
        '<button id="unbind-go" class="btn danger" disabled>Unbind selected</button>'
        f'<span class="muted" style="font-size:.85rem">bind/unbind edit <code class="path">{e(str(registry_path))}</code>; nothing in the run directories is touched</span></div>'
        '<div id="bind-form" class="callout" style="display:none">'
        '<label style="display:block;font-weight:600;margin-bottom:.3rem">Directory to bind</label>'
        '<input id="bind-path" type="text" placeholder="/oak/…/<dataset>/rsi" autocomplete="off" spellcheck="false" '
        'style="width:100%;font:.92em ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;padding:.45rem .7rem;border:1px solid var(--line);border-radius:6px">'
        f'<p class="desc" style="margin:.5rem 0">{hint}</p>'
        '<div style="display:flex;gap:.6rem;align-items:center;flex-wrap:wrap">'
        '<label>name <input id="bind-name" type="text" placeholder="(default: directory basename)" autocomplete="off" '
        'style="font:inherit;padding:.35rem .6rem;border:1px solid var(--line);border-radius:6px;width:22ch"></label>'
        '<button id="bind-go" class="btn">Bind</button><button id="bind-cancel" class="btn plain">Cancel</button></div></div>'
        '<div id="nav-msg" class="callout" style="display:none"></div>'
    )
    table = (
        '<input id="nav-q" type="search" placeholder="search name / path / stage / job state…" autocomplete="off" '
        'style="width:100%;font:inherit;padding:.5rem .8rem;border:1px solid var(--line);border-radius:8px;margin:0 0 .8rem">'
        '<div class="wrap"><table><thead><tr><th class="l" style="width:1.5rem"><input id="sel-all" type="checkbox" title="select all visible"></th>'
        '<th class="l">dataset</th><th>input cells</th><th>final cells</th>'
        '<th>units released</th><th class="l">stage</th><th class="l" title="last job in <root>/jobs.log (or status.txt), asked of squeue/sacct">slurm job</th><th class="l">path</th></tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table></div>'
        if items else '<p class="empty">nothing bound yet — use <b>+ Bind dataset…</b> above or <code>eca-rsi serve scan-add &lt;dir-or-glob&gt;</code></p>'
    )
    body = (
        '<header class="top"><div><div class="crumb">ecarsi serve</div><h1>Datasets</h1></div>'
        f'<div class="event"><span id="nav-n">{len(items)}</span> bound</div></header>'
        f'<section>{toolbar}{table}</section><script>{NAV_JS}</script>'
    )
    css = (".btn{font:inherit;font-weight:600;padding:.4rem .9rem;border-radius:8px;border:1px solid var(--accent);background:var(--accent);color:#fff;cursor:pointer}"
           ".btn:disabled{opacity:.45;cursor:default}.btn.danger{background:var(--bad);border-color:var(--bad)}"
           ".btn.plain{background:var(--card);color:var(--ink);border-color:var(--line)}")
    return (
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1"><title>ecarsi serve</title>'
        f"<style>{index.CSS}{css}</style></head><body><div class=\"page\">{body}</div></body></html>"
    )


# ---------------------------------------------------------------- handler

def _render_index(root: Path, sub: str) -> str | None:
    """HTML for a landing page rendered from disk right now, or None if the
    request isn't for one. Never writes into the dataset directory (the
    pipeline steps write their own static index.html for offline use)."""
    parts = [p for p in sub.split("/") if p]
    if parts and parts[-1] == L.INDEX:
        parts = parts[:-1]
    elif parts and not sub.endswith("/"):
        return None  # a file, not a directory landing page
    if L.is_unit(root):
        return index.render_unit(root) if not parts else None
    if not parts:
        return index.render_root(root)
    if len(parts) == 2 and parts[0] == L.UNITS and L.is_unit(root / L.UNITS / parts[1]):
        return index.render_unit(root / L.UNITS / parts[1])
    return None


class Handler(http.server.SimpleHTTPRequestHandler):
    """Multi-tenant static files: first path segment selects a dataset from
    the registry, the rest is served from that directory (self.directory /
    self.path are recomputed per request, which is safe — translate_path
    reads them fresh on every call, not cached from __init__)."""

    def __init__(self, *a, registry: Registry, auth: str | None = None, **kw):
        self._registry = registry
        self._auth = auth  # "user:pass" -> HTTP basic auth enforced here, on every request; None = open
        super().__init__(*a, **kw)  # directory defaults to cwd; do_GET always overrides it before use

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
        self.send_header("WWW-Authenticate", 'Basic realm="ecarsi serve", charset="UTF-8"')
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
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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
            return self._json(403, {"ok": False, "error": "admin actions are refused through the public tunnel unless the server was started with --auth"})
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
        parts = [p for p in raw.split("/") if p]
        if not parts:
            return self._html(_navigator_html(self._registry.snapshot(), self._registry.path))
        name = parts[0]
        root = self._registry.get(name)
        if root is None:
            # `message` (the short arg) lands in the HTTP status line and
            # must be latin-1 — anything fancier (em dash, etc.) belongs in
            # `explain` (the body) instead, or send_error raises and the
            # connection dies with an empty reply, no 404 at all
            return self.send_error(404, "unknown dataset", explain=f"no dataset bound as {name!r}; see the navigator at /")
        if not root.is_dir():
            return self.send_error(404, "dataset missing", explain=f"{root} (bound as {name!r}) is not on disk any more")
        sub = "/" + "/".join(parts[1:]) + ("/" if raw.endswith("/") and len(parts) > 1 else "")
        if not raw.endswith("/") and (len(parts) == 1 or (root / sub.lstrip("/")).is_dir()):
            return self._redirect(raw + "/")  # ourselves, not SimpleHTTPRequestHandler: its redirect would drop the /<name> prefix
        try:
            page = _render_index(root, sub)
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
        sys.stderr.write(f"[serve] {self.address_string()} {format % args}\n")


# ---------------------------------------------------------------- ngrok

def start_ngrok(port: int, domain: str | None) -> tuple[subprocess.Popen, str]:
    exe = shutil.which("ngrok")
    if not exe:
        raise SystemExit("ngrok not found on PATH — install it and add your authtoken (ngrok config add-authtoken …)")
    cmd = [exe, "http", str(port), "--log", "stdout", "--log-format", "json"]
    if domain:
        cmd += ["--domain", domain]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
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
        raise SystemExit("ngrok did not report a tunnel within 30 s (see its output above)")
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
            print(f"[serve] two command-line dirs both named {p.name!r}: {extra[p.name]} and {p} — put one in the registry under another name")
            return 2
        extra[p.name] = p
    registry = Registry(reg_path, extra)
    items = registry.snapshot()
    httpd = http.server.ThreadingHTTPServer((args.bind, args.port),
                                          partial(Handler, registry=registry, auth=args.auth))
    print(f"[serve] navigator on http://{args.bind}:{args.port}/  ({len(items)} dataset(s); registry {reg_path}"
          + (f", {len(extra)} from the command line)" if extra else ")")
          + (f"  [password-protected, user {args.auth.split(':', 1)[0]!r}]" if args.auth else "  [no password]"), flush=True)
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

GENERIC_DIR_NAMES = {"rsi", "root", "run", "runs", "out", "output", "results", "eca-rsi", "ecarsi", "eca-pp", "units", "data", "sc"}


def _scan_names(dirs: list[Path], taken: dict[str, Path]) -> dict[Path, str]:
    """Names for a batch of scanned dirs. Path components that carry no
    information (rsi, eca-pp, units, ...) are dropped; each dir starts with
    its last informative component and every dir whose name collides —
    within the batch or with an existing entry for another path — is
    qualified by one more component, symmetrically (Brain across three
    collections becomes mca1.1-Brain / mca2.0-Brain / mca3.0-Brain, not
    Brain / Brain-rsi / eca-pp-Brain)."""
    comps = {d: [c for c in d.parts[1:] if c not in GENERIC_DIR_NAMES] or [d.name] for d in dirs}
    depth = {d: 1 for d in dirs}
    name = lambda d: "-".join(comps[d][-depth[d]:])
    while True:
        by: dict[str, list[Path]] = {}
        for d in dirs:
            by.setdefault(name(d), []).append(d)
        clash = [d for n, ds in by.items() for d in ds
                 if len(ds) > 1 or (n in taken and taken[n] != d)]
        clash = [d for d in clash if depth[d] < len(comps[d])]  # can't qualify further
        if not clash:
            return {d: name(d) for d in dirs}
        for d in clash:
            depth[d] += 1


def cmd_scan_add(args: argparse.Namespace) -> int:
    reg = Registry(Path(args.registry).expanduser().resolve())
    matches = sorted({Path(m).resolve() for pat in args.glob for m in _glob.glob(os.path.expandvars(os.path.expanduser(pat)))})
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
        print(f"[serve] nothing new to add ({len(matches)} matched, {len(skipped)} skipped, {len(matches) - len(skipped)} already in)")
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
    print(f"[serve] wrote {len(items)} entr{'y' if len(items) == 1 else 'ies'} -> {out}")
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
    print(f"[serve] {'replaced with' if args.replace else 'merged'} {len(incoming)} entr{'y' if len(incoming) == 1 else 'ies'} from {src} -> {reg_path} ({len(merged)} total)")
    return 0


# ---------------------------------------------------------------- cli

def main(argv: list[str]) -> int:
    def registry_arg(p):
        p.add_argument("--registry", default=str(default_registry()), metavar="FILE",
                       help="registry file, JSON {name: path} (default $XDG_CONFIG_HOME/ecarsi/registry.json)")

    if argv and argv[0] in SUBCOMMANDS:
        ap = argparse.ArgumentParser(prog="ecarsi.serve", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
        sub = ap.add_subparsers(dest="cmd", required=True)

        p = sub.add_parser("scan-add", help="add every organize root / unit matching the globs to the registry")
        p.add_argument("glob", nargs="+", help="dirs or globs, e.g. '$OAK/data/sc/*/eca-pp/*/rsi' (quote it)")
        p.add_argument("--name", default=None, help="name for the (single) dataset instead of the derived one")
        p.add_argument("--dry-run", action="store_true", help="show what would be added, add nothing")
        registry_arg(p)
        p.set_defaults(func=cmd_scan_add)

        p = sub.add_parser("remove", help="remove datasets from the registry by name")
        p.add_argument("name", nargs="+")
        registry_arg(p)
        p.set_defaults(func=cmd_remove)

        p = sub.add_parser("list", help="list the registry, with each dataset's stage and final cells")
        p.add_argument("--json", action="store_true")
        registry_arg(p)
        p.set_defaults(func=cmd_list)

        p = sub.add_parser("dump", help="copy the registry file to PATH (no PATH: print it)")
        p.add_argument("path", nargs="?", default=None)
        registry_arg(p)
        p.set_defaults(func=cmd_dump)

        p = sub.add_parser("reload", help="merge another registry file into the registry")
        p.add_argument("path")
        p.add_argument("--replace", action="store_true", help="replace the whole list instead of merging")
        registry_arg(p)
        p.set_defaults(func=cmd_reload)

        args = ap.parse_args(argv)
        return args.func(args)

    ap = argparse.ArgumentParser(prog="ecarsi.serve", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog="registry subcommands: " + " | ".join(SUBCOMMANDS) + "  (ecarsi serve <sub> --help)")
    ap.add_argument("dir", nargs="*", help="extra dataset dirs to serve for this process only (name = basename)")
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--bind", default="127.0.0.1", help="default local only; 0.0.0.0 to expose on the LAN")
    ap.add_argument("--ngrok", action="store_true", help="also open an ngrok tunnel to this port")
    ap.add_argument("--domain", default=None, help="reserved ngrok domain (implies --ngrok)")
    ap.add_argument("--auth", default=None, metavar="USER:PASS",
                    help="web-level password (HTTP basic auth, enforced by the server on every request, local or tunnel); default none")
    registry_arg(ap)
    args = ap.parse_args(argv)
    return cmd_serve(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

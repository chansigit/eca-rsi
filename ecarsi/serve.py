"""ecarsi.serve — a persistent navigator server for eca-rsi run reports.

    ecarsi serve start [dir] [--name NAME] [--port 8899] [--bind 127.0.0.1]
                       [--ngrok [--domain csj.example.app] [--auth user:pass]]
                       [--session ecarsi-serve] [--state DIR] [--attach]
    ecarsi serve attach [--session ecarsi-serve]
    ecarsi serve stop   [--session ecarsi-serve] [--state DIR]
    ecarsi serve status [--session ecarsi-serve] [--state DIR]
    ecarsi serve bind   <dir> [--name NAME] [--state DIR]
    ecarsi serve unbind <name> [--state DIR]
    ecarsi serve list   [--state DIR]
    ecarsi serve dump   [path] [--state DIR]
    ecarsi serve reload [path] [--state DIR]

One long-running daemon serves every bound dataset (an organize root or a
single unit — see ecarsi.layout) under its own name: `/<name>/...`. The
landing pages are re-rendered from disk on every request (ecarsi.index), so
a run that is still going shows its current stage; `/` is a navigator page
listing everything currently bound. Datasets can be bound and unbound at
runtime without restarting the daemon or its ngrok tunnel — `bind`/`unbind`
talk to the running daemon over a local admin socket
(<state>/admin.sock, not reachable through the public tunnel).

The daemon always runs inside a tmux session (default name `ecarsi-serve`)
so it can be watched and detached like any other tmux session: `attach`
drops you into its live output, the ordinary tmux prefix+d detaches you
back to your shell (no custom code — it's just tmux), and the daemon keeps
running. `stop` tears the whole thing down (daemon, its ngrok tunnel, the
tmux session).

The bound-dataset list is in-memory only — a daemon restart forgets it.
`dump`/`reload` are explicit snapshot/restore commands, not automatic.

Default: local only (http://127.0.0.1:PORT). --ngrok additionally opens ONE
ngrok tunnel covering every bound dataset (ngrok binary + authtoken are the
user's responsibility; so are account limits such as one agent session per
free account). --domain uses a reserved domain instead of a random URL;
--auth adds HTTP basic auth on the tunnel — note this is the only thing
gating the admin operations from the public internet, since ngrok forwards
to 127.0.0.1 same as a local request; use it if the daemon's tunnel is ever
left open to untrusted networks.
"""

from __future__ import annotations

import argparse
import html as _h
import http.server
import json
import os
import shlex
import shutil
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import time
from functools import partial
from pathlib import Path

from . import index
from . import layout as L

DEFAULT_SESSION = "ecarsi-serve"


# ---------------------------------------------------------------- registry

class Registry:
    """The live name -> dataset-dir mapping. In-memory only; see dump/reload."""

    def __init__(self) -> None:
        self._d: dict[str, Path] = {}
        self._lock = threading.Lock()

    def bind(self, name: str, path: Path, force: bool = False) -> None:
        path = Path(path)
        if not (L.is_root(path) or L.is_unit(path)):
            raise ValueError(f"{path} is neither an organize root nor a unit dir (see ecarsi.layout)")
        with self._lock:
            existing = self._d.get(name)
            if not force and existing is not None and existing != path:
                raise ValueError(f"name {name!r} already bound to {existing} — use --name to disambiguate or unbind it first")
            self._d[name] = path

    def unbind(self, name: str) -> None:
        with self._lock:
            if name not in self._d:
                raise ValueError(f"nothing bound as {name!r}")
            del self._d[name]

    def get(self, name: str) -> Path | None:
        with self._lock:
            return self._d.get(name)

    def snapshot(self) -> dict[str, Path]:
        with self._lock:
            return dict(self._d)

    def dump(self, path: Path) -> None:
        data = {k: str(v) for k, v in self.snapshot().items()}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, sort_keys=True))

    def reload(self, path: Path) -> int:
        if not path.is_file():
            raise ValueError(f"{path} does not exist")
        data = json.loads(path.read_text())
        for name, p in data.items():
            self.bind(name, Path(p), force=True)
        return len(data)


# ---------------------------------------------------------------- handler

def _refresh_index(root: Path, mode: str, sub: str) -> None:
    """Re-render index.html on disk before serving it, so it reflects the
    current run state (ecarsi.index). Mirrors the old single-tenant logic,
    just parameterized over which bound dataset this request landed on."""
    parts = [p for p in sub.split("/") if p]
    is_index = not parts or parts[-1] == L.INDEX
    if not is_index:
        return
    try:
        if mode == "unit":
            if len(parts) <= 1:
                index.write_unit_index(root)
        elif len(parts) <= 1:
            index.write_root_index(root)
        elif len(parts) == 3 and parts[0] == L.UNITS:
            unit = root / L.UNITS / parts[1]
            if L.is_unit(unit):
                index.write_unit_index(unit)
    except Exception as e:  # a broken page must not take the server down
        sys.stderr.write(f"[serve] index refresh failed for {sub}: {e}\n")


NAV_JS = r"""
(function(){
  const q = document.getElementById("nav-q"), rows = [...document.querySelectorAll("tbody tr")], n = document.getElementById("nav-n");
  function apply(){ const t = q.value.trim().toLowerCase(); let k = 0;
    for (const r of rows) { const hit = !t || r.dataset.text.includes(t); r.style.display = hit ? "" : "none"; k += hit; }
    n.textContent = t ? `${k} / ${rows.length}` : `${rows.length}`; }
  q.addEventListener("input", apply); q.focus(); apply();
})();
"""


def _dataset_state(root: Path) -> dict:
    """Per-bound-dataset summary read from disk (ecarsi.index), for the navigator."""
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


def _navigator_html(items: dict[str, Path]) -> str:
    e = _h.escape
    rows = []
    for name, p in sorted(items.items()):
        st = _dataset_state(p)
        cells = index._n(st["final_cells"]) or "–"
        rows.append(f'<tr data-text="{e((name + " " + str(p) + " " + st["stage"]).lower())}">'
                    f'<td><a href="/{e(name)}/"><b>{e(name)}</b></a></td>'
                    f'<td class="num">{index._n(st["n_input"]) or "–"}</td>'
                    f'<td class="num"><b>{cells}</b>{"" if st["cls"] == "released" else " <small class=\"muted\">so far</small>" if st["final_cells"] else ""}</td>'
                    f'<td class="num">{st["released"]}/{st["units"]}</td>'
                    f'<td class="l"><span class="pill {st["cls"]}">{e(st["stage"])}</span></td>'
                    f'<td class="l muted"><code class="path">{e(str(p))}</code></td></tr>')
    body = (
        '<header class="top"><div><div class="crumb">ecarsi serve</div><h1>Datasets</h1></div>'
        f'<div class="event"><span id="nav-n">{len(items)}</span> bound</div></header>'
        + ('<section><input id="nav-q" type="search" placeholder="search name / path / stage…" autocomplete="off" '
           'style="width:100%;font:inherit;padding:.5rem .8rem;border:1px solid var(--line);border-radius:8px;margin:0 0 .8rem">'
           '<div class="wrap"><table><thead><tr><th class="l">dataset</th><th>input cells</th><th>final cells</th>'
           '<th>units released</th><th class="l">stage</th><th class="l">path</th></tr></thead>'
           f'<tbody>{"".join(rows)}</tbody></table></div></section><script>{NAV_JS}</script>'
           if items else '<p class="empty">nothing bound yet — <code>ecarsi serve bind &lt;dir&gt;</code></p>')
    )
    return (
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1"><title>ecarsi serve</title>'
        f"<style>{index.CSS}</style></head><body><div class=\"page\">{body}</div></body></html>"
    )


class Handler(http.server.SimpleHTTPRequestHandler):
    """Multi-tenant static files: first path segment selects a bound
    dataset from the registry, the rest is served exactly like the old
    single-tenant handler did (self.directory/self.path are recomputed per
    request, which is safe — they're read fresh by translate_path on every
    call, not cached from __init__)."""

    def __init__(self, *a, registry: Registry, **kw):
        self._registry = registry
        super().__init__(*a, **kw)  # directory defaults to cwd; do_GET always overrides it before use

    def do_GET(self):
        raw = self.path.split("?", 1)[0]
        parts = [p for p in raw.split("/") if p]
        if not parts:
            return self._navigator()
        name = parts[0]
        root = self._registry.get(name)
        if root is None:
            # `message` (the short arg) lands in the HTTP status line and
            # must be latin-1 — anything fancier (em dash, etc.) belongs in
            # `explain` (the body) instead, or send_error raises and the
            # connection dies with an empty reply, no 404 at all
            return self.send_error(404, "unknown dataset", explain=f"no dataset bound as {name!r}; see the navigator at /")
        if len(parts) == 1 and not raw.endswith("/"):
            return self._redirect(raw + "/")
        sub = "/" + "/".join(parts[1:])
        mode = "unit" if L.is_unit(root) else "root"
        _refresh_index(root, mode, sub)
        self.directory = str(root)
        self.path = sub
        http.server.SimpleHTTPRequestHandler.do_GET(self)

    def _navigator(self) -> None:
        enc = _navigator_html(self._registry.snapshot()).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(enc)))
        self.end_headers()
        self.wfile.write(enc)

    def _redirect(self, location: str) -> None:
        self.send_response(301)
        self.send_header("Location", location)
        self.end_headers()

    def log_message(self, format, *args):  # noqa: A002 - matches BaseHTTPRequestHandler's signature
        sys.stderr.write(f"[serve] {self.address_string()} {format % args}\n")


# ---------------------------------------------------------------- ngrok

def start_ngrok(port: int, domain: str | None, auth: str | None) -> tuple[subprocess.Popen, str]:
    exe = shutil.which("ngrok")
    if not exe:
        raise SystemExit("ngrok not found on PATH — install it and add your authtoken (ngrok config add-authtoken …)")
    cmd = [exe, "http", str(port), "--log", "stdout", "--log-format", "json"]
    if domain:
        cmd += ["--domain", domain]
    if auth:
        cmd += ["--basic-auth", auth]
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


# ---------------------------------------------------------------- admin socket

class _AdminHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        line = self.rfile.readline()
        if not line:
            return
        try:
            req = json.loads(line.decode())
            op = req["op"]
        except Exception:
            return self._reply({"ok": False, "error": "bad request"})
        reg: Registry = self.server.registry  # type: ignore[attr-defined]
        try:
            if op == "ping":
                self._reply({"ok": True})
            elif op == "bind":
                reg.bind(req["name"], Path(req["path"]))
                self._reply({"ok": True})
            elif op == "unbind":
                reg.unbind(req["name"])
                self._reply({"ok": True})
            elif op == "list":
                self._reply({"ok": True, "items": {k: str(v) for k, v in reg.snapshot().items()}})
            elif op == "dump":
                p = Path(req["path"])
                reg.dump(p)
                self._reply({"ok": True, "path": str(p)})
            elif op == "reload":
                n = reg.reload(Path(req["path"]))
                self._reply({"ok": True, "added": n})
            else:
                self._reply({"ok": False, "error": f"unknown op {op!r}"})
        except Exception as e:
            self._reply({"ok": False, "error": str(e)})

    def _reply(self, obj: dict) -> None:
        self.wfile.write((json.dumps(obj) + "\n").encode())


class _AdminServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

    def __init__(self, sock_path: Path, registry: Registry):
        if sock_path.exists():
            sock_path.unlink()
        super().__init__(str(sock_path), _AdminHandler)
        self.registry = registry
        os.chmod(str(sock_path), 0o600)  # local-user-only; this is the control plane, not the public port


def _admin_call(state: Path, op: str, timeout: float = 5.0, **kw) -> dict:
    sock_path = state / "admin.sock"
    if not sock_path.exists():
        return {"ok": False, "error": "daemon not running (no admin socket) — `ecarsi serve start` first"}
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(str(sock_path))
        s.sendall((json.dumps({"op": op, **kw}) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
        return json.loads(buf.decode()) if buf else {"ok": False, "error": "no response from daemon"}
    except OSError as e:
        return {"ok": False, "error": f"could not reach daemon: {e}"}
    finally:
        s.close()


# ---------------------------------------------------------------- daemon (runs inside tmux)

def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _daemon_main(args: argparse.Namespace) -> int:
    state = Path(args.state)
    state.mkdir(parents=True, exist_ok=True)
    registry = Registry()
    httpd = http.server.ThreadingHTTPServer((args.bind, args.port), partial(Handler, registry=registry))
    admin = _AdminServer(state / "admin.sock", registry)
    threading.Thread(target=admin.serve_forever, daemon=True).start()
    (state / "pid").write_text(str(os.getpid()))
    print(f"[serve] navigator on http://{args.bind}:{args.port}/  (state {state})", flush=True)

    tunnel = None
    npf = state / "ngrok_pid"
    if args.ngrok:
        tunnel, url = start_ngrok(args.port, args.domain, args.auth)
        print(f"[serve] public: {url}/" + (f"  (basic auth {args.auth.split(':', 1)[0]})" if args.auth else ""), flush=True)
        # recorded separately from the main pidfile so `stop` can still reap
        # this child if the daemon ever dies without running its own cleanup
        # (crash, OOM-kill, kill -9) — otherwise it's an orphan forever
        npf.write_text(str(tunnel.pid))

    def shutdown(*_):
        if tunnel and tunnel.poll() is None:
            tunnel.terminate()
        threading.Thread(target=httpd.shutdown, daemon=True).start()
        threading.Thread(target=admin.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
        admin.server_close()
        if tunnel and tunnel.poll() is None:
            tunnel.terminate()
        npf.unlink(missing_ok=True)
        (state / "pid").unlink(missing_ok=True)
        (state / "admin.sock").unlink(missing_ok=True)
    print("[serve] stopped", flush=True)
    return 0


# ---------------------------------------------------------------- tmux process lifecycle

def _tmux_has_session(session: str) -> bool:
    return subprocess.run(["tmux", "has-session", "-t", session],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def _state_dir(args: argparse.Namespace) -> Path:
    if getattr(args, "state", None):
        return Path(args.state).resolve()
    base = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state")))
    return base / "ecarsi" / "serve"


def cmd_start(args: argparse.Namespace) -> int:
    state = _state_dir(args)
    state.mkdir(parents=True, exist_ok=True)
    if _tmux_has_session(args.session):
        print(f"[serve] already running (tmux session {args.session!r}) — `ecarsi serve attach` or `ecarsi serve stop` first")
        return 1
    cmd = [sys.executable, "-m", "ecarsi.serve", "_daemon", "--port", str(args.port),
           "--bind", args.bind, "--state", str(state)]
    if args.ngrok or args.domain:
        cmd.append("--ngrok")
    if args.domain:
        cmd += ["--domain", args.domain]
    if args.auth:
        cmd += ["--auth", args.auth]
    subprocess.run(["tmux", "new-session", "-d", "-s", args.session, shlex.join(cmd)], check=True,
                   cwd=str(Path(__file__).resolve().parent.parent))
    deadline = time.time() + 15
    up = False
    while time.time() < deadline:
        if (state / "admin.sock").exists() and _admin_call(state, "ping", timeout=2).get("ok"):
            up = True
            break
        time.sleep(0.3)
    if not up:
        print(f"[serve] daemon did not come up in time; check `tmux attach -t {args.session}`")
        return 1
    print(f"[serve] started (tmux session {args.session!r}); attach with `ecarsi serve attach`")
    if args.dir:
        name = args.name or Path(args.dir).resolve().name
        r = _admin_call(state, "bind", name=name, path=str(Path(args.dir).resolve()))
        print(f"[serve] bound {name!r} -> {args.dir}" if r.get("ok") else f"[serve] bind failed: {r.get('error')}")
    if args.attach:
        os.execvp("tmux", ["tmux", "attach", "-t", args.session])
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    state = _state_dir(args)
    pf = state / "pid"
    if pf.is_file():
        pid = int(pf.read_text())
        if _alive(pid):
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
            print(f"[serve] stopped pid {pid}")
            for _ in range(20):
                if not _alive(pid):
                    break
                time.sleep(0.2)
        else:
            print(f"[serve] pid {pid} was not running")
        pf.unlink(missing_ok=True)
    else:
        print("[serve] no pidfile — daemon may not be running")
    # independent reap: if the daemon died earlier without running its own
    # cleanup (crash, OOM-kill, kill -9), its ngrok child is orphaned and the
    # SIGTERM above never reaches it — kill it directly by its own pid
    npf = state / "ngrok_pid"
    if npf.is_file():
        npid = int(npf.read_text())
        if _alive(npid):
            try:
                os.kill(npid, signal.SIGTERM)
            except OSError:
                pass
            print(f"[serve] stopped orphaned ngrok pid {npid}")
        npf.unlink(missing_ok=True)
    if _tmux_has_session(args.session):
        subprocess.run(["tmux", "kill-session", "-t", args.session])
    return 0


def cmd_attach(args: argparse.Namespace) -> int:
    if not _tmux_has_session(args.session):
        print(f"[serve] no tmux session {args.session!r} — `ecarsi serve start` first")
        return 1
    os.execvp("tmux", ["tmux", "attach", "-t", args.session])


def cmd_status(args: argparse.Namespace) -> int:
    alive = _tmux_has_session(args.session)
    print(f"[serve] session {args.session!r}: {'running' if alive else 'not running'}")
    if not alive:
        return 0
    r = _admin_call(_state_dir(args), "list")
    if not r.get("ok"):
        print(f"  (could not query admin socket: {r.get('error')})")
        return 0
    items = r["items"]
    if not items:
        print("  (nothing bound)")
    for name, path in sorted(items.items()):
        print(f"  {name:20s} {path}")
    return 0


def cmd_bind(args: argparse.Namespace) -> int:
    d = Path(args.dir).resolve()
    name = args.name or d.name
    r = _admin_call(_state_dir(args), "bind", name=name, path=str(d))
    if r.get("ok"):
        print(f"[serve] bound {name!r} -> {d}")
        return 0
    print(f"[serve] bind failed: {r.get('error')}")
    return 1


def cmd_unbind(args: argparse.Namespace) -> int:
    r = _admin_call(_state_dir(args), "unbind", name=args.name)
    if r.get("ok"):
        print(f"[serve] unbound {args.name!r}")
        return 0
    print(f"[serve] unbind failed: {r.get('error')}")
    return 1


def cmd_list(args: argparse.Namespace) -> int:
    r = _admin_call(_state_dir(args), "list")
    if not r.get("ok"):
        print(f"[serve] {r.get('error')}")
        return 1
    items = r["items"]
    if not items:
        print("(nothing bound)")
    for name, path in sorted(items.items()):
        print(f"{name:20s} {path}")
    return 0


def cmd_dump(args: argparse.Namespace) -> int:
    state = _state_dir(args)
    path = Path(args.path) if args.path else state / "registry.json"
    r = _admin_call(state, "dump", path=str(path))
    if r.get("ok"):
        print(f"[serve] dumped registry -> {r['path']}")
        return 0
    print(f"[serve] dump failed: {r.get('error')}")
    return 1


def cmd_reload(args: argparse.Namespace) -> int:
    state = _state_dir(args)
    path = Path(args.path) if args.path else state / "registry.json"
    r = _admin_call(state, "reload", path=str(path))
    if r.get("ok"):
        print(f"[serve] reloaded {r.get('added', '?')} binding(s) from {path}")
        return 0
    print(f"[serve] reload failed: {r.get('error')}")
    return 1


# ---------------------------------------------------------------- cli

def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="ecarsi.serve", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def state_arg(p):
        p.add_argument("--state", default=None,
                       help="state dir (default $XDG_STATE_HOME/ecarsi/serve or ~/.local/state/ecarsi/serve)")

    def session_arg(p):
        p.add_argument("--session", default=DEFAULT_SESSION, help=f"tmux session name (default {DEFAULT_SESSION})")

    p = sub.add_parser("start", help="start the daemon inside a tmux session; optionally bind one dir immediately")
    p.add_argument("dir", nargs="?", default=None, help="optional: bind this dir right away")
    p.add_argument("--name", default=None, help="bind name for dir (default: its basename)")
    p.add_argument("--port", type=int, default=8899)
    p.add_argument("--bind", default="127.0.0.1", help="default local only; 0.0.0.0 to expose on the LAN")
    p.add_argument("--ngrok", action="store_true", help="also open an ngrok tunnel to this port")
    p.add_argument("--domain", default=None, help="reserved ngrok domain (implies --ngrok)")
    p.add_argument("--auth", default=None, metavar="USER:PASS", help="HTTP basic auth on the ngrok tunnel")
    p.add_argument("--attach", action="store_true", help="attach to the tmux session after starting")
    session_arg(p)
    state_arg(p)
    p.set_defaults(func=cmd_start)

    p = sub.add_parser("stop", help="stop the daemon, its ngrok tunnel, and the tmux session")
    session_arg(p)
    state_arg(p)
    p.set_defaults(func=cmd_stop)

    p = sub.add_parser("attach", help="attach to the running daemon's tmux session (tmux prefix+d to detach)")
    session_arg(p)
    p.set_defaults(func=cmd_attach)

    p = sub.add_parser("status", help="is the daemon running, and what's bound")
    session_arg(p)
    state_arg(p)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("bind", help="bind a dataset dir into the running daemon")
    p.add_argument("dir")
    p.add_argument("--name", default=None)
    state_arg(p)
    p.set_defaults(func=cmd_bind)

    p = sub.add_parser("unbind", help="unbind a dataset by name")
    p.add_argument("name")
    state_arg(p)
    p.set_defaults(func=cmd_unbind)

    p = sub.add_parser("list", help="list bound datasets")
    state_arg(p)
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("dump", help="write the current bindings to a JSON file (explicit snapshot, not automatic)")
    p.add_argument("path", nargs="?", default=None)
    state_arg(p)
    p.set_defaults(func=cmd_dump)

    p = sub.add_parser("reload", help="merge bindings from a JSON file into the running daemon")
    p.add_argument("path", nargs="?", default=None)
    state_arg(p)
    p.set_defaults(func=cmd_reload)

    p = sub.add_parser("_daemon", help=argparse.SUPPRESS)  # internal: launched by `start` inside tmux
    p.add_argument("--port", type=int, required=True)
    p.add_argument("--bind", default="127.0.0.1")
    p.add_argument("--ngrok", action="store_true")
    p.add_argument("--domain", default=None)
    p.add_argument("--auth", default=None)
    p.add_argument("--state", required=True)
    p.set_defaults(func=_daemon_main)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

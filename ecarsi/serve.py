"""ecarsi.serve — put a run's reports on the web.

    python -m ecarsi.serve <root | unit> [--port 8899] [--bind 127.0.0.1]
                           [--ngrok [--domain csj.example.app] [--auth user:pass]]
                           [--detach | --stop]

Serves the directory ecarsi.layout describes (an organize root, or a single
unit) as static files. The landing pages (index.html at the root and in
every unit) are re-rendered from disk on every request — see ecarsi.index —
so a run that is still going shows its current stage, finished samples,
rounds in progress and the needs-review list so far. Reports written by
osp / msp / zmip are served as they are.

Default: local only (http://127.0.0.1:PORT). --ngrok additionally opens an
ngrok tunnel to the same port (ngrok binary + authtoken are the user's
responsibility; so are account limits such as one agent session per free
account). --domain uses a reserved domain instead of a random URL; --auth
adds HTTP basic auth on the tunnel.

--detach runs the whole thing in the background (pid + log under
<dir>/.serve/); --stop ends a detached server.
"""

from __future__ import annotations

import argparse
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

SERVE_DIR = ".serve"


# ---------------------------------------------------------------- handler

class Handler(http.server.SimpleHTTPRequestHandler):
    """Static files, with index pages re-rendered from disk when requested."""

    def __init__(self, *a, root: Path, mode: str, **kw):
        self._root, self._mode = root, mode
        super().__init__(*a, directory=str(root), **kw)

    def do_GET(self):
        self._refresh(self.path.split("?", 1)[0])
        super().do_GET()

    def _refresh(self, path: str) -> None:
        parts = [p for p in path.split("/") if p]
        is_index = not parts or parts[-1] == L.INDEX
        if not is_index:
            return
        try:
            if self._mode == "unit":
                if len(parts) <= 1:
                    index.write_unit_index(self._root)
            elif len(parts) <= 1:
                index.write_root_index(self._root)
            elif len(parts) == 3 and parts[0] == L.UNITS:
                unit = self._root / L.UNITS / parts[1]
                if L.is_unit(unit):
                    index.write_unit_index(unit)
        except Exception as e:  # a broken page must not take the server down
            sys.stderr.write(f"[serve] index refresh failed for {path}: {e}\n")

    def log_message(self, fmt, *args):
        sys.stderr.write(f"[serve] {self.address_string()} {fmt % args}\n")


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


# ---------------------------------------------------------------- run

def serve(root: Path, port: int, bind: str, ngrok: bool, domain: str | None, auth: str | None) -> int:
    mode = "unit" if L.is_unit(root) else "root" if L.is_root(root) else None
    if mode is None:
        raise SystemExit(f"{root} is neither an organize root nor a unit dir (see ecarsi.layout)")
    index.write_all(root)
    httpd = http.server.ThreadingHTTPServer((bind, port), partial(Handler, root=root, mode=mode))
    print(f"[serve] {mode} {root}\n[serve] local:  http://{bind}:{port}/", flush=True)
    tunnel = None
    if ngrok:
        tunnel, url = start_ngrok(port, domain, auth)
        print(f"[serve] public: {url}/" + (f"  (basic auth {auth.split(':', 1)[0]})" if auth else ""), flush=True)

    def stop(*_):
        if tunnel and tunnel.poll() is None:
            tunnel.terminate()
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
        if tunnel and tunnel.poll() is None:
            tunnel.terminate()
    print("[serve] stopped", flush=True)
    return 0


def _pidfile(root: Path) -> Path:
    return root / SERVE_DIR / "pid"


def detach(root: Path, argv: list[str]) -> int:
    pf = _pidfile(root)
    if pf.is_file() and _alive(int(pf.read_text())):
        print(f"[serve] already running (pid {pf.read_text().strip()}); --stop first")
        return 1
    pf.parent.mkdir(exist_ok=True)
    log = pf.parent / "serve.log"
    cmd = [sys.executable, "-m", "ecarsi.serve", *[a for a in argv if a != "--detach"]]
    with open(log, "ab") as lf:
        proc = subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT, start_new_session=True,
                                cwd=str(Path(__file__).resolve().parent.parent))
    pf.write_text(str(proc.pid))
    time.sleep(3)
    if proc.poll() is not None:
        print(f"[serve] exited immediately (rc={proc.returncode}); see {log}")
        return 1
    print(f"[serve] detached pid {proc.pid}; log {log}")
    print(log.read_text()[-2000:])
    return 0


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def stop_detached(root: Path) -> int:
    pf = _pidfile(root)
    if not pf.is_file():
        print("[serve] nothing detached here")
        return 1
    pid = int(pf.read_text())
    if _alive(pid):
        os.killpg(os.getpgid(pid), signal.SIGTERM)  # server + its ngrok child
        print(f"[serve] stopped pid {pid}")
    else:
        print(f"[serve] pid {pid} was not running")
    pf.unlink()
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="ecarsi.serve", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dir", help="organize root (serves every unit) or one unit dir")
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--bind", default="127.0.0.1", help="default local only; 0.0.0.0 to expose on the LAN")
    ap.add_argument("--ngrok", action="store_true", help="also open an ngrok tunnel to this port")
    ap.add_argument("--domain", default=None, help="reserved ngrok domain (implies --ngrok)")
    ap.add_argument("--auth", default=None, metavar="USER:PASS", help="HTTP basic auth on the ngrok tunnel")
    ap.add_argument("--detach", action="store_true", help="run in the background (pid/log under <dir>/.serve/)")
    ap.add_argument("--stop", action="store_true", help="stop a detached server for <dir>")
    args = ap.parse_args(argv)
    root = Path(args.dir).resolve()
    if args.stop:
        return stop_detached(root)
    if args.detach:
        return detach(root, argv)
    return serve(root, args.port, args.bind, args.ngrok or bool(args.domain), args.domain, args.auth)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

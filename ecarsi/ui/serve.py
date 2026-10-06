"""ecarsi.serve — Periscope, a stateless navigator server for eca-rsi run reports.

Periscope is the name of the web UI (page titles, the sidebar brand, the
startup line); the CLI verb stays `serve`.

    ecarsi serve [dir...] [--registry FILE] [--port 8899] [--bind 127.0.0.1]
                 [--ngrok [--domain csj.example.app]] [--auth user:pass | --auth-file FILE]
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
rendered from the run directory on every request (ecarsi.ui.index), so a run
that is still going shows its current stage; the server never writes into
a dataset directory.

The single source of truth for what is served by hand is the DATASET LIST
(default $XDG_CONFIG_HOME/ecarsi/periscope-datasets.json, i.e. ~/.config/ecarsi/
periscope-datasets.json), a JSON object {name: path}. The server re-reads it whenever
its mtime changes, so `scan-add` / `remove` — and the navigator's Bind /
Unbind buttons, which edit the same file — take effect within a request,
without talking to the running process. Kill and restart the server on any
host and the same list comes back. Directories given on the `serve`
command line are served in addition, for this process only.

A fourth source, under the file and the command line, is the DISPLAY ZONES: the results file
(default $XDG_CONFIG_HOME/ecarsi/results.json, `--results`) names a `display_root` and may list
`more_display_roots`; every `<root>/<collection>/<dataset>/<run>/display.json` there is served under
the name it records. The server rescans them every 10 minutes in the background and re-reads the
results file each time, so a new display zone or root appears without a restart.

`dump` copies the registry file elsewhere (or prints it); `reload` merges
another such file into it (`--replace` to swap the whole list) — handy for
keeping several lists, e.g. one per project.

Default: local only (http://127.0.0.1:PORT). --ngrok additionally opens ONE
ngrok tunnel covering everything, for an attended session in the foreground only:
Sherlock forbids unattended tunnels, so ops/start-periscope.sh, which runs Periscope
in the background, never passes it (ngrok binary + authtoken are the user's
responsibility; so are account limits such as one agent session per free
account). --domain uses a reserved domain instead of a random URL;
--auth USER:PASS (or --auth-file, a file holding USER:PASS, which keeps it out of
the process list) puts a password on the whole site (HTTP basic auth,
checked by this server on every request — local, LAN or tunnel; ngrok is
not involved). Default: no password, so day-to-day debugging is prompt-free.
The navigator's Bind / Unbind buttons (POST /_bind, /_unbind) are refused
for requests arriving through the tunnel (ngrok stamps X-Forwarded-For)
unless a password is set; local requests always may.
"""

from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import hmac
import http.server
import json
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
from functools import partial
from pathlib import Path

from .home import APP, _home_html, _navigator_html, _parse_at, _render_index, fleet_history, history_at
from .registry import (Registry, _check_dataset, cmd_dump, cmd_list, cmd_reload, cmd_remove, cmd_scan_add,
                       default_registry, default_results)
from .fleet import ControlVerdicts, StateCache, _dataset_state, unstale

SUBCOMMANDS = ("scan-add", "remove", "list", "dump", "reload")


# ---------------------------------------------------------------- registry


# ---------------------------------------------------------------- navigator


# ---------------------------------------------------------------- handler


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
        base = states.get if states else _dataset_state  # fleet pages: cached states when a warmer runs
        # Every fleet surface (overview, sidebar, history) reads states through here, so they cannot
        # disagree about which runs are failed: the sidebar once skipped unstale and counted 7 more.
        self._state = lambda p: unstale(base(p), verdicts)
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
    if args.auth_file:
        args.auth = Path(args.auth_file).expanduser().read_text().strip()
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
    registry = Registry(reg_path, extra, published=verdicts.runs, config=Path(args.results).expanduser())
    registry.scan_display()
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


# ---------------------------------------------------------------- cli


def main(argv: list[str]) -> int:
    def registry_arg(p):
        p.add_argument(
            "--registry",
            default=str(default_registry()),
            metavar="FILE",
            help="dataset list, JSON {name: path} (default $XDG_CONFIG_HOME/ecarsi/periscope-datasets.json)",
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
        "--results",
        default=str(default_results()),
        metavar="FILE",
        help="results file, JSON {\"display_root\": DIR, \"more_display_roots\": [DIR, ...], ...}: the display zones to serve "
        "(default $XDG_CONFIG_HOME/ecarsi/results.json)",
    )
    ap.add_argument(
        "--auth",
        default=None,
        metavar="USER:PASS",
        help="web-level password (HTTP basic auth, enforced by the server on every request, local or tunnel); default none",
    )
    ap.add_argument("--auth-file", default=None, metavar="FILE", help="read USER:PASS for --auth from FILE")
    registry_arg(ap)
    args = ap.parse_args(argv)
    return cmd_serve(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

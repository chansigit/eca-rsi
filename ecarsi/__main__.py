"""eca-rsi — the one entry point of the main line.

    eca-rsi run       <eca-pp-dir> <root> [--rounds N] [--cap 10] [--serve [PORT]] [--ngrok] [--domain D] [--auth U:P]
    eca-rsi organize  <eca-pp-dir> <root>
    eca-rsi persample <unit> [...]           eca-rsi loop   <unit> [...]
    eca-rsi crosssample <unit> [round_dir]   eca-rsi zoomin <unit> [round_dir]
    eca-rsi ledger    <unit> [round dirs]    eca-rsi index  <root|unit>
    eca-rsi prune     <root|unit> [--dry-run]  (runs by itself after every release unless --no-prune)
    eca-rsi serve     [dir...] [--registry F] [--port] [--ngrok --domain D] [--auth U:P]
    eca-rsi serve     scan-add|remove|list|dump|reload ...   (edit the registry file)
    eca-rsi umapdata  <h5ad> <out.json>

`run` chains everything for one dataset: organize the eca-pp products into
<root>, then for every analysis unit persample (osp) → loop (msp + zmip
rounds until the cell count converges) → release; with --serve it finally
adds <root> to the serve registry and serves it (and everything else in the
registry) in the foreground at http://127.0.0.1:PORT/<root-name>/ (add
--ngrok to publish; Ctrl-C to stop).
Every step resumes, so re-running the same command after an interruption
continues where it stopped. `python -m ecarsi ...` is the same thing.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import layout as L

STEPS = ("organize", "persample", "crosssample", "zoomin", "loop", "ledger", "index", "serve", "umapdata", "prune")


def _module(name: str):
    import importlib

    return importlib.import_module(f"ecarsi.{name}")


def run(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="eca-rsi run", description="organize → persample → loop for every unit (→ serve)")
    ap.add_argument("input", help="eca-pp output directory (standardize/standardized.h5ad + result.json per sample)")
    ap.add_argument("root", help="run root; everything lands under <root>/units/<unit>/ (ecarsi.layout)")
    ap.add_argument("--rounds", type=int, default=None, help="fixed number of loop rounds (default: converge on cell count)")
    ap.add_argument("--cap", type=int, default=None, help="loop safety cap (default 10)")
    ap.add_argument("--force-reopen", action="store_true", help="continue past an existing release")
    ap.add_argument("--no-prune", action="store_true", help="keep intermediate round h5ads after release (default: prune them)")
    ap.add_argument("--serve", nargs="?", const=8899, type=int, default=None, metavar="PORT",
                    help="after the run, add <root> to the serve registry and serve (foreground) on this port (default 8899)")
    ap.add_argument("--ngrok", action="store_true", help="with --serve: also open an ngrok tunnel")
    ap.add_argument("--domain", default=None, help="with --serve: reserved ngrok domain")
    ap.add_argument("--auth", default=None, help="with --serve: web-level password USER:PASS")
    a = ap.parse_args(argv)
    root = Path(a.root).resolve()

    if not L.is_root(root):
        rc = _module("organize").main([a.input, str(root)])
        if rc:
            return rc
    else:
        print(f"[eca-rsi] {root} already organized — resuming")
    units = L.units(root)
    if not units:
        print(f"[eca-rsi] no analysis units under {L.units_root(root)}")
        return 3
    print(f"[eca-rsi] {len(units)} unit(s): " + ", ".join(u.name for u in units))
    loop_args = []
    if a.rounds is not None:
        loop_args += ["--rounds", str(a.rounds)]
    if a.cap is not None:
        loop_args += ["--cap", str(a.cap)]
    if a.force_reopen:
        loop_args.append("--force-reopen")
    if a.no_prune:
        loop_args.append("--no-prune")
    failed = []
    for u in units:
        print(f"\n[eca-rsi] ===== unit {u.name}: persample =====", flush=True)
        rc = _module("persample").main([str(u)])
        if rc:
            failed.append((u.name, "persample", rc))
            continue
        print(f"\n[eca-rsi] ===== unit {u.name}: loop =====", flush=True)
        rc = _module("loop").main([str(u), *loop_args])
        if rc:
            failed.append((u.name, "loop", rc))
    _module("index").write_all(root)
    for name, step, rc in failed:
        print(f"[eca-rsi] FAILED {name} at {step} (rc={rc}) — re-run the same command to resume")
    if a.serve is not None:
        # record this root in the registry file (so any later `eca-rsi serve`
        # shows it too), then serve everything in the registry in the
        # foreground until Ctrl-C — the server itself keeps no state
        serve = _module("serve")
        try:
            serve.Registry(serve.default_registry()).bind(root.name, root)
        except ValueError as e:
            print(f"[eca-rsi] not added to the registry ({e}); serving it for this process only")
        serve_args = [str(root), "--port", str(a.serve)]
        if a.ngrok:
            serve_args.append("--ngrok")
        if a.domain:
            serve_args += ["--domain", a.domain]
        if a.auth:
            serve_args += ["--auth", a.auth]
        print(f"[eca-rsi] serving {root.name} at http://127.0.0.1:{a.serve}/{root.name}/", flush=True)
        rc = serve.main(serve_args)
        return rc or (1 if failed else 0)
    print(f"\n[eca-rsi] done — landing page: {root / L.INDEX}  (eca-rsi serve scan-add {root}; eca-rsi serve)")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0 if argv else 2
    cmd, rest = argv[0], argv[1:]
    if cmd == "run":
        return run(rest)
    if cmd in STEPS:
        return _module(cmd).main(rest)
    print(f"unknown command {cmd!r}; expected run or one of {', '.join(STEPS)}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

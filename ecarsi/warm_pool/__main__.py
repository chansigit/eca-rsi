"""Development Warm Pool CLI: explicit state root, native HQ, bounded runtime."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

from .backend import check_hq, check_runtime, join, serve
from .state import cancel, digest, file_digest, lock, pool_root, read, save, status, submit, sync_directory


def configure_runtime(root, runtime):
    """Validate from inside the target runtime; existing requests keep their pin."""
    root = pool_root(root)
    check_runtime(runtime, imports=True)
    with lock(root / "init.lock"):
        config = read(root / "config.json")
        save(root / "config.json", {**config, "runtime": runtime})
    return {"runtime_digest": digest(runtime)}


def initialize(root, hq, runtime):
    root = Path(root).resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o077:
        raise ValueError("pool directory must be owned by this user with mode 0700")
    runtime = str(Path(runtime).resolve(strict=True))
    config = dict(protocol=1, hq=str(Path(hq).resolve(strict=True)), executor=sys.executable,
                  runtime=dict(command=[runtime], files={runtime: file_digest(runtime)},
                    version=subprocess.run([runtime, "--version"], capture_output=True, text=True,
                                           check=True, timeout=10).stdout.strip()))
    check_hq(config["hq"])
    with lock(root / "init.lock"):
        previous = read(root / "config.json")
        if previous and previous != config:
            raise ValueError("pool already initialized with a different runtime")
        (root / "requests").mkdir(mode=0o700, exist_ok=True)
        save(root / "config.json", config)
        sync_directory(root.parent)
    return dict(root=str(root), runtime=config["runtime"], version="0.26.2")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, required=True)
    commands = p.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init")
    init.add_argument("--hq", required=True)
    init.add_argument("--runtime", default=sys.executable)
    runtime = commands.add_parser("configure-runtime", help="preflight and select the runtime for new requests")
    runtime.add_argument("spec", type=Path)
    server = commands.add_parser("scheduler")
    server.add_argument("--host")
    worker = commands.add_parser("worker")
    worker.add_argument("--cpus", required=True, help="explicit CPU IDs, e.g. 0,1")
    worker.add_argument("--memory-mb", type=int, required=True)
    worker.add_argument("--work-dir", type=Path, required=True, help="worker-local temporary directory")
    worker.add_argument("--allocation-profile", type=Path, help="fresh host probe; normally set by slurm-worker")
    worker.add_argument("--time-limit-seconds", type=int, help="optional shorter worker lifetime")
    worker.add_argument("--gpu", help="one explicitly reserved NVIDIA GPU UUID")
    slurm = commands.add_parser("slurm-worker", help="probe an existing Slurm grant on the host, then enter the runtime")
    slurm.add_argument("--cpus", required=True)
    slurm.add_argument("--memory-mb", type=int, required=True)
    slurm.add_argument("--work-dir", type=Path, required=True)
    slurm.add_argument("--job-id", help="optional expected allocation ID")
    slurm.add_argument("--time-limit-seconds", type=int)
    slurm.add_argument("--gpu", action="store_true", help="use the one GPU granted to this Slurm step")
    slurm.add_argument("runtime_command", nargs=argparse.REMAINDER, help="runtime Python command after --")
    submission = commands.add_parser("submit")
    submission.add_argument("spec", type=Path)
    inspection = commands.add_parser("status")
    inspection.add_argument("request_id", nargs="?")
    cancellation = commands.add_parser("cancel")
    cancellation.add_argument("request_id")
    a = p.parse_args(argv)
    if a.command == "init":
        result = initialize(a.root, a.hq, a.runtime)
    elif a.command == "configure-runtime":
        result = configure_runtime(a.root, read(a.spec))
    elif a.command == "scheduler":
        return serve(a.root, a.host)
    elif a.command == "worker":
        return join(a.root, [int(v) for v in a.cpus.split(",")], a.memory_mb, a.work_dir,
                    a.allocation_profile, a.time_limit_seconds, a.gpu)
    elif a.command == "slurm-worker":
        from .allocation import launch
        return launch(a.root, [int(v) for v in a.cpus.split(",")], a.memory_mb, a.work_dir,
                      a.runtime_command, a.job_id, a.time_limit_seconds, a.gpu)
    elif a.command == "submit":
        result = submit(a.root, read(a.spec))
    elif a.command == "cancel":
        result = cancel(a.root, a.request_id)
    else:
        result = status(a.root, a.request_id)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

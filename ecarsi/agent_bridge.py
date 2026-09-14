"""Durable Organize planning inbox on a trusted shared filesystem.

Session-level admission only: this is not yet the per-model-turn Bridge.
Requires coherent flock, atomic rename and fsync, like warm_pool.state.
No model secrets are stored. Unknown executions are never retried implicitly.
"""
import argparse
import asyncio
from collections import Counter
import os
from pathlib import Path
import stat
import subprocess
import sys
import time

from .warm_pool.state import digest, file_digest, identifier, lock, read, save, sync_directory


def root_path(root):
    root = Path(root).resolve(strict=True)
    info = root.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError("Bridge root must be user-owned with mode 0700")
    if read(root / "config.json") is None:
        raise ValueError("Bridge root is not initialized")
    return root


def init(root, catalog, concurrency=2):
    if type(concurrency) is not int or concurrency < 1:
        raise ValueError("concurrency must be a positive integer")
    catalog = str(Path(catalog).resolve(strict=True))
    root = Path(root).resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.stat().st_uid != os.getuid() or stat.S_IMODE(root.stat().st_mode) & 0o077:
        raise ValueError("Bridge root must be user-owned with mode 0700")
    with lock(root / "init.lock"):
        config = {"catalog": catalog, "concurrency": concurrency}
        previous = read(root / "config.json")
        if previous is not None and previous != config:
            raise ValueError("Bridge is already initialized with another configuration")
        (root / "requests").mkdir(mode=0o700, exist_ok=True)
        save(root / "config.json", config)
        sync_directory(root.parent)
    return root


def submit(root, spec):
    root = root_path(root)
    if not isinstance(spec, dict) or set(spec) != {"request_id", "operation_id", "profiles", "cwd"}:
        raise ValueError("Expected request_id, operation_id, profiles and cwd")
    identifier(spec["request_id"])
    identifier(spec["operation_id"])
    profiles = spec["profiles"]
    if not isinstance(profiles, list) or not profiles or not all(
        isinstance(p, dict) and isinstance(p.get("name"), str) and
        isinstance(p.get("h5ad"), str) for p in profiles
    ):
        raise ValueError("Expected nonempty Organize profiles with name and h5ad")
    cwd = Path(spec["cwd"])
    if not cwd.is_absolute() or not cwd.is_dir():
        raise ValueError("cwd must be an existing absolute directory")
    spec = dict(spec, cwd=str(cwd.resolve()))
    fingerprint = digest(spec)
    folder = root / "requests" / spec["request_id"]
    folder.mkdir(mode=0o700, exist_ok=True)
    sync_directory(folder.parent)
    with lock(folder / "request.lock"):
        previous = read(folder / "request.json")
        if previous is not None:
            if previous["digest"] != fingerprint:
                raise ValueError("Request ID already has different content")
        else:
            save(folder / "request.json", {
                "spec": spec, "digest": fingerprint, "submitted_at": time.time(),
                "brief": (Path(__file__).parent / "prompts/plan.md").read_text() + "\n\n" +
                         (Path(__file__).parent / "prompts/organize_v2.md").read_text(),
                "adapter_sha256": file_digest(Path(__file__).with_name("plan.py")),
            })
    return status(root, spec["request_id"])


def status(root, request_id):
    folder = root_path(root) / "requests" / identifier(request_id)
    request = read(folder / "request.json")
    if request is None:
        raise KeyError(request_id)
    result = read(folder / "result.json")
    state = {**read(folder / "state.json", {"state": "queued"}), **(result or {})}
    return {"request_id": request_id, "submitted_at": request["submitted_at"], **state}


def run_organize(request, *, folder=None):
    from .plan import _propose, _validate, validate_sample_mapping
    spec = request["spec"]
    result = asyncio.run(_propose(spec["profiles"], brief=request["brief"],
                                 cwd=spec["cwd"], full_result=True,
                                 on_submitted=(lambda plan: save(folder / "proposal.json", plan))
                                 if folder is not None else None, require_sample_mapping=True))
    _validate(result.submitted, spec["profiles"])
    validate_sample_mapping(result.submitted, spec["profiles"])
    return {"plan": result.submitted, "transcript": result.transcript_text,
            "model": result.effective_config.as_manifest() if result.effective_config else None,
            "usage": {"tokens_in": result.tokens_in, "tokens_out": result.tokens_out,
                      "cost_usd": result.cost_usd}}


def recover_result(folder, reason):
    proposal = read(folder / "proposal.json")
    if proposal is not None:
        save(folder / "result.json", {
            "state": "reply_saved", "finished_at": time.time(), "recovered_after": reason,
            "response": {"plan": proposal, "transcript": None, "model": None,
                         "usage": {"tokens_in": None, "tokens_out": None, "cost_usd": None}}})
    else:
        save(folder / "state.json", {"state": "unknown_external_result",
                                      "reason": reason, "updated_at": time.time()})


def execute(root, request_id):
    folder = root_path(root) / "requests" / identifier(request_id)
    with lock(folder / "execution.lock", blocking=False):
        # A replacement service may have reconciled an unstarted launch as unknown.
        # It must never become a new provider call after that decision.
        state = read(folder / "state.json", {})
        if read(folder / "result.json") is not None or state.get("state") != "running":
            return
        request = read(folder / "request.json")
        try:
            if file_digest(Path(__file__).with_name("plan.py")) != request["adapter_sha256"]:
                save(folder / "result.json", {"state": "failed", "reason": "adapter_changed",
                                              "finished_at": time.time()})
                return
            result = run_organize(request, folder=folder)
            save(folder / "result.json", {"state": "reply_saved", "finished_at": time.time(),
                                          "response": result})
        except Exception as exc:
            # A timeout/error is not proof that the provider did no work.
            # Keep details in the private execution log, not the status response.
            import traceback
            traceback.print_exc()
            recover_result(folder, type(exc).__name__)


def reconcile(folder):
    if read(folder / "result.json") is not None:
        return
    try:
        with lock(folder / "execution.lock", blocking=False):
            if read(folder / "result.json") is None:
                recover_result(folder, "executor_lost")
    except BlockingIOError:
        pass  # The accepted executor survived the service; let it publish its reply.


def launch(root, folder, catalog):
    from .model_web import normalized_models, PROVIDERS
    models = normalized_models(catalog)
    if not models:
        raise ValueError("No models configured")
    env = dict(os.environ)
    env["AGENT_MODEL_POOL"] = ",".join(m["harness"] + ":" + m["model"] for m in models)
    for backend, (_, variable) in PROVIDERS.items():
        found = next((m for m in models if m["harness"] == backend and m["url"]), None)
        if found:
            env[variable] = found["url"]
    save(folder / "state.json", {"state": "running", "started_at": time.time(),
                                 "models": models, "catalog_digest": digest(catalog)})
    with (folder / "execution.log").open("ab") as log:
        return subprocess.Popen([sys.executable, "-m", "ecarsi.agent_bridge", "_execute",
                                 str(root), folder.name], env=env, stdin=subprocess.DEVNULL,
                                stdout=log, stderr=log, start_new_session=True)


def serve(root, *, once=False):
    root = root_path(root)
    children = {}
    with lock(root / "service.lock", blocking=False):
        while True:
            config = read(root / "config.json")
            limit = config["concurrency"]
            if type(limit) is not int or limit < 1:
                raise ValueError("concurrency must be a positive integer")
            # ponytail: directory scan per second; index only if queue size makes it costly.
            folders = sorted((root / "requests").iterdir())
            queued, active = [], 0
            for folder in folders:
                if not (folder / "request.json").is_file():
                    continue
                child = children.get(folder.name)
                if child is not None and child.poll() is not None:
                    del children[folder.name]
                record = status(root, folder.name)
                if record["state"] == "running":
                    if folder.name not in children:
                        reconcile(folder)
                    if status(root, folder.name)["state"] in {"running", "unknown_external_result"}:
                        active += 1
                elif record["state"] == "queued":
                    queued.append((record["submitted_at"], folder))
                elif record["state"] == "unknown_external_result":
                    # Provider work may still exist even when its local caller vanished.
                    active += 1
            error = None
            for _, folder in sorted(queued):
                if active >= limit:
                    break
                try:
                    # New dispatches read the existing catalog, not a second model list.
                    catalog = read(Path(config["catalog"]))
                    children[folder.name] = launch(root, folder, catalog)
                    active += 1
                except Exception as exc:
                    error = type(exc).__name__
                    import traceback
                    with (folder / "execution.log").open("a") as log:
                        traceback.print_exc(file=log)
                    if status(root, folder.name)["state"] == "running":
                        reconcile(folder)
                    break
            counts = Counter(status(root, f.name)["state"] for f in folders
                             if (f / "request.json").is_file())
            save(root / "summary.json", {"updated_at": time.time(), "counts": dict(counts),
                                         "concurrency": limit, "dispatch_error": error})
            if once:
                return children  # Test/embedding caller owns reaping any launched children.
            time.sleep(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("init")
    p.add_argument("root")
    p.add_argument("--catalog", required=True)
    p.add_argument("--concurrency", type=int, default=2)
    for name in ("serve", "submit", "status", "_execute"):
        p = commands.add_parser(name)
        p.add_argument("root")
        if name == "submit":
            p.add_argument("request_file")
        if name in {"status", "_execute"}:
            p.add_argument("request_id")
    args = parser.parse_args()
    if args.command == "init":
        init(args.root, args.catalog, args.concurrency)
    elif args.command == "serve":
        serve(args.root)
    elif args.command == "_execute":
        execute(args.root, args.request_id)
    else:
        import json
        result = submit(args.root, read(args.request_file)) if args.command == "submit" else status(args.root, args.request_id)
        print(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    main()

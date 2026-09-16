"""Durable model-turn inbox with legacy Organize session support.

Requires coherent flock, atomic rename and fsync, like warm_pool.state.
No model secrets are stored. Unknown executions are never retried implicitly.
"""
import argparse
import asyncio
from collections import Counter
from contextlib import contextmanager
import copy
import os
from pathlib import Path
import stat
import subprocess
import sys
import time

from .warm_pool.state import digest, file_digest, identifier, lock, read, save, sync_directory, validate_trace


class LocalRestoreError(RuntimeError):
    """SDK checkpoint restoration failed before Runner could contact a provider."""


@contextmanager
def sdk_restore_compat(folder):
    """SDK 0.22 can serialize Chat text without annotations, then reject its own state.

    Keep this boundary shim while pinned 0.22 sessions exist. It preserves saved
    records and avoids changing their pinned agent adapter during a live run.
    Each Bridge executor owns one model turn in its own process.
    """
    from agents import RunState
    import agents
    original = RunState.from_json

    async def restore(agent, state, **kwargs):
        state = copy.deepcopy(state)
        repaired = 0
        def visit(value):
            nonlocal repaired
            if isinstance(value, dict):
                if value.get("type") == "message" and isinstance(value.get("content"), list):
                    for item in value["content"]:
                        if isinstance(item, dict) and item.get("type") == "output_text" and "annotations" not in item:
                            item["annotations"] = []
                            repaired += 1
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)
        if agents.__version__ == "0.22.0":
            visit(state)
        try:
            result = await original(agent, state, **kwargs)
        except Exception as exc:
            raise LocalRestoreError("Local SDK checkpoint restore failed before model invocation") from exc
        if repaired:
            save(folder / "sdk-restore.json", {"sdk_version": agents.__version__, "empty_annotations_restored": repaired})
        return result

    RunState.from_json = staticmethod(restore)
    try:
        yield
    finally:
        RunState.from_json = staticmethod(original)


def root_path(root):
    root = Path(root).resolve(strict=True)
    info = root.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError("Bridge root must be user-owned with mode 0700")
    if read(root / "config.json") is None:
        raise ValueError("Bridge root is not initialized")
    return root


def init(root, catalog, concurrency=2, *, pool_root=None):
    if type(concurrency) is not int or concurrency < 1:
        raise ValueError("concurrency must be a positive integer")
    catalog = str(Path(catalog).resolve(strict=True))
    root = Path(root).resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.stat().st_uid != os.getuid() or stat.S_IMODE(root.stat().st_mode) & 0o077:
        raise ValueError("Bridge root must be user-owned with mode 0700")
    with lock(root / "init.lock"):
        config = {"catalog": catalog, "concurrency": concurrency}
        if pool_root is not None:
            from .warm_pool.state import pool_root as verified_pool
            config["pool_root"] = str(verified_pool(pool_root))
        previous = read(root / "config.json")
        if previous is not None and previous != config:
            raise ValueError("Bridge is already initialized with another configuration")
        (root / "requests").mkdir(mode=0o700, exist_ok=True)
        save(root / "config.json", config)
        sync_directory(root.parent)
    return root


def submit(root, spec):
    root = root_path(root)
    is_turn = isinstance(spec, dict) and spec.get("operation_id") == "agent.turn"
    required = ({"request_id", "operation_id", "session", "context", "trace"} if is_turn else
                {"request_id", "operation_id", "profiles", "cwd"})
    if not isinstance(spec, dict) or not required <= spec.keys() or spec.keys() - required - {"trace"}:
        raise ValueError("Expected request_id, operation_id, profiles and cwd")
    identifier(spec["request_id"])
    identifier(spec["operation_id"])
    if "trace" in spec:
        validate_trace(spec["trace"])
    if is_turn:
        from .agent_session import validate_turn
        validate_turn(spec)
    else:
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
                "brief": "" if is_turn else (Path(__file__).parent / "prompts/plan.md").read_text() + "\n\n" +
                         (Path(__file__).parent / "prompts/organize_v2.md").read_text(),
                "adapter_sha256": file_digest(Path(__file__).with_name("agent_session.py" if is_turn else "plan.py")),
            })
    return status(root, spec["request_id"])


def status(root, request_id):
    folder = root_path(root) / "requests" / identifier(request_id)
    request = read(folder / "request.json")
    if request is None:
        raise KeyError(request_id)
    result = read(folder / "result.json")
    state = read(folder / "state.json", {"state": "queued"})
    if result and result.get('state') == 'failed' and state.get('retry_of') == digest(result):
        result = None  # Audited retry intent survives a crash before result archival.
    state = {**state, **(result or {})}
    return {"request_id": request_id, "submitted_at": request["submitted_at"], **state}


def retry_turn(root, request_id, *, reason):
    """Explicit bounded recovery of a tool-free model turn, retaining failed attempts."""
    from .agent_session import immutable, verified, reference
    from .agent_dispatch import policy
    from .warm_pool.state import status as pool_status
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError('A model recovery reason is required')
    root = root_path(root)
    folder = root / 'requests' / identifier(request_id)
    with lock(folder / 'request.lock'):
        request, result, state = read(folder / 'request.json'), read(folder / 'result.json'), read(folder / 'state.json', {})
        spec = request['spec']
        if (spec['operation_id'] != 'agent.turn' or verified(spec['session']).get('protocol', 1) < 2
                or not result or result.get('state') != 'failed'
                or result.get('reason') not in {'timeout', 'provider_error', 'worker_failed', 'worker_lost'}):
            raise ValueError('Only failed, tool-free transient model requests can be retried')
        if state.get('retry_of') == digest(result):
            return status(root, request_id)
        for attempt in state.get('attempts', []):
            current = pool_status(state['pool_root'], attempt['pool_request_id'])
            if current['state'] not in {'succeeded', 'failed', 'cancelled'}:
                raise ValueError('Prior worker attempt is not terminal: ' + attempt['pool_request_id'])
        config = read(root / 'config.json')
        audit = immutable(folder / ('recovery-' + digest(result) + '.json'),
            dict(reason=reason, request=reference(folder / 'request.json'), result=result, previous_state=state))
        save(folder / 'state.json', dict(state, state='queued', retry_of=digest(result), recovery=audit,
            attempt_limit=len(state.get('attempts', [])) + policy(config)['max_attempts'], updated_at=time.time()))
    return status(root, request_id)


def cancel(root, request_id):
    """Stop queued/Pool-backed calls; preserve every completed reply and attempt."""
    root = root_path(root)
    folder = root / "requests" / identifier(request_id)
    with lock(folder / "request.lock"):
        record = status(root, request_id)
        if record["state"] in {"reply_saved", "failed"}:
            return record
        if record["state"] != "queued" and record.get("execution") != "pool":
            raise ValueError("Legacy external execution requires explicit reconciliation")
        from .warm_pool.state import cancel as cancel_pool
        for attempt in record.get("attempts", []):
            try:
                cancel_pool(record["pool_root"], attempt["pool_request_id"])
            except KeyError:
                pass  # Dispatch intent was saved, but no Pool task was submitted.
        save(folder / "result.json", dict(state="failed", reason="cancelled", finished_at=time.time()))
    return status(root, request_id)


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
    turn = read(folder / "turn-response.json")
    if turn is not None:
        save(folder / "result.json", {"state": "reply_saved", "finished_at": time.time(),
                                      "recovered_after": reason, "response": turn})
        return
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
            is_turn = request["spec"]["operation_id"] == "agent.turn"
            adapter = "agent_session.py" if is_turn else "plan.py"
            if file_digest(Path(__file__).with_name(adapter)) != request["adapter_sha256"]:
                save(folder / "result.json", {"state": "failed", "reason": "adapter_changed",
                                              "finished_at": time.time()})
                return
            if is_turn:
                from .agent_session import run_turn
                with sdk_restore_compat(folder):
                    result = asyncio.run(run_turn(request, folder))
            else:
                result = run_organize(request, folder=folder)
            save(folder / "result.json", {"state": "reply_saved", "finished_at": time.time(),
                                          "response": result})
        except Exception as exc:
            # A timeout/error is not proof that the provider did no work.
            # Keep details in the private execution log, not the status response.
            import traceback
            traceback.print_exc()
            if isinstance(exc, LocalRestoreError):
                save(folder / "result.json", {"state": "failed", "reason": "local_state_restore",
                     "provider_called": False, "finished_at": time.time()})
            else:
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


def confirm_stopped(root, request_id, *, reason):
    """Operator/provider confirmation, never a timeout-based assumption or retry."""
    root = root_path(root)
    folder = root / 'requests' / identifier(request_id)
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError('Record how the remote execution was confirmed stopped')
    with lock(folder / 'execution.lock', blocking=False):
        previous = status(root, request_id)
        if previous['state'] != 'unknown_external_result':
            raise ValueError('Only an unresolved external outcome can be reconciled')
        # A surviving/restored response always wins over declaring its result unavailable.
        if read(folder / 'turn-response.json') is not None or read(folder / 'proposal.json') is not None:
            recover_result(folder, 'confirmed_stopped_with_saved_reply')
        else:
            from .agent_session import immutable
            audit = immutable(folder / 'resolution.json', dict(request_digest=read(folder / 'request.json')['digest'],
                previous=previous, remote_stopped=True, reason=reason))
            save(folder / 'result.json', dict(state='failed', reason='remote_stopped_without_reply',
                resolution=audit, finished_at=time.time()))
    return status(root, request_id)


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
    if read(root / "config.json").get("pool_root"):
        from .agent_dispatch import serve as serve_pool
        return serve_pool(root, once=once)
    children, finished = {}, {}
    with lock(root / "service.lock", blocking=False):
        while True:
            scanning = time.monotonic()
            config = read(root / "config.json")
            limit = config["concurrency"]
            if type(limit) is not int or limit < 1:
                raise ValueError("concurrency must be a positive integer")
            # Terminal requests are immutable; rebuild this cache after restart.
            # ponytail: still list names per tick; index them if directory listing dominates.
            folders = sorted((root / "requests").iterdir())
            for name, child in list(children.items()):
                if child.poll() is not None:
                    del children[name]
            queued, active = [], 0
            for folder in folders:
                if folder.name in finished or not (folder / "request.json").is_file():
                    continue
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
                    if (folder / 'turn-response.json').is_file() or (folder / 'proposal.json').is_file():
                        reconcile(folder)
                    if status(root, folder.name)['state'] == 'unknown_external_result':
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
            counts = Counter(finished.values())
            for folder in folders:
                if folder.name in finished or not (folder / "request.json").is_file():
                    continue
                state = status(root, folder.name)['state']
                counts[state] += 1
                if state in {'reply_saved', 'failed'}:
                    finished[folder.name] = state
            save(root / "summary.json", {"updated_at": time.time(), "counts": dict(counts),
                                         "dispatch_scan_seconds": time.monotonic() - scanning,
                                         "concurrency": limit, "dispatch_error": error,
                                         "running": counts['running'],
                                         "unresolved": counts['unknown_external_result'],
                                         "available": max(0, limit-counts['running']-counts['unknown_external_result'])})
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
    p.add_argument("--pool-root", required=True, help="Pool for all model and harness execution")
    p = commands.add_parser('confirm-stopped', help='reconcile an uncertain call after confirming remote execution has stopped')
    p.add_argument('root')
    p.add_argument('request_id')
    p.add_argument('--reason', required=True)
    p = commands.add_parser('retry-turn', help='audit and retry a failed tool-free model request')
    p.add_argument('root')
    p.add_argument('request_id')
    p.add_argument('--reason', required=True)
    for name in ("serve", "submit", "status", "cancel", "_execute"):
        p = commands.add_parser(name)
        p.add_argument("root")
        if name == "submit":
            p.add_argument("request_file")
        if name in {"status", "cancel", "_execute"}:
            p.add_argument("request_id")
    args = parser.parse_args()
    if args.command == "init":
        init(args.root, args.catalog, args.concurrency, pool_root=args.pool_root)
    elif args.command == "serve":
        serve(args.root)
    elif args.command == "_execute":
        execute(args.root, args.request_id)
    elif args.command == 'confirm-stopped':
        import json
        print(json.dumps(confirm_stopped(args.root, args.request_id, reason=args.reason), ensure_ascii=True))
    elif args.command == 'retry-turn':
        import json
        print(json.dumps(retry_turn(args.root, args.request_id, reason=args.reason), ensure_ascii=True))
    else:
        import json
        result = (submit(args.root, read(args.request_file)) if args.command == "submit" else
                  cancel(args.root, args.request_id) if args.command == "cancel" else status(args.root, args.request_id))
        print(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    main()

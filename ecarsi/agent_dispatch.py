"""Bridge routing and Pool execution. Only the worker imports provider clients."""
import argparse
import asyncio
from collections import Counter
from contextlib import nullcontext
import json
import os
from pathlib import Path
import socket
import subprocess
import time

from .warm_pool.state import digest, file_digest, lock, read, save, submit, status


DEFAULT_POLICY = dict(response_timeout_seconds=900, cooldown_seconds=300,
                      failure_threshold=2, model_concurrency=2, max_attempts=3,
                      worker_cpus=1, worker_memory_mb=1024)


def policy(config):
    overrides = config.get("routing", {})
    if not isinstance(overrides, dict) or overrides.keys() - DEFAULT_POLICY.keys():
        raise ValueError("Unknown Bridge routing setting")
    result = DEFAULT_POLICY | overrides
    if any(type(value) is not int or value < 1 for value in result.values()):
        raise ValueError("Bridge routing settings must be positive integers")
    return result


def model_key(model):
    return digest(model)[:24]


def routes(config, request):
    from .model_web import normalized_models
    from .agent_session import verified
    models = normalized_models(read(config["catalog"]))
    if request["spec"]["operation_id"] == "agent.turn":
        session = verified(request["spec"]["session"])
        if session.get("protocol", 1) < 2:
            return [session["model"]]
        models = [m for m in models if m["harness"] in {"openai", "openai@vllm", "openai@openrouter"}]
    if not models:
        raise ValueError("No compatible models configured")
    return models


def model_health(events, models, active, settings, now=None):
    now = time.time() if now is None else now
    rows = []
    for model in models:
        key = model_key(model)
        history = sorted((e for e in events.values() if model_key(e["model"]) == key),
                         key=lambda e: e["finished_at"])
        failures = 0
        cooldown = 0
        for event in history:
            if event["outcome"] == "success":
                failures = 0
            elif event.get("model_failure", True):
                failures += 1
                if failures >= settings["failure_threshold"]:
                    cooldown = max(cooldown, event["finished_at"] + settings["cooldown_seconds"])
        successes = [e for e in history if e["outcome"] == "success"]
        row = dict(model=model, key=key, in_flight=active[key], consecutive_failures=failures,
                   successes=len(successes), failures=sum(e["outcome"] != "success" for e in history),
                   timeouts=sum(e["outcome"] == "timeout" for e in history),
                   last_response_at=successes[-1]["finished_at"] if successes else None,
                   last_latency_seconds=history[-1].get("elapsed_seconds") if history else None,
                   cooldown_until=cooldown if cooldown > now else None,
                   last_outcome=history[-1]["outcome"] if history else None)
        row["state"] = "cooling_down" if cooldown > now else "busy" if active[key] >= settings["model_concurrency"] else "ready"
        rows.append(row)
    return rows


def record_event(root, folder, attempt, outcome, *, elapsed=None, model_failure=True):
    path = root / "model-events" / (digest([folder.name, attempt["pool_request_id"]]) + ".json")
    existing = read(path)
    if existing:
        return existing
    event = dict(request_id=folder.name, pool_request_id=attempt["pool_request_id"],
                 model=attempt["model"], outcome=outcome, model_failure=model_failure,
                 elapsed_seconds=elapsed, finished_at=time.time())
    save(path, event)
    return event


def dispatch(root, folder, config, model):
    with lock(folder / "request.lock"):
        if read(folder / "result.json") is not None:
            return None
        return _dispatch(root, folder, config, model)


def _dispatch(root, folder, config, model):
    """Persist intent before enqueueing so dispatcher replacement cannot duplicate a turn."""
    from .agent_session import immutable, archive_adapter
    request = read(folder / "request.json")
    settings = policy(config)
    state = read(folder / "state.json", {})
    attempts = state.get("attempts", [])
    number = len(attempts)
    pool_id = "agent-" + digest([str(root), folder.name, number])[:32]
    plan_path = folder / f"dispatch-{number}.json"
    plan = read(plan_path) or dict(request=request, model=model, timeout_seconds=settings["response_timeout_seconds"],
                cpus=settings["worker_cpus"], memory_mb=settings["worker_memory_mb"],
                adapter_sha256=archive_adapter(root)["sha256"])
    plan_ref = immutable(plan_path, plan)
    attempt = dict(pool_request_id=pool_id, model=plan["model"], plan=plan_ref, submitted_at=time.time())
    attempts.append(attempt)
    save(folder / "state.json", dict(state="running", execution="pool", pool_root=config["pool_root"],
         started_at=state.get("started_at", time.time()), attempts=attempts))
    enqueue(folder, config, attempt)
    return attempt


def enqueue(folder, config, attempt):
    plan = read(attempt["plan"]["path"])
    request = read(folder / "request.json")
    trace = request["spec"].get("trace", {})
    submit(config["pool_root"], dict(request_id=attempt["pool_request_id"], operation_id="agent.call",
           args=["-m", "ecarsi.agent_dispatch", "execute", attempt["plan"]["path"]],
           cpus=plan["cpus"], memory_mb=plan["memory_mb"],
           timeout_seconds=plan["timeout_seconds"] + 60,
           inputs=[attempt["plan"]], outputs=["result.json"], **({"trace": trace} if trace else {})))


def reconcile_pool(root, folder, config, events):
    with lock(folder / "request.lock"):
        if read(folder / "result.json") is None:
            _reconcile_pool(root, folder, config, events)


def _reconcile_pool(root, folder, config, events):
    """Accept one attempt's fenced output; late alternatives never publish Bridge replies."""
    from .agent_session import verified
    state = read(folder / "state.json")
    attempt = state["attempts"][-1]
    enqueue(folder, config, attempt)
    current = status(state["pool_root"], attempt["pool_request_id"])
    output_dir = Path(state["pool_root"]) / "requests" / attempt["pool_request_id"] / current["attempt_id"] / "outputs"
    started = read(output_dir / "started.json", {})
    elapsed = time.time() - started["started_at"] if started else None
    outcome = None
    response = None
    if current["state"] == "succeeded":
        ref = next(o for o in current["receipt"]["outputs"] if Path(o["path"]).name == "result.json")
        response = verified({k: ref[k] for k in ("path", "sha256")})
        outcome = response["outcome"]
        elapsed = response.get("elapsed_seconds", elapsed)
    elif current["receipt"]:
        outcome = "worker_failed"
    elif elapsed is not None and elapsed > settings_timeout(attempt) + 30:
        # The task wrapper also has a hard deadline. Fence output first; only
        # read-only model turns can be safely reissued while the provider is uncertain.
        outcome = "timeout"
    elif (current["state"] == "unknown_external_result" and elapsed is None and current["accepted"]
          and time.time() - current["accepted"]["started_at"] > settings_timeout(attempt) + 60):
        outcome = "worker_lost"
    # A stale heartbeat alone is not loss: a busy host may still finish and publish.
    if outcome is None:
        return
    if current["state"] not in {"succeeded", "failed", "cancelled"}:
        from .warm_pool.state import cancel
        cancel(state["pool_root"], attempt["pool_request_id"])
    model_failure = outcome in {"timeout", "provider_error"}
    event = record_event(root, folder, attempt, outcome, elapsed=elapsed, model_failure=model_failure)
    events[attempt["pool_request_id"]] = event
    if outcome == "success":
        save(folder / "result.json", dict(state="reply_saved", finished_at=time.time(),
             response=response["response"], worker=response["worker"], pool_request_id=attempt["pool_request_id"]))
    else:
        # Legacy harnesses may run arbitrary programs; an uncertain legacy call
        # is not equivalent to a tool-free model turn and must not be blindly repeated.
        spec = read(folder / "request.json")["spec"]
        safe = spec["operation_id"] == "agent.turn" and verified(spec["session"]).get("protocol", 1) >= 2
        retry = safe and len(state["attempts"]) < policy(config)["max_attempts"] and outcome != "local_error"
        save(folder / "state.json", dict(state, state="queued" if retry else "failed",
                                         last_outcome=outcome, updated_at=time.time()))
        if not retry:
            save(folder / "result.json", dict(state="failed", reason=outcome, finished_at=time.time()))


def settings_timeout(attempt):
    return read(attempt["plan"]["path"])["timeout_seconds"]


def serve(root, *, once=False):
    from .agent_bridge import root_path, status as bridge_status, reconcile
    from .model_web import normalized_models
    root = root_path(root)
    (root / "model-events").mkdir(mode=0o700, exist_ok=True)
    # Read immutable events once per service lifetime; no growing history scan per tick.
    events = {e["pool_request_id"]: e for p in (root / "model-events").glob("*.json") if (e := read(p))}
    finished = {}
    with lock(root / "service.lock", blocking=False):
        while True:
            scanning = time.monotonic()
            config = read(root / "config.json")
            settings = policy(config)
            if type(config["concurrency"]) is not int or config["concurrency"] < 1:
                raise ValueError("Bridge concurrency must be a positive integer")
            active, queued, legacy_active = Counter(), [], 0
            error = None
            for folder in sorted((root / "requests").iterdir()):
                if folder.name in finished or not (folder / "request.json").is_file():
                    continue
                state = bridge_status(root, folder.name)
                if state["state"] == "running" and state.get("execution") == "pool":
                    try:
                        reconcile_pool(root, folder, config, events)
                    except (ValueError, KeyError, StopIteration) as exc:
                        with lock(folder / "request.lock"):
                            if read(folder / "result.json") is None:
                                save(folder / "result.json", dict(state="failed", reason="invalid_attempt_receipt",
                                     error=type(exc).__name__, finished_at=time.time()))
                    except OSError as exc:
                        error = type(exc).__name__
                    state = bridge_status(root, folder.name)
                elif state["state"] in {"running", "unknown_external_result"}:
                    reconcile(folder)
                    state = bridge_status(root, folder.name)
                if state["state"] in {"reply_saved", "failed"}:
                    finished[folder.name] = state["state"]
                elif state["state"] == "queued":
                    queued.append((state["submitted_at"], folder))
                elif state.get("attempts"):
                    active[model_key(state["attempts"][-1]["model"])] += 1
                else:
                    legacy_active += 1
            for _, folder in sorted(queued):
                if sum(active.values()) + legacy_active >= config["concurrency"]:
                    break
                try:
                    models = routes(config, read(folder / "request.json"))
                    rows = model_health(events, models, active, settings)
                    tried = {model_key(a["model"]) for a in read(folder / "state.json", {}).get("attempts", [])}
                    ready = [row for row in rows if row["state"] == "ready"]
                    fresh = [row for row in ready if row["key"] not in tried]
                    if fresh or ready:
                        selected = (fresh or ready)[0]
                        attempt = dispatch(root, folder, config, selected["model"])
                        if attempt:
                            active[model_key(attempt["model"])] += 1
                except Exception as exc:
                    error = type(exc).__name__
                    save(folder / "dispatch-error.json", dict(error=error, observed_at=time.time()))
            counts = Counter(finished.values())
            for _, folder in queued:
                counts[bridge_status(root, folder.name)["state"]] += 1
            counts["running"] = sum(active.values())
            save(root / "summary.json", dict(updated_at=time.time(), counts=dict(counts), execution="pool",
                 concurrency=config["concurrency"], running=sum(active.values()), unresolved=legacy_active,
                 available=max(0, config["concurrency"]-sum(active.values())-legacy_active), dispatch_error=error,
                 dispatch_scan_seconds=time.monotonic()-scanning,
                 models=model_health(events, normalized_models(read(config["catalog"])), active, settings),
                 routing=settings))
            if once:
                return
            time.sleep(1)


def load_worker_key(model):
    """Read the user's shell configuration in the worker, never in task arguments/logs."""
    from .model_web import PROVIDERS
    if model["harness"] not in PROVIDERS:
        return
    key = PROVIDERS[model["harness"]][0]
    if not os.environ.get(key):
        # bashrc is trusted user configuration. Capture only the selected key in
        # a private pipe; silence shell startup output and never persist the value.
        script = 'source "$HOME/.bashrc" >/dev/null 2>&1; exec "$1" -c \'import os,sys;sys.stdout.write(os.environ.get(sys.argv[1],""))\' "$2"'
        result = subprocess.run(["bash", "--noprofile", "--norc", "-c", script, "bash", os.sys.executable, key],
                                capture_output=True, timeout=30, check=True)
        if result.stdout:
            os.environ[key] = result.stdout.decode()
    if not os.environ.get(key):
        raise ValueError("Worker credential is missing: " + key)


def execute(plan_path):
    from .agent_bridge import run_organize, sdk_restore_compat
    from .agent_session import run_turn, verified, validate_turn, pinned_adapter
    plan = read(plan_path)
    folder = Path.cwd()
    started = time.time()
    worker = dict(host=socket.gethostname().split(".")[0], pid=os.getpid())
    save(folder / "started.json", dict(started_at=started, worker=worker, model=plan["model"]))
    outcome, response, error = "local_error", None, None
    try:
        spec = plan["request"]["spec"]
        if spec["operation_id"] == "agent.turn":
            session = verified(spec["session"])
            pinned_adapter(session["spec"]["bridge_root"], plan["adapter_sha256"])
            validate_turn(spec)
        elif file_digest(Path(__file__).with_name("agent_session.py")) != plan["adapter_sha256"]:
            raise ValueError("Agent adapter changed after dispatch")
        load_worker_key(plan["model"])
        os.environ["OPENAI_AGENTS_REQUEST_TIMEOUT_S"] = str(plan["timeout_seconds"])
        try:
            if plan["request"]["spec"]["operation_id"] == "agent.turn":
                legacy = verified(plan["request"]["spec"]["session"]).get("protocol", 1) < 2
                with sdk_restore_compat(folder) if legacy else nullcontext():
                    response = asyncio.run(asyncio.wait_for(run_turn(plan["request"], folder, model=plan["model"]),
                                                           timeout=plan["timeout_seconds"]))
            else:
                from .model_web import PROVIDERS
                model = plan["model"]
                os.environ["AGENT_MODEL_POOL"] = model["harness"] + ":" + model["model"]
                if model["url"] and model["harness"] in PROVIDERS:
                    os.environ[PROVIDERS[model["harness"]][1]] = model["url"]
                response = run_organize(plan["request"], folder=folder)
            outcome = "success"
        except Exception as exc:
            # Model turns cannot execute tools. Failed turn outputs remain fenced
            # in this Pool attempt, even if a remote model completes later.
            error = type(exc).__name__
            saved = read(folder / "turn-response.json")
            if saved is not None:
                outcome, response = "success", saved
            else:
                outcome = "timeout" if isinstance(exc, TimeoutError) or "Timeout" in error else "provider_error"
    except Exception as exc:
        error = type(exc).__name__
    save(folder / "result.json", dict(outcome=outcome, response=response, error=error, worker=worker,
                                     elapsed_seconds=time.time()-started, model=plan["model"]))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["execute"])
    parser.add_argument("plan")
    args = parser.parse_args()
    execute(args.plan)

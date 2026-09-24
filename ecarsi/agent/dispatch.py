"""Bridge routing and Pool execution. Only the worker imports provider clients."""
import argparse
import asyncio
from collections import Counter, deque
from contextlib import nullcontext
import json
import os
from pathlib import Path
import socket
import subprocess
import time

from ..warm_pool.state import digest, file_digest, lock, read, save, submit, status


DEFAULT_POLICY = dict(response_timeout_seconds=900, cooldown_seconds=300,
                      failure_threshold=2, model_concurrency=2, max_attempts=3,
                      worker_cpus=1, worker_memory_mb=1024, session_wait_seconds=900)


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
    from ..model_web import normalized_models
    from ..warm_pool.state import verified
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
                cooldown = 0  # A newer accepted reply demonstrates recovery.
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


def record_event(root, folder, attempt, outcome, *, elapsed=None, model_failure=True, finished_at=None):
    path = root / "model-events" / (digest([folder.name, attempt_key(attempt)]) + ".json")
    existing = read(path)
    if existing:
        return existing
    event = dict(request_id=folder.name, pool_request_id=attempt.get("pool_request_id"), turn_id=attempt.get("turn_id"),
                 model=attempt["model"], outcome=outcome, model_failure=model_failure,
                 elapsed_seconds=elapsed, finished_at=time.time() if finished_at is None else finished_at)
    save(path, event)
    return event


def dispatch(root, folder, config, model):
    with lock(folder / "request.lock"):
        result = read(folder / "result.json")
        if result is not None:
            if (result.get('state') != 'failed' or
                    read(folder / 'state.json', {}).get('retry_of') != digest(result)):
                return None
            from ..warm_pool.state import immutable
            immutable(folder / ('failed-result-' + digest(result) + '.json'), result)
            (folder / 'result.json').unlink()
        return _dispatch(root, folder, config, model)


def _dispatch(root, folder, config, model):
    """Persist intent before enqueueing so dispatcher replacement cannot duplicate a turn."""
    from ..warm_pool.state import immutable, verified
    from .session import archive_adapter
    request = read(folder / "request.json")
    settings = policy(config)
    state = read(folder / "state.json", {})
    attempts = state.get("attempts", [])
    number = len(attempts)
    # The turn's own digest is part of the id: a turn folder re-created under the same name with
    # different content must not replay the earlier reply (eye 2026-09-17: a resumed session replayed
    # 34 archived model replies because the pool replays saved ids).
    identity = digest([str(root), folder.name, request["digest"], number])[:32]
    plan_path = folder / f"dispatch-{number}.json"
    plan = read(plan_path) or dict(request=request, model=model, timeout_seconds=settings["response_timeout_seconds"],
                cpus=settings["worker_cpus"], memory_mb=settings["worker_memory_mb"],
                adapter_sha256=archive_adapter(root)["sha256"])
    portable = False
    if request['spec']['operation_id'] == 'agent.turn':
        session = verified(request['spec']['session'])
        portable = session.get('protocol', 1) >= 2
        if not plan_path.exists() and portable and session['adapter_sha256'] != plan['adapter_sha256']:
            # Upgrade only the portable transport; original session/tool contracts
            # remain immutable and are validated by their original adapter.
            plan['portable_adapter'] = archive_adapter(root)
    plan_ref = immutable(plan_path, plan)
    key = model_key(plan["model"])
    if portable and runner_ready(root, config, key):
        attempt = dict(execution="service", turn_id="turn-" + identity, runner=key, model=plan["model"],
                       plan=plan_ref, submitted_at=time.time())
    else:
        attempt = dict(pool_request_id="agent-" + identity, model=plan["model"], plan=plan_ref, submitted_at=time.time())
    attempts.append(attempt)
    save(folder / "state.json", dict(state, state="running", execution=attempt.get("execution", "pool"),
         pool_root=config["pool_root"], started_at=state.get("started_at", time.time()), attempts=attempts))
    enqueue(folder, config, attempt)
    return attempt


def attempt_key(attempt):
    return attempt.get("pool_request_id") or attempt["turn_id"]


def runner_state(root, key):
    return read(Path(root) / "runners" / (key + ".json"), {})


def runner_ready(root, config, key, now=None):
    """A resident runner (agent/runner.py) takes this model's turns while its heartbeat is fresh
    and config["service"]["models"] names the model ("all" or a list of model keys); otherwise,
    and for legacy sessions, the turn is a pool task as before. Online: read every dispatch."""
    service = config.get("service") or {}
    wanted = service.get("models", [])
    if not (wanted == "all" or key in wanted):
        return False
    beat = runner_state(root, key)
    now = time.time() if now is None else now
    return bool(beat) and not beat.get("draining") and now - beat.get("observed_at", 0) <= service.get("stale_seconds", 120)


def enqueue(folder, config, attempt):
    if attempt.get("execution") == "service":
        return enqueue_service(folder, attempt)
    plan = read(attempt["plan"]["path"])
    request = read(folder / "request.json")
    trace = request["spec"].get("trace", {})
    submit(config["pool_root"], dict(request_id=attempt["pool_request_id"], operation_id="agent.call",
           args=["-m", "ecarsi.agent.dispatch", "execute", attempt["plan"]["path"]],
           cpus=plan["cpus"], memory_mb=plan["memory_mb"],
           timeout_seconds=plan["timeout_seconds"] + 60,
           inputs=[attempt["plan"]], outputs=["result.json"], **({"trace": trace} if trace else {})))


def enqueue_service(folder, attempt):
    """A marker the runner picks up; idempotent, and never re-queued once the turn has started."""
    root = folder.parent.parent
    turn_dir = root / "turns" / attempt["turn_id"]
    if (turn_dir / "started.json").exists() or (turn_dir / "result.json").exists():
        return
    marker = root / "runner-queue" / attempt["runner"] / (attempt["turn_id"] + ".json")
    if not marker.exists():
        marker.parent.mkdir(parents=True, exist_ok=True)
        turn_dir.mkdir(parents=True, exist_ok=True)
        save(marker, dict(request=folder.name, plan=attempt["plan"]["path"], turn_dir=str(turn_dir), queued_at=time.time()))


def reconcile_pool(root, folder, config, events):
    with lock(folder / "request.lock"):
        if read(folder / "result.json") is None:
            _reconcile_pool(root, folder, config, events)


def _reconcile_pool(root, folder, config, events):
    """Accept one attempt's fenced output; late alternatives never publish Bridge replies."""
    from ..warm_pool.state import verified
    state = read(folder / "state.json")
    attempt = state["attempts"][-1]
    if attempt.get("execution") == "service":
        return _reconcile_service(root, folder, config, events, state, attempt)
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
        from ..warm_pool.state import cancel
        cancel(state["pool_root"], attempt["pool_request_id"])
    _settle(root, folder, config, events, state, attempt, outcome, response, elapsed,
            (current["receipt"] or {}).get("finished_at"))


def _reconcile_service(root, folder, config, events, state, attempt):
    """The runner's turn directory plays the pool attempt's part: started.json, then result.json.
    No result and no runner heartbeat is a lost worker; a started turn past its deadline is a timeout."""
    enqueue(folder, config, attempt)
    turn_dir = root / "turns" / attempt["turn_id"]
    response = read(turn_dir / "result.json")
    started = read(turn_dir / "started.json", {})
    beat = runner_state(root, attempt["runner"])
    stale = time.time() - beat.get("observed_at", 0) > (config.get("service") or {}).get("stale_seconds", 120)
    elapsed = time.time() - started["started_at"] if started else None
    outcome = None
    if response is not None:
        outcome, elapsed = response["outcome"], response.get("elapsed_seconds", elapsed)
    elif started and elapsed > settings_timeout(attempt) + 30:
        outcome = "timeout"
    elif stale:
        outcome = "worker_lost"
    if outcome is None:
        return
    (root / "runner-queue" / attempt["runner"] / (attempt["turn_id"] + ".json")).unlink(missing_ok=True)
    _settle(root, folder, config, events, state, attempt, outcome, response, elapsed, None)


def _settle(root, folder, config, events, state, attempt, outcome, response, elapsed, finished_at):
    from ..warm_pool.state import verified
    model_failure = outcome in {"timeout", "provider_error", "incomplete_submission"}
    event = record_event(root, folder, attempt, outcome, elapsed=elapsed, model_failure=model_failure,
                         finished_at=finished_at)
    events[attempt_key(attempt)] = event
    if outcome == "success":
        origin = {"pool_request_id": attempt["pool_request_id"]} if "pool_request_id" in attempt else {"turn_id": attempt["turn_id"]}
        save(folder / "result.json", dict(state="reply_saved", finished_at=time.time(),
             response=response["response"], worker=response["worker"], **origin))
    else:
        # Legacy harnesses may run arbitrary programs; an uncertain legacy call
        # is not equivalent to a tool-free model turn and must not be blindly repeated.
        spec = read(folder / "request.json")["spec"]
        safe = spec["operation_id"] == "agent.turn" and verified(spec["session"]).get("protocol", 1) >= 2
        retry = safe and len(state["attempts"]) < state.get('attempt_limit', policy(config)["max_attempts"]) and outcome != "local_error"
        save(folder / "state.json", dict(state, state="queued" if retry else "failed",
                                         last_outcome=outcome, updated_at=time.time()))
        if not retry:
            save(folder / "result.json", dict(state="failed", reason=outcome, finished_at=time.time()))


def settings_timeout(attempt):
    return read(attempt["plan"]["path"])["timeout_seconds"]


def credential_timeout(state):
    """Recognize older credential-load timeouts from their accepted worker receipt."""
    from ..warm_pool.state import verified
    attempt = state.get('attempts', [])[-1:]
    if not attempt:
        return False
    current = status(state['pool_root'], attempt[0]['pool_request_id'])
    if current['state'] != 'succeeded':
        return False
    outputs = [o for o in current['receipt']['outputs'] if Path(o['path']).name == 'result.json']
    if len(outputs) != 1:
        return False
    response = verified({k: outputs[0][k] for k in ('path', 'sha256')})
    return (response.get('outcome') == 'local_error' and response.get('error') == 'TimeoutExpired'
            and response.get('response') is None)


def invalid_dispatch_snapshot(state):
    """Recognize a historical unused snapshot that failed before any API call."""
    from ..warm_pool.state import verified
    from .session import validate_turn
    attempt = state.get('attempts', [])[-1:]
    if not attempt:
        return False
    current = status(state['pool_root'], attempt[0]['pool_request_id'])
    if current['state'] != 'succeeded':
        return False
    output = next((o for o in current['receipt']['outputs'] if Path(o['path']).name == 'result.json'), None)
    if output is None:
        return False
    response = verified({k: output[k] for k in ('path', 'sha256')})
    if (response.get('outcome') != 'local_error' or response.get('response') is not None
            or response.get('error') not in {'SyntaxError', 'IndentationError'}):
        return False
    plan = verified(attempt[0]['plan']); spec = plan['request']['spec']
    session = verified(spec['session'])
    if plan['adapter_sha256'] == session['adapter_sha256']:
        return False
    validate_turn(spec)  # The adapter that will actually execute must still validate.
    path = Path(session['spec']['bridge_root']) / 'adapters' / (plan['adapter_sha256'] + '.py')
    if file_digest(path) != plan['adapter_sha256']:
        return False
    try:
        compile(path.read_bytes(), str(path), 'exec')
    except SyntaxError:
        return True
    return False


def completed_replacement(pool_root, request_id, bridge_root):
    """A fenced model-only attempt cannot block recovery after its reply succeeded."""
    from ..warm_pool.state import verified
    from . import status as bridge_status
    request = read(Path(pool_root) / 'requests' / request_id / 'request.json', {})
    spec = request.get('spec', {})
    args = spec.get('args', [])
    if (spec.get('operation_id') != 'agent.call' or len(args) != 4 or
            args[:3] != ['-m', 'ecarsi.agent.dispatch', 'execute']):
        return False
    current = status(pool_root, request_id)
    if current['state'] not in {'failed', 'cancelled'} or not current.get('receipt'):
        return False
    plan_ref = next((r for r in spec['inputs'] if r['path'] == args[3]), None)
    if plan_ref is None:
        return False
    plan = verified(plan_ref)
    turn = plan['request']['spec']
    if turn['operation_id'] != 'agent.turn' or verified(turn['session']).get('protocol', 1) < 2:
        return False
    folder = Path(bridge_root).resolve() / 'requests' / turn['request_id']
    if Path(args[3]).resolve().parent != folder or read(folder / 'request.json') != plan['request']:
        return False
    result = bridge_status(bridge_root, folder.name)
    attempts = result.get('attempts', [])
    if (result['state'] != 'reply_saved' or not attempts or
            request_id not in [a['pool_request_id'] for a in attempts[:-1]] or
            result.get('pool_root') != str(pool_root) or
            result.get('pool_request_id') != attempts[-1]['pool_request_id']):
        return False
    winner = status(pool_root, result['pool_request_id'])
    if winner['state'] != 'succeeded':
        return False
    output = next(o for o in winner['receipt']['outputs'] if Path(o['path']).name == 'result.json')
    response = verified({k: output[k] for k in ('path', 'sha256')})
    return response.get('outcome') == 'success' and response.get('response') == result.get('response')


def session_order(queued, wait_seconds, cache, now, offset):
    """Advance existing sessions while reserving one in four choices for aged FIFO."""
    sessions = deque(sorted(queued, key=lambda x: (cache[x[1].name]['first'], x[0])))
    aged = deque(sorted((x for x in queued if now - x[0] >= wait_seconds), key=lambda x: x[0]))
    chosen = set()
    while len(chosen) < len(queued):
        for items in (sessions, aged):
            while items and items[0][1] in chosen:
                items.popleft()
        # ponytail: fixed 3:1 service share; tune only from measured completion
        # throughput. All-aged FIFO alone restores the round-robin convoy.
        items = aged if offset % 4 == 3 and aged else sessions
        item = items.popleft()
        chosen.add(item[1])
        offset += 1
        yield item


def queue_order(root, queued, wait_seconds, cache, now=None, offset=0, served=None):
    """Round-robin ready operation kinds so sample fan-out cannot bury later stages."""
    now = time.time() if now is None else now
    groups = {}
    for submitted, folder in queued:
        if folder.name not in cache:
            spec = read(folder / 'request.json')['spec']
            ref, first = spec.get('session'), submitted
            if ref:
                session = read(ref['path'])['spec']
                initial = read(root / 'requests' / (session['session_id'] + '.turn-0') / 'request.json')
                if initial:
                    first = initial['submitted_at']
            cache[folder.name] = dict(first=first,
                unit=spec.get('trace', {}).get('unit_id', spec.get('operation_id', 'agent.turn')))
        groups.setdefault(cache[folder.name]['unit'], []).append((submitted, folder))
    names = sorted(groups)
    if not names:
        return
    rotation = offset % len(names)
    names = names[rotation:] + names[:rotation]
    queues = deque(iter(session_order(groups[name], wait_seconds, cache, now,
        served.get(name, 0) if served is not None else offset)) for name in names)
    while queues:
        items = queues.popleft()
        item = next(items, None)
        if item is not None:
            yield item
            queues.append(items)


def serve(root, *, once=False, finished=None):
    from . import root_path, status as bridge_status, reconcile
    from ..model_web import normalized_models
    root = root_path(root)
    (root / "model-events").mkdir(mode=0o700, exist_ok=True)
    # Read immutable events once per service lifetime; no growing history scan per tick.
    events = {e.get("pool_request_id") or e.get("turn_id"): e for p in (root / "model-events").glob("*.json") if (e := read(p))}
    # Settled requests are cached by (name, inode): a folder archived and re-created under the
    # same name is new work (Eye turn-0 sat queued for 14 h behind a name-keyed cache, 2026-09-17).
    # The cache is persisted: rebuilding it read two files in each of 78k folders and stalled every
    # model reply for ~40 min after each bridge restart (2026-09-23/24). Advisory, like the pool's
    # settled.json -- a stale entry costs one re-read, a reply can only be published from a result.
    finished, ordering = ({} if finished is None else finished), {}
    if not finished:
        finished.update({(name, ino): state for name, ino, state in read(root / "finished.json", [])})
    persisted = len(finished)
    dispatch_count = len(events)
    served = Counter()
    with lock(root / "service.lock", blocking=False):
        while True:
            scanning = time.monotonic()
            config = read(root / "config.json")
            settings = policy(config)
            if type(config["concurrency"]) is not int or config["concurrency"] < 1:
                raise ValueError("Bridge concurrency must be a positive integer")
            active, queued, legacy_active = Counter(), [], 0
            error = None
            for entry in sorted(os.scandir(root / "requests"), key=lambda e: e.name):
                folder, key = Path(entry.path), (entry.name, entry.inode())
                # Successful replies are immutable; failed requests can be
                # explicitly reopened. Revisit only those few failures.
                if finished.get(key) == 'reply_saved' or not (folder / "request.json").is_file():
                    continue
                finished.pop(key, None)
                state = bridge_status(root, folder.name)
                if state["state"] == "running" and state.get("execution") in {"pool", "service"}:
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
                    finished[key] = state["state"]
                elif state["state"] == "queued":
                    queued.append((state["submitted_at"], folder))
                elif state.get("attempts"):
                    active[model_key(state["attempts"][-1]["model"])] += 1
                else:
                    legacy_active += 1
            health = {}
            for _, folder in queue_order(root, queued, settings['session_wait_seconds'], ordering,
                                         offset=dispatch_count, served=served):
                if sum(active.values()) + legacy_active >= config["concurrency"]:
                    break
                try:
                    models = routes(config, read(folder / "request.json"))
                    rows = []
                    for model in models:
                        key = model_key(model)
                        if key not in health:
                            health[key] = model_health(events, [model], active, settings)[0]
                        rows.append(health[key])
                    tried = {model_key(a["model"]) for a in read(folder / "state.json", {}).get("attempts", [])}
                    untried = [row for row in rows if row["key"] not in tried]
                    ready = [row for row in (untried or rows) if row["state"] == "ready"]
                    if ready:
                        selected = ready[0]
                        attempt = dispatch(root, folder, config, selected["model"])
                        if attempt:
                            active[model_key(attempt["model"])] += 1
                            dispatch_count += 1
                            served[ordering[folder.name]['unit']] += 1
                            selected['in_flight'] += 1
                            if selected['in_flight'] >= settings['model_concurrency']:
                                selected['state'] = 'busy'
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
                 routing=settings, service=config.get("service") or {},
                 runners={p.stem: read(p) for p in (root / "runners").glob("*.json")} if (root / "runners").is_dir() else {}))
            if len(finished) != persisted:
                save(root / "finished.json", [[name, ino, state] for (name, ino), state in finished.items()])
                persisted = len(finished)
            if once:
                return
            time.sleep(.5)  # a turn waits half a tick on average before dispatch; the scan itself is ~1 s


def load_worker_key(model):
    """Read the user's shell configuration in the worker, never in task arguments/logs."""
    from ..model_web import PROVIDERS
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
    """Pool-task entry: one process per model turn. The runner (runner.py) calls perform() directly."""
    asyncio.run(perform(read(plan_path), Path.cwd(), setup=configure))


def configure(model, timeout_seconds):
    """Provider settings are process environment (session.py and the SDK read them): the pool
    executor sets them per task, a runner once per process, so a runner serves one model."""
    from ..model_web import PROVIDERS
    load_worker_key(model)
    os.environ["OPENAI_AGENTS_REQUEST_TIMEOUT_S"] = str(timeout_seconds)
    os.environ["AGENT_MODEL_POOL"] = model["harness"] + ":" + model["model"]
    if model["url"] and model["harness"] in PROVIDERS:
        os.environ[PROVIDERS[model["harness"]][1]] = model["url"]


async def perform(plan, folder, *, setup=None):
    """Run one planned model turn in `folder`, leaving started.json and result.json there.
    `setup(model, timeout)` runs after the plan is validated: the pool executor configures its
    process here (credentials come from the user's shell, so after validation and inside the
    result's accounting); a runner did it once at start and passes nothing."""
    from . import run_organize, sdk_restore_compat
    from .session import run_turn, validate_turn, pinned_adapter
    from ..warm_pool.state import verified
    started = time.time()
    worker = dict(host=socket.gethostname().split(".")[0], pid=os.getpid())
    save(folder / "started.json", dict(started_at=started, worker=worker, model=plan["model"]))
    outcome, response, error = "local_error", None, None
    try:
        spec = plan["request"]["spec"]
        turn, turn_options = run_turn, {}
        if spec["operation_id"] == "agent.turn":
            session = verified(spec["session"])
            # validate_turn loads the session-pinned adapter actually used by
            # run_turn. The dispatch-time snapshot is provenance, not executable.
            validate_turn(spec)
            if plan.get('portable_adapter'):
                ref = plan['portable_adapter']
                if session.get('protocol', 1) != 2 or file_digest(ref['path']) != ref['sha256']:
                    raise ValueError('Invalid portable adapter upgrade')
                adapter = pinned_adapter(session['spec']['bridge_root'], ref['sha256'])
                turn = adapter.run_turn if adapter is not None else run_turn
                turn_options['portable_upgrade'] = True
        elif file_digest(Path(__file__).with_name("session.py")) != plan["adapter_sha256"]:
            raise ValueError("Agent adapter changed after dispatch")
        if setup is not None:
            setup(plan["model"], plan["timeout_seconds"])
        try:
            if spec["operation_id"] == "agent.turn":
                legacy = session.get("protocol", 1) < 2
                with sdk_restore_compat(folder) if legacy else nullcontext():
                    response = await asyncio.wait_for(turn(plan["request"], folder, model=plan["model"], **turn_options),
                                                      timeout=plan["timeout_seconds"])
            else:
                response = run_organize(plan["request"], folder=folder)
            outcome = "success"
        except Exception as exc:
            # Model turns cannot execute tools. Failed turn outputs remain fenced
            # in this attempt, even if a remote model completes later.
            error = type(exc).__name__
            saved = read(folder / "turn-response.json")
            if saved is not None:
                outcome, response = "success", saved
            else:
                outcome = "timeout" if isinstance(exc, TimeoutError) or "Timeout" in error else "provider_error"
    except subprocess.TimeoutExpired as exc:
        # Shell credential setup precedes the provider request. A transient
        # worker startup delay is retryable and must not poison model health.
        outcome, error = 'worker_setup_timeout', type(exc).__name__
    except Exception as exc:
        error = type(exc).__name__
    if (outcome == 'success' and spec['operation_id'] == 'agent.turn'
            and session['spec'].get('completion_tool') and response.get('kind') == 'final'):
        # This turn cannot execute tools. A free-text conclusion is not a
        # submitted scientific result; bounded model fallback can safely retry.
        outcome, error = 'incomplete_submission', 'Required completion tool was not called'
    save(folder / "result.json", dict(outcome=outcome, response=response, error=error, worker=worker,
                                     elapsed_seconds=time.time()-started, model=plan["model"],
                                     provider_response=read(folder / 'provider-response.json')))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["execute"])
    parser.add_argument("plan")
    args = parser.parse_args()
    execute(args.plan)

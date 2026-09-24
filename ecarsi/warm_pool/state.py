"""Small durable records on a filesystem with coherent flock and atomic rename.

This is request/receipt storage, not a second resource scheduler. All clients
must use the same trusted, durable pool directory; node-local storage is not
a replacement for it. No SQLite database is opened on a shared mount.
"""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time
import uuid


def read(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def save(path, value):
    """Publish only after file contents and the containing directory are synced."""
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextmanager
def lock(path, blocking=True):
    """flock on the file at path, creating it, or on the directory itself when path is one.

    A directory lock costs no inode, but Lustre only enforces it within a node (measured
    2026-09-24: a directory flock held on one node was granted again on another, a file
    flock was not), so it fits only same-node arbitration such as an attempt's execution lock.
    """
    path = Path(path)
    if path.is_dir():
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            yield _Fd(fd)
        finally:
            os.close(fd)
        return
    with path.open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        yield stream


class _Fd:
    def __init__(self, fd):
        self._fd = fd

    def fileno(self):
        return self._fd


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", value):
        raise ValueError("IDs must be 1-96 ASCII letters, digits, dots, dashes or underscores")
    return value


def validate_trace(trace):
    required = {"workflow_id", "dataset_id", "unit_id"}
    if not isinstance(trace, dict) or not required <= trace.keys() or trace.keys() - required - {"depends_on", "sample_id"}:
        raise ValueError("trace requires workflow_id, dataset_id and unit_id")
    for key in required | ({"sample_id"} if "sample_id" in trace else set()):
        value = trace[key]
        if not isinstance(value, str) or not 0 < len(value) <= 256 or any(ord(c) < 32 for c in value):
            raise ValueError("trace " + key + " must be a nonempty printable string")
    if "depends_on" in trace:
        parents = trace["depends_on"]
        if not isinstance(parents, list) or len(parents) > 4096 or len(set(map(str, parents))) != len(parents):
            raise ValueError("trace depends_on must be a unique list of at most 4096 request IDs")
        for parent in parents:
            identifier(parent)
    return trace


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def file_digest(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def pool_root(root):
    root = Path(root).resolve(strict=True)
    info = root.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError("pool directory must be owned by this user with mode 0700")
    if not (root / "config.json").is_file():
        raise ValueError("pool directory has not been initialized")
    return root


def validate(spec):
    allowed = {"request_id", "operation_id", "args", "cpus", "memory_mb", "timeout_seconds",
               "time_request_seconds", "inputs", "outputs", "trace", "gpu"}
    if not isinstance(spec, dict) or not {"request_id", "operation_id"} <= spec.keys():
        raise ValueError("request must be an object with request_id and operation_id")
    if set(spec) - allowed:
        raise ValueError("unknown request fields: " + ", ".join(sorted(set(spec) - allowed)))
    spec = dict(spec)
    if "gpu" in spec:
        gpu = spec["gpu"]
        if (not isinstance(gpu, dict) or set(gpu) != {"mode", "memory_mb"} or
                gpu["mode"] not in {"required", "preferred"} or
                type(gpu["memory_mb"]) is not int or gpu["memory_mb"] <= 0):
            raise ValueError("gpu needs required/preferred mode and positive memory_mb")
    for key in ("request_id", "operation_id"):
        identifier(spec[key])
    if "trace" in spec:
        validate_trace(spec["trace"])
    if not isinstance(spec.get("args"), list) or not spec["args"] or not all(isinstance(a, str) and "\0" not in a for a in spec["args"]):
        raise ValueError("args must be a nonempty argument list for the configured runtime")
    for key in ("cpus", "memory_mb", "timeout_seconds"):
        if type(spec.get(key)) is not int or spec[key] <= 0:
            raise ValueError(key + " must be a positive integer")
    spec.setdefault("time_request_seconds", spec["timeout_seconds"] + 30)
    if type(spec["time_request_seconds"]) is not int or spec["time_request_seconds"] < spec["timeout_seconds"]:
        raise ValueError("time request must cover the execution timeout")
    spec.setdefault("inputs", [])
    spec.setdefault("outputs", [])
    if not isinstance(spec["inputs"], list) or not isinstance(spec["outputs"], list) or not spec["outputs"]:
        raise ValueError("inputs and nonempty outputs must be lists")
    for item in spec["inputs"]:
        if (not isinstance(item, dict) or set(item) != {"path", "sha256"}
                or not isinstance(item["path"], str) or "\0" in item["path"] or not Path(item["path"]).is_absolute()
                or not isinstance(item["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"])):
            raise ValueError("inputs require an absolute path and SHA256")
    for name in spec["outputs"]:
        if (not isinstance(name, str) or not name or "\0" in name or Path(name).is_absolute()
                or name != str(Path(name)) or any(p in {"..", "."} for p in name.split("/"))):
            raise ValueError("outputs must be paths relative to the attempt output directory")
    if len(set(spec["outputs"])) != len(spec["outputs"]):
        raise ValueError("duplicate outputs")
    return spec


def submit(root, spec):
    from .budget import measured_ceiling
    root, requested = pool_root(root), validate(spec)
    spec = measured_ceiling(requested)
    folder = root / "requests" / spec["request_id"]
    folder.mkdir(mode=0o700, exist_ok=True)
    sync_directory(folder.parent)
    with lock(folder / "request.lock"):
        existing = read(folder / "request.json")
        fingerprint = digest(spec)
        if existing:
            # A request id names work already recorded: the id carries the payload digest
            # (or the run's immutable spec), so a resubmission that differs only in packaging
            # (budget ceilings, program version, operator floors) replays the saved request
            # instead of failing the resume. The difference is kept for audit.
            if not {fingerprint, digest(requested)} & {existing["digest"], existing.get("original_digest")}:
                save(folder / "resubmitted.json", dict(digest=fingerprint, requested_digest=digest(requested),
                                                       spec=spec, at=time.time()))
        else:
            config = read(root / "config.json")
            existing = dict(spec=spec, digest=fingerprint, submitted_at=time.time(),
                            attempt_id=uuid.uuid4().hex, runtime=config["runtime"],
                            runtime_digest=digest(config["runtime"]))
            attempt = folder / existing["attempt_id"]
            attempt.mkdir(mode=0o700)
            (attempt / "outputs").mkdir(mode=0o700)
            sync_directory(attempt)
            save(folder / "request.json", existing)
    return status(root, spec["request_id"])


def cancel(root, request_id):
    folder = pool_root(root) / "requests" / identifier(request_id)
    if not read(folder / "request.json"):
        raise KeyError(request_id)
    # Separate from the execution lock: cancellation cannot wait for completion.
    with lock(folder / "request.lock"):
        if not read(folder / "cancel.json"):
            save(folder / "cancel.json", dict(requested_at=time.time()))
    return status(root, request_id)


def retry(root, request_id, *, reason, use_current_runtime=False, memory_mb=None, timeout_seconds=None, without_gpu=False):
    """New attempt after a confirmed failure; retain the original inputs and audit.

    memory_mb / timeout_seconds raise the budget of a request that died on its RSS
    watchdog or its execution deadline; without_gpu drops a preferred GPU whose memory
    budget the attempt exceeded. The inputs, program and outputs are unchanged, so the
    work identity is retained.
    """
    root = pool_root(root)
    folder = root / "requests" / identifier(request_id)
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("Retry needs a recorded reason")
    with lock(folder / "request.lock"):
        request = read(folder / "request.json")
        if not request:
            raise KeyError(request_id)
        previous = folder / request["attempt_id"]
        receipt = read(previous / "receipt.json")
        if (read(folder / "cancel.json") or not receipt or receipt.get("state") != "failed"
                or not receipt.get("finished_at") or receipt.get("attempt_id") != request["attempt_id"]
                or receipt.get("request_digest") != request["digest"]
                or receipt.get("runtime_digest") != request["runtime_digest"]):
            raise ValueError("Only a confirmed failed attempt can be retried; reconcile unknown outcomes first")
        for item in request["spec"]["inputs"]:
            if file_digest(item["path"]) != item["sha256"]:
                raise ValueError("Retry input changed: " + item["path"])
        runtime = read(root / "config.json")["runtime"] if use_current_runtime else request["runtime"]
        spec = request["spec"]
        if memory_mb is not None:
            if type(memory_mb) is not int or memory_mb <= spec["memory_mb"]:
                raise ValueError("memory_mb override must exceed the failed attempt's budget")
            spec = dict(spec, memory_mb=memory_mb)
        if timeout_seconds is not None:
            if type(timeout_seconds) is not int or timeout_seconds <= spec["timeout_seconds"]:
                raise ValueError("timeout_seconds override must exceed the failed attempt's limit")
            spec = dict(spec, timeout_seconds=timeout_seconds,
                        time_request_seconds=timeout_seconds + spec["time_request_seconds"] - spec["timeout_seconds"])
        if without_gpu:
            if spec.get("gpu", {}).get("mode") != "preferred":
                raise ValueError("without_gpu applies to a preferred GPU only")
            spec = {k: v for k, v in spec.items() if k != "gpu"}
        attempt_id = uuid.uuid4().hex
        attempt = folder / attempt_id
        attempt.mkdir(mode=0o700)
        (attempt / "outputs").mkdir(mode=0o700)
        sync_directory(attempt)
        save(previous / "request.json", request)  # the audit copy carries the scheduler's last observation
        replacement = dict(request, spec=spec, digest=digest(spec), backend=dict(state="queued", attempt_id=attempt_id),
            original_digest=request.get("original_digest", request["digest"]),
            attempt_id=attempt_id, submitted_at=time.time(),
            runtime=runtime, runtime_digest=digest(runtime),
            retry_count=request.get("retry_count", 0) + 1,
            retry=dict(previous_attempt_id=request["attempt_id"], reason=reason,
                       use_current_runtime=use_current_runtime, memory_mb=memory_mb,
                       timeout_seconds=timeout_seconds, without_gpu=without_gpu))
        # The old receipt remains authoritative until request.json switches atomically.
        save(folder / "request.json", replacement)
        (folder / "backend.json").unlink(missing_ok=True)
    return status(root, request_id)


def observation(folder, request):
    """The scheduler's last look at a request (state, HQ job, generation). Inside request.json since
    2026-09-24; a separate backend.json before that, kept readable until those requests are gone."""
    return request.get("backend") or read(folder / "backend.json", {})


def status(root, request_id=None):
    root = pool_root(root)
    if request_id is None:
        return [status(root, p.name) for p in sorted((root / "requests").iterdir()) if (p / "request.json").is_file()]
    folder = root / "requests" / identifier(request_id)
    request = read(folder / "request.json")
    if request is None:
        raise KeyError(request_id)
    attempt = folder / request["attempt_id"]
    receipt = read(attempt / "receipt.json")
    accepted = read(attempt / "accepted.json")
    cancellation = read(folder / "cancel.json")
    backend = observation(folder, request)
    usage = read(attempt / "usage.json")
    state = receipt["state"] if receipt else "running" if accepted else backend.get("state", "queued")
    if accepted and not receipt and time.time() - (usage or {}).get("observed_at", accepted["started_at"]) > 15:
        state = "unknown_external_result"  # stale observation never authorizes a retry
    if cancellation:
        # A late result remains a candidate artifact but cannot advance a workflow.
        state = "cancelled" if receipt or not accepted and backend.get("state") == "cancelled" else "cancel_requested"
    return dict(request_id=request_id, operation_id=request["spec"]["operation_id"],
                attempt_id=request["attempt_id"], state=state, receipt=receipt,
                accepted=accepted, backend=backend, cancellation=cancellation,
                usage=usage, submitted_at=request["submitted_at"])

def reference(path):
    path = Path(path).resolve(strict=True)
    return {"path": str(path), "sha256": file_digest(path)}


def verified(ref):
    if set(ref) != {"path", "sha256"} or not Path(ref["path"]).is_absolute():
        raise ValueError("Expected absolute artifact reference")
    if file_digest(ref["path"]) != ref["sha256"]:
        raise ValueError("Artifact changed: " + ref["path"])
    return read(ref["path"])


def immutable(path, value):
    path = Path(path)
    with lock(path.with_suffix(path.suffix + ".lock")):
        old = read(path)
        if old is not None and digest(old) != digest(value):
            raise ValueError("Conflicting durable content: " + str(path))
        if old is None:
            save(path, value)
    return reference(path)


def archive(root, runs, destination=None, dry_run=False, on_progress=None):
    """Move every settled request of the named runs out of the pool.

    A request folder is not a log of what ran: `<attempt>/outputs/` holds the computed
    artefacts themselves, and a dataset's publications reference them by absolute path
    and digest. Moving them is therefore safe only once a run is published -- the light
    artefacts are copied into the round directories by then (see control/artifacts.py),
    and the matrices are in `release/`. What it does give up is reopening that run:
    `verified(reference(path))` will no longer find its inputs.

    Requests are named after a stage (`<dataset>-<hash>.<kind>-<hash>`), a session
    (`cross-<hash>.tool-<hash>`) or nothing at all (`agent-<hash>`), so membership comes
    from `spec.trace`, which every request carries. That means reading one small file per
    folder, paced: an unpaced walk of this directory stalled the control node's Lustre
    client on 2026-09-17 and failed three healthy datasets.
    """
    root = pool_root(root)
    runs = set(runs)
    if not runs:
        raise ValueError("Name the runs to archive; this command never guesses")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    destination = Path(destination) if destination else root.parent / "archived-requests" / ("released-" + stamp)
    moved, kept, unsettled, reads = [], 0, [], 0
    for entry in sorted(os.scandir(root / "requests"), key=lambda e: e.name):
        reads += 1
        if reads > 500:
            time.sleep(0.01)   # ponytail: same crude cap as the observatory walk
        if on_progress and reads % 5000 == 0:
            on_progress(reads, len(moved))
        folder = Path(entry.path)
        request = read(folder / "request.json")
        if request is None:
            continue
        trace = request["spec"].get("trace") or {}
        workflow = str(trace.get("workflow_id", ""))
        # Exact names only: `startswith(run)` also swallowed the `-b`/`-c` resubmissions of an archived
        # run (their organize and dataset-level requests), which then could not be resumed (2026-09-23).
        if not any(workflow.split("/")[-1] in (run, run + "-organize") or trace.get("dataset_id") == run for run in runs):
            kept += 1
            continue
        state = status(root, entry.name)["state"]
        if state not in {"succeeded", "complete", "cancelled", "failed"}:
            unsettled.append((entry.name, state))
            continue
        moved.append(entry.name)
    # Decide over the whole pool before moving anything: a live request found late must not
    # leave the ones scanned before it already gone.
    if unsettled:
        raise ValueError(f"{len(unsettled)} request(s) of these runs are still live, "
                         f"first: {unsettled[0]}; archive only a finished run")
    if not dry_run and moved:
        (destination / "requests").mkdir(mode=0o700, parents=True, exist_ok=True)
        for name in moved:
            (root / "requests" / name).rename(destination / "requests" / name)
    manifest = dict(archived_at=stamp, runs=sorted(runs), pool=str(root),
                    destination=str(destination), requests=moved, retained=kept, dry_run=dry_run)
    if not dry_run and moved:
        save(destination / "manifest.json", manifest)
        settled = [pair for pair in read(root / "settled.json", []) if pair[0] not in set(moved)]
        save(root / "settled.json", settled)
    return manifest

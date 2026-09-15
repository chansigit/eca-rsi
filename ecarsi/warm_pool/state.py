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
    with Path(path).open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        yield stream


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
        if not isinstance(parents, list) or len(parents) > 32 or len(set(map(str, parents))) != len(parents):
            raise ValueError("trace depends_on must be a unique list of at most 32 request IDs")
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
    root, spec = pool_root(root), validate(spec)
    folder = root / "requests" / spec["request_id"]
    folder.mkdir(mode=0o700, exist_ok=True)
    sync_directory(folder.parent)
    with lock(folder / "request.lock"):
        existing = read(folder / "request.json")
        fingerprint = digest(spec)
        if existing:
            if existing["digest"] != fingerprint:
                raise ValueError("request ID already exists with different content")
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
    backend = read(folder / "backend.json", {})
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

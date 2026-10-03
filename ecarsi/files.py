"""Durable JSON records on a shared filesystem: atomic publish, flock, content references.

Every part of eca-rsi hands its results over as files (decision 0004); these are the primitives they all use.
`save` publishes only after the contents and the directory are synced; `immutable` writes a record once;
`reference` / `verified` name a file by content. (`run_state` keeps the older, byte-different JSON format of
the organize manifests; their identities depend on it.)
"""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile


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

def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()

def file_digest(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()

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
    """Write a record once; a later call must carry the same content.

    The lock only arbitrates the first write, so it is unlinked once the record is published
    (it used to stay: ~40 % of a run's files were empty locks). Whoever still holds or waits
    on the unlinked lock, and whoever creates a new one, reads the published record and only
    compares. The lock stays if the write fails, so no record-less lock is ever removed."""
    path = Path(path)
    old = read(path)  # published by atomic rename: complete or absent
    if old is None:
        guard = path.with_suffix(path.suffix + ".lock")
        with lock(guard):
            old = read(path)
            if old is None:
                save(path, value)
            guard.unlink(missing_ok=True)
    if old is not None and digest(old) != digest(value):
        raise ValueError("Conflicting durable content: " + str(path))
    return reference(path)

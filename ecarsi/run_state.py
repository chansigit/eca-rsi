"""Atomic records, content identities and process locks for the front pipeline."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path

from .warm_pool.state import sync_directory


def read_json(path: Path) -> dict:
    return json.loads(path.read_text())


def write_json(path: Path, value: dict) -> None:
    """Atomic and durable: a node death must not leave an empty manifest or .pruned marker. Not
    warm_pool.state.save: its bytes differ, and earlier manifests' file identities depend on them."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        with tmp.open("w") as stream:
            stream.write(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        sync_directory(path.parent)
    finally:
        tmp.unlink(missing_ok=True)


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def file_identity(path: Path) -> dict:
    """Hash once per driver invocation, never once per sample/child.

    Stat before/after detects concurrent mutation; paths are deliberately not
    part of identity so moving an entire run directory is supported.
    """
    before = path.stat()
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(8 << 20), b""):
            h.update(block)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError(f"input changed while reading: {path}")
    return {"sha256": h.hexdigest(), "size": after.st_size}


@contextlib.contextmanager
def writer_lock(path: Path):
    """Advisory process lock; kernel releases it even after a crash."""
    import fcntl

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(f"another writer holds {path}") from exc
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


#: Subpackages that render results rather than produce them. Excluded from the
#: identity digest so a stylesheet edit cannot invalidate a stage that is
#: verifying at that second -- which cost two recomputations on 2026-09-07 and
#: has kept four checkouts read-only for the length of every batch since
#: (eca-rsi#10). A directory, not a file list: the rule stays one line, and
#: anything added to it is presentation by construction.

"""Atomic records, content identities and process locks for the front pipeline."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path


def read_json(path: Path) -> dict:
    return json.loads(path.read_text())


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
        os.replace(tmp, path)
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


def runtime_identity() -> dict:
    """No kernel imports: also runs in OSP_PYTHON before any analysis."""
    import importlib.metadata
    import importlib.util
    import subprocess
    import sys

    result = {"python": sys.version, "executable": str(Path(sys.executable).resolve()), "packages": {}}
    for module, dist in (("ecarsi", "ecarsi"), ("osp", "osp-sc"), ("harness_bridge", "agent-harness-bridge")):
        spec = importlib.util.find_spec(module)
        if spec is None or spec.origin is None:
            raise RuntimeError(f"{module} is not installed in {sys.executable}")
        folder = Path(spec.origin).parent
        files = sorted(p for p in folder.rglob("*") if p.suffix in (".py", ".md", ".json") and "__pycache__" not in p.parts)
        source = digest({str(p.relative_to(folder)): file_identity(p) for p in files})
        git = subprocess.run(["git", "-C", str(folder), "rev-parse", "HEAD"], capture_output=True, text=True)
        result["packages"][module] = {
            "version": importlib.metadata.version(dist), "path": str(folder.resolve()),
            "commit": git.stdout.strip() if git.returncode == 0 else None, "source_sha256": source,
        }
    return result

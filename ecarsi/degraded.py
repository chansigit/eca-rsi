"""Degraded results (decision 0013): a step that fails without failing the run leaves a record.

A missing report or a stale display zone is not worth an analysis, so these steps never fail a run.
But a failure that is only a line in a log goes unseen: the zoom-in lineage reports did not draw for
two weeks (#26). Every such step therefore calls `note` in its `except` block. Stage programs keep
the notes in their result (`final.json` key `degraded`); the control plane copies them, and its own,
to one file each under `<run root>/degraded/`. Release lists them in needs_review (category
`degraded`); Periscope marks the run. With ECARSI_STRICT=1 (the test suite) `note` re-raises: a
degradation there is a bug.
"""
import json
import os
import time
import uuid
from pathlib import Path

from .layout import UNITS

STRICT = "ECARSI_STRICT"
DIRECTORY = "degraded"


def note(what: str, exc: BaseException) -> dict:
    """Call from an `except` block: the record to keep, or, in strict mode, the exception itself."""
    if os.environ.get(STRICT) == "1":
        raise exc
    record = {"what": what, "error": f"{type(exc).__name__}: {exc}"[:500], "at": round(time.time()),
              "id": uuid.uuid4().hex[:12]}
    print(f"[degraded] {what}: {record['error']}", flush=True)
    return record


def save(where, records, **context) -> None:
    """Keep `records` with their run: one file each under <run>/degraded/, named by the record, so
    concurrent writers never touch the same file and a copy of a record never doubles it. `where` is
    the run directory (or its display zone) or any directory inside one of its units."""
    where = Path(where)
    root, unit = where, {}
    for parent in (where, *where.parents):
        if parent.parent.name == UNITS:
            root, unit = parent.parent.parent, {"unit": parent.name}
            break
    for record in records:
        folder = root / DIRECTORY
        folder.mkdir(mode=0o700, exist_ok=True)
        path = folder / f"{record['at']}-{record['id']}.json"
        path.write_text(json.dumps({**record, **unit, **context}, sort_keys=True))


def read(root) -> list[dict]:
    folder = Path(root) / DIRECTORY
    return [json.loads(p.read_text()) for p in sorted(folder.glob("*.json"))] if folder.is_dir() else []

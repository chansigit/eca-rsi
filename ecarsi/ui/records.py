"""What the workers published about the work: finished tasks from their journals, running tasks from
their own active index.

This replaces reading the request tree. The monitor used to answer "what ran in the last hour" by
stat'ing every request folder -- 142,258 of them, 83 seconds on a cold process, against the same
Lustre metadata server the coordinators poll, and growing with every batch. It had to: a request
folder is named after a hash, so the only way to find the recent ones is to ask each one how old it
is. Reading 142,000 records to find 2,000 is not a slow implementation of a good idea; it is the
cost of deriving a fact nobody published.

So the owners publish instead. Each worker appends one line per finished attempt to its own
`workers/<id>/tasks-<day>.jsonl` (warm_pool/worker.journal), and marks what it is running now in
`cache/active/<host>/` (warm_pool/worker.mark_active). One file per worker per day means "the last
hour" is a seek to the end of five files, and the twelve thousandth batch costs exactly what the
first one did.
"""
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..warm_pool.state import read


def journal_paths(pool: Path, since: float, until: float):
    """Only the UTC day partitions the window touches."""
    day = datetime.fromtimestamp(since, timezone.utc).date()
    last = datetime.fromtimestamp(until, timezone.utc).date()
    while day <= last:
        yield from (pool / "workers").glob(f"*/tasks-{day.isoformat()}.jsonl")
        day += timedelta(days=1)


def journal_rows(pool: Path, since: float, until: float, cache: dict | None = None) -> list[dict]:
    """Finished attempts in the window, read forward from where the last read stopped.

    Append-only, so a re-read only ever covers what was appended since; a file that shrank was
    rotated or replaced and is read again from the start. A partial final line is left for the next
    pass -- the writer appends whole lines, so a short read means the write is still in flight."""
    cache = cache if cache is not None else {}
    rows = []
    for path in journal_paths(pool, since, until):
        try:
            size = path.stat().st_size
        except OSError:
            continue
        offset, saved = cache.get(path, (0, []))
        if size < offset:
            offset, saved = 0, []
        if size != offset:
            try:
                with path.open("rb") as stream:
                    stream.seek(offset)
                    while True:
                        position = stream.tell()
                        line = stream.readline()
                        if not line or not line.endswith(b"\n"):
                            break
                        try:
                            value = json.loads(line)
                        except ValueError:
                            continue      # one corrupt line never costs the rest of the file
                        if isinstance(value, dict) and value.get("request_id"):
                            saved.append(value)
                cache[path] = (position, saved)
            except OSError:
                continue
        rows.extend(row for row in saved
                    if (row.get("submitted_at") or 0) <= until
                    and (row.get("finished_at") or row.get("started_at") or 0) >= since)
    return rows


def active_rows(pool: Path) -> list[dict]:
    """Attempts a worker says it is running: one marker file per request, written on acceptance.

    Bounded by how much work is in flight -- tens, not tens of thousands -- so reading each one's
    request and acceptance is cheap, and it is the only place a running task exists at all: a task
    has no journal line until it finishes."""
    rows = []
    index = pool / "cache" / "active"
    try:
        hosts = [h for h in os.scandir(index) if h.is_dir()]
    except OSError:
        return rows
    for host in hosts:
        for marker in os.scandir(host.path):
            if marker.name.startswith("."):
                continue
            folder = pool / "requests" / marker.name
            request = read(folder / "request.json")
            if not request:
                continue
            attempt = folder / request["attempt_id"]
            if read(attempt / "receipt.json"):
                continue          # finished between the marker and this read; the journal has it
            accepted = read(attempt / "accepted.json", {})
            spec = request.get("spec", {})
            rows.append({
                "request_id": spec.get("request_id", marker.name), "attempt_id": request["attempt_id"],
                "operation": spec.get("operation_id"), "state": "running",
                "worker_id": accepted.get("worker_id"), "host": accepted.get("host") or host.name,
                "submitted_at": request.get("submitted_at"), "started_at": accepted.get("started_at"),
                "finished_at": None, "cpus": spec.get("cpus"), "memory_mb": spec.get("memory_mb"),
                "cpu_ids": accepted.get("cpu_ids"), "gpu_ids": accepted.get("gpu_ids", []),
                "compute_backend": accepted.get("compute_backend"),
                **{key: (spec.get("trace") or {}).get(key)
                   for key in ("dataset_id", "workflow_id", "unit_id", "sample_id")},
            })
    return rows


def tasks(pool: Path, since: float, until: float, cache: dict | None = None) -> list[dict]:
    """Every task the window covers: finished ones from the journals, running ones from the active
    index. A task that finishes between the two reads appears once -- active_rows drops anything
    that already has a receipt, and the journal line is written before the marker is cleared."""
    rows = journal_rows(pool, since, until, cache)
    seen = {(row["request_id"], row.get("attempt_id")) for row in rows}
    rows += [row for row in active_rows(pool)
             if (row["request_id"], row["attempt_id"]) not in seen
             and (row.get("submitted_at") or 0) <= until]
    return rows


def as_timeline_row(row: dict) -> dict:
    """A published record in the shape the timeline draws.

    `service` is what kind of work it was, not which service held the request: a model turn executes
    on a pool worker (`bridge/summary.json` says `execution: pool`), and a worker timeline that hid
    that would be drawing an org chart rather than the machines."""
    operation = row.get("operation") or ""
    trace = {"workflow_id": row.get("workflow_id") or "", "unit_id": row.get("unit_id") or operation,
             "dataset_id": row.get("dataset_id") or "Unattributed"}
    if row.get("sample_id"):
        trace["sample_id"] = row["sample_id"]
    return {
        "id": row["request_id"], "operation": operation, "state": row.get("state"),
        "submitted_at": row.get("submitted_at") or row.get("started_at"),
        "started_at": row.get("started_at"), "finished_at": row.get("finished_at"),
        "host": row.get("host"), "worker_id": row.get("worker_id"),
        "cpu_ids": row.get("cpu_ids"), "cpus": row.get("cpus"), "memory_mb": row.get("memory_mb"),
        "gpu_ids": row.get("gpu_ids") or [], "compute_backend": row.get("compute_backend"),
        "peak_rss_bytes": row.get("peak_rss_bytes"), "error": row.get("error"),
        "service": "bridge" if operation.startswith("agent.") else "pool",
        "trace": trace, "trace_source": "explicit" if row.get("workflow_id") else "unattributed",
    }

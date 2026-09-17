"""Persistent dataset admission on user-provisioned Slurm nodes.

The queue is a shared file, protected by flock and atomic replacement. Node
agents run on the host; each dataset owns an independent container supervisor.
Compute workers retain their separate resource budgets and pool scheduler.
"""
from __future__ import annotations

import argparse
import contextlib
import errno
from functools import lru_cache
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import uuid

from .run_state import write_json, writer_lock
from .pool.slurm import process_identity, group_alive

ACTIVE = {"assigned", "running"}
TERMINAL = {"completed", "failed", "paused", "prepared"}
DEFAULT_CONFIG = Path.home() / ".config/ecarsi/batch.json"


def launch_failure(exc):
    return "launch_transient" if isinstance(exc, OSError) and exc.errno in {
        errno.EAGAIN, errno.ENOMEM, errno.EMFILE, errno.ENFILE,
    } else "launch_failed"


def input_resources(source):
    """Size the local workflow, not the remote compute worker, from H5AD metadata.

    Eight count-matrix copies cover merge/reset/annotation; three float64 HVG
    matrices cover local scaling/PCA. This is an admission estimate, not a
    guarantee: Slurm still enforces the per-attempt limit.
    """
    import h5py
    from .upstream import discover
    units, extra = discover(Path(source))
    if not units or extra:
        raise ValueError("resource profiling needs declared ECA-PP standardized inputs")
    cells = matrix_bytes = hvg_bytes = 0
    files = []
    for unit in units:
        path = Path(unit["h5ad"])
        before = path.stat()
        with h5py.File(path, "r") as f:
            matrix = f["layers/counts"]
            if isinstance(matrix, h5py.Dataset):
                shape = matrix.shape
                size = matrix.size * matrix.dtype.itemsize
            else:
                shape = matrix.attrs["shape"]
                size = sum(matrix[k].size * matrix[k].dtype.itemsize for k in ("data", "indices", "indptr"))
            cells += int(shape[0])
            matrix_bytes += int(size)
            hvg_bytes += int(shape[0]) * min(2000, int(shape[1])) * 8
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError(f"input changed during resource profiling: {path}")
        files.append(dict(path=str(path), size=after.st_size, mtime_ns=after.st_mtime_ns))
    memory = max(4, 4 * math.ceil((2 * 2**30 + 8 * matrix_bytes + 3 * hvg_bytes) / (4 * 2**30)))
    return dict(cpus=1 if cells <= 25000 else 2, memory_gb=memory,
                resource_profile=dict(version=1, n_cells=cells, counts_bytes=matrix_bytes,
                                      dense_hvg_bytes=hvg_bytes, inputs=files))


def configuration(path):
    c = json.loads(Path(path).read_text())
    for key in ("directory", "python", "scheduler"):
        if not isinstance(c.get(key), str) or not c[key]:
            raise ValueError(f"configuration needs {key}")
    if not Path(c["directory"]).is_absolute() or not Path(c["python"]).is_absolute():
        raise ValueError("directory and python must be absolute paths")
    env = c.setdefault("env", {})
    if not isinstance(env, dict) or not all(isinstance(v, str) for v in env.values()):
        raise ValueError("env must map names to strings")
    if any(any(s in k.upper() for s in ("KEY", "TOKEN", "SECRET", "PASSWORD")) for k in env):
        raise ValueError("keep credentials in the node environment, not this configuration")
    for key in ("pause_dispatch", "driver_memory_lending"):
        if key in c and type(c[key]) is not bool:
            raise ValueError(f"{key} must be boolean")
    c.setdefault("max_cpu_percent", 90)
    if not 0 < c["max_cpu_percent"] <= 100:
        raise ValueError("max_cpu_percent must be in (0, 100]")
    slots = c.setdefault("driver_cpu_slots_per_core", 1)
    if type(slots) is not int or not 1 <= slots <= 8:
        raise ValueError("driver_cpu_slots_per_core must be an integer from 1 to 8")
    margin = c.get('driver_model_memory_margin_gb', 2)
    if type(margin) not in (int, float) or not math.isfinite(margin) or not .5 <= margin <= 8:
        raise ValueError('driver_model_memory_margin_gb must be between 0.5 and 8')
    if c.get('preparation_module'):
        if not isinstance(c['preparation_module'], str) or not re.fullmatch(r'[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+', c['preparation_module']):
            raise ValueError('preparation_module must be a Python module name')
        for key, default in [('preparation_memory_gb', 4), ('preparation_max_datasets', 8), ('preparation_samples', 4)]:
            value = c.setdefault(key, default)
            if type(value) is not int or value <= 0:
                raise ValueError(f'{key} must be a positive integer')
    if type(c.get('preparation_continuation', False)) is not bool:
        raise ValueError('preparation_continuation must be boolean')
    slots = c.setdefault('preparation_backfill_slots', 0)
    if type(slots) is not int or slots < 0:
        raise ValueError('preparation_backfill_slots must be a nonnegative integer')
    if c.get('osp_dispatch_module') is not None and (not isinstance(c['osp_dispatch_module'], str)
            or not re.fullmatch(r'[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+', c['osp_dispatch_module'])):
        raise ValueError('osp_dispatch_module must be a Python module name')
    c.setdefault("max_attempts", 1)
    if type(c["max_attempts"]) is not int or not 1 <= c["max_attempts"] <= 5:
        raise ValueError("max_attempts must be an integer from 1 to 5")
    if c.get("resource_policy", "explicit") not in {"explicit", "input"}:
        raise ValueError("resource_policy must be explicit or input")
    if "min_attempt_hours" in c and (type(c["min_attempt_hours"]) not in (int, float)
            or not math.isfinite(c["min_attempt_hours"]) or c["min_attempt_hours"] <= 0):
        raise ValueError("min_attempt_hours must be positive and finite")
    if not isinstance(c.get("required_env", []), list) or not all(
            isinstance(v, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", v) for v in c.get("required_env", [])):
        raise ValueError("required_env must list environment variable names")
    return c


@contextlib.contextmanager
def queue(directory, *, write=True):
    import fcntl
    root = Path(directory)
    if not write:
        # status.json is atomically replaced; readers need neither the writer
        # lock nor a publication of a snapshot they did not change.
        yield json.loads((root/'status.json').read_text())
        return
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".queue.lock").open("a") as lock:
        waiting_since = time.monotonic()
        fcntl.flock(lock, fcntl.LOCK_EX)
        acquired_at = time.monotonic()
        p = root / "status.json"
        state = json.loads(p.read_text()) if p.exists() else {"schema_version": 1, "datasets": [], "nodes": {}}
        try:
            yield state
            state["updated_at"] = time.time()
            write_json(p, state)
        finally:
            if acquired_at - waiting_since > 5 or time.monotonic() - acquired_at > 5:
                print(f"[batch] queue lock wait={acquired_at-waiting_since:.1f}s hold={time.monotonic()-acquired_at:.1f}s", flush=True)


def submit(config, rows):
    """Validate the entire submission before publishing; duplicate outputs are idempotent."""
    prepared = []
    for row in rows:
        r = {**config.get("defaults", {}), **row}
        if config.get("resource_policy") == "input" and not any(k in row for k in ("cpus", "memory_gb")):
            r.update(input_resources(r["input"]))
        for key in ("input", "output", "mirror"):
            if key == "mirror" and not r.get(key):
                continue
            r[key] = str(Path(r[key]).expanduser().resolve())
        if not Path(r["input"]).is_dir():
            raise ValueError(f"input directory missing: {r['input']}")
        if r["input"] == r["output"] or r.get("mirror") == r["output"]:
            raise ValueError("input, working output and mirror must be distinct")
        for key, default in (("cpus", 2), ("memory_gb", 8), ("hours", 6)):
            r.setdefault(key, default)
            if type(r[key]) not in (int, float) or not math.isfinite(r[key]) or r[key] <= 0:
                raise ValueError(f"invalid {key}")
        if type(r["cpus"]) is not int:
            raise ValueError("cpus must be an integer")
        r.setdefault("name", Path(r["input"]).name)
        r.setdefault("log", r["output"] + ".log")
        r["log"] = str(Path(r["log"]).resolve())
        r.update(id=uuid.uuid4().hex, state="queued", submitted_at=time.time(), attempt=0)
        prepared.append(r)
    with queue(config["directory"]) as state:
        existing = {r["output"]: r for r in state["datasets"]}
        result = []
        for r in prepared:
            old = existing.get(r["output"])
            if old and (old["input"] != r["input"] or old.get("mirror") != r.get("mirror")):
                raise ValueError(f"output already belongs to a different submission: {r['output']}")
            if not old:
                state["datasets"].append(r)
                existing[r["output"]] = r
            result.append(old or r)
    return result


def receipt_path(directory, row):
    return Path(directory) / "receipts" / f"{row['id']}-{row['attempt']}.json"


def receipt_observations(state, directory, owners=None):
    """Read slow shared-storage receipts without holding the fleet queue lock."""
    observations = {}
    for row in state["datasets"]:
        if owners is not None and row.get("node") not in owners:
            continue
        if row["state"] not in ACTIVE | {"failed", "paused"} and not row.get("reservation_held"):
            continue
        try:
            receipt = json.loads(receipt_path(directory, row).read_text())
        except FileNotFoundError:
            receipt = None
        usage = receipt_path(directory, row).with_suffix(".usage.json")
        metrics = json.loads(usage.read_text()) if usage.exists() else None
        observations[(row['id'], row['attempt'])] = receipt, metrics
    return observations


def reconcile(state, directory, observations=None):
    if observations is None:
        observations = receipt_observations(state, directory)
    for row in state["datasets"]:
        if row["state"] not in ACTIVE | {"failed", "paused"} and not row.get("reservation_held"):
            continue
        receipt, metrics = observations.get((row['id'], row['attempt']), (None, None))
        if receipt is None:
            continue
        if receipt.get("node") == row.get("node"):
            if receipt.get("state") in TERMINAL and receipt.get("finished_at") is None:
                row["reservation_held"] = True
                continue  # cleanup in progress is not a confirmed termination
            if row.get("reservation_held") and receipt.get("finished_at") is None:
                continue
            row.update({k: receipt[k] for k in ("state", "pid", "child_pid", "driver_pid", "rss_bytes", "cpu_percent",
                       "updated_at", "started_at", "finished_at", "exit_code", "reason", "termination_reason") if k in receipt})
            if receipt.get("finished_at") is not None and receipt["state"] in TERMINAL:
                row.pop("reservation_held", None)
                if "reason" not in receipt:
                    row.pop("reason", None)
                if receipt['state'] == 'prepared':
                    row.update(state='queued', preparation_complete=True,
                               preparation_attempts=row.get('preparation_attempts', 0)+1)
                    for key in ('node', 'cpu_ids', 'execution', 'launch_requested', 'pid', 'child_pid', 'driver_pid',
                                'rss_bytes', 'cpu_percent', 'admission_memory_gb', 'admission_phase'):
                        row.pop(key, None)
                    continue
        if metrics and time.time() - metrics["observed_at"] < 15:
            row.update({k: metrics[k] for k in ("rss_bytes", "cpu_percent", "driver_pid")})


def capacity(node, rows, now):
    owners = {node["id"], *node.get("predecessors", [])}
    running = [r for r in rows if r.get("node") in owners and (r["state"] in ACTIVE or r.get("reservation_held"))]
    # The node owns these CPU locks; children inherit the same file descriptions.
    # Limited sharing lets an I/O/model wait coexist with another driver. Linux
    # still confines all children to this driver's slice, separate from workers.
    # Never borrow a CPU whose lock is held only by an older/unknown supervisor.
    slots = node.get("driver_cpu_slots_per_core", 1)
    uses = {cpu: sum(cpu in r["cpu_ids"] for r in running) for cpu in node["cpu_ids"]}
    free_cpus = sorted((cpu for cpu in node.get("available_cpu_ids", node["cpu_ids"])
                        if uses[cpu] < slots), key=lambda cpu: (uses[cpu], cpu))
    from .driver_budget import reserved as memory_reserved
    reserved = sum(max(memory_reserved(r), r.get("rss_bytes", 0)) for r in running)
    slack = sum(max(0, memory_reserved(r) - r.get("rss_bytes", 0)) for r in running)
    free_memory = min(node["memory"] - reserved, node.get("memory_headroom", 0) - slack)
    busy = sum(r.get("cpu_percent", 0) for r in running) / node["cpus"]
    return free_cpus, free_memory, busy


def assign(state, config, now=None):
    """FIFO with fitting jobs allowed past a blocked one; spread across free nodes."""
    now = time.time() if now is None else now
    from .driver_budget import settle
    settle(state, config, now)
    for row in state["datasets"]:
        node = state["nodes"].get(row.get("node"), {})
        if row["state"] == "assigned" and not row.get("launch_requested") and (node.get("draining") or now - node.get("observed_at", 0) > 60):
            row["state"] = "queued"  # no launch was claimed; reassignment is safe under the queue lock
            for key in ("node", "cpu_ids", "execution"):
                row.pop(key, None)
    # ponytail: O(datasets * nodes * active datasets); index reservations if fleet size warrants it.
    for row in state["datasets"]:
        if row["state"] != "queued":
            continue
        offer = state.get('preparation_offers', {}).get(row.get('id'), {})
        continuing = bool(config.get('preparation_continuation') and row.get('preparation_complete')
                          and row.get('queue_reason') == 'Waiting for driver CPU/memory capacity'
                          and now - state.get('preparation_offers_at', 0) <= 60
                          and offer.get('remaining_count', 0) > 0
                          and offer.get('prepared_count', 0) < config.get('preparation_samples', 4)
                          and preparation_backfill_fits(state, config, row, now,
                                                       cells=offer.get('min_missing_cells')))
        preparing = continuing or bool(config.get('preparation_module') and not row.get('preparation_complete')
                         and (row['attempt'] == 0 or row.get('admission_phase') == 'preparation'))
        backlog = sum(r.get('admission_phase') == 'preparation' and r['state'] in ACTIVE or
                      r.get('preparation_complete', False) and r['state'] == 'queued' for r in state['datasets'])
        backfill = continuing
        if preparing and not continuing:
            if backlog >= config.get('preparation_max_datasets', 8):
                backfill = preparation_backfill_fits(state, config, row, now,
                    cells=row.get('preparation_limits', {}).get('preparation_max_sample_cells'))
                if not backfill:
                    row['queue_reason'] = 'Prepared OSP backlog is full'
                    continue
        # Preparation offloads expression work, but its metadata/Dask process
        # still grows with large inputs. Small inputs keep the configured floor.
        profile = row.get('resource_profile', {})
        cells = profile.get('n_cells', row.get('n_cells', 0))
        counts_bytes = profile.get('counts_bytes', 0)
        counts_bytes = counts_bytes if type(counts_bytes) is int and counts_bytes > 0 else 0
        preparation_memory = max(config.get('preparation_memory_gb', 4),
                                 math.ceil(1 + cells*1024/2**30),
                                 2 + math.ceil(max(0, counts_bytes - .75*2**30)/2**30),
                                 row.get('preparation_memory_floor_gb', 0))
        needed_memory = min(row['memory_gb'], preparation_memory) if preparing else row['memory_gb']
        needed_cpus = 1 if preparing else row['cpus']
        if row.get("retry_after", 0) > now:
            row["queue_reason"] = "Checkpoint retry backoff"
            continue
        choices, reasons = [], set()
        for key, node in state["nodes"].items():
            if node.get('role') == 'preparation' and not preparing:
                # When compute-ahead is full, a sizeable preparation slice can
                # finish queued drivers; the small control slice stays prep-only.
                if node['memory'] < 16 * 2**30 or backlog < config.get('preparation_max_datasets', 8):
                    continue
            node = {**node, "driver_cpu_slots_per_core": config.get("driver_cpu_slots_per_core", 1)}
            if node.get("draining") or now - node["observed_at"] > 60:
                reasons.add("Node offline or draining")
                continue
            admission_hours = min(row["hours"], config.get("min_attempt_hours", row["hours"]))
            if now + admission_hours * 3600 + 60 > node["end_time"]:
                reasons.add("Insufficient Slurm time remaining")
                continue
            cpus, memory, busy = capacity(node, state["datasets"], now)
            from .driver_budget import expansion_room
            memory -= expansion_room(node, state["datasets"])
            if len(cpus) < needed_cpus or memory < needed_memory * 2**30:
                reasons.add("Waiting for driver CPU/memory capacity")
                continue
            if busy >= config["max_cpu_percent"]:
                reasons.add("Node CPU usage is high")
                continue
            score = max(1 - len(cpus) / node["cpus"], 1 - memory / node["memory"], busy / 100)
            choices.append((score, key, cpus))
        if config.get("pause_dispatch"):
            row["queue_reason"] = "Dataset dispatch is paused"
        elif choices:
            _, node, cpus = min(choices)
            continuation_capacity = preparation_backfill_capacity(state, config, row, now,
                cells=offer['min_missing_cells']) if continuing else 0
            row.update(state="assigned", node=node, cpu_ids=cpus[:needed_cpus],
                       preparation_backfill=backfill,
                       admission_phase='preparation' if preparing else 'driver', admission_memory_gb=needed_memory,
                       driver_cpu_slots_per_core=config.get("driver_cpu_slots_per_core", 1),
                       memory_lending_protocol=int(config.get("driver_memory_lending", False)),
                       reserved_memory_bytes=needed_memory*2**30, memory_state="active",
                       memory_grant_token=None, assigned_at=now, attempt=row["attempt"] + 1,
                       execution={k: config[k] for k in ("python", "scheduler", "env", "required_env", "osp_dispatch_module", "driver_memory_lending", "preparation_module", "preparation_samples") if k in config})
            if continuing:
                row['preparation_complete'] = False
                row['execution']['preparation_samples'] = min(config.get('preparation_samples', 4),
                    config.get('preparation_samples', 4) - offer['prepared_count'])
                row['preparation_limits'] = dict(
                    preparation_max_prepared_samples=config.get('preparation_samples', 4),
                    preparation_max_sample_cells=continuation_capacity)
            if preparing:
                row['execution'].update(row.get('preparation_limits', {}))
            row.pop("queue_reason", None)
        else:
            # Historical/drained node records do not explain why a live node
            # cannot admit this row. Keep offline only when no live node exists.
            if len(reasons) > 1:
                reasons.discard("Node offline or draining")
            row["queue_reason"] = "; ".join(sorted(reasons)) or "No dataset execution nodes registered"


def preparation_backfill_fits(state, config, row, now, *, cells=None):
    return preparation_backfill_capacity(state, config, row, now, cells=cells) > 0


def preparation_backfill_capacity(state, config, row, now, *, cells=None):
    """Bounded extra preparation when ordinary backlog blocks fitting work."""
    limit = config.get('preparation_backfill_slots', 0)
    # Continuation reuses a dataset already counted in the prepared backlog.
    # Its completed cache is bounded per dataset; only active preparation uses
    # a continuation slot. Fresh datasets retain the extra-backlog bound.
    occupied = sum(r.get('preparation_backfill', False) and
                   (r['state'] in ACTIVE or cells is None and
                    bool(r.get('preparation_complete')) and r['state'] == 'queued')
                   for r in state['datasets'])
    offer = state.get('pool_capacity', {})
    if occupied >= limit or now - offer.get('observed_at', 0) > 30:
        return False
    if cells is None:
        cells = row.get('resource_profile', {}).get('n_cells', 0)
    if type(cells) is not int or cells <= 0:
        return False  # unknown samples cannot justify a smaller request
    env = config.get('env', {})
    # Before sample confirmation, assume the entire dataset is one sample.
    memory = max(row['memory_gb'] * 2**30,
                 2**30 + cells * float(env.get('PERSAMPLE_MEM_PER_CELL_MB', '.5')) * 2**20)
    cpus = int(env.get('ECA_POOL_TASK_CPUS', env.get('OSP_POOL_TASK_CPUS', '1')))
    per_cell = float(env.get('PERSAMPLE_MEM_PER_CELL_MB', '.5')) * 2**20
    return max((int(min((w['free_memory']-2**30)/per_cell, (w['end_time']-now-60)*20))
                for w in offer.get('workers', []) if w['free_cpus'] >= cpus
                and w['free_memory'] >= memory and w['end_time'] > now + cells/20 + 60), default=0)


def pool_capacity(scheduler):
    """Read admission capacity outside the dataset queue lock."""
    from .pool.client import connect
    from .pool.scheduler import dispatch
    with connect(scheduler, timeout=3, set_as_default=False) as client:
        snapshot = client.run_on_scheduler(dispatch, 'status')
    now = time.time()
    workers = []
    for address, worker in snapshot['workers'].items():
        if (worker.get('draining') or worker.get('recycle_reason') or worker.get('gpus')
                or now - worker['observed_at'] > 30):
            continue
        active = [t for t in snapshot['tasks'].values() if t.get('worker') == address
                  and t['state'] in {'granted', 'running', 'stopping'}]
        if len(active) >= worker.get('task_slots', 1):
            continue
        workers.append(dict(address=address, end_time=worker['end_time'],
            free_cpus=max(0, worker['cpus']-sum(t['cpus'] for t in active)),
            free_memory=max(0, worker['memory']-max(worker.get('rss_bytes', 0),
                                                  sum(t['memory'] for t in active)))))
    return dict(observed_at=now, workers=workers)


def _handoff_request(path, row):
    try:
        request = json.loads(path.with_suffix(".handoff.json").read_text())
        requested_at = request.get("requested_at")
        if (request.get("id") == row["id"] and request.get("attempt") == row["attempt"]
                and type(requested_at) in (int, float) and math.isfinite(requested_at)
                and requested_at > 0):
            return request
    except (OSError, ValueError, AttributeError):
        pass
    return None


def retry_finished(state, config, now=None):
    """Bounded checkpoint recovery, only after a matching terminal receipt.

    A model/data rejection remains a rejection. Explicit pauses and uncertain
    executions are never restarted by the controller.
    """
    now = time.time() if now is None else now
    for row in state["datasets"]:
        if row["state"] == "retry_wait":
            if row["retry_after"] <= now:
                row["state"] = "queued"
                row.pop("retry_after")
            continue
        if row["state"] not in {"failed", "paused"} or row.get("reservation_held"):
            continue
        try:
            receipt = json.loads(receipt_path(config["directory"], row).read_text())
        except (OSError, ValueError):
            continue
        if (receipt.get("node") != row.get("node") or not receipt.get("finished_at")
                or receipt.get("state") not in {"failed", "paused"}):
            continue
        unstarted = receipt.get("termination_reason") == "launch_interrupted"
        allocation_end = receipt.get("termination_reason") == "allocation_end"
        request = _handoff_request(receipt_path(config["directory"], row), row)
        handoff = bool(request and receipt["state"] == "paused"
                       and type(receipt["finished_at"]) in (int, float)
                       and receipt["finished_at"] >= request["requested_at"]
                       and ((receipt.get("exit_code") == 3 and not receipt.get("reason")) or
                            (receipt.get("termination_reason") == "execution_lost" and
                             receipt.get("reason") == "Previous supervisor stopped; execution confirmed absent") or
                            (request.get("interrupt") is True and
                             (receipt.get("termination_reason") == "deployment_handoff" or
                              (receipt.get("termination_reason") == "user_stop" and
                               receipt.get("reason") == "Driver stop requested")))))
        try:
            controls = [json.loads(p.read_text()) for p in Path(row["output"]).glob("units/*/loop_control.json")]
        except (OSError, ValueError):
            continue  # an unreadable user control is not permission to resume
        if any(not isinstance(c, dict) or c.get("pause") or c.get("pause_after_stage") or c.get("stop_after_round") for c in controls):
            continue
        oom = (row.get('admission_phase') == 'preparation' and receipt['state'] == 'failed'
               and receipt.get('exit_code') in {1, 137} and
               (receipt.get('termination_reason') == 'out_of_memory' or attempt_out_of_memory(row)))
        next_prep_memory = min(row['memory_gb'], max(row.get('preparation_memory_floor_gb', 0),
                                2 * row.get('admission_memory_gb', config.get('preparation_memory_gb', 4)))) if oom else 0
        oom = bool(oom and next_prep_memory > row.get('admission_memory_gb', 0))
        if not (unstarted or handoff or allocation_end or oom) and row["attempt"] - row.get("unstarted_attempts", 0) - row.get("handoff_restarts", 0) - row.get("allocation_restarts", 0) - row.get('preparation_attempts', 0) >= config.get("max_attempts", 1):
            continue
        lost = oom or handoff or receipt.get("termination_reason") in {"execution_lost", "launch_interrupted", "launch_transient", "allocation_end"}
        if not lost and receipt["state"] == "paused" and receipt.get("reason") != "Driver time budget or Slurm allocation ending":
            continue
        if not lost and receipt.get("exit_code") != 1 and not (receipt["state"] == "paused" and receipt.get("exit_code") in {3, 137}):
            continue
        # OSP already distinguishes data/contract errors from transient failures.
        failures = []
        for path in Path(row["output"]).glob("units/*/persample/*/run_state.json"):
            try:
                value = json.loads(path.read_text())
                if value.get("state") == "failed":
                    failures.append(value)
            except (OSError, ValueError):
                continue
        if not unstarted and any(f.get("retryable") is False for f in failures):
            continue
        complete_samples = False
        for path in Path(row["output"]).glob("units/*/persample/manifest.json"):
            try:
                complete_samples |= json.loads(path.read_text()).get("state") == "complete"
            except (OSError, ValueError):
                continue
        if not lost and not complete_samples and not any(f.get("retryable") is True for f in failures):
            continue  # input planning/grouping failures require investigation, not a new model guess
        if not lost and receipt["state"] != "paused" and not any(f.get("retryable") is True for f in failures):
            try:
                with Path(row["log"]).open("rb") as log:
                    log.seek(max(0, log.seek(0, 2) - 65536))
                    tail = log.read().decode(errors="replace")
            except (KeyError, OSError):
                continue
            # Legacy drivers did not publish a typed downstream failure. Use
            # only the last unhandled exception, not an earlier recovered one.
            exceptions = re.findall(r"^[\w.]*(?:Error|Exception):[^\n]*", tail, re.M)
            recoverable = {
                "ConnectionError: pool task lost: worker disconnected",
                "ConnectionError: pool task lost: execution deadline exceeded",
                "ConnectionError: pool execution retired: execution deadline exceeded",
                "ConnectionError: pool restarted or request expired; resume through the driver",
                # Older clients report `done` when execution finished but the
                # worker disappeared before the pinned result was retrieved.
                "ConnectionError: pool task done:",
            }
            if not exceptions or exceptions[-1].strip() not in recoverable:
                continue
        if handoff:
            row["handoff_restarts"] = row.get("handoff_restarts", 0) + 1
        if allocation_end:
            row["allocation_restarts"] = row.get("allocation_restarts", 0) + 1
        if unstarted:
            row["unstarted_attempts"] = row.get("unstarted_attempts", 0) + 1
        if oom:
            row['preparation_memory_floor_gb'] = next_prep_memory
            row['termination_reason'] = 'out_of_memory'
        row.setdefault("attempt_history", []).append({k: row.get(k) for k in
            ("attempt", "node", "state", "exit_code", "reason", "termination_reason", "started_at", "finished_at")})
        # A separate state also fences old node agents that do not know backoff.
        row.update(state="retry_wait", retry_after=now + (0 if handoff else 60 * row["attempt"]),
                   queue_reason="Checkpoint retry backoff",
                   recovery_reason=(f"Preparation exceeded Slurm memory; retry with {next_prep_memory} GiB"
                                    if oom else "Previous driver stopped; bounded retry through validated checkpoints"))
        for key in ("node", "cpu_ids", "execution", "launch_requested", "reason", "termination_reason", "exit_code", "finished_at", "started_at", "assigned_at", "pid", "child_pid", "driver_pid", "rss_bytes", "cpu_percent", "supervisor_protocol", "recovery_status"):
            row.pop(key, None)


def recover_ended_allocations(directory):
    """Retire lost attempts only after Slurm completion and exclusive output locks.

    Missing heartbeats and failed Slurm queries are never evidence of death.
    Runs outside the queue transaction so accounting cannot block admission.
    """
    now = time.time()
    with queue(directory, write=False) as state:
        nodes = state['nodes'].copy()
        rows = [r.copy() for r in state['datasets'] if
                (r['state'] in ACTIVE or r.get('reservation_held')) and r.get('launch_requested')]
    rows = [r for r in rows if (n := nodes.get(r.get('node'), {})).get('job_id')
            and (now - n.get('observed_at', 0) > 90 or now >= n['end_time'])]
    jobs = {str(nodes[r['node']]['job_id']) for r in rows}
    if not jobs:
        return
    if any(not j.isdigit() for j in jobs):
        raise ValueError('invalid allocation identity')
    result = subprocess.run(['sacct', '-X', '-n', '-P', '-j', ','.join(sorted(jobs)),
                             '-o', 'JobIDRaw,State'], capture_output=True, text=True, check=True, timeout=15)
    terminal = {'COMPLETED', 'CANCELLED', 'FAILED', 'TIMEOUT', 'NODE_FAIL', 'OUT_OF_MEMORY', 'PREEMPTED', 'BOOT_FAIL', 'DEADLINE'}
    ended = set()
    timed_out = set()
    for line in result.stdout.splitlines():
        fields = line.split('|')
        if len(fields) >= 2 and fields[0] in jobs and fields[1].strip() and fields[1].split()[0].rstrip('+') in terminal:
            ended.add(fields[0])
            if fields[1].split()[0].rstrip('+') == 'TIMEOUT':
                timed_out.add(fields[0])
    if not ended:
        return
    confirmed = set()
    for job in ended:
        try:
            if not allocation_steps(job, allow_missing=True):
                confirmed.add(job)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            print(f"[batch] allocation {job} recovery deferred: {exc}", flush=True)
    ended = confirmed
    for row in rows:
        if str(nodes[row['node']]['job_id']) not in ended:
            continue
        path = receipt_path(directory, row)
        root = Path(row['output'])
        try:
            with contextlib.ExitStack() as locks:
                locks.enter_context(writer_lock(path.with_suffix('.lock')))
                record = json.loads(path.read_text()) if path.exists() else dict(node=row['node'])
                if record.get('finished_at'):
                    continue
                locks.enter_context(writer_lock(root / '.batch-driver.lock'))
                locks.enter_context(writer_lock(root / 'organize/.writer.lock'))
                for unit in root.glob('units/*'):
                    for target in [unit/'.rsi-downstream.lock', unit/'persample/.driver.lock',
                                   *unit.glob('persample/*/.writer.lock')]:
                        locks.enter_context(writer_lock(target))
                record.update(state='paused', termination_reason='allocation_end' if str(nodes[row['node']]['job_id']) in timed_out else 'execution_lost', exit_code=None,
                              reason='Slurm allocation ended; all driver writers confirmed stopped',
                              finished_at=time.time(), updated_at=time.time())
                write_json(path, record)
        except RuntimeError as exc:
            if 'another writer holds' not in str(exc):
                raise


def controller(config_path):
    """Reconcile and admit from live node offers; only node agents launch work."""
    config = configuration(config_path)
    directory = Path(config["directory"])
    checked_allocations = checked_pool = checked_preparations = 0
    with writer_lock(directory / ".controller.lock"):
        while True:
            try:
                candidate = configuration(config_path)
                if candidate["directory"] != str(directory):
                    raise ValueError("queue directory cannot change while controller is running")
                config = candidate
            except (OSError, ValueError, TypeError) as exc:
                print(f"[batch] config update rejected: {exc}", flush=True)
            try:
                preparation_offers = None
                if config.get('preparation_continuation') and time.monotonic() >= checked_preparations:
                    from .preparation_offer import offer as preparation_offer
                    with queue(directory, write=False) as snapshot:
                        candidates = [r for r in snapshot['datasets']
                                      if r['state'] == 'queued' and r.get('preparation_complete')]
                    preparation_offers = {r['id']: preparation_offer(r['output']) for r in candidates}
                    checked_preparations = time.monotonic() + 30
                offer = None
                if config.get('preparation_backfill_slots') and time.monotonic() >= checked_pool:
                    checked_pool = time.monotonic() + 15
                    try:
                        offer = pool_capacity(config['scheduler'])
                    except Exception as exc:
                        offer = dict(observed_at=time.time(), workers=[], error=str(exc))
                        print(f"[batch] pool capacity unavailable: {exc}", flush=True)
                if time.monotonic() >= checked_allocations:
                    checked_allocations = time.monotonic() + 60
                    try:
                        recover_ended_allocations(directory)
                    except (OSError, ValueError, subprocess.SubprocessError) as exc:
                        print(f"[batch] allocation recovery deferred: {exc}", flush=True)
                try:
                    with queue(directory, write=False) as snapshot:
                        observations = receipt_observations(snapshot, directory)
                except FileNotFoundError:
                    observations = {}  # first start creates the queue in the write transaction
                with queue(directory) as state:
                    if preparation_offers is not None:
                        state['preparation_offers'] = preparation_offers
                        state['preparation_offers_at'] = time.time()
                    if offer is not None:
                        state['pool_capacity'] = offer
                    reconcile(state, directory, observations)
                    if not config.get("pause_dispatch"):
                        retry_finished(state, config)
                    assign(state, config)
                    state["controller"] = dict(pid=os.getpid(), observed_at=time.time())
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                # Shared-storage outages must not kill queue maintenance. The
                # failed transaction is not published; retry from disk next tick.
                print(f"[batch] queue reconciliation deferred: {exc}", flush=True)
            time.sleep(5)


def process_tree(pid):
    """Host /proc, including independent container descendants; no extra dependency."""
    procs = {}
    for p in Path("/proc").glob("[0-9]*/stat"):
        try:
            fields = p.read_text().rsplit(")", 1)[1].split()
            procs[int(p.parent.name)] = (int(fields[1]), int(fields[11]) + int(fields[12]), int(fields[21]))
        except (OSError, ValueError, IndexError):
            continue
    children = {pid}
    while True:
        expanded = children | {p for p, v in procs.items() if v[0] in children}
        if expanded == children:
            break
        children = expanded
    values = [procs[p] for p in children if p in procs]
    return sum(v[2] for v in values) * os.sysconf("SC_PAGE_SIZE"), sum(v[1] for v in values) / os.sysconf("SC_CLK_TCK")


def driver_process(row, node):
    """srun's task is parented by slurmd, not by the local srun client."""
    modules = {b"ecarsi"}
    if module := row.get('execution', {}).get('osp_dispatch_module'):
        modules.add(os.fsencode(module))
    if module := row.get('execution', {}).get('preparation_module'):
        modules.add(os.fsencode(module))
    for p in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            args = p.read_bytes().split(b"\0")
            if not modules.intersection(args) or not Path(os.fsdecode(args[0])).name.startswith("python"):
                continue
            if not any(os.fsdecode(a) == row["output"] or os.fsdecode(a).startswith(row["output"] + "/") for a in args):
                continue
            group = (p.parent / "cgroup").read_text()
            if f"/job_{node['job_id']}/step_" not in group or "/step_extern" in group:
                continue
            pid = int(p.parent.name)
            if set(os.sched_getaffinity(pid)) <= set(row["cpu_ids"]):
                return pid
        except (OSError, ValueError):
            continue
    return None


def memory_headroom(proc_cgroup='/proc/self/cgroup', sysfs='/sys/fs/cgroup', meminfo='/proc/meminfo'):
    """Cgroup headroom including reclaimable clean file cache, bounded by RAM."""
    from .resources import available_memory_bytes
    candidates = [available_memory_bytes()]
    for line in Path(proc_cgroup).read_text().splitlines():
        _, controllers, suffix = line.split(":", 2)
        if controllers == "":
            root, limit, used = Path(sysfs), "memory.max", "memory.current"
        elif "memory" in controllers.split(","):
            root, limit, used = Path(sysfs)/'memory', "memory.limit_in_bytes", "memory.usage_in_bytes"
        else:
            continue
        p = root / suffix.lstrip("/")
        while p == root or root in p.parents:
            try:
                cap = int((p / limit).read_text())
                current = int((p / used).read_text())
                try:
                    stats = dict(line.split() for line in (p / 'memory.stat').read_text().splitlines())
                except OSError:
                    stats = {}
                if controllers == '':
                    clean = (int(stats.get('file', 0)) - int(stats.get('shmem', 0))
                             - int(stats.get('file_dirty', 0)) - int(stats.get('file_writeback', 0)))
                else:
                    clean = (int(stats.get('total_cache', 0)) - int(stats.get('total_shmem', 0))
                             - int(stats.get('total_dirty', 0)) - int(stats.get('total_writeback', 0)))
                # Clean file cache is reclaimable under cgroup pressure; keep
                # 20% as headroom for cache churn and concurrent writes.
                candidates.append(min(cap, max(0, cap - current + max(0, clean) * 4 // 5)))
            except (OSError, ValueError):
                pass
            if p == root:
                break
            p = p.parent
    for line in Path(meminfo).read_text().splitlines():
        if line.startswith("MemAvailable:"):
            candidates.append(int(line.split()[1]) * 1024)
    return min(candidates)


def step_name(row):
    return f"rsi-{row['id']}-{row['attempt']}"


def attempt_out_of_memory(row):
    """Only trust Slurm's OOM line after this exact attempt's log header."""
    try:
        with Path(row['log']).open('rb') as log:
            active = oom = False
            marker = f" attempt={row['attempt']}\n".encode()
            for line in log:
                if line.startswith(b'[batch] node='):
                    active = row['node'].encode() in line and marker in line
                    oom = False
                elif active and b'oom_kill event in StepId=' in line:
                    oom = True
            return oom
    except (KeyError, OSError):
        return False


def allocation_steps(job_id, *, allow_missing=False):
    """Query the job's step manager; squeue can omit its live numeric steps."""
    job = str(job_id)
    if not job.isdigit():
        raise ValueError("invalid allocation identity")
    try:
        result = subprocess.run(["scontrol", "-o", "show", "step", job],
                                capture_output=True, text=True, timeout=15, check=True)
    except subprocess.CalledProcessError as exc:
        # Only callers with positive terminal accounting evidence may accept a
        # job already purged from the controller. Network errors are not death.
        if allow_missing and "Invalid job id specified" in ((exc.stderr or "") + (exc.stdout or "")):
            return {}
        raise
    steps = {}
    for line in result.stdout.splitlines():
        if not line.strip() or line.strip() == "No steps in the system.":
            continue
        fields = dict(re.findall(r"(?:^|\s)(\w+)=([^\s]*)", line))
        identity = fields.get("StepId", "")
        if not re.fullmatch(re.escape(job) + r"(?:\+\d+)?\.[^\s]+", identity):
            raise ValueError("invalid step response; refusing to signal an allocation or unknown step identity")
        if "Name" not in fields:
            raise ValueError("step response is missing its name")
        steps[identity] = fields["Name"]
    return steps


def attempt_steps(row, node):
    """Only query this attempt's named steps; a failed Slurm query is not death."""
    if not node.get("job_id"):
        return []
    steps = []
    for identity, name in allocation_steps(node["job_id"]).items():
        if name == step_name(row):
            if not re.fullmatch(re.escape(str(node["job_id"])) + r"\.\d+", identity):
                raise ValueError("refusing to signal an allocation or unknown step identity")
            steps.append(identity)
    return steps


def signal_step(job_id, step, sig):
    if not re.fullmatch(re.escape(str(job_id)) + r"\.\d+", step):
        raise ValueError("refusing to signal an allocation or unknown step identity")
    try:
        subprocess.run(["scancel", "--signal=" + sig, step],
                       capture_output=True, text=True, timeout=15, check=True)
    except subprocess.CalledProcessError:
        if step in allocation_steps(job_id):
            raise  # disappearance is benign; a failed signal against a live step is not


def enroll_legacy_execution(row, node, path, record):
    """Observe an old live supervisor before permitting later orphan cleanup."""
    if record.get('supervisor_protocol') == 1 or not node.get('job_id'):
        return
    supervisor = process_identity(record.get('pid'))
    child = process_identity(record.get('child_pid'))
    driver = driver_process(row, node)
    if not supervisor or not child or not driver:
        return
    args = Path(f"/proc/{child['pid']}/cmdline").read_bytes().split(b'\0')
    if os.fsencode(row['output']) not in args:
        return
    group = Path(f'/proc/{driver}/cgroup').read_text()
    steps = set(re.findall(r'/job_' + re.escape(str(node['job_id'])) + r'/step_(\d+)(?:/|$)', group, re.M))
    if not steps or process_identity(child['pid']) != child or process_identity(supervisor['pid']) != supervisor:
        return
    write_json(path.with_suffix('.processes.json'), dict(supervisor=supervisor, child=child,
               steps=[str(node['job_id'])+'.'+step for step in sorted(steps)], observed_at=time.time()))


def recover_execution(row, node, directory):
    """Keep uncertain execution reserved during startup and regular recovery."""
    try:
        _recover_execution(row, node, directory)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        row.update(reservation_held=True, recovery_status=f"Execution check unavailable: {exc}")


def _recover_execution(row, node, directory):
    """Called on the execution host. Adopt a writer, or fence its orphan step.

    The receipt lock spans launch through termination. Holding it prevents a
    delayed supervisor from starting while we confirm and retire this attempt.
    """
    path = receipt_path(directory, row)
    # The node agent publishes launch_requested before spawning the supervisor.
    # Shared-storage and Slurm startup can take longer than one agent tick.
    owner = row.get("launch_owner")
    owner_alive = owner is None or process_identity(owner["pid"]) == owner
    if (not path.exists() and row.get("launch_requested") and owner_alive and
            time.time() - row["launch_requested"] < 120):
        row.update(reservation_held=True, recovery_status="Waiting for supervisor start")
        return
    try:
        with writer_lock(path.with_suffix(".lock")):
            record = json.loads(path.read_text()) if path.exists() else dict(node=row["node"])
            if record.get("finished_at"):
                return
            legacy = None
            if row.get("supervisor_protocol") != 1 and record.get("supervisor_protocol") != 1:
                try:
                    legacy = json.loads(path.with_suffix('.processes.json').read_text())
                except FileNotFoundError:
                    pass
                if (not legacy or legacy['child']['pid'] != record.get('child_pid') or
                        legacy['supervisor']['pid'] != record.get('pid')):
                    row.update(reservation_held=True, recovery_status="Waiting for legacy execution or allocation termination evidence")
                    return
                record['child_identity'] = legacy['child']
            # A surviving local srun may still create its remote step. Fence the
            # launcher first, then query Slurm on the following reconciliation.
            launchers = []
            orphan_groups = set()
            for proc in Path("/proc").glob("[0-9]*/cmdline"):
                try:
                    args = proc.read_bytes().split(b"\0")
                    if (b"--job-name=" + step_name(row).encode()) in args:
                        identity = process_identity(int(proc.parent.name))
                        if identity:
                            launchers.append(identity)
                except FileNotFoundError:
                    continue
            child = record.get("child_identity")
            if child:
                current = process_identity(child["pid"])
                if current == child:
                    launchers.append(child)
                elif current is None and child.get("boot_id") == Path("/proc/sys/kernel/random/boot_id").read_text().strip():
                    # The process group can survive its leader (e.g. a shell
                    # launcher). Its numeric ID cannot be reused while alive.
                    for proc in Path("/proc").glob("[0-9]*/stat"):
                        try:
                            fields = proc.read_text().rsplit(")", 1)[1].split()
                            if fields[0] != "Z" and int(fields[2]) == child["pid"]:
                                launchers.append(child)
                                orphan_groups.add(child["pid"])
                                break
                        except FileNotFoundError:
                            continue
            now = time.time()
            since = record.setdefault("cleanup_started_at", now)
            sig = signal.SIGKILL if now - since >= 15 else signal.SIGTERM
            for identity in launchers:
                current = process_identity(identity["pid"])
                if current == identity or (current is None and identity["pid"] in orphan_groups and group_alive(identity["pid"])):
                    try:
                        os.killpg(identity["pid"], sig)
                    except ProcessLookupError:
                        pass
            if legacy:
                steps = list(set(allocation_steps(node['job_id'])) & set(legacy['steps']))
                if any(not re.fullmatch(re.escape(str(node['job_id']))+r'\.\d+', step) for step in steps):
                    raise ValueError('invalid legacy step identity')
            else:
                steps = attempt_steps(row, node)
            for identity in steps:
                signal_step(node['job_id'], identity, signal.Signals(sig).name)
            if launchers or steps:
                record.update(state="paused", reason="Stopping orphan execution", execution_observed=True, updated_at=now)
                row.update(reservation_held=True, recovery_status="Stopping orphan execution")
            else:
                record.update(state="paused", reason="Previous supervisor stopped; execution confirmed absent",
                              termination_reason="execution_lost" if record.get("child_pid") or record.get("execution_observed") else "launch_interrupted",
                              exit_code=None, finished_at=now, updated_at=now)
            write_json(path, record)
    except RuntimeError as exc:
        if "another writer holds" not in str(exc):
            raise
        # The old supervisor still owns the writer lock. Its receipt remains
        # authoritative, even when its parent node agent has changed.
        record = json.loads(path.read_text()) if path.exists() else {}
        try:
            enroll_legacy_execution(row, node, path, record)
        except FileNotFoundError:
            pass  # command exited during observation; retry the next tick
        row["state"] = record.get("state", "assigned")
        row.pop("reservation_held", None)
        row.pop("recovery_status", None)
        if "reason" not in record:
            row.pop("reason", None)


def supervise(config_path, row_id, attempt, directory=None):
    """Own the container and resource locks even if the node agent is interrupted."""
    directory = directory or configuration(config_path)["directory"]
    with queue(directory) as state:
        row = next((r.copy() for r in state["datasets"] if r["id"] == row_id and r["attempt"] == attempt), None)
        if row is None or row["state"] not in ACTIVE or not row.get("node"):
            return 0  # admission was reassigned before this delayed launcher started
        node = state["nodes"][row["node"]].copy()
    config = row["execution"]  # configuration changes apply only to the next admission
    path = receipt_path(directory, row)
    with writer_lock(path.with_suffix(".lock")), writer_lock(Path(row["output"]) / ".batch-driver.lock"):
        if path.exists():
            raise RuntimeError("attempt already has a receipt; inspect it before retrying")
        record = dict(node=row["node"], state="assigned", pid=os.getpid(),
                      supervisor_protocol=1, supervisor_identity=process_identity(os.getpid()), updated_at=time.time())
        write_json(path, record)
        launched = False
        try:
            os.sched_setaffinity(0, row["cpu_ids"])
            env = dict(os.environ, **config["env"])
            for name in config.get("required_env", []):
                if not env.get(name):
                    raise ValueError(f"required node environment variable is missing: {name}")
            env.update(ECA_POOL_SCHEDULER=config["scheduler"], OSP_COMPUTE_ENDPOINT="pool", MSP_COMPUTE_ENDPOINT="pool",
                       ECA_RSI_PAUSE_FILE=str(path.with_suffix(".pause")),
                       ECA_DRIVER_MEMORY_BYTES=str(int(row.get('admission_memory_gb', row['memory_gb']) * 2**30)),
                       ECA_DATASET_PEAK_MEMORY_BYTES=str(int(row['memory_gb'] * 2**30)),
                       XDG_CACHE_HOME=str(Path(row["output"]) / ".cache"),
                       MPLCONFIGDIR=str(Path(row["output"]) / ".cache/mpl"))
            if config.get('driver_memory_lending'):
                env['ECA_DRIVER_LEASE_DIRECTORY'] = str(Path(directory)/'memory-leases'/f"{row_id}-{attempt}")
            for k in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMBA_NUM_THREADS", "MSP_MAX_THREADS"):
                env[k] = env["APPTAINERENV_" + k] = str(len(row['cpu_ids']))
            attempt_deadline = time.time() + row["hours"] * 3600
            allocation_deadline = node["end_time"] - 30
            deadline = min(allocation_deadline, attempt_deadline)
            stopping = False
            def stop(*_):
                nonlocal stopping
                stopping = True
            signal.signal(signal.SIGTERM, stop)
            signal.signal(signal.SIGINT, stop)
            log_path = Path(row["log"])
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a") as log:
                log.write(f"\n[batch] node={row['node']} cpus={row['cpu_ids']} memory_gb={row['memory_gb']} attempt={attempt}\n")
                log.flush()
                for cmd in dataset_commands(config["python"], row):
                    if node.get("job_id"):
                        # Overlap steps may choose only the first N allocated CPUs. Request
                        # the allocation's mask, then bind within the driver CPU slice.
                        # Slurm enforces this dataset's memory limit independently.
                        cmd = ["srun", "--jobid=" + str(node["job_id"]), "--nodelist=" + node["host"],
                               "--job-name=" + step_name(row), "--overlap", "--exact", "--immediate=30", "-N1", "-n1",
                               "-c" + str(node["allocation_cpus"]), "--mem=" + str(math.ceil(row.get('admission_memory_gb', row['memory_gb']) * 1024)) + "M",
                               "--cpu-bind=none", "taskset", "-c", ",".join(map(str, row["cpu_ids"])), *cmd]
                    child = subprocess.Popen(cmd, cwd="/", env=env, stdin=subprocess.DEVNULL, stdout=log,
                                             stderr=log, start_new_session=True)
                    launched = True
                    record.setdefault("started_at", time.time())
                    record.update(state="running", child_pid=child.pid, child_identity=process_identity(child.pid))
                    write_json(path, record)
                    previous_cpu, previous_time = 0, time.monotonic()
                    previous_pid = None
                    try:
                        while child.poll() is None:
                            measured_pid = driver_process(row, node) if node.get("job_id") else child.pid
                            rss, cpu = process_tree(measured_pid) if measured_pid else (0, 0)
                            now = time.monotonic()
                            usage = max(0, cpu - previous_cpu) / max(.1, now - previous_time) * 100 if measured_pid == previous_pid else 0
                            record.update(rss_bytes=rss, cpu_percent=usage, driver_pid=measured_pid,
                                          updated_at=time.time())
                            previous_cpu, previous_time, previous_pid = cpu, now, measured_pid
                            write_json(path, record)
                            reason = ("Driver stop requested" if stopping else
                                      "Driver time budget or Slurm allocation ending" if time.time() >= deadline else None)
                            if reason:
                                record["reason"] = reason
                                if stopping:
                                    request = _handoff_request(path, row)
                                    record["termination_reason"] = (
                                        "deployment_handoff" if request and request.get("interrupt") is True
                                        and path.with_suffix(".pause").is_file() else "user_stop")
                                else:
                                    record["termination_reason"] = (
                                        "allocation_end" if allocation_deadline <= attempt_deadline else "deadline")
                                os.killpg(child.pid, signal.SIGTERM)
                                try:
                                    child.wait(timeout=15)
                                except subprocess.TimeoutExpired:
                                    os.killpg(child.pid, signal.SIGKILL)
                                    child.wait()
                                break
                            time.sleep(5)
                    finally:
                        if child.poll() is None:
                            os.killpg(child.pid, signal.SIGTERM)
                            try:
                                child.wait(timeout=15)
                            except subprocess.TimeoutExpired:
                                os.killpg(child.pid, signal.SIGKILL)
                                child.wait()
                    cleanup = time.monotonic()
                    while True:
                        # Reaping the srun client is not proof that its remote
                        # step ended. Do not release memory/CPU or start another
                        # command until Slurm confirms this attempt is absent.
                        try:
                            steps = attempt_steps(row, node)
                            if not steps:
                                break
                            record['cleanup_steps'] = steps
                            elapsed = time.monotonic() - cleanup
                            if "reason" in record or elapsed >= 15:
                                sig = "SIGKILL" if elapsed >= 15 else "SIGTERM"
                                for step in steps:
                                    signal_step(node['job_id'], step, sig)
                            record.pop('cleanup_error', None)
                        except (OSError, ValueError, subprocess.SubprocessError) as exc:
                            record['cleanup_error'] = str(exc)
                        record.update(updated_at=time.time())
                        write_json(path, record)
                        time.sleep(1)
                    record.pop("cleanup_steps", None)
                    record.pop("cleanup_error", None)
                    if child.returncode != 0 or "reason" in record:
                        break
                    launched = False
                rc = child.returncode
                success = 'prepared' if row.get('admission_phase') == 'preparation' else 'completed'
                log.flush()
                if rc != 0 and attempt_out_of_memory(row):
                    record['termination_reason'] = 'out_of_memory'
                record.update(state="paused" if rc == 3 or "reason" in record else success if rc == 0 else "failed",
                              exit_code=rc, finished_at=time.time(), updated_at=time.time())
                write_json(path, record)
        except Exception as exc:
            if not launched and not record.get("finished_at"):
                record.update(state="failed", exit_code=None, termination_reason=launch_failure(exc),
                              reason=f"{type(exc).__name__}: {exc}", finished_at=time.time(), updated_at=time.time())
                write_json(path, record)
            raise
    return 0


def dataset_commands(python, row):
    """Keep explicit experiment mappings from existing dataset preparations."""
    base = [python, "-P", "-m", "ecarsi"]
    if row.get('admission_phase') == 'preparation':
        execution = row['execution']
        command = [python, '-P', '-m', execution['preparation_module'], row['input'], row['output'],
                   '--samples', str(execution.get('preparation_samples', 4))]
        for key, flag in [('preparation_max_prepared_samples', '--max-prepared-samples'),
                          ('preparation_max_sample_cells', '--max-sample-cells')]:
            if execution.get(key):
                command += [flag, str(execution[key])]
        if row.get('sample_map'):
            command += ['--sample-map', row['sample_map']]
        if row.get('mirror'):
            command += ['--mirror', row['mirror']]
        yield command
        return
    run = base + ["run", row["input"], row["output"]]
    if row.get("mirror"):
        run += ["--mirror", row["mirror"]]
    dispatcher = row.get('execution', {}).get('osp_dispatch_module')
    if row.get("sample_map") or dispatcher:
        yield run + ["--stop-after", "organize"]
        from .layout import units
        for unit in units(Path(row["output"])):
            command = ([python, '-P', '-m', dispatcher, str(unit)] if dispatcher else base + ["persample", str(unit)])
            if row.get('sample_map'):
                command += ['--sample-map', row['sample_map']]
            yield command
    yield run


def node_agent(config_path, memory_gb, cpus=None, role='driver', drain_only=False):
    import fcntl
    from .pool.slurm import inventory
    config = configuration(config_path)
    for name in config.get("required_env", []):
        if not os.environ.get(name):
            raise ValueError(f"required node environment variable is missing: {name}")
    profile = inventory(int(memory_gb * 2**30), cpus)
    profile['role'] = role
    os.sched_setaffinity(0, profile["cpu_ids"])
    identity = f"{profile['host']}/{profile['job_id']}/" + ",".join(map(str, profile["cpu_ids"]))
    lockdir = Path.home() / ".cache/ecarsi-pool" / profile["host"]
    lockdir.mkdir(parents=True, exist_ok=True)
    with writer_lock(lockdir / ("node-" + identity.replace("/", "-") + ".lock")):
        return run_node_agent(config_path, memory_gb, cpus, profile, identity, lockdir, drain_only)


def drain_complete(rows, owners):
    return not any(r.get('node') in owners and (r['state'] in ACTIVE or r.get('reservation_held')) for r in rows)


def run_node_agent(config_path, memory_gb, cpus, profile, identity, lockdir, drain_only=False):
    import fcntl
    from .pool.slurm import inventory
    config = configuration(config_path)
    locks = {}
    profile.update(id=identity, draining=False,
                   allocation_cpus=int(re.search(r"(?:^|,)cpu=(\d+)", profile["allocated_tres"])[1]))
    profile['inventory_observed_at'] = profile.get('observed_at', time.time())
    with queue(config["directory"]) as state:
        predecessors = []
        for old in state["nodes"].values():
            if old["id"] == identity or (old.get("host"), old.get("job_id")) != (profile["host"], profile["job_id"]):
                continue
            if not set(old["cpu_ids"]) & set(profile["cpu_ids"]):
                continue
            if not set(old["cpu_ids"]) <= set(profile["cpu_ids"]):
                raise ValueError("replacement node must cover each old driver CPU slice completely")
            if not old.get("draining") and time.time() - old.get("observed_at", 0) <= 60:
                raise ValueError("drain the previous node agent before merging its CPU slice")
            predecessors.append(old["id"])
        profile["predecessors"] = predecessors
        owners = {identity, *predecessors}
        pending_recovery = [r.copy() for r in state["datasets"] if r.get("node") in owners and r.get("launch_requested")
                            and (r["state"] in ACTIVE or r.get("reservation_held"))]
    # Retire orphan/unstarted admissions before budget validation. Otherwise an
    # interrupted launch can prevent the very agent needed to reconcile it.
    for row in pending_recovery:
        recover_execution(row, profile, config["directory"])
    with queue(config["directory"], write=False) as snapshot:
        observations = receipt_observations(snapshot, config["directory"], owners=owners)
    with queue(config["directory"]) as state:
        reconcile(state, config["directory"], observations)
        from .driver_budget import reserved as memory_reserved
        reserved = sum(max(memory_reserved(r), r.get('rss_bytes', 0)) for r in state['datasets']
                       if r.get('node') in owners and (r['state'] in ACTIVE or r.get('reservation_held')))
        # A running computation may already be using the return window.
        # Adoption reserves its actual grant; only NEW admission needs a spare
        # window. Requiring both here would block the agent needed for release.
        if reserved > profile["memory"]:
            raise ValueError("replacement driver budget is below active execution reservations")
    from .pool.budget import reserve
    reserve(profile, "driver:" + identity, lockdir / ("node-" + identity.replace("/", "-") + ".lock"),
            role="driver", replace=["driver:" + old for old in predecessors], queue_directory=config["directory"])
    children = {}
    stopping = drain_only
    def stop(*_):
        nonlocal stopping
        stopping = True  # drain; independent supervisors retain the resource locks
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    refresh = 0
    while True:
        try:
            candidate = configuration(config_path)
            if candidate["directory"] != config["directory"]:
                raise ValueError("queue directory cannot change while this node is running")
            config = candidate
        except (OSError, ValueError, TypeError) as exc:
            print(f"[batch] config update rejected: {exc}", flush=True)
        if time.monotonic() >= refresh:
            try:
                refreshed = inventory(int(memory_gb * 2**30), cpus)
                profile.update(refreshed, inventory_observed_at=refreshed['observed_at'])
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
                print(f"[batch] inventory stale: {exc}", flush=True)
            refresh = time.monotonic() + 30
        profile.update(memory_headroom=memory_headroom(), draining=stopping,
                       driver_cpu_slots_per_core=config["driver_cpu_slots_per_core"])
        pending = []
        owners = {identity, *profile["predecessors"]}
        with queue(config["directory"], write=False) as state:
            observations = receipt_observations(state, config["directory"], owners=owners)
            candidates = [r.copy() for r in state["datasets"] if r.get("node") in owners
                          and r.get("launch_requested") and (r["state"] in ACTIVE or r.get("reservation_held"))]
        # Slurm and /proc inspection is outside the fleet queue lock. A slow
        # node must not prevent unrelated nodes admitting/completing datasets.
        for row in candidates:
            recover_execution(row, profile, config["directory"])
        recovered = {(r["id"], r["attempt"]): r for r in candidates}
        with queue(config["directory"]) as state:
            reconcile(state, config["directory"], observations)
            for row in state["datasets"]:
                child = children.get(row["id"])
                if child and child.poll() is not None:
                    children.pop(row["id"])
                checked = recovered.get((row["id"], row["attempt"]))
                if checked and (row["state"] in ACTIVE or row.get("reservation_held")):
                    for key in ("reservation_held", "recovery_status"):
                        if key in checked:
                            row[key] = checked[key]
                        else:
                            row.pop(key, None)
                    if row["state"] == "paused" and checked["state"] in ACTIVE:
                        row["state"] = checked["state"]
            if not config.get("pause_dispatch"):
                retry_finished(state, config)
            for cpu in profile["cpu_ids"]:
                if cpu not in locks:
                    lock = (lockdir / f"cpu-{cpu}.lock").open("a+")
                    try:
                        # The durable node/budget locks identify this driver
                        # slice. Shared CPU locks survive agent replacement;
                        # worker launchers still need an exclusive CPU lock.
                        fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
                    except BlockingIOError:
                        lock.close()
                    else:
                        locks[cpu] = lock
            profile["available_cpu_ids"] = sorted(locks)
            # Agent liveness is distinct from the slower Slurm inventory
            # refresh. Publish after recovery/lock waits, not before them.
            profile.update(observed_at=time.time(),
                           draining=stopping or time.time()-profile['inventory_observed_at'] > 90)
            state["nodes"][identity] = profile.copy()
            assign(state, config)
            finished_draining = drain_complete(state['datasets'], owners)
            for row in state["datasets"]:
                if row.get("node") == identity and row["state"] == "assigned" and not row.get("launch_requested"):
                    if not set(row['cpu_ids']) <= locks.keys():
                        # A controller may have used the preceding node process's
                        # offer. No launch was claimed: reacquire ownership first.
                        row.update(state='queued', attempt=row['attempt']-1,
                                   queue_reason='Waiting for driver CPU locks')
                        for key in ('node', 'cpu_ids', 'execution', 'assigned_at'):
                            row.pop(key, None)
                        continue
                    row["launch_requested"] = time.time()
                    row["launch_owner"] = process_identity(os.getpid())
                    row["supervisor_protocol"] = 1
                    pending.append(row.copy())
        for row in pending:
            cmd = [sys.executable, "-m", "ecarsi.batch", "supervise", "--config", str(config_path),
                   "--directory", config["directory"], row["id"], str(row["attempt"])]
            try:
                children[row["id"]] = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, start_new_session=True,
                                                      pass_fds=tuple(locks[cpu].fileno() for cpu in row["cpu_ids"]))
            except OSError as exc:
                write_json(receipt_path(config["directory"], row), {"node": identity, "state": "failed", "reason": str(exc), "exit_code": None,
                    "termination_reason": launch_failure(exc), "finished_at": time.time(), "updated_at": time.time()})
        if (stopping and not children and finished_draining) or time.time() >= profile["end_time"]:
            break
        time.sleep(5)
    with queue(config["directory"]) as state:
        state["nodes"][identity]["draining"] = True


def handoff_nodes(config, nodes, dataset_ids=None, *, interrupt=False):
    """Mark selected attempts for checkpoint handoff; never signal them here.

    A dedicated per-attempt marker lets recovery distinguish this maintenance
    pause from a user pause. interrupt=True authorizes a separately signalled
    supervisor stop to resume after its old process and Slurm step are gone.
    """
    if type(interrupt) is not bool:
        raise ValueError("interrupt must be boolean")
    with queue(config["directory"]) as state:
        rows = [r.copy() for r in state["datasets"] if r.get("node") in nodes and r["state"] in ACTIVE
                and (dataset_ids is None or r["id"] in dataset_ids)]
    requested = []
    for row in rows:
        path = receipt_path(config["directory"], row)
        if not path.exists():
            continue
        receipt = json.loads(path.read_text())
        if receipt.get("finished_at") or receipt.get("node") != row["node"]:
            continue
        marker = path.with_suffix(".pause")
        request = dict(id=row["id"], attempt=row["attempt"], requested_at=time.time(), interrupt=interrupt)
        try:
            with marker.open("x") as f:
                f.write("checkpoint handoff requested by deployment\n")
        except FileExistsError:
            # An earlier cooperative deployment can itself be stuck in a pool
            # wait. Explicit interrupt may finish that same recorded handoff.
            if (not interrupt or _handoff_request(path, row) is None
                    or marker.read_text() != 'checkpoint handoff requested by deployment\n'):
                continue  # an existing user pause is never converted to maintenance
        write_json(path.with_suffix(".handoff.json"), request)
        requested.append(row["id"])
    return requested


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("submit", "status", "node", "supervise", "retry", "profile", "controller", "handoff"):
        a = sub.add_parser(name)
        a.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
        if name == "submit":
            a.add_argument("manifest", type=Path, help='JSON list of {input, output, mirror, cpus, memory_gb, hours}')
        elif name == "handoff":
            a.add_argument("nodes", nargs="+")
        elif name == "node":
            a.add_argument("--memory-gb", type=float, required=True, help="driver budget, excluding compute workers")
            a.add_argument("--cpus", type=int)
            a.add_argument('--role', choices=['driver', 'preparation'], default='driver')
            a.add_argument('--drain-only', action='store_true', help='recover old executions without admitting new work')
        elif name == "supervise":
            a.add_argument("--directory")
            a.add_argument("id")
            a.add_argument("attempt", type=int)
        elif name == "retry":
            a.add_argument("id")
            a.add_argument("--memory-gb", type=float)
    a = p.parse_args(argv)
    if a.command == "supervise":
        return supervise(a.config.resolve(), a.id, a.attempt, a.directory)
    c = configuration(a.config)
    if a.command == "controller":
        controller(a.config.resolve())
    elif a.command == "profile":
        # Metadata I/O stays outside the queue lock. A concurrently admitted job
        # keeps its original resources for that attempt.
        with queue(c["directory"]) as state:
            pending = [(r["id"], r["input"]) for r in state["datasets"] if r["state"] == "queued"]
        profiles = {key: input_resources(source) for key, source in pending}
        with queue(c["directory"]) as state:
            changed = 0
            for row in state["datasets"]:
                if row["state"] == "queued" and row["id"] in profiles:
                    row.setdefault("original_resources", {k: row[k] for k in ("cpus", "memory_gb")})
                    row.update(profiles[row["id"]])
                    changed += 1
        print(json.dumps({"profiled": changed}))
    elif a.command == "submit":
        rows = submit(c, json.loads(a.manifest.read_text()))
        if c.get("registry"):
            from .serve import Registry
            os.environ["ECA_DATASET_QUEUE"] = c["directory"]
            registry = Registry(Path(c["registry"]))
            for row in rows:
                if row.get("mirror"):
                    registry.bind(row.get("registry_name", row["name"]), Path(row["mirror"]))
        print(json.dumps([{k: r[k] for k in ("id", "name", "state")} for r in rows], indent=2))
    elif a.command == "status":
        with queue(c["directory"]) as state:
            reconcile(state, c["directory"])
            print(json.dumps(state, indent=2))
    elif a.command == "handoff":
        print(json.dumps({"handoff_requested": handoff_nodes(c, set(a.nodes))}))
    elif a.command == "node":
        node_agent(a.config.resolve(), a.memory_gb, a.cpus, a.role, a.drain_only)
    else:
        with queue(c["directory"]) as state:
            reconcile(state, c["directory"])
            row = next(r for r in state["datasets"] if r["id"] == a.id)
            receipt = json.loads(receipt_path(c["directory"], row).read_text())
            if row["state"] not in {"failed", "paused"} or receipt.get("finished_at") is None:
                raise ValueError("retry needs a confirmed terminal receipt; inspect uncertain execution first")
            if a.memory_gb is not None:
                if not math.isfinite(a.memory_gb) or a.memory_gb <= 0:
                    raise ValueError("invalid memory budget")
                row["memory_gb"] = a.memory_gb
            row["state"] = "queued"
            for key in ("node", "cpu_ids", "execution", "launch_requested", "reason", "termination_reason", "exit_code", "finished_at", "started_at", "assigned_at", "pid", "child_pid", "driver_pid", "rss_bytes", "cpu_percent", "reservation_held", "retry_after"):
                row.pop(key, None)
    return 0


_monitor_background = None
_monitor_ready = None


def start_monitor():
    """Periscope shares one background reader; CLI callers remain synchronous."""
    import threading
    global _monitor_background, _monitor_ready
    if _monitor_background is not None:
        return _monitor_ready
    _monitor_ready = threading.Event()
    _monitor_background = dict(datasets=[], nodes={}, by_mirror={})
    def refresh():
        global _monitor_background
        while True:
            try:
                _monitor_background = _read_monitor()
                _monitor_ready.set()
            except Exception as exc:
                print(f'[batch] background status refresh: {exc}', file=sys.stderr)
            time.sleep(5)
    threading.Thread(target=refresh, daemon=True, name='batch-status-reader').start()
    return _monitor_ready


def monitor():
    return _monitor_background if _monitor_background is not None else _read_monitor()


def _read_monitor():
    """Read-only union of the persistent queue and an optional legacy batch."""
    sources = []
    for name, persistent in (("ECA_PERISCOPE_BATCH_STATUS", False), ("ECA_DATASET_QUEUE", True)):
        value = os.environ.get(name)
        if value:
            path = Path(value) / "status.json" if persistent else Path(value)
            try:
                stat = path.stat()
                stamp = (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino,
                         not persistent and path.with_name("pause").exists())
            except OSError:
                stamp = None
            sources.append((str(path), persistent, stamp))
    return _monitor(tuple(sources), int(time.monotonic() // 5))


def activity(row):
    """Latest logged phase, not an assertion that a model/GPU is currently busy."""
    if not row.get("log"):
        return {}
    try:
        path = Path(row["log"])
        with path.open("rb") as f:
            f.seek(max(0, path.stat().st_size - 65536))
            lines = f.read().decode(errors="replace").splitlines()
        result = {"last_log_at": path.stat().st_mtime}
    except OSError:
        return {}
    for line in reversed(lines):
        lower = line.lower()
        if "[pool] waiting" in lower:
            phase, kind = "Waiting for compute", "compute_wait"
        elif "[pool]" in lower and " -> " in line:
            phase, kind = "Compute submitted", "compute"
        elif "agent:" in lower or "[agent]" in lower:
            phase = ("Model annotation" if "[annotate]" in lower or "[osp annotate]" in lower else
                     "Model inspection" if "[inspect]" in lower else
                     "Model planning" if "[zmip plan]" in lower or "[identify " in lower else "Model review")
            kind = "model"
        elif lower.startswith("[zmip]") or lower.startswith("[msp]") or "== " in lower and not "[" in lower:
            phase, kind = "Local pipeline work", "local"
        elif "[eca-rsi] done" in lower or "[eca-rsi] failed" in lower:
            phase, kind = "Driver finishing", "finishing"
        else:
            continue
        return dict(result, activity=phase, activity_kind=kind, last_message=line[:240])
    return dict(result, activity="Starting / no phase reported", activity_kind="unknown")


@lru_cache(maxsize=4)
def _monitor(sources, tick):
    # One snapshot per UI refresh, not one full queue scan per displayed dataset.
    result = {"datasets": [], "nodes": {}}
    for source, persistent, stamp in sources:
        path = Path(source)
        try:
            state = json.loads(path.read_text())
            if persistent:
                reconcile(state, path.parent)
            for row in state.get("datasets", []):
                row = dict(row)
                if persistent and row["state"] == "queued" and row.get("queue_reason") == "Dataset dispatch is paused":
                    row.update(state="paused", dispatch_paused=True)
                if row["state"] in ACTIVE:
                    row.update(activity(row))
                row.setdefault("submitted_at", state.get("submitted_at"))
                row["waiting"] = row["state"] in {"queued", "assigned", "retry_wait"} or (
                    not persistent and row["state"] == "paused" and not state.get("runner_finished_at")
                    and not path.with_name("pause").exists())
                result["datasets"].append(row)
            result["nodes"].update(state.get("nodes", {}))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    # Persistent queue entries supersede a migrated legacy run. Count each
    # working output once, including in workflow totals (not just navigation).
    result['datasets'] = list({os.path.normpath(r.get('output') or r.get('mirror') or r['name']): r
                               for r in result['datasets']}.values())
    result["by_mirror"] = {str(Path(r["mirror"]).resolve()): r for r in result["datasets"] if r.get("mirror")}
    return result


if __name__ == "__main__":
    raise SystemExit(main())

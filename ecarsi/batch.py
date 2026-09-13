"""Persistent dataset admission on user-provisioned Slurm nodes.

The queue is a shared file, protected by flock and atomic replacement. Node
agents run on the host; each dataset owns an independent container supervisor.
Compute workers retain their separate resource budgets and pool scheduler.
"""
from __future__ import annotations

import argparse
import contextlib
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

ACTIVE = {"assigned", "running"}
TERMINAL = {"completed", "failed", "paused"}
DEFAULT_CONFIG = Path.home() / ".config/ecarsi/batch.json"


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
    for key in ("pause_dispatch",):
        if key in c and type(c[key]) is not bool:
            raise ValueError(f"{key} must be boolean")
    c.setdefault("max_cpu_percent", 90)
    if not 0 < c["max_cpu_percent"] <= 100:
        raise ValueError("max_cpu_percent must be in (0, 100]")
    if not isinstance(c.get("required_env", []), list) or not all(
            isinstance(v, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", v) for v in c.get("required_env", [])):
        raise ValueError("required_env must list environment variable names")
    return c


@contextlib.contextmanager
def queue(directory):
    import fcntl
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".queue.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        p = root / "status.json"
        state = json.loads(p.read_text()) if p.exists() else {"schema_version": 1, "datasets": [], "nodes": {}}
        yield state
        state["updated_at"] = time.time()
        write_json(p, state)


def submit(config, rows):
    """Validate the entire submission before publishing; duplicate outputs are idempotent."""
    prepared = []
    for row in rows:
        r = {**config.get("defaults", {}), **row}
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


def reconcile(state, directory):
    for row in state["datasets"]:
        if row["state"] not in ACTIVE and not row.get("reservation_held"):
            continue
        try:
            receipt = json.loads(receipt_path(directory, row).read_text())
        except FileNotFoundError:
            continue
        if receipt.get("node") == row.get("node"):
            if row.get("reservation_held") and receipt.get("finished_at") is None:
                continue
            row.update({k: receipt[k] for k in ("state", "pid", "child_pid", "driver_pid", "rss_bytes", "cpu_percent",
                       "updated_at", "started_at", "finished_at", "exit_code", "reason") if k in receipt})
            if receipt.get("finished_at") is not None and receipt["state"] in TERMINAL:
                row.pop("reservation_held", None)
        usage = receipt_path(directory, row).with_suffix(".usage.json")
        if usage.exists():
            metrics = json.loads(usage.read_text())
            if time.time() - metrics["observed_at"] < 15:
                row.update({k: metrics[k] for k in ("rss_bytes", "cpu_percent", "driver_pid")})


def capacity(node, rows, now):
    running = [r for r in rows if r.get("node") == node["id"] and (r["state"] in ACTIVE or r.get("reservation_held"))]
    used = {cpu for r in running for cpu in r["cpu_ids"]}
    free_cpus = [cpu for cpu in node["cpu_ids"] if cpu not in used]
    reserved = sum(max(r["memory_gb"] * 2**30, r.get("rss_bytes", 0)) for r in running)
    slack = sum(max(0, r["memory_gb"] * 2**30 - r.get("rss_bytes", 0)) for r in running)
    free_memory = min(node["memory"] - reserved, node.get("memory_headroom", 0) - slack)
    busy = sum(r.get("cpu_percent", 0) for r in running) / node["cpus"]
    return free_cpus, free_memory, busy


def assign(state, config, now=None):
    """FIFO with fitting jobs allowed past a blocked one; spread across free nodes."""
    now = time.time() if now is None else now
    for row in state["datasets"]:
        node = state["nodes"].get(row.get("node"), {})
        if row["state"] == "assigned" and not row.get("launch_requested") and now - node.get("observed_at", 0) > 60:
            row["state"] = "queued"  # no launch was claimed; reassignment is safe under the queue lock
            for key in ("node", "cpu_ids", "execution"):
                row.pop(key, None)
    # ponytail: O(datasets * nodes * active datasets); index reservations if fleet size warrants it.
    for row in state["datasets"]:
        if row["state"] != "queued":
            continue
        choices, reasons = [], set()
        for key, node in state["nodes"].items():
            if node.get("draining") or now - node["observed_at"] > 60:
                reasons.add("Node offline or draining")
                continue
            if now + row["hours"] * 3600 + 60 > node["end_time"]:
                reasons.add("Insufficient Slurm time remaining")
                continue
            cpus, memory, busy = capacity(node, state["datasets"], now)
            if len(cpus) < row["cpus"] or memory < row["memory_gb"] * 2**30:
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
            row.update(state="assigned", node=node, cpu_ids=cpus[:row["cpus"]],
                       assigned_at=now, attempt=row["attempt"] + 1,
                       execution={k: config[k] for k in ("python", "scheduler", "env", "required_env") if k in config})
            row.pop("queue_reason", None)
        else:
            row["queue_reason"] = "; ".join(sorted(reasons)) or "No dataset execution nodes registered"


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
    for p in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            args = p.read_bytes().split(b"\0")
            if b"ecarsi" not in args or not Path(os.fsdecode(args[0])).name.startswith("python"):
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


def memory_headroom():
    """Tightest live cgroup headroom, additionally bounded by physical MemAvailable."""
    from .resources import available_memory_bytes
    candidates = [available_memory_bytes()]
    for line in Path("/proc/self/cgroup").read_text().splitlines():
        _, controllers, suffix = line.split(":", 2)
        if controllers == "":
            root, limit, used = Path("/sys/fs/cgroup"), "memory.max", "memory.current"
        elif "memory" in controllers.split(","):
            root, limit, used = Path("/sys/fs/cgroup/memory"), "memory.limit_in_bytes", "memory.usage_in_bytes"
        else:
            continue
        p = root / suffix.lstrip("/")
        while p == root or root in p.parents:
            try:
                candidates.append(max(0, int((p / limit).read_text()) - int((p / used).read_text())))
            except (OSError, ValueError):
                pass
            if p == root:
                break
            p = p.parent
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            candidates.append(int(line.split()[1]) * 1024)
    return min(candidates)


def supervise(config_path, row_id, attempt, directory=None):
    """Own the container and resource locks even if the node agent is interrupted."""
    directory = directory or configuration(config_path)["directory"]
    with queue(directory) as state:
        row = next(r.copy() for r in state["datasets"] if r["id"] == row_id and r["attempt"] == attempt)
        node = state["nodes"][row["node"]].copy()
    config = row["execution"]  # configuration changes apply only to the next admission
    path = receipt_path(directory, row)
    with writer_lock(path.with_suffix(".lock")), writer_lock(Path(row["output"]) / ".batch-driver.lock"):
        if path.exists():
            raise RuntimeError("attempt already has a receipt; inspect it before retrying")
        os.sched_setaffinity(0, row["cpu_ids"])
        record = dict(node=row["node"], state="running", pid=os.getpid(), started_at=time.time())
        env = dict(os.environ, **config["env"])
        for name in config.get("required_env", []):
            if not env.get(name):
                raise ValueError(f"required node environment variable is missing: {name}")
        env.update(ECA_POOL_SCHEDULER=config["scheduler"], OSP_COMPUTE_ENDPOINT="pool", MSP_COMPUTE_ENDPOINT="pool",
                   ECA_RSI_PAUSE_FILE=str(path.with_suffix(".pause")),
                   ECA_DRIVER_MEMORY_BYTES=str(int(row["memory_gb"] * 2**30)),
                   XDG_CACHE_HOME=str(Path(row["output"]) / ".cache"),
                   MPLCONFIGDIR=str(Path(row["output"]) / ".cache/mpl"))
        for k in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMBA_NUM_THREADS", "MSP_MAX_THREADS"):
            env[k] = env["APPTAINERENV_" + k] = str(row["cpus"])
        deadline = min(node["end_time"] - 30, time.time() + row["hours"] * 3600)
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
                    # the allocation's mask, then bind to our reserved, disjoint subset.
                    # Slurm enforces this dataset's memory limit independently.
                    cmd = ["srun", "--jobid=" + str(node["job_id"]), "--nodelist=" + node["host"],
                           "--overlap", "--exact", "--immediate=30", "-N1", "-n1",
                           "-c" + str(node["allocation_cpus"]), "--mem=" + str(math.ceil(row["memory_gb"] * 1024)) + "M",
                           "--cpu-bind=none", "taskset", "-c", ",".join(map(str, row["cpu_ids"])), *cmd]
                child = subprocess.Popen(cmd, cwd="/", env=env, stdin=subprocess.DEVNULL, stdout=log,
                                         stderr=log, start_new_session=True)
                record["child_pid"] = child.pid
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
                        reason = ("Driver time budget or Slurm allocation ending" if time.time() >= deadline else
                                  "Driver stop requested" if stopping else None)
                        if reason:
                            record["reason"] = reason
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
                if child.returncode != 0 or "reason" in record:
                    break
            rc = child.returncode
            record.update(state="paused" if rc == 3 or "reason" in record else "completed" if rc == 0 else "failed",
                          exit_code=rc, finished_at=time.time(), updated_at=time.time())
            write_json(path, record)
    return 0


def dataset_commands(python, row):
    """Keep explicit experiment mappings from existing dataset preparations."""
    base = [python, "-P", "-m", "ecarsi"]
    run = base + ["run", row["input"], row["output"]]
    if row.get("mirror"):
        run += ["--mirror", row["mirror"]]
    if row.get("sample_map"):
        yield run + ["--stop-after", "organize"]
        from .layout import units
        for unit in units(Path(row["output"])):
            yield base + ["persample", str(unit), "--sample-map", row["sample_map"]]
    yield run


def node_agent(config_path, memory_gb, cpus=None):
    import fcntl
    from .pool.slurm import inventory
    config = configuration(config_path)
    for name in config.get("required_env", []):
        if not os.environ.get(name):
            raise ValueError(f"required node environment variable is missing: {name}")
    profile = inventory(int(memory_gb * 2**30), cpus)
    os.sched_setaffinity(0, profile["cpu_ids"])
    identity = f"{profile['host']}/{profile['job_id']}/" + ",".join(map(str, profile["cpu_ids"]))
    locks = []
    lockdir = Path.home() / ".cache/ecarsi-pool" / profile["host"]
    lockdir.mkdir(parents=True, exist_ok=True)
    for cpu in profile["cpu_ids"]:
        lock = (lockdir / f"cpu-{cpu}.lock").open("a")
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.time() >= profile["end_time"]:
                    return
                time.sleep(5)  # existing task/worker keeps its CPU until it exits
        locks.append(lock)
    profile.update(id=identity, draining=False,
                   allocation_cpus=int(re.search(r"(?:^|,)cpu=(\d+)", profile["allocated_tres"])[1]))
    children = {}
    stopping = False
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
                profile.update(inventory(int(memory_gb * 2**30), cpus))
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
                print(f"[batch] inventory stale: {exc}", flush=True)
            refresh = time.monotonic() + 30
        profile.update(memory_headroom=memory_headroom(), draining=stopping)
        pending = []
        with queue(config["directory"]) as state:
            reconcile(state, config["directory"])
            state["nodes"][identity] = profile.copy()
            for row in state["datasets"]:
                child = children.get(row["id"])
                if child and child.poll() is not None:
                    if row["state"] in ACTIVE:
                        row.update(state="paused", reservation_held=True,
                                   reason="Driver supervisor exited without a terminal receipt; resources held pending inspection")
                    children.pop(row["id"])
                if row.get("node") == identity and row["state"] in ACTIVE and row["id"] not in children:
                    # A previous host agent may have died. Never automatically rerun an uncertain attempt.
                    if row.get("launch_requested"):
                        row.update(state="paused", reservation_held=True, reason="Unconfirmed previous execution; inspect before retry")
            assign(state, config)
            for row in state["datasets"]:
                if row.get("node") == identity and row["state"] == "assigned" and not row.get("launch_requested"):
                    row["launch_requested"] = time.time()
                    pending.append(row.copy())
        for row in pending:
            cmd = [sys.executable, "-m", "ecarsi.batch", "supervise", "--config", str(config_path),
                   "--directory", config["directory"], row["id"], str(row["attempt"])]
            try:
                children[row["id"]] = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, start_new_session=True,
                                                      pass_fds=tuple(f.fileno() for f in locks))
            except OSError as exc:
                write_json(receipt_path(config["directory"], row), {"node": identity, "state": "failed", "reason": str(exc), "exit_code": None})
        if (stopping and not children) or time.time() >= profile["end_time"]:
            break
        time.sleep(5)
    with queue(config["directory"]) as state:
        state["nodes"][identity]["draining"] = True


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("submit", "status", "node", "supervise", "retry"):
        a = sub.add_parser(name)
        a.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
        if name == "submit":
            a.add_argument("manifest", type=Path, help='JSON list of {input, output, mirror, cpus, memory_gb, hours}')
        elif name == "node":
            a.add_argument("--memory-gb", type=float, required=True, help="driver budget, excluding compute workers")
            a.add_argument("--cpus", type=int)
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
    if a.command == "submit":
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
    elif a.command == "node":
        node_agent(a.config.resolve(), a.memory_gb, a.cpus)
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
            for key in ("node", "cpu_ids", "execution", "launch_requested", "reason", "exit_code", "finished_at", "rss_bytes", "cpu_percent", "reservation_held"):
                row.pop(key, None)
    return 0


def monitor():
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
                row.setdefault("submitted_at", state.get("submitted_at"))
                row["waiting"] = row["state"] in {"queued", "assigned"} or (
                    not persistent and row["state"] == "paused" and not state.get("runner_finished_at")
                    and not path.with_name("pause").exists())
                result["datasets"].append(row)
            result["nodes"].update(state.get("nodes", {}))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    result["by_mirror"] = {str(Path(r["mirror"]).resolve()): r for r in result["datasets"] if r.get("mirror")}
    return result


if __name__ == "__main__":
    raise SystemExit(main())

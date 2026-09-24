"""Pinned HyperQueue CLI adapter. HQ alone grants CPU and memory resources."""
import ctypes
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import time

from .state import digest, file_digest, lock, pool_root, read, save


# ponytail: bounded native alternatives cover up to 64 cards per worker, including
# workers that join after submission. Raise this protocol limit if hardware needs it.
GPU_SLOT_LIMIT = 64

# How many attempts may share one device. The device slot exists to tell a task which
# card it got, not to serialise the card: what actually bounds concurrency is
# gpuMemoryMB, which every GPU request spends against the device's own VRAM. One shared
# list entry per share keeps both properties. Measured 2026-09-20 on an 80 GiB H100:
# cross-sample integration peaked at 0.7-3.5 GiB and per-sample OSP under 1 GiB, so a
# single-entry slot left the card 95 % idle while four datasets queued behind it.
GPU_SHARES_PER_DEVICE = 8


def device_shares(gpu):
    """Only a card in Default compute mode admits more than one process: under
    Exclusive_Process (the 2026-09-20 Sherlock H100s) the second context fails at once
    with cudaErrorDevicesUnavailable, so sharing such a card kills tasks rather than
    filling it. An unreported mode is treated as exclusive — the safe direction."""
    return GPU_SHARES_PER_DEVICE if gpu.get("compute_mode") == "Default" else 1


def gpu_resources(devices, host_mb=0):
    """Independent device and VRAM pairs, plus one host-memory reserve (`gpuHostMB`) shared by the
    cards that only the GPU alternatives spend: without it 1-CPU work fills the node's RAM and the card idles
    while GPU-preferred tasks queue (sh04-07n12, 2026-09-23)."""
    if len(devices) > GPU_SLOT_LIMIT:
        raise ValueError(f"this resource protocol supports at most {GPU_SLOT_LIMIT} GPUs per worker")
    resources = []
    for slot, gpu in enumerate(devices):
        shares = ",".join(f"{gpu['uuid']}#{share}" for share in range(device_shares(gpu)))
        resources += ["--resource", f"gpuSlot/{slot}=[{shares}]",
                      "--resource", f"gpuMemoryMB/{slot}=sum({int(gpu['memory_mb'] * .9)})"]
    if host_mb:
        resources += ["--resource", f"gpuHostMB=sum({host_mb})"]
    return resources


def parent_death_signal(parent):
    """Native Linux children cannot outlive a crashed component supervisor."""
    def prepare():
        if ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGKILL) != 0:
            raise OSError(ctypes.get_errno(), "PR_SET_PDEATHSIG")
        if os.getppid() != parent:
            os.kill(os.getpid(), signal.SIGKILL)
    return prepare


def runtime_environment(runtime, environment=None):
    env = dict(os.environ if environment is None else environment)
    env["PYTHONUNBUFFERED"] = "1"
    if "pythonpath" in runtime:
        paths = runtime["pythonpath"]
        if not isinstance(paths, list) or not all(isinstance(p, str) and Path(p).is_absolute() and Path(p).is_dir() for p in paths):
            raise ValueError("Runtime Python paths must be explicit existing absolute directories")
        env["PYTHONPATH"] = os.pathsep.join(paths)
        env["PYTHONNOUSERSITE"] = "1"
    return env


def check_runtime(runtime, *, imports=False):
    if runtime.get("image"):
        image = runtime["image"]
        active = os.environ.get("APPTAINER_CONTAINER") or os.environ.get("SINGULARITY_CONTAINER")
        if not active or Path(active).resolve() != Path(image["path"]).resolve():
            raise ValueError("Worker is not running the configured scientific image")
        if imports and file_digest(image["path"]) != image["sha256"]:
            raise ValueError("Scientific image identity mismatch")
    for path, expected in runtime.get("files", {}).items():
        if file_digest(path) != expected:
            raise ValueError("runtime file identity mismatch: " + path)
    probe = subprocess.run(runtime["command"] + ["--version"], capture_output=True,
                           text=True, check=True, timeout=15, env=runtime_environment(runtime))
    if probe.stdout.strip() != runtime["version"]:
        raise ValueError("runtime version mismatch")
    if imports and runtime.get("imports"):
        subprocess.run(runtime["command"] + ["-c", "import importlib,sys; [importlib.import_module(n) for n in sys.argv[1:]]",
            *runtime["imports"]], check=True, timeout=60, env=runtime_environment(runtime))


def check_hq(binary):
    result = subprocess.run([binary, "--version"], capture_output=True, text=True, check=True, timeout=10)
    if result.stdout.strip() != "hyperqueue v0.26.2":
        raise ValueError("this adapter is validated against HyperQueue 0.26.2")


def gpu_jobfile(request, attempt, name, executor, pythonpath):
    """Native HQ alternatives: GPU first; CPU fallback only when declared."""
    spec = request["spec"]
    command = [executor, "-m", "ecarsi.warm_pool.worker", "execute",
               str(attempt.parent.parent.parent), spec["request_id"], request["attempt_id"]]
    fields = dict(command=command, cwd=str(attempt), pin="taskset", crash_limit="never-restart",
                  stdout=str(attempt / "hq-%{INSTANCE_ID}.stdout"), stderr=str(attempt / "hq-%{INSTANCE_ID}.stderr"))
    text = "name = " + json.dumps(name) + "\n[[task]]\n"
    text += "\n".join(k + " = " + json.dumps(v) for k, v in fields.items())
    text += "\nenv = { PYTHONPATH = " + json.dumps(pythonpath) + ', ECA_POOL_GPU_LAYOUT = "slots-v2" }\n'
    resources = {"cpus": spec["cpus"], "mem": spec["memory_mb"], "runtime/" + request["runtime_digest"]: 1}
    gpu_side = {k: v for k, v in resources.items() if k != "mem"}
    variants = [{**gpu_side, f"gpuSlot/{slot}": 1, f"gpuMemoryMB/{slot}": spec["gpu"]["memory_mb"],
                 "gpuHostMB": spec["memory_mb"]} for slot in range(GPU_SLOT_LIMIT)]
    if spec["gpu"]["mode"] == "preferred":
        variants.append(resources)
    for variant in variants:
        text += "\n[[task.request]]\ntime_request = " + json.dumps(str(spec["time_request_seconds"]) + "s")
        text += "\nresources = { " + ", ".join(json.dumps(k) + " = " + json.dumps(v) for k, v in variant.items()) + " }\n"
    return text


def resource_sample(cpu_ids, previous=None, gpu_ids=()):
    """Cheap host measurements on a worker's allocated CPUs, every 30 seconds."""
    counters = {}
    for line in Path("/proc/stat").read_text().splitlines():
        fields = line.split()
        if fields[0].startswith("cpu") and fields[0][3:].isdigit() and int(fields[0][3:]) in cpu_ids:
            values = list(map(int, fields[1:]))
            counters[fields[0]] = (sum(values), values[3] + (values[4] if len(values) > 4 else 0))
    busy = None
    if previous:
        total = sum(counters[c][0] - previous[c][0] for c in counters.keys() & previous.keys())
        idle = sum(counters[c][1] - previous[c][1] for c in counters.keys() & previous.keys())
        if total > 0:
            busy = round(100 * (total - idle) / total, 1)
    memory = {parts[0].rstrip(":"): int(parts[1]) * 1024 for line in Path("/proc/meminfo").read_text().splitlines()
              if (parts := line.split()) and parts[0] in {"MemTotal:", "MemAvailable:"}}
    gpus = []
    try:
        result = subprocess.run(["nvidia-smi", "--query-gpu=uuid,name,utilization.gpu,memory.used,memory.total",
                                 "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=3, check=True)
        for line in result.stdout.splitlines():
            uuid, name, *values = [v.strip() for v in line.split(",")]
            if uuid not in gpu_ids:
                continue
            utilization, used, total = (int(v) if v.isdigit() else None for v in values)
            gpus.append(dict(uuid=uuid, name=name, utilization_percent=utilization, memory_used_mb=used, memory_total_mb=total))
    except (FileNotFoundError, subprocess.SubprocessError, ValueError):
        pass
    return dict(observed_at=time.time(), host=socket.gethostname().split(".")[0], cpu_ids=cpu_ids,
                cpu_percent=busy, memory_used_bytes=memory.get("MemTotal", 0) - memory.get("MemAvailable", 0),
                memory_total_bytes=memory.get("MemTotal"), gpus=gpus), counters


AGENT_CALL_SHARE = "0.125"  # ponytail: a model turn only waits on HTTP; eight share one core


def hq_shares(spec):
    """(--cpus, runtime marker) HQ asks for a request: whole cores for compute, a slice for a model turn.

    A full core per waiting model call starved compute of slots (2026-09-16: 38 of 64 bigmem
    CPUs parked in agent.call while DEG tasks queued). The wrapper still sees one bound core,
    which is what spec["cpus"] == 1 promises."""
    if spec["operation_id"] == "agent.call":
        return AGENT_CALL_SHARE, AGENT_CALL_SHARE
    return str(spec["cpus"]), "1"


def allocation_ended(root, accepted, live_hosts, now=None, grace=300):
    """The Slurm grant that accepted this attempt is over and nothing from that host is connected.

    Nobody else can write the receipt then: the executor died with the node and no worker
    from the host will run reconcile_local. A connected worker on the host keeps ownership."""
    now = time.time() if now is None else now
    host = accepted.get("host")
    if any(str(name).split(".")[0] == host for name in live_hosts):
        return False
    ends = [record.get("expires_at") or float("inf") for path in (Path(root) / "workers").glob("*/identity.json")
            if (record := read(path, {})).get("host") == host]
    return bool(ends) and max(ends) + grace < now


def worker_lost_receipt(request, accepted, error):
    return dict(state="failed", outputs=[], retryable=True, error=error,
                request_id=request["spec"]["request_id"], attempt_id=request["attempt_id"],
                request_digest=request["digest"], runtime_digest=request["runtime_digest"],
                started_at=accepted["started_at"], finished_at=time.time())


RELEASE_DEFAULTS = dict(
    backlog_per_cpu=1 / 3,    # HQ waiting queue kept at ~a third of the pool's CPUs (one tick of starts)
    backlog_min=16,
    drain_age_seconds=600,    # a feasible task waiting this long in HQ starts a drain
    max_drain_seconds=900,    # a drain that has not placed it by then yields for as long again
    gpu_host_fraction=0.5,    # share of a GPU worker's RAM only GPU tasks may use (shared by its cards)
    gpu_task_seconds=120,     # typical GPU / CPU run of a GPU-preferred task (2026-09-23: 1.7 vs 3-10 min)
    cpu_task_seconds=360,
)


def worker_capacity(workers):
    """Live HQ workers as (cpus, memory_mb, gpu_slots) from `hq worker list` JSON."""
    out = []
    for w in workers or []:
        if w.get("ended"):
            continue
        cpus = mem = gpus = 0
        for r in w["configuration"]["resources"]["resources"]:
            if r["name"] == "cpus":
                cpus = len(r.get("values") or [])
            elif r["name"] == "mem":
                mem = r.get("size", 0) / 10000
            elif r["name"].startswith("gpuSlot/"):
                gpus += len(r.get("values") or []) or 1
        out.append((cpus, mem, gpus))
    return out


def release_plan(candidates, *, hq_waiting, cap, draining, gpu_waiting, gpu_slots, gpu_seconds, cpu_seconds):
    """Which held requests go to HQ this tick, and whether a GPU-preferred one goes GPU-only.

    HQ fills a freed core with whatever waiting task fits, so a deep HQ queue of 1-CPU tasks
    starves every wider one, and HQ task priorities crash 0.26.2. Keeping HQ's queue short and
    ordering the rest here gives: interactive work first (model calls and session tools, both
    short and waited on by a model), then FIFO by submission; a drain releases no batch work so cores
    accumulate for an aged wide task; a GPU-preferred task is pinned to the GPU while the GPU queue
    ahead of it would clear sooner than a CPU run takes (earliest finish), else it may take either.
    candidates: dicts with key, klass ('agent'|'tool'|'work'), submitted_at, gpu_preferred."""
    order = {"agent": 0, "tool": 1, "work": 2}
    budget = max(0, cap - hq_waiting)
    plan = []
    for c in sorted(candidates, key=lambda c: (order[c["klass"]], c["submitted_at"])):
        if c["klass"] == "agent":
            plan.append((c["key"], False))
            continue
        if c["klass"] == "work":
            if draining or budget <= 0:
                continue
            budget -= 1
        gpu_only = False
        if c.get("gpu_preferred") and gpu_slots:
            gpu_only = gpu_waiting / gpu_slots * gpu_seconds < cpu_seconds
            gpu_waiting += 1
        plan.append((c["key"], gpu_only))
    return plan


class HyperQueue:
    def __init__(self, root):
        self.root = pool_root(root)
        self.config = read(self.root / "config.json")
        self.command = [self.config["hq"], "--server-dir", str(self.root / "hq"), "--output-mode", "json"]
        self.finished = {}
        # Succeeded/cancelled folders: nothing can change them, not even a retry. That makes the
        # set a durable fact rather than a cache, and the reason to keep it across restarts: a
        # fresh scheduler otherwise stats request.json for every folder in the pool before it can
        # dispatch anything. On 2026-09-20 that cold reconcile ran 33 minutes over 91,856 folders
        # and delayed 21 tasks, 5 of them by 1061 s -- paid on every handover of the control plane.
        self.settled = {(name, ino) for name, ino in read(self.root / "settled.json", [])}
        self._saved = len(self.settled)
        self.release = {**RELEASE_DEFAULTS, **self.config.get("release", {})}
        self.drain_since = self.cooldown_until = None
        self.release_state = {}

    def call(self, *args):
        result = subprocess.run(self.command + list(args), stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=15)
        if result.returncode:
            raise RuntimeError(result.stderr[-1500:] or result.stdout[-1500:])
        return json.loads(result.stdout) if result.stdout.strip() else None

    def _persist_settled(self):
        """One write per tick that adds settled requests, none when nothing settles. The file is
        advisory: a stale or missing entry costs one extra stat, a wrong one cannot skip work that
        is not already sealed, because only a receipt puts a folder in here."""
        if len(self.settled) == self._saved:
            return
        save(self.root / "settled.json", sorted(self.settled))
        self._saved = len(self.settled)

    def _release(self, candidates, jobs, aged, gpu_waiting, hq_workers, generation, now):
        cfg = self.release
        if not candidates and not aged:
            self.drain_since = None
            return []
        capacity = worker_capacity(hq_workers())
        if aged and not (self.cooldown_until and now < self.cooldown_until):
            if self.drain_since is None:
                self.drain_since = now
            if now - self.drain_since > cfg["max_drain_seconds"]:
                self.drain_since, self.cooldown_until = None, now + cfg["max_drain_seconds"]
        else:
            self.drain_since = None
        draining = self.drain_since is not None
        hq_waiting = sum(1 for j in jobs if j["task_stats"]["waiting"])
        cap = max(cfg["backlog_min"], int(sum(c[0] for c in capacity) * cfg["backlog_per_cpu"]))
        gpu_slots = sum(c[2] for c in capacity)
        plan = release_plan(candidates, hq_waiting=hq_waiting, cap=cap, draining=draining,
                            gpu_waiting=gpu_waiting, gpu_slots=gpu_slots,
                            gpu_seconds=cfg["gpu_task_seconds"], cpu_seconds=cfg["cpu_task_seconds"])
        by_key = {c["key"]: c for c in candidates}
        submissions = []
        for key, gpu_only in plan:
            c = by_key[key]
            folder, attempt, request = c["folder"], c["attempt"], c["request"]
            with lock(folder / "request.lock"):
                current = read(folder / "request.json")
                if (not current or current["attempt_id"] != request["attempt_id"] or read(folder / "cancel.json")
                        or read(folder / "backend.json", {}).get("state") == "submitting"):
                    continue
                save(folder / "backend.json", dict(state="submitting", generation=generation, observed_at=time.time()))
            if request["spec"].get("gpu"):
                spec = request["spec"]
                if gpu_only:
                    request = dict(request, spec=dict(spec, gpu=dict(spec["gpu"], mode="required")))
                path = attempt / "job.toml"
                pythonpath = os.pathsep.join(filter(None, (str(Path(__file__).resolve().parents[2]), os.environ.get("PYTHONPATH", ""))))
                path.write_text(gpu_jobfile(request, attempt, c["name"], self.config["executor"], pythonpath))
                submissions.append((folder, ("job", "submit-file", str(path))))
            else:
                submissions.append((folder, c["args"]))
        self.release_state = dict(held=len(candidates) - len(submissions), released=len(submissions),
                                  hq_waiting=hq_waiting, backlog_cap=cap, draining=draining, aged=len(aged),
                                  gpu_slots=gpu_slots, gpu_waiting=gpu_waiting)
        return submissions

    def dispatch(self, info):
        jobs = self.call("job", "list", "--all")
        by_name = {j["name"]: j for j in jobs}
        generation = digest({k: info[k] for k in ("server_uid", "pid", "start_date")})
        seen = []  # `hq worker list`, fetched once per tick and only when something needs it

        def hq_workers():
            if not seen:
                listed = self.call("worker", "list")
                seen.append(listed if isinstance(listed, list) else [])
            return seen[0]
        now = time.time()
        submissions, forgettable, candidates, aged, gpu_waiting = [], [], [], [], 0
        for entry in sorted(os.scandir(self.root / "requests"), key=lambda e: e.name):
            folder, key = Path(entry.path), (entry.name, entry.inode())
            if key in self.settled:
                continue  # 74k saved requests: two stats each per tick was most of a 20 s tick
            try:
                stat = (folder / 'request.json').stat()
            except FileNotFoundError:
                continue
            # A retry atomically replaces request.json. A new HQ generation must
            # revisit receipts to cancel any replayed journal jobs. Neither may
            # be hidden by the completed-attempt cache.
            stamp = (generation, stat.st_ino, stat.st_mtime_ns, stat.st_size,
                     (folder / 'cancel.json').exists())
            if self.finished.get(folder.name) == stamp:
                continue
            self.finished.pop(folder.name, None)
            with lock(folder / "request.lock"):
                request = read(folder / "request.json")
                if not request:
                    continue
                attempt = folder / request["attempt_id"]
                receipt = read(attempt / "receipt.json")
                previous = read(folder / "backend.json", {})
                name = "rsi." + request["spec"]["request_id"] + "." + request["attempt_id"]
                job = by_name.get(name)
                if read(folder / "cancel.json"):
                    accepted = read(attempt / "accepted.json")
                    # HQ escalates cancellation to SIGKILL after one second. An
                    # accepted executor must finish its own descendant cleanup.
                    if job and (receipt or not accepted) and (job["task_stats"]["running"] or job["task_stats"]["waiting"]):
                        self.call("job", "cancel", str(job["id"]))
                    if receipt or not accepted:
                        save(folder / "backend.json", dict(previous, state="cancelled", observed_at=time.time()))
                    if receipt:
                        self.finished[folder.name] = stamp
                        self.settled.add(key)
                    continue
                if receipt:
                    # A persisted result wins over a replayed HQ journal entry.
                    if job and job["task_stats"]["waiting"]:
                        self.call("job", "cancel", str(job["id"]))
                    self.finished[folder.name] = stamp
                    if receipt.get("state") == "succeeded":
                        self.settled.add(key)  # a failed one may be retried: keep watching its stamp
                        if job and not (job["task_stats"]["running"] or job["task_stats"]["waiting"]):
                            forgettable.append(str(job["id"]))
                    continue
                accepted = read(attempt / "accepted.json")
                if accepted and not (job and (job["task_stats"]["running"] or job["task_stats"]["waiting"])):
                    live_hosts = [w["configuration"]["hostname"] for w in hq_workers() if not w.get("ended")]
                    if allocation_ended(self.root, accepted, live_hosts):
                        save(attempt / "receipt.json", worker_lost_receipt(request, accepted,
                             "WorkerLost: the Slurm allocation ended before a completion receipt"))
                        self.finished[folder.name] = stamp
                        continue
                if job:
                    counts = job["task_stats"]
                    state = ("running" if counts["running"] else "queued" if counts["waiting"]
                             else "unknown_external_result")
                    record = dict(state=state, job_id=job["id"], generation=generation, task_stats=counts)
                    spec = request["spec"]
                    if state == "queued":
                        gpu_waiting += bool(spec.get("gpu"))
                        if (previous.get("state") == "queued" and previous.get("generation") == generation
                                and now - previous.get("observed_at", now) > self.release["drain_age_seconds"]
                                and spec["operation_id"] != "agent.call"
                                and (spec.get("gpu") or {}).get("mode", "preferred") == "preferred"
                                and any(c[0] >= spec["cpus"] and c[1] >= spec["memory_mb"]
                                        for c in worker_capacity(hq_workers()))):
                            aged.append(folder.name)
                    if any(previous.get(k) != v for k, v in record.items()):
                        # Each save is an fsync'd write on Lustre; refreshing observed_at
                        # for 150 live jobs every tick cost more than the whole scan.
                        save(folder / "backend.json", dict(record, observed_at=time.time()))
                    continue
                if read(attempt / "accepted.json"):
                    # A lost backend record is not evidence that computation stopped.
                    save(folder / "backend.json", dict(previous, state="unknown_external_result", observed_at=time.time()))
                    continue
                if previous.get("state") in {"submitting", "unknown_external_result"} and previous.get("generation") == generation:
                    continue  # reply may have been lost; reconcile by stable job name
                if read(folder / "cancel.json"):
                    continue
                spec = request["spec"]
                cpu_share, runtime_share = hq_shares(spec)
                # No --priority: HQ 0.26.2 panics in its scheduling solver (workerload.rs:160, index out of
                # bounds) once prioritised tasks meet the GPU jobs' multi-variant requests (2026-09-23 18:44).
                args = ["submit", "--name", name, "--cpus", cpu_share,
                        "--resource", "mem=" + str(spec["memory_mb"]),
                        "--resource", "runtime/" + request["runtime_digest"] + "=" + runtime_share,
                        "--time-request", str(spec["time_request_seconds"]) + "s",
                        "--pin", "taskset", "--crash-limit", "never-restart", "--directives", "off",
                        "--cwd", str(attempt), "--stdout", str(attempt / "hq-%{INSTANCE_ID}.stdout"),
                        "--stderr", str(attempt / "hq-%{INSTANCE_ID}.stderr"),
                        "--env", "PYTHONPATH=" + os.pathsep.join(filter(None, (
                            str(Path(__file__).resolve().parents[2]), os.environ.get("PYTHONPATH", "")))),
                        self.config["executor"], "-m", "ecarsi.warm_pool.worker", "execute",
                        str(self.root), spec["request_id"], request["attempt_id"]]
                klass = ("agent" if spec["operation_id"] == "agent.call" else
                         "tool" if ".tool-" in spec["request_id"] else "work")
                candidates.append(dict(key=folder.name, klass=klass, submitted_at=request["submitted_at"],
                                       gpu_preferred=(spec.get("gpu") or {}).get("mode") == "preferred",
                                       folder=folder, attempt=attempt, request=request, name=name, args=tuple(args)))
        self._persist_settled()
        submissions = self._release(candidates, jobs, aged, gpu_waiting, hq_workers, generation, now)
        if forgettable:
            # The receipt is the record; HQ's copy only makes `job list --all` grow with history.
            try:
                self.call("job", "forget", ",".join(forgettable))
            except (RuntimeError, OSError, ValueError, subprocess.SubprocessError):
                pass  # forgotten next tick, or never: harmless
        if not submissions:
            return
        # Every hq client call costs ~0.25 s. Submitting inline, one flush each, made a
        # tick take 5-11 s under load and every new request wait a median 10 s for its
        # first look. Submit concurrently outside the folder locks, then flush once.
        from concurrent.futures import ThreadPoolExecutor

        def submit_one(item):
            folder, command = item
            try:
                return folder, self.call(*command), None
            except Exception as exc:  # noqa: BLE001 - recorded on the request, resubmitted next tick
                return folder, None, (type(exc).__name__ + ": " + str(exc))[:500]
        with ThreadPoolExecutor(max_workers=8) as workers:
            results = list(workers.map(submit_one, submissions))
        # RSI already persisted acceptance. Flush makes backend lookup
        # survive ordinary restart; wrapper receipts cover later loss.
        self.call("journal", "flush")
        for folder, submitted, error in results:
            with lock(folder / "request.lock"):
                previous = read(folder / "backend.json", {})
                if previous.get("state") != "submitting" or previous.get("generation") != generation:
                    continue  # cancelled or replaced meanwhile; the stable job name reconciles it
                if error:
                    # Not "submitting": that state means a lost reply for a job that may exist,
                    # which the next tick reconciles by name. This one is retried from scratch.
                    save(folder / "backend.json", dict(state="submit_failed", error=error,
                         generation=generation, observed_at=time.time()))
                else:
                    save(folder / "backend.json", dict(state="queued", job_id=submitted["id"],
                         generation=generation, observed_at=time.time()))


def serve(root, host=None):
    backend = HyperQueue(root)
    check_hq(backend.config["hq"])
    stopping = False
    def stop(*_):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    with lock(backend.root / "scheduler.lock", blocking=False):
        server = None
        try:
            with (backend.root / "scheduler.log").open("a") as log:
                server = subprocess.Popen(backend.command + ["server", "start", "--host", host or socket.gethostname(),
                         "--journal", str(backend.root / "journal"), "--journal-flush-period", "1s"],
                         stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
                         preexec_fn=parent_death_signal(os.getpid()))
                while not stopping:
                    if server.poll() is not None:
                        raise RuntimeError("HQ server exited; see scheduler.log")
                    try:
                        info = backend.call("server", "info")
                        scanning = time.monotonic()
                        backend.dispatch(info)
                        save(backend.root / "scheduler.json", dict(pid=os.getpid(), host=socket.gethostname(),
                             backend_pid=info["pid"], observed_at=time.time(), state="running",
                             dispatch_scan_seconds=time.monotonic() - scanning, release=backend.release_state))
                    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as exc:
                        save(backend.root / "scheduler.json", dict(pid=os.getpid(), host=socket.gethostname(),
                             observed_at=time.time(), state="reconciling", error=str(exc)))
                    time.sleep(.2)  # a new request waits half a tick on average before HQ sees it
        finally:
            if server is not None and server.poll() is None:
                # `hq server stop` cancels worker computations. Dropping this
                # connection invokes workers' tested finish-running policy.
                os.killpg(server.pid, signal.SIGKILL)
                server.wait()
            save(backend.root / "scheduler.json", dict(pid=os.getpid(), host=socket.gethostname(),
                 observed_at=time.time(), state="stopped"))


def join(root, cpu_ids, memory_mb, work_dir, allocation_profile=None, time_limit_seconds=None, gpu_ids=()):
    """An independently supervised native HQ worker; local explicit budgets first."""
    from contextlib import ExitStack
    from .worker import reconcile_local
    from .allocation import validate_profile, gpu_device
    backend = HyperQueue(root)
    check_hq(backend.config["hq"])
    if not cpu_ids or len(set(cpu_ids)) != len(cpu_ids) or not set(cpu_ids) <= os.sched_getaffinity(0):
        raise ValueError("worker CPU IDs must be unique and within current affinity")
    if type(memory_mb) is not int or memory_mb <= 0:
        raise ValueError("memory_mb must be a positive integer")
    if time_limit_seconds is not None and (type(time_limit_seconds) is not int or time_limit_seconds <= 0):
        raise ValueError("time_limit_seconds must be a positive integer")
    profile = validate_profile(allocation_profile, cpu_ids, memory_mb)
    gpu_ids = list(gpu_ids)
    if len(set(gpu_ids)) != len(gpu_ids):
        raise ValueError("GPU UUIDs must be unique")
    if profile and set(profile.get("gpu_ids", [])) != set(gpu_ids):
        raise ValueError("GPU must match the actual Slurm step grant")
    gpus = [gpu_device(gpu_id) for gpu_id in gpu_ids]
    os.environ["CUDA_VISIBLE_DEVICES"] = ",".join(gpu_ids)
    expires_at = profile["end_time"] - 60 if profile else None
    if time_limit_seconds is not None:
        expires_at = min(expires_at or float("inf"), time.time() + time_limit_seconds)
    runtime = backend.config["runtime"]
    check_runtime(runtime, imports=True)
    work_dir = Path(work_dir).resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    host = socket.gethostname().split(".")[0]
    worker_id = host + "-" + digest(str(work_dir))[:12]
    telemetry_dir = backend.root / "workers" / worker_id
    telemetry_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    worker_identity = dict(worker_id=worker_id, host=host,
         cpu_ids=cpu_ids, memory_mb=memory_mb, work_dir=str(work_dir),
         slurm_job_id=profile["job_id"] if profile else None,
         allocation=profile, expires_at=expires_at, gpu_ids=gpu_ids, gpus=gpus,
         gpu=gpus[0] if len(gpus) == 1 else None)
    locks = Path.home() / ".cache" / "ecarsi-pool" / host
    locks.mkdir(parents=True, exist_ok=True)
    stopping = False
    def stop(*_):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    with ExitStack() as stack:
        if profile:
            stack.enter_context(lock(locks / f"allocation-{profile['job_id']}.lock", blocking=False))
        owner_lock = work_dir / "owner.lock"
        stack.enter_context(lock(owner_lock, blocking=False))
        registration = dict(pool_root=str(backend.root), cpu_ids=sorted(cpu_ids))
        previous_registration = read(work_dir / "registration.json")
        if previous_registration is not None and previous_registration != registration:
            raise ValueError("worker directory belongs to another pool or CPU slice; use a new directory")
        save(work_dir / "registration.json", registration)
        # Reconcile before replacing a previous memory claim; a dead HQ process
        # can still have a detached, bounded scientific executor.
        while reconcile_local(backend.root, cpu_ids, gpu_ids):
            if stopping or expires_at is not None and time.time() >= expires_at:
                return 0
            time.sleep(1)
        if profile:
            from ecarsi.warm_pool.reservation import reserve
            # Reserve before taking CPU locks so stale legacy entries can prove
            # their old locks are free. The ledger serializes competing claims.
            reserve(profile, "worker:" + str(work_dir), owner_lock, role="worker", worker_directory=work_dir)
        def release_record():
            pending = reconcile_local(backend.root, cpu_ids, gpu_ids)
            state = "waiting_for_previous_execution" if pending else "stopped"
            save(work_dir / "launcher.json", dict(state=state, updated_at=time.time(), requests=pending))
            save(work_dir / "worker.json", dict(state=state, observed_at=time.time(), requests=pending))
        # Runs while our ownership lock still holds, after CPU locks are closed.
        # Uncertain descendants keep the shared memory claim reserved.
        stack.callback(release_record)
        save(work_dir / "launcher.json", dict(state="running", updated_at=time.time()))
        for cpu in sorted(cpu_ids):
            stack.enter_context(lock(locks / f"cpu-{cpu}.lock", blocking=False))
        for gpu_id in sorted(gpu_ids):
            stack.enter_context(lock(locks / (gpu_id + ".lock"), blocking=False))
            previous = read(locks / (gpu_id + ".json"))
            if previous and reconcile_local(pool_root(previous["pool_root"]), [], [gpu_id]):
                raise ValueError("previous GPU executor is still uncertain; keep its reservation")
            save(locks / (gpu_id + ".json"), registration)
        for gpu_id in gpu_ids:
            subprocess.run(runtime["command"] + ["-c", "import cupy as cp,rapids_singlecell; "
                "assert cp.cuda.runtime.getDeviceCount()==1; assert int(cp.arange(8).sum())==28"],
                check=True, timeout=60, env=dict(runtime_environment(runtime), CUDA_VISIBLE_DEVICES=gpu_id))
        reserve = int(memory_mb * backend.release["gpu_host_fraction"]) if gpus else 0
        device_resources = gpu_resources(gpus, reserve)
        save(telemetry_dir / "identity.json", worker_identity)
        os.sched_setaffinity(0, set(cpu_ids))
        log = stack.enter_context((work_dir / "worker.log").open("a"))
        proc = None
        previous_counters, last_sample = None, 0
        try:
            while not stopping and (expires_at is None or time.time() < expires_at):
                if time.monotonic() - last_sample >= 30:
                    try:
                        sample, previous_counters = resource_sample(cpu_ids, previous_counters, gpu_ids)
                        sample["worker_id"] = worker_id
                        day = datetime.fromtimestamp(sample["observed_at"], timezone.utc).strftime("%Y-%m-%d")
                        with (telemetry_dir / (day + ".jsonl")).open("a", encoding="utf-8") as stream:
                            stream.write(json.dumps(sample, separators=(",", ":")) + "\n")
                    except Exception as exc:
                        log.write(f"Resource observation skipped: {type(exc).__name__}: {exc}\n")
                        log.flush()
                    finally:
                        last_sample = time.monotonic()
                if proc is not None and proc.poll() is None:
                    time.sleep(.5)
                    continue  # finish old work before re-registering its CPUs
                pending = reconcile_local(backend.root, cpu_ids, gpu_ids)
                if pending:
                    save(work_dir / "worker.json", dict(state="waiting_for_previous_execution",
                         requests=pending, observed_at=time.time()))
                    time.sleep(1)
                    continue
                try:
                    backend.call("server", "info")
                except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
                    time.sleep(1)
                    continue
                lifetime = []
                if expires_at is not None:
                    remaining = int(expires_at - time.time())
                    if remaining <= 0:
                        break
                    lifetime = ["--time-limit", str(remaining) + "s"]
                proc = subprocess.Popen(backend.command + ["worker", "start", "--manager", "none",
                        "--cpus", "[" + ",".join(map(str, cpu_ids)) + "]", "--detect-resources", "none",
                        "--resource", f"mem=sum({memory_mb - reserve})",
                        "--resource", f"runtime/{digest(runtime)}=sum({len(cpu_ids)})",
                        "--on-server-lost", "finish-running", "--overview-interval", "30s",
                        "--work-dir", str(work_dir)] + lifetime + device_resources,
                        env=dict(os.environ, ECA_POOL_WORKER_ID=worker_id),
                        stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
                        preexec_fn=parent_death_signal(os.getpid()))
                save(work_dir / "worker.json", dict(state="connecting", pid=os.getpid(),
                     hq_pid=proc.pid, cpu_ids=cpu_ids, expires_at=expires_at, observed_at=time.time()))
                time.sleep(1)
        finally:
            if proc is not None and proc.poll() is None:
                # Explicit worker stop is cancellation; scheduler loss isn't.
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()

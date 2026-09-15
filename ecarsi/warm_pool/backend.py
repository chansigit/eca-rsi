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
    text += "\nenv = { PYTHONPATH = " + json.dumps(pythonpath) + " }\n"
    resources = {"cpus": spec["cpus"], "mem": spec["memory_mb"], "runtime/" + request["runtime_digest"]: 1}
    variants = [{**resources, "gpus/nvidia": 1, "gpuMemoryMB": spec["gpu"]["memory_mb"]}]
    if spec["gpu"]["mode"] == "preferred":
        variants.append(resources)
    for variant in variants:
        text += "\n[[task.request]]\ntime_request = " + json.dumps(str(spec["time_request_seconds"]) + "s")
        text += "\nresources = { " + ", ".join(json.dumps(k) + " = " + str(v) for k, v in variant.items()) + " }\n"
    return text


def resource_sample(cpu_ids, previous=None):
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
            utilization, used, total = (int(v) if v.isdigit() else None for v in values)
            gpus.append(dict(uuid=uuid, name=name, utilization_percent=utilization, memory_used_mb=used, memory_total_mb=total))
    except (FileNotFoundError, subprocess.SubprocessError, ValueError):
        pass
    return dict(observed_at=time.time(), host=socket.gethostname().split(".")[0], cpu_ids=cpu_ids,
                cpu_percent=busy, memory_used_bytes=memory.get("MemTotal", 0) - memory.get("MemAvailable", 0),
                memory_total_bytes=memory.get("MemTotal"), gpus=gpus), counters


class HyperQueue:
    def __init__(self, root):
        self.root = pool_root(root)
        self.config = read(self.root / "config.json")
        self.command = [self.config["hq"], "--server-dir", str(self.root / "hq"), "--output-mode", "json"]
        self.finished = {}

    def call(self, *args):
        result = subprocess.run(self.command + list(args), stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=15)
        if result.returncode:
            raise RuntimeError(result.stderr[-1500:] or result.stdout[-1500:])
        return json.loads(result.stdout) if result.stdout.strip() else None

    def dispatch(self, info):
        jobs = self.call("job", "list", "--all")
        by_name = {j["name"]: j for j in jobs}
        generation = digest({k: info[k] for k in ("server_uid", "pid", "start_date")})
        for folder in sorted((self.root / "requests").iterdir()):
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
                    continue
                if receipt:
                    # A persisted result wins over a replayed HQ journal entry.
                    if job and job["task_stats"]["waiting"]:
                        self.call("job", "cancel", str(job["id"]))
                    self.finished[folder.name] = stamp
                    continue
                if job:
                    counts = job["task_stats"]
                    state = ("running" if counts["running"] else "queued" if counts["waiting"]
                             else "unknown_external_result")
                    save(folder / "backend.json", dict(state=state, job_id=job["id"],
                         generation=generation, observed_at=time.time(), task_stats=counts))
                    continue
                if read(attempt / "accepted.json"):
                    # A lost backend record is not evidence that computation stopped.
                    save(folder / "backend.json", dict(previous, state="unknown_external_result", observed_at=time.time()))
                    continue
                if previous.get("state") in {"submitting", "unknown_external_result"} and previous.get("generation") == generation:
                    continue  # reply may have been lost; reconcile by stable job name
                if read(folder / "cancel.json"):
                    continue
                save(folder / "backend.json", dict(state="submitting", generation=generation, observed_at=time.time()))
                spec = request["spec"]
                args = ["submit", "--name", name, "--cpus", str(spec["cpus"]),
                        "--resource", "mem=" + str(spec["memory_mb"]),
                        "--resource", "runtime/" + request["runtime_digest"] + "=1",
                        "--time-request", str(spec["time_request_seconds"]) + "s",
                        "--pin", "taskset", "--crash-limit", "never-restart", "--directives", "off",
                        "--cwd", str(attempt), "--stdout", str(attempt / "hq-%{INSTANCE_ID}.stdout"),
                        "--stderr", str(attempt / "hq-%{INSTANCE_ID}.stderr"),
                        "--env", "PYTHONPATH=" + os.pathsep.join(filter(None, (
                            str(Path(__file__).resolve().parents[2]), os.environ.get("PYTHONPATH", "")))),
                        self.config["executor"], "-m", "ecarsi.warm_pool.worker", "execute",
                        str(self.root), spec["request_id"], request["attempt_id"]]
                if spec.get("gpu"):
                    path = attempt / "job.toml"
                    path.write_text(gpu_jobfile(request, attempt, name, self.config["executor"],
                        os.pathsep.join(filter(None, (str(Path(__file__).resolve().parents[2]), os.environ.get("PYTHONPATH", ""))))))
                    submitted = self.call("job", "submit-file", str(path))
                else:
                    submitted = self.call(*args)
                # RSI already persisted acceptance. Flush makes backend lookup
                # survive ordinary restart; wrapper receipts cover later loss.
                self.call("journal", "flush")
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
                             dispatch_scan_seconds=time.monotonic() - scanning))
                    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as exc:
                        save(backend.root / "scheduler.json", dict(pid=os.getpid(), host=socket.gethostname(),
                             observed_at=time.time(), state="reconciling", error=str(exc)))
                    time.sleep(.5)
        finally:
            if server is not None and server.poll() is None:
                # `hq server stop` cancels worker computations. Dropping this
                # connection invokes workers' tested finish-running policy.
                os.killpg(server.pid, signal.SIGKILL)
                server.wait()
            save(backend.root / "scheduler.json", dict(pid=os.getpid(), host=socket.gethostname(),
                 observed_at=time.time(), state="stopped"))


def join(root, cpu_ids, memory_mb, work_dir, allocation_profile=None, time_limit_seconds=None, gpu_id=None):
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
    if profile and profile.get("gpu_ids", []) != ([gpu_id] if gpu_id else []):
        raise ValueError("GPU must match the actual Slurm step grant")
    gpu = gpu_device(gpu_id) if gpu_id else None
    if gpu:
        os.environ["CUDA_VISIBLE_DEVICES"] = gpu_id
    expires_at = profile["end_time"] - 60 if profile else None
    if time_limit_seconds is not None:
        expires_at = min(expires_at or float("inf"), time.time() + time_limit_seconds)
    runtime = backend.config["runtime"]
    check_runtime(runtime, imports=True)
    if gpu:
        subprocess.run(runtime["command"] + ["-c", "import cupy as cp,rapids_singlecell; "
            "assert cp.cuda.runtime.getDeviceCount()==1; assert int(cp.arange(8).sum())==28"],
            check=True, timeout=60, env=runtime_environment(runtime))
    work_dir = Path(work_dir).resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    host = socket.gethostname().split(".")[0]
    worker_id = host + "-" + digest(str(work_dir))[:12]
    telemetry_dir = backend.root / "workers" / worker_id
    telemetry_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    worker_identity = dict(worker_id=worker_id, host=host,
         cpu_ids=cpu_ids, memory_mb=memory_mb, work_dir=str(work_dir),
         slurm_job_id=profile["job_id"] if profile else None,
         allocation=profile, expires_at=expires_at, gpu_ids=[gpu_id] if gpu else [], gpu=gpu)
    locks = Path.home() / ".cache" / "ecarsi-pool" / host
    locks.mkdir(parents=True, exist_ok=True)
    stopping = False
    def stop(*_):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    with ExitStack() as stack:
        owner_lock = work_dir / "owner.lock"
        stack.enter_context(lock(owner_lock, blocking=False))
        registration = dict(pool_root=str(backend.root), cpu_ids=sorted(cpu_ids))
        previous_registration = read(work_dir / "registration.json")
        if previous_registration is not None and previous_registration != registration:
            raise ValueError("worker directory belongs to another pool or CPU slice; use a new directory")
        save(work_dir / "registration.json", registration)
        # Reconcile before replacing a previous memory claim; a dead HQ process
        # can still have a detached, bounded scientific executor.
        while reconcile_local(backend.root, cpu_ids, [gpu_id] if gpu else []):
            if stopping or expires_at is not None and time.time() >= expires_at:
                return 0
            time.sleep(1)
        if profile:
            from ecarsi.pool.budget import reserve
            # Reserve before taking CPU locks so stale legacy entries can prove
            # their old locks are free. The ledger serializes competing claims.
            reserve(profile, "worker:" + str(work_dir), owner_lock, role="worker", worker_directory=work_dir)
        def release_record():
            pending = reconcile_local(backend.root, cpu_ids, [gpu_id] if gpu else [])
            state = "waiting_for_previous_execution" if pending else "stopped"
            save(work_dir / "launcher.json", dict(state=state, updated_at=time.time(), requests=pending))
            save(work_dir / "worker.json", dict(state=state, observed_at=time.time(), requests=pending))
        # Runs while our ownership lock still holds, after CPU locks are closed.
        # Uncertain descendants keep the shared memory claim reserved.
        stack.callback(release_record)
        save(work_dir / "launcher.json", dict(state="running", updated_at=time.time()))
        for cpu in sorted(cpu_ids):
            stack.enter_context(lock(locks / f"cpu-{cpu}.lock", blocking=False))
        gpu_resources = []
        if gpu:
            stack.enter_context(lock(locks / (gpu_id + ".lock"), blocking=False))
            previous = read(locks / (gpu_id + ".json"))
            if previous and reconcile_local(pool_root(previous["pool_root"]), [], [gpu_id]):
                raise ValueError("previous GPU executor is still uncertain; keep its reservation")
            save(locks / (gpu_id + ".json"), registration)
            gpu_resources = ["--resource", f"gpus/nvidia=[{gpu_id}]", "--resource",
                             f"gpuMemoryMB=sum({int(gpu['memory_mb'] * .9)})"]
        save(telemetry_dir / "identity.json", worker_identity)
        os.sched_setaffinity(0, set(cpu_ids))
        log = stack.enter_context((work_dir / "worker.log").open("a"))
        proc = None
        previous_counters, last_sample = None, 0
        try:
            while not stopping and (expires_at is None or time.time() < expires_at):
                if time.monotonic() - last_sample >= 30:
                    try:
                        sample, previous_counters = resource_sample(cpu_ids, previous_counters)
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
                pending = reconcile_local(backend.root, cpu_ids, [gpu_id] if gpu else [])
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
                        "--resource", f"mem=sum({memory_mb})",
                        "--resource", f"runtime/{digest(runtime)}=sum({len(cpu_ids)})",
                        "--on-server-lost", "finish-running", "--heartbeat", "1s", "--overview-interval", "5s",
                        "--work-dir", str(work_dir)] + lifetime + gpu_resources,
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

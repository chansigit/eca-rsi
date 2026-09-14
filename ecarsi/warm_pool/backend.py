"""Pinned HyperQueue CLI adapter. HQ alone grants CPU and memory resources."""
import ctypes
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


def check_runtime(runtime):
    for path, expected in runtime.get("files", {}).items():
        if file_digest(path) != expected:
            raise ValueError("runtime file identity mismatch: " + path)
    probe = subprocess.run(runtime["command"] + ["--version"], capture_output=True,
                           text=True, check=True, timeout=15)
    if probe.stdout.strip() != runtime["version"]:
        raise ValueError("runtime version mismatch")


def check_hq(binary):
    result = subprocess.run([binary, "--version"], capture_output=True, text=True, check=True, timeout=10)
    if result.stdout.strip() != "hyperqueue v0.26.2":
        raise ValueError("this adapter is validated against HyperQueue 0.26.2")


class HyperQueue:
    def __init__(self, root):
        self.root = pool_root(root)
        self.config = read(self.root / "config.json")
        self.command = [self.config["hq"], "--server-dir", str(self.root / "hq"), "--output-mode", "json"]

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
                continue
            if receipt:
                # A persisted result wins over a replayed HQ journal entry.
                if job and job["task_stats"]["waiting"]:
                    self.call("job", "cancel", str(job["id"]))
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
            with lock(folder / "request.lock"):
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
                        "--env", "PYTHONPATH=" + str(Path(__file__).resolve().parents[2]),
                        self.config["executor"], "-m", "ecarsi.warm_pool.worker", "execute",
                        str(self.root), spec["request_id"], request["attempt_id"]]
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
                        backend.dispatch(info)
                        save(backend.root / "scheduler.json", dict(pid=os.getpid(), host=socket.gethostname(),
                             backend_pid=info["pid"], observed_at=time.time(), state="running"))
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


def join(root, cpu_ids, memory_mb, work_dir):
    """An independently supervised native HQ worker; local explicit budgets first."""
    from contextlib import ExitStack
    from .worker import reconcile_local
    backend = HyperQueue(root)
    check_hq(backend.config["hq"])
    if not cpu_ids or len(set(cpu_ids)) != len(cpu_ids) or not set(cpu_ids) <= os.sched_getaffinity(0):
        raise ValueError("worker CPU IDs must be unique and within current affinity")
    if type(memory_mb) is not int or memory_mb <= 0:
        raise ValueError("memory_mb must be a positive integer")
    runtime = backend.config["runtime"]
    check_runtime(runtime)
    work_dir = Path(work_dir).resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    host = socket.gethostname().split(".")[0]
    locks = Path.home() / ".cache" / "ecarsi-pool" / host
    locks.mkdir(parents=True, exist_ok=True)
    stopping = False
    def stop(*_):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    with ExitStack() as stack:
        for cpu in sorted(cpu_ids):
            stack.enter_context(lock(locks / f"cpu-{cpu}.lock", blocking=False))
        os.sched_setaffinity(0, set(cpu_ids))
        log = stack.enter_context((work_dir / "worker.log").open("a"))
        proc = None
        try:
            while not stopping:
                if proc is not None and proc.poll() is None:
                    time.sleep(.5)
                    continue  # finish old work before re-registering its CPUs
                pending = reconcile_local(backend.root, cpu_ids)
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
                proc = subprocess.Popen(backend.command + ["worker", "start", "--manager", "none",
                        "--cpus", "[" + ",".join(map(str, cpu_ids)) + "]", "--detect-resources", "none",
                        "--resource", f"mem=sum({memory_mb})",
                        "--resource", f"runtime/{digest(runtime)}=sum({len(cpu_ids)})",
                        "--on-server-lost", "finish-running", "--heartbeat", "1s", "--overview-interval", "5s",
                        "--work-dir", str(work_dir)],
                        stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
                        preexec_fn=parent_death_signal(os.getpid()))
                save(work_dir / "worker.json", dict(state="connecting", pid=os.getpid(),
                     hq_pid=proc.pid, cpu_ids=cpu_ids, observed_at=time.time()))
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

"""Host-side inventory/launcher. Only reads Slurm; never acquires/releases jobs."""
from __future__ import annotations

import argparse
import datetime
import fcntl
import json
import os
import re
import signal
import socket
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from ecarsi.resources import available_memory_bytes
from ecarsi.run_state import write_json


def memory_bytes(value):
    match = re.fullmatch(r"([0-9.]+)([KMGT]?)", value)
    if not match:
        raise ValueError(f"unrecognized Slurm memory: {value}")
    return int(float(match[1]) * 1024 ** {"": 2, "K": 1, "M": 2, "G": 3, "T": 4}[match[2]])


def gpu_inventory(xml, visible, granted):
    """Slurm device minors and CUDA/NVML visible ordinals are different namespaces."""
    devices = ET.fromstring(xml).findall("gpu")
    minors = {g.findtext("minor_number"): g.findtext("uuid") for g in devices}
    ordinals = {str(i): g.findtext("uuid") for i, g in enumerate(devices)}
    allowed = {minors.get(x, x) for x in granted.split(",")}
    selected = [ordinals.get(x, x) for x in visible.split(",")]
    if len(set(selected)) != len(selected) or not set(selected) <= allowed:
        raise ValueError("visible GPU UUIDs do not match Slurm's device minors")
    stats = []
    for g in devices:
        if g.findtext("uuid") not in selected:
            continue
        item = {"uuid": g.findtext("uuid"), "name": g.findtext("product_name"), "minor": g.findtext("minor_number")}
        for key, path in (("utilization_percent", "utilization/gpu_util"),
                          ("memory_used_mib", "fb_memory_usage/used"), ("memory_total_mib", "fb_memory_usage/total")):
            try:
                item[key] = float((g.findtext(path) or "N/A").split()[0])
            except ValueError:
                item[key] = None
        stats.append(item)
    return selected, stats


def inventory(memory, cpus=None, gpu=False):
    cgroup = Path("/proc/self/cgroup").read_text()
    jobs = set(re.findall(r"/job_(\d+)(?:/|$)", cgroup, re.M))
    if len(jobs) != 1:
        raise RuntimeError("worker must run inside an existing Slurm job cgroup; SLURM_JOB_ID alone is insufficient")
    job = next(iter(jobs))
    info = subprocess.run(["scontrol", "show", "job", job, "-o"], check=True,
                          capture_output=True, text=True, timeout=15).stdout
    fields = dict(re.findall(r"\b(\w+)=(\S+)", info))
    if fields.get("JobState") != "RUNNING":
        raise RuntimeError("Slurm allocation is not RUNNING")
    host = socket.gethostname().split(".")[0]
    nodes = subprocess.run(["scontrol", "show", "hostnames", fields["NodeList"]], check=True,
                           capture_output=True, text=True, timeout=15).stdout.split()
    if host not in nodes:
        raise RuntimeError("current host is outside the Slurm allocation")
    affinity = sorted(os.sched_getaffinity(0))
    if len(affinity) > int(fields["NumCPUs"]):
        raise RuntimeError("CPU affinity is not constrained to the Slurm grant; launch inside an srun step")
    if cpus is not None:
        if not 1 <= cpus <= len(affinity):
            raise ValueError("requested CPUs exceed this process's Slurm affinity")
        affinity = affinity[:cpus]
    if "MinMemoryNode" in fields:
        slurm_mem = memory_bytes(fields["MinMemoryNode"])
    else:
        slurm_mem = memory_bytes(fields["MinMemoryCPU"]) * len(os.sched_getaffinity(0))
    limit = min(available_memory_bytes(), slurm_mem)
    if not 0 < memory <= int(limit * .9):
        raise ValueError("worker memory must fit within 90% of the Slurm/cgroup memory limit")
    gpu_ids = []
    gpu_stats = []
    if gpu:
        visible = os.environ.get("CUDA_VISIBLE_DEVICES", "")
        granted = os.environ.get("SLURM_STEP_GPUS") or os.environ.get("SLURM_JOB_GPUS")
        if not visible or not granted or visible in {"-1", "NoDevFiles"}:
            raise ValueError("GPU worker needs Slurm GPU IDs and CUDA_VISIBLE_DEVICES; start in a GPU srun step")
        xml = subprocess.run(["nvidia-smi", "-q", "-x"], check=True,
                             capture_output=True, text=True, timeout=15).stdout
        gpu_ids, gpu_stats = gpu_inventory(xml, visible, granted)
    steps = re.findall(r"/step_([^/\n]+)", cgroup)
    return {"job_id": job, "job_name": fields["JobName"], "step_id": steps[0] if steps else None,
            "requested_tres": fields["ReqTRES"], "allocated_tres": fields["AllocTRES"],
            "time_limit": fields["TimeLimit"],
            "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            "host": host, "cpu_ids": affinity, "cpus": len(affinity),
            "memory": memory, "process_memory": limit, "allocation_memory": slurm_mem,
            "gpu_ids": gpu_ids, "gpus": len(gpu_ids), "gpu_stats": gpu_stats,
            "end_time": datetime.datetime.fromisoformat(fields["EndTime"]).timestamp(),
            "observed_at": time.time()}


def process_identity(pid):
    """PIDs are identified by host boot and process birth, never by number alone."""
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        if fields[0] == "Z":
            return None
        return dict(pid=pid, start_ticks=int(fields[19]),
                    boot_id=Path("/proc/sys/kernel/random/boot_id").read_text().strip())
    except FileNotFoundError:
        return None


def exited(proc):
    """Observe exit without reaping: the zombie pins its process-group ID."""
    return os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None


def group_alive(pid):
    for path in Path("/proc").glob("[0-9]*/stat"):
        try:
            fields = path.read_text().rsplit(")", 1)[1].split()
            if int(fields[2]) == pid and fields[0] != "Z":
                return True
        except (OSError, ValueError, IndexError):
            continue
    return False


def stop_worker(proc):
    """Fence the group before reaping its leader, preventing PGID reuse races.

    Callers use exited(), never poll()/wait(), until this function returns.
    """
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + 15
    while group_alive(proc.pid) and time.monotonic() < deadline:
        time.sleep(.1)
    # Also fence descendants when the launcher itself exited first.
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    # SIGKILL can remain pending during kernel I/O. Keep our CPU locks until
    # all live members of the old group are gone, rather than overlap attempts.
    while group_alive(proc.pid):
        time.sleep(1)
    proc.wait()


def supervise(a, profile, locks, stopping):
    """Keep the provisioned worker alive, without acquiring a new Slurm job.

    An exited worker does not imply a successful shutdown: Dask may exit zero
    after losing its scheduler. Only a host stop or allocation expiry stops us.
    """
    path = (a.directory / "inventory.json").resolve()
    health = a.directory / "launcher.json"
    roots = [str(Path(r).resolve(strict=True)) for r in a.root]
    state = dict(pid=os.getpid(), identity=process_identity(os.getpid()), supervisor_protocol=2, restarts=0, state="starting")
    proc = None
    retry_at = 0
    delay = 1
    refresh = 0
    started = 0
    fixed = {k: profile[k] for k in ("host", "job_id", "cpu_ids", "gpu_ids", "memory")}

    def record(**changes):
        state.update(changes, updated_at=time.time(), end_time=profile["end_time"])
        write_json(health, state)

    try:
        while not stopping() and time.time() < profile["end_time"]:
            if proc is not None and exited(proc):
                stop_worker(proc)
                rc = proc.returncode
                proc = None
                if time.monotonic() - started >= 300:
                    delay = 1
                retry_at = time.monotonic() + delay
                state["restarts"] += 1
                record(state="restarting", child_pid=None, last_exit_code=rc,
                       next_retry_at=time.time() + delay)
                print(f"[pool] worker exited {rc}; restarting in {delay}s within the existing Slurm grant", flush=True)
                delay = min(60, delay * 2)
                refresh = retry_at  # revalidate the grant before every new container
            if time.monotonic() >= refresh:
                try:
                    candidate = inventory(profile["memory"], a.cpus, a.gpu)
                    if any(candidate[k] != v for k, v in fixed.items()):
                        raise ValueError("worker resource identity changed; refusing to reuse its locks")
                    profile = candidate
                    write_json(path, dict(profile, roots=roots,
                                          supervisor={k: state[k] for k in ("pid", "restarts", "last_exit_code") if k in state}))
                    state.pop("last_error", None)
                    refresh = time.monotonic() + 30
                except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
                    record(last_error=str(exc))
                    print(f"[pool] inventory unavailable; no new worker starts until revalidated: {exc}", flush=True)
                    refresh = time.monotonic() + 30
                    if proc is None:
                        retry_at = refresh
                        time.sleep(1)
                        continue
            if proc is None and time.monotonic() >= retry_at:
                if stopping() or time.time() >= profile["end_time"]:
                    break
                # Never launch using a stale inventory after a failed refresh.
                if time.time() - profile["observed_at"] > 90 or state.get("last_error"):
                    time.sleep(1)
                    continue
                env = dict(os.environ)
                env["CUDA_VISIBLE_DEVICES"] = ",".join(profile["gpu_ids"])
                env["APPTAINERENV_CUDA_VISIBLE_DEVICES"] = env["CUDA_VISIBLE_DEVICES"]
                if a.gpu:
                    env["APPTAINER_NV"] = "1"
                for k in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMBA_NUM_THREADS", "MSP_MAX_THREADS"):
                    env[k] = env["APPTAINERENV_" + k] = str(profile["cpus"])
                try:
                    proc = subprocess.Popen([a.python, "-m", getattr(a, "service_module", "ecarsi.pool"), "worker", "--scheduler", a.scheduler,
                                             "--inventory", str(path)], env=env, start_new_session=True,
                                            pass_fds=tuple(f.fileno() for f in locks))
                except OSError as exc:
                    retry_at = time.monotonic() + delay
                    record(state="restarting", last_error=str(exc), next_retry_at=time.time() + delay)
                    print(f"[pool] worker launch failed; retrying in {delay}s: {exc}", flush=True)
                    delay = min(60, delay * 2)
                    refresh = retry_at
                    continue
                started = time.monotonic()
                record(state="running", child_pid=proc.pid, child_identity=process_identity(proc.pid), next_retry_at=None)
            time.sleep(1)
    finally:
        if proc is not None:
            stop_worker(proc)
        record(state="stopped", child_pid=None, next_retry_at=None)
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--scheduler", required=True)
    p.add_argument("--directory", type=Path, required=True)
    p.add_argument("--python", required=True, help="worker interpreter or container wrapper")
    p.add_argument("--service-module", default="ecarsi.pool", help="operational pool package; scientific runtime remains in PYTHONPATH")
    p.add_argument("--cpus", type=int, help="first N CPUs in current affinity; use taskset to select others")
    p.add_argument("--memory-gb", type=float, required=True)
    p.add_argument("--gpu", action="store_true")
    p.add_argument("--root", action="append", default=[], help="shared data root, repeatable")
    a = p.parse_args(argv)
    stopping = False

    def stop(*_):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    # Transient Slurm RPC failures must not permanently remove a worker at startup.
    while not stopping:
        try:
            profile = inventory(int(a.memory_gb * 2**30), a.cpus, a.gpu)
            break
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as exc:
            print(f"[pool] initial Slurm query failed; retrying in 30s: {exc}", flush=True)
            for _ in range(30):
                if stopping:
                    return 0
                time.sleep(1)
    else:
        return 0
    a.directory.mkdir(parents=True, exist_ok=True)
    # Locks also prevent our launchers advertising the same CPU in two pools.
    locks = []
    lockdir = Path.home() / ".cache" / "ecarsi-pool" / profile["host"]
    lockdir.mkdir(mode=0o700, parents=True, exist_ok=True)
    for cpu in profile["cpu_ids"]:
        lock = (lockdir / f"cpu-{cpu}.lock").open("a")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        locks.append(lock)
    os.sched_setaffinity(0, profile["cpu_ids"])
    from .budget import reserve
    reserve(profile, "worker:" + str(a.directory.resolve()), lockdir / f"cpu-{profile['cpu_ids'][0]}.lock",
            role="worker", worker_directory=a.directory, owned_cpu_locks=True)
    try:
        return supervise(a, profile, locks, lambda: stopping)
    finally:
        for lock in locks:
            lock.close()


if __name__ == "__main__":
    raise SystemExit(main())

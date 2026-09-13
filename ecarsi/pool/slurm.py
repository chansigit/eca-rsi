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
from pathlib import Path

from ecarsi.resources import available_memory_bytes


def memory_bytes(value):
    match = re.fullmatch(r"([0-9.]+)([KMGT]?)", value)
    if not match:
        raise ValueError(f"unrecognized Slurm memory: {value}")
    return int(float(match[1]) * 1024 ** {"": 2, "K": 1, "M": 2, "G": 3, "T": 4}[match[2]])


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
    if gpu:
        visible = os.environ.get("CUDA_VISIBLE_DEVICES", "")
        granted = os.environ.get("SLURM_STEP_GPUS") or os.environ.get("SLURM_JOB_GPUS")
        if not visible or not granted or visible in {"-1", "NoDevFiles"}:
            raise ValueError("GPU worker needs Slurm GPU IDs and CUDA_VISIBLE_DEVICES; start in a GPU srun step")
        rows = subprocess.run(["nvidia-smi", "--query-gpu=index,uuid", "--format=csv,noheader,nounits"],
                              check=True, capture_output=True, text=True, timeout=15).stdout.splitlines()
        ids = dict(row.replace(" ", "").split(",") for row in rows)
        allowed = {ids.get(x, x) for x in granted.split(",")}
        gpu_ids = [ids.get(x, x) for x in visible.split(",")]
        if len(set(gpu_ids)) != len(gpu_ids) or not set(gpu_ids) <= allowed:
            raise ValueError("visible GPUs are not a unique subset of Slurm's GPU grant")
    steps = re.findall(r"/step_([^/\n]+)", cgroup)
    return {"job_id": job, "step_id": steps[0] if steps else None,
            "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            "host": host, "cpu_ids": affinity, "cpus": len(affinity),
            "memory": memory, "process_memory": limit, "allocation_memory": slurm_mem,
            "gpu_ids": gpu_ids, "gpus": len(gpu_ids),
            "end_time": datetime.datetime.fromisoformat(fields["EndTime"]).timestamp(),
            "observed_at": time.time()}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--scheduler", required=True)
    p.add_argument("--directory", type=Path, required=True)
    p.add_argument("--python", required=True, help="worker interpreter or container wrapper")
    p.add_argument("--cpus", type=int, help="first N CPUs in current affinity; use taskset to select others")
    p.add_argument("--memory-gb", type=float, required=True)
    p.add_argument("--gpu", action="store_true")
    p.add_argument("--root", action="append", default=[], help="shared data root, repeatable")
    a = p.parse_args(argv)
    profile = inventory(int(a.memory_gb * 2**30), a.cpus, a.gpu)
    a.directory.mkdir(parents=True, exist_ok=True)
    # Locks also prevent our launchers advertising the same CPU in two pools.
    locks = []
    lockdir = Path.home() / ".cache" / "ecarsi-pool" / profile["host"]
    lockdir.mkdir(mode=0o700, parents=True, exist_ok=True)
    for cpu in profile["cpu_ids"]:
        lock = (lockdir / f"cpu-{cpu}.lock").open("w")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        locks.append(lock)
    os.sched_setaffinity(0, profile["cpu_ids"])
    path = (a.directory / "inventory.json").resolve()

    def write(profile):
        profile["roots"] = [str(Path(r).resolve(strict=True)) for r in a.root]
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(profile))
        tmp.replace(path)

    write(profile)
    env = dict(os.environ)
    env["CUDA_VISIBLE_DEVICES"] = ",".join(profile["gpu_ids"])
    env["APPTAINERENV_CUDA_VISIBLE_DEVICES"] = env["CUDA_VISIBLE_DEVICES"]
    if a.gpu:
        env["APPTAINER_NV"] = "1"
    for k in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMBA_NUM_THREADS", "MSP_MAX_THREADS"):
        env[k] = env["APPTAINERENV_" + k] = str(profile["cpus"])
    proc = subprocess.Popen([a.python, "-m", "ecarsi.pool", "worker", "--scheduler", a.scheduler,
                             "--inventory", str(path)], env=env, start_new_session=True)
    stopping = False

    def stop(*_):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    refresh = time.monotonic() + 30
    try:
        while proc.poll() is None and not stopping:
            if time.time() >= profile["end_time"]:
                break
            if time.monotonic() >= refresh:
                try:
                    profile = inventory(profile["memory"], a.cpus, a.gpu)
                    write(profile)
                except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
                    print(f"[pool] inventory refresh failed; admission drains after 90s: {exc}", flush=True)
                refresh = time.monotonic() + 30
            time.sleep(1)
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())

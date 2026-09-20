"""Host-side Slurm inventory and process fencing. Only reads Slurm; never acquires or releases jobs."""
from __future__ import annotations

import datetime
import os
import re
import signal
import socket
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from ecarsi.resources import available_memory_bytes


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

"""Read an existing Slurm grant on the host, then enter the scientific runtime."""
import math
import os
from pathlib import Path
import re
import socket
import time

from .state import read, save


def current_job():
    jobs = set(re.findall(r"/job_(\d+)(?:/|$)", Path("/proc/self/cgroup").read_text(), re.M))
    if len(jobs) > 1:
        raise ValueError("ambiguous Slurm cgroup")
    return next(iter(jobs), None)


def validate_profile(path, cpu_ids, memory_mb):
    if path is None:
        if current_job():
            raise ValueError("use slurm-worker from the host inside a Slurm allocation")
        return None
    path = Path(path)
    if path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o022:
        raise ValueError("allocation profile must be owned by this user and not writable by others")
    profile = read(path)
    now = time.time()
    if (profile["job_id"] != current_job() or
            profile["host"] != socket.gethostname().split(".")[0] or
            profile["boot_id"] != Path("/proc/sys/kernel/random/boot_id").read_text().strip()):
        raise ValueError("allocation profile belongs to a different job, host, or boot")
    if not math.isfinite(profile["observed_at"]) or not 0 <= now - profile["observed_at"] <= 90:
        raise ValueError("allocation profile is stale; probe Slurm again on the host")
    if not math.isfinite(profile["end_time"]) or profile["end_time"] <= now + 60:
        raise ValueError("allocation has less than 60 seconds remaining")
    from ecarsi.resources import available_memory_bytes
    if (profile["cpu_ids"] != cpu_ids or not set(cpu_ids) <= os.sched_getaffinity(0) or
            profile["memory"] != memory_mb * 2**20 or
            not 0 < profile["memory"] <= .9 * min(profile["allocation_memory"], available_memory_bytes())):
        raise ValueError("worker budget does not match the current Slurm/cgroup grant")
    return profile


def launch(root, cpu_ids, memory_mb, work_dir, prefix, job_id=None, time_limit_seconds=None):
    from ecarsi.pool.slurm import inventory
    profile = inventory(memory_mb * 2**20)
    if job_id is not None and profile["job_id"] != job_id:
        raise ValueError("current Slurm job differs from --job-id")
    if not cpu_ids or len(set(cpu_ids)) != len(cpu_ids) or not set(cpu_ids) <= set(profile["cpu_ids"]):
        raise ValueError("requested CPU IDs are outside the Slurm grant")
    if prefix[:1] == ["--"]:
        prefix = prefix[1:]
    if not prefix:
        raise ValueError("supply the runtime Python command after --")
    work_dir = Path(work_dir).resolve()
    work_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    # One immutable probe per launch: a simultaneous launcher must not overwrite
    # the profile that another container is still starting with.
    path = work_dir / f"allocation-{os.getpid()}-{time.time_ns()}.json"
    save(path, {**profile, "cpu_ids": cpu_ids, "cpus": len(cpu_ids)})
    command = prefix + ["-m", "ecarsi.warm_pool", "--root", str(Path(root).resolve()), "worker",
                       "--cpus", ",".join(map(str, cpu_ids)), "--memory-mb", str(memory_mb),
                       "--work-dir", str(work_dir), "--allocation-profile", str(path)]
    if time_limit_seconds is not None:
        command += ["--time-limit-seconds", str(time_limit_seconds)]
    os.execvp(command[0], command)

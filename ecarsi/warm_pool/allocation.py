"""Read an existing Slurm grant on the host, then enter the scientific runtime."""
import math
import os
from pathlib import Path
import re
import socket
import subprocess
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


def gpu_process_memory_mb(gpu_id, pid):
    """VRAM held on this card by the attempt's own process tree, or None when the driver
    will not say (older drivers and MIG report no compute apps). A shared card's total is
    not this attempt's bill."""
    result = subprocess.run(["nvidia-smi", "--id=" + gpu_id, "--query-compute-apps=pid,used_gpu_memory",
                             "--format=csv,noheader,nounits"], capture_output=True, text=True, check=True, timeout=10)
    mine, seen = 0, False
    for row in result.stdout.strip().splitlines():
        if not row.strip() or "not supported" in row.lower():
            return None
        process, used = [s.strip() for s in row.split(",")]
        seen = True
        if group_of(int(process)) == group_of(pid):
            mine += int(used)
    return mine if seen or not result.stdout.strip() else None


def group_of(pid):
    try:
        return int(Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()[2])
    except (OSError, IndexError, ValueError):
        return None


def gpu_device(gpu_id):
    if not re.fullmatch(r"GPU-[a-fA-F0-9-]+", gpu_id):
        raise ValueError("GPU identity must be a full NVIDIA UUID")
    result = subprocess.run(["nvidia-smi", "--id=" + gpu_id,
        "--query-gpu=uuid,name,memory.total,memory.used,utilization.gpu,compute_mode", "--format=csv,noheader,nounits"],
        capture_output=True, text=True, check=True, timeout=10)
    rows = result.stdout.strip().splitlines()
    if len(rows) != 1:
        raise ValueError("expected exactly one GPU")
    uuid, name, total, used, utilization, compute_mode = [s.strip() for s in rows[0].split(",")]
    if uuid != gpu_id:
        raise ValueError("GPU UUID mismatch")
    return dict(uuid=uuid, name=name, memory_mb=int(total), used_mb=int(used),
                utilization_percent=int(utilization), compute_mode=compute_mode)


def launch(root, cpu_ids, memory_mb, work_dir, prefix, job_id=None, time_limit_seconds=None, gpu=False):
    from ecarsi.warm_pool.slurm import inventory
    profile = inventory(memory_mb * 2**20, gpu=gpu)
    if gpu and not profile["gpu_ids"]:
        raise ValueError("GPU worker requires a nonempty Slurm GPU grant")
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
    if gpu:
        for gpu_id in profile["gpu_ids"]:
            command += ["--gpu", gpu_id]
        os.environ["APPTAINER_NV"] = "1"
        os.environ["APPTAINERENV_CUDA_VISIBLE_DEVICES"] = ",".join(profile["gpu_ids"])
    # Clean scientific environments still need explicitly allowed agent keys.
    # Inherit them once at launch; never put values in argv or startup records.
    from ecarsi.model_web import PROVIDERS
    for key in {provider[0] for provider in PROVIDERS.values()}:
        if not os.environ.get(key):
            os.environ[key] = shell_variable(key)
        if os.environ.get(key):
            os.environ['APPTAINERENV_' + key] = os.environ[key]
    os.execvp(command[0], command)


def shell_variable(key):
    """Read one exported variable from the user's shell configuration, on the host.

    Workers launched from a shell without the key otherwise make every model
    call source .bashrc inside the clean container, where it stalls for 30 s.
    """
    import subprocess
    script = 'source "$HOME/.bashrc" >/dev/null 2>&1; printf %s "${!1}"'
    try:
        result = subprocess.run(['bash', '--noprofile', '--norc', '-c', script, 'bash', key],
                                capture_output=True, timeout=30, check=True)
    except (subprocess.SubprocessError, OSError):
        return ''
    return result.stdout.decode()

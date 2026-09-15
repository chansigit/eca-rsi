"""Join an existing allocation using the pool's configured scientific runtime."""
import json
import math
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time

from ecarsi.pool.slurm import inventory, process_identity
from .state import lock, pool_root, read, save


def runtime_prefix(runtime, binds=None):
    environment = {"PYTHONPATH": os.pathsep.join(runtime.get("pythonpath", [str(Path(__file__).resolve().parents[2])])),
                   "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1",
                   "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1", "LC_ALL": "C", "LANG": "C"}
    if not runtime.get("image"):
        return ["env", *(f"{k}={v}" for k, v in environment.items()), *runtime["command"]]
    command = ["apptainer", "exec", "--cleanenv"]
    for path in binds if binds is not None else [p for p in ("/scratch", "/oak", "/home", "/lscratch") if Path(p).exists()]:
        command += ["--bind", path]
    for key, value in environment.items():
        command += ["--env", f"{key}={value}"]
    return command + [runtime["image"]["path"], *runtime["command"]]


def allocated_gpus(profile):
    """AllocTRES, not the node's physical GPU count or stale login environment."""
    tres = dict(item.split("=", 1) for item in profile["allocated_tres"].split(",") if "=" in item)
    return int(tres["gres/gpu"]) if "gres/gpu" in tres else sum(
        int(value) for name, value in tres.items() if name.startswith("gres/gpu:"))


def worker_command(root, profile, prefix, work_dir, cpu_ids, memory_mb, gpu):
    use_gpu = allocated_gpus(profile) > 0 if gpu is None else gpu
    if use_gpu and not allocated_gpus(profile):
        raise ValueError("this Slurm allocation has no GPU grant")
    command = [sys.executable, "-m", "ecarsi.warm_pool", "--root", str(root), "slurm-worker",
               "--cpus", ",".join(map(str, cpu_ids)), "--memory-mb", str(memory_mb),
               "--work-dir", str(work_dir), "--job-id", profile["job_id"]]
    if use_gpu:
        command += ["--gpu"]
        visible = os.environ.get("CUDA_VISIBLE_DEVICES", "")
        step_grant = os.environ.get("SLURM_STEP_GPUS") or os.environ.get("SLURM_JOB_GPUS")
        if not step_grant or visible in {"", "-1", "NoDevFiles"}:
            # The existing worker owns one GPU; never silently discard extra grants.
            if allocated_gpus(profile) != 1:
                raise ValueError("multiple GPUs: launch one worker per explicitly granted GPU step")
            command = ["srun", "--jobid=" + profile["job_id"], "--nodelist=" + profile["host"],
                       "--overlap", "--exact", "--nodes=1", "--ntasks=1", "--immediate=15",
                       "--cpus-per-task=" + str(len(cpu_ids)), "--mem=" + str(math.ceil(memory_mb / .9)),
                       "--gpus-per-task=1", *command]
    return command + ["--", *prefix]


def registered_workers(root, prefix):
    # Run HQ in the same runtime as the worker, including on older host glibc.
    code = "import json,sys; from ecarsi.warm_pool.backend import HyperQueue; print(json.dumps(HyperQueue(sys.argv[1]).call('worker','list')))"
    try:
        result = subprocess.run(prefix + ["-c", code, str(root)], capture_output=True, text=True, check=True, timeout=20)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return []  # A temporarily unavailable scheduler must not prevent worker startup.
    return [row for row in json.loads(result.stdout) if row.get("ended") is None]


def add_worker(root, host=None, *, host_python=None, job_id=None, cpu_ids=None, memory_mb=None,
               work_dir=None, gpu=None, binds=None, wait_seconds=120):
    root = pool_root(root)
    if wait_seconds <= 0:
        raise ValueError("wait_seconds must be positive")
    if host:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", host):
            raise ValueError("invalid worker hostname")
        command = ["env", "LC_ALL=C", "LANG=C", "PYTHONPATH=" + str(Path(__file__).resolve().parents[2]),
                   host_python or sys.executable, "-m", "ecarsi.warm_pool", "--root", str(root),
                   "add-worker", "--wait-seconds", str(wait_seconds)]
        for flag, value in (("--job-id", job_id), ("--cpus", ",".join(map(str, cpu_ids)) if cpu_ids is not None else None),
                            ("--memory-mb", memory_mb), ("--work-dir", work_dir)):
            if value is not None:
                command += [flag, str(value)]
        if gpu is not None:
            command += ["--gpu" if gpu else "--no-gpu"]
        for path in binds or []:
            command += ["--bind", path]
        subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", host, shlex.join(command)], check=True)
        return None

    profile = inventory(2**20)
    if job_id is not None and profile["job_id"] != job_id:
        raise ValueError("current Slurm job differs from --job-id")
    if profile["end_time"] <= time.time() + 60:
        raise ValueError("allocation has less than 60 seconds remaining")
    cpu_ids = profile["cpu_ids"] if cpu_ids is None else cpu_ids
    memory_mb = int(profile["process_memory"] * .9) // 2**20 if memory_mb is None else memory_mb
    if not cpu_ids or len(set(cpu_ids)) != len(cpu_ids) or not set(cpu_ids) <= set(profile["cpu_ids"]):
        raise ValueError("requested CPU IDs are outside the Slurm grant")
    if not 0 < memory_mb * 2**20 <= int(profile["process_memory"] * .9):
        raise ValueError("worker memory must fit within 90% of the Slurm/cgroup memory limit")
    runtime = read(root / "config.json")["runtime"]
    prefix = runtime_prefix(runtime, binds)
    explicit_work_dir = work_dir is not None
    work_dir = Path(work_dir).resolve() if work_dir else root / "worker-state" / (profile["host"] + "-" + profile["job_id"])
    command = worker_command(root, profile, prefix, work_dir, cpu_ids, memory_mb, gpu)
    work_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    log_path = work_dir / "startup.log"
    with lock(work_dir / "start.lock"):
        # Native registration also covers workers started through the older CLI.
        identities = {identity["work_dir"]: identity for p in (root / "workers").glob("*/identity.json")
                      if (identity := read(p))}
        for row in registered_workers(root, prefix):
            identity = identities.get(row["configuration"]["work_dir"])
            if (identity and identity["host"] == profile["host"] and identity.get("slurm_job_id") == profile["job_id"]
                    and (not explicit_work_dir or identity["work_dir"] == str(work_dir))
                    and identity["cpu_ids"] == cpu_ids and identity["memory_mb"] == memory_mb
                    and bool(identity.get("gpu_ids")) == (allocated_gpus(profile) > 0 if gpu is None else gpu)):
                existing_dir = Path(identity["work_dir"])
                existing_log = existing_dir / "startup.log"
                return dict(state="online", already_running=True, hq_worker_id=row["id"],
                            host=identity["host"], job_id=identity["slurm_job_id"], cpus=len(identity["cpu_ids"]),
                            memory_mb=identity["memory_mb"], allocated_tres=profile["allocated_tres"],
                            gpu=bool(identity.get("gpu_ids")), work_dir=str(existing_dir),
                            log=str(existing_log if existing_log.exists() else existing_dir / "worker.log"))
        previous = read(work_dir / "startup.json", {})
        owner = previous.get("process")
        already_running = bool(owner and process_identity(owner["pid"]) == owner)
        if already_running and previous["command"] != command:
            raise ValueError("worker is already starting with different settings; do not change its resources while running")
        if not already_running:
            env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2]), LC_ALL="C", LANG="C")
            with log_path.open("a") as stream:
                stream.write("\nStarting worker: " + shlex.join(command) + "\n")
                stream.flush()
                proc = subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL, stdout=stream,
                                        stderr=subprocess.STDOUT, start_new_session=True)
            owner = process_identity(proc.pid)
            save(work_dir / "startup.json", dict(process=owner, command=command, allocation=profile,
                                                cpu_ids=cpu_ids, memory_mb=memory_mb, log=str(log_path)))
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            if not owner or process_identity(owner["pid"]) != owner:
                with log_path.open("rb") as stream:
                    stream.seek(max(0, log_path.stat().st_size - 3000))
                    detail = stream.read().decode(errors="replace")
                raise RuntimeError(f"worker startup failed; see {log_path}\n{detail}")
            if (work_dir / "worker.json").exists():
                for row in registered_workers(root, prefix):
                    if row["configuration"]["work_dir"] == str(work_dir):
                        return dict(state="online", already_running=already_running, host=profile["host"],
                                    job_id=profile["job_id"], hq_worker_id=row["id"], cpus=len(cpu_ids),
                                    memory_mb=memory_mb, allocated_tres=profile["allocated_tres"],
                                    gpu=bool(allocated_gpus(profile)) if gpu is None else gpu,
                                    log=str(log_path), work_dir=str(work_dir))
            time.sleep(1)
        raise TimeoutError(f"worker is still starting; it was left running. Inspect {log_path}; retrying add-worker will not duplicate it")

"""Bounded command execution whose receipt does not depend on scheduler RPC."""
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import traceback

from .backend import check_runtime, parent_death_signal
from .state import digest, file_digest, identifier, lock, pool_root, read, save, sync_directory


def identity(pid):
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        if fields[0] == "Z":
            return None
        return dict(pid=pid, start_ticks=int(fields[19]),
                    boot_id=Path("/proc/sys/kernel/random/boot_id").read_text().strip())
    except FileNotFoundError:
        return None


def registered_worker_id(root, cpus):
    """Resolve a worker by its exclusive CPU grant when HQ omits parent env."""
    host = socket.gethostname().split(".")[0]
    matches = []
    for path in (root / "workers").glob("*/identity.json"):
        record = read(path, {})
        if record.get("host") == host and set(cpus) <= set(record.get("cpu_ids", [])):
            matches.append(record.get("worker_id"))
    return matches[0] if len(matches) == 1 else None


def group_usage(pgid):
    ticks = rss = members = 0
    for path in Path("/proc").glob("[0-9]*/stat"):
        try:
            fields = path.read_text().rsplit(")", 1)[1].split()
            if int(fields[2]) == pgid and fields[0] != "Z":
                members += 1
                ticks += int(fields[11]) + int(fields[12])
                rss += int(fields[21]) * os.sysconf("SC_PAGE_SIZE")
        except (OSError, ValueError, IndexError):
            continue
    return dict(ticks=ticks, rss_bytes=rss, processes=members)


def stop_group(pgid):
    # Call only for this attempt's unreaped child group or a recorded orphan on
    # the same boot. The leader's zombie pins the PGID during normal cleanup.
    for sig, seconds in ((signal.SIGTERM, 2), (signal.SIGKILL, 5)):
        if not group_usage(pgid)["processes"]:
            return
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if not group_usage(pgid)["processes"]:
                return
            time.sleep(.1)
    raise RuntimeError("attempt process group has not stopped; resources remain uncertain")


def reconcile_local(root, cpu_ids, gpu_ids=()):
    """Do not re-advertise CPUs while a previous local execution can be alive."""
    host = socket.gethostname().split(".")[0]
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    pending = []
    cache_path = root / 'cache' / 'local-recovery' / (host + '-' + boot + '.json')
    cache = read(cache_path, {})
    completed = cache.get('completed', {})
    observed = {}
    # Every new executor uses this guard, not just worker reconnections. Share
    # terminal fingerprints across those processes; retries replace request.json.
    # ponytail: retain one stat per request. An active-attempt journal can replace
    # this scan if metadata checks, rather than historical JSON reads, dominate.
    for folder in (root / "requests").iterdir():
        try:
            info = (folder / 'request.json').stat()
        except FileNotFoundError:
            continue
        stamp = [info.st_ino, info.st_mtime_ns, info.st_size]
        if completed.get(folder.name) == stamp:
            observed[folder.name] = stamp
            continue
        request = read(folder / "request.json")
        if not request:
            continue
        attempt = folder / request["attempt_id"]
        if read(attempt / 'receipt.json'):
            observed[folder.name] = stamp
            continue
        accepted = read(attempt / "accepted.json")
        if (not accepted or accepted["host"] != host or not (set(cpu_ids).intersection(accepted["cpu_ids"])
                or set(gpu_ids).intersection(accepted.get("gpu_ids", [])))):
            continue
        try:
            with lock(attempt / "execution.lock", blocking=False):
                if read(attempt / "receipt.json"):
                    continue
                same_boot = accepted["identity"]["boot_id"] == boot
                if same_boot and (identity(accepted["identity"]["pid"]) == accepted["identity"]
                        or accepted.get("pgid") and group_usage(accepted["pgid"])["processes"]):
                    pending.append(request["spec"]["request_id"])
                    continue
                # No live owner, inherited execution lock, or recorded process
                # group remains. Do not start a new numerical attempt here.
                save(attempt / "receipt.json", dict(state="failed", outputs=[], retryable=True,
                     error="WorkerLost: previous local executor stopped without a completion receipt",
                     request_id=request["spec"]["request_id"], attempt_id=request["attempt_id"],
                     request_digest=request["digest"], runtime_digest=request["runtime_digest"],
                     started_at=accepted["started_at"], finished_at=time.time()))
        except BlockingIOError:
            pending.append(request["spec"]["request_id"])
    if observed != completed:
        cache_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            with lock(cache_path.with_suffix('.lock'), blocking=False):
                # A racing writer may only omit newer terminal entries, causing
                # an extra read next time. Never cache an unfinished execution.
                latest = read(cache_path, {})
                if time.time() - latest.get('updated_at', 0) >= 30:
                    save(cache_path, dict(updated_at=time.time(), completed=observed))
        except BlockingIOError:
            pass
    return pending


def assigned_gpu(spec, environment):
    """Only an HQ grant can expose a GPU to a compute subprocess."""
    values = environment.get("HQ_RESOURCE_VALUES_gpus_nvidia", "")
    if not spec.get("gpu"):
        return None
    variant = environment.get("HQ_RESOURCE_VARIANT")
    if environment.get("ECA_POOL_GPU_LAYOUT") == "slots-v1":
        from .backend import GPU_SLOT_LIMIT
        slots = {key: value for key, value in environment.items() if key.startswith("HQ_RESOURCE_VALUES_gpuSlot_")}
        if not slots and spec["gpu"]["mode"] == "preferred" and variant == str(GPU_SLOT_LIMIT):
            return None
        if (variant not in {str(i) for i in range(GPU_SLOT_LIMIT)} or
                len(slots) != 1 or not (values := slots.get("HQ_RESOURCE_VALUES_gpuSlot_" + variant)) or "," in values):
            raise ValueError("GPU grant disagrees with the selected HQ resource alternative")
    else:
        # Already-submitted jobfiles from the original single-GPU protocol.
        required = spec["gpu"]["mode"] == "required" or variant == "0"
        if not values:
            if required or variant != "1":
                raise ValueError("HQ did not provide the requested GPU or a valid CPU alternative")
            return None
        if (variant not in ({None, "0"} if spec["gpu"]["mode"] == "required" else {"0"}) or "," in values):
            raise ValueError("GPU grant disagrees with the selected HQ resource alternative")
    from .allocation import gpu_device
    gpu = gpu_device(values)
    if spec["gpu"]["memory_mb"] > int(gpu["memory_mb"] * .9):
        raise ValueError("GPU request exceeds this device's usable memory")
    return values


def output_receipts(attempt, names):
    root = attempt / "outputs"
    result = []
    for name in names:
        path = root / name
        if path.is_symlink() or not path.is_file() or root not in path.resolve().parents:
            raise ValueError("missing or unsafe output: " + name)
        with path.open("rb") as stream:
            os.fsync(stream.fileno())
        parent = path.parent
        while parent != attempt:
            sync_directory(parent)
            parent = parent.parent
        result.append(dict(path=str(path), size=path.stat().st_size, sha256=file_digest(path)))
    return result


def execute(root, request_id, attempt_id):
    root = pool_root(root)
    folder = root / "requests" / identifier(request_id)
    request = read(folder / "request.json")
    if request is None or request["attempt_id"] != identifier(attempt_id):
        raise ValueError("unknown or superseded attempt")
    attempt = folder / attempt_id
    try:
        with lock(attempt / "execution.lock", blocking=False) as ownership:
            receipt = read(attempt / "receipt.json")
            if receipt:
                return 0 if receipt["state"] == "succeeded" else 1
            if read(attempt / "accepted.json"):
                return 75  # orphan status needs reconciliation, never blind replay
            return run(folder, request, ownership)
    except BlockingIOError:
        return 75  # another transport delivery is executing this same attempt


def run(folder, request, ownership):
    spec, runtime = request["spec"], request["runtime"]
    attempt = folder / request["attempt_id"]
    stopping = False
    def stop(*_):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    proc = None
    started = time.time()
    deadline = time.monotonic() + spec["timeout_seconds"]
    receipt = dict(request_id=spec["request_id"], attempt_id=request["attempt_id"],
                   request_digest=request["digest"], runtime_digest=request["runtime_digest"],
                   started_at=started, state="failed", outputs=[], preflight_seconds={})
    try:
        if digest(spec) != request["digest"] or digest(runtime) != request["runtime_digest"]:
            raise ValueError("request or runtime identity changed")
        cpus = sorted(os.sched_getaffinity(0))
        if len(cpus) != spec["cpus"]:
            raise ValueError("HQ CPU binding does not match the requested CPU count")
        gpu_id = assigned_gpu(spec, os.environ)
        # An HQ task wrapper can itself die while descendants still exist. HQ
        # may already have freed its grant; check locally before new compute.
        phase_started = time.monotonic()
        while reconcile_local(folder.parent.parent, cpus, [gpu_id] if gpu_id else []):
            if read(folder / "cancel.json"):
                receipt["state"] = "cancelled"
                return 0
            if stopping or time.monotonic() >= deadline:
                raise InterruptedError("previous local execution has not released the requested CPUs")
            time.sleep(.5)
        receipt['preflight_seconds']['local_recovery'] = time.monotonic() - phase_started
        phase_started = time.monotonic()
        check_runtime(runtime)
        receipt['preflight_seconds']['runtime_validation'] = time.monotonic() - phase_started
        phase_started = time.monotonic()
        for item in spec["inputs"]:
            if file_digest(item["path"]) != item["sha256"]:
                raise ValueError("input identity mismatch: " + item["path"])
        receipt['preflight_seconds']['input_validation'] = time.monotonic() - phase_started
        with lock(folder / "request.lock"):
            if read(folder / "cancel.json") or stopping:
                receipt["state"] = "cancelled"
                return 0
            accepted = dict(host=socket.gethostname().split(".")[0],
                            worker_id=os.environ.get("ECA_POOL_WORKER_ID") or
                            registered_worker_id(folder.parent.parent, cpus), identity=identity(os.getpid()),
                            cpu_ids=cpus, gpu_ids=[gpu_id] if gpu_id else [],
                            compute_backend="rapids" if gpu_id else "cpu", started_at=time.time())
            save(attempt / "accepted.json", accepted)
        env = {k: v for k, v in os.environ.items() if not k.startswith(("ECA_DRIVER_", "ECA_POOL_"))}
        env.update(MSP_COMPUTE_ENDPOINT="local", OSP_COMPUTE_ENDPOINT="local", CUDA_VISIBLE_DEVICES=gpu_id or "",
                   RSI_COMPUTE_BACKEND="rapids" if gpu_id else "cpu", PYTHONUNBUFFERED="1")
        for key in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMBA_NUM_THREADS"):
            env[key] = str(spec["cpus"])
        from .backend import runtime_environment
        env = runtime_environment(runtime, env)
        if runtime.get("image"):
            cache = folder.parent.parent / "cache" / request["runtime_digest"]
            cache.mkdir(mode=0o700, parents=True, exist_ok=True)
            env["NUMBA_CACHE_DIR"] = str(cache)
        with (attempt / "stdout.log").open("ab", buffering=0) as out, (attempt / "stderr.log").open("ab", buffering=0) as err:
            # Publish the process group before permitting numerical code to
            # spawn descendants. Parent death before this gate opens runs none.
            gate_read, gate_write = os.pipe()
            try:
                gate = ("import os,sys;fd=int(sys.argv[1]);go=os.read(fd,1);os.close(fd);"
                        "sys.exit(75) if go!=b'1' else os.execvpe(sys.argv[2],sys.argv[2:],os.environ)")
                proc = subprocess.Popen([sys.executable, "-c", gate, str(gate_read)] + runtime["command"] + spec["args"],
                             cwd=attempt / "outputs", env=env, stdin=subprocess.DEVNULL,
                             stdout=out, stderr=err, start_new_session=True,
                             pass_fds=(ownership.fileno(), gate_read), preexec_fn=parent_death_signal(os.getpid()))
                accepted["child"] = identity(proc.pid)
                accepted["pgid"] = proc.pid
                save(attempt / "accepted.json", accepted)
                os.write(gate_write, b"1")
            finally:
                os.close(gate_read)
                os.close(gate_write)
            previous, stamp = group_usage(proc.pid), time.monotonic()
            peak = 0
            gpu_usage, gpu_stamp, peak_gpu_mb = None, 0, 0
            while os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is None:
                now = time.monotonic()
                usage = group_usage(proc.pid)
                peak = max(peak, usage["rss_bytes"])
                if gpu_id and now - gpu_stamp >= 5:
                    from .allocation import gpu_device
                    gpu_usage, gpu_stamp = gpu_device(gpu_id), now
                    peak_gpu_mb = max(peak_gpu_mb, gpu_usage["used_mb"])
                    if gpu_usage["used_mb"] > spec["gpu"]["memory_mb"]:
                        raise MemoryError("attempt exceeded its GPU memory budget")
                save(attempt / "usage.json", dict(usage, observed_at=time.time(),
                     cpu_percent=max(0, usage["ticks"] - previous["ticks"]) / os.sysconf("SC_CLK_TCK") / max(.001, now - stamp) * 100,
                     cpu_count=spec["cpus"], reserved_memory_bytes=spec["memory_mb"] * 2**20,
                     memory_enforcement="process-group RSS watchdog", peak_rss_bytes=peak,
                     gpu=gpu_usage, peak_gpu_memory_mb=peak_gpu_mb))
                previous, stamp = usage, now
                if read(folder / "cancel.json"):
                    receipt["state"] = "cancelled"
                    break
                if stopping:
                    raise InterruptedError("worker execution was interrupted")
                if now >= deadline:
                    raise TimeoutError("execution time limit reached")
                if usage["rss_bytes"] > spec["memory_mb"] * 2**20:
                    raise MemoryError("attempt exceeded its RSS budget")
                time.sleep(.25)
            stop_group(proc.pid)
            rc = proc.wait()
            os.fsync(out.fileno())
            os.fsync(err.fileno())
            receipt.update(exit_code=rc, peak_rss_bytes=peak, gpu_ids=[gpu_id] if gpu_id else [],
                           compute_backend="rapids" if gpu_id else "cpu", peak_gpu_memory_mb=peak_gpu_mb)
            if receipt["state"] == "cancelled":
                return 0
            if rc:
                raise RuntimeError(f"command exited with status {rc}")
        receipt["outputs"] = output_receipts(attempt, spec["outputs"])
        receipt["state"] = "succeeded"
        return 0
    except Exception as exc:
        receipt.update(error=type(exc).__name__ + ": " + str(exc), retryable=isinstance(exc, InterruptedError))
        with (attempt / "stderr.log").open("a", encoding="utf-8") as stream:
            traceback.print_exc(file=stream)
        return 1
    finally:
        if proc is not None and proc.returncode is None:
            stop_group(proc.pid)
            proc.wait()
        for name in ("stdout.log", "stderr.log"):
            path = attempt / name
            if path.exists():
                with path.open("rb") as stream:
                    os.fsync(stream.fileno())
        receipt["finished_at"] = time.time()
        save(attempt / "receipt.json", receipt)


if __name__ == "__main__":
    if len(sys.argv) != 5 or sys.argv[1] != "execute":
        raise SystemExit("usage: python -m ecarsi.warm_pool.worker execute ROOT REQUEST ATTEMPT")
    raise SystemExit(execute(*sys.argv[2:]))

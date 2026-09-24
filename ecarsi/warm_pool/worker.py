"""Bounded command execution whose receipt does not depend on scheduler RPC."""
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import traceback

from .backend import check_runtime, parent_death_signal

GPU_BLIND_LIMIT = 120  # seconds nvidia-smi may keep failing before the attempt is given up
from .state import digest, file_digest, identifier, lock, pool_root, read, save, sync_directory



NUMBA_INDEX_LIMIT = 1 << 18   # ponytail: bytes (~200 entries, ~7 s); rotate on size, not on the (unknown) key that keeps missing


def numba_cache(root):
    """The numba cache directory for this node and runtime, rotated once any index outgrows 256 KiB (an index costs ~26 s per MiB to read).

    scanpy's `get._kernels.agg_sum_csr` misses the cache on every call and appends one more
    entry to its index, and each process reads the whole index first: after 7,019 entries on
    sh04-01n17 (2026-09-23) the first aggregate call took 195 s against 1.7 s with an empty
    cache, which made every DEG there ten times slower. A new generation starts empty; all but the
    newest three are removed."""
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    generations = sorted(int(e.name[4:]) for e in os.scandir(root) if e.name.startswith("gen-") and e.name[4:].isdigit()) or [0]
    current = root / f"gen-{generations[-1]}"
    try:
        full = any(f.name.endswith(".nbi") and f.stat().st_size > NUMBA_INDEX_LIMIT
                   for d in os.scandir(current) if d.is_dir() for f in os.scandir(d.path))
    except FileNotFoundError:
        full = False
    if full:
        current = root / f"gen-{generations[-1] + 1}"
        generations.append(generations[-1] + 1)
    current.mkdir(mode=0o700, exist_ok=True)
    # Keep three generations: a task started two rotations ago may still be compiling into its own.
    # Older ones are never read again; a busy node leaves ~10 GB a day on a shared /tmp otherwise.
    for old in generations[:-3]:
        shutil.rmtree(root / f"gen-{old}", ignore_errors=True)
    return current

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


def active_index(root):
    """Attempts accepted on this host that may still own CPUs: one marker file per request."""
    return Path(root) / "cache" / "active" / socket.gethostname().split(".")[0]


def mark_active(root, request_id):
    index = active_index(root)
    index.mkdir(mode=0o700, parents=True, exist_ok=True)
    (index / identifier(request_id)).touch()


def reconcile_local(root, cpu_ids, gpu_ids=(), shared=False):
    """Do not re-advertise CPUs while a previous local execution can be alive.

    Only attempts this host accepted can hold its CPUs, so only the host's active
    index is visited (a full walk of every saved request cost 72 of 534 reserved
    CPU-hours on 2026-09-16). The index is seeded once from a full walk when a host
    first runs this code; run() marks every acceptance afterwards.

    shared=True is a model turn: it shares its core with other live model turns by
    design (fractional HQ slot), so those neighbours are not pending; orphans still are."""
    root = Path(root)
    host = socket.gethostname().split(".")[0]
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    index = active_index(root)
    if not (index / ".seeded").exists():
        index.mkdir(mode=0o700, parents=True, exist_ok=True)
        with lock(index.with_name(index.name + ".seed.lock")):
            if not (index / ".seeded").exists():
                # The walk only exists for a host that accepted attempts before this index
                # did; a host whose only worker directory is the one add-worker just made
                # has none, and must not pay for it. 85k request folders on Lustre kept a
                # fresh node out of the pool for a quarter of an hour, finding nothing
                # (2026-09-20, sh02-06n61). Every later acceptance marks the index directly.
                worked_here = [p for p in (root / "worker-state").glob(host + "-*") if p.is_dir()]
                for folder in (() if len(worked_here) == 1 else (root / "requests").iterdir()):
                    request = read(folder / "request.json")
                    if not request:
                        continue
                    attempt = folder / request["attempt_id"]
                    if read(attempt / "accepted.json", {}).get("host") == host and not read(attempt / "receipt.json"):
                        (index / folder.name).touch()
                (index / ".seeded").touch()
    pending = []
    for marker in index.iterdir():
        if marker.name.startswith("."):
            continue
        folder = root / "requests" / marker.name
        request = read(folder / "request.json")
        if not request:
            marker.unlink(missing_ok=True)
            continue
        attempt = folder / request["attempt_id"]
        if read(attempt / "receipt.json"):
            marker.unlink(missing_ok=True)
            continue
        accepted = read(attempt / "accepted.json")
        if not accepted:
            continue  # accepted on this host but its attempt was replaced; a retry re-marks it
        if accepted["host"] != host or not (set(cpu_ids).intersection(accepted["cpu_ids"])
                or set(gpu_ids).intersection(accepted.get("gpu_ids", []))):
            continue
        neighbour = shared and request["spec"]["operation_id"] == "agent.call"
        try:
            with lock(attempt / "execution.lock", blocking=False):
                if read(attempt / "receipt.json"):
                    marker.unlink(missing_ok=True)
                    continue
                same_boot = accepted["identity"]["boot_id"] == boot
                if same_boot and (identity(accepted["identity"]["pid"]) == accepted["identity"]
                        or accepted.get("pgid") and group_usage(accepted["pgid"])["processes"]):
                    if not neighbour:
                        pending.append(request["spec"]["request_id"])
                    continue
                # No live owner, inherited execution lock, or recorded process
                # group remains. Do not start a new numerical attempt here.
                save(attempt / "receipt.json", dict(state="failed", outputs=[], retryable=True,
                     error="WorkerLost: previous local executor stopped without a completion receipt",
                     request_id=request["spec"]["request_id"], attempt_id=request["attempt_id"],
                     request_digest=request["digest"], runtime_digest=request["runtime_digest"],
                     started_at=accepted["started_at"], finished_at=time.time()))
                marker.unlink(missing_ok=True)
        except BlockingIOError:
            if not neighbour:
                pending.append(request["spec"]["request_id"])
    return pending


def assigned_gpu(spec, environment):
    """Only an HQ grant can expose a GPU to a compute subprocess."""
    values = environment.get("HQ_RESOURCE_VALUES_gpus_nvidia", "")
    if not spec.get("gpu"):
        return None
    variant = environment.get("HQ_RESOURCE_VARIANT")
    if environment.get("ECA_POOL_GPU_LAYOUT") in {"slots-v1", "slots-v2"}:
        from .backend import GPU_SLOT_LIMIT
        slots = {key: value for key, value in environment.items() if key.startswith("HQ_RESOURCE_VALUES_gpuSlot_")}
        if not slots and spec["gpu"]["mode"] == "preferred" and variant == str(GPU_SLOT_LIMIT):
            return None
        if (variant not in {str(i) for i in range(GPU_SLOT_LIMIT)} or
                len(slots) != 1 or not (values := slots.get("HQ_RESOURCE_VALUES_gpuSlot_" + variant)) or "," in values):
            raise ValueError("GPU grant disagrees with the selected HQ resource alternative")
        # slots-v2 hands out one share of the device: "<uuid>#<share>" names the card.
        values = values.split("#", 1)[0]
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


def journal(folder, request, receipt):
    """One line per finished attempt, appended to the executing worker's own daily journal.

    The receipt already holds the timings, but a receipt is one file inside one of a hundred
    thousand request folders: answering "how long did today's tasks take" from receipts means
    walking the whole tree, which is both slow and the thing that stalled the coordinators' Lustre
    client once already. This is the greppable record instead, so the monitor page can stop being
    the archive and only ever ask about the recent past. Per-worker file, so concurrent executors
    never append to the same one; best effort, since a task must not fail over its own bookkeeping.
    """
    try:
        accepted = read(folder / request["attempt_id"] / "accepted.json", {})
        worker_id = accepted.get("worker_id") or accepted.get("host") or "unassigned"
        spec, trace = request["spec"], request["spec"].get("trace") or {}
        started, finished = receipt.get("started_at"), receipt.get("finished_at")
        line = {
            "request_id": spec["request_id"], "attempt_id": request["attempt_id"],
            "operation": spec.get("operation_id"), "state": receipt.get("state"),
            "dataset_id": trace.get("dataset_id"), "workflow_id": trace.get("workflow_id"),
            "unit_id": trace.get("unit_id"),
            "worker_id": worker_id, "host": accepted.get("host") or socket.gethostname(),
            "submitted_at": request.get("submitted_at"), "started_at": started,
            "finished_at": finished,
            "duration_s": round(finished - started, 3) if started and finished else None,
            "queue_wait_s": round(started - request["submitted_at"], 3)
                            if started and request.get("submitted_at") else None,
            "exit_code": receipt.get("exit_code"), "error": receipt.get("error"),
            "cpus": spec.get("cpus"), "memory_mb": spec.get("memory_mb"),
            "peak_rss_bytes": receipt.get("peak_rss_bytes"), "cpu_seconds": receipt.get("cpu_seconds"),
            # What this attempt was actually given, so the line answers on its own and nobody has to
            # come back to the request folder to find out (ui/records.py).
            "sample_id": trace.get("sample_id"), "cpu_ids": accepted.get("cpu_ids"),
            "gpu_ids": accepted.get("gpu_ids") or [],
            "compute_backend": accepted.get("compute_backend"),
        }
        directory = folder.parent.parent / "workers" / identifier(worker_id)
        directory.mkdir(parents=True, exist_ok=True)
        day = time.strftime("%Y-%m-%d", time.gmtime(finished or time.time()))
        with (directory / f"tasks-{day}.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(line, separators=(",", ":")) + "\n")
    except Exception as exc:  # noqa: BLE001 - bookkeeping never decides a task's outcome
        sys.stderr.write(f"task journal skipped: {type(exc).__name__}: {exc}\n")


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
        while reconcile_local(folder.parent.parent, cpus, [gpu_id] if gpu_id else [],
                              shared=spec["operation_id"] == "agent.call"):
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
            mark_active(folder.parent.parent, spec["request_id"])
        env = {k: v for k, v in os.environ.items() if not k.startswith(("ECA_DRIVER_", "ECA_POOL_"))}
        env.update(MSP_COMPUTE_ENDPOINT="local", OSP_COMPUTE_ENDPOINT="local", CUDA_VISIBLE_DEVICES=gpu_id or "",
                   RSI_COMPUTE_BACKEND="rapids" if gpu_id else "cpu", PYTHONUNBUFFERED="1")
        for key in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMBA_NUM_THREADS"):
            env[key] = str(spec["cpus"])
        from .backend import runtime_environment
        env = runtime_environment(runtime, env)
        if runtime.get("image"):
            # Node-local, per runtime: the shared Lustre cache corrupted its index
            # under concurrent writers from several nodes. Costs one recompile per node.
            cache = numba_cache(Path(os.environ.get("L_SCRATCH") or tempfile.gettempdir()) / "rsi-numba" / request["runtime_digest"])
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
            peak, cpu_ticks = 0, 0  # cpu_ticks: positive deltas only; an exiting child drops out of the group sum
            gpu_usage, gpu_stamp, peak_gpu_mb, gpu_blind = None, 0, 0, None
            while os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is None:
                now = time.monotonic()
                usage = group_usage(proc.pid)
                peak = max(peak, usage["rss_bytes"])
                cpu_ticks += max(0, usage["ticks"] - previous["ticks"])
                if gpu_id and now - gpu_stamp >= 5:
                    from .allocation import gpu_device, gpu_process_memory_mb
                    gpu_stamp = now
                    try:
                        gpu_usage, gpu_blind = gpu_device(gpu_id), None
                        # The card is shared: charge this attempt for its own processes,
                        # not for what its neighbours put on the device.
                        gpu_usage = dict(gpu_usage, attempt_mb=gpu_process_memory_mb(gpu_id, proc.pid))
                    except (subprocess.SubprocessError, OSError, ValueError) as exc:
                        # nvidia-smi blocks while the driver is busy; one unreadable sample
                        # must not kill hours of work. Keep the last reading, try again in
                        # five seconds, and only give up once the device stays unreadable.
                        gpu_blind = gpu_blind or now
                        if now - gpu_blind >= GPU_BLIND_LIMIT:
                            raise RuntimeError(f"GPU unreadable for {GPU_BLIND_LIMIT} s: {exc}") from exc
                        print(f"[worker] GPU probe failed, keeping the last reading: {exc}", flush=True)
                    else:
                        mine = gpu_usage["attempt_mb"]
                        peak_gpu_mb = max(peak_gpu_mb, mine if mine is not None else gpu_usage["used_mb"])
                        if peak_gpu_mb > spec["gpu"]["memory_mb"]:
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
            receipt.update(exit_code=rc, peak_rss_bytes=peak, cpu_seconds=round(cpu_ticks / os.sysconf("SC_CLK_TCK"), 2),
                           gpu_ids=[gpu_id] if gpu_id else [],
                           compute_backend="rapids" if gpu_id else "cpu", peak_gpu_memory_mb=peak_gpu_mb)
            if receipt["state"] == "cancelled":
                return 0
            if rc:
                raise RuntimeError(f"command exited with status {rc}")
        receipt["outputs"] = output_receipts(attempt, spec["outputs"])
        receipt["state"] = "succeeded"
        return 0
    except Exception as exc:
        # A host-RSS kill is retryable at a larger budget (check_pool doubles it); a
        # GPU-memory kill is not, its budget came from the device inventory.
        receipt.update(error=type(exc).__name__ + ": " + str(exc),
                       retryable=isinstance(exc, InterruptedError) or isinstance(exc, MemoryError) and "RSS" in str(exc))
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
        journal(folder, request, receipt)


if __name__ == "__main__":
    if len(sys.argv) != 5 or sys.argv[1] != "execute":
        raise SystemExit("usage: python -m ecarsi.warm_pool.worker execute ROOT REQUEST ATTEMPT")
    raise SystemExit(execute(*sys.argv[2:]))

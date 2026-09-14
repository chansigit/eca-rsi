"""Operational CPU/GPU choice for one MSP integration, outside science inputs.

Install before importing an MSP/ZMIP stage. The choice is pinned per outdir so
concurrent ZMIP lineages cannot change one another's compute backend.
"""
from __future__ import annotations

from contextvars import ContextVar
import fcntl
import json
import logging
import math
import os
from pathlib import Path
import runpy
import sys
import tempfile
import time


log = logging.getLogger("msp.compute_policy")
_backend = ContextVar("msp_integration_backend", default=None)
_installed = False
_SIDECAR = ".rsi-compute-backend.json"


def _matrix_bytes(matrix):
    return sum(getattr(getattr(matrix, part, None), "nbytes", 0)
               for part in ("data", "indices", "indptr")) or getattr(matrix, "nbytes", 0)


def gpu_available(ad):
    """Check an immediately admissible, runtime-compatible GPU pool slot."""
    from ecarsi.pool.client import connect, runtime
    from ecarsi.pool.scheduler import PoolScheduler, dispatch

    here = runtime()
    required = ("rapids-singlecell-cu12", "cupy-cuda12x")
    if not all(here.get(name) for name in required):
        return False
    size = _matrix_bytes(ad.X)
    memory = max(512 << 20, size * 8,
                 int(float(os.environ.get("ECA_POOL_TASK_MEMORY_GB", "0")) * 2**30))
    task = {
        "cpus": int(os.environ.get("ECA_POOL_TASK_CPUS", "1")),
        "memory": memory,
        "gpus": 1,
        "seconds": float(os.environ.get("ECA_POOL_TASK_SECONDS", "0")) or max(1, ad.n_obs / 20),
        "runtime": {key: value for key, value in here.items()
                    if key in {"python", "ecarsi", "msp", "numpy", "scipy", "scanpy", "anndata", *required}},
        "required_runtime": list(required),
    }
    with connect(timeout=3) as client:
        state = client.run_on_scheduler(dispatch, "status")
        joined = client.scheduler_info()["workers"]
    now = time.time()
    occupied = {}
    for item in state["tasks"].values():
        if item["state"] in {"granted", "running", "stopping"}:
            occupied.setdefault(item.get("worker"), []).append(item)
    # DEG moves expression data to the GPU; a conservative free-VRAM floor
    # avoids selecting a card already full of another process's allocations.
    vram_needed = max(2 << 30, size * 4)
    for address, worker in state["workers"].items():
        active = occupied.get(address, [])
        if (address not in joined or worker.get("draining") or worker.get("recycle_reason")
                or now - worker["observed_at"] > 90
                or PoolScheduler.fits(task, worker, now)
                or len(active) >= worker.get("task_slots", 1)
                or any(task[key] + sum(item[key] for item in active) > worker[key]
                       for key in ("cpus", "memory", "gpus"))):
            continue
        free = [gpu["memory_total_mib"] - gpu["memory_used_mib"]
                for gpu in worker.get("gpu_stats", [])
                if gpu.get("memory_total_mib") is not None and gpu.get("memory_used_mib") is not None
                and gpu.get("uuid") not in {gpu_id for item in active for gpu_id in item.get("gpu_ids", [])}]
        if free and max(free) * (1 << 20) >= vram_needed:
            return True
    return False


def _read_sidecar(path, outdir):
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise ValueError(f"invalid compute backend record {path}: {exc}") from exc
    if (not isinstance(value, dict) or type(value.get("schema")) is not int or value["schema"] != 1
            or value.get("outdir") != str(outdir)
            or value.get("backend") not in {"cpu", "gpu"}
            or not isinstance(value.get("reason"), str) or not value["reason"]
            or type(value.get("selected_at")) not in (int, float)
            or not math.isfinite(value["selected_at"])):
        raise ValueError(f"invalid compute backend record {path}")
    return value


def _write_sidecar(path, record):
    with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=path.name + ".", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(record, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def _choose(outdir, ad, probe):
    outdir = Path(outdir).resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    sidecar = outdir / _SIDECAR
    with (outdir / ".rsi-compute-backend.lock").open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        if sidecar.exists():
            record = _read_sidecar(sidecar, outdir)
            if ((outdir / "integrated.h5ad").exists()
                    and not (outdir / ".msp-state" / "integrate.pending").exists()):
                # A completed prior generation is being rebuilt because the
                # caller rejected its science receipt; keep its provenance.
                history = outdir / ".msp-history"
                history.mkdir(exist_ok=True)
                os.replace(sidecar, history / f"compute-backend-{time.time_ns()}.json")
                log.info("[compute-policy] %s: completed result rejected; select backend for rebuild", outdir)
            else:
                log.info("[compute-policy] %s: resume %s (%s)", outdir, record["backend"], record["reason"])
                return record["backend"]
        # The callers invoke integrate_adata only after their own science
        # receipt rejected the old result. The function immediately archives
        # integrated.h5ad and recomputes; its mere presence is not a resume.
        if ad.n_obs < 5000:
            choice, reason = "cpu", "small_integration"
        else:
            try:
                available = probe(ad)
            except Exception as exc:
                log.warning("[compute-policy] GPU probe failed for %s: %s", outdir, exc)
                available = False
            choice, reason = ("gpu", "compatible_gpu_ready") if available else ("cpu", "gpu_unavailable")
        _write_sidecar(sidecar, {"schema": 1, "outdir": str(outdir), "backend": choice,
                                 "reason": reason, "selected_at": time.time()})
        log.info("[compute-policy] %s: selected %s (%s)", outdir, choice, reason)
        return choice


def install(probe=gpu_available):
    """Patch only the MSP call-site aliases, before a stage imports them."""
    global _installed
    if _installed:
        return
    from msp import compute, integrate
    from msp.integrate import deg, pipeline

    original_gpu = compute.gpu_requested
    original_integrate = pipeline.integrate_adata

    def gpu_requested():
        selected = _backend.get()
        return original_gpu() if selected is None else selected == "gpu"

    def integrate_adata(*args, **kwargs):
        ad = args[0] if args else kwargs["ad"]
        outdir = args[2] if len(args) > 2 else kwargs["outdir"]
        choice = _choose(outdir, ad, probe)
        token = _backend.set(choice)
        try:
            return original_integrate(*args, **kwargs)
        finally:
            _backend.reset(token)

    compute.gpu_requested = pipeline.gpu_requested = deg.gpu_requested = gpu_requested
    pipeline.integrate_adata = integrate.integrate_adata = integrate_adata
    for module in (sys.modules.get("msp.__main__"), sys.modules.get("zmip.lineage")):
        if module is not None and hasattr(module, "integrate_adata"):
            module.integrate_adata = integrate_adata
    _installed = True


def main(argv=None):
    """Child entry: python -m <runtime>.compute_policy <runtime>.zmip.lineage ARGS..."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in {"zmip.lineage", f"{__package__}.zmip.lineage"}:
        raise SystemExit("expected: compute_policy <runtime>.zmip.lineage ARGS...")
    install()
    sys.argv = [argv[0], *argv[1:]]
    runpy.run_module(argv[0], run_name="__main__")


if __name__ == "__main__":
    main()

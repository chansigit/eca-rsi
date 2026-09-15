"""One OSP subprocess, staged compute/annotation recovery and machine status.

Uses OSP's public Python API with the same explicit options as its CLI.
No scientific implementation is duplicated here.
"""
from __future__ import annotations

import errno
import os
import shutil
import uuid
import sys
import traceback
from pathlib import Path

from . import layout as L
from .osp_contract import COMPUTE_STATE, output_identities, validate_outputs
from .run_state import file_identity, read_json, write_json, writer_lock


def classify_error(exc: Exception, stage: str) -> tuple[str, bool]:
    if isinstance(exc, (ValueError, KeyError, TypeError, AssertionError)):
        return ("input_or_compute" if stage == "compute" else "annotation_contract"), False
    if isinstance(exc, (TimeoutError, ConnectionError)) or (
        isinstance(exc, OSError) and exc.errno in (errno.EAGAIN, errno.ETIMEDOUT, errno.ECONNRESET, errno.ECONNREFUSED)
    ):
        return "transient_infrastructure", True
    # Unknown/runtime/SDK failures remain visible; do not infer transience
    # from traceback text or rerun a known deterministic computation.
    return "unclassified", False


def compute_sample(request, source: Path, outdir: Path):
    """The same OSP science for both local and pooled execution."""
    cfg = request["config"]
    import anndata as ad
    from osp import generate_report, run_one_sample_pipeline
    from osp.report import write_report_context
    from .upstream import validate_matrix

    if file_identity(source / "subset.h5ad") != request["subset_identity"]:
        raise ValueError("subset content changed since driver hand-off")
    a = ad.read_h5ad(source / "subset.h5ad")
    validate_matrix(a)
    if a.n_obs != request["n_cells"] or set(a.obs["eca_sample_id"].astype(str)) != {request["value"]}:
        raise ValueError("subset differs from the requested experiment")
    write_report_context(str(outdir), request.get("context"))
    run_one_sample_pipeline(
        a, sample_label=request["value"], sample_col="eca_sample_id",
        qc_kwargs={"run_scrublet": cfg["scrublet"], "run_decontx": cfg["decontx"]},
        cluster_kwargs={"resolutions": (cfg["resolution"],), "primary_resolution": cfg["resolution"],
                        **({"compute_backend": cfg["compute_backend"]} if "compute_backend" in cfg else {})},
        outdir=str(outdir),
    )
    generate_report(str(outdir))
    validate_outputs(outdir, False)


def compute_attempt(request, source):
    """Only this invocation writes here; even a replay cannot publish twice."""
    source = Path(source)
    outdir = source / ".pool-attempts" / uuid.uuid4().hex
    outdir.mkdir(parents=True)
    shutil.copyfile(source / "input_cells.csv.gz", outdir / "input_cells.csv.gz")
    try:
        compute_sample(request, source, outdir)
        return {"directory": str(outdir), "error": None}
    except Exception as exc:
        traceback.print_exc()
        return {"directory": str(outdir), "error": exc}


def _positive_env_int(name):
    value = os.environ.get(name)
    if value is None:
        return None
    try:
        number = int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if number < 1:
        raise ValueError(f"{name} must be a positive integer")
    return number


def run_compute(request, outdir):
    mode = os.environ.get("OSP_COMPUTE_ENDPOINT", "local")
    if mode == "local":
        return compute_sample(request, outdir, outdir)
    from .pool.client import PoolEndpoint
    from .persample import _estimate_bytes
    root = Path(os.environ.get("ECA_POOL_DATA_ROOT", str(outdir))).resolve()
    if not outdir.resolve().is_relative_to(root):
        raise ValueError("OSP output is outside ECA_POOL_DATA_ROOT")
    osp_cpus = _positive_env_int("OSP_POOL_TASK_CPUS")
    min_cells = _positive_env_int("OSP_POOL_TASK_CPUS_MIN_CELLS")
    task_cpus = int(os.environ.get("ECA_POOL_TASK_CPUS", "1"))
    if osp_cpus is not None and (min_cells is None or request["n_cells"] >= min_cells):
        task_cpus = osp_cpus
    needs = {"cpus": task_cpus,
             "memory": int(float(os.environ.get("ECA_POOL_TASK_MEMORY_GB", "0")) * 2**30) or _estimate_bytes(request["n_cells"]),
             "seconds": float(os.environ.get("ECA_POOL_TASK_SECONDS", "0")) or max(5, request["n_cells"] / 20),
             "roots": [str(root)], "modules": ["ecarsi", "osp"]}
    with PoolEndpoint(mode=mode) as endpoint:
        result = endpoint.submit(compute_attempt, request, str(outdir), needs=needs).result()
    attempt = Path(result["directory"])
    # The driver's existing writer lock owns publication. No worker touches the
    # official output or completion checkpoint, including after a driver dies.
    if result["error"] is None:
        validate_outputs(attempt, False)
        for path in attempt.iterdir():
            if path.name.startswith(".") or path.name in {
                L.RUN_STATE, COMPUTE_STATE, "request.json", "subset.h5ad", "computed.h5ad", "input_cells.csv.gz"
            }:
                continue  # kernel output cannot replace driver-owned state or inputs
            target = outdir / path.name
            if path.is_dir():
                shutil.copytree(path, target, dirs_exist_ok=True)
            else:
                shutil.copyfile(path, target)
        validate_outputs(outdir, False)
        shutil.rmtree(attempt)
    else:
        for name in ("qc_summary.csv", "qc_removed.csv"):
            (outdir / name).unlink(missing_ok=True)
            if (attempt / name).exists():
                shutil.copyfile(attempt / name, outdir / name)
        raise result["error"]


def run(request_path: Path, *, compute_only: bool = False) -> int:
    from .osp_stage import run as run_stage
    return run_stage(request_path, compute_only=compute_only)


def main(argv: list[str] | None = None) -> int:
    import argparse
    from harness_bridge import configure_logging
    configure_logging("ecarsi", "osp", stream=sys.stderr)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("request", type=Path)
    parser.add_argument("--compute-only", action="store_true")
    args = parser.parse_args(argv)
    return run(args.request.resolve(), compute_only=args.compute_only)


if __name__ == "__main__":
    raise SystemExit(main())

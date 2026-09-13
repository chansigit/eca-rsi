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
        cluster_kwargs={"resolutions": (cfg["resolution"],), "primary_resolution": cfg["resolution"]},
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


def run_compute(request, outdir):
    mode = os.environ.get("OSP_COMPUTE_ENDPOINT", "local")
    if mode == "local":
        return compute_sample(request, outdir, outdir)
    from .pool.client import PoolEndpoint
    from .persample import _estimate_bytes
    root = Path(os.environ.get("ECA_POOL_DATA_ROOT", str(outdir))).resolve()
    if not outdir.resolve().is_relative_to(root):
        raise ValueError("OSP output is outside ECA_POOL_DATA_ROOT")
    needs = {"cpus": int(os.environ.get("ECA_POOL_TASK_CPUS", "1")),
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


def run(request_path: Path) -> int:
    request = read_json(request_path)
    outdir = request_path.parent
    cfg = request["config"]
    state_path = outdir / L.RUN_STATE
    with writer_lock(outdir / ".writer.lock"):
        previous = read_json(state_path) if state_path.is_file() else {}
        state = {"schema_version": 1, "identity": request["identity"], "annotate": cfg["annotate"],
                 "attempt": previous.get("attempt", 0) + 1, "state": "running", "exit_code": None,
                 "stage": "compute", "runtime": request["runtime"]}
        write_json(state_path, state)
        stage = "compute"
        try:
            checkpoint = outdir / COMPUTE_STATE
            compute = read_json(checkpoint) if checkpoint.is_file() else None
            reusable = False
            if compute and compute.get("identity") == request["identity"]:
                # Annotation legitimately rewrites clustered.h5ad. A pristine
                # compute snapshot restores it on an annotation-only retry.
                reusable = all((outdir / name).is_file() and file_identity(outdir / name) == ident
                               for name, ident in compute["files"].items())
            if reusable:
                import shutil
                shutil.copyfile(outdir / "computed.h5ad", outdir / "clustered.h5ad")
                validate_outputs(outdir, False)
                print("[osp-worker] verified compute checkpoint; resume annotation", flush=True)
            else:
                run_compute(request, outdir)
                import shutil
                shutil.copyfile(outdir / "clustered.h5ad", outdir / "computed.h5ad")
                names = ("computed.h5ad", "qc_summary.csv", "qc_removed.csv", "input_cells.csv.gz")
                write_json(checkpoint, {"identity": request["identity"], "files": {
                    name: file_identity(outdir / name) for name in names}})
            if cfg["annotate"]:
                stage = "annotation"
                state["stage"] = stage
                write_json(state_path, state)
                from osp.annotate import propose_annotation
                propose_annotation(str(outdir), species=cfg["species"], tissue=cfg["tissue"],
                                   language=cfg["language"], model=cfg["model"], effort=cfg["effort"])
            validation = validate_outputs(outdir, cfg["annotate"])
            state.update(state="complete", exit_code=0, stage="complete", validation=validation,
                         outputs=output_identities(outdir, cfg["annotate"]))
            write_json(state_path, state)
            return 0
        except Exception as exc:
            kind, retryable = classify_error(exc, stage)
            if stage == "compute" and (outdir / "qc_summary.csv").is_file():
                import pandas as pd
                try:
                    qc = pd.read_csv(outdir / "qc_summary.csv", index_col=0).iloc[:, 0]
                    survived = int(qc["n_cells"]) - int(qc["n_low_quality"])
                    if survived < 3:
                        kind, retryable = ("qc_zero_survivors" if survived == 0 else "qc_too_few_survivors"), False
                except (ValueError, KeyError, OSError):
                    pass
            state.update(state="failed", exit_code=1, stage=stage, failure_kind=kind,
                         retryable=retryable, error=f"{type(exc).__name__}: {exc}")
            write_json(state_path, state)
            traceback.print_exc()
            return 1


def main(argv: list[str] | None = None) -> int:
    from harness_bridge import configure_logging
    configure_logging("ecarsi", "osp", stream=sys.stderr)
    args = sys.argv[1:] if argv is None else argv
    return run(Path(args[0]).resolve())


if __name__ == "__main__":
    raise SystemExit(main())

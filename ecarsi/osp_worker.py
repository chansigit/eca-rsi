"""One OSP subprocess, staged compute/annotation recovery and machine status.

Uses OSP's public Python API with the same explicit options as its CLI.
No scientific implementation is duplicated here.
"""
from __future__ import annotations

import errno
import os
import sys
from pathlib import Path

from .osp_contract import validate_outputs
from .run_state import file_identity


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


def run_compute(request, outdir):
    mode = os.environ.get("OSP_COMPUTE_ENDPOINT", "local")
    if mode != "local":
        # The gen-1 Dask pool that served remote OSP compute is gone; gen-2
        # schedules whole stages through the warm pool instead.
        raise ValueError(f"OSP_COMPUTE_ENDPOINT={mode!r} is no longer supported; use 'local'")
    return compute_sample(request, outdir, outdir)


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

"""OSP checkpoint handoff; computation remains in ecarsi.osp_worker."""
from __future__ import annotations

import sys
import traceback
from pathlib import Path

from ecarsi import layout as L
from .osp_contract import COMPUTE_STATE, output_identities, validate_outputs
from ecarsi.run_state import file_identity, read_json, write_json, writer_lock

def run(request_path: Path, *, compute_only: bool = False) -> int:
    from ecarsi import osp_worker
    # The old backed AnnData reader eagerly loads layers during publication.
    # Keep its numerical kernel pinned; use the metadata-only contract here.
    osp_worker.validate_outputs = validate_outputs
    from ecarsi.osp_worker import run_compute, classify_error
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
            if compute_only:
                state.update(state="computed", exit_code=0, stage="compute")
                write_json(state_path, state)
                return 0
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

"""Validate OSP publication and exact per-cell QC conservation."""
from __future__ import annotations

from pathlib import Path

from . import layout as L
from .run_state import file_identity, read_json

INPUT_CELLS = "input_cells.csv.gz"
COMPUTE_STATE = "compute_state.json"
REQUEST = "request.json"


def validate_outputs(outdir: Path, annotate: bool) -> dict:
    import anndata as ad
    import numpy as np
    import pandas as pd

    required = L.PS_CONTRACT + L.PS_QC_CONTRACT + (INPUT_CELLS,)
    if annotate:
        required += ("annotation_proposal.json",)
    for name in required:
        path = outdir / name
        if not path.is_file() or path.stat().st_size == 0:
            raise ValueError(f"missing/empty required OSP file: {name}")
    if "<html" not in (outdir / "report.html").read_text().lower():
        raise ValueError("OSP report is not HTML")
    expected = pd.read_csv(outdir / INPUT_CELLS, dtype=str, keep_default_na=False)["cell_id"]
    removed = pd.read_csv(outdir / "qc_removed.csv", dtype=str, keep_default_na=False)
    if "cell" not in removed or "qc_reason" not in removed:
        raise ValueError("QC removal ledger lacks cell/qc_reason columns")
    if expected.duplicated().any() or removed.cell.duplicated().any():
        raise ValueError("duplicate input/removed cell IDs")
    qc = pd.read_csv(outdir / "qc_summary.csv", index_col=0, dtype=str).iloc[:, 0]
    if not qc.index.is_unique:
        raise ValueError("duplicate QC summary metrics")
    a = ad.read_h5ad(outdir / "clustered.h5ad", backed="r")
    try:
        survivors = set(a.obs_names)
        deleted = set(removed.cell)
        if not a.obs_names.is_unique or a.n_obs < 3 or a.n_vars < 2:
            raise ValueError("invalid clustered dimensions/IDs")
        if survivors & deleted or survivors | deleted != set(expected):
            raise ValueError("QC cell conservation failed: input != survivors disjoint-union removed")
        if int(qc["n_cells"]) != len(expected) or int(qc["n_low_quality"]) != len(deleted):
            raise ValueError("QC summary disagrees with cell ledger")
        if "counts" not in a.layers:
            raise ValueError("clustered H5AD lacks counts")
        if annotate:
            proposal = read_json(outdir / "annotation_proposal.json")
            key = proposal.get("cluster_key")
            if key not in a.obs:
                raise ValueError("proposal cluster_key missing from H5AD")
            entries = proposal.get("clusters", [])
            labels = [str(e["cluster"]) for e in entries]
            actual = a.obs[key].astype(str)
            if len(labels) != len(set(labels)) or set(labels) != set(actual):
                raise ValueError("annotation proposal does not exactly cover clusters")
            for obs_col, prop_col in (("_ann_coarse", "label_coarse"), ("_ann_fine", "label_fine")):
                mapped = actual.map({str(e["cluster"]): e[prop_col] for e in entries})
                if obs_col not in a.obs or mapped.isna().any() or not (mapped.astype(str) == a.obs[obs_col].astype(str)).all():
                    raise ValueError(f"proposal/H5AD label mismatch: {obs_col}")
            if "_qc_action" not in a.obs or not set(a.obs["_qc_action"].astype(str)) <= {"keep", "flag", "drop"}:
                raise ValueError("invalid annotation QC actions")
            # Validate action application without importing the model SDK.
            actions = np.full(a.n_obs, "keep", dtype=object)
            import operator
            ops = {">": operator.gt, ">=": operator.ge, "<": operator.lt, "<=": operator.le, "==": operator.eq}
            for verb in ("flag", "drop"):
                for action in proposal.get("qc_actions", []):
                    if action["action"] != verb:
                        continue
                    mask = (actual == str(action["cluster"])).to_numpy()
                    if action["scope"] == "cells":
                        mask &= ops[action["op"]](a.obs[action["metric"]].to_numpy(float), float(action["value"]))
                    elif action["scope"] != "cluster":
                        raise ValueError("invalid QC action scope")
                    actions[mask] = verb
            if not (actions == a.obs["_qc_action"].astype(str).to_numpy()).all():
                raise ValueError("proposal/H5AD QC actions disagree")
        return {"n_input": len(expected), "n_survived": a.n_obs, "n_removed": len(deleted),
                "qc_summary": qc.fillna("").to_dict()}
    finally:
        a.file.close()


def output_identities(outdir: Path, annotate: bool) -> dict:
    names = L.PS_CONTRACT + L.PS_QC_CONTRACT + (INPUT_CELLS,)
    if annotate:
        names += ("annotation_proposal.json",)
    return {name: file_identity(outdir / name) for name in names}


def is_done(outdir: Path, annotate: bool = False, identity: str | None = None) -> bool:
    try:
        state = read_json(outdir / L.RUN_STATE)
        if state.get("state") != "complete" or state.get("exit_code") != 0 or state.get("annotate") != annotate:
            return False
        if identity is not None and state.get("identity") != identity:
            return False
        if state.get("outputs") != output_identities(outdir, annotate):
            return False
        validate_outputs(outdir, annotate)
        return True
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        return False

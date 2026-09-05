"""File-only ECA-PP schema 2 adapter (0.2.x through the 0.5.x contract)."""
from __future__ import annotations

import os
from pathlib import Path

from .run_state import file_identity, read_json, write_json

RESERVED = ("eca_source_cell_id", "eca_pp_batch", "eca_pp_cell_type", "eca_sample_id")


def normalize(values):
    s = values.astype("string").str.strip()
    return s.mask(s.str.lower().isin(("", "na", "n/a", "nan", "none", "null", "<na>", "missing")))


def current_files(root: Path):
    """Prune only ECA-PP's step-local .history, not arbitrary hidden dirs."""
    for folder, dirs, files in os.walk(root):
        p = Path(folder)
        if p.name in ("standardize", "identify_columns") and ".history" in dirs:
            dirs.remove(".history")
        for name in sorted(files):
            yield p / name


def discover(root: Path) -> tuple[list[dict], list[Path]]:
    files = set(current_files(root))
    steps = sorted({p.parent for p in files if p.parent.name == "standardize"})
    units, claimed = [], set()
    for step in steps:
        d = step.parent
        h5, rj = step / "standardized.h5ad", step / "result.json"
        name = (root.parent.name if root.name in ("input", "data") else root.name) if d == root else d.name
        u = {"name": name, "dir": str(d), "h5ad": str(h5), "standardize_result": str(rj)}
        ic = d / "identify_columns" / "result.json"
        if ic in files:
            u["identify_columns_result"] = str(ic)
        units.append(u)
        claimed.add(h5)
    if len({u["name"] for u in units}) != len(units):
        raise ValueError("duplicate source directory names; rename them before organize")
    return units, sorted(p for p in files if p.suffix == ".h5ad" and p not in claimed)


def result_state(result: dict, step: str) -> str:
    if result.get("schema_version") != 2 or result.get("step") != step:
        raise ValueError(f"unsupported result schema/step: {result.get('schema_version')}/{result.get('step')}")
    if not result.get("step_version"):
        raise ValueError("result lacks step_version")
    pair = result.get("status"), result.get("exit_code")
    if pair in (("ok", 0), ("needs_review", 0)):
        return "accepted"
    if pair == ("rejected", 2):
        return "rejected"
    raise ValueError(f"upstream input not ready: status={pair[0]}, exit_code={pair[1]}")


def validate_matrix(a, result: dict | None = None) -> None:
    import numpy as np
    from scipy import sparse

    if a.n_obs == 0 or a.n_vars == 0 or not a.obs_names.is_unique or not a.var_names.is_unique:
        raise ValueError("matrix must be nonempty with unique cell and gene IDs")
    if "counts" not in a.layers:
        raise ValueError("required raw counts layer is missing; X is not a counts fallback")
    counts = a.layers["counts"]
    for start in range(0, a.n_obs, 4096):
        chunk = counts[start:start + 4096]
        values = chunk.data if sparse.issparse(chunk) else np.asarray(chunk)
        if not np.isfinite(values).all() or (values < 0).any():
            raise ValueError("counts contain negative or non-finite values")
    if result:
        metrics = result.get("metrics") or {}
        if (metrics.get("n_cells"), metrics.get("n_vars")) != a.shape:
            raise ValueError(f"result dimensions disagree with H5AD: {a.shape}")
        if not (result.get("species") or {}).get("resolved"):
            raise ValueError("upstream species is unresolved")


def column_values(obs, designation, folder: Path):
    """Resolve old/current colspecs before concat changes original barcodes."""
    import pandas as pd

    if designation is None:
        return None, None
    spec = designation if isinstance(designation, str) else designation.get("value")
    kind = "existing" if isinstance(designation, str) else designation.get("kind")
    if kind == "existing":
        if spec not in obs:
            raise ValueError(f"upstream column does not exist: {spec!r}")
        return normalize(obs[spec]), None
    if kind != "derived" or not isinstance(spec, str):
        raise ValueError(f"unsupported column designation: {designation}")
    # Bind to the current step directory even when JSON retains a pre-move path.
    path = folder / Path(spec).name
    df = pd.read_csv(path, sep="\t", header=None, dtype=str, keep_default_na=False)
    if df.shape[1] != 2:
        raise ValueError(f"TSV needs two columns: {path}")
    df.columns = ["cell_id", "value"]
    if len(df) and list(df.iloc[0]) == ["cell_id", "value"]:
        df = df.iloc[1:]
    if df.cell_id.duplicated().any() or (df.cell_id == "").any():
        raise ValueError(f"TSV contains duplicate/empty cell IDs: {path}")
    if set(df.cell_id) != set(obs.index.astype(str)):
        raise ValueError(f"TSV cell coverage differs from source: {path}")
    s = df.set_index("cell_id").value.reindex(obs.index.astype(str))
    s.index = obs.index
    return normalize(s), path


def load_evidence(unit: dict, obs):
    evidence, values, files = {}, {}, {}
    if "identify_columns_result" in unit:
        path = Path(unit["identify_columns_result"])
        evidence = read_json(path)
        if result_state(evidence, "identify_columns") != "accepted":
            raise ValueError("identify-columns result was rejected; resolve or remove that optional result")
        for role in ("batch", "cell_type"):
            s, tsv = column_values(obs, (evidence.get("columns") or {}).get(role), path.parent)
            if s is not None:
                values[f"eca_pp_{role}"] = s
            if tsv is not None:
                files[role] = {"path": str(tsv), "identity": file_identity(tsv)}
    return evidence, values, files


def inspect_unit(unit: dict) -> dict:
    import anndata as ad

    result = read_json(Path(unit["standardize_result"]))
    state = result_state(result, "standardize")
    record = {**unit, "state": state, "standardize": result, "files": {
        "standardize_result": file_identity(Path(unit["standardize_result"]))}}
    h5 = Path(unit["h5ad"])
    if state == "rejected":
        if result.get("output") or h5.exists():
            raise ValueError("rejected source retains a declared/current output")
        return record
    if not isinstance(result.get("output"), str) or Path(result["output"]).name != h5.name:
        raise ValueError("result does not declare standardized.h5ad output")
    a = ad.read_h5ad(h5, backed="r")
    try:
        validate_matrix(a, result)
        if set(RESERVED) & set(a.obs.columns) or "source_unit" in a.obs:
            raise ValueError("source contains reserved RSI obs columns")
        evidence, _, files = load_evidence(unit, a.obs)
        record.update(identify_columns=evidence, derived_files=files)
    finally:
        a.file.close()
    record["files"]["h5ad"] = file_identity(h5)
    if "identify_columns_result" in unit:
        record["files"]["identify_columns_result"] = file_identity(Path(unit["identify_columns_result"]))
    return record


def snapshot(record: dict, target: Path) -> None:
    """Self-contained result JSON, derived TSV and complete source obs metadata."""
    import anndata as ad
    import shutil

    target.mkdir(parents=True, exist_ok=True)
    write_json(target / "standardize.json", record["standardize"])
    write_json(target / "identify_columns.json", record.get("identify_columns", {}))
    a = ad.read_h5ad(record["h5ad"], backed="r")
    try:
        _, values, _ = load_evidence(record, a.obs)
        obs = a.obs.copy()
        for col, s in values.items():
            obs[col] = s
        obs.to_csv(target / "source_obs.csv.gz", index_label="cell_id")
    finally:
        a.file.close()
    for role, f in record.get("derived_files", {}).items():
        shutil.copyfile(f["path"], target / f"{role}.tsv")


def verify_snapshots(input_dir: Path, manifest: dict) -> None:
    for source, entry in manifest.get("upstream", {}).items():
        for name, identity in entry.get("snapshot_files", {}).items():
            if file_identity(input_dir / entry["dir"] / name) != identity:
                raise ValueError(f"upstream snapshot changed: {source}/{name}")

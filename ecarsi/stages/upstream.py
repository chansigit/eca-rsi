"""File-only ECA-PP schema 2 adapter (0.2.x through the 0.5.x contract)."""
from __future__ import annotations

import json
import os
from pathlib import Path

from ..contracts import check
from ..sample_mapping import normalize
from ..run_state import file_identity, read_json, write_json

RESERVED = ("eca_source_cell_id", "eca_pp_batch", "eca_pp_cell_type", "eca_pp_library", "eca_sample_id")
# ECA-PP identify-columns names each source's per-sample QC unit (0.5.4, decision 0016).
SAMPLE_UNITS = ("library", "batch", "whole", "stop")
# ECA-PP's sample column may leave up to this share of a source's cells blank; build_mapping drops them as a
# policy exclusion (owner 2026-10-08, #56: Li2019_skin's patient column, 5.5 % blank). More: the agent decides.
ECA_PP_BLANK_MAX = 0.10


def is_run_root(p: Path) -> bool:
    """An ECA-RSI run root (ours or a mirror of one) is never ECA-PP input."""
    return (p / "organize" / "manifest.json").is_file()


def current_files(root: Path):
    """Prune only ECA-PP's step-local .history and ECA-RSI run roots (a
    finished run mirrored back next to standardize/ must not turn its own
    organized.h5ad / final.h5ad into "undeclared" inputs on the next run)."""
    for folder, dirs, files in os.walk(root):
        p = Path(folder)
        if p.name in ("standardize", "identify_columns") and ".history" in dirs:
            dirs.remove(".history")
        dirs[:] = [d for d in dirs if not is_run_root(p / d)]
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
        # also beside an input root that is the standardize folder itself, as the dataset specs name it
        # (until 0.4.4 such runs never saw ECA-PP's batch; decision 0016)
        if ic in files or ic.is_file():
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
        for role in ("batch", "cell_type", "library"):
            s, tsv = column_values(obs, (evidence.get("columns") or {}).get(role), path.parent)
            if s is not None:
                values[f"eca_pp_{role}"] = s
            if tsv is not None:
                files[role] = {"path": str(tsv), "identity": file_identity(tsv)}
    return evidence, values, files


def eca_pp_decision(evidence: dict, values: dict) -> dict | None:
    """A source's experiment decision taken from ECA-PP identify-columns (decision 0016), or None when the
    source has no such result or its column leaves more than ECA_PP_BLANK_MAX of the cells blank: the organize
    agent decides those. A smaller blank share is dropped (`drop_blank`, sample_mapping.build_mapping).

    ECA-PP names the per-sample QC unit (identify-columns 0.5.4 `sample_unit`: library, batch, whole or
    stop) and keeps the rule; eca-rsi keeps no copy. Batch: ECA-PP's batch only when it recommends the
    correction. A result from before 0.5.4 names no unit: its library or batch is still the unit, and a
    source with neither stops until identify-columns is re-run. A stop carries an `error` that organize
    raises unless the owner's sample map decides the source (owner, 2026-10-03)."""
    if not evidence:
        return None
    check("eca-pp-identify-columns", evidence)
    columns = evidence["columns"]
    batch = columns.get("batch") or {}
    named = evidence.get("sample_unit") or {}
    unit = named.get("value") or ("library" if columns.get("library") else "batch" if batch else "stop")
    if unit not in SAMPLE_UNITS:
        raise ValueError(f"ECA-PP sample_unit {unit!r} is not one of {SAMPLE_UNITS}")
    role = unit if unit in ("library", "batch") else None
    blank = float(values[f"eca_pp_{role}"].isna().mean()) if role else 0.0
    if blank > ECA_PP_BLANK_MAX:
        return None
    label = lambda block: block.get("label") or block.get("value")
    decision = {"sample_column": f"eca_pp_{role}" if role else None, "source": "eca_pp",
                "platform": (evidence.get("platform") or {}).get("value", "unknown"),
                "batch": "eca_pp_batch" if batch.get("correction") == "recommended" else None,
                "ladder": evidence.get("ladder")}
    if role == "library":
        decision["rationale"] = (f"ECA-PP library {label(columns['library'])!r}: "
                                 + columns["library"].get("evidence", ""))
    elif role == "batch":
        decision["rationale"] = (f"ECA-PP batch {label(batch)!r} (correction {batch.get('correction')}): "
                                 + str(batch.get("evidence", ""))[:400])
    if blank:
        decision["drop_blank"] = round(blank, 4)
    else:
        reason = named.get("reason") or (
            f"identify-columns {evidence.get('step_version')} found no batch and names no sample unit "
            "(eca-pp < 0.5.4); re-run identify-columns")
        decision.update(confirmed_single=True, rationale=f"ECA-PP: {reason}"[:400])
        if unit == "stop":
            decision["error"] = (f"{reason}. ECA-PP ladder: "
                                 f"{evidence.get('ladder') or columns.get('batch_evidence') or 'not recorded'}.")
    return decision


def inspect_unit(unit: dict) -> dict:
    from .h5ad import open_counts

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
    a = open_counts(h5, min_vars=1)
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
    from .h5ad import read_obs
    import shutil

    target.mkdir(parents=True, exist_ok=True)
    write_json(target / "standardize.json", record["standardize"])
    write_json(target / "identify_columns.json", record.get("identify_columns", {}))
    obs = read_obs(record["h5ad"])
    _, values, _ = load_evidence(record, obs)
    for col, s in values.items():
        obs[col] = s
    obs.to_csv(target / "source_obs.csv.gz", index_label="cell_id")
    for role, f in record.get("derived_files", {}).items():
        shutil.copyfile(f["path"], target / f"{role}.tsv")


def verify_snapshots(input_dir: Path, manifest: dict) -> None:
    for source, entry in manifest.get("upstream", {}).items():
        for name, identity in entry.get("snapshot_files", {}).items():
            if file_identity(input_dir / entry["dir"] / name) != identity:
                raise ValueError(f"upstream snapshot changed: {source}/{name}")


def profile_unit(unit: dict, max_levels: int = 30) -> dict:
    """Cheap obs/metadata profile the planning agent reasons over.

    Reads upstream result.json verbatim (species, counts, cell numbers) and
    the h5ad obs directly, with counts validated in bounded chunks. For every
    low-cardinality categorical column, its value counts — organ/tissue/compartment structure lives
    there, and obs metadata is the ONLY sanctioned evidence for splitting.
    """
    from .h5ad import open_counts
    import pandas as pd

    prof = {"name": unit["name"], "h5ad": unit["h5ad"]}
    with open(unit["standardize_result"]) as f:
        std = json.load(f)

    prof["species"] = (std.get("species") or {}).get("resolved")
    prof["n_cells"] = (std.get("metrics") or {}).get("n_cells")
    prof["n_vars"] = (std.get("metrics") or {}).get("n_vars")

    a = open_counts(unit["h5ad"], min_vars=1)
    try:
        validate_matrix(a, std)
        evidence, values, _ = load_evidence(unit, a.obs)
        prof["upstream_evidence"] = evidence
        prof["eca_pp_decision"] = eca_pp_decision(evidence, values)
        prof["upstream_review"] = std.get("reasons", [])
        cols = {}
        for c in a.obs.columns:
            s = a.obs[c]
            semantic = normalize(s) if pd.api.types.is_string_dtype(s.dtype) or isinstance(s.dtype, pd.CategoricalDtype) else s
            nuniq = semantic.nunique(dropna=True)
            entry: dict = {"dtype": str(s.dtype), "n_unique": int(nuniq), "n_na": int(semantic.isna().sum())}
            if nuniq <= max_levels and (pd.api.types.is_string_dtype(s.dtype) or isinstance(s.dtype, pd.CategoricalDtype)):
                # drop unused categorical levels — phantom zero counts would
                # pollute the profile the agent reasons over
                entry["value_counts"] = {str(k): int(v) for k, v in semantic.value_counts().items() if v}
            cols[str(c)] = entry
        prof["obs_columns"] = cols
        prof["n_obs"] = int(a.n_obs)
    finally:
        a.file.close()
    return prof


# ---------------------------------------------------------------- cli

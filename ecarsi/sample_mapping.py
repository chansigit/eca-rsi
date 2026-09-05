"""Source-scoped experiment partitions, explicit cross-file merges and pool checks."""
from __future__ import annotations

import re
from pathlib import Path

from . import layout as L
from .run_state import digest, file_identity, read_json
from .upstream import normalize

SAMPLE_KEY = "eca_sample_id"


def obs_profile(obs) -> dict:
    cols = {}
    for col in obs:
        s = normalize(obs[col])
        counts = s.value_counts()
        cols[str(col)] = {
            "dtype": str(obs[col].dtype), "n_unique": len(counts), "n_na": int(s.isna().sum()),
            "value_counts": {str(k): int(v) for k, v in counts.head(50).items()},
            "group_sizes": {"min": int(counts.min()), "max": int(counts.max()),
                            "median": float(counts.median())} if len(counts) else {},
            "values_truncated": len(counts) > 50,
        }
    return {"n_obs": len(obs), "obs_columns": cols}


def build_mapping(h5ad: Path, unit: Path | None, spec: dict | None, identify,
                  column: str | None = None, single: bool = False):
    import anndata as ad
    import pandas as pd

    a = ad.read_h5ad(h5ad, backed="r")
    try:
        obs = a.obs.copy()
    finally:
        a.file.close()
    if not obs.index.is_unique or len(obs) == 0:
        raise ValueError("sample mapping requires nonempty unique cell IDs")
    if "source_unit" in obs and normalize(obs["source_unit"]).isna().any():
        raise ValueError("missing source identity")
    sources = obs["source_unit"].astype(str) if "source_unit" in obs else pd.Series("input", index=obs.index)
    original_ids = obs["eca_source_cell_id"].astype(str) if "eca_source_cell_id" in obs else obs.index.to_series()
    upstream = read_json(L.input_manifest(unit)).get("upstream", {}) if unit and L.input_manifest(unit).is_file() else {}
    if spec is not None and set(spec.get("sources", {})) != set(sources):
        raise ValueError("sample-map sources must exactly cover this analysis unit's sources")
    if single and sources.nunique() != 1:
        raise ValueError("--single-sample requires one source; cross-source pooling needs sample-map merges with evidence")
    table = pd.DataFrame({"source_unit": sources, "source_cell_id": original_ids})
    table["source_value"] = ""
    table[SAMPLE_KEY] = ""
    decisions, groups = {}, {}
    for source in sorted(sources.unique()):
        part = obs.loc[sources == source]
        evidence = upstream.get(source, {})
        profile = obs_profile(part)
        profile.update(source=source, upstream=evidence)
        if spec is not None:
            decision = spec["sources"][source]
        elif column is not None:
            decision = {"sample_column": column, "rationale": "explicit --sample-column"}
        elif single:
            decision = {"sample_column": None, "confirmed_single": True, "rationale": "explicit --single-sample"}
        else:
            decision = identify(profile)
            from . import cost
            cost.record(unit or h5ad.parent, f"{L.PERSAMPLE}/identify/{source}",
                        getattr(identify, "last_cost", None), "identify experiment column")
        from .persample import _validate_sample_column
        problem = _validate_sample_column(decision, profile)
        if problem:
            raise ValueError(f"{source}: {problem}")
        col = decision["sample_column"]
        values = normalize(part[col]) if col else pd.Series("all", index=part.index)
        if values.isna().any():
            raise ValueError(f"{source}: sample partition contains missing values")
        # Full source metadata is saved before organ filtering. Check exact
        # original IDs, so a library split between tissues cannot run QC twice.
        if evidence:
            path = L.input_manifest(unit).parent / evidence["dir"] / "source_obs.csv.gz"
            if file_identity(path) != evidence["source_obs_identity"]:
                raise ValueError(f"source metadata snapshot changed: {source}")
            full = pd.read_csv(path, index_col=0, dtype=str, keep_default_na=False)
            full.index = full.index.astype(str)
            full_values = normalize(full[col]) if col else pd.Series("all", index=full.index)
            for value in values.unique():
                expected = set(full.index[full_values == value])
                actual = set(original_ids.loc[values.index[values == value]])
                if expected != actual:
                    raise ValueError(f"{source}/{value}: organize split an experiment across units; complete-pool QC is required")
        for value in sorted(values.unique()):
            pair = source, str(value)
            safe = lambda s: re.sub(r"[^A-Za-z0-9._-]+", "_", str(s))[:50]
            sid = f"{safe(source)}__{safe(value)}__{digest(pair)[:10]}"
            groups[pair] = sid
            idx = values.index[values == value]
            table.loc[idx, "source_value"] = str(value)
            table.loc[idx, SAMPLE_KEY] = sid
        decisions[source] = decision
    assigned, merge_ids = set(), set()
    for merge in (spec or {}).get("merges", []):
        sid = merge.get("sample_id")
        if (not isinstance(sid, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}", sid)
                or not str(merge.get("evidence", "")).strip()):
            raise ValueError("cross-source merge needs sample_id and positive experiment evidence")
        if sid in merge_ids or sid in groups.values():
            raise ValueError(f"duplicate/colliding merged sample_id: {sid}")
        members = [(m["source"], str(m["value"])) for m in merge.get("members", [])]
        if len(members) < 2 or len(set(members)) != len(members):
            raise ValueError("merge requires at least two distinct source/value members")
        for pair in members:
            if pair not in groups or pair in assigned:
                raise ValueError(f"unknown or multiply merged source group: {pair}")
            table.loc[table[SAMPLE_KEY] == groups[pair], SAMPLE_KEY] = sid
            assigned.add(pair)
        merge_ids.add(sid)
    if table.duplicated([SAMPLE_KEY, "source_cell_id"]).any():
        raise ValueError("a merged experiment contains repeated original cell IDs; resolve overlapping source cells first")
    return table, {"sources": decisions, "merges": (spec or {}).get("merges", [])}


def mapping_identity(table) -> str:
    return digest([[str(idx), *map(str, row)] for idx, row in zip(table.index, table.to_numpy())])

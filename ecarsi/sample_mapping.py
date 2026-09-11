"""Source-scoped experiment partitions, explicit cross-file merges and pool checks."""
from __future__ import annotations

import re
from pathlib import Path

from . import layout as L
from . import policies as P
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
    if spec is not None:
        P.check_spec_keys(spec)
        if set(spec.get("sources", {})) != set(sources):
            raise ValueError("sample-map sources must exactly cover this analysis unit's sources")
    if single and sources.nunique() != 1:
        raise ValueError("--single-sample requires one source; cross-source pooling needs sample-map merges with evidence")
    table = pd.DataFrame({"source_unit": sources, "source_cell_id": original_ids})
    table["source_value"] = ""
    table[SAMPLE_KEY] = ""
    # policy exclusions come first: excluded cells never reach a profile,
    # a partition or an OSP subset, and stay in the table with their reason
    excluded = pd.Series("", index=obs.index, dtype=object)
    rules = P.apply_rules(obs, spec.get("exclude_cells", []), excluded, "sample_map") if spec else []
    decisions, groups = {}, {}
    for source in sorted(sources.unique()):
        part = obs.loc[(sources == source) & excluded.eq("")]
        if part.empty:
            raise ValueError(f"{source}: exclude_cells removed every cell of the source")
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
            decision = identify(profile, part)
            from . import cost
            tin, tout = getattr(identify, "last_tokens", (None, None))
            cost.record(unit or h5ad.parent, f"{L.PERSAMPLE}/identify/{source}",
                        getattr(identify, "last_cost", None), "identify experiment column", tin, tout)
            if decision.get("exclude_cells"):
                # the agent's proposal, re-applied by the host exactly like a user rule
                rules += P.apply_rules(part, decision["exclude_cells"], excluded, "agent", strict=True)
                part = part.loc[excluded.reindex(part.index).eq("")]
                profile = obs_profile(part)
                profile.update(source=source, upstream=evidence)
        from .persample import _validate_sample_column
        derive = decision.get("derive_from_cell_id") if spec is not None else None
        missing_as = decision.get("missing_as") if spec is not None else None
        if derive is not None:
            # explicit spec only: the library name lives in the original cell ID
            # (e.g. "AdultBrain_1.<barcode>") and no obs column carries it
            if not isinstance(derive, str) or re.compile(derive).groups != 1:
                raise ValueError(f"{source}: derive_from_cell_id must be a regex with exactly one capture group")
            if not str(decision.get("rationale", "")).strip():
                raise ValueError(f"{source}: experiment decision requires a rationale")
            decision = {**decision, "sample_column": None}
            col = None
        else:
            if missing_as is not None and (not isinstance(missing_as, str) or not missing_as.strip()):
                raise ValueError(f"{source}: missing_as must be a non-empty label")
            problem = _validate_sample_column(decision, profile, allow_na=missing_as is not None)
            if problem:
                raise ValueError(f"{source}: {problem}")
            col = decision["sample_column"]

        def partition(frame, ids):
            if derive is not None:
                got = ids.astype(str).str.extract(derive, expand=False)
                if got.isna().any():
                    raise ValueError(f"{source}: derive_from_cell_id does not match {int(got.isna().sum())} cell IDs")
                return got
            if col is None:
                return pd.Series("all", index=frame.index)
            v = normalize(frame[col])
            return v.fillna(missing_as) if missing_as is not None else v

        values = partition(part, original_ids.loc[part.index])
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
            full_values = partition(full, full.index.to_series())
            gone = set(original_ids[(sources == source) & excluded.ne("")])  # policy-excluded, by original ID
            for value in values.unique():
                expected = set(full.index[full_values == value]) - gone
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
    kept = table[SAMPLE_KEY].ne("")
    if table[kept].duplicated([SAMPLE_KEY, "source_cell_id"]).any():
        raise ValueError("a merged experiment contains repeated original cell IDs; resolve overlapping source cells first")
    table["excluded_reason"] = excluded.reindex(table.index).fillna("")
    decision = {"sources": decisions, "merges": (spec or {}).get("merges", []), "exclude_cells": rules}
    if spec is not None and spec.get("batch_key") is not None:
        decision["batch_key"] = P.resolve_batch_key(obs[kept.to_numpy()], table.loc[kept, SAMPLE_KEY], spec["batch_key"])
    return table, decision


def mapping_identity(table) -> str:
    return digest([[str(idx), *map(str, row)] for idx, row in zip(table.index, table.to_numpy())])

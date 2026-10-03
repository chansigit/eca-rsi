"""Find the eca-pp products under an input folder and profile each one, for the organize stage
(ecarsi.stages.organize): every h5ad must be an eca-pp product (`.../standardize/standardized.h5ad`
next to its `result.json`); a bare h5ad stops organize."""

from __future__ import annotations

import json
from pathlib import Path

# ---------------------------------------------------------------- detection


def find_ecapp_units(root: Path) -> tuple[list[dict], list[Path]]:
    from .upstream import discover

    return discover(root)


# ---------------------------------------------------------------- profiling


def profile_unit(unit: dict, max_levels: int = 30) -> dict:
    """Cheap obs/metadata profile the planning agent reasons over.

    Reads upstream result.json verbatim (species, counts, cell numbers) and
    the h5ad obs directly, with counts validated in bounded chunks. For every
    low-cardinality categorical column, its value counts — organ/tissue/compartment structure lives
    there, and obs metadata is the ONLY sanctioned evidence for splitting.
    """
    from .downstream import _data
    import pandas as pd

    prof = {"name": unit["name"], "h5ad": unit["h5ad"]}
    with open(unit["standardize_result"]) as f:
        std = json.load(f)
    from .upstream import load_evidence, normalize, validate_matrix

    prof["species"] = (std.get("species") or {}).get("resolved")
    prof["n_cells"] = (std.get("metrics") or {}).get("n_cells")
    prof["n_vars"] = (std.get("metrics") or {}).get("n_vars")

    a = _data(unit["h5ad"], min_vars=1)
    try:
        validate_matrix(a, std)
        evidence, _, _ = load_evidence(unit, a.obs)
        prof["upstream_evidence"] = evidence
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

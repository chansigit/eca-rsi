"""Study-design text for the MSP/ZMIP agents (--design-context).

Derived, not asked: obs columns of the unit's organized input that are constant
within every persample sample and differ across samples describe how the samples
were produced (Tabula Muris FACS: one plate = one mouse x one sort gate). The
agents otherwise read a sample-confined cluster as a batch artefact.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from . import layout as L
from .upstream import normalize

MAX_COLS, MAX_ROWS, MAX_VALUES = 6, 120, 20
NOTE = "Each sample is the unit of batch correction; columns listed are constant within a sample."


def _obs(h5ad: Path) -> pd.DataFrame:
    import h5py
    from anndata.io import read_elem

    with h5py.File(h5ad, "r") as f:
        return read_elem(f["obs"])


def sample_of_cells(unit: Path, obs: pd.DataFrame) -> pd.Series | None:
    """obs index → sample value as MSP sees it; None when the unit is one sample."""
    mapping = L.persample_root(unit) / L.SAMPLE_MAPPING
    mp = L.persample_manifest(unit)
    if mapping.is_file():
        from .sample_mapping import SAMPLE_KEY

        table = pd.read_csv(mapping, index_col=0, dtype=str, keep_default_na=False)
        sample = table[SAMPLE_KEY].reindex(obs.index)
        return sample.mask(sample.eq(""))  # policy-excluded cells belong to no sample
    if mp.is_file():
        with open(mp) as f:
            col = json.load(f).get("sample_column")
        if col and col in obs:
            return normalize(obs[col])
    return None


def design_table(obs: pd.DataFrame, sample: pd.Series) -> pd.DataFrame:
    """One row per sample, one column per obs column that is constant within
    every sample (missing values ignored) and takes >1 value across samples."""
    sample = sample.rename("sample")
    rows = {}
    for col in sorted(map(str, obs.columns)):
        if col == sample.name or pd.api.types.is_float_dtype(obs[col]):
            continue
        s = normalize(obs[col])
        if s.nunique() < 2:
            continue
        groups = s.groupby(sample, observed=True)
        if (groups.nunique() > 1).any():
            continue  # varies within a sample: a per-cell label or id
        first = groups.first()
        if first.nunique() > 1:
            rows[col] = first
    return pd.DataFrame(rows).sort_index()


def render(table: pd.DataFrame) -> str:
    if table.empty:
        return ""
    cols, extra = list(table.columns[:MAX_COLS]), list(table.columns[MAX_COLS:])
    lines = [NOTE]
    if len(table) <= MAX_ROWS:
        for sample, row in table[cols].iterrows():
            lines.append(f"sample {sample}: " + ", ".join(f"{c}={row[c]}" for c in cols if pd.notna(row[c])))
    else:
        lines.append(f"{len(table)} samples (too many to list).")
        for c in cols:
            vals = sorted(table[c].dropna().unique(), key=str)
            shown = ", ".join(map(str, vals[:MAX_VALUES])) + (f", ... ({len(vals)} values)" if len(vals) > MAX_VALUES else "")
            lines.append(f"column {c} takes values {shown}")
    if extra:
        lines.append("Also constant within each sample: " + ", ".join(extra))
    return "\n".join(lines)


def design_text(unit: Path) -> str:
    """'' when nothing distinguishes the samples (or the unit is one sample)."""
    h5ad = L.input_h5ad(unit)
    if not h5ad.is_file():
        return ""
    obs = _obs(h5ad)
    sample = sample_of_cells(unit, obs)
    if sample is None or sample.nunique() < 2:
        return ""
    return render(design_table(obs, sample))


def main(argv: list[str] | None = None) -> int:
    import sys

    print(design_text(Path((argv or sys.argv[1:])[0]).resolve()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

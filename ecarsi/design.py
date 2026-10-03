"""Read a unit's obs table straight from its h5ad (no expression), for organize."""
from __future__ import annotations

from pathlib import Path

import pandas as pd


def _obs(h5ad: Path) -> pd.DataFrame:
    import h5py
    from anndata.io import read_elem

    with h5py.File(h5ad, "r") as f:
        return read_elem(f["obs"])

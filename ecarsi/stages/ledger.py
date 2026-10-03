"""Per-cell ledger helpers: read the removal records and labels the stages leave (osp qc_removed.csv,
msp annotation_removed.csv, zmip zmip_removed.csv, the labels in the h5ads) and the Sankey data of a unit.
The release stage (ecarsi.stages.release) builds `cell_ledger.csv` and `sankey.json` with them; every
removal must be accounted per cell. Labels under 1% of a column are pooled into "other"."""

from __future__ import annotations

from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import numpy as np
import pandas as pd


REMOVED_PREFIX = "removed:"
OTHER_MIN_FRAC = 0.01  # labels below this share of a stage are pooled into "other"


# ---------------------------------------------------------------- ledger

def _obs_source(path: Path) -> Path | None:
    """The h5ad itself, or the obs sidecar ecarsi.prune left in its place."""
    if path.is_file():
        return path
    for suf in (".obs.parquet", ".obs.csv.gz"):
        side = path.with_name(path.name + suf)
        if side.is_file():
            return side
    return None


def _obs(path: Path, cols: list[str]) -> pd.DataFrame:
    src = _obs_source(path)
    if src is None:
        raise FileNotFoundError(path)
    if src.suffix == ".parquet":
        obs = pd.read_parquet(src)
    elif src.name.endswith(".csv.gz"):
        obs = pd.read_csv(src, dtype=str, keep_default_na=False)
        obs = obs.set_index(obs.columns[0])
    else:
        import anndata as ad

        a = ad.read_h5ad(src, backed="r")
        obs = a.obs
        df = obs[[c for c in cols if c in obs.columns]].copy()
        a.file.close()
        df.index.name = "cell"
        _cell_ids(df.index, str(src))
        return df.astype(object)
    df = obs[[c for c in cols if c in obs.columns]].copy()
    df.index.name = "cell"
    _cell_ids(df.index, str(src))
    return df.astype(object)


def _cell_ids(values, source):
    ids = pd.Index(values)
    if ids.hasnans or not ids.is_unique or any(not isinstance(v, str) or not v for v in ids):
        raise ValueError(f"invalid/duplicate cell IDs: {source}")
    return set(ids)


def _table(path, columns):
    if not path.is_file():
        raise ValueError(f"missing ledger: {path}")
    table = pd.read_csv(path, dtype=str, keep_default_na=False)
    if not set(columns) <= set(table.columns):
        raise ValueError(f"missing ledger columns {columns}: {path}")
    if "cell" in columns:
        _cell_ids(table["cell"], str(path))
        table = table.set_index("cell")
    return table


def _partition(expected, survivors, removed, stage):
    kept = _cell_ids(survivors, stage + " survivors")
    gone = _cell_ids(removed, stage + " removed")
    if kept & gone or kept | gone != expected:
        raise ValueError(f"{stage} cell conservation failed: survivors and removals must partition the input")


# ---------------------------------------------------------------- sankey

def _stage_nodes(ledger: pd.DataFrame, label_col: str | None, status_col: str, keep_mask: np.ndarray,
                 pool: bool = True):
    """Per-row node id for one stage: the label for cells still in play,
    'removed: <source>' for cells the stage removed, None for cells already
    gone before this stage. Small labels pooled into 'other' when pool."""
    status = ledger[status_col].astype(str) if status_col in ledger else pd.Series("", index=ledger.index)
    node = pd.Series([None] * len(ledger), index=ledger.index, dtype=object)
    gone_here = status.str.startswith(REMOVED_PREFIX).values | (status == "excluded-sample").values
    alive = keep_mask & status.isin(["kept", "not-zoomed"]).values
    if label_col and label_col in ledger:
        lab = ledger[label_col].astype(object).where(ledger[label_col].notna(), "unlabelled").astype(str)
        node[alive] = lab[alive]
        vc = lab[alive].value_counts()
        small = set(vc[vc < OTHER_MIN_FRAC * max(alive.sum(), 1)].index)
        if pool and len(small) > 1:
            node[alive & lab.isin(small).values] = f"other ({len(small)} labels)"
    else:
        node[alive] = "cells"
    gone = keep_mask & gone_here
    src = status[gone].str.replace(REMOVED_PREFIX, "", regex=False).str.replace(r"^agent:", "", regex=True)
    node[gone] = "removed: " + src
    return node, alive


def sankey_data(ledger: pd.DataFrame, stages: list[tuple[str, str | None, str]]) -> dict:
    """The Sankey as data, every label kept (no 'other' pooling) so an
    interactive renderer can show the tiny clusters on hover:
    {stages: [title], nodes: [{stage, name, count, removed}], flows: [{src, dst, count}]}
    (src/dst index into nodes)."""
    keep = np.ones(len(ledger), dtype=bool)
    cols = []
    for _, label_col, status_col in stages:
        node, alive = _stage_nodes(ledger, label_col, status_col, keep, pool=False)
        cols.append(node)
        keep = np.asarray(alive)
    nodes, idx = [], {}
    for i, node in enumerate(cols):
        vc = node.value_counts()
        kept = sorted([n for n in vc.index if not n.startswith("removed:")], key=lambda n: -vc[n])
        rm = sorted([n for n in vc.index if n.startswith("removed:")], key=lambda n: -vc[n])
        for n in kept + rm:
            idx[(i, n)] = len(nodes)
            nodes.append({"stage": i, "name": n, "count": int(vc[n]), "removed": n.startswith("removed:")})
    flows = []
    for i in range(len(cols) - 1):
        a, b = cols[i], cols[i + 1]
        m = a.notna() & b.notna() & ~a.astype(str).str.startswith("removed:")
        if not m.any():
            continue
        ct = pd.crosstab(a[m], b[m])
        for s_ in ct.index:
            for d in ct.columns:
                v = int(ct.loc[s_, d])
                if v:
                    flows.append({"src": idx[(i, s_)], "dst": idx[(i + 1, d)], "count": v})
    return {"stages": [t for t, _, _ in stages], "total": int(len(ledger)), "nodes": nodes, "flows": flows}


# ---------------------------------------------------------------- entry

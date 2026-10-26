"""The zoom-in stress rule (#68): stress cells are picked inside a lineage, only in clusters where several panel
genes rise clearly, and only the cells above the clean background's 95th percentile."""

import numpy as np
import pandas as pd
import scanpy as sc
import anndata as ad
from scipy import sparse

from zmip.stress import stress_cells

PANEL = [
    "FOS",
    "JUN",
    "JUNB",
    "EGR1",
    "IER2",
    "HSPA1A",
    "HSPA1B",
    "DNAJB1",
    "ZFP36",
    "GADD45B",
]


def lineage(stressed=0, seed=0):
    """1,200 cells of two subtypes over 300 genes; `stressed` cells of subtype A get the panel genes 8x higher."""
    rng = np.random.default_rng(seed)
    genes = PANEL + [f"G{i}" for i in range(300 - len(PANEL))]
    mu = rng.gamma(2.0, 1.0, size=len(genes))
    counts = []
    for k in range(2):
        m = mu.copy()
        m[len(PANEL) + 40 * k : len(PANEL) + 40 * k + 40] *= (
            6  # each subtype has its own genes
        )
        counts.append(rng.poisson(m, size=(600, len(genes))))
    x = np.vstack(counts).astype(np.float32)
    if stressed:
        x[:stressed, : len(PANEL)] = rng.poisson(
            mu[: len(PANEL)] * 8, size=(stressed, len(PANEL))
        )
    a = ad.AnnData(
        sparse.csr_matrix(x),
        obs=pd.DataFrame(index=[f"c{i}" for i in range(len(x))]),
        var=pd.DataFrame(index=genes),
    )
    sc.pp.normalize_total(a, target_sum=1e4)
    sc.pp.log1p(a)
    sc.pp.pca(a, n_comps=20)
    sc.pp.neighbors(a, n_neighbors=15)
    return a


def test_a_stressed_group_inside_a_lineage_is_found_and_only_its_high_cells_go():
    a = lineage(stressed=200)
    cells, clusters = stress_cells(a)
    hit = set(cells.index[cells.stress])
    truth = {f"c{i}" for i in range(200)}
    assert clusters.candidate.any() and len(hit) > 100
    assert len(hit & truth) / len(hit) > 0.9  # the picked cells are the stressed ones
    assert cells.attrs["threshold"] == cells.attrs["threshold"]  # a background existed


def test_a_lineage_without_stress_loses_nothing():
    cells, clusters = stress_cells(lineage(stressed=0))
    assert not cells.stress.any() and not clusters.candidate.any()


def test_cells_left_out_are_not_tested():
    a = lineage(stressed=200)
    eligible = np.ones(a.n_obs, dtype=bool)
    eligible[:200] = False  # a numerical check already removes the stressed cells
    cells, _ = stress_cells(a, eligible)
    assert len(cells) == a.n_obs - 200 and not cells.stress.any()

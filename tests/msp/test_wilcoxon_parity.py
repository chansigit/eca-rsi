"""msp's sparse Wilcoxon rank sums (deg_logging._wilcoxon) give Scanpy's own tables to the bit."""

import anndata as an
import numpy as np
import pandas as pd
import pytest
import scanpy as sc
from scipy import sparse

from msp import deg_logging


def data():
    """Ties among the stored values (rounded), a few negative values, stored zeros, an unexpressed gene,
    uneven groups."""
    rng = np.random.default_rng(1)
    X = np.round(np.log1p(rng.poisson(0.5, (400, 80))), 1).astype(np.float32)
    X[rng.random(X.shape) < 0.03] *= -1
    X[:, 0] = 0
    matrix = sparse.csr_matrix(X)
    matrix.data[::40] = 0  # explicit zeros: Scanpy drops them first
    k = rng.choice(list("abcde"), 400, p=[0.35, 0.25, 0.2, 0.15, 0.05])
    obs = pd.DataFrame({"k": pd.Categorical(k)}, index=[str(i) for i in range(400)])
    return an.AnnData(X=matrix, obs=obs, var=pd.DataFrame(index=[f"g{i}" for i in range(80)]), uns={"log1p": {}})


# Several groups vs the rest, an untested group left in the reference (global DEG, parent-core DEG, zmip
# lineage_markers), and one group vs the rest (local DEG, check_deg).
@pytest.mark.parametrize("groups", [["a", "b", "c", "d"], ["c"]], ids=["groups-vs-rest", "one-vs-rest"])
def test_sparse_rank_sums_give_scanpys_tables_to_the_bit(monkeypatch, groups):
    ad = data()
    monkeypatch.setattr(deg_logging, "RANK_BLOCK_NNZ", 100)  # many column blocks

    def table():
        work = ad.copy()
        deg_logging.rank_genes_groups(work, "k", groups=groups, method="wilcoxon", use_raw=False, pts=True)
        return sc.get.rank_genes_groups_df(work, group=None)

    assert sc.tl._rank_genes_groups._RankGenes.wilcoxon is deg_logging._wilcoxon
    new = table()
    monkeypatch.setattr(sc.tl._rank_genes_groups._RankGenes, "wilcoxon", deg_logging.SCANPY_WILCOXON)
    old = table()
    assert (old["scores"] != 0).mean() > 0.9  # a real test, not a table of ties
    pd.testing.assert_frame_equal(new, old, check_exact=True)

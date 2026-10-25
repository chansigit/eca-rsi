"""Doublets from Scrublet, contamination from decontX (#70): msp.integrate.doublets."""
import anndata as ad
import numpy as np
import pandas as pd
import pytest
import scipy.sparse as sp

from msp.integrate import doublets as D


def test_a_percentile_needs_enough_background_cells():
    assert [D.min_background(p) for p in (0.95, 0.99, 0.999)] == [20, 100, 1000]


def test_library_threshold_is_scrublets_own_only_when_enough_simulated_doublets_exceed_it():
    sim = np.r_[np.full(30, 0.8), np.full(70, 0.1)]
    assert D.library_threshold(0.5, sim, 0.25) == 0.5  # 30% above: trusted
    assert D.library_threshold(0.9, sim, 0.25) == pytest.approx(np.quantile(sim, 0.75))  # 0% above: the quantile
    assert D.library_threshold(None, sim, 0.25) == pytest.approx(np.quantile(sim, 0.75))  # Scrublet found none
    assert np.isnan(D.library_threshold(None, None, 0.25))  # Scrublet did not run


def two_library_obs():
    """Two libraries of one cluster: library B sequences twice as deep. In each, 40 cells below the threshold
    and two above it: one with 1.5x its library's RNA, one with its library's RNA."""
    rows = []
    for lib, depth in (("A", 1000), ("B", 2000)):
        rows += [dict(library=lib, total_counts=depth, doublet_score=0.1) for _ in range(40)]
        rows += [dict(library=lib, total_counts=1.5 * depth, doublet_score=0.9),
                 dict(library=lib, total_counts=depth, doublet_score=0.9)]
    obs = pd.DataFrame(rows, index=[f"c{i}" for i in range(len(rows))])
    obs["msp_leiden_r1.0"], obs[D.THRESHOLD_COL] = "0", 0.5
    return obs


def test_the_cell_level_check_compares_rna_within_the_cluster_and_library():
    obs = two_library_obs()
    above = D.doublet_above(obs)
    assert above.sum() == 4
    ref = D.rna_reference(obs, ~above, "msp_leiden_r1.0", "library")
    raised = obs.total_counts.to_numpy() >= D.DOUBLET_RNA_FACTOR * ref
    assert list(obs.index[above & raised]) == ["c40", "c82"]  # 1.5x of its own library, A's and B's
    pooled = D.rna_reference(obs, ~above, "msp_leiden_r1.0")  # without the library the deep library's ordinary
    assert (obs.total_counts.to_numpy() >= D.DOUBLET_RNA_FACTOR * pooled)[83]  # cell (2000) looks raised


def detection_data():
    """Groups by detected genes: A genes 0-4, B genes 5-9, AB (doublets) genes 0-9, R (rare) genes 0-4 and its
    own gene 10, low (doublets but few above the threshold) genes 0-9."""
    pattern = {"A": range(0, 5), "B": range(5, 10), "AB": range(0, 10), "R": [0, 1, 2, 3, 4, 10], "low": range(0, 10)}
    sizes = {"A": 200, "B": 200, "AB": 30, "R": 30, "low": 30}
    labels, rows = [], []
    for name, n in sizes.items():
        for _ in range(n):
            row = np.zeros(11)
            row[list(pattern[name])] = 3
            rows.append(row)
            labels.append(name)
    labels = np.array(labels)
    above = np.isin(labels, ["AB", "R"]) | ((labels == "low") & (np.arange(len(labels)) % 3 == 0))
    return sp.csr_matrix(np.array(rows)), labels, above


def test_a_doublet_cluster_has_half_its_cells_above_and_no_gene_of_its_own():
    X, labels, above = detection_data()
    own = D.own_gene_counts(X, labels, ["AB", "R", "A"])
    assert own == {"AB": 0, "R": 1, "A": 0}
    removed, table = D.doublet_clusters(X, labels, above)
    assert set(labels[removed]) == {"AB"}, "R has a gene of its own; low has a third of its cells above"
    assert table.set_index("cluster").loc["low", "share_above"] == pytest.approx(1 / 3, abs=0.02)


def blobs(n_blobs=10, per_blob=200, seed=0):
    import scanpy as sc
    rng = np.random.default_rng(seed)
    centres = rng.normal(0, 20, (n_blobs, 10))
    X = np.vstack([c + rng.normal(0, 1, (per_blob, 10)) for c in centres]).astype(np.float32)
    data = ad.AnnData(X)
    data.obsm["X_pca"] = X
    sc.pp.neighbors(data, n_neighbors=15, use_rep="X_pca", random_state=0)
    return data


def test_overclustering_finds_the_resolution_for_the_median_cluster_size():
    data = blobs(per_blob=200)
    labels, resolution = D.overcluster(data, target=100)
    median = float(np.median(labels.value_counts()))
    assert 60 <= median <= 160 and resolution > 0
    again, same = D.overcluster(data, target=100)
    assert same == resolution and again.equals(labels), "fixed seed: the same partition"


def test_contamination_is_judged_on_foreign_soup_genes_in_clusters_with_a_large_background():
    """Soup: cluster T (big, dominant) expresses genes 0-9, cluster B genes 10-19. Four B candidates carry
    T's genes, two do not. A small cluster S has too little background. T is the soup's own lineage."""
    rng = np.random.default_rng(0)
    def cells(n, genes, extra=None):
        X = np.zeros((n, 30))
        X[:, genes] = rng.poisson(5, (n, len(genes)))
        if extra is not None:
            X[:, extra] += rng.poisson(4, (n, len(extra)))
        return X
    blocks = [cells(4000, range(0, 10)), cells(1200, range(10, 20)), cells(4, range(10, 20), range(0, 10)),
              cells(2, range(10, 20)), cells(60, range(20, 30)), cells(3, range(20, 30), range(0, 10))]
    cluster = ["T"] * 4000 + ["B"] * 1206 + ["S"] * 63
    lineage = ["T cell"] * 4000 + ["B cell"] * 1206 + ["Stromal"] * 63
    data = ad.AnnData(sp.csr_matrix(np.vstack(blocks)), obs=pd.DataFrame({"msp_leiden_r1.0": cluster}, index=[f"c{i}" for i in range(5269)]))
    candidate = np.zeros(5269, dtype=bool)
    candidate[5200:5206] = True  # B: 4 carrying T genes, 2 clean
    candidate[5266:5269] = True  # S: carrying T genes, but 60 background cells
    candidate[:5] = True  # T: the dominant lineage
    table = D.contamination_check(data, candidate, "msp_leiden_r1.0", lineage)
    assert set(table.index[table.contamination.astype(bool)]) == {f"c{i}" for i in range(5200, 5204)}
    assert table.loc["c5267", "status"].startswith("not tested: background")
    assert table.loc["c0", "status"] == "not tested: dominant lineage of the soup"
    assert table.loc["c5200", "n_foreign_genes"] == 20  # T's genes and S's: every soup gene B lacks


def test_doublet_checks_join_the_three_branches_on_a_real_graph():
    data = blobs(n_blobs=4, per_blob=150)
    n = data.n_obs
    rng = np.random.default_rng(1)
    data.obs["msp_leiden_r1.0"] = np.repeat(["0", "1", "2", "3"], 150)
    data.obs["library"] = np.tile(["A", "B"], n // 2)
    data.obs["total_counts"] = rng.normal(1000, 50, n)
    data.obs["doublet_score"] = rng.uniform(0.0, 0.2, n)
    data.obs[D.THRESHOLD_COL] = 0.5
    data.layers["counts"] = sp.csr_matrix(np.ones((n, data.n_vars)))
    data.obs.iloc[0, data.obs.columns.get_loc("doublet_score")] = 0.9  # above, raised RNA: cell level
    data.obs.iloc[0, data.obs.columns.get_loc("total_counts")] = 2000
    data.obs.iloc[1, data.obs.columns.get_loc("doublet_score")] = 0.9  # above, ordinary RNA: kept
    candidate = np.zeros(n, dtype=bool)
    candidate[[2, 3]] = True  # decontX candidates, both with raised RNA
    data.obs.iloc[2, data.obs.columns.get_loc("doublet_score")] = 0.4  # high for its cluster, below the threshold
    data.obs.iloc[[2, 3], data.obs.columns.get_loc("total_counts")] = 2000
    out = D.doublet_checks(data, "msp_leiden_r1.0", candidate, "library")
    assert list(out.index[out.doublet_cell]) == [data.obs_names[0]]
    assert list(out.index[out.doublet_decontx]) == [data.obs_names[2]], "cell 3's score is ordinary for its cluster"
    assert not out.doublet_cluster.any() and out.overcluster.nunique() > 1
    assert list(out.index[out.doublet]) == [data.obs_names[0], data.obs_names[2]]
    del data.obs[D.THRESHOLD_COL]
    assert D.doublet_checks(data, "msp_leiden_r1.0", candidate, "library") is None

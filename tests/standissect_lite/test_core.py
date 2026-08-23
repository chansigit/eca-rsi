import pathlib
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from standissect_lite import dissect_partition, umap_leiden_partition  # noqa: E402

ad = pytest.importorskip("anndata")


def _blob(center, n, rng, spread=0.4):
    return rng.normal(center, spread, size=(n, 2))


def _planted_adata(n_main=300, n_other=300, n_minor=40, seed=0):
    """3 spatial blobs, 2 RNA labels: blob C shares RNA label '0' with blob A
    but sits far away on the UMAP -> a planted minor inside RNA cluster 0."""
    rng = np.random.default_rng(seed)
    xy = np.vstack([
        _blob((0, 0), n_main, rng),        # A: RNA 0, main core
        _blob((12, 0), n_other, rng),      # B: RNA 1
        _blob((20, 20), n_minor, rng),     # C: RNA 0, planted minor
    ])
    rna = np.array(["0"] * n_main + ["1"] * n_other + ["0"] * n_minor)
    a = ad.AnnData(np.zeros((len(rna), 1), dtype="float32"))
    a.obs_names = [f"cell{i}" for i in range(len(rna))]
    a.obs["leiden"] = pd.Categorical(rna)
    a.obsm["X_umap"] = xy
    return a


# ------------------------------------------------------------- low-level part
def test_umap_leiden_partition_two_blobs_target_k():
    rng = np.random.default_rng(1)
    xy = np.vstack([_blob((0, 0), 200, rng), _blob((15, 0), 200, rng)])
    labels, info = umap_leiden_partition(xy, target_k=2, tol=0)
    assert info["n_clusters"] == 2
    assert len(labels) == 400
    # size-ranked: label 0 is the (joint-)largest cluster
    assert labels.value_counts().index[0] == 0


# ---------------------------------------------------------------- happy path
def test_planted_minor_is_found():
    a = _planted_adata(n_minor=60)
    res = dissect_partition(a, cluster_col="leiden")
    f = res.fragments
    # RNA cluster 0 splits into main core (300) + minor (60)
    f0 = f[f.parent == "0"].sort_values("rank")
    assert list(f0["subcluster"])[:2] == ["c0_0", "c0_1"]
    assert f0.iloc[0]["n_cells"] == 300 and bool(f0.iloc[0]["is_main"])
    assert f0.iloc[1]["n_cells"] == 60 and not bool(f0.iloc[1]["is_main"])
    assert bool(f0.iloc[1]["is_minor"])          # 60 >= default threshold 50
    # RNA cluster 1 stays whole: one fragment, no minors
    f1 = f[f.parent == "1"]
    assert len(f1) == 1 and bool(f1.iloc[0]["is_main"])


def test_min_subcluster_size_gates_is_minor():
    a = _planted_adata(n_minor=40)
    small = dissect_partition(a, cluster_col="leiden", min_subcluster_size=50)
    fr = small.fragments
    minor = fr[(fr.parent == "0") & (fr["rank"] == 1)].iloc[0]
    assert not bool(minor["is_minor"])           # 40 < 50 -> flagged off
    loose = dissect_partition(a, cluster_col="leiden", min_subcluster_size=10)
    minor = loose.fragments
    minor = minor[(minor.parent == "0") & (minor["rank"] == 1)].iloc[0]
    assert bool(minor["is_minor"])               # 40 >= 10 -> on


def test_labels_align_and_agree_with_fragments():
    a = _planted_adata()
    res = dissect_partition(a, cluster_col="leiden")
    assert list(res.labels.index) == list(a.obs_names)
    assert set(res.labels.columns) == {"umap_cluster", "subcluster", "rank", "is_main"}
    # per-cell labels aggregate to exactly the fragment sizes
    agg = res.labels.groupby("subcluster").size()
    for _, r in res.fragments.iterrows():
        assert agg[r["subcluster"]] == r["n_cells"]
    # overlap accounts for every cell
    assert int(res.overlap.values.sum()) == a.n_obs


def test_pure_function_does_not_touch_adata():
    a = _planted_adata()
    obs_before = a.obs.copy()
    obsm_before = np.array(a.obsm["X_umap"], copy=True)
    dissect_partition(a, cluster_col="leiden")
    pd.testing.assert_frame_equal(a.obs, obs_before)
    np.testing.assert_array_equal(a.obsm["X_umap"], obsm_before)


def test_granularity_matching_defaults_to_rna_k():
    a = _planted_adata()
    res = dissect_partition(a, cluster_col="leiden")   # umap_target_k=None
    # RNA side has 2 labels -> UMAP side lands within 2 +/- tol(2)
    assert abs(res.info["n_clusters"] - 2) <= 2


# ---------------------------------------------------------------- error paths
def test_missing_cluster_col_raises():
    a = _planted_adata()
    with pytest.raises(KeyError, match="cluster_col"):
        dissect_partition(a, cluster_col="nope")


def test_missing_umap_key_raises():
    a = _planted_adata()
    with pytest.raises(KeyError, match="umap_key"):
        dissect_partition(a, cluster_col="leiden", umap_key="X_missing")


def test_nan_coordinates_raise_with_count():
    a = _planted_adata()
    xy = np.array(a.obsm["X_umap"], copy=True)
    xy[3, 0] = np.nan
    xy[7, 1] = np.inf
    a.obsm["X_umap"] = xy
    with pytest.raises(ValueError, match="2 cells"):
        dissect_partition(a, cluster_col="leiden")


def test_one_dim_embedding_raises():
    a = _planted_adata()
    a.obsm["X_bad"] = np.zeros((a.n_obs, 1))
    with pytest.raises(ValueError, match=">=2 columns"):
        dissect_partition(a, cluster_col="leiden", umap_key="X_bad")


# ------------------------------------------------------- direct-matrix input
def test_umap_matrix_input_matches_obsm_path():
    a = _planted_adata()
    xy = np.array(a.obsm["X_umap"], copy=True)
    by_key = dissect_partition(a, cluster_col="leiden")
    by_mat = dissect_partition(a, cluster_col="leiden", umap_Nx2_mat=xy)
    pd.testing.assert_frame_equal(by_key.labels, by_mat.labels)
    pd.testing.assert_frame_equal(by_key.fragments, by_mat.fragments)


def test_umap_matrix_works_without_obsm():
    a = _planted_adata()
    xy = np.array(a.obsm["X_umap"], copy=True)
    del a.obsm["X_umap"]                        # obsm not consulted at all
    res = dissect_partition(a, cluster_col="leiden", umap_Nx2_mat=xy)
    assert int(res.overlap.values.sum()) == a.n_obs


def test_umap_matrix_wrong_length_raises():
    a = _planted_adata()
    with pytest.raises(ValueError, match="one row per cell"):
        dissect_partition(a, cluster_col="leiden",
                          umap_Nx2_mat=np.zeros((5, 2)))

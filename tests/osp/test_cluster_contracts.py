import anndata as ad
import numpy as np
import pandas as pd
import pytest

from osp.cluster import (
    _invalidate_stale_derived_outputs,
    _leiden_key,
    _remove_stale_primary_tables,
    cluster_and_deg,
    deg_two_groups,
)


def test_deg_group_named_rest_does_not_collide_with_internal_reference():
    matrix = np.log1p(
        np.array(
            [
                [8, 1, 0],
                [7, 1, 0],
                [9, 0, 1],
                [1, 8, 0],
                [1, 7, 1],
                [0, 9, 1],
            ],
            dtype=float,
        )
    )
    data = ad.AnnData(matrix, obs=pd.DataFrame({"group": ["rest"] * 3 + ["A"] * 3}))
    result = deg_two_groups(data, "group", "rest", hvg_only=False)
    assert np.isfinite(result["logfc"]).any()
    assert set(result["high_in"]) | set(result["low_in"]) == {"rest", "all other cells"}


def test_deg_rejects_partially_unknown_requested_groups():
    data = ad.AnnData(np.ones((4, 3)), obs=pd.DataFrame({"group": ["A", "A", "B", "B"]}))
    with pytest.raises(ValueError, match="UNKNOWN"):
        deg_two_groups(data, "group", ["A", "UNKNOWN"], hvg_only=False)


def test_reclustering_removes_only_stale_derived_outputs(tmp_path):
    for name in (
        "cluster_summary_leiden_r0.5.csv",
        "de_top_genes_leiden_r0.5.csv",
        "paga_connectivities_leiden_r0.5.csv",
        "decontx_top_genes_leiden_r0.5.csv",
        "cluster_summary_leiden_r1.0.csv",
        "de_top_genes_leiden_r1.0.csv",
        "paga_connectivities_leiden_r1.0.csv",
        "keep.txt",
        "report.html",
        "annotation_proposal.json",
    ):
        (tmp_path / name).touch()
    figures = tmp_path / "figures"
    figures.mkdir()
    (figures / "umap_clusters_leiden_r0.5.png").touch()
    (figures / "unrelated.png").touch()

    _remove_stale_primary_tables(tmp_path, "leiden_r1.0", has_decontx=False)
    _invalidate_stale_derived_outputs(tmp_path, figures)

    assert (tmp_path / "cluster_summary_leiden_r1.0.csv").exists()
    assert (tmp_path / "de_top_genes_leiden_r1.0.csv").exists()
    assert (tmp_path / "paga_connectivities_leiden_r1.0.csv").exists()
    assert not (tmp_path / "decontx_top_genes_leiden_r0.5.csv").exists()
    assert not (tmp_path / "report.html").exists()
    assert not (tmp_path / "annotation_proposal.json").exists()
    assert (tmp_path / "keep.txt").exists()
    assert (figures / "unrelated.png").exists()


def test_leiden_keys_name_integer_and_float_resolutions_alike():
    assert _leiden_key(1) == _leiden_key(1.0) == "leiden_r1.0"
    assert _leiden_key(np.float64(0.5)) == "leiden_r0.5"


def test_cluster_and_deg_rejects_duplicate_and_boolean_resolutions():
    data = ad.AnnData(np.ones((4, 3)), obs=pd.DataFrame({"sample": ["A"] * 4}))
    with pytest.raises(ValueError, match="duplicates"):
        cluster_and_deg(data, resolutions=(1, 1.0), primary_resolution=1, make_plots=False)
    with pytest.raises(ValueError, match="finite non-negative"):
        cluster_and_deg(data, resolutions=(True,), primary_resolution=True, make_plots=False)


def test_reclustering_also_drops_the_stale_qc_action_umap(tmp_path):
    figures = tmp_path / "figures"
    figures.mkdir()
    (figures / "umap_qc_action.png").touch()
    _invalidate_stale_derived_outputs(tmp_path, figures)
    assert not (figures / "umap_qc_action.png").exists()


def test_cluster_and_deg_embeds_the_three_cell_minimum():
    """3 QC survivors is the smallest set OSP accepts; umap-learn's spectral
    init needs > n_components+1 nodes and crashed on exactly this size
    (tabula-muris-facs Bladder plate B002771, 2026-09-06)."""
    rng = np.random.default_rng(0)
    counts = rng.poisson(3.0, size=(3, 80)).astype(np.float32)
    counts[0, :10] += 20  # give the cells some structure
    data = ad.AnnData(counts.copy(), obs=pd.DataFrame(index=[f"c{i}" for i in range(3)]))
    data.layers["counts"] = counts
    result = cluster_and_deg(data, resolutions=(1.0,), primary_resolution=1.0, make_plots=False)[0]
    assert result.obsm["X_umap"].shape == (3, 2)
    assert np.isfinite(result.obsm["X_umap"]).all()


def test_clustered_output_is_slim(tmp_path):
    """0.1.6: no .raw copy of X (DE and marker scores read X), int64 counts
    narrowed to int32, float32 PCA -- clustered.h5ad was ~1/3 duplicate bytes."""
    import h5py
    from scipy import sparse

    rng = np.random.default_rng(1)
    dense = rng.poisson(2.0, size=(60, 80)).astype(np.int64)
    dense[:30, :20] += 15  # two populations so DE has something to rank
    counts = sparse.csr_matrix(dense)
    data = ad.AnnData(counts.astype(np.float32), obs=pd.DataFrame(index=[f"c{i}" for i in range(60)]),
                      var=pd.DataFrame(index=[f"g{i}" for i in range(80)]))
    data.layers["counts"] = counts
    result, de_df, *_ = cluster_and_deg(data, resolutions=(1.0,), primary_resolution=1.0, make_plots=False,
                                        outdir=str(tmp_path), marker_genes={"pop1": ["g0", "g1", "absent"]})
    assert result.raw is None
    assert result.layers["counts"].dtype == np.int32 and (result.layers["counts"] != counts).nnz == 0
    assert result.obsm["X_pca"].dtype == np.float32
    assert "score_pop1" in result.obs and len(de_df)  # scoring and DE ran on X
    with h5py.File(tmp_path / "clustered.h5ad") as h:
        assert "raw" not in h and h["layers/counts/data"].dtype == np.int32 and h["obsm/X_pca"].dtype == np.float32

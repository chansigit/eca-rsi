import anndata as ad
import numpy as np
import pandas as pd
import pytest

from osp.cluster import (
    _invalidate_stale_derived_outputs,
    _remove_stale_primary_tables,
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

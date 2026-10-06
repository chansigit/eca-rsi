"""Regression checks for resume metadata and small-data output contracts."""


import anndata as ad
import numpy as np
import pandas as pd
import pytest
import scanpy as sc
from scipy import sparse

import msp.integrate as integrate
import msp.resources as resources
from msp.evidence import DegTables, cluster_order, load_paga_neighbors, load_removal_mask


def test_removal_mask_preserves_cell_ids_and_alignment(tmp_path):
    data = ad.AnnData(np.ones((4, 2)), obs=pd.DataFrame(index=["NA", "002", "001", "unlisted"]))
    pd.DataFrame(
        {
            "cell": ["001", "002", "NA"],
            "recommend_removal": [True, False, True],
        }
    ).to_csv(tmp_path / "preannotation_removal.csv", index=False)

    np.testing.assert_array_equal(load_removal_mask(tmp_path, data), [True, False, True, False])


def test_missing_removal_file_preserves_legacy_behavior(tmp_path):
    data = ad.AnnData(np.ones((2, 2)))
    np.testing.assert_array_equal(load_removal_mask(tmp_path, data), [False, False])


@pytest.mark.parametrize("contents", [None, "\n", "cluster,neighbor,rank,connectivity\n"])
def test_empty_paga_neighbors(tmp_path, contents):
    if contents is not None:
        (tmp_path / "paga_neighbors_k.csv").write_text(contents)
    assert load_paga_neighbors(tmp_path, "k") == {}


def test_paga_reader_preserves_ids_and_rank_order(tmp_path):
    (tmp_path / "paga_neighbors_k.csv").write_text("cluster,neighbor,rank,connectivity\n001,003,2,0.2\n001,002,1,0.8\n")
    assert load_paga_neighbors(tmp_path, "k") == {"001": ["002", "003"]}


def test_malformed_paga_schema_is_not_silently_ignored(tmp_path):
    (tmp_path / "paga_neighbors_k.csv").write_text("wrong_column\nvalue\n")
    with pytest.raises(KeyError):
        load_paga_neighbors(tmp_path, "k")


@pytest.mark.parametrize("second_size,connected", [(2, True), (12, True), (12, False)])
def test_cluster_annotations_preserve_csv_contract(tmp_path, monkeypatch, second_size, connected):
    """Run real Wilcoxon tests with a controlled two-cluster neighbor graph."""
    rng = np.random.default_rng(4)
    data = ad.AnnData(
        np.log1p(rng.poisson(2, (12 + second_size, 8))).astype(float),
        obs=pd.DataFrame(
            {"k": pd.Categorical(["0"] * 12 + ["1"] * second_size)},
            index=[f"c{i}" for i in range(12 + second_size)],
        ),
    )
    data.raw = data.copy()

    def paga(subset, groups):
        edge = 1 if connected else 0
        subset.uns["paga"] = {"connectivities": sparse.csr_matrix([[0, edge], [edge, 0]])}

    # Graph construction is unrelated to the DEG serialization regression.
    monkeypatch.setattr(sc.pp, "neighbors", lambda *args, **kwargs: None)
    monkeypatch.setattr(sc.tl, "paga", paga)
    monkeypatch.setattr(resources, "available_cpus", lambda: 1)
    integrate._cluster_annotations(data, np.zeros(data.n_obs, dtype=bool), ["k"], [1.0], tmp_path)

    global_df = pd.read_csv(tmp_path / "deg_global_k.csv", dtype={"group": str})
    expected_groups = {"0"} if second_size < 10 else {"0", "1"}
    assert set(global_df["group"]) == expected_groups
    assert set(global_df.columns) == {
        "group",
        "names",
        "scores",
        "logfoldchanges",
        "pvals",
        "pvals_adj",
        "pct1",
        "pct2",
    }
    assert global_df.groupby("group").size().to_dict() == dict.fromkeys(expected_groups, data.n_vars)
    assert data.n_obs == 12 + second_size
    neighbors = pd.read_csv(tmp_path / "paga_neighbors_k.csv")
    assert list(neighbors.columns) == ["cluster", "neighbor", "rank", "connectivity"]
    if connected:
        local_df = pd.read_csv(tmp_path / "deg_local_k.csv", dtype={"group": str})
        assert set(local_df["group"]) == expected_groups
    else:
        assert neighbors.empty
        assert load_paga_neighbors(tmp_path, "k") == {}


def test_deg_lookup_limits_each_view_after_filtering(tmp_path):
    rows = pd.DataFrame(
        {
            "group": ["0"] * 4,
            "names": ["G0", "G1", "G2", "G3"],
            "logfoldchanges": [0.1, 2.0, 3.0, 4.0],
            "pvals_adj": [0.01] * 4,
            "pct1": [0.8] * 4,
            "pct2": [0.1] * 4,
        }
    )
    for view in ("global", "local"):
        rows.to_csv(tmp_path / f"deg_{view}_k.csv", index=False)
    with DegTables(tmp_path, base_key="k") as tables:
        result = tables.lookup(cluster="0", top_n=2, min_logfc=1)
        assert "4 row(s) of 6 passing" in result
        assert result.count("G1 #2") == result.count("G2 #3") == 2
        assert "G0 #1" not in result and "G3 #4" not in result


def test_deg_tables_skip_per_cell_csvs_but_keep_summaries(tmp_path):
    pd.DataFrame({"cell": ["a", "b"], "recommend_removal": [True, False]}).to_csv(
        tmp_path / "preannotation_removal.csv", index=False
    )
    pd.DataFrame({"cell": ["a"], "recommend_removal": [True]}).to_csv(tmp_path / "cell_outliers.csv", index=False)
    pd.DataFrame({"key": ["k"], "cluster": ["0"], "stress": [False]}).to_csv(
        tmp_path / "stress_clusters.csv", index=False
    )
    with DegTables(tmp_path, base_key="k") as tables:
        assert set(tables.extra_tables) == {"stress_clusters"}
        assert "no rows" not in tables.sql("SELECT cluster FROM stress_clusters")
        assert tables.sql("SELECT * FROM cell_outliers").startswith("SQL error")


def test_cluster_order_accepts_any_iterable_and_non_numeric_ids():
    assert cluster_order(["10", "2", "5,1", "5,0"]) == ["2", "5,0", "5,1", "10"]
    assert cluster_order(["5,10", "5,2", "5"]) == ["5", "5,2", "5,10"]  # every part numeric, not just the first
    assert cluster_order(pd.Series(["b", "a", "b"])) == ["a", "b"]
    assert cluster_order(["c1_0", "c0_1", "c0_0"]) == ["c0_0", "c0_1", "c1_0"]  # standissect ids: string order
    assert cluster_order(iter(["1", "1", "0"])) == ["0", "1"]


def test_deg_tables_skip_oversized_csvs_but_list_them(tmp_path, monkeypatch):
    pd.DataFrame({"key": ["k"], "cluster": ["0"], "stress": [False]}).to_csv(
        tmp_path / "stress_clusters.csv", index=False
    )
    pd.DataFrame({"a": range(200)}).to_csv(tmp_path / "large_export.csv", index=False)
    monkeypatch.setattr(DegTables, "MAX_EXTRA_TABLE_BYTES", 100)
    with DegTables(tmp_path, base_key="k") as tables:
        assert tables.skipped_tables == ["large_export"]
        assert set(tables.extra_tables) == {"stress_clusters"}
        assert "large_export" in tables.sql("schema") and "not loaded" in tables.schema_text()

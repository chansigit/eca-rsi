"""Exercise resume identity, interrupted work, tool parsing and child ownership."""

import copy
import importlib

import numpy as np
import pandas as pd
import scanpy as sc
from anndata import AnnData
from scipy import sparse


lineage = importlib.import_module("zmip.lineage")
plan_module = importlib.import_module("zmip.plan")
annotate = importlib.import_module("zmip.annotate")


def test_graph_only_paga_matches_full_object_without_mutation(tmp_path, monkeypatch):
    n = 12
    ad = AnnData(
        np.ones((n, 3), dtype="float32"),
        obs=pd.DataFrame(
            {"coarse": pd.Categorical(["A"] * 6 + ["B"] * 6), "sample": "s"}, index=[f"c{i}" for i in range(n)]
        ),
    )
    graph = sparse.csr_matrix(np.ones((n, n)) - np.eye(n))
    ad.obsp["connectivities"] = graph
    ad.obsp["distances"] = graph.copy()
    ad.uns["neighbors"] = {
        "connectivities_key": "connectivities",
        "distances_key": "distances",
        "params": {"n_neighbors": 5},
    }
    ad.layers["counts"] = ad.X.copy()
    ad.raw = ad
    original_obs, original_uns = ad.obs.copy(), copy.deepcopy(ad.uns)
    full = ad.copy()
    sc.tl.paga(full, groups="coarse")
    expected = full.uns["paga"]["connectivities"].toarray().round(3)
    monkeypatch.setattr(plan_module, "save_single_umap", lambda *a, **k: None)
    _, _, paga, _ = plan_module.lineage_evidence(ad, "coarse", "sample", str(tmp_path))
    assert paga is not None
    np.testing.assert_array_equal(paga.loc[["A", "B"], ["A", "B"]].values, expected)
    pd.testing.assert_frame_equal(ad.obs, original_obs)
    assert ad.uns == original_uns
    np.testing.assert_array_equal(ad.layers["counts"], ad.X)


def test_shared_island_requires_written_review_at_any_edge_share():
    counts = pd.DataFrame({"n_cells": [1000, 1000]}, index=["A", "B"])
    islands = pd.DataFrame({"island_1": [100.0, 100.0]}, index=["A", "B"])
    candidate = {"lineages": [{"name": x, "coarse_labels": [x]} for x in counts.index], "confirm_shared_islands": True}
    for mix in (2.0, 8.5, 15.0):
        knn = pd.DataFrame([[100 - mix, mix], [mix, 100 - mix]], index=counts.index, columns=counts.index)
        problems, result = plan_module.validate_plan(candidate, list(counts.index), counts, 800, islands, knn)
        assert result is None and any("shared_island_reviews" in p for p in problems)
        reviewed = {
            **candidate,
            "shared_island_reviews": {"island_1": "Distinct marker programs; graph separation uncertain"},
        }
        problems, result = plan_module.validate_plan(reviewed, list(counts.index), counts, 800, islands, knn)
        assert not problems and result["shared_island_reviews"] == reviewed["shared_island_reviews"]
        assert result["host_warnings"]
        reviewed["shared_island_reviews"]["island_1"] = ""
        assert plan_module.validate_plan(reviewed, list(counts.index), counts, 800, islands, knn)[0]

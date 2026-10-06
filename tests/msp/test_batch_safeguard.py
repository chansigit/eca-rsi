"""Batch composition may request review, but never proves invalid cells."""

import copy

import anndata as ad
import numpy as np
import pandas as pd

from msp import annotate, inspect


def proposal():
    return {
        "cluster_key": annotate.BASE_KEY,
        "clusters": [
            {
                "cluster": "0",
                "verdict": "artifact-batch",
                "action": "drop",
                "confidence": "low",
                "tests": dict.fromkeys(
                    ["markers", "qc", "composition", "geometry", "stability"], "Unresolved sample/condition confounding"
                ),
                "rationale": "One sample dominates this cluster",
            },
            {
                "cluster": "1",
                "verdict": "artifact-doublet",
                "action": "drop",
                "confidence": "low",
                "tests": dict.fromkeys(
                    ["markers", "qc", "composition", "geometry", "stability"], "Independent evidence needs review"
                ),
                "rationale": "Existing low-confidence policy is unchanged",
            },
        ],
    }


def data():
    obs = pd.DataFrame(
        {
            annotate.BASE_KEY: pd.Categorical(["0", "0", "1"]),
            annotate.PARENT_KEY: pd.Categorical(["0", "0", "1"]),
            "batch": pd.Categorical(["a", "a", "b"]),
            "doublet_score": [0.1, 0.9, 0.2],
        },
        index=["c0", "c1", "c2"],
    )
    obj = ad.AnnData(np.ones((3, 2)), obs=obs)
    obj.layers["counts"] = obj.X.copy()
    obj.uns["msp"] = {"batch_col": "batch"}
    obj.obsm["X_umap"] = np.array([[0.0, 0.0], [0.0, 1.0], [1.0, 1.0]])
    return obj


def test_guard_is_auditable_idempotent_and_does_not_override_other_low_confidence():
    p = proposal()
    original = copy.deepcopy(p)
    assert inspect._guard_batch_actions(p) is p
    entry = p["clusters"][0]
    assert entry["action"] == "flag" and entry["requested_action"] == "drop"
    assert entry["host_adjustment"]["policy"] == "batch_verdict_non_destructive_v1"
    assert entry["tests"] == original["clusters"][0]["tests"]
    assert p["clusters"][1] == original["clusters"][1]
    snapshot = copy.deepcopy(p)
    assert inspect._guard_batch_actions(p) == snapshot


def test_saved_proposal_apply_cannot_bypass_guard_and_keeps_independent_cell_qc():
    obj = data()
    p = proposal()
    p["cell_actions"] = [
        {
            "cluster": "0",
            "metric": "doublet_score",
            "op": ">",
            "value": 0.8,
            "action": "drop",
            "reason": "doublet",
            "note": "Independent cell-level QC",
        }
    ]
    before = obj.layers["counts"].copy()
    inspect._apply_proposal(obj, annotate.BASE_KEY, p)
    assert obj.obs["_msp_action"].tolist() == ["flag", "drop", "drop"]
    assert p["clusters"][0]["action"] == "flag"
    np.testing.assert_array_equal(obj.layers["counts"], before)



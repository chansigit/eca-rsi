import json

import anndata as ad
import numpy as np
import pandas as pd
import pytest

from osp import annotate


def _proposal():
    return {
        "clusters": [
            {
                "cluster": "0",
                "label_coarse": "Myeloid",
                "label_fine": "Monocyte",
                "confidence": "high",
                "evidence_genes": ["LYZ"],
                "doubts": "",
            },
            {
                "cluster": "1",
                "label_coarse": "Lymphoid",
                "label_fine": "T cell",
                "confidence": "medium",
                "evidence_genes": ["CD3D"],
                "doubts": "subtype uncertain",
            },
        ],
        "qc_actions": [],
        "threshold_suggestions": [],
        "overall": "Usable",
    }


def _obs():
    return pd.DataFrame({"score": [0.1, 0.9], "text": ["a", "b"]})


def test_valid_proposal_is_accepted():
    assert annotate._validate_proposal(_proposal(), ["0", "1"], _obs()) == []


def test_validator_handles_non_object_and_rejects_bad_nested_values():
    assert "JSON object" in annotate._validate_proposal([], ["0"], _obs())[0]

    proposal = _proposal()
    proposal["clusters"] = [proposal["clusters"][0], "not an object"]
    proposal["qc_actions"] = [
        {
            "cluster": "0",
            "scope": "cells",
            "metric": "text",
            "op": ">",
            "value": float("nan"),
            "action": "remove",
            "reason": "guess",
            "note": 4,
        }
    ]
    problems = annotate._validate_proposal(proposal, ["0", "1"], _obs())
    assert any("must be an object" in problem for problem in problems)
    assert any("must be drop|flag" in problem for problem in problems)
    assert any("finite and numeric" in problem for problem in problems)
    assert any("numeric obs column" in problem for problem in problems)


def test_validator_rejects_duplicate_missing_and_unknown_clusters():
    proposal = _proposal()
    proposal["clusters"] = [proposal["clusters"][0], proposal["clusters"][0]]
    problems = annotate._validate_proposal(proposal, ["0", "1"], _obs())
    assert any("more than once" in problem for problem in problems)
    assert any("without an annotation" in problem for problem in problems)

    proposal["clusters"][1] = {**proposal["clusters"][0], "cluster": "9"}
    problems = annotate._validate_proposal(proposal, ["0", "1"], _obs())
    assert any("unknown clusters" in problem for problem in problems)


def test_drop_action_wins_over_flag_action():
    data = ad.AnnData(np.ones((2, 2)), obs=pd.DataFrame({"cluster": ["0", "1"]}))
    proposal = _proposal()
    proposal["qc_actions"] = [
        {
            "cluster": "0",
            "scope": "cluster",
            "action": "drop",
            "reason": "other",
            "note": "drop",
        },
        {
            "cluster": "0",
            "scope": "cluster",
            "action": "flag",
            "reason": "other",
            "note": "flag",
        },
    ]
    annotate._apply_proposal(data, "cluster", proposal)
    assert data.obs["_qc_action"].astype(str).tolist() == ["drop", "keep"]


def test_prompt_requires_decontx_figure_only_when_present(tmp_path):
    prompt = annotate._system_prompt(tmp_path, "leiden_r1.0", ["0"], None, None, "English")
    assert "every qc_violin_*, and the decontx heatmap" not in prompt

    figures = tmp_path / "figures"
    figures.mkdir()
    (figures / "decontx_heatmap_by_cluster.png").touch()
    prompt = annotate._system_prompt(tmp_path, "leiden_r1.0", ["0"], None, None, "English")
    assert "every qc_violin_*, and the decontx heatmap" in prompt


def test_proposal_completion_marker_is_published_last(tmp_path, monkeypatch):
    data = ad.AnnData(
        np.ones((2, 2)),
        obs=pd.DataFrame({"leiden_r1.0": pd.Categorical(["0", "1"])}),
    )
    data.uns["paga"] = {"groups": "leiden_r1.0"}
    proposal_path = tmp_path / "annotation_proposal.json"
    proposal_path.write_text(json.dumps({"old": True}), encoding="utf-8")
    events = []

    async def fake_run_agent(*args, **kwargs):
        return _proposal() | {"cluster_key": "leiden_r1.0"}

    monkeypatch.setattr(annotate.sc, "read_h5ad", lambda path: data)
    monkeypatch.setattr(annotate, "_run_agent", fake_run_agent)

    def fake_plot(*args, **kwargs):
        assert not proposal_path.exists()
        events.append("plot")

    monkeypatch.setattr(annotate, "_plot_annotation", fake_plot)
    monkeypatch.setattr(annotate, "atomic_write_h5ad", lambda *args: events.append("h5ad"))
    monkeypatch.setattr(annotate, "generate_report", lambda *args, **kwargs: events.append("report") or "report.html")
    monkeypatch.setattr(annotate, "atomic_write_json", lambda *args: events.append("proposal"))

    annotate.propose_annotation(tmp_path, model="test-model")
    assert events == ["plot", "h5ad", "report", "proposal"]


def test_failed_agent_rerun_does_not_leave_old_completion_marker(tmp_path, monkeypatch):
    data = ad.AnnData(
        np.ones((2, 2)),
        obs=pd.DataFrame({"leiden_r1.0": pd.Categorical(["0", "1"])}),
    )
    data.uns["paga"] = {"groups": "leiden_r1.0"}
    proposal_path = tmp_path / "annotation_proposal.json"
    proposal_path.write_text(json.dumps({"old": True}), encoding="utf-8")

    async def failed_agent(*args, **kwargs):
        raise RuntimeError("provider failed")

    monkeypatch.setattr(annotate.sc, "read_h5ad", lambda path: data)
    monkeypatch.setattr(annotate, "_run_agent", failed_agent)
    with pytest.raises(RuntimeError, match="provider failed"):
        annotate.propose_annotation(tmp_path, model="test-model")
    assert not proposal_path.exists()

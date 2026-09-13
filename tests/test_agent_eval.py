import importlib
import json
import logging
from pathlib import Path

import pytest


def modules(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "eval"))
    return importlib.import_module("extract"), importlib.import_module("replay")


def source(tmp_path):
    root = tmp_path / "units/unit/rounds/round01/zoomin"
    lineage = root / "Mural"
    (lineage / "figures").mkdir(parents=True)
    (root / "zmip_plan.json").write_text(
        json.dumps(
            {
                "lineages": [
                    dict(name="Mural", coarse_labels=["Mural"], n_cells=4, zoom=True)
                ]
            }
        )
    )
    (root / "lineage_markers.csv").write_text("lineage,gene\n")
    (lineage / "annotation_proposal.json").write_text(json.dumps({"clusters": []}))
    (lineage / "integrated.h5ad").write_bytes(b"input")
    (lineage / "deg_global_msp_leiden_r2.0.csv").write_text("group,names\n0,G1\n")
    for name in (
        "annotation_notes.md",
        ".annotation-progress.json",
        "annotation_removed.csv",
    ):
        (lineage / name).write_text("OLD ANSWER")
    for name in ("annotation_umap_fine.png", "umap_batch.png"):
        (lineage / "figures" / name).write_bytes(b"image")
    (lineage / ".msp-history").mkdir()
    (lineage / ".msp-history/old-answer.json").write_text("OLD ANSWER")
    return lineage


def test_fixture_excludes_answers_is_immutable_and_requires_fresh_outputs(
    tmp_path, monkeypatch
):
    extract, replay = modules(monkeypatch)
    lineage = source(tmp_path)
    fixture = extract.extract(lineage, tmp_path / "fixtures", [])
    identity = extract.verify_fixture(fixture)
    files = {
        str(p.relative_to(fixture / "inputs"))
        for p in (fixture / "inputs").rglob("*")
        if p.is_file()
    }
    assert files == {
        "integrated.h5ad",
        "deg_global_msp_leiden_r2.0.csv",
        "lineage_markers.csv",
        "zmip_plan.json",
        "figures/umap_batch.png",
    }
    with pytest.raises(FileExistsError):
        extract.extract(lineage, tmp_path / "fixtures", [])
    (fixture / "inputs/extra.json").write_text("{}")
    with pytest.raises(ValueError, match="fixture changed"):
        extract.verify_fixture(fixture)
    (fixture / "inputs/extra.json").unlink()
    assert extract.verify_fixture(fixture) == identity


def test_replay_records_failure_and_removes_its_log_handler(tmp_path, monkeypatch):
    pytest.importorskip("zmip")
    import anndata as ad
    import numpy as np

    extract, replay = modules(monkeypatch)
    lineage = source(tmp_path)
    data = ad.AnnData(np.ones((4, 2)))
    data.uns["msp"] = {"species": "human", "resolutions": [1.0, 2.0]}
    data.write_h5ad(lineage / "integrated.h5ad")
    fixture = extract.extract(lineage, tmp_path / "fixtures", [])
    monkeypatch.delenv("AGENT_MODEL_POOL", raising=False)

    def fail(*args, **kwargs):
        raise RuntimeError("agent did not submit")

    monkeypatch.setattr("zmip.annotate.annotate_lineage", fail)
    monkeypatch.setattr("zmip.foreign.score_foreign", lambda *a: [])
    logger = logging.getLogger()
    before = list(logger.handlers), logger.level
    result = replay.run(fixture, tmp_path / "work", "test")
    assert result["status"] == "failed" and not result["contract"]["passed"]
    assert result["agreement"] is None and result["cost_usd"] is None
    assert (list(logger.handlers), logger.level) == before
    with pytest.raises(ValueError, match="already exists"):
        replay.run(fixture, tmp_path / "work", "another-model")


def test_errors_usage_and_label_partitions_are_scored_separately(monkeypatch):
    _, replay = modules(monkeypatch)
    watch = replay._Watch()
    for message in (
        "tool error in check_deg: unknown cluster",
        "tool error in submit_cluster: JSON parse error",
        "HARNESS=openai@openrouter model=vendor/model:free run: 2 model request(s), 100 input / 20 output tokens (4 reasoning)",
        "agent cost: $0.00",
    ):
        watch.emit(logging.makeLogRecord({"msg": message}))
    assert len(watch.rejections) == len(watch.tool_errors) == 1
    assert watch.cost == 0 and watch.usage == dict(
        requests=2, input=100, output=20, reasoning=4
    )

    def proposal(labels):
        return {
            "clusters": [
                dict(
                    cluster_id=str(i),
                    action="keep",
                    coarse_label="Mural",
                    fine_label=label,
                )
                for i, label in enumerate(labels)
            ]
        }

    result = replay.agreement(proposal(["X", "X", "Y"]), proposal(["A", "A", "B"]))
    assert result["fine_partition_agreement"] == 1
    assert result["discriminating"]["action"] is False

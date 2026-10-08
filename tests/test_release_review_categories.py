"""#47: every needs_review category documented in review.KINDS is produced by the gen2 release builder
(stages/release.review_items), so a category can no longer exist on paper and in the gen1 pages only (#46).
review.collect, the gen1 reader, is frozen: it renders runs of the removed layout and decides nothing new."""
import json

import pandas as pd

from ecarsi import layout as L
from ecarsi.files import reference, save
from ecarsi.review import KINDS
from ecarsi.stages.release import review_items

LEGACY_ONLY = {"agent_config_changed"}  # written by the local path, removed in 0.4.0; kept so old releases render


def release_inputs(tmp_path):
    run = tmp_path / "run"
    unit_dir = run / "units" / "u"
    unit_dir.mkdir(parents=True)
    L.organize_manifest(run).parent.mkdir()
    save(L.organize_manifest(run), dict(source_inventory=[dict(name="srcX", state="rejected")]))
    (run / "degraded").mkdir()
    (run / "degraded" / "1-a.json").write_text(json.dumps(dict(stage="round01.display", what="display sync", error="boom")))
    save(tmp_path / "bundle.json", dict(sample="S1", validation=dict(qc_summary=dict(n_mito_genes=0))))
    save(tmp_path / "per-sample.json", dict(samples=[reference(tmp_path / "bundle.json")],
                                            skipped_samples=[dict(sample="S9", n_cells=7, error="session died")]))
    rounds = []
    for number in (1, 2):
        save(tmp_path / f"round{number}.json", dict(round=number, stats=dict(frac=.2, removed=20, n_in=100, reason="cap")))
        rounds.append(reference(tmp_path / f"round{number}.json"))
    unit = dict(per_sample=reference(tmp_path / "per-sample.json"), rounds=rounds, forced_release=True,
                unit=dict(path=str(unit_dir)))
    low = json.dumps([{"code": "quality_decision", "detail": {"action": "drop", "confidence": "low"}}])
    rows = [dict(cell_uid="p0", round=0, release_stage="per-sample", operation="persample.partition", reason="blank donor"),
            dict(cell_uid="r0", round=1, release_stage="round01.cross-sample", operation="", reason=low),
            dict(cell_uid="f0", round=1, release_stage="round01.cross-sample", operation="",
                 reason=json.dumps([{"code": "fragment_qc", "detail": {"tests": ["decontX"]}}])),
            dict(cell_uid="s0", round=1, release_stage="round01.cross-sample", operation="", sample_id="S5",
                 reason=json.dumps([{"code": "sample_excluded", "detail": "too few cells"}]))]
    source = lambda name: dict(path=str(tmp_path / name), sha256="0" * 64)
    decisions = [
        dict(round=1, stage="cross-sample", scope="", source=source("inspection_proposal.json"),
             value=dict(clusters=[dict(cluster_id="3", action="flag", verdict="ambiguous", confidence="low", rationale="r")])),
        dict(round=1, stage="cross-sample", scope="", source=source("annotation_proposal.json"),
             value=dict(boundary_reviews=[dict(uncertain=True, coarse_labels=["A", "B"], evidence="e")],
                        clusters=[dict(cluster_id="5", coarse_label="A", fine_label="a", confidence="low", action="keep")])),
        dict(round=1, stage="zoom-in", scope="", source=source("zmip_plan.json"),
             value=dict(host_warnings=["islands split"], lineages=[dict(name="Tiny", zoom=False, n_cells=30, reason="host: min")])),
        dict(round=1, stage="zoom-in", scope="", source=source("annotation_proposal.json"),
             value=dict(types=dict(clusters=[]), quality=dict(clusters=[dict(cluster_id="1", decisions=[
                 dict(action="reassign", reassign_to="B", type_clusters=["0"], fine_label="x", confidence="medium")])]))),
    ]
    ledger = pd.DataFrame({"final_status": ["kept"], "retained_stage": ["round01.cross-sample"],
                           "retained_state": ["stress"], "round01.cross-sample_coarse": ["Chondrocyte"]})
    return unit, pd.DataFrame(rows), decisions, unit_dir, ledger


def test_every_documented_category_comes_out_of_a_gen2_release(tmp_path):
    unit, exclusions, decisions, root, ledger = release_inputs(tmp_path)
    produced = {item.kind for item in review_items(unit, exclusions, decisions, root=root, ledger=ledger)}
    assert produced == {kind for kind, _, _ in KINDS} - LEGACY_ONLY

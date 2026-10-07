"""#46: a sample the inclusion agent kept out of integration is one needs_review item per sample."""
import json
import pandas as pd


def test_release_lists_every_sample_the_inclusion_agent_excluded(tmp_path):
    from ecarsi.files import reference, save
    from ecarsi.stages.release import review_items
    save(tmp_path / "per-sample.json", dict(samples=[]))
    unit = dict(per_sample=reference(tmp_path / "per-sample.json"), rounds=[], forced_release=False)
    why = json.dumps([{"code": "sample_excluded", "detail": "Only 5 cells after QC", "evidence": {"path": "x"}}])
    rows = [dict(cell_uid=f"c{i}", round=1, release_stage="round01.cross-sample", sample_id="S_tiny", reason=why)
            for i in range(5)]
    rows.append(dict(cell_uid="d0", round=1, release_stage="round01.cross-sample", sample_id="S_other", reason=why))
    rows.append(dict(cell_uid="q0", round=0, release_stage="per-sample", sample_id="S_tiny", reason="mad_outlier"))
    items = review_items(unit, pd.DataFrame(rows), [])
    excluded = [(i.kind, i.round, i.scope, i.n_cells, i.action) for i in items if i.kind == "sample_excluded"]
    assert excluded == [("sample_excluded", 1, "S_other", 1, "exclude"), ("sample_excluded", 1, "S_tiny", 5, "exclude")]
    assert "Only 5 cells" in items[-1].note
    assert not [i for i in items if i.kind == "removed"]  # an exclusion is not a low-confidence removal

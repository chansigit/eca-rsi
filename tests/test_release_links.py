"""#26: needs_review links a decision to its copy inside the unit, which outlives the pruned pool request."""
import json
from pathlib import Path

from ecarsi.stages.release import local_link
from ecarsi.files import file_digest


def entry(tmp_path, original, stage, number=0, scope=''):
    return dict(round=number, stage=stage, scope=scope, source=dict(path=str(original), sha256=file_digest(original)))


def test_a_decision_links_its_identical_copy_in_the_unit_or_keeps_the_pool_path(tmp_path):
    from zmip.report import slug
    pool, unit = tmp_path / "pool" / "requests" / "r" / "a" / "outputs", tmp_path / "unit"
    pool.mkdir(parents=True)
    cases = [("per-sample", 0, "S1", "annotation_proposal.json", "01-per-sample/S1/annotation_proposal.json"),
             ("cross-sample", 2, "", "inspection_proposal.json", "rounds/round02/02-cross-sample/inspection_proposal.json"),
             ("zoom-in", 2, "", "zmip_plan.json", "rounds/round02/03-zoom-in/zmip_plan.json"),
             ("zoom-in", 2, "T cell", "annotation_proposal.json", f"rounds/round02/03-zoom-in/{slug('T cell')}/annotation_proposal.json")]
    for stage, number, scope, name, rel in cases:
        original = pool / name
        original.write_text(json.dumps({"stage": stage, "scope": scope}))
        e = entry(tmp_path, original, stage, number, scope)
        assert local_link(unit, e) == str(original)          # no copy yet: the original
        (unit / rel).parent.mkdir(parents=True, exist_ok=True)
        (unit / rel).write_text("stale")
        assert local_link(unit, e) == str(original)          # a copy that differs is not the decision
        (unit / rel).write_text(original.read_text())
        assert local_link(unit, e) == rel

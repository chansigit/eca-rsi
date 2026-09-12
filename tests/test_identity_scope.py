"""The agent runtime (agent-harness-bridge) is provenance, never identity: a
bridge patch release must not invalidate an in-flight unit (2026-09-12,
0.2.11 -> 0.2.12 did exactly that under the old rule)."""

import pytest

from ecarsi.run_state import runtime_identity, source_provenance


def test_runtime_identity_excludes_the_agent_runtime():
    assert set(runtime_identity()["packages"]) == {"ecarsi", "osp"}


def test_provenance_records_the_bridge_version_instead():
    prov = source_provenance()
    assert "harness_bridge" in prov
    assert prov["harness_bridge"]["version"]  # recorded for humans, never compared


def test_runtime_check_can_be_skipped_while_developing(tmp_path, monkeypatch):
    """ECA_RSI_DEVELOPER_MODE=1: a changed runtime no longer fails resume;
    input/config changes still do."""
    from ecarsi import downstream as D

    inp = tmp_path / "in.h5ad"
    inp.write_bytes(b"x")
    out = tmp_path / "stage"
    out.mkdir()
    monkeypatch.setattr(D, "kernel_runtime", lambda py, kernel: {"v": 1})
    monkeypatch.setattr(D, "computational_config", lambda config: {"c": 1})
    D.prepare("python", "msp", [str(inp)], out, {})

    monkeypatch.setattr(D, "kernel_runtime", lambda py, kernel: {"v": 2})
    monkeypatch.delenv("ECA_RSI_DEVELOPER_MODE", raising=False)
    with pytest.raises(ValueError, match="ECA_RSI_DEVELOPER_MODE"):
        D.prepare("python", "msp", [str(inp)], out, {})

    monkeypatch.setenv("ECA_RSI_DEVELOPER_MODE", "1")
    D.prepare("python", "msp", [str(inp)], out, {})  # runtime differs: accepted

    monkeypatch.setattr(D, "computational_config", lambda config: {"c": 2})
    with pytest.raises(ValueError, match="changed"):
        D.prepare("python", "msp", [str(inp)], out, {})  # config differs: still refused


def test_round_one_is_exempt_from_the_over_budget_review_flag():
    from ecarsi.review import _loop_items

    st = {"frac": 0.25, "removed": 250, "n_in": 1000, "reason": "round 1 never releases"}
    assert _loop_items(1, st, forced=False, last=False) == []
    assert [i.action for i in _loop_items(2, st, forced=False, last=False)] == ["over budget"]


def test_persample_developer_resume_keeps_verified_sample_receipts(tmp_path, monkeypatch):
    from ecarsi import persample, layout as L
    from ecarsi.run_state import read_json
    from tests.test_front_integration import matrix

    source, out = tmp_path / "input.h5ad", tmp_path / "samples"
    matrix().write_h5ad(source)
    monkeypatch.setattr(persample, "_kernel_runtime", lambda py: {"version": "old"})
    args = [str(source), str(out), "--sample-column", "sample", "--no-annotate"]
    assert persample.main(args + ["--plan-only"]) == 0
    saved = {s["dir"]: s["identity"] for s in read_json(out / L.MANIFEST)["samples"]}
    monkeypatch.setattr(persample, "is_finished", lambda path, annotate, identity: saved.get(str(path)) == identity)
    monkeypatch.setattr(persample, "is_empty", lambda *a: False)
    monkeypatch.setattr(persample, "drive", lambda *a, **k: pytest.fail("verified sample was recomputed"))
    monkeypatch.setattr(persample, "_kernel_runtime", lambda py: {"version": "new"})
    monkeypatch.setenv("ECA_RSI_DEVELOPER_MODE", "1")
    assert persample.main(args) == 0
    assert persample.main(args) == 0
    state = read_json(out / L.MANIFEST)
    assert state["state"] == "complete" and state["runtime_check"] == "skipped"
    assert {s["dir"]: s["identity"] for s in state["samples"]} == saved
    assert persample.main(args + ["--resolution", "0.8"]) == 1


def test_uncertain_boundary_is_visible_in_release_review():
    from ecarsi.review import _annotation_items, to_markdown, to_html
    prop = {"boundary_reviews": [{"coarse_labels": ["Stromal", "Fibroblast"],
                                 "evidence": "Lineage distinction remains unresolved", "uncertain": True}]}
    items = _annotation_items(3, "crosssample", "", prop, {}, {}, "report.html")
    assert len(items) == 1 and items[0].kind == "annotation_boundary"
    assert "Lineage distinction remains unresolved" in to_markdown(items, "test", 3)
    assert "Lineage distinction remains unresolved" in to_html(items)

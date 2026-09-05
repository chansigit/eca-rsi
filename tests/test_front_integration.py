"""Front-pipeline regressions; no MSP/ZMIP imports and no live model calls."""
from __future__ import annotations

from tests.bridge_contract import BRIDGE_LEGACY_API

import json
from pathlib import Path

import anndata as ad
import numpy as np
import pandas as pd
import pytest
from scipy import sparse

from ecarsi import layout as L, organize, persample
from ecarsi.osp_contract import INPUT_CELLS, is_done, output_identities, validate_outputs
from ecarsi.run_state import file_identity, read_json, write_json, writer_lock
from ecarsi.sample_mapping import SAMPLE_KEY, build_mapping
from ecarsi.upstream import column_values, inspect_unit


def matrix(n=6):
    counts = sparse.csr_matrix(np.arange(n * 4).reshape(n, 4) + 1, dtype=float)
    a = ad.AnnData(counts.copy(), obs=pd.DataFrame({"sample": ["S1"] * n}, index=[f"cell{i}" for i in range(n)]))
    a.layers["counts"] = counts
    a.X.data = np.log1p(a.X.data)
    return a


def source(root, name="A", *, status="ok", code=0, species="mouse", n=6, write_h5=True):
    step = root / name / "standardize"
    step.mkdir(parents=True)
    a = matrix(n)
    if write_h5:
        a.write_h5ad(step / "standardized.h5ad")
    result = {"schema_version": 2, "step": "standardize", "step_version": "0.2.0",
              "status": status, "exit_code": code, "species": {"resolved": species},
              "metrics": {"n_cells": n, "n_vars": 4}, "reasons": ["review me"] if status == "needs_review" else [],
              "output": "/before/move/standardized.h5ad" if write_h5 else None}
    write_json(step / "result.json", result)
    return step


def plan_file(path, names=("A",), split=False):
    members = [{"source": name, "obs_filter": None} for name in names]
    plan = {"analysis_units": [{"name": "test-unit", "members": members,
                                "rationale": "same tissue", "batch_key_hint": None}], "notes": "test"}
    write_json(path, plan)
    return path


@pytest.fixture(autouse=True)
def no_index(monkeypatch):
    monkeypatch.setattr("ecarsi.index.write_all", lambda *args: None)


def organize_two(tmp_path):
    root, out = tmp_path / "inputs", tmp_path / "out"
    for name in ("A", "B"):
        step = source(root, name)
        if name == "B":
            a = ad.read_h5ad(step / "standardized.h5ad")
            a.obs_names = [f"B-{c}" for c in a.obs_names]
            a.write_h5ad(step / "standardized.h5ad")
    plan = plan_file(tmp_path / "plan.json", ("A", "B"))
    assert organize.main([str(root), str(out), "--plan-json", str(plan)]) == 0
    return L.unit_dir(out, "test-unit")


def test_history_is_pruned_but_other_h5ad_rejected(tmp_path):
    step = source(tmp_path)
    history = step / ".history" / "standardize-old"
    history.mkdir(parents=True)
    (history / "standardized.h5ad").write_bytes(b"old")
    units, violations = organize.find_ecapp_units(tmp_path)
    assert len(units) == 1 and violations == []
    extra = step / "extra.h5ad"
    extra.write_bytes(b"extra")
    assert organize.find_ecapp_units(tmp_path)[1] == [extra]


@pytest.mark.parametrize("status,code", [("error", 1), ("needs_review", 3), ("ok", 1)])
def test_failed_upstream_blocks_even_with_h5ad(tmp_path, status, code):
    source(tmp_path, status=status, code=code)
    u = organize.find_ecapp_units(tmp_path)[0][0]
    with pytest.raises(ValueError, match="not ready"):
        inspect_unit(u)


def test_review_and_rejected_inventory_survive(tmp_path):
    root, out = tmp_path / "in", tmp_path / "out"
    source(root, status="needs_review")
    source(root, "rejected", status="rejected", code=2, write_h5=False)
    plan = plan_file(tmp_path / "p.json")
    assert organize.main([str(root), str(out), "--plan-json", str(plan)]) == 0
    gm = read_json(L.organize_manifest(out))
    assert [r["state"] for r in gm["source_inventory"]] == ["accepted", "rejected"]
    um = read_json(L.input_manifest(L.unit_dir(out, "test-unit")))
    assert um["upstream"]["A"]["standardize"]["reasons"] == ["review me"]


@pytest.mark.parametrize("problem", ["counts", "negative", "dimensions", "species", "schema"])
def test_upstream_validation(tmp_path, problem):
    step = source(tmp_path)
    result = read_json(step / "result.json")
    a = ad.read_h5ad(step / "standardized.h5ad")
    if problem == "counts":
        del a.layers["counts"]
    elif problem == "negative":
        a.layers["counts"].data[0] = -1
    elif problem == "dimensions":
        result["metrics"]["n_cells"] += 1
    elif problem == "species":
        result["species"] = None
    else:
        result["schema_version"] = 99
    a.write_h5ad(step / "standardized.h5ad")
    write_json(step / "result.json", result)
    with pytest.raises(ValueError):
        inspect_unit(organize.find_ecapp_units(tmp_path)[0][0])


def test_mixed_species_plan_is_rejected(tmp_path):
    root = tmp_path / "in"
    source(root, "A")
    source(root, "B", species="human")
    plan = plan_file(tmp_path / "p.json", ("A", "B"))
    assert organize.main([str(root), str(tmp_path / "out"), "--plan-json", str(plan)]) == 3


def test_derived_tsv_aligns_original_ids_and_rejects_bad_coverage(tmp_path):
    obs = matrix().obs
    spec = {"value": "/old/place/batch.tsv", "kind": "derived"}
    rows = pd.DataFrame({"cell_id": obs.index[::-1], "value": ["B"] * 3 + ["A"] * 3})
    rows.to_csv(tmp_path / "batch.tsv", sep="\t", index=False)
    values, _ = column_values(obs, spec, tmp_path)
    assert values.tolist() == ["A"] * 3 + ["B"] * 3
    for bad in (rows.iloc[:-1], pd.concat([rows, rows.iloc[:1]])):
        bad.to_csv(tmp_path / "batch.tsv", sep="\t", index=False)
        with pytest.raises(ValueError):
            column_values(obs, spec, tmp_path)


def test_same_name_samples_separate_unless_explicit_merge(tmp_path):
    unit = organize_two(tmp_path)
    h5 = L.input_h5ad(unit)
    table, _ = build_mapping(h5, unit, None, None, column="sample")
    assert sorted(table[SAMPLE_KEY].value_counts()) == [6, 6]
    spec = {"sources": {s: {"sample_column": "sample", "rationale": "verified library metadata"} for s in ("A", "B")},
            "merges": [{"sample_id": "library-1", "evidence": "two cell shards of the same GEM well",
                        "members": [{"source": s, "value": "S1"} for s in ("A", "B")]}]}
    table, _ = build_mapping(h5, unit, spec, None)
    assert table[SAMPLE_KEY].value_counts().to_dict() == {"library-1": 12}
    spec["merges"][0]["evidence"] = ""
    with pytest.raises(ValueError, match="evidence"):
        build_mapping(h5, unit, spec, None)


def test_unknown_is_not_single_and_many_groups_allowed():
    p = {"obs_columns": {"sample": {"n_unique": 201, "n_na": 0}}}
    assert persample._validate_sample_column({"sample_column": "sample", "rationale": "201 libraries"}, p) is None
    assert persample._validate_sample_column({"sample_column": None, "rationale": "missing metadata"}, p)
    assert persample._validate_sample_column({"sample_column": None, "confirmed_single": True, "rationale": "one GEM well"}, p) is None


def test_split_experiment_cannot_run_local_qc(tmp_path):
    root, out = tmp_path / "in", tmp_path / "out"
    step = source(root)
    a = ad.read_h5ad(step / "standardized.h5ad")
    a.obs["tissue"] = ["liver"] * 3 + ["blood"] * 3
    a.write_h5ad(step / "standardized.h5ad")
    plan = {"analysis_units": [{"name": tissue, "members": [{"source": "A", "obs_filter": {"column": "tissue", "values": [tissue]}}]} for tissue in ("liver", "blood")]}
    path = tmp_path / "plan.json"
    write_json(path, plan)
    assert organize.main([str(root), str(out), "--plan-json", str(path)]) == 0
    unit = L.unit_dir(out, "liver")
    with pytest.raises(ValueError, match="split an experiment"):
        build_mapping(L.input_h5ad(unit), unit, None, None, column="sample")


def publish(out, annotate=True):
    out.mkdir(parents=True, exist_ok=True)
    a = matrix(6)
    a.obs["ann_sub1"] = ["a"] * 3 + ["b"] * 3
    a.obs["_ann_coarse"] = ["T"] * 3 + ["B"] * 3
    a.obs["_ann_fine"] = a.obs["_ann_coarse"]
    a.obs["_qc_action"] = "keep"
    a.write_h5ad(out / "clustered.h5ad")
    (out / "report.html").write_text("<html>report</html>")
    pd.Series({"n_cells": 7, "n_low_quality": 1}).to_csv(out / "qc_summary.csv")
    pd.DataFrame({"cell": ["removed"], "qc_reason": ["low counts"]}).to_csv(out / "qc_removed.csv", index=False)
    pd.DataFrame({"cell_id": list(a.obs_names) + ["removed"]}).to_csv(out / INPUT_CELLS, index=False)
    write_json(out / "annotation_proposal.json", {"cluster_key": "ann_sub1", "qc_actions": [], "clusters": [
        {"cluster": k, "label_coarse": label, "label_fine": label} for k, label in (("a", "T"), ("b", "B"))]})
    write_json(out / L.RUN_STATE, {"identity": "expected", "annotate": annotate, "state": "complete", "exit_code": 0,
                                  "outputs": output_identities(out, annotate)})
    return a


def test_completion_checks_content_status_and_dynamic_clusters(tmp_path):
    for f in ("report.html", "clustered.h5ad", "annotation_proposal.json"):
        (tmp_path / f).touch()
    assert not is_done(tmp_path, True)
    publish(tmp_path)
    assert is_done(tmp_path, True, "expected")
    assert not is_done(tmp_path, True, "wrong-input")
    state = read_json(tmp_path / L.RUN_STATE)
    state["exit_code"] = 1
    write_json(tmp_path / L.RUN_STATE, state)
    assert not is_done(tmp_path, True)


@pytest.mark.parametrize("fault", ["missing", "foreign", "overlap", "duplicate", "summary", "labels"])
def test_qc_and_annotation_must_agree(tmp_path, fault):
    a = publish(tmp_path)
    if fault == "missing":
        (tmp_path / "qc_removed.csv").unlink()
    elif fault in ("foreign", "overlap", "duplicate"):
        ids = ["foreign"] if fault == "foreign" else ["cell0"] if fault == "overlap" else ["removed", "removed"]
        pd.DataFrame({"cell": ids, "qc_reason": "bad"}).to_csv(tmp_path / "qc_removed.csv", index=False)
    elif fault == "summary":
        pd.Series({"n_cells": 123, "n_low_quality": 1}).to_csv(tmp_path / "qc_summary.csv")
    else:
        a.obs["_ann_coarse"] = "wrong"
        a.write_h5ad(tmp_path / "clustered.h5ad")
    with pytest.raises((ValueError, FileNotFoundError)):
        validate_outputs(tmp_path, True)
    assert not is_done(tmp_path, True)


def test_resume_identity_and_failed_drive_are_not_masked(tmp_path, monkeypatch):
    h5 = tmp_path / "input.h5ad"
    matrix().write_h5ad(h5)
    out = tmp_path / "ps"
    monkeypatch.setattr(persample, "_kernel_runtime", lambda py: {"version": "test"})
    args = [str(h5), str(out), "--sample-column", "sample", "--no-annotate"]
    assert persample.main(args + ["--plan-only"]) == 0
    driven = []
    def failed_with_files(entries, *args):
        driven.append(True)
        return entries
    monkeypatch.setattr(persample, "drive", failed_with_files)
    monkeypatch.setattr(persample, "is_done", lambda *args: bool(driven))
    assert persample.main(args) == 1
    assert read_json(out / L.MANIFEST)["state"] == "failed"
    assert persample.main(args + ["--resolution", "0.8"]) == 1
    assert persample.main(args + ["--no-scrublet"]) == 1
    matrix(7).write_h5ad(h5)
    assert persample.main(args) == 1


def test_organize_resume_verifies_all_units(tmp_path, monkeypatch):
    root, out = tmp_path / "in", tmp_path / "out"
    source(root)
    plan = plan_file(tmp_path / "p.json")
    (out / L.UNITS).mkdir(parents=True)
    args = [str(root), str(out), "--plan-json", str(plan)]
    assert organize.main(args) == 0
    assert organize.main(args) == 0
    gm = read_json(L.organize_manifest(out))
    gm["state"] = "running"
    write_json(L.organize_manifest(out), gm)
    monkeypatch.setattr("ecarsi.plan.propose_plan", lambda *_: pytest.fail("must reuse persisted plan"))
    assert organize.main([str(root), str(out)]) == 0
    L.input_h5ad(L.unit_dir(out, "test-unit")).write_bytes(b"broken")
    assert organize.main(args) == 3


def test_writer_lock_rejects_concurrent_writer(tmp_path):
    with writer_lock(tmp_path / "lock"):
        with pytest.raises(RuntimeError, match="another writer"):
            with writer_lock(tmp_path / "lock"):
                pass


def test_front_bridge_identity():
    import harness_bridge
    from ecarsi import harness as rsi
    from osp import harness as osp
    for key in BRIDGE_LEGACY_API:
        assert getattr(rsi, key) is getattr(osp, key) is getattr(harness_bridge, key)


def test_unit_page_renders_before_and_after_front_review(tmp_path):
    from ecarsi.index import render_unit
    unit = organize_two(tmp_path)
    assert "test-unit" in render_unit(unit)
    write_json(L.persample_root(unit) / "needs_review.json", {
        "items": [{"step": "standardize", "source": "A", "detail": "check input counts"}]})
    assert "check input counts" in render_unit(unit)


def test_partial_two_unit_organize_resumes_remaining_plan(tmp_path, monkeypatch):
    from ecarsi import execute
    root, out = tmp_path / "in", tmp_path / "out"
    for src in ("A", "B"):
        source(root, src)
    path = tmp_path / "plan.json"
    write_json(path, {"analysis_units": [
        {"name": src.lower(), "members": [{"source": src, "obs_filter": None}]}
        for src in ("A", "B")]})
    original = execute._load_member
    def interrupt_second(units, member):
        if member["source"] == "B":
            raise OSError("simulated interruption while writing second unit")
        return original(units, member)
    monkeypatch.setattr(execute, "_load_member", interrupt_second)
    assert organize.main([str(root), str(out), "--plan-json", str(path)]) == 3
    first = file_identity(L.input_h5ad(L.unit_dir(out, "a")))
    assert read_json(L.organize_manifest(out))["state"] == "running"
    monkeypatch.setattr(execute, "_load_member", original)
    monkeypatch.setattr("ecarsi.plan.propose_plan", lambda *_: pytest.fail("must reuse persisted plan"))
    assert organize.main([str(root), str(out)]) == 0
    gm = read_json(L.organize_manifest(out))
    assert {u["name"] for u in gm["units_written"]} == {"a", "b"}
    assert file_identity(L.input_h5ad(L.unit_dir(out, "a"))) == first


def test_input_and_output_roots_can_move_without_losing_identity(tmp_path):
    root, out = tmp_path / "in", tmp_path / "out"
    source(root)
    p = plan_file(tmp_path / "p.json")
    assert organize.main([str(root), str(out), "--plan-json", str(p)]) == 0
    moved_root, moved_out = tmp_path / "moved-in", tmp_path / "moved-out"
    root.rename(moved_root)
    out.rename(moved_out)
    assert organize.main([str(moved_root), str(moved_out)]) == 0


def test_dense_sources_with_different_genes_merge_as_zero_counts(tmp_path):
    root, out = tmp_path / "in", tmp_path / "out"
    for name in ("A", "B"):
        step = source(root, name)
        a = ad.read_h5ad(step / "standardized.h5ad")
        a.X = a.X.toarray()
        a.layers["counts"] = a.layers["counts"].toarray()
        if name == "B":
            a.var_names = ["0", "1", "2", "B-only"]
        a.write_h5ad(step / "standardized.h5ad")
    p = plan_file(tmp_path / "p.json", ("A", "B"))
    assert organize.main([str(root), str(out), "--plan-json", str(p)]) == 0
    a = ad.read_h5ad(L.input_h5ad(L.unit_dir(out, "test-unit")))
    assert np.isfinite(a.layers["counts"]).all()
    assert (a[a.obs.source_unit == "A", "B-only"].layers["counts"] == 0).all()


@pytest.mark.parametrize("legacy_check", [True, False], ids=["0.5.0-with-check", "0.5.1-trust-raw"])
def test_raw_expansion_results_are_preserved(tmp_path, legacy_check):
    root, out = tmp_path / "in", tmp_path / "out"
    step = source(root, status="needs_review" if legacy_check else "ok")
    result = read_json(step / "result.json")
    result["step_version"] = "0.5.0" if legacy_check else "0.5.1"
    expansion = {"applied": True, "n_vars_x": 2, "n_vars_raw": 4,
                 "reason": "rebuilt on raw's gene space", "dropped_layers": ["counts"]}
    if legacy_check:
        expansion.update(reference_source="layers/counts", counts_check={
            "reference": "layers/counts", "n_shared_genes": 2,
            "n_cells_sampled": 6, "n_values_compared": 12, "match_frac": 0.98})
    result["metrics"]["raw_expansion"] = expansion
    result["reasons"] = ["raw/reference counts differ"] if legacy_check else []
    write_json(step / "result.json", result)
    p = plan_file(tmp_path / "p.json")
    assert organize.main([str(root), str(out), "--plan-json", str(p)]) == 0
    unit = L.unit_dir(out, "test-unit")
    entry = read_json(L.input_manifest(unit))["upstream"]["A"]
    assert entry["standardize"] == result
    assert read_json(L.input_manifest(unit).parent / entry["dir"] / "standardize.json") == result


def test_agent_can_report_unknown_without_being_pushed_to_guess(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    decision = {"sample_column": None, "confirmed_single": False, "rationale": "no experiment metadata"}
    profile = {"n_obs": 10, "obs_columns": {}}
    async def agent(**kwargs):
        response = await kwargs["tools"][0].handler({"decision_json": json.dumps(decision)})
        assert response["is_error"] is False
        return SimpleNamespace(submitted=response["_submitted"], cost_usd=None)
    monkeypatch.setattr("ecarsi.harness.run_agent", agent)
    assert asyncio.run(persample._identify(profile)) == decision
    assert persample._validate_sample_column(decision, profile) is not None


@pytest.mark.parametrize("dtype", ["object", "string", "category"])
def test_profile_preserves_low_cardinality_text_evidence(tmp_path, monkeypatch, dtype):
    step = source(tmp_path)
    data = ad.read_h5ad(step / "standardized.h5ad")
    data.obs["tissue"] = pd.array(["bone", "bone", "blood", "blood", "bone", "blood"], dtype=dtype)
    data.obs["numeric_score"] = np.array([1, 1, 2, 2, 1, 2])
    monkeypatch.setattr(ad.settings, "allow_write_nullable_strings", True)
    data.write_h5ad(step / "standardized.h5ad", convert_strings_to_categoricals=False)
    units, violations = organize.find_ecapp_units(tmp_path)
    assert not violations
    profile = organize.profile_unit(units[0])
    assert profile["obs_columns"]["tissue"]["value_counts"] == {"bone": 3, "blood": 3}
    assert "value_counts" not in profile["obs_columns"]["numeric_score"]


def test_profile_nullable_string_missing_values_do_not_hide_tissue(tmp_path, monkeypatch):
    step = source(tmp_path)
    data = ad.read_h5ad(step / "standardized.h5ad")
    data.obs["tissue"] = pd.array(["bone", "bone", "blood", None, "bone", "blood"], dtype="string")
    monkeypatch.setattr(ad.settings, "allow_write_nullable_strings", True)
    data.write_h5ad(step / "standardized.h5ad", convert_strings_to_categoricals=False)
    units, _ = organize.find_ecapp_units(tmp_path)
    assert organize.profile_unit(units[0])["obs_columns"]["tissue"]["value_counts"] == {"bone": 3, "blood": 2}

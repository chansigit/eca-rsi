"""Front-pipeline regressions; no MSP/ZMIP imports and no live model calls."""
from __future__ import annotations

from tests.bridge_contract import BRIDGE_LEGACY_API


import anndata as ad
import numpy as np
import pandas as pd
import pytest
from scipy import sparse

from pathlib import Path

from ecarsi import layout as L, organize as O
from ecarsi.osp_contract import INPUT_CELLS, validate_outputs
from ecarsi.run_state import read_json, write_json, writer_lock
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


def organize(root, out, plan):
    """Organize with a given plan, as the control plane does: its stage's prepare, then the shared
    execute_plan (the stage's execute adds the sample-mapping checks; these plans name no mapping)."""
    from ecarsi.execute import execute_plan
    from ecarsi.stages.organize import prepare
    prepared = prepare(Path(root), Path(out).parent / (Path(out).name + "-prepared.json"))
    plan = plan if isinstance(plan, dict) else read_json(Path(plan))
    execute_plan(prepared["records"], prepared["profiles"], plan, Path(out), records=prepared["records"],
                 input_identity=prepared["source_identity"], adapter_identity="test")


@pytest.fixture(autouse=True)
def no_index(monkeypatch):
    monkeypatch.setattr("ecarsi.ui.index.write_all", lambda *args: None)


def organize_two(tmp_path):
    root, out = tmp_path / "inputs", tmp_path / "out"
    for name in ("A", "B"):
        step = source(root, name)
        if name == "B":
            a = ad.read_h5ad(step / "standardized.h5ad")
            a.obs_names = [f"B-{c}" for c in a.obs_names]
            a.write_h5ad(step / "standardized.h5ad")
    plan = plan_file(tmp_path / "plan.json", ("A", "B"))
    organize(root, out, plan)
    return L.unit_dir(out, "test-unit")


def test_history_is_pruned_but_other_h5ad_rejected(tmp_path):
    step = source(tmp_path)
    history = step / ".history" / "standardize-old"
    history.mkdir(parents=True)
    (history / "standardized.h5ad").write_bytes(b"old")
    units, violations = O.find_ecapp_units(tmp_path)
    assert len(units) == 1 and violations == []
    extra = step / "extra.h5ad"
    extra.write_bytes(b"extra")
    assert O.find_ecapp_units(tmp_path)[1] == [extra]


@pytest.mark.parametrize("status,code", [("error", 1), ("needs_review", 3), ("ok", 1)])
def test_failed_upstream_blocks_even_with_h5ad(tmp_path, status, code):
    source(tmp_path, status=status, code=code)
    u = O.find_ecapp_units(tmp_path)[0][0]
    with pytest.raises(ValueError, match="not ready"):
        inspect_unit(u)


def test_review_survives_and_a_rejected_source_is_refused(tmp_path):
    root, out = tmp_path / "in", tmp_path / "out"
    source(root, status="needs_review")
    source(root, "rejected", status="rejected", code=2, write_h5=False)
    plan = plan_file(tmp_path / "p.json")
    # the control plane refuses an input set with a rejected source; the local path used to skip it
    with pytest.raises(ValueError, match="every source must have accepted"):
        organize(root, out, plan)
    import shutil
    shutil.rmtree(root / "rejected")
    organize(root, out, plan)
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
        inspect_unit(O.find_ecapp_units(tmp_path)[0][0])


def test_mixed_species_plan_is_rejected(tmp_path):
    root = tmp_path / "in"
    source(root, "A")
    source(root, "B", species="human")
    plan = plan_file(tmp_path / "p.json", ("A", "B"))
    with pytest.raises((ValueError, RuntimeError)):
        organize(root, tmp_path / "out", plan)


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


def test_split_experiment_cannot_run_local_qc(tmp_path):
    root, out = tmp_path / "in", tmp_path / "out"
    step = source(root)
    a = ad.read_h5ad(step / "standardized.h5ad")
    a.obs["tissue"] = ["liver"] * 3 + ["blood"] * 3
    a.write_h5ad(step / "standardized.h5ad")
    plan = {"analysis_units": [{"name": tissue, "members": [{"source": "A", "obs_filter": {"column": "tissue", "values": [tissue]}}]} for tissue in ("liver", "blood")]}
    path = tmp_path / "plan.json"
    write_json(path, plan)
    organize(root, out, path)
    unit = L.unit_dir(out, "liver")
    with pytest.raises(ValueError, match="split an experiment"):
        build_mapping(L.input_h5ad(unit), unit, None, None, column="sample")


def publish(out):
    """An OSP sample directory as osp publishes it: 6 survivors, 1 QC removal, an annotation proposal."""
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
    return a


def test_osp_contract_does_not_materialize_expression(tmp_path, monkeypatch):
    import h5py

    publish(tmp_path)
    original = h5py.Dataset.__getitem__
    def read(dataset, key, *args, **kwargs):
        assert not dataset.name.startswith(('/X/', '/layers/', '/obsp/'))
        return original(dataset, key, *args, **kwargs)
    monkeypatch.setattr(h5py.Dataset, '__getitem__', read)
    assert validate_outputs(tmp_path, True)['n_survived'] == 6
    # Metadata-only validation must still reject a missing count matrix.
    with h5py.File(tmp_path / 'clustered.h5ad', 'a') as f:
        del f['layers/counts']
    with pytest.raises(ValueError, match='counts'):
        validate_outputs(tmp_path, True)


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
    from ecarsi.ui.index import render_unit
    unit = organize_two(tmp_path)
    assert "test-unit" in render_unit(unit)
    write_json(L.persample_root(unit) / "needs_review.json", {
        "items": [{"step": "standardize", "source": "A", "detail": "check input counts"}]})
    assert "check input counts" in render_unit(unit)


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
    organize(root, out, p)
    a = ad.read_h5ad(L.input_h5ad(L.unit_dir(out, "test-unit")))
    assert np.isfinite(a.layers["counts"]).all()
    assert (a[a.obs.source_unit == "A", "B-only"].layers["counts"] == 0).all()


def test_organized_h5ad_is_a_slim_counts_carrier(tmp_path):
    # the upstream's normalized X is dead weight (validate_matrix refuses X as a
    # counts fallback; OSP/MSP rebuild X from layers["counts"]) -> an empty
    # placeholder, and int64 counts narrow to int32
    root, out = tmp_path / "in", tmp_path / "out"
    step = source(root)
    a = ad.read_h5ad(step / "standardized.h5ad")
    a.layers["counts"] = a.layers["counts"].astype(np.int64)
    a.write_h5ad(step / "standardized.h5ad")
    p = plan_file(tmp_path / "p.json")
    organize(root, out, p)
    path = L.input_h5ad(L.unit_dir(out, "test-unit"))
    o = ad.read_h5ad(path)
    assert o.X.nnz == 0 and o.X.shape == a.shape and "X_placeholder" in o.uns
    assert o.layers["counts"].dtype == np.int32
    assert (o.layers["counts"] != a.layers["counts"]).nnz == 0  # values untouched
    b = ad.read_h5ad(path, backed="r")  # persample validates the file backed: layers must stay reachable
    try:
        assert b.layers["counts"].shape == a.shape
    finally:
        b.file.close()


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
    organize(root, out, p)
    unit = L.unit_dir(out, "test-unit")
    entry = read_json(L.input_manifest(unit))["upstream"]["A"]
    assert entry["standardize"] == result
    assert read_json(L.input_manifest(unit).parent / entry["dir"] / "standardize.json") == result


@pytest.mark.parametrize("dtype", ["object", "string", "category"])
def test_profile_preserves_low_cardinality_text_evidence(tmp_path, monkeypatch, dtype):
    step = source(tmp_path)
    data = ad.read_h5ad(step / "standardized.h5ad")
    data.obs["tissue"] = pd.array(["bone", "bone", "blood", "blood", "bone", "blood"], dtype=dtype)
    data.obs["numeric_score"] = np.array([1, 1, 2, 2, 1, 2])
    monkeypatch.setattr(ad.settings, "allow_write_nullable_strings", True)
    data.write_h5ad(step / "standardized.h5ad", convert_strings_to_categoricals=False)
    units, violations = O.find_ecapp_units(tmp_path)
    assert not violations
    profile = O.profile_unit(units[0])
    assert profile["obs_columns"]["tissue"]["value_counts"] == {"bone": 3, "blood": 3}
    assert "value_counts" not in profile["obs_columns"]["numeric_score"]


def test_profile_nullable_string_missing_values_do_not_hide_tissue(tmp_path, monkeypatch):
    step = source(tmp_path)
    data = ad.read_h5ad(step / "standardized.h5ad")
    data.obs["tissue"] = pd.array(["bone", "bone", "blood", None, "bone", "blood"], dtype="string")
    monkeypatch.setattr(ad.settings, "allow_write_nullable_strings", True)
    data.write_h5ad(step / "standardized.h5ad", convert_strings_to_categoricals=False)
    units, _ = O.find_ecapp_units(tmp_path)
    assert O.profile_unit(units[0])["obs_columns"]["tissue"]["value_counts"] == {"bone": 3, "blood": 2}


def test_organize_prepare_reads_counts_in_chunks_without_eager_layers(tmp_path, monkeypatch):
    from ecarsi.stages.organize import prepare
    from ecarsi.design import _obs
    step = source(tmp_path / "inputs", n=5000)
    def no_eager_read(*args, **kwargs):
        raise AssertionError("Organize preparation must not load AnnData layers")
    monkeypatch.setattr(ad, "read_h5ad", no_eager_read)
    prepared = prepare(tmp_path / "inputs", tmp_path / "prepared.json")
    assert prepared["profiles"][0]["n_obs"] == 5000
    assert len(_obs(step / "standardized.h5ad")) == 5000
    # A bad value beyond the first validation chunk must still be rejected.
    import h5py
    with h5py.File(step / "standardized.h5ad", "r+") as handle:
        handle["layers/counts/data"][-1] = float("nan")
    with pytest.raises(ValueError, match="non-finite"):
        prepare(tmp_path / "inputs", tmp_path / "bad.json")

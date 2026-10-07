"""Scientific handoff guards: complete experiments, not just cell totals."""
from pathlib import Path

import anndata as ad
import numpy as np
import pandas as pd
import pytest
from scipy import sparse

from ecarsi.stages.organize_execute import _experiment_audit
from ecarsi.plan import validate_sample_mapping


def test_complete_experiment_cannot_be_split_between_analysis_units(tmp_path: Path):
    obs = pd.DataFrame({"experiment": ["S1", "S1", "S2", "S2"],
                        "tissue": ["left", "right", "left", "right"]},
                       index=["001", "002", "003", "004"])
    data = ad.AnnData(sparse.csr_matrix(np.eye(4)), obs=obs)
    source = tmp_path / "source.h5ad"
    data.write_h5ad(source)
    plan = {"sample_mapping": {"source": {"sample_column": "experiment",
                                           "rationale": "source experiment ID"}},
            "analysis_units": [{"name": tissue, "members": [
                {"source": "source", "obs_filter": {"column": "tissue", "values": [tissue]}}]}
                for tissue in ("left", "right")]}
    with pytest.raises(ValueError, match="split a complete experiment"):
        _experiment_audit({"source": {"h5ad": str(source)}}, plan)
    plan["analysis_units"] = [{"name": "together", "members": [
        {"source": "source", "obs_filter": None}]}]
    assert _experiment_audit({"source": {"h5ad": str(source)}}, plan)["source"]["experiments"] == 2


def test_unknown_source_mapping_is_rejected():
    profile = {"name": "source", "obs_columns": {"experiment": {
        "n_unique": 2, "n_na": 0}}}
    with pytest.raises(ValueError, match="every source"):
        validate_sample_mapping({"sample_mapping": {}}, [profile])
    with pytest.raises(ValueError, match="leaves 2 cells NA"):
        validate_sample_mapping({"sample_mapping": {"source": {
            "sample_column": "experiment", "rationale": "metadata"}}},
            [{**profile, "obs_columns": {"experiment": {"n_unique": 2, "n_na": 2}}}])
    with pytest.raises(ValueError, match="conflicts with multiple explicit"):
        validate_sample_mapping({"sample_mapping": {"source": {
            "sample_column": None, "confirmed_single": True,
            "rationale": "one donor"}}},
            [{**profile, "obs_columns": {"sample_id": {"n_unique": 3, "n_na": 0}}}])


def test_worker_plan_returns_correctable_error_then_accepts_complete_experiments(tmp_path, monkeypatch):
    import json
    from ecarsi.stages.organize import plan_tool
    from ecarsi.run_state import digest
    from ecarsi.files import save
    from ecarsi.stages import upstream
    obs = pd.DataFrame({"sample_id": ["s1", "s1", "s2", "s2"],
                        "tissue": ["left", "right", "left", "right"]}, index=["01", "02", "03", "04"])
    path = tmp_path / "source.h5ad"
    ad.AnnData(sparse.csr_matrix(np.eye(4)), obs=obs).write_h5ad(path)
    record = {"name": "source", "h5ad": str(path)}
    monkeypatch.setattr(upstream, "inspect_unit", lambda r: r)
    prepared = tmp_path / "prepared.json"
    save(prepared, {"records": [record], "source_identity": digest([record]),
        "profiles": [{**record, "species": "human", "n_obs": 4, "n_vars": 4,
                      "obs_columns": {"sample_id": {"n_unique": 2, "n_na": 0}, "tissue": {"n_unique": 2}}}]})
    args, result = tmp_path / "args.json", tmp_path / "result.json"
    save(args, {"source": "source", "column": "sample_id", "offset": 0})
    assert plan_tool("inspect_source", prepared, args, result)["value_counts"] == {"s1": 2, "s2": 2}
    plan = {"notes": "", "sample_mapping": {"source": {"sample_column": "sample_id", "rationale": "library IDs"}},
            "analysis_units": [{"name": side, "rationale": "test", "batch_key_hint": "sample_id", "members": [
                {"source": "source", "obs_filter": {"column": "tissue", "values": [side]}}]}
                for side in ("left", "right")]}
    save(args, {"plan_json": json.dumps(plan)})
    rejected = plan_tool("submit_plan", prepared, args, result)
    assert rejected["accepted"] is False and "split a complete experiment" in rejected["error"]
    plan["analysis_units"] = [{"name": "whole", "rationale": "keep experiments complete", "batch_key_hint": "sample_id",
                                "members": [{"source": "source", "obs_filter": None}]}]
    save(args, {"plan_json": json.dumps(plan)})
    accepted = plan_tool("submit_plan", prepared, args, result)
    assert accepted["accepted"] is True and accepted["experiments"]["source"]["experiments"] == 2
    assert accepted["conservation"]["sources"]["source"]["unique_assigned"] == 4


def test_inspect_source_names_the_real_columns_and_reads_null_text_as_the_profile(tmp_path, monkeypatch):
    """A model that guesses column names, or writes JSON null as text, is pointed back at the profile (glm gate 2026-10-07)."""
    from ecarsi.stages.organize import plan_tool
    from ecarsi.run_state import digest
    from ecarsi.files import save
    from ecarsi.stages import upstream
    record = {"name": "source", "h5ad": str(tmp_path / "absent.h5ad")}
    monkeypatch.setattr(upstream, "inspect_unit", lambda r: r)
    prepared = tmp_path / "prepared.json"
    save(prepared, {"records": [record], "source_identity": digest([record]),
        "profiles": [{**record, "species": "human", "n_obs": 4, "n_vars": 4,
                      "obs_columns": {"sample_id": {"n_unique": 2}, "tissue": {"n_unique": 2}}}]})
    args, result = tmp_path / "args.json", tmp_path / "result.json"
    save(args, {"source": "source", "column": "donor", "offset": 0})
    guessed = plan_tool("inspect_source", prepared, args, result)
    assert guessed["accepted"] is False and '["sample_id", "tissue"]' in guessed["error"] and "column=null" in guessed["error"]
    for text in ("null", "None", ""):
        save(args, {"source": "source", "column": text, "offset": 0})
        assert sorted(plan_tool("inspect_source", prepared, args, result)["obs_columns"]) == ["sample_id", "tissue"]


def test_the_planning_prompt_starts_with_the_read_first_procedure(tmp_path):
    from ecarsi.stages.organize import planning_spec
    from ecarsi.files import save
    prepared = tmp_path / "prepared.json"
    save(prepared, {"input_root": str(tmp_path), "profiles": [{"name": "11_Shietal"}]})
    spec = {"input_root": str(tmp_path), "run_id": "r", "pool_root": str(tmp_path), "bridge_root": str(tmp_path),
            "output_root": str(tmp_path / "out"), "prepare_cpus": 1, "prepare_memory_mb": 1024, "prepare_timeout_seconds": 60}
    prompt = planning_spec(spec, prepared)["prompt"]
    assert prompt.startswith("# How to work") and 'Sources: ["11_Shietal"]' in prompt and "{sources}" not in prompt
    assert prompt.index("Read every profile first") < prompt.index("# Task: propose analysis units")

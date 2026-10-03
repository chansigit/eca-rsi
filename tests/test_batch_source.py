"""Decision 0016: samples and batches come from ECA-PP identify-columns; big samples run as chunks."""

from __future__ import annotations

import anndata as ad
import numpy as np
import pandas as pd
import pytest

from ecarsi import layout as L
from ecarsi.plan import validate_sample_mapping
from ecarsi.run_state import read_json, write_json
from ecarsi.sample_mapping import CHUNK, SAMPLE_KEY, build_mapping
from ecarsi.stages.organize_execute import effective_mapping, unit_batch_key
from ecarsi.stages.upstream import eca_pp_decision
from ecarsi.warm_pool.budget import from_cells
from tests.test_front_integration import organize, source


def evidence(platform="droplet", batch=None, library=None):
    return {
        "platform": {"value": platform},
        "columns": {"batch": batch, "library": library},
        "ladder": [{"rung": 1, "label": "donor", "verdict": "rejected"}],
    }


def values(**cols):
    return {f"eca_pp_{role}": pd.Series(v, dtype="string") for role, v in cols.items()}


def test_decision_takes_the_library_then_the_batch_then_the_whole_source():
    adopted = {"value": "donor", "kind": "existing", "correction": "recommended"}
    d = eca_pp_decision(
        evidence(
            batch=adopted,
            library={
                "value": "library.tsv",
                "kind": "derived",
                "label": "barcode:head:.",
            },
        ),
        values(batch=["D1", "D2"], library=["L1", "L2"]),
        160_000,
    )
    assert (d["sample_column"], d["batch"], d["source"]) == (
        "eca_pp_library",
        "eca_pp_batch",
        "eca_pp",
    )
    unnecessary = {**adopted, "correction": "unnecessary"}
    d = eca_pp_decision(evidence(batch=unnecessary), values(batch=["S1", "S2"]), 5_000)
    assert (d["sample_column"], d["batch"]) == ("eca_pp_batch", None)
    d = eca_pp_decision(evidence("split-pool"), {}, 465_000)
    assert d["sample_column"] is None and d["confirmed_single"] and "error" not in d
    assert "error" not in eca_pp_decision(evidence("droplet"), {}, 8_000)
    assert "sample_map" in eca_pp_decision(evidence("droplet"), {}, 200_000)["error"]
    assert "sample_map" in eca_pp_decision(evidence("unknown"), {}, 200_000)["error"]
    assert "error" not in eca_pp_decision(evidence("plate"), {}, 200_000)
    # cells the column leaves unassigned, or no ECA-PP result: the organize agent decides
    assert (
        eca_pp_decision(evidence(batch=adopted), values(batch=["S1", None]), 5_000)
        is None
    )
    assert eca_pp_decision({}, {}, 5_000) is None


def test_plan_may_leave_out_what_eca_pp_decided():
    decided = {
        "name": "A",
        "obs_columns": {},
        "eca_pp_decision": {"sample_column": None, "source": "eca_pp"},
    }
    open_ = {"name": "B", "obs_columns": {"sample": {"n_unique": 2, "n_na": 0}}}
    validate_sample_mapping({"sample_mapping": {}}, [decided])
    validate_sample_mapping(
        {"sample_mapping": {"A": {"anything": 1}}}, [decided]
    )  # not used, not checked
    with pytest.raises(ValueError, match="did not decide"):
        validate_sample_mapping({"sample_mapping": {}}, [decided, open_])
    validate_sample_mapping(
        {
            "sample_mapping": {
                "B": {"sample_column": "sample", "rationale": "libraries"}
            }
        },
        [decided, open_],
    )


def test_owner_map_then_eca_pp_then_agent():
    stopped = {
        "sample_column": None,
        "source": "eca_pp",
        "error": "no batch; name it in the sample_map",
    }
    profiles = [
        {
            "name": "A",
            "eca_pp_decision": {"sample_column": "eca_pp_batch", "source": "eca_pp"},
        },
        {"name": "B"},
        {"name": "C", "eca_pp_decision": stopped},
    ]
    plan = {
        "sample_mapping": {"A": {"sample_column": "x"}, "B": {"sample_column": "agent"}}
    }
    with pytest.raises(ValueError, match="C: no batch"):
        effective_mapping(plan, profiles)
    assert effective_mapping(plan, profiles, stop=False)["C"] is stopped
    owner = {"sources": {"C": {"sample_column": "lane", "rationale": "owner"}}}
    got = effective_mapping(plan, profiles, owner)
    assert [got[s]["sample_column"] for s in "ABC"] == ["eca_pp_batch", "agent", "lane"]


def test_unit_batch_follows_eca_pp_for_one_source():
    mapping = {
        "A": {"source": "eca_pp", "batch": "eca_pp_batch"},
        "B": {"source": "eca_pp", "batch": None},
        "C": {"sample_column": "sample"},
    }
    assert unit_batch_key(["A"], mapping, {}) == "eca_pp_batch"
    assert unit_batch_key(["B"], mapping, {}) is False
    assert unit_batch_key(["C"], mapping, {}) is None
    assert unit_batch_key(["A", "B"], mapping, {}) is None
    assert unit_batch_key(["A"], mapping, {"batch_key": "donor"}) == "donor"


def big_unit(tmp_path, n=2500):
    root, out = tmp_path / "in", tmp_path / "out"
    source(root, n=n)
    organize(
        root,
        out,
        {
            "analysis_units": [
                {"name": "u", "members": [{"source": "A", "obs_filter": None}]}
            ]
        },
    )
    unit = L.unit_dir(out, "u")
    return L.input_h5ad(unit), unit


def test_big_samples_run_as_chunks_that_keep_their_sample_as_batch(tmp_path):
    h5, unit = big_unit(tmp_path)
    spec = {
        "sources": {
            "A": {
                "sample_column": None,
                "confirmed_single": True,
                "rationale": "one library",
            }
        },
        "chunk_cells": 1000,
    }
    table, decision = build_mapping(h5, unit, spec)
    (parent,) = decision["chunks"]
    chunks = decision["chunks"][parent]
    assert (
        len(chunks) == 3
        and all(CHUNK.search(c) for c in chunks)
        and not CHUNK.search(parent)
    )
    assert sorted(table[SAMPLE_KEY].unique()) == chunks
    assert table[SAMPLE_KEY].value_counts().min() > 600  # random, not empty
    assert decision["batch_key"]["column"] == "eca_batch"
    assert set(decision["batch_key"]["of_sample"].values()) == {parent}
    again, _ = build_mapping(h5, unit, spec)
    assert again[SAMPLE_KEY].equals(table[SAMPLE_KEY])  # deterministic
    _, single = build_mapping(h5, unit, {**spec, "batch_key": False})
    assert set(single["batch_key"]["of_sample"]) == set(chunks)
    assert set(single["batch_key"]["of_sample"].values()) == {"single_batch"}
    _, whole = build_mapping(h5, unit, {**spec, "chunk_cells": 5000})
    assert "chunks" not in whole and "batch_key" not in whole
    with pytest.raises(ValueError, match="chunk_cells"):
        build_mapping(h5, unit, {**spec, "chunk_cells": 10})


def test_organize_takes_library_and_batch_from_eca_pp(tmp_path):
    root, out = tmp_path / "in", tmp_path / "out"
    step = source(root, n=12)
    a = ad.read_h5ad(step / "standardized.h5ad")
    a.obs["donor"] = ["D1"] * 6 + ["D2"] * 6
    a.obs["lib"] = [f"L{i // 3}" for i in range(12)]
    a.write_h5ad(step / "standardized.h5ad")
    write_json(
        step.parent / "identify_columns" / "result.json",
        {
            "schema_version": 2,
            "step": "identify_columns",
            "step_version": "0.5.2",
            "status": "ok",
            "exit_code": 0,
            "platform": {"value": "droplet"},
            "columns": {
                "batch": {
                    "value": "donor",
                    "kind": "existing",
                    "label": "donor",
                    "correction": "recommended",
                },
                "library": {
                    "value": "lib",
                    "kind": "existing",
                    "label": "lib",
                    "evidence": "test",
                },
                "cell_type": None,
            },
        },
    )
    organize(
        root,
        out,
        {
            "sample_mapping": {},
            "analysis_units": [
                {"name": "u", "members": [{"source": "A", "obs_filter": None}]}
            ],
        },
    )
    manifest = read_json(L.input_manifest(L.unit_dir(out, "u")))
    decision = manifest["sample_mapping"]["decision"]
    from ecarsi.ui.index import batch_source

    assert batch_source(manifest) == (
        "samples from eca_pp: eca_pp_library; batch eca_pp_batch; platform droplet"
    )
    assert decision["sources"]["A"]["source"] == "eca_pp"
    assert decision["sources"]["A"]["sample_column"] == "eca_pp_library"
    of = decision["batch_key"]["of_sample"]
    assert decision["batch_key"]["column"] == "eca_pp_batch"
    assert len(of) == 4 and sorted(set(of.values())) == ["D1", "D2"]


def test_whole_matrix_steps_are_sized_from_cells_and_never_lowered():
    request = {"operation_id": "osp.compute", "memory_mb": 8192}
    assert from_cells(request, 5_000) is request
    assert from_cells(request, 89_000)["memory_mb"] >= 24.7 * 1024
    assert (
        from_cells({**request, "operation_id": "cross-sample.compute"}, 465_000)[
            "memory_mb"
        ]
        > 8192
    )
    assert np.isclose(from_cells(request, 20_000)["memory_mb"], 9216, atol=256)


def test_identify_columns_is_found_beside_a_standardize_input_root(tmp_path):
    from ecarsi.stages.upstream import discover

    step = source(tmp_path / "in")
    write_json(step.parent / "identify_columns" / "result.json", {"step": "identify_columns"})
    (unit,), _ = discover(step)
    assert unit["name"] == "A"
    assert unit["identify_columns_result"] == str(step.parent / "identify_columns" / "result.json")

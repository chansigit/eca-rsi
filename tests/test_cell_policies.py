"""Sample-map cell policies: exclude_cells before OSP, batch_key for Harmony, agent proposals."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import anndata as ad
import pandas as pd
import pytest

from ecarsi import crosssample, layout as L, organize, persample, policies as P, review
from ecarsi.design import design_text
from ecarsi.ledger import _persample_frames, build_ledger, sankey_data, stage_list
from ecarsi.run_state import read_json, write_json
from ecarsi.sample_mapping import SAMPLE_KEY, build_mapping, mapping_identity
from tests.test_front_integration import plan_file, source
from tests.test_ledger_conservation import csv, h5, write_round

BLANK = {
    "blank": ["mouse.id", "subtissue", "cell_ontology_class"],
    "reason": "upstream_qc_blank",
    "rationale": "the authors' QC dropped these wells and left their metadata empty",
}


def facs(tmp_path):
    """One source, 8 cells: plate P1 (mouse m1) = cells 0-2, P2 (m2) = 3-5; cells 6-7 blank everywhere."""
    root, out = tmp_path / "in", tmp_path / "out"
    step = source(root, "A", n=8)
    a = ad.read_h5ad(step / "standardized.h5ad")
    a.obs["plate"] = ["P1"] * 3 + ["P2"] * 3 + ["missing"] * 2
    a.obs["mouse.id"] = [
        "m1",
        "m1",
        "missing",
        "m2",
        "m2",
        "m2",
        "missing",
        "",
    ]  # cell2: NA inside P1
    a.obs["subtissue"] = ["imm"] * 3 + ["epi"] * 3 + ["", "missing"]
    a.obs["cell_ontology_class"] = [
        "T",
        "B",
        "",
        "E",
        "E",
        "F",
        "",
        "",
    ]  # cell2 blank here only: kept
    a.obs["half"] = ["d1"] * 3 + ["missing"] * 3 + [""] * 2  # blank throughout P2
    a.write_h5ad(step / "standardized.h5ad")
    assert (
        organize.main(
            [str(root), str(out), "--plan-json", str(plan_file(tmp_path / "plan.json"))]
        )
        == 0
    )
    return L.unit_dir(out, "test-unit")


def spec(**extra):
    return {
        "sources": {"A": {"sample_column": "plate", "rationale": "plate = library"}},
        **extra,
    }


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr("ecarsi.index.write_all", lambda *args: None)
    monkeypatch.setattr("ecarsi.agent_retry.MAX_ATTEMPTS", 1)
    monkeypatch.delenv("MSP_BATCH_COL", raising=False)


def agent_submitting(monkeypatch, decisions, seen):
    """Fake harness: submits each decision in turn until the host accepts one."""

    async def agent(**kwargs):
        tool = kwargs["tools"][0]
        for d in decisions:
            r = await tool.handler({"decision_json": json.dumps(d)})
            seen.append(r["content"][0]["text"])
            if not r["is_error"]:
                return SimpleNamespace(submitted=r["_submitted"], cost_usd=None, tokens_in=None, tokens_out=None)
        raise AssertionError(seen)

    monkeypatch.setattr("ecarsi.harness.run_agent", agent)


# ---------------------------------------------------------------- rules


def test_blank_rule_drops_cells_before_the_partition(tmp_path):
    unit = facs(tmp_path)
    h5ad = L.input_h5ad(unit)
    with pytest.raises(ValueError, match="leaves 2 cells NA"):
        build_mapping(h5ad, unit, spec(), None)
    table, decision = build_mapping(h5ad, unit, spec(exclude_cells=[BLANK]), None)
    assert table["excluded_reason"].tolist() == [""] * 6 + ["upstream_qc_blank"] * 2
    assert (table.loc[table.excluded_reason.ne(""), SAMPLE_KEY] == "").all()
    assert table.loc[table.excluded_reason.eq(""), SAMPLE_KEY].nunique() == 2
    (rec,) = decision["exclude_cells"]
    assert (
        rec["n_cells"] == 2
        and rec["proposed_by"] == "sample_map"
        and "warning" not in rec
    )


def test_where_rule_is_literal_and_rules_are_validated(tmp_path):
    unit = facs(tmp_path)
    h5ad = L.input_h5ad(unit)
    where = {"where": {"plate": ["missing"]}, "reason": "no_plate", "rationale": "x"}
    nothing = {"where": {"plate": ["P9"]}, "reason": "none", "rationale": "x"}
    _, decision = build_mapping(h5ad, unit, spec(exclude_cells=[where, nothing]), None)
    assert [r["n_cells"] for r in decision["exclude_cells"]] == [2, 0]
    assert decision["exclude_cells"][1]["warning"] == "matched no cell"
    for bad, msg in [
        (
            {"where": {"nope": ["x"]}, "reason": "r", "rationale": "x"},
            "unknown obs column",
        ),
        (
            {
                "where": {"plate": ["P1"]},
                "blank": ["plate"],
                "reason": "r",
                "rationale": "x",
            },
            "exactly one",
        ),
        (
            {"where": {"plate": ["P1"]}, "reason": "Bad Reason", "rationale": "x"},
            "slug",
        ),
        ({"blank": ["plate"], "reason": "r", "rationale": " "}, "rationale"),
        (
            {"blank": ["plate"], "reason": "r", "rationale": "x", "wher": {}},
            "unknown key",
        ),
        (
            {
                "where": {"plate": ["P1", "P2", "missing"]},
                "reason": "all",
                "rationale": "x",
            },
            "every cell",
        ),
    ]:
        with pytest.raises(ValueError, match=msg):
            build_mapping(h5ad, unit, spec(exclude_cells=[bad]), None)
    with pytest.raises(ValueError, match="unique"):
        build_mapping(h5ad, unit, spec(exclude_cells=[where, where]), None)
    with pytest.raises(ValueError, match="unknown sample-map key"):
        build_mapping(h5ad, unit, spec(exclude_cell=[]), None)


def test_rules_are_part_of_the_mapping_identity(tmp_path):
    unit = facs(tmp_path)
    h5ad = L.input_h5ad(unit)
    a, _ = build_mapping(h5ad, unit, spec(exclude_cells=[BLANK]), None)
    b, _ = build_mapping(
        h5ad, unit, spec(exclude_cells=[{**BLANK, "reason": "other_slug"}]), None
    )
    assert mapping_identity(a) != mapping_identity(b)


# ---------------------------------------------------------------- batch_key


def test_batch_key_constant_per_experiment_ignoring_na(tmp_path):
    unit = facs(tmp_path)
    h5ad = L.input_h5ad(unit)
    table, decision = build_mapping(
        h5ad, unit, spec(exclude_cells=[BLANK], batch_key="mouse.id"), None
    )
    bk = decision["batch_key"]
    sid = table[table[SAMPLE_KEY].ne("")].groupby("source_value")[SAMPLE_KEY].first()
    assert bk == {
        "column": "mouse.id",
        "of_sample": {sid["P1"]: "m1", sid["P2"]: "m2"},
        "n_filled": 1,
    }
    for key, msg in [
        ("donor", "not an obs column"),
        ("cell_ontology_class", "several values"),
        ("half", "blank throughout"),
        ("sample", "single value"),
    ]:
        with pytest.raises(ValueError, match=msg):
            build_mapping(h5ad, unit, spec(exclude_cells=[BLANK], batch_key=key), None)


def test_batch_col_resolution_env_wins_and_mismatch_is_an_error(monkeypatch):
    declared = {
        "sample_column": SAMPLE_KEY,
        "sample_mapping": {"batch_key": {"column": "mouse.id"}},
    }
    assert crosssample.resolve_batch_col(declared) == (
        "mouse.id",
        {
            "experiment_column": SAMPLE_KEY,
            "batch_col": "mouse.id",
            "correction": "harmony_if_multiple_batch_values",
            "selection": "sample_map",
        },
    )
    col, policy = crosssample.resolve_batch_col(
        {"sample_column": SAMPLE_KEY, "sample_mapping": {}}
    )
    assert col == SAMPLE_KEY and policy["selection"] == "compatibility_default"
    monkeypatch.setenv("MSP_BATCH_COL", "mouse.id")
    assert crosssample.resolve_batch_col(declared)[1]["selection"] == "explicit"
    monkeypatch.setenv("MSP_BATCH_COL", "plate")
    with pytest.raises(ValueError, match="contradicts"):
        crosssample.resolve_batch_col(declared)


# ---------------------------------------------------------------- ledger


def test_ledger_accounts_policy_excluded_cells(tmp_path):
    unit = tmp_path / "unit"
    d = L.persample_root(unit) / "S1-x"
    h5(d / "clustered.h5ad", ["a", "b"], _ann_coarse=["A", "A"], _ann_fine=["a", "a"])
    csv(d / "qc_removed.csv", ["cell", "qc_reason"], [["c", "low"]])
    csv(d / "input_cells.csv.gz", ["cell_id"], [["a"], ["b"], ["c"]])
    L.persample_manifest(unit).write_text(
        json.dumps({"samples": [{"value": "S1", "dir": str(d), "n_cells": 3}]})
    )
    h5(L.input_h5ad(unit), ["a", "b", "c", "x", "y"])
    with pytest.raises(ValueError, match="does not cover"):
        _persample_frames(unit)
    cols = ["cell", "source_unit", "source_cell_id", "reason", "proposed_by"]
    policy = L.persample_root(unit) / L.EXCLUDED_CELLS
    csv(
        policy,
        cols,
        [
            ["x", "A", "x", "upstream_qc_blank", "sample_map"],
            ["y", "A", "y", "upstream_qc_blank", "agent"],
        ],
    )
    frames = pd.concat(_persample_frames(unit))
    assert (
        frames.loc["x", "osp_status"] == "removed:persample-policy:upstream_qc_blank"
        and frames.loc["x", "sample"] == ""
    )
    assert (
        frames.loc["a", "osp_status"] == "kept"
        and frames.loc["c", "osp_status"] == "removed:low"
    )
    rdir = L.round_dir(unit, 1)
    csv(
        L.crosssample_dir(rdir) / "sample_decisions.csv",
        ["sample", "decision"],
        [["S1", "include"]],
    )
    write_round(rdir, ["a", "b"], ["a"], [])
    ledger = build_ledger(unit, [rdir])
    assert (
        ledger.loc["x", "r01_msp_status"] == ""
        and ledger.loc["b", "r01_msp_status"] == "removed:inspect"
    )
    graph = sankey_data(ledger, stage_list(1))
    sinks = {
        n["name"]: n["count"]
        for n in graph["nodes"]
        if n["stage"] == 0 and n["removed"]
    }
    assert sinks == {
        "removed: persample-policy:upstream_qc_blank": 2,
        "removed: low": 1,
    }
    csv(
        policy,
        cols,
        [
            ["a", "A", "a", "r", "sample_map"],
            ["x", "A", "x", "r", "sample_map"],
            ["y", "A", "y", "r", "sample_map"],
        ],
    )
    with pytest.raises(ValueError, match="duplicate"):
        _persample_frames(unit)
    csv(
        policy,
        cols,
        [["x", "A", "x", "", "sample_map"], ["y", "A", "y", "r", "sample_map"]],
    )
    with pytest.raises(ValueError, match="missing exclusion reason"):
        _persample_frames(unit)


# ---------------------------------------------------------------- persample end to end (drive mocked)


def test_persample_writes_policy_ledger_batch_subsets_and_review(
    tmp_path, monkeypatch, capsys
):
    unit = facs(tmp_path)
    m = tmp_path / "map.json"
    write_json(m, spec(exclude_cells=[BLANK], batch_key="mouse.id"))
    monkeypatch.setattr(persample, "_kernel_runtime", lambda py: {"version": "test"})
    monkeypatch.setattr(
        persample, "drive", lambda entries, *args, **kw: entries
    )  # nothing runs, subsets stay
    monkeypatch.setattr(
        "ecarsi.harness.run_agent", None
    )  # no agent call may happen on this path
    args = [str(unit), "--sample-map", str(m), "--no-annotate"]
    assert persample.main(args) == 1  # every sample "failed" (mocked drive)
    out = L.persample_root(unit)
    man = read_json(out / L.MANIFEST)
    assert (
        man["sample_mapping"]["batch_key"]["column"] == "mouse.id"
        and man["batch_key_recommendation"] is None
    )
    gone = pd.read_csv(out / L.EXCLUDED_CELLS, dtype=str, keep_default_na=False)
    assert gone["cell"].tolist() == ["cell6", "cell7"] and set(gone["reason"]) == {
        "upstream_qc_blank"
    }
    assert set(gone["proposed_by"]) == {"sample_map"} and set(gone["source_unit"]) == {
        "A"
    }
    assert [s["n_cells"] for s in man["samples"]] == [3, 3]
    for s in man["samples"]:
        sub = ad.read_h5ad(Path(s["dir"]) / persample.SUBSET_FILE)
        assert sub.obs["mouse.id"].astype(str).unique().tolist() == [
            man["sample_mapping"]["batch_key"]["of_sample"][s["value"]]
        ]
    items = review.collect(unit, [], [], False)
    (item,) = [i for i in items if i.kind == "policy_excluded"]
    assert (
        item.n_cells == 2
        and item.scope == "upstream_qc_blank"
        and "sample_map" in item.note
    )
    assert item.label == "blank in all of mouse.id, subtissue, cell_ontology_class"
    text = design_text(unit)
    assert "mouse.id=m1" in text and "mouse.id=m2" in text and "sample :" not in text
    # a changed rule is a different mapping: no resume into this directory
    write_json(
        m, spec(exclude_cells=[{**BLANK, "reason": "other"}], batch_key="mouse.id")
    )
    assert persample.main(args) == 1
    assert "experiment mapping changed" in capsys.readouterr().out


def test_batch_key_recommendation_is_recorded_not_applied(tmp_path, monkeypatch):
    unit = facs(tmp_path)
    m = tmp_path / "map.json"
    write_json(m, spec(exclude_cells=[BLANK]))
    monkeypatch.setattr(persample, "_kernel_runtime", lambda py: {"version": "test"})
    monkeypatch.setattr(persample, "drive", lambda entries, *args, **kw: entries)
    seen: list = []
    agent_submitting(
        monkeypatch,
        [
            {"batch_key": "nope", "rationale": "x"},
            {"batch_key": "mouse.id", "rationale": ""},
            {
                "batch_key": "mouse.id",
                "rationale": "replicate id next to the gate column",
            },
        ],
        seen,
    )
    assert (
        persample.main(
            [str(unit), "--sample-map", str(m), "--no-annotate", "--plan-only"]
        )
        == 0
    )
    assert (
        len(seen) == 3
        and "not a sample-constant column" in seen[0]
        and seen[2] == "recorded"
    )
    man = read_json(L.persample_root(unit) / L.MANIFEST)
    assert (
        man["batch_key_recommendation"]["batch_key"] == "mouse.id"
        and "batch_key" not in man["sample_mapping"]
    )
    assert crosssample.resolve_batch_col(man)[0] == SAMPLE_KEY  # advisory only
    assert (
        persample.main([str(unit), "--sample-map", str(m), "--no-annotate"]) == 1
    )  # resume reuses the record
    assert len(seen) == 3
    (item,) = read_json(L.persample_root(unit) / "needs_review.json")["items"]
    assert (
        item["step"] == "batch_key_recommendation"
        and item["source"] == "mouse.id"
        and "NOT applied" in item["detail"]
    )
    assert any(
        i.kind == "upstream_review" and "NOT applied" in i.note
        for i in review.collect(unit, [], [], False)
    )


def test_failed_recommendation_does_not_fail_persample(tmp_path, monkeypatch):
    unit = facs(tmp_path)
    m = tmp_path / "map.json"
    write_json(m, spec(exclude_cells=[BLANK]))
    monkeypatch.setattr(persample, "_kernel_runtime", lambda py: {"version": "test"})

    async def broken(**kwargs):
        raise RuntimeError("no model")

    monkeypatch.setattr("ecarsi.harness.run_agent", broken)
    assert (
        persample.main(
            [str(unit), "--sample-map", str(m), "--no-annotate", "--plan-only"]
        )
        == 0
    )
    rec = read_json(L.persample_root(unit) / L.MANIFEST)["batch_key_recommendation"]
    assert rec["batch_key"] is None and "unavailable" in rec["rationale"]


# ---------------------------------------------------------------- agent exclusion proposals


def test_agent_exclusion_proposal_is_validated_like_a_user_rule(tmp_path, monkeypatch):
    unit = facs(tmp_path)
    good = {
        "sample_column": "plate",
        "rationale": "plate = library",
        "exclude_cells": [BLANK],
    }
    attempts = [
        {**good, "exclude_cells": [{**BLANK, "blank": ["nope"]}]},
        {
            **good,
            "exclude_cells": [
                {"where": {"plate": ["P9"]}, "reason": "r", "rationale": "x"}
            ],
        },
        {
            **good,
            "exclude_cells": [
                {"where": {"plate": ["P1", "P2"]}, "reason": "r", "rationale": "x"}
            ],
        },
        {
            "sample_column": "plate",
            "rationale": "plate = library",
        },  # without the rule plate has NA cells
        good,
    ]
    seen: list = []
    agent_submitting(monkeypatch, attempts, seen)
    monkeypatch.setattr("ecarsi.cost.record", lambda *args: None)
    table, decision = build_mapping(
        L.input_h5ad(unit), unit, None, persample.identify_sample_column
    )
    assert [t.split(" — ")[0][:22] for t in seen] == [
        "rule upstream_qc_blank",
        "rule r: matches no cel",
        "rule r: would exclude ",
        "sample column 'plate' ",
        "recorded",
    ]
    assert "ceiling" in seen[2] and "leaves 2 cells NA" in seen[3]
    (rec,) = decision["exclude_cells"]
    assert rec["proposed_by"] == "agent" and rec["n_cells"] == 2
    assert table["excluded_reason"].tolist() == [""] * 6 + ["upstream_qc_blank"] * 2
    assert decision["sources"]["A"]["exclude_cells"] == [BLANK]


def test_proposal_without_cells_in_session_is_refused(monkeypatch):
    profile = {"n_obs": 8, "obs_columns": {"plate": {"n_unique": 2, "n_na": 2}}}
    seen: list = []
    agent_submitting(
        monkeypatch,
        [
            {"sample_column": "plate", "rationale": "x", "exclude_cells": [BLANK]},
            {"sample_column": None, "confirmed_single": False, "rationale": "unknown"},
        ],
        seen,
    )
    assert asyncio.run(persample._identify(profile))["sample_column"] is None
    assert "cannot be checked" in seen[0]


def test_review_flags_a_rule_that_matched_nothing(tmp_path):
    unit = tmp_path / "unit"
    write_json(
        L.persample_manifest(unit),
        {
            "samples": [],
            "sample_mapping": {
                "exclude_cells": [
                    {
                        "where": {"plate": ["P9"]},
                        "reason": "none",
                        "rationale": "shared map",
                        "proposed_by": "sample_map",
                        "n_cells": 0,
                        "warning": "matched no cell",
                    }
                ]
            },
        },
    )
    (item,) = review.collect(unit, [], [], False)
    assert (
        item.kind == "policy_excluded"
        and item.n_cells == 0
        and "WARNING: matched no cell" in item.note
    )
    assert item.label == "plate in ['P9']"
    assert "matched no cell" in review.to_markdown([item], "unit", 0)

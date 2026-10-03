"""Sample-map cell policies: exclude_cells before OSP, batch_key for Harmony, agent proposals."""

from __future__ import annotations

import json
from types import SimpleNamespace

import anndata as ad
import pandas as pd
import pytest

from ecarsi import layout as L, policies as P, review
from ecarsi.run_state import write_json
from ecarsi.sample_mapping import SAMPLE_KEY, build_mapping, mapping_identity
from tests.test_front_integration import organize, plan_file, source

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
    organize(root, out, plan_file(tmp_path / "plan.json"))
    return L.unit_dir(out, "test-unit")


def spec(**extra):
    return {
        "sources": {"A": {"sample_column": "plate", "rationale": "plate = library"}},
        **extra,
    }


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


# ---------------------------------------------------------------- ledger


# ---------------------------------------------------------------- persample end to end (drive mocked)


# ---------------------------------------------------------------- agent exclusion proposals


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


def test_where_rule_matches_numbers_by_value_and_text_literally():
    obs = pd.DataFrame(
        {
            "n": [3.0, None, 4.5],  # an int column read back from h5ad once it holds an NA
            "cat": pd.Categorical([3.0, None, 4.0]),
            "flag": [True, False, True],
            "plate": ["03", "3", "P1"],
        },
        index=list("abc"),
    )

    def hits(col, value):
        return P.rule_mask(obs, {"where": {col: [value]}, "reason": "r", "rationale": "x"}).tolist()

    for value in (3, "3", 3.0, "3.0"):
        assert hits("n", value) == [True, False, False] and hits("cat", value) == [True, False, False]
    assert hits("n", "4.5") == [False, False, True]
    assert hits("flag", "true") == hits("flag", True) == [True, False, True]
    assert hits("plate", 3) == [False, True, False]  # text stays literal: "03" is not 3

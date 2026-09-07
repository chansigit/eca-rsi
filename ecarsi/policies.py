"""Declarative cell policies of the sample map — host-applied, never inferred.

exclude_cells   rules dropping cells BEFORE any OSP subset is cut; every
                excluded cell is a ledger row (osp_status
                "removed:persample-policy:<reason>", persample/excluded_cells.csv).
                Rule: {"where": {col: [values]}, "reason", "rationale"} (raw
                string equality, AND across columns — a literal "missing"
                matches "missing") or {"blank": [col, ...], "reason",
                "rationale"} (NA-family in ALL listed columns, see
                upstream.normalize). Unknown column = error; a rule matching no
                cell = recorded warning (a shared map may not apply to every
                organ); a source left with no cell = error. The sample-column
                agent may propose rules of the same shape; those are strict:
                no-match or more than AGENT_EXCLUDE_MAX_FRAC of the source is
                rejected in-session.
batch_key       obs column Harmony corrects by instead of eca_sample_id. Must
                be constant within every OSP experiment (NA ignored, two non-NA
                values = error), present in every experiment, >= 2 values in
                the unit. The per-sample constant is written into each OSP
                subset so NA cells follow their experiment.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pandas as pd

from .upstream import normalize

STEP = "persample-policy"
REASON_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")
AGENT_EXCLUDE_MAX_FRAC = (
    0.5  # ponytail: one ceiling for agent proposals; an explicit sample map has none
)
RULE_KEYS = {"where", "blank", "reason", "rationale"}
SPEC_KEYS = {"sources", "merges", "exclude_cells", "batch_key"}

RULE_SCHEMA = {
    "type": "object",
    "properties": {
        "where": {
            "type": "object",
            "additionalProperties": {"type": "array", "items": {"type": "string"}},
        },
        "blank": {"type": "array", "items": {"type": "string"}},
        "reason": {"type": "string", "pattern": "^[a-z0-9][a-z0-9_-]{0,39}$"},
        "rationale": {"type": "string"},
    },
    "required": ["reason", "rationale"],
}
BATCH_KEY_SCHEMA = {
    "type": "object",
    "properties": {
        "batch_key": {"type": ["string", "null"]},
        "rationale": {"type": "string"},
    },
    "required": ["batch_key", "rationale"],
}


# ---------------------------------------------------------------- exclude_cells


def check_spec_keys(spec: dict) -> None:
    unknown = set(spec) - SPEC_KEYS
    if unknown:
        raise ValueError(
            f"unknown sample-map key(s) {sorted(unknown)}; allowed: {sorted(SPEC_KEYS)}"
        )


def check_rule(rule, columns) -> None:
    if not isinstance(rule, dict) or not REASON_RE.fullmatch(
        str(rule.get("reason", ""))
    ):
        raise ValueError("exclude_cells rule needs a slug reason ([a-z0-9_-], max 40)")
    name = rule["reason"]
    if set(rule) - RULE_KEYS:
        raise ValueError(f"rule {name}: unknown key(s) {sorted(set(rule) - RULE_KEYS)}")
    if not str(rule.get("rationale", "")).strip():
        raise ValueError(f"rule {name}: rationale required")
    where, blank = rule.get("where"), rule.get("blank")
    if (where is None) == (blank is None):
        raise ValueError(f"rule {name}: exactly one of where / blank")
    if where is not None and (
        not isinstance(where, dict)
        or not where
        or any(not isinstance(v, list) or not v for v in where.values())
    ):
        raise ValueError(f"rule {name}: where maps obs columns to nonempty value lists")
    if blank is not None and (
        not isinstance(blank, list)
        or not blank
        or any(not isinstance(c, str) for c in blank)
    ):
        raise ValueError(f"rule {name}: blank lists obs column names")
    unknown = [c for c in (where or blank) if c not in columns]
    if unknown:
        raise ValueError(f"rule {name}: unknown obs column(s) {unknown}")


def rule_mask(obs: pd.DataFrame, rule: dict) -> pd.Series:
    check_rule(rule, obs.columns)
    if "where" in rule:
        m = pd.Series(True, index=obs.index)
        for col, values in rule["where"].items():
            hit = obs[col].astype("string").str.strip().isin([str(v) for v in values])
            m &= hit.fillna(False).astype(bool)
        return m
    return pd.concat([normalize(obs[c]).isna() for c in rule["blank"]], axis=1).all(
        axis=1
    )


def apply_rules(
    obs: pd.DataFrame,
    rules,
    excluded: pd.Series,
    proposed_by: str,
    strict: bool = False,
) -> list[dict]:
    """Mark the cells of `obs` each rule excludes in `excluded` (reason per
    cell, '' = kept; the first matching rule wins). Returns the applied
    records (rule + proposed_by + n_cells [+ warning])."""
    if not isinstance(rules, list):
        raise ValueError("exclude_cells must be a list of rules")
    reasons = [r.get("reason") for r in rules if isinstance(r, dict)]
    if len(set(reasons)) != len(rules):
        raise ValueError("exclude_cells reasons must be unique")
    applied = []
    for rule in rules:
        m = rule_mask(obs, rule) & excluded.reindex(obs.index).eq("").to_numpy()
        n = int(m.sum())
        record = {**rule, "proposed_by": proposed_by, "n_cells": n}
        if strict:
            if n == 0:
                raise ValueError(f"rule {rule['reason']}: matches no cell")
            if n > AGENT_EXCLUDE_MAX_FRAC * len(obs):
                raise ValueError(
                    f"rule {rule['reason']}: would exclude {n}/{len(obs)} cells — above the agent "
                    f"proposal ceiling ({AGENT_EXCLUDE_MAX_FRAC:.0%}); an exclusion that large is an explicit "
                    "sample-map decision"
                )
        elif n == 0:
            record["warning"] = "matched no cell"
        excluded.loc[obs.index[m]] = rule["reason"]
        applied.append(record)
    return applied


# ---------------------------------------------------------------- batch_key


def resolve_batch_key(obs: pd.DataFrame, sample: pd.Series, key) -> dict:
    """{"column", "of_sample": {sample: value}, "n_filled"}; NA cells (blank /
    "missing") are ignored for constancy and later filled with their
    experiment's value in the OSP subset."""
    if not isinstance(key, str) or key not in obs.columns:
        raise ValueError(
            f"batch_key {key!r} is not an obs column of the organized input"
        )
    values = normalize(obs[key])
    groups = values.groupby(sample.reindex(obs.index), observed=True)
    conflicts = [s for s, n in groups.nunique().items() if n > 1]
    if conflicts:
        raise ValueError(
            f"batch_key {key!r} takes several values inside experiment(s) {conflicts[:5]}; "
            "an OSP experiment cannot be split across batches"
        )
    first = groups.first()
    empty = [s for s in sample.unique() if pd.isna(first.get(s))]
    if empty:
        raise ValueError(
            f"batch_key {key!r} is blank throughout experiment(s) {empty[:5]}"
        )
    if first.nunique() < 2:
        raise ValueError(
            f"batch_key {key!r} takes a single value in this unit; nothing to correct by"
        )
    return {
        "column": key,
        "of_sample": {str(s): str(v) for s, v in first.items()},
        "n_filled": int(values.isna().sum()),
    }


# ---------------------------------------------------------------- agent: batch_key recommendation


def recommend_batch_key(design: str, columns: list[str]) -> dict:
    """Advisory only: {"batch_key": col|None, "rationale"} from the study
    design table; recorded in needs_review, never applied."""
    from .agent_retry import run_with_retry

    return run_with_retry(
        lambda: _recommend(design, columns), label="batch key recommendation"
    )


async def _recommend(design: str, columns: list[str]) -> dict:
    from . import model
    from .harness import ToolSpec, run_agent

    brief = (Path(__file__).parent / "prompts" / "batch_key.md").read_text()
    prompt = (
        brief
        + "\n\n## Study design\n\n"
        + design
        + "\n\nFinish by calling submit_batch_key with a JSON string matching the schema above."
    )

    def err(text):
        return {
            "content": [{"type": "text", "text": text + " — fix and resubmit"}],
            "is_error": True,
        }

    async def submit_batch_key(args: dict) -> dict:
        try:
            decision = json.loads(args["decision_json"])
        except json.JSONDecodeError as exc:
            return err(f"JSON parse error: {exc}")
        if (
            not isinstance(decision, dict)
            or "batch_key" not in decision
            or not str(decision.get("rationale", "")).strip()
        ):
            return err("batch_key (column or null) and rationale are required")
        key = decision["batch_key"]
        if key is not None and key not in columns:
            return err(
                f"{key!r} is not a sample-constant column; choose from {columns} or null"
            )
        return {
            "content": [{"type": "text", "text": "recorded"}],
            "is_error": False,
            "_submitted": decision,
        }

    tool = ToolSpec(
        name="submit_batch_key",
        description="Submit the batch-key recommendation. decision_json is a JSON string with this schema:\n"
        + json.dumps(BATCH_KEY_SCHEMA, indent=1),
        input_schema={"decision_json": str},
        handler=submit_batch_key,
    )
    result = await run_agent(
        tools=[tool],
        submit_tool="submit_batch_key",
        prompt=prompt,
        cwd=os.getcwd(),
        model=model(),
        max_turns=4,
        allowed_builtin=(),
        label="batch key recommendation",
    )
    recommend_batch_key.last_cost = result.cost_usd  # type: ignore[attr-defined]
    return result.submitted

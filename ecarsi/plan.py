"""Agent planning call — the only model-facing stage of organize.

The agent gets the unit profiles and the brief in `prompts/plan.md`, runs
read-only (it may Read the result.json files for context, nothing else),
and must return a plan conforming to PLAN_SCHEMA — the SDK enforces the
schema, so the executor never parses prose.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "analysis_units": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "pattern": "^[a-z0-9][a-z0-9-]*$"},
                    "members": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "properties": {
                                "source": {"type": "string"},
                                "obs_filter": {
                                    "type": ["object", "null"],
                                    "properties": {
                                        "column": {"type": "string"},
                                        "values": {"type": "array", "items": {"type": "string"}},
                                    },
                                    "required": ["column", "values"],
                                },
                            },
                            "required": ["source", "obs_filter"],
                        },
                    },
                    "rationale": {"type": "string"},
                    "batch_key_hint": {"type": ["string", "null"]},
                },
                "required": ["name", "members", "rationale", "batch_key_hint"],
            },
        },
        "notes": {"type": "string"},
    },
    "required": ["analysis_units", "notes"],
}


def propose_plan(profiles: list[dict]) -> dict:
    plan = asyncio.run(_propose(profiles))
    _validate(plan, profiles)
    return plan


async def _propose(profiles: list[dict]) -> dict:
    from claude_agent_sdk import ClaudeAgentOptions, query

    brief = (Path(__file__).parent / "prompts" / "plan.md").read_text()
    prompt = (
        brief
        + "\n\n## Unit profiles\n\n```json\n"
        + json.dumps(profiles, indent=1)
        + "\n```\n"
    )
    from . import model

    options = ClaudeAgentOptions(
        model=model(),
        allowed_tools=["Read", "Grep", "Glob"],  # read-only probing, no writes
        max_turns=30,
        output_format={"type": "json_schema", "schema": PLAN_SCHEMA},
    )
    result = None
    async for msg in query(prompt=prompt, options=options):
        so = getattr(msg, "structured_output", None)
        if so is not None:
            result = so
    if result is None:
        raise RuntimeError("planner ended without structured output")
    return result


def _validate(plan: dict, profiles: list[dict]) -> None:
    """Executor-side sanity: every member references a real unit and a real
    obs column; unit names unique. Schema handled the shapes already."""
    known = {p["name"]: p for p in profiles}
    by_h5ad = {p["h5ad"]: p["name"] for p in profiles}  # tolerate agent citing the h5ad path instead of the unit name
    # tolerate whitespace/quoting noise and path variants (resolved symlinks,
    # trailing slash, agent citing the unit dir instead of the h5ad file)
    by_h5ad_stripped = {h5ad.strip().strip("'\""): name for h5ad, name in by_h5ad.items()}
    by_resolved = {str(Path(h5ad).resolve()): name for h5ad, name in by_h5ad.items()}
    names = [au["name"] for au in plan["analysis_units"]]
    if len(names) != len(set(names)):
        raise ValueError(f"duplicate analysis unit names in plan: {names}")
    for au in plan["analysis_units"]:
        for m in au["members"]:
            src = m["source"]
            if src not in known:
                cand = src.strip().strip("'\"")
                if cand in known:
                    m["source"] = cand
                elif cand in by_h5ad_stripped:
                    m["source"] = by_h5ad_stripped[cand]
                elif cand in by_h5ad:
                    m["source"] = by_h5ad[cand]
                else:
                    try:
                        resolved = str(Path(cand).resolve())
                    except OSError:
                        resolved = None
                    if resolved and resolved in by_resolved:
                        m["source"] = by_resolved[resolved]
                    elif len(known) == 1:
                        # single-unit dataset: nothing else it could refer to
                        (m["source"],) = known.keys()
            if m["source"] not in known:
                raise ValueError(f"plan references unknown source unit {m['source']!r}")
            flt = m.get("obs_filter")
            if flt and flt["column"] not in known[m["source"]]["obs_columns"]:
                raise ValueError(
                    f"plan filters {m['source']!r} on obs column {flt['column']!r}, "
                    "which its profile does not contain"
                )

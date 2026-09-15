"""Agent planning call — the only model-facing stage of organize.

The agent gets the unit profiles and the brief in `prompts/plan.md`, runs
read-only (it may Read the result.json files for context, nothing else),
and must call submit_plan with a plan conforming to PLAN_SCHEMA — the tool
handler validates it (both structurally and, via _validate, against the
actual profiles) before accepting, so the executor never parses prose. This
submit-tool shape (rather than a harness-native structured-output mode) is
what makes this call portable across HARNESS backends — see ecarsi.harness.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .harness import ToolSpec, run_agent

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
        "sample_mapping": {"type": "object"},
    },
    "required": ["analysis_units", "notes"],
}


def propose_plan(profiles: list[dict]) -> dict:
    from .agent_retry import run_with_retry

    # validation lives inside the retried coroutine: a plan the agent produced
    # with an unresolvable source reference is the same kind of transient
    # malformed output as a dropped connection — retry the whole proposal.
    async def _propose_validated() -> dict:
        return await _propose(profiles)

    return run_with_retry(_propose_validated, label="organize plan")


async def _propose(profiles: list[dict], *, brief=None, cwd=None, full_result=False,
                   on_submitted=None, require_sample_mapping=False):
    if brief is None:
        brief = (Path(__file__).parent / "prompts" / "plan.md").read_text()
    prompt = (
        brief
        + "\n\n## Unit profiles\n\n```json\n"
        + json.dumps(profiles, indent=1)
        + "\n```\n"
        + "\nFinish by calling submit_plan with a JSON string matching the schema above."
    )
    from . import model

    async def submit_plan(args: dict) -> dict:
        try:
            plan = json.loads(args["plan_json"])
        except json.JSONDecodeError as exc:
            return {"content": [{"type": "text", "text": f"JSON parse error, fix and resubmit: {exc}"}],
                    "is_error": True}
        try:
            _validate(plan, profiles)
            if require_sample_mapping:
                validate_sample_mapping(plan, profiles)
        except ValueError as exc:
            return {"content": [{"type": "text", "text": f"invalid, fix and resubmit: {exc}"}],
                    "is_error": True}
        if on_submitted is not None:
            on_submitted(plan)
        return {"content": [{"type": "text", "text": "plan accepted"}], "is_error": False, "_submitted": plan}

    tool = ToolSpec(
        name="submit_plan",
        description="Submit the analysis-unit plan. plan_json is a JSON string with this schema:\n"
                    + json.dumps(PLAN_SCHEMA, indent=1),
        input_schema={"plan_json": str},
        handler=submit_plan,
    )
    result = await run_agent(
        tools=[tool], submit_tool="submit_plan", prompt=prompt,
        cwd=cwd or os.getcwd(), model=model(),
        max_turns=30, allowed_builtin=("read", "glob", "grep"), label="organize plan",
    )
    return result if full_result else result.submitted


def validate_sample_mapping(plan: dict, profiles: list[dict]) -> None:
    """New Organize contract; legacy plans remain readable in the old runner."""
    from .sample_mapping import _validate_sample_column

    mapping = plan.get("sample_mapping")
    known = {p["name"]: p for p in profiles}
    if not isinstance(mapping, dict) or set(mapping) != set(known):
        raise ValueError("sample_mapping must name every source exactly once")
    for source, decision in mapping.items():
        if not isinstance(decision, dict) or set(decision) - {"sample_column", "confirmed_single", "rationale"}:
            raise ValueError(f"{source}: invalid experiment decision")
        error = _validate_sample_column(decision, known[source])
        if error:
            raise ValueError(f"{source}: {error}")
        if decision["sample_column"] is None:
            explicit = ("sample_id", "sample", "library_id", "library_plate",
                        "cdna_plate", "plate.barcode", "plate_barcode")
            conflicting = [name for name in explicit if
                           known[source].get("obs_columns", {}).get(name, {}).get("n_unique", 0) > 1]
            if conflicting:
                raise ValueError(f"{source}: confirmed_single conflicts with multiple explicit "
                                 f"sample/library groups in {', '.join(conflicting)}; choose a "
                                 "complete experimental unit instead of merging by donor")


def _validate(plan: dict, profiles: list[dict]) -> None:
    """Executor-side sanity: every member references a real unit and a real
    obs column; unit names unique. Schema handled the shapes already."""
    if not isinstance(plan, dict) or not plan.get("analysis_units"):
        raise ValueError("plan needs nonempty analysis_units")
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
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", au["name"]) or not au.get("members"):
            raise ValueError("analysis unit needs a safe name and nonempty members")
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
        species = {known[m["source"]].get("species") for m in au["members"]}
        if None in species or len(species) != 1:
            raise ValueError(f"analysis unit {au['name']!r} must contain one resolved species: {species}")

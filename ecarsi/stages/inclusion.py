"""The cross-sample inclusion decision: its schema, the single-sample note and its validation,
shared by the cross-sample stage (ecarsi.stages.crosssample)."""

from __future__ import annotations




INCLUSION_SCHEMA = {
    "type": "object",
    "properties": {
        "samples": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "sample": {"type": "string"},
                    "include": {"type": "boolean"},
                    "reason": {"type": "string"},
                },
                "required": ["sample", "include", "reason"],
            },
        },
        "notes": {"type": "string"},
    },
    "required": ["samples", "notes"],
}

# what a completed persample sample dir must contain (annotation is a hard
# prerequisite for crosssample: the inclusion agent judges by it)


# ---------------------------------------------------------------- resolve


# ---------------------------------------------------------------- include


SINGLE_SAMPLE_NOTE = "single sample — inclusion agent not consulted; harmony is skipped downstream"
CHUNKED_NOTE = ("chunked samples (random slices of one sample, decision 0016) — inclusion agent not "
                "consulted; every sample included")


def validate_inclusion(decision, expected):
    if not isinstance(decision, dict) or not isinstance(decision.get("notes"), str):
        raise ValueError("invalid inclusion decision")
    rows = decision.get("samples")
    if not isinstance(rows, list) or any(not isinstance(e, dict) or not isinstance(e.get("sample"), str) for e in rows):
        raise ValueError("inclusion samples must be named objects")
    got = [e["sample"] for e in rows]
    if sorted(got) != sorted(expected) or len(got) != len(set(got)):
        raise ValueError("inclusion must cover each sample exactly once")
    if any(type(e.get("include")) is not bool or not isinstance(e.get("reason"), str) or not e["reason"].strip() for e in rows):
        raise ValueError("inclusion requires boolean include and nonempty reasons")


# ---------------------------------------------------------------- execute

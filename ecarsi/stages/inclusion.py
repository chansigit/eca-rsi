"""The cross-sample inclusion decision: its schema, the single-sample note and its validation,
shared by the cross-sample stage (ecarsi.stages.crosssample)."""

from __future__ import annotations




# Why a sample is excluded (owner, 2026-11-06): low-quality is checked by code against the included samples
# (crosssample._exclusion_check); not-this-dataset (wrong tissue or species, a duplicate upload, a control or
# technical sample) cannot be, and needs_review lists it. Batch, composition, treatment and few cells are no reason.
EXCLUDE_REASONS = ("low-quality", "not-this-dataset")

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
                    "exclude_reason": {
                        "enum": [*EXCLUDE_REASONS, None],
                        "description": "required when include is false. low-quality: the code checks it, and includes the "
                        "sample unless its cells have clearly fewer genes or counts, or a clearly higher mitochondrial "
                        "fraction, than the cells of the samples you include, and no gene of its own. not-this-dataset: "
                        "wrong tissue or species, a duplicate upload, a control or technical sample.",
                    },
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
    unnamed = [e["sample"] for e in rows if not e["include"] and e.get("exclude_reason") not in EXCLUDE_REASONS]
    if unnamed:
        raise ValueError(f"an excluded sample names exclude_reason, one of {list(EXCLUDE_REASONS)}: {unnamed}")


# ---------------------------------------------------------------- execute

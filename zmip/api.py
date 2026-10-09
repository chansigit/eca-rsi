"""eca-rsi's contract with zmip: every zmip name eca-rsi uses, under a public name.

eca-rsi imports zmip only from here (its test_layers.py enforces that). Renaming, removing or changing
the behaviour of a name below breaks eca-rsi; everything else in zmip is free to change. Names resolve
on first use, so importing this module loads nothing beyond the zmip package itself.
"""
import importlib

# public name: (module, attribute)
_NAMES = {
    "PLAN_SCHEMA_DOC": ("zmip.plan", "_PLAN_SCHEMA_DOC"),
    "lineage_evidence": ("zmip.plan", "lineage_evidence"),
    "validate_plan": ("zmip.plan", "validate_plan"),
    "CLUSTER_SCHEMA_DOC": ("zmip.annotate", "_CLUSTER_SCHEMA_DOC"),
    "REMOVE_BUDGET": ("zmip.annotate", "REMOVE_BUDGET"),
    "components": ("zmip.annotate", "components"),
    "lineage_markers": ("zmip.foreign", "lineage_markers"),
    "score_foreign": ("zmip.foreign", "score_foreign"),
    "subset_for": ("zmip.lineage", "subset_for"),
    "merge_back": ("zmip.merge", "merge_back"),
    "slug": ("zmip.report", "slug"),
    "QUALITY_KEY": ("zmip.keys", "QUALITY_KEY"),
    "TYPE_KEY": ("zmip.keys", "TYPE_KEY"),
    "apply_decisions": ("zmip.scheduled", "apply_decisions"),
    "compute_lineage": ("zmip.scheduled", "compute_lineage"),
    "partitions": ("zmip.scheduled", "partitions"),
    "validate_quality": ("zmip.scheduled", "validate_quality"),
    "validate_types": ("zmip.scheduled", "validate_types"),
}
__all__ = sorted(_NAMES)


def __getattr__(name):
    if name not in _NAMES:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module, attribute = _NAMES[name]
    return getattr(importlib.import_module(module), attribute)

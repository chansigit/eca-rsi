"""eca-rsi's contract with osp: every osp name eca-rsi uses, under a public name.

eca-rsi imports osp only from here (its test_layers.py enforces that). Renaming, removing or changing
the behaviour of a name below breaks eca-rsi; everything else in osp is free to change. Names resolve
on first use, so importing this module loads nothing beyond the osp package itself,
in particular not osp.annotate and its optional agent dependencies.
"""
import importlib

# public name: (module, attribute)
_NAMES = {
    "run_one_sample_pipeline": ("osp", "run_one_sample_pipeline"),
    "generate_report": ("osp.report", "generate_report"),
    "write_report_context": ("osp.report", "write_report_context"),
    "ANNOTATION_OPS": ("osp.annotate", "_OPS"),
    "PROPOSAL_SCHEMA_DOC": ("osp.annotate", "_PROPOSAL_SCHEMA_DOC"),
    "apply_proposal": ("osp.annotate", "_apply_proposal"),
    "detect_primary_key": ("osp.annotate", "_detect_primary_key"),
    "gene_table": ("osp.annotate", "_gene_table"),
    "plot_annotation": ("osp.annotate", "_plot_annotation"),
    "qc_table": ("osp.annotate", "_qc_table"),
    "subcluster_once": ("osp.annotate", "_subcluster_once"),
    "system_prompt": ("osp.annotate", "_system_prompt"),
    "validate_proposal": ("osp.annotate", "_validate_proposal"),
}
__all__ = sorted(_NAMES)


def __getattr__(name):
    if name not in _NAMES:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module, attribute = _NAMES[name]
    return getattr(importlib.import_module(module), attribute)

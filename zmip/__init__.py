"""zmip (zoom-in pipeline): per-lineage refinement of an msp annotation: lineage planning rules, each lineage
re-embedded on its own and scored for foreign-lineage signal, decisions validated and merged back with real
removal. eca-rsi's zoom-in stage calls it through ``zmip.api`` (decision 0014) and runs the agents in its own
sessions. The standalone command line and agent flows were removed with #28.
"""

import importlib

# public name: submodule
_LAZY = {
    "DEFAULT_MIN_CELLS": ".plan",
    "generate_report": ".report",
    "lineage_markers": ".foreign",
    "merge_back": ".merge",
    "score_foreign": ".foreign",
    "validate_plan": ".plan",
}
__all__ = sorted(_LAZY)


def __getattr__(name):  # PEP 562: a submodule loads on first use, so `import zmip` alone loads no scanpy
    if name not in _LAZY:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(importlib.import_module(_LAZY[name], __name__), name)

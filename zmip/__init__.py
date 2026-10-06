"""zmip (zoom-in pipeline): per-lineage refinement of an msp annotation: lineage planning rules, each lineage
re-embedded on its own and scored for foreign-lineage signal, decisions validated and merged back with real
removal. eca-rsi's zoom-in stage calls it through ``zmip.api`` (decision 0014) and runs the agents in its own
sessions. The standalone command line and agent flows were removed with #28.
"""

from .foreign import lineage_markers, score_foreign
from .merge import merge_back
from .plan import DEFAULT_MIN_CELLS, validate_plan
from .report import generate_report

__all__ = [
    "DEFAULT_MIN_CELLS",
    "generate_report",
    "lineage_markers",
    "merge_back",
    "score_foreign",
    "validate_plan",
]

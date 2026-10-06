"""msp (multi-sample-pipeline): integrate per-sample outputs (Harmony) → multi-resolution Leiden + UMAP →
cluster QC / DEG tables → HTML report; plus the validation and application of inspection and annotation
proposals. eca-rsi's cross-sample stage calls it through ``msp.api`` (decision 0014) and runs the agents in its
own sessions. The standalone command line and agent flows were removed with #28.
"""

from .integrate import integrate_adata, load_and_merge
from .plots import save_single_umap
from .report import generate_report

__version__ = "0.5.4"  # the last msp-sc release; since decision 0018 msp ships inside eca-rsi and has no version of its own

__all__ = [
    "__version__",
    "integrate_adata",
    "load_and_merge",
    "generate_report",
    "save_single_umap",
]

"""msp (multi-sample-pipeline): integrate per-sample outputs (Harmony) → multi-resolution Leiden + UMAP →
cluster QC / DEG tables → HTML report; plus the validation and application of inspection and annotation
proposals. eca-rsi's cross-sample stage calls it through ``msp.api`` (decision 0014) and runs the agents in its
own sessions. The standalone command line and agent flows were removed with #28.
"""

import importlib

__version__ = "0.5.4"  # the last msp-sc release; since decision 0018 msp ships inside eca-rsi and has no version of its own

# public name: submodule
_LAZY = {
    "integrate_adata": ".integrate",
    "load_and_merge": ".integrate",
    "generate_report": ".report",
    "save_single_umap": ".plots",
}
__all__ = ["__version__", *sorted(_LAZY)]


def __getattr__(name):  # PEP 562: a submodule loads on first use, so `import msp` alone loads no scanpy
    if name not in _LAZY:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(importlib.import_module(_LAZY[name], __name__), name)

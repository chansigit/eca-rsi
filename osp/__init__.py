"""OSP (one-sample-pipeline): single-sample scRNA-seq QC → clustering/DEG → self-contained HTML report.

Strictly single-sample: eca-rsi's per-sample stage runs it once per sample through ``osp.api`` (decision 0014);
its agent annotation runs in eca-rsi's own sessions. The standalone command line and agent flow were removed
with #28.
"""

import importlib

# public name: submodule
_LAZY = {
    "DEFAULT_QC_PCA_COVARIATES": ".cluster",
    "DOUBLET_SCORE_REFERENCE": ".qc",
    "QC_OVERLAY_COLS": ".cluster",
    "SPECIES_GENE_PATTERNS": ".qc",
    "assert_single_sample": ".qc",
    "cluster_and_deg": ".cluster",
    "cluster_order": ".qc",
    "decontx_top_genes": ".qc",
    "generate_report": ".report",
    "qc_one_sample": ".qc",
    "run_one_sample_pipeline": ".cluster",
}
__all__ = sorted(_LAZY)


def __getattr__(name):  # PEP 562: a submodule loads on first use, so `import osp` alone loads no scanpy
    if name not in _LAZY:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(importlib.import_module(_LAZY[name], __name__), name)

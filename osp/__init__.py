"""OSP (one-sample-pipeline): single-sample scRNA-seq QC → clustering/DEG → self-contained HTML report.

Strictly single-sample: eca-rsi's per-sample stage runs it once per sample through ``osp.api`` (decision 0014);
its agent annotation runs in eca-rsi's own sessions. The standalone command line and agent flow were removed
with #28.
"""

from .cluster import (
    DEFAULT_QC_PCA_COVARIATES,
    QC_OVERLAY_COLS,
    cluster_and_deg,
    run_one_sample_pipeline,
)
from .qc import (
    DISSOCIATION_GENES_HS,
    DOUBLET_SCORE_REFERENCE,
    SPECIES_GENE_PATTERNS,
    assert_single_sample,
    cluster_order,
    decontx_top_genes,
    qc_one_sample,
)
from .report import generate_report

__all__ = [
    "DEFAULT_QC_PCA_COVARIATES",
    "DISSOCIATION_GENES_HS",
    "DOUBLET_SCORE_REFERENCE",
    "QC_OVERLAY_COLS",
    "SPECIES_GENE_PATTERNS",
    "assert_single_sample",
    "cluster_and_deg",
    "cluster_order",
    "decontx_top_genes",
    "generate_report",
    "qc_one_sample",
    "run_one_sample_pipeline",
]

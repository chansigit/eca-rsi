"""OSP (one-sample-pipeline): single-sample scRNA-seq QC → clustering/DEG →
self-contained HTML report.

Strictly single-sample by design — one sample per run, no cross-sample batch
integration; loop over samples in an outer driver, and treat integration as a
separate downstream step.

Entry points:
    from osp import run_one_sample_pipeline, generate_report

    ad_fo = adata[adata.obs["sample"] == "FO"]
    run_one_sample_pipeline(ad_fo, sample_label="FO", outdir="osp_out/FO")
    generate_report("osp_out/FO")

Stepwise calls / utilities:
    qc_one_sample     QC only (flags cells, drops nothing) — see osp.qc
    cluster_and_deg   clustering/DEG/PAGA on QC-passed data — see osp.cluster
    deg_two_groups    wilcoxon DEG between any two cell groups — see osp.cluster

Command line:
    python -m osp data.h5ad --sample FO --outdir osp_out   # full pipeline + report
    python -m osp.qc data.h5ad --sample FO                 # QC only
    python -m osp.report osp_out                           # report only
    python -m osp.annotate osp_out --species mouse --tissue "bone marrow"
                                                           # annotation agent
                                                           # (needs osp-sc[agent])

osp.annotate is intentionally not imported here — it depends on the optional
agent dependencies; use `from osp.annotate import propose_annotation` when
needed.
"""

from .cluster import (
    DEFAULT_QC_PCA_COVARIATES,
    QC_OVERLAY_COLS,
    cluster_and_deg,
    deg_two_groups,
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
    "deg_two_groups",
    "generate_report",
    "qc_one_sample",
    "run_one_sample_pipeline",
]

"""standissect-lite — data-driven tiny-cluster detection inside existing clusters.

Clustering + subset-reclustering often fails to separate tiny subpopulations
and low-quality subgroups from their parent cluster; locally compact UMAP
point groups still carry a data-level clue. This package crosses a
precomputed RNA-side Leiden clustering with a UMAP-coordinate clustering
(granularity-matched) and ranks each RNA cluster's fragments (rank 0 = main
core, the rest = candidate minors) — a reproducible replacement for manual
lassoing. Detection only: biological meaning needs downstream verification.
No scanpy, no DEG, no diagnosis, no reports — made to be imported by other
projects.

    from standissect_lite import dissect_partition
    res = dissect_partition(adata, cluster_col="leiden", umap_key="X_umap")
    res.fragments[res.fragments.is_minor]

See :mod:`standissect_lite.core` for the full rationale and the
granularity-matching behaviour of ``umap_target_k``.
"""
from .core import PartitionResult, dissect_partition, umap_leiden_partition

__version__ = "0.1.0"
__all__ = ["dissect_partition", "umap_leiden_partition", "PartitionResult"]

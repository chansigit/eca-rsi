"""standissect-lite — data-driven tiny-cluster detection inside existing clusters.

Clustering + subset-reclustering often fails to separate tiny subpopulations
and low-quality subgroups from their parent cluster, so analysts fall back on
lassoing suspicious UMAP islands by hand. This package turns that visual
heuristic into a systematic candidate-detection procedure: it crosses a
precomputed RNA-side Leiden clustering with a granularity-matched
UMAP-coordinate clustering and ranks each RNA cluster's fragments (rank 0 =
main core, the rest = its minor siblings — small fragments carrying the same
RNA label as the core). Candidates only — validation is explicitly left to
downstream evidence.
No scanpy, no DEG, no diagnosis, no reports — made to be imported by other
projects.

    from standissect_lite import dissect_partition
    res = dissect_partition(adata, cluster_col="leiden", umap_key="X_umap")
    res.fragments[res.fragments.is_minor_sibling]

See :mod:`standissect_lite.core` for the full rationale and the
granularity-matching behaviour of ``umap_target_k``.
"""
from .core import PartitionResult, dissect_partition, umap_leiden_partition

__version__ = "0.2.0"
# 0.2.0: renamed the raw, globally-ranked UMAP-side partition column from
# "umap_cluster" to "_umap_partition" (in both labels and fragments) to stop
# it being mistaken for a per-parent rank — "subcluster"/"rank" (already
# rank-0-is-largest within each RNA cluster) is the only headline identifier.
__all__ = ["dissect_partition", "umap_leiden_partition", "PartitionResult"]

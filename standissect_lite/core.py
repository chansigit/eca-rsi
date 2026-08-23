"""standissect_lite.core — minor-fragment detection inside existing clusters.

Why this exists (出发点)
------------------------
Single-cell annotation and QC lean on clustering, yet clustering followed by
subset-and-recluster often fails to cleanly separate tiny subpopulations and
low-quality subgroups from their parent cluster — so in practice analysts
fall back on the UMAP and lasso suspicious islands by hand. UMAP is rightly
criticised against over-interpretation; still, local separations in the
embedding can provide useful candidate structure, because UMAP is built to
preserve aspects of the high-dimensional neighbourhood graph. Analysts
already treat UMAP islands as hypotheses — this module turns that visual
heuristic into a systematic candidate-detection procedure, and explicitly
leaves validation to downstream evidence: whether a fragment is biologically
meaningful (or a doublet pocket / low-quality tail) is not decided here. The
full `standissect` package adds that interpretation layer (DEG, QC drift,
LLM diagnosis, reports); this module is the detection step alone, extracted
so other projects can reuse it without the heavy stack: no scanpy, no DEG,
no diagnosis, no server.

What it does
------------
Cross two partitions of the same cells:

1. the **precomputed RNA-side Leiden clustering** — read as-is from
   ``adata.obs[cluster_col]``, never recomputed here;
2. a **UMAP-side clustering** computed by this module — kNN graph on the 2-D
   UMAP coordinates + Leiden on that graph.

Their overlap table (the "cartesian product" of the two labelings) splits each
RNA cluster into fragments, ranked by size: rank 0 is the cluster's clean
**main core**, every other fragment is a candidate **minor**.

Granularity matching
--------------------
The split is only meaningful when the UMAP-side clustering has roughly the
same granularity as the RNA-side one: too fine chops clean cores into fake
minors, too coarse swallows real minors into the core. Therefore, by default
(``umap_target_k=None``) the UMAP-side target cluster count is taken from the
RNA side — the number of distinct labels in ``cluster_col`` — and the
UMAP-side Leiden resolution is binary-searched until the UMAP partition lands
within ``umap_target_k ± umap_target_tol`` clusters.

Who owns which parameter
------------------------
Every tuning knob steers the **UMAP-side** clustering and is prefixed
``umap_``; the RNA side contributes nothing but its labels (and, through the
default ``umap_target_k``, its cluster count). ``min_subcluster_size`` is not
a clustering parameter at all — it only sets the ``is_minor`` flag in the
returned fragment table.

Usage
-----
    import anndata as ad
    from standissect_lite import dissect_partition

    adata = ad.read_h5ad("data.h5ad")      # needs obs['leiden'] + obsm['X_umap']
    res = dissect_partition(adata, cluster_col="leiden", umap_key="X_umap")
    # or hand a precomputed UMAP matrix directly (obsm not consulted):
    res = dissect_partition(adata, cluster_col="leiden", umap_Nx2_mat=xy)

    res.fragments[res.fragments.is_minor]  # the minors, one row per fragment
    res.overlap                            # RNA-cluster × UMAP-cluster cell counts
    # opt-in write-back (this module never touches adata itself):
    adata.obs["original_cluster_split"] = res.labels["subcluster"]
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd


# VENDORED from standissect/cluster.py (canonical source) — keep semantics
# byte-compatible so both packages name fragments identically. Parameters here
# carry no ``umap_`` prefix: the function name already scopes everything to the
# UMAP side (``dissect_partition``'s ``umap_*`` knobs forward 1:1 onto these).
def umap_leiden_partition(
    umap_xy: np.ndarray,
    *,
    target_k: int | None = None,
    resolution: float = 0.5,
    n_neighbors: int = 30,
    tol: int = 2,
    max_iter: int = 12,
    random_state: int = 0,
) -> tuple[pd.Series, dict]:
    """kNN+Leiden on UMAP-2D for all cells. Returns (labels Series, info dict).

    If ``target_k`` is given, binary-search ``resolution`` so the result has
    ``target_k ± tol`` clusters (up to ``max_iter`` iterations). Otherwise use
    ``resolution`` directly.
    """
    from sklearn.neighbors import kneighbors_graph
    import igraph as ig
    import leidenalg
    k = min(n_neighbors, max(2, len(umap_xy) - 1))
    knn = kneighbors_graph(umap_xy, n_neighbors=k, mode='connectivity', include_self=False)
    knn = knn.maximum(knn.T)
    sources, targets = knn.nonzero()
    edges = list({(int(min(s, t)), int(max(s, t))) for s, t in zip(sources, targets) if s != t})
    g = ig.Graph(n=len(umap_xy), edges=edges, directed=False)

    def _run(res):
        part = leidenalg.find_partition(
            g, leidenalg.RBConfigurationVertexPartition,
            resolution_parameter=res, seed=random_state,
        )
        return np.array(part.membership)

    history = []
    if target_k is None:
        labels_raw = _run(resolution)
        final_res = resolution
    else:
        lo, hi = 1e-3, 10.0
        cur_res = resolution
        labels_raw = _run(cur_res)
        n = len(np.unique(labels_raw))
        history.append((cur_res, n))
        for _ in range(max_iter):
            if abs(n - target_k) <= tol:
                break
            if n < target_k:
                lo = cur_res
                cur_res = cur_res * 1.5 if hi == 10.0 else (cur_res + hi) / 2
            else:
                hi = cur_res
                cur_res = cur_res * 0.5 if lo == 1e-3 else (cur_res + lo) / 2
            labels_raw = _run(cur_res)
            n = len(np.unique(labels_raw))
            history.append((cur_res, n))
        final_res = cur_res

    # Size-rank rename so label '0' is largest etc.
    ranked = pd.Series(labels_raw).value_counts()
    remap = {orig: rank for rank, (orig, _) in enumerate(ranked.items())}
    labels = pd.Series([remap[x] for x in labels_raw])
    info = {
        'final_resolution': float(final_res),
        'n_clusters': int(labels.nunique()),
        'history': history,
    }
    return labels, info


@dataclass
class PartitionResult:
    """Return value of :func:`dissect_partition` — plain tables, no side effects.

    labels
        DataFrame indexed by ``adata.obs_names``: ``umap_cluster`` (``u0`` …,
        size-ranked globally), ``subcluster`` (``c{cluster}_{rank}``), ``rank``
        (int, 0 = main core within its RNA cluster) and ``is_main`` (bool).
        Write back with e.g. ``adata.obs["split"] = res.labels["subcluster"]``.
    overlap
        RNA-cluster × UMAP-cluster crosstab of cell counts — the "cartesian
        product" the split is derived from.
    fragments
        One row per non-empty (RNA cluster, UMAP fragment) combination:
        ``parent``, ``subcluster``, ``umap_label``, ``n_cells``,
        ``frac_of_parent``, ``rank``, ``is_main``, ``is_minor``.
        ``is_minor`` = rank > 0 **and** n_cells >= ``min_subcluster_size`` —
        the direct answer to "which minors hide in my clusters?".
    info
        UMAP-side clustering diagnostics: ``final_resolution``, ``n_clusters``
        and the (resolution, k) binary-search ``history``.
    """
    labels: pd.DataFrame
    overlap: pd.DataFrame
    fragments: pd.DataFrame
    info: dict = field(default_factory=dict)


def dissect_partition(
    adata,
    *,
    cluster_col: str,
    umap_key: str = "X_umap",
    umap_Nx2_mat: np.ndarray | None = None,
    umap_n_neighbors: int = 30,
    umap_resolution: float = 0.5,
    umap_target_k: int | None = None,
    umap_target_tol: int = 2,
    umap_random_state: int = 0,
    min_subcluster_size: int = 50,
) -> PartitionResult:
    """Split each precomputed RNA cluster into a main core + minor fragments.

    Crosses the **precomputed** RNA-side clustering in ``adata.obs[cluster_col]``
    with a UMAP-side clustering computed here (kNN + Leiden on
    ``adata.obsm[umap_key][:, :2]``). Within each RNA cluster its UMAP fragments
    are ranked by size and named ``c{cluster}_{rank}``: rank 0 is the clean
    main core, the rest are candidate minors. Pure function — ``adata`` is
    read-only and never modified; write-back is the caller's one-liner.

    Parameters
    ----------
    adata
        AnnData with ``obs[cluster_col]`` (the precomputed clustering — this
        function never re-clusters the RNA side) and a 2-D embedding in
        ``obsm[umap_key]``.
    cluster_col
        Name of the precomputed RNA-side cluster column in ``obs``.
    umap_key
        Key of the 2-D UMAP coordinates in ``obsm`` (extra columns ignored).
    umap_Nx2_mat
        Alternative to ``umap_key``: a precomputed UMAP coordinate matrix
        passed directly, one row per cell in ``adata`` order (>=2 columns,
        extra columns ignored). When given it takes precedence over
        ``umap_key`` and ``obsm`` is not touched; row count must equal
        ``adata.n_obs`` (``ValueError`` otherwise).
    umap_n_neighbors, umap_resolution, umap_random_state
        UMAP-side clustering knobs (kNN graph size, Leiden resolution / seed
        on that graph). Defaults are sane; rarely worth touching.
    umap_target_k, umap_target_tol
        Granularity matching. ``umap_target_k=None`` (default) targets the
        RNA side's own cluster count: the UMAP-side resolution is
        binary-searched until the UMAP partition has ``umap_target_k ±
        umap_target_tol`` clusters, so both partitions have comparable
        granularity — too fine chops cores into fake minors, too coarse
        swallows real ones. Pass an int to override the target explicitly.
    min_subcluster_size
        Not a clustering knob: fragments with rank > 0 and at least this many
        cells get ``is_minor=True`` in the fragment table.

    Returns
    -------
    PartitionResult
        ``labels`` / ``overlap`` / ``fragments`` / ``info`` (see its docstring).

    Raises
    ------
    KeyError
        ``cluster_col`` not in ``obs`` or ``umap_key`` not in ``obsm``.
    ValueError
        Embedding has fewer than 2 columns, or contains non-finite
        coordinates (count reported; clean or subset first — silently
        dropping rows would misalign labels with ``obs_names``).
    """
    if cluster_col not in adata.obs.columns:
        raise KeyError(f"cluster_col {cluster_col!r} not in adata.obs")
    if umap_Nx2_mat is not None:
        xy = np.asarray(umap_Nx2_mat, dtype=float)
        src = "umap_Nx2_mat"
        if xy.ndim != 2 or xy.shape[0] != adata.n_obs:
            raise ValueError(
                f"umap_Nx2_mat must have one row per cell "
                f"({adata.n_obs}), got shape {xy.shape}")
    else:
        if umap_key not in adata.obsm:
            raise KeyError(f"umap_key {umap_key!r} not in adata.obsm")
        xy = np.asarray(adata.obsm[umap_key], dtype=float)
        src = f"obsm[{umap_key!r}]"
    if xy.ndim != 2 or xy.shape[1] < 2:
        raise ValueError(f"{src} must be 2-D with >=2 columns, "
                         f"got shape {xy.shape}")
    xy = xy[:, :2]
    bad = int((~np.isfinite(xy)).any(axis=1).sum())
    if bad:
        raise ValueError(
            f"{bad} cells have non-finite coordinates in {src} — "
            f"clean or subset them first (silently dropping rows would "
            f"misalign labels with obs_names)")

    rna = adata.obs[cluster_col].astype(str)
    if umap_target_k is None:
        umap_target_k = int(rna.nunique())     # granularity matching (see docstring)

    raw, info = umap_leiden_partition(
        xy,
        target_k=umap_target_k,
        resolution=umap_resolution,
        n_neighbors=umap_n_neighbors,
        tol=umap_target_tol,
        random_state=umap_random_state,
    )
    umap_label = pd.Series([f"u{int(x)}" for x in raw], index=adata.obs_names,
                           name="umap_cluster")

    overlap = pd.crosstab(rna.values, umap_label.values)
    overlap.index.name = cluster_col
    overlap.columns.name = "umap_cluster"

    # Per RNA cluster, rank its UMAP fragments by size (desc; ties broken by
    # the crosstab's column order — deterministic) and name c{parent}_{rank}.
    frag_rows, name_of = [], {}
    for parent, row in overlap.iterrows():
        row = row[row > 0].sort_values(ascending=False, kind="stable")
        total = int(row.sum())
        for rank, (ulab, n) in enumerate(row.items()):
            sub = f"c{parent}_{rank}"
            name_of[(parent, ulab)] = (sub, rank)
            frag_rows.append({
                "parent": parent, "subcluster": sub, "umap_label": ulab,
                "n_cells": int(n), "frac_of_parent": float(n) / total,
                "rank": rank, "is_main": rank == 0,
                "is_minor": rank > 0 and int(n) >= min_subcluster_size,
            })
    fragments = pd.DataFrame(frag_rows, columns=[
        "parent", "subcluster", "umap_label", "n_cells", "frac_of_parent",
        "rank", "is_main", "is_minor"])

    pairs = [name_of[(p, u)] for p, u in zip(rna.values, umap_label.values)]
    labels = pd.DataFrame({
        "umap_cluster": umap_label.values,
        "subcluster": [s for s, _ in pairs],
        "rank": [r for _, r in pairs],
        "is_main": [r == 0 for _, r in pairs],
    }, index=adata.obs_names.copy())
    return PartitionResult(labels=labels, overlap=overlap,
                           fragments=fragments, info=info)

"""Doublets from Scrublet, contamination from decontX (#70).

Three doublet checks run on every cell before annotation; any one removes the cell:
  cell level      doublet_score above its library's threshold, and at least DOUBLET_RNA_FACTOR x the RNA of
                  the cells below the threshold in the same r1.0 cluster and library;
  doublet cluster an over-clustering (median cluster size ~OVERCLUSTER_SIZE) whose cluster has half or more of
                  its cells above the threshold and no gene of its own: the doublets Scrublet scores low gather
                  there with the ones it scores high;
  decontX-assisted a decontX candidate whose doublet_score is high for its own r1.0 cluster and whose RNA is
                  raised as above (decontX alone only says "carries another cluster's genes").
The library threshold is OSP's Scrublet threshold when enough simulated doublets exceed it, else a quantile of
the simulated scores (library_threshold); eca-rsi writes it per cell into obs[THRESHOLD_COL].

decontX decides contamination only, after the type agent (contamination_check): a candidate whose share of
UMIs on the soup genes foreign to its cluster is above the cluster's 99.9th percentile. Every percentile
check needs a background of at least min_background(p) cells; otherwise it is not made and the cell stays.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import scipy.sparse as sp

THRESHOLD_COL = "osp_doublet_threshold"  # per cell: its library's Scrublet threshold (eca-rsi fills it)
SIM_FALLBACK_QUANTILE = (
    0.75  # an untrusted threshold is replaced by this quantile of the simulated scores
)
DOUBLET_RNA_FACTOR = 1.3
MIN_GROUP = 20  # a cluster x library median needs this many cells below the threshold, else the cluster's
OVERCLUSTER_SIZE = (
    200  # median cluster size of the over-clustering the doublet-cluster check uses
)
OVERCLUSTER_SEED = 0
DOUBLET_CLUSTER_SHARE = 0.5
DOUBLET_CLUSTER_MIN_CELLS = 20
OWN_GENE_IN, OWN_GENE_OUT = (
    0.30,
    0.05,
)  # detected in >= 30% of the cluster and <= 5% of every other cluster
DECONTX_DOUBLET_QUANTILE = 0.95
CONTAMINATION_QUANTILE = 0.999
SOUP_TOP_GENES = 200
FOREIGN_FACTOR = 0.25  # a soup gene is foreign to a cluster when its share there is < 1/4 of its soup share
FOREIGN_MIN_UMIS = 3
DOMINANT_LINEAGE_SHARE = (
    0.5  # a lineage this large in the soup is not tested: the soup is mostly itself
)


def min_background(p):
    """Cells a background needs before its p-quantile means anything: a normal cell beats it with probability
    about 1/(n+1), so n >= 1/(1-p) (20 for the 95th, 100 for the 99th, 1,000 for the 99.9th percentile)."""
    return math.ceil(1 / (1 - p) - 1e-9)


def library_threshold(threshold, sim_scores, min_sim_above):
    """A library's doublet threshold: Scrublet's own when at least min_sim_above of its simulated doublets
    exceed it (OSP's rule for trusting it), else the SIM_FALLBACK_QUANTILE of the simulated scores; NaN when
    Scrublet did not run."""
    sim = np.asarray(sim_scores if sim_scores is not None else [], dtype=float)
    if not len(sim):
        return float("nan") if threshold is None else float(threshold)
    if threshold is not None and np.mean(sim > float(threshold)) >= min_sim_above:
        return float(threshold)
    return float(np.quantile(sim, SIM_FALLBACK_QUANTILE))


def doublet_above(obs):
    """Cells whose doublet_score is above their library's threshold (False where either is missing)."""
    if "doublet_score" not in obs or THRESHOLD_COL not in obs:
        return np.zeros(len(obs), dtype=bool)
    score = np.asarray(pd.to_numeric(obs["doublet_score"], errors="coerce"), dtype=float)
    threshold = np.asarray(pd.to_numeric(obs[THRESHOLD_COL], errors="coerce"), dtype=float)
    with np.errstate(invalid="ignore"):
        return np.nan_to_num(score) > np.where(np.isnan(threshold), np.inf, threshold)


def rna_reference(obs, below, cluster_key, library_col=None):
    """Per cell: the median total_counts of the cells below the threshold in its cluster and library, or in
    its cluster when that group has fewer than MIN_GROUP such cells."""
    counts = (
        pd.to_numeric(obs["total_counts"], errors="coerce").astype(float).where(below)
    )
    cluster = obs[cluster_key].astype(str)
    by_cluster = counts.groupby(cluster).transform("median")
    if not library_col or library_col not in obs:
        return by_cluster.to_numpy()
    groups = counts.groupby([cluster, obs[library_col].astype(str)])
    median, n = groups.transform("median"), groups.transform("count")
    return median.where(n >= MIN_GROUP, by_cluster).to_numpy()


def detection(X, groups, min_cells=1):
    """(names, fractions): for every group with at least min_cells cells, the share of its cells in which each
    gene is detected (groups x genes, dense float32)."""
    labels = pd.Series(np.asarray(groups).astype(str))
    sizes = labels.value_counts()
    names = sorted(sizes.index[sizes >= min_cells])
    if not names:
        return [], np.zeros((0, X.shape[1]), dtype=np.float32)
    code = labels.map({n: i for i, n in enumerate(names)})
    rows = code.dropna().astype(int).to_numpy()
    cells = np.flatnonzero(code.notna().to_numpy())
    onehot = sp.csr_matrix(
        (np.ones(len(rows), dtype=np.float32), (rows, cells)),
        shape=(len(names), len(labels)),
    )
    hit = (
        sp.csr_matrix(X, copy=True) if sp.issparse(X) else sp.csr_matrix(np.asarray(X))
    )
    hit.data = (hit.data > 0).astype(np.float32)
    frac = (
        np.asarray((onehot @ hit).todense(), dtype=np.float32)
        / sizes[names].to_numpy(dtype=np.float32)[:, None]
    )
    return names, frac


def own_gene_counts(X, groups, targets, min_cells=DOUBLET_CLUSTER_MIN_CELLS):
    """{target: number of genes detected in >= OWN_GENE_IN of its cells and <= OWN_GENE_OUT of every other
    group of at least min_cells cells}: a distinct type has some, a doublet group of two known types has none."""
    names, frac = detection(X, groups, min_cells)
    index = {n: i for i, n in enumerate(names)}
    out = {}
    for target in targets:
        i = index.get(str(target))
        if i is None:
            continue
        others = np.delete(frac, i, axis=0)
        outside = (
            others.max(axis=0)
            if len(others)
            else np.zeros(frac.shape[1], dtype=np.float32)
        )
        out[str(target)] = int(
            np.sum((frac[i] >= OWN_GENE_IN) & (outside <= OWN_GENE_OUT))
        )
    return out


def overcluster(ad, target=OVERCLUSTER_SIZE, seed=OVERCLUSTER_SEED, key="_overcluster"):
    """(labels, resolution): Leiden on ad's kNN graph at the resolution whose median cluster size is closest to
    `target` (a fixed resolution would give different sizes in datasets of different sizes). Bracketed by
    doubling, then a few log-scale bisections; the seed is fixed."""
    import scanpy as sc

    tried = {}

    def run(r):
        if r not in tried:
            sc.tl.leiden(
                ad,
                resolution=r,
                key_added=key,
                flavor="igraph",
                n_iterations=2,
                random_state=seed,
                adjacency=ad.obsp["connectivities"],
            )
            labels = ad.obs.pop(key).astype(str)
            tried[r] = (float(np.median(labels.value_counts().to_numpy())), labels)
        return tried[r][0]

    if ad.n_obs < 2 * target:
        run(0.1)
        return tried[0.1][1], 0.1
    lo = hi = 1.0
    if run(1.0) > target:
        while run(hi) > target and hi < 512:
            lo, hi = hi, hi * 2
    else:
        while run(lo) < target and lo > 0.02:
            lo, hi = lo / 2, lo
    for _ in range(5):
        mid = math.sqrt(lo * hi)
        if run(mid) > target:
            lo = mid
        else:
            hi = mid
    best = min(tried, key=lambda r: (abs(math.log(tried[r][0] / target)), r))
    return tried[best][1], best


def doublet_clusters(X, labels, above, min_cells=DOUBLET_CLUSTER_MIN_CELLS):
    """(removed cells, per-cluster table): clusters of at least min_cells cells with at least
    DOUBLET_CLUSTER_SHARE of their cells above the threshold and no own gene."""
    labels = pd.Series(np.asarray(labels).astype(str))
    share = pd.Series(above, dtype=float).groupby(labels.to_numpy()).mean()
    size = labels.value_counts()
    candidates = [
        c
        for c in share.index
        if size[c] >= min_cells and share[c] >= DOUBLET_CLUSTER_SHARE
    ]
    own = own_gene_counts(X, labels, candidates, min_cells) if candidates else {}
    doublet = {c for c in candidates if own.get(c, 1) == 0}
    table = pd.DataFrame(
        {
            "cluster": share.index,
            "n_cells": size[share.index].to_numpy(),
            "share_above": share.to_numpy().round(4),
        }
    )
    table["n_own_genes"] = table.cluster.map(own)
    table["doublet_cluster"] = table.cluster.isin(doublet)
    return labels.isin(doublet).to_numpy(), table


def doublet_checks(
    ad, cluster_key, decontx_candidate, library_col=None, counts_layer="counts"
):
    """The three doublet checks (module docstring) as a per-cell frame; None without a doublet_score or a library
    threshold, in which case no doublet is removed here. Over-clusters on ad's kNN graph (ad.obsp)."""
    obs = ad.obs
    if (
        "doublet_score" not in obs
        or THRESHOLD_COL not in obs
        or "total_counts" not in obs
        or cluster_key not in obs
    ):
        return None
    above = doublet_above(obs)
    ref = rna_reference(obs, ~above, cluster_key, library_col)
    with np.errstate(invalid="ignore"):
        rich = (
            np.asarray(pd.to_numeric(obs["total_counts"], errors="coerce"), dtype=float)
            >= DOUBLET_RNA_FACTOR * ref
        )
    out = pd.DataFrame(index=obs.index.copy())
    out["doublet_above"], out["rna_reference"], out["rna_raised"] = above, ref, rich
    out["doublet_cell"] = above & rich
    X = ad.layers[counts_layer] if counts_layer in ad.layers else ad.X
    if "connectivities" in ad.obsp and ad.n_obs >= DOUBLET_CLUSTER_MIN_CELLS:
        labels, resolution = overcluster(ad)
        in_cluster, _ = doublet_clusters(X, labels, above)
        out["overcluster"] = labels.to_numpy()
        out["overcluster_resolution"] = resolution
        out["doublet_cluster"] = in_cluster
    else:
        out["doublet_cluster"] = False
    score = pd.to_numeric(obs["doublet_score"], errors="coerce")
    cluster = obs[cluster_key].astype(str)
    background = score.where(~np.asarray(decontx_candidate, dtype=bool)).groupby(
        cluster
    )
    cut = background.transform(
        lambda s: (
            s.quantile(DECONTX_DOUBLET_QUANTILE)
            if s.count() >= min_background(DECONTX_DOUBLET_QUANTILE)
            else np.inf
        )
    )
    out["doublet_decontx"] = (
        np.asarray(decontx_candidate, dtype=bool) & (score > cut).to_numpy() & rich
    )
    out["doublet"] = out.doublet_cell | out.doublet_cluster | out.doublet_decontx
    return out


def contamination_check(
    ad, candidate, cluster_key, lineage, left=None, counts_layer="counts"
):
    """The decontX contamination check, one row per candidate cell (contamination True = remove).

    candidate: decontX candidates; lineage: per cell, the type agent's coarse label; left: the cells not removed
    by anything else (the background and the cluster profiles use only those). The soup is the pooled profile of
    every cell. A cluster is tested when it has at least min_background(CONTAMINATION_QUANTILE) background cells
    and its lineage (its cells' most common label) holds less than DOMINANT_LINEAGE_SHARE of the soup's UMIs."""
    X = ad.layers[counts_layer] if counts_layer in ad.layers else ad.X
    X = sp.csr_matrix(X)
    candidate = np.asarray(candidate, dtype=bool)
    left = (
        np.ones(ad.n_obs, dtype=bool) if left is None else np.asarray(left, dtype=bool)
    )
    lineage = pd.Series(np.asarray(lineage).astype(str), index=ad.obs_names)
    totals = np.asarray(X.sum(axis=1)).ravel()
    soup = np.asarray(X.sum(axis=0)).ravel()
    soup_share = soup / max(soup.sum(), 1)
    top = np.argsort(soup)[::-1][:SOUP_TOP_GENES]
    lineage_share = pd.Series(totals, index=ad.obs_names).groupby(lineage).sum() / max(
        totals.sum(), 1
    )
    cluster = ad.obs[cluster_key].astype(str).to_numpy()
    need = min_background(CONTAMINATION_QUANTILE)
    rows = []
    for c in sorted(set(cluster[candidate & left])):
        in_c = (cluster == c) & left
        targets = np.flatnonzero(in_c & candidate)
        background = np.flatnonzero(in_c & ~candidate)
        name = lineage[in_c].mode().iloc[0] if in_c.any() else ""
        row = dict(
            cluster=c,
            lineage=name,
            lineage_soup_share=round(float(lineage_share.get(name, 0.0)), 4),
            n_background=len(background),
        )
        if len(background) < need:
            status, foreign = f"not tested: background < {need} cells", []
        elif lineage_share.get(name, 0.0) >= DOMINANT_LINEAGE_SHARE:
            status, foreign = "not tested: dominant lineage of the soup", []
        else:
            profile = np.asarray(X[np.flatnonzero(in_c)].sum(axis=0)).ravel()
            share = profile / max(profile.sum(), 1)
            foreign = [g for g in top if share[g] < FOREIGN_FACTOR * soup_share[g]]
            status = "tested" if foreign else "not tested: no foreign soup gene"
        if foreign:
            on_f = np.asarray(X[:, foreign].sum(axis=1)).ravel()
            frac = on_f / np.maximum(totals, 1)
            cut = float(np.quantile(frac[background], CONTAMINATION_QUANTILE))
        for i in targets:
            r = dict(
                row, cell=ad.obs_names[i], status=status, n_foreign_genes=len(foreign)
            )
            if foreign:
                r.update(
                    foreign_umis=int(on_f[i]),
                    foreign_share=round(float(frac[i]), 5),
                    threshold=round(cut, 5),
                    contamination=bool(frac[i] > cut and on_f[i] >= FOREIGN_MIN_UMIS),
                )
            else:
                r["contamination"] = False
            rows.append(r)
    columns = [
        "cell",
        "cluster",
        "lineage",
        "lineage_soup_share",
        "n_background",
        "status",
        "n_foreign_genes",
        "foreign_umis",
        "foreign_share",
        "threshold",
        "contamination",
    ]
    return pd.DataFrame(rows, columns=columns).set_index("cell")

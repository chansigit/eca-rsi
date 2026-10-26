"""Stress cells of one lineage (#68): the rule that decides which cells zoom-in removes as dissociation stress.

A stress state is a deviation from cells of the same type, so it is tested inside a lineage only:
1. over-cluster on the lineage's kNN graph to a median cluster size of about 200 cells (msp's overcluster);
2. a candidate is a cluster of at least STRESS_MIN_CLUSTER cells with STRESS_MIN_GENES or more stress-panel genes
   at logFC >= STRESS_LFC, Bonferroni p < STRESS_PADJ and expressed in >= STRESS_PCT of its cells, one cluster
   against the rest of the lineage;
3. the background is the lineage's cells outside every candidate (at least STRESS_MIN_CLUSTER of them);
4. in each candidate the cells whose panel score (scanpy score_genes) is above the background's
   STRESS_QUANTILE are stress cells. No whole-cluster removal.
Calibrated on HCL adult adrenal gland (genuine stress: 11-21 panel genes pass) and PBMC68k (0.1 % of T/NK at 6 genes);
replayed on 05_Fuetal and tabula-sapiens-clean Lung (decision 0024).
"""

import numpy as np
import pandas as pd

STRESS_MIN_CLUSTER = 20
STRESS_MIN_GENES = 6  # owner 2026-10-25: 4 took 1.4 % of PBMC68k T/NK (an IEG-only group, 4-5 genes) per round
STRESS_LFC = 1.5
STRESS_PADJ = 0.01
STRESS_PCT = 0.10
STRESS_QUANTILE = 0.95


def stress_cells(ad, eligible=None):
    """(cells, clusters) for a lineage AnnData whose X is log-normalised and whose obsp holds its kNN graph.
    `eligible`: boolean mask of the cells to test (the others, already removed by a numerical check, are left
    out). cells: index cell; stress_overcluster, stress_score, stress_candidate, stress (bool). clusters: one row
    per over-cluster with its size, the panel genes that pass and whether it is a candidate. Cells and clusters
    are empty when the matrix holds no panel gene."""
    import scanpy as sc
    from msp.api import is_stress, overcluster

    sub = ad[eligible].copy() if eligible is not None else ad.copy()
    panel = [g for g in sub.var_names if is_stress(g)]
    empty = (
        pd.DataFrame(
            columns=["stress_overcluster", "stress_score", "stress_candidate", "stress"]
        ),
        pd.DataFrame(
            columns=[
                "overcluster",
                "n_cells",
                "n_panel_genes",
                "panel_genes",
                "candidate",
                "n_stress",
            ]
        ),
    )
    if not panel or sub.n_obs < 2 * STRESS_MIN_CLUSTER:
        return empty
    labels, resolution = overcluster(sub)
    sub.obs["_stress_oc"] = labels.to_numpy()
    sc.tl.score_genes(sub, panel, score_name="_stress_score", random_state=0)
    score = sub.obs["_stress_score"].to_numpy()
    sizes = sub.obs["_stress_oc"].value_counts()
    tested = sorted(sizes.index[sizes >= STRESS_MIN_CLUSTER], key=lambda c: (len(c), c))
    genes = {c: [] for c in sizes.index}
    if len(tested) > 1:
        p = sub[:, panel].copy()
        sc.tl.rank_genes_groups(
            p,
            "_stress_oc",
            groups=tested,
            reference="rest",
            method="wilcoxon",
            pts=True,
            key_added="_stress_de",
        )
        de = sc.get.rank_genes_groups_df(p, None, key="_stress_de")
        m = sub.n_vars  # ponytail: Bonferroni over every gene of the lineage, stricter than the calibration's BH
        strong = de[
            (de.logfoldchanges >= STRESS_LFC)
            & (de.pvals * m < STRESS_PADJ)
            & (de.pct_nz_group >= STRESS_PCT)
        ]
        for c, g in strong.groupby("group", observed=True):
            genes[str(c)] = [
                f"{n}:{lfc:.2f}" for n, lfc in zip(g.names, g.logfoldchanges)
            ]
    candidate = {c for c in tested if len(genes[c]) >= STRESS_MIN_GENES}
    oc = sub.obs["_stress_oc"].astype(str).to_numpy()
    inside = np.isin(oc, list(candidate))
    stress = np.zeros(sub.n_obs, dtype=bool)
    threshold = np.nan
    if candidate and (~inside).sum() >= STRESS_MIN_CLUSTER:
        threshold = float(np.quantile(score[~inside], STRESS_QUANTILE))
        stress = inside & (score > threshold)
    cells = pd.DataFrame(
        {
            "stress_overcluster": oc,
            "stress_score": score,
            "stress_candidate": inside,
            "stress": stress,
        },
        index=sub.obs_names,
    )
    cells.attrs.update(threshold=threshold, resolution=resolution)
    clusters = pd.DataFrame(
        [
            {
                "overcluster": c,
                "n_cells": int(sizes[c]),
                "n_panel_genes": len(genes[c]),
                "panel_genes": "|".join(genes[c]),
                "candidate": c in candidate,
                "n_stress": int(stress[oc == c].sum()),
            }
            for c in sizes.index
        ]
    )
    clusters = clusters.sort_values(["n_stress", "n_panel_genes"], ascending=False)
    clusters.attrs.update(threshold=threshold, resolution=resolution)
    return cells, clusters

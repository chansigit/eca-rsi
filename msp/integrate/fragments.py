"""Fragment evidence: minor-sibling QC on standissect-lite fragments and the
parent-core marker dot plot that explains what each fragment is."""

from __future__ import annotations

import csv
import functools
import logging
import math
import os
import re
from fractions import Fraction

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import scanpy as sc
import seaborn as sns
from matplotlib.colors import LinearSegmentedColormap
from scipy.cluster.hierarchy import dendrogram, linkage

from ..deg_logging import rank_genes_groups
from ..plots import UMAP_DPI

log = logging.getLogger(__name__)

# thresholds for minor-sibling QC testing (candidate detection, not a final
# verdict — msp.inspect judges what actually matters). "n_hits"/
# "recommend_removal" here are deliberately not "flag" — osp already uses
# keep/flag/drop for a different, per-cell concept and reusing the word
# would be confusing side by side.
HEATMAP_GENES = 150  # rows of fractal_marker_heatmap.png
HEATMAP_PIXELS = 16_000_000  # its size: the DPI drops below UMAP_DPI to stay under (#31)
MIN_N_FOR_TEST = 5  # below this, Mann-Whitney has no real power — mark insufficient_data
BIG_SIBLING_FRAC = 0.25  # sibling >= this fraction of its own parent's core is skipped, not a "minor" fragment
BIG_SIBLING_N = 800  # sibling >= this many cells (absolute) is skipped too, regardless of frac_of_core
MT_MEDIAN_THRESH = 20.0  # pct_counts_mt is already on a 0-100 scale
DROP_PCT_THRESH = 50.0  # % of a sibling's cells already _qc_action=drop upstream (osp)
MIN_AUC = 0.7  # a test also needs this effect size (owner, 2026-10-06, #27; decision 0017 judges dying cells the same way)


def _mwu_greater(sib_vals, core_vals):
    """One-sided Mann-Whitney U: is `sib_vals` clearly greater than `core_vals`: p < 0.05 and AUC >= MIN_AUC?
    None when either side has too few measured values."""
    test = _mwu_test(sib_vals, core_vals)
    return None if test is None else bool(test[0] < 0.05 and test[1] >= MIN_AUC)


def _mwu_test(sib_vals, core_vals):
    """(p, AUC) of a one-sided Mann-Whitney U, sibling greater than cores. Rank-based — doesn't assume
    normality, works for the small, skewed samples minor siblings usually are. AUC = U / (n1 n2): 0.5 is
    no shift; at a few hundred core cells a negligible shift is already significant, hence the AUC floor."""
    from scipy.stats import mannwhitneyu

    # Partial sample metadata must not turn an otherwise valid comparison into
    # a NaN p-value. Require enough measured values on both sides of the test.
    sib_vals = np.asarray(sib_vals, dtype=float)
    core_vals = np.asarray(core_vals, dtype=float)
    sib_vals = sib_vals[np.isfinite(sib_vals)]
    core_vals = core_vals[np.isfinite(core_vals)]
    if min(len(sib_vals), len(core_vals)) < MIN_N_FOR_TEST:
        return None
    n1, n2 = len(sib_vals), len(core_vals)
    if min(n1, n2) <= 8 and len(np.unique(np.concatenate([sib_vals, core_vals]))) == n1 + n2:
        # scipy's choice for this case is its exact test, whose recursion is quadratic in U: one 5-cell
        # sibling against 280k core cells took minutes per metric (batch 2). Same p, computed fast.
        u = mannwhitneyu(sib_vals, core_vals, alternative="greater", method="asymptotic").statistic
        return _exact_mwu_sf(int(u), n1, n2), float(u / (n1 * n2))
    u, p = mannwhitneyu(sib_vals, core_vals, alternative="greater")
    return float(p), float(u / (n1 * n2))


@functools.lru_cache(maxsize=8)
def _mwu_counts(n1, n2):
    """How many of the C(n1+n2, n1) rank arrangements without ties give U = 0 .. n1*n2/2: the coefficients of
    prod (1 - q^(n2+i)) / (1 - q^i), i = 1..min(n1, n2), in exact integers; linear in U per factor."""
    n1, n2 = sorted((n1, n2))
    m = n1 * n2 // 2
    counts = np.zeros(m + 1, dtype=object)
    counts[0] = 1
    for i in range(1, n1 + 1):  # divide by (1 - q^i)
        for r in range(i):
            counts[r::i] = np.cumsum(counts[r::i])
    for d in range(n2 + 1, n2 + n1 + 1):  # multiply by (1 - q^d)
        if d <= m:
            counts[d:] = counts[d:] - counts[:m + 1 - d]
    return counts


def _exact_mwu_sf(u, n1, n2):
    """P(U >= u) under the null, without ties: scipy's exact Mann-Whitney p-value."""
    counts, total = _mwu_counts(n1, n2), math.comb(n1 + n2, n1)
    if n1 * n2 - u < len(counts):  # P(U >= u) = P(U <= n1*n2 - u), the distribution is symmetric
        return float(Fraction(int(sum(counts[:n1 * n2 - u + 1])), total))
    return float(Fraction(total - int(sum(counts[:u])), total))


def _minor_sibling_qc(ad, res, outdir):
    """Test minor-sibling fragments (standissect subclusters c{parent}_i,
    i>0) — candidates for msp.inspect to verify, but ANY one of these
    criteria hitting is enough to mark the whole fragment recommend_removal
    (this function proposes, it never removes anything itself).

    Parent cores (rank 0) are never tested. Siblings holding >=25% of their
    own parent core's cell count, OR >=800 cells outright, are "big"
    fragments, not minor — skipped entirely (no rows below apply to them).

    For every remaining (non-big) sibling (#70; decontX and dissociation are no
    fragment tests any more: a fragment of another subtype carries other clusters'
    genes, and stress is judged within a lineage, #68):
      - upstream drop: >50% of its cells already carry _qc_action=="drop" from the
        per-sample annotation (computed even when the sibling is too small to test);
      - doublet: the doublet-cluster criterion of msp.integrate.doublets — at least
        20 cells, at least half above their library's Scrublet threshold, and no gene
        of its own against the other standissect products;
      - mt: one-sided Mann-Whitney U against its OWN parent core (the largest UMAP
        piece of the same parent), p < 0.05 and AUC >= MIN_AUC, and its median above
        MT_MEDIAN_THRESH; needs >= MIN_N_FOR_TEST cells."""
    from .doublets import DOUBLET_CLUSTER_MIN_CELLS, DOUBLET_CLUSTER_SHARE, THRESHOLD_COL, doublet_above, own_gene_counts

    frag = res.fragments
    core_n = frag.loc[frag["rank"] == 0].set_index("parent")["n_cells"]
    core_of = frag.loc[frag["rank"] == 0].set_index("parent")["subcluster"]
    product = ad.obs["standissect_product"].astype(str)
    above = doublet_above(ad.obs)
    has_threshold = THRESHOLD_COL in ad.obs
    X = ad.layers["counts"] if "counts" in ad.layers else ad.X

    rows, doublet_targets = [], []
    for _, frow in frag[frag["rank"] > 0].iterrows():
        sub, parent, n_cells = frow["subcluster"], frow["parent"], int(frow["n_cells"])
        cn = int(core_n.get(parent, 0))
        row = {
            "subcluster": sub,
            "parent": parent,
            "n_cells": n_cells,
            "core_n_cells": cn,
            "frac_of_core": round(n_cells / cn, 3) if cn else None,
        }
        if (cn and n_cells >= BIG_SIBLING_FRAC * cn) or n_cells >= BIG_SIBLING_N:
            row["status"] = "big_sibling_skip"
            rows.append(row)
            continue

        sib_mask = (product == sub).to_numpy()
        if "_qc_action" in ad.obs:
            row["pct_drop_upstream"] = round(float((ad.obs.loc[sib_mask, "_qc_action"] == "drop").mean() * 100), 2)

        if has_threshold:
            row["doublet_above_pct"] = round(float(above[sib_mask].mean() * 100), 2)
            if n_cells >= DOUBLET_CLUSTER_MIN_CELLS and above[sib_mask].mean() >= DOUBLET_CLUSTER_SHARE:
                doublet_targets.append(sub)

        if n_cells < MIN_N_FOR_TEST:
            row["status"] = "insufficient_data"
        else:
            row["status"] = "tested"
            if "pct_counts_mt" in ad.obs and parent in core_of.index:
                sib_vals = ad.obs.loc[sib_mask, "pct_counts_mt"]
                core_vals = ad.obs.loc[(product == core_of[parent]).to_numpy(), "pct_counts_mt"]
                test = _mwu_test(sib_vals, core_vals)
                sig = None if test is None else bool(test[0] < 0.05 and test[1] >= MIN_AUC)
                row["mt_median"] = round(float(sib_vals.median()), 4)
                row["mt_auc"] = None if test is None else round(test[1], 3)
                row["mt_significant"] = bool(sig and sib_vals.median() > MT_MEDIAN_THRESH) if sig is not None else None
        rows.append(row)

    own = own_gene_counts(X, product.to_numpy(), doublet_targets) if doublet_targets else {}
    for row in rows:
        if row["status"] == "big_sibling_skip":
            continue
        sub = row["subcluster"]
        if sub in doublet_targets:
            row["n_own_genes"] = own.get(sub)
        row["doublet_significant"] = bool(sub in doublet_targets and own.get(sub, 1) == 0) if has_threshold else None
        drop_hit = row.get("pct_drop_upstream", 0) > DROP_PCT_THRESH
        hits = [bool(row.get("doublet_significant")), bool(row.get("mt_significant"))]
        row["n_hits"] = sum(hits)
        row["recommend_removal"] = bool(drop_hit or any(hits))

    df = pd.DataFrame(rows)
    df.to_csv(os.path.join(outdir, "minor_sibling_qc.csv"), index=False)
    return df


# Seurat's classic DoHeatmap palette (colorRampPalette(c("purple","black","yellow")))
SEURAT_HEATMAP_CMAP = LinearSegmentedColormap.from_list("seurat_purple_black_yellow", ["purple", "black", "yellow"])


def _select_fractal_markers(de_df, ribo, top_n):
    """Per-parent marker selection from a rank_genes_groups_df table (group,
    names, logfoldchanges, pvals_adj): logFC>0, padj<0.05, ribosomal genes
    excluded, top_n by logFC per parent. Returns ``(marker_rows, gene_parent,
    markers)`` — the fractal_markers.csv rows, the parent each gene is a
    marker for (first parent wins when a gene repeats), and the de-duplicated
    gene list in parent order (numeric, not the groupby's alpha-sort), then
    rank within parent."""
    marker_rows = []
    for parent, g in de_df.groupby("group", observed=True):
        g = g[(g["pvals_adj"] < 0.05) & (g["logfoldchanges"] > 0) & (~g["names"].isin(ribo))]
        g = g.sort_values("logfoldchanges", ascending=False).head(top_n)
        for rank, row in enumerate(g.itertuples(), start=1):
            marker_rows.append(
                {
                    "parent": parent,
                    "gene": row.names,
                    "rank": rank,
                    "logfoldchange": round(row.logfoldchanges, 3),
                    "pvals_adj": row.pvals_adj,
                }
            )
    marker_rows_sorted = sorted(marker_rows, key=lambda r: (int(r["parent"]), r["rank"]))
    gene_parent = {}
    for r in marker_rows_sorted:  # a gene shared across parents belongs to its lowest parent, like its row
        gene_parent.setdefault(r["gene"], r["parent"])
    markers = list(gene_parent)
    return marker_rows, gene_parent, markers


def _fractal_marker_heatmap(ad, res, outdir, figdir, top_n=10):
    """Explain fractal identity from the transcriptome, not just QC stats:
    DE each parent's core cells one-vs-rest against every OTHER parent's
    core cells (core-only, so a minor sibling's cells never leak into
    either side of the comparison) -> per-parent top markers (logFC>0,
    padj<0.05, ribosomal genes excluded) -> dot plot across every
    standissect cluster (cores AND fractals): dot size = fraction of a
    cluster's cells expressing the gene, dot color = row-wise (per-gene)
    z-score of average log1p expression, Seurat-style purple-black-yellow.

    Columns are clustered (optimal leaf ordering) purely to order them — no
    dendrogram is drawn. Rows are NOT clustered: genes stay grouped by the
    parent they're a marker for, with a colored strip per gene showing that
    parent, since that grouping is more legible than a gene-gene dendrogram
    here. Column labels: parent-core clusters (c{parent}_0) bold; clusters
    minor_sibling_qc.csv marked recommend_removal in red."""
    frag = res.fragments
    core_rows = frag.loc[frag["rank"] == 0]
    parent_of_core = dict(zip(core_rows["subcluster"], core_rows["parent"], strict=True))
    core_mask = ad.obs["standissect_product"].astype(str).isin(parent_of_core)
    core_ad = ad[core_mask].copy()
    core_ad.obs["parent"] = (
        core_ad.obs["standissect_product"].astype(str).map(parent_of_core).astype(str).astype("category")
    )

    sizes = core_ad.obs["parent"].value_counts()
    eligible = sizes.index[(sizes >= 2) & (len(core_ad) - sizes >= 2)].tolist()
    skipped = sizes.loc[~sizes.index.isin(eligible)]
    if len(skipped):
        pd.DataFrame({"parent": skipped.index, "n_core_cells": skipped.to_numpy(),
                      "reference_cells": len(core_ad) - skipped.to_numpy(),
                      "reason": "target or reference has fewer than two core cells"}).to_csv(
            os.path.join(outdir, "parent_core_deg_skipped.csv"), index=False)
        log.warning("== parent-core DEG unavailable for groups %s; cells retained", skipped.index.tolist())
    if not eligible:
        log.warning("== no testable parent-core comparisons — skipping parent-core DEG/heatmap")
        return

    log.info("== parent-core DEG (one vs other parent cores)")
    # Untestable target groups remain in the reference and in the plotted cells.
    rank_genes_groups(core_ad, "parent", groups=eligible, method="wilcoxon", use_raw=False, pts=True)
    de_df = sc.get.rank_genes_groups_df(core_ad, group=None)
    if "group" not in de_df:
        de_df["group"] = eligible[0]  # Scanpy omits this column for one tested group.
    de_df.to_csv(os.path.join(outdir, "de_parent_core_vs_core.csv"), index=False)

    ribo = set(ad.var_names[ad.var["ribo"]]) if "ribo" in ad.var else set()
    marker_rows, gene_parent, markers = _select_fractal_markers(de_df, ribo, top_n)
    pd.DataFrame(marker_rows).to_csv(os.path.join(outdir, "fractal_markers.csv"), index=False)
    if not markers:
        log.warning("== no marker genes passed the filters — skipping heatmap")
        return

    log.info(
        f"== fractal marker dot plot ({len(markers)} genes x {ad.obs['standissect_product'].nunique()} clusters)",
    )
    sub = ad[:, markers]
    X = sub.X.toarray() if hasattr(sub.X, "toarray") else np.asarray(sub.X)
    expr = pd.DataFrame(X, index=ad.obs_names, columns=markers)
    expr["cluster"] = ad.obs["standissect_product"].astype(str).values
    grp = expr.groupby("cluster")
    avg = grp[markers].mean().T  # genes x clusters, average log1p expression
    frac = grp[markers].apply(lambda d: (d > 0).mean()).T  # genes x clusters, fraction expressing
    avg.to_csv(os.path.join(outdir, "fractal_marker_avg_expr.csv"))
    frac.to_csv(os.path.join(outdir, "fractal_marker_frac_expr.csv"))

    z = avg.sub(avg.mean(axis=1), axis=0).div(avg.std(axis=1).replace(0, 1), axis=0).fillna(0)
    z = z.loc[markers]  # parent-then-rank order (see marker_rows_sorted above)
    frac = frac.loc[markers]

    # column order from hierarchical clustering (optimal leaf ordering) — used
    # only to order columns, no dendrogram is drawn
    col_linkage = linkage(z.values.T, method="average", metric="euclidean", optimal_ordering=True)
    leaf_order = dendrogram(col_linkage, no_plot=True)["leaves"]
    cluster_order = [z.columns[i] for i in leaf_order]
    z, frac = z[cluster_order], frac[cluster_order]
    # The figure draws at most HEATMAP_GENES rows, each parent's first markers; fractal_markers.csv and the
    # expression tables keep top_n per parent. Batch 2 drew up to 378 rows x 200 fragments, 9853x12055 px,
    # past the model provider's 36 Mpx per image (#31).
    if len(markers) > HEATMAP_GENES:
        rank = {(r["parent"], r["gene"]): r["rank"] for r in marker_rows}
        per_parent = max(3, HEATMAP_GENES // len(set(gene_parent.values())))
        markers = [g for g in markers if rank[(gene_parent[g], g)] <= per_parent]
        z, frac = z.loc[markers], frac.loc[markers]

    removal_path = os.path.join(outdir, "minor_sibling_qc.csv")
    removal_set = set()
    if os.path.exists(removal_path):
        with open(removal_path) as f:
            removal_set = {r["subcluster"] for r in csv.DictReader(f) if r.get("recommend_removal") == "True"}
    core_pattern = re.compile(r"^c\d+_0$")

    parents = sorted(set(gene_parent.values()), key=int)
    parent_palette = dict(zip(parents, sns.color_palette("tab20", n_colors=max(len(parents), 1)), strict=False))

    n_genes, n_clusters = z.shape

    # Every margin below is a fixed inch amount, not a figure-fraction — so
    # whitespace stays constant (tight) regardless of n_genes/n_clusters,
    # instead of growing with figure size the way fraction-based offsets did.
    left_margin, strip_w, col_gap, right_margin = 1.3, 0.18, 0.06, 0.15
    top_margin, plot_col_w = 0.1, n_clusters * 0.32
    xtick_h, legend_gap, legend_h, bottom_margin = 0.9, 0.12, 0.8, 0.05
    plot_row_h = max(2.0, n_genes * 0.16)

    fig_w = left_margin + strip_w + col_gap + plot_col_w + right_margin
    fig_h = top_margin + plot_row_h + xtick_h + legend_gap + legend_h + bottom_margin
    fig = plt.figure(figsize=(fig_w, fig_h))

    plot_bottom = (xtick_h + legend_gap + legend_h + bottom_margin) / fig_h
    plot_h = plot_row_h / fig_h
    ax_strip = fig.add_axes((left_margin / fig_w, plot_bottom, strip_w / fig_w, plot_h))
    ax = fig.add_axes(
        ((left_margin + strip_w + col_gap) / fig_w, plot_bottom, plot_col_w / fig_w, plot_h), sharey=ax_strip
    )

    strip = np.array([parent_palette[gene_parent[g]] for g in markers])[:, None, :]
    ax_strip.imshow(strip, aspect="auto")
    ax_strip.set_xticks([])
    ax_strip.set_yticks(range(n_genes))
    ax_strip.set_yticklabels(markers, fontsize=8)
    ax_strip.set_ylabel("marker gene")

    xs, ys, sizes, colors = [], [], [], []
    for xi, cl in enumerate(cluster_order):
        for yi, gene in enumerate(markers):
            xs.append(xi)
            ys.append(yi)
            sizes.append(frac.loc[gene, cl])
            colors.append(z.loc[gene, cl])
    max_dot_area = 200.0
    sca = ax.scatter(
        xs,
        ys,
        s=np.asarray(sizes) * max_dot_area + 2,
        c=colors,
        cmap=SEURAT_HEATMAP_CMAP,
        vmin=-2.5,
        vmax=2.5,
        edgecolor="none",
    )
    for x in range(1, n_clusters):  # thin separators between columns only
        ax.axvline(x - 0.5, color="#dddddd", linewidth=0.6, zorder=0)
    ax.set_xlim(-0.5, n_clusters - 0.5)
    ax.set_ylim(n_genes - 0.5, -0.5)
    ax.set_xticks(range(n_clusters))
    ax.set_xticklabels(cluster_order, rotation=90)
    # NOT ax.set_yticks([]) — sharey means the y Locator is shared with
    # ax_strip; clearing ticks here would silently wipe its gene labels too
    ax.tick_params(axis="y", left=False, labelleft=False)
    ax.set_xlabel("standissect cluster (core + fractals)")
    for label in ax.get_xticklabels():
        name = label.get_text()
        if core_pattern.match(name):
            label.set_fontweight("bold")
        if name in removal_set:
            label.set_color("#c0392b")

    # colorbar (dot color = z-score) and size legend, packed into the fixed
    # legend_h/bottom_margin band reserved above — no negative coordinates,
    # so there's no bbox_inches="tight" guesswork; every element's text stays
    # within its own axes' [0,1] range so nothing depends on overflow room
    cax = fig.add_axes(
        ((left_margin + strip_w + col_gap) / fig_w, (bottom_margin + 0.4) / fig_h, 1.6 / fig_w, 0.15 / fig_h)
    )
    fig.colorbar(sca, cax=cax, orientation="horizontal", label="z-score")

    lax = fig.add_axes(
        ((left_margin + strip_w + col_gap + 2.1) / fig_w, bottom_margin / fig_h, 2.2 / fig_w, legend_h / fig_h)
    )
    lax.set_xlim(0, 4)
    lax.set_ylim(0, 1)
    lax.axis("off")
    for i, frac_ref in enumerate((0.25, 0.5, 0.75, 1.0)):
        lax.scatter([i], [0.55], s=frac_ref * max_dot_area + 2, c="grey")
        lax.text(i, 0.15, f"{frac_ref:g}", ha="center", va="top", fontsize=7)
    lax.text(1.5, 0.95, "fraction expressing", ha="center", va="top", fontsize=8)

    fig.savefig(os.path.join(figdir, "fractal_marker_heatmap.png"), dpi=min(UMAP_DPI, (HEATMAP_PIXELS / (fig_w * fig_h)) ** 0.5))
    plt.close("all")

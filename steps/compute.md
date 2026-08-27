# Step: compute — rebuild the feature space for this round's scope

Scripted analysis only — no judgments about cell identity or quality here.
Read this round's `explore.md` for the scope and goals.

## Round 1 only: build the checkpoint

- Merge the files explore selected into `checkpoint.h5ad` (save the merge
  script; keep per-file provenance in an obs column). The provenance value is
  the sample's **directory name**, not the filename — ecasteps outputs are all
  named `standardized.h5ad`, so filenames cannot distinguish samples. If
  explore's plan says the batch key comes from an upstream `batch.tsv` (a
  derived per-cell mapping, `cell_id<TAB>value`), merge those values into obs
  now. If files carry prior labels under different column names, unify them
  into one column (e.g. `prior_label`) per explore's plan, keeping a note of
  each file's source column.
- Ensure a raw-counts layer exists. Inputs that explore identified as
  ecasteps-standardized already carry `layers["counts"]` (integer, verified
  upstream) — use it, don't re-derive. Otherwise, if X is log-normalized,
  recover counts (reverse log1p) and sanity-check the recovery (integer-ness,
  or correlation with a provided total-counts column). Never fabricate counts.
- Per-cell QC metrics (total counts, genes, pct mito/ribo/hb). Standardized
  inputs already carry authoritative `pct_counts_mt` / `pct_counts_hb` /
  `total_counts` / `n_genes_by_counts` — keep them; `pct_counts_hb` is direct
  evidence for the red-blood-cell noise class later. Ignore `*__original`
  columns (superseded upstream copies).
- Doublet scores per sample on the **complete** per-sample pool (scrublet).
  Cache scores in obs — this is the one thing computed once and kept, because
  it is only valid on the full pool.

## Every round, on the current cells (and this round's scope)

Recompute — never reuse a previous round's result — because removing cells
changes what the feature space can see (docs/CONSTITUTION.md, preamble).
The context header states a **re-embed exemption threshold**. At its default
of 0% there is **no exception**: whenever the cell count has changed since
the global embedding was last built, rebuild it this round. Only if the
threshold is positive AND the previous round removed fewer than that percent
of cells may you skip the *global* re-embedding and scope this round's
compute to the subsets the plan targets — and then you must state the skip,
the threshold, and the removal count in `compute.md`. (This exemption was
once chained round after round, so a dataset shipped its round-1 partition —
leftover crumbs of partially-removed clusters, including a 1-cell cluster,
survived to release. Skipping is the exception, never the routine.) The
global UMAP figures are **not** part of any exemption: they are for the
human reviewer, cost seconds from the existing checkpoint, and are produced
every round regardless.

- HVG selection → PCA → integration across the sample/batch key (harmonypy)
  → neighbors → leiden at 2–3 resolutions → UMAP.
- Population-relative statistics (e.g. MAD outlier thresholds) recomputed on
  the current cells. Cached per-cell *metrics* are fine; cached *thresholds*
  are not.
- Whatever tables this round's goals need: per-cluster marker DE (with
  per-sample consistency if there are ≥2 samples), signature/QC summaries per
  cluster, composition crosstabs vs sample and condition.
- **Figures for the human reviewer** (they cannot read your tables). The
  per-round set is fixed — same names in every round dir:
  - `umap_clusters.png` — this round's clusters
  - `umap_sample.png` — sample/batch key (the integration check)
  - `umap_qc.png` — two panels: pct mitochondrial counts, doublet score
  - `umap_label_coarse.png` — `label_l1` (once labels exist)
  - `umap_label_fine.png` — `label_l2` (once labels exist)
  - `umap_removed.png` — this round's removals in red on lightgray retained
    cells (produced by the **apply** step, listed here so the fixed set is
    documented in one place)
  - `dotplot_label_coarse.png` / `dotplot_label_fine.png` — dot plots
    (`sc.pl.dotplot`) of the genes that carry this round's argument, grouped
    by `label_l1` / `label_l2` (once labels exist): the top discriminating
    markers per population (2–3 each at fine granularity, more at coarse),
    plus doublet-indicator genes (the co-expressed cross-lineage markers
    used in doublet verdicts) and any gene a decision cites. Order genes by
    group so the diagonal reads.

  **Figure quality rules — every UMAP, whoever produces it (compute or
  apply), main set or `lineage_*` drill-down:**
  - **Square, equal-scale axes**: `ax.set_aspect("equal")` and identical
    x/y limits (one shared range covering both UMAP dimensions), so the
    axes box is square and distances are undistorted. Never let a long
    legend reshape the plot.
  - **Legend below the axes, never beside them**:
    `loc="upper center", bbox_to_anchor=(0.5, -0.06), frameon=False`, with
    `ncol` chosen so entries wrap into tidy rows (long fine-granularity
    labels → fewer columns; use a smaller font before using more space).
  - **Legend markers are large filled circles, decoupled from scatter point
    size**: build the handles yourself —
    `Line2D([], [], marker="o", linestyle="", markersize=8, color=hex)` —
    rather than reusing scatter handles, whose legend size follows the
    point size and becomes invisible on large datasets.
  - **Point size adapts to cell count** (e.g. `s = clip(12000/n, 0.5, 10)`),
    which is exactly why the legend markers above must not inherit it.
  - **Every categorical coloring uses a stanhue palette** (the context
    header gives the scripts path): `sys.path.insert(0, <that path>)`, then
    `from scatter_colormap import assign_celltype_colors`;
    `assign_celltype_colors(coords, labels)` returns `{label: hex}` —
    related populations get adjacent shades, distant lineages distinct hue
    families. Deterministic given coords+labels; colors may legitimately
    shift between rounds because coordinates change. Continuous panels (QC)
    use a standard sequential colormap, not stanhue.

  **Drill-down figures** (lineage re-embeddings, boundary tests) follow one
  naming scheme so the same object lines up across rounds:
  `lineage_<lineage>_<content>.png` — e.g. `lineage_endothelial_subembed.png`,
  `lineage_fibrochondrocyte_depth_corrected.png`. Never prefix figures with
  script numbers (`c01_`, `c02_`): those are per-round and make figures
  impossible to align across rounds.
- If a previous round exists: a crosstab of **this round's clusters vs the
  previous round's labels** (cells carry their labels in obs, so this is one
  line). Cluster numbering changes every round — this table is how annotate
  knows which new cluster is which old population, without it renames cannot
  be explained.

Write cluster assignments and scores back into `checkpoint.h5ad` — but never
in place: write `checkpoint.tmp.h5ad`, then rename it over the old file. The
checkpoint is the loop's only state; a crash halfway through an in-place
write destroys it unrecoverably, a rename cannot. The naming convention is
`roundNN_<name>` (e.g. `round03_leiden_r10`, `round03_ec_leiden_r20`) —
per-round columns are the audit trail that makes cross-round drift visible; results from earlier rounds that no
longer apply (e.g. clustering of cells now outside the scope) must not be
left looking current. Save every script. Report in `compute.md`: what was
computed, cluster count and sizes, and anything anomalous you noticed on the
way.

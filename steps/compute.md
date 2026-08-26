# Step: compute — rebuild the feature space for this round's scope

Scripted analysis only — no judgments about cell identity or quality here.
Read this round's `explore.md` for the scope and goals.

## Round 1 only: build the checkpoint

- Merge the files explore selected into `checkpoint.h5ad` (save the merge
  script; keep per-file provenance in an obs column). If explore's plan says
  the batch key comes from an upstream `batch.tsv` (a derived per-cell
  mapping, `cell_id<TAB>value`), merge those values into obs now.
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
changes what the feature space can see (docs/CONSTITUTION.md, preamble):

- HVG selection → PCA → integration across the sample/batch key (harmonypy)
  → neighbors → leiden at 2–3 resolutions → UMAP.
- Population-relative statistics (e.g. MAD outlier thresholds) recomputed on
  the current cells. Cached per-cell *metrics* are fine; cached *thresholds*
  are not.
- Whatever tables this round's goals need: per-cluster marker DE (with
  per-sample consistency if there are ≥2 samples), signature/QC summaries per
  cluster, composition crosstabs vs sample and condition.
- UMAP figures for the human reviewer (they cannot read your tables):
  `umap_clusters.png`, `umap_sample.png`, `umap_qc.png` (pct mito / doublet
  score), and `umap_labels.png` once labels exist — saved in the round dir.
- If a previous round exists: a crosstab of **this round's clusters vs the
  previous round's labels** (cells carry their labels in obs, so this is one
  line). Cluster numbering changes every round — this table is how annotate
  knows which new cluster is which old population, without it renames cannot
  be explained.

Write cluster assignments and scores back into `checkpoint.h5ad` — but never
in place: write `checkpoint.tmp.h5ad`, then rename it over the old file. The
checkpoint is the loop's only state; a crash halfway through an in-place
write destroys it unrecoverably, a rename cannot. Name results so
the round they belong to is unambiguous; results from earlier rounds that no
longer apply (e.g. clustering of cells now outside the scope) must not be
left looking current. Save every script. Report in `compute.md`: what was
computed, cluster count and sizes, and anything anomalous you noticed on the
way.

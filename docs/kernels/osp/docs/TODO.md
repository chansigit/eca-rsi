# TODO

## Local-view DEG via top PAGA neighbors

Use the PAGA connectivity matrix (`paga_connectivities_{key}.csv`, from
`sc.tl.paga` in `osp.cluster.cluster_and_deg`) to give the annotation agent a
local differential-expression view.

- For each primary cluster, select its top-k PAGA neighbors by connectivity.
- Run `deg_two_groups` (or a one-vs-neighbors Wilcoxon test) restricted to
  the cluster and those neighbors, instead of the current one-vs-rest contrast.
- Expose the result to the agent as a tool (e.g. `check_local_deg`) and/or a
  `de_local_{key}.csv` table, alongside the existing `check_genes`,
  `check_qc_scores`, and `subcluster` tools in `osp.annotate`.

Motivation: one-vs-rest markers are dominated by broad lineage genes. A
neighbor-restricted contrast surfaces the genes that separate adjacent,
closely related populations, which is where the agent most often needs finer
detail to distinguish subtypes, transitional states, and doublets from true
biology. PAGA is already computed and shown to the agent, so this reuses an
existing evidence layer without changing clustering, QC, or filtering.

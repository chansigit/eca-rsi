# News

Release notes for OSP. Newest first. Install a specific version with
`pip install "osp-sc[agent]==<version>"`.

## 0.1.1 (2026-09-04)

The first update after the initial PyPI release. The pipeline's inputs,
outputs, and reruns now follow explicit contracts, the annotation agent runs
on a shared, backend-agnostic harness, and the documentation was rewritten
around the sample-first QC and AI annotation workflow.

### Annotation agent runtime

- The agent now runs through the shared `agent-harness-bridge` package
  instead of a Claude-only integration. `osp.harness` is a thin
  compatibility shim over that package.
- Three backends are available: `openai` (default), `deepseek`, and
  `claude`. Select one with the new `--harness` CLI flag or the `HARNESS`
  environment variable; `--model` and `--effort` apply to the selected
  backend. The `osp-sc[agent]` extra installs `agent-harness-bridge[all]`.
- The agent's biological tools are declared as `ToolSpec` entries:
  `check_genes`, `check_qc_scores`, `subcluster`, and `submit_annotation`.
  The harness owns provider execution, retries, timeouts, and submission
  control; OSP owns the tools and proposal validation.
- `propose_annotation` resolves the clustering column from the PAGA groups
  stored in `clustered.h5ad` before falling back to table names, and rejects
  a `cluster_key` that is not present in `obs`.
- A previous `annotation_proposal.json` is removed before the agent starts,
  and the new proposal is published only after labels are written to the
  H5AD and the report is refreshed. A run that ends without a validated
  proposal raises instead of leaving stale completion markers behind.

### Pipeline contracts and validation

- QC and clustering validate their inputs up front: non-empty AnnData with
  at least one gene, one sample per run, finite non-negative counts, at
  least three cells and two genes for clustering, at least two selected
  HVGs, and numeric, finite, non-negative Leiden resolutions.
- `qc_pca_covariates` must be a sequence of column names, and covariates
  containing NaN or infinite values are rejected before PCA.
- All durable outputs (`clustered.h5ad`, CSV tables, JSON proposals, and
  `report.html`) are written through a sibling temporary file and replaced
  atomically (new `osp._io`). H5AD files are reopened and shape-checked
  before replacement.
- Reruns invalidate stale outputs: `report.html`, the proposal, and notes
  are removed at the start of `run_one_sample_pipeline`; derived plots and
  obsolete primary-resolution tables are removed when replacement
  clustering is ready to write.
- The report refuses to guess when a directory holds several QC overview
  files or primary cluster summaries, and asks for one directory per sample.
- The main CLI opens the input in backed mode, loads only the requested
  sample, checks that the sample column and label exist, and closes the file
  before analysis.
- DecontX now always starts from OSP's explicit coarse Leiden clustering. A
  fit that stays pinned near complete contamination is flagged in
  `uns["osp_decontx_degenerate"]`, and its contamination column is excluded
  from PCA covariates while the estimates remain available for inspection.

### Tests and tooling

- A pytest suite covers the annotation contract, cluster contracts, atomic
  I/O, QC and DecontX behaviour, and the report contract. Install it with
  the new `osp-sc[test]` extra, which also brings `ruff`.

### Documentation

- README rewritten for first-time users: why sample-first QC, what you get,
  how QC and cluster interpretation work, and how OSP fits the ECA-RSI
  ecosystem with ECA-PP, MSP, and ZMIP.
- New [input and output reference](input-output.md) covering matrix
  contents, output files, Python return values, reruns, and completion
  rules.
- New [interactive workflow diagram](https://raw.githack.com/chansigit/osp/main/docs/diagrams/osp-workflow.html),
  embedded in the README as light and dark SVGs and regenerable from
  `docs/diagrams/osp-workflow.archify.json` with `docs/diagrams/export_svg.py`.
- Project logo, README badges, and a [TODO](TODO.md) for local-view DEG
  over top PAGA neighbors.
- The example driver and Slurm array template are backend-agnostic.

## 0.1.0 (2026-09-02)

Initial PyPI release as `osp-sc` (the import name stays `osp`).

- Single-sample QC with fixed and MAD-adaptive thresholds, Scrublet doublet
  detection, and a vendored pure-Python DecontX for ambient RNA estimates.
- Leiden clustering, UMAP, Wilcoxon DEG with `pct1`/`pct2`, PAGA
  connectivity, and per-cluster QC summaries.
- Self-contained HTML report with embedded plots and `--report-context`.
- Per-cell `qc_removed.csv` ledger for every cell dropped by the QC filter.
- Claude-based annotation agent proposing cell-type labels and QC actions.

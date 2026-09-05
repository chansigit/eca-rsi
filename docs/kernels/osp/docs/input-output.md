# OSP input and output conventions

These conventions describe the Python API and command-line entry points in
this checkout. See the [README](../README.md) for installation and examples.

## Input data

OSP analyzes one sample at a time. Python calls accept an in-memory AnnData
or view and work on copies. If `sample_col` exists in `obs`, every cell must
have a non-missing label and all labels must identify the same sample. If
the column is absent, Python callers are responsible for supplying one
sample. Empty inputs are rejected.

The main CLI requires the sample column, opens the input H5AD in backed
mode, selects the requested sample using string labels, loads the subset
into memory, and closes the input file. This avoids loading the full
expression matrix; the selected sample still needs to fit in memory.

QC and clustering expect raw, non-negative counts. By default, an existing
`layers["counts"]` takes precedence over `X`. If absent, OSP assumes `X`
contains counts; it does not recover counts from normalized expression.
QC rejects non-finite and negative values. An alternative `counts_layer`
can be supplied through both `qc_kwargs` and `cluster_kwargs`.

Clustering requires at least three cells and two genes, at least two
selected HVGs, and at least two clusters at the primary resolution. These
are implementation requirements, not guarantees of a scientifically useful
analysis. If QC retains fewer than three cells, the full pipeline writes
the QC summary and removed-cell ledger before raising an error.

## Python return values

| Function | Return value |
| --- | --- |
| `qc_one_sample` | `(ad_qc, qc_summary)`; all input cells remain, with QC flags |
| `cluster_and_deg` | `(ad_final, de, cluster_summary, paga, decontx_by_cluster)` |
| `run_one_sample_pipeline` | The five clustering values above, followed by `qc_summary` |
| `generate_report` | Path to the generated HTML report |
| `propose_annotation` | Accepted proposal dictionary, including the host-selected `cluster_key` |
| `deg_two_groups` | DataFrame with `gene`, `logfc`, `pct1`, `pct2`, `pval`, `pval_adj`, `high_in`, `low_in` |

`decontx_by_cluster` is `None` when no `decontX_counts` layer is available.
`cluster_and_deg` computes on supplied cells; only the full pipeline
automatically removes cells marked `low_quality` by its QC step.

Python clustering defaults to resolutions `(0.5, 1.0)`, with 1.0 primary.
The main CLI computes only its selected `--resolution` (default 1.0).
The primary resolution supplies the marker, cluster-summary, and PAGA
tables. Primary marker testing uses the full normalized gene matrix;
the separate `deg_two_groups` helper uses HVGs by default.

## Files in a sample output directory

Here, `{key}` is the primary clustering column, for example `leiden_r1.0`.

| Path | Contents / availability |
| --- | --- |
| `clustered.h5ad` | Analyzed cells; the full pipeline includes only QC survivors |
| `qc_summary.csv` | Sample-level QC summary from the full pipeline |
| `qc_removed.csv` | Removed-cell ledger, indexed by `cell`, with sample, reasons, and available QC metrics |
| `cluster_summary_{key}.csv` | Primary cluster sizes and available QC summaries |
| `de_top_genes_{key}.csv` | Top Wilcoxon markers per primary cluster, including `pct1` / `pct2` expression fractions |
| `paga_connectivities_{key}.csv` | Primary cluster-by-cluster connectivity matrix |
| `decontx_top_genes_{key}.csv` | Contaminating-gene rankings when corrected counts are available |
| `qc_figures/` | Sample QC plots, overview JSON, and optional DecontX tables when plotting is enabled |
| `figures/` | Cluster, QC, PAGA, and optional contamination, marker, and annotation plots |
| `report.html` | Self-contained report with embedded images; generated separately in Python, automatically by the main CLI |
| `annotation_proposal.json` | Validated annotation and QC proposals; published after annotation finalization succeeds |
| `annotation_notes.md` | Best-effort agent narrative, written when the backend returns text |

Direct `cluster_and_deg` calls do not create the full pipeline's QC summary
or removed-cell ledger. Setting `make_plots=False` skips that stage's plots.
Keep the default figure directories when generating the standard report;
its image lookup uses `qc_figures/` and `figures/` under the output directory.

## Matrix contents in `clustered.h5ad`

| Location | Meaning after clustering |
| --- | --- |
| `X` | Expression normalized to 10,000 counts per cell, then log1p transformed |
| `layers["counts"]` | Raw counts, preserved before normalization (or the caller's configured layer name) |
| `raw.X` | Full normalized, log1p expression used for primary marker testing; this is not raw counts |
| `layers["decontX_counts"]` | Corrected counts, when DecontX ran upstream; not substituted into clustering input |
| `obs["leiden_r..."]` | Leiden labels for each requested resolution |
| `obsm["X_pca"]`, `obsm["X_umap"]` | PCA and UMAP coordinates |

OSP keeps the full gene matrix; HVG selection is used for PCA input.
By default, available QC covariates are standardized and appended to the
scaled HVG expression. Pass `qc_pca_covariates=None` or `()` to
`cluster_and_deg` to disable this addition. A DecontX fit marked with
`uns["osp_decontx_degenerate"]` retains its values for inspection, but its
contamination column is excluded from PCA covariates.

The historical summary value `decontx_z_source="leiden_fallback"` denotes
OSP's normal explicit coarse-Leiden initialization. It does not mean an
internal initialization was attempted and failed. Caller-supplied `z`
labels are recorded as `decontx_z_source="user"`. The same summary carries
`decontx_degenerate` (`True` / `False`) so that `qc_summary.csv`, the report,
and the annotation agent all see the degenerate flag without opening the
H5AD.

When Scrublet runs, the QC summary also carries `scrublet_threshold`
(`None` if scanpy could not find one, in which case no cell is called a
doublet), the `doublet_score` median, p90 and p99, and
`pct_doublet_score_above_0.25`. Treat the score distribution, not
`n_doublet`, as the primary doublet evidence.

Leiden resolutions are keyed as floats: `resolutions=(1,)` and
`resolutions=(1.0,)` both produce `obs["leiden_r1.0"]` and
`cluster_summary_leiden_r1.0.csv`.

## Annotation proposals

The agent inspects individual plots and tables, checks markers and QC
metrics, and can refine a heterogeneous cluster with the `subcluster` tool.
It submits through `submit_annotation`. OSP validates the submission against
the current cluster IDs before accepting it. The shared harness owns
provider execution, retries, timeouts, and submission control; OSP owns its
biological tools and proposal validation.

The proposal contains:

- `clusters`: one annotation per current cluster, with `cluster`,
  `label_coarse`, `label_fine`, `confidence` (`high`, `medium`, or `low`),
  `evidence_genes`, and `doubts`.
- `qc_actions`: `drop` or `flag` proposals for a whole cluster
  (`scope="cluster"`) or cells within it (`scope="cells"`). Cell-level
  actions specify an existing numeric `obs` metric, an operator (`>`, `>=`,
  `<`, or `<=`), and a finite threshold. Each action includes a `reason`
  and free-text `note`.
- `threshold_suggestions`: free-text suggestions, with no automatic change
  to QC parameters.
- `overall`: sample-level assessment text.
- `cluster_key`: added by the host; may identify a refined clustering such
  as `ann_sub1`. Use this key to interpret the proposal's cluster IDs.

Supported action reasons are `doublet`, `ambient`, `debris`,
`dissociation-stress`, `low-quality`, and `other`.

Annotation maps accepted labels to `obs["_ann_coarse"]` and
`obs["_ann_fine"]`, and actions to `obs["_qc_action"]` (`keep`, `flag`,
or `drop`). Cells with no matching action get `keep`; `drop` takes precedence
over `flag` when both match. These are proposed decisions: annotation does
not remove cells, rerun QC, or recluster after proposed drops.

## Reruns and completion

Use one directory per sample and one active writer per directory. OSP
reuses fixed filenames and does not provide a directory-wide transaction
or concurrent-writer lock.

At the start of `run_one_sample_pipeline`, old `report.html`,
`annotation_proposal.json`, and `annotation_notes.md` are removed. When
replacement clustering is ready to write, OSP also invalidates derived
plots and removes obsolete primary-resolution tables. QC plotting removes
its own generated files for the same sample before replacement. Other
files can remain, including earlier intermediate outputs after a failure.
The report rejects multiple QC overview files or primary cluster summaries
instead of silently selecting one.

For annotation-only reruns, the previous proposal is removed before the
agent starts. Accepted labels and actions are plotted and saved into
`clustered.h5ad`, then the report is refreshed, and only then is
`annotation_proposal.json` published. Older notes or plots may remain after
a failed attempt; they are not completion signals.

Core H5AD, CSV, JSON, and report writes use a sibling temporary file followed
by replacement; the replaced file receives the permissions a plainly created
file would get under the current umask. H5AD writes are reopened in backed
mode and shape-checked before replacement. This protects individual destination files against
partial writes; it does not validate every value or make the entire output
directory atomic.

An outer driver should record successful process completion and the input
and configuration used. For full pipeline-plus-report completion, verify
the expected H5AD and report from that run. For annotation completion, also
require the final proposal. Merely finding an old H5AD, plot, or report is
insufficient to establish success of a new attempt.

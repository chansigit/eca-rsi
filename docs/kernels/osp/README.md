# osp — one-sample-pipeline

Single-sample scRNA-seq QC → clustering/DEG → self-contained HTML report, with
an optional agent step that proposes cell-type annotations and QC
actions from the report and the cluster marker tables.

Strictly single-sample by design — one sample per run, no cross-sample batch
integration. Loop over samples in an outer driver (e.g. a Slurm job array);
treat integration as a separate downstream step.

## Install

Python 3.10 or newer is required.

```bash
pip install osp-sc                     # PyPI name; `import osp` / `python -m osp`
# with the optional annotation agent (Claude, dsh, and OpenAI Agents SDK backends):
pip install "osp-sc[agent]"
```

To use this checkout, including changes not yet released on PyPI:

```bash
pip install -e .
# Include annotation runtimes and development checks when needed:
pip install -e ".[agent,test]"
```

The runtime implementation comes from the independent
`agent-harness-bridge==0.1.0` dependency; `osp.harness` remains a compatibility import.
OSP continues to own its prompts, biological tools and submit validation.

## Annotation backend

The default backend is OpenAI Agents SDK with
`doubao-seed-2-1-turbo-260628`; provide `ARK_API_KEY`. It uses Ark's
Responses API and server-side `previous_response_id` chaining by default.
Set `HARNESS=deepseek` for dsh or `HARNESS=claude` for claude_agent_sdk. Set
`OPENAI_AGENTS_SERVER_STATE=0` when complete local-history replay is required.
OSP annotation needs image tools, so keep `OPENAI_AGENTS_API=responses`;
the bridge's `chat_completions` mode does not support image tool output.
If an image-heavy session reaches Ark's context limit, the backend retains
host-side Tasks and accepted submissions and continues in a fresh model
session (at most `OPENAI_AGENTS_MAX_CONTEXT_RESETS`, default 2).

## Quick usage

Pass one sample as an in-memory AnnData. Supply raw counts in
`layers["counts"]`, or in `X` when that layer is absent. For large inputs,
the CLI below loads only the selected sample's matrix into memory.

```python
from osp import run_one_sample_pipeline, generate_report

ad_fo = adata[adata.obs["sample"] == "FO"]
run_one_sample_pipeline(ad_fo, sample_label="FO", outdir="osp_out/FO")
generate_report("osp_out/FO")
```

To inspect QC before choosing which cells to cluster:

```python
from osp import qc_one_sample, cluster_and_deg, deg_two_groups

ad_qc, qc_summary = qc_one_sample(ad_fo, sample_label="FO")
ad_pass = ad_qc[~ad_qc.obs["low_quality"]].copy()
ad_final, de, clusters, paga, contamination = cluster_and_deg(
    ad_pass, outdir="osp_out/FO"
)
```

- `qc_one_sample` — QC only (flags cells, drops nothing)
- `run_one_sample_pipeline` — runs QC, removes `low_quality` cells, then
  clusters survivors; call `generate_report` separately in Python.
- `cluster_and_deg` — clustering/DEG/PAGA on caller-selected cells; it does
  not apply the `low_quality` filter itself.
- `deg_two_groups` — Wilcoxon DEG between two groups, or one group versus
  the remaining cells. Expects normalized, log1p expression; uses HVGs by
  default (`hvg_only=False` includes all genes).

Pass `qc_kwargs` and `cluster_kwargs` to `run_one_sample_pipeline` for
parameter control. For example, `cluster_kwargs={"qc_pca_covariates": None}`
selects expression-only PCA. See [input and output conventions](docs/input-output.md)
for matrix contents, return values, and output files.

## Command line

```bash
python -m osp data.h5ad --sample FO --outdir osp_out/FO     # full pipeline + report
python -m osp data.h5ad --sample FO --outdir osp_out/FO --annotate \
    --harness openai --model doubao-seed-2-1-turbo-260628
# quality-oriented, higher-cost option; validate it on your workload
python -m osp data.h5ad --sample FO --outdir osp_out/FO --annotate \
    --harness openai --model doubao-seed-2-1-pro-260628
python -m osp.report osp_out/FO                           # rebuild the report only
# Annotate existing outputs without rerunning QC or clustering:
HARNESS=openai python -m osp.annotate osp_out/FO --species mouse --tissue "bone marrow"
# QC plots and printed summary only; this command does not export an H5AD:
python -m osp.qc data.h5ad --sample FO --figdir qc_figs/FO
```

Use `--sample-col` when the sample identifier is not in `obs["sample"]`.
CLI sample selection compares labels as strings, including numeric labels.
Each sample needs its own `--outdir`; the main CLI uses the supplied path
directly and does not append the sample name. `--resolution` selects the
primary Leiden resolution (default 1.0). `--species` and `--tissue` on the
main CLI provide annotation context; they do not change QC gene patterns.

`--harness` overrides `HARNESS`, and `--model` overrides `MODEL`; otherwise
the bridge defaults apply. The standalone annotation CLI selects its backend
through `HARNESS`. Use `--language Chinese` for Chinese annotation text.

See [examples/run_one_sample.py](examples/run_one_sample.py) for the sample
driver (which appends the sample name to its output root), and
[examples/submit_array.sbatch](examples/submit_array.sbatch) for Slurm.
From the repository root, edit the template's paths and sample list, create
`osp_out/logs`, then submit an explicit array range such as
`sbatch --array=0-2 examples/submit_array.sbatch` for three samples.

## Development checks

```bash
pip install -e ".[test]"
ruff check .
pytest -q
```

The tests cover input and proposal validation, action precedence, DecontX
boundary cases, atomic writes, and stale-output handling. They do not
establish biological annotation accuracy or live provider reliability.
If Python/readline fails at startup with an unavailable locale on the
cluster, use `LC_ALL=C LANG=C pytest -q`.

## Conventions

- **Raw counts convention**: if `adata.layers["counts"]` exists, `X` is swapped
  for it at the start of both the QC and clustering stages — this makes the
  pipeline robust to inputs where `X` already holds normalized values with
  raw counts kept in a layer (common in released h5ad files).
- **QC is flag-only**: `qc_one_sample` never drops cells; `low_quality` is a
  column. The full pipeline filters that column before clustering and writes
  the removed-cell ledger to `qc_removed.csv`.
- **Agent actions are proposals**: annotation adds `_ann_coarse`, `_ann_fine`,
  and `_qc_action` to `clustered.h5ad`. Even `drop` leaves the cell in this
  file; downstream filtering requires a separate decision. If both `flag`
  and `drop` match a cell, `drop` takes precedence.
- **PCA includes QC covariates by default**: available doublet score, counts,
  gene count, DecontX contamination, MT percentage, and dissociation score
  are standardized and appended to HVG expression before PCA. This affects
  neighbors and clustering; use `qc_pca_covariates=None` in
  `cluster_and_deg` for expression-only PCA.
- **DecontX initialization and degeneracy guard**: OSP supplies an explicit
  coarse-Leiden clustering to the vendored DecontX implementation. The
  historical compatibility value is
  `summary["decontx_z_source"] == "leiden_fallback"`; it now denotes the normal
  explicit-clustering path, not a failed first attempt. Fits pinned near
  complete contamination are retained for inspection but marked in
  `adata.uns["osp_decontx_degenerate"]` and excluded from PCA covariates.
  DecontX is enabled by default for monitoring; corrected counts do not
  replace raw counts for clustering, and contamination does not directly
  set `low_quality`.
- **MAD-outlier assumption**: the adaptive per-sample QC thresholds (`nmads`
  MADs around the median) assume a roughly regular within-sample
  distribution. On samples with unusually shallow depth or heavy ambient
  contamination this assumption can break — a naturally low-complexity but
  perfectly healthy population (e.g. neutrophils, dominated by a handful of
  granule genes) can get its `pct_counts_in_top_20_genes` MAD range squeezed
  and be flagged en masse. The QC report's "MAD keep-ranges" table exists
  specifically so this is visible instead of silent — a suspiciously tight
  range, or one metric dominating the fail counts, is a signal to check which
  cell types are being flagged before trusting the calls.

## Outputs and reruns

The main outputs are `clustered.h5ad`, QC and cluster CSV tables, individual
PNGs, and `report.html`. Annotation updates the H5AD and report and publishes
`annotation_proposal.json` last, after those writes succeed.

Use one output directory per sample and one active writer per directory.
Rerunning the full pipeline invalidates the old report and annotation
completion files. A failed rerun may leave earlier intermediate files, so
file existence alone does not prove that a new run completed. See
[output files and completion rules](docs/input-output.md#reruns-and-completion)
before implementing resume logic.

## Sherlock / HPC notes

- Never run the pipeline on a login node — submit through Slurm or use an
  interactive allocation.
- For large h5ad files, load in backed mode and subset to one sample before
  bringing it into memory (see `examples/run_one_sample.py`).
- `osp.annotate` is intentionally not imported by `osp/__init__.py` — it
  depends on the optional agent runtime extras; import it explicitly
  (`from osp.annotate import propose_annotation`) only when you need it.

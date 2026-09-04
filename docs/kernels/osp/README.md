# osp — one-sample-pipeline

Single-sample scRNA-seq QC → clustering/DEG → self-contained HTML report, with
an optional agent step that proposes cell-type annotations and QC
actions from the report and the cluster marker tables.

Strictly single-sample by design — one sample per run, no cross-sample batch
integration. Loop over samples in an outer driver (e.g. a Slurm job array);
treat integration as a separate downstream step.

## Install

```bash
pip install osp-sc                     # PyPI name; `import osp` / `python -m osp`
# with the optional annotation agent (Claude, dsh, and OpenAI Agents SDK backends):
pip install "osp-sc[agent]"
```

The runtime implementation comes from the independent
`agent-harness-bridge` package; `osp.harness` remains a compatibility import.
OSP continues to own its prompts, biological tools and submit validation.

The default backend is OpenAI Agents SDK with
`doubao-seed-2-1-turbo-260628`; provide `ARK_API_KEY`. It uses Ark's
Responses API and server-side `previous_response_id` chaining by default.
Set `HARNESS=deepseek` for dsh or `HARNESS=claude` for claude_agent_sdk. Set
`OPENAI_AGENTS_API=chat_completions` only for text-only compatibility, or
`OPENAI_AGENTS_SERVER_STATE=0` when complete local-history replay is required.
If an image-heavy session reaches Ark's context limit, the backend retains
host-side Tasks and accepted submissions and continues in a fresh model
session (at most `OPENAI_AGENTS_MAX_CONTEXT_RESETS`, default 2).

## Quick usage

```python
from osp import run_one_sample_pipeline, generate_report

ad_fo = adata[adata.obs["sample"] == "FO"]
run_one_sample_pipeline(ad_fo, sample_label="FO", outdir="osp_out/FO")
generate_report("osp_out/FO")
```

Stepwise calls, if you want more control:

```python
from osp import qc_one_sample, cluster_and_deg, deg_two_groups
```

- `qc_one_sample` — QC only (flags cells, drops nothing)
- `cluster_and_deg` — clustering/DEG/PAGA on QC-passed data
- `deg_two_groups` — Wilcoxon DEG between any two cell groups, for ad hoc comparisons outside the main pipeline

## Command line

```bash
python -m osp data.h5ad --sample FO --outdir osp_out          # full pipeline + report
python -m osp data.h5ad --sample FO --outdir osp_out --annotate \
    --harness openai --model doubao-seed-2-1-turbo-260628
# quality-oriented, higher-cost option; validate it on your workload
python -m osp data.h5ad --sample FO --outdir osp_out --annotate \
    --harness openai --model doubao-seed-2-1-pro-260628
python -m osp.report osp_out                                   # rebuild the report only
```

See `examples/run_one_sample.py` for a driver that loads a large h5ad in
backed mode and pulls out one sample (for per-sample Slurm array tasks), and
`examples/submit_array.sbatch` for the job-array template.

## Development checks

```bash
pip install -e ".[test]"
ruff check .
pytest -q
```

## Conventions

- **Raw counts convention**: if `adata.layers["counts"]` exists, `X` is swapped
  for it at the start of both the QC and clustering stages — this makes the
  pipeline robust to inputs where `X` already holds normalized values with
  raw counts kept in a layer (common in released h5ad files).
- **QC is flag-only**: `qc_one_sample` never drops cells; `low_quality` is a
  column, filtering is the caller's decision.
- **DecontX initialization and degeneracy guard**: OSP supplies an explicit
  coarse-Leiden clustering to the vendored DecontX implementation. The
  historical compatibility value is
  `summary["decontx_z_source"] == "leiden_fallback"`; it now denotes the normal
  explicit-clustering path, not a failed first attempt. Fits pinned near
  complete contamination are retained for inspection but marked in
  `adata.uns["osp_decontx_degenerate"]` and excluded from PCA covariates.
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

## Sherlock / HPC notes

- Never run the pipeline on a login node — submit through Slurm or use an
  interactive allocation.
- For large h5ad files, load in backed mode and subset to one sample before
  bringing it into memory (see `examples/run_one_sample.py`).
- `osp.annotate` is intentionally not imported by `osp/__init__.py` — it
  depends on the optional agent runtime extras; import it explicitly
  (`from osp.annotate import propose_annotation`) only when you need it.

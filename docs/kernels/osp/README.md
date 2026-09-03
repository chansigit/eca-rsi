# osp — one-sample-pipeline

Single-sample scRNA-seq QC → clustering/DEG → self-contained HTML report, with
an optional Claude-agent step that proposes cell-type annotations and QC
actions from the report and the cluster marker tables.

Strictly single-sample by design — one sample per run, no cross-sample batch
integration. Loop over samples in an outer driver (e.g. a Slurm job array);
treat integration as a separate downstream step.

## Install

```bash
pip install osp-sc                     # PyPI name; `import osp` / `python -m osp`
# with the optional annotation agent (needs claude-agent-sdk + claude CLI credentials):
pip install "osp-sc[agent]"            # + claude-agent-sdk for --annotate
```

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
python -m osp data.h5ad --sample FO --outdir osp_out --annotate --model claude-sonnet-5
python -m osp.report osp_out                                   # rebuild the report only
```

See `examples/run_one_sample.py` for a driver that loads a large h5ad in
backed mode and pulls out one sample (for per-sample Slurm array tasks), and
`examples/submit_array.sbatch` for the job-array template.

## Conventions

- **Raw counts convention**: if `adata.layers["counts"]` exists, `X` is swapped
  for it at the start of both the QC and clustering stages — this makes the
  pipeline robust to inputs where `X` already holds normalized values with
  raw counts kept in a layer (common in released h5ad files).
- **QC is flag-only**: `qc_one_sample` never drops cells; `low_quality` is a
  column, filtering is the caller's decision.
- **DecontX degeneracy guard**: DecontX's own UMAP+DBSCAN init can collapse on
  samples where the dominant cell lineage's transcriptome resembles the
  ambient RNA pool (all contamination pinned near 1, or the init shattering
  into 100+ tiny clusters). When detected, `qc_one_sample` automatically
  re-runs DecontX with an explicit coarse-leiden clustering; check
  `summary["decontx_z_source"]` (`"internal"` vs `"leiden_fallback"`) and the
  Ambient Contamination section of the report.
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
  depends on the optional `claude-agent-sdk`; import it explicitly
  (`from osp.annotate import propose_annotation`) only when you need it.

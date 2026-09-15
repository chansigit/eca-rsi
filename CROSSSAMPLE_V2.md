# Cross-sample v2

Temporal coordinates the operations; Warm Pool workers execute MSP kernels and evidence tools; Agent Bridge handles model turns. The input is a completed per-sample `publication.json`. This path does not start a legacy dataset driver or run `msp inspect` / `msp annotate` as long-lived agents.

The order is input verification → sample inclusion → integration → parallel DEG → evidence publication → type annotation → quality annotation → final publication. Datasets advance independently, and model waits hold no compute grant. Integration keeps normalization, HVG, PCA, Harmony, neighbors, Leiden, UMAP and numerical QC together. Global DEG runs per resolution; local DEG runs per target against its pooled PAGA neighbors. Comparisons map shared frozen expression buffers; one assembly task publishes read-only SQLite.

Quality can request bounded subclustering. The coordinator schedules it on a worker, rebuilds matching DEG, then requests type review for affected clusters and merge partners before restarting quality. Parent labels are context, not child annotations. Failed operations retain their receipts and completed siblings and never become successful publications.

## Start

Use the deployment's recorded source revision and science image. The image must include MSP's `feature/scheduled-operations` changes; released MSP 0.5.2 alone does not provide these interfaces. CPU and GPU use the same pinned scientific dependencies.

```json
{
  "run_id": "example-crosssample", "dataset_id": "Example dataset",
  "input": {"path": "/shared/persample/publication.json", "sha256": "SHA256_OF_FILE"},
  "output_root": "/shared/crosssample/example",
  "pool_root": "/shared/pool", "bridge_root": "/shared/bridge",
  "inspect_budget": {"cpus": 1, "memory_mb": 2048, "timeout_seconds": 180},
  "compute_budget": {"cpus": 4, "memory_mb": 16384, "timeout_seconds": 3600},
  "deg_budget": {"cpus": 1, "memory_mb": 4096, "timeout_seconds": 1800},
  "tool_budget": {"cpus": 1, "memory_mb": 8192, "timeout_seconds": 600},
  "finalize_budget": {"cpus": 2, "memory_mb": 16384, "timeout_seconds": 1800},
  "max_in_flight_deg": 8, "max_refinements": 2,
  "config": {
    "batch_col": "eca_sample_id", "species": "human", "tissue": "prostate",
    "n_top_genes": 3000, "n_pcs": 30, "n_neighbors": 15,
    "compute_backend": "auto", "gpu_min_cells": 5000, "gpu_memory_mb": 8192
  }
}
```

Budgets are examples, not dataset-size estimates. Set them for the input and available workers. The output directory must be fresh. Species and sample key must match Organize's accepted mapping. `auto` prefers GPU above the configured threshold and permits CPU fallback; `rapids` requires GPU; `cpu` uses Scanpy. `max_in_flight_deg` bounds each dataset's dispatched comparisons; worker resources control actual concurrency.

```bash
python -m ecarsi.work_coordinator --temporal HOST:7233 start-crosssample spec.json
python -m ecarsi.work_coordinator --temporal HOST:7233 status-crosssample example-crosssample
```

The coordinator worker registers this alongside Organize and per-sample. Restarting it resumes open Temporal histories. After workflow failure, reconcile failed/unknown Pool or Bridge receipts before `resume-crosssample`; unresolved external API outcomes are not blindly submitted again. Changing input, code or scientific settings requires a fresh run/output. Do not modify worker programs while sessions reference their recorded hashes.

## Outputs and validation

The final `publication.json` references `annotated.h5ad`, type and quality proposals, figures, report, and `cell_exclusions.csv.gz` through its `files` map. Intermediate evidence is an immutable bundle: resolve files through the manifest, since they can reside in different completed Pool attempts.

The exclusion ledger records stable cell IDs, original source IDs, stage, run, evidence version and applicable reasons. Overlapping rules count a cell once. Reasons include sample exclusion, numerical QC, inherited OSP proposals and accepted type/quality removals. The input publication links earlier ledgers; temporary DEG masks are not physical removals. Publication requires serialized output/ledger conservation.

The development observatory consumes the same Pool and Bridge traces, including DEG fan-in dependencies and actual worker placement. Small-data acceptance does not establish large-dataset speedup. Representative memory/throughput tests, global storage budgets, and the subsequent Zoom-in migration remain separate work.

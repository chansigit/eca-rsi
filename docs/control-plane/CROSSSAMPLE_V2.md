# Cross-sample stage

Temporal coordinates the operations. Warm pool workers run the MSP kernels and the evidence tools. The model-turn service runs the model turns. For round 1, the input is a completed per-sample `publication.json`. For round ≥ 2, the input is the zoom-in publication from the previous round.

## Steps

1. `inspect`: Verify the published input.
2. Sample inclusion (round 1, more than one sample): An agent session decides which samples take part. A single sample skips the session (`include-single`).
3. `compute`: Run merge, normalization, HVG, PCA, Harmony, neighbors, Leiden, UMAP, and numerical QC in one task. For round ≥ 2, run `compute-round` on the previous survivors instead.
4. DEG: Compute global DEG per resolution. Compute local DEG per target against its pooled PAGA neighbors. Comparisons map shared frozen expression buffers. Each pool request (`deg-batch`) runs eight comparisons up to 50,000 cells and fewer above (one from 400,000); `max_in_flight_deg` is multiplied by the same factor. The timeout of the request is twice the per-comparison budget.
5. `assemble`: Publish the evidence bundle and a read-only SQLite DEG database.
6. Type annotation: An agent session labels the clusters.
7. Quality review: An agent session decides removals, merges, and subclustering.
8. `refine` (optional): The quality review can request subclustering. The coordinator recomputes the affected clusters and their DEG. The coordinator asks type review for the affected clusters and merge partners. The coordinator then restarts quality. Repeat this process up to `max_refinements` times.
9. `finalize`: Apply the decisions, plot, write the report, and publish.

Datasets advance independently. A model wait holds no compute grant. Failed operations keep their receipts and completed siblings. They never become publications.

## Spec

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
  "max_in_flight_deg": 16, "max_refinements": 2,
  "config": {
    "batch_col": "eca_sample_id", "species": "human", "tissue": "prostate",
    "n_top_genes": 3000, "n_pcs": 30, "n_neighbors": 15,
    "compute_backend": "auto", "gpu_min_cells": 5000, "gpu_memory_mb": 8192
  }
}
```

Budgets are examples. Size them for the input and the workers. The output directory must be fresh. Species and sample key must match the accepted Organize mapping. For `compute_backend`, `auto` prefers a GPU above `gpu_min_cells` and allows CPU fallback. The setting `rapids` requires a GPU. The setting `cpu` uses Scanpy. `max_in_flight_deg` bounds the dispatched DEG requests of the dataset. Worker resources decide real concurrency.

`config.stress_policy` (remove or keep) is set only when the dataset spec sets it ([decision 0017](../decisions/0017-stress-population-policy.md)). The type phase checks each stress removal of 10 or more cells against `stress_clusters.csv` and keeps the unsupported ones under the agent's labels; finalize marks retained cells in obs `retained_state`.

CPU DEG requests with the mapped-buffer layout use a measured memory estimate. Calculate this estimate as four times the expression and metadata file bytes plus 2 GiB. Round the estimate up to 256 MiB. Cap the estimate at `deg_budget.memory_mb`. Unknown layouts and GPU requests keep their declared budgets. Existing requests never change.

Change the DEG window of a running workflow without restarting it:

```bash
python -m ecarsi.control --service-root <control> set-deg-limit cross-sample RUN_ID 16
```

Lowering the window lets dispatched work finish before refilling. Sparse global DEG requires a private normalized-expression workspace. Scanpy edits sparse storage in place. Include this workspace in the memory budget.

## Commands

Inside a dataset workflow, the stage starts by itself. Run the stage standalone:

```bash
python -m ecarsi.control --service-root <control> --task-queue <queue> start-crosssample spec.json
python -m ecarsi.control --service-root <control> --task-queue <queue> status-crosssample RUN_ID
```

Restart the coordinator to resume open histories. The system retries confirmed local interruptions automatically, up to three times. Other failures need a recorded repair before you run `resume-dataset` for the dataset. The system never resubmits unresolved external outcomes blindly. The system verifies and reuses completed agent submissions on resume.

For a confirmed failed computation, keep its history and retry through the pool:

```bash
python -m ecarsi.warm_pool --root <pool> retry REQUEST_ID --reason "Verified repair"   # --use-current-runtime only after an intended runtime upgrade
python -m ecarsi.control --service-root <control> resume-dataset DATASET_RUN_ID --reason "Verified repair"
```

The retry command refuses changed inputs, completed or cancelled tasks, and unknown outcomes. Changing a pinned program or a scientific input requires a fresh run. Do not modify programs while active sessions reference their hashes.

## Outputs

The final `publication.json` references `annotated.h5ad`, the type and quality proposals, figures, the report, and `cell_exclusions.csv.gz` through its `files` map. The evidence bundle is immutable. Files can live in different pool attempts. Resolve its files through the manifest.

The exclusion ledger records stable cell ids, original source ids, stage, run, evidence version, and reasons. Overlapping rules count a cell once. Reasons include sample exclusion, numerical QC, inherited OSP proposals, and accepted type or quality removals. Temporary DEG masks are not removals. Publication requires serialized output and ledger conservation.

Periscope's `/_control/` page shows the same pool and bridge traces. These traces include DEG fan-in and worker placement.

## History

The 2026-09-15 acceptance is in [docs/history/CROSSSAMPLE_V2_ACCEPTANCE.md](../history/CROSSSAMPLE_V2_ACCEPTANCE.md).

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
  "max_in_flight_deg": 16, "max_refinements": 2,
  "config": {
    "batch_col": "eca_sample_id", "species": "human", "tissue": "prostate",
    "n_top_genes": 3000, "n_pcs": 30, "n_neighbors": 15,
    "compute_backend": "auto", "gpu_min_cells": 5000, "gpu_memory_mb": 8192
  }
}
```

Budgets are examples, not dataset-size estimates. Set them for the input and available workers. The output directory must be fresh. Species and sample key must match Organize's accepted mapping. `auto` prefers GPU above the configured threshold and permits CPU fallback; `rapids` requires GPU; `cpu` uses Scanpy. `max_in_flight_deg` bounds each dataset's dispatched comparisons; worker resources control actual concurrency.

CPU DEG requests with the recognized mapped-buffer layout use a persisted memory
estimate: four times the expression/metadata file bytes plus 2 GiB, rounded up to
256 MiB and capped by `deg_budget.memory_mb`. This allows private sparse copies,
rank workspaces and imports without reserving counts/graph layers that DEG never
loads. Unknown layouts, unaccepted inputs and GPU requests retain their declared
budgets. Existing requests never change. A replay over 1,262 completed comparisons
from 20 development datasets retained at least 2.17 times their observed RSS peak;
this is calibration evidence, not a guarantee for every future input/runtime.

The dispatch window can also be updated without restarting a workflow:

```bash
python -m ecarsi.control --service-root /absolute/control \
  set-deg-limit cross-sample RUN_ID 16
```

The acknowledged update is durable in Temporal history. Lowering the window lets
already dispatched work finish before refilling; it does not cancel comparisons.
The configured `max_in_flight_deg` remains the initial value for each new workflow.
The reusable dataset example now uses 16. Pool grants still decide how many of
these submitted requests actually execute together.

```bash
python -m ecarsi.control --temporal HOST:7233 start-crosssample spec.json
python -m ecarsi.control --temporal HOST:7233 status-crosssample example-crosssample
```

The coordinator worker registers this alongside Organize and per-sample. Restarting it resumes open Temporal histories. Confirmed local interruptions retry automatically, up to three attempts. Other failures require a recorded repair before `resume-crosssample`; unresolved external API outcomes are not blindly submitted again. Completed agent submissions are verified and reused on resume. An unfinished session with an incompatible adapter revision requires a new session/run.

For a known failed computation, preserve its history and retry through the Pool API:

```bash
python -m ecarsi.warm_pool --root /shared/pool retry REQUEST_ID --reason "Verified repair"
# Add --use-current-runtime only after validating an intentional runtime upgrade.
python -m ecarsi.control --temporal HOST:7233 resume-crosssample example-crosssample
```

Retry refuses changed inputs, completed/cancelled tasks and unknown outcomes. Runtime upgrades are explicit and retained in the attempt history. Changing a pinned worker program or scientific input requires a fresh run/output. Do not modify programs while active sessions reference their hashes. Sparse global DEG needs a private normalized-expression workspace because Scanpy edits sparse storage in place; include that workspace in its memory budget. Shared evidence files remain unchanged.

## Outputs and validation

The final `publication.json` references `annotated.h5ad`, type and quality proposals, figures, report, and `cell_exclusions.csv.gz` through its `files` map. Intermediate evidence is an immutable bundle: resolve files through the manifest, since they can reside in different completed Pool attempts.

The exclusion ledger records stable cell IDs, original source IDs, stage, run, evidence version and applicable reasons. Overlapping rules count a cell once. Reasons include sample exclusion, numerical QC, inherited OSP proposals and accepted type/quality removals. The input publication links earlier ledgers; temporary DEG masks are not physical removals. Publication requires serialized output/ledger conservation.

The development observatory consumes the same Pool and Bridge traces, including DEG fan-in dependencies and actual worker placement. Small-data acceptance does not establish large-dataset speedup. Representative memory/throughput tests, global storage budgets, and the subsequent Zoom-in migration remain separate work.

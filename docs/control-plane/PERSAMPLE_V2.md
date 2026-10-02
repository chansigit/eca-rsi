# Per-sample stage

One Temporal workflow consumes an accepted Organize analysis unit. It uses the published experiment mapping; no agent identifies the samples again. Each sample has its own child workflow.

| Step | Runs on | Work |
| --- | --- | --- |
| `persample.partition` | pool worker | Verify the Organize identities and prepare a bounded batch of sample inputs. |
| `osp.compute` | pool worker | Run OSP QC, Scrublet, DecontX, clustering, UMAP and DEG for one sample. |
| `osp.annotate` | model-turn service and worker tools | Read figures and tables, verify markers and QC, optionally refine a cluster, submit a validated annotation. |
| `osp.finalize` | pool worker | Apply the accepted labels and QC proposals, render the report, verify cell conservation. |

Samples and datasets overlap. A model wait consumes no pool grant; a tool consumes no model slot. All matrix operations run on workers, including agent-requested subclustering. Figures reach the model as images. Subclustering saves a new immutable matrix version; proposals for an older version are rejected. The model must read the required evidence and check markers and QC before its submission is accepted.

## Spec

The dataset spec carries this block under `per_sample`. Standalone:

```json
{
  "run_id": "dataset-a-persample-1",
  "dataset_id": "Dataset A",
  "unit": "/runs/organize-a/units/analysis-unit",
  "output_root": "/runs/persample-a",
  "pool_root": "/services/pool",
  "bridge_root": "/services/bridge",
  "partition_budget": {"cpus": 1, "memory_mb": 4096, "timeout_seconds": 600},
  "compute_budget": {"cpus": 1, "memory_mb": 8192, "timeout_seconds": 1800},
  "tool_budget": {"cpus": 1, "memory_mb": 8192, "timeout_seconds": 600},
  "finalize_budget": {"cpus": 1, "memory_mb": 8192, "timeout_seconds": 600},
  "batch_size": 2,
  "max_in_flight_samples": 4,
  "max_batch_bytes": 1073741824,
  "config": {"scrublet": true, "decontx": true, "resolution": 1.0, "tissue": "Prostate"}
}
```

The output root must be fresh. `batch_size` limits preparation per grant. `max_in_flight_samples` bounds sample computation; an accepted compute receipt releases the slot before annotation starts. `max_prepared_samples` bounds all unfinished sample children, including model waits (default `max(32, 4 * max_in_flight_samples)`). `max_batch_bytes` bounds each prepared batch on disk. Partition loads the organized matrix once per batch; its memory budget must cover that matrix. Start `compute_budget.memory_mb` at 16384 or more for large samples; the 8192 default has failed on PanSci samples.

For marker, QC and annotation checks and for finalization, an accepted compute peak can shrink an oversized reservation: twice the measured peak plus 1 GiB, rounded up to 256 MiB, with a 2 GiB floor and never above the declaration. Re-clustering keeps its original budget. RSS sampling can miss spikes; there is no automatic escalation after an OOM.

### CPU and GPU

Add to `config`:

```json
"compute_backend": "auto",
"gpu_min_cells": 10000,
"gpu_memory_mb": 8192
```

`auto` makes eligible samples prefer a GPU and allows CPU when no GPU is free. `rapids` requires a GPU. `cpu`, and older specs without the field, stay on CPU. RAM, CPUs and time still come from `compute_budget`. RAPIDS runs PCA, neighbors and UMAP; QC, HVG, Leiden, DEG and reporting stay on CPU. GPU and CPU clusters need not be identical.

## Sessions

A sample whose session fails restarts once with a fresh session (`-r2`). A second failure skips the sample: its labels become `unannotated` and needs_review records `agent_skipped`. The stage fails when skipped samples hold more than 10 % of the input cells.

## Outputs

`publication.json` holds verified references to each sample bundle with input, retained and removed cell counts. Bundle paths point at accepted pool attempt outputs on shared storage. Each QC removal is in `cell_exclusions.csv.gz` with the original source cell id, sample, reason, run and input version. Annotation QC actions stay proposals; cross-sample applies them.

## Recovery

Stable request ids and receipts let a coordinator restart without repeating computation or model turns. A failed sample does not cancel its siblings. When they finish, the parent publishes an `incomplete` record and fails visibly. Confirmed local interruptions retry up to two times. Scientific errors and memory-limit failures stay visible.

Inside a dataset workflow, use `resume-dataset`. Standalone:

```bash
python -m ecarsi.control --service-root <control> --task-queue <queue> start-persample spec.json
python -m ecarsi.control --service-root <control> --task-queue <queue> status-persample RUN_ID
python -m ecarsi.control --service-root <control> --task-queue <queue> resume-persample RUN_ID
```

`resume-persample` verifies the saved spec and input identity, refuses unresolved failed or unknown requests, and follows the original request ids. Completed samples and saved turns are reused. A prior incomplete publication is kept by content hash. While siblings continue, the parent checks failed samples for repaired receipts every 30 seconds and replays a repaired sample once, within the in-flight limit.

## Adjust admission during a run

```bash
python -m ecarsi.control --service-root <control> set-persample-limit PER_SAMPLE_RUN_ID 16
```

The value must be at least `batch_size`. Increasing it wakes admission; decreasing it drains children without cancelling them. The update survives restarts and is recorded in Temporal history. `python tests/check_persample_capacity.py HOST:PORT` exercises this on an isolated task queue without model calls.

## History

Acceptance records of September 2026 are in [docs/history/PERSAMPLE_ACCEPTANCE_20260914.md](../history/PERSAMPLE_ACCEPTANCE_20260914.md).

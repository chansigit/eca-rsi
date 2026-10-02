# Per-sample stage

One Temporal workflow consumes an accepted Organize analysis unit. It uses the published experiment mapping. No agent identifies the samples again. Each sample has its own child workflow.

| Step | Runs on | Work |
| --- | --- | --- |
| `persample.partition` | pool worker | Verify the Organize identities. Prepare a bounded batch of sample inputs. |
| `osp.compute` | pool worker | Run OSP QC, Scrublet, DecontX, clustering, UMAP and DEG for one sample. |
| `osp.annotate` | model-turn service and worker tools | Read figures and tables. Verify markers and QC. Optionally refine a cluster. Submit a validated annotation. |
| `osp.finalize` | pool worker | Apply the accepted labels and QC proposals. Render the report. Verify cell conservation. |

Samples and datasets overlap. A model wait consumes no pool grant. A tool consumes no model slot. Workers perform all matrix operations, including agent-requested subclustering. Figures reach the model as images. Subclustering saves a new immutable matrix version. Reject proposals for an older version. Submission acceptance requires the model to read the required evidence. It also requires the model to check markers and QC.
## Spec

The dataset spec contains this block under `per_sample`. Standalone:

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

Use a fresh output root. `batch_size` limits preparation per grant. `max_in_flight_samples` limits sample computation. An accepted compute receipt releases the slot before annotation starts.

`max_prepared_samples` limits all unfinished sample children, including model waits. Its default is `max(32, 4 * max_in_flight_samples)`. `max_batch_bytes` limits the size of each prepared batch on disk.

Partition loads the organized matrix once per batch. Set its memory budget to cover that matrix. Start `compute_budget.memory_mb` at 16384 or more for large samples. The 8192 default has failed on PanSci samples.

An accepted compute peak can reduce oversized reservations for marker, QC, and annotation checks. It can also reduce oversized reservations for finalization. The calculation adds 1 GiB to twice the measured peak. It then rounds the result up to 256 MiB. A 2 GiB floor applies. The reservation never exceeds the declaration.

Re-clustering keeps its original budget. RSS sampling can miss spikes. No automatic escalation occurs after an OOM.

### CPU and GPU

Add to `config`:

```json
"compute_backend": "auto",
"gpu_min_cells": 10000,
"gpu_memory_mb": 8192
```

`auto` makes eligible samples prefer a GPU. It allows CPU use when no GPU is free. `rapids` requires a GPU. `cpu` keeps computation on CPU. Older specs without this field also keep computation on CPU.

RAM, CPUs, and time still come from `compute_budget`. RAPIDS runs PCA, neighbors, and UMAP. QC, HVG, Leiden, DEG, and reporting stay on CPU. GPU and CPU clusters can differ.
## Sessions

If a sample's session fails, the sample restarts once with a fresh session (`-r2`). A second failure skips the sample. The skipped sample's labels become `unannotated`. needs_review records `agent_skipped` for the skipped sample. The stage fails if skipped samples contain more than 10 % of the input cells.
## Outputs

`publication.json` contains verified references to each sample bundle, with input, retained, and removed cell counts. Bundle paths point to accepted pool attempt outputs on shared storage. `cell_exclusions.csv.gz` records each QC removal with the original source cell id, sample, reason, run, and input version. Annotation QC actions remain proposals. Cross-sample applies these actions.
## Recovery

Stable request ids and receipts let a coordinator restart without repeating computation or model turns. A failed sample does not cancel its siblings. After the siblings finish, the parent publishes an `incomplete` record. The parent then fails visibly. Confirmed local interruptions trigger up to two retries. Scientific errors and memory-limit failures stay visible.

Inside a dataset workflow, use `resume-dataset`. For standalone use, run:

```bash
python -m ecarsi.control --service-root <control> --task-queue <queue> start-persample spec.json
python -m ecarsi.control --service-root <control> --task-queue <queue> status-persample RUN_ID
python -m ecarsi.control --service-root <control> --task-queue <queue> resume-persample RUN_ID
```

`resume-persample` verifies the saved spec and input identity. It refuses unresolved failed or unknown requests. It follows the original request ids. It reuses completed samples and saved turns. It keeps a prior incomplete publication by content hash. While siblings continue, the parent checks failed samples for repaired receipts every 30 seconds. The parent replays each repaired sample once, within the in-flight limit.
## Adjust admission during a run

```bash
python -m ecarsi.control --service-root <control> set-persample-limit PER_SAMPLE_RUN_ID 16
```

Set the value to at least `batch_size`. Increasing the value wakes admission. Decreasing the value drains children without cancelling them.

The update survives restarts. Temporal history records the update.

`python tests/check_persample_capacity.py HOST:PORT` tests this behavior on an isolated task queue. The test makes no model calls.
## History

The September 2026 acceptance records are in [docs/history/PERSAMPLE_ACCEPTANCE_20260914.md](../history/PERSAMPLE_ACCEPTANCE_20260914.md).

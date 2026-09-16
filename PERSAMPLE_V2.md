# Per-sample v2

One Temporal workflow consumes an accepted Organize analysis unit. It uses the
published experiment mapping without asking another agent to identify samples.
Each sample has its own child workflow:

| Step | Runs on | Work |
| --- | --- | --- |
| `persample.partition` | Warm Pool Worker | Verify Organize identities and prepare a bounded batch of sample inputs. |
| `osp.compute` | Warm Pool Worker | Run existing OSP QC, Scrublet, DecontX, clustering, UMAP and DEG together for one sample. |
| `osp.annotate` | Agent Bridge and worker tools | Read figures/tables, verify markers and QC, optionally refine a cluster, submit a validated annotation. |
| `osp.finalize` | Warm Pool Worker | Apply the accepted labels and QC proposals, render the report and verify cell conservation. |

Samples and datasets can overlap. A model wait consumes no Pool grant; an
annotation tool consumes no Bridge model slot. All matrix operations, including
agent-requested subclustering, run on workers. Figures return as actual image
inputs to the model. Subclustering saves a new immutable matrix version; proposals
for an older version are rejected. The model must read the required evidence and
check current markers and QC before its submission is accepted.

## Run

Use the same Coordinator service as Organize, with the v2 code and a Pool runtime
that can import OSP and its scientific dependencies. The registered model must
support tool calls and image inputs.

```bash
python -m ecarsi.work_coordinator --temporal HOST:7233 worker
python -m ecarsi.work_coordinator --temporal HOST:7233 start-persample spec.json
python -m ecarsi.work_coordinator --temporal HOST:7233 status-persample RUN_ID
python -m ecarsi.work_coordinator --temporal HOST:7233 resume-persample RUN_ID
```

Example explicit spec; size the budgets for the actual data and workers:

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

The output root must be fresh. `batch_size` limits preparation per grant.
For new workflows, `max_in_flight_samples` bounds sample computation; an accepted
compute receipt releases that slot before annotation starts. Optional
`max_prepared_samples` bounds all unfinished sample children, including model
waits (default `max(32, 4 * max_in_flight_samples)`, at least `batch_size`).
`max_batch_bytes` bounds each prepared batch on disk. These are per-workflow
bounds, not a global storage quota or global annotation-session limit. A Temporal
patch marker preserves the earlier whole-sample admission behavior when replaying
older histories. Partition still loads the organized matrix once per batch, so
its memory budget must cover that matrix.

Evidence reads request at most 256 MiB and never load the expression matrix.
Table reads combine up to four complete 60,000-character pages, preserving exact
text, page coverage and an explicit next offset for larger evidence. A rejected
annotation lists missing pages/checks; scientific validation remains unchanged.
Execution plans are fixed before submission and reuse their exact request on
activity retries. Existing requests and pinned scientific programs are retained.

For initial-clustering marker/QC/annotation checks and sample finalization, an
accepted upstream compute peak can reduce an oversized memory reservation:
twice measured peak plus 1 GiB, rounded up to 256 MiB, with a 2-GiB floor and no
increase beyond the original declaration. The receipt and resulting plan are
pinned. Re-clustering retains its original budget. This is conservative empirical
calibration, not a guaranteed bound: RSS sampling can miss spikes, and automatic
budget escalation after an OOM is not implemented.

### CPU and GPU execution

The workflow is new; the numerical implementation is shared with OSP. One
sample's computation remains one Pool task, while samples run independently.
With the RAPIDS-enabled OSP package and a matching scientific image, add:

```json
"compute_backend": "auto",
"gpu_min_cells": 10000,
"gpu_memory_mb": 8192
```

These fields belong inside `config`. The threshold and VRAM budget above are
examples to tune for the data. `auto` makes eligible samples prefer a GPU and
allows CPU execution when GPU capacity is unavailable; smaller samples use CPU.
`rapids` requires a GPU, regardless of the threshold. `cpu`, including older
specs with no backend field, keeps CPU execution. RAM, CPUs and time still come
from `compute_budget`; a GPU does not remove those requirements.

RAPIDS runs PCA, neighbors and UMAP. QC, HVG selection, Leiden, DEG and reporting
remain shared CPU code. Annotation and its tools have separate resource grants.
GPU and CPU clusters need not be identical; a fresh run annotates its own result
and never borrows labels from the other backend. Cross-sample will have its own
operation boundaries rather than invoking the old MSP workflow as a single task.

`publication.json` contains verified references to each final sample bundle,
with input, retained and removed cell counts. Bundle paths refer to accepted
Pool attempt outputs on durable shared storage; they are not a copy in the old
driver's directory layout. Keep those artifacts while their publication is in
use. Each actual QC removal is recorded in `cell_exclusions.csv.gz`, including
original source cell ID, sample, reason, run and input version. Annotation QC
actions remain proposals; they do not silently remove additional cells.

## Recovery and remaining work

Stable request IDs and receipts allow Coordinator process replacement without
repeating accepted computation or model turns. A failed sample does not cancel
independent siblings. Once they finish, the parent publishes an `incomplete`
record and fails visibly. Confirmed local interruptions can be retried up to two
times by the shared Pool checker; scientific errors and memory-limit failures
remain visible. Unknown external outcomes are never treated as proof that a task
stopped.

After the underlying failure is resolved, `resume-persample` starts a new
Temporal run under the same logical Workflow ID. It verifies the saved spec
and input identity, refuses unresolved failed/unknown Pool or Bridge requests,
and follows the original stable request IDs. Completed samples and saved model
turns are reused; only unfinished work proceeds. A prior incomplete publication
is preserved by content hash before `publication.json` advances. Completed
publications cannot be replaced with different results. Original failed Temporal
history remains available. Process restart of a still-running workflow requires
no resume command.

While independent siblings continue, the parent checks failed samples for
repaired receipts at most every 30 seconds. If that sample's Pool tasks have all succeeded and its Bridge calls
are now saved or actively queued/running, a replacement child follows the same
stable request IDs, within the same in-flight limit and at most once per sample.
This can consume repaired results without repeating accepted science. Any remaining failed, cancelled or unknown request blocks that replay;
it does not authorize a new compute attempt or an uncertain provider retry.

CPU Scanpy and GPU RAPIDS are connected. `DATASET_V2.md` describes the integrated
Organize, per-sample, cross-sample and Zoom-in workflow. Global storage admission
and production Temporal database failover remain separate work. The development
integration does not resume production datasets.

## GPU workflow acceptance, 2026-09-15

`prostate-gpu-20260915-0119` reused an accepted Organize input and wrote a fresh
per-sample output root. All three samples completed: 625 input cells, 507 retained
and 118 actual exclusions with reasons and source IDs. One compute task used
RAPIDS on `sh03-15n05`; two used CPU on `sh04-14n18`, with peak compute concurrency
of three. The trial explicitly set `gpu_min_cells=1` to exercise GPU placement;
this is not a recommended size threshold.

All 31 model turns were saved. Worker tools handled annotation evidence checks
and agent-requested subclustering before final publication. Model waits held no
Pool resources. Final H5AD identities, annotation columns, sample coverage and
cell-exclusion conservation were verified. The three-node pool uses the same
CPU/GPU scientific image; this small batch does not measure large-dataset
throughput or GPU speedup. Cross-sample remains unimplemented in v2.

The report is `workflow-acceptance.json` and the final publication is
`persample-prostate/publication.json`, under
`/scratch/users/chensj16/eca-runs/warmpool-v2-development/gpu-integration-20260915-004458`.

## Real-data acceptance, 2026-09-14

| Dataset | Samples | Input cells | After QC | Recorded QC removals |
| --- | ---: | ---: | ---: | ---: |
| Tabula Sapiens SS2 Prostate | 3 | 625 | 507 | 118 |
| Tabula Sapiens SS2 Uterus | 4 | 666 | 441 | 225 |

All seven samples completed real OSP and model annotation. Independent backed
H5AD reads and exclusion-table checks confirmed exact cell conservation and
source IDs. The 21 additional cells proposed for removal by annotation remain
in these outputs; those proposals are not counted as actual exclusions.

Three workers on `sh03-13n22`, `sh03-15n05` and `sh04-14n18` each had an explicit
2-CPU/16-GiB test budget. Peak sample-compute concurrency was six; the seven
whole-sample computations took 39–71 seconds each. Recorded compute intervals
overlap model waiting in other datasets. This is a small integration test,
not a large-dataset throughput benchmark or use of each node's whole allocation.

There were 81 saved model turns. The initial two-turn concurrency limit was
raised to four during the test. An SDK checkpoint-restoration defect was
reproduced without a provider call, fixed at the shared Bridge restore boundary,
and the original request was resumed with an audit record. Uterus's failed
parent then used `resume-persample`; the original seven compute attempts and
already-saved model turns were reused. Completed siblings were retained.
Coordinator process interruption also left a worker able to finish its receipt.
All 25 Temporal histories, including original failure and resumed runs, replayed.

After acceptance, figure reads were changed from fixed four-image pages to
up to 16 images within a 9-MiB raw-image budget. A container check returned all
ten real figures in one 970,530-byte JSON result. This reduces unnecessary
model round trips; the full workflow above used the earlier page size.
Read-only evidence calls no longer import the numerical stack. Operation start,
completion and existing OSP log messages go to the task log; Python output is
unbuffered.

Specs, launch commands, logs, source references, independent verification,
the recovery audit, histories and an online development-SQLite backup are under
`/scratch/users/chensj16/eca-runs/warmpool-v2-development/persample-migration-20260914-223044/`.
The two output roots are `persample-v2-prostate-20260914-2253` and
`persample-v2-uterus-20260914-2253` alongside that directory.

The current two-second workflow polling grows Temporal history during long
model waits; cold replay of roughly 10,000 events produced 5–8 second warnings.
That waiting mechanism needs improvement before sustained large batches.
Node/database-loss recovery and automatic failed-compute attempts remain
unvalidated. Passing the focused tests and this batch does not establish
unattended production readiness.

## Adjust admission during a run

The in-flight limit bounds prepared samples, including those awaiting annotation.
It does not reserve CPU or memory for the whole sample workflow: Pool grants each
compute/tool/finalize operation separately. A small limit can still exhaust the
prepared backlog while agents are waiting and leave later samples uncomputed.

A running per-sample workflow accepts a durable Temporal update:

```bash
python -m ecarsi.work_coordinator --service-root /shared/rsi/control \
  set-persample-limit PER_SAMPLE_RUN_ID 16
```

The positive integer must be at least `batch_size`. Increasing it immediately
wakes admission; decreasing it drains existing children without cancelling them.
The update survives Coordinator restart and is recorded in Temporal history.
Original inputs, scientific settings, resource grants and accepted results remain
immutable. It applies to this running per-sample workflow; configure
`max_in_flight_samples` in the dataset specification for future runs.

`python tests/check_persample_capacity.py HOST:PORT` exercises this behavior on an
isolated task queue, including invalid update rejection, admission while existing
children wait, worker replacement, draining and deterministic replay. It makes no
model calls and submits no scientific Pool tasks.

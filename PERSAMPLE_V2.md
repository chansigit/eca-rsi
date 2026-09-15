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

The output root must be fresh. `batch_size` limits preparation per grant;
`max_in_flight_samples` includes samples waiting on models; `max_batch_bytes`
limits each prepared batch on disk. These are per-workflow limits, not a global
storage quota. Partition currently loads the organized matrix once per batch,
so its memory budget must cover that matrix.

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
record and fails visibly. Automatic retry of failed Pool attempts is not
implemented. Unknown external outcomes are never
treated as proof that a task stopped.

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

CPU Scanpy is connected. GPU execution, automatic Organize-to-all-units chaining,
global storage admission, production Temporal database failover, and the new
cross-sample/Zoom-in workflows remain separate work. The development integration
does not resume production datasets.

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

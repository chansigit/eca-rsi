# Organize v2 integration

The development branch connects one Temporal Workflow per dataset to three
business steps: `organize.prepare` in Warm Pool, `organize.plan` alternating
between Agent Bridge model turns and Warm Pool tools, and `organize.execute`
in Warm Pool. Temporal stores only references
and state transitions. Pool and Bridge store their own accepted requests and
results. Model waiting never occupies a Pool grant. No Slurm allocation is
requested or released by these services.

`organize.prepare` discovers accepted ECA-PP products, validates source files,
and saves compact metadata profiles. `organize.plan` uses the configured model
through the durable Agents SDK session. The worker tools `inspect_source` and
`submit_plan` provide metadata and validate a source-scoped experiment mapping.
Both tools use the explicit prepare CPU, memory and timeout budget. The model
has no local filesystem/code tools. Rejected proposals return concrete errors
for correction within the 30-turn session budget. A successful submit tool ends
planning without an extra model acknowledgement. `organize.execute` rechecks source identities, verifies cell conservation,
refuses to split one complete experiment across analysis units, writes organized
H5AD files and per-cell sample maps, then records output identities. The
Coordinator verifies the receipt, H5ADs, manifests, upstream snapshots and
mapping before atomically publishing to a fresh output root. Per-sample reads
the confirmed map and does not identify the sample column again. It also skips
its optional batch-key model suggestion for this already confirmed path.

The first real model answer for Prostate proposed merging all three libraries
because they share a donor. We rejected that conclusion and added a validation
rule: explicit multi-valued sample/library IDs cannot be called a single
experiment. A subsequent model request chose `sample_id`, making three groups.
This rule addresses a demonstrated scientific failure, not just a formatting
error. The same rule still requires review for other metadata conventions.

## Reproduce on an isolated development setup

Install `ecarsi[coordinator]` or `temporalio==1.32.0` in an isolated environment.
Start a Temporal Service, the separate Agent Bridge, and Warm Pool Scheduler /
Worker against explicit development roots. For local SDK tests,
`WorkflowEnvironment.start_local()` can start a development server; its
in-memory state and SQLite development option do **not** establish production
service recovery. The coordinator takes the service address explicitly:

```bash
python -m ecarsi.control --temporal 127.0.0.1:7233 worker
python -m ecarsi.control --temporal 127.0.0.1:7233 start workflow-spec.json
python -m ecarsi.control --temporal 127.0.0.1:7233 status RUN_ID
```

The spec has an explicit `run_id`, `input_root`, fresh `output_root`, `pool_root`,
`bridge_root`, and positive CPU, memory-MiB and timeout budgets for prepare and
execute. One accepted Workflow ID cannot be reused for a different run. The
Pool runtime must include the scientific Python packages and the checked-out
code. The development test bound an existing Python package tree into the slim
Apptainer image; this is an integration expedient, **not** a pinned scientific
image. Neither the old Pool nor production dataset registrations were changed.

## Real-data evidence, 2026-09-14

Both runs used existing `standardized.h5ad` and ECA-PP `result.json`, real
model requests through Agent Bridge, the isolated one-CPU Warm Pool Worker,
and a Temporal development service. Output roots and request/receipt files are
under `/scratch/users/chensj16/eca-runs/warmpool-v2-development/`.

| Workflow | Real source | Result | Experiment mapping |
| --- | --- | --- | --- |
| `organize/prostate-20260914-d` | Tabula Sapiens SS2 Prostate | 625/625 cells assigned, output published | `sample_id`, 3 complete groups |
| `organize/uterus-20260914-a` | Tabula Sapiens SS2 Uterus | 666/666 cells assigned, output published | `sample_id`, 4 complete groups |

Prostate was waiting on its model when Uterus's Pool preparation ran from
epoch 1789420226.8 to 1789420233.9. The Prostate Bridge request started at
1789420212.6 and was still running after that preparation completed. This is
measured overlap of model waiting with useful computation on a different
dataset, not a claim about large-scale throughput.

For `organize/prostate-20260914-recovery`, the Coordinator Python Worker was
killed while the Bridge request was running. Bridge saved the real model reply
at epoch 1789420398.0 with the Coordinator absent. After starting a replacement
Coordinator Worker, Temporal continued to the Pool execution and published the
625-cell result. Its Bridge log recorded one model request. This tests
Coordinator **process** recovery while the Temporal Service remained alive.

The Prostate result is at
`/scratch/users/chensj16/eca-runs/warmpool-v2-development/organize-v2-prostate-20260914/temporal-output/`;
Uterus is at the parallel `organize-v2-uterus-20260914/temporal-output/` path.
Both contain `organize/manifest.json`, `publication.json`, and per-unit
`input/sample_mapping.csv.gz`. The existing per-sample runner's `--plan-only`
accepted both maps and produced 3 and 4 sample entries respectively; no OSP
compute has run in this milestone.

## Remaining gates

- Temporal Service and supported database stopped and restored on another
  allocated host, with durable persistence and stale-writer exclusion. The
  development server used above is not this test.
- GPU scientific image and whole-application identity. Per-sample now uses a
  pinned CPU scientific image containing OSP and its dependencies. Slurm CPU
  budgets and worker lifetime are connected; GPU tasks remain to be integrated.
- Full Agent Bridge design B: provider-turn quotas, tool pause/recovery,
  explicit resolution for unknown external calls and shared multi-model policy.
  New Organize workflows release Bridge capacity at each worker-tool boundary;
  replay of old workflows preserves their whole-session planning adapter. Unknown
  provider outcomes conservatively reserve capacity and may require review.
- Plan-time mapping and conservation audit errors now return to the model for
  bounded correction. Execution-time input changes or other failures remain
  explicit failures; they do not silently change the accepted decision.
- Larger real datasets and sustained throughput have not been accepted yet.
  Seven real per-sample OSP/annotation workflows completed across three workers;
  see [per-sample acceptance](PERSAMPLE_V2.md). These bounded tests establish
  concurrent placement and recovery, not production throughput.

## Concurrent Organize test, 2026-09-14

Eight Tabula Sapiens SS2 datasets (Prostate, Uterus, Thymus, Fat, Salivary
Gland, Small Intestine, Tongue and Blood) completed Organize and published
fresh outputs. Independent backed H5AD reads confirmed 11,060 input cells
and 11,060 output cells across the eight datasets, with 72 mapped experiments.
Three workers on `sh03-13n22`, `sh03-15n05` and `sh04-14n18` each had a
2-CPU, 16-GiB test budget. Recorded start/end times show six simultaneous
compute tasks, two per node. Completed compute intervals also overlap model
waiting in other datasets. Bridge retained its two-session concurrency limit.

The initial worker launch omitted the editable `harness_bridge` source path:
prepare could import its dependencies, but execute could not. Thymus, Fat and
Blood failed before scientific execution. After correcting all worker launch
environments and checking executor/publication imports, those three datasets
were submitted as explicit new `runtime-fixed` Workflows with fresh outputs
and new model calls. All eight datasets then completed. The three initial
failed Workflows and receipts remain visible; this is not evidence of automatic
failure recovery. The compute validators still indirectly import the harness.

Specifications, launch commands, service logs, `runtime-incident.md`,
`verification.json`, exported Temporal histories and an online SQLite backup
are under
`/scratch/users/chensj16/eca-runs/warmpool-v2-development/concurrent-organize-20260914-185643/`.
Temporal ran from a node-local SQLite database; cross-host persistence recovery
and a pinned scientific image remain open. No production workflows or OSP
computations were resumed. The observatory shows this batch automatically;
browser checks rendered all 22 dependencies in the latest interval and all
30 including earlier history in the 24-hour view, with no missing or misplaced
connectors.

Focused checks:

```bash
LC_ALL=C LANG=C OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 \
  PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q \
  tests/test_durable_agent_bridge.py tests/test_front_integration.py \
  tests/test_organize_v2_contract.py
```

## Agent/worker migration acceptance, 2026-09-14

New `OrganizeWorkflow` runs start an `AgentWorkflow` child for planning.
The durable patch marker `organize-worker-plan-v1` preserves the original
activity sequence when replaying pre-migration histories. The child uses the
parent dataset/workflow trace and `organize.plan` business step; each model
turn and worker tool still has its own timing and explicit dependencies.
`organize.execute` depends on the accepted `submit_plan` worker request. The
viewer keeps the three business step names and exposes the concrete operation
in task details; it does not relabel standalone agent test workflows.

Three real Tabula Sapiens SS2 datasets completed with fresh output roots:

| Dataset | Input/output cells | Complete experiments | Worker tasks | Model turns |
| --- | ---: | ---: | ---: | ---: |
| Prostate | 625 / 625 | 3 | 4 | 2 |
| Uterus | 666 / 666 | 4 | 4 | 2 |
| Thymus | 1,381 / 1,381 | 9 | 4 | 2 |

Each worker sequence was prepare, inspect_source, submit_plan, execute.
The model performed no local file reads or registered program execution.
Tasks ran on `sh03-15n05` and `sh04-14n18`; no accepted task ran twice.
Recorded intervals show no overlap between a dataset's model turn and its
worker grants. Independently backed-read outputs retained all 2,672 cells;
worker audits and published manifests agreed on all 16 experiments.
Twenty focused tests passed, including rejection of a plan that divides one
complete experiment. Fourteen pre-migration Temporal histories and all six
new parent/child histories replayed successfully. This validates this bounded
Organize migration, not sustained production throughput or host-loss recovery.

Specs, service launch records, request timing checks, exported histories,
`acceptance.json`, `verify.py`, and an online SQLite backup are saved under
`/scratch/users/chensj16/eca-runs/warmpool-v2-development/organize-agent-migration-20260914-201658/`.
The original inputs and earlier outputs were retained.

## Migration status after Organize

| Area | Implemented in v2 | Still to implement |
| --- | --- | --- |
| Organize | Three business steps; durable model/tool handoffs; plan correction; audited publication | Larger-scale validation; execution-failure recovery policy |
| Per-sample | [Temporal sample fan-out](PERSAMPLE_V2.md), whole-sample CPU OSP, durable annotation tools, publication and checkpoint resume; 7 real samples accepted | GPU variant; automatic failed-compute attempts; larger data |
| Cross-sample | Design and legacy implementation | New workflow; integration/UMAP compute; parallel precomputed DEG; annotation handoffs |
| Zoom-in | Design and legacy implementation | New lineage workflows; compute/annotation handoffs; GPU integration |
| Work Coordinator | Organize, per-sample and generic agent workflows; process recovery and history replay | Cross-sample/Zoom-in; production database; host-expiry and failover acceptance |
| Warm Pool Scheduler / Worker | HyperQueue adapter; concurrent CPU tasks; pinned CPU science image; Slurm grant and shared-memory accounting; worker lifetime; telemetry and receipts | GPU capabilities; storage staging across tiers; unexpected allocation loss at scale |
| Agent Bridge | Shared turn admission; saved SDK state; worker tools and result verification | Provider/account quotas and balancing; uncertain-call reconciliation; other harness adapters |
| Cell-exclusion accounting | Per-sample QC ledger with source cell IDs, reasons and input versions | Unified cross-stage ledger in the new workflows |

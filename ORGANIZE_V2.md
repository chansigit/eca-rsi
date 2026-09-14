# Organize v2 integration

The development branch connects one Temporal Workflow per dataset to three
bounded operations: `organize.prepare` in Warm Pool, `organize.plan` in Agent
Bridge, and `organize.execute` in Warm Pool. Temporal stores only references
and state transitions. Pool and Bridge store their own accepted requests and
results. Model waiting never occupies a Pool grant. No Slurm allocation is
requested or released by these services.

`organize.prepare` discovers accepted ECA-PP products, validates source files,
and saves compact metadata profiles. `organize.plan` uses the existing model
catalog and harness, now requiring a source-scoped experiment mapping in the
plan. `organize.execute` rechecks source identities, verifies cell conservation,
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
python -m ecarsi.work_coordinator --temporal 127.0.0.1:7233 worker
python -m ecarsi.work_coordinator --temporal 127.0.0.1:7233 start workflow-spec.json
python -m ecarsi.work_coordinator --temporal 127.0.0.1:7233 status RUN_ID
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
- Pinned CPU/GPU scientific images and whole-runtime identity. The current
  test container binds a mutable Python environment. Slurm walltime-aware
  admission, worker expiry and GPU tasks still need integration.
- Full Agent Bridge design B: provider-turn quotas, tool pause/recovery,
  explicit resolution for unknown external calls and shared multi-model policy.
  The Organize bridge currently limits whole planning sessions. Unknown
  provider outcomes conservatively reserve capacity and may require review.
- Bounded automatic replanning on an execution-time mapping audit rejection;
  current rejection is explicit and does not silently bypass the decision.
- Larger real datasets, concurrent-node throughput, and full per-sample OSP
  execution have not been accepted yet.

Focused checks:

```bash
LC_ALL=C LANG=C OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 \
  PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q \
  tests/test_durable_agent_bridge.py tests/test_front_integration.py \
  tests/test_organize_v2_contract.py
```

# Dataset workflows

One Temporal `DatasetWorkflow` submits Organize, starts each accepted analysis unit independently, and collects their outcomes. Each `AnalysisUnitWorkflow` runs per-sample once, then iterates cross-sample and Zoom-in on the surviving cells. Different datasets and units can advance simultaneously; these parent workflows reserve no numerical CPU, memory, or GPU resources.

Scientific work still uses the versioned OSP/MSP/ZMIP operations on Warm Pool workers. Agent sessions still use the shared Agent Bridge and dispatch their tools to workers. This change supplies the missing dataset-level orchestration, rather than another implementation of the scientific kernels.

## Submit

Use [the specification template](examples/dataset-v2.json), replacing the absolute input/output/service paths and selecting operation budgets and tissue context for the dataset. Species and sample key come from the accepted Organize manifest. Conflicting explicit stage settings are rejected.

```bash
python -m ecarsi.work_coordinator --service-root /shared/rsi/control \
  --task-queue ecarsi-durable-v2 start-dataset dataset.json

python -m ecarsi.work_coordinator --service-root /shared/rsi/control \
  --task-queue ecarsi-durable-v2 status-dataset RUN_ID
```

The output root must be fresh and its parent must exist. Resource budgets remain per operation. GPU `auto` follows the existing Pool preference and size threshold; `rapids` requires a GPU, and `cpu` selects Scanpy. Neither submission nor execution requests a Slurm allocation.

Outputs are arranged as:

```text
dataset/
  spec.json
  00-organize/
  units/UNIT/
    01-per-sample/
    rounds/round01/
      02-cross-sample/
      03-zoom-in/
      publication.json
    rounds/round02/...
    publication.json
  publication.json
```

Stage publications retain their artifact references and exact exclusion ledgers. Unit publications link every round and the final Zoom-in publication; the dataset publication links the units. The final H5AD is the `annotated_zmip.h5ad` artifact of that final stage. These manifests do not yet recreate the legacy `release/` directory and its presentation pages.

## Iteration and scheduling

The first cross-sample pass uses the accepted per-sample results and the existing sample inclusion decision. Later passes consume only the preceding Zoom-in survivors. They restore counts through the existing MSP integration kernel and archive prior annotation columns under `r01_`, `r02_`, etc. Original cell IDs and source/sample identities remain unchanged. Organize and per-sample are not repeated.

The shared `round_policy.decide` function is also used by the legacy loop. In automatic mode, round one never releases; subsequent rounds use the existing fractional/absolute removal thresholds, plateau rule, absolute floor, optional extra rounds, and safety cap. A forced cap completion is marked `forced_release`. An explicit positive `rounds` selects a fixed number of iterations. Policies are recorded with the publication; this initial dataset API does not expose live edits to them.

At a round boundary, `continue_as_new` bounds the analysis unit's Temporal history while preserving its parent workflow's child-result relationship. The downstream stage's first task records the actual producing Pool requests as dependencies, including the per-sample finalizers. The timeline therefore connects stages and successive iterations, not just tasks within a stage.

Each completed round checks both upstream publication identity and cell-count conservation before proceeding. Failed units retain their artifacts and do not cancel successful siblings. Once siblings finish, a dataset with failed units publishes an incomplete record and fails visibly. Unknown external outcomes are not automatically duplicated or treated as successful exclusions.

New Zoom-in histories also let independent lineages finish after another lineage fails. Their accepted computations and annotations remain reusable, but the stage cannot publish a merged success until all selected lineages succeed. A Temporal patch marker preserves replay behavior for histories that already entered the earlier scheduling path.

## Resume a terminal failure

Ordinary Coordinator/service restarts recover running workflows automatically. A terminal scientific failure first needs its cause corrected and any failed external request reconciled. Then use the same dataset run ID:

```bash
python -m ecarsi.work_coordinator --service-root /shared/rsi/control \
  --task-queue ecarsi-durable-v2 resume-dataset RUN_ID \
  --reason 'Confirmed failure corrected; existing accepted outputs retained'
```

This starts a new Temporal run under the same workflow ID and immutable dataset specification. It does not create a new dataset directory. The preflight traverses child histories, including continued runs, and rejects active descendants or unresolved failed/unknown requests. Accepted or still-running external requests keep their stable IDs; resuming does not duplicate them. For a confirmed failed Pool program, the existing `warm_pool retry` command records a new attempt and can explicitly adopt a corrected runtime with `--use-current-runtime`. It preserves the original input hashes and failure receipt.

Completed Organize, per-sample, cross-sample and Zoom-in stages are reused only after checking their specifications, upstream references, accepted Pool outputs and cell totals. These checks traverse stage boundaries; they do not repeat numerical work or model decisions. Downstream workers retain the normal artifact-integrity checks. Incomplete stages reuse their existing operation IDs and saved agent sessions. A changed input/configuration or an unaccepted output blocks recovery instead of silently overwriting it.

The dataset keeps recovery intent and new Temporal run IDs in `recoveries/`. Before replacing an incomplete `publication.json`, it archives the previous record as `publication-<digest>.json`. Completed publications cannot be replaced by different results. Recovery does not relax scientific validation or resolve an uncertain provider call by assuming it failed.

## Acceptance scope

Unit checks cover unchanged convergence decisions, source/count continuity, preservation of string cell IDs and prior annotations, skipping Organize/per-sample in subsequent rounds, preserving a running sibling after another unit fails, and service discovery reconnection. Existing CPU numerical, DEG, exclusion-accounting, and legacy loop-control checks also pass.

Real Prostate and Uterus acceptance runs were submitted as dataset workflows on 2026-09-15, using two fixed rounds and the explicitly reduced Zoom-in `min_cells=50` for these small inputs. The template retains the normal `min_cells=800`. Their final outcomes are recorded below after verification.

Both runs completed Organize and per-sample automatically: Prostate retained 507 of 625 cells; Uterus retained 441 of 666. Both first cross-sample integrations and their 48 independent DEG tasks completed. Full two-round acceptance remains in progress.

Uterus subsequently exposed a singleton parent-core DEG comparison in first-round Zoom-in. MSP `e19e777` now skips statistically untestable comparisons, records their reasons, and retains the cells. The corrected computation succeeded in science image 7. `resume-dataset` then restarted the failed dataset under the same workflow ID, with 279 existing Organize/per-sample/cross-sample request records unchanged; the resumed unit started only its unfinished Zoom-in child. Evidence is in `durable-control-20260915/dataset-terminal-recovery.json`. This verifies recovery into actual computation; full two-round publication acceptance is still separate.

A separate numerical acceptance used the previously accepted Prostate Zoom-in publication to exercise `compute-round` on 433 real surviving cells. Reintegration completed on a Pool CPU worker in 31.6 seconds. All 42,128 genes and their raw counts were unchanged, source/sample identities and archived annotations matched, and PCA/UMAP values were finite (`numerical-round2-acceptance.json`). This checks the new reintegration adapter; it is separate from the two running dataset workflows.

The first integration passes already demonstrate cross-dataset overlap: Uterus completed a 35-second `cross-sample.compute` request while Prostate was awaiting a model response. Matching Pool and Bridge receipts are recorded in `durable-control-20260915/compute-model-overlap.json`. This verifies that model waiting does not reserve numerical worker capacity; it is not a sustained-throughput benchmark.

After a dataset completes, run the read-only artifact check in the RSI science environment:

```bash
python tests/check_dataset_publication.py /shared/rsi/dataset/publication.json
```

It verifies artifact identities, exact kept/excluded cell sets across every stage and round, original source IDs, nonempty exclusion reasons, retained skipped lineages, and preservation of previous-round annotations.

Remaining production gates include completion of both live two-round workflows and their final artifact checks, large-dataset budget/history calibration, legacy release presentation and aggregation of scientific review advisories, and sustained multi-dataset throughput. The `forced_release` flag refers only to the convergence safety cap; scientific advisory details remain in stage artifacts. Ordinary Coordinator or Temporal service interruption while a workflow is running is recovered through the [shared control service](DURABLE_CONTROL.md), and does not require dataset resubmission.

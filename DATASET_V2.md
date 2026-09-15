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

## Acceptance scope

Unit checks cover unchanged convergence decisions, source/count continuity, preservation of string cell IDs and prior annotations, skipping Organize/per-sample in subsequent rounds, and service discovery reconnection. Existing CPU numerical, DEG, exclusion-accounting, and legacy loop-control checks also pass.

Real Prostate and Uterus acceptance runs were submitted as dataset workflows on 2026-09-15, using two fixed rounds and the explicitly reduced Zoom-in `min_cells=50` for these small inputs. The template retains the normal `min_cells=800`. Their final outcomes are recorded below after verification.

The first integration passes already demonstrate cross-dataset overlap: Uterus completed a 35-second `cross-sample.compute` request while Prostate was awaiting a model response. Matching Pool and Bridge receipts are recorded in `durable-control-20260915/compute-model-overlap.json`. This verifies that model waiting does not reserve numerical worker capacity; it is not a sustained-throughput benchmark.

After a dataset completes, run the read-only artifact check in the RSI science environment:

```bash
python tests/check_dataset_publication.py /shared/rsi/dataset/publication.json
```

It verifies artifact identities, exact kept/excluded cell sets across every stage and round, original source IDs, nonempty exclusion reasons, retained skipped lineages, and preservation of previous-round annotations.

Remaining production gates include dataset-level restart after a terminal scientific failure, large-dataset budget/history calibration, legacy release presentation and aggregation of scientific review advisories, and sustained multi-dataset throughput. The `forced_release` flag refers only to the convergence safety cap; scientific advisory details remain in stage artifacts. Ordinary Coordinator or Temporal service interruption while a workflow is running is recovered through the [shared control service](DURABLE_CONTROL.md), and does not require dataset resubmission.

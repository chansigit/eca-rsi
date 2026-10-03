# Dataset workflows

One Temporal `DatasetWorkflow` submits Organize. It starts each accepted analysis unit independently. It then collects their outcomes. Each `AnalysisUnitWorkflow` runs per-sample once. It then iterates cross-sample and zoom-in on the surviving cells. Datasets and units advance at the same time. These parent workflows reserve no CPU, memory, or GPU.

Scientific work runs as versioned OSP, MSP, and ZMIP operations on pool workers. Agent sessions go through the model-turn service. They dispatch their tools to workers.

## Submit

Start from [the specification template](../../examples/dataset-v2.json). Replace the input, output, and service paths. Set the operation budgets and the tissue context. Get the species and sample key from the accepted Organize manifest.

```bash
python -m ecarsi.control --service-root <control> --task-queue <queue> start-dataset dataset.json
python -m ecarsi.control --service-root <control> --task-queue <queue> status-dataset RUN_ID
```

The output root must be fresh, and its parent must exist. Budgets apply per operation. The GPU setting `auto` follows pool preference and size threshold. The setting `rapids` requires a GPU. The setting `cpu` selects Scanpy. Neither submission nor execution requests a Slurm allocation.

Outputs:

```text
dataset/
  spec.json
  00-organize/
  units/UNIT/
    01-per-sample/
    rounds/round01/
      02-cross-sample/
      03-zoom-in/
      ledger/
      publication.json
    rounds/round02/...
    publication.json
    release/
      final.h5ad
      cell_ledger.csv.gz
      cell_exclusions.csv.gz
      decisions.json
      needs_review.json
      needs_review.md
      sankey.json
      umap.json
      summary.json
      receipt.json
  publication.json
```

Stage publications keep their artifact references and exact exclusion ledgers. Unit publications link every round and the final zoom-in publication. The dataset publication links the units.

## Rounds

Round 1 cross-sample uses the accepted per-sample results and the sample inclusion decision. Later rounds consume only the surviving cells from the previous zoom-in stage. They restore counts through the MSP integration kernel. They archive previous annotation columns under `r01_`, `r02_`, and so on. Cell ids and source identities never change. The workflow does not repeat Organize and per-sample.

At each round boundary, the unit workflow:

1. checks upstream publication identity and cell-count conservation;
2. reads `<unit>/loop_control.json` and applies it over the spec `round_policy`;
3. decides `continue` or `release` with `round_policy.decide`;
4. writes `rounds/roundNN/publication.json` with `stats`, the effective `policy`, and the raw `control`;
5. runs `dataset.round-ledger` on a pool worker to write the round cell ledger and Sankey data under `rounds/roundNN/ledger/`;
6. continues as new to keep the Temporal history of the unit bounded.

**Round policy.** `round_policy` in the spec holds `rounds`, `cap`, `extra_rounds_after_convergence`, and `max_removed`. In automatic mode, round 1 never releases. From round 2, the unit releases when the round removed less than 1 % or fewer than 100 cells. The unit also releases when three consecutive rounds removed less than 2 % each. In both cases, the round must have removed fewer than `max_removed` (default 1000) cells. The parameter `cap` (default 15) forces a release, marked `forced_release`. A positive `rounds` fixes the number of rounds.

**Manual controls.** The workflow rereads `loop_control.json` at every round boundary. Parameters `cap`, `rounds`, `extra_rounds_after_convergence`, and `max_removed` override the spec. Settings `pause: true` and `stop_after_round: N` publish the round. They then end the unit workflow with a `PAUSED` non-retryable failure. Setting `pause_after_stage: crosssample | zoomin` stops execution after that stage publishes, before the round settles. Periscope shows `PAUSED` in the waiting colour, not as a failure. Clear the control entry, then run `resume-dataset`. The workflow logs and ignores an invalid file.

Failed units keep their artifacts and do not cancel successful siblings. When siblings finish, a dataset with failed units publishes an incomplete record and fails visibly. Independent zoom-in lineages finish after another lineage fails. The stage publishes only when all selected lineages succeed.

## Release

At completion, `dataset.release` runs on a pool worker with the zoom-in merge budget. It copies the final H5AD and joins the stage ledgers. It checks exact cell partitions and source ids at every boundary. The ledger starts with the accepted Organize input. OSP proposal-only removals count when cross-sample applies them.

The file `cell_ledger.csv.gz` contains one row per input cell. The file `cell_exclusions.csv.gz` contains one row per excluded cell. The release contains the model decisions with their references, review advisories (`needs_review`), and UMAP and Sankey data. Reassignments stay survivors. Publication uses the crash-recoverable directory transaction. Retries verify and reuse the committed receipt.

After release, run the read-only artifact check inside the compute image:

```bash
python tests/check_dataset_publication.py <dataset>/publication.json
```

It verifies artifact identities and original source ids across every stage and round. It verifies exact kept and excluded cell sets, non-empty exclusion reasons, retained skipped lineages, and preserved previous-round annotations.

## Resume a terminal failure

Ordinary coordinator or service restarts recover running workflows automatically. Before you resume, correct the failure cause and reconcile any failed external requests. Then run:

```bash
python -m ecarsi.control --service-root <control> --task-queue <queue> resume-dataset RUN_ID --reason 'Confirmed failure corrected'
```

This starts a new Temporal run under the same workflow id and the same immutable spec. It does not create a new directory. The preflight check traverses child histories, including continued runs. It rejects active descendants and unresolved failed or unknown requests. Requests of superseded sessions count as superseded. Accepted or running requests keep their ids. The system duplicates nothing.

The system reuses completed stages only after their specs, upstream references, accepted outputs, and cell totals check out. Incomplete stages reuse their operation ids and saved sessions. A changed input or configuration blocks recovery instead of overwriting data.

For a confirmed failed pool program, `warm_pool retry` records a new attempt. It can adopt a corrected runtime with `--use-current-runtime`. When a request pin changed after failure, archive that request folder first, then resume.

The dataset keeps recovery intent and new run ids in `recoveries/`. Before replacing an incomplete `publication.json`, it archives the old record as `publication-<digest>.json`. The workflow cannot replace completed publications with different results.

## History

Acceptance records and the September 2026 throughput follow-up are in [docs/history/DATASET_ACCEPTANCE_20260915.md](../history/DATASET_ACCEPTANCE_20260915.md). [DURABLE_CONTROL.md](DURABLE_CONTROL.md) covers service interruptions during workflow execution.

# Per-sample acceptance records — 2026-09-14/15

Moved from `docs/control-plane/PERSAMPLE_V2.md` on 2026-10-02. Historical record: hosts, images, paths and limits are those of September 2026.

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
throughput or GPU speedup. Cross-sample was unimplemented when this was written; it shipped as `control/crosssample.py` +
`stages/crosssample.py` (see CROSSSAMPLE_V2_ACCEPTANCE.md). This paragraph is the record of the
per-sample acceptance run, not a current statement of scope.

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

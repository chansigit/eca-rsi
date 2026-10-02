# Dataset workflow acceptance and throughput follow-up — 2026-09-15/16

Moved from `docs/control-plane/DATASET_V2.md` on 2026-10-02. Historical record: hosts, images, paths and limits are those of September 2026.

## Acceptance scope

Unit checks cover unchanged convergence decisions, source/count continuity, preservation of string cell IDs and prior annotations, skipping Organize/per-sample in subsequent rounds, preserving a running sibling after another unit fails, and service discovery reconnection. Existing CPU numerical, DEG, exclusion-accounting, and legacy loop-control checks also pass.

Real Prostate and Uterus acceptance runs were submitted as dataset workflows on 2026-09-15, using two fixed rounds and the explicitly reduced Zoom-in `min_cells=50` for these small inputs. The template retains the normal `min_cells=800`. Their final outcomes are recorded below after verification.

Both runs completed Organize and per-sample automatically: Prostate retained 507 of 625 cells; Uterus retained 441 of 666. Both first cross-sample integrations and their 48 independent DEG tasks completed. Both dataset workflows subsequently completed two rounds and their Pool-backed final release. Independent publication checks verified source-cell identity, stage partitions, final H5AD checksums, and full exclusion ledgers: Prostate retained 411 of 625 cells (214 excluded), and Uterus retained 259 of 666 (407 excluded). Evidence is in `durable-control-20260915/dataset-acceptance.json`. These small runs validate the complete execution and accounting path, not sustained large-dataset throughput or the biological correctness of every model judgment.

Uterus exposed a singleton parent-core DEG comparison in first-round Zoom-in. MSP `e19e777` now skips statistically untestable comparisons, records their reasons, and retains the cells. The corrected computation succeeded in science image 7. `resume-dataset` restarted the failed dataset under the same workflow ID, with 279 existing Organize/per-sample/cross-sample request records unchanged; the resumed unit started only its unfinished Zoom-in child and subsequently reached the verified final publication above. Evidence is in `durable-control-20260915/dataset-terminal-recovery.json`.

A separate numerical acceptance used the previously accepted Prostate Zoom-in publication to exercise `compute-round` on 433 real surviving cells. Reintegration completed on a Pool CPU worker in 31.6 seconds. All 42,128 genes and their raw counts were unchanged, source/sample identities and archived annotations matched, and PCA/UMAP values were finite (`numerical-round2-acceptance.json`). This checks the new reintegration adapter; it is separate from the two running dataset workflows.

The first integration passes already demonstrate cross-dataset overlap: Uterus completed a 35-second `cross-sample.compute` request while Prostate was awaiting a model response. Matching Pool and Bridge receipts are recorded in `durable-control-20260915/compute-model-overlap.json`. This verifies that model waiting does not reserve numerical worker capacity; it is not a sustained-throughput benchmark.

After a dataset completes, run the read-only artifact check in the RSI science environment:

```bash
python tests/check_dataset_publication.py /shared/rsi/dataset/publication.json
```

It verifies artifact identities, exact kept/excluded cell sets across every stage and round, original source IDs, nonempty exclusion reasons, retained skipped lineages, and preservation of previous-round annotations.

Release checks additionally cover repeat publication, exact string IDs such as `001` and `NA`, corruption rejection, independent H5AD copying, and waiting for the accepted Pool release before completing. Initial exporter checks used first-round artifacts (Prostate 625 → 459 and Uterus 666 → 259); the subsequent full two-round checks above validate the final releases.

A Pool export of the completed Uterus first-round artifacts succeeded on `sh04-14n18` in 5.2 seconds, with peak RSS 257 MiB (`release-validation-uterus-20260915`). This separate export-validation directory is not the dataset's final two-round release. The Coordinator was updated after 81 historical workflow runs replayed successfully.

The in-progress timing audit (`acceptance-timing.json`, filtered by these workflow IDs) found median Pool queue delays of 3.6–3.9 seconds. Repeated model evidence requests dominate these small runs: cross-sample guidance now explicitly describes the existing all-cluster `check_genes` mode and bounded marker panels, in addition to batched DEG SQL. Saved sessions retain their original prompts. This is a targeted reduction in avoidable round trips, not a measured sustained-throughput improvement. That timing sample used the earlier one-tool-per-reply adapter. New sessions can now batch explicitly declared read-only tool calls; existing sessions retain their original policy.

## Throughput follow-up, 2026-09-15

The live large batch contains 28 datasets, 301 samples and 1,084,377 input cells.
Its 301 initial sample computations already existed before this follow-up.
The changes separate compute admission from annotation backlog, combine complete
evidence pages, calibrate downstream operation budgets from accepted measurements,
and share model admission across ready operation kinds. Earlier sessions receive
completion preference within each kind, with aged-request service retained.
They also repair Organize resume validation and add audited transient model-turn
recovery. No sample or required scientific decision is bypassed.

Focused regression checks and replay of 34 real Temporal histories passed.
The isolated capacity check verifies that a second sample starts computation
while the first waits for annotation, and that the prepared-backlog bound still
blocks further admission. Live Kidney recovery reused accepted Organize output;
Mammary's failed model turn completed through its configured alternative.

In one 638-second development observation window, 31 sample finalizers completed,
versus 9 in the preceding 600 seconds. Successful model attempts were 94 versus
53, with 8 versus 3 timeouts. Service replacements, queue-policy changes and task
mix confound this comparison; it is not a controlled speedup benchmark. Weighted
five-minute worker CPU usage was still about 6.6%, and no dataset in this large
batch had reached final release at that snapshot. Pool queue delays during the
following check were roughly 2 seconds; model-dependent task supply remained the
main constraint. Pro cooldowns led to reducing the experimental model ceiling
from 24 total calls to 16, retaining a 12-per-model bound and primary-first routing.

Detailed snapshots, budget receipts, replay evidence and rollout records are in
`/scratch/users/chensj16/eca-runs/warmpool-v2-development/throughput-review-20260915/`.
The live batch monitor continues recording usage and completion counts every
minute and independently checks each dataset publication when it completes.

Remaining production gates include large-dataset budget/history calibration, legacy release presentation, consolidation of upstream-standardization review advisories, and sustained multi-dataset throughput. The `forced_release` flag refers only to the convergence safety cap. Ordinary Coordinator or Temporal service interruption while a workflow is running is recovered through the [shared control service](../control-plane/DURABLE_CONTROL.md), and does not require dataset resubmission.

### Full-size concurrent acceptance (started 2026-09-15)

The active test submits all 28 existing Tabula Sapiens clean tissue datasets
(1,084,377 cells, 3,055–126,016 per dataset), without subsampling. It uses automatic
round convergence, the normal Zoom-in minimum of 800 cells, real Turbo/Pro calls,
and the three existing CPU allocations. The requested GPU allocation is pending;
this run cannot yet establish GPU performance.

The first batch exposed eager AnnData layer loading in Organize metadata reads:
Lung, Bladder, Fat and Lymph Node exceeded their 4 GiB preparation budgets. The
shared HDF5 reader now slices counts and reads obs directly. Full count validation,
input identities, cell conservation and complete-experiment checks remain enabled.
All four failed preparations subsequently succeeded under the unchanged 4 GiB
limit, with peak RSS 540–669 MiB. Front-pipeline, downstream, Organize publication
and Agent dispatch/cancellation regression checks passed (65 distinct tests).

The first batch is retained as `scale-acceptance-20260915` for diagnosis. Its running
workflows and pending calls were explicitly terminated/cancelled before changing
pinned tool source. The full batch restarted with fresh IDs and output directories
under `scale-acceptance-20260915-v2`. This is an ongoing acceptance run, not a claim
that all scientific workflows have completed. `manifest.json` records all inputs
and budgets; `status.json` and `throughput.jsonl` record workflow, operation, model
and worker status every minute. Preparation receipts and memory measurements are
in `durable-control-20260915/metadata-pool-check-results.json`.

The scale test also reached the per-dataset preparation limit: the first 27
Organize publications described 283 samples, but only 104 could be prepared under
the four-sample limit while annotation waited. The new per-sample admission update
allows the running workflows to increase that limit without restarting their
science or bypassing model decisions. All 27 existing per-sample histories passed
replay before deployment; the isolated update/restart check also passed.

Cold Coordinator recovery exposed the SDK's default of 500 concurrent workflow
activations, producing over 500 threads and repeated workflow-task timeouts on an
8-CPU control allocation. Coordinator now defaults to two concurrent activations per process,
bounding cold replay while keeping normal and sticky task admission responsive. Override with
`worker --workflow-slots N` when appropriate. This limit concerns short workflow
activations, not the number of running datasets, numerical tasks, or model calls.

The 2026-09-16 01:38 UTC snapshot verified all **301 sample computations** across
the 28 datasets, accounting for exactly **1,084,377 input cells**. Median OSP task
wall time was 63.4 seconds and peak recorded process-group RSS was 7,840 MiB.
These are per-task measurements, not sustained end-to-end throughput: the run
included development fixes and service replacements. Details are in
`scale-acceptance-20260915-v2/operation-summary-latest.json`.

Scale testing exposed live-upgrade session incompatibilities and transient worker
observations that prematurely failed child workflows. Failed histories remain
visible in `failed-workflow-audit.json`; 17 saved sessions were verified unchanged
after the compatibility fix. The acceptance monitor records child failures,
performs one audited dataset recovery for these corrected causes through the
existing recovery API, and submits independent publication/ledger verification
to Pool when a dataset finishes. An unreconciled failed request blocks recovery.

Model calls remain a separate bottleneck. A 32-call trial produced timeouts and
was returned to 16 total admissions, eight per model. One Mammary turn exhausted
three 180-second attempts after a preceding 95,512-token input; its failure was
retained. New calls have a 300-second bound, and retries wait for an untried
alternative instead of repeatedly consuming their budget on the same primary.
The final scientific workflows have **not yet passed full-batch acceptance**.
The GPU allocation remains pending, so these results establish no GPU speedup.


### Bounded parallelism and recovery follow-up (2026-09-15, Pacific time)

The development batch now admits independent read-only agent tools in windows
of four, and cross-sample/Zoom-in DEG windows can be updated durably to 16.
Pool CPU/memory/GPU admission still applies. Across 1,262 completed DEG tasks in
20 datasets, the proposed workspace budgets exceeded observed peak RSS by at
least 2.18 times. The first 53 tasks actually executed with the reduced budgets
all succeeded. This is empirical headroom, not a guarantee for unseen inputs.

A simultaneous live Coordinator comparison covered 483 activations at one slot
and 1,033 at two slots, excluding the first 15 seconds after restart. Median
schedule-to-start delay fell from 0.910 to 0.154 seconds, and P95 from 9.954 to
2.236 seconds. Execution P95 stayed similar (0.342 versus 0.299 seconds). Four
control processes now use two slots each. Different task mixes and a short
observation window prevent interpreting this as an end-to-end pipeline speedup.
Temporal distinguishes polling/admission capacity from actual task execution;
see its [worker performance documentation](https://github.com/temporalio/documentation/blob/main/docs/develop/worker-performance/index.mdx).

Bridge also clears an obsolete cooldown when a newer accepted model reply
succeeds. After deployment, its existing 16-call capacity was occupied again
(12 Turbo, 4 Pro); increasing the configured provider concurrency was unnecessary
for that recovery. Long/empty model responses remain a throughput concern.

During this work, an invalid live source edit caused 13 task attempts to fail
while importing code. Eight non-model requests were retried successfully; three
model turns obtained accepted replacement replies. The original attempt
receipts remain intact. Adapter publication now rejects invalid Python, unused
dispatch snapshots are not executed, and subsequent source replacements were
syntax-checked before atomic publication. Full immutable application releases
remain a deployment hardening item; archive validation alone does not isolate
all imports from a mutable development checkout.

Sixteen terminal dataset parents were resumed after their prerequisites were
reconciled. Older sessions that ended without their required submission can
create one audited repair session, which rereads evidence and preserves the
original scientific checks. At the post-recovery snapshot, 25 datasets were
running and three still failed; this is recovery progress, not completed delivery
or sustained high CPU utilization. The GPU allocation was still pending.

Evidence is under
`/scratch/users/chensj16/eca-runs/warmpool-v2-development/throughput-review-20260915`:
`parallel-live.json`, `deg-budget-live.json`, `deg-budget-backtest/report.json`,
`activation-canary-comparison.json`, `recover-edit-window.json`,
`resume-submission-contract.json`, and `restore-deg-window.json`.
Relevant checks: 38 submission/parallel/recovery tests, four control-service tests,
and 12 dispatch tests passed. Earlier parallel and DEG changes also replayed
real Temporal histories before deployment. The batch monitor continues recording
Pool usage, task receipts, model outcomes and workflow state every minute.

# Changelog

## Unreleased

- **Remote `add-worker` carries the library path.** It ran the host Python over `ssh <node> '<python> …'`, whose shell
  loads no modules since `~/.bashrc` loads `~/pp` in interactive shells only (2026-10-05); the module-built Python then
  could not find `libpython3.12.so` and the image switch left the pool with no worker. The remote command now gets this
  side's `LD_LIBRARY_PATH`, and `switch-images.sh` prints a failed add-worker's error instead of a blank line.
- **No workflow patch branches left.** All 21 `workflow.patched` branches of the stage workflows lost their old path;
  each patch id keeps one `workflow.deprecate_patch` line so the histories since 2026-10-04 still replay (139 of 141;
  the scale test's cross-sample and zoom-in predate DEG batching by cells). The dead organize `submit_plan` activity is
  gone and organize's tools are sized by `stages.resources.size`. Deploy only at 0 running executions.
- **Turn results are contract `turn/1`** (outcome one of seven, a worker record), checked by both writers and the reader.
- **Workflow tests run on Temporal's time-skipping test server** (`tests/temporal_env.py`): the real workflow classes,
  fake activities and child workflows registered under their real names, real updates, queries, timers and
  continue-as-new. No test patches `ecarsi.control` internals or `temporalio.workflow` any more.
- **The pool names no stage operation.** `warm_pool/budget.py` moved to `stages/resources.py`: the measured ceilings,
  CPU caps and `from_cells` / `from_artifact` / `from_compute` / `from_deg_buffers`. Stage code sizes each request
  where it is built (`size` in the three stage workflows, organize prepare and `stages.evidence.plan` for every
  tool call); `warm_pool.state.submit` applies only the operator's online `ceilings` (`operator_ceiling`).
  `tests/test_layers.py` fails when a warm_pool module names an operation of the table.
- **Stage programs and stage workflows share code through `common` modules only.** `stages/common.py` holds sealed
  bundles, artifacts, `png_url` and the DEG comparisons (from `stages/persample.py` and `stages/crosssample.py`);
  `control/common.py` holds `call`, `await_pool`, `handoff`, `stage_with_waits`, `HISTORY_LIMIT`,
  `SKIPPED_CELL_LIMIT` and the DEG fan-out (from `control/persample.py`). Each program pins its own file, `common.py`
  and `contract.py`, so a change to `persample.py` no longer invalidates queued zoom-in requests.
  `tests/test_layers.py` fails when one stage program imports another.
- **The per-sample unit rule lives in ECA-PP only** (eca-pp 0.5.4 `sample_unit`; decision 0016 amended):
  organize maps ECA-PP's verdict (library, batch, whole, stop) to columns; `LIBRARY_MAX_CELLS` and the platform list
  left eca-rsi. The identify-columns result is contract `eca-pp-identify-columns/1`. A pre-0.5.4 result without a
  batch or library stops organize until identify-columns is re-run.
- **MSP 0.5.4**: DEG leaves out the genes no cell of a comparison expresses (a gene expressed in one group only
  is kept); pvals_adj is still corrected over all genes, so the DEG tables do not change. In the scale test no cell
  expressed 20 % of the genes and a single cluster none of 49 % (median).
- **DEG batches follow the cell count** (`control/common.py` `deg_batches`, workflow patch `deg-batch-cells-v1`):
  eight comparisons per request up to 50,000 cells, fewer above, one from 400,000, and `max_in_flight_deg` grows by
  the same factor. In the scale test (418k cells) one request of eight ran 75 min and timed out once while most of a
  64-core node sat idle. Each comparison is computed alone either way, so results do not change. Cross-sample and
  zoom-in share the fan-out loop (`run_degs`). `ops/replay-check.py` takes `--status` and `--where` to replay
  closed histories.
- Cross-sample and zoom-in compute ask 0.12 MiB per cell (was an estimate of 0.15): the scale test peaked at
  0.069 (418k cells, 30.3 GiB) and 0.084 (a 181k-cell lineage).
- Figures above 512 KiB reach the model as a 256-colour palette PNG at full size (`stages.persample.png_url`, used by
  per-sample, cross-sample and zoom-in reads). The 418k-cell `umap__ann_coarse.png` fell from 1.66 MB to 533 KiB; in
  the scale test, turns carrying it failed at the provider after 10 minutes and one cross-sample session died.
- Per-sample `read_evidence` reads figures in fixed pages (offset 0, then each returned `next_offset`). Any other
  offset is an error. In the 2026-10-04 scale test one session read offsets 1–9 after a complete first page, put
  55 images (7.5 MB) in its context, and its next model call timed out three times.

## 0.4.4 — 2026-10-03

Samples and batches follow ECA-PP; large samples no longer need hand-made splits
([decision 0016](docs/decisions/0016-samples-and-batches-from-eca-pp.md)).

- **Organize takes each source's samples from ECA-PP identify-columns** (eca-pp 0.5.3: batch ladder, platform, library):
  the library, else the batch, else the whole source; Harmony only when ECA-PP recommends it. Precedence: the
  spec's `sample_map`, then ECA-PP, then the planning agent (sources without an identify-columns result). A large
  source without batch or library stops at organize unless the platform is split-pool or plate.
- **Chunks**: a sample above 20,000 cells (`sample_map.chunk_cells`) runs per-sample QC as `<sample>.chunkNN`
  chunks that keep the sample as their batch; a chunked unit skips the inclusion agent.
- **Budgets from size**: per-sample, cross-sample and zoom-in compute ask for memory by cell count
  (`budget.from_cells`, raise only); partition and organize execute by file size.
- Periscope's Samples header says where the samples and the batch came from.
- `docs/history/HARD_DATASETS_20261003.md` corrected: correcting PanSci across age and sex is what the atlas wants.

## 0.4.3 — 2026-10-02

Every module now sits in one part of the system.

- **Stage helpers moved into `ecarsi/stages/`**: `organize_execute` (was `execute`), `upstream` (with organize's
  `profile_unit`), `h5ad` (`read_obs`, `open_counts`; was `design` and `downstream`), `inclusion` (was the top-level
  `crosssample`), `osp_worker`, `osp_contract`, `ledger`, `release_state`, `archive`. Pinned program lists follow.
- **`ecarsi/files.py`**: the durable-record helpers (`read`, `save`, `lock`, `immutable`, `reference`, `verified`,
  `digest`) every part used from `warm_pool/state.py`; the pool keeps only requests and receipts.
- **Shared modules import no part** (`tests/test_layers.py`; `display` and `observatory` are the listed exceptions);
  `control/dataset.py` takes run-directory names from `layout`. Organize no longer writes static Periscope pages.
- **Removed**: the `ecarsi.index`, `ecarsi.umapdata` and `ecarsi.harness` shims, `ecarsi.cost` and the sample map's
  unfed agent fallback (`build_mapping` has no `identify` argument).
- **Release gate** `ops/gate.py`: after a switch, runs `~/.config/ecarsi/gate-dataset.json` end to end and checks
  release, degraded steps, lineage reports (#26), display zone and archive (#25).
- Decision 0015 proposes running two image versions side by side; not built.

## 0.4.2 — 2026-10-02

Make the system smaller to explain and harder to break silently.

- **Degraded steps leave a record** ([decision 0013](docs/decisions/0013-degraded-results-are-recorded.md)): a
  report, a copy of readable files, a display sync or a round ledger that fails without failing the run is kept
  under `<run>/degraded/`, listed first in `needs_review` (`degraded`) and marked on Periscope ("N degraded").
  The tests run with `ECARSI_STRICT=1`, where such a failure raises.
- **Boundaries are tested** ([decision 0014](docs/decisions/0014-boundaries-are-tested.md)): the stage programs
  reach osp / msp / zmip only through their new `api` modules (OSP 0.1.8, MSP 0.5.3, ZMIP 0.3.10), which alias
  the 27 private kernel functions eca-rsi used; `tests/test_layers.py` enforces that and which subsystem may
  import which. `ecarsi/contracts.py` names the fields of every shared JSON file; writers and readers check them.
- **Deployment scripts in the repository** (`ops/`, linked from `$BASE/ops`), reading every path from
  `deployment.env`. `build-images-update.sh` replaces a wheel's distribution wherever the image has it (the
  kernels live in the science image's `/opt/rsi-python`). The unused `observatory` component of
  `control-plane.sh` and the `ecarsi/serve.py` shim are gone.

## 0.4.1 — 2026-10-02

- **Explicit sample maps on the control plane**: the dataset spec's `organize.sample_map` (`sources`, `merges`,
  `exclude_cells`, `batch_key`) reaches organize, pinned by content with its request. `sources` override the
  planning agent's experiment column; excluded cells are ledger rows with their reason and `policy_excluded`
  review items. `"batch_key": false` declares a unit one batch: `eca_batch = single_batch` on every cell, so MSP
  and ZMIP skip Harmony and the per-batch HVG vote. A named `batch_key` is checked as before (constant per
  experiment, two or more values).
- **Rejected ECA-PP sources are skipped**, as the local path did, instead of failing organize: the source
  inventory keeps them and every unit's `needs_review` lists them (`upstream_review`).
- Rounds after the first record each cell's experiment (`eca_sample_id`), not its batch value, as the sample
  of its input and exclusion rows.
- Tests run the real MSP integration for a named batch column (Harmony runs) and for one batch (skipped).

## 0.4.0 — 2026-10-02

The control plane is the only way ECA-RSI runs; the local path is gone.

- **Removed the local path**: the CLI commands `run`, `organize`, `persample`, `crosssample`, `zoomin`, `loop`,
  `ledger`, `prune` (`eca-rsi` keeps `serve`, `index`, `umapdata`), `run-eca-rsi.sh`, the modules `loop`, `zoomin`,
  `persample`, `osp_dispatch`, `prune`, `service`, `mirror` (`--mirror`), `agent_retry`, `osp_stage`, and the parts
  of `crosssample`, `downstream`, `ledger`, `design`, `cost`, `policies`, `organize`, `osp_contract`, `osp_worker`,
  `resources`, `run_state` and `layout` only it reached: about 3,900 lines of package code and 2,000 of tests, found by a module-qualified
  reachability walk from the code the control plane runs. With it went the runtime-identity digest
  (`runtime_identity`, `ECA_RSI_DEVELOPER_MODE`) and the environment variables only it read (`AGENT_WALL_MIN`,
  `PERSAMPLE_PARALLEL`, `ZMIP_PARALLEL`, `MSP_BATCH_COL`, `MSP_PYTHON`/`ZMIP_PYTHON`/`OSP_PYTHON`, `ECA_RSI_PAUSE_FILE`, ...).
- Pages still read the local path's layout (generation 1): its runs are served from their display zones.
- Found on the way, not changed: on the control plane the organize plan sets only each source's experiment column,
  so explicit sample maps (`merges`, `exclude_cells`, `batch_key`) have no input; and the study-design context for
  the agents (`ecarsi.design`) was only ever passed by the local path.

## Unreleased — 2026-09-28

The warm-pool scheduler after a week of gen-2 batches (2026-09-24 to 09-28): the two-day maintenance sprint of
09-27/28 replaced the scheduler's own queueing with HyperQueue's, cut the DEG request count eightfold, and ended
with an end-to-end run on real data. Version is still 0.3.2; this entry collects what is on `main` since then.

- **HyperQueue priorities are back, on a patched HQ.** The gap-computation panic that forced priorities off on
  09-19 is an upstream bug (It4innovations/hyperqueue#1135: `WorkerResources::remove*` indexes a worker's resource
  vector with global resource ids). Our fix is PR #1137; the maintainer's #1136 rewrites the same area and also
  covers it. The pool runs a local build of upstream `main` + #1136 + #1137 (`release.hq_priority` switches
  priorities off if that ever has to be undone); `check_hq` accepts 0.26.2 or any later build (ef512b2).
- **Priority = class first, then width** (`hq_priority`: model turn 1000, session tool 800, batch work 0, plus ten
  per CPU). Decided by replaying a journal day under alternative rules: `python -m ecarsi.warm_pool.replay
  --root <pool> --day <day>` (1f5567b, 87c1ce4) rebuilds a day from the workers' task journals with causal lags and
  runs it through a policy. On 2026-09-24 (352k tasks, 13 workers) every variant finished datasets in the same
  time (span ratio 1.01–1.02); only interactive waits differed, and class-first priority cut the p90 agent/tool
  wait from 95 s to 2 s.
- **The scheduler-side hold, backlog cap and drain are gone** (54f1f6b). Every request goes to HQ the tick it is
  seen; HQ orders by priority and, with #1136, reserves cores for wide tasks itself. `release` keeps
  `model_call_resource`, `hq_priority`, the GPU pinning knobs and `pin_wait_seconds` (was `drain_age_seconds`).
- **A request no live worker can hold stays out of HQ** (16412ad): `infeasible: <reason>` in its observation,
  counted per reason under `release.infeasible` in `scheduler.json`, re-checked every tick. The waiting workflow
  still sees `queued`.
- **Release timings come from the journals** (38c550a): `warm_pool measure` writes `pool/measured.json` (per
  operation median/p90 run on cores and cards, p90 wait); the scheduler refreshes it every 30 min and uses it for
  GPU pinning and unpinning unless a knob is set in `config.json`.
- **DEG comparisons run eight per pool request** (0b2d5b2, 622fc5a): `stages.crosssample.deg_batch` verifies and
  maps the shared buffers once, writes each comparison's `deg-<i>/result.json` and a `results.json` manifest;
  `assemble` takes manifests and single results alike. Both workflows gate it with `deg-batch-v1`; `DEG_BATCH_SIZE`
  lives in `control.persample`. The request's timeout is twice the per-comparison budget, not budget × batch: HQ
  matches `time_request` against a worker's remaining allocation, so an hours-long request never lands on a short node.
- Memory ceilings are the values the pool ran on after the PanSci organs (ad82f9c); resume supersedes every
  generation of a restarted session and accepts a publication whose Pool folder was pruned (462f069); the bridge
  summary counts model turns per dataset for Periscope (91e17fc).
- **Verified on real data, 2026-09-27/28:** 11_Shietal (9,163 cells) on two 16-core `normal` nodes plus the plane
  node: cross-sample 47 comparisons → 6 requests (22–44 s, peak 590–670 MB), zoom-in 42 → 6, four rounds, 93
  requests all succeeded first time, `hq_waiting` 0 throughout. Stopped by the owner at round 4 and pruned.
- Operations: Sherlock rejects sleeper jobs, so a worker node is now requested with a job that *is* the worker
  (`ecarsi.warm_pool slurm-worker` over the whole grant; it joins the pool by itself and leaves with the job).
  Deploy guards read the pool's own `hq` binary from `config.json` and exit with `os._exit` (the Temporal client
  can segfault at interpreter teardown and abort a `set -e` deploy after printing its verdict).

## 0.3.2 — 2026-09-19

Remove the first generation's Dask warm pool. Generation 1 runs are no longer resumed — their reports and metadata
stay readable in Periscope, and anything unfinished is recomputed by generation 2 — so the pool that used to feed
them has no remaining user.

- Delete `ecarsi.pool` (client, scheduler, executor, observe, status, CLI), `ecarsi.pool_web`, `ecarsi.compute_policy`,
  `container/dask-pool.sh` and their tests, plus the `pool` extra. Periscope loses the **Warm pool** sidebar button,
  the `/_pool/*` routes and `--pool-scheduler`; it no longer probes a dead scheduler every five seconds.
- Keep the two shared helpers generation 2 still calls: `pool/slurm.py` → `warm_pool/slurm.py` (inventory,
  process fencing; the Dask worker supervisor in it is gone) and `pool/budget.py` → `warm_pool/reservation.py`
  (the Slurm-allocation memory ledger).
- `OSP_COMPUTE_ENDPOINT` accepts only `local`; the `pool` / `auto` offload went with the pool. msp's own
  `MSP_COMPUTE_ENDPOINT=dask*` endpoints are untouched — they are an optional extra generation 2 pins to `local`.
- Nothing in the package imports dask any more.

## 0.3.1 — 2026-09-17 (branch `gen2`)

The second generation (Temporal control plane, HyperQueue warm pool, durable model-turn bridge, stage programs),
developed as feature/warmpool-v2 since 2026-09-14, integrated on one branch and given a package structure. The first
generation (`eca-rsi run`, Slurm pool, batch admission) keeps its modules where 0.3.0 left them.

- Move the flat gen-2 modules into `ecarsi.control` (work_coordinator as `control.coordinator`, temporal_service, *_workflow), `ecarsi.agent`
  (agent_bridge, agent_session, agent_dispatch, …; named `agent` so that `bridge` means only the external
  agent-harness-bridge package), `ecarsi.stages` (organize/persample/crosssample/zoomin `_v2`, dataset_release),
  `ecarsi.warm_pool.budget` (operation_budget) and `ecarsi.observatory` (dev_observatory); `python -m
  ecarsi.control|agent|observatory` entry points.
- The observatory's web server is gone: Periscope (`ecarsi serve --control-plane <run dir>`) mounts the same page and
  APIs at `/_control/` (`observatory.ControlPlane`; sidebar item "Control plane"); `container/control-plane.sh`
  starts Periscope for the run directory. `ecarsi.observatory` keeps `status` / `releases` / `tokens` / `temporal-ui`.
- A dead agent session restarts once (`control.coordinator.run_agent`: new session id `-r2`, directory `restart/`,
  same evidence; `restart.json` names the superseded session and resume treats that session's requests as
  superseded, as it now does for context resets). A second death skips the sample (finalized unannotated, prior
  label `unannotated`, `skipped_samples` in the per-sample publication, needs_review kind `agent_skipped`) or the
  lineage (kept with its cross-sample labels, `skipped_lineages` in the zoom-in publication, plan reason); a
  cross-sample session only restarts. Skipped cells above 10 % of a stage's input (`SKIPPED_CELL_LIMIT`) fail it.
- Round cap default 10 → 15 (`round_policy.DEFAULT_CAP`, `--cap`); gen-2 dataset specs state `round_policy.cap` explicitly.
- Pool failures of the budget class get one automatic retry from `control.coordinator.check_pool_once`: an execution
  deadline at twice the time limit, a preferred GPU's memory budget on CPUs (`warm_pool.state.retry` grew
  `timeout_seconds` / `without_gpu`, also on `warm_pool retry`). On 2026-09-18 a node whose Lustre client stalled
  turned seven 5-second prepare steps into 180 s deadlines, and a 4 GiB GPU budget failed three 50k-cell
  integrations; each failure ended its dataset.
- `reference` / `verified` / `immutable` belong to `warm_pool.state`; pinned program files come from `stages.program()`,
  the pinned adapter from `agent.adapter_path()`.
- Fold the v3 protocol wrappers into the stage programs: one program and one contract per stage, `stages/contract.py`
  holds what they share (no-argument listings, deg_lookup thresholds, lenient proposal parsing, checklists).
- Stages plan their own tool execution: `stages/evidence.py` and `stages/execution.py` (formerly agent_evidence /
  agent_tool_execution) are registered by the session as its `planner`; the model-turn service imports nothing from stages.
- Retire the first generation's batch admission on this branch: `eca-rsi batch`, the node agents, OSP compute-ahead,
  the stage runtime builder and the driver memory leases (batch, preparation_offer, prepare_osp, build_stage_runtime,
  stage_python, driver_python, runtime_logging, driver_budget, workflow_web). Datasets are admitted by the Temporal
  control plane; Periscope keeps its dataset and pool views.
- Absorbed from the batch: replay of already-saved pool/bridge requests, inode-keyed settled caches, poll tolerance and
  120 s host steps, saved-program resume, repeat-rejection stop, protocol v4 (inline evidence, single-call paged tools,
  lenient JSON, no finalize step), GPU columns in the status report.
- Protocol v4 tools without arguments tolerate an ignored `offset`, the type-context hint no longer asks for pages, and a
  rejected cross-sample submission names the missing DEG query or figure.
- The observatory defaults to `<root>/pool` and `<root>/bridge` and takes `--pool-root` / `--bridge-root` from the
  control-plane template; `ecarsi.control` imports nothing heavy, so the host-side observatory can read the
  Temporal endpoint without temporalio.
- Stages declare their read-only tools (`read_only` in the session spec; per-sample, cross-sample and zoom-in each
  register their own) and size their tool requests (`stages.evidence.budget`); the model-turn service keeps no list of
  stage tool or module names, only the read-only flag and the `{state}` handoff decide what may run in parallel.
- Accepted 2026-09-17 evening on a fresh run directory (Tabula Sapiens ear, testis, kidney; two pool nodes): organize →
  per-sample → cross-sample → zoom-in → round 2 with no failed workflow, a coordinator restart in mid-session that
  every dataset survived, a zoom-in lineage completed by an accepted submit_quality, and the observatory page and APIs
  serving the run; 125 model replies, 0 failed, every rejected submission a host rule the model then satisfied.
- `ecarsi.observatory tokens --bridge-root …` (`control-plane.sh tokens`): per dataset run, model turns and prompt /
  completion tokens summed over the saved replies, by session kind and model; `releases` lists released units.
- Per-sample evidence tables reach the model compacted (`stages.execution.compact_tables`: top 15 markers per cluster with
  two-decimal lfc and pct, top 8 ambient genes, PAGA as a sparse neighbour list; 185k → 24k characters for a 43-cluster
  sample): raw CSV pages overran the provider context at the third turn of a large sample, in every generation.
- A model turn's pool request id includes the turn's content digest: a turn folder re-created under the same name with
  different content no longer replays the earlier reply (a resumed session had replayed 34 archived replies).
- Gen-2 documents move to `docs-gen2/` (plus ARCHITECTURE.md); `container/control-plane.sh` is the launcher template and
  `container/agent-worker-runtime-20260917.json` the science runtime for the new import names.

## 0.3.0 — 2026-09-12

- Add an optional Slurm warm pool with a shared FIFO/resource-fit queue, manually started workers and CPU/memory/GPU/time inventory. No automatic allocation or job cancellation.
- Add OSP compute dispatch with isolated attempts and driver-owned validated publication; retain local execution and driver-side annotation.
- Add local/pool/auto routing, worker drain/loss fencing and live CPU/memory/GPU status. Require MSP 0.5.2 for the optional pool adapter.
- Match Slurm GPU device minors to CUDA-visible devices through UUIDs.

## 0.2.10 — 2026-09-12

- Add `pause` and `pause_after_stage` controls, drain running sample/lineage workers and preserve exit 3 through the driver.
- Require MSP 0.5.1 / ZMIP 0.3.9 / bridge 0.2.14 for validated partial annotation recovery and cooperative pause.
- Integrate recorded-lineage model evaluation with hashed fixtures, fresh candidate work directories, failure records and production output validation.

## 0.2.9 — 2026-09-12

- Keep bridge version/source as agent provenance, outside scientific runtime identity; retain provider-qualified names.
- Fix developer resume to verify reused OSP receipts with their original identity and preserve skipped runtime checks.
- Surface uncertain MSP coarse boundaries and written ZMIP island reviews; round one no longer receives an over-budget convergence flag.
- Require the bridge 0.2.13 / MSP 0.5.0 / ZMIP 0.3.8 combination and document the manual CPU/GPU pool scope.

## 0.2.1 — 2026-09-07

- Source provenance tolerates a missing `git` binary (slim containers): the commit is recorded as null instead of
  failing persample. Found on the first Apptainer-env run (calico-aging kidney).

## 0.2.0 — 2026-09-07

- Sample-map cell policies (`ecarsi.policies`): `exclude_cells` rules applied before any OSP subset is cut (every
  excluded cell on the ledger as `removed:persample-policy:<reason>`, listed in needs_review) and a declared
  `batch_key` (validated constant per experiment, back-filled for blank cells, passed to MSP). Without a map the
  sample-column agent may propose exclusions (host-validated) and a separate call only *recommends* a batch key.
- Run identity compares content only (package version + source hash); checkout path and git HEAD are recorded as
  `provenance`. Doc-only commits or a relocated worktree no longer invalidate a resume.
- `loop`: manual overrides via `<unit>/loop_control.json`, re-read at every round boundary (`cap`, `rounds`,
  `extra_rounds_after_convergence`, `stop_after_round` → pause with exit 3); the round loop is a `while`.
- A sample whose OSP QC removes every cell is finished-and-empty: accounted in `qc_removed.csv`, auto-excluded
  before the inclusion agent, listed under needs_review; the loop prerequisite accepts it.
- `organize` ignores ECA-RSI run roots mirrored inside the ECA-PP input tree; sample maps gain
  `derive_from_cell_id` and `missing_as` for explicit experiment partitions.
- Landing pages: one design system, overview page and navigator grouped by collection; step state derived from
  light markers only, so a `--mirror` copy without h5ad shows the same stage as the run root; Sankey stage titles
  vertical, labels decluttered, big nodes centred.
- `serve`: access log with the visitor's address (`X-Forwarded-For` behind ngrok) and user agent; resizable
  sidebar, sorting, no Slurm-specific column.

- `--mirror DIR` on `run` / `organize` / `persample` / `loop` (`ecarsi.mirror`): remembered in `<root>/mirror.json`;
  light files copied to DIR after every landing-page write, the whole root at release (with pruned files removed
  from DIR's copy of that unit only). Page footers carry a `run state updated <time>` stamp derived from state-file mtimes.
- Derive a study-design text per unit (`ecarsi.design`: obs columns constant within each sample)
  and pass it to MSP and ZMIP as `--design-context` in every round. Agent context only; not part of run identity.

## 0.1.0 — 2026-09-05

Initial PyPI release of the ECA-RSI workflow driver.

- Connect ECA-PP products to OSP per-sample processing and iterative MSP/ZMIP analysis, with explicit sample identities, upstream status checks, removal ledgers, and resume validation.
- Require the published bridge 0.2.3, OSP 0.1.2, and MSP/ZMIP 0.3.3 compatibility baselines; install kernels with `ecarsi[kernels]`.
- Embed final UMAP data in unit HTML pages for offline viewing, with adaptive point sizes, zoom, hover, and legend selection.
- Default report prose to English; other languages require explicit configuration.
- Preserve current stress and mitochondrial removal policies. A separate processing-stress policy remains under discussion.

Validation includes repository tests, installed-package checks, and offline browser interaction checks. Earlier real-data acceptance covers a two-round RSI run on Clayton and a separate full-size MSP/ZMIP run on 19Liu; the latter is not a full RSI release or a rerun of all model decisions on this release.

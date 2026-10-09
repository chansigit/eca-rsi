# ECA-RSI: recursive self-improvement for an Ensemble Cell Atlas

ECA-RSI is the `ecarsi/` package, with its kernels and agent harness in the same repository (decision 0018). Deterministic kernels (osp / msp / zmip) do the computation. Agents make narrow decisions. The host validates these decisions. A round loop repeats integration and refinement until it meets a cell-count rule.

## Read this first

- **New to the code:** [docs/OVERVIEW.md](docs/OVERVIEW.md) (one page: six parts, one dataset start to finish), then [docs/decisions/](docs/decisions/README.md) (why each design choice was made).
- **One supported way to run: the control-plane path.** Start Temporal, the HyperQueue (HQ) warm pool, and the bridge from `container/control-plane.sh`. It is the only path: 0.4.0 removed the local path (`eca-rsi run`, `run-eca-rsi.sh`, `--mirror`). Its old runs are still shown from their display zones, so `ecarsi/ui` and `layout.py` keep reading their layout (generation 1).
- **Code lives in `$GROUP_HOME/chensj16/eca/src/`**: eca-rsi (with `osp/`, `msp/`, `zmip/`, `standissect_lite/`, `harness_bridge/`, `genesets/` at its top since 0018; the old separate checkouts are frozen) and the upstream eca-pp. Worktrees are in `$GROUP_HOME/chensj16/eca/worktrees/`. The paths in `$SCRATCH/projects/*` are symlinks to these checkouts.
- **Production code is the current version** (decision 0019): a read-only copy of a commit in `$GROUP_HOME/chensj16/eca/versions/<commit12>/`, named by the `versions/current` link; its coordinators and runners serve new datasets, and the next child workflows of running ones (0022), on the task queue `ecarsi-<commit12>`. The shared components and the Python environments come from the two images in `$GROUP_HOME/chensj16/eca/images/` (`ops/build-images-update.sh`, `ops/switch-images.sh`). Editing a checkout changes nothing in production. The deployment scripts are in [ops/](ops/README.md) (`$BASE/ops` links there) and take effect when `main` moves.
- **Change code like this:** Edit in `worktrees/eca-rsi-dev`. Run the tests inside the images. Fast-forward `main` and push. Publish it as a version, gate it next to production, make it current, retire the old one when its queue is empty (INSTALL.md A.10). No waiting for running executions; only an image change waits for 0 (A.9).
- **Test like this:** Run `bash ops/test-lane.sh <layer>` while developing (the test modules of one layer, in parallel, 10-30 s) and `bash ops/test-lane.sh all` (the whole suite on every core, about 1.5 min) before a push; `bash ops/runsci-dev.sh -m pytest -q tests` is the same suite on one core (GitHub runs the subset that needs no compute image and `ruff check` on every push; tests/README.md). This command runs the whole suite inside the compute image (~4 min). The script `runpy-dev.sh` uses the control image and cannot import the kernels. For pure document changes, run only `git diff --check` and a link check.
- **Runtime state is in `$GROUP_SCRATCH/chensj16/eca/{control,pool,bridge,runs}`.** Set directories to mode 0700. Do not scan `pool/requests` or `bridge/requests` unpaced, and do not `du` or mass-delete them: the coordinators' Lustre client stalls under unpaced scans of the request folders. A walk of one run directory is fine.
- The working language with the owner is Chinese. Do not write Japanese.

## Package map

```
ecarsi/control/     orchestration (Temporal workflows): coordinator, temporal, dataset, persample, crosssample, zoomin
ecarsi/agent/       model-turn service: dispatch, session, parallel, tool_errors, runners
ecarsi/warm_pool/   execution pool: requests and receipts (state), HQ adapter, scheduler, workers, provisioning, measure
ecarsi/stages/      programs the pool runs: organize, persample, crosssample, zoomin, release, display, contract, evidence,
                    tools (the agent tools cross-sample and zoom-in share, and the tail of every tool call: hint, amend note, result);
                    execution; and their helpers: organize_execute, upstream (ECA-PP products), h5ad, inclusion,
                    osp_worker, osp_contract, ledger, release_state, archive
ecarsi/ui/          Periscope: serve (HTTP), registry (dataset list), fleet (dataset summaries), home (navigator,
                    overview), index (run and unit pages; gen1, gen2, common), static/ (CSS, JS), umapdata, control,
                    records, top (the control page in a terminal): read-only, tests/test_monitor_isolation.py
ecarsi/*.py         the vocabulary every part shares; imports no part: layout, files (durable JSON records),
                    contracts, degraded, run_state, review, round_policy, plan, sample_mapping, policies,
                    model_web, resources; display (zone sync) and observatory (operator reports) are the two
                    listed exceptions
ops/                the deployment scripts ($BASE/ops links here; ops/README.md)
```

Boundaries (0014, `tests/test_layers.py`): only `ecarsi/stages/` uses the kernels, and only through `osp.api`, `msp.api`, `zmip.api`; a name the stages need goes into that kernel's `api` module first. The pool, agents, stages and Periscope never import orchestration; the shared modules import no part; see the test for the full table.

The kernels are osp, msp, and zmip, top-level packages of this repository (0018). The osp kernel performs per-sample QC, clustering, and annotation. The msp kernel performs cross-sample integration, inspection, and annotation. The zmip kernel performs lineage zoom-in. The zmip kernel reuses the DEG, evidence, and report code from the msp kernel; msp uses `standissect_lite` for fragments; osp and msp share `genesets` (0020: the stress panel and the mitochondrial genes, one rule for every species). `harness_bridge` is the agent runtime. Their tests are in `tests/<package>/`; `test_layers` says what each may import (none imports `ecarsi`; since #28 none runs agents or imports `harness_bridge`).

Run directory layout (one run = one dataset):

```
<root>/spec.json  publication.json  00-organize/  display-sync/
<root>/units/<unit>/
  01-per-sample/<sample>/
  rounds/roundNN/{02-cross-sample/, 03-zoom-in/<lineage>/, ledger/, publication.json}
  release/{final.h5ad, summary.json, needs_review.md, needs_review.json, cell_ledger.csv.gz, cell_exclusions.csv.gz, decisions.json, sankey.json, umap.json, receipt.json}
```

## Control plane

Layers, module map and mechanisms: [docs/control-plane/ARCHITECTURE.md](docs/control-plane/ARCHITECTURE.md). The numbers below refer to [docs/decisions/](docs/decisions/README.md).

- **Components** (`control-plane.sh start|stop|restart|status|report`): temporal, hq, scheduler, bridge, runners, coordinators (4; 0008), fleet-status, pruner. Periscope runs from the compute image (`ops/start-periscope.sh`); its control-plane page is `/_control/`, and `ops/rtop` shows the same records in a terminal (`eca-rsi top`). It serves the display zones under `display_root` and `more_display_roots` of `~/.config/ecarsi/results.json` (rescanned every 10 min) plus `~/.config/ecarsi/periscope-datasets.json`.
- **Settings** (0011): everything in `~/.config/ecarsi/`; `deployment.env` holds every machine path, template `examples/deployment.env`; the table is INSTALL.md A.4. Model API keys go in `~/.config/ecarsi/keys.env` (mode 600) or `~/.bashrc`; never in a catalog, spec, issue or log.
- **Images** (0010): `CODE=<checkout>` in the launcher shadows the snapshot for development. `control-plane.sh host-code` unpacks the snapshot to `control/image-code` for host-side helpers. Run `warm_pool configure-runtime` inside the compute image.
- **Workers** (0006): the owner requests Slurm nodes; there is no autoscaler. `container/worker-node.sh` makes the job itself the worker; `warm_pool add-worker <host> --job-id <id>` joins a running allocation (a worker directory keeps its CPU slice: pass the same `--cpus`). Re-add the workers after an image switch. The plane node can be a 6-core test worker; stop it before a real batch. Do not touch the owner's `warmpool-gpu` jobs.
- **Scheduling** (0006): HQ priority = class base (agent 1000, tool 800, work 0) + 10 × cpus; `infeasible: <reason>` when no live worker can hold a request. DEG runs 8 comparisons per request up to 50,000 cells, fewer above (one from 400,000), and `max_in_flight_deg` grows by the same factor (`control/common.py` `deg_batches`).
- **Pinned files** (0005): pool requests pin their version's stage files (`ecarsi/stages/*`, `ecarsi/agent/session.py`), and a version never changes, so sessions in flight are safe; never edit anything under `versions/`. Switch images only when `ops/count-wf.py` reports 0 running executions.
- **Workflows** (0003): a version replays only the histories of its own queue, so a changed workflow ships as a new version without `patched` branches or a replay check (the old `deprecate_patch` lines went with 0019). Every child workflow (unit, per-sample, each sample, cross-sample, zoom-in) starts on the current version's queue when that version has a coordinator, else on its parent's (0022: `common.start_child` asks the `before_child` activity; each stage record names its version, a publication lists `versions`); no other workflow code names a task queue (`tests/test_versions.py`), and nothing may read files at workflow-module import (Temporal's sandbox; same test). Cross-sample and zoom-in continue as new past 5,000 history events (`progress['history_limit']` lowers it, for tests). Workflow tests run the real classes on Temporal's time-skipping test server with fake activities and child workflows registered by name (`tests/temporal_env.py`); no test patches `ecarsi.control` or `temporalio.workflow`.
- **Sessions**: protocol 2 (portable history; legacy protocol-1 restores fill `annotations: []`). A failed session restarts once (`-r2`, same evidence); a second failure skips the sample (label `unannotated`, needs_review `agent_skipped`) or the lineage (keeps cross-sample labels). Cross-sample sessions never skip. A stage fails when skipped cells exceed 10 % of its input (`SKIPPED_CELL_LIMIT`).
- **Two zones** (0009): `start-dataset` fills the spec key `storage` from `results.json`. A `dataset.display` pool task (tool class) syncs the display zone after every stage; the final sync archives the work tree to `<archive_root>/<collection>/<dataset>/<run_id>.tar.gz`. Syncs are not awaited; a failed sync never fails a run. Old runs were normalized into `$OAK/eca-rsi/{display,work}` (`.tar.zst`).
- **Pruner**: deletes the pool requests of finished runs. Failed runs keep their requests and their Periscope row until a later run of the same dataset completes.
- **Hard cases** (0021): the `published` sync first freezes every hard agent session (restarted, context reset, 2+ rejected submissions) with its evidence into `<archive_root>/_cases/<collection>/<dataset>/<run>/`, before the pruner can delete the requests; bounds and rules are constants in `ecarsi/stages/cases.py`.
- **Manual controls** (0022): `<unit>/loop_control.json`: the limits `cap`, `rounds`, `extra_rounds_after_convergence`, `max_removed` (read at every round boundary) and the brakes `brake: round` (after this round; the old `pause`, `stop_after_round: N`), `brake: stage` (after the stage now running; the old `pause_after_stage: crosssample|zoomin`), `brake: step` (before the next child workflow or the release: no new sample, stage or round starts, the ones running finish). Each ends the unit workflow as `PAUSED: …`; clear the control and run `resume-dataset <run_id> --reason …`. The hard brake is a command, `python -m ecarsi.control … brake <run_id> --hard --reason …`: the dataset's workflows end now, pool tasks already on a worker run out and are discarded, every directory stays, `resume-dataset` continues. `warm_pool cancel` is the kill (pool tasks killed, not resumable). Periscope shows PAUSED in the wait colour.

## Scientific rules

- **Stopping uses cell counts only.** Label changes are not a stopping criterion. With `round_policy.rounds` N, release after N rounds (N = 1 is allowed). Otherwise, release when condition A or condition B holds, and condition C holds. Condition A: this round removed < 1 % or < 100 cells. Condition B: three consecutive rounds removed < 2 %. Condition C: this round removed < 1 000 cells (`max_removed`). In automatic mode, round 1 never releases. `round_policy.cap` (default 15) forces a release and marks it.
- **Cumulative removal is not a risk.** Only the per-round policy gates a release. Do not flag total cell loss across rounds.
- **Every removal is accounted per cell.** Track removals in osp `qc_removed.csv`, msp `annotation_removed.csv`, and zmip `zmip_removed.csv`. The ledger aligns these files. The counts must match exactly.
- **Removal semantics.** OSP QC filters cells directly. OSP annotation keep/flag/drop provides advice only. MSP integrate and MSP inspect keep cells. MSP annotation applies preannotation, inspect drops, and agent removals. Surviving cells remain in `annotated.h5ad`. ZMIP applies local removals and outputs `annotated_zmip.h5ad`.
- **Zoom-in.** ZMIP plans lineages from labels and connectivity. The default minimum size is 800 cells. Small lineages keep their labels. Reassign changes labels without re-embedding in the target lineage. The host check `lineage_islands.csv` rejects merging separate islands into one lineage. Splitting one island requires `confirm_shared_islands: true` and lands in needs_review.
- **Single sample.** A single sample skips the inclusion agent. If inclusion leaves one sample, MSP skips Harmony. In that case, inspect and annotate ignore sample composition.
- **Stress policy** (0017, spec `stress_policy`: remove by default, or keep). A removal of 10 or more cells as stress or dissociation stands only when `stress_clusters.csv` marks its cluster, as dying only when its cluster is marked mito (a small cluster with more than 3 mitochondrial genes among its top 10 against its local siblings) or on a clearly higher mitochondrial fraction or fewer genes than the same identity (AUC ≥ 0.7, p < 0.05); otherwise its cells stay, labelled in obs `retained_state`. Under keep they always stay, as do fragments removed only by the dissociation or mitochondrial test and OSP dissociation-stress drops. `stages/common.py` holds the rules.
- **Biological doubts do not block release.** Send low confidence, inspect flags, sample exclusions, and reassignments to `release/needs_review.*`. Group these items by category: degraded, convergence, removed, fragment_removed, stress_retained, sample_excluded, reassigned (recurring = its cells were already moved in an earlier round, whatever the labels), inspect_flag, lineage_skipped, low_confidence, agent_skipped, and policy_excluded. Execution failures still fail the unit.
- **Degraded steps are recorded** (0013, `ecarsi/degraded.py`). A step that may not fail the run (a report, a copy of readable files, a display sync, a round ledger) calls `degraded.note` in its `except`; the record goes to `<run>/degraded/`, needs_review `degraded` and Periscope's "N degraded" mark. Never swallow such a failure with a bare print. The tests run with `ECARSI_STRICT=1` (conftest), where `note` re-raises.
- **Boundary reviews.** MSP requires `boundary_reviews` for adjacent coarse-label pairs. ZMIP requires `shared_island_reviews` for same-island splits. A missing DEG or a fixed mixing percentage never forces a merge.
- **Samples and batches** (0016): each source's samples come from ECA-PP identify-columns, which names the unit (`sample_unit`: library, batch, whole or stop, eca-pp 0.5.4; Harmony only when ECA-PP recommends it); the planning agent decides only sources without that result. Samples above 20,000 cells run as chunks (`<sample>.chunkNN`) that keep their sample as the batch; a chunked unit skips the inclusion agent. A `stop` unit (a large droplet-like source with no batch or library) or a pre-0.5.4 result with neither stops organize; the rule lives in ECA-PP only.
- **Sample map** (spec `organize.sample_map`; `ecarsi/policies.py`, `sample_mapping.py`): overrides both. `sources` set the experiment column; `merges`, `exclude_cells`, `batch_key` and `chunk_cells` apply as written. `batch_key: false` = one batch, no Harmony. See [docs/control-plane/DATASET_V2.md](docs/control-plane/DATASET_V2.md). ECA-PP sources with status `rejected` are skipped and listed in `needs_review`.
- **H5AD slimming.** The file `organized.h5ad` keeps expression in `layers["counts"]` only. `X` is an empty placeholder. Convert wide integer counts to int32. Do not include `.raw`. Store embeddings as float32.
- **Release.** `release_state.py` builds the release in a staging directory and switches atomically. Intermediate matrices stay in the pool requests that computed them; the pruner deletes those after the run finishes.

## Environment variables

Agent models come from the catalog `~/.config/ecarsi/models.json` (`ECA_MODEL_CATALOG` for another file). For every call the model-turn service sets the bridge's selection variables itself (`AGENT_MODEL_POOL`, the provider URL; `ecarsi/agent/dispatch.py`). What remains for the owner:

| Variable | Meaning |
|---|---|
| `ARK_API_KEY`, `OPENROUTER_API_KEY`, `VLLM_API_KEY` | Provider keys, in `~/.config/ecarsi/keys.env` (mode 600; `ECA_KEYS_FILE` for another file) or `~/.bashrc`; the runners and pool turns read them there. A catalog entry with its own `url` and `key_env` (e.g. `ARK_PLAN_API_KEY`) uses that variable (#54). |
| `OPENAI_AGENTS_API` | The options are `responses` and `chat_completions`. The default value is `responses`. |
| `OPENAI_AGENTS_MAX_NUDGES`, `OPENAI_AGENTS_MAX_CONTEXT_RESETS` | The default value is 2 for each variable. |
| `OPENAI_AGENTS_SERVER_STATE` | The default value is 1. This value enables incremental Responses continuation. |
| `MSP_COMPUTE_ENDPOINT` | The control-plane path pins `local`. |

## Versions

The current version is ecarsi 0.4.8 (upstream: eca-pp 0.5.5); since 0018 it covers the kernels and harness_bridge, which keep their last release numbers (OSP 0.1.8, MSP 0.5.4, ZMIP 0.3.10, harness_bridge 0.2.15, standissect-lite 0.2.0) only as history. The combination also includes openai-agents 0.22.3 and claude-agent-sdk 0.2.163. HQ is the owner's patched fork on branch `local`. The authoritative lists are [INSTALL.md](INSTALL.md) and `container/control-requirements.lock`.

## Targeted checks

```bash
# organize and per-sample contracts; no MSP/ZMIP needed
python -m pytest -q tests/test_front_integration.py tests/test_osp_worker.py tests/test_empty_samples.py tests/test_cell_policies.py
# round policy, release, display zone, the stage workflows
python -m pytest -q tests/test_round_policy.py tests/test_loop_control_gen2.py tests/test_release_state.py tests/test_dataset_release.py tests/test_display_zone.py tests/test_zoomin_v2.py
```

Run the tests through `ops/runsci-dev.sh` to resolve imports. `tests/conftest.py` restores `os.environ` around each test.

## History

- Branch `primitive` holds the six-step prompt loop: Explore → Compute → Annotate → QC → Apply → Stop. In this loop, agents write their own analysis code. Directory [docs/history/primitive/](docs/history/primitive/) holds its `run.sh` and `steps/*.md`. The main line does not include its removal budgets and its `docs -> ../eca-cycle/docs` link.
- `attic-v01` (schema + lint + skill, August 2026) was never in git. It is on Oak under `$OAK/eca-rsi/work/_batches/attic-v01/`, with the other archives listed in `$OAK/eca-rsi/INDEX.tsv`.
- The project retired several rules. It no longer makes the main checkouts read-only during batches, because production now runs from images. Version 0.3.2 removed the Dask pool and `compute_policy`. Version 0.4.0 removed the local path (CLI `run`/`organize`/`persample`/`loop`, `--mirror`, prune, the runtime-identity digest, `ECA_RSI_DEVELOPER_MODE` and the local-only environment variables). The project retired batch admission before 0.3.0 (`eca-rsi batch`, node agents). It also retired the keeper autoscaler.
- Dated records, acceptance reports, and the operational to-do live in [docs/history/](docs/history/) and in the deployment directory's `OPEN-ITEMS.md`.

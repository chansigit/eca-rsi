# ECA-RSI: recursive self-improvement for an Ensemble Cell Atlas

ECA-RSI is the `ecarsi/` package. Deterministic kernels (osp / msp / zmip) do the computation. Agents make narrow decisions. The host validates these decisions. A round loop repeats integration and refinement until it meets a cell-count rule.

## Read this first

- **One supported way to run: the control-plane path.** Start Temporal, the HyperQueue (HQ) warm pool, and the bridge from `container/control-plane.sh`. The local path is not maintained (`eca-rsi run`, `run-eca-rsi.sh`). Do not debug the local path. Its interpreter venvs are gone.
- **Code lives in `$GROUP_HOME/chensj16/eca/src/`** (eca-rsi, osp, msp, zmip, agent-harness-bridge, standissect-lite). Worktrees are in `$GROUP_HOME/chensj16/eca/worktrees/`. The paths in `$SCRATCH/projects/*` are symlinks to these checkouts.
- **Production code is the snapshot inside the two images** in `$GROUP_HOME/chensj16/eca/images/`. Editing a checkout changes nothing in production. New code reaches production only through a rebuilt image (`ops/build-images-update.sh`, `ops/switch-images.sh` in the deployment directory).
- **Change code like this:** Edit in `worktrees/eca-rsi-dev`. Run the tests inside the images. Fast-forward `main`. Rebuild and switch images when no execution is running.
- **Test like this:** Run `bash $CONTROL/ops/runsci-dev.sh -m pytest -q tests`. This command runs the whole suite inside the compute image (~4 min). The script `runpy-dev.sh` uses the control image and cannot import the kernels. For pure document changes, run only `git diff --check` and a link check.
- **Runtime state is in `$GROUP_SCRATCH/chensj16/eca/{control,pool,bridge,runs}`.** Set directories to mode 0700. Do not scan `pool/requests` or `bridge/requests` unpaced, and do not `du` or mass-delete them: the coordinators' Lustre client stalls under unpaced scans of the request folders. A walk of one run directory is fine.
- The working language with the owner is Chinese. Do not write Japanese.

## Package map

```
ecarsi/control/     Temporal workflows: coordinator, temporal, dataset, persample, crosssample, zoomin (the package itself does not import temporalio)
ecarsi/agent/       model-turn service: dispatch, session, parallel, tool_errors, runners
ecarsi/warm_pool/   bounded compute requests, HQ adapter, scheduler, provisioning (add-worker, slurm-worker), measure
ecarsi/stages/      programs that run in the pool: organize, persample, crosssample, zoomin, release, contract, evidence, execution
ecarsi/ui/          Periscope (serve, index, umapdata); outside the identity digest
ecarsi/layout.py    the only place that defines the run directory layout; no step builds paths by hand
ecarsi/observatory.py   status / releases / tokens reports; the data behind Periscope's /_control/ page
```

The kernels are osp, msp, and zmip. The osp kernel performs per-sample QC, clustering, and annotation. The msp kernel performs cross-sample integration, inspection, and annotation. The zmip kernel performs lineage zoom-in. The zmip kernel reuses the DEG, evidence, and report code from the msp kernel. The bridge (agent-harness-bridge) is the agent runtime. It provides provenance and is not part of the run identity.

Run directory layout (one run = one dataset):

```
<root>/index.html  organize/manifest.json  mirror.json
<root>/units/<unit>/
  index.html  progress.log  input/  persample/<sample>/
  rounds/roundNN/{manifest.json, input.h5ad (N≥2), crosssample/, zoomin/, ledger/, stats.txt, decision.txt}
  release/{final.h5ad, summary.md, summary.json, needs_review.md, needs_review.json, cell_ledger.csv, sankey_coarse.png, umap.json}
```

## Control-plane path

For details, see [docs/control-plane/ARCHITECTURE.md](docs/control-plane/ARCHITECTURE.md).

**Components.** The command `control-plane.sh start|stop|status` manages several components: temporal, hq, scheduler, bridge, runners, coordinators, fleet-status, and pruner. Temporal includes Temporal Server and PostgreSQL from `/opt/rsi-services`. The HQ server runs separately so scheduler restarts keep workers connected. Runners are resident model-call processes. Coordinators are 4 Temporal workers. Start Periscope from the compute image with `ops/start-periscope.sh`. The control-plane page for Periscope is `/_control/`. Periscope serves every display zone under the `display_roots` of `~/.config/ecarsi/periscope.json` (rescanned every 10 min), plus the entries of `~/.config/ecarsi/registry.json`.

**Images.** The image `rsi-control-*.sif` holds Temporal, PostgreSQL, HQ, the agent SDKs, and the ecarsi snapshot at `/opt/eca-rsi`. The compute image `rsi-science-*.sif` holds the kernels at `/opt/rsi-python`, HQ, and the same snapshot. In the launcher, `CODE=<checkout>` shadows the snapshot for development. The command `control-plane.sh host-code` unpacks the snapshot to `control/image-code` for host-side helpers. Run `warm_pool configure-runtime` inside the compute image.

**Workers.** The owner requests Slurm nodes. There is no autoscaler. The script `container/worker-node.sh` makes the job itself the worker (`slurm-worker`). Run `warm_pool add-worker <host> --job-id <id>` to join a running allocation. Workers advertise a runtime digest. When you switch images, stop the worker supervisors and add the workers again. The plane node itself can be a 6-core test worker. Stop it before a real batch. Do not touch the owner's `warmpool-gpu` jobs.

**Scheduler.** Every request goes to HQ with a native priority. The priority formula is class base + 10 × cpus. The class base values are 1000 for agent, 800 for tool, and 0 for work. There is no hold, drain, or backlog layer. The scheduler marks a request as `infeasible: <reason>` if no live worker can hold it (cpus, memory, required GPU, or worker time left ≥ `time_request`). The scheduler retries the request every tick. DEG runs in batches of 8 comparisons per request (`DEG_BATCH_SIZE` in `ecarsi/control/persample.py`). A batch timeout is twice the per-comparison budget.

**Requests pin program files by content.** Do not fast-forward a stage file (`ecarsi/stages/*`, `ecarsi/agent/session.py`) while sessions are in flight. Fast-forwarding kills queued requests and sessions. Deploy pinned files only with zero running executions. For images, switch images only when `ops/count-wf.py` reports 0 running executions.

**Sessions.** New sessions use protocol 2 (portable history). Legacy protocol-1 restores fill `annotations: []` for every SDK version. A failed session restarts once (`-r2`, same evidence). A second failure skips the sample (label `unannotated`, needs_review `agent_skipped`) or the lineage (keeps cross-sample labels). Cross-sample sessions restart but never skip. If skipped cells exceed 10 % of the stage input (`SKIPPED_CELL_LIMIT`), the stage fails. Superseded sessions count as `superseded` in resume preflight. Crosssample and Zoomin workflows continue-as-new past 5 000 history events. Run `ops/replay-check.py` before you deploy any `control/` workflow change.

**Two zones.** A run's work tree (`output_root`, on scratch) holds everything the system needs to resume and replay it. Its display zone holds what Periscope shows: pages, stage reports, the release. The spec key `storage` (`{display_root, archive_root}`; `start-dataset` fills it from `~/.config/ecarsi/storage.json`) places the zone at `<display_root>/<collection>/<dataset>/<run_id>/`. After every stage, a small pool task (`dataset.display`, tool class) renders the run's pages and copies the files they need (`ecarsi/display.py`). When a dataset completes, the last sync also archives the whole work tree to `<archive_root>/<collection>/<dataset>/<run_id>.tar.gz` (`ecarsi/archive.py`). Syncs are submitted, not awaited; a failed sync never fails a run. The scratch work tree stays until the owner deletes it. Old runs were normalized the same way into `$OAK/eca-rsi/{display,work}` (`ops/display-zone.py`, `.tar.zst`).

**Pruner.** The script `container/request-pruner.py` deletes the pool requests of finished runs. Failed runs keep their requests and their Periscope row until a later run of the same dataset completes.

**Manual controls.** Both paths read `<unit>/loop_control.json` at every round boundary. This file controls `cap`, `rounds`, `extra_rounds_after_convergence`, `max_removed`, `pause`, `stop_after_round`, and `pause_after_stage: crosssample|zoomin`. A pause ends the unit workflow with a `PAUSED: …` non-retryable failure. To recover, clear the control and run `resume-dataset <run_id> --reason …`. Periscope shows PAUSED in the wait colour, not the failure colour.

**Runtime identity.** The function `runtime_identity()` hashes the computation packages (ecarsi except `ecarsi/ui/`, the kernels, the numeric stack). The control-plane path ignores this digest. Content-pinned inputs serve as its guard. `ECA_RSI_DEVELOPER_MODE=1` is a no-op there.

## Scientific rules

- **Stopping uses cell counts only.** Label changes are not a stopping criterion. With `--rounds N`, release after N rounds (N = 1 is allowed). Without `--rounds N`, release when condition A or condition B holds, and condition C holds. Condition A: this round removed < 1 % or < 100 cells. Condition B: three consecutive rounds removed < 2 %. Condition C: this round removed < 1 000 cells (`max_removed`). In automatic mode, round 1 never releases. The option `--cap` (default 15) forces a release and marks it.
- **Cumulative removal is not a risk.** Only the per-round policy gates a release. Do not flag total cell loss across rounds.
- **Every removal is accounted per cell.** Track removals in osp `qc_removed.csv`, msp `annotation_removed.csv`, and zmip `zmip_removed.csv`. The ledger aligns these files. The counts must match exactly.
- **Removal semantics.** OSP QC filters cells directly. OSP annotation keep/flag/drop provides advice only. MSP integrate and MSP inspect keep cells. MSP annotation applies preannotation, inspect drops, and agent removals. Surviving cells remain in `annotated.h5ad`. ZMIP applies local removals and outputs `annotated_zmip.h5ad`.
- **Zoom-in.** ZMIP plans lineages from labels and connectivity. The default minimum size is 800 cells. Small lineages keep their labels. Reassign changes labels without re-embedding in the target lineage. The host check `lineage_islands.csv` rejects merging separate islands into one lineage. Splitting one island requires `confirm_shared_islands: true` and lands in needs_review.
- **Single sample.** A single sample skips the inclusion agent. If inclusion leaves one sample, MSP skips Harmony. In that case, inspect and annotate ignore sample composition.
- **Biological doubts do not block release.** Send low confidence, inspect flags, sample exclusions, and reassignments to `release/needs_review.*`. Group these items by category: convergence, removed, sample_excluded, reassigned (with recurring items marked), inspect_flag, lineage_skipped, low_confidence, agent_skipped, and policy_excluded. Execution failures still fail the unit.
- **Boundary reviews.** MSP requires `boundary_reviews` for adjacent coarse-label pairs. ZMIP requires `shared_island_reviews` for same-island splits. A missing DEG or a fixed mixing percentage never forces a merge.
- **Sample-map policies** (`ecarsi/policies.py`): Policies include `exclude_cells` and `batch_key`. See [docs/front-integration.md](docs/front-integration.md). `MSP_BATCH_COL` overrides `batch_key`. A conflict causes an error.
- **Design context.** `ecarsi.design` derives the study design from obs and passes it as agent context only. The study design is not part of the run identity.
- **H5AD slimming.** The file `organized.h5ad` keeps expression in `layers["counts"]` only. `X` is an empty placeholder. Convert wide integer counts to int32. Do not include `.raw`. Store embeddings as float32.
- **Release and prune.** The script `release_state.py` builds the release in a staging directory and switches atomically. After release, the system prunes intermediate H5AD files. It creates `.pruned` markers and keeps `.obs.parquet` for the ledger. Option `--mirror DIR` copies light files to a long-term directory at every landing-page write. It copies all files at release.
- **Agent config changes are recorded, not blocked.** A backend or model change during a run is written to progress.log and to needs_review under `agent_config_changed`.

## Environment variables

| Variable | Meaning |
|---|---|
| `HARNESS` | The options are `openai`, `claude`, and `deepseek`. The default value is `openai`. This option uses the OpenAI Agents SDK on Ark/Doubao. |
| `MODEL` | Specifies the model. The default model per backend is `doubao-seed-2-1-turbo-260628` / `claude-sonnet-5`. |
| `ARK_API_KEY` | Specifies the Ark key for the openai backend. |
| `OPENAI_AGENTS_API` | The options are `responses` and `chat_completions`. The default value is `responses`. |
| `OPENAI_AGENTS_MAX_NUDGES`, `OPENAI_AGENTS_MAX_CONTEXT_RESETS` | The default value is 2 for each variable. |
| `OPENAI_AGENTS_SERVER_STATE` | The default value is 1. This value enables incremental Responses continuation. |
| `AGENT_WALL_MIN` | Specifies the wall-clock budget per agent run on the local path (the bridge harnesses). The default value is 180 min. The control-plane path does not read it: there a session is bounded by `max_turns` × the per-turn response timeout (bridge `response_timeout_seconds`, 900 s). |
| `AGENT_MODEL_POOL` | Specifies an ordered fallback list of `harness:model,...`. Set `AGENT_MODEL_POOL_ROTATE=1` to rotate the start per subprocess. This setting is off by default. |
| `PERSAMPLE_PARALLEL`, `PERSAMPLE_MEM_PER_CELL_MB`, `ZMIP_PARALLEL` | Configures local-path concurrency. The control-plane path budgets per request instead. |
| `MSP_BATCH_COL` | Specifies the explicit batch column. Keep this value constant within each OSP experiment. |
| `MSP_COMPUTE_ENDPOINT` | The options are `local|dask-local|dask`. The control-plane path pins `local`. |
| `ZMIP_MIN_CELLS` | Specifies the lineage zoom-in threshold. The default value is 800. |
| `ECA_RSI_PAUSE_FILE` | Specifies the shared pause-request file for SIGTERM handling. |
| `ECA_RSI_DEVELOPER_MODE` | Skips the runtime-identity comparison on the local path. This variable has no effect on the control-plane path. |
| `MSP_PYTHON`, `ZMIP_PYTHON`, `DSH_BIN` | Specifies the kernel interpreters and the DeepSeek harness binary for the local path. |

## Versions

The current combination includes ecarsi 0.3.2, agent-harness-bridge 0.2.15, OSP 0.1.7, MSP 0.5.2, ZMIP 0.3.9, and standissect-lite 0.2.0. The combination also includes openai-agents 0.22.3 and claude-agent-sdk 0.2.163. HQ is the owner's patched fork on branch `local`. The authoritative lists are [INSTALL.md](INSTALL.md) and `container/control-requirements.lock`.

## Targeted checks

```bash
# front half only; no MSP/ZMIP needed
python -m pytest -q tests/test_front_integration.py tests/test_osp_worker.py tests/test_agent_selection.py
# downstream integration, cell conservation, release recovery, shared-object identity
python -m pytest -q tests/test_downstream.py tests/test_downstream_state.py tests/test_ledger_conservation.py tests/test_release_state.py tests/test_crosssample_cwd.py tests/test_harness_sync.py
```

Run the tests through `ops/runsci-dev.sh` to resolve imports. `tests/conftest.py` restores `os.environ` around each test.

## History

- Branch `primitive` holds the six-step prompt loop: Explore → Compute → Annotate → QC → Apply → Stop. In this loop, agents write their own analysis code. Directory [docs/history/primitive/](docs/history/primitive/) holds its `run.sh` and `steps/*.md`. The main line does not include its removal budgets and its `docs -> ../eca-cycle/docs` link.
- `attic-v01` (schema + lint + skill, August 2026) was never in git. It is on Oak under `$OAK/eca-rsi/work/_batches/attic-v01/`, with the other archives listed in `$OAK/eca-rsi/INDEX.tsv`.
- The project retired several rules. It no longer makes the main checkouts read-only during batches, because production now runs from images. Version 0.3.2 removed the Dask pool and `compute_policy`. The project retired batch admission before 0.3.0 (`eca-rsi batch`, node agents). It also retired the keeper autoscaler.
- Dated records, acceptance reports, and the operational to-do live in [docs/history/](docs/history/) and in the deployment directory's `OPEN-ITEMS.md`.

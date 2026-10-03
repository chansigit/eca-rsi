# ECA-RSI control-plane path: architecture

The control plane is the only way ECA-RSI runs: Temporal workflows, a HyperQueue warm pool, and a model-turn service, processing many datasets at once (formerly the `gen2` branch). The local path (`eca-rsi run`, one process per dataset) was removed in 0.4.0. Module names follow the code. Start with [OVERVIEW.md](../OVERVIEW.md); the reasons behind the design are in [decisions/](../decisions/README.md).

## Layers

```
Operations        container/control-plane.sh (launcher, extracted from the control image)
                  container/worker-node.sh (a Slurm job that is itself a pool worker)
                  ops scripts in the deployment directory (image build, image switch, Periscope start)
──────────────────────────────────────────────────────────────────────────────────────
Control           ecarsi.control     Temporal workflow tree: dataset → unit → persample / crosssample / zoomin → agent
──────────────────────────────────────────────────────────────────────────────────────
Model turns       ecarsi.agent       durable inbox for model turns: session contract, dispatch, parallel reads,
                                     resident runners (one process per catalog model)
Warm pool         ecarsi.warm_pool   bounded compute requests: state (file protocol), backend (HyperQueue),
                                     worker, allocation, provision, budget
──────────────────────────────────────────────────────────────────────────────────────
Stage programs    ecarsi.stages      organize / persample / crosssample / zoomin / release. They run in the
                                     compute image, wrap the kernels and validate every model proposal on the host.
                                     contract is the shared model contract. evidence / execution plan the tool work.
──────────────────────────────────────────────────────────────────────────────────────
Kernels           osp · msp · zmip · standissect-lite        Model runtime: agent-harness-bridge (`harness_bridge`)
──────────────────────────────────────────────────────────────────────────────────────
Observation       Periscope (`ecarsi.ui.serve`), with the control-plane page at `/_control/`;
                  `ecarsi.observatory` provides that page and the `status` / `tokens` reports
```

Dependencies point downward. `control` calls `agent` and `warm_pool`. `agent` calls `warm_pool`. `stages` depends only on the file primitives of `warm_pool.state` and on library functions of the package. `stages` never imports `control` or `agent`. When `agent` needs a stage execution plan, it imports the `planner` module named in the session.

## Images

Everything runs from two Apptainer images.

| Image | File | Contents | Runs |
|---|---|---|---|
| Control image | `rsi-control-<stamp>.sif` | Temporal, PostgreSQL, HQ, the control Python environment, a snapshot of eca-rsi at `/opt/eca-rsi` | temporal, hq, scheduler, bridge, runners, coordinators, fleet-status, pruner |
| Compute image | `rsi-science-<stamp>.sif` | the kernels and numerical stack, HQ, the same eca-rsi snapshot | pool workers, Periscope |

Set `CODE=<checkout>` in the launcher to put a checkout first on `PYTHONPATH`. This setting shadows the snapshot for development. Production code changes reach the plane only through a rebuilt image. See [container/README.md](../../container/README.md).

## Module map

| Package | Module | Role |
|---|---|---|
| `ecarsi.control` | `coordinator` | activities, `AgentWorkflow`, CLI (`worker`, `start-*`, `resume-*`, `status-*`), poll retry |
| | `temporal` | hosts Temporal Server and PostgreSQL from the control image; publishes `service.json` |
| | `dataset` `persample` `crosssample` `zoomin` | stage workflows |
| `ecarsi.agent` | `__init__` | handles submit, status, serve, and reconcile of turn requests; `adapter_path` names the pinned host code |
| | `dispatch` | sends a turn to the pool or to a resident runner; finished-cache keyed by inode |
| | `runner` | resident model-turn runners, one process per catalog model |
| | `session` | session contract: create, reset, validate_turn, and continuation; stops after repeated rejections |
| | `parallel` `tool_errors` | parallel read-only tools, argument rejection |
| `ecarsi.warm_pool` | `state` `backend` `worker` `allocation` `provision` | file protocol (`reference` / `verified` / `immutable`), HQ adapter, worker, Slurm probe, `add-worker` |
| | `budget` `reservation` `measure` | budgets from measured runs, measured ceilings, half-hourly `measured.json` |
| `ecarsi.stages` | `organize` `persample` `crosssample` `zoomin` | host programs: compute, validate, publish |
| | `release` | dataset release (ledger, review, UMAP data) |
| | `contract` | shared model contract: tools without parameter lists, `deg_lookup` thresholds, lenient JSON, checklists |
| | `evidence` `execution` | execution plans on the pool: evidence batch reads, single-tool plans, measured budgets |
| `ecarsi` | `observatory` (+ `.html`) | control-plane page, timeline, `status` and `tokens` reports |
| | `round_policy` | round stopping rules and `loop_control.json` |
| | `prompts/` | prompts and checklists of the agent sessions |

## Entry points

```bash
python -m ecarsi.control.temporal --root <control> --postgres-bin … --temporal-dir … --schema-dir … --bind <ip>
python -m ecarsi.warm_pool --root <pool> hq-server --host <node>          # HyperQueue server, apart from the scheduler
python -m ecarsi.warm_pool --root <pool> scheduler --host <node>          # releases requests to HQ
python -m ecarsi.agent serve <bridge>                                      # model-turn service (the directory is still called bridge)
python -m ecarsi.agent runners <bridge>                                    # resident runners, one per catalog model
python -m ecarsi.control --service-root <control> --task-queue <q> worker  # coordinator (several may run)
python -m ecarsi.control --service-root <control> --task-queue <q> start-dataset|resume-dataset|status-dataset <run_id>
python -m ecarsi serve --control-plane <base> --control-pool-root … --control-bridge-root … --control-temporal-root …   # Periscope
```

`container/control-plane.sh start|stop|restart|status|report|tokens` wraps these commands. Start Periscope from the compute image with `ops/start-periscope.sh` in the deployment directory.

Workers join in one of two ways:

- `container/worker-node.sh` runs as the body of a Slurm job. The job itself is the worker (`warm_pool slurm-worker`).
- Run `python -m ecarsi.warm_pool --root <pool> add-worker <host> --job-id <job>` for an allocation that already exists.

The owner requests the nodes. The system has no autoscaler and no keeper.

## Invariants

- **Pin by content.** Every request lists the program files that it depends on in `inputs` (`stages.program()`). A session records the hash of `agent/session.py` as `adapter_path`. Do not change these files while a session is in flight. Change the contract only when no session is in flight.
- **The request id is the replay key.** A request id that already exists in the pool or the bridge replays the stored content. The system records differences only in `resubmitted.json`.
- **Polling only.** The pool and the bridge use file protocols. The coordinator polls through activities and tolerates about 35 minutes of poll failures. Do not scan the request directories without pacing on the control-plane node. The control-plane node shares the Lustre client with the coordinators.
- **Deployment parameters stay out of the package.** `~/.config/ecarsi/deployment.env`, the dataset specs and the service configs define nodes, images, paths, and concurrency limits ([decision 0011](../decisions/0011-settings-in-one-directory.md)).

## Runtime mechanisms at a glance

| Mechanism | What it does | Code |
|---|---|---|
| HQ priority | Every request receives a native HQ priority. This priority equals the class base (agent 1000, tool 800, work 0) plus 10 × cpus. HQ orders the queue. The system has no hold, drain, or backlog layer. | `warm_pool/backend.py`: `PRIORITY_BASE`, `hq_priority` |
| Feasibility gate | The gate does not submit a request that no live worker can hold. The gate checks cpus, memory, a required GPU, and the worker's remaining time against `time_request_seconds`. The system marks the request as `infeasible: <reason>`. The system retries the request every tick. | `warm_pool/backend.py`: `worker_capacity`, `infeasible` |
| DEG batching | One pool request runs up to 8 DEG comparisons (`deg-batch`). The request timeout is twice the per-comparison budget. Both cross-sample and zoom-in stages batch comparisons. | `control/persample.py`: `DEG_BATCH_SIZE`; `stages/crosssample.py`: `deg_batch` |
| Session restart and skip | A failed agent session restarts once as a new session (`-r2`, same evidence). A second failure skips the sample or lineage. A stage fails when skipped cells exceed 10 % of its input. | `control/coordinator.py`: `run_agent`; `control/persample.py`: `SKIPPED_CELL_LIMIT` |
| Continue-as-new | Cross-sample and zoom-in workflows continue as new past 5,000 history events. These workflows carry finished results. | `control/persample.py`: `HISTORY_LIMIT`; `control/zoomin.py`, `control/crosssample.py` |
| Resident runners | One process per catalog model keeps many turns in flight in one event loop. The bridge configuration `service.models` lists the models. An empty list disables runners. | `agent/runner.py`, `agent/dispatch.py`: `runner_ready` |
| Pruner | The pruner deletes the pool requests of finished dataset runs. A worker executes this deletion as a pool task. | `container/request-pruner.py`; `warm_pool/state.py`: `prune_list` |
| Pinned-file rule | Deploying a changed stage file fails queued requests and in-flight sessions that pinned the old content. Deploy changes only when zero executions run. | `stages/__init__.py`: `program` |
| Manual controls | The system reads `<unit>/loop_control.json` at every round boundary: `cap`, `rounds`, `extra_rounds_after_convergence`, `max_removed`, `pause`, `stop_after_round`, `pause_after_stage`. A pause ends the unit workflow with a `PAUSED` non-retryable failure. Resume execution with `resume-dataset`. | `round_policy.py`: `read_control`; `control/dataset.py` |

## History

The control-plane path was developed on the `gen2` branch between 2026-09-14 and 2026-09-17 and merged into `main` on 2026-09-18. Acceptance records and the original design drafts are in [docs/history/](../history/).

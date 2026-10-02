# Install and run

ECA-RSI runs from two Apptainer images. It installs nothing on the host. Part A is the default. Use Part A to deploy the images and start the control-plane path. Use Part B for development only.

See [README.md](README.md) to learn what the system does and how to read its results. See [docs/control-plane/](docs/control-plane/ARCHITECTURE.md) for the control-plane design.

## A. Deployment

### A.1 Prerequisites

- You need a Linux cluster with Apptainer, Slurm, and a shared filesystem. Sherlock is the reference site.
- Reserve one long-running Slurm allocation for the control plane (the "plane node"). Reserve approximately 13 CPUs and 96 GB. The four coordinators use 6–7 GB each.
- The owner must request Slurm allocations for workers. Nothing requests nodes automatically.
- Use host Python 3.11 or newer. Only the worker launcher uses host Python. The host runs `scontrol`, `nvidia-smi`, and `ssh`.
- Set model credentials in the shell environment of the plane node. For example, set `ARK_API_KEY` for Doubao through Volcengine Ark.

### A.2 Images

| Image | File | Carries |
|---|---|---|
| control image | `rsi-control-<stamp>.sif` (~250 MB) | the control Python environment (`/opt/rsi-control`), Temporal 1.32.0 and PostgreSQL 16.15 (`/opt/rsi-services/{temporal,postgres}`), HyperQueue (`/opt/rsi-bin/hq`), and a snapshot of eca-rsi (`/opt/eca-rsi`, with `BUILD.json`) |
| compute image | `rsi-science-<stamp>.sif` (~3.9 GB) | the kernels and numerical stack (`/opt/rsi-python`, Python 3.12.14), `/opt/rsi-control`, HyperQueue, and the same eca-rsi snapshot. Workers and Periscope run here |

Keep the images on a non-purged filesystem, for example `$GROUP_HOME/<user>/eca/images/`. Use one `<stamp>` for a pair. The HyperQueue binary is the owner's patched build (branch `local` of `chansigit/hyperqueue`).

The file `container/control-requirements.lock` defines the pinned Python environment of the control image. The package versions inside the images are:

| Package | Version |
|---|---|
| ecarsi | 0.3.2 |
| agent-harness-bridge | 0.2.15 |
| osp-sc (`osp`) | 0.1.7 |
| msp-sc (`msp`) | 0.5.2 |
| zmip | 0.3.9 |
| standissect-lite | 0.2.0 |
| openai-agents / openai | 0.22.3 / 3.23.0 |
| claude-agent-sdk | 0.2.163 (bundles Claude Code 2.1.286) |
| temporalio | 1.32.0 |

`BUILD.json` inside each image names the eca-rsi commit. The command `control-plane.sh start` logs it to `control-logs/identity.log`.

### A.3 Directory layout

Store runtime state on a shared, writable filesystem. Purged scratch is acceptable because you copy out results at release.

```text
$GROUP_SCRATCH/<user>/eca/
  control/            # control-plane.sh (deployment copy), durable-control/ (Temporal + PostgreSQL), control-logs/, image-code/, ops/
  pool/               # warm pool: config.json, requests/, hq/, worker-state/
  bridge/             # model-turn service: requests/
  runs/               # dataset run directories
```

Rules:

- You must own every directory with mode `0700`. The pool and bridge refuse to start otherwise.
- Set `control/pool` and `control/bridge` as symlinks to `../pool` and `../bridge`.
- Create `bridge/requests` before you start the bridge.

```bash
E=$GROUP_SCRATCH/$USER/eca
mkdir -p $E/control $E/pool $E/bridge/requests $E/runs
chmod 700 $E $E/control $E/pool $E/bridge $E/bridge/requests $E/runs
ln -s ../pool $E/control/pool; ln -s ../bridge $E/control/bridge
```

### A.4 The launcher

The launcher lives inside the control image. The deployment copy only exports paths and runs it:

```bash
# $E/control/control-plane.sh
export BASE=$E/control
export IMG=$GROUP_HOME/$USER/eca/images/rsi-control-<stamp>.sif
export SCIENCE_IMG=$GROUP_HOME/$USER/eca/images/rsi-science-<stamp>.sif
export CONTROL=$BASE/durable-control POOL=$E/pool BRIDGE=$E/bridge
export HOSTPY=/path/to/host/python3                       # host Python 3.11+ for the worker launcher
export TEMPORAL_PORT=7633 DATABASE_PORT=55433 UI_PORT=8633 OBSERVATORY_PORT=8766   # only when another plane shares the host
apptainer exec "$IMG" cat /opt/eca-rsi/container/control-plane.sh > "$BASE/.control-plane.from-image.sh"
exec bash "$BASE/.control-plane.from-image.sh" "$@"
```

You can set optional variables: `COORDINATORS` (default 4), `STAGE_LIMIT_FLOORS` (JSON, for example `{"max_in_flight_deg": 12, "max_in_flight_lineages": 6}`), `TEMPORAL_DYNAMIC_CONFIG` (a Temporal dynamic-config YAML), and `BINDS` (default `/scratch,/oak,/home,/lscratch`). Do not set `CODE` in a deployment. See B.2.

### A.5 Initialise the pool

Run both commands inside the compute image with the image Python. `init` records the HyperQueue binary and the interpreter. `configure-runtime` validates the imports and pins the compute image for new requests.

```bash
SCI=$GROUP_HOME/$USER/eca/images/rsi-science-<stamp>.sif
run_sci() { apptainer exec --cleanenv --bind /scratch,/oak,/home,/lscratch --env PYTHONSAFEPATH=1 \
  --env PYTHONPATH=/opt/eca-rsi:/opt/rsi-control:/opt/rsi-python "$SCI" /usr/local/bin/python3.12 "$@"; }

run_sci -m ecarsi.warm_pool --root $E/pool init --hq /opt/rsi-bin/hq --runtime /usr/local/bin/python3.12
run_sci -m ecarsi.warm_pool --root $E/pool configure-runtime $E/control/pool-runtime-current.json
```

`pool-runtime-current.json` contains the pool `runtime` record and an `image` entry with the path and sha256 of the compute image. See `ops/switch-images.sh` for the lines that write it. `configure-runtime` fails if it does not run inside the image it names.

### A.6 Start the control plane

```bash
cd /tmp
bash $E/control/control-plane.sh start temporal hq scheduler bridge runners coordinators fleet-status pruner
bash $E/control/control-plane.sh status
```

| Component | Role |
|---|---|
| temporal | Temporal server on PostgreSQL, from `/opt/rsi-services` |
| hq | HyperQueue server. It runs apart from the scheduler, so scheduler restarts keep workers connected |
| scheduler | warm-pool scheduler: releases requests to HQ with native priorities and the feasibility gate |
| bridge | model-turn service (`ecarsi.agent serve`) |
| runners | resident model runners (`ecarsi.agent runners`) |
| coordinators | Temporal workers for the dataset, unit, and stage workflows |
| fleet-status | writes `fleet-status.json` for Periscope |
| pruner | deletes the pool requests of finished runs |

The first start on a new state directory initialises PostgreSQL and the Temporal schema. A later start refuses a state directory whose recorded binaries changed. Move `durable-control/runtime.json` aside after a deliberate image change.

Start Periscope from the compute image. Pass the four roots so its `/_control/` page sees the plane:

```bash
cd /tmp && APPTAINERENV_APPEND_PATH=$HOME/local/bin setsid nohup apptainer exec --cleanenv --bind /scratch,/oak,/home,/lscratch \
  --env PYTHONSAFEPATH=1 --env PYTHONPATH=/opt/eca-rsi:/opt/rsi-control:/opt/rsi-python \
  "$SCI" /usr/local/bin/python3.12 -m ecarsi serve --port 8899 \
  --control-plane $E/control --control-pool-root $E/pool --control-bridge-root $E/bridge --control-temporal-root $E/control/durable-control \
  > $SCRATCH/serve-8899.log 2>&1 < /dev/null &
```

Add `--ngrok --domain <reserved domain>` for a public tunnel. `APPTAINERENV_APPEND_PATH` makes your `ngrok` binary visible inside the image.

### A.7 Add workers

Workers are Slurm jobs. You can join a worker in two ways.

**A job that is the worker.** `control-plane.sh start` unpacks the image's eca-rsi into `$BASE/image-code`. Submit its worker script:

```bash
POOL=$E/pool SCIENCE_IMG=$SCI HOSTPY=/path/to/host/python3 \
  sbatch --time=8:00:00 --cpus-per-task=16 --mem=32G $E/control/image-code/container/worker-node.sh
```

The job takes every granted core and 90 % of the memory. It joins the HQ server and leaves when the job ends.

**An allocation that already exists.** Run `add-worker` from the plane node with the host Python:

```bash
PYTHONPATH=$E/control/image-code /path/to/host/python3 -m ecarsi.warm_pool --root $E/pool \
  add-worker <hostname> --job-id <slurm job id> --wait-seconds 300
```

Omit `<hostname>` when you run it inside the allocation. Add `--cpus 8,12,14` and `--memory-mb 24576` to give the worker a subset of the allocation.

Workers advertise the digest of the compute image they run. HQ places a request only on a worker with the same digest. That worker must have enough time left for the request's `time_request`.

### A.8 Verify

```bash
bash $E/control/control-plane.sh report --sessions 2        # plane components, workers, pool queue, datasets, sessions
apptainer exec "$IMG" /opt/rsi-bin/hq --server-dir $E/pool/hq worker list
```

Every worker must show `RUNNING` with the same `runtime/<digest>` resource. Periscope's `/_control/` page shows the same data.

### A.9 Switch to a new image pair

Switch only when the report shows `running executions: 0`. The script `ops/switch-images.sh <stamp> <compute image sha256> <host:jobid>...` executes these steps in order:

1. Stop every worker supervisor. Send SIGTERM to the `pid` in `pool/worker-state/*/worker.json`.
2. Stop the plane.
3. Write `pool-runtime-current.json` for the new compute image and run `configure-runtime` inside it.
4. Repoint the launcher, Periscope, and helper scripts to the new stamp.
5. Start the plane and Periscope.
6. Re-add the workers.

Keep the previous pair until the next end-to-end run passes.

## B. Development

### B.1 Checkouts

Store the source repositories next to each other on a non-purged filesystem:

```text
$GROUP_HOME/<user>/eca/
  images/
  src/{eca-rsi,osp,msp,zmip,agent-harness-bridge,standissect-lite}   # main checkouts
  worktrees/eca-rsi-dev                                              # development worktree, branch dev
```

Develop in the `dev` worktree. Fast-forward `main` after the tests pass. Do not edit code that a running plane imports.

### B.2 Run a checkout instead of the image snapshot

Set `CODE=<checkout>` in the deployment copy of the launcher. The checkout comes first on `PYTHONPATH`. It shadows `/opt/eca-rsi` in both images. `control-plane.sh identity` prints which one runs. Use this setting on a test plane only. Production runs from the snapshot.

The pool pins program files by content into every request. A changed stage file (`ecarsi/stages/*`, `ecarsi/agent/session.py`) breaks queued requests and in-flight sessions. Deploy such changes only when 0 executions are running.

### B.3 Run the tests

The tests run inside the images. You do not need a host environment. The images do not contain pytest. Install it once with `pip install --target $GROUP_HOME/<user>/pytest-only pytest`. Put that directory on `PYTHONPATH`.

```bash
cd $GROUP_HOME/$USER/eca/worktrees/eca-rsi-dev
SIB=$GROUP_HOME/$USER/eca/src
apptainer exec --cleanenv --bind /scratch,/oak,/home,/lscratch --env LC_ALL=C --env LANG=C --env PYTHONDONTWRITEBYTECODE=1 \
  --env "PYTHONPATH=$PWD:/opt/rsi-control:$GROUP_HOME/$USER/pytest-only" \
  --env "ECA_SIBLINGS=$SIB/msp:$SIB/osp:$SIB/zmip:$SIB/agent-harness-bridge" \
  "$SCI" /usr/local/bin/python3 -m pytest -q -p no:cacheprovider tests
```

Use the compute image for the full suite. The control image lacks the numerical stack. Therefore, only the control and warm-pool tests collect there. Run Python from `/tmp` or with `PYTHONSAFEPATH=1`. This prevents a checkout in the working directory from shadowing the image's packages.

For pure documentation changes, run `git diff --check` and a link check. You do not need to run tests.

### B.4 Editable installs

Use an editable install only for IDEs and quick experiments on a host with Python 3.10 or newer:

```bash
python -m pip install -e $GROUP_HOME/$USER/eca/src/eca-rsi --no-deps
```

Re-run it after every version bump. Otherwise, `runtime_identity()` reads the old version from `importlib.metadata`. The kernels are separate packages. Install each kernel the same way with `pip install -e`. Dependency ranges are in `pyproject.toml`. The images contain the versions that are tested together (A.2).

### B.5 Rebuild the images

`ops/build-images-update.sh` updates the current pair in place:

```bash
FROM=<old stamp> STAMP=<new stamp> [WHEELS=<dir of wheels>] bash ops/build-images-update.sh
```

It extracts both images into sandboxes on node-local disk. It replaces `/opt/eca-rsi` with `git archive HEAD` of the `dev` worktree. It writes `BUILD.json`. Optionally, it swaps Python distributions by their `RECORD` files. Then it packs each sandbox. The checkout must be clean.

Pack by hand. `apptainer build` from a sandbox segfaults in its mksquashfs step on the compute sandbox. The script runs `mksquashfs ... -processors 4`. Then it runs `apptainer sif new` and `apptainer sif add --datatype 4 --parttype 2 --partfs 1 --partarch 2 --groupid 1`.

A full rebuild (`ops/build-images-20261001.sh`) downloads Temporal and PostgreSQL. It installs `container/control-requirements.lock` with `--require-hashes`. Record the sha256 of every new image. Then switch (A.9). Run an end-to-end regression on a small dataset.

### B.6 Source repositories

[eca-rsi](https://github.com/chansigit/eca-rsi),
[osp](https://github.com/chansigit/osp),
[msp](https://github.com/chansigit/msp),
[zmip](https://github.com/chansigit/zmip),
[agent-harness-bridge](https://github.com/chansigit/agent-harness-bridge),
[standissect-lite](https://github.com/chansigit/standissect-lite).

## C. Run-time configuration

Agent backends:

| Variable | Meaning / default |
|---|---|
| `HARNESS` / `--harness` | `openai` (default, Doubao through Ark), `openai@ark`, `openai@openrouter`, `openai@vllm`, `claude`, `deepseek` |
| `MODEL` / `--model` | The CLI option overrides the environment variable. Default for openai and deepseek is `doubao-seed-2-1-turbo-260628`. Default for claude is `claude-sonnet-5`. |
| `ARK_API_KEY` / `DOUBAO_BASE_URL` | Ark key and endpoint. The default endpoint is the Beijing region `/api/v3`. |
| `OPENROUTER_API_KEY` / `OPENROUTER_BASE_URL` | OpenRouter key and endpoint. The default endpoint is `https://openrouter.ai/api/v1`. |
| `VLLM_API_KEY` / `VLLM_BASE_URL` | vLLM key and endpoint. The default endpoint is `http://127.0.0.1:8000/v1`. `MODEL` must match the served name. |
| `OPENAI_AGENTS_API` | API type: `responses` (default) or `chat_completions` (text only). |
| `OPENAI_AGENTS_MAX_NUDGES` | Reminders in the same history when the agent does not submit. The default is 2. |
| `OPENAI_AGENTS_MAX_CONTEXT_RESETS` | Recoveries from an over-long context. The default is 2. |
| `OPENAI_AGENTS_SERVER_STATE` | The default is 1. When set to 1, Responses continues with `previous_response_id`. |
| `AGENT_WALL_MIN` | Wall-clock budget for one agent call. The default is 180 minutes. |
| `AGENT_MODEL_POOL` / `AGENT_MODEL_POOL_ROTATE` | Ordered fallback list formatted as `harness:model,...`. Set `ROTATE=1` to stagger the start model per subprocess. |
| `DSH_BIN` | The dsh binary for `HARNESS=deepseek`. |

Kernels and concurrency:

| Variable | Meaning / default |
|---|---|
| `OSP_PYTHON` / `MSP_PYTHON` / `ZMIP_PYTHON` | Kernel interpreters. The default is the current interpreter. `ZMIP_PYTHON` falls back to `MSP_PYTHON`. |
| `PERSAMPLE_PARALLEL` / `PERSAMPLE_MEM_PER_CELL_MB` | Per-sample concurrency and memory estimate. Defaults derive from available CPUs and memory. |
| `ZMIP_PARALLEL` | Lineage concurrency. A value of 1 runs lineages in sequence. |
| `ZMIP_MIN_CELLS` | Smallest lineage to zoom into. The default is 800. |
| `MSP_BATCH_COL` | Harmony correction column. The default is `eca_sample_id`. This value must be constant within every OSP experiment. |
| `MSP_N_PCS` / `MSP_N_TOP_GENES` / `MSP_N_NEIGHBORS` | Integration parameters for every round. |
| `MSP_RESOLUTIONS` / `ZMIP_RESOLUTIONS` | Space-separated or comma-separated values. You must include 1 and 2. |
| `MSP_HARMONY` / `ZMIP_HARMONY` | JSON object, for example `{"theta": 1}`. This parameter is part of the run identity. |
| `MSP_LANGUAGE` / `ZMIP_LANGUAGE` | Report prose language. The default is `English`. Labels stay English. |
| `MSP_EFFORT` / `ZMIP_EFFORT` / `MSP_MAX_TURNS` / `ZMIP_MAX_TURNS` | Agent reasoning and budget settings. |
| `MSP_COMPUTE_ENDPOINT` | The control-plane path pins this value to `local`. |
| `ECA_RSI_DEVELOPER_MODE=1` | Local path only: skip RSI's source-digest comparison and record `runtime_check: skipped`. The control-plane path ignores this setting. Its guard is the content pin of every request. |

## D. Submit a dataset

The control-plane path admits datasets through the coordinators. A dataset spec names the ECA-PP input, the run directory under `runs/`, the budgets, and the round policy. `examples/dataset-v2.json` provides a complete example. `docs/control-plane/DATASET_V2.md` explains the fields.

```bash
cd /tmp
apptainer exec --cleanenv --bind /scratch,/oak,/home,/lscratch --env PYTHONSAFEPATH=1 --env PYTHONPATH=/opt/eca-rsi:/opt/rsi-control \
  "$IMG" /usr/local/bin/python3 -m ecarsi.control --service-root $E/control/durable-control --task-queue ecarsi-durable-v2 start-dataset dataset.json
# later: status-dataset RUN_ID, resume-dataset RUN_ID --reason "..."
```

Follow progress on Periscope. A unit that stops on `loop_control.json` (`pause`, `stop_after_round`, `pause_after_stage`) ends as `PAUSED`. Clear the control. Then run `resume-dataset` on the unit.

The local path (`eca-rsi run <eca-pp output> <root>`) is not maintained. Its CLI still exists for single-machine experiments. README.md documents its semantics because the control-plane path shares them. These shared semantics include round policy, `loop_control.json`, release layout, and `needs_review`.

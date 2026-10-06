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
| compute image | `rsi-science-<stamp>.sif` (~3.9 GB) | the numerical stack (`/opt/rsi-python`, Python 3.12.14), `/opt/rsi-control`, HyperQueue, and the same eca-rsi snapshot, which carries the kernels (decision 0018). Workers and Periscope run here |

Keep the images on a non-purged filesystem, for example `$GROUP_HOME/<user>/eca/images/`. Use one `<stamp>` for a pair. The HyperQueue binary is the owner's patched build (branch `local` of `chansigit/hyperqueue`).

The files `container/control-requirements.lock` and `container/science-requirements.lock` define the pinned Python environments of the two images (`/opt/rsi-control`, `/opt/rsi-python`). The package versions inside the images are:

| Package | Version |
|---|---|
| ecarsi, with `osp`, `msp`, `zmip`, `standissect_lite`, `harness_bridge` (one repository, decision 0018) | 0.4.5 |
| openai-agents / openai | 0.22.3 / 3.23.0 |
| claude-agent-sdk | 0.2.163 (bundles Claude Code 2.1.286) |
| temporalio | 1.32.0 |

`BUILD.json` inside each image names the eca-rsi commit. The command `control-plane.sh start` logs it to `control-logs/identity.log`.

### A.3 Directory layout

Store runtime state on a shared, writable filesystem. Purged scratch is acceptable because you copy out results at release.

```text
$GROUP_SCRATCH/<user>/eca/
  control/            # ops/ and control-plane.sh (links into the eca-rsi checkout), durable-control/ (Temporal + PostgreSQL), control-logs/, image-code/
  pool/               # warm pool: config.json, requests/, hq/, worker-state/
  bridge/             # model-turn service: requests/
  runs/               # dataset run directories
```

Rules:

- You must own every directory with mode `0700`. The pool and bridge refuse to start otherwise.
- Set `control/pool` and `control/bridge` as symlinks to `../pool` and `../bridge`.
- Link the deployment scripts from the checkout: `ln -s <eca-rsi checkout>/ops control/ops` and `ln -s ops/control-plane.sh control/control-plane.sh`. [ops/README.md](ops/README.md) lists them.
- Create `bridge/requests` before you start the bridge.

```bash
E=$GROUP_SCRATCH/$USER/eca
mkdir -p $E/control $E/pool $E/bridge/requests $E/runs
chmod 700 $E $E/control $E/pool $E/bridge $E/bridge/requests $E/runs
ln -s ../pool $E/control/pool; ln -s ../bridge $E/control/bridge
```

### A.4 Configuration and the launcher

Every setting you edit lives in `~/.config/ecarsi/`:

| File | What it holds | Written by |
|---|---|---|
| `deployment.env` | This machine: the two images, the state directory, the host directories the containers see (`BINDS`), the host Python, ports, Periscope's port | you; `ops/switch-images.sh` rewrites the two image lines |
| `results.json` | `display_root` and `archive_root` for new runs; `more_display_roots` that Periscope shows as well | you |
| `models.json` | The model catalog: harness, model and URL, in calling order. No keys | you, or Periscope's model page |
| `periscope-datasets.json` | Runs Periscope shows besides the display zones, `{name: run dir}` | Periscope (Bind / Unbind), or you |
| `temporal.yaml` | Temporal dynamic config: workflow task timeout, history limits | you |
| `periscope-password` | `user:pass` for Periscope (mode 600) | you |
| `models-admin-key` | The key for edits on Periscope's model page (mode 600). Not a model API key | you |
| `gate-dataset.json` | The release gate's dataset: a spec without `run_id`, `output_root`, `dataset_id`, with test `storage` roots (`ops/gate.py`) | you |

Model API keys (`ARK_API_KEY`, ...) stay in `~/.bashrc`; the runners read them from there. Two more files belong to their services and are written by tools: `pool/config.json` (`warm_pool init`, `configure-runtime`) and `bridge/config.json` (`ecarsi.agent init`). To move to another machine, edit `deployment.env` and `results.json`, then initialise the pool (A.5) and the bridge there. Nothing else names a machine path.

`examples/deployment.env` is the template. The launcher lives inside the control image; `ops/control-plane.sh` reads `deployment.env` and runs it:

```bash
# ops/control-plane.sh
set -a; . ~/.config/ecarsi/deployment.env; set +a
apptainer exec "$IMG" cat /opt/eca-rsi/container/control-plane.sh > "$BASE/.control-plane.from-image.sh"
exec bash "$BASE/.control-plane.from-image.sh" "$@"
```

`BINDS` is required. `COORDINATORS` (default 4) and `STAGE_LIMIT_FLOORS` are optional. Do not set `CODE` in a deployment. See B.2.

### A.5 Initialise the pool

Run both commands inside the compute image with the image Python. `init` records the HyperQueue binary and the interpreter. `configure-runtime` validates the imports and pins the compute image for new requests.

```bash
set -a; . ~/.config/ecarsi/deployment.env; set +a
run_sci() { apptainer exec --cleanenv --bind "$BINDS" --env PYTHONSAFEPATH=1 \
  --env PYTHONPATH=/opt/eca-rsi:/opt/rsi-control:/opt/rsi-python "$SCIENCE_IMG" /usr/local/bin/python3.12 "$@"; }

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
set -a; . ~/.config/ecarsi/deployment.env; set +a
cd /tmp && APPTAINERENV_APPEND_PATH=$HOME/local/bin setsid nohup apptainer exec --cleanenv --bind "$BINDS" \
  --env PYTHONSAFEPATH=1 --env PYTHONPATH=/opt/eca-rsi:/opt/rsi-control:/opt/rsi-python \
  "$SCIENCE_IMG" /usr/local/bin/python3.12 -m ecarsi serve --port $PERISCOPE_PORT --auth-file ~/.config/ecarsi/periscope-password \
  --control-plane $BASE --control-pool-root $POOL --control-bridge-root $BRIDGE --control-temporal-root $CONTROL \
  > $STATE/control/control-logs/periscope.log 2>&1 < /dev/null &
```

Periscope listens on `127.0.0.1` only: reach it through your own attended `ssh -L` forward (Sherlock forbids unattended tunnels). `--auth-file` keeps the password out of the process list.

Periscope serves the display zones under `display_root` and `more_display_roots` of `~/.config/ecarsi/results.json` (`--results` for another file), and the entries of `~/.config/ecarsi/periscope-datasets.json`:

```json
{"display_root": "/oak/stanford/projects/eca/eca-rsi/display", "archive_root": "/oak/stanford/projects/eca/eca-rsi/work",
 "more_display_roots": ["/oak/stanford/projects/eca/eca-rsi-test/display"]}
```

### A.7 Add workers

Workers are Slurm jobs. You can join a worker in two ways.

**A job that is the worker.** `control-plane.sh start` unpacks the image's eca-rsi into `$BASE/image-code`. Submit its worker script:

```bash
set -a; . ~/.config/ecarsi/deployment.env; set +a     # sbatch passes POOL, SCIENCE_IMG, HOSTPY and BINDS on
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

Then run the release gate on the new pair, in the background: `bash ops/runpy.sh ops/gate.py start`, then `bash ops/runpy.sh ops/gate.py wait <run_id>`. It runs `gate-dataset.json` end to end and checks that every unit is released, that no step degraded, that every zoomed lineage has its report and that the display zone and work archive exist. Keep the previous pair until the gate passes; if it fails, switch back.

### A.10 Ship a code change as a version

A change to eca-rsi's own code (`ecarsi/`, the kernels, `harness_bridge/`) ships as a version (decision 0019); it
does not wait for running executions, and executions already running finish on the version that started them.

1. Fast-forward `main` after the full suite passes, then `bash ops/publish-version.sh` (default: `main`). It prints
   the version name, the first 12 characters of the commit.
2. `bash ops/start-version.sh <name> 1` starts one coordinator on its task queue `ecarsi-<name>` and its runners.
3. `VERSION=<name> bash ops/runpy.sh ops/gate.py start`, then `... gate.py wait <run_id>`: the gate runs on the new
   version next to production.
4. After the gate passes: `bash ops/set-current.sh <name>` (new datasets use it from now on) and
   `bash ops/start-version.sh <name> 4`.
5. When `bash ops/runpy.sh ops/count-wf.py` shows no running execution on the old version's queue,
   `bash ops/retire-version.sh <old name>` stops its coordinators and runners. Its directory stays.

`ops/control-plane.sh start|stop|restart|status` runs the current version's coordinators and runners beside the
image's shared components. Changes to `ops/` take effect when `main` is fast-forwarded. A change to the images
that the shared components need (HQ, Temporal, PostgreSQL) still needs A.9; new Python packages for the stages can
come with a version (A.11).

### A.11 A version with new images

A version records the two images it was published with (`version.json`). Its coordinators and runners run in its
control image, and its pool requests go only to workers of its compute image, so it can bring new third-party
packages while production keeps running on the old images.

1. Put the new versions into `container/science-requirements.lock` (or the control lock) and build the pair (B.5).
2. Register the new compute image without making it current: inside it, run
   `python -m ecarsi.warm_pool --root $POOL configure-runtime --register-only <runtime.json>` (the runtime record
   `ops/switch-images.sh` writes, for the new image).
3. Add workers of it: `warm_pool add-worker <host> --job-id <id> --image <new compute .sif>`. Workers of the old image
   keep declaring the old runtime. A request whose image has no live worker shows `infeasible` in `scheduler.json`.
4. `VERSION_IMG=<new control .sif> VERSION_SCIENCE_IMG=<new compute .sif> bash ops/publish-version.sh`, then A.10
   from step 2.

The new image's snapshot must carry this registration code (images built after 2026-10-06's image registration
change): its workers read the runtime they declare from `--runtime`, which older snapshots do not know.

## B. Development

### B.1 Checkouts

Store the source repositories next to each other on a non-purged filesystem:

```text
$GROUP_HOME/<user>/eca/
  images/
  src/eca-rsi              # main checkout: ecarsi and, since decision 0018, the kernels and harness_bridge
  worktrees/eca-rsi-dev    # development worktree, branch dev
```

Develop in the `dev` worktree. Fast-forward `main` after the tests pass. Do not edit code that a running plane imports.

### B.2 Run a checkout instead of the image snapshot

Set `CODE=<checkout>` before `ops/control-plane.sh`. The checkout comes first on `PYTHONPATH`. It shadows `/opt/eca-rsi` in both images. `control-plane.sh identity` prints which one runs. Use this setting on a test plane only. Production runs from the snapshot.

The pool pins program files by content into every request. A changed stage file (`ecarsi/stages/*`, `ecarsi/agent/session.py`) breaks queued requests and in-flight sessions. Deploy such changes only when 0 executions are running.

### B.3 Run the tests

The tests run inside the images. You do not need a host environment. The images do not contain pytest. Install it once with `pip install --target $GROUP_HOME/<user>/pytest-only pytest`. Put that directory on `PYTHONPATH`.

```bash
cd $GROUP_HOME/$USER/eca/worktrees/eca-rsi-dev
apptainer exec --cleanenv --bind /scratch,/oak,/home,/lscratch --env LC_ALL=C --env LANG=C --env PYTHONDONTWRITEBYTECODE=1 \
  --env "PYTHONPATH=$PWD:/opt/rsi-control:/opt/rsi-python:$GROUP_HOME/$USER/pytest-only" \
  "$SCI" /usr/local/bin/python3 -m pytest -q -p no:cacheprovider tests
```

Use the compute image for the full suite. The control image lacks the numerical stack. Therefore, only the control and warm-pool tests collect there. Run Python from `/tmp` or with `PYTHONSAFEPATH=1`. This prevents a checkout in the working directory from shadowing the image's packages.

For pure documentation changes, run `git diff --check` and a link check. You do not need to run tests.

### B.4 Editable installs

Use an editable install only for IDEs and quick experiments on a host with Python 3.10 or newer:

```bash
python -m pip install -e $GROUP_HOME/$USER/eca/src/eca-rsi --no-deps
```

The kernels and harness_bridge are packages of the same repository (decision 0018), so this one install covers them. Dependency ranges are in `pyproject.toml`. The images contain the versions that are tested together (A.2).

### B.5 Rebuild the images

`ops/build-images-update.sh` updates the current pair in place:

```bash
FROM=<old stamp> STAMP=<new stamp> [WHEELS=<dir of wheels>] bash ops/build-images-update.sh
```

It extracts both images into sandboxes on node-local disk. It replaces `/opt/eca-rsi` with `git archive HEAD` of the `dev` worktree: ecarsi, the kernels and harness_bridge (decision 0018). It writes `BUILD.json`, takes the old wheels of those packages out of the images, and checks that each is imported from `/opt/eca-rsi`. Optionally, it swaps third-party Python distributions by their `RECORD` files. Then it packs each sandbox. The checkout must be clean.

Pack by hand. `apptainer build` from a sandbox segfaults in its mksquashfs step on the compute sandbox. The script runs `mksquashfs ... -processors 4`. Then it runs `apptainer sif new` and `apptainer sif add --datatype 4 --parttype 2 --partfs 1 --partarch 2 --groupid 1`.

A full rebuild (`ops/build-images.sh`) starts from the two base images, whose Python environments come from the two locks with `--require-hashes`. The compute environment installs into a sandbox of `python312-slim.sif` with

```bash
apptainer exec --cleanenv --bind <sandbox dir> images/base/python312-slim.sif python3 -m pip install --no-deps \
  --require-hashes --extra-index-url https://pypi.nvidia.com --target <sandbox>/opt/rsi-python -r container/science-requirements.lock
```

(3.6 GB of wheels; docrep comes as an sdist). On 2026-10-06 this reproduced the `/opt/rsi-python` of the images file for file (every `RECORD` of the 118 distributions identical). A new third-party version goes into the lock first; `build-images-update.sh` with `WHEELS=` swaps it into the current pair. It adds the eca-rsi snapshot, and Temporal, PostgreSQL and HQ taken from an existing control image (every control image carries them; nothing on scratch does). The Python environments' own root is `$GROUP_HOME/chensj16/eca/images/base/python312-slim.sif` ([docs/history/CONTAINER_LEGACY.md](docs/history/CONTAINER_LEGACY.md)). Record the sha256 of every new image. Then switch (A.9). Run an end-to-end regression on a small dataset.

### B.6 Source repositories

[eca-rsi](https://github.com/chansigit/eca-rsi) holds everything since decision 0018. The former repositories
[osp](https://github.com/chansigit/osp), [msp](https://github.com/chansigit/msp), [zmip](https://github.com/chansigit/zmip),
[agent-harness-bridge](https://github.com/chansigit/agent-harness-bridge) and
[standissect-lite](https://github.com/chansigit/standissect-lite) are archived; their histories are part of eca-rsi's.

## C. Run-time configuration

Agent models come from the catalog `~/.config/ecarsi/models.json` (A.4): harness, model and URL, in calling order. The model-turn service sets the bridge's selection for every call itself. The variables left to you:

| Variable | Meaning / default |
|---|---|
| `ARK_API_KEY` | Ark key (`openai`, `openai@ark`). Endpoint from the catalog URL, else `DOUBAO_BASE_URL`, else the Beijing region `/api/v3`. |
| `OPENROUTER_API_KEY` | OpenRouter key (`openai@openrouter`). Default endpoint `https://openrouter.ai/api/v1`. |
| `VLLM_API_KEY` | vLLM key (`openai@vllm`). Default endpoint `http://127.0.0.1:8000/v1`. |
| `OPENAI_AGENTS_API` | API type: `responses` (default) or `chat_completions` (text only). |
| `OPENAI_AGENTS_MAX_NUDGES` | Reminders in the same history when the agent does not submit. The default is 2. |
| `OPENAI_AGENTS_MAX_CONTEXT_RESETS` | Recoveries from an over-long context. The default is 2. |
| `OPENAI_AGENTS_SERVER_STATE` | The default is 1. When set to 1, Responses continues with `previous_response_id`. |

Put the keys in `~/.bashrc`; the runners read them from there. A session is bounded by `max_turns` × the per-turn response timeout (bridge `routing.response_timeout_seconds`, 900 s). Kernel settings (for example `resolution`, `n_pcs`, `n_top_genes`, `n_neighbors`, the zoom-in `min_cells`) are keys of each stage's `config` in the dataset spec, not environment variables; see [docs/control-plane/DATASET_V2.md](docs/control-plane/DATASET_V2.md).

## D. Submit a dataset

The control-plane path admits datasets through the coordinators. A dataset spec names the ECA-PP input, the run directory under `runs/`, the budgets, and the round policy. `examples/dataset-v2.json` provides a complete example. `docs/control-plane/DATASET_V2.md` explains the fields.

```bash
cd /tmp
apptainer exec --cleanenv --bind "$BINDS" --env PYTHONSAFEPATH=1 --env PYTHONPATH=/opt/eca-rsi:/opt/rsi-control \
  "$IMG" /usr/local/bin/python3 -m ecarsi.control --service-root $E/control/durable-control --task-queue ecarsi-durable-v2 start-dataset dataset.json
# later: status-dataset RUN_ID, resume-dataset RUN_ID --reason "..."
```

`start-dataset` adds a `storage` key from `display_root` and `archive_root` of `~/.config/ecarsi/results.json` (`--results` for another file) when the spec has none. With it, the run keeps its display zone up to date after every stage and archives its work tree when the dataset completes.

Follow progress on Periscope. A unit that stops on `loop_control.json` (`pause`, `stop_after_round`, `pause_after_stage`) ends as `PAUSED`. Clear the control. Then run `resume-dataset` on the unit.

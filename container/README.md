# Images and launcher

ECA-RSI runs from two Apptainer images. You install nothing on the host.

| Image | File | Size | Contents |
|---|---|---|---|
| Control image | `rsi-control-<stamp>.sif` | ~250 MB | the control Python environment (`/opt/rsi-control`), Temporal server, sql-tool and ui-server with the PostgreSQL v12 schema (`/opt/rsi-services/temporal`), PostgreSQL 16 (`/opt/rsi-services/postgres`, RUNPATH relative), HQ (`/opt/rsi-bin/hq`), an eca-rsi snapshot (`/opt/eca-rsi`; `BUILD.json` names the commit) |
| Compute image | `rsi-science-<stamp>.sif` | ~3.9 GB | the numerical stack, CPU and RAPIDS (`/opt/rsi-python`), `/opt/rsi-control`, HQ, the same eca-rsi snapshot, which carries the kernels (decision 0018) |

The current pair is `rsi-control-20261001-3.sif` and `rsi-science-20261001-3.sif` in `$GROUP_HOME/chensj16/eca/images/`. Keep the `-2` pair as a rollback image until the next production batch runs on `-3`.

The compute image runs the pool workers and Periscope. The control image runs all other services. One compute image serves every worker, with or without GPU support. Therefore, the worker pool uses a single runtime.

## Launcher

The deployment directory contains a short launcher script. This launcher exports the directories and the two images. It extracts `control-plane.sh` from the control image and executes the script:

```bash
export BASE=<control dir> IMG=<control.sif> SCIENCE_IMG=<compute.sif> POOL=<pool dir> BRIDGE=<bridge dir>
export CONTROL=$BASE/durable-control HOSTPY=python3
apptainer exec "$IMG" cat /opt/eca-rsi/container/control-plane.sh > "$BASE/.control-plane.from-image.sh"
exec bash "$BASE/.control-plane.from-image.sh" "$@"
```

```
control-plane.sh start|stop|restart|status [temporal|hq|scheduler|bridge|runners|coordinators|fleet-status|pruner ...]
control-plane.sh report [--sessions HOURS] [--json]   # pool, bridge, workers, datasets
control-plane.sh tokens [--json]                      # model turns and tokens per dataset
control-plane.sh identity                             # which eca-rsi runs: the image snapshot or a checkout
control-plane.sh host-code                            # unpack the snapshot to $BASE/image-code for host-side helpers
```

- The `start` command logs the active eca-rsi version in `control-logs/identity.log`. Set `CODE=<checkout>` for development. The checkout appears first on `PYTHONPATH` and shadows `/opt/eca-rsi`. Deploy production code changes to the control plane only through a rebuilt image.
- Set directory permissions under `BASE` to mode `0700`. Create `bridge/requests` before the first start.
- Set `TEMPORAL_PORT`, `DATABASE_PORT`, `UI_PORT`, and `TASK_QUEUE` if another control plane shares the host. The `TEMPORAL_DYNAMIC_CONFIG` variable names a hot-reloaded Temporal dynamic configuration YAML file. The `STAGE_LIMIT_FLOORS` variable raises `max_in_flight_deg` and `max_in_flight_lineages` for every dataset.
- The `COORDINATORS` variable sets the number of coordinator processes (default: 4). Allocate 96 GB of memory to the control plane. Each coordinator process uses 6–7 GB.

Start Periscope from the compute image using `ops/start-periscope.sh` from the deployment directory. It listens on `127.0.0.1`; reach it through your own `ssh -L` forward.

## Pool runtime and workers

Register the compute image as the pool runtime from inside that image:

```bash
apptainer exec --cleanenv --bind /scratch,/oak,/home,/lscratch --env PYTHONSAFEPATH=1 \
  --env PYTHONPATH=/opt/eca-rsi:/opt/rsi-control:/opt/rsi-python "$SCIENCE_IMG" /usr/local/bin/python3.12 \
  -m ecarsi.warm_pool --root "$POOL" configure-runtime runtime.json
```

The `runtime.json` file records the image path, image sha256 hash, interpreter, and import list. It sets `pythonpath: ["/opt/eca-rsi", "/opt/rsi-control", "/opt/rsi-python"]`. The `config.json` file contains `hq: /opt/rsi-bin/hq`.

Host-side helpers (`worker-node.sh` and `add-worker`) call `scontrol`, `nvidia-smi`, and `ssh`. The container images do not contain these utilities. Run `control-plane.sh host-code` to unpack the snapshot to `$BASE/image-code`. Then execute these commands:

```bash
POOL=$POOL SCIENCE_IMG=$SCIENCE_IMG sbatch -t 8:00:00 -c 16 --mem=32G $BASE/image-code/container/worker-node.sh   # a job that is a worker
PYTHONPATH=$BASE/image-code python3 -m ecarsi.warm_pool --root $POOL add-worker <host> --job-id <job>              # an existing allocation
```

Workers advertise the runtime digest. After you switch images, stop the worker supervisors. Send a SIGTERM signal to the process ID in `worker-state/*/worker.json`. Run `configure-runtime` inside the new image. Add the workers again. Running tasks retain their original runtime identity.

## Build

For a full build, run [ops/build-images.sh](../ops/build-images.sh). It starts from base images whose control environment comes from [control-requirements.lock](control-requirements.lock) (CPython 3.12 x86_64 hashed wheels). It takes Temporal, PostgreSQL, and HQ out of an existing control image. It then copies the eca-rsi checkout, kernels and harness_bridge included (decision 0018), to `/opt/eca-rsi`.

For an incremental build, run `ops/build-images-update.sh`. Extract the current images into a sandbox directory on local node storage. Replace `/opt/eca-rsi`, take out the old wheels of the repository's own packages, and replace any updated third-party wheels. Pack the sandbox directory and wrap it as a SIF file.

Pack the sandbox manually. The command `apptainer build` causes a segmentation fault during mksquashfs on the 6.6 GB compute sandbox. This fault occurs in apptainer 1.4 with mksquashfs 4.7.5, with or without `--mksquashfs-args`. Use this manual procedure instead:

```bash
mksquashfs <sandbox> <stamp>.squashfs -noappend -processors 4
apptainer sif new <stamp>.sif
apptainer sif add --datatype 4 --parttype 2 --partfs 1 --partarch 2 --groupid 1 <stamp>.sif <stamp>.squashfs
```

Perform these steps after a build:

1. Record the SIF sha256 hash.
2. Run the test suite inside the compute image with `ops/runsci.sh -m pytest -q tests`.
3. Switch the plane with `ops/switch-images.sh <stamp> <compute sha256> <host:job>...` when no executions are running. This script stops the workers and the plane. It registers the runtime and updates script references. It then starts the plane and re-adds the workers.
4. Run one end-to-end regression dataset before you run a production batch.

Moving Temporal or PostgreSQL into an image changed the binary bytes because of RUNPATH. The Temporal launcher accepts the same file hashes in a different directory layout. The system rejects a different installation until you intentionally move `durable-control/runtime.json` aside.

## Rules learned the hard way

- **Equal version numbers are not equal code.** An image once contained msp and zmip with matching checkout version numbers, but 11 source files differed. Since decision 0018 the kernels are part of the snapshot, so `BUILD.json`'s commit names their source too.
- **Never edit the live code path.** Pool tasks import eca-rsi for each task. A draft edit in the live worktree caused 21 tasks to fail in one minute. Develop in a separate worktree, run tests, and then build a new image.
- **Deploy pinned stage files only with zero running executions.** Requests pin program files by content. A modified file causes queued requests and active sessions to fail.
- **`pandas<3` is pinned.** In pandas 3, Copy-on-Write returns read-only arrays from `Series.values`. This behavior broke OSP quality control actions, even though every test passed.
- **`scikit-image` is installed explicitly.** The scrublet path in scanpy imports `scikit-image`.
- **`HARNESS=claude` needs the Claude Code CLI.** The control image includes claude-agent-sdk with the bundled CLI. The default configuration `HARNESS=openai` requires no extra packages.
- **Run in-image checks from `/tmp` or with `PYTHONSAFEPATH=1`.** A repository checkout in the current directory shadows the image snapshot.
- **A SIF is never edited in place.** The pool runtime records the SIF sha256 hash. A patched file invalidates every stage that verifies against this hash. To change the image, write a new file.

## Pluggability test (eca-rsi#13)

The script [pluggability-test.sh](pluggability-test.sh) runs one test cycle: snapshot, kill, snapshot, and compare. A snapshot records the attempt directories, receipt state, and sha256 hash of each `publication.json` file for every request. Point `B` to a **disposable** control plane. The test sends SIGKILL to PostgreSQL.

```bash
export B=<disposable run dir> PY="apptainer exec $SCIENCE_IMG /usr/local/bin/python3.12"
bash container/pluggability-test.sh snapshot before
bash container/pluggability-test.sh kill-plane        # or kill-coordinators
bash container/pluggability-test.sh start-plane
bash container/pluggability-test.sh compare before after
```

Test results from the 2026-09-21 run show that killing the control plane does not interrupt active tasks. Stage programs execute in the worker pool and dispatch their own sub-requests. Every active task completed successfully. PostgreSQL recovered automatically, and the system did not resubmit any tasks. Destroying a worker does not mean losing its allocation. A task counts as lost only when the Slurm grant expires (`expires_at` plus 300 s), because the worker can rejoin. To test the expiration path, use a dedicated test allocation.

## History

Find the build.sh / eca-ct toolchain, the 2026-09-15..21 images, and their retention schedule in [docs/history/CONTAINER_LEGACY.md](../docs/history/CONTAINER_LEGACY.md). The scripts and runtime manifests of that period are in [docs/history/container-legacy/](../docs/history/container-legacy/).

# Container environment for the ecarsi chain

## Worker model calls

[Agent Worker manifest](agent-worker-runtime-20260915.json) extends the pinned
science image with `/opt/rsi-control` from the pinned control image. Its Python
path puts `/opt/rsi-control` before `/opt/rsi-python`, so Bridge and Worker use
the same harness/SDK dependencies. NumPy, SciPy and scientific kernels come from
`/opt/rsi-python`; the control directory contains no numerical stack.
Model clients and registered scientific tools can therefore run on the same
allocation without using a host Python environment.

To rebuild, extract both recorded source images with `apptainer build --sandbox`,
copy the control image's `/opt/rsi-control` into the science sandbox, and pack it
with `LC_ALL=C LANG=C apptainer build` — **no `--mksquashfs-args`**: passing
`-processors N` makes apptainer 1.4 / mksquashfs 4.7.5 die with SIGSEGV right after
"Creating SIF file…" (2026-09-21, both 4 and 2 processors, while the same mksquashfs
run by hand on the same sandbox succeeds). Apptainer parallelises on its own.
Record the resulting SIF hash. Run `configure-runtime` inside that image before
rejoining idle workers with `add-worker`; running tasks retain their original
runtime identity. Workers load model credentials from user shell configuration
only in model executor processes, never in request manifests.

## V2 control runtime

The [control manifest](control-runtime-20260915.json) records the base SIF,
installed Bridge wheel, and resulting SIF hashes. Its
[requirements lock](control-requirements.lock) pins CPython 3.12 Linux x86_64
wheels, including transitive dependencies. This image runs the Work Coordinator,
Agent Bridge, and Warm Pool Scheduler. Scientific operations retain their own
science image; adding scientific packages to the control environment is unnecessary.
Organize validates sample decisions from prepared metadata without importing pandas.

To rebuild, obtain the base image and Bridge wheel matching the manifest hashes.
The wheel can be built from the recorded Bridge commit; use a separate checkout.
Run from this repository, with absolute paths for the build inputs:

```bash
# Set BASE_SIF, BRIDGE_WHEEL, BUILD_ROOT, and CONTROL_SIF to your local paths.
apptainer build --sandbox "$BUILD_ROOT" "$BASE_SIF"
apptainer exec --cleanenv --bind "$PWD,$BUILD_ROOT,$(dirname "$BRIDGE_WHEEL")" \
  "$BASE_SIF" python3 -m pip install --only-binary=:all: --require-hashes \
  --target "$BUILD_ROOT/opt/rsi-control" -r "$PWD/container/control-requirements.lock"
apptainer exec --cleanenv --bind "$BUILD_ROOT,$(dirname "$BRIDGE_WHEEL")" \
  "$BASE_SIF" python3 -m pip install --no-deps \
  --target "$BUILD_ROOT/opt/rsi-control" "$BRIDGE_WHEEL"
apptainer build "$CONTROL_SIF" "$BUILD_ROOT"   # no --mksquashfs-args; see the note above
```

Build timestamps can change the resulting SIF hash; record the new artifact's hash
after validation. Launch against an explicit RSI checkout and shared run directories:

```bash
apptainer exec --cleanenv --bind /path/to/rsi,/shared/rsi \
  --env PYTHONPATH=/path/to/rsi:/opt/rsi-control \
  --env PYTHONNOUSERSITE=1 --env PYTHONSAFEPATH=1 \
  "$CONTROL_SIF" python3 -m ecarsi.control \
  --service-root /shared/rsi/control --task-queue ecarsi-durable-v2 worker
```

Use the same interpreter prefix for `ecarsi.agent` and `ecarsi.warm_pool`.
Bind the recorded native binaries when launching `ecarsi.control.temporal`.
With `--cleanenv`, forward each configured provider credential through an
`APPTAINERENV_` environment variable (for example `APPTAINERENV_OPENROUTER_API_KEY`);
do not put credentials in command arguments or manifests. Preserve the chosen
`OPENAI_AGENTS_API` mode when restarting services. Replay existing histories before
replacing the Coordinator, and retain the previous image until verification passes.

## Legacy full-chain runtime

`build.sh` + `install-wrapper.sh` reproduce the interpreter that ECA-RSI batches run on.
Nothing in them is site-specific; everything is set by environment variable.

## Why

If the host's glibc is older than the manylinux tag a wheel targets, pip and uv silently
fall back to building from source. For numpy that yields a build with **no BLAS**: it
imports, all tests pass, and matmul is ~100x slower. Building the venv inside a modern
image removes that entire failure mode, and it pins the numeric stack independently of
whatever the login environment happens to have.

Measured on CentOS 7 (glibc 2.17), 2026-09: host numpy `dgemm 4000³` 45 s, container 0.37 s.

## Build

```bash
export ECA_CT_ROOT=/path/to/env          # venv + wrapper land here
export ECA_SIF=/path/to/python312-slim.sif
export ECA_REPOS=/path/to/checkouts      # holds agent-harness-bridge osp msp zmip eca-rsi
apptainer exec --bind /scratch,/oak,/home "$ECA_SIF" bash container/build.sh
bash container/install-wrapper.sh
```

Any stock `python:3.12-slim` image works; there is no custom recipe. Then point the
pipeline at the wrapper:

```bash
ECA_RSI_PYTHON=$ECA_CT_ROOT/python eca-rsi run <eca-pp dir> <root>
```

## Rules learned the hard way

- **Never run `.venv/bin/python` directly on the host.** Its wheels target the image's
  glibc; only the wrapper (or a shell inside the image) is valid.
- **The image needs no `git`.** `runtime_identity()` treats the commit as provenance and
  tolerates a missing binary (ecarsi ≥ 0.2.1); identity is content-only.
- **`pandas<3` is pinned deliberately.** pandas 3's Copy-on-Write returns read-only arrays
  from `Series.values`, which broke osp's cells-scope QC actions — and every test suite
  passed under pandas 3 without catching it. Lift the pin only after a real run.
- **`scikit-image` is installed explicitly** because scanpy's scrublet path imports it and
  osp does not declare `scanpy[scrublet]`.
- **`HARNESS=claude` needs the Claude Code CLI (node)**, which a slim image does not have.
  `HARNESS=openai` (the default) works as is.
- **Switching an organ between interpreters changes its runtime identity**, so finished
  stages are not reusable across the switch — start such an organ in a new output directory.
- To let the image see a host binary that is not on its PATH (ngrok for
  `ecarsi serve --ngrok`), export `APPTAINERENV_APPEND_PATH=$HOME/local/bin` for that call
  rather than editing the shared wrapper.

(labels identical); see msp `docs/compute-endpoint-design.md`.

## Validation

`build.sh` ends with a sanity block (BLAS check, a dgemm timing, versions, imports).
Beyond that, run the suites inside the image:

```bash
for r in eca-rsi osp msp zmip agent-harness-bridge; do
  (cd "$ECA_REPOS/$r" && "$ECA_CT_ROOT/python" -m pytest -q)
done
```

Reference run 2026-09-07 on Sherlock: eca-rsi 47 + 68/2 skipped, osp 40, msp 136, zmip 78,
agent-harness-bridge 88 (its one node-dependent test fails in a slim image).

## Branch `v2` layout (2026-09-17)

The second-generation modules live in subpackages: `ecarsi.control` (Temporal workflows, `python -m ecarsi.control … worker`),
`ecarsi.agent` (`python -m ecarsi.agent serve`), `ecarsi.stages` (the programs the pool runs), `ecarsi.warm_pool` and
`ecarsi.observatory`. [agent-worker-runtime-20260917.json](agent-worker-runtime-20260917.json) is the science runtime for that
layout (the import list names the new modules); [control-plane.sh](control-plane.sh) is the launcher template the run
directory copies and configures. See `docs-gen2/ARCHITECTURE.md`.

## Science image 20260917-1 (OSP 0.1.7)

`rsi-science-20260917-1.sif` is `rsi-science-20260915-8.sif` with osp 0.1.7 (`qc.py`: fixed mitochondrial cutoff
15 → 25 %, osp branch `qc-mt-25`); every other file is identical. Built unprivileged on a compute node's local disk
(`apptainer build --sandbox`, copy the file, rename the dist-info, refresh `/opt/rsi-runtime.json`, `apptainer build`)
in three minutes; the script that did it is kept next to the images as `build-20260917-1.sh`.
[agent-worker-runtime-20260917-osp017.json](agent-worker-runtime-20260917-osp017.json) is its runtime record; enable it
on a pool with `configure-runtime` (the `runtime` sub-object of that file) run inside the image. Per-sample results of
datasets already past their per-sample stage do not change.

## Science image 20260921-1 (msp and zmip synced to their checkouts)

`rsi-science-20260921-1.sif` is `rsi-science-20260917-1.sif` with `/opt/rsi-python/{msp,zmip}` replaced by the
checkout mains (msp `386ff27`, zmip `8aa16a9`); osp is unchanged. Same recipe; the script that did it is kept next to the images as `build-20260921-1.sh`, ~10 minutes.

Why it was needed: **committing a kernel fix does not deploy it.** The pool's runtime pythonpath is
`[<eca-rsi worktree>, /opt/rsi-control, /opt/rsi-python]`, so only `ecarsi` comes from a checkout — and the
image's msp/zmip carried the *same version numbers* as the checkouts (0.5.2, 0.3.9) while 11 source files
differed and `msp/agent_data.py` was missing entirely. That is how zmip `8aa16a9` (`report._proposal`) stayed
undeployed and kept `test_zoomin_v2` red in the suite. Equal versions are not equal code; the manifest's
`msp_source` / `zmip_source` digests in `/opt/rsi-runtime.json` are what to compare.

[agent-worker-runtime-20260921.json](agent-worker-runtime-20260921.json) is its runtime record; enable it on a pool
with `configure-runtime` (the `runtime` sub-object) run inside the image. Changing the image changes the runtime
digest, so this is batch-boundary work: results already computed keep their original runtime identity. It has been
the acceptance pool's runtime since 2026-09-21.

**One image serves every worker, GPU included.** A pool's `config.json` holds a single `runtime`, and this image
carries both stacks: the science tree has NumPy 2.2.6 / SciPy 1.16.3 / scanpy 1.12.4 *and* cudf/cuml 25.12 with
cupy 13.6, while `/opt/rsi-control` has temporalio 1.32, openai-agents 0.22 and agent-harness-bridge 0.2.14. So
there is no separate GPU image to fall behind — which matters, because falling behind is exactly the failure this
image was built to fix, and a second accelerator-specific image would double the chance of it. The two host venvs
`venvs/eca-ct` (NumPy 2.5.3) and `venvs/eca-ct-gpu` (NumPy 2.4.6) are generation-1 leftovers on a different
numerical stack: `eca-ct` still runs Periscope, which is presentation and outside the identity hash, and
`eca-ct-gpu` has no consumer left. Neither is on any generation-2 compute path.

What this does **not** settle is whether the GPU is worth using. RAPIDS is installed and importable; no run has
measured a speed-up, and nothing in the scheduler steers GPU-suited work to a GPU worker. That is a measurement,
not a build.


## Testing that the plane is pluggable at both ends (eca-rsi#13)

[pluggability-test.sh](pluggability-test.sh) runs one step of the test: snapshot, kill, snapshot,
compare. A snapshot records, per pool request, its attempt directories (the resubmission evidence),
its receipt state, and the sha256 of every `publication.json`; `compare` prints what moved between
two of them. Point `B` at a **disposable** plane — the test SIGKILLs PostgreSQL, so it must never
be aimed at one holding real history:

```bash
export B=$SCRATCH/eca-runs/pluggability-<date> PY=$SCRATCH/venvs/eca-ct/python
bash container/pluggability-test.sh snapshot before
bash container/pluggability-test.sh kill-plane        # or kill-coordinators
bash container/pluggability-test.sh start-plane
bash container/pluggability-test.sh compare before after
```

Setting one up: copy `control-plane.sh` into the new run directory with its own `TEMPORAL_PORT`,
`DATABASE_PORT`, `UI_PORT`, `OBSERVATORY_PORT` and `TASK_QUEUE`, then `warm_pool init` +
`configure-runtime`, `agent init` for the bridge, and borrow workers with `add-worker`. A Slurm
allocation can host only one pool worker at a time (`~/.cache/ecarsi-pool/<host>/allocation-<job>.lock`),
so a node has to leave its old pool before it can join the test one.

Two things the 2026-09-21 run established that are worth knowing before repeating it:

- **Killing the leader disturbs nothing.** Stage programs run in the pool and dispatch their own
  sub-requests; the coordinator only opens stages and decides rounds. With the whole plane
  SIGKILLed — Temporal, PostgreSQL, coordinators, scheduler and bridge — every in-flight task ran
  to completion, PostgreSQL recovered from the dirty shutdown by itself, `owner.lock` did not block
  the restart, and nothing was resubmitted or recomputed.
- **Destroying a worker is not the same as losing its allocation.** `backend.allocation_ended()`
  declares a task lost only once the Slurm grant is over (`expires_at` + 300 s), because while the
  grant lives a worker may still rejoin and reconcile. Kill the worker process and the request sits
  in `unknown_external_result` until someone rejoins that host — which then writes a retryable
  `WorkerLost` receipt and the session continues. Testing the expiry path needs a sacrificial
  allocation.

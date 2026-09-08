# Container environment for the ecarsi chain

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

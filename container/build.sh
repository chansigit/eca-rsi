#!/bin/bash
# Build the container-only venv for the ecarsi chain: official manylinux wheels
# (numpy/scipy with their bundled OpenBLAS) plus the five repos as editable installs.
#
# Why a container at all: on a host whose glibc predates the manylinux tag a wheel
# targets, pip/uv silently fall back to an sdist build. For numpy that produces a
# build with NO BLAS -- imports fine, matmul ~100x slower. Building the venv inside
# a modern image sidesteps the whole class of problem. (Measured on CentOS 7 /
# glibc 2.17, 2026-09: host numpy dgemm 4000^3 took 45 s, container 0.37 s.)
#
# Configure with environment variables -- nothing here is site-specific:
#   ECA_CT_ROOT   where the venv and wrapper live        (default: $PWD)
#   ECA_SIF       image to build inside                  (default: $ECA_CT_ROOT/python312-slim.sif)
#   ECA_REPOS     directory holding the checkouts        (default: parent of this repo)
#   PIP_CACHE_DIR pip cache                              (default: pip's own)
#
# Run it INSIDE the image, e.g.
#   apptainer exec --bind /scratch,/oak,/home "$ECA_SIF" bash container/build.sh
set -euo pipefail
ROOT="${ECA_CT_ROOT:-$PWD}"
REPOS="${ECA_REPOS:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
V="$ROOT/.venv"
CACHE_ARG=()
[ -n "${PIP_CACHE_DIR:-}" ] && CACHE_ARG=(--cache-dir "$PIP_CACHE_DIR")

for r in agent-harness-bridge osp msp zmip eca-rsi stancounts stangene eca-pp; do
    [ -d "$REPOS/$r" ] || { echo "missing checkout: $REPOS/$r (set ECA_REPOS)" >&2; exit 2; }
done

python -m venv --clear "$V"
"$V/bin/pip" install -q "${CACHE_ARG[@]}" -U pip
# pandas is pinned: pandas 3's Copy-on-Write hands out read-only arrays from
# Series.values, which broke osp's cells-scope QC actions in a way all four test
# suites passed straight through (2026-09-07). Lift the pin only with a real run.
# scikit-image is scanpy's scrublet dependency and is not declared by osp.
"$V/bin/pip" install "${CACHE_ARG[@]}" \
    -e "$REPOS/agent-harness-bridge[all]" -e "$REPOS/osp[agent]" -e "$REPOS/msp[agent]" \
    -e "$REPOS/zmip" -e "$REPOS/eca-rsi" \
    -e "$REPOS/stancounts" -e "$REPOS/stangene" -e "$REPOS/eca-pp[probe,openai,claude,test]" \
    "pandas<3" pyarrow pytest scikit-image 2>&1 | grep -v "already satisfied" | tail -15

echo "=== sanity ==="
"$V/bin/python" - <<'PY'
import contextlib, io, time
import importlib.metadata as m
import numpy as np
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    np.show_config()
blas = "openblas" in buf.getvalue().lower() or "found: true" in buf.getvalue().lower()
print("numpy", np.__version__, "| BLAS linked:", blas)
x = np.random.rand(4000, 4000)
t = time.perf_counter(); x @ x
print(f"dgemm 4000^3: {time.perf_counter() - t:.2f} s   (no-BLAS builds take ~40 s)")
for p in ("scipy", "scanpy", "anndata", "h5py", "numba", "umap-learn", "pynndescent",
          "scikit-learn", "harmonypy", "igraph", "pyarrow", "openai-agents",
          "agent-harness-bridge", "osp-sc", "msp-sc", "zmip", "ecarsi",
          "stancounts", "stangene", "eca-pp"):
    try:
        print(f"  {p:22s} {m.version(p)}")
    except Exception as e:
        print(f"  {p:22s} MISSING ({type(e).__name__})")
import ecarsi, osp, msp, zmip, harness_bridge  # noqa: F401
print("imports ok:", ecarsi.__file__)
PY
echo
echo "Now install the wrapper:  ECA_CT_ROOT=$ROOT ECA_SIF=\${ECA_SIF:?} bash container/install-wrapper.sh"

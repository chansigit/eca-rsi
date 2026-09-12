#!/bin/bash
# Shared Dask warm pool for MSP_COMPUTE_ENDPOINT=dask (eca-rsi#8): one scheduler,
# workers in whatever Slurm allocations you already hold, any number of runs
# attach concurrently. Every process runs the container interpreter, so tasks
# unpickle into exactly the environment that submitted them.
#
#   dask-pool.sh scheduler                 # start here (this node), in the background
#   dask-pool.sh worker <node> [nprocs]    # add a worker on a node you hold an allocation on
#   dask-pool.sh worker <node> --gpu       # one worker per GPU, --nv, resource GPU=1 (tier="gpu")
#   dask-pool.sh status                    # who is connected
#   dask-pool.sh stop                      # kill workers and scheduler
#
# Environment:  DASK_POOL_DIR  (default $SCRATCH/dask-pool) holds scheduler.json + logs.
#               ECA_CT_ROOT    (default /scratch/users/chensj16/venvs/eca-ct) has the python wrapper.
#               ECA_CT_GPU_ROOT wrapper for --gpu workers (default: ECA_CT_ROOT); a venv built with
#                              ECA_GPU=1 build.sh, i.e. the same packages plus rapids-singlecell.
#               PYTHONPATH     is forwarded to workers: the pool must import the SAME msp
#                              as the runs that use it (a worktree override travels along).
# Then, in the run:  MSP_COMPUTE_ENDPOINT=dask MSP_DASK_SCHEDULER=$DASK_POOL_DIR/scheduler.json
set -euo pipefail
POOL="${DASK_POOL_DIR:-$SCRATCH/dask-pool}"
PY="${ECA_CT_ROOT:-/scratch/users/chensj16/venvs/eca-ct}/python"
PY_GPU="${ECA_CT_GPU_ROOT:-${ECA_CT_ROOT:-/scratch/users/chensj16/venvs/eca-ct}}/python"
SF="$POOL/scheduler.json"
mkdir -p "$POOL"

case "${1:-}" in
scheduler)
    [ -f "$SF" ] && { echo "scheduler file exists: $SF (stop first)" >&2; exit 2; }
    nohup "$PY" -m distributed.cli.dask_scheduler --scheduler-file "$SF" --no-dashboard \
        > "$POOL/scheduler.log" 2>&1 &
    for _ in $(seq 30); do [ -f "$SF" ] && break; sleep 1; done
    [ -f "$SF" ] || { echo "scheduler did not come up, see $POOL/scheduler.log" >&2; exit 1; }
    echo "scheduler $(python3 -c "import json;print(json.load(open('$SF'))['address'])") pid $!"
    ;;
worker)
    node="${2:?node}"; n="${3:-4}"; extra=""; tag="$node"; wpy="$PY"
    if [ "${3:-}" = "--gpu" ]; then
        wpy="$PY_GPU"
        # one process per GPU, each advertising GPU=1 so only tier="gpu" tasks
        # land on it; APPTAINER_NV=1 makes the container wrapper pass --nv.
        n=$(ssh -o BatchMode=yes "$node" 'nvidia-smi -L | wc -l'); extra="--resources GPU=1"; tag="$node-gpu"
    fi
    # ssh -f: fork after auth, before the remote command. A plain `ssh node 'cmd &'`
    # never returns -- ssh keeps waiting on the inherited stdin (spike, 2026-09-12).
    ssh -f -o BatchMode=yes "$node" "env PYTHONPATH='${PYTHONPATH:-}' ${extra:+APPTAINER_NV=1} '$wpy' -m distributed.cli.dask_worker \
        --scheduler-file '$SF' --nworkers $n --nthreads 1 --name '$tag' $extra \
        > '$POOL/worker-$tag.log' 2>&1"
    echo "worker on $node ($n procs${extra:+, GPU tier}), log $POOL/worker-$tag.log"
    ;;
status)
    "$PY" - "$SF" <<'PYEOF'
import sys
from distributed import Client
with Client(scheduler_file=sys.argv[1], timeout=10) as c:
    info = c.scheduler_info()
    print(info["address"])
    for w in info["workers"].values():
        res = " ".join(f"{k}={v:g}" for k, v in w.get("resources", {}).items())
        print(f"  {w['name']:20s} {w['host']:16s} threads={w['nthreads']} {res}")
PYEOF
    ;;
stop)
    [ -f "$SF" ] && "$PY" - "$SF" <<'PYEOF' || true
import sys
from distributed import Client
with Client(scheduler_file=sys.argv[1], timeout=10) as c:
    c.shutdown()  # scheduler tells every worker to exit, then exits itself
print("pool shut down")
PYEOF
    rm -f "$SF"
    ;;
*)
    sed -n 2,16p "$0"; exit 2
    ;;
esac

# Persistent dataset queue

`eca-rsi batch` distributes dataset drivers across the Slurm allocations you
have already supplied. The drivers submit OSP/MSP/ZMIP computation through the
existing warm pool. You can keep adding datasets while other datasets run;
neither the queue nor the node agents need restarting.

Drivers and compute workers have separate resource budgets. Reserve disjoint
CPU sets and memory for these two roles when configuring a node. Their combined
memory budgets must fit the allocation, with room for Slurm and runtime overhead.
Keeping compute capacity available prevents drivers from occupying all the
resources they subsequently need for computation. A node may run several drivers
and several compute worker processes.

## Configure once

Create `~/.config/ecarsi/batch.json` (or pass `--config` to each command):

```json
{
  "directory": "/shared/pool/datasets",
  "python": "/shared/venv/python",
  "scheduler": "/shared/pool/scheduler.json",
  "env": {
    "PYTHONPATH": "/shared/pinned-scientific-runtime",
    "PERSAMPLE_PARALLEL": "2",
    "MSP_COMPUTE_GPU": "1",
    "MSP_PYTHON": "/shared/gpu-venv/bin/python",
    "ZMIP_PYTHON": "/shared/gpu-venv/bin/python"
  },
  "required_env": ["ARK_API_KEY"],
  "max_cpu_percent": 90,
  "pause_dispatch": false,
  "defaults": {"cpus": 2, "memory_gb": 8, "hours": 6}
}
```

Use paths and a provider environment appropriate to your installation. The
`python` entry may be a container wrapper. `MSP_PYTHON` and `ZMIP_PYTHON` must be
usable from inside that container. GPU stages require a compatible GPU worker;
runtime matching remains enforced. CPU-only deployments can omit the GPU entries.
Keep API keys in the node environment, not the JSON file. An optional `registry`
path automatically registers submitted mirrors in Periscope.

Start a node agent **using host Python, inside your existing Slurm allocation**:

```bash
taskset -c <reserved-driver-cpus> python3 -m ecarsi.batch node --memory-gb 32
```

The host Python must be able to import this installation of `ecarsi`. It needs
no scientific dependencies for node management. The agent validates the Slurm
cgroup, affinity, memory and expiry, and holds the same CPU locks used by compute
worker launchers. If those CPUs still belong to earlier work, it waits for their
release. Start agents on more allocated nodes to add capacity. No command here
requests a new allocation or returns one to Slurm.

## Submit datasets

Prepare a JSON list, with one entry per independent dataset:

```json
[
  {
    "name": "study-a",
    "input": "/shared/eca-pp/study-a",
    "output": "/scratch/rsi/study-a",
    "mirror": "/shared/eca-pp/study-a/rsi",
    "registry_name": "collection-study-a",
    "cpus": 2,
    "memory_gb": 16,
    "hours": 6
  }
]
```

```bash
eca-rsi batch submit datasets.json
eca-rsi batch status
# Later, while the first submission is still running:
eca-rsi batch submit more-datasets.json
```

`memory_gb` is the driver budget, separate from remote kernel memory. `hours` is
both the admission time requirement and the attempt's runtime limit. Optional
`sample_map` points to an existing experiment mapping; it is explicitly passed
to every unit's persample stage. Output directories identify submissions:
resubmitting the same input/output/mirror entry returns its existing queue ID.
It does not start another copy. An input must already satisfy the ECA-PP contract;
batch submission does not replace preprocessing or scientific input validation.

The queue uses arrival order, allowing a fitting dataset past an older one that
needs more resources. It selects a node with spare capacity, accounting for
reserved CPU IDs, reserved RAM, observed driver memory and CPU usage, live cgroup
headroom, and remaining Slurm time. Unused CPU percentage does not erase an
active task's reservation: a temporarily idle model call can become busy again.
Every driver runs in a separate Slurm step with its own enforced memory limit
and CPU affinity. Node-level concurrency follows these budgets, not a fixed
fleet-wide dataset count.

Node agents reload configuration every five seconds. `pause_dispatch` and the
CPU pressure threshold affect new admission immediately. An invalid update
retains the previous configuration. Each admitted attempt retains its own
runtime/environment snapshot; changing defaults does not rewrite active work.
Changing a node's CPU/memory partition requires draining that node agent first.

## Monitoring and recovery

Set `ECA_DATASET_QUEUE=/shared/pool/datasets` for Periscope. Warm pool then shows
dataset execution nodes, driver resource reservations and measured process usage,
datasets waiting for drivers, and the separate queue of compute tasks waiting
for workers. A legacy `ECA_PERISCOPE_BATCH_STATUS` may coexist during migration.
Usage follows the actual Slurm task process; the local `srun` client is not the
scientific process. Refreshes are every five seconds.

The shared queue survives agent restarts and continues accepting submissions
even with no nodes online. SIGTERM to a node agent drains it. Each dataset has an
independent host supervisor that owns its container and retains CPU locks until
execution exits; restarting an agent does not terminate that container. Work
whose execution is uncertain is not silently retried. Once a terminal receipt
confirms that an attempt has stopped, resume it with:

```bash
eca-rsi batch retry <queue-id>
eca-rsi batch retry <queue-id> --memory-gb 32
```

The scientific pipeline validates its recorded checkpoints on resume. Completed
outputs and previous attempt receipts remain intact. Queue files live under
`directory`: `status.json`, `.queue.lock`, and `receipts/`; dataset logs default
to `<output>.log`. The shared filesystem must support POSIX file locks and atomic
rename. Keep it on durable shared storage according to your cluster's policy.

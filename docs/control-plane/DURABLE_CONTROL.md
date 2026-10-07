# Temporal service and recovery

The control service runs Temporal Server on PostgreSQL. The control image provides both services under `/opt/rsi-services/{temporal,postgres}`. PostgreSQL stores data and WAL in one private directory on shared storage. The immutable SQLite DEG databases that the annotation tools read are separate and unaffected.

PostgreSQL requires a filesystem with POSIX and synchronization semantics. The service also requires coherent cross-host `flock`. Sherlock Lustre satisfies both requirements. See the [Temporal deployment guide](https://docs.temporal.io/self-hosted-guide/deployment) and the [PostgreSQL filesystem guidance](https://www.postgresql.org/docs/16/creating-cluster.html#CREATING-CLUSTER-FILESYSTEM).

Pinned versions are PostgreSQL 16.15, Temporal Server 1.32.0, Python SDK 1.32.0, and UI Server 2.54.1. The UI is optional. Use `--ui-port 0` to disable the UI.

## Start

Start the service through the launcher. The launcher runs the command below inside the control image with the image binaries:

```bash
BASE=<run dir> IMG=<control image> SCIENCE_IMG=<compute image> bash control-plane.sh start temporal
```

The underlying command is:

```bash
python -m ecarsi.control.temporal \
  --root <run dir>/durable-control \
  --postgres-bin /opt/rsi-services/postgres/bin \
  --temporal-dir /opt/rsi-services/temporal \
  --schema-dir /opt/rsi-services/temporal/schema/postgresql/v12 \
  --bind "$(hostname -f)"
```

The launcher creates the root directory with mode `0700`. An existing root directory must have that mode and belong to the current user. Default ports are 7333 for the frontend, 55432 for PostgreSQL, and 8333 for the UI. Internal Temporal services also use frontend offsets 1, 2, 6, 100, 101, 102, and 106. Set `TEMPORAL_PORT`, `DATABASE_PORT`, and `UI_PORT` in the launcher when another control plane shares the host.

Bind the service to a private interface. Reach the ports with SSH forwarding. The service does not provide a public API. PostgreSQL listens only on loopback. It uses a generated SCRAM password stored in the private control directory.

Start coordinators and submit datasets through the same launcher, or directly:

```bash
python -m ecarsi.control --service-root <run dir>/durable-control --task-queue ecarsi-durable-v2 worker
python -m ecarsi.control --service-root <run dir>/durable-control --task-queue ecarsi-durable-v2 start-dataset dataset.json
```

The submission and the worker must use the same task queue. The service creates the `default` namespace with 30-day completed-history retention. Published scientific artifacts and pool or bridge receipts have their own lifetimes. Temporal history retention does not delete them.

## Coordinator sizing

Each coordinator process defaults to two concurrent workflow activations with `--workflow-slots 2`. Its 16 activity slots and the external pool computations stay concurrent. Cold recovery of a 14,000-event history exceeded the 10-second workflow-task deadline when several Python replays competed in one process. One slot left sticky continuations waiting almost 10 seconds while the process was mostly idle. Two slots keep polling responsive with a small replay budget. Tune the override against cold replay latency as well as steady-state throughput. The slot count does not limit active datasets or model calls.

Several coordinator processes can poll the same task queue. The launcher starts `COORDINATORS` of them, with a default of 4. Scale the count within the control allocation when Python replay is CPU-bound. Reserve capacity for the Temporal service, the database, and the other control components. Coordinators use 6–7 GB each. A 96 GB control allocation is the tested size.

## Observation

Periscope follows the same record. Start Periscope from the compute image with the deployment `ops/start-periscope.sh` script, or directly:

```bash
python -m ecarsi serve --control-plane <run dir> --control-temporal-root <run dir>/durable-control \
  --control-pool-root <run dir>/pool --control-bridge-root <run dir>/bridge --port 8899
```

The control-plane page is at `/_control/`. Forward the Periscope port and the Temporal UI port separately.

`ops/rtop` (`python -m ecarsi top --root <run dir> --pool-root ... --bridge-root ...`) shows the same records in a terminal, in colour, every two seconds: workers with CPU and memory meters and a ten-minute CPU sparkline, the tasks they run, the models, the pool, the datasets at work, node productivity and the failed attempts. It reads the files itself, as the page does, so it needs no Periscope and no password; each refresh costs about 0.1 s of CPU on its node and nothing on the workers.

## Recovery behavior

`service.json` publishes the serving address and generation every five seconds. A coordinator started with `--service-root` stops polling an unavailable generation. The coordinator then connects to the replacement generation. It does not cancel accepted pool or bridge requests. A restarted coordinator reconstructs its workflow from Temporal history. Callers do not resubmit datasets after an ordinary process or service interruption.

On a new node, run the same launcher with the same `BASE`. The database and history stay in place. PostgreSQL runs with `fsync=on`, `synchronous_commit=on`, and `full_page_writes=on`. This module does not acquire or release any Slurm job.

The supervisor and the native service children share one filesystem ownership lock. The system rejects a second owner even while the heartbeat of the first owner is late. Linux parent-death signals stop the native children if the supervisor dies. PostgreSQL performs WAL recovery on restart. A UI process failure cannot take down the database.

The `runtime.json` file pins the runtime and schema file hashes. The system accepts files with the same set of hashes in a different layout if the image moved them. The system rejects a different installation until you move `runtime.json` aside on purpose.

To perform a graceful shutdown, send SIGTERM to the supervisor on the host recorded in `service.json`. Never remove `owner.lock`, `postmaster.pid`, or WAL files to force a second writer. An expired heartbeat is an availability observation, not permission to bypass ownership.

Client-only coordinator commands with `status-*` disable the worker heartbeat thread of the SDK. They do not host a Temporal worker. This avoids a native exit crash after otherwise successful RPCs.

## History

The 2026-09-15 acceptance tests and their remaining limits are in [docs/history/DURABLE_CONTROL_ACCEPTANCE.md](../history/DURABLE_CONTROL_ACCEPTANCE.md).

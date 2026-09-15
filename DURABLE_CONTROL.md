# Temporal service recovery

The v2 control service runs the official Temporal Server with PostgreSQL. PostgreSQL data and WAL live under one private directory on shared storage. This replaces node-local development SQLite for new workflows; it does not change the immutable SQLite DEG databases used by annotation tools.

Temporal supports PostgreSQL and standalone server binaries. PostgreSQL requires a filesystem with the expected POSIX and synchronization semantics. The implementation additionally requires coherent cross-host `flock`; the acceptance deployment uses Sherlock Lustre. See the [Temporal deployment guide](https://docs.temporal.io/self-hosted-guide/deployment) and [PostgreSQL filesystem guidance](https://www.postgresql.org/docs/16/creating-cluster.html#CREATING-CLUSTER-FILESYSTEM).

## Start and connect

Install PostgreSQL, including `btree_gin`, and the matching Temporal server binaries and SQL schema. The tested versions are PostgreSQL 16.15, Temporal Server 1.32.0, Python SDK 1.32.0, and UI Server 2.54.1. The server release supplies `temporal-server` and `temporal-sql-tool`; its source release supplies `schema/postgresql/v12`. The UI binary is optional (`--ui-port 0` disables it).

Run in the configured RSI control environment on a resource the user has provided:

```bash
python -m ecarsi.temporal_service \
  --root /shared/rsi/control \
  --postgres-bin /shared/tools/postgresql/bin \
  --temporal-dir /shared/tools/temporal \
  --schema-dir /shared/tools/temporal/schema/postgresql/v12 \
  --bind "$(hostname -f)"
```

The root is created with mode `0700`; an existing root must have that mode and belong to the current user. Defaults are frontend port 7333, PostgreSQL port 55432, and UI port 8333. Internal Temporal services also use frontend offsets 1, 2, 6, 100, 101, 102, and 106. Use a private interface and SSH forwarding; this deployment does not provide a public authenticated API. PostgreSQL itself listens only on loopback and uses a generated SCRAM password, stored only in the private control directory.

```bash
python -m ecarsi.work_coordinator --service-root /shared/rsi/control \
  --task-queue ecarsi-durable-v2 worker

python -m ecarsi.work_coordinator --service-root /shared/rsi/control \
  --task-queue ecarsi-durable-v2 start-dataset dataset.json
```

The submission and worker must use the same task queue. Existing `--temporal HOST:PORT` commands remain available. The service creates the `default` namespace with 30-day completed-history retention. Published scientific artifacts and Pool/Bridge receipts have their own lifetimes; they are not deleted by Temporal history retention.

The observatory can follow the same record:

```bash
python -m ecarsi.dev_observatory serve --root /shared/rsi/runs \
  --temporal-service-root /shared/rsi/control --port 8765
```

Forward the observatory and UI ports separately. The development deployment retains the old SQLite UI on 8233 and serves the new PostgreSQL-backed UI on 8333.

## Recovery behavior

`service.json` publishes the serving address and generation every five seconds. A Coordinator started with `--service-root` stops polling an unavailable generation and connects to the replacement. It does not cancel accepted Pool or Bridge requests. A restarted Coordinator reconstructs its workflow from Temporal history; callers do not resubmit datasets after an ordinary process or service interruption.

On a newly provided node, run the same service command with the same root and the new bind address. The database and history remain in place. PostgreSQL uses `fsync=on`, `synchronous_commit=on`, and `full_page_writes=on`; this is not periodic copying of a node-local database. No Slurm job is acquired or released by this module.

The supervisor and native service children inherit one filesystem ownership lock. A second owner is rejected even while the first process heartbeat is late. Linux parent-death signals stop native service children if the supervisor dies. PostgreSQL performs WAL recovery on restart. A UI process failure cannot take down the database. The runtime and schema file hashes are pinned in `runtime.json`; a different installation is rejected until an explicit offline upgrade is performed.

Graceful shutdown is SIGTERM to the supervisor on the host recorded in `service.json`. Never remove `owner.lock`, `postmaster.pid`, or WAL files to force a second writer. An expired heartbeat is an availability observation, not authorization to bypass ownership.

## Acceptance and remaining limits

On 2026-09-15, an active workflow survived SIGKILL of the supervisor and resumed with the same workflow ID and run ID. The saved history replayed. A second test moved the same database from `sh03-01n03` to `sh04-14n18`; discovery reconnected the Coordinator and the waiting workflow completed without resubmission. Cross-host attempts to take the existing owner lock were rejected. A completed real Prostate Organize workflow remained available throughout both tests.

The service was subsequently returned to `sh03-01n03`. Evidence is in `durable-control-20260915/{recovery-acceptance,cross-host-recovery}.json` under the development run directory. The cross-host test's elapsed time includes correcting a test launcher argument; it is not a failover-time benchmark. A further SIGKILL/recovery test during both real dataset workflows retained their original run IDs without resubmission (`active-dataset-recovery.json`).

The legacy SQLite database also has an online-consistent cold archive on shared storage, created through SQLite's backup API while the old service remained running. Its integrity check passed; `legacy-archive.json` records the archive identity. This is an archive for restoration, not a live SQLite database on Lustre, and its history was not imported into PostgreSQL.

These checks cover process interruption and relocation between two live allocated hosts. Physical host power loss, network partitions, filesystem eviction/failure, replicated database failover, backup restoration, and sustained high-volume operation are not certified by these checks. A replacement service still needs to be started on an available resource; there is no always-on host or automatic Slurm provisioning. PostgreSQL may conservatively reject a stale PID file whose numeric PID matches an unrelated process on the replacement host; this wrapper preserves that safety check and requires diagnosis instead of deleting the file automatically.

The scientific operations use the previously accepted Apptainer science image. The current control launch still imports some Python packages from the existing `dl2025` environment alongside the pinned Temporal SDK; a fully pinned, separate control image remains deployment work. Native PostgreSQL/Temporal binaries and schemas are already pinned by this service.

# RSI v2 development observatory

The read-only page combines the isolated Warm Pool request/receipt records,
Agent Bridge replies and token usage, and published Organize outputs. The
generic timeline reads explicit `workflow_id`, `dataset_id`, and `unit_id`
trace fields on new Pool and Bridge submissions, plus a stable worker ID on
new Pool acceptances. It can show any operation type, including future OSP,
cross-sample and Zoom-in units. Overlapping tasks stack vertically inside one group per worker or service. The
viewer supports a time window of up to 24 hours, a dataset filter, and a
2,000-task response cap; narrow the window or filter when capped. Completed
requests are cached so they are not reread on every refresh. Wheel over the
plot to zoom around the pointer (30 seconds to 24 hours); selecting a preset
restores a live rolling window. The default `Latest activity` view finds the
last group of executed tasks separated from earlier work by at least five
minutes, then fits that interval with modest padding. Each Pool worker and
Agent Bridge has one labeled group; overlapping task bars stack inside it.
The label reports the peak concurrent task/request count in the selected
window, not a fixed execution slot or configured capacity. New `agent.turn` bars cover model turns and exclude worker-tool waiting.
Legacy Organize bars span whole planning sessions, including harness waits
and retries; they do not measure individual provider HTTP requests. Queued requests have no execution
bar, and queue wait appears in task details. Historical records lacking worker
IDs group by host; CPU affinity IDs appear only in task details.

CPU, RAM and GPU curves live in a separate, initially collapsed `Worker
resource history` section using the same time window. Resource refreshes do
not rebuild an unchanged task chart. Running bars stop at the present even
when a custom window extends into the future.
Task bars and connectors use one stable muted color per dataset; service is
expressed by the lane, while a red inset outline marks failure.

The separate `Compute Warm Pool` panel groups worker identities by physical
host. Each worker retains its own advertised CPU/RAM budget, unfinished-task
reservations, current CPU measurement and five-minute sample average. Host
RAM and visible GPU measurements appear once per host and are never added to
pool capacity. Telemetry is sampled every 30 seconds and the page refreshes
every 10 seconds; a recent sample means the supervisor is reporting, not proof
of HyperQueue membership. Identities with no sample in 90 seconds move to the
text history with their last observed time; old JSONL tails remain readable
across day boundaries. A missing GPU sample is unknown, not zero usage.

Same-host workers must use distinct work directories and disjoint CPU IDs;
the current adapter prevents overlapping CPUs with host-scoped locks. Slurm
job IDs are recorded from the joining process environment on new launches;
older or non-Slurm launches show `not recorded`. This does not yet implement
Slurm allocation discovery, aggregate memory-budget validation, or GPU
scheduling. The panel reports explicit v2 worker budgets, not entire Slurm
allocations. Old task receipts also supply historical hosts when no worker
identity was saved; these show last task activity and unknown membership,
not an invented departure time. It reads only this pool's records.

Curved connectors join completed upstream requests to started downstream
requests across Pool and Bridge lanes. Future workflow modules can record
predecessor request IDs in optional `trace.depends_on`; for example a plan
could have `"depends_on":["run-17.prepare"]`. Existing Organize receipts use
its known prepare → plan → execute order within the same workflow. No link is
inferred from dataset name or submission order alone, so separate retries remain
separate. Hover a task to highlight its workflow. On crowded timelines the
wide connectors fade until a workflow is highlighted. Thin connectors retain
50% opacity to remain legible against the background.

Each `depends_on` entry identifies a predecessor request within the same
workflow. Lists support fan-out, fan-in and successive iterations with distinct
request IDs; explicit dependencies are not inferred from execution order or
discarded because of status or clock skew. Task glyphs follow their duration,
with a one-pixel visibility minimum and a separate wider mouse target. Ribbons
are drawn even across subpixel gaps or overlapping glyphs and are repositioned
when the viewport changes. The note shows rendered versus loaded edge counts;
tasks outside the selected window or response cap cannot provide visible endpoints.

Run `node tests/test_dev_observatory_ui.cjs` for the lightweight geometry and
dependency checks. For a browser with Playwright available, run
`node tests/check_dev_observatory_browser.cjs <viewer-url>` against existing
records. It checks the live dependency chain at different scales, then renders
100 synthetic datasets with branches, joins and another iteration in the
browser only. Set `OBSERVATORY_ARTIFACTS` to save a screenshot. This check does
not submit scientific work or model requests.

New workflow modules should set the same optional `trace` object on each Pool
or Bridge submission, for example `{"workflow_id":"osp/run-17",
"dataset_id":"dataset-17","unit_id":"per-sample.compute","depends_on":["run-17.partition"]}`. Without it, a
new operation is shown as `Unattributed`; the viewer never guesses a dataset
from an arbitrary future request ID. `GET /api/timeline?since=<epoch>&until=<epoch>&dataset=<text>`
returns the selected task records and downsampled resource points.

New v2 workers append host resource samples every 30 seconds to UTC-day JSONL
files under `<pool-root>/workers/<worker-id>/`: allocated-core CPU usage,
whole-host RAM usage, and accessible-GPU utilization and memory. The server
returns at most 240 averaged points per worker on the same task time axis.
Old resource samples cannot be reconstructed from task reservations. It polls
the filesystem every 10 seconds. It does not start the Scheduler, Worker,
Coordinator, model requests or dataset computation. The page and the Temporal
Web UI listen on the current allocated host's private cluster interface; the
browser connects through SSH.

The current host is `sh03-01n03`. The observatory runs in a detached tmux
session on `/scratch/users/chensj16/.tmux/sh03-01n03.sock` and listens on the
node's private cluster address. The concurrent Organize test started a new
Temporal development service using node-local SQLite, avoiding the previous
shared-filesystem lock errors:

- `rsi_v2_observatory`: `http://127.0.0.1:8765/`
- Temporal UI: `http://127.0.0.1:8233/`

The 2026-09-14 concurrent test's service commands, process records, workflow
specifications and logs are under
`/scratch/users/chensj16/eca-runs/warmpool-v2-development/concurrent-organize-20260914-185643/`.
Its three workers run on `sh03-13n22`, `sh03-15n05`, and `sh04-14n18`, each
with an explicit test budget of 2 CPUs and 16 GiB. The Temporal database is
under `/lscratch/chensj16/concurrent-organize-20260914-185643/`; this is
node-local test persistence, not automatic failover to another node. These
test services were launched as detached processes; their `*-launch.json`
records and per-worker `worker.json` identify them, rather than tmux sessions.

From a **laptop**, open one SSH tunnel through the Sherlock login node and keep
the terminal open. This does not require a second SSH login to the compute node:

```bash
ssh -N -L 8765:sh03-01n03:8765 -L 8233:sh03-01n03:8233 \
  chensj16@login.sherlock.stanford.edu
```

Then open `http://127.0.0.1:8765/`. The Temporal button is enabled when its
UI is reachable. Previous successful Organize tests used an in-memory Temporal
server, so their Workflow histories cannot be recovered; the timeline uses
real Pool/Bridge receipts instead. The attempted development SQLite persistence
under `warmpool-v2-development/temporal-persistent/` is **not validated**:
the shared-filesystem server has reported SQLite lock and visibility-index
errors. An HTTP 200 from its UI does not prove Workflow queries or restart
recovery work. This is not a production multi-host Temporal deployment.
HyperQueue's native dashboard is terminal-only; the page displays its durable
RSI request/receipt records. The current v2 test is separate from production
RSI; production dataset work was not resumed.

To inspect service output or stop only these viewers:

```bash
tmux -S /scratch/users/chensj16/.tmux/sh03-01n03.sock capture-pane -pt rsi_v2_observatory
tmux -S /scratch/users/chensj16/.tmux/sh03-01n03.sock kill-session -t rsi_v2_observatory
```

The dashboard is implemented with the Python standard library in
`ecarsi/observatory.py` and `ecarsi/observatory.html`. The viewer accepts
`--pool-root` and `--bridge-root` to read a shared Pool and Bridge outside the
current development root. To move the viewer after a node change, recreate its
tmux session on the new allocated host and update the SSH tunnel hostname.
The unvalidated Temporal development command was:

```bash
python -m ecarsi.observatory temporal-ui \
  --database /scratch/users/chensj16/eca-runs/warmpool-v2-development/temporal-persistent/temporal.sqlite \
  --bind 0.0.0.0 --port 7233 --ui-port 8233
```

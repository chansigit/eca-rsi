# RSI v2 development observatory

The read-only page combines the isolated Warm Pool request/receipt records,
Agent Bridge replies and token usage, and published Organize outputs. The
generic timeline reads explicit `workflow_id`, `dataset_id`, and `unit_id`
trace fields on new Pool and Bridge submissions, plus a stable worker ID on
new Pool acceptances. It can show any operation type, including future OSP,
cross-sample and Zoom-in units. Concurrent tasks get separate lanes. The
viewer supports a time window of up to 24 hours, a dataset filter, and a
2,000-task response cap; narrow the window or filter when capped. Completed
requests are cached so they are not reread on every refresh. Wheel over the
plot to zoom around the pointer (30 seconds to 24 hours); selecting a preset
restores a live rolling window. Pool lanes use actual worker IDs, with extra
display rows only when tasks overlap. Resource curves sit below that worker's
tasks. Bridge lanes only separate overlapping calls; executor identity is not
recorded. Queue wait for started tasks appears in task details, not on a future
worker lane. Historical Organize records lack trace fields and are explicitly marked as inferred;
historical worker lanes use host/CPU IDs.

New workflow modules should set the same optional `trace` object on each Pool
or Bridge submission, for example `{"workflow_id":"osp/run-17",
"dataset_id":"dataset-17","unit_id":"per-sample.compute"}`. Without it, a
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
node's private cluster address. Temporal is currently stopped after its
SQLite errors:

- `rsi_v2_observatory`: `http://127.0.0.1:8765/`
- `rsi_v2_temporal`: offline pending persistence repair

From a **laptop**, open one SSH tunnel through the Sherlock login node and keep
the terminal open. This does not require a second SSH login to the compute node:

```bash
ssh -N -L 8765:sh03-01n03:8765 -L 8233:sh03-01n03:8233 \
  chensj16@login.sherlock.stanford.edu
```

Then open `http://127.0.0.1:8765/`. The Temporal button remains disabled while
the service is offline. Previous successful Organize tests used an in-memory Temporal
server, so their Workflow histories cannot be recovered; the timeline uses
real Pool/Bridge receipts instead. The attempted development SQLite persistence
under `warmpool-v2-development/temporal-persistent/` is **not validated**:
the shared-filesystem server has reported SQLite lock and visibility-index
errors. An HTTP 200 from its UI does not prove Workflow queries or restart
recovery work. This is not a production multi-host Temporal deployment.
HyperQueue's native dashboard is terminal-only; the page displays its durable
RSI request/receipt records. Both production RSI and v2 computation remain
stopped.

To inspect service output or stop only these viewers:

```bash
tmux -S /scratch/users/chensj16/.tmux/sh03-01n03.sock capture-pane -pt rsi_v2_observatory
tmux -S /scratch/users/chensj16/.tmux/sh03-01n03.sock capture-pane -pt rsi_v2_temporal
tmux -S /scratch/users/chensj16/.tmux/sh03-01n03.sock kill-session -t rsi_v2_observatory
tmux -S /scratch/users/chensj16/.tmux/sh03-01n03.sock kill-session -t rsi_v2_temporal
```

The dashboard is implemented with the Python standard library in
`ecarsi/dev_observatory.py` and `ecarsi/dev_observatory.html`. The viewer accepts
`--pool-root` and `--bridge-root` to read a shared Pool and Bridge outside the
current development root. To move the viewer after a node change, recreate its
tmux session on the new allocated host and update the SSH tunnel hostname.
The unvalidated Temporal development command was:

```bash
python -m ecarsi.dev_observatory temporal-ui \
  --database /scratch/users/chensj16/eca-runs/warmpool-v2-development/temporal-persistent/temporal.sqlite \
  --bind 0.0.0.0 --port 7233 --ui-port 8233
```

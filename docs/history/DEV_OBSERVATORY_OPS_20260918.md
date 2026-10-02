# Development observatory operations — 2026-09-14 to 2026-09-18

Moved from `docs/control-plane/DEV_OBSERVATORY.md` on 2026-10-02. The standalone observatory process described here was merged into Periscope (`/_control/`) on 2026-09-18. Hosts, sockets and paths are those of September 2026.

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

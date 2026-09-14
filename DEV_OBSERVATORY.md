# RSI v2 development observatory

The read-only page combines the isolated Warm Pool request/receipt records,
Agent Bridge replies and token usage, and published Organize outputs. It polls
the filesystem every 10 seconds. It does not start the Scheduler, Worker,
Coordinator, model requests or dataset computation. The page and the Temporal
Web UI listen on the current allocated host's private cluster interface; the
browser connects through SSH.

The current host is `sh03-01n03`. Both services run in detached tmux sessions
on `/scratch/users/chensj16/.tmux/sh03-01n03.sock`. The observatory listens
on the node's private cluster address; the isolated Temporal development UI
also accepts connections from that address for one-hop SSH forwarding:

- `rsi_v2_observatory`: `http://127.0.0.1:8765/`
- `rsi_v2_temporal`: `http://127.0.0.1:8233/`

From a **laptop**, open one SSH tunnel through the Sherlock login node and keep
the terminal open. This does not require a second SSH login to the compute node:

```bash
ssh -N -L 8765:sh03-01n03:8765 -L 8233:sh03-01n03:8233 \
  chensj16@login.sherlock.stanford.edu
```

Then open `http://127.0.0.1:8765/`. The Temporal button uses the second
forwarded port. The source Temporal development database contains **zero**
retained Workflow executions: previous successful tests used an in-memory
development server. The native UI is available but currently has no old runs;
the RSI output receipts on the main page are real historical evidence. The
Temporal process uses an SQLite **copy** under
`warmpool-v2-development/visualization/`, so it cannot alter the old database.
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
`ecarsi/dev_observatory.py` and `ecarsi/dev_observatory.html`. To restart after
a node change, use a fresh Temporal snapshot filename and recreate the two
tmux sessions on the new allocated host; update the SSH tunnel hostname.

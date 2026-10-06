# ops/: running a deployment

Scripts the owner runs on the control-plane node. Every one reads `~/.config/ecarsi/deployment.env`
(template `examples/deployment.env`), so none holds a machine path. The deployment directory links here:
`$BASE/ops -> <eca-rsi checkout>/ops` and `$BASE/control-plane.sh -> ops/control-plane.sh` (INSTALL.md A.3).
Logs of builds and switches go to `$BASE/control-logs/`.

| Script | Does |
|---|---|
| `control-plane.sh start\|stop\|restart\|status\|report [component…]` | the plane's components; the launcher itself comes from the control image (`container/control-plane.sh`), or from the published version named by `VERSION` / `INFRA` |
| `publish-version.sh [commit]` | a commit as a read-only version in `$CODE_HOME/versions/<commit12>/`, import-checked in both images (decision 0019) |
| `start-version.sh <name> [n]` | that version's coordinators (on its own task queue) and runners, next to the running ones |
| `set-current.sh <name>` | new datasets and `run.sh` use that version from now on; refused while no coordinator serves it |
| `worker-node.sh` | Slurm job script: the job itself becomes a pool worker (`sbatch … ops/worker-node.sh`) |
| `start-periscope.sh`, `restart-periscope.sh` | Periscope from the science image, public through ngrok when `PERISCOPE_DOMAIN` is set |
| `run.sh control\|compute [--dev] <python args>` | Python inside an image; `runpy.sh`, `runsci.sh` and their `-dev` forms are shorthands. `--dev` runs `$DEV_WORKTREE` instead of the image snapshot; `VERSION=<name>` a published version, by default the current one |
| `count-wf.py` | running Temporal executions by type; switch images only at 0 |
| `replay-check.py` | replays running workflow histories against the dev checkout; run before deploying a `control/` change |
| `build-images-update.sh` | new image pair from the current one with a fresh eca-rsi snapshot (and optional wheels) |
| `build-images.sh` | full rebuild from base images (Temporal, PostgreSQL, HQ added) |
| `switch-images.sh <stamp> <science sha256> <host:job>…` | stop, repoint `deployment.env`, start, re-add workers |
| `gate.py start\|wait <run_id>\|check <run root>` | the release gate after a switch: `~/.config/ecarsi/gate-dataset.json` end to end, then the checks (released, nothing degraded, lineage reports, display zone, archive) |
| `node-check.sh` | one look: Slurm jobs, HQ, scheduler, productivity, recent failures, coordinator memory |
| `fail-recent.py` | pool failures of the last ~70 min, grouped by reason |
| `pool-smoke.py [N] [sleep]` | trivial requests through the scheduler, without a dataset |
| `resume-one.py <workflow id> <reason>`, `resume-many.py <reason> <workflow id>…` | resume closed dataset workflows, each on the queue (version) it was started on |

Python helpers run inside an image: `bash ops/runpy.sh ops/count-wf.py`.
Tests: `bash ops/runsci-dev.sh -m pytest -q tests` (the whole suite, in the compute image).

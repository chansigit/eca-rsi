# ops/: running a deployment

Scripts the owner runs on the control-plane node. Every one reads `~/.config/ecarsi/deployment.env`
(template `examples/deployment.env`), so none holds a machine path. The deployment directory links here:
`$BASE/ops -> <eca-rsi checkout>/ops` and `$BASE/control-plane.sh -> ops/control-plane.sh` (INSTALL.md A.3).
Logs of builds and switches go to `$BASE/control-logs/`.

| Script | Does |
|---|---|
| `control-plane.sh start\|stop\|restart\|status\|report [component…]` | the plane's components; the launcher itself comes from the control image (`container/control-plane.sh`), or from the published version named by `VERSION` / `INFRA`; once a version is current, `start\|stop\|restart\|status` take its coordinators and runners from it and the shared components from the image |
| `publish-version.sh [commit]` | a commit as a read-only version in `$CODE_HOME/versions/<commit12>/`, import-checked in both images (decision 0019) |
| `start-version.sh <name> [n]` | that version's coordinators (on its own task queue) and runners, next to the running ones |
| `set-current.sh <name>` | new datasets and `run.sh` use that version from now on; refused while no coordinator serves it |
| `retire-version.sh <name>` | stops a version's coordinators and runners once its queue has no running execution; lists its datasets that ended unfinished; refused for the current version; the directory stays |
| `worker-node.sh` | Slurm job script: the job itself becomes a pool worker and leaves with the job (`sbatch … ops/worker-node.sh`; Sherlock's recipe, INSTALL.md A.7) |
| `start-periscope.sh`, `restart-periscope.sh` | Periscope from the science image, on `INFRA`'s version when set; local only (your own `ssh -L` forward) |
| `rtop [--interval N] [--once]` | `eca-rsi top`: the records of Periscope's `/_control/` in a colour terminal, every 2 s, `q` quits; this checkout's code in the science image, read-only, about 0.1 s of CPU per refresh; run it on the plane node; on PATH: `ln -s $CODE_HOME/src/eca-rsi/ops/rtop ~/.local/bin/rtop` |
| `run.sh control\|compute [--dev] <python args>` | Python inside an image; `runpy.sh`, `runsci.sh` and their `-dev` forms are shorthands. `--dev` runs `$DEV_WORKTREE` instead of the image snapshot; `VERSION=<name>` a published version, by default the current one |
| `count-wf.py` | running Temporal executions by type and by task queue (one per version) |
| `replay-check.py` | replays workflow histories against the dev checkout; no longer a deployment step, since a version replays only its own histories (0019) |
| `build-images-update.sh` | new image pair from the current one with a fresh eca-rsi snapshot (and optional wheels) |
| `build-images.sh` | full rebuild from base images (Temporal, PostgreSQL, HQ added) |
| `switch-images.sh <stamp> <science sha256> <host:job>…` | stop, repoint `deployment.env`, start, re-add workers |
| `gate.py start\|wait <run_id>\|check <run root>` | the release gate after a switch: `~/.config/ecarsi/gate-dataset.json` end to end, then the checks (released, nothing degraded, lineage reports, display zone, archive) |
| `node-check.sh` | one look: Slurm jobs, HQ, scheduler, productivity, recent failures, coordinator memory |
| `fail-recent.py` | pool failures of the last ~70 min, grouped by reason |
| `turn-report.py <run root>` | a run's model-turn attempts by error class and by size (input tokens, image bytes), #31 |
| `plane-node.sh` | the control plane as a Slurm job: starts every component, the current version and Periscope, stops them before the walltime, takes over from the running plane job (INSTALL.md A.6), #62 |
| `run-health.py <run root \| batch dir> ...` or `--tag <text>` | where a batch's time goes: per run elapsed time split into DEG, compute, tool calls, queue and the gap (model turns, orchestration), per pool operation core-hours, efficiency, durations, failures and RSS kills, integration step times from the integrations' stdout.log (until the pruner removes them), model turns per stage with rewritten submissions; run it before the batch's folders are deleted, #60 |
| `new-bridge.sh <name> <catalog.json>` | a second bridge `$STATE/<name>` with its own model catalog, routing and service copied from the main one (#54); start it with `BRIDGE=$STATE/<name> INFRA=<version> bash ops/control-plane.sh start bridge`; a dataset or gate (`gate.py start --bridge=`) uses it through its `bridge_root` |
| `pool-smoke.py [N] [sleep]` | trivial requests through the scheduler, without a dataset |
| `resume-one.py <workflow id> <reason>`, `resume-many.py <reason> <workflow id>…` | resume closed dataset workflows, each on the queue (version) it was started on |

Python helpers run inside an image: `bash ops/runpy.sh ops/count-wf.py`.
Tests: `bash ops/runsci-dev.sh -m pytest -q tests` (the whole suite, in the compute image).

Images (#36): keep every image pair that a published version names in its `version.json` while that version is
current, is `INFRA`, or has datasets that ended unfinished (`retire-version.sh` lists them: a resume runs in the
version's own images), and the base images in `images/base/`. Delete the rest by hand once the next version's gate
has passed; no script removes images.

# 0019 Code versions run side by side

Decided 2026-10-06 by the owner; replaces the proposal 0015. Built in steps: the code below is on main (step 1);
nothing uses it until a version is published and started.

**Context.** A change to `ecarsi/stages/`, `ecarsi/control/` or `session.py` could only ship as a new image pair
switched in at zero running executions (0005, 0010), followed by a gate. Long datasets made that wait hours, and a
gate could not run next to production on different code. Debugging was slow for the same reason.

**Decision.** A *version* is a commit plus the two images it runs in.

- **Publishing** (`ops/publish-version.sh <commit>`) copies the commit's packages (`git archive`) to a read-only
  directory `$CODE_HOME/versions/<commit12>/` with `version.json`, byte-compiles them and checks that they import in
  both images. A code-only change needs no image build. `ecarsi.version()` reads `version.json` beside the packages;
  a checkout or an image snapshot has none and behaves as before.
- **One Temporal task queue per version**, `ecarsi-<commit12>` (`ecarsi.task_queue()`). Temporal keeps an
  execution's activities, child workflows and continue-as-new on the queue it started on, so every execution stays
  on the version that started it; workflow code never names a queue (tests/test_versions.py). Resuming a run uses
  the queue it was started on. Coordinators of a version serve only its queue.
- **Pool requests carry the version.** `warm_pool.state.submit` puts the submitter's version directory first on
  the request's Python path (by default the caller's own version: a version's coordinators and sessions submit for
  it). Workers declare the *image* runtime, so the request also records `placement`, the digest of the image
  runtime, and HQ places it on any worker of that image (`backend.placement`). A request may not pin a file of
  another version. Requests without a version are unchanged.
- **Agent turns stay on their version.** A bridge request records the submitting version. The bridge (shared)
  plans the turn with that version's adapter and never upgrades it; the turn goes to that version's resident
  runners (`<version>.<model key>`) or, without them, to a pool task on the version's code.
- **Shared components** (Temporal, HQ, scheduler, bridge, fleet-status, pruner) run from `INFRA=<version>` in
  `deployment.env`; restarting the scheduler leaves the workers connected (0006).
- **Starting and choosing.** `ops/start-version.sh <name> [n]` starts a version's coordinators and runners (at most
  8 coordinators across versions: each holds 6-7 GB on a 96 GB node). `ops/set-current.sh <name>` makes `ops/run.sh`,
  and so new datasets and the ops helpers, use it, after checking that its queue has a coordinator. `VERSION=<name>`
  runs any ops command (a gate, say) on another version next to production.

**Why task queues and not Temporal's worker versioning (0015).** A queue per version needs nothing from the server,
is visible by name in every tool, and is the same mechanism the tests run.

**What it costs.** Old versions stay until their executions end; their directories are never deleted. Every
version keeps its own coordinators while it drains. Still to build: a version that brings new images (the compute
image first needs a dependency lock), retiring versions, contracts on the files the shared components and versions
both read, and showing the version on Periscope; then the workflow `deprecate_patch` lines and the replay check stop
being deployment steps (0003), and `switch-images.sh` retires.

**Status 2026-10-06.** The first version became current at 04:10 after its gate passed beside production; the
second (the kernels without their standalone flows, #28) at 11:05 after its gate ran beside the first one's, every
workflow on its own queue and no turn planned with another version's adapter. `ops/control-plane.sh` starts the
current version's coordinators and runners; `ops/retire-version.sh` retires a drained version; the
`deprecate_patch` lines are gone and the replay check is no longer a deployment step. The files a version shares
with the shared components have contracts (`pool-request/1`, `turn-plan/1`, `receipt/1`, `turn/1`; `contracts.SHARED`)
and `publish-version.sh` refuses a version that writes a version of them the shared side (`INFRA` or the image) does
not know: update `INFRA` first. Periscope's `/_control/` names each running dataset's version. The compute image's
environment is locked (`container/science-requirements.lock`, reproduced file for file). Versions can bring their own images
(INSTALL.md A.11): the pool registers several image runtimes and workers declare the one of their image. Not yet
exercised with a real second image; until it is, `switch-images.sh` stays for image changes.

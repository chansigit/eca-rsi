# Design decisions

One page per decision that shapes ECA-RSI: what was decided, why, and what it costs. Read these when
something looks strange; most strange things are one of them. New decisions get the next number; a
decision that is replaced keeps its page and says so at the top.

| # | Decision |
|---|---|
| [0001](0001-kernels-compute-agents-decide-host-checks.md) | Kernels compute, agents make narrow decisions, the host checks every decision |
| [0002](0002-stop-on-cell-counts.md) | A unit stops on cell counts only |
| [0003](0003-temporal-for-orchestration.md) | Temporal runs the orchestration |
| [0004](0004-file-protocols.md) | Subsystems talk through request folders on shared storage |
| [0005](0005-pin-programs-by-content.md) | Every request pins its program files by content |
| [0006](0006-hyperqueue-priorities-owner-requested-workers.md) | HyperQueue orders the work; the owner requests the workers |
| [0007](0007-model-turn-service.md) | Model turns run in their own service, not in the compute pool |
| [0008](0008-stateless-coordinators.md) | Four stateless coordinators with two workflow slots each |
| [0009](0009-two-zones.md) | A run has a work tree and a display zone |
| [0010](0010-production-code-is-the-image.md) | Production code is the image snapshot |
| [0011](0011-settings-in-one-directory.md) | Every setting in `~/.config/ecarsi`, machine paths only in `deployment.env` |
| [0012](0012-one-execution-path.md) | One execution path: the local path was removed |
| [0013](0013-degraded-results-are-recorded.md) | A step that fails without failing the run leaves a record |
| [0014](0014-boundaries-are-tested.md) | Subsystem boundaries are written down and tested |
| [0015](0015-image-versions-side-by-side.md) | Two image versions side by side (proposed, not built) |
| [0016](0016-samples-and-batches-from-eca-pp.md) | Samples and batches come from ECA-PP; big samples run as chunks |
| [0017](0017-stress-population-policy.md) | Stress, dissociation and dying removals follow a policy the code checks |
| [0018](0018-one-repository.md) | One repository: the kernels and the agent harness live in eca-rsi |

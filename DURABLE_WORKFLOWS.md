# Durable RSI workflows

Status: isolated development baseline and architecture proposal; no production migration.
Branch: `feature/durable-workflows`.

## Component names and proposed responsibility boundary

Use four public component names: **Work Coordinator**, **Agent Bridge**,
**Warm Pool Scheduler**, and **Warm Pool Worker**. There is no additional
"Warm Pool Coordinator"; older generic coordinator/scheduler references below
refer to Work Coordinator / Warm Pool Scheduler respectively. Temporal and HQ
retain their own implementation-specific terminology.

| Component | Owns | Does not own |
| --- | --- | --- |
| Work Coordinator | Versioned operation dependencies, validated decisions, iteration/convergence, cancellation intent, logical completion, and execution requirements | Per-worker CPU/GPU grants, provider quotas, or executing numerical kernels |
| Agent Bridge | Model request queues, shared quota groups, permitted model routing, persistent responses/conversation state | Scientific workflow progression or direct execution of heavy tools |
| Warm Pool Scheduler | Admission of submitted ready computations, worker selection, concrete resource grants, worker lifecycle observations and execution-attempt placement | Biological dependencies, model decisions, convergence, or deciding which upstream scientific result is valid |
| Warm Pool Worker | Execute bounded authorized attempts, runtime/resource enforcement, data preparation, process-tree supervision and result receipts | Inventing new workflow steps or requesting Slurm allocations |

Two states answer different questions: Work Coordinator's `ready` means the
scientific dependencies permit an operation; the execution backend's `queued`
means that ready operation is waiting for execution resources. The coordinator
stores the backend handle rather than independently predicting/assigning its
worker or maintaining a second resource reservation for the same compute.

## Accepted deployment model: a pool in every mode (2026-09-14)

All numerical execution follows one route:
Work Coordinator -> Warm Pool Scheduler -> Warm Pool Worker.
Local execution is a single-worker pool; distributed execution adds workers on
ordinary machines and/or user-provisioned Slurm allocations. There is no separate
local-direct bypass. Pool does not imply Slurm or a specific scheduler engine.

Local startup should be one command with explicit CPU/RAM/GPU/disk budgets,
not manual setup of four services or implicit use of all host resources.
Co-located components have independent supervision: a coordinator crash must
not terminate healthy worker computations. The subprocess execution module is
Worker library code, not a fifth service or an alternative coordinator route.
Lightweight control updates and validation remain within the owning service.

Only Warm Pool Scheduler grants compute resources. A submission timeout does
not authorize another attempt: reconcile or fence/cancel the original first.
Placement does not change scientific task identity unless the numerical
implementation changes. Both deployment topologies use the shared Agent Bridge
and identical operation, receipt and recovery contracts. Temporal and HyperQueue
remain implementation candidates; their local operational cost needs validation.

Resource requests cover only their operations, never a whole dataset's peak
budget while waiting for a model. Bridge limits model calls; Warm Pool Scheduler
limits numerical work. Shared storage capacity is accounted
once per storage domain, independently of per-worker RAM reservations.

## Accepted execution model: option B (2026-09-14)

The user selected persistent interaction state plus isolated compute attempts.
All datasets share one logical Agent Bridge, with independent conversation and
scientific-decision state. Model replies and tool requests are persisted before
the next action. Waiting for a tool releases model-call capacity and does not
retain a dataset-specific process holding expression matrices.

Bridge execution replicas share global quota accounting. Configured quota groups
represent provider/account/model limits; different keys or URLs do not imply
independent quotas. Prefer the primary model and allow new requests to overflow
to configured, task-eligible alternatives before overload causes timeouts.
Preserve the actual model, retries, token use and routing reason for each call.
Changing models must preserve compatible context and existing accepted decisions.

Compute attempts run in separate processes under explicit runtime/resource
contracts. Node disk caches may be reused; resident matrix sessions are deferred
until measured reload costs justify their lifecycle and memory management.
This decision does not select Temporal or HyperQueue, or authorize production
migration before recovery acceptance.

Remaining policy decisions, with proposed defaults (not yet user-approved):
- Control-service/database placement remains open. The accepted outage behavior
  below replaces immediate coordinator failover as a first-version requirement.
- Model eligibility and budget: configure task-purpose eligibility, quota groups,
  concurrency and token/cost limits; never silently downgrade important decisions.
- Repeated scientific failure/nonconvergence: bounded continuation, then a visible
  stopped state while independent datasets continue; never mark it converged.
- Reliable artifact destinations/retention: retain inputs, accepted decisions,
  final results and recovery checkpoints; auto-evict only authorized caches.
- Migration: new test runs first, then new production runs; existing runs retain
  their executor unless an explicit validated checkpoint migration is selected.

## Accepted outage behavior (2026-09-14)

The user requires independent components that finish their pre-authorized work,
wait without tearing down healthy workers, and reconcile automatically when a
service returns. Immediate replacement of a failed coordinator by a worker is
not required. Nodes and allocations remain user-provisioned.

| Failure | Required behavior |
| --- | --- |
| Coordinator unavailable | Scheduler stops expanding dispatch beyond the already granted bounded work. Workers finish their accepted work and persist receipts, then remain idle and reconnect while their allocations remain valid. Bridge likewise finishes accepted, budgeted model calls and retains replies. Neither invents new scientific decisions or tool work. |
| Bridge unavailable | Compute already runnable from accepted inputs continues. Only operations requiring new model results wait. Coordinator persists pending model intents for replay by stable ID; restarted Bridge reconciles recorded replies before retrying. |
| Worker unavailable or allocation expired | Stop assigning to that worker; keep pending work. Other eligible workers continue. Replacement workers automatically advertise identity, resources, runtime and visible stores, then receive work after reconciliation. |
| Scheduler unavailable | Worker numerical processes finish their bounded grants independently of scheduler RPCs. Persist receipts and wait for reconnect; do not kill healthy computation solely because dispatch service disappeared. |
| All eligible workers absent | Queue waits durably. No automatic Slurm submission or allocation release. Resume automatically once a user starts compatible workers. |

"Pre-authorized work" is a finite manifest already accepted by the executor:
operation/attempt IDs, immutable inputs, allowed local dependencies, resource
limits, runtime, outputs and execution deadline. It may include a small bounded
bundle, not an unbounded dataset loop. No dependency on a new model decision
can be crossed while that decision is unavailable. External dependencies such
as required storage must still be available; otherwise checkpoint or stop safely.

Each component runs under a supervisor that restarts its process on an existing
authorized host, with bounded backoff and visible persistent failures. It checks
allocation validity before restarting workers. Heartbeat loss disables new
dispatch; it does not by itself prove an already granted attempt stopped.
Never release its resources or reissue the attempt solely on a missed heartbeat.
Reconcile the executor, process fencing, grant deadline and durable receipts
before authorizing a replacement; no reliance on lease expiry alone to stop code.

On reconnect: establish current service identity, enumerate accepted/running
attempts and pending receipts, verify result identities, acknowledge accepted
results, then issue new work. Receipts survive missed notifications. If central
storage is temporarily unavailable, spool locally without claiming global
durability; loss of that node may require recomputation. Expiring nodes prioritize
publishing required persistent replicas within their remaining time.

If a service host expires, automatic process restart cannot bring that host back.
Restart on another already authorized host when placement permits, or wait for a
user-provided host. Discovery/reconnect and reconciliation require no manual
per-dataset resubmission or address editing. The first-version requirement is
recoverable waiting and automatic resumption, not uninterrupted coordination.

Acceptance must cover asymmetric network loss, crash between result write and
acknowledgement, prolonged coordinator/Bridge downtime with useful compute still
finishing, worker expiry, no-worker waiting, and automatic reconnection. Verify
no duplicate accepted result, no stale publication and no unintended allocation.

## Isolation

The production worktree stays on main. DURABLE_BASELINE.json records hashes of
151 copied source/document/test files, including the fixes subsequently committed on main as `f85414e`.
Untracked Slurm logs and ignored runtime data were not copied. Existing config,
registry, scheduler endpoints, service processes, allocations and output roots
were not changed. The copied code still has production defaults: do not launch
it against those defaults. All experiments require explicit development endpoints,
separate state/output roots, and fake compute/model providers initially. Do not
install this worktree into an environment used by the running system.

## Initial choice: evaluate Temporal without committing to it

This is a recommendation, not a claim of production validation. Reuse durable
workflow execution rather than writing a new event journal, replay engine and
leader election mechanism. Keep RSI scientific contracts and resource scheduling.

| Candidate | Useful capability | Constraint / decision |
| --- | --- | --- |
| Temporal | Persistent workflow history, task queues, retries, timers, signals, Python SDK, asynchronous activity completion | Best initial match for node loss and long model waits. Requires Temporal service and supported persistent database; operational cost must be measured. |
| DBOS | Python workflow/step persistence and database-backed queues with a lighter application architecture | Worth retaining as alternative. Multi-executor crash recovery needs explicit recovery management or Conductor; not automatically solved just by a Postgres connection. |
| Prefect | Flow/task orchestration, self-hosted server, worker/work-pool operations | Viable comparison for batch operations; test exact mid-stage crash/recovery and model wait semantics before treating it as equivalent to Temporal. |
| Existing Dask | Numerical execution, worker/resource integration already present | Keep as an execution candidate. Scheduler failure requires resubmission; it must not be the only durable record of outstanding work. |

Official sources reviewed:
- https://docs.temporal.io/self-hosted-guide
- https://github.com/temporalio/sdk-python
- https://github.com/temporalio/documentation/blob/main/docs/encyclopedia/activities/activity-execution.mdx
- https://docs.dbos.dev/architecture
- https://docs.dbos.dev/production/workflow-recovery
- https://docs.dbos.dev/production/hosting-conductor
- https://docs.prefect.io/v3/concepts/server
- https://docs.prefect.io/v3/concepts/workers
- https://distributed.dask.org/en/latest/resilience.html

## Four responsibilities

Workflow coordination owns stage dependencies, accepted scientific decisions,
and semantic retries. With Temporal, orchestration code runs in replaceable
workflow workers; Temporal service/database retains execution history. There
need not be a single permanent Python coordinator holding all datasets.

The pool scheduler owns CPU/memory/GPU/time admission into user-provisioned
allocations. Workers execute numerical tasks inside pinned Apptainer runtimes.
Agent Bridge owns model requests, rate limits, provider fallback and usage logs.
Do not confuse Temporal task queues with Slurm resource admission, or replace
the latter with generic workflow concurrency limits.

A compute request contains a stable logical task ID, workflow/run identity,
stage identity, input/config/runtime identities, resource requirements, and
output location. Each execution attempt gets a separate ID. Persist acceptance
before acknowledging it. Retrying a submission with the same identity must
return the existing task; conflicting contents for that identity must fail.

Workers publish attempt-specific results and verified manifests. Accepting a
result must be fenced against expired attempts; late results cannot overwrite
an accepted generation. Recovery reconciles recorded attempts, live workers and
published receipts before resubmitting. An asynchronous notification is a hint:
a missed notification must not lose a persisted result.

AI requests use the same stable identity principle. Persist provider responses
before advancing the workflow. Unknown external completion is an explicit state:
provider APIs without idempotency/result lookup cannot promise exactly-once
billing. Never interpret a workflow engine's replay guarantees as exactly-once
external effects.

## First vertical slice

1. Establish primitive and storage contracts, then evaluate an isolated Temporal service and separate database/state, with pinned tooling.
   A development server is acceptable for experiments, not an HA deployment.
2. One confirmed sample: OSP request -> compute receipt -> model request ->
   annotation receipt. Use fake providers first; preserve mandatory decisions.
3. Multiple datasets concurrently: while sample A waits for a model, sample B
   must run compute and publish its result. Bound pending compute/model work
   independently; do not reserve a dataset-wide peak while waiting.
4. Exercise real process death, then attach the existing OSP numerical adapter
   against a separate test pool/output root. No live output has two writers.
5. Extend the same transition contracts to MSP and ZMIP, including model-directed
   re-computation, branching lineages, cancellation and versioned retries.

## Recovery acceptance before any production cutover

| Injected fault | Required result |
| --- | --- |
| Kill coordinator after compute completes but before consuming completion | Replacement advances from the stored result, without redoing successful compute. |
| Kill/restart pool scheduler | Accepted task is discoverable; running attempts are reconciled without duplicate allocation. |
| Kill worker / expire allocation | Lease expires; retry is bounded; obsolete attempt cannot publish over replacement. |
| Kill Agent Bridge before/after provider response persistence | Recorded reply is reused; unknown response is explicit; no fabricated success. |
| Lose submit acknowledgement | Same task identity is accepted only once logically. |
| Restore old coordinator or partition network | Stale process cannot commit state or command current workers. |
| Restart durable service/database from supported persistence | Workflows recover; database availability and recovery time are separately measured. |
| All user-provisioned hosts expire | Progress persists; execution resumes when a user-provided host is available. |

The durable database/service is also infrastructure that can fail. Automatic
continuity requires a surviving host and supported database/service failover;
shared storage alone is not high availability. Do not place a distributed
coordination database on shared SQLite and assume multi-host failover works.
Placement, backups and recovery objectives must be settled before production.

Existing scientific checkpoints remain authoritative. No automatic resource
allocation/release is added. Periscope will eventually observe the durable state;
UI changes follow recovery and throughput acceptance, not precede them.

## Primitive scheduling and HyperQueue evaluation

The expanded design supersedes whole-stage resource admission: stages become
compositions of primitive operations, with dynamic iteration and versioned
artifact dependencies rather than a global round barrier. Temporal remains a
candidate for both local and distributed deployments. HyperQueue is a
candidate execution scheduler, with auto-allocation disabled by user policy.

- [Primitive catalog, local modes and tiered storage](PRIMITIVE_OPERATIONS.zh-CN.md)
- [HyperQueue evidence, recovery gaps and prototype acceptance](HYPERQUEUE_REVIEW.zh-CN.md)

These are design documents, not implemented backend or recovery guarantees.

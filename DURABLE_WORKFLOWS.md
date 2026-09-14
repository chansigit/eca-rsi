# Durable RSI workflows

Status: approved architecture and isolated implementation; no production migration.
Design branch: `feature/durable-workflows`. Implementation branch: `feature/warmpool-v2`.
The [first Warm Pool implementation](WARM_POOL_V2.md) starts with local
Scheduler/Worker recovery before scientific or Temporal integration.

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

The [Agent Bridge design](design/agent-bridge/index.html) specifies per-provider-call
admission, durable reply/tool boundaries, configuration reload and recovery.
Start with one asynchronous Bridge dispatcher; adapter subprocesses are internal
executors. Existing whole-session `run_agent()` wrappers and process-local model
fallback do not establish this contract. Each eligible backend must demonstrate
per-call quota control and persisted tool-boundary resumption. Unknown provider
completion remains explicit; recorded usage and estimated usage remain separate.

Compute attempts run in separate processes under explicit runtime/resource
contracts. Node disk caches may be reused; resident matrix sessions are deferred
until measured reload costs justify their lifecycle and memory management.
This decision does not select Temporal or HyperQueue, or authorize production
migration before recovery acceptance.

Remaining policy decisions, with proposed defaults (not yet user-approved):
- Model eligibility and budget: configure task-purpose eligibility, quota groups,
  concurrency and token/cost limits; never silently downgrade important decisions.
- Reliable artifact destinations/retention: retain inputs, accepted decisions,
  final results and recovery checkpoints; auto-evict only authorized caches.
- Migration: new test runs first, then new production runs; existing runs retain
  their executor unless an explicit validated checkpoint migration is selected.

## Accepted dispatch, failure and hosting policy

The coordinator dispatches ready execution units across datasets asynchronously:
A/unit-1 may be followed by B/unit-3 when B's predecessors are already accepted.
Each execution unit preserves its defined internal sequence; dependent units wait
for their own inputs, without a global dataset or round barrier. A dispatch turn
is not preemptive CPU time slicing. Scheduler owns concrete placement and grants;
independent accepted computations and model calls can run concurrently.

Infrastructure failures retry automatically within configured bounds. Invalid
inputs, exhausted retries or repeated inability to obtain a valid model decision
pause the affected analysis unit with saved progress and an explicit error.
Dependent downstream work waits; independent work continues. Do not automatically
omit failed samples, delete cells to bypass failures, or report false convergence.

There is no permanently available small host. Coordinator interruption is an
expected operating condition, including interruption of its state-store process.
Already granted finite worker computations and accepted in-flight model calls
can finish and persist results. Restart on an authorized host, reconcile recorded
requests/receipts, then resume dispatch without per-dataset resubmission.
Acknowledged control state and deduplication identities must survive loss of the
original host; node-local files or periodic snapshots that can lose acknowledged
requests are insufficient. Storage implementation remains to be selected under
this requirement; uninterrupted service is not required.

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

## Temporal implementation mapping (evaluation, not adoption)

The updated design prefers short, idempotent submission Activities followed by
Signals and receipt reconciliation for external Pool/Bridge work. Long-lived
asynchronous Activity completion remains an alternative to evaluate, not a
requirement. Temporal retry attempts must not automatically create new numerical
attempts. If adopted, Temporal owns authoritative Workflow state; do not implement
a competing coordinator journal or global Python leader election. External
request deduplication and scientific output validation remain RSI obligations.
The no-permanent-host requirement must include recovery of the Service and its
production persistence database on a replacement host. Dev-server SQLite alone
is not production evidence. See the linked integration analysis for sources.

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

1. Start with the Warm Pool Scheduler and Worker, using the approved primitive
   and storage contracts. Verify durable requests/receipts and local process
   recovery with pinned HyperQueue before integrating a Work Coordinator.
   Temporal service/database validation remains a later, separate gate.
2. One confirmed sample: OSP request -> compute receipt -> model request ->
   annotation receipt. Use fake providers first; preserve mandatory decisions.
3. Multiple datasets concurrently: while sample A waits for a model, sample B
   must run compute and publish its result. Bound pending compute/model work
   independently; do not reserve a dataset-wide peak while waiting.
4. Exercise real process death, then attach the existing OSP numerical adapter
   against a separate test pool/output root. No live output has two writers.
5. Extend the same transition contracts to cross-sample and ZMIP, including model-directed
   re-computation, branching lineages, cancellation and versioned retries.

## Recovery acceptance before any production cutover

The local Warm Pool recovery slice is now implemented and has an opt-in real
process failure test. Cross-host, Temporal, Agent Bridge and complete workflow
recovery remain future acceptance gates; a passing local test does not establish
the entire matrix below.


| Injected fault | Required result |
| --- | --- |
| Kill coordinator after compute completes but before consuming completion | Replacement advances from the stored result, without redoing successful compute. |
| Kill/restart pool scheduler | Accepted task is discoverable; running attempts are reconciled without duplicate allocation. |
| Kill worker / expire allocation | Stop new placement; reconcile receipts and confirm termination or fencing before bounded retry. Heartbeat/lease expiry alone cannot prove execution stopped; obsolete attempts cannot overwrite accepted results. |
| Kill Agent Bridge before/after provider response persistence | Recorded reply is reused; unknown response is explicit; no fabricated success. |
| Lose submit acknowledgement | Same task identity is accepted only once logically. |
| Restore old coordinator or partition network | Stale process cannot commit state or command current workers. |
| Restart durable service/database from supported persistence | Workflows recover; database availability and recovery time are separately measured. |
| All user-provisioned hosts expire | Progress persists; execution resumes when a user-provided host is available. |

The state-store process is also expected to stop; the accepted target is recovery
on another authorized host, not uninterrupted database availability. Acknowledged
state must survive the old host, and recovery must exclude stale writers. Shared
storage alone does not establish those guarantees. Do not assume a shared SQLite
file provides multi-host failover. Validate the selected persistence and recovery
mechanism before production.

Existing scientific checkpoints remain authoritative. No automatic resource
allocation/release is added. Periscope will eventually observe the durable state;
UI changes follow recovery and throughput acceptance, not precede them.

## Primitive scheduling and HyperQueue evaluation

HyperQueue remains the preferred reuse candidate for Warm Pool scheduling and
command execution. The Warm Pool design specifies required behavior, not a
commitment to implementing a second scheduler. Prefer HQ server/worker with thin
RSI adapters; reconcile its recovery gaps before adoption. Isolation experiments
remain deferred while component design continues.


The expanded design supersedes whole-stage resource admission: stages become
compositions of primitive operations, with dynamic iteration and versioned
artifact dependencies rather than a global round barrier. Temporal remains a
candidate for both local and distributed deployments. HyperQueue is a
candidate execution scheduler, with auto-allocation disabled by user policy.

- [Primitive catalog, local modes and tiered storage](PRIMITIVE_OPERATIONS.zh-CN.md)
- [Organize execution blocks (HTML)](design/00-organize/index.html)
- [Per-sample / OSP execution blocks (HTML)](design/01-per-sample/index.html)
- [cross-sample execution blocks (HTML)](design/02-cross-sample/index.html)
- [zoom-in execution blocks (HTML)](design/03-zoom-in/index.html)
- [Cell exclusion ledger and per-step conservation](design/cell-exclusion-ledger.md)
- [Work Coordinator execution and recovery (HTML)](design/work-coordinator/index.html)
- [Temporal integration and intermittent-host constraints (HTML)](design/work-coordinator/temporal.html)
- [Warm Pool Scheduler / Worker contracts (HTML)](design/warm-pool/index.html)
- [Agent Bridge shared routing, conversations and recovery (HTML)](design/agent-bridge/index.html)
- [HyperQueue evidence, recovery gaps and prototype acceptance](HYPERQUEUE_REVIEW.zh-CN.md)

These are design documents, not implemented backend or recovery guarantees.

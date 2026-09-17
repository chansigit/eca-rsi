# Agent Bridge: model turns and worker tool handoffs

Bridge owns the durable inbox, model health, routing and admission. **All new
model calls and agent harness execution run as bounded Warm Pool tasks.**
Coordinator validates tool requests and submits their registered programs as
separate Pool tasks. Model waits use a small, explicit worker budget; heavy
programs retain their own CPU/memory/GPU budgets. Waiting for a tool releases the
Bridge model slot. The Bridge process never imports a provider client in Pool mode.

New sessions and dispatches archive the exact model-call adapter source in
`BRIDGE_ROOT/adapters/<sha256>.py`. Existing dispatches retain their verified
revision. New dispatches may upgrade protocol-2 transport using an explicit,
immutable `portable_adapter` reference; their original session validator still
checks the unchanged specification, tool policy and conversation. Legacy SDK
sessions cannot use this upgrade. Missing or modified snapshots fail closed.
Sessions created before source archiving need their exact recorded revision
restored from version control once. This archives the model-call adapter, not
all scientific dependencies: tool inputs, runtime images and SDK versions retain
their separate integrity checks. Local session validation failures do not count
as provider health failures.
Adapter source is syntax-checked before publication. Executors validate and load
the session-pinned adapter unless the dispatch explicitly records a portable
upgrade. Validate changes in an isolated worktree and deploy a fixed release;
never edit source currently imported by services or workers.

A session with a completion tool cannot finish with free text: the common
executor rejects premature final text as `incomplete_submission`, using bounded
model fallback. Provider tool selection stays automatic; forced-call decoding
is not a substitute for host validation.
The scientific submission tool still performs all evidence/decision checks.
When explicitly resuming an older session that already ended without submission,
creation records one immutable repair session with the original specification
and initial evidence state. It rereads evidence; neither the old reply nor a
scientific result is fabricated. A failed repair does not create another repair.


New sessions use a portable conversation record: public messages, tool calls and
verified tool results. Switching a model resends the pending model turn with this
context; it never reruns completed tools. Old SDK sessions keep their pinned
model and conservative recovery rules.

## Model health and fallback

The model catalog defines calling order. A free, healthy primary is preferred;
a busy or cooling primary allows the next configured model. Each selected
attempt has an immutable dispatch record and a separate Pool request. Defaults
are configurable in the Bridge `config.json` under `routing`:

```json
{
  "response_timeout_seconds": 900,
  "cooldown_seconds": 300,
  "failure_threshold": 2,
  "model_concurrency": 2,
  "max_attempts": 3,
  "worker_cpus": 1,
  "worker_memory_mb": 1024,
  "session_wait_seconds": 900
}
```

The timeout starts on the Worker, not while waiting for Pool resources. A failed
portable model turn selects an untried eligible alternative first.
If an untried alternative is busy or cooling, that retry waits rather than
spending its remaining attempts on the same failed primary. Other queued
requests can still use available model capacity. Consecutive
provider failures put that model in cooldown. A later accepted successful reply
clears that cooldown; otherwise it expires after the configured interval. Attempts and retries are bounded. Settings are reread each tick;
an already dispatched task retains its original resource budget and timeout.

`summary.json.models` exposes state, in-flight admissions, consecutive failures,
timeouts, last response time, last attempt latency and cooldown expiry. Immutable
`model-events/*.json` retain every attempt outcome. The observatory displays
these health records and places model execution on its actual Worker lane.

Timeout does not prove remote inference stopped or was not billed. Safe automatic
fallback applies to tool-free `agent.turn` calls: only one accepted attempt may
provide the tool plan. Superseded/late replies cannot execute tools or advance the
workflow. Legacy harness sessions that can execute arbitrary programs do not
receive this retry policy. Provider RPM/TPM quotas are not inferred from the
per-model concurrency limit.

Ready operation kinds (`trace.unit_id`) take turns receiving model admissions,
so a large per-sample fan-out cannot bury a newly ready cross-sample or Zoom-in
decision. Within each kind, queue ordering favors earlier-started sessions so accepted tool results can
lead to completed annotations without every continuation rejoining the end of
the batch. One in four admission choices within that kind is reserved for the oldest request
waiting at least `session_wait_seconds`; model compatibility, cooldown and
capacity still apply. This is a simple service share, not a latency guarantee
or a global limit on the number of open sessions. Health history is aggregated
once per model per dispatch tick, not once per queued request.

After correcting a transient failure, a terminal, tool-free protocol-2 turn can
be explicitly reopened with a bounded new attempt allowance:

```bash
python -m ecarsi.bridge retry-turn /absolute/development/bridge REQUEST_ID \
  --reason 'Corrected timeout or provider availability; prior attempts are terminal'
```

The command retains the old failure, attempts and an immutable recovery record.
This also covers `incomplete_submission` after correcting the provider protocol.
It rejects uncertain attempts, cancellations, invalid local state and legacy
sessions that may execute tools. Running parents can consume the repaired
request; terminal parents use their normal stage/dataset resume command.

## Service interruption

There is no automatic control-node acquisition or takeover. A person restarts
Work Coordinator, Warm Pool Scheduler, Agent Bridge and Temporal on an available
node using their durable shared state. Workers finish already accepted bounded
work and save receipts while the control services are down. On restart, Bridge
reconciles the existing Pool request before dispatching anything new.

## Usage

Use the development worktree and a Python environment containing its existing
`agent-harness-bridge` dependency. The service and executors must import the same
worktree. Export provider keys in the user's bashrc. Model executors read the selected key
on the Worker; credentials are not placed in task arguments, config or receipts.

```bash
python -m ecarsi.bridge init /absolute/development/bridge \
  --catalog /absolute/path/model-pool.json --concurrency 4 \
  --pool-root /absolute/development/pool
python -m ecarsi.bridge serve /absolute/development/bridge
```

The catalog is the existing `ECA_MODEL_CATALOG` file (normally
`~/.config/ecarsi/model-pool.json`), passed explicitly to avoid accidentally using
production defaults. Models and URLs are read from that file at dispatch; each
child process gets its own environment. No model names are hard-coded.

A legacy planning request JSON contains `request_id`, `operation_id`, `cwd` (absolute existing
directory) and `profiles` (the actual prepared Organize metadata profiles).
Do not invent profiles or sample mappings for scientific acceptance. The client
can submit and later query from any host sharing the trusted Bridge directory:

```bash
python -m ecarsi.bridge submit /absolute/development/bridge request.json
python -m ecarsi.bridge status /absolute/development/bridge REQUEST_ID
```

Submission saves the request, current planning prompt and planning adapter hash
before acknowledging it. An identical request ID/content returns the existing
record; conflicting content fails. A structurally validated submitted proposal is
saved before acknowledging the agent tool, so SDK teardown failure does not lose
it. This is a candidate, not final scientific acceptance. Request IDs must be stable across submission
retries. Each accepted request has private `request.json`, `state.json`,
`execution.log` and, when available, `result.json`. Replies include the submitted
plan, transcript and reported usage; unreported usage remains null, not zero.
`reply_saved` means an agent reply is available; the Organize executor and
Coordinator still validate input identities, cell conservation and sample
boundaries before scientific acceptance.

`summary.json` contains counts, concurrency and update time. It is a snapshot,
not a liveness guarantee when the service is stopped. A request remains queued
if the catalog is invalid; dispatch errors are recorded in its private log.
It also separates `running`, `unresolved`, and `available` slots. Legacy uncertain
executions retain capacity until reconciled. Portable model attempts follow the
bounded fallback policy above.

The dispatcher caches immutable `reply_saved` records in memory and
rebuilds that cache from disk after restart. It continues checking queued,
running, failed and uncertain requests; audited retries are visible without
clearing a cache, and late replies still release their reserved slot.
`dispatch_scan_seconds` records loop processing time. In the 2026-09-15 live
acceptance with over 1,100 retained requests, update intervals fell from about
7.5 seconds to 1.03 seconds; hot scans took 15–30 ms. Both calls running during
the dispatcher replacement finished normally. This measures dispatch overhead,
not model inference speed or end-to-end scientific throughput.
The first 24 subsequent model requests had a median admission delay of 0.50 seconds.

For legacy local executions, the dispatcher automatically recovers a subsequently available saved response
for an uncertain request, without another model call. If no response is
recoverable, an operator or provider-side reconciler must first confirm that
the remote execution has stopped, then record that evidence:

```bash
python -m ecarsi.bridge confirm-stopped /absolute/development/bridge REQUEST_ID \
  --reason 'Provider-side confirmation and evidence reference'
```

This refuses a request whose local executor still owns its lock. A saved reply
is recovered in preference to failure. Otherwise `resolution.json` preserves
the uncertain state, request identity and confirmation reason, and the request
becomes failed so unrelated queued work can use the slot. It does not retry
the affected model call, fabricate a successful reply, or resume its failed
workflow. Elapsed time alone is insufficient confirmation. Provider-specific
automatic lookup/cancellation remains separate from the portable-turn fallback policy.

## Recovery and limits

### Agent workflows with worker tools

Install the `coordinator` extra. The resumable adapter uses the existing OpenAI
Agents SDK harness with SDK 0.22.0 and persists its version in each session.
It supports the configured `openai`, `openai@vllm`, and `openai@openrouter`
backend names. Unsupported CLI harnesses fail before calling a model; they are
not silently run on the Bridge host as a fallback.

```bash
python -m ecarsi.control --temporal HOST:7233 worker
python -m ecarsi.control --temporal HOST:7233 start-agent session-spec.json
python -m ecarsi.control --temporal HOST:7233 status-agent SESSION_ID
```

The session spec explicitly lists `session_id`, `dataset_id`, an English
`prompt`, `pool_root`, `bridge_root`, a private absolute `output_root`,
`max_turns`, and `tools`. Each tool registration contains:

| Field | Contract |
| --- | --- |
| `name`, `description`, `parameters` | Tool name, description and closed JSON Schema for model arguments. |
| `args` | Fixed argv for the Pool's Python runtime, such as `["/path/operation.py", "{arguments}"]`. The whole `{arguments}` token becomes an immutable JSON argument-file path; there is no shell interpolation. |
| `cpus`, `memory_mb`, `timeout_seconds` | Positive budgets fixed by the application, never chosen by the model. |
| `inputs` | Absolute path/SHA256 references, including program files and scientific input files required by the operation. |
| `outputs`, `result_file` | Declared output paths relative to the attempt directory. `result_file` is one of them and normally contains at most 256 KiB of JSON. Explicit `multimodal: true` registrations permit up to 16 MiB, including at most 16 inline PNG images. Large scientific arrays remain references. |

Stateful registrations can include one whole `{state}` argv token. The session
supplies an initial `tool_state` reference; each successful tool returns a new
immutable `state` reference. Coordinator carries it across both consecutive
tools and saved model turns. Internal state references are not model-visible
tool text. Per-sample annotation uses this to retain clustering versions without
keeping an AnnData object alive during model waits.

SDK 0.22.0 can save Chat model text without `annotations` and subsequently
reject that checkpoint during restoration. The Bridge's process-local
compatibility wrapper supplies the missing empty annotation lists in memory;
original checkpoints remain unchanged. A `sdk-restore.json` record counts the
repairs. This shim remains necessary while sessions pin the 0.22 adapter.
An exception specifically during local SDK restoration is a failed request
with `provider_called: false`, releasing Bridge capacity. Errors during model
invocation remain uncertain unless a saved reply proves their outcome.

Programs run as ordinary bounded Pool tasks. They receive no model API credentials
from Bridge. A registration is trusted application code, not permission for a
model to choose arbitrary executables or write arbitrary code. Programs must
validate their scientific arguments and write only their declared outputs; the
current Pool container is not a hostile-code sandbox. A stateful Python REPL is
not transparently migrated: tools communicate through explicit saved artifacts.

Coordinator polls queued model requests every 15 seconds and running ones every
3 seconds, with a Temporal patch marker preserving earlier timer histories.
This reduces empty history growth; completion is still polled, not pushed.

One model turn is a durable Bridge request. SDK interruptions are automatically
resolved by Coordinator policy after successful Pool receipts, without human
approval. Multiple calls in one reply execute in order; their independence is
not inferred. Every call gets a stable Pool request ID and argument-file hash.
Repeated activity delivery returns the existing request. Failed, cancelled,
unknown or modified outputs cannot resume a model session. Tool failure stops
the workflow visibly; automatic tool retries/replanning remain separate work.

The session pins its API mode, SDK version and adapter hash. Protocol 2 sessions
select models from the current catalog and carry portable accepted context across
model changes; legacy sessions retain their initial model/endpoint.
SDK state and tool outputs survive process replacement on shared storage. If a
model caller disappears before its response is saved, the existing conservative
`unknown_external_result` handling still applies. A saved interruption response
can be recovered without another model request. The final reply is a candidate
result; scientific workflow code must still validate it before applying changes.

Real acceptance on 2026-09-14 used two H5AD inputs and a separate recovery session:
three completed Temporal Workflows, nine Bridge model-turn requests and six
successful worker programs. The programs read dimensions and computed count
matrix totals on `sh04-14n18`; none ran on the Bridge node `sh03-01n03`.
Coordinator and Bridge were stopped while one tool ran. The worker saved its
receipt independently; after restart the original workflow resumed, with no
duplicate tool execution. This tests process recovery, not node/database loss.
Specs, pinned test program, logs, histories, receipts and `acceptance.json` are at
`/scratch/users/chensj16/eca-runs/warmpool-v2-development/agent-tool-handoff-20260914-194008/`.
The observatory draws explicit model → tool → model dependencies; worker-tool
waiting is not included in the new Bridge turn bars.

The default timeline groups dataset → stage workflow → named step. Separate
workflow IDs stay separate even when they share a dataset or stage name. Clicking
a step opens its model calls and worker tasks; `Workers` retains the exact
execution-placement view. Dataset pages are selected before the task limit, so
a busy dataset cannot remove its neighbors from the viewer. Truncated task
history is labeled and can be narrowed by dataset/time. Stage spans summarize
loaded records and include gaps; they are not authoritative workflow status.
Dataset colors persist in the browser, with additional hue separation for small
comparisons. Labels remain the identity cue for large collections.

### Legacy Organize sessions and shared limitations

- A shared `flock` permits only one dispatcher. The directory must be user-owned
  with mode 0700 on storage supporting coherent locks, rename and fsync.
- Stopping the service does not kill accepted executors. On restart, executors
  still holding their request lock are allowed to finish and count against the
  concurrency limit. Saved replies are returned without invoking the model again.
- An executor lost with a saved proposal recovers that proposal; missing usage
  or transcript remains explicitly unknown. Without a saved reply/proposal it
  becomes `unknown_external_result`.
  It is not retried and conservatively retains a capacity slot because provider
  work may still exist. Automated provider reconciliation is not implemented; `confirm-stopped`
  provides explicit resolution. This limitation prevents claiming unattended production
  readiness; do not erase state or invent a new ID to bypass it.
- Legacy Organize requests still hold a slot for the complete planning session,
  including harness retries. New `agent.turn` requests hold it only until the
  next worker-tool boundary or final response. Per-model concurrency and cooldown apply in Pool mode; provider/account RPM/TPM
  accounting and general workflow cancellation remain separate work.
- The existing harness controls its internal timeouts/fallbacks. Killing a local
  caller does not establish whether the provider completed or billed a request.
- A changed planning adapter fails before calling a provider. Full dependency
  and image pinning, cross-host Bridge recovery and turn-level quotas remain
  required. The synthetic tests make no API requests. Separate real-data
  integration runs with this Bridge are recorded in ORGANIZE_V2.md.

## Verification

```bash
LC_ALL=C LANG=C OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 \
  python -m unittest discover -s tests -p test_durable_agent_bridge.py -v
LC_ALL=C LANG=C PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  python -m pytest -q tests/test_agent_session.py tests/test_agent_dispatch.py
```

Tests cover adapter handoff, unknown usage, duplicate submissions, conflicting
IDs, bounded subprocess dispatch, dispatcher SIGKILL, surviving executors,
restart with pending work, catalog changes, reusable replies and no implicit
retry after an uncertain execution. Synthetic providers are confined to the test.

### Business workflows and validated completion tools

A session may supply `trace` to retain its enclosing business workflow and
step; request dependencies still follow the actual model/tool boundaries.
An optional registered `completion_tool` ends the session only after its
uncancelled Pool receipt and hashed JSON result contain `accepted: true`.
A rejection returns to the saved model conversation for correction. Merely
returning prose does not supply Organize's required accepted plan.
New Organize runs use `inspect_source` and `submit_plan` under `organize.plan`.
The validated output is then passed by reference to `organize.execute`.

### Worker routing acceptance (2026-09-15)

Three concurrent Temporal Agent workflows completed six real model turns (four
Turbo, two Pro) and exactly three registered worker programs. The calls executed
on `sh03-01n54` and `sh04-14n18`; every program returned the verified sum 500500.
A separate recovery check stopped Bridge after a Worker accepted a model call.
The Worker saved its successful receipt while Bridge was down; restarting Bridge
resumed the same workflow to completion. This does not claim provider-side
cancellation or a sustained scientific throughput benchmark.

Evidence is under `durable-control-20260915/agent-worker-acceptance` in the shared
v2 run directory: `real-acceptance.json` and `bridge-inflight-recovery.json`.
The container regression passed 29 tests, including local HTTP timeout/fallback,
portable tool and image continuation, model health and the existing Bridge tests.

### Cancel a request

`python -m ecarsi.bridge cancel BRIDGE_ROOT REQUEST_ID` cancels a queued or
Pool-dispatched call and records a terminal cancellation. It also requests Pool
cancellation for its attempts. Dispatch and result acceptance share the request
lock, so a cancelled call cannot publish a late reply or start another attempt.
An already accepted reply is preserved. Provider-side inference or billing may
continue after local cancellation. Legacy execution with an uncertain external
outcome still requires explicit reconciliation.

A transient stale Pool heartbeat is treated as an unknown observation rather than
immediate worker loss. Bridge waits for a valid receipt or the attempt's existing
response/startup deadline before fencing and retrying. A confirmed failed receipt
still permits the normal safe retry policy. The scale run exposed one unnecessary
model retry from the former immediate-stale rule; the accepted continuation was
preserved and no registered tool was replayed.

### Batch independent evidence requests

A registered tool may declare `read_only: true`. New portable sessions with such
tools enable the provider's native batched function calls and tell the model to
combine independent evidence requests. Known evidence readers may run concurrently
with observation-state merging; generic tools retain ordered execution. Every
output must be present in original call order before the conversation resumes.
Missing or reordered results are rejected.

A batch containing any undeclared/non-read-only tool is rejected before any
program runs. Decision submission tools cannot declare themselves read-only and
must be called individually, after their evidence has returned. Existing sessions
without declarations retain their original single-tool policy. New per-sample,
cross-sample and Zoom-in sessions declare their evidence readers explicitly;
subclustering and decision tools remain individual operations.

### Pack mandatory evidence on the Worker

Before a new evidence tool request is submitted, `agent_evidence.plan` records
its exact execution in `execution.json`. Existing requests and saved plans retain
their original command. This works with existing single-tool sessions: one model
request can return several observations, without changing its model-call policy.
If the model already requested multiple tools in the same turn, automatic
prefetch is disabled to avoid reading its requested evidence twice.

The Worker invokes the original registered tools sequentially, carrying forward
their immutable state. It can return up to eight continuous evidence pages or
mandatory observations in one response. OSP adds missing figures/tables and
current QC; cross-sample inclusion packs sample inventories and required UMAPs;
cross-sample quality can include QC and accepted type context. Zoom-in also uses
bounded text pagination and lineage QC. Gene selection, DEG queries, refinement,
and decisions remain model-directed. No scientific submission check is skipped.

The response retains the requested tool's result, adds `additional_evidence`,
and records `evidence_batch.calls` plus an explicit pending tool/arguments when
more reading is required. Additional images use indices into the top-level image
array. The batch is bounded by eight calls, 240 KB of serialized evidence text,
16 images and approximately 9 MiB of PNG data. Evidence that does not fit is not
added to the published read state. QC reserves its registered matrix-reading
budget; OSP may reuse the existing accepted-compute memory calibration.

The execution layer also corrects the old cross-sample text reader's page offset,
which advanced past its one-character lookahead. Original artifact hash checks
remain active, and tests check complete text delivery and rejection of changed
evidence. Batch size measures combined observations, not a guaranteed reduction
in provider calls: live model continuations and downstream results must still be
observed to establish that benefit.

Dataset and per-sample recovery recognize a superseded model attempt only when
its Worker has a terminal receipt and the same portable model turn has a different,
successful attempt whose verified output matches the accepted Bridge reply.
Historical timeout cancellations therefore do not block that recovery. An
uncertain Worker, a cancelled replacement, or an ordinary scientific tool failure
still requires reconciliation; their safety checks remain unchanged.

In the 2026-09-15 development batch, Prostate received six sample inventories in
one response; Eye's next model request advanced from inventory offset 2 to 10.
The first twelve completed batches returned 46 registered observations with no
tool errors. Forty-two focused tests passed, including full text/Unicode delivery,
oversized-image read-state rollback, native model batches, pinned execution plans,
and recovery with a verified replacement model attempt. These checks establish
evidence delivery and recovery behavior, not sustained high Pool utilization.
Long retained workflow histories still make cold recovery expensive; large model
responses can also exhaust the configured deadline. The Testis type-annotation
turn hit three 300-second deadlines in this observation window and needs separate
diagnosis; evidence packing must not be reported as a fix for that timeout.

Registered independent evidence calls now use a four-request sliding window per
agent. A model turn may return up to 64 calls; that response limit is separate
from concurrent execution. Only known read-only tools with mergeable observation
state are eligible. The continuation verifies every accepted result and merges
read paths, inventory coverage, lookup records and QC flags; changed scientific
data, labels or clustering versions are rejected. Decisions and generic tools
remain ordered. An immutable per-turn policy preserves both old ordered batches
and new parallel batches across recovery. Temporal patching retains old histories.

Light readers reserve at most 2 GiB rather than the parent matrix budget. Matrix
readers can reuse the existing accepted-compute peak policy; evidence batches
that perform QC retain matrix headroom. Already submitted requests stay exact.
The common executor requires a tool call when a completion tool is registered, so
a free-text conclusion cannot replace a validated submission. Archived adapters
retain their original behavior.

Worker credential-shell timeouts are bounded transient setup failures, not model
failures or corrupt session state. They retry under the existing attempt budget
without affecting model cooldown. Audited recovery also recognizes this specific
failure in older receipts; unrelated local errors remain terminal. New Slurm
workers inherit the supported API-key variables through Apptainer's explicit
environment forwarding, keeping values out of commands and state files. Existing
workers keep their inherited environment until relaunched.

# Agent Bridge: model turns and worker tool handoffs

This development service accepts model work independently of its callers and
persists replies. `AgentWorkflow` pauses at each tool boundary: Bridge saves the
Agents SDK state and exits, Coordinator validates and submits the registered
program to Warm Pool, then Bridge resumes the saved conversation with the
verified tool result. Worker tool waits occupy no Bridge model slot. Model
waiting occupies no Pool compute grant. Both paths use the same Bridge admission
limit and model catalog. New Organize workflows use this path; the legacy
planning adapter remains for old Temporal histories and explicit legacy requests.
Production workflows have not been migrated automatically.

## Usage

Use the development worktree and a Python environment containing its existing
`agent-harness-bridge` dependency. The service and executors must import the same
worktree. Set credentials in the service launch environment as for the existing
harness; credentials are not placed in request/config records.

```bash
python -m ecarsi.agent_bridge init /absolute/development/bridge \
  --catalog /absolute/path/model-pool.json --concurrency 2
python -m ecarsi.agent_bridge serve /absolute/development/bridge
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
python -m ecarsi.agent_bridge submit /absolute/development/bridge request.json
python -m ecarsi.agent_bridge status /absolute/development/bridge REQUEST_ID
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
It also separates `running`, `unresolved`, and `available` slots. An uncertain
provider operation reserves capacity until its outcome can be reconciled;
an idle local process count is not proof that provider capacity is free.

The dispatcher caches terminal `reply_saved`/`failed` records in memory and
rebuilds that cache from disk after restart. It continues checking queued,
running and uncertain requests; late replies still release their reserved slot.
`dispatch_scan_seconds` records loop processing time. In the 2026-09-15 live
acceptance with over 1,100 retained requests, update intervals fell from about
7.5 seconds to 1.03 seconds; hot scans took 15–30 ms. Both calls running during
the dispatcher replacement finished normally. This measures dispatch overhead,
not model inference speed or end-to-end scientific throughput.
The first 24 subsequent model requests had a median admission delay of 0.50 seconds.

The dispatcher automatically recovers a subsequently available saved response
for an uncertain request, without another model call. If no response is
recoverable, an operator or provider-side reconciler must first confirm that
the remote execution has stopped, then record that evidence:

```bash
python -m ecarsi.agent_bridge confirm-stopped /absolute/development/bridge REQUEST_ID \
  --reason 'Provider-side confirmation and evidence reference'
```

This refuses a request whose local executor still owns its lock. A saved reply
is recovered in preference to failure. Otherwise `resolution.json` preserves
the uncertain state, request identity and confirmation reason, and the request
becomes failed so unrelated queued work can use the slot. It does not retry
the affected model call, fabricate a successful reply, or resume its failed
workflow. Elapsed time alone is insufficient confirmation. Provider-specific
automatic lookup/cancellation and safe model-request retry remain open work.

## Recovery and limits

### Agent workflows with worker tools

Install the `coordinator` extra. The resumable adapter uses the existing OpenAI
Agents SDK harness with SDK 0.22.0 and persists its version in each session.
It supports the configured `openai`, `openai@vllm`, and `openai@openrouter`
backend names. Unsupported CLI harnesses fail before calling a model; they are
not silently run on the Bridge host as a fallback.

```bash
python -m ecarsi.work_coordinator --temporal HOST:7233 worker
python -m ecarsi.work_coordinator --temporal HOST:7233 start-agent session-spec.json
python -m ecarsi.work_coordinator --temporal HOST:7233 status-agent SESSION_ID
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

One model turn is a durable Bridge request. SDK interruptions are automatically
resolved by Coordinator policy after successful Pool receipts, without human
approval. Multiple calls in one reply execute in order; their independence is
not inferred. Every call gets a stable Pool request ID and argument-file hash.
Repeated activity delivery returns the existing request. Failed, cancelled,
unknown or modified outputs cannot resume a model session. Tool failure stops
the workflow visibly; automatic tool retries/replanning remain separate work.

The session pins its initial catalog model/endpoint, API mode, SDK version and
adapter hash. Changing those identities requires an explicit new session;
cross-provider continuation/fallback is not implemented for this new adapter.
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
  work may still exist. Automated provider reconciliation / explicit resolution
  is not implemented yet. This limitation prevents claiming unattended production
  readiness; do not erase state or invent a new ID to bypass it.
- Legacy Organize requests still hold a slot for the complete planning session,
  including harness retries. New `agent.turn` requests hold it only until the
  next worker-tool boundary or final response. Provider/account-specific quotas,
  uncertain-call reconciliation and general cancellation remain unimplemented.
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
  python -m pytest -q tests/test_agent_session.py
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

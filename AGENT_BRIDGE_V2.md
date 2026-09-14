# Agent Bridge: Organize planning inbox

This development adapter accepts Organize planning requests independently of the
caller, runs the existing agent harness in separate processes, and persists replies.
It is not connected to production. Temporal's Organize Workflow now submits its
requests and consumes saved replies. Planning requires an explicit experiment
mapping for every source; the executor audits it before publishing output.

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

A request JSON contains `request_id`, `operation_id`, `cwd` (absolute existing
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

## Recovery and limits

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
- The limit applies to complete Organize planning sessions, including harness
  retries. Per-provider-turn quotas, account groups, mid-session context recovery,
  external tool suspension and cancellation are not implemented. This is not
  the complete Agent Bridge design B and does not run computational tools.
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
```

Tests cover adapter handoff, unknown usage, duplicate submissions, conflicting
IDs, bounded subprocess dispatch, dispatcher SIGKILL, surviving executors,
restart with pending work, catalog changes, reusable replies and no implicit
retry after an uncertain execution. Synthetic providers are confined to the test.

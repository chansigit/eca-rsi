# Model-turn service (`ecarsi.agent`)

`ecarsi.agent` manages the durable inbox for model turns, model health, routing, and admission. The run directory that stores its state is named `bridge`. The external package agent-harness-bridge (`harness_bridge`) executes each turn as the model runtime. This document refers to `harness_bridge` as "the bridge" only where the code does.

Every model call runs as a bounded task. It runs on a warm pool worker or in a resident runner. The coordinator validates tool requests. Then, the coordinator submits the registered programs as separate pool tasks.

A model wait uses a small worker budget. Heavy programs keep their own budgets for CPU, memory, and GPU. When a task waits for a tool, it releases the model slot. The service process never imports a provider client.

## Sessions

A stage program creates a session. The session includes a prompt, a tool list, a completion tool, and an evidence reference. The coordinator drives the session turn by turn through `run_agent`.

- **Protocol 2 (default).** The session keeps a portable conversation record. This record contains public messages, tool calls, and verified tool results. When you switch the model, the coordinator resends only the pending turn with this record. Completed tools never run again.
- **Protocol 1 (legacy).** Older sessions restore the SDK run state. The restore procedure fills empty `annotations` lists for text blocks. The OpenAI Agents SDK from 0.22.1 validates restored messages, but Doubao replies do not include annotations. The repair procedure runs for every SDK version and increments a counter in `sdk-restore.json`. These sessions keep their pinned model and SDK version.

A session with a completion tool cannot finish with free text. The executor rejects premature final text as `incomplete_submission`. The executor then nudges the model within a configured bound. The completion tool runs full scientific validation. A rejected completion returns to the conversation for correction. The session stops after repeated identical rejections (`REPEAT_LIMIT`).

New sessions and dispatches archive the exact adapter source in `<bridge>/adapters/<sha256>.py`. Existing dispatches keep their verified revision. Missing or modified snapshots fail closed. Do not edit source code that services or workers currently import. Validate changes in a worktree and deploy a release.

**Session death.** When a session fails, the coordinator starts one fresh session with the same evidence (`<session>-r2`). The coordinator marks the old session as superseded. A second failure skips the sample (labels `unannotated`, needs_review `agent_skipped`) or the lineage (cross-sample labels kept). Cross-sample sessions restart, but they never skip. A stage fails when skipped cells exceed 10 % of its input. Resume preflight marks superseded requests automatically. You do not need to archive requests manually.

## Model health and fallback

The model catalog (`ECA_MODEL_CATALOG`, normally `~/.config/ecarsi/models.json`) defines the calling order. The system prefers a free, healthy primary model. A busy or cooling primary model lets the next configured model take the turn. Each attempt has an immutable dispatch record and an independent task.

The bridge `config.json` file defines defaults under `routing`:

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

The timeout counter starts when the executor starts. The counter does not run while the task waits for resources.

A failed portable turn tries an untried eligible alternative first. Consecutive provider failures put a model into cooldown. A later accepted reply clears the cooldown state.

The system rereads settings on each tick. The file `summary.json.models` reports state, in-flight admissions, failures, and cooldown expiry. Files in `model-events/*.json` record every attempt outcome.

**What each attempt records (#52).** The attempt's `result.json` (`turns/<turn>/` for a runner, the pool attempt's `outputs/` otherwise) keeps the outcome, the error's type and message, the provider's response shape, and since #52: `provider_calls` (one entry per HTTP request: request bytes, inlined images and their decoded bytes, latency, HTTP status, input and output tokens, provider error code; no latency means no response came), their totals `input_tokens`, `output_tokens`, `images`, `image_bytes`, `latency_s`, and `error_class` (timeout, rate_limit, context_too_long, output_limit, parse_error, other; none for a success; `dispatch.error_class`). The version's runner writes them, so the shared bridge needs no change. `bash ops/runpy.sh ops/turn-report.py <run root>` groups one run's attempts by class and by size; older attempts are classified from their error text.

Ready operation kinds (`trace.unit_id`) take turns to receive admissions. This policy prevents a large per-sample fan-out from delaying cross-sample or zoom-in decisions. Within an operation kind, older sessions go first. The system reserves one in four admissions for the oldest request waiting at least `session_wait_seconds`.

A timeout does not prove that remote inference stopped or was not billed. Superseded or late replies cannot run tools or advance a workflow.

## Resident runners

The command `python -m ecarsi.agent runners <bridge>` supervises one resident runner per catalog model (`python -m ecarsi.agent runner <bridge> --model …`). A runner keeps many turns in flight in one event loop. Therefore, a model turn does not incur the startup cost of a pool task.

The bridge configuration setting `service.models` specifies the models that use a runner (`"all"` or a list of keys). An empty list disables resident runners and sends every turn to the pool. When `dispatch.runner_ready` is true, the system routes a turn to a live runner. Otherwise, the turn runs as a pool task.

## Worker tools

The session specification defines `session_id`, `dataset_id`, an English `prompt`, `pool_root`, `bridge_root`, a private `output_root`, `max_turns`, and `tools`.

Each tool registration contains the following fields:

| Field | Contract |
| --- | --- |
| `name`, `description`, `parameters` | Tool name, description, and closed JSON Schema for model arguments. |
| `args` | Fixed argv list for the Python runtime of the pool, such as `["/path/operation.py", "{arguments}"]`. The token `{arguments}` resolves to an immutable JSON file path. There is no shell. |
| `cpus`, `memory_mb`, `timeout_seconds` | Resource budgets set by the application. The model never selects these budgets. |
| `inputs` | Absolute file paths and SHA-256 references. These items include program files and scientific inputs. |
| `outputs`, `result_file` | Declared output paths relative to the attempt directory. The file `result_file` stores at most 256 KiB of JSON. Alternatively, it stores 16 MiB with at most 16 inline PNG images for tools with `multimodal: true`. |
| `read_only` | Flag that identifies an evidence reader. Read-only tools can run in parallel and in native batched calls. |
| `{state}` | Optional argv token for a stateful tool. Each successful run returns a new immutable `state` reference. |

Tool programs are trusted application code. They validate their arguments and write only their declared outputs. The pool container is not a hostile-code sandbox.

One model turn represents one durable request. Multiple calls in one reply run sequentially unless they are read-only. Every call receives a stable request ID and an argument-file hash. Failed, cancelled, unknown, or modified outputs cannot resume a session. The coordinator polls queued requests every 15 seconds. It polls running requests every 3 seconds.

**Batched evidence.** Registered read-only tools run in a sliding window of four per session. A turn can return up to 64 calls. The continuation verifies every result. It merges read paths, inventory coverage, lookups, and QC flags. The system rejects modified data, modified labels, or modified clustering versions. Decision tools run alone after their evidence returns.

**Evidence packing.** Before submitting an evidence request, `stages.evidence.plan` records its execution plan in `execution.json`. The worker runs registered tools in sequence. It can return up to eight continuous evidence pages in one response. These pages include missing figures, tables, current QC data, sample inventories, and required UMAPs.

A batch cannot exceed eight calls, 240 KB of text, 16 images, and approximately 9 MiB of PNG data. Light readers reserve at most 2 GiB. Matrix readers follow the accepted-compute peak policy.

Worker credential-shell timeouts are transient setup failures, not model failures. New workers receive supported API-key variables through Apptainer environment forwarding. Secret keys never appear in commands or state files.

## Commands

```bash
python -m ecarsi.agent init <bridge> --catalog ~/.config/ecarsi/models.json --concurrency 4 --pool-root <pool>
python -m ecarsi.agent serve <bridge>
python -m ecarsi.agent runners <bridge>
python -m ecarsi.agent submit <bridge> request.json
python -m ecarsi.agent status <bridge> REQUEST_ID
python -m ecarsi.agent cancel <bridge> REQUEST_ID
python -m ecarsi.agent retry-turn <bridge> REQUEST_ID --reason '…'      # a failed tool-free turn, after the cause is fixed
python -m ecarsi.agent confirm-stopped <bridge> REQUEST_ID --reason '…' # a legacy call whose remote outcome is confirmed stopped
```

The submission command saves the request to disk before acknowledging it. If a submission matches an existing ID and content, the system returns the existing record. If the content conflicts, the command fails.

The file `summary.json` records request counts, concurrency, and update timestamps. It provides a static snapshot, not a live status check.

Replies include the submitted result, transcript, and reported usage. Unreported usage remains null rather than zero.

The `cancel` command records a terminal cancellation. It requests the pool to cancel active attempts. A cancelled call cannot publish a late reply.

The `retry-turn` command records a recovery while preserving earlier failure and attempt records. It rejects uncertain attempts and legacy sessions that can run tools.

The `confirm-stopped` command marks an uncertain legacy call as failed. Run this command only after a person confirms that remote execution stopped. Elapsed time alone does not confirm that execution stopped.

## Service interruption

The system does not restart the control node automatically. When an outage occurs, an operator must restart the services on an available node with the same shared storage. Restart the coordinators, the scheduler, this service, and Temporal.

Workers complete accepted work and write receipts while services are offline.

During restart, the dispatcher rebuilds its cache from disk. It reconciles existing requests before dispatching new work. A shared `flock` lock ensures that only one dispatcher runs. Stopping the service does not terminate active executors.

## Checks

```bash
LC_ALL=C LANG=C OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 python -m unittest discover -s tests -p test_durable_agent_bridge.py -v
LC_ALL=C LANG=C PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q tests/test_agent_session.py tests/test_agent_dispatch.py
```

The test suite validates adapter handoffs, unknown usage handling, duplicate submissions, and conflicting IDs. It also tests bounded dispatch, dispatcher SIGKILL recovery, and executor survival across restarts. Additionally, tests verify recovery with pending work, catalog updates, reply reuse, and prevention of implicit retries after uncertain executions. Synthetic providers run only within tests.

## History

Acceptance records of September 2026 are in [docs/history/AGENT_BRIDGE_ACCEPTANCE_20260915.md](../history/AGENT_BRIDGE_ACCEPTANCE_20260915.md).

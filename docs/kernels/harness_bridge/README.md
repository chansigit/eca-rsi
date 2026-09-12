# Agent Harness Bridge

`agent-harness-bridge` gives applications one small, submit-tool-oriented API
for three different agent runtimes:

- OpenAI Agents SDK, including OpenAI-compatible endpoints such as Volcengine Ark
- Claude Agent SDK
- DeepSeek Harness (`dsh`)

It deliberately does not hide backend lifecycle differences. Each adapter owns
its native session continuation, MCP transport, timeout, cleanup and recovery
logic, while applications keep their prompts, domain tools and submit
validation.

## Install

Install only the runtime you need, or all validated adapters:

```bash
pip install 'agent-harness-bridge[openai]==0.2.3'
pip install 'agent-harness-bridge[claude]==0.2.3'
pip install 'agent-harness-bridge[deepseek]==0.2.3'
pip install 'agent-harness-bridge[all]==0.2.3'
```

The dsh adapter also imports `deepseek_harness`. DeepSeek's current SDK
depends on a platform-specific runtime wheel, so the bridge does not force
that wheel onto every installation. Install the SDK using the method supported
by the target host. On older-glibc clusters, load `polyfill-glibc/0.1` before
using its runtime or point `DSH_BIN` at a validated source build.

## Configuration

Harness and model selection are independent:

```bash
HARNESS=openai MODEL=doubao-seed-2-1-turbo-260628 python your_workflow.py
HARNESS=openai MODEL=doubao-seed-2-1-pro-260628 python your_workflow.py
HARNESS=openrouter MODEL=dots-studio/dots-3-note-preview:free python your_workflow.py   # OPENROUTER_API_KEY
HARNESS=deepseek MODEL=doubao-seed-2-1-turbo-260628 python your_workflow.py
HARNESS=claude MODEL=claude-sonnet-5 python your_workflow.py
```

The default remains OpenAI Agents SDK with
`doubao-seed-2-1-turbo-260628`. Model identifiers are intentionally open
strings rather than a hard-coded catalog.

### Falling back across backends (ModelPool)

`HARNESS`/`MODEL` resolve to exactly one backend for the whole run. To fall
back to a different `{harness, model}` when the current one is provably
broken (auth/permission/unknown-model errors, or repeated malformed
submissions — never a busy provider; waiting out a rate limit is a separate,
unrelated mechanism), pass an ordered pool instead:

```bash
AGENT_MODEL_POOL='openai:doubao-seed-2-1-turbo-260628,openai:doubao-seed-2-1-pro-260628,claude:claude-sonnet-5' python your_workflow.py
```

or build one explicitly with `ModelPool(parse_model_pool(spec))` / `resolve_model_pool()`
and pass it to `run_agent(..., pool=pool)`. Unlike `HARNESS`/`MODEL` above,
**harness and model are always one paired token, never two separate lists**
— a model pinned without its harness is exactly how a run ends up asking one
backend for another's model id. `AgentRunResult.effective_config` records
which candidate actually produced a result, so a caller never has to
re-derive "what backend answered this" from the environment after the fact.

One pool instance is one fallback scope: create a new one per unit of work
that should stay on whichever backend it falls back to (a caller-level
decision — the bridge only tracks which candidate is current), and pass the
same instance to every `run_agent()` call within that scope.

## Logging

Every bridge line (`== [label] agent: tool(...)`, retries, usage limits,
run summaries) goes through the `harness_bridge` logger family at `INFO`.
Configure it once in your CLI entry point, together with your own logger
families, so one stream carries one style of output:

```python
from harness_bridge import configure_logging

configure_logging("myapp")            # harness_bridge + myapp -> stdout, "%(message)s"
configure_logging("myapp", stream=sys.stderr, level="DEBUG")
```

Records are flushed one by one, so Slurm and `tee` logs stay live. If nobody
configured logging before `run_agent` runs, the bridge attaches the same
default handler itself (`ensure_logging`), which keeps the pre-0.2 behaviour
of printing to stdout. Loggers keep propagating to the root logger, so
`pytest`'s `caplog` still sees the records.

## Contract

Applications provide `ToolSpec` objects and designate one successful submit
tool as the completion condition:

```python
from harness_bridge import ToolSpec, run_agent

async def submit(args):
    return {
        "content": [{"type": "text", "text": "accepted"}],
        "_submitted": args,
    }

result = await run_agent(
    tools=[ToolSpec("submit_answer", "Submit the checked answer", {"answer": str}, submit)],
    submit_tool="submit_answer",
    prompt="Check the evidence and submit the answer.",
    cwd="/absolute/read-only/workdir",
)
```

Tool handlers return an MCP-shaped result containing text or image content,
an optional `is_error`, and an optional private `_submitted` value captured by
the host after successful validation. A handler that raises is reported to
the model as an error result under every backend; it never aborts the run.

`run_agent()` validates the tool table before importing any SDK: the submit
tool must be present, tool names must be unique, `allowed_builtin` must be a
subset of `read`, `glob`, `grep`, `tasks`, and application tools may not
reuse the name of a requested builtin.

`allowed_builtin` selects Claude Code's own Read/Glob/Grep/Task tools under
`HARNESS=claude`. The OpenAI and dsh adapters serve same-named, cwd-confined
host tools implemented in pure Python (Grep needs no `rg` on the host), so
prompts stay portable across backends.

`backend_capabilities()` exposes runtime facts that callers can check before a
run. Unsupported built-in capabilities fail closed.

## Design boundary

The bridge owns only runtime concerns. Domain workflows should continue to own:

- prompts and scientific or business policy
- tool handler implementations
- submit validation
- output files and resume manifests

Backend-specific defenses remain adapter-local. In particular, OpenAI
Responses continuation and context reset, Claude SDK teardown and permissions,
and dsh MCP startup/watchdog/SSE recovery are not reduced to a lowest-common-
denominator loop.


### Bounded text reads (0.2.1)

The host `Read` tool used by OpenAI and DeepSeek returns 8 KiB of text by default,
with a 32 KiB hard cap. It accepts `byte_offset` and `max_bytes` (both `0` for the
default first page); truncated results give the exact next offset. UTF-8 characters
are preserved across page boundaries. Existing path-only handler calls still work.
Image reads keep their existing image contract; Claude Code uses its native reader.

Large cell-level CSVs should be queried or searched for specific evidence, rather
than copied into the model context page by page. The smaller default prevents a
single barcode ledger from consuming an entire context window; it does not guarantee
that an arbitrarily long agent session cannot exhaust its context.

### Recovering provider length limits

The OpenAI Responses backend starts a fresh model session when the provider
explicitly terminates an incomplete response for `length` or `max_output_tokens`.
`OPENAI_AGENTS_MAX_OUTPUT_RESETS` limits these attempts (default `2`, `0` disables
recovery), separately from `OPENAI_AGENTS_MAX_CONTEXT_RESETS`. Completed host
tasks and validated partial submissions remain available in the same process.
Content filtering, unknown incomplete reasons and unrelated model errors still
propagate. Truncated response text never counts as a valid submission.

A public SDK hook counts each logical model invocation before it starts, enforcing one
turn budget across all fresh sessions, even when exceptions lack run data. Internal
SDK/HTTP transport retries are not separate turns.
Recovery also retains usage reported for completed requests in SDK exception
state. If provider usage
for the failed request is unavailable, logs explicitly mark usage as incomplete.
The wall-clock limit continues across fresh sessions. This does not provide
cross-process checkpoints; a process that has already exited cannot recover
partial decisions from its transcript alone.

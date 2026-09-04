"""Stable application-facing contract for pluggable agent runtimes.

Every call site builds a small, self-contained tool table (the "submit tool"
pattern: one designated tool ends the run and its handler is the only place
the actual answer is produced/validated — the model never needs filesystem
write access to get its answer out) and hands it to `run_agent()`. Which SDK
actually drives the model is an env-var choice, not a call-site choice:

    HARNESS=openai      (default since 2026-09-04) OpenAI Agents SDK with
                         the Doubao Ark endpoint, direct in-process
                         function tools
    HARNESS=deepseek    DeepSeek Harness (dsh) via its Python SDK, driving
                         Doubao by default, tools bridged over an in-process
                         streamable-http MCP server (see below)
    HARNESS=claude      claude_agent_sdk, in-process MCP tools
The tool `handler` return shape (`{"content": [{"type": "text", ...}],
"is_error": bool}`) is already the real MCP `CallToolResult` wire shape —
Claude Agent SDK's in-process server is itself an MCP server — so the same
handler bodies serve all backends unchanged.

Every run_agent() call is wrapped in `retry_transient`. Runtime-specific
session, transport and recovery behavior remains inside its adapter.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Literal, Mapping, TypeVar, cast

ToolHandler = Callable[[dict], Awaitable[dict]]
T = TypeVar("T")
HarnessName = Literal["openai", "deepseek", "claude"]
BuiltinCapability = Literal["read", "glob", "grep", "tasks"]

# Concurrent Slurm job starts (a batch of jobs all launching agent sessions
# around the same time) can blow a local control handshake or kill the
# subprocess transport — nothing to do with the account's usage limit, just
# local contention — and get a short, immediate retry. A genuine usage/rate
# limit gets a long wait instead, bounded by a total wait budget, since a
# self-driving loop must not stop for that.
LIMIT_PATTERN = re.compile(
    r"usage limit|rate[ _-]?limit|limit will reset|resets at|too many requests|overloaded|"
    r"quota|429|capacity|out of extra usage|spend limit",
    re.IGNORECASE,
)
TRANSIENT_PATTERN = re.compile(
    r"control request timeout|broken pipe|connection reset|econnreset|epipe|"
    r"process exited unexpectedly|failed to start|connection closed|stdout closed|"
    r"transportclosed|initialize timed out|timed out waiting|mcp tools never listed|"
    r"initial connection or tool synchronization failed",
    re.IGNORECASE,
)
# ("returned an error result" used to be here for the old bundled CLI's
# image-read bug; with the SDK version gate that string now only ever means
# a real error, which retrying 5× would just delay by 200 s)
MAX_TRANSIENT_ATTEMPTS = 5
TRANSIENT_BACKOFF_SECONDS = 20  # linear: 20s, 40s, 60s, 80s
MAX_TIMEOUT_ATTEMPTS = 2  # a run that blew its wall-clock budget gets exactly one fresh start
DEFAULT_WALL_MINUTES = 180.0


class AgentTimeout(RuntimeError):
    """The run exceeded its wall-clock budget (AGENT_WALL_MIN) and was killed."""


def wall_seconds() -> float | None:
    """Per-run wall-clock budget in seconds: AGENT_WALL_MIN minutes (default
    180; 0 or a non-number = unlimited). Enforced by every backend — Claude's
    max_turns bounds turns but not a turn that hangs, and dsh has neither a
    turn cap nor a run-level timeout, so a model stuck in a loop would
    otherwise burn until the provider hangs up."""
    raw = os.environ.get("AGENT_WALL_MIN", "")
    try:
        minutes = float(raw) if raw.strip() else DEFAULT_WALL_MINUTES
    except ValueError:
        return None
    return minutes * 60 if minutes > 0 else None


class AgentLimitExhausted(RuntimeError):
    pass


async def retry_transient(coro_fn: Callable[[], Awaitable[T]], label: str) -> T:
    """Run coro_fn(); on a transient-looking failure, retry a bounded number
    of times with linear backoff; on a usage/rate-limit-looking failure,
    wait and retry, bounded by a total wait budget (env AGENT_LIMIT_WAIT_MIN
    minutes between tries, default 10; AGENT_LIMIT_WAIT_MAX_H total hours,
    default 12). Any other failure raises immediately."""
    wait_min = float(os.environ.get("AGENT_LIMIT_WAIT_MIN", "10"))
    max_h = float(os.environ.get("AGENT_LIMIT_WAIT_MAX_H", "12"))
    waited = 0.0
    limit_attempt = 0
    transient_attempts = 0
    timeout_attempts = 0
    while True:
        try:
            return await coro_fn()
        except AgentTimeout as e:
            timeout_attempts += 1
            if timeout_attempts >= MAX_TIMEOUT_ATTEMPTS:
                raise
            print(f"== [{label}] {e} — one fresh attempt", flush=True)
            continue
        except Exception as e:
            msg = str(e)
            if TRANSIENT_PATTERN.search(msg):
                transient_attempts += 1
                if transient_attempts >= MAX_TRANSIENT_ATTEMPTS:
                    raise RuntimeError(
                        f"[{label}] transient agent-startup failure persisted after "
                        f"{transient_attempts} attempts: {msg}"
                    ) from None
                wait = TRANSIENT_BACKOFF_SECONDS * transient_attempts
                print(f"== [{label}] transient agent-startup failure (attempt {transient_attempts}/"
                      f"{MAX_TRANSIENT_ATTEMPTS}): {msg[:160]!r} — retrying in {wait}s", flush=True)
                await asyncio.sleep(wait)
                continue
            if LIMIT_PATTERN.search(msg):
                limit_attempt += 1
                if waited / 3600 >= max_h:
                    raise AgentLimitExhausted(
                        f"[{label}] usage limit still in force after {waited / 3600:.1f} h: {msg}"
                    ) from None
                print(f"== [{label}] usage/rate limit (attempt {limit_attempt}): {msg[:160]!r} — "
                      f"waiting {wait_min:.0f} min, {max_h - waited / 3600:.1f} h of wait budget left",
                      flush=True)
                t0 = time.time()
                await asyncio.sleep(wait_min * 60)
                waited += time.time() - t0
                continue
            raise


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    # {param_name: python_type} — the same flat shape claude_agent_sdk's
    # @tool() takes as its third argument; translated to real JSON Schema
    # for the DeepSeek backend's MCP server.
    input_schema: dict[str, type]
    handler: ToolHandler


@dataclass
class AgentRunResult:
    submitted: dict | None  # whatever the submit tool's handler captured; None if it never fired
    transcript_text: str | None  # best-effort final assistant text, for *_notes.md-style logging
    cost_usd: float | None  # best-effort; None where the backend doesn't report it


class AgentIncompleteError(RuntimeError):
    """The run ended without the submit tool ever firing."""


DEFAULT_BACKEND = "openai"
KNOWN_BACKENDS = frozenset(("openai", "deepseek", "claude"))


_DEFAULT_MODEL = {
    "claude": "claude-sonnet-5",
    "openai": "doubao-seed-2-1-turbo-260628",
    # HARNESS=deepseek's default provider is Doubao via dsh's pi-ai adapter
    # (see _harness_deepseek); DSH_PROVIDER=deepseek-official switches to a
    # real DeepSeek model, in which case override MODEL too.
    "deepseek": "doubao-seed-2-1-turbo-260628",
}


@dataclass(frozen=True, slots=True)
class AgentConfig:
    """Resolved runtime choice. Model ids remain open strings by design."""

    harness: HarnessName
    model: str

    def as_manifest(self) -> dict[str, str]:
        return {"harness": self.harness, "model": self.model}


@dataclass(frozen=True, slots=True)
class HarnessCapabilities:
    """Capabilities implemented by a bridge adapter, not merely its model."""

    builtins: frozenset[BuiltinCapability]
    image_tool_outputs: bool
    response_chaining: bool
    same_session_nudge: bool
    mcp_transport: bool
    context_reset: bool


def resolve_agent_config(
    *,
    harness: str | None = None,
    model: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> AgentConfig:
    """Resolve explicit values before environment values before defaults."""
    env = os.environ if environ is None else environ
    selected = (harness or env.get("HARNESS") or DEFAULT_BACKEND).strip().lower()
    if selected not in KNOWN_BACKENDS:
        raise ValueError(
            f"unknown HARNESS backend {selected!r} (expected 'claude', 'openai', or 'deepseek')"
        )
    selected_model = (model or env.get("MODEL") or _DEFAULT_MODEL[selected]).strip()
    if not selected_model:
        raise ValueError(f"HARNESS={selected} needs a non-empty model id")
    return AgentConfig(harness=cast(HarnessName, selected), model=selected_model)


def backend_name() -> str:
    return resolve_agent_config().harness


def default_model() -> str:
    """Return MODEL, or the default for the selected HARNESS backend."""
    return resolve_agent_config().model


def backend_capabilities(
    config: AgentConfig | None = None,
    *,
    openai_api: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> HarnessCapabilities:
    """Describe features implemented by the selected adapter.

    OpenAI image results, response chaining and context reset require the
    Responses path. Other provider/model-specific facts are intentionally not
    guessed here.
    """
    env = os.environ if environ is None else environ
    resolved = config or resolve_agent_config(environ=env)
    builtins: frozenset[BuiltinCapability] = frozenset(("read", "glob", "grep", "tasks"))
    if resolved.harness == "openai":
        mode = (openai_api or env.get("OPENAI_AGENTS_API") or "responses").strip().lower()
        if mode not in {"responses", "chat_completions"}:
            raise ValueError(
                f"invalid OPENAI_AGENTS_API={mode!r} (expected 'responses' or 'chat_completions')"
            )
        responses = mode == "responses"
        return HarnessCapabilities(
            builtins=builtins,
            image_tool_outputs=responses,
            response_chaining=responses,
            same_session_nudge=True,
            mcp_transport=False,
            context_reset=True,
        )
    if resolved.harness == "deepseek":
        return HarnessCapabilities(
            builtins=builtins,
            image_tool_outputs=True,
            response_chaining=False,
            same_session_nudge=True,
            mcp_transport=True,
            context_reset=False,
        )
    return HarnessCapabilities(
        builtins=builtins,
        image_tool_outputs=True,
        response_chaining=False,
        same_session_nudge=False,
        mcp_transport=True,
        context_reset=False,
    )


async def run_agent(
    *,
    tools: list[ToolSpec],
    submit_tool: str,
    prompt: str,
    system_prompt: str | None = None,
    cwd: str,
    model: str | None = None,
    effort: str | None = None,
    max_turns: int = 30,
    allowed_builtin: tuple[str, ...] = ("read", "glob", "grep"),
    label: str = "agent",
    max_buffer_size: int | None = None,
) -> AgentRunResult:
    """Run one agent turn to completion; raise AgentIncompleteError if
    `submit_tool` never fired. `allowed_builtin` is the read-only filesystem
    exploration surface ("read", "glob", "grep") plus "tasks" — a session
    task list the model keeps as its own progress checklist (Claude Code's
    TaskCreate/TaskUpdate/TaskList/TaskGet; the DeepSeek and OpenAI backends
    serve same-named in-memory tools so prompts stay identical). The model
    never gets write access under any backend."""
    config = resolve_agent_config(model=model)
    backend = config.harness
    model = config.model
    if backend == "claude":
        from ._harness_claude import run_agent as _run
    elif backend == "openai":
        from ._harness_openai import run_agent as _run
    elif backend == "deepseek":
        from ._harness_deepseek import run_agent as _run
    else:  # pragma: no cover - resolve_agent_config validates the registry
        raise AssertionError(f"unregistered harness backend: {backend}")

    wall = wall_seconds()

    async def _attempt() -> AgentRunResult:
        coro = _run(
            tools=tools, submit_tool=submit_tool, prompt=prompt, system_prompt=system_prompt,
            cwd=cwd, model=model, effort=effort, max_turns=max_turns,
            allowed_builtin=allowed_builtin, label=label, max_buffer_size=max_buffer_size,
            wall_seconds=wall,
        )
        if wall is None:
            return await coro
        try:
            # the backend enforces the budget itself (dsh: watchdog closes the
            # runtime; claude: cancellation tears the CLI down); this is the
            # backstop that also covers a backend stuck in its own teardown
            return await asyncio.wait_for(coro, timeout=wall + 120)
        except asyncio.TimeoutError:
            raise AgentTimeout(f"[{label}] agent run exceeded the wall-clock budget of {wall / 60:g} min "
                               f"(AGENT_WALL_MIN)") from None

    return await retry_transient(_attempt, label)

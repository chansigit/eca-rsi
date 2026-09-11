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
                         streamable-http MCP server
    HARNESS=claude      claude_agent_sdk, in-process MCP tools

The tool `handler` return shape (`{"content": [{"type": "text", ...}],
"is_error": bool}`) is already the real MCP `CallToolResult` wire shape —
Claude Agent SDK's in-process server is itself an MCP server — so the same
handler bodies serve all backends unchanged.

Every run_agent() call is wrapped in `retry_transient`. Runtime-specific
session, transport and recovery behavior remains inside its adapter.

Optionally, a caller can pass an ordered `ModelPool` (or set AGENT_MODEL_POOL)
to fall back to a different {harness, model} when the current one is
provably broken (auth/permission/unknown-model, or repeated malformed
submissions) -- see ModelPool below. Without one, behavior is unchanged: a
single AgentConfig resolved once from HARNESS/MODEL.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import os
import re
import time
import dataclasses
from dataclasses import dataclass
from typing import Awaitable, Callable, Literal, Mapping, TypeVar, cast

from ._logging import ensure_logging

ToolHandler = Callable[[dict], Awaitable[dict]]
T = TypeVar("T")
HarnessName = Literal["openai", "deepseek", "claude"]
BuiltinCapability = Literal["read", "glob", "grep", "tasks"]


# --------------------------------------------------------------------------
# Failure classes
# --------------------------------------------------------------------------


log = logging.getLogger(__name__)


class AgentTimeout(RuntimeError):
    """The run exceeded its wall-clock budget (AGENT_WALL_MIN) and was killed."""


class AgentLimitExhausted(RuntimeError):
    """A usage/rate limit outlasted the total wait budget (AGENT_LIMIT_WAIT_MAX_H)."""


class AgentIncompleteError(RuntimeError):
    """The run ended without the submit tool ever firing."""


def _qualname(e: BaseException) -> str:
    return f"{type(e).__module__}.{type(e).__qualname__}"


def _is_malformed_submission(e: BaseException) -> bool:
    """The model itself produced a turn the gateway/SDK could not accept --
    an empty tool name (agents.exceptions.ModelBehaviorError, seen 2026-09-09:
    'Tool , not found') or a tool call missing its arguments (Ark's structured
    error code 'MissingParameter' on `input.arguments`, seen 2026-09-08). Not
    a network/gateway blip and not a real answer either; a fresh turn on the
    *same* backend is the cheap recovery, same tier as TRANSIENT_PATTERN.

    Classified by type / structured field, not by matching the rendered
    message text, so a reworded SDK message or a new-but-equivalent Ark error
    code is still caught without a new regex entry. Duck-typed on qualified
    class name / an attribute openai's APIError family always sets, so this
    module does not need to import `agents` or `openai` just to classify an
    error -- callers on the claude/deepseek backends never load either.
    """
    if _qualname(e) == "agents.exceptions.ModelBehaviorError":
        return True
    return getattr(e, "code", None) == "MissingParameter"


def _is_startup_failure(e: BaseException) -> bool:
    """Auth/permission or unknown-model errors (issue #1): retrying the same
    backend can never succeed, only a different one can. Duck-typed on
    openai's 401/403/404 exception classes -- the only ones this has real
    evidence for; extend when another backend's equivalent is actually seen,
    not speculatively."""
    return _qualname(e) in {
        "openai.AuthenticationError", "openai.PermissionDeniedError", "openai.NotFoundError",
    }


# --------------------------------------------------------------------------
# Retry policy
# --------------------------------------------------------------------------

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
    r"initial connection or tool synchronization failed|"
    # provider HTTP layer (openai SDK with max_retries=0): a slow/reset Ark
    # request must not end the whole agent run, let alone the sample
    r"request timed out|apitimeouterror|apiconnectionerror|connection error|"
    r"server disconnected|remote protocol error|remoteprotocolerror|readtimeout|connecttimeout|"
    # Ark gateway/server-side hiccups: a bare non-JSON 400 "Error when parsing
    # request" (no error code or request id, unlike every API validation error)
    # right after a multi-image tool-result upload, and HTTP 500
    # InternalServiceError. Both are rare, the identical payload succeeds on
    # replay, and the SDK will not replay a previous_response_id request itself.
    r"error when parsing request|internalservererror|internalserviceerror|"
    # Responses-API state loss: with SERVER_STATE=1 the SDK chains turns by
    # previous_response_id, and Ark has been seen to forget a stored response
    # under load (observed 2026-09-07 right after two 429 ServerOverloaded in
    # the same call). The id can never come back, so retrying the request is
    # useless -- but a retry here restarts the whole run with a fresh chain,
    # which is exactly the recovery. Without this the stage dies and the
    # driver recomputes it from the top.
    r"previousresponsenotfound",
    re.IGNORECASE,
)
# ("returned an error result" used to be here for the old bundled CLI's
# image-read bug; with the SDK version gate that string now only ever means
# a real error, which retrying 5× would just delay by 200 s)
MAX_TRANSIENT_ATTEMPTS = 5
TRANSIENT_BACKOFF_SECONDS = 20  # linear: 20s, 40s, 60s, 80s
MAX_TIMEOUT_ATTEMPTS = 2  # a run that blew its wall-clock budget gets exactly one fresh start
DEFAULT_WALL_MINUTES = 180.0
DEFAULT_LIMIT_WAIT_MINUTES = 10.0
DEFAULT_LIMIT_WAIT_MAX_HOURS = 12.0


def _env_float(name: str, default: float) -> float:
    """Float env knob. A blank or unparsable value falls back to the default
    rather than crashing (or silently unbounding) a multi-hour job on a typo."""
    raw = os.environ.get(name, "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        log.warning(f"== ignoring non-numeric {name}={raw!r}; using {default:g}")
        return default


def wall_seconds() -> float | None:
    """Per-run wall-clock budget in seconds: AGENT_WALL_MIN minutes (default
    180; 0 or negative = unlimited). Enforced by every backend — Claude's
    max_turns bounds turns but not a turn that hangs, and dsh has neither a
    turn cap nor a run-level timeout, so a model stuck in a loop would
    otherwise burn until the provider hangs up."""
    minutes = _env_float("AGENT_WALL_MIN", DEFAULT_WALL_MINUTES)
    return minutes * 60 if minutes > 0 else None


MAX_MALFORMED_ATTEMPTS = 2   # one retry on the same backend before it counts against the pool
MAX_INCOMPLETE_ATTEMPTS = 2  # ditto for a run that never submitted


async def retry_transient(
    coro_fn: Callable[[], Awaitable[T]],
    label: str,
    *,
    pool: "ModelPool | None" = None,
) -> T:
    """Run coro_fn(); on a transient-looking failure, retry a bounded number
    of times with linear backoff; on a usage/rate-limit-looking failure,
    wait and retry, bounded by a total wait budget (env AGENT_LIMIT_WAIT_MIN
    minutes between tries, default 10; AGENT_LIMIT_WAIT_MAX_H total hours,
    default 12). Any other failure raises immediately.

    `pool`, if given, is consulted (never mutated by anything else) when a
    failure is provably not fixed by retrying the same backend: an
    auth/permission/unknown-model error advances immediately, a run that
    never submits or keeps producing malformed submissions advances after
    a couple of same-backend retries. `coro_fn` is expected to read
    `pool.current()` itself on every call (`run_agent()` does); this
    function only decides *when* to advance and re-invoke `coro_fn`.
    Exhausting the pool raises with every abandoned candidate's reason
    attached. Without a pool, every one of these behaves exactly as before
    -- raise immediately.
    """
    wait_min = _env_float("AGENT_LIMIT_WAIT_MIN", DEFAULT_LIMIT_WAIT_MINUTES)
    max_h = _env_float("AGENT_LIMIT_WAIT_MAX_H", DEFAULT_LIMIT_WAIT_MAX_HOURS)
    waited = 0.0
    limit_attempt = 0
    transient_attempts = 0
    timeout_attempts = 0
    malformed_attempts = 0
    incomplete_attempts = 0

    def _advance_or_raise(reason: str, final: Exception) -> bool:
        """True if the pool advanced (caller should retry); raises `final`
        (with every candidate's reason attached) if there is nowhere left to
        fall back to, including when no pool was given at all."""
        if pool is None or not pool.can_advance():
            if pool is not None:
                trail = "; ".join(f"{c}: {r}" for c, r in [*pool.failures(), (pool.current(), reason)])
                raise type(final)(f"[{label}] model pool exhausted ({trail})") from None
            raise final
        old = pool.current()
        new = pool.advance(reason)
        log.warning(f"== [{label}] falling back from {old} to {new}: {reason[:200]}")
        return True

    while True:
        try:
            return await coro_fn()
        except AgentIncompleteError as e:
            # The run completed and the model simply never submitted. Its
            # message quotes the model's final reply, so it must never reach
            # the classifiers below: a biology answer mentioning "capacity"
            # would otherwise read as a usage limit and park the job for up
            # to AGENT_LIMIT_WAIT_MAX_H, re-running the whole session each time.
            # Without a pool this still raises on the very first occurrence,
            # unchanged from before pools existed -- the bounded retry below
            # is a pool-only behavior, not a default nobody asked for.
            if pool is None:
                raise
            incomplete_attempts += 1
            if incomplete_attempts < MAX_INCOMPLETE_ATTEMPTS:
                log.info(f"== [{label}] {e} — one fresh attempt")
                continue
            if _advance_or_raise(str(e), e):
                incomplete_attempts = 0
                continue
        except AgentTimeout as e:
            timeout_attempts += 1
            if timeout_attempts >= MAX_TIMEOUT_ATTEMPTS:
                raise
            log.info(f"== [{label}] {e} — one fresh attempt")
            continue
        except Exception as e:
            msg = str(e)
            if _is_startup_failure(e):
                # Retrying the same backend can never fix an auth/permission/
                # unknown-model error -- only a different candidate can, so
                # there is no same-backend retry step here at all.
                if _advance_or_raise(msg, e):
                    continue
            if _is_malformed_submission(e):
                malformed_attempts += 1
                if malformed_attempts < MAX_MALFORMED_ATTEMPTS:
                    log.info(f"== [{label}] malformed submission (attempt {malformed_attempts}/"
                          f"{MAX_MALFORMED_ATTEMPTS}): {msg[:160]!r} — one fresh turn")
                    continue
                if _advance_or_raise(msg, e):
                    malformed_attempts = 0
                    continue
            if TRANSIENT_PATTERN.search(msg):
                transient_attempts += 1
                if transient_attempts >= MAX_TRANSIENT_ATTEMPTS:
                    raise RuntimeError(
                        f"[{label}] transient agent failure persisted after "
                        f"{transient_attempts} attempts: {msg}"
                    ) from None
                wait = TRANSIENT_BACKOFF_SECONDS * transient_attempts
                log.info(f"== [{label}] transient agent failure (attempt {transient_attempts}/"
                      f"{MAX_TRANSIENT_ATTEMPTS}): {msg[:160]!r} — retrying in {wait}s")
                await asyncio.sleep(wait)
                continue
            if LIMIT_PATTERN.search(msg):
                # A busy provider is not the pool's problem: waiting is the
                # recovery, and falling back here would quietly move a run
                # onto a different (possibly pricier) backend just because
                # the first one was busy (issue #1, explicitly not a fallback).
                limit_attempt += 1
                if waited / 3600 >= max_h:
                    raise AgentLimitExhausted(
                        f"[{label}] usage limit still in force after {waited / 3600:.1f} h: {msg}"
                    ) from None
                log.info(f"== [{label}] usage/rate limit (attempt {limit_attempt}): {msg[:160]!r} — "
                      f"waiting {wait_min:.0f} min, {max_h - waited / 3600:.1f} h of wait budget left")
                t0 = time.time()
                await asyncio.sleep(wait_min * 60)
                waited += time.time() - t0
                continue
            raise


# --------------------------------------------------------------------------
# Tool table contract
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    # {param_name: python_type} — the same flat shape claude_agent_sdk's
    # @tool() takes as its third argument; translated to real JSON Schema
    # for the OpenAI and DeepSeek backends.
    input_schema: dict[str, type]
    handler: ToolHandler


SLOW_TOOL_SECONDS = 1.0  # a single tool call at or above this gets its own "took" line


def _timed(spec: ToolSpec, times: dict[str, list[float]], label: str) -> ToolSpec:
    """Same tool, but every call is timed: slow calls are logged as they
    return and every duration feeds the end-of-run summary."""
    inner = spec.handler

    async def handler(arguments):
        t0 = time.monotonic()
        try:
            return await inner(arguments)
        finally:
            dt = time.monotonic() - t0
            times.setdefault(spec.name, []).append(dt)
            if dt >= SLOW_TOOL_SECONDS:
                log.info(f"== [{label}] {spec.name} took {dt:.1f} s")

    return dataclasses.replace(spec, handler=handler)


def _log_tool_summary(label: str, times: dict[str, list[float]], wall: float) -> None:
    """One line per run: wall time, time inside application tools, and the
    tools ranked by total time — the answer to "where did this run's time
    go" without instrumenting the kernels."""
    calls = sum(len(v) for v in times.values())
    in_tools = sum(sum(v) for v in times.values())
    ranked = sorted(times.items(), key=lambda kv: -sum(kv[1]))
    detail = ", ".join(f"{name} {sum(v):.1f} s ×{len(v)}" for name, v in ranked[:6])
    log.info(f"== [{label}] time: wall {wall:.0f} s, tools {in_tools:.1f} s in {calls} call(s)"
             + (f" — {detail}" if detail else ""))


@dataclass
class AgentRunResult:
    submitted: dict | None  # whatever the submit tool's handler captured; None if it never fired
    transcript_text: str | None  # best-effort final assistant text, for *_notes.md-style logging
    cost_usd: float | None  # best-effort; None where the backend doesn't report it
    # The config that actually produced this result -- set by run_agent(), never
    # by an adapter (adapters only see a plain model string, not the resolved
    # AgentConfig). Defaults to None only for the rare direct adapter-level
    # construction in tests; every run_agent() caller gets a real value, even
    # with no pool, so "what backend actually answered this" never requires
    # re-deriving it from HARNESS/MODEL after the fact.
    effective_config: "AgentConfig | None" = None
    tokens_in: int | None = None  # best-effort; None where the backend doesn't report it
    tokens_out: int | None = None


# Tool names each read-only capability exposes to the model. HARNESS=claude
# allows Claude Code's own tools under these names; the OpenAI and DeepSeek
# adapters serve same-named host-side tools so application prompts stay
# portable across backends.
BUILTIN_TOOL_NAMES: dict[BuiltinCapability, tuple[str, ...]] = {
    "read": ("Read",),
    "glob": ("Glob",),
    "grep": ("Grep",),
    "tasks": ("TaskCreate", "TaskUpdate", "TaskList", "TaskGet"),
}
BUILTIN_CAPABILITIES = frozenset(BUILTIN_TOOL_NAMES)


def _validate_tool_table(tools: list[ToolSpec], submit_tool: str, allowed_builtin: tuple[str, ...]) -> None:
    """Reject a malformed call before any SDK is imported. A misspelled
    submit tool can never fire, so without this the mistake would surface
    only as AgentIncompleteError after a complete (paid) run."""
    names = [spec.name for spec in tools]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ValueError(f"duplicate tool names in the tool table: {duplicates}")
    if submit_tool not in names:
        raise ValueError(f"submit tool {submit_tool!r} is not in the tool table {names}")
    unknown = sorted(set(allowed_builtin) - BUILTIN_CAPABILITIES)
    if unknown:
        raise ValueError(f"unsupported allowed_builtin capabilities: {unknown}")
    reserved = {name for cap in allowed_builtin for name in BUILTIN_TOOL_NAMES[cast(BuiltinCapability, cap)]}
    clashes = sorted(set(names) & reserved)
    if clashes:
        raise ValueError(f"tool names collide with requested builtin tools: {clashes}")


# --------------------------------------------------------------------------
# Backend selection
# --------------------------------------------------------------------------

# One row per backend: the adapter module (imported lazily, so an application
# needs only the SDK it selected) and the model used when MODEL is unset.
# Model ids stay open strings by design — no catalog to keep current.
_BACKENDS: dict[HarnessName, tuple[str, str]] = {
    "openai": ("._harness_openai", "doubao-seed-2-1-turbo-260628"),
    # HARNESS=deepseek's default provider is Doubao via dsh's pi-ai adapter
    # (see _harness_deepseek); DSH_PROVIDER=deepseek-official switches to a
    # real DeepSeek model, in which case override MODEL too.
    "deepseek": ("._harness_deepseek", "doubao-seed-2-1-turbo-260628"),
    "claude": ("._harness_claude", "claude-sonnet-5"),
}
DEFAULT_BACKEND = "openai"
KNOWN_BACKENDS = frozenset(_BACKENDS)


@dataclass(frozen=True, slots=True)
class AgentConfig:
    """Resolved runtime choice. Model ids remain open strings by design."""

    harness: HarnessName
    model: str

    def as_manifest(self) -> dict[str, str]:
        return {"harness": self.harness, "model": self.model}

    def __str__(self) -> str:
        return f"{self.harness}:{self.model}"


class ModelPool:
    """Ordered {harness, model} candidates, first = primary. `retry_transient`
    advances forward on a provably-broken current candidate (never back --
    'one fallback per failure class, no ping-pong'); everything else about a
    fallback (which failures qualify, backoff) lives in retry_transient, this
    object only tracks position and the trail of what failed and why.

    One instance per *stickiness scope* -- the caller decides what that scope
    is (e.g. ECA-RSI creates one per crosssample/zoomin stage, matching
    `.rsi-stage.json`'s own granularity, and threads it through every agent
    call in that stage) and passes the same instance to every `run_agent()`
    call that should share it. The bridge has no opinion on what a "stage"
    is; it only tracks which candidate is current.
    """

    def __init__(self, candidates: list[AgentConfig]) -> None:
        if not candidates:
            raise ValueError("ModelPool needs at least one candidate")
        self._candidates = list(candidates)
        self._index = 0
        self._failures: list[tuple[AgentConfig, str]] = []

    def current(self) -> AgentConfig:
        return self._candidates[self._index]

    def can_advance(self) -> bool:
        return self._index + 1 < len(self._candidates)

    def advance(self, reason: str) -> AgentConfig:
        if not self.can_advance():
            raise RuntimeError("model pool exhausted")
        self._failures.append((self.current(), reason))
        self._index += 1
        return self.current()

    def failures(self) -> list[tuple[AgentConfig, str]]:
        """(config, reason) for every candidate abandoned so far, in order --
        attached to the final error message when the whole pool is exhausted."""
        return list(self._failures)


def parse_model_pool(spec: str) -> list[AgentConfig]:
    """'harness:model,harness:model,...' -- harness and model are always
    paired in one token, never two separate lists: a model pinned without its
    harness is exactly how a run asks one backend for another's model id
    (eca-pp#6, 257 generated job scripts pinned a claude model with no
    HARNESS=claude)."""
    out = []
    for item in spec.split(","):
        item = item.strip()
        if not item:
            continue
        harness, sep, model = item.partition(":")
        if not sep or not harness.strip() or not model.strip():
            raise ValueError(f"AGENT_MODEL_POOL entry {item!r} must be 'harness:model'")
        out.append(resolve_agent_config(harness=harness.strip(), model=model.strip()))
    if not out:
        raise ValueError("AGENT_MODEL_POOL is set but empty")
    return out


def resolve_model_pool(environ: Mapping[str, str] | None = None) -> "ModelPool | None":
    """None when AGENT_MODEL_POOL is unset -- the single-config path is then
    unchanged from before pools existed."""
    env = os.environ if environ is None else environ
    spec = env.get("AGENT_MODEL_POOL", "").strip()
    return ModelPool(parse_model_pool(spec)) if spec else None


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
    if selected not in _BACKENDS:
        raise ValueError(
            f"unknown HARNESS backend {selected!r} (expected one of {sorted(_BACKENDS)})"
        )
    backend = cast(HarnessName, selected)
    selected_model = (model or env.get("MODEL") or _BACKENDS[backend][1]).strip()
    if not selected_model:
        raise ValueError(f"HARNESS={selected} needs a non-empty model id")
    return AgentConfig(harness=backend, model=selected_model)


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
    if resolved.harness == "openai":
        mode = (openai_api or env.get("OPENAI_AGENTS_API") or "responses").strip().lower()
        if mode not in {"responses", "chat_completions"}:
            raise ValueError(
                f"invalid OPENAI_AGENTS_API={mode!r} (expected 'responses' or 'chat_completions')"
            )
        responses = mode == "responses"
        return HarnessCapabilities(
            builtins=BUILTIN_CAPABILITIES,
            image_tool_outputs=responses,
            response_chaining=responses,
            same_session_nudge=True,
            mcp_transport=False,
            context_reset=True,
        )
    if resolved.harness == "deepseek":
        return HarnessCapabilities(
            builtins=BUILTIN_CAPABILITIES,
            image_tool_outputs=True,
            response_chaining=False,
            same_session_nudge=True,
            mcp_transport=True,
            context_reset=False,
        )
    return HarnessCapabilities(
        builtins=BUILTIN_CAPABILITIES,
        image_tool_outputs=True,
        response_chaining=False,
        same_session_nudge=False,
        mcp_transport=True,
        context_reset=False,
    )


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


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
    pool: "ModelPool | None" = None,
) -> AgentRunResult:
    """Run one agent turn to completion; raise AgentIncompleteError if
    `submit_tool` never fired. `allowed_builtin` is the read-only filesystem
    exploration surface ("read", "glob", "grep") plus "tasks" — a session
    task list the model keeps as its own progress checklist (Claude Code's
    TaskCreate/TaskUpdate/TaskList/TaskGet; the DeepSeek and OpenAI backends
    serve same-named in-memory tools so prompts stay identical). The
    model never gets write access under any backend.

    `pool`: an ordered ModelPool to fall back through on a provably-broken
    current candidate (see ModelPool). Falls back to AGENT_MODEL_POOL from
    the environment when not given explicitly; with neither, a single
    AgentConfig is resolved from HARNESS/MODEL exactly as before pools
    existed. `model` is ignored once a pool is in play -- pass a one-item
    pool instead of both.
    """
    ensure_logging()
    if pool is None:
        pool = resolve_model_pool()
    single_config = None if pool is not None else resolve_agent_config(model=model)
    _validate_tool_table(tools, submit_tool, allowed_builtin)
    wall = wall_seconds()
    tool_times: dict[str, list[float]] = {}
    tools = [_timed(spec, tool_times, label) for spec in tools]
    started = time.monotonic()

    async def _attempt() -> AgentRunResult:
        config = pool.current() if pool is not None else single_config
        adapter = importlib.import_module(_BACKENDS[config.harness][0], __package__)
        coro = adapter.run_agent(
            tools=tools, submit_tool=submit_tool, prompt=prompt, system_prompt=system_prompt,
            cwd=cwd, model=config.model, effort=effort, max_turns=max_turns,
            allowed_builtin=allowed_builtin, label=label, max_buffer_size=max_buffer_size,
            wall_seconds=wall,
        )
        if wall is None:
            result = await coro
        else:
            try:
                # the backend enforces the budget itself (dsh: watchdog closes
                # the runtime; claude: cancellation tears the CLI down); this
                # is the backstop that also covers a backend stuck in its own
                # teardown
                result = await asyncio.wait_for(coro, timeout=wall + 120)
            except asyncio.TimeoutError:
                raise AgentTimeout(f"[{label}] agent run exceeded the wall-clock budget of "
                                   f"{wall / 60:g} min (AGENT_WALL_MIN)") from None
        result.effective_config = config
        return result

    try:
        return await retry_transient(_attempt, label, pool=pool)
    finally:
        _log_tool_summary(label, tool_times, time.monotonic() - started)

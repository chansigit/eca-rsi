"""Pluggable agent execution backend for every model-facing call in ecarsi.

Every call site builds a small, self-contained tool table (the "submit tool"
pattern already used by osp/msp/zmip: one designated tool ends the run and
its handler is the only place the actual answer is produced/validated — the
model never needs filesystem write access to get its answer out) and hands
it to `run_agent()`. Which SDK actually drives the model is an env-var
choice, not a call-site choice:

    HARNESS=claude     (default) claude_agent_sdk, in-process MCP tools
    HARNESS=deepseek    DeepSeek Harness (dsh) via its Python SDK, tools
                         bridged over a real stdio MCP server subprocess

The tool `handler` return shape (`{"content": [{"type": "text", ...}],
"is_error": bool}`) is already the real MCP `CallToolResult` wire shape —
Claude Agent SDK's in-process server is itself an MCP server — so the same
handler bodies serve both backends unchanged.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Awaitable, Callable

ToolHandler = Callable[[dict], Awaitable[dict]]


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


def backend_name() -> str:
    return os.environ.get("HARNESS", "claude")


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
    exploration surface ("read", "glob", "grep" — the only values used
    anywhere in this codebase today); the model never gets write access
    under either backend."""
    backend = backend_name()
    if backend == "claude":
        from ._harness_claude import run_agent as _run
    elif backend == "deepseek":
        from ._harness_deepseek import run_agent as _run
    else:
        raise ValueError(f"unknown HARNESS backend {backend!r} (expected 'claude' or 'deepseek')")
    return await _run(
        tools=tools, submit_tool=submit_tool, prompt=prompt, system_prompt=system_prompt,
        cwd=cwd, model=model, effort=effort, max_turns=max_turns,
        allowed_builtin=allowed_builtin, label=label, max_buffer_size=max_buffer_size,
    )

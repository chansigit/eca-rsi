"""Stable public API for the agent harness bridge."""

from .harness import (
    AgentConfig,
    AgentIncompleteError,
    AgentLimitExhausted,
    AgentRunResult,
    AgentTimeout,
    HarnessCapabilities,
    ToolSpec,
    backend_capabilities,
    backend_name,
    default_model,
    resolve_agent_config,
    retry_transient,
    run_agent,
    wall_seconds,
)

__all__ = [
    "AgentConfig",
    "AgentIncompleteError",
    "AgentLimitExhausted",
    "AgentRunResult",
    "AgentTimeout",
    "HarnessCapabilities",
    "ToolSpec",
    "backend_capabilities",
    "backend_name",
    "default_model",
    "resolve_agent_config",
    "retry_transient",
    "run_agent",
    "wall_seconds",
]

__version__ = "0.1.0"

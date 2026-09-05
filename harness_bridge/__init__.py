"""Stable public API for the agent harness bridge."""

from ._logging import DEFAULT_FORMAT, configure_logging, ensure_logging
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
    "DEFAULT_FORMAT",
    "HarnessCapabilities",
    "ToolSpec",
    "backend_capabilities",
    "backend_name",
    "configure_logging",
    "default_model",
    "ensure_logging",
    "resolve_agent_config",
    "retry_transient",
    "run_agent",
    "wall_seconds",
]

__version__ = "0.2.1"

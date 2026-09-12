"""Stable public API for the agent harness bridge."""

from ._logging import DEFAULT_FORMAT, configure_logging, ensure_logging
from .harness import (
    AgentConfig,
    AgentIncompleteError,
    AgentLimitExhausted,
    AgentRunResult,
    AgentTimeout,
    HarnessCapabilities,
    ModelPool,
    ToolSpec,
    backend_capabilities,
    backend_name,
    default_model,
    parse_model_pool,
    resolve_agent_config,
    resolve_model_pool,
    retry_transient,
    rotate_model_pool,
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
    "ModelPool",
    "ToolSpec",
    "backend_capabilities",
    "backend_name",
    "configure_logging",
    "default_model",
    "ensure_logging",
    "parse_model_pool",
    "resolve_agent_config",
    "resolve_model_pool",
    "retry_transient",
    "rotate_model_pool",
    "run_agent",
    "wall_seconds",
]

__version__ = "0.2.13"

"""ecarsi — pluggable-harness tooling for the eca-rsi curation loop.

HARNESS env var selects the agent execution backend for every call in this
package (see ecarsi.harness): 'deepseek' (default — DeepSeek Harness / dsh
driving Doubao, see ecarsi._harness_deepseek) or 'claude' (claude_agent_sdk,
spends Claude Code quota)."""


def model() -> str:
    """Model for every agent call in this package: MODEL env, else the
    HARNESS-appropriate default — a bare model name is never portable
    across backends. Same rule as osp/msp/zmip's harness.default_model()."""
    from .harness import default_model

    return default_model()

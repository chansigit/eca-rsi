"""ecarsi — pluggable-harness tooling for the eca-rsi curation loop.

HARNESS env var selects the agent execution backend for every call in this
package (see ecarsi.harness): 'claude' (default, claude_agent_sdk) or
'deepseek' (DeepSeek Harness / dsh, see ecarsi._harness_deepseek)."""

import os

DEFAULT_MODEL = {
    "claude": "claude-sonnet-5",
    # HARNESS=deepseek's default provider is Doubao (via dsh's pi-ai adapter,
    # see ecarsi._harness_deepseek), not a DeepSeek model — DSH_PROVIDER=
    # deepseek-official switches back, in which case override MODEL too.
    "deepseek": "doubao-seed-2-1-turbo-260628",
}


def model() -> str:
    """Model for every agent call in this package: MODEL env, else the
    HARNESS-appropriate default — a bare model name is never portable
    across backends."""
    backend = os.environ.get("HARNESS", "claude")
    default = DEFAULT_MODEL.get(backend, DEFAULT_MODEL["claude"])
    return os.environ.get("MODEL", default)

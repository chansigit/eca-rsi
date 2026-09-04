"""ecarsi — pluggable-harness tooling for the eca-rsi curation loop.

HARNESS env var selects the agent execution backend for every call in this
package (see ecarsi.harness): 'openai' (default — OpenAI Agents SDK driving
Doubao through Ark), 'deepseek' (DeepSeek Harness / dsh driving Doubao), or
'claude' (claude_agent_sdk, spends Claude Code quota)."""

from __future__ import annotations

import os


def model() -> str:
    """Model for every agent call in this package: MODEL env, else the
    HARNESS-appropriate default — a bare model name is never portable
    across backends. Same rule as osp/msp/zmip's harness.default_model()."""
    from .harness import default_model

    return default_model()


def agent_config() -> dict[str, str]:
    """The two independent choices that must stay fixed within a run."""
    from .harness import backend_name

    return {"harness": backend_name(), "model": model()}


def check_agent_config(recorded: dict, where: str) -> None:
    """Reject a silent backend/model switch when resuming modern outputs.

    Manifests written before these fields existed remain resumable with an
    explicit warning because their original configuration cannot be proved.
    """
    if "harness" not in recorded or "model" not in recorded:
        print(f"[agent] {where} predates harness/model recording — resume cannot verify the old choice")
        return
    want = agent_config()
    got = {"harness": str(recorded["harness"]), "model": str(recorded["model"])}
    if got == want:
        return
    message = (
        f"{where} used harness={got['harness']} model={got['model']}; current selection is "
        f"harness={want['harness']} model={want['model']}"
    )
    allow = os.environ.get("ECA_ALLOW_AGENT_CHANGE", "").strip().lower() in {"1", "true", "yes", "on"}
    if not allow:
        raise RuntimeError(message + " (use --allow-agent-change only if a mixed run is intentional)")
    print(f"[agent] WARNING: {message} — mixed run explicitly allowed")

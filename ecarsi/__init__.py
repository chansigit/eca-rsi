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


def effective_or_requested(fn) -> dict[str, str]:
    """{"harness", "model"} for a manifest field recorded *after* an agent
    call: the AgentConfig that actually produced the result if `fn` stashed
    one (the `.last_effective_config` convention, same idea as `.last_cost` --
    set by the caller of run_agent(), read here), else the plain env
    snapshot from agent_config(). With a fallback pool (AGENT_MODEL_POOL) in
    play, these can differ; without one they are always the same value.

    Only usable where the manifest field is a *record* of what happened, not
    an input to a pre-call resume/identity decision -- persample's own
    config is built and hashed before its agent call can run at all, so it
    correctly keeps using agent_config() unconditionally.
    """
    cfg = getattr(fn, "last_effective_config", None)
    return cfg.as_manifest() if cfg is not None else agent_config()


def check_agent_config(recorded: dict, where: str) -> str | None:
    """Compare a resumed stage's recorded {harness, model} against the
    current selection; never blocks the resume (2026-09-10: switching
    backend mid-run is a legitimate operator move -- round 3 shows the
    current model's QC judgement is too lax, so the remaining rounds
    continue with a stronger one -- dropped the --allow-agent-change guard
    that used to require asking permission for it every time).

    Returns the human-readable mismatch message (also printed) so a caller
    that has a durable per-unit log can additionally record it there; None
    when the config matches or predates harness/model recording (older
    manifests remain resumable with a warning because their original choice
    cannot be proved either way).
    """
    if "harness" not in recorded or "model" not in recorded:
        print(f"[agent] {where} predates harness/model recording — resume cannot verify the old choice")
        return None
    want = agent_config()
    got = {"harness": str(recorded["harness"]), "model": str(recorded["model"])}
    if got == want:
        return None
    message = (
        f"{where} used harness={got['harness']} model={got['model']}; current selection is "
        f"harness={want['harness']} model={want['model']}"
    )
    print(f"[agent] {message} — continuing (mixed-model run)")
    return message

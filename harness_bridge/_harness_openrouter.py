"""HARNESS=openrouter: the OpenAI Agents SDK loop of ``_harness_openai``
against OpenRouter (https://openrouter.ai/api/v1, OPENROUTER_API_KEY).

Same tools, same nudge/reset logic; the differences live in
``_harness_openai.PROVIDERS["openrouter"]``: OpenRouter's Responses endpoint
is stateless, so the complete local history is sent every turn (no
previous_response_id, no store). Model ids are OpenRouter's
``vendor/model[:free]`` strings; free-tier models come and go and rate-limit
upstream, which is what an AGENT_MODEL_POOL is for.
"""

from __future__ import annotations

from ._harness_openai import run_agent as _run_openai


async def run_agent(**kwargs):
    return await _run_openai(provider="openrouter", **kwargs)

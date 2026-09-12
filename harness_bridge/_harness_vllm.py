"""HARNESS=vllm: the OpenAI Agents SDK loop of ``_harness_openai`` against a
self-hosted OpenAI-compatible server (vLLM, ``VLLM_BASE_URL``, ``VLLM_API_KEY``).
Responses API with images and tools works on vLLM >= 0.27; conversation
state stays local (full history every turn -- vLLM's prefix cache makes
that cheap)."""

from __future__ import annotations

from ._harness_openai import run_agent as _run_openai


async def run_agent(**kwargs):
    return await _run_openai(provider="vllm", **kwargs)

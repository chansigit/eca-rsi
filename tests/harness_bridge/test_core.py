"""Provider-free tests for the stable bridge contract."""

from __future__ import annotations

import asyncio

import pytest

import harness_bridge
from harness_bridge import (
    AgentRunResult,
    ToolSpec,
    backend_capabilities,
    resolve_agent_config,
)


def test_default_config_is_openai_turbo():
    config = resolve_agent_config(environ={})
    assert config.as_manifest() == {
        "harness": "openai",
        "model": "doubao-seed-2-1-turbo-260628",
    }


@pytest.mark.parametrize(
    ("harness", "expected"),
    [
        ("openai", "doubao-seed-2-1-turbo-260628"),
        ("deepseek", "doubao-seed-2-1-turbo-260628"),
        ("claude", "claude-sonnet-5"),
    ],
)
def test_backend_defaults(harness, expected):
    assert resolve_agent_config(harness=harness, environ={}).model == expected


def test_explicit_config_overrides_environment_and_accepts_new_models():
    config = resolve_agent_config(
        harness="openai",
        model="doubao-seed-2-1-pro-260628",
        environ={"HARNESS": "deepseek", "MODEL": "old-model"},
    )
    assert config.harness == "openai"
    assert config.model == "doubao-seed-2-1-pro-260628"


def test_unknown_backend_fails_before_an_adapter_is_imported():
    with pytest.raises(ValueError, match="unknown HARNESS backend"):
        resolve_agent_config(harness="unknown", environ={})


def test_capabilities_reflect_openai_api_surface():
    config = resolve_agent_config(harness="openai", environ={})
    responses = backend_capabilities(config, openai_api="responses", environ={})
    chat = backend_capabilities(config, openai_api="chat_completions", environ={})

    assert responses.image_tool_outputs is True
    assert responses.response_chaining is True
    assert chat.image_tool_outputs is False
    assert chat.response_chaining is False
    assert responses.builtins == frozenset({"read", "glob", "grep", "tasks"})


def test_public_types_are_singletons_across_package_exports():
    from harness_bridge import harness

    assert harness_bridge.ToolSpec is harness.ToolSpec
    assert harness_bridge.AgentRunResult is harness.AgentRunResult


def test_run_agent_uses_resolved_model(monkeypatch, tmp_path):
    captured = {}

    async def backend(**kwargs):
        captured.update(kwargs)
        return AgentRunResult({"ok": True}, None, None)

    async def submit(_args):
        raise AssertionError("backend is mocked")

    monkeypatch.setenv("HARNESS", "openai")
    monkeypatch.setenv("MODEL", "doubao-seed-2-1-pro-260628")
    monkeypatch.setattr("harness_bridge._harness_openai.run_agent", backend)
    result = asyncio.run(harness_bridge.run_agent(
        tools=[ToolSpec("submit", "submit", {}, submit)],
        submit_tool="submit",
        prompt="probe",
        cwd=str(tmp_path),
    ))

    assert result.submitted == {"ok": True}
    assert captured["model"] == "doubao-seed-2-1-pro-260628"

"""Provider-free tests for the stable bridge contract."""

from __future__ import annotations

import asyncio
import logging

import pytest

import harness_bridge
from harness_bridge import (
    AgentIncompleteError,
    AgentLimitExhausted,
    AgentRunResult,
    AgentTimeout,
    ToolSpec,
    backend_capabilities,
    resolve_agent_config,
    retry_transient,
    wall_seconds,
)
from harness_bridge import harness as H


async def _submit(_args):
    raise AssertionError("backend is mocked")


SUBMIT = ToolSpec("submit", "submit", {}, _submit)


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
    assert harness_bridge.ToolSpec is H.ToolSpec
    assert harness_bridge.AgentRunResult is H.AgentRunResult


def test_wall_seconds_parsing(monkeypatch):
    monkeypatch.delenv("AGENT_WALL_MIN", raising=False)
    assert wall_seconds() == H.DEFAULT_WALL_MINUTES * 60
    monkeypatch.setenv("AGENT_WALL_MIN", "0")
    assert wall_seconds() is None
    monkeypatch.setenv("AGENT_WALL_MIN", "2.5")
    assert wall_seconds() == 150
    monkeypatch.setenv("AGENT_WALL_MIN", "three hours")  # a typo must not unbound the run
    assert wall_seconds() == H.DEFAULT_WALL_MINUTES * 60


def test_run_agent_uses_resolved_model(monkeypatch, tmp_path):
    captured = {}

    async def backend(**kwargs):
        captured.update(kwargs)
        return AgentRunResult({"ok": True}, None, None)

    monkeypatch.setenv("HARNESS", "openai")
    monkeypatch.setenv("MODEL", "doubao-seed-2-1-pro-260628")
    monkeypatch.setattr("harness_bridge._harness_openai.run_agent", backend)
    result = asyncio.run(harness_bridge.run_agent(
        tools=[SUBMIT], submit_tool="submit", prompt="probe", cwd=str(tmp_path),
    ))

    assert result.submitted == {"ok": True}
    assert captured["model"] == "doubao-seed-2-1-pro-260628"
    assert captured["wall_seconds"] == H.DEFAULT_WALL_MINUTES * 60


@pytest.mark.parametrize(
    ("tools", "submit_tool", "allowed_builtin", "match"),
    [
        ([SUBMIT], "submit_answer", (), "not in the tool table"),
        ([SUBMIT, SUBMIT], "submit", (), "duplicate tool names"),
        ([SUBMIT], "submit", ("read", "write"), "unsupported allowed_builtin"),
        ([SUBMIT, ToolSpec("Read", "shadow", {}, _submit)], "submit", ("read",), "collide with requested builtin"),
    ],
)
def test_run_agent_rejects_malformed_tool_tables_before_any_sdk(monkeypatch, tmp_path, tools, submit_tool,
                                                                allowed_builtin, match):
    async def backend(**_kwargs):
        raise AssertionError("adapter must not run")

    monkeypatch.setenv("HARNESS", "openai")
    monkeypatch.setattr("harness_bridge._harness_openai.run_agent", backend)
    with pytest.raises(ValueError, match=match):
        asyncio.run(harness_bridge.run_agent(
            tools=tools, submit_tool=submit_tool, prompt="p", cwd=str(tmp_path), allowed_builtin=allowed_builtin,
        ))


def test_application_tool_may_reuse_a_builtin_name_it_did_not_request(monkeypatch, tmp_path):
    async def backend(**_kwargs):
        return AgentRunResult({"ok": True}, None, None)

    monkeypatch.setenv("HARNESS", "openai")
    monkeypatch.setattr("harness_bridge._harness_openai.run_agent", backend)
    tools = [SUBMIT, ToolSpec("Grep", "domain grep", {}, _submit)]
    result = asyncio.run(harness_bridge.run_agent(
        tools=tools, submit_tool="submit", prompt="p", cwd=str(tmp_path), allowed_builtin=("read",),
    ))
    assert result.submitted == {"ok": True}


# --------------------------------------------------------------------------
# retry_transient
# --------------------------------------------------------------------------


@pytest.fixture
def instant_sleep(monkeypatch):
    slept: list[float] = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(H.asyncio, "sleep", fake_sleep)
    return slept


def _failing(errors, then=None):
    calls = {"n": 0}

    async def attempt():
        calls["n"] += 1
        if calls["n"] <= len(errors):
            raise errors[calls["n"] - 1]
        return then

    return attempt, calls


def test_transient_failures_retry_with_linear_backoff(instant_sleep):
    attempt, calls = _failing([RuntimeError("Control request timeout"), OSError("Broken pipe")], then="ok")
    assert asyncio.run(retry_transient(attempt, "t")) == "ok"
    assert calls["n"] == 3
    assert instant_sleep == [20, 40]


def test_ark_gateway_400_and_5xx_are_retried_as_transient(instant_sleep):
    # Seen after multi-PNG Read turns: Ark answers with a non-JSON 400 body,
    # so the openai client's message is the bare text (no "Error code:" prefix,
    # no request id). Same payload succeeds on replay, so it must not end the run.
    attempt, calls = _failing([
        RuntimeError("Error when parsing request"),
        RuntimeError("Error code: 500 - {'error': {'code': 'InternalServiceError', 'message': 'x'}}"),
    ], then="ok")
    assert asyncio.run(retry_transient(attempt, "t")) == "ok"
    assert calls["n"] == 3
    assert instant_sleep == [20, 40]


def test_transient_failures_give_up_after_the_attempt_budget(instant_sleep):
    attempt, calls = _failing([RuntimeError("process exited unexpectedly")] * 10)
    with pytest.raises(RuntimeError, match="persisted after 5 attempts"):
        asyncio.run(retry_transient(attempt, "t"))
    assert calls["n"] == 5


def test_usage_limit_waits_within_the_hour_budget(instant_sleep, monkeypatch):
    monkeypatch.setenv("AGENT_LIMIT_WAIT_MIN", "30")
    monkeypatch.setenv("AGENT_LIMIT_WAIT_MAX_H", "1")
    fake_now = {"t": 0.0}

    def clock():
        fake_now["t"] += 1800  # each wait "takes" the full 30 min
        return fake_now["t"]

    monkeypatch.setattr(H.time, "time", clock)
    attempt, calls = _failing([RuntimeError("429 Too Many Requests")] * 10)
    with pytest.raises(AgentLimitExhausted):
        asyncio.run(retry_transient(attempt, "t"))
    assert instant_sleep == [1800, 1800]  # two waits exhaust the 1 h budget; the third failure is final
    assert calls["n"] == 3


def test_wall_clock_timeout_gets_exactly_one_fresh_attempt(instant_sleep):
    attempt, calls = _failing([AgentTimeout("budget hit")], then="ok")
    assert asyncio.run(retry_transient(attempt, "t")) == "ok"
    attempt, calls = _failing([AgentTimeout("budget hit")] * 3)
    with pytest.raises(AgentTimeout):
        asyncio.run(retry_transient(attempt, "t"))
    assert calls["n"] == 2


def test_incomplete_run_is_never_mistaken_for_a_limit_or_transient_failure(instant_sleep):
    # The model's own final reply is quoted in this message; it contains the
    # words the limit and transient classifiers look for.
    reply = ("Final reply:\nCluster 3 shows high proliferative capacity; the connection closed "
             "between the two lineages hints at a rate limit on differentiation, quota 429.")
    attempt, calls = _failing([AgentIncompleteError(reply)] * 3)
    with pytest.raises(AgentIncompleteError):
        asyncio.run(retry_transient(attempt, "t"))
    assert calls["n"] == 1
    assert instant_sleep == []


def test_unclassified_failures_raise_immediately(instant_sleep):
    attempt, calls = _failing([ValueError("bad schema")] * 3)
    with pytest.raises(ValueError):
        asyncio.run(retry_transient(attempt, "t"))
    assert calls["n"] == 1


def test_tool_calls_are_timed_and_summarised(monkeypatch, tmp_path, caplog):
    """Every application tool is wrapped: slow calls get a "took" line as they
    return, and the run ends with one wall/tools summary ranked by tool."""
    async def slow(args):
        return {"content": [{"type": "text", "text": "ok"}]}

    async def backend(**kwargs):
        by_name = {t.name: t for t in kwargs["tools"]}
        await by_name["slow"].handler({"x": 1})
        await by_name["slow"].handler({"x": 2})
        return AgentRunResult({"ok": True}, None, None)

    monkeypatch.setenv("HARNESS", "openai")
    monkeypatch.setattr("harness_bridge._harness_openai.run_agent", backend)
    monkeypatch.setattr(H, "SLOW_TOOL_SECONDS", 0.0)
    with caplog.at_level(logging.INFO, logger="harness_bridge"):
        asyncio.run(harness_bridge.run_agent(
            tools=[SUBMIT, ToolSpec("slow", "x", {"x": int}, slow)], submit_tool="submit",
            prompt="probe", cwd=str(tmp_path), label="t",
        ))
    took = [r.message for r in caplog.records if " took " in r.message]
    assert len(took) == 2 and took[0].startswith("== [t] slow took ")
    summary = [r.message for r in caplog.records if "] time: wall" in r.message]
    assert len(summary) == 1 and "in 2 call(s)" in summary[0] and "slow " in summary[0] and "×2" in summary[0]

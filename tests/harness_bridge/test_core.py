"""Provider-free tests for the stable bridge contract."""

from __future__ import annotations

import asyncio
import logging

import pytest

import harness_bridge
from harness_bridge import (
    AgentConfig,
    AgentIncompleteError,
    AgentLimitExhausted,
    AgentRunResult,
    AgentTimeout,
    ModelPool,
    ToolSpec,
    backend_capabilities,
    parse_model_pool,
    rotate_model_pool,
    resolve_agent_config,
    resolve_model_pool,
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


def test_a_forgotten_previous_response_restarts_the_run(instant_sleep):
    # With SERVER_STATE=1 the SDK chains turns by previous_response_id. Ark has
    # been seen to forget a stored response under load (2026-09-07, right after
    # two 429 ServerOverloaded in the same call). The id is gone for good, so
    # replaying that request would be pointless -- but the retry here starts a
    # fresh run with a new chain, which does recover. Without it the stage dies
    # and the driver recomputes it from the top.
    attempt, calls = _failing([RuntimeError(
        "Error code: 400 - {'error': {'code': 'InvalidParameter.PreviousResponseNotFound', "
        "'message': 'Previous response with id resp_02178882 not found'}}")], then="ok")
    assert asyncio.run(retry_transient(attempt, "t")) == "ok"
    assert calls["n"] == 2


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


# --------------------------------------------------------------------------
# ModelPool: parsing, resolution, position tracking
# --------------------------------------------------------------------------


def test_parse_model_pool_pairs_harness_and_model():
    pool = ModelPool(parse_model_pool("openai:doubao-seed-2-1-turbo, claude:claude-sonnet-5"))
    assert str(pool.current()) == "openai:doubao-seed-2-1-turbo"
    assert pool.can_advance()
    assert str(pool.advance("test")) == "claude:claude-sonnet-5"
    assert not pool.can_advance()


@pytest.mark.parametrize("spec", ["openai", "claude:", ":claude-sonnet-5", "openai:x,claude"])
def test_parse_model_pool_rejects_an_unpaired_entry(spec):
    # eca-pp#6: a model pinned without its harness is exactly this mistake,
    # one list per axis instead of one paired token — must fail to parse,
    # not silently fall back to a default harness for a stray model id.
    with pytest.raises(ValueError, match="harness:model"):
        parse_model_pool(spec)


def test_resolve_model_pool_is_none_when_unset():
    assert resolve_model_pool(environ={}) is None


def test_resolve_model_pool_reads_the_env_var():
    pool = resolve_model_pool(environ={"AGENT_MODEL_POOL": "openai:doubao-seed-2-1-turbo,claude:claude-sonnet-5"})
    assert [str(pool.current())] == ["openai:doubao-seed-2-1-turbo"]


def test_rotate_model_pool_wraps_around():
    spec = "openai:m1,claude:m2,deepseek:m3"
    assert rotate_model_pool(spec, 0) == "openai:m1,claude:m2,deepseek:m3"
    assert rotate_model_pool(spec, 1) == "claude:m2,deepseek:m3,openai:m1"
    assert rotate_model_pool(spec, 2) == "deepseek:m3,openai:m1,claude:m2"
    assert rotate_model_pool(spec, 3) == "openai:m1,claude:m2,deepseek:m3"  # mod length
    assert rotate_model_pool(spec, -1) == "deepseek:m3,openai:m1,claude:m2"  # python's %, wraps like a worker index never does


def test_rotate_model_pool_rejects_the_same_malformed_specs_as_parse():
    with pytest.raises(ValueError, match="harness:model"):
        rotate_model_pool("openai", 0)


def test_model_pool_needs_at_least_one_candidate():
    with pytest.raises(ValueError):
        ModelPool([])


def test_model_pool_advance_past_the_end_raises():
    pool = ModelPool([AgentConfig("openai", "m1")])
    with pytest.raises(RuntimeError, match="exhausted"):
        pool.advance("no more candidates")


def test_model_pool_records_the_failure_trail():
    pool = ModelPool([AgentConfig("openai", "m1"), AgentConfig("claude", "m2")])
    pool.advance("auth error")
    assert pool.failures() == [(AgentConfig("openai", "m1"), "auth error")]


# --------------------------------------------------------------------------
# exception classification: type/structured-field, not message regex
# --------------------------------------------------------------------------


class _FakeModelBehaviorError(Exception):
    """Stands in for agents.exceptions.ModelBehaviorError without importing
    the openai-agents SDK — this test file is provider-free by design."""


_FakeModelBehaviorError.__module__ = "agents.exceptions"
_FakeModelBehaviorError.__qualname__ = "ModelBehaviorError"


class _FakeBadRequest(Exception):
    def __init__(self, message, code):
        super().__init__(message)
        self.code = code


class _FakeAuthenticationError(Exception):
    pass


_FakeAuthenticationError.__module__ = "openai"
_FakeAuthenticationError.__qualname__ = "AuthenticationError"


def test_is_malformed_submission_matches_by_type_and_structured_code():
    assert H._is_malformed_submission(_FakeModelBehaviorError("Tool , not found"))
    assert H._is_malformed_submission(_FakeBadRequest("missing input.arguments", code="MissingParameter"))
    assert not H._is_malformed_submission(RuntimeError("Tool , not found"))  # right text, wrong type/no code
    assert not H._is_malformed_submission(_FakeBadRequest("x", code="SomethingElse"))


def test_is_startup_failure_matches_openais_401_403_404_by_type():
    assert H._is_startup_failure(_FakeAuthenticationError("bad key"))
    assert not H._is_startup_failure(RuntimeError("bad key"))  # message alone is not enough


# --------------------------------------------------------------------------
# retry_transient: malformed submissions and startup failures, with and without a pool
# --------------------------------------------------------------------------


def test_malformed_submission_gets_one_same_backend_retry(instant_sleep):
    attempt, calls = _failing([_FakeModelBehaviorError("Tool , not found")], then="ok")
    assert asyncio.run(retry_transient(attempt, "t")) == "ok"
    assert calls["n"] == 2
    assert instant_sleep == []  # a fresh turn, not a backoff sleep


def test_malformed_submission_without_a_pool_raises_after_the_attempt_budget(instant_sleep):
    attempt, calls = _failing([_FakeModelBehaviorError("x")] * 5)
    with pytest.raises(_FakeModelBehaviorError):
        asyncio.run(retry_transient(attempt, "t"))
    assert calls["n"] == H.MAX_MALFORMED_ATTEMPTS


def test_malformed_submission_falls_back_to_the_next_pool_candidate(instant_sleep, caplog):
    pool = ModelPool([AgentConfig("openai", "flaky"), AgentConfig("claude", "steady")])
    attempt, calls = _failing([_FakeModelBehaviorError("x")] * H.MAX_MALFORMED_ATTEMPTS, then="ok")
    with caplog.at_level(logging.WARNING):
        assert asyncio.run(retry_transient(attempt, "t", pool=pool)) == "ok"
    assert calls["n"] == H.MAX_MALFORMED_ATTEMPTS + 1
    assert str(pool.current()) == "claude:steady"
    assert any("falling back from openai:flaky to claude:steady" in r.message for r in caplog.records)


def test_persistent_transient_failure_falls_back_to_the_next_pool_candidate(instant_sleep, caplog):
    # A provider that is still timing out after the whole backoff schedule
    # is down, not hiccuping: with a pool the run moves on instead of dying.
    pool = ModelPool([AgentConfig("openai", "down"), AgentConfig("claude", "steady")])
    attempt, calls = _failing([RuntimeError("Request timed out.")] * H.MAX_TRANSIENT_ATTEMPTS, then="ok")
    with caplog.at_level(logging.WARNING):
        assert asyncio.run(retry_transient(attempt, "t", pool=pool)) == "ok"
    assert calls["n"] == H.MAX_TRANSIENT_ATTEMPTS + 1
    assert str(pool.current()) == "claude:steady"
    assert any("falling back from openai:down to claude:steady" in r.message for r in caplog.records)


def test_persistent_transient_failure_with_exhausted_pool_carries_the_trail(instant_sleep):
    pool = ModelPool([AgentConfig("openai", "down")])
    attempt, calls = _failing([RuntimeError("Request timed out.")] * 10)
    with pytest.raises(RuntimeError, match="model pool exhausted"):
        asyncio.run(retry_transient(attempt, "t", pool=pool))
    assert calls["n"] == H.MAX_TRANSIENT_ATTEMPTS


def test_startup_failure_falls_back_immediately_with_no_same_backend_retry(instant_sleep):
    pool = ModelPool([AgentConfig("openai", "wrong-key"), AgentConfig("claude", "steady")])
    attempt, calls = _failing([_FakeAuthenticationError("bad key")], then="ok")
    assert asyncio.run(retry_transient(attempt, "t", pool=pool)) == "ok"
    assert calls["n"] == 2  # no retry attempt spent on the broken backend


def test_startup_failure_without_a_pool_raises_immediately():
    attempt, calls = _failing([_FakeAuthenticationError("bad key")] * 3)
    with pytest.raises(_FakeAuthenticationError):
        asyncio.run(retry_transient(attempt, "t"))
    assert calls["n"] == 1


def test_incomplete_run_falls_back_after_bounded_retries_with_a_pool(instant_sleep):
    pool = ModelPool([AgentConfig("openai", "silent"), AgentConfig("claude", "steady")])
    attempt, calls = _failing([AgentIncompleteError("no submission")] * H.MAX_INCOMPLETE_ATTEMPTS, then="ok")
    assert asyncio.run(retry_transient(attempt, "t", pool=pool)) == "ok"
    assert calls["n"] == H.MAX_INCOMPLETE_ATTEMPTS + 1
    assert str(pool.current()) == "claude:steady"


def test_pool_exhausted_raises_with_every_candidates_reason(instant_sleep):
    pool = ModelPool([AgentConfig("openai", "wrong-key"), AgentConfig("claude", "also-wrong")])
    attempt, calls = _failing([_FakeAuthenticationError("key A bad"), _FakeAuthenticationError("key B bad")])
    with pytest.raises(_FakeAuthenticationError, match="key A bad.*key B bad"):
        asyncio.run(retry_transient(attempt, "t", pool=pool))
    assert calls["n"] == 2


def test_a_single_candidate_pool_behaves_like_no_pool_on_exhaustion(instant_sleep):
    pool = ModelPool([AgentConfig("openai", "only-option")])
    attempt, calls = _failing([_FakeAuthenticationError("bad key")])
    with pytest.raises(_FakeAuthenticationError, match="bad key"):
        asyncio.run(retry_transient(attempt, "t", pool=pool))
    assert calls["n"] == 1


def test_usage_limit_is_never_a_fallback_trigger_even_with_a_pool(instant_sleep, monkeypatch):
    # issue #1: a busy provider is not a broken one; falling back here would
    # quietly move the run onto a different (possibly pricier) backend.
    monkeypatch.setenv("AGENT_LIMIT_WAIT_MIN", "30")
    monkeypatch.setenv("AGENT_LIMIT_WAIT_MAX_H", "1")
    fake_now = {"t": 0.0}

    def clock():
        fake_now["t"] += 1800
        return fake_now["t"]

    monkeypatch.setattr(H.time, "time", clock)
    pool = ModelPool([AgentConfig("openai", "busy"), AgentConfig("claude", "idle")])
    attempt, calls = _failing([RuntimeError("429 Too Many Requests")] * 10)
    with pytest.raises(AgentLimitExhausted):
        asyncio.run(retry_transient(attempt, "t", pool=pool))
    assert str(pool.current()) == "openai:busy"  # never advanced


# --------------------------------------------------------------------------
# run_agent(): the pool actually changes which adapter/model gets called
# --------------------------------------------------------------------------


def test_run_agent_falls_back_across_a_real_adapter_switch(monkeypatch, tmp_path):
    calls = []

    async def openai_backend(**kwargs):
        calls.append(("openai", kwargs["model"]))
        raise _FakeAuthenticationError("bad Ark key")

    async def claude_backend(**kwargs):
        calls.append(("claude", kwargs["model"]))
        return AgentRunResult({"ok": True}, None, None)

    monkeypatch.setattr("harness_bridge._harness_openai.run_agent", openai_backend)
    monkeypatch.setattr("harness_bridge._harness_claude.run_agent", claude_backend)
    pool = ModelPool([AgentConfig("openai", "doubao-seed-2-1-turbo"), AgentConfig("claude", "claude-sonnet-5")])
    result = asyncio.run(harness_bridge.run_agent(
        tools=[SUBMIT], submit_tool="submit", prompt="p", cwd=str(tmp_path), pool=pool,
    ))
    assert calls == [("openai", "doubao-seed-2-1-turbo"), ("claude", "claude-sonnet-5")]
    assert result.submitted == {"ok": True}
    assert result.effective_config == AgentConfig("claude", "claude-sonnet-5")


def test_run_agent_records_effective_config_with_no_pool(monkeypatch, tmp_path):
    async def backend(**_kwargs):
        return AgentRunResult({"ok": True}, None, None)

    monkeypatch.setenv("HARNESS", "openai")
    monkeypatch.setenv("MODEL", "doubao-seed-2-1-pro-260628")
    monkeypatch.setattr("harness_bridge._harness_openai.run_agent", backend)
    result = asyncio.run(harness_bridge.run_agent(
        tools=[SUBMIT], submit_tool="submit", prompt="p", cwd=str(tmp_path),
    ))
    assert result.effective_config == AgentConfig("openai", "doubao-seed-2-1-pro-260628")


def test_run_agent_logs_the_resolved_backend_for_any_caller_scraping_stdout(monkeypatch, tmp_path, caplog):
    # osp per-sample and zmip per-lineage workers are subprocesses; the parent orchestrator
    # only ever sees what lands on stdout, never the AgentRunResult object itself.
    async def backend(**_kwargs):
        return AgentRunResult({"ok": True}, None, None)

    monkeypatch.setenv("HARNESS", "claude")
    monkeypatch.setenv("MODEL", "claude-sonnet-5")
    monkeypatch.setattr("harness_bridge._harness_claude.run_agent", backend)
    with caplog.at_level("INFO"):
        asyncio.run(harness_bridge.run_agent(
            tools=[SUBMIT], submit_tool="submit", prompt="p", cwd=str(tmp_path), label="qc",
        ))
    assert "[qc] resolved backend: harness=claude model=claude-sonnet-5" in caplog.text


def test_openrouter_is_a_pool_candidate_without_response_chaining(monkeypatch):
    from harness_bridge.harness import backend_capabilities, parse_model_pool

    pool = parse_model_pool("openai:doubao-seed-2-1-turbo-260628,openrouter:dots-studio/dots-3-note-preview:free")
    assert [c.harness for c in pool] == ["openai", "openrouter"]
    monkeypatch.delenv("OPENAI_AGENTS_API", raising=False)
    assert backend_capabilities(pool[0]).response_chaining is True
    monkeypatch.delenv("OPENROUTER_IMAGE_MODELS", raising=False)
    caps = backend_capabilities(pool[1])
    assert caps.response_chaining is False and caps.image_tool_outputs is False  # text-only unless listed
    monkeypatch.setenv("OPENROUTER_IMAGE_MODELS", "dots-studio/dots-3-note-preview:free")
    assert backend_capabilities(pool[1]).image_tool_outputs is True


def test_openrouter_rate_limit_advances_the_pool_instead_of_waiting(instant_sleep):
    from harness_bridge.harness import ModelPool, parse_model_pool, retry_transient

    pool = ModelPool(parse_model_pool("openrouter:a/one:free,openrouter:b/two:free"))
    calls = []

    async def run():
        calls.append(str(pool.current()))
        if pool.current().model == "a/one:free":
            raise RuntimeError("Error code: 429 - a/one:free is temporarily rate-limited upstream")
        return "ok"

    assert asyncio.run(retry_transient(run, "t", pool=pool)) == "ok"
    assert calls[-1] == "openrouter:b/two:free"
    assert len(calls) <= 6  # bounded same-candidate retries, then the fallback


def test_doubao_rate_limit_still_waits_even_with_openrouter_behind_it(instant_sleep, monkeypatch):
    from harness_bridge.harness import ModelPool, parse_model_pool, retry_transient

    monkeypatch.setenv("AGENT_LIMIT_WAIT_MAX_H", "0.001")
    pool = ModelPool(parse_model_pool("openai:doubao-x,openrouter:b/two:free"))
    seen = []

    async def run():
        seen.append(str(pool.current()))
        raise RuntimeError("Error code: 429 - rate limit exceeded")

    with pytest.raises(Exception, match="usage limit still in force"):
        asyncio.run(retry_transient(run, "t", pool=pool))
    assert set(seen) == {"openai:doubao-x"}  # never moved off the primary

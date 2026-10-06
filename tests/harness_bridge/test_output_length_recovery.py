import asyncio
from types import SimpleNamespace

import pytest
from agents.exceptions import ModelBehaviorError
from agents.models._response_terminal import response_terminal_failure_error

from harness_bridge import AgentIncompleteError, ToolSpec
from harness_bridge import _harness_openai as H


def failure(reason="length"):
    return response_terminal_failure_error(
        "response.incomplete",
        SimpleNamespace(
            status="incomplete", error=None, incomplete_details={"reason": reason}
        ),
    )


@pytest.mark.parametrize("reason", ["length", "max_output_tokens"])
def test_recognizes_sdk_terminal_length_reasons(reason):
    assert H._is_output_length_error(failure(reason))
    assert H._is_output_length_error(
        ModelBehaviorError(
            "Responses stream ended with terminal event `response.incomplete`. "
            f"status=incomplete; incomplete_details=IncompleteDetails(reason='{reason}')."
        )
    )


@pytest.mark.parametrize(
    "exc",
    [
        failure("content_filter"),
        failure("unknown"),
        failure(None),
        ModelBehaviorError("bad tool JSON with length max_output_tokens"),
        ModelBehaviorError(
            "Responses stream ended with terminal event `response.failed`. incomplete_details={'reason': 'length'}."
        ),
        RuntimeError(str(failure())),
        ModelBehaviorError(
            "Responses stream ended with terminal event `response.incomplete`. error=content_filter; incomplete_details={'reason': 'length'}."
        ),
    ],
)
def test_does_not_retry_other_failures(exc):
    assert not H._is_output_length_error(exc)


@pytest.fixture
def setup(monkeypatch, tmp_path):
    async def close():
        pass

    monkeypatch.setattr(H, "_client", lambda *a, **k: SimpleNamespace(close=close))
    monkeypatch.setattr(H, "_model", lambda *a: "dummy")
    monkeypatch.setenv("OPENAI_AGENTS_API", "responses")
    return {
        "submit_tool": "finish",
        "prompt": "original task",
        "system_prompt": None,
        "cwd": str(tmp_path),
        "model": "dummy",
        "effort": None,
        "max_turns": 20,
        "allowed_builtin": ("tasks",),
        "label": "recovery-test",
        "max_buffer_size": None,
    }


def result():
    return SimpleNamespace(
        new_items=[],
        final_output="done",
        last_response_id="resp-done",
        context_wrapper=SimpleNamespace(
            usage=SimpleNamespace(
                requests=1,
                input_tokens=3,
                output_tokens=2,
                output_tokens_details=SimpleNamespace(reasoning_tokens=0),
            )
        ),
    )


@pytest.mark.parametrize("reason", ["length", "max_output_tokens"])
def test_fresh_session_preserves_host_task_and_partial_submission(
    setup, monkeypatch, caplog, reason
):
    entries = {}
    calls = []

    async def partial(args):
        entries[args["key"]] = args["value"]
        return {"content": [{"type": "text", "text": "accepted"}]}

    async def finish(args):
        assert entries == {"0": "saved"}
        return {
            "content": [{"type": "text", "text": "done"}],
            "_submitted": dict(entries),
        }

    async def run(agent, prompt, **kwargs):
        await kwargs["hooks"].on_llm_start(None, agent, None, [])
        tools = {t.name: t for t in agent.tools}
        calls.append((agent, prompt, kwargs))
        if len(calls) == 1:
            await tools["TaskCreate"].on_invoke_tool(
                None, '{"subject":"done cluster","description":"saved"}'
            )
            await tools["TaskUpdate"].on_invoke_tool(
                None, '{"taskId":"1","status":"completed"}'
            )
            await tools["partial"].on_invoke_tool(None, '{"key":"0","value":"saved"}')
            for _ in range(4):
                await kwargs["hooks"].on_llm_start(None, agent, None, [])
            exc = failure(reason)
            exc.run_data = SimpleNamespace(
                context_wrapper=SimpleNamespace(
                    usage=SimpleNamespace(
                        requests=4,
                        input_tokens=100,
                        output_tokens=20,
                        output_tokens_details=SimpleNamespace(reasoning_tokens=5),
                    )
                )
            )
            raise exc
        assert agent is calls[0][0]
        assert (
            "fresh session" in prompt
            and "truncated response is not a valid submission" in prompt
        )
        assert "previous_response_id" not in kwargs
        assert kwargs["max_turns"] == 15
        tasks = await tools["TaskList"].on_invoke_tool(None, "{}")
        assert "completed" in tasks[0].text
        await tools["finish"].on_invoke_tool(None, "{}")
        return result()

    monkeypatch.setattr("agents.Runner.run", run)
    with caplog.at_level("INFO"):
        out = asyncio.run(
            H.run_agent(
                tools=[
                    ToolSpec("partial", "", {"key": str, "value": str}, partial),
                    ToolSpec("finish", "", {}, finish),
                ],
                **setup,
            )
        )
    assert out.submitted == entries and len(calls) == 2
    assert "5 model request(s), 103 input / 22 output tokens" in caplog.text
    assert "usage incomplete" in caplog.text


@pytest.mark.parametrize("budget", [0, 1, 2])
def test_repeated_length_failure_has_finite_reset_budget(setup, monkeypatch, budget):
    calls = []

    async def run(*a, **kw):
        await kw["hooks"].on_llm_start(None, a[0], None, [])
        calls.append(kw["max_turns"])
        raise failure()

    monkeypatch.setenv("OPENAI_AGENTS_MAX_OUTPUT_RESETS", str(budget))
    monkeypatch.setattr("agents.Runner.run", run)
    with pytest.raises(AgentIncompleteError, match="output-length recovery budget"):
        asyncio.run(H.run_agent(tools=[], **setup))
    assert len(calls) == budget + 1
    assert calls == list(range(20, 20 - budget - 1, -1))


def test_hook_enforces_total_budget_without_exception_run_data(setup, monkeypatch):
    calls = []
    attempted_requests = []
    setup["max_turns"] = 4
    BadRequestError = type("BadRequestError", (Exception,), {})

    async def run(agent, prompt, **kwargs):
        calls.append(kwargs)
        for _ in range(3):
            await kwargs["hooks"].on_llm_start(None, agent, None, [])
            attempted_requests.append(True)
        raise BadRequestError("maximum context length exceeded")

    monkeypatch.setattr("agents.Runner.run", run)
    with pytest.raises(AgentIncompleteError, match="exhausted max_turns"):
        asyncio.run(H.run_agent(tools=[], **setup))
    assert len(calls) == 2 and len(attempted_requests) == 4
    assert calls[1]["max_turns"] == 1


@pytest.mark.parametrize(
    "api,exc",
    [
        ("responses", failure("content_filter")),
        ("responses", ModelBehaviorError("invalid tool JSON")),
        ("chat_completions", failure()),
    ],
)
def test_unrecognized_or_wrong_api_propagates_without_retry(
    setup, monkeypatch, api, exc
):
    calls = []

    async def run(*a, **kw):
        await kw["hooks"].on_llm_start(None, a[0], None, [])
        calls.append(kw)
        raise exc

    monkeypatch.setenv("OPENAI_AGENTS_API", api)
    monkeypatch.setattr("agents.Runner.run", run)
    with pytest.raises(type(exc)) as caught:
        asyncio.run(H.run_agent(tools=[], **setup))
    assert caught.value is exc and len(calls) == 1


def test_length_reset_discards_previous_response_chain(setup, monkeypatch):
    calls = []

    async def finish(_args):
        return {
            "content": [{"type": "text", "text": "done"}],
            "_submitted": {"ok": True},
        }

    async def run(agent, prompt, **kwargs):
        await kwargs["hooks"].on_llm_start(None, agent, None, [])
        calls.append((prompt, kwargs))
        if len(calls) == 1:
            return result()
        if len(calls) == 2:
            assert kwargs["previous_response_id"] == "resp-done"
            raise failure()
        assert "previous_response_id" not in kwargs
        assert isinstance(prompt, str) and "output-length limit" in prompt
        tool = next(t for t in agent.tools if t.name == "finish")
        await tool.on_invoke_tool(None, "{}")
        return result()

    monkeypatch.setattr("agents.Runner.run", run)
    out = asyncio.run(H.run_agent(tools=[ToolSpec("finish", "", {}, finish)], **setup))
    assert out.submitted == {"ok": True} and len(calls) == 3


@pytest.mark.parametrize("turn_budget", [2, 3])
def test_real_sdk_runner_preserves_partial_state_and_enforces_hook_budget(
    setup, monkeypatch, turn_budget
):
    from agents.items import ModelResponse
    from agents.models.interface import Model
    from agents.usage import Usage
    from openai.types.responses import ResponseFunctionToolCall

    entries = {}

    class ScriptedModel(Model):
        calls = 0

        async def get_response(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 2:
                raise failure()
            name = "partial" if self.calls == 1 else "finish"
            return ModelResponse(
                output=[
                    ResponseFunctionToolCall(
                        id=f"fc{self.calls}",
                        call_id=f"call{self.calls}",
                        type="function_call",
                        name=name,
                        arguments="{}",
                    )
                ],
                usage=Usage(requests=1, input_tokens=2, output_tokens=1),
                response_id=f"resp{self.calls}",
            )

        async def stream_response(self, *args, **kwargs):
            raise AssertionError("non-streaming Runner expected")
            yield

    model = ScriptedModel()

    async def partial(_args):
        entries["accepted"] = True
        return {"content": [{"type": "text", "text": "saved"}]}

    async def finish(_args):
        assert entries == {"accepted": True}
        return {
            "content": [{"type": "text", "text": "done"}],
            "_submitted": entries.copy(),
        }

    monkeypatch.setattr(H, "_model", lambda *a: model)
    setup["max_turns"] = turn_budget
    coroutine = H.run_agent(
        tools=[
            ToolSpec("partial", "", {}, partial),
            ToolSpec("finish", "", {}, finish),
        ],
        **setup,
    )
    if turn_budget == 2:
        with pytest.raises(AgentIncompleteError, match="max_turns"):
            asyncio.run(coroutine)
    else:
        assert asyncio.run(coroutine).submitted == {"accepted": True}
    assert model.calls == turn_budget
    assert entries == {"accepted": True}

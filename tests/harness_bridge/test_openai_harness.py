"""Contract tests for the direct OpenAI Agents SDK backend."""

from __future__ import annotations

import asyncio
import base64
from types import SimpleNamespace

import pytest

from harness_bridge import _harness_openai as H
from harness_bridge import AgentIncompleteError, ToolSpec


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class FakeClient:
    closed = False

    async def close(self):
        self.closed = True


@pytest.fixture
def offline_model(monkeypatch):
    """Runner.run is replaced by each test, but Agent still validates that the
    model is a model id or a Model instance during construction, and the
    client must exist so run_agent can close it."""
    client = FakeClient()
    monkeypatch.setattr(H, "_client", lambda *a, **k: client)
    monkeypatch.setattr(H, "_model", lambda *_args: "dummy-model")
    return client


def test_params_schema_is_strict_and_maps_current_types():
    async def unused(_args):
        raise AssertionError

    spec = ToolSpec(
        "probe",
        "probe",
        {"name": str, "count": int, "score": float, "genes": list},
        unused,
    )
    assert H._params_schema(spec) == {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "count": {"type": "integer"},
            "score": {"type": "number"},
            "genes": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["name", "count", "score", "genes"],
        "additionalProperties": False,
    }


def test_tool_converts_mcp_content_and_captures_valid_submit():
    async def submit(args):
        return {
            "content": [
                {"type": "text", "text": "accepted"},
                {
                    "type": "image",
                    "data": base64.b64encode(PNG_1X1).decode("ascii"),
                    "mimeType": "image/png",
                },
            ],
            "_submitted": {"answer": args["answer"]},
        }

    holder = {}
    tool = H._tool(
        ToolSpec("submit", "submit", {"answer": str}, submit),
        holder,
        True,
        "test",
        "responses",
    )
    outputs = asyncio.run(tool.on_invoke_tool(None, '{"answer":"ok"}'))
    assert holder == {"value": {"answer": "ok"}}
    assert [output.type for output in outputs] == ["text", "image"]
    assert outputs[1].image_url.startswith("data:image/png;base64,")


def test_invalid_submit_is_model_visible_and_not_captured():
    async def submit(_args):
        return {
            "content": [{"type": "text", "text": "fix and resubmit"}],
            "is_error": True,
        }

    holder = {}
    tool = H._tool(
        ToolSpec("submit", "submit", {}, submit), holder, True, "test", "responses"
    )
    output = asyncio.run(tool.on_invoke_tool(None, "{}"))
    assert holder == {}
    assert output[0].text == "ERROR: fix and resubmit"


def test_model_input_exception_is_visible_instead_of_aborting_run():
    async def submit(_args):
        raise TypeError("decoded cluster entry must be an object")

    holder = {}
    tool = H._tool(
        ToolSpec("submit", "submit", {}, submit), holder, True, "test", "responses"
    )
    output = asyncio.run(tool.on_invoke_tool(None, "{}"))
    assert holder == {}
    assert output.type == "text"
    assert output.text.startswith("ERROR: submit rejected the input (TypeError:")
    assert "Fix it and call the tool again" in output.text


@pytest.mark.parametrize("raw", ['{"answer": ', '["not", "an", "object"]', "null"])
def test_malformed_tool_arguments_go_back_to_the_model(raw):
    async def submit(_args):
        raise AssertionError("handler must not run on malformed arguments")

    holder = {}
    tool = H._tool(
        ToolSpec("submit", "submit", {"answer": str}, submit),
        holder,
        True,
        "test",
        "responses",
    )
    output = asyncio.run(tool.on_invoke_tool(None, raw))
    assert holder == {}
    assert output.text.startswith("ERROR: submit rejected the input (")


def test_unexpected_handler_failure_is_reported_not_fatal():
    async def flaky(_args):
        raise OSError("disk went away")

    tool = H._tool(
        ToolSpec("flaky", "flaky", {}, flaky), {}, False, "test", "responses"
    )
    output = asyncio.run(tool.on_invoke_tool(None, "{}"))
    assert "OSError: disk went away" in output.text


def test_no_submit_nudges_with_previous_response_then_accepts(
    monkeypatch, tmp_path, offline_model
):
    calls = []

    class FakeResult:
        def __init__(self, final_output):
            self.final_output = final_output
            self.last_response_id = "resp-1"
            self.new_items = []
            self.context_wrapper = SimpleNamespace(
                usage=SimpleNamespace(
                    requests=1,
                    input_tokens=10,
                    output_tokens=5,
                    output_tokens_details=SimpleNamespace(reasoning_tokens=2),
                )
            )

        def to_input_list(self):
            return [{"role": "user", "content": "original history"}]

    async def fake_run(agent, run_input, **_kwargs):
        await _kwargs["hooks"].on_llm_start(None, agent, None, [])
        calls.append((run_input, _kwargs))
        if len(calls) == 2:
            submit = next(tool for tool in agent.tools if tool.name == "submit_answer")
            await submit.on_invoke_tool(None, '{"answer":"done"}')
        return FakeResult("paused" if len(calls) == 1 else "done")

    async def submit(args):
        return {"content": [{"type": "text", "text": "accepted"}], "_submitted": args}

    monkeypatch.setattr("agents.Runner.run", fake_run)
    monkeypatch.setenv("OPENAI_AGENTS_MAX_NUDGES", "2")
    result = asyncio.run(
        H.run_agent(
            tools=[ToolSpec("submit_answer", "submit", {"answer": str}, submit)],
            submit_tool="submit_answer",
            prompt="do it",
            system_prompt=None,
            cwd=str(tmp_path),
            model="doubao-test",
            effort=None,
            max_turns=5,
            allowed_builtin=(),
            label="nudge-test",
            max_buffer_size=None,
            wall_seconds=None,
        )
    )
    assert result.submitted == {"answer": "done"}
    assert len(calls) == 2
    assert "previous turn ended" in calls[1][0][0]["content"]
    assert calls[0][1]["auto_previous_response_id"] is True
    assert calls[1][1]["previous_response_id"] == "resp-1"
    assert offline_model.closed is True


def test_server_state_can_be_disabled_for_local_history(
    monkeypatch, tmp_path, offline_model
):
    calls = []

    class FakeResult:
        final_output = "paused"
        last_response_id = "resp-1"
        new_items = []
        context_wrapper = SimpleNamespace(
            usage=SimpleNamespace(
                requests=1,
                input_tokens=10,
                output_tokens=5,
                output_tokens_details=SimpleNamespace(reasoning_tokens=0),
            )
        )

        def to_input_list(self):
            return [{"role": "user", "content": "original history"}]

    async def fake_run(agent, run_input, **kwargs):
        await kwargs["hooks"].on_llm_start(None, agent, None, [])
        calls.append((run_input, kwargs))
        if len(calls) == 2:
            submit = next(tool for tool in agent.tools if tool.name == "submit_answer")
            await submit.on_invoke_tool(None, '{"answer":"done"}')
        return FakeResult()

    async def submit(args):
        return {"content": [{"type": "text", "text": "accepted"}], "_submitted": args}

    monkeypatch.setattr("agents.Runner.run", fake_run)
    monkeypatch.setenv("OPENAI_AGENTS_SERVER_STATE", "0")
    result = asyncio.run(
        H.run_agent(
            tools=[ToolSpec("submit_answer", "submit", {"answer": str}, submit)],
            submit_tool="submit_answer",
            prompt="do it",
            system_prompt=None,
            cwd=str(tmp_path),
            model="doubao-test",
            effort=None,
            max_turns=5,
            allowed_builtin=(),
            label="local-history-test",
            max_buffer_size=None,
            wall_seconds=None,
        )
    )

    assert result.submitted == {"answer": "done"}
    assert calls[0][1].get("auto_previous_response_id") is None
    assert calls[1][0][0]["content"] == "original history"
    assert "previous turn ended" in calls[1][0][-1]["content"]


def test_context_limit_starts_fresh_session_but_keeps_host_state(
    monkeypatch, tmp_path, offline_model
):
    calls = []
    BadRequestError = type("BadRequestError", (Exception,), {})

    class FakeResult:
        final_output = "done"
        last_response_id = "resp-after-reset"
        new_items = []
        context_wrapper = SimpleNamespace(
            usage=SimpleNamespace(
                requests=1,
                input_tokens=10,
                output_tokens=5,
                output_tokens_details=SimpleNamespace(reasoning_tokens=0),
            )
        )

    async def fake_run(agent, run_input, **kwargs):
        await kwargs["hooks"].on_llm_start(None, agent, None, [])
        calls.append((run_input, kwargs))
        if len(calls) == 1:
            raise BadRequestError(
                "Total tokens of image and text exceed max message tokens"
            )
        submit = next(tool for tool in agent.tools if tool.name == "submit_answer")
        await submit.on_invoke_tool(None, '{"answer":"recovered"}')
        return FakeResult()

    async def submit(args):
        return {"content": [{"type": "text", "text": "accepted"}], "_submitted": args}

    monkeypatch.setattr("agents.Runner.run", fake_run)
    result = asyncio.run(
        H.run_agent(
            tools=[ToolSpec("submit_answer", "submit", {"answer": str}, submit)],
            submit_tool="submit_answer",
            prompt="do it",
            system_prompt=None,
            cwd=str(tmp_path),
            model="doubao-test",
            effort=None,
            max_turns=5,
            allowed_builtin=(),
            label="context-test",
            max_buffer_size=None,
            wall_seconds=None,
        )
    )

    assert result.submitted == {"answer": "recovered"}
    assert len(calls) == 2
    assert calls[1][1].get("previous_response_id") is None
    assert "fresh session" in calls[1][0]


def test_item_count_limit_starts_fresh_session_too(
    monkeypatch, tmp_path, offline_model
):
    """Ark's Responses API caps total item count, not just tokens — a
    different message shape than the token-based context-limit case above,
    same recovery path."""
    calls = []
    BadRequestError = type("BadRequestError", (Exception,), {})

    class FakeResult:
        final_output = "done"
        last_response_id = "resp-after-reset"
        new_items = []
        context_wrapper = SimpleNamespace(
            usage=SimpleNamespace(
                requests=1,
                input_tokens=10,
                output_tokens=5,
                output_tokens_details=SimpleNamespace(reasoning_tokens=0),
            )
        )

    async def fake_run(agent, run_input, **kwargs):
        await kwargs["hooks"].on_llm_start(None, agent, None, [])
        calls.append((run_input, kwargs))
        if len(calls) == 1:
            raise BadRequestError(
                "Invalid input: Maximum of 1000 items allowed in input."
            )
        submit = next(tool for tool in agent.tools if tool.name == "submit_answer")
        await submit.on_invoke_tool(None, '{"answer":"recovered"}')
        return FakeResult()

    async def submit(args):
        return {"content": [{"type": "text", "text": "accepted"}], "_submitted": args}

    monkeypatch.setattr("agents.Runner.run", fake_run)
    result = asyncio.run(
        H.run_agent(
            tools=[ToolSpec("submit_answer", "submit", {"answer": str}, submit)],
            submit_tool="submit_answer",
            prompt="do it",
            system_prompt=None,
            cwd=str(tmp_path),
            model="doubao-test",
            effort=None,
            max_turns=5,
            allowed_builtin=(),
            label="item-limit-test",
            max_buffer_size=None,
            wall_seconds=None,
        )
    )

    assert result.submitted == {"answer": "recovered"}
    assert len(calls) == 2
    assert calls[1][1].get("previous_response_id") is None
    assert "fresh session" in calls[1][0]


def test_refusing_model_raises_incomplete_and_still_closes_client(
    monkeypatch, tmp_path, offline_model
):
    class FakeResult:
        final_output = "I would rather not."
        last_response_id = None
        new_items = []
        context_wrapper = SimpleNamespace(
            usage=SimpleNamespace(
                requests=1,
                input_tokens=1,
                output_tokens=1,
                output_tokens_details=SimpleNamespace(reasoning_tokens=0),
            )
        )

        def to_input_list(self):
            return []

    async def fake_run(agent, run_input, **kwargs):
        await kwargs["hooks"].on_llm_start(None, agent, None, [])
        return FakeResult()

    async def submit(args):
        return {"content": [{"type": "text", "text": "accepted"}], "_submitted": args}

    monkeypatch.setattr("agents.Runner.run", fake_run)
    monkeypatch.setenv("OPENAI_AGENTS_MAX_NUDGES", "1")
    with pytest.raises(AgentIncompleteError, match="1 nudge"):
        asyncio.run(
            H.run_agent(
                tools=[ToolSpec("submit_answer", "submit", {"answer": str}, submit)],
                submit_tool="submit_answer",
                prompt="do it",
                system_prompt=None,
                cwd=str(tmp_path),
                model="doubao-test",
                effort=None,
                max_turns=5,
                allowed_builtin=(),
                label="refusal-test",
                max_buffer_size=None,
                wall_seconds=None,
            )
        )
    assert offline_model.closed is True


def test_client_uses_a_bounded_request_timeout_by_default(monkeypatch):
    captured = {}

    class FakeAsyncOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr("openai.AsyncOpenAI", FakeAsyncOpenAI)
    monkeypatch.setenv("ARK_API_KEY", "k")
    monkeypatch.delenv("OPENAI_AGENTS_REQUEST_TIMEOUT_S", raising=False)
    H._client()
    assert captured["timeout"] == 900.0


def test_client_request_timeout_is_env_overridable(monkeypatch):
    captured = {}

    class FakeAsyncOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr("openai.AsyncOpenAI", FakeAsyncOpenAI)
    monkeypatch.setenv("ARK_API_KEY", "k")
    monkeypatch.setenv("OPENAI_AGENTS_REQUEST_TIMEOUT_S", "45")
    H._client()
    assert captured["timeout"] == 45.0


def test_openrouter_client_uses_its_own_key_base_url_and_attribution(monkeypatch):
    captured = {}

    class FakeAsyncOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr("openai.AsyncOpenAI", FakeAsyncOpenAI)
    monkeypatch.delenv("ARK_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-k")
    H._client("openrouter")
    assert captured["api_key"] == "or-k"
    assert captured["base_url"] == "https://openrouter.ai/api/v1"
    assert captured["default_headers"]["X-Title"] == "agent-harness-bridge"


def test_openrouter_client_needs_its_key_not_arks(monkeypatch):
    monkeypatch.setenv("ARK_API_KEY", "k")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="HARNESS=openrouter needs OPENROUTER_API_KEY"):
        H._client("openrouter")


def test_openrouter_adapter_delegates_with_the_openrouter_provider(monkeypatch):
    import harness_bridge._harness_openrouter as R

    seen = {}

    async def fake_run(**kwargs):
        seen.update(kwargs)
        return "ok"

    monkeypatch.setattr(R, "_run_openai", fake_run)
    assert asyncio.run(R.run_agent(model="m", label="x")) == "ok"
    assert seen["provider"] == "openrouter" and seen["model"] == "m"


def test_openrouter_models_get_images_only_when_listed(monkeypatch):
    monkeypatch.delenv("OPENROUTER_IMAGE_MODELS", raising=False)
    assert H.model_accepts_images("ark", "doubao-x") is True
    assert H.model_accepts_images("openrouter", "nvidia/nemotron-3.5-lightning:free") is False
    monkeypatch.setenv("OPENROUTER_IMAGE_MODELS", "thinkingmachines/inkling-small:free, a/b")
    assert H.model_accepts_images("openrouter", "a/b") is True
    assert H.model_accepts_images("openrouter", "nvidia/nemotron-3.5-lightning:free") is False


def test_text_only_model_gets_a_text_error_instead_of_an_image(monkeypatch):
    from agents import ToolOutputText

    async def handler(args):
        return {"content": [{"type": "image", "mimeType": "image/png", "data": "AAAA"}]}

    spec = ToolSpec("plot", "a plot", {}, handler)
    tool = H._tool(spec, {}, False, "t", "responses", images_ok=False)
    out = asyncio.run(tool.on_invoke_tool(None, "{}"))
    assert isinstance(out[0], ToolOutputText) and "does not accept images" in out[0].text


def test_vllm_client_points_at_the_local_server(monkeypatch):
    captured = {}

    class FakeAsyncOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr("openai.AsyncOpenAI", FakeAsyncOpenAI)
    monkeypatch.setenv("VLLM_API_KEY", "local")
    monkeypatch.delenv("VLLM_BASE_URL", raising=False)
    H._client("vllm")
    assert captured["base_url"] == "http://127.0.0.1:8000/v1" and captured["api_key"] == "local"
    assert H.model_accepts_images("vllm", "local-coder") is True

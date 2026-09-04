"""Contract tests for the direct OpenAI Agents SDK backend."""

from __future__ import annotations

import asyncio
import base64
from types import SimpleNamespace

from ecarsi import _harness_openai as H
from ecarsi.harness import ToolSpec


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def test_params_schema_is_strict_and_maps_current_types():
    async def unused(_args):
        raise AssertionError

    spec = ToolSpec(
        "probe", "probe", {"name": str, "count": int, "score": float, "genes": list}, unused,
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
                {"type": "image", "data": base64.b64encode(PNG_1X1).decode("ascii"),
                 "mimeType": "image/png"},
            ],
            "_submitted": {"answer": args["answer"]},
        }

    holder = {}
    tool = H._tool(ToolSpec("submit", "submit", {"answer": str}, submit), holder, True, "test", "responses")
    outputs = asyncio.run(tool.on_invoke_tool(None, '{"answer":"ok"}'))
    assert holder == {"value": {"answer": "ok"}}
    assert [output.type for output in outputs] == ["text", "image"]
    assert outputs[1].image_url.startswith("data:image/png;base64,")


def test_invalid_submit_is_model_visible_and_not_captured():
    async def submit(_args):
        return {"content": [{"type": "text", "text": "fix and resubmit"}], "is_error": True}

    holder = {}
    tool = H._tool(ToolSpec("submit", "submit", {}, submit), holder, True, "test", "responses")
    output = asyncio.run(tool.on_invoke_tool(None, "{}"))
    assert holder == {}
    assert output[0].text == "ERROR: fix and resubmit"


def test_model_input_exception_is_visible_instead_of_aborting_run():
    async def submit(_args):
        raise TypeError("decoded cluster entry must be an object")

    holder = {}
    tool = H._tool(ToolSpec("submit", "submit", {}, submit), holder, True, "test", "responses")
    output = asyncio.run(tool.on_invoke_tool(None, "{}"))
    assert holder == {}
    assert output.type == "text"
    assert output.text.startswith("ERROR: submit rejected the input (TypeError:")
    assert "Fix it and call the tool again" in output.text


def test_no_submit_nudges_with_previous_response_then_accepts(monkeypatch, tmp_path):
    calls = []

    class FakeResult:
        def __init__(self, final_output):
            self.final_output = final_output
            self.last_response_id = "resp-1"
            self.new_items = []
            self.context_wrapper = SimpleNamespace(usage=SimpleNamespace(
                requests=1,
                input_tokens=10,
                output_tokens=5,
                output_tokens_details=SimpleNamespace(reasoning_tokens=2),
            ))

        def to_input_list(self):
            return [{"role": "user", "content": "original history"}]

    async def fake_run(agent, run_input, **_kwargs):
        calls.append((run_input, _kwargs))
        if len(calls) == 2:
            submit = next(tool for tool in agent.tools if tool.name == "submit_answer")
            await submit.on_invoke_tool(None, '{"answer":"done"}')
        return FakeResult("paused" if len(calls) == 1 else "done")

    async def submit(args):
        return {"content": [{"type": "text", "text": "accepted"}], "_submitted": args}

    # Runner.run is replaced below, but Agent still validates that the model is
    # either a model id or a Model instance during construction.
    monkeypatch.setattr(H, "_model", lambda *_args: "dummy-model")
    monkeypatch.setattr("agents.Runner.run", fake_run)
    monkeypatch.setenv("OPENAI_AGENTS_MAX_NUDGES", "2")
    result = asyncio.run(H.run_agent(
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
    ))
    assert result.submitted == {"answer": "done"}
    assert len(calls) == 2
    assert "previous turn ended" in calls[1][0][0]["content"]
    assert calls[0][1]["auto_previous_response_id"] is True
    assert calls[1][1]["previous_response_id"] == "resp-1"


def test_server_state_can_be_disabled_for_local_history(monkeypatch, tmp_path):
    calls = []

    class FakeResult:
        final_output = "paused"
        last_response_id = "resp-1"
        new_items = []
        context_wrapper = SimpleNamespace(usage=SimpleNamespace(
            requests=1,
            input_tokens=10,
            output_tokens=5,
            output_tokens_details=SimpleNamespace(reasoning_tokens=0),
        ))

        def to_input_list(self):
            return [{"role": "user", "content": "original history"}]

    async def fake_run(agent, run_input, **kwargs):
        calls.append((run_input, kwargs))
        if len(calls) == 2:
            submit = next(tool for tool in agent.tools if tool.name == "submit_answer")
            await submit.on_invoke_tool(None, '{"answer":"done"}')
        return FakeResult()

    async def submit(args):
        return {"content": [{"type": "text", "text": "accepted"}], "_submitted": args}

    monkeypatch.setattr(H, "_model", lambda *_args: "dummy-model")
    monkeypatch.setattr("agents.Runner.run", fake_run)
    monkeypatch.setenv("OPENAI_AGENTS_SERVER_STATE", "0")
    result = asyncio.run(H.run_agent(
        tools=[ToolSpec("submit_answer", "submit", {"answer": str}, submit)],
        submit_tool="submit_answer", prompt="do it", system_prompt=None,
        cwd=str(tmp_path), model="doubao-test", effort=None, max_turns=5,
        allowed_builtin=(), label="local-history-test", max_buffer_size=None,
        wall_seconds=None,
    ))

    assert result.submitted == {"answer": "done"}
    assert calls[0][1].get("auto_previous_response_id") is None
    assert calls[1][0][0]["content"] == "original history"
    assert "previous turn ended" in calls[1][0][-1]["content"]


def test_context_limit_starts_fresh_session_but_keeps_host_state(monkeypatch, tmp_path):
    calls = []
    BadRequestError = type("BadRequestError", (Exception,), {})

    class FakeResult:
        final_output = "done"
        last_response_id = "resp-after-reset"
        new_items = []
        context_wrapper = SimpleNamespace(usage=SimpleNamespace(
            requests=1,
            input_tokens=10,
            output_tokens=5,
            output_tokens_details=SimpleNamespace(reasoning_tokens=0),
        ))

    async def fake_run(agent, run_input, **kwargs):
        calls.append((run_input, kwargs))
        if len(calls) == 1:
            raise BadRequestError("Total tokens of image and text exceed max message tokens")
        submit = next(tool for tool in agent.tools if tool.name == "submit_answer")
        await submit.on_invoke_tool(None, '{"answer":"recovered"}')
        return FakeResult()

    async def submit(args):
        return {"content": [{"type": "text", "text": "accepted"}], "_submitted": args}

    monkeypatch.setattr(H, "_model", lambda *_args: "dummy-model")
    monkeypatch.setattr("agents.Runner.run", fake_run)
    result = asyncio.run(H.run_agent(
        tools=[ToolSpec("submit_answer", "submit", {"answer": str}, submit)],
        submit_tool="submit_answer", prompt="do it", system_prompt=None,
        cwd=str(tmp_path), model="doubao-test", effort=None, max_turns=5,
        allowed_builtin=(), label="context-test", max_buffer_size=None,
        wall_seconds=None,
    ))

    assert result.submitted == {"answer": "recovered"}
    assert len(calls) == 2
    assert calls[1][1].get("previous_response_id") is None
    assert "fresh session" in calls[1][0]

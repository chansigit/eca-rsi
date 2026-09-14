import asyncio
import json

import pytest

from harness_bridge import AgentRunResult, ToolSpec, run_agent, telemetry


async def submit(_args):
    return {"ok": True}


def test_agent_telemetry_records_live_wait_success_failure_and_reported_usage(tmp_path, monkeypatch):
    root = tmp_path / "events"
    monkeypatch.setenv("AGENT_BRIDGE_TELEMETRY_DIR", str(root))
    monkeypatch.setenv("HARNESS", "openai")
    monkeypatch.setenv("MODEL", "test-model")
    tool = ToolSpec("submit", "submit", {}, submit)

    async def success(**_kwargs):
        telemetry.record("model_start")
        live = telemetry.snapshot()
        assert len(live["active"]) == 1
        assert live["active"][0]["phase"] == "waiting_for_model"
        telemetry.record("model_end")
        return AgentRunResult({"ok": True}, None, 1.25, tokens_in=123, tokens_out=45)

    monkeypatch.setattr("harness_bridge._harness_openai.run_agent", success)
    result = asyncio.run(run_agent(tools=[tool], submit_tool="submit", prompt="SECRET_PROMPT",
                                   cwd=str(tmp_path), label="test task"))
    assert result.tokens_in == 123

    async def failure(**_kwargs):
        raise ValueError("SECRET_REPLY")

    monkeypatch.setattr("harness_bridge._harness_openai.run_agent", failure)
    with pytest.raises(ValueError):
        asyncio.run(run_agent(tools=[tool], submit_tool="submit", prompt="SECRET_PROMPT",
                              cwd=str(tmp_path), label="other task"))
    state = telemetry.snapshot()
    assert (state["started"], state["succeeded"], state["failed"]) == (2, 1, 1)
    assert (state["tokens_in"], state["tokens_out"], state["usage_reported"]) == (123, 45, 1)
    assert (state["cost_usd"], state["cost_reported"]) == (1.25, 1)
    assert state["observed_model_requests"] == 1 and state["active"] == []
    assert state["recent"][0]["error_type"] == "ValueError"
    journal = next(root.glob("events-*.jsonl")).read_text()
    assert len(journal.splitlines()) == 9
    assert "SECRET_PROMPT" not in journal and "SECRET_REPLY" not in journal
    assert "SECRET_PROMPT" not in json.dumps(state) and "SECRET_REPLY" not in json.dumps(state)

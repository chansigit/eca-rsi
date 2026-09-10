"""Harness and model selection stay independent at public entry points."""

from __future__ import annotations

import asyncio

import pytest

from ecarsi import check_agent_config
from ecarsi import __main__ as cli
from ecarsi import harness
from ecarsi.harness import AgentRunResult, ToolSpec


def test_default_agent_config_is_openai_turbo(monkeypatch):
    monkeypatch.delenv("HARNESS", raising=False)
    monkeypatch.delenv("MODEL", raising=False)

    assert harness.backend_name() == "openai"
    assert harness.default_model() == "doubao-seed-2-1-turbo-260628"


def test_global_cli_agent_options_override_environment(monkeypatch):
    monkeypatch.setenv("HARNESS", "deepseek")
    monkeypatch.setenv("MODEL", "old-model")

    dispatched = {}

    def fake_run(argv):
        dispatched["argv"] = argv
        return 0

    monkeypatch.setattr(cli, "run", fake_run)
    assert cli.main([
        "run", "input", "root",
        "--harness", "openai",
        "--model", "doubao-seed-2-1-pro-260628",
    ]) == 0
    assert dispatched["argv"] == ["input", "root"]
    assert harness.backend_name() == "openai"
    assert harness.default_model() == "doubao-seed-2-1-pro-260628"


def test_run_agent_resolves_backend_default_when_model_is_omitted(monkeypatch, tmp_path):
    captured = {}

    async def fake_backend(**kwargs):
        captured.update(kwargs)
        return AgentRunResult(submitted={"ok": True}, transcript_text=None, cost_usd=None)

    async def submit(_args):
        raise AssertionError("backend is mocked")

    monkeypatch.setenv("HARNESS", "openai")
    monkeypatch.setenv("MODEL", "doubao-seed-2-1-pro-260628")
    monkeypatch.setattr("harness_bridge._harness_openai.run_agent", fake_backend)
    result = asyncio.run(harness.run_agent(
        tools=[ToolSpec("submit", "submit", {}, submit)],
        submit_tool="submit",
        prompt="probe",
        cwd=str(tmp_path),
        model=None,
    ))

    assert result.submitted == {"ok": True}
    assert captured["model"] == "doubao-seed-2-1-pro-260628"


def test_recorded_model_change_is_reported_not_blocked(monkeypatch):
    # 2026-09-10: switching backend mid-run is a legitimate operator move
    # (round 3 shows the current model's QC is too lax -> continue with a
    # stronger one); check_agent_config never raises, it reports.
    monkeypatch.setenv("HARNESS", "openai")
    monkeypatch.setenv("MODEL", "doubao-seed-2-1-pro-260628")
    recorded = {
        "harness": "openai",
        "model": "doubao-seed-2-1-turbo-260628",
    }

    changed = check_agent_config(recorded, "manifest.json")
    assert changed is not None
    assert "doubao-seed-2-1-turbo-260628" in changed and "doubao-seed-2-1-pro-260628" in changed

    monkeypatch.setenv("MODEL", "doubao-seed-2-1-turbo-260628")
    assert check_agent_config(recorded, "manifest.json") is None  # matches -> nothing to report


def test_agent_config_predating_the_recorded_fields_is_unverifiable_not_blocked():
    assert check_agent_config({}, "old-manifest.json") is None

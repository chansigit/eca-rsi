"""review.py: an agent-config-changed event in progress.log becomes a
review item, and check_agent_config never blocks a mixed-model resume."""
from __future__ import annotations

from ecarsi import check_agent_config, layout as L, review


def test_agent_config_change_is_reported_and_scanned_into_a_review_item(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS", "openai")
    monkeypatch.setenv("MODEL", "claude-sonnet-5-via-doubao")  # any string; only compared, not resolved
    unit = tmp_path / "unit"
    recorded = {"harness": "claude", "model": "claude-sonnet-5"}

    changed = check_agent_config(recorded, str(unit / "rounds" / "round03" / "manifest.json"))
    assert changed is not None  # never raises; reports the mismatch instead
    L.log_event(unit, f"round 3 agent config changed: {changed}")

    items = review._agent_config_items(unit)
    assert len(items) == 1
    assert items[0].kind == "agent_config_changed" and items[0].round == 3
    assert "claude-sonnet-5" in items[0].note and "claude-sonnet-5-via-doubao" in items[0].note


def test_matching_config_is_not_reported(monkeypatch):
    monkeypatch.setenv("HARNESS", "openai")
    monkeypatch.setenv("MODEL", "doubao-seed-2-1-turbo-260628")
    recorded = {"harness": "openai", "model": "doubao-seed-2-1-turbo-260628"}
    assert check_agent_config(recorded, "manifest.json") is None


def test_review_kind_agent_config_changed_is_registered():
    assert "agent_config_changed" in review.KIND_INDEX

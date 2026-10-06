import json
import os
import signal
from pathlib import Path

import pytest

from harness_bridge.control import (
    PauseRequested,
    pausable,
    pause_requested,
    pause_signals,
    safe_point,
)


def test_sigterm_requests_shared_pause_and_new_run_can_resume(monkeypatch):
    monkeypatch.delenv("ECA_RSI_PAUSE_FILE", raising=False)
    monkeypatch.delenv("ECA_RSI_CONTROL", raising=False)
    previous = signal.getsignal(signal.SIGTERM)
    with pause_signals():
        marker = Path(os.environ["ECA_RSI_PAUSE_FILE"])
        assert not pause_requested()
        with pause_signals():
            os.kill(os.getpid(), signal.SIGTERM)
            assert marker.exists() and pause_requested()
        with pytest.raises(PauseRequested) as exc:
            safe_point()
        assert exc.value.code == 3
    assert not marker.exists() and signal.getsignal(signal.SIGTERM) == previous
    with pause_signals():
        assert not pause_requested()


def test_control_boolean_stage_and_exit_code(tmp_path, monkeypatch):
    path = tmp_path / "loop_control.json"
    monkeypatch.setenv("ECA_RSI_CONTROL", str(path))
    for value in (False, "true", 1, None):
        path.write_text(json.dumps({"pause": value}))
        assert not pause_requested()
    path.write_text(json.dumps({"pause_after_stage": "crosssample"}))
    assert not pause_requested() and not pause_requested("zoomin")
    assert pause_requested("crosssample")
    path.write_text(json.dumps({"pause": True}))
    assert pausable(lambda: safe_point())() == 3
    assert json.loads(path.read_text())["pause"] is True

import os
import signal
import subprocess
import sys
import time

import pytest
from harness_bridge.control import PauseRequested, pause_signals

from ecarsi import persample


def test_sample_pool_drains_running_work_and_does_not_launch_waiting_samples(
    tmp_path, monkeypatch
):
    real_spawn, real_sleep = subprocess.Popen, time.sleep
    started = []
    finished = tmp_path / "finished"

    def spawn(cmd, **kwargs):
        process = real_spawn(cmd, **kwargs)
        started.append(process)
        os.kill(os.getpid(), signal.SIGTERM)
        return process

    monkeypatch.setattr(persample.subprocess, "Popen", spawn)
    monkeypatch.setattr(persample.time, "sleep", lambda _: real_sleep(0.01))
    monkeypatch.setattr(persample, "plan_concurrency", lambda _: (2, 10**12, 1))
    monkeypatch.setattr(persample, "is_done", lambda *a: finished.exists())
    command = [
        sys.executable,
        "-c",
        "import pathlib,sys,time; time.sleep(.15); pathlib.Path(sys.argv[1]).touch()",
        str(finished),
    ]
    entries = [
        dict(value=name, n_cells=2, outdir=str(tmp_path / name), command=command)
        for name in ("A", "B")
    ]
    with pause_signals(), pytest.raises(PauseRequested) as exc:
        persample.drive(entries, tmp_path, False)
    assert exc.value.code == 3 and len(started) == 1
    assert started[0].returncode == 0 and finished.exists()
    assert not (tmp_path / "failures.md").exists()

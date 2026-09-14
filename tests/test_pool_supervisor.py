"""Exercise process recovery and fail-closed inventory with no Slurm allocation."""
import json
import os
from pathlib import Path
import subprocess
import time
from types import SimpleNamespace

from ecarsi.pool import slurm


def test_worker_exit_restarts_and_fences_descendants(tmp_path, monkeypatch):
    wrapper = tmp_path / "python"
    # Simulate a clean Dask exit that leaves a child behind. A clean exit must
    # also restart; the new attempt must not overlap the old process group.
    wrapper.write_text("#!/usr/bin/env python3\n" + f"""
import json, pathlib, subprocess, time, os
root = pathlib.Path({str(tmp_path)!r})
marker = root / 'old-child'
if not marker.exists():
    child = subprocess.Popen(['sleep', '60'])
    marker.write_text(str(child.pid))
else:
    p = pathlib.Path('/proc') / marker.read_text() / 'stat'
    alive = p.exists() and p.read_text().rsplit(')', 1)[1].split()[0] != 'Z'
    (root / 'recovered').write_text(json.dumps({{'overlap': alive}}))
    time.sleep(60)
""")
    wrapper.chmod(0o755)
    profile = dict(host="test", job_id="1", cpu_ids=[0], gpu_ids=[], memory=1024,
                   cpus=1, end_time=time.time() + 20, observed_at=time.time())
    monkeypatch.setattr(slurm, "inventory", lambda *a: dict(profile, observed_at=time.time()))
    args = SimpleNamespace(directory=tmp_path, python=str(wrapper), scheduler="unused",
                           cpus=None, gpu=False, root=[])
    lock = (tmp_path / "resource.lock").open("a")
    try:
        assert slurm.supervise(args, profile, [lock], lambda: (tmp_path / "recovered").exists()) == 0
    finally:
        lock.close()
    assert json.loads((tmp_path / "recovered").read_text()) == {"overlap": False}
    health = json.loads((tmp_path / "launcher.json").read_text())
    assert health["state"] == "stopped" and health["restarts"] == 1
    assert health["last_exit_code"] == 0 and health["child_pid"] is None


def test_no_launch_without_fresh_grant_or_after_expiry(tmp_path, monkeypatch):
    now = [100.0]
    profile = dict(host="test", job_id="1", cpu_ids=[0], gpu_ids=[], memory=1024,
                   cpus=1, end_time=135, observed_at=100)
    args = SimpleNamespace(directory=tmp_path, python="unused", scheduler="unused",
                           cpus=None, gpu=False, root=[])
    monkeypatch.setattr(slurm.time, "time", lambda: now[0])
    monkeypatch.setattr(slurm.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(slurm.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
    calls = []
    def unavailable(*_):
        calls.append(now[0])
        raise subprocess.TimeoutExpired("scontrol", 15)
    monkeypatch.setattr(slurm, "inventory", unavailable)
    def forbidden(*_, **__):
        raise AssertionError("must not launch without a freshly validated grant")
    monkeypatch.setattr(slurm.subprocess, "Popen", forbidden)
    assert slurm.supervise(args, profile, [], lambda: False) == 0
    assert calls == [100, 130]
    assert json.loads((tmp_path / "launcher.json").read_text())["state"] == "stopped"

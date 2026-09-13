"""Durable late submissions, resource packing, live pressure, and retained mappings."""
import json
import time

import pytest

from ecarsi.batch import assign, capacity, configuration, dataset_commands, queue, reconcile, receipt_path, submit


def test_persistent_admission(tmp_path):
    c = {"directory": str(tmp_path / "queue"), "python": "/usr/bin/python3", "scheduler": "tcp://test:1",
         "max_cpu_percent": 90, "env": {}}
    source = tmp_path / "source"
    source.mkdir()
    def job(n, **kw):
        return {"input": str(source), "output": str(tmp_path / n), "cpus": 2, "memory_gb": 4, "hours": 1, **kw}
    now = time.time()
    def node(n):
        return {"id": n, "cpu_ids": [0, 1, 2, 3], "cpus": 4, "memory": 8 * 2**30,
                "memory_headroom": 8 * 2**30, "observed_at": now, "end_time": now + 7200}
    first = submit(c, [job("a"), job("b"), job("c"), job("d"), job("e")])
    assert submit(c, [job("a")])[0]["id"] == first[0]["id"]
    with queue(c["directory"]) as s:
        s["nodes"] = {k: node(k) for k in ("n1", "n2")}
        assign(s, c, now)
        assert [r["state"] for r in s["datasets"]] == ["assigned"] * 4 + ["queued"]
        assert [r["node"] for r in s["datasets"][:4]] == ["n1", "n2", "n1", "n2"]
        assert set(s["datasets"][0]["cpu_ids"]).isdisjoint(s["datasets"][2]["cpu_ids"])
        r = s["datasets"][0]
        p = receipt_path(c["directory"], r)
        p.parent.mkdir()
        p.write_text(json.dumps({"node": "n1", "state": "completed", "exit_code": 0}))
    # Open the durable queue afresh, as a restarted node would. The freed slot is reused.
    with queue(c["directory"]) as s:
        reconcile(s, c["directory"])
        assign(s, c, now)
        assert s["datasets"][-1]["state"] == "assigned"
    late = submit(c, [job("late")])[0]
    with queue(c["directory"]) as s:
        assign(s, c, now)
        assert s["datasets"][-1]["id"] == late["id"] and s["datasets"][-1]["state"] == "queued"
        # A newly joined node takes the late job without restarting or changing task names.
        s["nodes"]["n3"] = node("n3")
        s["nodes"]["n3"]["memory_headroom"] = 1
        assign(s, c, now)
        assert s["datasets"][-1]["state"] == "queued"  # real cgroup pressure wins
        s["nodes"]["n3"]["memory_headroom"] = 8 * 2**30
        assign(s, c, now)
        assert s["datasets"][-1]["node"] == "n3"
    # No partial publication after a bad row.
    with pytest.raises(ValueError):
        submit(c, [job("new"), job("bad", memory_gb=float("nan"))])
    with queue(c["directory"]) as s:
        assert len(s["datasets"]) == 6


def test_pressure_stale_time_and_mapping(tmp_path):
    now = time.time()
    node = {"id": "n", "cpu_ids": [0, 1, 2, 3], "cpus": 4, "memory": 16 * 2**30,
            "memory_headroom": 16 * 2**30, "observed_at": now, "end_time": now + 7200}
    running = {"node": "n", "state": "running", "cpu_ids": [0], "memory_gb": 4,
               "rss_bytes": 10 * 2**30, "cpu_percent": 380}
    assert capacity(node, [running], now) == ([1, 2, 3], 6 * 2**30, 95)
    running.update(state="paused", reservation_held=True)
    assert capacity(node, [running], now) == ([1, 2, 3], 6 * 2**30, 95)
    running.update(state="running", reservation_held=False)
    waiting = {"state": "queued", "cpus": 1, "memory_gb": 4, "hours": 1, "attempt": 0}
    s = {"nodes": {"n": node}, "datasets": [running, waiting]}
    assign(s, {"max_cpu_percent": 90}, now)
    assert "CPU usage" in waiting["queue_reason"]
    running["cpu_percent"] = 0
    node["observed_at"] = now - 61
    assign(s, {"max_cpu_percent": 90}, now)
    assert "offline" in waiting["queue_reason"]
    node["observed_at"] = now
    node["end_time"] = now + 100
    assign(s, {"max_cpu_percent": 90}, now)
    assert "Slurm time" in waiting["queue_reason"]
    c = tmp_path / "config.json"
    c.write_text(json.dumps({"directory": str(tmp_path), "python": "/usr/bin/python3", "scheduler": "x",
                            "env": {"ARK_API_KEY": "never-store-this"}}))
    with pytest.raises(ValueError, match="credentials"):
        configuration(c)
    unit = tmp_path / "units" / "u"
    (unit / "input").mkdir(parents=True)
    (unit / "input" / "organized.h5ad").touch()
    commands = list(dataset_commands("python", {"input": "in", "output": str(tmp_path), "sample_map": "map.json"}))
    assert commands[0][-2:] == ["--stop-after", "organize"]
    assert any(c[-2:] == ["--sample-map", "map.json"] for c in commands)


def test_independent_supervisor_and_monitor(tmp_path, monkeypatch):
    import os
    from ecarsi.batch import supervise, monitor
    script = tmp_path / 'fake-python'
    script.write_text('#!/bin/sh\nsleep 0.1\nexit 0\n')
    script.chmod(0o700)
    c = {"directory": str(tmp_path / 'queue'), "python": str(script), "scheduler": "test",
         "env": {}, "max_cpu_percent": 90}
    config = tmp_path / 'config.json'
    config.write_text(json.dumps(c))
    inp = tmp_path / 'input'
    inp.mkdir()
    row = submit(c, [{"input": str(inp), "output": str(tmp_path / 'output'), 'cpus': 1, 'memory_gb': 1, 'hours': 1}])[0]
    affinity = sorted(os.sched_getaffinity(0))
    with queue(c['directory']) as s:
        s['nodes']['n'] = {'id': 'n', 'cpus': 1, 'cpu_ids': affinity[:1], 'memory': 2**30,
                            'memory_headroom': 2**30, 'observed_at': time.time(), 'end_time': time.time()+7200}
        assign(s, c)
    try:
        assert supervise(config, row['id'], 1) == 0
    finally:
        os.sched_setaffinity(0, affinity)
    monkeypatch.setenv('ECA_DATASET_QUEUE', c['directory'])
    monkeypatch.delenv('ECA_PERISCOPE_BATCH_STATUS', raising=False)
    state = monitor()
    assert state['datasets'][0]['state'] == 'completed'
    assert state['datasets'][0]['exit_code'] == 0
    assert not state['datasets'][0]['waiting']
    with pytest.raises(RuntimeError, match='receipt'):
        supervise(config, row['id'], 1)


def test_slurm_driver_is_not_the_srun_client(tmp_path, monkeypatch):
    import os
    from pathlib import Path
    from ecarsi.batch import driver_process
    proc = tmp_path / '123'
    proc.mkdir()
    (proc / 'cmdline').write_bytes(b'/venv/bin/python\0-P\0-m\0ecarsi\0run\0/input\0/output\0')
    (proc / 'cgroup').write_text('1:memory:/slurm/uid_1/job_42/step_7/task_0\n')
    original = Path.glob
    monkeypatch.setattr(Path, 'glob', lambda p, pattern: iter([proc / 'cmdline']) if str(p) == '/proc' else original(p, pattern))
    monkeypatch.setattr(os, 'sched_getaffinity', lambda pid: {2, 3})
    assert driver_process({'output': '/output', 'cpu_ids': [2, 3]}, {'job_id': 42}) == 123
    assert driver_process({'output': '/output', 'cpu_ids': [0, 1]}, {'job_id': 42}) is None
    assert driver_process({'output': '/output', 'cpu_ids': [2, 3]}, {'job_id': 43}) is None

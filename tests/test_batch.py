"""Durable late submissions, resource packing, live pressure, and retained mappings."""
import json
import time

import pytest

from ecarsi.batch import assign, capacity, configuration, dataset_commands, queue, reconcile, receipt_observations, receipt_path, submit


def test_drain_only_waits_for_existing_executions_and_receipts():
    from ecarsi.batch import drain_complete

    row = {'node': 'n', 'state': 'running'}
    assert not drain_complete([row], {'n'})
    row['state'] = 'paused'
    row['reservation_held'] = True
    assert not drain_complete([row], {'n'})
    row['reservation_held'] = False
    assert drain_complete([row], {'n'})


def test_confirmed_lost_execution_with_handoff_marker_requeues(tmp_path):
    from ecarsi.batch import retry_finished

    row = dict(id='x', output=str(tmp_path/'out'), state='paused', attempt=5, node='n')
    receipt = receipt_path(tmp_path, row)
    receipt.parent.mkdir()
    receipt.write_text(json.dumps(dict(node='n', state='paused', finished_at=11, exit_code=None,
                                       termination_reason='execution_lost',
                                       reason='Previous supervisor stopped; execution confirmed absent')))
    receipt.with_suffix('.handoff.json').write_text(json.dumps(dict(id='x', attempt=5, requested_at=10)))
    retry_finished({'datasets': [row]}, {'directory': str(tmp_path), 'max_attempts': 1}, now=12)
    assert row['state'] == 'retry_wait' and row['handoff_restarts'] == 1


def test_atomic_queue_reader_does_not_wait_for_writer_or_publish(tmp_path):
    with queue(tmp_path) as state:
        state['datasets'] = [{'name':'committed'}]
    before = (tmp_path/'status.json').read_bytes()
    with queue(tmp_path) as writer:
        writer['datasets'] = [{'name':'pending'}]
        with queue(tmp_path, write=False) as snapshot:
            assert snapshot['datasets'] == [{'name':'committed'}]
            snapshot['datasets'].clear()
        assert (tmp_path/'status.json').read_bytes() == before


def test_node_receipt_reads_only_owners_and_preserves_unobserved_execution(tmp_path):
    own = dict(id='own', attempt=1, node='current', state='running')
    inherited = dict(id='inherited', attempt=2, node='previous', state='running')
    foreign = dict(id='foreign', attempt=3, node='another', state='paused', reservation_held=True)
    state = {'datasets': [own, inherited, foreign]}
    for row, receipt in (
        (own, dict(node='current', state='failed', finished_at=None)),
        (inherited, dict(node='previous', state='failed', finished_at=10)),
    ):
        path = receipt_path(tmp_path, row)
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(receipt))
    # This file is intentionally unreadable as JSON: another node's bad or
    # stalled receipt must not block this node's reconciliation.
    receipt_path(tmp_path, foreign).write_text('{invalid')
    observations = receipt_observations(state, tmp_path, owners={'current', 'previous'})
    assert set(observations) == {('own', 1), ('inherited', 2)}
    reconcile(state, tmp_path, observations)
    assert own['state'] == 'running' and own['reservation_held'] is True
    assert inherited['state'] == 'failed' and inherited['finished_at'] == 10
    assert foreign['state'] == 'paused' and foreign['reservation_held'] is True


def test_model_wait_shares_driver_cpu_without_borrowing_memory_or_locks():
    import copy

    node = dict(id='n', cpu_ids=[0], available_cpu_ids=[0], cpus=1,
                memory=6 * 2**30, memory_headroom=6 * 2**30,
                observed_at=100, end_time=10000)
    waiting = dict(id='model', node='n', state='running', cpu_ids=[0],
                   memory_gb=2, rss_bytes=2**30, cpu_percent=0)
    new = dict(id='compute', state='queued', cpus=1, memory_gb=2, hours=1, attempt=0)
    state = dict(nodes={'n': node}, datasets=[waiting, new])
    config = dict(python='/python', scheduler='pool', env={}, max_cpu_percent=90)
    assign(state, config, now=100)
    assert new['state'] == 'queued'
    node['driver_cpu_slots_per_core'] = 2
    config['driver_cpu_slots_per_core'] = 2
    blocked = copy.deepcopy(state)
    blocked['nodes']['n']['available_cpu_ids'] = []
    assign(blocked, config, now=100)
    assert blocked['datasets'][1]['state'] == 'queued'  # old supervisor owns the CPU lock
    hot = copy.deepcopy(state)
    hot['datasets'][0]['cpu_percent'] = 95
    assign(hot, config, now=100)
    assert hot['datasets'][1]['state'] == 'queued'
    full = copy.deepcopy(state)
    full['datasets'][1]['memory_gb'] = 5
    assign(full, config, now=100)
    assert full['datasets'][1]['state'] == 'queued'  # actual low RSS cannot replace reserved RAM
    assign(state, config, now=100)
    assert new['state'] == 'assigned' and new['cpu_ids'] == [0]
    assert capacity(node, state['datasets'], 100)[0] == []  # bounded CPU sharing
    assert capacity(node, state['datasets'], 100)[1] == 2 * 2**30


def test_input_budget_uses_uncompressed_matrix_metadata(tmp_path):
    import h5py
    from ecarsi.batch import input_resources
    step = tmp_path / "standardize"
    step.mkdir()
    (step / "result.json").write_text("{}")
    path = step / "standardized.h5ad"
    with h5py.File(path, "w") as f:
        f.create_dataset("layers/counts", shape=(20, 300), dtype="i4", chunks=True)
    small = input_resources(tmp_path)
    assert small["cpus"] == 1 and small["memory_gb"] == 4
    with h5py.File(path, "w") as f:
        # Unallocated chunks keep the file tiny; in-memory reads would be huge.
        f.create_dataset("layers/counts", shape=(50000, 30000), dtype="f8", chunks=True)
    large = input_resources(tmp_path)
    assert path.stat().st_size < 100000
    assert large["memory_gb"] > 64 and large["cpus"] == 2


@pytest.mark.parametrize('pool_error', ['ConnectionError: pool task lost: worker disconnected',
                                       'ConnectionError: pool task done:'])
def test_checkpoint_retry_is_bounded_and_requires_terminal_evidence(tmp_path, pool_error):
    from ecarsi.batch import retry_finished
    c = {"directory": str(tmp_path), "max_attempts": 2}
    r = dict(id="x", output=str(tmp_path / "run"), state="failed", attempt=1, node="n", exit_code=1)
    s = {"datasets": [r]}
    retry_finished(s, c, 100)
    assert r["state"] == "failed"  # no receipt
    p = receipt_path(tmp_path, r);p.parent.mkdir()
    p.write_text(json.dumps({"node":"n", "state":"failed", "exit_code":1, "finished_at":90}))
    retry_finished(s, c, 100)
    assert r["state"] == "failed"  # no valid sample plan; do not guess again
    samples = tmp_path / "run/units/u/persample"
    samples.mkdir(parents=True)
    (samples / "manifest.json").write_text('{"state":"complete"}')
    r["reservation_held"] = True
    retry_finished(s, c, 100)
    assert r["state"] == "failed"  # old execution still uncertain
    r.pop("reservation_held")
    retry_finished(s, c, 100)
    assert r["state"] == "failed"  # arbitrary scientific errors are not retried
    log = tmp_path / "run.log"
    log.write_text("ConnectionError: pool task lost: worker disconnected\nValueError: invalid annotations\n")
    r["log"] = str(log)
    retry_finished(s, c, 100)
    assert r["state"] == "failed"  # an old transient error is not the terminal cause
    log.write_text(pool_error + "\n")
    r["log"] = str(log)
    retry_finished(s, c, 100)
    assert r["state"] == "retry_wait" and r["retry_after"] == 160
    assert r["attempt_history"][0]["attempt"] == 1 and "node" not in r
    retry_finished(s, c, 159)
    assert r["state"] == "retry_wait"
    retry_finished(s, c, 160)
    assert r["state"] == "queued" and "retry_after" not in r
    r.update(state="failed", attempt=2, node="n")
    retry_finished(s, c, 200)
    assert r["state"] == "failed"  # finite retry budget


def test_activity_distinguishes_model_from_compute(tmp_path):
    from ecarsi.batch import activity
    p=tmp_path/'run.log'
    p.write_text('[pool] __main__.compute_attempt -> tcp://worker\n12:00 == [annotate] agent: submit_cluster(...)\n')
    assert activity({'log':str(p)})['activity_kind']=='model'
    with p.open('a') as f:f.write('[pool] waiting msp.integrate.pipeline._run_harmony_gpu: gpus\n')
    assert activity({'log':str(p)})['activity_kind']=='compute_wait'


def test_terminal_receipt_replaces_earlier_supervisor_failure(tmp_path):
    row = dict(id='x', attempt=1, node='n', state='failed',
               reason='Driver supervisor exited without a terminal receipt')
    path = receipt_path(tmp_path, row)
    path.parent.mkdir()
    path.write_text(json.dumps(dict(node='n',state='failed',exit_code=1,finished_at=90)))
    reconcile({'datasets':[row]},tmp_path)
    assert row['exit_code'] == 1 and row['finished_at'] == 90
    assert 'reason' not in row


def test_deadline_retry_preserves_user_pauses_and_clears_attempt_fields(tmp_path):
    from ecarsi.batch import retry_finished
    c = {"directory": str(tmp_path), "max_attempts": 2}
    stale = dict(pid=10, child_pid=11, driver_pid=12, started_at=20, assigned_at=19)
    r = dict(id="deadline", output=str(tmp_path / "run"), state="paused", attempt=1,
             node="n", exit_code=3, **stale)
    samples = tmp_path / "run/units/u/persample"
    samples.mkdir(parents=True)
    (samples / "manifest.json").write_text('{"state":"complete"}')
    p = receipt_path(tmp_path, r)
    p.parent.mkdir()
    receipt = dict(node="n", state="paused", exit_code=3, finished_at=90, reason="Driver stop requested")
    p.write_text(json.dumps(receipt))
    retry_finished({"datasets": [r]}, c, 100)
    assert r["state"] == "paused"
    receipt["reason"] = "Driver time budget or Slurm allocation ending"
    p.write_text(json.dumps(receipt))
    retry_finished({"datasets": [r]}, c, 100)
    assert r["state"] == "retry_wait" and not stale.keys() & r.keys()
    assert r["attempt_history"][0]["started_at"] == 20


def test_launcher_failure_records_terminal_and_releases_capacity(tmp_path, monkeypatch):
    import os
    from ecarsi.batch import supervise
    c = dict(directory=str(tmp_path / "queue"), python=str(tmp_path / "missing"),
             scheduler="unused", env={}, max_cpu_percent=90)
    inp = tmp_path / "input"
    inp.mkdir()
    submit(c, [dict(input=str(inp), output=str(tmp_path / "run"), cpus=1, memory_gb=1, hours=1)])
    with queue(c["directory"]) as s:
        s["nodes"]["n"] = dict(id="n", cpu_ids=[0], cpus=1, memory=2**30, memory_headroom=2**30,
                                observed_at=time.time(), end_time=time.time()+7200)
        assign(s, c)
        row = s["datasets"][0].copy()
    monkeypatch.setattr(os, "sched_setaffinity", lambda *_: None)
    with pytest.raises(FileNotFoundError):
        supervise(None, row["id"], row["attempt"], directory=c["directory"])
    record = json.loads(receipt_path(c["directory"], row).read_text())
    assert record["state"] == "failed" and record["finished_at"]
    assert record["termination_reason"] == "launch_failed" and "child_pid" not in record
    with queue(c["directory"]) as s:
        reconcile(s, c["directory"])
        assert s["datasets"][0]["state"] == "failed"
        assert capacity(s["nodes"]["n"], s["datasets"], time.time())[:2] == ([0], 2**30)


def test_controller_recovers_after_transient_receipt_read_failure(tmp_path, monkeypatch):
    import ecarsi.batch as batch
    c = dict(directory=str(tmp_path / "queue"), python="/unused", scheduler="unused", env={})
    config = tmp_path / "config.json"
    config.write_text(json.dumps(c))
    calls = []
    admissions = []
    monkeypatch.setattr(batch, "assign", lambda state, config: admissions.append(1))
    def reconcile_once(state, directory, observations=None):
        calls.append(1)
        if len(calls) == 1:
            state["do_not_publish"] = True
            raise OSError("temporary shared storage outage")
        assert "do_not_publish" not in state
    def wait(_):
        if len(calls) == 2:
            raise KeyboardInterrupt
    monkeypatch.setattr(batch, "reconcile", reconcile_once)
    monkeypatch.setattr(batch.time, "sleep", wait)
    with pytest.raises(KeyboardInterrupt):
        batch.controller(config)
    state = json.loads((tmp_path / "queue/status.json").read_text())
    assert state["controller"]["observed_at"] and len(calls) == 2
    assert admissions == [1]  # placement resumes after reconciliation succeeds


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
        p.write_text(json.dumps({"node": "n1", "state": "completed", "exit_code": 0, "finished_at": now}))
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
    s['nodes']['old'] = dict(node, id='old', draining=True)
    assign(s, {"max_cpu_percent": 90}, now)
    assert 'offline' not in waiting['queue_reason']
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
    commands = list(dataset_commands("python", dict(input="in", output=str(tmp_path),
                    execution={"osp_dispatch_module": "eca_stages.osp_dispatch"})))
    assert commands[1] == ["python", "-P", "-m", "eca_stages.osp_dispatch", str(unit)]
    assert commands[-1] == ["python", "-P", "-m", "ecarsi", "run", "in", str(tmp_path)]


def test_merged_driver_budget_counts_predecessor_executions():
    node = dict(id="merged", predecessors=["cpu-rich", "memory-rich"],
                cpu_ids=[0, 1, 2, 3], available_cpu_ids=[2, 3], cpus=4,
                memory=16*2**30, memory_headroom=16*2**30)
    old = dict(node="cpu-rich", state="running", cpu_ids=[0], memory_gb=4, rss_bytes=2**30)
    held = dict(node="memory-rich", state="paused", reservation_held=True, cpu_ids=[1], memory_gb=4)
    cpus, memory, busy = capacity(node, [old, held], time.time())
    assert cpus == [2, 3] and memory == 8*2**30


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
    (proc / 'cmdline').write_bytes(b'/venv/bin/python\0-P\0-m\0eca_stages.osp_dispatch\0/output/units/u\0')
    row = dict(output='/output', cpu_ids=[2, 3], execution={'osp_dispatch_module': 'eca_stages.osp_dispatch'})
    assert driver_process(row, {'job_id': 42}) == 123
    assert driver_process(row, {'job_id': 43}) is None


def test_checkpoint_handoff_does_not_consume_failure_budget_or_override_user_pause(tmp_path):
    from ecarsi.batch import handoff_nodes, retry_finished
    c = dict(directory=str(tmp_path/'q'), max_attempts=1)
    r = dict(id='h', attempt=1, node='old', state='running', output=str(tmp_path/'run'))
    with queue(c['directory']) as state:
        state['datasets'] = [r]
    receipt = receipt_path(c['directory'], r)
    receipt.parent.mkdir()
    receipt.write_text(json.dumps(dict(node='old', state='running')))
    assert handoff_nodes(c, {'old'}) == ['h']
    assert handoff_nodes(c, {'old'}) == []
    finished = dict(node='old', state='paused', exit_code=3, finished_at=time.time()+1)
    receipt.write_text(json.dumps(finished))
    r.update(state='paused')
    control = tmp_path/'run/units/u/loop_control.json'
    control.parent.mkdir(parents=True)
    control.write_text('{"pause":true}')
    retry_finished({'datasets':[r]}, c, time.time()+2)
    assert r['state']=='paused'
    control.write_text('{}')
    retry_finished({'datasets':[r]}, c, time.time()+2)
    assert r['state']=='retry_wait' and r['handoff_restarts']==1
    assert r['attempt_history'][0]['attempt']==1


@pytest.mark.parametrize('marker,termination,expected', [
    ('interrupt', 'user_stop', 'retry_wait'),
    ('interrupt', 'deployment_handoff', 'retry_wait'),
    ('ordinary', 'user_stop', 'paused'),
    ('absent', 'user_stop', 'paused'),
    ('wrong_attempt', 'user_stop', 'paused'),
])
def test_interrupted_handoff_retries_only_matching_maintenance_request(tmp_path, marker, termination, expected):
    from ecarsi.batch import handoff_nodes, retry_finished
    c = dict(directory=str(tmp_path / 'q'), max_attempts=1)
    row = dict(id='target', attempt=1, node='n', state='running', output=str(tmp_path / 'run'))
    other = dict(row, id='other', output=str(tmp_path / 'other'))
    with queue(c['directory']) as state:
        state['datasets'] = [row, other]
    path = receipt_path(c['directory'], row)
    path.parent.mkdir()
    path.write_text(json.dumps(dict(node='n', state='running')))
    other_path = receipt_path(c['directory'], other)
    other_path.write_text(json.dumps(dict(node='n', state='running')))
    if marker != 'absent':
        assert handoff_nodes(c, {'n'}, dataset_ids={'target'}, interrupt=marker != 'ordinary') == ['target']
        assert not other_path.with_suffix('.handoff.json').exists()
        if marker == 'wrong_attempt':
            handoff = path.with_suffix('.handoff.json')
            request = json.loads(handoff.read_text())
            request['attempt'] = 2
            handoff.write_text(json.dumps(request))
    path.write_text(json.dumps(dict(node='n', state='paused', exit_code=137,
                                    reason='Driver stop requested', termination_reason=termination,
                                    finished_at=time.time() + 1)))
    row['state'] = 'paused'
    retry_finished({'datasets': [row]}, c, now=time.time() + 2)
    assert row['state'] == expected


def test_supervisor_records_maintenance_stop_after_step_is_absent(tmp_path, monkeypatch):
    import os
    import signal
    import ecarsi.batch as batch

    c = dict(directory=str(tmp_path / 'q'))
    row = dict(id='target', attempt=1, node='n', state='assigned', cpu_ids=[0],
               input=str(tmp_path / 'input'), output=str(tmp_path / 'run'),
               log=str(tmp_path / 'run.log'), memory_gb=1, hours=1,
               execution=dict(python='/unused', scheduler='unused', env={}))
    with queue(c['directory']) as state:
        state['nodes']['n'] = dict(end_time=time.time() + 3600, job_id=None)
        state['datasets'] = [row]
    handlers = {}
    checks = []
    monkeypatch.setattr(batch.signal, 'signal', lambda kind, handler: handlers.__setitem__(kind, handler))
    monkeypatch.setattr(batch.os, 'sched_setaffinity', lambda *_: None)
    monkeypatch.setattr(batch.os, 'killpg', lambda *_: None)
    monkeypatch.setattr(batch, 'process_tree', lambda *_: (0, 0))
    monkeypatch.setattr(batch, 'attempt_steps', lambda *_: checks.append('absent') or [])

    class Child:
        pid = os.getpid()
        returncode = None

        def __init__(self, *args, **kwargs):
            assert batch.handoff_nodes(c, {'n'}, dataset_ids={'target'}, interrupt=True) == ['target']
            handlers[signal.SIGTERM](signal.SIGTERM, None)

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            self.returncode = 137
            return self.returncode

    monkeypatch.setattr(batch.subprocess, 'Popen', Child)
    assert batch.supervise(None, 'target', 1, directory=c['directory']) == 0
    receipt = json.loads(receipt_path(c['directory'], row).read_text())
    assert checks and receipt['state'] == 'paused'
    assert receipt['termination_reason'] == 'deployment_handoff'


def test_shorter_allocation_window_and_expiry_preserve_failure_budget(tmp_path):
    from ecarsi.batch import retry_finished
    now = time.time()
    c = dict(directory=str(tmp_path/'q'), max_attempts=1, max_cpu_percent=90)
    node = dict(id='n', cpu_ids=[0], cpus=1, memory=8*2**30,
                memory_headroom=8*2**30, observed_at=now, end_time=now+3*3600)
    row = dict(id='expiry', output=str(tmp_path/'run'), state='queued',
               hours=6, cpus=1, memory_gb=4, attempt=0)
    state = dict(nodes={'n': node}, datasets=[row])
    assign(state, c, now)
    assert row['state']=='queued' and 'Slurm time' in row['queue_reason']
    c['min_attempt_hours']=2
    assign(state, c, now)
    assert row['state']=='assigned' and row['hours']==6
    row['state']='paused'
    path=receipt_path(c['directory'],row)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(dict(node='n',state='paused',finished_at=now+1,
                                   termination_reason='allocation_end',exit_code=137)))
    control=tmp_path/'run/units/u/loop_control.json'
    control.parent.mkdir(parents=True)
    control.write_text('{"pause":true}')
    retry_finished(state,c,now+2)
    assert row['state']=='paused'
    control.write_text('{}')
    retry_finished(state,c,now+2)
    assert row['state']=='retry_wait' and row['allocation_restarts']==1
    retry_finished(state,c,now+100)
    node.update(end_time=now+101,observed_at=now+100)
    assign(state,c,now+100)
    assert row['state']=='queued'  # cannot churn through the expiring allocation


def test_transient_launch_failure_has_bounded_retry(tmp_path):
    import errno
    from ecarsi.batch import launch_failure, retry_finished
    c = dict(directory=str(tmp_path/'q'), max_attempts=2)
    for code in (errno.EAGAIN, errno.ENOENT):
        r = dict(id=str(code), attempt=1, node='n', state='failed', output=str(tmp_path/'out'))
        path = receipt_path(c['directory'], r)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(dict(node='n', state='failed', finished_at=time.time(),
            exit_code=None, termination_reason=launch_failure(OSError(code, 'injected')))))
        retry_finished({'datasets':[r]}, c)
        assert r['state'] == ('retry_wait' if code == errno.EAGAIN else 'failed')
        r.update(state='failed', node='n', attempt=2)
        path = receipt_path(c['directory'], r)
        path.write_text(json.dumps(dict(node='n', state='failed', finished_at=time.time(),
            exit_code=None, termination_reason='launch_transient')))
        retry_finished({'datasets':[r]}, c)
        assert r['state'] == 'failed'
def test_driver_headroom_reclaims_clean_cgroup_cache_but_not_dirty_or_shared(tmp_path, monkeypatch):
    from ecarsi.batch import memory_headroom
    import ecarsi.resources
    gib = 2**30
    monkeypatch.setattr(ecarsi.resources, 'available_memory_bytes', lambda: 32*gib)
    meminfo = tmp_path/'meminfo'
    meminfo.write_text(f'MemAvailable: {128*gib//1024} kB\n')
    proc = tmp_path/'cgroup'
    sysfs = tmp_path/'sys'
    job = sysfs/'memory/slurm/job'
    job.mkdir(parents=True)
    (job/'memory.limit_in_bytes').write_text(str(32*gib))
    (job/'memory.usage_in_bytes').write_text(str(23*gib))
    (job/'memory.stat').write_text(f'total_cache {21*gib}\ntotal_shmem {2*gib}\n'
                                   f'total_dirty {gib}\ntotal_writeback 0\n')
    proc.write_text('4:memory:/slurm/job/step\n')
    assert memory_headroom(proc, sysfs, meminfo) == (32-23)*gib + 18*gib*4//5
    (job/'memory.stat').unlink()
    assert memory_headroom(proc, sysfs, meminfo) == 9*gib

    unified = sysfs/'slurm/job'
    unified.mkdir(parents=True)
    (unified/'memory.max').write_text(str(32*gib))
    (unified/'memory.current').write_text(str(23*gib))
    (unified/'memory.stat').write_text(f'file {21*gib}\nshmem {2*gib}\n'
                                       f'file_dirty {gib}\nfile_writeback 0\n')
    proc.write_text('0::/slurm/job/step\n')
    assert memory_headroom(proc, sysfs, meminfo) == (32-23)*gib + 18*gib*4//5


def test_confirmed_samples_continue_without_full_driver_admission(tmp_path):
    from ecarsi.batch import assign, dataset_commands
    now = time.time()
    row = dict(id='ready',state='queued',attempt=1,preparation_complete=True,
               queue_reason='Waiting for driver CPU/memory capacity',cpus=2,memory_gb=32,hours=1,
               input='/input',output='/output',resource_profile=dict(n_cells=200000))
    node = dict(id='prep',role='preparation',cpus=2,cpu_ids=[0,1],memory=8*2**30,
                memory_headroom=8*2**30,observed_at=now,end_time=now+10000)
    cached = dict(id='cached',state='queued',attempt=1,preparation_complete=True,
                  preparation_backfill=True,cpus=2,memory_gb=32,hours=1)
    state = dict(nodes={'prep':node},datasets=[cached,row],preparation_offers_at=now,
                 preparation_offers={'ready':dict(prepared_count=4,remaining_count=20,min_missing_cells=10000)},
                 pool_capacity=dict(observed_at=now,workers=[dict(free_cpus=8,free_memory=48*2**30,end_time=now+10000)]))
    config = dict(directory=str(tmp_path),max_cpu_percent=90,preparation_module='v.prepare',
                  preparation_memory_gb=2,preparation_max_datasets=1,preparation_samples=8,
                  preparation_backfill_slots=1,preparation_continuation=True)
    assign(state,config,now)
    assert row['state']=='assigned' and row['admission_phase']=='preparation'
    assert row['admission_memory_gb']==2 and row['execution']['preparation_samples']==4
    assert row['execution']['preparation_max_sample_cells']>=10000
    command=next(dataset_commands('/python',row))
    assert command[command.index('--max-prepared-samples')+1]=='8'
    # The cap survives a partial attempt and retry; no new unbounded batch.
    row.update(state='queued',queue_reason='',attempt=2)
    row.pop('node');row.pop('cpu_ids')
    assign(state,config,now)
    assert row['state']=='assigned' and row['execution']['preparation_max_prepared_samples']==8


def test_interrupt_can_finish_a_recorded_cooperative_deployment(tmp_path):
    from ecarsi.batch import handoff_nodes
    c=dict(directory=str(tmp_path/'q'))
    row=dict(id='h',attempt=1,node='n',state='running')
    with queue(c['directory']) as state:
        state['datasets']=[row]
    receipt=receipt_path(c['directory'],row)
    receipt.parent.mkdir()
    receipt.write_text(json.dumps(dict(node='n',state='running')))
    assert handoff_nodes(c,{'n'})==['h']
    assert handoff_nodes(c,{'n'},interrupt=True)==['h']
    assert json.loads(receipt.with_suffix('.handoff.json').read_text())['interrupt'] is True
    receipt.with_suffix('.pause').write_text('user pause\n')
    assert handoff_nodes(c,{'n'},interrupt=True)==[]

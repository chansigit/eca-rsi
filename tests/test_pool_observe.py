"""A broken dataset status file must not stop compute telemetry."""
# The gen-1 Dask pool: only the environments that carry dask/distributed run these.
# Generation 2 schedules through HyperQueue and never imports them, so the science image
# (what the workers run) has no distributed and skips this file. See tests/README.md.
import pytest

pytest.importorskip("distributed")

import json
import time
from types import SimpleNamespace

from ecarsi.pool import client, observe


def test_trailing_resource_averages_separate_workers_and_ignore_missing():
    def worker(address, cpu, rss, gpu=20):
        return dict(address=address, host='same-node', cpus=4, memory=1000,
                    metrics=dict(cpu=cpu, memory=rss), gpu_stats=[dict(uuid='gpu', utilization_percent=gpu)])
    rows = [dict(observed_at=100, workers=[worker('old', 400, 1000)]),
            dict(observed_at=600, workers=[worker('a', 200, 200), worker('b', 0, 400)]),
            dict(observed_at=700, workers=[worker('a', 400, None, 60)]),
            dict(observed_at=800, workers=[worker('a', None, 600, None)])]
    result = observe.resource_averages(rows, now=800)
    assert set(result['workers']) == {'a', 'b'}
    assert result['workers']['a']['cpu_percent'] == 75
    assert result['workers']['a']['memory_percent'] == 40
    assert result['workers']['a']['rss_bytes'] == 400
    assert result['workers']['b']['cpu_percent'] == 0
    assert result['gpu_utilization_percent']['gpu'] == 40  # shared GPU is counted once
    assert result['samples'] == 3 and result['max_sample_gap_seconds'] == 100
    assert result['started'] == 600 and result['workers']['b']['finished'] == 600
    assert observe.resource_averages(rows, now=1500)['workers'] == {}


def test_process_counters_include_descendants_and_reaped_child_cpu(monkeypatch):
    def stat(pid, parent, ticks, rss):
        fields = ["0"] * 22
        fields[0], fields[1] = "S", str(parent)
        fields[11:15] = map(str, ticks)
        fields[21] = str(rss)
        return SimpleNamespace(parent=SimpleNamespace(name=str(pid)),
                               read_text=lambda: f"{pid} (name with spaces) " + " ".join(fields))
    monkeypatch.setattr(observe.Path, 'glob', lambda *args: [
        stat(100, 1, [10, 2, 30, 4], 5),
        stat(101, 100, [20, 3, 0, 0], 7),
        stat(102, 1, [99, 99, 99, 99], 99),
    ])
    assert observe.counters([{'pid': 100}]) == {100: (69, 12)}


def test_observe_once_survives_bad_dataset_status(tmp_path, monkeypatch):
    queue = tmp_path / "status.json"
    queue.write_text("unfinished JSON")
    closed = []
    fake = SimpleNamespace(
        run_on_scheduler=lambda *args: {"epoch": "test", "workers": {}, "tasks": {}},
        scheduler_info=lambda: {"workers": {}},
        close=lambda **kwargs: closed.append(True),
    )
    monkeypatch.setattr(client, "connect", lambda *args, **kwargs: fake)
    observe.observe(SimpleNamespace(
        directory=tmp_path, queue=queue, scheduler=None, host_python="python3",
        report_interval=600, interval=15, once=True,
    ))
    sample = json.loads((tmp_path / "utilization-latest.json").read_text())
    assert sample["queue_error"] and "error" not in sample
    assert sample["epoch"] == "test" and sample["workers"] == []
    assert closed == [True]


def test_resource_budgets_events_and_compact_history(tmp_path, monkeypatch):
    now = time.time()
    task = dict(id='task', label='osp.compute', state='running', worker='worker',
                cpus=1, memory=2**30, gpus=0, submitted=now-8, started=now-4)
    worker = dict(host='host', job_id='job', cpus=4, cpu_ids=[0, 1, 2, 3], memory=8*2**30,
                  allocation_memory=32*2**30, gpus=1,
                  gpu_stats=[dict(uuid='gpu', utilization_percent=37)], draining=False,
                  observed_at=now, end_time=now+3600)
    pool = dict(epoch='test', workers={'worker': worker}, tasks={'task': task})
    failed = []
    def status(*args):
        if failed:
            raise ConnectionError('scheduler unavailable')
        return pool
    fake = SimpleNamespace(run_on_scheduler=status,
        scheduler_info=lambda: {'workers': {'worker': {'metrics': dict(cpu=200, memory=3*2**30, time=now)}}},
        close=lambda **kwargs: None)
    monkeypatch.setattr(client, 'connect', lambda *args, **kwargs: fake)
    queue = tmp_path/'queue.json'
    log = tmp_path/'driver.log'
    log.write_text('private model response must not be copied into resource logs')
    queue.write_text(json.dumps(dict(updated_at=now-7, controller=dict(observed_at=now-5),
        nodes={'node':dict(id='node', host='host', observed_at=now-80)},
        datasets=[dict(id='dataset',name='example',state='running',attempt=1,node='node',
            memory_gb=12,admission_memory_gb=4,memory_lending_protocol=1,reserved_memory_bytes=3*2**30,
            memory_state='model_wait',rss_bytes=2**30,log=str(log))])))
    args = SimpleNamespace(directory=tmp_path,queue=queue,scheduler=None,host_python='python3',
                           report_interval=1e-9,interval=15,once=True)
    observe.observe(args)
    first = json.loads((tmp_path/'utilization-latest.json').read_text())
    w = first['workers'][0]
    assert w['cpu_percent'] == 50 and w['cpu_cores_used'] == 2
    assert w['reserved_memory_bytes'] == 2**30 and w['rss_bytes'] == 3*2**30
    assert w['active_task_ids'] == ['task'] and w['metrics_source'] == 'dask_process'
    node = first['nodes'][0]
    assert node['reserved_memory_bytes'] == 3*2**30 and node['measured_rss_bytes'] == 2**30
    assert node['model_wait_datasets'] == 1 and first['queue_age_seconds'] >= 7
    assert first['drivers'][0]['log_path'] == str(log) and 'log_tail' not in first['drivers'][0]
    task.update(state='done',finished=time.time(),seconds_actual=4)
    observe.observe(args)
    report = json.loads((tmp_path/'ten-minute-latest.json').read_text())
    assert report['kernel_outcomes'] == {'done': 1}  # completion between collection windows is retained
    assert report['mean_worker_cpu_percent'] == {'worker': 50}
    assert report['peak_worker_rss_bytes'] == {'worker': 3*2**30}
    assert report['mean_gpu_utilization_percent'] == {'gpu': 37}
    averages = json.loads((tmp_path/'utilization-averages.json').read_text())
    assert averages['workers']['worker']['cpu_percent'] == 50
    assert averages['workers']['worker']['memory_percent'] == 37.5
    events = [json.loads(line) for line in (tmp_path/'events.jsonl').read_text().splitlines()]
    assert any(e['event']=='tasks.observed' and e['previous']['state']=='running'
               and e['value']['state']=='done' for e in events if e.get('previous'))
    before = (tmp_path/'events.jsonl').read_bytes()
    observe.observe(args)
    assert (tmp_path/'events.jsonl').read_bytes() == before
    history = [json.loads(line) for line in (tmp_path/'utilization.jsonl').read_text().splitlines()]
    assert len(history[1]['tasks']) == 1 and history[2]['tasks'] == []
    assert json.loads((tmp_path/'utilization-latest.json').read_text())['tasks'][0]['state']=='done'
    failed.append(True)
    observe.observe(args)
    new_events = (tmp_path/'events.jsonl').read_text()[len(before):]
    assert 'collector.health' in new_events and 'workers.departed' not in new_events


def test_log_rotation_and_bounded_history_recovery(tmp_path):
    from logging.handlers import RotatingFileHandler
    path = tmp_path/'utilization.jsonl'
    handler = RotatingFileHandler(path, maxBytes=160, backupCount=2)
    try:
        for i in range(20):
            observe.record(handler, dict(observed_at=i, payload='x'*30))
    finally:
        handler.close()
    assert path.with_name(path.name+'.1').is_file()
    assert path.with_name(path.name+'.2').is_file()
    assert not path.with_name(path.name+'.3').exists()
    recovered = list(observe.recent_history(tmp_path, 17, 160))
    assert recovered and recovered[-1]['observed_at'] == 19
    assert all(row['observed_at'] > 17 for row in recovered)

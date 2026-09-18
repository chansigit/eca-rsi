"""Real child processes show compute can advance while annotation waits."""
import json
import sys
import time

import pytest

from ecarsi import layout as L, persample, osp_dispatch
from ecarsi.run_state import read_json


def test_released_unit_is_verified_without_rewriting_orchestration(tmp_path, monkeypatch):
    from ecarsi.downstream import seal_release
    (tmp_path/'release').mkdir()
    final = tmp_path/'release/final.h5ad'
    final.write_bytes(b'verified scientific output')
    (tmp_path/'persample').mkdir()
    record = tmp_path/'persample/orchestration.json'
    record.write_text('{"started_at": 123}')
    seal_release(tmp_path, [])
    def no_replan(*args):
        raise AssertionError('released unit was replanned')
    monkeypatch.setattr(persample, 'main', no_replan)
    for _ in range(2):
        assert osp_dispatch.main([str(tmp_path)]) == 0
        assert record.read_text() == '{"started_at": 123}'
    final.write_bytes(b'changed')
    with pytest.raises(ValueError, match='completed output changed'):
        osp_dispatch.main([str(tmp_path)])


@pytest.mark.parametrize("budget,costs,order", [(1, [1, 1, 1], ['0', '1', '2']),
                                              (10, [1, 1, 1], ['0', '1', '2']),
                                              (2, [1, 2, 1], ['0', '2', '1'])])
def test_compute_progress_and_shared_memory_budget(tmp_path, monkeypatch, budget, costs, order):
    script = tmp_path / 'worker.py'
    script.write_text('''import json,sys,time
from pathlib import Path
value=sys.argv[1]
compute='--compute-only' in sys.argv
phase='compute' if compute else 'annotation'
def event(kind):
    with Path(sys.argv[2]).open('a') as f:
        f.write(json.dumps([value,phase,kind,time.monotonic()])+'\\n')
event('start')
time.sleep(.05 if compute else .4)
Path(sys.argv[3]).write_text(json.dumps(dict(identity=value,state='computed' if compute else 'complete')))
event('end')
''')
    monkeypatch.setenv('OSP_COMPUTE_ENDPOINT', 'pool')
    monkeypatch.setattr(persample, 'plan_concurrency', lambda _: (1, budget, 1))
    monkeypatch.setattr(osp_dispatch, '_driver_estimate_bytes', lambda e: costs[int(e['value'])])
    monkeypatch.setattr(persample, 'is_done', lambda p, *args: read_json(p / L.RUN_STATE)['state'] == 'complete')
    monkeypatch.setattr(persample, 'is_empty', lambda *args: False)
    sleep = time.sleep
    monkeypatch.setattr(persample.time, 'sleep', lambda _: sleep(.02))
    events = tmp_path / 'events.jsonl'
    entries = [dict(value=str(i), identity=str(i), n_cells=7, outdir=str(tmp_path / str(i)),
                    command=[sys.executable, str(script), str(i), str(events), L.RUN_STATE])
               for i in range(3)]
    completed = []
    elapsed = {}
    def done(entry, minutes):
        completed.append(entry['value'])
        elapsed[entry['value']] = minutes * 60
    assert persample.drive(entries, tmp_path, True, on_done=done) == []
    assert completed == order
    timing = {(v, p, k): t for v, p, k, t in map(json.loads, events.read_text().splitlines())}
    overlaps = timing[order[1], 'compute', 'start'] < timing['0', 'annotation', 'end']
    assert overlaps == (budget > 1)
    assert all(timing[str(i), 'compute', 'end'] < timing[str(i), 'annotation', 'start'] for i in range(3))
    assert elapsed['0'] >= timing['0', 'annotation', 'end'] - timing['0', 'compute', 'start']
    usage = 0
    for value, phase, kind, when in sorted(map(json.loads, events.read_text().splitlines()), key=lambda e: e[3]):
        usage += costs[int(value)] * (1 if kind == 'start' else -1)
        assert 0 <= usage <= budget


def test_pause_keeps_computed_sample_without_starting_annotation(tmp_path, monkeypatch):
    from harness_bridge import control

    monkeypatch.setenv('OSP_COMPUTE_ENDPOINT', 'pool')
    monkeypatch.setattr(persample, 'plan_concurrency', lambda _: (1, 10, 1))
    monkeypatch.setattr(osp_dispatch, '_driver_estimate_bytes', lambda _: 1)
    marker = tmp_path / 'pause'
    monkeypatch.setattr(control, 'pause_requested', marker.exists)
    sleep = time.sleep
    monkeypatch.setattr(persample.time, 'sleep', lambda _: sleep(.02))
    script = ("import json,pathlib,sys; assert '--compute-only' in sys.argv; "
              "pathlib.Path(sys.argv[1]).write_text(json.dumps(dict(identity='s',state='computed'))); "
              "pathlib.Path(sys.argv[2]).touch()")
    entry = dict(value='s', identity='s', n_cells=7, outdir=str(tmp_path),
                 command=[sys.executable, '-c', script, L.RUN_STATE, str(marker)])
    with pytest.raises(control.PauseRequested):
        persample.drive([entry], tmp_path, True)
    assert read_json(tmp_path / L.RUN_STATE)['state'] == 'computed'
    assert not (tmp_path / 'failures.md').exists()

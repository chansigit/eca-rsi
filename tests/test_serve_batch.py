import json
from pathlib import Path

from ecarsi import index, serve


def test_paused_admission_is_consistent_and_preserves_queue_and_cell_counts(tmp_path, monkeypatch):
    from ecarsi import batch, workflow_web
    rows = [dict(id=str(i), name=str(i), attempt=1, output=str(tmp_path/str(i)),
                 mirror=str(tmp_path/str(i)/'rsi'), n_cells=100,
                 state='running' if i == 0 else 'queued',
                 queue_reason='Dataset dispatch is paused') for i in range(3)]
    path = tmp_path/'status.json'
    original = json.dumps(dict(datasets=rows, nodes={}))
    path.write_text(original)
    monkeypatch.delenv('ECA_PERISCOPE_BATCH_STATUS', raising=False)
    monkeypatch.setenv('ECA_DATASET_QUEUE', str(tmp_path))
    monkeypatch.setattr(batch, '_monitor_background', None)
    items = {r['name']: Path(r['mirror']) for r in rows}
    states = {name: (serve._dataset_state(p), p) for name, p in items.items()}
    assert [s['cls'] for s, _ in states.values()] == ['running', 'paused', 'paused']
    assert not any(r['waiting'] for r in batch.monitor()['datasets'])
    assert '2 Paused</span>' in serve._navigator_html(items, tmp_path/'registry.json')
    overview = serve._home_html(items)
    assert '>2</span><span class="k">Paused</span>' in overview
    assert '>0</span><span class="k">Queued</span>' in overview
    assert '1 running' in workflow_web.render() and '2 paused' in workflow_web.render()
    totals = serve.fleet_totals(serve.fleet_history(states))
    assert totals['cells_queued'] == 200 and totals['undated_input'] == 100
    assert path.read_text() == original  # presentation must never mutate admission
    for row in rows:
        row['queue_reason'] = 'Waiting for driver CPU/memory capacity'
    path.write_text(json.dumps(dict(datasets=rows, nodes={})))
    assert serve._dataset_state(items['1'])['cls'] == 'queued'
    assert batch.monitor()['datasets'][1]['waiting'] is True


def test_workflow_activity_shows_latest_fifteen_with_full_totals(monkeypatch):
    from ecarsi import workflow_web
    rows = [dict(name=f'dataset-{i:02}', output=f'/runs/{i}', state='running',
                 last_log_at=i+1, submitted_at=100-i) for i in range(20)]
    monkeypatch.setattr(workflow_web, 'monitor', lambda: {'datasets': rows})
    page = workflow_web.render()
    assert page.count('<tr><td>') == 15
    assert '20 running' in page and 'Showing latest 15 of 20' in page
    assert 'dataset-04' not in page
    assert page.index('dataset-19') < page.index('dataset-05')


def test_persistent_submission_supersedes_legacy_failure(tmp_path, monkeypatch):
    from ecarsi import batch
    legacy, current = tmp_path/'legacy.json', tmp_path/'status.json'
    row = dict(name='old',output=str(tmp_path/'working'),mirror=str(tmp_path/'rsi'),state='failed')
    legacy.write_text(json.dumps(dict(datasets=[row])))
    current.write_text(json.dumps(dict(datasets=[dict(row,state='queued')],nodes={})))
    monkeypatch.setenv('ECA_PERISCOPE_BATCH_STATUS',str(legacy))
    monkeypatch.setenv('ECA_DATASET_QUEUE',str(tmp_path))
    result = batch._read_monitor()
    assert len(result['datasets']) == 1
    assert result['datasets'][0]['state'] == 'queued'
    assert result['by_mirror'][row['mirror']]['state'] == 'queued'


def test_submitted_dataset_visible_before_output_exists(tmp_path, monkeypatch):
    study = tmp_path / 'chondroatlas' / '07_Swahnetal'
    (study / 'standardize').mkdir(parents=True)
    (study / 'standardize/result.json').write_text('{}')
    output = study / 'rsi'
    status = tmp_path / 'status.json'
    row = {'name': study.name, 'mirror': str(output), 'state': 'queued', 'n_cells': 184276}
    status.write_text(json.dumps({'datasets': [row]}))
    monkeypatch.setenv('ECA_PERISCOPE_BATCH_STATUS', str(status))
    registry = serve.Registry(tmp_path / 'registry.json')
    registry.bind('chondroatlas-07_Swahnetal', output)
    assert not output.exists()  # viewing a queue must not manufacture analysis outputs
    assert index.collection_of(output) == 'chondroatlas'
    assert serve._dataset_state(output)['stage'] == 'queued for driver'
    assert serve._dataset_state(output)['n_input'] == 184276
    assert '07_Swahnetal' in serve._navigator_html(registry.snapshot(), registry.path)
    assert 'waiting for a dataset driver' in index.render_root(output)
    assert serve._dataset_state(tmp_path / 'unrelated')['cls'] == 'failed'
    row['state'] = 'running'
    status.write_text(json.dumps({'datasets': [row]}))
    assert serve._dataset_state(output)['stage'] == 'organizing'
    row['state'] = 'paused'
    status.write_text(json.dumps({'datasets': [row]}))
    assert serve._dataset_state(output)['stage'] == 'queued for driver'
    (tmp_path / 'pause').touch()
    assert serve._dataset_state(output)['stage'] == 'paused'


def test_queued_and_running_counts_agree(tmp_path, monkeypatch):
    status = tmp_path / 'status.json'
    rows = [{'name': str(i), 'mirror': str(tmp_path / str(i) / 'rsi'),
             'state': 'running' if i == 0 else 'queued'} for i in range(5)]
    status.write_text(json.dumps({'datasets': rows}))
    monkeypatch.setenv('ECA_PERISCOPE_BATCH_STATUS', str(status))
    items = {r['name']: Path(r['mirror']) for r in rows}
    nav = serve._navigator_html(items, tmp_path / 'registry.json')
    overview = serve._home_html(items)
    assert '1 Running</span>' in nav and '4 Queued</span>' in nav
    assert 'working</span>' not in nav
    assert '>1</span><span class="k">Running</span>' in overview
    assert '>4</span><span class="k">Queued</span>' in overview

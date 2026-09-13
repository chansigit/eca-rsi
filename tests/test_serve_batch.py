import json
from pathlib import Path

from ecarsi import index, serve


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

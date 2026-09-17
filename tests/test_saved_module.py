"""A resumed stage rebuilds an agent request with the program it was saved with."""
import json

from ecarsi.control.persample import saved_module


def test_saved_module_keeps_v3_and_defaults_new(tmp_path):
    folder = tmp_path / 'requests' / 'run.agent-1'
    folder.mkdir(parents=True)
    (folder / 'request.json').write_text(json.dumps({'spec': {'args': ['-m', 'ecarsi.stages.zoomin_v3', 'agent', 'x']}}))
    assert saved_module(tmp_path, 'run.agent-1') == 'ecarsi.stages.zoomin_v3'
    assert saved_module(tmp_path, 'run.agent-2') is None

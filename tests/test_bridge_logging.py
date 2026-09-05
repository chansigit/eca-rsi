"""CLI logging must be visible without polluting stdout or duplicating lines."""
import subprocess
import sys

import pytest


@pytest.mark.parametrize('entry', ['cli', 'worker'])
def test_entry_logging_streams_and_repeat_initialization(entry):
    script = '''
import logging
from harness_bridge import ensure_logging
from ecarsi import __main__ as cli, osp_worker

def work(*args):
    ensure_logging('ecarsi', 'osp')
    logging.getLogger('harness_bridge.smoke').info('bridge-marker')
    logging.getLogger('ecarsi.smoke').info('rsi-marker')
    if ENTRY == 'worker':
        logging.getLogger('osp.smoke').info('osp-marker')
    print('{"ok": true}')
    return 0

if ENTRY == 'cli':
    cli.run = work
    for _ in range(2):
        assert cli.main(['run']) == 0
else:
    osp_worker.run = work
    for _ in range(2):
        assert osp_worker.main(['request.json']) == 0
'''
    result = subprocess.run([sys.executable, '-c', f'ENTRY = {entry!r}\n' + script],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ['{"ok": true}'] * 2
    assert result.stderr.count('bridge-marker') == 2
    assert result.stderr.count('rsi-marker') == 2
    assert result.stderr.count('osp-marker') == (2 if entry == 'worker' else 0)


def test_shim_exception_is_catchable_as_shared_exception():
    from ecarsi.harness import AgentIncompleteError
    from harness_bridge import AgentIncompleteError as SharedError
    with pytest.raises(SharedError):
        raise AgentIncompleteError('submit missing')

"""CLI logging must be visible without polluting stdout or duplicating lines."""
import subprocess
import sys

import pytest


def test_cli_logging_streams_and_repeat_initialization():
    script = '''
import logging, sys, types
from harness_bridge import ensure_logging
from ecarsi import __main__ as cli

def work(argv):
    ensure_logging('ecarsi')
    logging.getLogger('harness_bridge.smoke').info('bridge-marker')
    logging.getLogger('ecarsi.smoke').info('rsi-marker')
    print('{"ok": true}')
    return 0

sys.modules['smoke_command'] = types.SimpleNamespace(main=work)
cli.COMMANDS['smoke'] = 'smoke_command'
for _ in range(2):
    assert cli.main(['smoke']) == 0
'''
    result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ['{"ok": true}'] * 2
    assert result.stderr.count('bridge-marker') == 2
    assert result.stderr.count('rsi-marker') == 2


def test_shim_exception_is_catchable_as_shared_exception():
    from ecarsi.harness import AgentIncompleteError
    from harness_bridge import AgentIncompleteError as SharedError
    with pytest.raises(SharedError):
        raise AgentIncompleteError('submit missing')

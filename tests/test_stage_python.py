import json

import pytest

from ecarsi import stage_python


def test_cooperative_pause_exits_after_anyio_tool_thread():
    import os
    import subprocess
    import sys
    # A prior threaded tool followed by a pause inside gather reproduces the
    # production hang: the event loop closes before AnyIO's stop callback runs.
    code = '''
import asyncio, anyio, os, tempfile
from harness_bridge.control import pausable, safe_point
from ecarsi.runtime_logging import install
os.environ['ECA_STAGE_LOG_NAMESPACES'] = 'old_version'
install()
async def tool(): safe_point()
async def run():
    await anyio.to_thread.run_sync(lambda: 1)
    await asyncio.gather(tool(), tool())
@pausable
def main(): asyncio.run(run())
with tempfile.NamedTemporaryFile() as marker:
    os.environ['ECA_RSI_PAUSE_FILE'] = marker.name
    result = main()
    print('checkpoint pause returned', result, flush=True)
    raise SystemExit(result)
'''
    env = {k: v for k, v in os.environ.items() if k != 'ECA_RSI_CONTROL'}
    result = subprocess.run([sys.executable, '-P', '-c', code], env=env,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 3, result.stderr
    assert 'checkpoint pause returned 3' in result.stdout
    assert 'closed AnyIO thread' in result.stderr


def test_retained_runtime_logs_reach_cli_handler_once(monkeypatch):
    import io
    import logging
    from ecarsi.runtime_logging import install
    stream = io.StringIO()
    family = logging.getLogger('msp')
    previous = family.handlers[:], family.level
    logger = logging.getLogger('old_version.msp.annotate')
    monkeypatch.setenv('ECA_STAGE_LOG_NAMESPACES', 'old_version')
    try:
        family.handlers = [logging.StreamHandler(stream)]
        family.setLevel(logging.INFO)
        install()
        logger.info('accepted cluster checkpoint')
        assert stream.getvalue() == 'accepted cluster checkpoint\n'
    finally:
        family.handlers, family.level = previous


def test_published_namespace_preserves_cli_logger_family(tmp_path):
    from ecarsi.build_stage_runtime import MODULES, build
    for package, modules in MODULES.items():
        root = tmp_path/'source'/package
        root.mkdir(parents=True)
        for module in modules:
            (root/(module+'.py')).write_text('import logging\nlog = logging.getLogger(__name__)\n')
    bundle = build(tmp_path/'published', tmp_path/'source', tmp_path/'source', 'version1')
    for package in MODULES:
        context = {'__name__': 'version1.'+package+'.__main__'}
        exec((bundle/package/'__main__.py').read_text(), context)
        assert context['log'].name == package


def test_stage_adapter_keeps_existing_progress_and_checks_published_identity(tmp_path, monkeypatch):
    calls = []
    monkeypatch.delenv('ECA_DRIVER_LEASE_DIRECTORY', raising=False)
    monkeypatch.setattr(stage_python.runpy, 'run_module', lambda target, **kw: calls.append(target))
    monkeypatch.setattr(stage_python.sys, 'argv', [])
    # Hash only this tiny fixture, just as a published namespace hashes itself.
    source = tmp_path/'runtime'/'stage_python.py'
    source.parent.mkdir()
    source.write_text('original runtime')
    monkeypatch.setattr(stage_python, '__file__', str(source))
    old = tmp_path/'old'
    old.mkdir()
    (old/'annotation_progress.json').write_text('{}')
    stage_python.main(['-m', 'msp', '--outdir', str(old)])
    assert calls == ['msp.__main__']
    assert not (old/'.rsi-stage-runtime.json').exists()

    fresh = tmp_path/'fresh'
    stage_python.main(['-m', 'zmip', '--outdir', str(fresh)])
    record = fresh/'.rsi-stage-runtime.json'
    assert json.loads(record.read_text())['module'] == 'ecarsi.zmip.__main__'
    stage_python.main(['-m', 'zmip', '--outdir', str(fresh)])
    assert calls[-2:] == ['ecarsi.zmip.__main__'] * 2
    source.write_text('changed runtime')
    with pytest.raises(ValueError, match='orchestration changed'):
        stage_python.main(['-m', 'zmip', '--outdir', str(fresh)])

    # A retained sibling version handles its own receipt and checksum.
    old_runtime = source.parent.parent/'previous'
    old_runtime.mkdir()
    (old_runtime/'stage_python.py').touch()
    record.write_text(json.dumps({'module': 'previous.zmip.__main__', 'code_sha256': 'old'}))
    invoked = []
    def exec_old(executable, args):
        invoked.append(args)
        raise SystemExit(0)
    monkeypatch.setattr(stage_python.os, 'execv', exec_old)
    with pytest.raises(SystemExit):
        stage_python.main(['-m', 'zmip', '--outdir', str(fresh)])
    assert invoked[0][3] == 'previous.stage_python'

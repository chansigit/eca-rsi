"""Release directory transactions survive generation failures and process death."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

from ecarsi import release_state as R


def old_release(unit, previous):
    unit.mkdir(exist_ok=True)
    if previous:
        (unit / 'release').mkdir()
        (unit / 'release' / 'old.txt').write_text('old complete result')


def assert_old(unit, previous):
    assert (unit / 'release').exists() == previous
    if previous:
        assert sorted(p.name for p in (unit / 'release').iterdir()) == ['old.txt']
        assert (unit / 'release' / 'old.txt').read_text() == 'old complete result'
    assert not (unit / R.JOURNAL).exists()


@pytest.mark.parametrize('previous', [False, True])
def test_generation_exception_preserves_previous(tmp_path, previous):
    old_release(tmp_path, previous)
    with pytest.raises(ValueError, match='generation'):
        with R.publication(tmp_path) as stage:
            (stage / 'partial.txt').write_text('not complete')
            raise ValueError('generation failed')
    assert_old(tmp_path, previous)


@pytest.mark.parametrize('previous', [False, True])
def test_publication_commits_complete_new_directory(tmp_path, previous):
    old_release(tmp_path, previous)
    with R.publication(tmp_path) as stage:
        assert stage.parent.parent.parent == tmp_path
        assert (tmp_path / 'release').exists() == previous
        (stage / 'new.txt').write_text('new complete result')
    assert sorted(p.name for p in (tmp_path / 'release').iterdir()) == ['new.txt']
    assert not R.recover(tmp_path)


@pytest.mark.parametrize('previous', [False, True])
@pytest.mark.parametrize('after', [False, True])
@pytest.mark.parametrize('boundary', ['backup', 'publish'])
def test_process_death_around_each_directory_rename(tmp_path, previous, after, boundary):
    if not previous and boundary == 'backup':
        pytest.skip('first publication has no previous directory to back up')
    old_release(tmp_path, previous)
    script = '''
import os, sys
from pathlib import Path
from ecarsi import release_state as R
unit, boundary, after = Path(sys.argv[1]), sys.argv[2], sys.argv[3] == 'True'
replace = R.os.replace
def crash(source, target):
    match = (boundary == 'backup' and Path(target).name == 'old') or (boundary == 'publish' and Path(source).name == 'new')
    if match and not after:
        os._exit(73)
    result = replace(source, target)
    if match and after:
        os._exit(73)
    return result
R.os.replace = crash
with R.publication(unit) as stage:
    (stage / 'new.txt').write_text('new complete result')
'''
    env = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1])}
    result = subprocess.run([sys.executable, '-c', script, str(tmp_path), boundary, str(after)], env=env)
    assert result.returncode == 73
    assert R.recover(tmp_path)
    assert_old(tmp_path, previous)
    assert not R.recover(tmp_path)


@pytest.mark.parametrize('after', [False, True])
def test_rename_exception_rolls_back_before_return(tmp_path, monkeypatch, after):
    old_release(tmp_path, True)
    replace = R.os.replace
    triggered = False

    def fail(source, target):
        nonlocal triggered
        match = Path(source).name == 'new' and not triggered
        if match:
            triggered = True
            if not after:
                raise OSError('injected rename failure')
        result = replace(source, target)
        if match:
            raise OSError('injected rename failure')
        return result

    monkeypatch.setattr(R.os, 'replace', fail)
    with pytest.raises(OSError, match='injected'):
        with R.publication(tmp_path) as stage:
            (stage / 'new.txt').write_text('new complete result')
    assert_old(tmp_path, True)


def test_recovery_can_resume_after_restore_rename(tmp_path, monkeypatch):
    import json
    old_release(tmp_path, True)
    transaction = tmp_path / R.TRANSACTIONS / 'interrupted'
    transaction.mkdir(parents=True)
    os.replace(tmp_path / 'release', transaction / 'old')
    (tmp_path / 'release').mkdir()
    (tmp_path / 'release' / 'new.txt').write_text('uncommitted')
    (tmp_path / R.JOURNAL).write_text(json.dumps({
        'schema': 1, 'transaction': str(transaction.relative_to(tmp_path)), 'had_previous': True,
    }))
    replace = R.os.replace

    def fail_after_restore(source, target):
        result = replace(source, target)
        if Path(source).name == 'old':
            raise OSError('interrupted recovery')
        return result

    with monkeypatch.context() as patch:
        patch.setattr(R.os, 'replace', fail_after_restore)
        with pytest.raises(OSError, match='interrupted recovery'):
            R.recover(tmp_path)
    assert R.recover(tmp_path)
    assert_old(tmp_path, True)

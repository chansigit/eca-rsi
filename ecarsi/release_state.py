"""Crash-recoverable directory publication for a unit's release.

Callers hold the unit writer lock and populate the yielded staging directory,
including its completion receipt, before leaving ``publication``. A surviving
journal always rolls back to the previous release (or no release for the first
publication). Removing the journal commits the new directory. Pruning and any
subsequent receipt updates are separate caller-owned operations.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import tempfile

JOURNAL = '.rsi-release-publish.json'
TRANSACTIONS = '.rsi-release-transactions'


def _sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_journal(unit, record):
    path = unit / JOURNAL
    tmp = path.with_name(path.name + '.tmp')
    try:
        with tmp.open('w') as handle:
            json.dump(record, handle)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        _sync_dir(unit)
    finally:
        tmp.unlink(missing_ok=True)


def recover(unit):
    """Roll back an interrupted publication; safe to interrupt and call again.

    Returns True when a journal was recovered. Once the journal is absent,
    the visible release is committed and this function leaves it unchanged.
    The caller must serialize this with publication and other unit writers.
    """
    unit = Path(unit).resolve()
    journal = unit / JOURNAL
    if not journal.exists():
        return False
    record = json.loads(journal.read_text())
    relative = Path(record['transaction'])
    if (record.get('schema') != 1 or type(record.get('had_previous')) is not bool
            or relative.is_absolute() or len(relative.parts) != 2
            or relative.parts[0] != TRANSACTIONS or '..' in relative.parts):
        raise ValueError('invalid release publication journal')
    transaction = unit / relative
    if transaction.is_symlink() or transaction.parent.is_symlink():
        raise ValueError('release transaction must not be a symlink')
    release, backup = unit / 'release', transaction / 'old'
    discarded = transaction / 'discarded'
    if record['had_previous']:
        if backup.exists():
            if release.exists():
                # Rename, rather than recursively delete, the uncommitted tree.
                # After a crash here, backup still identifies the old release.
                os.replace(release, discarded)
                _sync_dir(unit)
                _sync_dir(transaction)
            os.replace(backup, release)
            _sync_dir(unit)
            _sync_dir(transaction)
        elif not release.is_dir():
            raise RuntimeError('previous release missing during publication recovery')
        # No backup plus an existing release means the old directory either
        # never moved or was restored by an earlier interrupted recovery.
    elif release.exists():
        os.replace(release, discarded)
        _sync_dir(unit)
        _sync_dir(transaction)
    journal.unlink()
    _sync_dir(unit)
    shutil.rmtree(transaction, ignore_errors=True)
    return True


@contextmanager
def publication(unit):
    """Yield an empty release directory and commit it on successful exit.

    Generation failures leave the previous release untouched. Publication
    failures recover the previous directory before propagating the exception.
    Process termination is recovered by ``recover(unit)`` at the next entry.
    """
    unit = Path(unit).resolve()
    unit.mkdir(parents=True, exist_ok=True)
    recover(unit)
    release = unit / 'release'
    if release.is_symlink() or (release.exists() and not release.is_dir()):
        raise ValueError('release must be a real directory')
    parent = unit / TRANSACTIONS
    if parent.is_symlink():
        raise ValueError('release transaction directory must not be a symlink')
    parent.mkdir(exist_ok=True)
    transaction = Path(tempfile.mkdtemp(prefix='publish-', dir=parent))
    stage = transaction / 'new'
    stage.mkdir()
    try:
        yield stage
        _write_journal(unit, {'schema': 1, 'transaction': str(transaction.relative_to(unit)),
                              'had_previous': release.exists()})
        if release.exists():
            os.replace(release, transaction / 'old')
            _sync_dir(unit)
            _sync_dir(transaction)
        os.replace(stage, release)
        _sync_dir(unit)
        _sync_dir(transaction)
        (unit / JOURNAL).unlink()  # Commit point: recovery now keeps the new release.
        _sync_dir(unit)
    except BaseException:
        recover(unit)
        raise
    finally:
        # A surviving journal owns its backup until recovery succeeds.
        if not (unit / JOURNAL).exists():
            shutil.rmtree(transaction, ignore_errors=True)

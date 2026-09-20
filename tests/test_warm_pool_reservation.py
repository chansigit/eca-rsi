"""The Slurm-allocation memory ledger shared by the driver and worker launchers."""
import pytest


def test_worker_reclaims_retired_driver_cpu_locks_only_after_queue_drains(tmp_path, monkeypatch):
    import fcntl
    import json
    from pathlib import Path
    from ecarsi.warm_pool.reservation import reserve

    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    root = tmp_path/'.cache/ecarsi-pool/host'
    root.mkdir(parents=True)
    queue = tmp_path/'queue'
    queue.mkdir()
    old = dict(role='driver', memory=4, cpu_ids=[1], owner_lock=str(root/'driver.lock'),
               queue_directory=str(queue), nodes=['old'], observed_at=1)
    ledger = root/'budget-1.json'
    ledger.write_text(json.dumps({'driver:old': old}))
    (queue/'status.json').write_text(json.dumps({'datasets': []}))
    profile = dict(host='host', job_id='1', cpu_ids=[1], memory=4, allocation_memory=8)
    with (root/'cpu-1.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        reserve(profile, 'worker:new', root/'cpu-1.lock', role='worker', owned_cpu_locks=True)
        assert set(json.loads(ledger.read_text())) == {'worker:new'}
        ledger.write_text(json.dumps({'driver:old': old}))
        (queue/'status.json').write_text(json.dumps({'datasets': [{'node': 'old', 'state': 'running'}]}))
        with pytest.raises(ValueError, match='overlap'):
            reserve(profile, 'worker:new', root/'cpu-1.lock', role='worker', owned_cpu_locks=True)

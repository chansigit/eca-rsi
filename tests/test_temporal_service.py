import asyncio
import time

import pytest

from ecarsi.temporal_service import endpoint
from ecarsi.warm_pool.state import save


def test_discovery_rejects_stale_or_stopped_owner(tmp_path):
    record = dict(state='ready', observed_at=time.time(), generation='first', endpoint='127.0.0.1:7333')
    save(tmp_path / 'service.json', record)
    assert endpoint(tmp_path)['generation'] == 'first'
    for changes in ({'observed_at': time.time() - 31}, {'state': 'stopped'}, {'observed_at': time.time() + 60}):
        save(tmp_path / 'service.json', record | changes)
        with pytest.raises(ConnectionError):
            endpoint(tmp_path)


def test_coordinator_reconnects_without_resubmitting_work(monkeypatch):
    from ecarsi import temporal_service, work_coordinator as coordinator

    async def scenario():
        generation = 'first'
        entered = asyncio.Event()
        connected = []
        stopped = []

        def discover(root):
            return dict(generation=generation, endpoint=generation)

        async def connect(address):
            return address

        async def worker(client, queue):
            connected.append(client)
            entered.set()
            try:
                await asyncio.Future()
            finally:
                stopped.append(client)

        real_wait = asyncio.wait

        async def fast_wait(tasks, *, timeout):
            return await real_wait(tasks, timeout=0.01)

        monkeypatch.setattr(temporal_service, 'endpoint', discover)
        monkeypatch.setattr(coordinator.Client, 'connect', connect)
        monkeypatch.setattr(coordinator, 'run_worker', worker)
        monkeypatch.setattr(coordinator.asyncio, 'wait', fast_wait)
        following = asyncio.create_task(coordinator.follow_service('shared', 'queue'))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            entered.clear()
            generation = 'second'
            await asyncio.wait_for(entered.wait(), 1)
            assert connected == ['first', 'second']
            assert stopped == ['first']
        finally:
            following.cancel()
            await asyncio.gather(following, return_exceptions=True)
        assert stopped == ['first', 'second']

    asyncio.run(scenario())

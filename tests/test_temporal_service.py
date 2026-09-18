import asyncio
import time

import pytest

from ecarsi.control.temporal import endpoint
from ecarsi.warm_pool.state import save


@pytest.mark.parametrize('command', ['status-dataset', 'worker'])
def test_only_client_commands_disable_sdk_worker_heartbeats(monkeypatch, command):
    import sys
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock
    import temporalio.runtime
    import ecarsi.control.coordinator as coordinator
    handle = SimpleNamespace(id='dataset/test',
        describe=AsyncMock(return_value=SimpleNamespace(status=SimpleNamespace(name='RUNNING'))),
        query=AsyncMock(return_value='testing'))
    client = SimpleNamespace(get_workflow_handle=lambda identity: handle)
    connect = AsyncMock(return_value=client)
    runtime = Mock()
    monkeypatch.setattr(temporalio.runtime, 'Runtime', runtime)
    monkeypatch.setattr(coordinator.Client, 'connect', connect)
    monkeypatch.setattr(coordinator, 'run_worker', AsyncMock())
    monkeypatch.setattr(sys, 'argv', ['coordinator', '--temporal', 'localhost:7333', command]
                        + (['test'] if command == 'status-dataset' else []))
    asyncio.run(coordinator.main())
    if command == 'worker':
        runtime.assert_not_called()
        assert connect.call_args.kwargs['runtime'] is None
    else:
        assert runtime.call_args.kwargs['worker_heartbeat_interval'] is None
        assert connect.call_args.kwargs['runtime'] is runtime.return_value


def test_discovery_rejects_stale_or_stopped_owner(tmp_path):
    record = dict(state='ready', observed_at=time.time(), generation='first', endpoint='127.0.0.1:7333')
    save(tmp_path / 'service.json', record)
    assert endpoint(tmp_path)['generation'] == 'first'
    for changes in ({'observed_at': time.time() - 31}, {'state': 'stopped'}, {'observed_at': time.time() + 60}):
        save(tmp_path / 'service.json', record | changes)
        with pytest.raises(ConnectionError):
            endpoint(tmp_path)


def test_coordinator_reconnects_without_resubmitting_work(monkeypatch):
    import ecarsi.control.temporal as temporal_service
    import ecarsi.control.coordinator as coordinator

    async def scenario():
        generation = 'first'
        entered = asyncio.Event()
        connected = []
        stopped = []

        def discover(root):
            return dict(generation=generation, endpoint=generation)

        async def connect(address):
            return address

        async def worker(client, queue, workflow_slots):
            assert workflow_slots == 3
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
        following = asyncio.create_task(coordinator.follow_service('shared', 'queue', 3))
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

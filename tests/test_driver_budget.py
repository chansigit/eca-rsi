import json
import time
from ecarsi.batch import assign, capacity
from ecarsi.driver_budget import settle, reserved
from ecarsi.run_state import write_json


def test_matrix_budget_uses_array_shape_not_compressed_file_size(tmp_path):
    import h5py
    from ecarsi.driver_budget import matrix_working_bytes
    path = tmp_path/'matrix.h5ad'
    with h5py.File(path, 'w') as f:
        f.create_dataset('X', shape=(2000,2000), dtype='f4', compression='gzip')
    assert path.stat().st_size < 1024*1024
    assert matrix_working_bytes(path) == 2**30 + 8*2000*2000*4 + 3*2000*2000*8


def test_stage_work_uses_current_tree_plus_its_own_matrix_budget(tmp_path):
    now, g = time.time(), 2**30
    node = dict(id='n', cpus=2, cpu_ids=[0,1], memory=16*g, memory_headroom=16*g,
                observed_at=now, end_time=now+7200)
    row = dict(id='a', attempt=1, node='n', state='running', cpu_ids=[0], memory_gb=12,
               memory_lending_protocol=1, reserved_memory_bytes=3*g, rss_bytes=g, updated_at=now)
    root = tmp_path/'memory-leases/a-1'
    write_json(root/'request.json', dict(token='stage',mode='full',work_memory_bytes=2*g))
    config = dict(directory=str(tmp_path),driver_model_memory_margin_gb=1)
    settle(dict(nodes={'n':node},datasets=[row]), config, now)
    assert reserved(row) == 4*g and row['memory_state'] == 'active'
    write_json(root/'request.json', dict(token='outer',mode='full'))
    settle(dict(nodes={'n':node},datasets=[row]), config, now)
    assert reserved(row) == 12*g


def test_model_wait_lends_memory_but_reserves_a_way_back(tmp_path):
    g = 2**30
    now = time.time()
    node = dict(id='n', cpus=8, cpu_ids=list(range(8)), memory=20*g,
                memory_headroom=20*g, observed_at=now, end_time=now+7200)
    a = dict(id='a', attempt=1, state='running', node='n', cpus=1, cpu_ids=[0],
             memory_gb=8, rss_bytes=g, driver_pid=123, updated_at=now, memory_lending_protocol=1)
    b = dict(id='b', state='running', node='n', cpu_ids=[1], memory_gb=8)
    c = dict(id='c', attempt=0, state='queued', cpus=1, memory_gb=4, hours=1)
    d = dict(id='d', attempt=0, state='queued', cpus=1, memory_gb=4, hours=1)
    s = dict(nodes={'n': node}, datasets=[a, b, c, d])
    config = dict(directory=str(tmp_path), max_cpu_percent=90)
    request = tmp_path/'memory-leases/a-1/request.json'
    write_json(request, dict(token='park', mode='compact'))
    assign(s, config, now)
    assert reserved(a) == 3*g
    assert c['state'] == 'assigned' and d['state'] == 'queued'
    assert capacity(node, s['datasets'], now)[1] == 5*g
    write_json(request, dict(token='compute', mode='full'))
    settle(s, config, now)
    assert reserved(a) == 8*g and a['memory_state'] == 'active'
    assert capacity(node, s['datasets'], now)[1] == 0
    assert json.loads(request.with_name('grant.json').read_text())['token'] == 'compute'
    # Stale/unknown process measurements cannot release a reservation.
    a['driver_pid'] = None
    write_json(request, dict(token='unknown', mode='compact'))
    settle(s, config, now)
    assert reserved(a) == 8*g


def test_preparation_agent_wait_lends_memory_and_restores_before_resuming(tmp_path, monkeypatch):
    import asyncio
    import threading
    from ecarsi import agent_retry

    now, g = time.time(), 2**30
    node = dict(id='n', cpus=2, cpu_ids=[0, 1], memory=3*g,
                memory_headroom=3*g, observed_at=now, end_time=now+7200)
    row = dict(id='a', attempt=1, state='running', node='n', cpu_ids=[0],
               memory_gb=10, admission_memory_gb=2, admission_phase='preparation',
               rss_bytes=g//2, driver_pid=123, updated_at=now,
               memory_lending_protocol=1, reserved_memory_bytes=2*g)
    state = dict(nodes={'n': node}, datasets=[row])
    config = dict(directory=str(tmp_path), driver_model_memory_margin_gb=1)
    directory = tmp_path/'memory-leases/a-1'
    monkeypatch.setenv('ECA_DRIVER_LEASE_DIRECTORY', str(directory))
    entered = threading.Event()
    resumed = threading.Event()

    async def call():
        entered.set()
        while reserved(row) == 2*g:
            await asyncio.sleep(.01)
        return 17

    def actor():
        assert agent_retry.run_with_retry(call, label='test model') == 17
        resumed.set()

    child = threading.Thread(target=actor, daemon=True)
    child.start()
    assert entered.wait(3)
    assert json.loads((directory/'request.json').read_text())['mode'] == 'compact'
    settle(state, config, time.time())
    assert reserved(row) == 3*g//2
    assert not resumed.wait(.2)  # a returned model decision is not yet permission to compute
    request = json.loads((directory/'request.json').read_text())
    assert request['mode'] == 'full'
    settle(state, config, time.time())
    write_json(tmp_path/'status.json', state)
    child.join(3)
    assert resumed.is_set() and reserved(row) == 2*g


def test_failed_agent_retry_still_restores_full_budget(tmp_path, monkeypatch):
    import pytest
    from ecarsi import agent_retry, driver_budget

    calls = []
    monkeypatch.setenv('ECA_DRIVER_LEASE_DIRECTORY', str(tmp_path/'memory-leases/a-1'))
    monkeypatch.setattr(driver_budget, '_request', lambda _, mode, **kw: calls.append((mode, kw['wait'])))
    monkeypatch.setattr(agent_retry, 'MAX_ATTEMPTS', 2)
    monkeypatch.setattr(agent_retry.time, 'sleep', lambda _: None)

    async def fail():
        raise RuntimeError('model unavailable')

    with pytest.raises(RuntimeError, match='model unavailable'):
        agent_retry.run_with_retry(fail, label='test model')
    assert calls == [('compact', False), ('full', True)]


def test_actor_waits_for_durable_queue_grant_and_nested_work(tmp_path, monkeypatch):
    import threading
    from ecarsi.driver_budget import work
    root = tmp_path/'q'
    directory = root/'memory-leases/a-1'
    directory.mkdir(parents=True)
    write_json(root/'status.json', {'datasets': []})
    monkeypatch.setenv('ECA_DRIVER_LEASE_DIRECTORY', str(directory))
    entered = threading.Event()
    def actor():
        with work():
            with work():
                entered.set()
    child = threading.Thread(target=actor, daemon=True)
    child.start()
    deadline = time.monotonic()+5
    while not (directory/'request.json').exists():
        assert time.monotonic() < deadline
        time.sleep(.01)
    request = json.loads((directory/'request.json').read_text())
    write_json(directory/'grant.json', request)
    assert not entered.wait(.35)  # ack before queue commit grants no resources
    write_json(root/'status.json', {'datasets': [{'memory_grant_token': request['token'], 'memory_state': 'active'}]})
    child.join(3)
    assert entered.is_set() and not child.is_alive()
    assert json.loads((directory/'request.json').read_text())['mode'] == 'compact'


def test_repeated_ack_does_not_rewrite_but_repairs_lost_ack(tmp_path, monkeypatch):
    from ecarsi import driver_budget as budget
    writes = []
    original = budget.write_json
    def record(path, value):
        writes.append(value)
        original(path, value)
    monkeypatch.setattr(budget, 'write_json', record)
    request = dict(token='first', mode='compact')
    for _ in range(3):
        budget.acknowledge(tmp_path, request)
    assert writes == [request]
    (tmp_path/'grant.json').unlink()
    budget.acknowledge(tmp_path, request)
    budget.acknowledge(tmp_path, dict(token='second', mode='full'))
    assert len(writes) == 3
    assert json.loads((tmp_path/'grant.json').read_text()) == dict(token='second', mode='full')


def test_node_adopts_computation_already_using_return_window(tmp_path, monkeypatch):
    import pytest
    from ecarsi import batch
    from ecarsi.pool import budget
    g = 2**30
    profile = dict(id='n', host='test', job_id=None, cpu_ids=[0, 1], memory=12*g, allocated_tres='cpu=2')
    with batch.queue(tmp_path) as state:
        state['nodes']['n'] = profile
        state['datasets'] = [dict(id=str(i), attempt=1, node='n', state='running', cpu_ids=[i],
            memory_gb=8, memory_lending_protocol=1, reserved_memory_bytes=amount*g)
            for i, amount in enumerate((8, 3))]
    monkeypatch.setattr(batch, 'configuration', lambda _: {'directory': str(tmp_path)})
    def adopted(*args, **kwargs):
        raise RuntimeError('adoption reached resource reservation')
    monkeypatch.setattr(budget, 'reserve', adopted)
    with pytest.raises(RuntimeError, match='adoption reached'):
        batch.run_node_agent(tmp_path/'config', 12, 2, profile, 'n', tmp_path)

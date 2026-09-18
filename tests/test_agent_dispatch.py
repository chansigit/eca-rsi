"""Real SDK + local HTTP provider + worker subprocesses; no provider monkeypatches."""
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import pytest

import ecarsi.agent as bridge
import ecarsi.agent.dispatch as dispatch
import ecarsi.agent.session as session
from ecarsi.warm_pool.state import immutable, reference, verified
from ecarsi.warm_pool.state import save, read, status


def test_portable_upgrade_pins_execution_without_rewriting_session(tmp_path, monkeypatch):
    from tests.test_agent_session import setup, Client, ScriptedModel
    from harness_bridge import _harness_openai
    spec, ref = setup(tmp_path)
    root = Path(spec['bridge_root'])
    save(root/'config.json', dict(read(root/'config.json'), pool_root=spec['pool_root']))
    old = tmp_path/'old.py'
    old.write_bytes(Path(session.__file__).read_bytes() + b'\n# previous release\n')
    archived = session.archive_adapter(root, old)
    saved = read(ref['path'])
    save(ref['path'], dict(saved, protocol=2, adapter_sha256=archived['sha256']))
    ref = reference(ref['path']); before = Path(ref['path']).read_bytes()
    turn = session.submit_turn(ref, 0)
    bridge.serve(root, once=True)
    plan = bridge.status(root, turn)['attempts'][0]['plan']
    upgraded = verified(plan)['portable_adapter']
    assert upgraded['sha256'] != archived['sha256']
    monkeypatch.setattr(_harness_openai, '_client', lambda *a: Client())
    monkeypatch.setattr(_harness_openai, '_model', lambda *a: ScriptedModel())
    monkeypatch.setattr(dispatch, 'load_worker_key', lambda *a: None)
    monkeypatch.chdir(tmp_path)
    dispatch.execute(plan['path'])
    assert read(tmp_path/'result.json')['outcome'] == 'success'
    assert Path(ref['path']).read_bytes() == before
    assert verified(plan)['portable_adapter'] == upgraded
    Path(upgraded['path']).chmod(0o600)
    Path(upgraded['path']).write_text('raise AssertionError("unverified code")')
    monkeypatch.setattr(dispatch, 'load_worker_key', lambda *a: pytest.fail('No provider access'))
    dispatch.execute(plan['path'])
    assert read(tmp_path/'result.json')['outcome'] == 'local_error'


def test_recovery_ignores_only_terminal_model_attempts_with_an_accepted_replacement(tmp_path):
    from tests.test_agent_session import setup, completed_tool
    from ecarsi.warm_pool.state import cancel
    from ecarsi.control.persample import sample_step
    spec, _ = setup(tmp_path)
    root = Path(spec['bridge_root'])
    config = read(root / 'config.json')
    save(root / 'config.json', dict(config, pool_root=spec['pool_root']))
    spec = dict(spec, session_id='recovery', output_root=str(tmp_path / 'recovery'),
        trace=dict(workflow_id='persample/test', dataset_id=spec['dataset_id'],
                   unit_id='osp.annotate', sample_id='sample'))
    ref = session.create_session(spec)
    turn = session.submit_turn(ref, 0)
    bridge.serve(root, once=True)
    first = bridge.status(root, turn)['attempts'][0]['pool_request_id']
    completed_tool(spec, dict(request_id=first), dict(outcome='timeout', elapsed_seconds=5))
    bridge.serve(root, once=True)
    second = bridge.status(root, turn)['attempts'][-1]['pool_request_id']
    assert second != first
    cancel(spec['pool_root'], first)
    assert not dispatch.completed_replacement(spec['pool_root'], first, root)
    completed_tool(spec, dict(request_id=second), dict(outcome='success', response={'kind': 'tools'}, worker={}))
    bridge.serve(root, once=True)
    assert dispatch.completed_replacement(spec['pool_root'], first, root)
    assert sample_step('recoverable', [dict(spec, run_id='test'), 'sample'])
    assert not dispatch.completed_replacement(spec['pool_root'], second, root)
    # A cancellation without its terminal process receipt is still uncertain.
    p = Path(spec['pool_root']) / 'requests' / first
    receipt = p / read(p / 'request.json')['attempt_id'] / 'receipt.json'
    before = read(receipt); receipt.unlink()
    assert not dispatch.completed_replacement(spec['pool_root'], first, root)
    save(receipt, before)
    # Cancelling the accepted replacement removes recovery eligibility too.
    cancel(spec['pool_root'], second)
    assert not dispatch.completed_replacement(spec['pool_root'], first, root)
    assert not sample_step('recoverable', [dict(spec, run_id='test'), 'sample'])


@pytest.mark.parametrize('outcome', ['timeout', 'incomplete_submission'])
def test_audited_retry_survives_dispatch_restart_and_preserves_failed_attempt(tmp_path, outcome):
    import pytest
    from tests.test_agent_session import setup, completed_tool
    spec, ref = setup(tmp_path)
    root = Path(spec['bridge_root'])
    config = read(root / 'config.json')
    config.update(pool_root=spec['pool_root'], routing=dict(max_attempts=1))
    save(root / 'config.json', config)
    saved = read(ref['path']); saved['protocol'] = 2
    save(ref['path'], saved); ref = reference(ref['path'])
    request_id = session.submit_turn(ref, 0)
    bridge.serve(root, once=True)
    attempt = bridge.status(root, request_id)['attempts'][0]
    completed_tool(spec, dict(request_id=attempt['pool_request_id']),
                   dict(outcome=outcome, elapsed_seconds=5))
    bridge.serve(root, once=True)
    folder = root / 'requests' / request_id
    failure = read(folder / 'result.json')
    assert failure['state'] == 'failed'
    assert bridge.retry_turn(root, request_id, reason='deadline corrected')['state'] == 'queued'
    assert read(folder / 'result.json') == failure  # Durable intent, before archival.
    bridge.serve(root, once=True)
    recovered = bridge.status(root, request_id)
    assert recovered['state'] == 'running' and recovered['attempt_limit'] == 2
    assert len(recovered['attempts']) == 2 and recovered['attempts'][0] == attempt
    assert list(folder.glob('failed-result-*.json'))
    assert verified(recovered['recovery'])['result'] == failure
    with pytest.raises(ValueError, match='Only failed'):
        bridge.retry_turn(root, request_id, reason='do not duplicate a running attempt')


def test_continuations_advance_older_sessions_without_starving_aged_requests(tmp_path):
    queued = []
    for identity, first, submitted in [('old', 10, 990), ('new', 900, 950), ('aged', 20, 50)]:
        session_path = tmp_path / (identity + '.json')
        save(session_path, dict(spec=dict(session_id=identity)))
        initial = tmp_path / 'requests' / (identity + '.turn-0')
        initial.mkdir(parents=True)
        save(initial / 'request.json', dict(submitted_at=first))
        folder = tmp_path / 'requests' / (identity + '.turn-1')
        folder.mkdir()
        save(folder / 'request.json', dict(spec=dict(session=dict(path=str(session_path)))))
        queued.append((submitted, folder))
    order = dispatch.queue_order(tmp_path, queued, 100, {}, now=1000)
    assert [p.name for _, p in order] == ['old.turn-1', 'aged.turn-1', 'new.turn-1']
    fair = dispatch.queue_order(tmp_path, queued, 100, {}, now=1000, offset=3)
    assert next(fair)[1].name == 'aged.turn-1'
    assert len(list(fair)) == 2  # No duplicate dispatch from the two priority lists.
    # A long queue must not force every continuation back behind all older turns.
    all_aged = [(100, queued[0][1]), (50, queued[1][1]), (80, queued[2][1])]
    assert next(dispatch.queue_order(tmp_path, all_aged, 100, {}, now=1000))[1].name == 'old.turn-1'
    # A newly ready downstream operation receives a turn even with many older
    # sample sessions. The scheduler uses trace kinds, never dataset names.
    downstream = tmp_path / 'requests/downstream'
    downstream.mkdir()
    save(downstream / 'request.json', dict(spec=dict(operation_id='agent.turn',
        trace=dict(unit_id='cross-sample.inclusion'))))
    combined = list(dispatch.queue_order(tmp_path, queued + [(999, downstream)], 100, {}, now=1000))
    assert downstream in [p for _, p in combined[:2]]
    assert len({p for _, p in combined}) == 4


def test_local_session_validation_does_not_poison_model_health(tmp_path, monkeypatch):
    from tests.test_agent_session import setup
    spec, ref = setup(tmp_path)
    request_id = session.submit_turn(ref, 0)
    root = Path(spec['bridge_root'])
    request = read(root/'requests'/request_id/'request.json')
    plan = tmp_path/'plan.json'
    save(plan, dict(request=request, model=read(ref['path'])['model'], timeout_seconds=5,
                    adapter_sha256=session.file_digest(Path(session.__file__))))
    save(ref['path'], dict(read(ref['path']), adapter_sha256='0'*64))
    monkeypatch.chdir(tmp_path)
    dispatch.execute(plan)
    result = read(tmp_path/'result.json')
    assert result['outcome'] == 'local_error' and result['error'] == 'ValueError'


def test_credential_timeout_retries_without_calling_or_penalizing_provider(tmp_path, monkeypatch):
    from tests.test_agent_session import setup, completed_tool
    spec, _ = setup(tmp_path)
    root = Path(spec['bridge_root'])
    save(root/'config.json', dict(read(root/'config.json'), pool_root=spec['pool_root']))
    spec = dict(spec, session_id='setup-retry', output_root=str(tmp_path/'setup-retry'))
    ref = session.create_session(spec)
    turn = session.submit_turn(ref, 0);bridge.serve(root, once=True)
    first = bridge.status(root, turn)['attempts'][0]
    def fail_setup(*args):
        raise subprocess.TimeoutExpired('credential shell', 30)
    monkeypatch.setattr(dispatch, 'load_worker_key', fail_setup)
    monkeypatch.chdir(tmp_path)
    dispatch.execute(first['plan']['path'])
    result = read(tmp_path/'result.json')
    assert result['outcome'] == 'worker_setup_timeout' and result['response'] is None
    completed_tool(spec, dict(request_id=first['pool_request_id']), result)
    bridge.serve(root, once=True)
    observed = bridge.status(root, turn)
    assert len(observed['attempts']) == 2 and observed['state'] == 'running'
    events = [read(p) for p in (root/'model-events').glob('*.json')]
    assert len(events) == 1 and events[0]['model_failure'] is False


def test_free_text_cannot_finish_a_session_requiring_submission(tmp_path, monkeypatch):
    from tests.test_agent_session import setup, completed_tool
    spec, _ = setup(tmp_path)
    root = Path(spec['bridge_root'])
    save(root/'config.json', dict(read(root/'config.json'), pool_root=spec['pool_root']))
    spec = dict(spec, session_id='contract', output_root=str(tmp_path/'contract'), completion_tool='compute')
    ref = session.create_session(spec);turn = session.submit_turn(ref, 0)
    bridge.serve(root, once=True);attempt = bridge.status(root, turn)['attempts'][0]
    async def premature(*args, **kwargs):
        return dict(kind='final', final_output='I am done', calls=[])
    monkeypatch.setattr(session, 'run_turn', premature)
    monkeypatch.setattr(dispatch, 'load_worker_key', lambda *a: None)
    monkeypatch.chdir(tmp_path);dispatch.execute(attempt['plan']['path'])
    result = read(tmp_path/'result.json')
    assert result['outcome'] == 'incomplete_submission'
    completed_tool(spec, dict(request_id=attempt['pool_request_id']), result)
    bridge.serve(root, once=True)
    assert not (root/'requests'/turn/'result.json').exists()
    assert len(bridge.status(root, turn)['attempts']) == 2


def test_retry_waits_for_untried_backup_before_reusing_failed_primary(tmp_path):
    from tests.test_agent_session import setup
    spec, ref = setup(tmp_path)
    root=Path(spec['bridge_root']); saved=read(ref['path'])
    saved['protocol']=2; save(ref['path'],saved); ref=reference(ref['path'])
    primary=saved['model']; backup=dict(primary,model='backup')
    config=read(root/'config.json'); config['pool_root']=spec['pool_root']
    save(config['catalog'],dict(models=[primary,backup])); save(root/'config.json',config)
    request_id=session.submit_turn(ref,0); folder=root/'requests'/request_id
    save(folder/'state.json',dict(state='queued',attempts=[dict(model=primary)]))
    (root/'model-events').mkdir()
    for i in range(2):
        dispatch.record_event(root,folder,dict(pool_request_id=f'backup-timeout-{i}',model=backup),'timeout')
    bridge.serve(root,once=True)
    # Primary has free capacity, but retrying it now would burn the bounded
    # attempt budget without ever trying the configured fallback.
    assert bridge.status(root,request_id)['state']=='queued'
    assert not list((Path(spec['pool_root'])/'requests').iterdir())


def test_worker_timeout_fallback_continuation_and_dispatcher_recovery(tmp_path):
    calls = []
    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            calls.append(body)
            if body['model'] == 'slow':
                time.sleep(7)
            messages = body['messages']
            if not any(m['role'] == 'tool' for m in messages):
                message = dict(role='assistant', content=None, tool_calls=[dict(id='compute-once', type='function',
                    function=dict(name='compute', arguments='{"value":7}'))])
            else:
                assert any('worker-result' in str(m) for m in messages)
                message = dict(role='assistant', content='Accepted worker result')
            payload = json.dumps(dict(id='response', object='chat.completion', created=1, model=body['model'],
                choices=[dict(index=0, finish_reason='tool_calls' if message.get('tool_calls') else 'stop', message=message)],
                usage=dict(prompt_tokens=10, completion_tokens=4, total_tokens=14))).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            try:
                self.wfile.write(payload)
            except BrokenPipeError:
                pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        catalog = tmp_path/'models.json'
        models = [dict(harness='openai@vllm', model=name, url=f'http://127.0.0.1:{server.server_port}/v1')
                  for name in ('slow', 'backup', 'third')]
        save(catalog, dict(models=models))
        pool = tmp_path/'pool'; pool.mkdir(mode=0o700); (pool/'requests').mkdir()
        save(pool/'config.json', dict(runtime=dict(command=[sys.executable], files={}, version='test')))
        root = bridge.init(tmp_path/'bridge', catalog, concurrency=2, pool_root=pool)
        config = read(root/'config.json')
        config['routing'] = dict(response_timeout_seconds=5, failure_threshold=1, cooldown_seconds=60, model_concurrency=1)
        save(root/'config.json', config)
        spec = dict(session_id='worker-agent', dataset_id='data', prompt='Call compute and report.', max_turns=4,
                    pool_root=str(pool), bridge_root=str(root), output_root=str(tmp_path/'session'),
                    tools=[dict(name='compute', description='Compute on a worker',
                    parameters=dict(type='object', properties=dict(value=dict(type='integer')), required=['value'], additionalProperties=False),
                    args=['-c', 'raise AssertionError("not on model caller")', '{arguments}'],
                    cpus=1, memory_mb=64, timeout_seconds=30, inputs=[], outputs=['result.json'], result_file='result.json')])
        ref = session.create_session(spec)
        # Select Chat in the durable session, without changing any real deployment config.
        saved = read(ref['path']); saved['api_mode']='chat_completions'; save(ref['path'], saved); ref=reference(ref['path'])
        request_id = session.submit_turn(ref, 0)
        bridge.serve(root, once=True)
        first = bridge.status(root, request_id)['attempts'][0]
        assert len(list((pool/'requests').iterdir())) == 1 and not calls
        # Restart before the worker starts; queue time is not provider time.
        bridge.serve(root, once=True)
        assert len(list((pool/'requests').iterdir())) == 1 and not calls
        # HQ may have an uncertain submission before a Worker accepts it.
        # No startup clock exists yet, and one such task must not stop Bridge.
        backend = pool/'requests'/first['pool_request_id']/'backend.json'
        save(backend, dict(state='unknown_external_result'))
        bridge.serve(root, once=True)
        assert status(pool, first['pool_request_id'])['accepted'] is None
        assert len(bridge.status(root, request_id)['attempts']) == 1
        save(backend, dict(state='queued'))

        def execute(attempt):
            request = read(pool/'requests'/attempt['pool_request_id']/'request.json')
            folder = pool/'requests'/attempt['pool_request_id']/request['attempt_id']
            output = folder/'outputs'
            env = dict(os.environ, VLLM_API_KEY='local-test-placeholder')
            subprocess.run([sys.executable, '-m', 'ecarsi.agent.dispatch', 'execute', attempt['plan']['path']],
                           cwd=output, env=env, check=True, timeout=20)
            result = read(output/'result.json')
            assert result['worker']['pid'] != os.getpid()
            save(folder/'receipt.json', dict(state='succeeded', started_at=1, finished_at=time.time(),
                outputs=[dict(**reference(output/'result.json'), size=(output/'result.json').stat().st_size)]))
            return result

        request = read(pool/'requests'/first['pool_request_id']/'request.json')
        accepted = pool/'requests'/first['pool_request_id']/request['attempt_id']/'accepted.json'
        save(accepted, dict(started_at=time.time() - 30))
        assert status(pool, first['pool_request_id'])['state'] == 'unknown_external_result'
        bridge.serve(root, once=True)
        assert len(bridge.status(root, request_id)['attempts']) == 1
        assert not list((root/'model-events').glob('*.json'))
        assert execute(first)['outcome'] == 'timeout'
        bridge.serve(root, once=True)
        attempts = bridge.status(root, request_id)['attempts']
        assert len(attempts) == 2 and attempts[1]['model']['model'] == 'backup'
        event = next(read(p) for p in (root/'model-events').glob('*.json'))
        assert event['finished_at'] == status(pool, first['pool_request_id'])['receipt']['finished_at']
        # A config edit must not mutate the already submitted task's budget.
        config['routing']['worker_memory_mb'] = 1536; save(root/'config.json', config)
        bridge.serve(root, once=True)
        assert execute(attempts[1])['outcome'] == 'success'
        bridge.serve(root, once=True)
        reply = root/'requests'/request_id/'result.json'
        assert read(reply)['response']['model']['model'] == 'backup'
        health = read(root/'summary.json')['models']
        assert health[0]['state'] == 'cooling_down' and health[0]['timeouts'] == 1
        # Native tool boundary is preserved, with exactly one accepted tool invocation.
        item = session.tool_request(ref, reply, 0)
        request = read(pool/'requests'/item['request_id']/'request.json')
        folder = pool/'requests'/item['request_id']/request['attempt_id']
        save(folder/'outputs/result.json', {'value': 'worker-result'})
        save(folder/'receipt.json', dict(state='succeeded', started_at=1, finished_at=time.time(),
             outputs=[dict(**reference(folder/'outputs/result.json'), size=24)]))
        context = session.continuation(ref, reply, [dict(item, path=str(folder/'outputs/result.json'))])
        # Remove the previous provider model; continuation must carry accepted tool output to another model.
        save(catalog, dict(models=[models[2]]))
        next_id = session.submit_turn(ref, 1, context, [item['request_id']])
        bridge.serve(root, once=True)
        attempt = bridge.status(root, next_id)['attempts'][0]
        assert execute(attempt)['outcome'] == 'success'
        bridge.serve(root, once=True)
        assert bridge.status(root, next_id)['response']['kind'] == 'final'
        assert bridge.status(root, next_id)['response']['usage_total']['tokens_in'] == 20
        # Repeated service replacement neither repeats calls nor double-counts timeouts.
        bridge.serve(root, once=True)
        assert len(calls) == 3 and len(list((root/'model-events').glob('*.json'))) == 3
        # Completed replies are immutable; cancellation fences pending and late attempts.
        assert bridge.cancel(root, next_id)['state'] == 'reply_saved'
        pending_ref = session.create_session(dict(spec, session_id='cancel-active', output_root=str(tmp_path/'cancel-active')))
        pending = session.submit_turn(pending_ref, 0)
        bridge.serve(root, once=True)
        attempt = bridge.status(root, pending)['attempts'][-1]
        assert bridge.cancel(root, pending)['reason'] == 'cancelled'
        folder = root/'requests'/pending
        before = read(folder/'result.json')
        dispatch.reconcile_pool(root, folder, read(root/'config.json'), {})
        assert dispatch.dispatch(root, folder, read(root/'config.json'), models[2]) is None
        assert read(folder/'result.json') == before
        assert (pool/'requests'/attempt['pool_request_id']/'cancel.json').is_file()
        queued_ref = session.create_session(dict(spec, session_id='cancel-queued', output_root=str(tmp_path/'cancel-queued')))
        queued = session.submit_turn(queued_ref, 0)
        assert bridge.cancel(root, queued)['reason'] == 'cancelled'
        bridge.serve(root, once=True)
        assert not bridge.status(root, queued).get('attempts')

    finally:
        server.shutdown(); server.server_close(); thread.join()


def test_model_cooldown_expiry_and_capacity():
    model = dict(harness='openai@vllm', model='one', url='http://localhost/v1')
    event = dict(model=model, outcome='timeout', finished_at=100)
    settings = dispatch.DEFAULT_POLICY | dict(failure_threshold=1, cooldown_seconds=20, model_concurrency=1)
    assert dispatch.model_health({'a':event}, [model], Counter(), settings, now=110)[0]['state'] == 'cooling_down'
    assert dispatch.model_health({'a':event}, [model], Counter(), settings, now=121)[0]['state'] == 'ready'
    assert dispatch.model_health({}, [model], Counter({dispatch.model_key(model):1}), settings, now=121)[0]['state'] == 'busy'
    recovered = dict(model=model, outcome='success', finished_at=105)
    health = dispatch.model_health({'a': event, 'b': recovered}, [model], Counter(), settings, now=110)[0]
    assert health['state'] == 'ready' and health['cooldown_until'] is None
    later = dict(event, finished_at=108)
    assert dispatch.model_health({'a': event, 'b': recovered, 'c': later}, [model], Counter(), settings, now=110)[0]['state'] == 'cooling_down'



def test_health_cache_keeps_per_model_admission_limits(tmp_path):
    from tests.test_agent_session import setup
    spec, ref = setup(tmp_path)
    root = Path(spec['bridge_root'])
    config = read(root / 'config.json')
    primary = read(ref['path'])['model']
    save(config['catalog'], dict(models=[primary, dict(primary, model='backup')]))
    save(root / 'config.json', dict(config, pool_root=spec['pool_root'], concurrency=8,
                                   routing=dict(model_concurrency=1)))
    requests = []
    for i in range(5):
        current = session.create_session(dict(spec, session_id='cache-' + str(i),
            output_root=str(tmp_path / ('cache-' + str(i)))))
        requests.append(session.submit_turn(current, 0))
    for _ in range(2):
        bridge.serve(root, once=True)
        states = [bridge.status(root, request) for request in requests]
        assert Counter(s['state'] for s in states) == {'running': 2, 'queued': 3}
        assert sorted(s['attempts'][0]['model']['model'] for s in states if s['state'] == 'running') == sorted([primary['model'], 'backup'])


def test_timeline_uses_worker_attempt_and_preserves_dependencies():
    from ecarsi.observatory import task_timeline
    trace = dict(dataset_id='data', workflow_id='workflow', unit_id='organize.plan')
    model = dict(id='model-worker', submitted_at=1, trace=trace, state='succeeded')
    tool = dict(id='tool', submitted_at=2, trace=dict(trace, depends_on=['bridge-turn']), state='succeeded')
    inbox = dict(id='bridge-turn', submitted_at=1, trace=trace, pool_attempts=['model-worker'])
    tasks = task_timeline([model, tool], [inbox], 0, 3)['tasks']
    assert [t['id'] for t in tasks] == ['model-worker', 'tool']
    assert tasks[1]['trace']['depends_on'] == ['model-worker']


def test_invalid_unused_dispatch_snapshot_recovers_without_relaxing_session_pin(tmp_path, monkeypatch):
    import pytest
    from tests.test_agent_session import setup, completed_tool
    import hashlib
    spec, ref = setup(tmp_path)
    root = Path(spec['bridge_root'])
    save(root/'config.json', dict(read(root/'config.json'), pool_root=spec['pool_root']))
    spec = dict(spec, session_id='snapshot-retry', output_root=str(tmp_path/'snapshot-retry'))
    ref = session.create_session(spec)
    turn = session.submit_turn(ref, 0)
    bad = b'if True:\ninvalid indentation\n'
    sha = hashlib.sha256(bad).hexdigest()
    snapshot = root/'adapters'/(sha+'.py');snapshot.write_bytes(bad)
    with monkeypatch.context() as m:
        m.setattr(session, 'archive_adapter', lambda *a: reference(snapshot))
        m.setattr(dispatch, 'enqueue', lambda *a: None)
        bridge.serve(root, once=True)
    state = bridge.status(root, turn); attempt = state['attempts'][0]
    plan_path = Path(attempt['plan']['path'])
    # Recreate the historical plan, before explicit portable upgrades existed.
    historical = read(plan_path); historical.pop('portable_adapter', None)
    save(plan_path, historical)
    state['attempts'][0]['plan'] = reference(plan_path)
    save(root/'requests'/turn/'state.json', state)
    dispatch.enqueue(root/'requests'/turn, read(root/'config.json'), state['attempts'][0])
    completed_tool(spec, dict(request_id=attempt['pool_request_id']),
                   dict(outcome='local_error', error='IndentationError', response=None))
    assert dispatch.invalid_dispatch_snapshot(state)
    bridge.serve(root, once=True)
    assert bridge.retry_turn(root, turn, reason='Validated session adapter; unused snapshot was invalid')['state'] == 'queued'
    async def good(*args, **kwargs):
        return dict(kind='final', final_output='complete', calls=[])
    monkeypatch.setattr(session, 'run_turn', good)
    monkeypatch.setattr(dispatch, 'load_worker_key', lambda *a: None)
    monkeypatch.chdir(tmp_path);dispatch.execute(plan_path)
    assert read(tmp_path/'result.json')['outcome'] == 'success'
    saved = read(ref['path']);saved['adapter_sha256'] = sha
    save(ref['path'], saved)
    with pytest.raises(ValueError, match='Artifact changed'):
        dispatch.invalid_dispatch_snapshot(state)


def test_a_turn_folder_recreated_under_an_archived_name_is_served_again(tmp_path):
    """Eye 2026-09-17: recovery archived turn-0, the resume re-created it, and a name-keyed
    settled cache in the long-running bridge hid it for 14 hours."""
    import shutil
    from tests.test_agent_session import setup, completed_tool
    spec, _ = setup(tmp_path)
    root = Path(spec['bridge_root'])
    save(root / 'config.json', dict(read(root / 'config.json'), pool_root=spec['pool_root']))
    ref = session.create_session(spec)
    turn = session.submit_turn(ref, 0)
    cache = {}
    bridge.serve(root, once=True, finished=cache)
    first = bridge.status(root, turn)['attempts'][0]['pool_request_id']
    completed_tool(spec, dict(request_id=first), dict(outcome='success', response={'kind': 'final', 'text': 'done'}, worker={}))
    bridge.serve(root, once=True, finished=cache)
    assert bridge.status(root, turn)['state'] == 'reply_saved'
    folder, archived = root / 'requests' / turn, root / 'archived-requests' / turn
    archived.parent.mkdir()
    folder.rename(archived)
    folder.mkdir()
    shutil.copy(archived / 'request.json', folder / 'request.json')
    assert bridge.status(root, turn)['state'] == 'queued'
    bridge.serve(root, once=True, finished=cache)
    assert bridge.status(root, turn)['attempts'], 'the re-created turn was hidden by the settled cache'
    assert bridge.status(root, turn)['attempts'][0]['pool_request_id'] == first  # same content, same reply


def test_a_turn_recreated_with_different_content_gets_its_own_pool_request(tmp_path, monkeypatch):
    """Eye 2026-09-17, second recovery: the session id derives from the phase inputs, so the re-created
    session had the same turn folder names and the pool replayed 34 archived v2 replies."""
    from tests.test_agent_session import setup
    spec, _ = setup(tmp_path)
    root = Path(spec['bridge_root'])
    save(root / 'config.json', dict(read(root / 'config.json'), pool_root=spec['pool_root']))
    ref = session.create_session(spec)
    monkeypatch.setattr(dispatch, 'enqueue', lambda *a: None)
    turn = session.submit_turn(ref, 0)
    bridge.serve(root, once=True)
    first = bridge.status(root, turn)['attempts'][0]['pool_request_id']
    folder = root / 'requests' / turn
    folder.rename(root / 'archived-turn-0')
    assert session.submit_turn(ref, 0, parents=('elsewhere',)) == turn
    bridge.serve(root, once=True)
    assert bridge.status(root, turn)['attempts'][0]['pool_request_id'] != first

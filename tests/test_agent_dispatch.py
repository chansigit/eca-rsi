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

from ecarsi import agent_bridge as bridge, agent_dispatch as dispatch, agent_session as session
from ecarsi.warm_pool.state import save, read, status


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
        saved = read(ref['path']); saved['api_mode']='chat_completions'; save(ref['path'], saved); ref=session.reference(ref['path'])
        request_id = session.submit_turn(ref, 0)
        bridge.serve(root, once=True)
        first = bridge.status(root, request_id)['attempts'][0]
        assert len(list((pool/'requests').iterdir())) == 1 and not calls
        # Restart before the worker starts; queue time is not provider time.
        bridge.serve(root, once=True)
        assert len(list((pool/'requests').iterdir())) == 1 and not calls

        def execute(attempt):
            request = read(pool/'requests'/attempt['pool_request_id']/'request.json')
            folder = pool/'requests'/attempt['pool_request_id']/request['attempt_id']
            output = folder/'outputs'
            env = dict(os.environ, VLLM_API_KEY='local-test-placeholder')
            subprocess.run([sys.executable, '-m', 'ecarsi.agent_dispatch', 'execute', attempt['plan']['path']],
                           cwd=output, env=env, check=True, timeout=20)
            result = read(output/'result.json')
            assert result['worker']['pid'] != os.getpid()
            save(folder/'receipt.json', dict(state='succeeded', started_at=1, finished_at=time.time(),
                outputs=[dict(**session.reference(output/'result.json'), size=(output/'result.json').stat().st_size)]))
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
             outputs=[dict(**session.reference(folder/'outputs/result.json'), size=24)]))
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


def test_timeline_uses_worker_attempt_and_preserves_dependencies():
    from ecarsi.dev_observatory import task_timeline
    trace = dict(dataset_id='data', workflow_id='workflow', unit_id='organize.plan')
    model = dict(id='model-worker', submitted_at=1, trace=trace, state='succeeded')
    tool = dict(id='tool', submitted_at=2, trace=dict(trace, depends_on=['bridge-turn']), state='succeeded')
    inbox = dict(id='bridge-turn', submitted_at=1, trace=trace, pool_attempts=['model-worker'])
    tasks = task_timeline([model, tool], [inbox], 0, 3)['tasks']
    assert [t['id'] for t in tasks] == ['model-worker', 'tool']
    assert tasks[1]['trace']['depends_on'] == ['model-worker']

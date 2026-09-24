"""Resident runner: the bridge routes a turn to a live runner, which performs it in-process; a
runner that vanishes before starting the turn costs one attempt and the next goes to the pool."""
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import sys
import threading
import time

import ecarsi.agent as bridge
import ecarsi.agent.session as session
from ecarsi.agent.dispatch import model_key
from ecarsi.agent.runner import serve_runner
from ecarsi.warm_pool.state import read, reference, save


class Provider(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        message = dict(role='assistant', content='A plain final answer')
        payload = json.dumps(dict(id='response', object='chat.completion', created=1, model=body['model'],
            choices=[dict(index=0, finish_reason='stop', message=message)],
            usage=dict(prompt_tokens=10, completion_tokens=4, total_tokens=14))).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(payload)


def test_runner_performs_routed_turns_and_a_lost_runner_falls_back_to_the_pool(tmp_path, monkeypatch):
    monkeypatch.setenv('VLLM_API_KEY', 'local-test-placeholder')
    server = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        catalog = tmp_path / 'models.json'
        model = dict(harness='openai@vllm', model='resident', url=f'http://127.0.0.1:{server.server_port}/v1')
        save(catalog, dict(models=[model]))
        pool = tmp_path / 'pool'; pool.mkdir(mode=0o700); (pool / 'requests').mkdir()
        save(pool / 'config.json', dict(runtime=dict(command=[sys.executable], files={}, version='test')))
        root = bridge.init(tmp_path / 'bridge', catalog, concurrency=4, pool_root=pool)
        config = read(root / 'config.json')
        config['routing'] = dict(response_timeout_seconds=20, model_concurrency=8)
        config['service'] = dict(models='all', stale_seconds=5)
        save(root / 'config.json', config)
        spec = dict(session_id='resident-agent', dataset_id='data', prompt='Answer briefly.', max_turns=2,
                    pool_root=str(pool), bridge_root=str(root), output_root=str(tmp_path / 'session'),
                    tools=[dict(name='compute', description='Compute on a worker',
                    parameters=dict(type='object', properties=dict(value=dict(type='integer')), required=['value'], additionalProperties=False),
                    args=['-c', 'raise AssertionError("not on model caller")', '{arguments}'],
                    cpus=1, memory_mb=64, timeout_seconds=30, inputs=[], outputs=['result.json'], result_file='result.json')])
        ref = session.create_session(spec)
        saved = read(ref['path']); saved['api_mode'] = 'chat_completions'; save(ref['path'], saved); ref = reference(ref['path'])
        key = model_key(model)
        beat = root / 'runners' / (key + '.json')
        beat.parent.mkdir()
        save(beat, dict(pid=1, generation='g1', observed_at=time.time(), in_flight=0, done=0, draining=False))

        first_turn = session.submit_turn(ref, 0)
        bridge.serve(root, once=True)
        attempt = bridge.status(root, first_turn)['attempts'][0]
        assert attempt['execution'] == 'service' and attempt['runner'] == key and 'pool_request_id' not in attempt
        assert not list((pool / 'requests').iterdir())
        marker = root / 'runner-queue' / key / (attempt['turn_id'] + '.json')
        assert marker.is_file()

        # The runner takes the marker, performs the turn in its own loop and leaves the result behind.
        assert asyncio.run(serve_runner(root, model, once=True)) == 0
        assert not marker.exists()
        result = read(root / 'turns' / attempt['turn_id'] / 'result.json')
        assert result['outcome'] == 'success' and result['worker']['pid'] == os.getpid()
        bridge.serve(root, once=True)
        state = bridge.status(root, first_turn)
        assert state['state'] == 'reply_saved' and state['turn_id'] == attempt['turn_id']
        assert read(beat)['done'] == 1 and read(beat)['draining'] is True

        # A runner that dies before starting the turn: stale heartbeat -> worker_lost -> the next attempt
        # is a pool task, because no runner is ready any more.
        second = session.create_session(dict(spec, session_id='resident-agent-2', output_root=str(tmp_path / 'session-2')))
        saved = read(second['path']); saved['api_mode'] = 'chat_completions'; save(second['path'], saved)
        second_turn = session.submit_turn(reference(second['path']), 0)
        save(beat, dict(pid=1, generation='g2', observed_at=time.time(), in_flight=0, done=0, draining=False))
        bridge.serve(root, once=True)
        lost = bridge.status(root, second_turn)['attempts'][0]
        assert lost['execution'] == 'service'
        save(beat, dict(read(beat), observed_at=time.time() - 60))
        bridge.serve(root, once=True)
        attempts = bridge.status(root, second_turn)['attempts']
        assert len(attempts) == 2 and 'pool_request_id' in attempts[1] and (pool / 'requests' / attempts[1]['pool_request_id']).is_dir()
        assert not (root / 'runner-queue' / key / (lost['turn_id'] + '.json')).exists()
        events = [read(p) for p in (root / 'model-events').glob('*.json')]
        assert sorted(e['outcome'] for e in events) == ['success', 'worker_lost']
    finally:
        server.shutdown()

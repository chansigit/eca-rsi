"""Real process recovery with a synthetic provider; never sends model requests."""
import multiprocessing
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

from ecarsi import agent_bridge as bridge


FAKE = '''
import os, time
from pathlib import Path
from ecarsi import agent_bridge as b
def provider(request, **kwargs):
    spec = request['spec']
    with (Path(spec['cwd']) / (spec['request_id'] + '.calls')).open('a') as f:
        f.write('called\\n'); f.flush(); os.fsync(f.fileno())
    time.sleep(1.5)
    return {'synthetic': True, 'route': os.environ['AGENT_MODEL_POOL']}
b.run_organize = provider
b.execute(*__import__('sys').argv[1:])
'''


def fake_service(root, once=False):
    original = subprocess.Popen

    def launch(argv, **kwargs):
        return original([sys.executable, '-c', FAKE, argv[-2], argv[-1]], **kwargs)

    with patch.object(bridge.subprocess, 'Popen', side_effect=launch):
        return bridge.serve(root, once=once)


def wait_for(predicate):
    end = time.monotonic() + 15
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(.05)
    raise AssertionError('Timed out waiting for test condition')


class DurableBridgeTest(unittest.TestCase):
    def test_terminal_history_is_cached_but_uncertain_calls_are_revisited(self):
        from collections import Counter
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            catalog = base / 'models.json'
            bridge.save(catalog, {'models': []})
            root = bridge.init(base / 'bridge', catalog, concurrency=1)
            spec = dict(operation_id='organize.plan', cwd=str(base),
                        profiles=[{'name': 'test', 'h5ad': '/test-only/input.h5ad'}])
            for n in range(20):
                name = 'old-' + str(n)
                bridge.submit(root, dict(spec, request_id=name))
                bridge.save(root / 'requests' / name / 'result.json', {'state': 'reply_saved'})
            for name in ('lost', 'waiting'):
                bridge.submit(root, dict(spec, request_id=name))
            bridge.save(root / 'requests/lost/state.json', {'state': 'unknown_external_result'})
            real_status, reads, ticks, launched = bridge.status, Counter(), [], []
            def inspect(root, name):
                reads[name] += 1
                return real_status(root, name)
            def launch(root, folder, catalog):
                launched.append(folder.name)
                bridge.save(folder / 'result.json', {'state': 'reply_saved'})
                return SimpleNamespace(poll=lambda: None)
            def tick(seconds):
                ticks.append(seconds)
                if len(ticks) == 1:
                    self.assertFalse(launched)  # Uncertain execution still owns its slot.
                    bridge.save(root / 'requests/lost/proposal.json', {'synthetic': 'late reply'})
                else:
                    raise RuntimeError('end test')
            with patch.object(bridge, 'status', side_effect=inspect), patch.object(bridge, 'launch', side_effect=launch), \
                    patch.object(bridge.time, 'sleep', side_effect=tick), self.assertRaisesRegex(RuntimeError, 'end test'):
                bridge.serve(root)
            self.assertEqual(launched, ['waiting'])
            self.assertTrue(all(reads['old-' + str(n)] == 2 for n in range(20)))
            self.assertEqual(bridge.read(root / 'summary.json')['counts'], {'reply_saved': 22})

    def test_existing_planner_adapter_preserves_prompt_and_usage(self):
        from ecarsi import plan
        proposal = {'analysis_units': [{'name': 'unit', 'members': [
            {'source': 'source', 'obs_filter': None}], 'rationale': 'test',
            'batch_key_hint': None}], 'notes': 'test', 'sample_mapping': {
                'source': {'sample_column': None, 'confirmed_single': True, 'rationale': 'fixture'}}}
        response = SimpleNamespace(submitted=proposal, transcript_text='test reply',
                                   effective_config=None, tokens_in=12, tokens_out=None, cost_usd=None)
        request = {'brief': 'Saved English prompt', 'spec': {
            'cwd': '/tmp', 'profiles': [{'name': 'source', 'h5ad': '/test-only.h5ad',
                                       'species': 'mouse'}]}}
        with patch.object(plan, 'run_agent', new=AsyncMock(return_value=response)) as agent:
            result = bridge.run_organize(request)
        self.assertTrue(agent.call_args.kwargs['prompt'].startswith(request['brief']))
        self.assertEqual(agent.call_args.kwargs['cwd'], '/tmp')
        self.assertEqual(result['plan'], proposal)
        self.assertEqual(result['usage'], {'tokens_in': 12, 'tokens_out': None, 'cost_usd': None})

    def test_recovery_and_bounded_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            catalog = base / 'models.json'
            bridge.save(catalog, {'models': [{'harness': 'claude', 'model': 'test-model'}]})
            root = bridge.init(base / 'bridge', catalog, concurrency=2)
            for name in ('a', 'b', 'c'):
                spec = dict(request_id=name, operation_id='organize.plan', cwd=str(base),
                            profiles=[{'name': 'test', 'h5ad': '/test-only/input.h5ad'}])
                self.assertEqual(bridge.submit(root, spec)['state'], 'queued')
                self.assertEqual(bridge.submit(root, spec)['state'], 'queued')
                with self.assertRaises(ValueError):
                    bridge.submit(root, dict(spec, operation_id='changed'))
            ctx = multiprocessing.get_context('spawn')
            service = ctx.Process(target=fake_service, args=(root,))
            service.start()
            try:
                wait_for(lambda: (base / 'a.calls').exists() and (base / 'b.calls').exists())
                self.assertFalse((base / 'c.calls').exists())
                # There can be only one dispatcher, including across replacement processes.
                with self.assertRaises(BlockingIOError):
                    bridge.serve(root, once=True)
                os.kill(service.pid, signal.SIGKILL)
                service.join(5)
                fake_service(root, once=True)
                self.assertEqual(bridge.status(root, 'a')['state'], 'running')
                self.assertFalse((base / 'c.calls').exists())
                wait_for(lambda: all(bridge.status(root, name)['state'] == 'reply_saved'
                                    for name in ('a', 'b')))
                # New dispatches use the edited catalog; in-flight requests kept their route.
                bridge.save(catalog, {'models': [{'harness': 'claude', 'model': 'next-model'}]})
                children = fake_service(root, once=True)
                wait_for(lambda: bridge.status(root, 'c')['state'] == 'reply_saved')
                for child in children.values():
                    self.assertEqual(child.wait(timeout=5), 0)
                fake_service(root, once=True)
                for name in ('a', 'b', 'c'):
                    self.assertEqual((base / (name + '.calls')).read_text(), 'called\n')
                self.assertEqual(bridge.status(root, 'a')['response']['route'], 'claude:test-model')
                self.assertEqual(bridge.status(root, 'c')['response']['route'], 'claude:next-model')
                self.assertEqual(bridge.read(root / 'summary.json')['counts'], {'reply_saved': 3})
            finally:
                if service.is_alive():
                    service.kill()
                service.join(5)
                # Test executors are bounded; let any survivors release their temp files.
                time.sleep(2)

    def test_uncertain_execution_never_retries(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            catalog = base / 'models.json'
            bridge.save(catalog, {'models': []})
            root = bridge.init(base / 'bridge', catalog, concurrency=1)
            spec = dict(request_id='lost', operation_id='organize.plan', cwd=str(base),
                        profiles=[{'name': 'test', 'h5ad': '/test-only/input.h5ad'}])
            bridge.submit(root, spec)
            bridge.submit(root, dict(spec, request_id='waiting'))
            folder = root / 'requests/lost'
            bridge.save(folder / 'state.json', {'state': 'running'})
            with patch.object(bridge, 'launch', side_effect=AssertionError('unknown still owns capacity')) as launcher:
                bridge.serve(root, once=True)
                launcher.assert_not_called()
            self.assertEqual(bridge.status(root, 'lost')['state'], 'unknown_external_result')
            self.assertEqual(bridge.status(root, 'waiting')['state'], 'queued')
            with patch.object(bridge, 'run_organize', side_effect=AssertionError('must not call')):
                bridge.execute(root, 'lost')
                bridge.submit(root, spec)
                bridge.serve(root, once=True)
            self.assertEqual(bridge.status(root, 'lost')['state'], 'unknown_external_result')

    def test_saved_result_is_reusable_and_missing_usage_stays_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            catalog = base / 'models.json'
            bridge.save(catalog, {'models': []})
            root = bridge.init(base / 'bridge', catalog)
            spec = dict(request_id='saved', operation_id='organize.plan', cwd=str(base),
                        trace={'workflow_id': 'organize/saved', 'dataset_id': 'dataset-a',
                               'unit_id': 'organize.plan'},
                        profiles=[{'name': 'test', 'h5ad': '/test-only/input.h5ad'}])
            bridge.submit(root, spec)
            self.assertEqual(bridge.read(root / 'requests/saved/request.json')['spec']['trace']['dataset_id'], 'dataset-a')
            bridge.save(root / 'requests/saved/state.json', {'state': 'running'})
            with patch.object(bridge, 'run_organize', return_value={'usage': None}) as provider:
                bridge.execute(root, 'saved')
                bridge.execute(root, 'saved')
                provider.assert_called_once()
            self.assertIsNone(bridge.status(root, 'saved')['response']['usage'])

    def test_confirmed_remote_stop_releases_capacity_without_retrying_lost_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            catalog = base / 'models.json'
            bridge.save(catalog, {'models': [{'harness': 'claude', 'model': 'test-model'}]})
            root = bridge.init(base / 'bridge', catalog, concurrency=1)
            spec = dict(request_id='lost', operation_id='organize.plan', cwd=str(base),
                        profiles=[{'name': 'test', 'h5ad': '/test-only/input.h5ad'}])
            bridge.submit(root, spec)
            bridge.submit(root, dict(spec, request_id='waiting'))
            folder = root / 'requests/lost'
            bridge.save(folder / 'state.json', {'state': 'unknown_external_result'})
            fake_service(root, once=True)
            summary = bridge.read(root / 'summary.json')
            self.assertEqual((summary['running'], summary['unresolved'], summary['available']), (0, 1, 0))
            with bridge.lock(folder / 'execution.lock'):
                with self.assertRaises(BlockingIOError):
                    bridge.confirm_stopped(root, 'lost', reason='Synthetic confirmed stop')
            resolved = bridge.confirm_stopped(root, 'lost', reason='Synthetic provider confirms no execution remains')
            self.assertEqual(resolved['state'], 'failed')
            self.assertTrue(bridge.read(folder / 'resolution.json')['remote_stopped'])
            with patch.object(bridge, 'run_organize', side_effect=AssertionError('must not retry lost call')):
                bridge.execute(root, 'lost')
            children = fake_service(root, once=True)
            for child in children.values():
                self.assertEqual(child.wait(timeout=10), 0)
            self.assertFalse((base / 'lost.calls').exists())
            self.assertEqual((base / 'waiting.calls').read_text(), 'called\n')
            self.assertEqual(bridge.status(root, 'waiting')['state'], 'reply_saved')

    def test_late_saved_reply_automatically_resolves_uncertain_outcome(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            catalog = base / 'models.json'
            bridge.save(catalog, {'models': []})
            root = bridge.init(base / 'bridge', catalog, concurrency=1)
            bridge.submit(root, dict(request_id='lost', operation_id='organize.plan', cwd=str(base),
                                     profiles=[{'name': 'test', 'h5ad': '/test-only/input.h5ad'}]))
            folder = root / 'requests/lost'
            bridge.save(folder / 'state.json', {'state': 'unknown_external_result'})
            bridge.save(folder / 'proposal.json', {'synthetic': 'saved reply'})
            with patch.object(bridge, 'launch', side_effect=AssertionError('must reuse saved reply')):
                bridge.serve(root, once=True)
            self.assertEqual(bridge.status(root, 'lost')['state'], 'reply_saved')
            self.assertEqual(bridge.read(root / 'summary.json')['available'], 1)

    def test_proposal_survives_sdk_teardown_failure(self):
        from ecarsi import plan
        import json
        proposal = {'analysis_units': [{'name': 'unit', 'members': [
            {'source': 'source', 'obs_filter': None}], 'rationale': 'test',
            'batch_key_hint': None}], 'notes': 'test', 'sample_mapping': {
                'source': {'sample_column': None, 'confirmed_single': True, 'rationale': 'fixture'}}}

        async def backend(**kwargs):
            await kwargs['tools'][0].handler({'plan_json': json.dumps(proposal)})
            raise TimeoutError('synthetic SDK teardown failure after submission')

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            catalog = base / 'models.json'
            bridge.save(catalog, {'models': []})
            root = bridge.init(base / 'bridge', catalog)
            bridge.submit(root, dict(request_id='saved', operation_id='organize.plan', cwd=str(base),
                                     profiles=[{'name': 'source', 'h5ad': '/test-only.h5ad', 'species': 'mouse'}]))
            bridge.save(root / 'requests/saved/state.json', {'state': 'running'})
            with patch.object(plan, 'run_agent', side_effect=backend) as agent:
                bridge.execute(root, 'saved')
                bridge.execute(root, 'saved')
                agent.assert_called_once()
            self.assertEqual(bridge.status(root, 'saved')['response']['plan'], proposal)
            self.assertEqual(bridge.status(root, 'saved')['state'], 'reply_saved')


if __name__ == '__main__':
    unittest.main()

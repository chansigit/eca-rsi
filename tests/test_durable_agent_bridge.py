"""Durable bridge records: recovery and operator confirmation, never a model request.

The pool-less dispatcher these tests once drove through real processes is gone (the bridge always
runs with --pool-root); pool dispatch and retry are covered in test_agent_dispatch.py."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

import ecarsi.agent as bridge


class DurableBridgeTest(unittest.TestCase):
    def test_sample_validation_needs_no_science_packages(self):
        import subprocess
        import sys
        subprocess.run([sys.executable, "-c", '''
import sys
class NoScience:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'numpy', 'pandas', 'anndata', 'scanpy'}:
            raise AssertionError('Control validation imported ' + fullname)
sys.meta_path.insert(0, NoScience())
from ecarsi.plan import validate_sample_mapping
profile = {'name': 'source', 'obs_columns': {'sample': {'n_unique': 2, 'n_na': 0}}}
plan = {'sample_mapping': {'source': {'sample_column': 'sample', 'rationale': 'library IDs'}}}
validate_sample_mapping(plan, [profile])
profile['obs_columns']['sample']['n_na'] = 1
try:
    validate_sample_mapping(plan, [profile])
except ValueError as exc:
    assert 'leaves 1 cells NA' in str(exc)
else:
    raise AssertionError('Incomplete partition was accepted')
'''], check=True)

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

    def test_confirmed_remote_stop_resolves_an_unknown_outcome_without_a_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            catalog = base / 'models.json'
            bridge.save(catalog, {'models': []})
            root = bridge.init(base / 'bridge', catalog, concurrency=1)
            bridge.submit(root, dict(request_id='lost', operation_id='organize.plan', cwd=str(base),
                                     profiles=[{'name': 'test', 'h5ad': '/test-only/input.h5ad'}]))
            folder = root / 'requests/lost'
            bridge.save(folder / 'state.json', {'state': 'unknown_external_result'})
            with bridge.lock(folder / 'execution.lock'):
                with self.assertRaises(BlockingIOError):
                    bridge.confirm_stopped(root, 'lost', reason='Synthetic confirmed stop')
            resolved = bridge.confirm_stopped(root, 'lost', reason='Synthetic provider confirms no execution remains')
            self.assertEqual(resolved['state'], 'failed')
            self.assertTrue(bridge.read(folder / 'resolution.json')['remote_stopped'])

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
            bridge.reconcile(folder)
            self.assertEqual(bridge.status(root, 'lost')['state'], 'reply_saved')

    def test_an_uncertain_execution_is_never_run_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            catalog = base / 'models.json'
            bridge.save(catalog, {'models': []})
            root = bridge.init(base / 'bridge', catalog, concurrency=1)
            bridge.submit(root, dict(request_id='lost', operation_id='organize.plan', cwd=str(base),
                                     profiles=[{'name': 'test', 'h5ad': '/test-only/input.h5ad'}]))
            bridge.save(root / 'requests/lost/state.json', {'state': 'unknown_external_result'})
            with patch.object(bridge, 'run_organize', side_effect=AssertionError('must not call')):
                bridge.execute(root, 'lost')
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


    def test_serve_requires_a_pool(self):
        with tempfile.TemporaryDirectory() as tmp:
            catalog = Path(tmp) / 'models.json'
            bridge.save(catalog, {'models': []})
            root = bridge.init(Path(tmp) / 'bridge', catalog)
            with self.assertRaisesRegex(ValueError, 'pool_root'):
                bridge.serve(root, once=True)


if __name__ == '__main__':
    unittest.main()

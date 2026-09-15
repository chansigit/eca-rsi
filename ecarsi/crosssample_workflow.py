"""Durable cross-sample integration, bounded DEG fan-out, then type and quality."""
import asyncio
from pathlib import Path

from temporalio import activity, workflow
from temporalio.exceptions import ApplicationError

from .persample_workflow import await_pool, call


def validate_spec(spec):
    from .agent_session import verified
    from .warm_pool.state import identifier, pool_root
    from .agent_bridge import root_path
    required = {'run_id', 'dataset_id', 'input', 'output_root', 'pool_root', 'bridge_root',
                'inspect_budget', 'compute_budget', 'deg_budget', 'tool_budget', 'finalize_budget',
                'config', 'max_in_flight_deg', 'max_refinements'}
    if not isinstance(spec, dict) or set(spec) != required:
        raise ValueError('Cross-sample needs explicit published input, services and operation budgets')
    identifier(spec['run_id'])
    if len(spec['run_id']) > 60 or not isinstance(spec['dataset_id'], str) or not spec['dataset_id'].strip():
        raise ValueError('Use a run ID up to 60 characters and a dataset label')
    output = Path(spec['output_root'])
    if not output.is_absolute() or output.exists():
        raise ValueError('Use a fresh absolute output directory')
    publication = verified(spec['input'])
    if publication.get('state') != 'complete' or publication.get('failed_samples') or not publication.get('samples'):
        raise ValueError('Cross-sample requires a completed per-sample publication')
    metadata = verified(publication['input'])
    if not metadata.get('sample_mapping'):
        raise ValueError('Organize must confirm sample mapping first')
    pool_root(spec['pool_root'])
    root_path(spec['bridge_root'])
    for name in ('inspect_budget', 'compute_budget', 'deg_budget', 'tool_budget', 'finalize_budget'):
        budget = spec[name]
        if set(budget) != {'cpus', 'memory_mb', 'timeout_seconds'} or any(type(v) is not int or v < 1 for v in budget.values()):
            raise ValueError('Budgets must specify positive CPU, MiB and timeout integers')
    if type(spec['max_in_flight_deg']) is not int or spec['max_in_flight_deg'] < 1:
        raise ValueError('max_in_flight_deg must be positive')
    if type(spec['max_refinements']) is not int or spec['max_refinements'] < 0:
        raise ValueError('max_refinements must be nonnegative')
    cfg = spec['config']
    if set(cfg) != {'batch_col', 'species', 'tissue', 'n_top_genes', 'n_pcs', 'n_neighbors',
                    'compute_backend', 'gpu_min_cells', 'gpu_memory_mb'}:
        raise ValueError('Explicit integration, tissue and backend settings required')
    if any(type(cfg[k]) is not int or cfg[k] < 1 for k in ('n_top_genes', 'n_pcs', 'n_neighbors', 'gpu_min_cells', 'gpu_memory_mb')):
        raise ValueError('Numerical and GPU settings must be positive integers')
    if cfg['compute_backend'] not in {'cpu', 'rapids', 'auto'}:
        raise ValueError('compute_backend must be cpu, rapids or auto')
    from .sample_mapping import SAMPLE_KEY
    batch = metadata['sample_mapping']['decision'].get('batch_key', {}).get('column', SAMPLE_KEY)
    if cfg['species'] != metadata['species'] or cfg['batch_col'] != batch:
        raise ValueError('Species and sample key must match the accepted Organize manifest')
    if any(not isinstance(cfg[k], str) or not cfg[k].strip() for k in ('tissue', 'batch_col')):
        raise ValueError('Tissue and sample key must be nonempty')
    return spec


@activity.defn
def crosssample_step(action, args):
    from .agent_session import immutable, reference, verified
    from .warm_pool.state import digest, submit
    if action == 'read':
        return verified(reference(args[0]))
    if action == 'accepted':
        from .persample_workflow import sample_step
        return sample_step('accepted_annotation', args)
    if action == 'publish':
        spec, path = args
        bundle = verified(reference(path))
        if bundle['state'] != 'complete' or bundle['input'] != spec['input'] or bundle['n_input'] != bundle['n_survived'] + bundle['n_removed']:
            raise ValueError('Cross-sample publication did not conserve the accepted input')
        publication = Path(spec['output_root']) / 'publication.json'
        immutable(publication, {**bundle, 'result': reference(path)})
        return str(publication)
    spec, payload, parents = args
    root = Path(spec['output_root'])
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    immutable(root / 'spec.json', spec)
    refs = [reference(path) for path in payload.get('paths', [])]
    request_id = spec['run_id'] + '.' + action + '-' + digest(payload)[:16]
    accelerator = {}
    command = [action, *[r['path'] for r in refs]]
    if action == 'inspect':
        command += [str(root / 'spec.json')]
        refs += [reference(root / 'spec.json'), spec['input']]
        budget, output = spec['inspect_budget'], 'inspected.json'
    elif action == 'include-single':
        budget, output = spec['inspect_budget'], 'decision.json'
    elif action in {'compute', 'refine'}:
        budget, output = spec['compute_budget'], 'prepared.json'
        cfg = spec['config']
        if action == 'compute':
            cells = verified(refs[0])['n_input']
            if cfg['compute_backend'] == 'rapids' or cfg['compute_backend'] == 'auto' and cells >= cfg['gpu_min_cells']:
                accelerator = {'gpu': {'mode': 'required' if cfg['compute_backend'] == 'rapids' else 'preferred', 'memory_mb': cfg['gpu_memory_mb']}}
    elif action == 'deg':
        command += [str(payload['index'])]
        budget, output = spec['deg_budget'], 'result.json'
    elif action == 'assemble':
        packed = immutable(root / ('comparisons-' + digest(payload)[:20] + '.json'), refs[1:])
        command = [action, refs[0]['path'], packed['path']]
        refs += [packed]
        budget, output = spec['tool_budget'], 'evidence.json'
    elif action == 'agent':
        packed = immutable(root / ('agent-input-' + digest(payload)[:20] + '.json'),
                           [spec, refs[0], payload['phase'], request_id, refs[1] if len(refs) > 1 else None])
        command = [action, packed['path']]
        refs += [packed]
        budget, output = spec['inspect_budget'], 'agent.json'
    elif action == 'finalize':
        budget, output = spec['finalize_budget'], 'final.json'
    else:
        raise ValueError('Unknown cross-sample operation')
    unit = 'cross-sample.' + (payload['phase'] + '.prepare' if action == 'agent' else action)
    submit(spec['pool_root'], dict(request_id=request_id, operation_id=unit,
           args=['-m', 'ecarsi.crosssample_v2', *command], **budget, **accelerator,
           inputs=refs + [reference(Path(__file__).with_name('crosssample_v2.py'))], outputs=[output],
           trace=dict(workflow_id='cross-sample/' + spec['run_id'], dataset_id=spec['dataset_id'],
                      unit_id=unit, depends_on=parents)))
    return {'id': request_id, 'output': output}


@workflow.defn
class CrosssampleWorkflow:
    @workflow.query
    def stage(self):
        return getattr(self, '_stage', 'created')

    @workflow.run
    async def run(self, spec):
        from .work_coordinator import AgentWorkflow

        async def run_operation(action, paths, parents, **details):
            request = await call(crosssample_step, action, [spec, dict(paths=paths, **details), parents])
            path = await await_pool(spec, request)
            return path, request['id']

        async def judge(phase, evidence, parent, types=None):
            self._stage = phase + ' annotation' if phase != 'inclusion' else 'sample inclusion'
            path, _ = await run_operation('agent', [evidence] + ([types] if types else []), [parent], phase=phase)
            session = await call(crosssample_step, 'read', [path])
            result = await workflow.execute_child_workflow(AgentWorkflow.run, session,
                id=workflow.info().workflow_id + '/' + session['session_id'])
            accepted = await call(crosssample_step, 'accepted', [result])
            return accepted['path'], accepted['parent']

        self._stage = 'inspecting input'
        inspected, parent = await run_operation('inspect', [], [])
        bundle = await call(crosssample_step, 'read', [inspected])
        if len(bundle['samples']) == 1:
            inclusion, inclusion_parent = await run_operation('include-single', [inspected], [parent])
        else:
            inclusion, inclusion_parent = await judge('inclusion', inspected, parent)
        self._stage = 'integrating'
        prepared, parent = await run_operation('compute', [inspected, inclusion], [inclusion_parent])
        for refinement in range(spec['max_refinements'] + 1):
            self._stage = 'DEG comparisons'
            plan = await call(crosssample_step, 'read', [prepared])
            pending, results, next_index = {}, {}, 0
            while next_index < len(plan['tasks']) or pending:
                while next_index < len(plan['tasks']) and len(pending) < spec['max_in_flight_deg']:
                    index = next_index
                    task = asyncio.create_task(run_operation('deg', [prepared], [parent], index=index))
                    pending[task] = index
                    next_index += 1
                done, _ = await workflow.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for task in sorted(done, key=lambda t: pending[t]):
                    index = pending.pop(task)
                    results[index] = await task
            ordered = [results[i] for i in sorted(results)]
            evidence, evidence_parent = await run_operation('assemble', [prepared] + [r[0] for r in ordered],
                [parent] + [r[1] for r in ordered])
            types, type_parent = await judge('type', evidence, evidence_parent)
            quality, quality_parent = await judge('quality', evidence, type_parent, types)
            decision = await call(crosssample_step, 'read', [quality])
            if decision.get('refinement'):
                if refinement == spec['max_refinements']:
                    raise ApplicationError('Cross-sample refinement limit reached; evidence retained for review', non_retryable=True)
                self._stage = 'refining clustering'
                prepared, parent = await run_operation('refine', [evidence, types, quality], [quality_parent])
                continue
            self._stage = 'finalizing'
            result, _ = await run_operation('finalize', [evidence, types, quality], [quality_parent])
            publication = await call(crosssample_step, 'publish', [spec, result])
            self._stage = 'complete'
            return publication

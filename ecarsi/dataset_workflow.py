"""One submission: Organize, per-sample, then independent converging unit loops."""
import asyncio
from pathlib import Path

from temporalio import activity, workflow
from temporalio.exceptions import ApplicationError

from .persample_workflow import call


def validate_spec(spec):
    from .warm_pool.state import identifier
    required = {'run_id', 'dataset_id', 'input_root', 'output_root', 'pool_root', 'bridge_root',
                'organize', 'per_sample', 'cross_sample', 'zoom_in', 'round_policy'}
    if not isinstance(spec, dict) or set(spec) != required:
        raise ValueError('Dataset needs explicit services, all four stage settings, and a round policy')
    identifier(spec['run_id'])
    if len(spec['run_id']) > 40 or not isinstance(spec['dataset_id'], str) or not spec['dataset_id'].strip():
        raise ValueError('Use a run ID up to 40 characters and a nonempty dataset label')
    output = Path(spec['output_root'])
    if not output.is_absolute() or output.exists() or not output.parent.is_dir():
        raise ValueError('Dataset output must be a fresh absolute path with an existing parent')
    policy = spec['round_policy']
    if not isinstance(policy, dict) or set(policy) != {'rounds', 'cap', 'extra_rounds_after_convergence', 'max_removed'}:
        raise ValueError('Explicit fixed/automatic rounds, safety cap, extra rounds and removal floor required')
    for key, value in policy.items():
        if key == 'rounds' and value is None:
            continue
        if type(value) is not int or value < (0 if key == 'extra_rounds_after_convergence' else 1):
            raise ValueError('Invalid round policy: ' + key)
    from .work_coordinator import validate_spec as validate_organize
    expected = {f'{phase}_{resource}' for phase in ('prepare', 'execute')
                for resource in ('cpus', 'memory_mb', 'timeout_seconds')}
    if not isinstance(spec['organize'], dict) or set(spec['organize']) != expected:
        raise ValueError('Organize settings must contain only its six resource budgets')
    validate_organize({k: spec[k] for k in ('run_id', 'dataset_id', 'input_root', 'output_root', 'pool_root', 'bridge_root')}
                     | spec['organize'])
    for stage in ('per_sample', 'cross_sample', 'zoom_in'):
        template = spec[stage]
        if not isinstance(template, dict) or not isinstance(template.get('config'), dict):
            raise ValueError('Every analysis stage needs its own configuration and budgets')
        if set(template) & {'run_id', 'dataset_id', 'input', 'unit', 'output_root', 'pool_root', 'bridge_root', 'previous_round', 'depends_on'}:
            raise ValueError('Stage inputs and services are supplied by the dataset workflow')
        for name, budget in template.items():
            if name.endswith('_budget') and (not isinstance(budget, dict)
                    or set(budget) != {'cpus', 'memory_mb', 'timeout_seconds'}
                    or any(type(v) is not int or v < 1 for v in budget.values())):
                raise ValueError('Operation budgets require positive CPU, MiB and timeout integers')
    return spec


@activity.defn
def dataset_step(action, args):
    from .agent_session import immutable, reference, verified
    from .warm_pool.state import digest
    if action == 'organize':
        spec, = args
        root = Path(spec['output_root'])
        root.mkdir(mode=0o700, exist_ok=True)
        immutable(root / 'spec.json', spec)
        return {k: spec[k] for k in ('run_id', 'dataset_id', 'input_root', 'pool_root', 'bridge_root')} | {
            'run_id': spec['run_id'] + '-organize', 'output_root': str(root / '00-organize'), **spec['organize']}
    if action == 'units':
        path, = args
        published = reference(Path(path) / 'publication.json')
        value = verified(published)
        names = [entry['name'] for entry in value['units']]
        if not names or len(set(names)) != len(names) or any(Path(n).name != n or n in {'.', '..'} for n in names):
            raise ValueError('Organize must publish unique analysis units')
        from .run_state import file_identity
        units = []
        for entry in value['units']:
            directory = Path(path) / 'units' / entry['name']
            manifest = directory / 'input/manifest.json'
            if file_identity(manifest) != entry['manifest']:
                raise ValueError('Organize unit manifest changed before scheduling')
            units.append(dict(name=entry['name'], path=str(directory), organize=published, manifest=reference(manifest)))
        return units
    if action == 'stage':
        spec, unit, stage, source, round_number = args
        verified(unit['organize'])
        directory = Path(spec['output_root']) / 'units' / unit['name']
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        template = spec[stage]
        common = {k: spec[k] for k in ('dataset_id', 'pool_root', 'bridge_root')}
        common['run_id'] = spec['run_id'][:20] + '-' + digest([spec['run_id'], unit['name'], stage, round_number])[:20]
        if stage == 'per_sample':
            from .persample_workflow import validate_spec as validate
            return validate({**template, **common, 'unit': unit['path'], 'output_root': str(directory / '01-per-sample'),
                             'depends_on': [spec['run_id'] + '-organize.execute']})
        metadata = verified(unit['manifest'])
        from .sample_mapping import SAMPLE_KEY
        derived = dict(species=metadata['species'],
            batch_col=(metadata['sample_mapping']['decision'].get('batch_key') or {}).get('column', SAMPLE_KEY))
        for key, value in derived.items():
            if key in template['config'] and template['config'][key] != value:
                raise ValueError('Stage settings disagree with Organize: ' + key)
        settings = {**template, **common, 'config': {**template['config'], **derived}, 'input': reference(source)}
        publication = verified(settings['input'])
        # Actual producing requests, including every independent sample finalizer.
        parents = publication['samples'] if stage == 'cross_sample' and round_number == 1 else [publication['result']]
        request_root = Path(spec['pool_root']) / 'requests'
        from .warm_pool.state import identifier
        settings['depends_on'] = sorted({identifier(Path(ref['path']).relative_to(request_root).parts[0]) for ref in parents})
        directory = directory / 'rounds' / f'round{round_number:02d}'
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if stage == 'cross_sample':
            from .crosssample_workflow import validate_spec as validate
            settings['output_root'] = str(directory / '02-cross-sample')
            if round_number > 1:
                settings['previous_round'] = round_number - 1
        elif stage == 'zoom_in':
            from .zoomin_workflow import validate_spec as validate
            settings['output_root'] = str(directory / '03-zoom-in')
        else:
            raise ValueError('Unknown dataset stage')
        return validate(settings)
    if action == 'round':
        from .round_policy import decide
        spec, unit, progress, cross_path, zoom_path = args
        before, cross, zoom = (verified(reference(p)) for p in (progress['input'], cross_path, zoom_path))
        if (cross['state'] != 'complete' or zoom['state'] != 'complete'
                or cross['input'] != reference(progress['input']) or zoom['input'] != reference(cross_path)
                or cross['n_input'] != before['n_survived'] or zoom['n_input'] != cross['n_survived']):
            raise ValueError('Round publications do not form a conserved survivor chain')
        n_in, n_out = before['n_survived'], zoom['n_survived']
        if n_in <= 0 or not 0 <= n_out <= n_in or cross['n_removed'] + zoom['n_removed'] != n_in - n_out:
            raise ValueError('Invalid round cell accounting')
        stats = progress['stats'] + [dict(n_in=n_in, n_out=n_out, removed=n_in-n_out, frac=(n_in-n_out)/n_in)]
        n = len(stats)
        policy = spec['round_policy']
        decision, reason = decide(n, stats, policy['rounds'], policy['cap'],
                                  policy['extra_rounds_after_convergence'], policy['max_removed'])
        stats[-1].update(decision=decision, reason=reason)
        directory = Path(spec['output_root']) / 'units' / unit['name'] / 'rounds' / f'round{n:02d}'
        record = immutable(directory / 'publication.json', dict(round=n, stats=stats[-1],
            cross_sample=reference(cross_path), zoom_in=reference(zoom_path), policy=policy))
        result = dict(per_sample=progress['per_sample'], input=zoom_path, stats=stats, rounds=progress['rounds'] + [record])
        if decision == 'release':
            first = verified(reference(progress['per_sample']))
            path = directory.parent.parent / 'publication.json'
            immutable(path, dict(state='complete', unit=unit, per_sample=reference(progress['per_sample']),
                rounds=result['rounds'], final=reference(zoom_path), policy=policy,
                n_input=first['n_input'], n_survived=n_out, n_removed=first['n_input']-n_out,
                forced_release=reason.startswith('FORCED:'), reason=reason))
            result['publication'] = str(path)
        return result
    if action == 'publish':
        spec, results, failures = args
        publications = [reference(p) for p in sorted(results)]
        units = [verified(p) for p in publications]
        path = Path(spec['output_root']) / 'publication.json'
        immutable(path, dict(state='incomplete' if failures else 'complete', dataset_id=spec['dataset_id'],
            units=publications, failed_units=failures, forced_release=any(u['forced_release'] for u in units),
            n_input=sum(u['n_input'] for u in units), n_survived=sum(u['n_survived'] for u in units),
            n_removed=sum(u['n_removed'] for u in units)))
        return str(path)
    raise ValueError('Unknown dataset operation')


@workflow.defn
class AnalysisUnitWorkflow:
    @workflow.query
    def stage(self):
        return getattr(self, '_stage', 'created')

    @workflow.run
    async def run(self, spec, unit, progress=None):
        from .persample_workflow import PersampleWorkflow
        from .crosssample_workflow import CrosssampleWorkflow
        from .zoomin_workflow import ZoominWorkflow
        if progress is None:
            self._stage = 'per-sample'
            stage = await call(dataset_step, 'stage', [spec, unit, 'per_sample', None, 0])
            output = await workflow.execute_child_workflow(PersampleWorkflow.run, stage, id='persample/' + stage['run_id'])
            progress = dict(per_sample=output, input=output, stats=[], rounds=[])
        number = len(progress['stats']) + 1
        self._stage = f'round {number}: cross-sample'
        stage = await call(dataset_step, 'stage', [spec, unit, 'cross_sample', progress['input'], number])
        cross = await workflow.execute_child_workflow(CrosssampleWorkflow.run, stage, id='cross-sample/' + stage['run_id'])
        self._stage = f'round {number}: zoom-in'
        stage = await call(dataset_step, 'stage', [spec, unit, 'zoom_in', cross, number])
        zoom = await workflow.execute_child_workflow(ZoominWorkflow.run, stage, id='zoom-in/' + stage['run_id'])
        progress = await call(dataset_step, 'round', [spec, unit, progress, cross, zoom])
        if 'publication' in progress:
            self._stage = 'complete'
            return progress['publication']
        # Bound each unit's Temporal history; continued runs preserve the child result contract.
        workflow.continue_as_new(args=[spec, unit, progress])


@workflow.defn
class DatasetWorkflow:
    @workflow.query
    def stage(self):
        return getattr(self, '_stage', 'created')

    @workflow.run
    async def run(self, spec):
        from .work_coordinator import OrganizeWorkflow
        self._stage = 'organize'
        stage = await call(dataset_step, 'organize', [spec])
        organized = await workflow.execute_child_workflow(OrganizeWorkflow.run, stage, id='organize/' + stage['run_id'])
        units = await call(dataset_step, 'units', [organized])
        pending = {}
        for index, unit in enumerate(units):
            child = await workflow.start_child_workflow(AnalysisUnitWorkflow.run, args=[spec, unit],
                id=workflow.info().workflow_id + '/unit-' + str(index))
            pending[child] = unit['name']
        results, failures = [], []
        while pending:
            self._stage = f'{len(pending)} analysis units active'
            done, _ = await workflow.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for child in sorted(done, key=lambda c: pending[c]):
                name = pending.pop(child)
                try:
                    results.append(await child)
                except Exception as exc:
                    failures.append(dict(unit=name, error=str(exc)))
        output = await call(dataset_step, 'publish', [spec, results, failures])
        if failures:
            raise ApplicationError('Analysis units failed; completed siblings retained at ' + output, non_retryable=True)
        self._stage = 'complete'
        return output

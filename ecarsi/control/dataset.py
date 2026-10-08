"""One submission: Organize, per-sample, then independent converging unit loops."""
import asyncio
import json
import os
import re
from pathlib import Path

from temporalio import activity, workflow
from temporalio.exceptions import ApplicationError

from .. import layout as L
from ..contracts import check
from .common import await_pool, brake_if_asked, call, stage_with_waits, start_child


def validate_spec(spec):
    from ..warm_pool.state import identifier
    required = {'run_id', 'dataset_id', 'input_root', 'output_root', 'pool_root', 'bridge_root',
                'organize', 'per_sample', 'cross_sample', 'zoom_in', 'round_policy'}
    if not isinstance(spec, dict) or set(spec) - {'storage', 'stress_policy'} != required:
        raise ValueError('Dataset needs explicit services, all four stage settings, and a round policy')
    if spec.get('stress_policy', 'remove') not in ('remove', 'keep'):  # optional, decision 0017
        raise ValueError('stress_policy is remove or keep')
    storage = spec.get('storage')  # optional: where the display zone and the work archive go (#25)
    if storage is not None and (not isinstance(storage, dict) or set(storage) != {'display_root', 'archive_root'}
                                or not all(isinstance(v, str) and Path(v).is_absolute() for v in storage.values())):
        raise ValueError('storage needs absolute display_root and archive_root paths')
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
    from .coordinator import validate_spec as validate_organize
    expected = {f'{phase}_{resource}' for phase in ('prepare', 'execute')
                for resource in ('cpus', 'memory_mb', 'timeout_seconds')}
    if not isinstance(spec['organize'], dict) or set(spec['organize']) - {'sample_map'} != expected:
        raise ValueError('Organize settings must contain its six resource budgets and at most a sample_map')
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


UNFINISHED = frozenset({'FAILED', 'TERMINATED', 'CANCELED', 'TIMED_OUT'})


def superseded_sessions(root):
    """Sessions whose Pool and Bridge requests no longer matter for resume: a restart ran the same
    judgement again as a fresh session (every generation of the one that died: the generation live at
    the restart kept six resumes blocked on its failed turn, 2026-09-24), a context reset continued it
    in a fresh conversation."""
    from ..files import read
    restarted = {read(path)['superseded'] for path in Path(root).rglob('restart.json')}
    ids = set(restarted)
    for path in Path(root).rglob('context-reset-*.json'):
        generation = int(path.stem.rsplit('-', 1)[1])
        current = read(path)['spec']['session_id']
        base = re.sub(r'-g\d+$', '', current)
        ids.add(base if generation == 2 else f'{base}-g{generation - 1}')
        if base in restarted:
            ids.add(current)
    return ids


def request_session(spec):
    """The agent session a Pool or Bridge request served; None for a host step."""
    if spec.get('operation_id') == 'agent.call' and len(spec.get('args', [])) == 4:
        return Path(spec['args'][3]).parent.name.rsplit('.turn-', 1)[0]
    name = spec.get('request_id', '')
    for marker in ('.turn-', '.tool-'):
        if marker in name:
            return name.split(marker, 1)[0]
    return None


SESSION = re.compile(r'(org|osp|cross|zoom)-[0-9a-f]{24}')


def sessions_of(root):
    """Agent session ids of a dataset run: every session leaves directories named after it in the
    run tree (`03-zoom-in/zoom-…`, `01-per-sample/agent-…/osp-….tool-…`, `00-organize.planning/org-….tool-…`)."""
    return {m.group(0) for p in Path(root).rglob('*') if (m := SESSION.match(p.name))}


def replaced(pool_root, request_id, bridge_root):
    """completed_replacement, accepting a winner whose Pool folder is gone: it raises KeyError only
    after the Bridge confirmed the reply was saved by that later attempt, and a released run that
    replayed the same session takes the attempt's folder with it when it is archived."""
    from ..agent.dispatch import completed_replacement
    try:
        return completed_replacement(pool_root, request_id, bridge_root)
    except KeyError:
        return True


def request_states(pool_root, bridge_root, identities, superseded, sessions):
    """Every Pool and Bridge request of these workflows with the state resume records; raises on one
    that neither finished nor was superseded (its session restarted or reset, or a model attempt
    was replaced).

    Candidate folders come from their names, the trace still decides: Pool requests are
    `<stage run_id>.<kind>-…` (organize: `<run_id>-organize.<step>`) or `<session_id>.tool-…`,
    Bridge requests `<session_id>[-rN|-gN].turn-N`. Reading every request.json instead took the
    2026-09-23 resume past 17 minutes on a 437k-folder pool, on the control-plane node."""
    from ..files import read
    from ..warm_pool.state import status
    from ..agent import status as bridge_status
    runs = {i.split('/')[1] for i in identities if '/' in i}
    heads = runs | {r + '-organize' for r in runs} | set(sessions)

    def owned(name):
        return re.sub(r'-[rg]\d+$', '', name.split('.', 1)[0]) in heads

    def folders(root):
        path = Path(root) / 'requests'
        return sorted(os.scandir(path), key=lambda e: e.name) if path.is_dir() else []
    # A model turn runs as a Pool `agent-<digest>` request; only the Bridge folder's state.json
    # names it, so those candidates come from the owned Bridge folders.
    attempts = {a['pool_request_id'] for e in folders(bridge_root) if owned(e.name)
                for a in read(Path(e.path) / 'state.json', {}).get('attempts', []) if a.get('pool_request_id')}
    requests = []
    for service, root, inspect, allowed, wanted in (
        ('pool_root', pool_root, status, {'queued', 'running', 'succeeded'}, lambda n: owned(n) or n in attempts),
        ('bridge_root', bridge_root, bridge_status, {'queued', 'running', 'reply_saved'}, owned),
    ):
        for entry in folders(root):
            if not wanted(entry.name):
                continue
            path = Path(entry.path) / 'request.json'
            request = read(path)
            if not request or request['spec'].get('trace', {}).get('workflow_id') not in identities:
                continue
            spec = request['spec']
            state = inspect(root, path.parent.name)['state']
            if state not in allowed:
                if request_session(spec) in superseded:
                    state = 'superseded_session'
                elif service == 'pool_root' and replaced(root, path.parent.name, bridge_root):
                    state = 'superseded_model_attempt'
                elif (service == 'pool_root' and state == 'failed' and spec.get('operation_id') != 'agent.call'
                      and request_session(spec) is not None and inspect(root, path.parent.name)['receipt'].get('retryable') is False):
                    # A tool call that failed for good is a result its session already consumed; the
                    # resumed session replays the same transcript and meets the same receipt.
                    state = 'settled_tool_failure'
                else:
                    raise ValueError(f'Reconcile {path.parent.name} ({state}) before resume')
            requests.append(dict(service=service, request_id=path.parent.name, state=state))
    return requests


async def resume_dataset(client, identity, task_queue, reason):
    """New Temporal run, same immutable dataset and accepted external request IDs.

    task_queue None means the queue the previous run was started on (the coordinator's main passes the
    current version's queue, 0022): a run started on a queue no coordinator polls sits at its first
    workflow task forever (2026-09-16, Eye)."""
    from temporalio.common import WorkflowIDReusePolicy
    from ..files import immutable, reference
    from ..files import read, digest
    if not reason.strip():
        raise ValueError('A recovery reason is required')
    previous = client.get_workflow_handle(identity)
    info = await previous.describe()
    if info.status.name not in UNFINISHED:
        raise ValueError('Dataset resume requires a failed, terminated, cancelled or timed-out workflow')
    history = await client.get_workflow_handle(identity, run_id=info.run_id).fetch_history()
    started = history.events[0].workflow_execution_started_event_attributes
    task_queue = task_queue or started.task_queue.name
    inputs = await client.data_converter.decode(started.input.payloads)
    spec = inputs[0]
    root = Path(spec['output_root'])
    if read(root / 'spec.json') != spec:
        raise ValueError('Saved dataset specification changed')
    pending, visited, identities = [(identity, info.run_id)], set(), set()
    while pending:
        key = pending.pop()
        if key in visited:
            continue
        visited.add(key)
        identities.add(key[0])
        handle = client.get_workflow_handle(key[0], run_id=key[1])
        if (await client.get_workflow_handle(key[0]).describe()).status.name == 'RUNNING':
            raise ValueError('Dataset still has an active workflow: ' + key[0])
        for event in (await handle.fetch_history()).events:
            if event.HasField('child_workflow_execution_started_event_attributes'):
                child = event.child_workflow_execution_started_event_attributes.workflow_execution
                pending.append((child.workflow_id, child.run_id))
            elif event.HasField('workflow_execution_continued_as_new_event_attributes'):
                pending.append((key[0], event.workflow_execution_continued_as_new_event_attributes.new_execution_run_id))
    # Earlier recovery runs may have skipped completed children. Their stable stage IDs
    # still own Pool/Bridge requests, so include their sealed specifications as well.
    identities.add('organize/' + spec['run_id'] + '-organize')
    for pattern, prefix in ((f'{L.UNITS}/*/{L.GEN2_PERSAMPLE}/{L.GEN2_SPEC}', 'persample/'),
                            (f'{L.UNITS}/*/{L.ROUNDS}/*/{L.GEN2_CROSS}/{L.GEN2_SPEC}', 'cross-sample/'),
                            (f'{L.UNITS}/*/{L.ROUNDS}/*/{L.GEN2_ZOOM}/{L.GEN2_SPEC}', 'zoom-in/')):
        for path in root.glob(pattern):
            stage = read(path)
            if any(stage[key] != spec[key] for key in ('dataset_id', 'pool_root', 'bridge_root')):
                raise ValueError('Saved stage belongs to another dataset or service')
            identities.add(prefix + stage['run_id'])
    requests = request_states(spec['pool_root'], spec['bridge_root'], identities, superseded_sessions(root), sessions_of(root))
    from uuid import uuid4
    audit = root / 'recoveries' / (uuid4().hex + '.json')
    audit.parent.mkdir(mode=0o700, exist_ok=True)
    previous_publication = read(root / 'publication.json')
    if previous_publication is not None:
        # Archived under its own content digest; the live slot is cleared, or the page keeps
        # reading a superseded failed_units record forever -- a resumed dataset never writes
        # publication.json again until it finishes, so nothing else would ever clear it
        # (2026-09-21: two resumed PanSci datasets stayed "failed" on Periscope 40+ minutes
        # into a clean rerun). The content is not lost, only its old path.
        previous_publication = immutable(root / ('publication-' + digest(previous_publication) + '.json'),
                                         previous_publication)
        (root / 'publication.json').unlink()
    intent = immutable(audit, dict(workflow_id=identity, failed_run_id=info.run_id, reason=reason,
        spec=reference(root / 'spec.json'), previous_publication=previous_publication,
        workflows=sorted(visited), requests=requests))
    handle = await client.start_workflow(DatasetWorkflow.run, args=[spec, True], id=identity,
        task_queue=task_queue, id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY)
    immutable(audit.with_suffix('.started.json'), dict(intent=intent, run_id=handle.result_run_id))
    return handle


def stage_limit_floors():
    """Operator floors for stage concurrency: ECA_RSI_STAGE_LIMIT_FLOORS, a JSON object such as
    {"max_in_flight_deg": 12, "max_in_flight_lineages": 6}. Empty by default: the dataset
    spec's own values stand. The spec is a Temporal workflow input and cannot change mid-run,
    so this is how an operator widens the windows for every stage started from now on (the
    workflows' set_deg_limit update covers stages already running). Resume ignores these keys."""
    raw = os.environ.get('ECA_RSI_STAGE_LIMIT_FLOORS', '').strip()
    floors = json.loads(raw) if raw else {}
    if not isinstance(floors, dict) or any(type(v) is not int or v < 1 for v in floors.values()):
        raise ValueError('ECA_RSI_STAGE_LIMIT_FLOORS must be a JSON object of positive integers')
    return floors


def with_limit_floors(settings, floors=None):
    floors = stage_limit_floors() if floors is None else floors
    return {**settings, **{k: max(settings[k], v) for k, v in floors.items() if k in settings}}


def same_stage_spec(saved, result, floors=None):
    floors = stage_limit_floors() if floors is None else floors
    def strip(value):
        return {k: v for k, v in (value or {}).items() if k not in floors}
    return strip(saved) == strip(result)


def stage_spec_on_disk(root, result, resume):
    """On resume a stage keeps the spec it was saved with: operator floors widen new stages only,
    a running stage is widened through set_deg_limit, and the stage workflow re-writes spec.json
    immutably (Eye, 2026-09-17: 'Conflicting durable content' with a floored resume spec)."""
    from ..files import read
    if not resume or not root.exists():
        return result
    saved = read(root / 'spec.json')
    if not same_stage_spec(saved, result):
        raise ValueError('Saved stage specification changed; resume cannot change inputs or settings')
    return saved


@activity.defn
def dataset_step(action, args):
    from ..files import immutable, reference, verified
    from ..files import digest, read, save, lock
    if action == 'release':
        from ..warm_pool.state import submit
        spec, path = args
        source = reference(path)
        unit = verified(source)
        root = L.unit_dir(Path(spec['output_root']), unit['unit']['name'])
        if Path(path).resolve() != (root / 'publication.json').resolve() or unit['state'] != 'complete':
            raise ValueError('Release requires this dataset\'s completed unit')
        from .. import stages
        programs = [stages.program(name) for name in ('release', 'release_state', 'ledger', 'common')] + [stages.PACKAGE / name for name in ('review.py', 'ui/umapdata.py')]
        packet = immutable(root / 'release-input.json', dict(input=source))
        request_id = spec['run_id'] + '.release-' + digest(source)[:16]
        parent = Path(verified(unit['final'])['result']['path']).relative_to(Path(spec['pool_root']) / 'requests').parts[0]
        submit(spec['pool_root'], dict(request_id=request_id, operation_id='dataset.release',
            args=['-m', 'ecarsi.stages.release', packet['path']], **spec['zoom_in']['merge_budget'],
            inputs=[packet, source, *[reference(path) for path in programs]],
            outputs=['released.json'], trace=dict(workflow_id='dataset/' + spec['run_id'],
                dataset_id=spec['dataset_id'], unit_id='dataset.release', depends_on=[parent])))
        return dict(id=request_id, output='released.json')
    if action == 'released':
        source, result = args
        receipt = verified(reference(result))
        if receipt['state'] != 'complete' or receipt['input'] != reference(source):
            raise ValueError('Release does not match completed unit')
        release = verified(receipt['release'])
        if release['state'] != 'complete' or release['input'] != receipt['input']:
            raise ValueError('Release receipt does not match completed unit')
        return source
    if action == 'completed':
        stage, spec = args
        root = Path(spec['output_root'])
        path = root / 'publication.json'
        publication = read(path)
        if publication is None or publication.get('state') == 'incomplete':
            return None
        from .coordinator import check_pool
        def accepted(ref):
            request = Path(ref['path']).relative_to(Path(spec['pool_root']) / 'requests').parts[0]
            try:
                result = check_pool(spec['pool_root'], request, Path(ref['path']).name)
            except KeyError:
                # The Pool folder is gone (pruned after the run finished, or archived by hand: six 3CA
                # resumes died on this, 2026-09-24); the publication's own reference still verifies the file.
                return verified(ref)
            if result['state'] != 'ready' or reference(result['path']) != ref:
                raise ValueError('Cached stage output is no longer accepted')
            return verified(ref)
        if stage == 'organize':
            try:
                result = check_pool(spec['pool_root'], spec['run_id'] + '.execute', 'completion.json')
            except KeyError:
                return str(root)  # Pool folder pruned; the relocated publication was accepted when written
            if result['state'] != 'ready':
                raise ValueError('Organize execution is no longer accepted')
            # Publication relocates the manifest. Reuse its full validation rather
            # than comparing the intentionally different worker/published records.
            from ..stages.organize import publish
            return publish(Path(result['path']).parent, root)
        if not same_stage_spec(read(root / 'spec.json'), spec):
            raise ValueError('Saved stage specification changed')
        if publication.get('state') != 'complete' or publication['input'] != spec.get('input', spec.get('input_manifest')):
            raise ValueError('Cached stage input changed or is incomplete')
        if stage == 'per_sample':
            if publication.get('failed_samples') or read(root / ('publication-' + digest(publication) + '.json')) != publication:
                raise ValueError('Per-sample publication is not an accepted revision')
            for ref in publication['samples']:
                accepted(ref)
            ref = publication['partition_exclusions']
            if reference(ref['path']) != ref:
                raise ValueError('Partition exclusion ledger changed')
        elif publication != {**accepted(publication['result']), 'result': publication['result']}:
            raise ValueError('Publication differs from its accepted worker result')
        if publication['n_input'] != publication['n_survived'] + publication['n_removed']:
            raise ValueError('Cached stage does not conserve cells')
        return str(path)
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
        from ..run_state import file_identity
        units = []
        for entry in value['units']:
            directory = L.unit_dir(Path(path), entry['name'])
            manifest = directory / 'input/manifest.json'
            if file_identity(manifest) != entry['manifest']:
                raise ValueError('Organize unit manifest changed before scheduling')
            units.append(dict(name=entry['name'], path=str(directory), organize=published, manifest=reference(manifest)))
        return units
    if action in {'stage', 'resume_stage'}:
        spec, unit, stage, source, round_number = args
        resume = action == 'resume_stage'
        def validated(settings, validate):
            result = validate(with_limit_floors(settings), resume=resume)
            return stage_spec_on_disk(Path(result['output_root']), result, resume)
        verified(unit['organize'])
        directory = L.unit_dir(Path(spec['output_root']), unit['name'])
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        template = spec[stage]
        common = {k: spec[k] for k in ('dataset_id', 'pool_root', 'bridge_root')}
        common['run_id'] = spec['run_id'][:20] + '-' + digest([spec['run_id'], unit['name'], stage, round_number])[:20]
        if stage == 'per_sample':
            from .persample import validate_spec as validate
            return validated({**template, **common, 'unit': unit['path'], 'output_root': str(directory / L.GEN2_PERSAMPLE),
                             'depends_on': [spec['run_id'] + '-organize.execute']}, validate)
        metadata = verified(unit['manifest'])
        from ..sample_mapping import SAMPLE_KEY
        derived = dict(species=metadata['species'],
            batch_col=(metadata['sample_mapping']['decision'].get('batch_key') or {}).get('column', SAMPLE_KEY))
        if 'stress_policy' in spec:  # only when set: a run without it keeps its stage specs on resume
            derived['stress_policy'] = spec['stress_policy']
        for key, value in derived.items():
            if key in template['config'] and template['config'][key] != value:
                raise ValueError('Stage settings disagree with Organize: ' + key)
        settings = {**template, **common, 'config': {**template['config'], **derived}, 'input': reference(source)}
        publication = verified(settings['input'])
        # Actual producing requests, including every independent sample finalizer.
        parents = publication['samples'] if stage == 'cross_sample' and round_number == 1 else [publication['result']]
        request_root = Path(spec['pool_root']) / 'requests'
        from ..warm_pool.state import identifier
        settings['depends_on'] = sorted({identifier(Path(ref['path']).relative_to(request_root).parts[0]) for ref in parents})
        directory = L.round_dir(directory, round_number)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if stage == 'cross_sample':
            from .crosssample import validate_spec as validate
            settings['output_root'] = str(directory / L.GEN2_CROSS)
            if round_number > 1:
                settings['previous_round'] = round_number - 1
        elif stage == 'zoom_in':
            from .zoomin import validate_spec as validate
            settings['output_root'] = str(directory / L.GEN2_ZOOM)
        else:
            raise ValueError('Unknown dataset stage')
        return validated(settings, validate)
    if action == 'pause-after-stage':
        # Generation 1 stops at harness_bridge's safe_point("crosssample") / safe_point("zoomin"):
        # after the stage's own outputs are on disk, before the next stage starts and before the
        # round is decided. Here the stage is a child workflow, so its completion is that same
        # safe point -- the check just has to be an activity, because reading the file is I/O.
        from ..round_policy import read_control
        spec, unit, stage = args
        control = read_control(L.unit_dir(Path(spec['output_root']), unit['name']))
        if control.get('pause_after_stage') != stage and control.get('brake') != 'stage':
            return None
        return (f'PAUSED: loop_control stopped the unit after {stage}; clear pause_after_stage or the brake '
                'and resume the dataset to continue')
    if action == 'round':
        from ..round_policy import decide_with_control, read_control, resolve
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
        # The spec's policy is what the dataset was admitted with and cannot change; loop_control
        # is the manual gearbox, re-read here because a round boundary is the only moment a
        # decision is made. Generation 1 has worked this way since the start; the durable control
        # plane froze the policy into workflow input, which left no way to move a limit mid-run.
        unit_root = L.unit_dir(Path(spec['output_root']), unit['name'])
        directory = L.round_dir(unit_root, n)
        path, notes = directory / 'publication.json', []
        stored = read(path)
        if stored and stored['stats']['decision'] == 'pause':
            # Resuming a paused unit: the stop was answered by clearing the control, so this round
            # is decided again (#15). The paused record stays under its digest.
            immutable(directory / ('publication-' + digest(stored) + '.json'), stored)
            path.unlink()
            stored = None
        if stored and (stored['cross_sample'], stored['zoom_in']) == (reference(cross_path), reference(zoom_path)):
            # Decided already (a resume re-drives every round): the decision stands even if
            # loop_control changed since, or deciding again would conflict with the record (#15).
            stats[-1], policy = stored['stats'], stored['policy']
            decision, reason = stats[-1]['decision'], stats[-1]['reason']
            record = reference(path)
        else:
            control = read_control(unit_root, on_error=notes.append)
            policy = resolve(spec['round_policy'], control)
            decision, reason = decide_with_control(n, stats, spec['round_policy'], control)
            stats[-1].update(decision=decision, reason=reason)
            from .. import version
            record = immutable(path, check('round', dict(round=n, stats=stats[-1],
                cross_sample=reference(cross_path), zoom_in=reference(zoom_path), policy=policy,
                version=(version() or {}).get('name'),  # the version that decided this round (0022)
                **({'control': control} if control else {}), **({'control_notes': notes} if notes else {}))))
        result = dict(per_sample=progress['per_sample'], input=zoom_path, stats=stats, rounds=progress['rounds'] + [record])
        for note in notes:
            print(f'[round {n}] {note}', flush=True)
        if decision == 'pause':
            result['paused'] = reason
        if decision == 'release':
            first = verified(reference(progress['per_sample']))
            path = directory.parent.parent / 'publication.json'
            versions = sorted({v for v in [first.get('version')] + [verified(r).get('version') for r in result['rounds']] if v})
            immutable(path, check('unit', dict(state='complete', unit=unit, per_sample=reference(progress['per_sample']),
                rounds=result['rounds'], final=reference(zoom_path), policy=policy, versions=versions,
                n_input=first['n_input'], n_survived=n_out, n_removed=first['n_input']-n_out,
                forced_release=reason.startswith('FORCED:'), reason=reason)))
            result['publication'] = str(path)
        return result
    if action == 'display':
        # The display zone after a stage (#25); `final` also archives the work tree. See ecarsi.display.
        from ..display import zone
        from ..warm_pool.state import submit
        from .. import stages
        spec, label, final = args
        key = digest([label, bool(final)])[:16]
        (Path(spec['output_root']) / L.GEN2_DISPLAY_SYNC).mkdir(exist_ok=True)
        packet = immutable(Path(spec['output_root']) / L.GEN2_DISPLAY_SYNC / (key + '.json'),
                           dict(zone(spec), root=spec['output_root'], final=bool(final), label=label))
        programs = [stages.program('display'), stages.PACKAGE / 'display.py', stages.program('archive'), stages.program('cases')]
        budget = DISPLAY_FINAL_BUDGET if final or label == 'published' else DISPLAY_BUDGET  # 'published' freezes hard cases
        request_id = spec['run_id'] + '.display-' + key
        submit(spec['pool_root'], dict(request_id=request_id, operation_id='dataset.display',
            args=['-m', 'ecarsi.stages.display', packet['path']], **budget,
            inputs=[packet, *[reference(path) for path in programs]], outputs=['synced.json'],
            trace=dict(workflow_id='dataset/' + spec['run_id'], dataset_id=spec['dataset_id'], unit_id='dataset.display')))
        return dict(id=request_id, output='synced.json')
    if action == 'round-ledger':
        from ..warm_pool.state import submit
        spec, unit, progress = args
        directory = L.round_dir(L.unit_dir(Path(spec['output_root']), unit['name']), len(progress['stats']))
        from .. import stages
        programs = [stages.program('release'), stages.program('ledger'), stages.PACKAGE / 'sample_mapping.py']
        packet = immutable(directory / 'ledger-input.json', dict(
            input=reference(progress['input']), unit=unit,
            per_sample=reference(progress['per_sample']), rounds=progress['rounds']))
        request_id = spec['run_id'] + '.ledger-' + digest(packet)[:16]
        parent = Path(verified(reference(progress['input']))['result']['path']).relative_to(
            Path(spec['pool_root']) / 'requests').parts[0]
        submit(spec['pool_root'], dict(request_id=request_id, operation_id='dataset.round-ledger',
            args=['-m', 'ecarsi.stages.release', 'ledger', packet['path']], **spec['zoom_in']['merge_budget'],
            inputs=[packet, *progress['rounds'], reference(progress['per_sample']),
                    *[reference(path) for path in programs]],
            outputs=['ledger.json'], trace=dict(workflow_id='dataset/' + spec['run_id'],
                dataset_id=spec['dataset_id'], unit_id='dataset.round-ledger', depends_on=[parent])))
        return dict(id=request_id, output='ledger.json')
    if action == 'round-ledger-published':
        spec, unit, progress, result = args
        from .artifacts import copy_light
        bundle = verified(reference(result))
        directory = L.round_dir(L.unit_dir(Path(spec['output_root']), unit['name']), len(progress['stats']))
        from ..degraded import save
        save(directory, copy_light(bundle.get('files'), directory / 'ledger'), stage=directory.name + '/ledger')
        return str(directory / 'ledger')
    if action == 'degraded':
        # A control-plane step that failed without failing the run (ecarsi.degraded). Kept in the work
        # tree, and in the display zone directly: a failed final sync would never carry it there.
        spec, unit_name, label, error = args
        from ..degraded import note, save
        from ..display import zone
        record = note(label, RuntimeError(error))
        root = Path(spec['output_root'])
        save(L.unit_dir(root, unit_name) if unit_name else root, [record], stage=label)
        display = Path(zone(spec)['dest']) if spec.get('storage') else None
        if display and display.is_dir():
            try:
                save(display, [record], stage=label, **({'unit': unit_name} if unit_name else {}))
            except OSError as exc:
                print(f'[degraded] {label}: not written to the display zone: {exc!r}', flush=True)
        return None
    if action == 'publish':
        spec, results, failures = args
        publications = [reference(p) for p in sorted(results)]
        units = [check('unit', verified(p)) for p in publications]
        path = Path(spec['output_root']) / 'publication.json'
        publication = dict(state='incomplete' if failures else 'complete', dataset_id=spec['dataset_id'],
            units=publications, failed_units=failures, forced_release=any(u['forced_release'] for u in units),
            versions=sorted({v for u in units for v in u.get('versions', [])}),  # every version that ran a stage (0022)
            n_input=sum(u['n_input'] for u in units), n_survived=sum(u['n_survived'] for u in units),
            n_removed=sum(u['n_removed'] for u in units))
        check('dataset', publication)
        # Same revision contract as per-sample: retain failed publications and seal successes.
        with lock(path.parent / 'publication.lock'):
            previous = read(path)
            if previous and previous != publication:
                if previous['state'] == 'complete':
                    raise ValueError('Cannot replace a completed publication with different results')
                immutable(path.parent / ('publication-' + digest(previous) + '.json'), previous)
            immutable(path.parent / ('publication-' + digest(publication) + '.json'), publication)
            save(path, publication)
        return str(path)
    raise ValueError('Unknown dataset operation')


async def hard_brake(client, identity, reason):
    """The hard brake (0022): terminate the dataset workflow now; its children end with it (parent close policy),
    pool tasks already on a worker run out and are discarded, every directory stays, `resume-dataset` continues."""
    if not reason.strip():
        raise ValueError('A brake reason is required')
    handle = client.get_workflow_handle(identity)
    info = await handle.describe()
    if info.status.name != 'RUNNING':
        raise ValueError(f'{identity} is {info.status.name}, not running')
    await handle.terminate(reason='hard brake: ' + reason)
    return dict(workflow_id=identity, run_id=info.run_id, terminated=True,
                resume=f'resume-dataset {identity.split("/", 1)[1]} --reason ...')


async def pause_if_asked(spec, unit, stage):
    """Stop the unit between stages, the way generation 1's safe_point does. Failing
    non-retryably is how a unit holds without releasing and stays resumable: the same
    contract `resume-dataset` already serves. Clear the control before resuming, or the
    next round pauses at the same place again."""
    reason = await call(dataset_step, 'pause-after-stage', [spec, unit, stage])
    if reason:
        raise ApplicationError(reason, non_retryable=True)


DISPLAY_BUDGET = dict(cpus=1, memory_mb=4096, timeout_seconds=1800)  # render the pages, copy what changed
DISPLAY_FINAL_BUDGET = dict(cpus=1, memory_mb=8192, timeout_seconds=4 * 3600)  # ... archive the work tree, freeze hard cases


async def show(spec, stage, unit=None, final=False):
    """Sync the display zone after a stage (#25). A sync never holds or fails the run: it is submitted and
    left to the pool (it renders the run as it is when it executes, so order does not matter). Only the
    final one, which also archives the work tree, is awaited, bounded, and its failure only logged."""
    if not spec.get('storage'):
        return
    label = f"{unit['name']}/{stage}" if unit else stage
    try:
        request = await call(dataset_step, 'display', [spec, label, final])
        if final:
            await asyncio.wait_for(await_pool(spec, request), DISPLAY_FINAL_BUDGET['timeout_seconds'] * 2)
    except Exception as exc:
        await degraded(spec, unit, f'display sync after {label}', exc)


async def degraded(spec, unit, label, exc):
    """Keep a step that failed without failing the run (ecarsi.degraded). In strict mode the activity raises."""
    await call(dataset_step, 'degraded', [spec, unit['name'] if unit else None, label, f'{type(exc).__name__}: {exc}'])


@workflow.defn
class AnalysisUnitWorkflow:
    @workflow.query
    def stage(self):
        return stage_with_waits(self)

    @workflow.run
    async def run(self, spec, unit, progress=None, resume=False):
        from .persample import PersampleWorkflow
        from .crosssample import CrosssampleWorkflow
        from .zoomin import ZoominWorkflow
        # the unit directory whose loop_control.json may brake: L.unit_dir without Path (the sandbox); tests pass bare dicts
        control = f"{spec['output_root']}/{L.UNITS}/{unit['name']}" if spec.get('output_root') and unit.get('name') else None
        async def execute(kind, source, number, run, prefix):
            stage = await call(dataset_step, 'resume_stage' if resume else 'stage', [spec, unit, kind, source, number])
            if resume:
                completed = await call(dataset_step, 'completed', [kind, stage])
                if completed:
                    return completed
            return await (await start_child(run, [stage], prefix + stage['run_id'], control))
        if progress is None:
            self._stage = 'per-sample'
            output = await execute('per_sample', None, 0, PersampleWorkflow.run, 'persample/')
            progress = dict(per_sample=output, input=output, stats=[], rounds=[])
            await show(spec, 'per-sample', unit)
        number = len(progress['stats']) + 1
        self._stage = f'round {number}: cross-sample'
        cross = await execute('cross_sample', progress['input'], number, CrosssampleWorkflow.run, 'cross-sample/')
        await show(spec, f'round{number:02d}/cross-sample', unit)
        await pause_if_asked(spec, unit, 'crosssample')
        self._stage = f'round {number}: zoom-in'
        zoom = await execute('zoom_in', cross, number, ZoominWorkflow.run, 'zoom-in/')
        await show(spec, f'round{number:02d}/zoom-in', unit)
        await pause_if_asked(spec, unit, 'zoomin')
        progress = await call(dataset_step, 'round', [spec, unit, progress, cross, zoom])
        # The round's own Sankey and ledger, the way generation 1 published them: a
        # reader should not have to wait for the release to see where the cells went.
        try:
            request = await call(dataset_step, 'round-ledger', [spec, unit, progress])
            # Bounded: the next round waits on this report, and an infeasible request held it forever (#18).
            limit = 2 * spec['zoom_in']['merge_budget']['timeout_seconds']
            result = await asyncio.wait_for(await_pool(spec, request), limit)
            await call(dataset_step, 'round-ledger-published', [spec, unit, progress, result])
        except Exception as exc:  # a report is not worth failing a finished round over
            await degraded(spec, unit, f'round{number:02d} ledger', exc)
        await show(spec, f'round{number:02d}/decided', unit)
        if progress.get('paused'):
            # The round is complete and published, with its ledger; only the next one is withheld.
            # Failing is how a unit stops without releasing and stays resumable -- the same
            # contract `resume-dataset` already serves, and generation 1's exit code 3.
            raise ApplicationError(progress['paused'], non_retryable=True)
        if 'publication' in progress:
            await brake_if_asked(control)
            self._stage = 'publishing final results'
            request = await call(dataset_step, 'release', [spec, progress['publication']])
            result = await await_pool(spec, request)
            await call(dataset_step, 'released', [progress['publication'], result])
            await show(spec, 'release', unit)
            self._stage = 'complete'
            return progress['publication']
        # Bound each unit's Temporal history; continued runs preserve the child result contract.
        workflow.continue_as_new(args=[spec, unit, progress, True] if resume else [spec, unit, progress])


@workflow.defn
class DatasetWorkflow:
    @workflow.query
    def stage(self):
        return stage_with_waits(self)

    @workflow.run
    async def run(self, spec, resume=False):
        from .coordinator import OrganizeWorkflow
        self._stage = 'organize'
        stage = await call(dataset_step, 'organize', [spec])
        organized = await call(dataset_step, 'completed', ['organize', stage]) if resume else None
        if organized is None:
            organized = await (await start_child(OrganizeWorkflow.run, [stage], 'organize/' + stage['run_id']))
        await show(spec, 'organize')
        units = await call(dataset_step, 'units', [organized])
        pending = {}
        for index, unit in enumerate(units):
            child = await start_child(AnalysisUnitWorkflow.run, [spec, unit, None, True] if resume else [spec, unit],
                                      workflow.info().workflow_id + '/unit-' + str(index))
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
                    # A child's failure arrives as 'Child Workflow execution failed'; record its cause.
                    failures.append(dict(unit=name, error=str(getattr(exc, 'cause', None) or exc)))
        output = await call(dataset_step, 'publish', [spec, results, failures])
        # The finished page; a complete dataset's work tree is archived with it.
        await show(spec, 'published', final=not failures)
        if failures:
            raise ApplicationError('Analysis units failed; completed siblings retained at ' + output, non_retryable=True)
        self._stage = 'complete'
        return output

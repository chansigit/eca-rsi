"""Fan out registered evidence reads; merge only their monotone observation state."""
from copy import deepcopy
from pathlib import Path

from ..warm_pool.state import immutable, reference, verified
from ..warm_pool.state import digest, read

MODULES = {'ecarsi.stages.persample', 'ecarsi.stages.crosssample', 'ecarsi.stages.zoomin'}
READS = {'read_evidence', 'list_evidence', 'sample_inventory', 'type_context',
         'deg_lookup', 'deg_sql', 'check_genes', 'check_qc_scores', 'annotation_status'}
MAX_CALLS = 64


def eligible(spec, calls):
    """read_only alone is insufficient: arbitrary tool state cannot be merged."""
    tools = {t['name']: t for t in spec['tools']}
    selected = [tools.get(c['name'], {}) for c in calls]
    return (1 < len(calls) <= MAX_CALLS and all(t.get('read_only') and t.get('name') in READS
        and len(t['args']) == 6 and t['args'][:1] == ['-m'] and t['args'][1] in MODULES
        and t['args'][2:] == ['tool', t['name'], '{state}', '{arguments}'] for t in selected)
        and len({t['args'][1] for t in selected}) == 1)


def choose(session_ref, reply_path):
    from .session import turn_reply
    spec = verified(session_ref)['spec']
    reply = turn_reply(session_ref, reply_path)
    turn = Path(reply_path).parent.name
    path = Path(spec['output_root']) / (turn + '.parallel.json')
    saved = read(path)
    if saved is not None:
        if saved['reply'] != reference(reply_path):
            raise ValueError('Parallel plan reply changed')
        return saved['enabled']
    # Recovery must not reinterpret already dispatched ordered requests.
    existing = any((Path(spec['pool_root']) / 'requests' /
        (spec['session_id'] + '.tool-' + digest([turn, c['call_id']])[:16]) / 'request.json').exists()
        for c in reply['calls'])
    enabled = eligible(spec, reply['calls']) and not existing
    immutable(path, dict(reply=reference(reply_path), enabled=enabled))
    return enabled


def merge_states(base, states):
    """Fail closed on data/label/version changes, removals, or unknown state fields."""
    merged = deepcopy(base)
    lists = {'read', 'lookups', 'inventories', 'type_read'}
    for state in states:
        if set(state) - set(base) - lists:
            raise ValueError('Parallel evidence introduced unknown state')
        for key in set(base) | set(state):
            before, after = base.get(key), state.get(key)
            if key in lists:
                before = before or []
                after = after or []
                if not isinstance(after, list) or any(v not in after for v in before):
                    raise ValueError('Parallel evidence removed an observation')
                current = merged.setdefault(key, [])
                for value in after:
                    if value not in current:
                        current.append(value)
            elif key == 'seen':
                if not isinstance(after, dict) or set(after) != set(before):
                    raise ValueError('Parallel evidence changed observation schema')
                for name, old in before.items():
                    new = after[name]
                    if name in {'figures', 'tables'} and isinstance(new, list) and all(v in new for v in old):
                        merged[key][name] = sorted(set(merged[key][name]) | set(new))
                    elif name in {'genes', 'qc'} and type(new) is bool and (not old or new):
                        merged[key][name] |= new
                    elif old != new:
                        raise ValueError('Parallel evidence changed observation state')
            elif key == 'qc' and type(after) is bool and (not before or after):
                merged[key] |= after
            elif before != after or (key in base) != (key in state):
                raise ValueError('Parallel evidence changed scientific state: ' + key)
    return merged


def budget(request, directory, spec, tool, state_ref):
    """Readers reserve reader memory; matrix tools reuse measured compute peaks."""
    path = Path(directory) / 'tool-resources.json'
    saved = read(path)
    if saved:
        if saved['base_digest'] != digest(request):
            raise ValueError('Tool resource request changed')
        return saved['request']
    if (Path(spec['pool_root']) / 'requests' / request['request_id'] / 'request.json').exists():
        return request
    args = tool['args']
    if len(args) != 6 or args[1] not in MODULES or state_ref is None:
        return request
    state = verified(state_ref)
    optimized = request
    light = tool['name'] in READS - {'check_genes', 'check_qc_scores', 'annotation_status'}
    if request['args'][:2] == ['-m', 'ecarsi.bridge.evidence']:
        # Existing evidence batches may also execute QC, which loads the matrix.
        light = light and args[1] != 'ecarsi.stages.persample' and state.get('phase') != 'quality' and state.get('kind') != 'lineage'
    if light:
        optimized = dict(request, memory_mb=min(request['memory_mb'], 2048))
    elif args[1] != 'ecarsi.stages.persample' and tool['name'] in READS:
        evidence = verified(state['evidence'])
        computed = evidence.get('prepared')
        if computed:
            from ..warm_pool.budget import from_compute
            optimized = from_compute(request, computed, Path(directory) / 'matrix-resources.json', spec['pool_root'])
    immutable(path, dict(base_digest=digest(request), request=optimized))
    return optimized

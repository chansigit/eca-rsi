"""Bounded evidence batches using the registered stage tools and checks.

`plan` runs on the control side when a session registers this module as its planner and turns a
single evidence read into one pool request that also pulls the mandatory observations; `execute`
runs that request on a pool worker."""
import importlib
import json
from pathlib import Path

from ..warm_pool.state import immutable, reference, verified
from ..warm_pool.state import digest, read, save

MODULES = {'ecarsi.stages.persample', 'ecarsi.stages.crosssample', 'ecarsi.stages.zoomin'}
PAGES = {'read_evidence', 'sample_inventory', 'type_context', 'list_evidence'}
MAX_CALLS = 8
TEXT_BYTES = 240000
IMAGE_BYTES = 12 * 2**20  # Base64 representation of at most 9 MiB of PNGs.


def plan(request, directory, session, batched=False):
    """Apply only before first submission; existing execution plans stay exact. A batch the model
    requested itself is never prefetched (its other calls would return the same evidence twice)."""
    from .execution import plan as original_plan
    directory = Path(directory)
    if batched:
        return original_plan(request, directory, session['pool_root'])
    path = directory / 'execution.json'
    args = request['args']
    if (read(path) is not None or
            (Path(session['pool_root']) / 'requests' / request['request_id'] / 'request.json').exists() or
            len(args) != 6 or args[0] != '-m' or args[1] not in MODULES or args[2] != 'tool' or
            args[3] not in PAGES):
        return original_plan(request, directory, session['pool_root'])
    registered = {t['name']: t for t in session['tools'] if t['args'] ==
                  ['-m', args[1], 'tool', t['name'], '{state}', '{arguments}']}
    allowed = sorted(set(registered) & (PAGES | {'check_qc_scores'}))
    if args[3] not in allowed:
        return original_plan(request, directory, session['pool_root'])
    packet = immutable(directory / 'evidence-batch.json', dict(module=args[1], name=args[3],
        state=args[4], arguments=args[5], allowed=allowed,
        multimodal=registered[args[3]].get('multimodal', False)))
    # QC can load the matrix: reserve its registered budget before packing it.
    budget = [registered[n] for n in allowed]
    wrapped = dict(request, args=['-m', 'ecarsi.stages.evidence', packet['path']],
        cpus=max(t['cpus'] for t in budget), memory_mb=max(t['memory_mb'] for t in budget),
        inputs=[*request['inputs'], packet, reference(Path(__file__))])
    if args[1] == 'ecarsi.stages.persample' and read(args[4])['version'] == 0:
        from ..warm_pool.budget import from_compute
        wrapped = from_compute(wrapped, read(args[4])['bundle'],
                               directory / 'evidence-resources.json', session['pool_root'])
    immutable(path, dict(base_digest=digest(request), request=wrapped))
    return wrapped


def next_required(module, state, allowed, multimodal):
    """Only mandatory observations; never choose markers or scientific decisions."""
    if module == 'ecarsi.stages.persample':
        from .persample import evidence_files
        figures, tables = evidence_files(verified(state['bundle']))
        if not state['seen']['qc'] and 'check_qc_scores' in allowed:
            return 'check_qc_scores', {}
        if 'read_evidence' in allowed:
            missing = sorted(set(range(0, max(1, len(tables)), 60000)) - set(state['seen']['tables']))
            if missing:
                return 'read_evidence', dict(kind='tables', offset=missing[0])
            missing = [i for i, f in enumerate(figures) if f not in state['seen']['figures']]
            if missing and multimodal:
                return 'read_evidence', dict(kind='figures', offset=missing[0])
    else:
        bundle = verified(state['evidence'])
        if state.get('phase') == 'inclusion':
            if 'sample_inventory' in allowed:
                for offset, sample in enumerate(bundle['samples']):
                    if sample['sample'] not in state.get('inventories', []):
                        return 'sample_inventory', dict(offset=offset)
            if multimodal and 'read_evidence' in allowed:
                for sample in bundle['samples']:
                    paths = [p for p in bundle['files'] if p.startswith(sample['sample'] + '/figures/')
                             and 'umap_clusters' in p and p.endswith('.png')]
                    if paths and not set(paths).intersection(state['read']):
                        return 'read_evidence', dict(path=paths[0], offset=0)
        elif state.get('phase') == 'quality' or state.get('kind') == 'lineage':
            if not state['qc'] and 'check_qc_scores' in allowed:
                return 'check_qc_scores', {}
            if state.get('types') and 'type_context' in allowed:
                entries = verified(state['types'])['proposal']['clusters']
                for offset in range(0, len(entries), 10):
                    if any(str(e['cluster_id']) not in state.get('type_read', []) for e in entries[offset:offset+10]):
                        return 'type_context', dict(offset=offset)
    return None


def execute(packet_path):
    packet = read(packet_path)
    if packet['module'] not in MODULES or not set(packet['allowed']) <= PAGES | {'check_qc_scores'}:
        raise ValueError('Unregistered evidence batch')
    tool = importlib.import_module(packet['module']).tool
    state_path = packet['state']
    first = (packet['name'], read(packet['arguments']))
    selected = first
    results, images, calls = [], [], []
    primary = None
    pending = None
    seen_calls = set()
    # ponytail: at most eight existing tool calls per grant. No parallel state
    # merge; the next call always receives the previous accepted state.
    for index in range(MAX_CALLS):
        name, arguments = selected
        identity = digest([name, arguments])
        if name not in packet['allowed'] or identity in seen_calls:
            raise ValueError('Evidence batch did not advance')
        seen_calls.add(identity)
        destination = Path.cwd() / f'evidence-{index}'
        destination.mkdir(exist_ok=True)
        arg = immutable(destination / 'arguments.json', arguments)
        tool(name, state_path, arg['path'], destination)
        result = read(destination / 'result.json')
        if (packet['module'] == 'ecarsi.stages.crosssample' and name == 'read_evidence'
                and result.get('next_offset') is not None and not result.get('is_error')):
            # The legacy reader peeks one character before tell(), skipping it
            # on the next page. Compute the text-stream cookie before that peek.
            from .crosssample import artifact
            bundle = verified(verified(reference(state_path))['evidence'])
            with artifact(bundle, arguments['path']).open() as stream:
                stream.seek(arguments['offset'])
                stream.read(len(result['content']))
                result['next_offset'] = stream.tell()
        more_images = result.get('images', [])
        body = {k: v for k, v in result.items() if k not in {'state', 'images'}}
        entry = dict(tool=name, arguments=arguments, result=body,
                     image_start=len(images), image_count=len(more_images))
        if index and (len(json.dumps([*results, entry]).encode()) > TEXT_BYTES or
                      len(images) + len(more_images) > 16 or
                      sum(map(len, images + more_images)) > IMAGE_BYTES):
            pending = selected
            break  # Do not publish the state of evidence that was not returned.
        if primary is None:
            primary = body.copy()
        results.append(entry)
        calls.append(dict(tool=name, arguments=arguments))
        images.extend(more_images)
        state_path = result['state']['path']
        state_ref = result['state']
        failed = result.get('error') or result.get('is_error') or result.get('accepted') is False
        next_offset = result.get('next_offset')
        if not failed and name == first[0] and {k:v for k,v in arguments.items() if k != 'offset'} == {
                k:v for k,v in first[1].items() if k != 'offset'}:
            if 'next_offset' in result:
                primary['next_offset'] = next_offset
        if failed:
            pending = selected
            break
        if next_offset is not None:
            if next_offset <= arguments.get('offset', -1):
                raise ValueError('Evidence pagination did not advance')
            selected = name, dict(arguments, offset=next_offset)
        else:
            selected = next_required(packet['module'], verified(state_ref),
                                     packet['allowed'], packet['multimodal'])
        pending = selected
        if selected is None:
            break
    primary.update(state=state_ref, additional_evidence=results[1:], evidence_batch=dict(
        calls=calls, pending=dict(tool=pending[0], arguments=pending[1]) if pending else None,
        instruction='Review all returned evidence, including additional_evidence and indexed images. '
                    'These observations were read by the registered Worker tools. Do not fetch them again. '
                    'If pending is present, request that tool next. Marker selection and decisions still require your judgment.'))
    if images:
        primary['images'] = images
    save('result.json', primary)


if __name__ == '__main__':
    import sys
    execute(sys.argv[1])

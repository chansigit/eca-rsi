"""Versioned execution plans for deterministic evidence work on Pool workers."""
import json
from pathlib import Path

from .agent_session import immutable, reference
from .warm_pool.state import digest, read, save


def plan(request, directory, pool_root):
    """Keep prior requests exact; record any optimization before first submission."""
    path = Path(directory) / 'execution.json'
    saved = read(path)
    if saved is not None:
        if saved['base_digest'] != digest(request):
            raise ValueError('Registered tool execution changed')
        return saved['request']
    previous = read(Path(pool_root) / 'requests' / request['request_id'] / 'request.json')
    if previous is not None:
        return request  # submit still checks the original full request identity.
    args = request['args']
    optimized = request
    if len(args) == 6 and args[:3] == ['-m', 'ecarsi.persample_v2', 'tool']:
        name = args[3]
        if name == 'read_evidence':
            # No expression matrix is loaded; even the image response is bounded
            # by the registered 9 MiB image budget and 16 MiB result contract.
            optimized = dict(request, memory_mb=min(request['memory_mb'], 256))
        if name == 'submit_annotation' or name == 'read_evidence' and read(args[5])['kind'] == 'tables':
            packet = immutable(Path(directory) / 'evidence-execution.json',
                               dict(name=name, state=args[4], arguments=args[5]))
            optimized = dict(optimized, args=['-m', 'ecarsi.agent_tool_execution', packet['path']],
                             inputs=[*request['inputs'], packet, reference(Path(__file__))])
    immutable(path, dict(base_digest=digest(request), request=optimized))
    return optimized


def execute(packet_path):
    from .persample_v2 import tool, evidence_files
    from .agent_session import verified
    packet = read(packet_path)
    name, state, arguments = packet['name'], packet['state'], read(packet['arguments'])
    if name == 'read_evidence':
        if arguments['kind'] != 'tables':
            raise ValueError('Automatic pagination only reads registered tables')
        chunks = []
        # ponytail: four full pages bound each response to 240k text characters;
        # larger evidence keeps an explicit next_offset for a subsequent request.
        for page in range(4):
            destination = Path.cwd() / f'page-{page}'
            destination.mkdir(exist_ok=True)
            arg = immutable(destination / 'arguments.json', arguments)
            tool(name, state, arg['path'], destination)
            result = read(destination / 'result.json')
            chunks.append(result.get('text', ''))
            next_offset = result.get('next_offset')
            if result.get('error') or next_offset is None:
                break
            if next_offset <= arguments['offset']:
                raise ValueError('Evidence pagination did not advance')
            state = result['state']['path']
            arguments = dict(arguments, offset=next_offset)
        result['text'] = ''.join(chunks)
        result['pages_returned'] = len(chunks)
        save('result.json', result)
    elif name == 'submit_annotation':
        tool(name, state, packet['arguments'], Path.cwd())
        result = read('result.json')
        if result.get('accepted') is False:
            current = verified(result['state'])
            figures, tables = evidence_files(verified(current['bundle']))
            seen = current['seen']
            missing = dict(figures=sorted(set(figures) - set(seen['figures'])),
                table_offsets=sorted(set(range(0, max(1, len(tables)), 60000)) - set(seen['tables'])),
                check_genes=not seen['genes'], check_qc_scores=not seen['qc'])
            result['missing_evidence'] = missing
            if any(missing.values()):
                result['error'] += '. Read the missing evidence before resubmitting: ' + json.dumps(missing)
            save('result.json', result)
    else:
        raise ValueError('Unregistered evidence execution')


if __name__ == '__main__':
    import sys
    execute(sys.argv[1])

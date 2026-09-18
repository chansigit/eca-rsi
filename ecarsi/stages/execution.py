"""Versioned execution plans for deterministic evidence work on Pool workers (per-sample tools)."""
import json
from pathlib import Path

from ..warm_pool.state import immutable, reference
from ..warm_pool.state import digest, read, save


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
    if len(args) == 6 and args[:3] == ['-m', 'ecarsi.stages.persample', 'tool']:
        name = args[3]
        if name in {'check_genes', 'check_qc_scores', 'submit_annotation'} and read(args[4])['version'] == 0:
            from ..warm_pool.budget import from_compute
            optimized = from_compute(optimized, read(args[4])['bundle'], Path(directory) / 'resources.json', pool_root)
        if name == 'read_evidence':
            # No expression matrix is loaded; even the image response is bounded
            # by the registered 9 MiB image budget and 16 MiB result contract.
            optimized = dict(request, memory_mb=min(request['memory_mb'], 256))
        if name == 'submit_annotation' or name == 'read_evidence' and read(args[5])['kind'] == 'tables':
            packet = immutable(Path(directory) / 'evidence-execution.json',
                               dict(name=name, state=args[4], arguments=args[5]))
            optimized = dict(optimized, args=['-m', 'ecarsi.stages.execution', packet['path']],
                             inputs=[*request['inputs'], packet, reference(Path(__file__))])
    immutable(path, dict(base_digest=digest(request), request=optimized))
    return optimized


def compact_tables(text, genes=15, contaminants=8, min_connectivity=0.05):
    """Render the per-sample evidence tables for a model, not a spreadsheet: top markers per cluster on one
    line with two decimals, the contamination leaders, and the PAGA graph as a sparse neighbour list. Fat
    sample TSP25 (43 clusters) shipped 187k characters of raw CSV this way and overran the provider's context
    at the third turn, three generations in a row (2026-09-18). Blocks this function does not know stay as
    they are; a block cut mid-row by paging loses that row only."""
    import csv, io
    blocks = []
    for block in text.split("\n\n"):
        block = block.strip("\n")  # each CSV ends with a newline, so the separator arrives as three
        if not block:
            continue
        name, _, body = block.partition("\n")
        try:
            rows = list(csv.DictReader(io.StringIO(body)))
        except csv.Error:
            rows = []
        if not rows or None in rows[-1].values():
            rows = rows[:-1] if rows else rows
        if name.startswith("de_top_genes_") and rows and {"group", "names", "logfoldchanges", "pct1", "pct2"} <= set(rows[0]):
            lines = [name + "  (top %d markers per cluster: gene lfc pct_in/pct_out)" % genes]
            per = {}
            for r in rows:
                per.setdefault(r["group"], []).append(r)
            for group, items in per.items():
                lines.append(group + ": " + ", ".join(f"{r['names']} {_num(r['logfoldchanges'])} {_num(r['pct1'])}/{_num(r['pct2'])}" for r in items[:genes]))
            blocks.append("\n".join(lines))
        elif name.startswith("decontx_top_genes_") and rows and {"cluster", "gene", "contam_fraction_of_gene_counts"} <= set(rows[0]):
            lines = [name + "  (top %d ambient-contaminated genes per cluster: gene fraction_of_gene_counts)" % contaminants]
            per = {}
            for r in rows:
                per.setdefault(r["cluster"], []).append(r)
            for cluster, items in per.items():
                lines.append(cluster + ": " + ", ".join(f"{r['gene']} {_num(r['contam_fraction_of_gene_counts'])}" for r in items[:contaminants]))
            blocks.append("\n".join(lines))
        elif name.startswith("paga_connectivities_") and rows:
            key = next(iter(rows[0]))
            lines = [name + "  (neighbours with connectivity >= %.2f)" % min_connectivity]
            for r in rows:
                near = [(c, float(v)) for c, v in r.items() if c != key and c != r[key] and _float(v) >= min_connectivity]
                near.sort(key=lambda cv: -cv[1])
                lines.append(r[key] + ": " + (", ".join(f"{c} {v:.2f}" for c, v in near) or "none"))
            blocks.append("\n".join(lines))
        else:
            blocks.append(block)
    return "\n\n".join(blocks)


def _float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _num(value):
    return f"{_float(value):.2f}"


def execute(packet_path):
    from .persample import tool, evidence_files
    from ..warm_pool.state import verified
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
        result['text'] = compact_tables(''.join(chunks))
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

"""Zoom-in worker protocol v3: zoomin_v2's numerics untouched, the model-facing contract repaired.

Every saved session pins its worker program by hash, so zoomin_v2.py cannot change while
sessions are in flight. New sessions register this module instead; it delegates all work to
v2 and changes only what the model sees:
  * deg_lookup asks for cluster or gene (key and thresholds optional), as its documentation
    already said; the v2 schema required all five fields and rejected the thresholds;
  * proposal_json may be handed over as the JSON object itself (the coordinator canonicalises
    it), which removes the double-escaping errors;
  * the prompt carries the required call order, and a rejected submission comes back with what
    was missing: the pending type scope, every 2.0 cluster with its type intersections, the
    lineage labels, the valid island names.
Measured 2026-09-16 over 6 h: 56 % of submit_quality, 32 % of submit_types and 64 % of
submit_plan calls were rejected; 13.9 % of all model turns were such retries.

Contract v4 (2026-09-17), same numerics, fewer turns:
  * the prompt already holds what list_evidence and annotation_status would answer at turn 0
    (evidence paths, UMAP figures, pending type clusters, every 2.0 cluster with its type
    intersections), so the first turn can read evidence; both tools remain, take no arguments
    and return everything in one call (a bundle has about 60 paths; paging cost a turn per page
    and a fixed page count in the checklist skipped the 61st path in 62 sessions);
  * an accepted submit_quality completes the session. finalize_annotation only copied the
    accepted state into its result; in 247 finished sessions no model revised after acceptance,
    so the step was one model turn (34 s and a full prompt) for nothing.
"""
import json
from pathlib import Path

from . import zoomin_v2 as v2
from .agent_session import immutable, reference, verified
from .warm_pool.state import read, save

PROGRAM = 'ecarsi.zoomin_v3'
SUBMISSIONS = {'submit_plan', 'submit_types', 'submit_quality'}
THRESHOLDS = {name: {'type': ['number', 'null']} for name in ('min_logfc', 'max_padj', 'min_pct1', 'max_pct2')}
OBJECT_NOTE = ' proposal_json may be the JSON object itself rather than an escaped string.'
# v2 pages these reads; here they take no arguments and the pages are merged on these list keys.
PAGED = {'list_evidence': ('content',), 'annotation_status': ('types', 'quality')}
PAGED_DESCRIPTIONS = {'list_evidence': 'List every evidence path (the prompt already lists them).',
                      'annotation_status': 'Accepted type and quality entries, the pending type clusters and every 2.0 cluster with its type intersections (the prompt already lists them).'}
NO_ARGUMENTS = {'type': 'object', 'properties': {}, 'required': [], 'additionalProperties': False}


def deg_lookup_schema(fields):
    """Key plus cluster or gene; view, top_n and thresholds optional, as DEG_TOOL_DOC describes."""
    return {'type': 'object', 'properties': {**fields, **THRESHOLDS},
            'anyOf': [{'required': ['cluster']}, {'required': ['gene']}], 'additionalProperties': False}


def evidence_paths(bundle):
    return [n for n in bundle['files'] if not n.startswith('deg_input/') and not n.endswith('.h5ad')]


def intersections(bundle):
    """2.0 cluster -> {1.0 cluster: cells}, from the table the assemble step writes."""
    import pandas as pd
    table = pd.read_csv(v2.artifact(bundle, 'type_quality_intersections.csv'), index_col=0)
    return {str(q): {str(t): int(n) for t, n in row.items() if n} for q, row in table.iterrows()}


def cluster_order(name):
    return (0, int(name)) if name.isdigit() else (1, name)


def inline_context(bundle, kind):
    """What list_evidence and annotation_status answer at turn 0, so the first turn reads evidence."""
    from zmip.scheduled import TYPE_KEY, QUALITY_KEY
    paths = evidence_paths(bundle)
    lines = ['Evidence files (read_evidence paths): ' + json.dumps(paths),
             'UMAP figures: ' + json.dumps([p for p in paths if p.endswith('.png') and 'umap' in p])]
    if kind != 'plan' and 'type_quality_intersections.csv' in bundle['files']:
        table = intersections(bundle)
        types = sorted({t for row in table.values() for t in row}, key=cluster_order)
        lines.append('Pending type clusters for submit_types (cluster_key %s): %s' % (TYPE_KEY, json.dumps(types)))
        lines.append('Quality clusters (cluster_key %s) with their type intersections and cell counts; submit_quality decides '
                     'each intersection exactly once: %s' % (QUALITY_KEY, json.dumps(table)))
    return '\n'.join(lines)


def agent_spec(spec, evidence, kind, parent):
    session = v2.agent_spec(spec, evidence, kind, parent)
    session['prompt'] += '\n\n' + inline_context(verified(evidence), kind)
    checklist = Path(__file__).with_name('prompts') / ('zoomin-%s-checklist-v4.md' % ('plan' if kind == 'plan' else 'annotation'))
    session['prompt'] += '\n\n' + checklist.read_text()
    tools = []
    for tool in session['tools']:
        if tool['name'] == 'finalize_annotation':
            continue  # an accepted submit_quality completes the session
        tool['args'] = ['-m', PROGRAM, 'tool', tool['name'], '{state}', '{arguments}']
        tool['inputs'] = [reference(Path(__file__)), *tool['inputs']]
        if tool['name'] in PAGED:
            tool['parameters'] = dict(NO_ARGUMENTS)
            tool['description'] = PAGED_DESCRIPTIONS[tool['name']]
        elif tool['name'] == 'deg_lookup':
            tool['parameters'] = deg_lookup_schema(tool['parameters']['properties'])
            tool['description'] += ' Give cluster or gene; key defaults to the type key, view/top_n/thresholds are optional.'
        elif tool['name'] in SUBMISSIONS:
            tool['description'] += OBJECT_NOTE
        tools.append(tool)
    session['tools'] = tools
    if kind != 'plan':
        session['completion_tool'] = 'submit_quality'
    return session


def canonical_arguments(name, args):
    """What v2 expects on disk: a JSON string proposal, a fully keyed deg_lookup, no null thresholds."""
    if name in SUBMISSIONS and isinstance(args.get('proposal_json'), (dict, list)):
        return dict(args, proposal_json=json.dumps(args['proposal_json']))
    if name == 'deg_lookup':
        from zmip.scheduled import TYPE_KEY
        filled = {k: v for k, v in args.items() if v is not None}
        filled.setdefault('key', TYPE_KEY)
        for field in ('cluster', 'gene'):
            filled.setdefault(field, '')
        return filled
    return args


def paged(name, state_path, destination):
    """Every v2 page in one result. These reads leave the state unchanged, so the last page's state stands."""
    merged, offset, number = None, 0, 0
    while True:
        arguments = immutable(destination / ('arguments-v3-%d.json' % number), {'offset': offset})['path']
        v2.tool(name, state_path, arguments, destination)
        page = read(destination / 'result.json')
        if page.get('is_error'):
            return
        if merged is None:
            merged = page
        else:
            for key in PAGED[name]:
                merged[key] = merged[key] + page[key]
            if 'intersections' in page:
                merged['intersections'].update(page['intersections'])
            merged['state'] = page['state']
        if page.get('next_offset') is None:
            merged['next_offset'] = None
            save(destination / 'result.json', merged)
            return
        offset, number = page['next_offset'], number + 1


def complete(result, destination):
    """An accepted quality proposal completes the session: what finalize_annotation returned, without the turn."""
    state = read(result['state']['path'])
    result.update(accepted=True, evidence=state['evidence'], types=state['types'], quality=state['quality'],
                  removal_fraction=state['removal_fraction'])
    save(destination / 'result.json', result)


def coverage_hint(obs, state, own, other):
    from zmip.scheduled import TYPE_KEY, QUALITY_KEY, partitions
    table = partitions(obs)
    intersections = {str(q): {str(t): int(n) for t, n in row.items() if n} for q, row in table.iterrows()}
    if state.get('types_complete'):
        types = 'Type coverage is already accepted; do not resubmit types.'
    else:
        scope = state.get('type_scope') or sorted(obs[TYPE_KEY].astype(str).unique())
        types = 'submit_types: cluster_key ' + TYPE_KEY + ', exactly these pending clusters: ' + json.dumps(scope) + '.'
    return ('Required coverage. ' + types + ' submit_quality: cluster_key ' + QUALITY_KEY + ', one entry per 2.0 cluster whose '
            'decisions name each of its type intersections exactly once; intersections with cell counts: '
            + json.dumps(intersections) + '. coarse_label for keep must be one of ' + json.dumps(own)
            + '; reassign_to must be one of ' + json.dumps(other) + '.')


def island_hint(bundle):
    import pandas as pd
    if 'lineage_islands.csv' not in bundle['files']:
        return ''
    islands = pd.read_csv(v2.artifact(bundle, 'lineage_islands.csv'), index_col=0)
    names = [c for c in islands.columns if c != 'noise']
    return ('Island names in this evidence: ' + json.dumps(names) + '. shared_island_reviews keys must be island '
            'names whose coarse labels your plan splits across lineages; omit the key when nothing is split.')


def error_hint(name, content, state):
    if content.startswith('Complete required checks') or content.startswith('Read the lineage UMAP'):
        return 'Do the listed reads first (they can be batched in one turn), then resubmit the same proposal.'
    if 'Expecting' in content or 'Extra data' in content:
        return 'proposal_json was not valid JSON; pass the proposal as a JSON object instead of an escaped string.'
    if name in {'submit_types', 'submit_quality'}:
        bundle = verified(state['evidence'])
        own, other = v2.lineage_labels(bundle)
        return coverage_hint(v2.data_from(bundle).obs, state, own, other)
    if name == 'submit_plan' and 'island' in content:
        return island_hint(verified(state['evidence']))
    return ''


def tool(name, state_path, args_path, destination):
    destination = Path(destination)
    if name in PAGED:
        return paged(name, state_path, destination)
    args = read(args_path)
    fixed = canonical_arguments(name, args)
    if fixed != args:
        args_path = immutable(destination / 'arguments-v3.json', fixed)['path']
    v2.tool(name, state_path, args_path, destination)
    result = read(destination / 'result.json')
    if result.get('is_error'):
        try:
            hint = error_hint(name, result['content'], read(state_path))
        except Exception:  # noqa: BLE001 - a hint must never turn a correctable error into a crash
            hint = ''
        if hint:
            result['content'] = (result['content'] + '\n' + hint)[:16000]
            save(destination / 'result.json', result)
    elif name == 'submit_quality':
        complete(result, destination)


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action')
    parser.add_argument('args', nargs='+')
    a = parser.parse_args()
    if a.action == 'tool':
        tool(*a.args, Path.cwd())
    elif a.action == 'agent':
        packet = read(a.args[0])
        save(Path.cwd() / 'agent.json', agent_spec(packet['spec'], packet['refs'][0], packet['kind'], packet['request_id']))
    else:
        raise ValueError('zoomin_v3 serves agent and tool only; numerical operations stay on zoomin_v2')


if __name__ == '__main__':
    main()

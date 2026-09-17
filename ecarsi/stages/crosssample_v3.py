"""Cross-sample worker protocol v3: crosssample_v2's numerics untouched, the model-facing contract repaired.

Saved sessions pin crosssample_v2.py by hash, so it cannot change in flight. New sessions
register this module, which delegates everything to v2 and changes only what the model sees:
  * deg_lookup asks for cluster or gene (key, view, top_n and thresholds optional);
  * proposal_json may be the JSON object itself;
  * the prompt carries the required call order, and a rejected submit_decision comes back
    with what was missing: the samples to cover, the assigned type clusters, the unread type
    entries, the valid coarse boundaries.
Measured 2026-09-16 over 6 h: 64 % of submit_decision calls were rejected.

Contract v4 (2026-09-17): the annotation prompts already list the evidence paths, and
list_evidence and type_context take no arguments and return everything in one call (an
annotation bundle has 52 paths; type entries number a few dozen). sample_inventory keeps one
sample per call: an inventory is 6-9 KB and a tissue has up to 30 of them.
"""
import json
from pathlib import Path

from . import crosssample as v2
from ..warm_pool.state import immutable, reference, verified
from . import PROMPTS
from ..warm_pool.state import read, save

PROGRAM = 'ecarsi.stages.crosssample_v3'
THRESHOLDS = {name: {'type': ['number', 'null']} for name in ('min_logfc', 'max_padj', 'min_pct1', 'max_pct2')}
OBJECT_NOTE = ' proposal_json may be the JSON object itself rather than an escaped string.'
# v2 pages these reads; here they take no arguments and the pages are merged on these list keys.
PAGED = {'list_evidence': ('content',), 'type_context': ('content',)}
PAGED_DESCRIPTIONS = {'list_evidence': 'List every evidence path (the annotation prompt already lists them).',
                      'type_context': 'Every accepted or preserved type entry in one call.'}
NO_ARGUMENTS = {'type': 'object', 'required': [], 'additionalProperties': False,
                'properties': {'offset': {'type': 'integer', 'description': 'Ignored: the whole listing comes back in one call.'}}}


def deg_lookup_schema(fields):
    return {'type': 'object', 'properties': {**fields, **THRESHOLDS},
            'anyOf': [{'required': ['cluster']}, {'required': ['gene']}], 'additionalProperties': False}


def evidence_paths(bundle):
    return [n for n in bundle['files'] if not n.startswith('deg_input/') and not n.endswith('.h5ad')]


def inline_context(bundle):
    """What list_evidence answers at turn 0 (52 paths), so the first turn reads evidence."""
    paths = evidence_paths(bundle)
    return ('Evidence files (read_evidence paths): ' + json.dumps(paths) + '\nFigures: '
            + json.dumps([p for p in paths if p.endswith('.png')]))


def agent_spec(spec, evidence_ref, phase, parent, types_ref=None):
    session = v2.agent_spec(spec, evidence_ref, phase, parent, types_ref)
    if phase != 'inclusion':
        session['prompt'] += '\n\n' + inline_context(verified(evidence_ref))
    checklist = PROMPTS / ('crosssample-%s-checklist-v4.md' % ('inclusion' if phase == 'inclusion' else 'annotation'))
    session['prompt'] += '\n\n' + checklist.read_text()
    for tool in session['tools']:
        tool['args'] = ['-m', PROGRAM, 'tool', tool['name'], '{state}', '{arguments}']
        tool['inputs'] = [reference(Path(__file__)), *tool['inputs']]
        if tool['name'] in PAGED:
            tool['parameters'] = dict(NO_ARGUMENTS)
            tool['description'] = PAGED_DESCRIPTIONS[tool['name']]
        elif tool['name'] == 'deg_lookup':
            tool['parameters'] = deg_lookup_schema(tool['parameters']['properties'])
            tool['description'] += ' Give cluster or gene; key defaults to the base key, view/top_n/thresholds are optional.'
        elif tool['name'] == 'submit_decision':
            tool['description'] += OBJECT_NOTE
    return session


def lenient_json(text):
    """The first complete JSON value; trailing quotes, fences or whitespace are dropped (Eye 2026-09-17:
    a proposal ending in }]}" was rejected 25 times in a row, 167k tokens each, for one stray quote)."""
    try:
        return json.loads(text)
    except ValueError:
        value, end = json.JSONDecoder().raw_decode(text.lstrip())
        if text.lstrip()[end:].strip(' \t\r\n"`\''):
            raise
        return value


def canonical_arguments(name, args):
    if name == 'submit_decision' and isinstance(args.get('proposal_json'), (dict, list)):
        return dict(args, proposal_json=json.dumps(args['proposal_json']))
    if name == 'submit_decision' and isinstance(args.get('proposal_json'), str):
        try:
            return dict(args, proposal_json=json.dumps(lenient_json(args['proposal_json'])))
        except ValueError:
            return args  # v2 reports the parse error and the hint explains the object form
    if name == 'deg_lookup':
        filled = {k: v for k, v in args.items() if v is not None}
        for field in ('cluster', 'gene'):
            filled.setdefault(field, '')
        return filled
    return args


def boundary_hint(bundle, proposal):
    """The adjacent coarse-label pairs the type proposal must review, as _check_coarse_boundaries derives them."""
    from msp.evidence import load_paga_neighbors
    entries = {str(e.get('cluster_id')): e for e in proposal.get('clusters', []) if isinstance(e, dict)}
    paga = load_paga_neighbors(v2.artifact(bundle, 'deg.sqlite').parent, v2.BASE)
    pairs = set()
    for cluster, neighbours in paga.items():
        entry = entries.get(str(cluster))
        if not entry or entry.get('action') != 'keep':
            continue
        for other in neighbours:
            neighbour = entries.get(str(other))
            if neighbour and neighbour.get('action') == 'keep':
                labels = tuple(sorted({str(entry.get('coarse_label', '')).strip(), str(neighbour.get('coarse_label', '')).strip()}))
                if len(labels) == 2:
                    pairs.add(labels)
    return ('boundary_reviews must contain exactly one review for each of these adjacent coarse-label pairs of your '
            'kept clusters, and no others: ' + json.dumps(sorted(pairs)))


def error_hint(name, content, state, args):
    if name != 'submit_decision':
        return ''
    if 'Expecting' in content or 'Extra data' in content:
        return 'proposal_json was not valid JSON; pass the proposal as a JSON object instead of an escaped string.'
    if content.startswith('Query DEG and read a figure'):
        return ('Call deg_lookup on an assigned cluster and read_evidence on one .png path from list_evidence '
                '(both in one turn), then resubmit the same proposal.')
    bundle = verified(state['evidence'])
    phase = state['phase']
    if phase == 'inclusion':
        return 'Samples to cover exactly once: ' + json.dumps([s['sample'] for s in bundle['samples']]) + '.'
    if content.startswith('Read accepted type context'):
        clusters = sorted(v2._data(bundle).obs[v2.BASE].astype(str).unique())
        unread = sorted(set(clusters) - set(state.get('type_read', [])))
        return ('type_context has not been read for clusters ' + json.dumps(unread)
                + '; call type_context once (it takes no arguments and returns every cluster), then resubmit.')
    if 'coarse boundary' in content:
        try:
            proposal = json.loads(args['proposal_json']) if isinstance(args.get('proposal_json'), str) else args.get('proposal_json', {})
        except ValueError:
            return ''
        return boundary_hint(bundle, proposal)
    if phase == 'type':
        return 'Assigned type clusters to cover exactly once: ' + json.dumps(bundle.get('type_scope', [])) + '.'
    clusters = sorted(v2._data(bundle).obs[v2.BASE].astype(str).unique())
    return 'Quality proposal must decide every cluster of ' + v2.BASE + ': ' + json.dumps(clusters) + '.'


def paged(name, state_path, destination):
    """Every v2 page in one result; the last page's state carries what the pages recorded."""
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
            merged['state'] = page['state']
        if page.get('next_offset') is None:
            merged['next_offset'] = None
            save(destination / 'result.json', merged)
            return
        offset, number = page['next_offset'], number + 1


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
            hint = error_hint(name, result['content'], read(state_path), fixed)
        except Exception:  # noqa: BLE001 - a hint must never turn a correctable error into a crash
            hint = ''
        if hint:
            result['content'] = (result['content'] + '\n' + hint)[:16000]
            save(destination / 'result.json', result)


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation')
    parser.add_argument('args', nargs='+')
    a = parser.parse_args()
    if a.operation == 'tool':
        tool(a.args[0], Path(a.args[1]), Path(a.args[2]), Path.cwd())
    elif a.operation == 'agent':
        spec, evidence, phase, parent, types = read(a.args[0])
        immutable(Path.cwd() / 'agent.json', agent_spec(spec, evidence, phase, parent, types))
    else:
        raise ValueError('crosssample_v3 serves agent and tool only; numerical operations stay on crosssample_v2')


if __name__ == '__main__':
    main()

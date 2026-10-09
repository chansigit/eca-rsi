"""The agent tools the cross-sample and zoom-in stages share (#59), and the tail of every tool call.

A stage's tool() reads its state and arguments, then calls run() with a function that answers its own tools
and falls back on the functions here; run() turns a rejected call into {is_error, content + hint}, adds
AMEND_NOTE when the rejected tool was a submission with a draft, and writes state.json and result.json.
Per-sample keeps its own tools: its evidence is a live clustered.h5ad read by OSP, and its results carry
text/error, not content/is_error."""
from ..files import immutable, save
from .common import artifact, gene_answer, png_url, qc_answer
from .contract import AMEND_NOTE, evidence_page, evidence_paths

TEXT = ('.csv', '.json', '.md', '.txt')
TEXT_PAGE = 16000  # characters per read_evidence page
IMAGE_BUDGET = 8 * 2**20  # bytes per figure
ERROR_CHARS, RESULT_CHARS = 8000, 16000


def read_evidence(state, args, path):
    """One figure, or one page of a text artifact at args['offset']; records the read in state['read']."""
    if path.suffix == '.png':
        if path.stat().st_size > IMAGE_BUDGET:
            raise ValueError('Figure exceeds the image budget')
        response = dict(content=args['path'], images=[png_url(path)])
    elif path.suffix in TEXT:
        with path.open() as stream:
            stream.seek(args['offset'])
            text = stream.read(TEXT_PAGE)
            offset = stream.tell()
            response = dict(content=text, next_offset=offset if stream.read(1) else None)
    else:
        raise ValueError('Use registered matrix/database tools for this artifact')
    state['read'] = sorted(set(state['read']) | {args['path']})
    return response


def list_evidence(bundle, args):
    page, nxt = evidence_page(evidence_paths(bundle), args.get('offset') or 0)
    return dict(content=page, next_offset=nxt)


def deg_query(bundle, name, args, state, key):
    """deg_lookup or deg_sql on the bundle's deg.sqlite; records the query in state['lookups']."""
    from msp.api import DegTables
    with DegTables(database=artifact(bundle, 'deg.sqlite'), base_key=key) as tables:
        content = tables.lookup(**args) if name == 'deg_lookup' else tables.sql(**args)
    state['lookups'].append(args)
    return dict(content=content)


def check_genes(bundle, args, key, load):
    return dict(content=gene_answer(bundle, args['genes'], key, [args['cluster']] if args['cluster'] else None, load))


def check_qc_scores(bundle, state, key):
    state['qc'] = True
    return dict(content=qc_answer(bundle, key))


def run(name, state, destination, answer, submits, hint):
    """Write the result of one tool call: answer() -> response, or the rejection with hint(content) and, for a
    submission that has a draft, AMEND_NOTE; then state.json and result.json in destination."""
    try:
        response = answer()
    except (ValueError, KeyError, TypeError, IndexError) as exc:
        content = str(exc)[:ERROR_CHARS]
        try:
            text = hint(content)
        except Exception:  # noqa: BLE001 - a hint must never turn a correctable error into a crash
            text = ''
        if name in submits and name in state.get('drafts', {}):
            text = (text + '\n' + AMEND_NOTE).strip()
        response = {'is_error': True, 'content': (content + '\n' + text)[:RESULT_CHARS] if text else content}
    response['state'] = immutable(destination / 'state.json', state)
    save(destination / 'result.json', response)

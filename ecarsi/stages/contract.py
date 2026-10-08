"""What every stage program shows the model (protocol v4, 2026-09-17) and how it reads the model's calls.

Same numerics as before, fewer turns: the prompt already holds what list_evidence (and, for zoom-in,
annotation_status) would answer at turn 0, so the first turn can read evidence; those tools stay and
take no required arguments. list_evidence pages by offset/next_offset like read_evidence and
sample_inventory (2026-09-21: a bundle was assumed to hold about 60 paths, but a unit that has run
many rounds/restarts can accumulate thousands, and returning them all in one call hit the tool-result
size cap and killed the dataset non-retryably); annotation_status still returns everything in one call.
An earlier pagination attempt used a fixed page count from the checklist prompt text and silently
skipped the 61st path in 62 sessions -- next_offset here is computed from the actual list each call,
the same mechanism read_evidence and sample_inventory already use without that failure mode.
deg_lookup asks for cluster or gene with optional thresholds, as its documentation already said.
A proposal with a stray trailing quote or fence is parsed anyway (Eye 2026-09-17: one }]}" was
rejected 25 times in a row, 167k tokens each). Measured 2026-09-16 over 6 h: 64 % of
submit_decision, 56 % of submit_quality and 32 % of submit_types calls were rejected.
"""
import copy
import json
import re
import shutil
from pathlib import Path

from . import PROMPTS
from ..files import file_digest

# Matrices, and the machine-side inputs and caches a stage feeds its agent: a lineage's
# deg_input/*.npy alone is 759 MiB, against ~50 MiB of tables and figures worth reading.
HEAVY = (".h5ad", ".zarr", ".parquet", ".npy", ".npz", ".sqlite")


def copy_light(files, folder):
    """Copy the readable half of a publication -- report, figures, tables -- out of the pool
    request that produced it. `files` is a {name: {path, sha256}} map. Copying never fails a stage:
    a missing report is a degradation (ecarsi.degraded), returned for the caller to keep."""
    from ..degraded import note
    folder, notes = Path(folder), []
    for name, ref in sorted((files or {}).items()):
        if name.endswith(HEAVY):
            continue
        target, source = folder / name, Path(ref["path"])
        try:
            # by content: a restarted session's report can have the old one's size
            if target.is_file() and file_digest(target) == ref.get("sha256"):
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            partial = target.with_name(target.name + ".part")
            shutil.copyfile(source, partial)
            partial.replace(target)
        except OSError as exc:
            notes.append(note(f"{folder.name}/{name} not copied", exc))
    return notes

NO_ARGUMENTS = {'type': 'object', 'required': [], 'additionalProperties': False,
                'properties': {'offset': {'type': 'integer', 'description': 'Ignored: the whole listing comes back in one call.'}}}
THRESHOLDS = {name: {'type': ['number', 'null']} for name in ('min_logfc', 'max_padj', 'min_pct1', 'max_pct2')}
LOOKUP_FIELDS = {'key': {'type': 'string'}, 'cluster': {'type': 'string'}, 'gene': {'type': 'string'},
                 'view': {'type': 'string', 'enum': ['global', 'local', 'both']},
                 'top_n': {'type': 'integer', 'minimum': 1, 'maximum': 200}}
LOOKUP_NOTE = ' Give cluster or gene; key defaults to %s, view/top_n/thresholds are optional.'
JSON_NOTE = 'proposal_json must be one JSON document; remove anything after its closing brace.'


def schema(fields, required=None):
    return {'type': 'object', 'properties': fields, 'required': list(fields) if required is None else required,
            'additionalProperties': False}


def deg_lookup_schema():
    """Key plus cluster or gene; view, top_n and thresholds optional."""
    return {'type': 'object', 'properties': {**LOOKUP_FIELDS, **THRESHOLDS},
            'anyOf': [{'required': ['cluster']}, {'required': ['gene']}], 'additionalProperties': False}


def lookup_arguments(args, default_key=None):
    """What DegTables.lookup expects: no null thresholds, empty cluster/gene when absent."""
    filled = {k: v for k, v in args.items() if v is not None}
    if default_key:
        filled.setdefault('key', default_key)
    for field in ('cluster', 'gene'):
        filled.setdefault(field, '')
    return filled


def lenient_json(text):
    """The first complete JSON value; trailing quotes, fences or whitespace are dropped."""
    try:
        return json.loads(text)
    except ValueError:
        value, end = json.JSONDecoder().raw_decode(text.lstrip())
        if text.lstrip()[end:].strip(' \t\r\n"`\''):
            raise
        return value


def proposal(args):
    value = args['proposal_json']
    return value if isinstance(value, (dict, list)) else lenient_json(value)


JSON_BREAK = ('proposal_json is not valid JSON: a bracket, brace, comma or quote is missing or extra where it breaks. '
              'Every { and [ closes with its own } and ] before the next field or entry; a nested object such as '
              'evidence closes before the next field of its entry.')


AMEND_NOTE = ('To correct it, resubmit only what changes: {"amend": true, ...} with the corrected or added entries '
              '(clusters by their id, boundary reviews by their label pair, samples by name) and any field to replace; the rest '
              'of your last submission is kept and the whole is checked again. To drop an entry, resubmit everything.')


def _entry_key(entry):
    """What an amendment entry replaces: a cluster or sample by its id, a boundary review by its label pair."""
    if not isinstance(entry, dict):
        return None
    for key in ('cluster_id', 'cluster', 'sample'):
        if key in entry:
            return key, str(entry[key])
    labels = entry.get('coarse_labels')
    return ('pair', tuple(sorted(map(str, labels)))) if isinstance(labels, list) else None


def amended(args, state, tool):
    """The submitted proposal; with "amend": true, the last parsed submission of `tool` with the given entries and
    fields replaced. A rejected 20-26k-character proposal used to be rewritten whole: a fifth of batch 2's model time.
    The result becomes the tool's draft in `state`, which the caller saves after a rejection too."""
    value = proposal(args)
    drafts = state.setdefault('drafts', {})
    if isinstance(value, dict) and value.pop('amend', False) is True:
        if tool not in drafts:
            raise ValueError('Nothing to amend yet: submit the whole proposal')
        merged = copy.deepcopy(drafts[tool])
        for field, given in value.items():
            keys = [_entry_key(e) for e in given] if isinstance(given, list) else []
            if isinstance(merged.get(field), list) and keys and all(keys):
                at = {_entry_key(e): i for i, e in enumerate(merged[field])}
                for key, entry in zip(keys, given):
                    if key in at:
                        merged[field][at[key]] = entry
                    else:
                        merged[field].append(entry)
            else:
                merged[field] = given
        value = merged
    drafts[tool] = copy.deepcopy(value)
    return value


def json_hint(content, raw=None):
    """What to fix in a proposal_json that does not parse: text after the document ('Extra data'), or a break
    inside it, quoted from `raw` at the character the parser names. In batch 2 the breaks were braces of nested
    objects in 20-26k-character proposals, and the old note sent the model to the end of the document."""
    if 'Extra data' in content:
        return JSON_NOTE
    at = re.search(r': line \d+ column \d+ \(char (\d+)\)', content)
    if not at:
        return ''
    i = int(at.group(1))
    return JSON_BREAK + (' It breaks here: ' + json.dumps(raw[max(0, i - 160):i]) + ' <<HERE>> ' + json.dumps(raw[i:i + 60])
                         if isinstance(raw, str) else '')


def checklist(name):
    return (PROMPTS / (name + '-checklist-v4.md')).read_text()


def evidence_paths(bundle):
    return [n for n in bundle['files'] if not n.startswith('deg_input/') and not n.endswith('.h5ad')]


EVIDENCE_PAGE = 400  # paths per list_evidence call; a unit that has run many rounds/restarts can
                      # accumulate thousands of evidence files, well past the "about 60" this module
                      # once assumed (2026-09-21: a 256 KB tool-result cap rejected a 5,278-path,
                      # 534 KB single-call listing outright and killed the whole dataset).


def evidence_page(paths, offset):
    page = paths[offset:offset + EVIDENCE_PAGE]
    more = offset + len(page)
    return page, more if more < len(paths) else None

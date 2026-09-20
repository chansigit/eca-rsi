"""What every stage program shows the model (protocol v4, 2026-09-17) and how it reads the model's calls.

Same numerics as before, fewer turns: the prompt already holds what list_evidence (and, for zoom-in,
annotation_status) would answer at turn 0, so the first turn can read evidence; those tools stay,
take no arguments and return everything in one call (a bundle has about 60 paths; paging cost a
turn per page and a fixed page count in the checklist skipped the 61st path in 62 sessions).
deg_lookup asks for cluster or gene with optional thresholds, as its documentation already said.
A proposal with a stray trailing quote or fence is parsed anyway (Eye 2026-09-17: one }]}" was
rejected 25 times in a row, 167k tokens each). Measured 2026-09-16 over 6 h: 64 % of
submit_decision, 56 % of submit_quality and 32 % of submit_types calls were rejected.
"""
import json
import shutil
from pathlib import Path

from . import PROMPTS

# Matrices, and the machine-side inputs and caches a stage feeds its agent: a lineage's
# deg_input/*.npy alone is 759 MiB, against ~50 MiB of tables and figures worth reading.
HEAVY = (".h5ad", ".zarr", ".parquet", ".npy", ".npz", ".sqlite")


def copy_light(files, folder):
    """Copy the readable half of a publication -- report, figures, tables -- out of the pool
    request that produced it. `files` is a {name: {path, sha256}} map. Copying never raises:
    a missing report is worth a warning, not a failed stage."""
    folder = Path(folder)
    for name, ref in sorted((files or {}).items()):
        if name.endswith(HEAVY):
            continue
        target, source = folder / name, Path(ref["path"])
        try:
            if target.is_file() and target.stat().st_size == source.stat().st_size:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            partial = target.with_name(target.name + ".part")
            shutil.copyfile(source, partial)
            partial.replace(target)
        except OSError as exc:
            print(f"[publish] warning: {folder.name}/{name} not copied: {exc}", flush=True)

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


def json_hint(content):
    return JSON_NOTE if 'Expecting' in content or 'Extra data' in content else ''


def checklist(name):
    return (PROMPTS / (name + '-checklist-v4.md')).read_text()


def evidence_paths(bundle):
    return [n for n in bundle['files'] if not n.startswith('deg_input/') and not n.endswith('.h5ad')]

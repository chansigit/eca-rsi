"""Bounded OSP compute-ahead; models run in the preparation process only."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path

from harness_bridge.control import pausable, safe_point
from ecarsi import layout as L
from ecarsi.run_state import digest, file_identity, read_json, write_json, writer_lock


def metadata_data(path):
    """Reuse the existing H5AD reader without AnnData's eager layers."""
    from ecarsi.downstream import _data
    result = _data(path)
    result.layers = {'counts': result.counts}
    result.shape = (result.n_obs, result.n_vars)
    return result


def source_profiles(source):
    from ecarsi.organize import find_ecapp_units, profile_unit
    from ecarsi.upstream import inspect_unit
    units, extra = find_ecapp_units(Path(source))
    if extra or not units:
        raise ValueError('preparation requires declared ECA-PP sources')
    records = [inspect_unit(unit) for unit in units]
    accepted = [r for r in records if r['state'] == 'accepted']
    if not accepted or any(r['state'] not in {'accepted', 'rejected'} for r in records):
        raise ValueError('upstream inputs are not ready')
    identity = digest([{**{k: r.get(k) for k in ('name', 'state', 'files')},
                        'derived': {k: f['identity'] for k, f in r.get('derived_files', {}).items()}}
                       for r in records])
    return identity, [profile_unit(r) for r in accepted]


def organize_confirmed(source, output, identity, plan_file, mirror):
    from ecarsi.organize import main
    current, _ = source_profiles(source)
    if current != identity:
        raise ValueError('upstream changed after preparation planning')
    args = [source, output, '--plan-json', plan_file]
    if mirror:
        args += ['--mirror', mirror]
    if main(args):
        raise ValueError('confirmed organize plan could not be executed')


def _remote_wait():
    """Return the local driver budget while its matrix work runs in the pool."""
    from .driver_budget import model_wait
    return model_wait()


def pool_call(function, *args, source, **kwargs):
    from ecarsi.pool.client import PoolEndpoint
    root = os.environ.get('ECA_POOL_DATA_ROOT', '/scratch')
    source_root = str(Path(*Path(source).resolve().parts[:2]))
    memory = int(os.environ['ECA_DATASET_PEAK_MEMORY_BYTES'])
    safe_point()
    with _remote_wait():
        with PoolEndpoint(mode='pool') as pool:
            result = pool.submit(function, *args, **kwargs, needs=dict(
                cpus=1, memory=memory, seconds=300, roots=sorted({root, source_root}),
                modules=['ecarsi', 'osp'])).result()
    safe_point()
    return result


def computed(entry):
    from ecarsi.osp_contract import COMPUTE_STATE
    path = Path(entry['outdir'])/COMPUTE_STATE
    if not path.is_file():
        return False
    try:
        record = read_json(path)
        return (record.get('identity') == entry['identity']
                and isinstance(record.get('files'), dict) and bool(record['files'])
                and all((path.parent/name).is_file() and file_identity(path.parent/name) == identity
                        for name, identity in record['files'].items()))
    except (OSError, ValueError, KeyError, TypeError):
        return False


@pausable
def main(argv=None):
    # The published package contains metadata-only copies of the original
    # planning entry points. Numerical kernels and result identities stay pinned.
    from . import persample as ps
    from .osp_stage import run as compute_stage
    from .osp_contract import is_finished, is_empty, REQUEST
    from ecarsi.plan import propose_plan
    import pandas as pd
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source')
    parser.add_argument('output')
    parser.add_argument('--samples', type=int, default=4)
    parser.add_argument('--max-prepared-samples', type=int)
    parser.add_argument('--max-sample-cells', type=int)
    parser.add_argument('--sample-map')
    parser.add_argument('--mirror')
    args = parser.parse_args(argv)
    if args.samples < 1:
        parser.error('samples must be positive')
    if args.max_prepared_samples is not None and args.max_prepared_samples < 1:
        parser.error('max-prepared-samples must be positive')
    if args.max_sample_cells is not None and args.max_sample_cells < 1:
        parser.error('max-sample-cells must be positive')
    root = Path(args.output)
    identity, profiles = pool_call(source_profiles, args.source, source=args.source)
    manifest = L.organize_manifest(root)
    record_path = root/L.ORGANIZE/'preparation_plan.json'
    if manifest.is_file():
        plan = read_json(manifest)['plan']
    elif record_path.is_file():
        record = read_json(record_path)
        if record['input_identity'] != identity:
            raise ValueError('preparation inputs changed; use a new output directory')
        plan = record['plan']
    else:
        plan = propose_plan(profiles)
        write_json(record_path, dict(input_identity=identity, plan=plan))
    plan_file = root/L.ORGANIZE/'preparation_accepted_plan.json'
    write_json(plan_file, plan)
    pool_call(organize_confirmed, args.source, str(root), identity, str(plan_file), args.mirror, source=args.source)
    prepared_path = root/'osp_prepared.json'
    previous = read_json(prepared_path) if prepared_path.is_file() else {}
    if previous and previous.get('input_identity') != identity:
        raise ValueError('preparation inputs changed; use a new output directory')
    completed = list(previous.get('samples', []))
    recorded = {(s['unit'], s['sample']) for s in completed}
    already_computed = 0
    candidates = {}
    for unit in L.units(root):
        arguments = [str(unit), '--plan-only']
        if args.sample_map:
            arguments += ['--sample-map', args.sample_map]
        rc = ps.main(arguments)
        if rc:
            return rc
        out = L.persample_root(unit)
        with writer_lock(out/'.driver.lock'):
            manifest = read_json(out/L.MANIFEST)
            from .sample_mapping import read_cell_table
            table = read_cell_table(out/L.SAMPLE_MAPPING)
            if ps.mapping_identity(table) != manifest['mapping_identity']:
                raise ValueError('confirmed sample mapping changed')
            entries = ps.build_entries(Path(manifest['h5ad']), ps.SAMPLE_KEY,
                {s['value']: s['n_cells'] for s in manifest['samples']}, out,
                os.environ.get('OSP_PYTHON', ''), manifest['config']['annotate'], manifest['config']['model'])
            identities = {s['value']: s['identity'] for s in manifest['samples']}
            for entry in entries:
                entry['identity'] = identities[entry['value']]
                if is_finished(Path(entry['outdir']), manifest['config']['annotate'], entry['identity']):
                    continue
                if computed(entry):
                    already_computed += 1
                    key = (unit.name, entry['value'])
                    if key not in recorded:
                        completed.append(dict(unit=unit.name, sample=entry['value'], directory=entry['outdir']))
                        recorded.add(key)
                    continue
                if args.max_sample_cells is None or entry['n_cells'] <= args.max_sample_cells:
                    candidates.setdefault(unit, []).append(entry['value'])
    remaining = min(args.samples, max(0, args.max_prepared_samples - already_computed)
                    if args.max_prepared_samples is not None else args.samples)
    for unit, values in candidates.items():
        if not remaining:
            break
        arguments = [str(unit), '--plan-only']
        if args.sample_map:
            arguments += ['--sample-map', args.sample_map]
        rc = ps.main(arguments)
        if rc:
            return rc
        out = L.persample_root(unit)
        with writer_lock(out/'.driver.lock'):
            manifest = read_json(out/L.MANIFEST)
            from .sample_mapping import read_cell_table
            table = read_cell_table(out/L.SAMPLE_MAPPING)
            if ps.mapping_identity(table) != manifest['mapping_identity']:
                raise ValueError('confirmed sample mapping changed')
            entries = ps.build_entries(Path(manifest['h5ad']), ps.SAMPLE_KEY,
                {s['value']: s['n_cells'] for s in manifest['samples']}, out,
                os.environ.get('OSP_PYTHON', ''), manifest['config']['annotate'], manifest['config']['model'])
            identities = {s['value']: s['identity'] for s in manifest['samples']}
            by_value = {e['value']: e for e in entries}
            to_compute = []
            for value in values[:remaining]:
                entry = by_value[value]
                entry['identity'] = identities[value]
                if (args.max_sample_cells is not None and entry['n_cells'] > args.max_sample_cells
                        or is_finished(Path(entry['outdir']), manifest['config']['annotate'], entry['identity'])
                        or computed(entry)):
                    continue
                entry['request'] = dict(identity=entry['identity'], config=manifest['config'],
                    runtime=manifest['runtime'], value=entry['value'], n_cells=entry['n_cells'], context=L.report_context(unit))
                to_compute.append(entry)
            if to_compute:
                # One input read prepares all selected subsets. Each OSP
                # output has its own writer lock and can compute independently.
                pool_call(ps.write_subsets, Path(manifest['h5ad']), table, to_compute,
                          manifest['sample_mapping'].get('batch_key'), source=args.source)
                with _remote_wait():
                    with ThreadPoolExecutor(max_workers=min(len(to_compute), max(1, int(os.environ.get('PERSAMPLE_PARALLEL', '2'))))) as executor:
                        results = list(executor.map(lambda e: compute_stage(Path(e['outdir'])/REQUEST, compute_only=True), to_compute))
                if any(rc and not is_empty(Path(entry['outdir']), entry['identity'])
                       for entry, rc in zip(to_compute, results)):
                    return 1
            for entry in to_compute:
                key = (unit.name, entry['value'])
                if key not in recorded:
                    completed.append(dict(unit=unit.name, sample=entry['value'], directory=entry['outdir']))
                    recorded.add(key)
                print(f"[prepare] OSP computed; awaiting sample annotation: {entry['value']}", flush=True)
                ps._pages(unit)
            remaining -= len(to_compute)
    write_json(prepared_path, dict(input_identity=identity, samples=completed,
               samples_limit=len(completed), annotation_required=True, module=__name__))
    return 0


if __name__ == '__main__':
    from harness_bridge import configure_logging
    configure_logging('ecarsi', stream=__import__('sys').stderr)
    raise SystemExit(main() or 0)

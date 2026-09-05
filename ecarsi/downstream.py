"""Verified downstream runs: content identity, cell conservation and one writer."""
from __future__ import annotations

from contextlib import contextmanager
from functools import wraps
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

from . import layout as L
from .run_state import digest, file_identity, read_json, write_json, writer_lock

STATE = '.rsi-stage.json'
_HELD = set()


@contextmanager
def unit_lock(unit):
    path = Path(unit).resolve() / '.rsi-downstream.lock'
    key = (os.getpid(), threading.get_ident(), path)
    if key in _HELD:
        yield
        return
    with writer_lock(path):
        _HELD.add(key)
        try:
            yield
        finally:
            _HELD.remove(key)


def locked_unit(function):
    @wraps(function)
    def wrapped(argv):
        if not argv or argv[0].startswith('-'):
            return function(argv)
        with unit_lock(Path(argv[0])):
            return function(argv)
    return wrapped


def runtime(kernel):
    modules = ['ecarsi', 'msp', 'harness_bridge', 'standissect_lite']
    if kernel == 'zmip':
        modules.append('zmip')
    sources = {}
    for name in modules:
        spec = importlib.util.find_spec(name)
        if spec is None or spec.origin is None:
            raise ValueError(f'{name} is unavailable in {sys.executable}')
        root = Path(spec.origin).parent
        sources[name] = {'path': str(root.resolve()), 'digest': digest({
            str(p.relative_to(root)): file_identity(p) for p in sorted(root.rglob('*'))
            if p.suffix in {'.py', '.md', '.json'} and '__pycache__' not in p.parts})}
    packages = {}
    for name in ('msp-sc', 'zmip', 'agent-harness-bridge', 'standissect-lite', 'harmonypy',
                 'scanpy', 'anndata', 'numpy', 'scipy', 'pandas', 'h5py', 'numba',
                 'scikit-learn', 'igraph', 'stanhue'):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    return {'python': sys.version, 'executable': str(Path(sys.executable).resolve()),
            'sources': sources, 'packages': packages}


def kernel_runtime(py, kernel):
    result = subprocess.run([py, '-m', 'ecarsi.downstream', 'runtime', kernel],
                            capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def options(kernel):
    """Shared first/later-round configuration, parsed by the kernel CLI."""
    prefix = kernel.upper()
    flags = []
    for key, flag in [('N_TOP_GENES', '--n-top-genes'), ('N_PCS', '--n-pcs'),
                      ('N_NEIGHBORS', '--n-neighbors'), ('LANGUAGE', '--language'),
                      ('EFFORT', '--effort'), ('MAX_TURNS', '--max-turns')]:
        value = os.environ.get(f'{prefix}_{key}')
        if value:
            flags += [flag, value]
    value = os.environ.get(f'{prefix}_RESOLUTIONS')
    if value:
        flags += ['--resolutions', *value.replace(',', ' ').split()]
    value = os.environ.get(f'{prefix}_HARMONY')
    if value:
        obj = json.loads(value)
        if not isinstance(obj, dict):
            raise ValueError(f'{prefix}_HARMONY must be a JSON object')
        for key, val in sorted(obj.items()):
            flags += ['--harmony', f'{key}=' + (','.join(map(str, val)) if isinstance(val, list) else str(val))]
    if kernel == 'zmip' and os.environ.get('ZMIP_MIN_CELLS'):
        flags += ['--min-cells', os.environ['ZMIP_MIN_CELLS']]
    return flags


def computational_config(config):
    result = dict(config)
    flags = iter(config.get('options', []))
    kept = []
    for flag in flags:
        if flag in {'--language', '--effort', '--max-turns'}:
            next(flags)
        else:
            kept.append(flag)
    result['options'] = kept
    return result


def stage_agent(kernel):
    from . import agent_config
    return {**agent_config(), 'kernel_options': options(kernel)}


def prepare(py, kernel, inputs, outdir, config):
    outdir = Path(outdir)
    identity = {'schema': 1, 'kernel': kernel, 'inputs': [file_identity(Path(p)) for p in inputs],
                'input_paths': [os.path.relpath(Path(p).resolve(), outdir.resolve()) for p in inputs],
                'config': computational_config(config), 'runtime': kernel_runtime(py, kernel)}
    from . import agent_config, check_agent_config
    path = outdir / STATE
    if path.exists():
        old = read_json(path)
        check_agent_config(old.get('agent', {}), str(path))
        if old.get('identity') != identity:
            raise ValueError('downstream input/configuration/runtime changed; use a new output directory')
        if old.get('state') == 'complete':
            _check_files(outdir, old['validation']['outputs'])
    elif any((outdir / f).exists() for f in (*L.MSP_CONTRACT, *L.ZMIP_CONTRACT)):
        raise ValueError('legacy downstream outputs have no RSI identity; use a new output directory')
    write_json(path, {'identity': identity, 'agent': stage_agent(kernel), 'state': 'running'})
    return identity


class _Matrix:
    """Read metadata and slice counts directly; AnnData backed mode loads layers."""
    def __init__(self, path):
        import h5py
        from anndata.io import read_elem, sparse_dataset
        self.file = h5py.File(path, 'r')
        try:
            self.obs = read_elem(self.file['obs'])
            self.obs_names = self.obs.index
            self.var_names = read_elem(self.file['var']).index
            self.n_obs, self.n_vars = len(self.obs_names), len(self.var_names)
            node = self.file['layers']['counts']
            self.counts = sparse_dataset(node) if isinstance(node, h5py.Group) else node
            if not self.obs_names.is_unique or not self.var_names.is_unique or self.n_obs == 0 or self.n_vars < 2:
                raise ValueError(f'invalid dimensions or IDs: {path}')
            if self.counts.shape != (self.n_obs, self.n_vars):
                raise ValueError('counts shape does not match metadata')
        except BaseException:
            self.file.close()
            raise

    def rows(self, indices):
        import numpy as np
        indices = np.asarray(indices)
        order = np.argsort(indices)
        block = self.counts[indices[order], :]
        return block[np.argsort(order), :]


def _data(path):
    return _Matrix(path)


def _same_counts(parent, child, child_mask=None):
    import numpy as np
    from scipy import sparse
    if not parent.var_names.equals(child.var_names):
        raise ValueError('gene axis changed downstream')
    rows = np.arange(child.n_obs) if child_mask is None else np.flatnonzero(child_mask)
    positions = parent.obs_names.get_indexer(child.obs_names[rows])
    if (positions < 0).any():
        raise ValueError('downstream invented cell IDs')
    for start in range(0, len(rows), 2048):
        end = min(start + 2048, len(rows))
        x = parent.rows(positions[start:end])
        y = child.rows(rows[start:end])
        if sparse.issparse(x) or sparse.issparse(y):
            equal = (sparse.csr_matrix(x) - sparse.csr_matrix(y)).nnz == 0
        else:
            equal = np.array_equal(x, y)
        if not equal:
            raise ValueError('raw counts changed downstream')


def validate(kernel, inputs, outdir):
    import pandas as pd
    outdir = Path(outdir)
    names = list(L.MSP_CONTRACT if kernel == 'msp' else L.ZMIP_CONTRACT)
    names += ['annotation_removed.csv'] if kernel == 'msp' else ['zmip_removed.csv', 'zmip_reassigned.csv', '.zmip-global.json', '.zmip-run.json']
    for name in names:
        p = outdir / name
        if not p.is_file() or not p.stat().st_size:
            raise ValueError(f'missing/empty downstream output: {p}')
    if '<html' not in (outdir / 'report.html').read_text().lower():
        raise ValueError('invalid downstream HTML report')
    for name in names:
        if name.endswith('.json'):
            read_json(outdir / name)
    if kernel == 'msp':
        from msp.steps import step_pending
        if step_pending(outdir, 'annotate'):
            raise ValueError('MSP has an unfinished step')
        integrated = _data(outdir / 'integrated.h5ad')
        try:
            expected = set()
            for p in inputs:
                a = _data(p)
                try:
                    ids = set(a.obs_names)
                    if expected & ids:
                        raise ValueError('duplicate cell IDs across MSP inputs')
                    expected |= ids
                    if not ids <= set(integrated.obs_names):
                        raise ValueError('MSP integration lost input cells')
                    _same_counts(a, integrated, integrated.obs_names.isin(ids))
                finally:
                    a.file.close()
            if expected != set(integrated.obs_names):
                raise ValueError('MSP integrated cell set differs from inputs')
            for col in ('_msp_action', '_msp_verdict'):
                if col not in integrated.obs:
                    raise ValueError(f'MSP inspection was not applied: {col}')
            final = _data(outdir / 'annotated.h5ad')
            removal = 'annotation_removed.csv'
            columns = ('msp_ann_coarse', 'msp_ann_fine')
            parent = integrated
        except BaseException:
            integrated.file.close()
            raise
    else:
        from zmip.publication import complete
        if not complete(outdir):
            raise ValueError('ZMIP publication is incomplete or changed')
        parent = _data(inputs[0])
        try:
            final = _data(outdir / 'annotated_zmip.h5ad')
        except BaseException:
            parent.file.close()
            raise
        removal = 'zmip_removed.csv'
        columns = ('zmip_ann_coarse', 'zmip_ann_fine', 'zmip_lineage')
    try:
        deleted = pd.read_csv(outdir / removal, dtype=str, keep_default_na=False)
        if 'cell' not in deleted or deleted.cell.duplicated().any():
            raise ValueError('invalid deletion ledger')
        kept, gone = set(final.obs_names), set(deleted.cell)
        if kept & gone or kept | gone != set(parent.obs_names):
            raise ValueError('downstream cell conservation failed')
        for col in columns:
            if col not in final.obs or final.obs[col].isna().any():
                raise ValueError(f'missing survivor annotations: {col}')
        _same_counts(parent, final)
        if kernel == 'zmip':
            moved = pd.read_csv(outdir / 'zmip_reassigned.csv', dtype=str, keep_default_na=False)
            if 'cell' not in moved or moved.cell.duplicated().any() or not set(moved.cell) <= kept:
                raise ValueError('invalid reassignment ledger')
        return {'n_input': parent.n_obs, 'n_output': final.n_obs, 'n_removed': len(gone),
                'outputs': {name: file_identity(outdir / name) for name in names}}
    finally:
        parent.file.close()
        final.file.close()


def verify(py, kernel, inputs, outdir, identity=None):
    result = subprocess.run([py, '-m', 'ecarsi.downstream', 'validate', kernel, str(outdir), *map(str, inputs)],
                            capture_output=True, text=True)
    if result.returncode:
        raise ValueError(f'{kernel} output validation failed: {result.stderr[-6000:]}')
    validation = json.loads(result.stdout)
    if identity is not None:
        if [file_identity(Path(p)) for p in inputs] != identity['inputs']:
            raise ValueError('downstream input changed during computation')
        if kernel_runtime(py, kernel) != identity['runtime']:
            raise ValueError('downstream runtime changed during computation')
        from . import agent_config
        write_json(Path(outdir) / STATE, {'identity': identity, 'agent': stage_agent(kernel), 'state': 'complete', 'validation': validation})
    return validation


def _record_files(root, paths):
    return {str(p.relative_to(root)): file_identity(p) for p in paths}


def _check_files(root, files):
    if not files:
        raise ValueError("empty completion receipt")
    for name, identity in files.items():
        if file_identity(root / name) != identity:
            raise ValueError(f"completed output changed: {root / name}")


def seal_round(rdir):
    paths = [rdir / L.STATS, rdir / L.DECISION]
    for step in (L.CROSSSAMPLE, L.ZOOMIN):
        stage = rdir / step
        saved = read_json(stage / STATE)
        if saved.get("state") != "complete":
            raise ValueError("cannot seal an incomplete round")
        paths += [stage / STATE, *[stage / n for n in saved["validation"]["outputs"]]]
    write_json(rdir / '.rsi-round.json', {"files": _record_files(rdir, paths)})


def check_round(rdir):
    saved = read_json(rdir / '.rsi-round.json')
    _check_files(rdir, saved['files'])
    for kernel, py in [('msp', os.environ.get('MSP_PYTHON', sys.executable)),
                       ('zmip', os.environ.get('ZMIP_PYTHON') or os.environ.get('MSP_PYTHON') or sys.executable)]:
        stage = L.crosssample_dir(rdir) if kernel == 'msp' else L.zoomin_dir(rdir)
        from . import check_agent_config
        record = read_json(stage / STATE)
        check_agent_config(record.get('agent', {}), str(stage / STATE))
        identity = record['identity']
        requested_batch = os.environ.get('MSP_BATCH_COL')
        if kernel == 'msp' and requested_batch and requested_batch != identity['config'].get('batch_col'):
            raise ValueError('completed round MSP_BATCH_COL changed; use a new output directory')
        if [file_identity(stage / p) for p in identity['input_paths']] != identity['inputs']:
            raise ValueError('completed round input changed')
        if identity['runtime'] != kernel_runtime(py, kernel) or identity['config']['options'] != computational_config({'options': options(kernel)})['options']:
            raise ValueError('completed round runtime/config changed; use a new output directory')
    if (L.crosssample_dir(rdir) / '.msp-state').exists() and list((L.crosssample_dir(rdir) / '.msp-state').glob('*.pending')):
        raise ValueError('MSP step is pending')
    if (L.zoomin_dir(rdir) / '.zmip-publish.json').exists():
        raise ValueError('ZMIP publication is pending')


def seal_release(unit, rounds, release_dir=None):
    rel = L.release_dir(unit) if release_dir is None else Path(release_dir)
    files = {str(Path(L.RELEASE) / p.relative_to(rel)): file_identity(p)
             for p in rel.rglob('*') if p.is_file() and p.name not in {'.rsi-release.json', 'pruned.json'}}
    # Scientific records survive pruning; runtime logs and generated navigation do not define identity.
    roots = [L.persample_root(unit), unit / L.INPUT, *rounds]
    for root in roots:
        for p in root.rglob('*'):
            if p.is_file() and p.suffix in {'.json', '.csv', '.gz', '.parquet', '.md', '.txt', '.html', '.pruned'}:
                if p.name not in {L.INDEX, 'progress.log', 'pruned.json'} and not any(x.startswith(('.msp-history', '.zmip-publish')) for x in p.relative_to(root).parts):
                    files[str(p.relative_to(unit))] = file_identity(p)
    write_json(rel / '.rsi-release.json', {'schema': 1, 'files': files})


def check_release(unit):
    path = L.release_dir(unit) / '.rsi-release.json'
    if not path.is_file():
        raise ValueError('legacy release has no verified receipt; browse it or use a new output directory')
    _check_files(unit, read_json(path)['files'])


def main(argv):
    if argv[0] == 'runtime':
        print(json.dumps(runtime(argv[1])))
    elif argv[0] == 'validate':
        print(json.dumps(validate(argv[1], [Path(p) for p in argv[3:]], Path(argv[2]))))
    else:
        raise ValueError('unknown downstream operation')


if __name__ == '__main__':
    main(sys.argv[1:])

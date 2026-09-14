import importlib
import json
import time
from pathlib import Path

from ecarsi.batch import assign, reconcile, receipt_path
from ecarsi.driver_budget import reserved
from ecarsi.run_state import read_json, write_json


def test_numeric_cell_ids_select_labels_not_row_positions(tmp_path):
    import anndata as ad
    import numpy as np
    import pandas as pd
    from ecarsi.persample import write_subsets
    from ecarsi.sample_mapping import SAMPLE_KEY, read_cell_table
    ids = ['1', '2', '003', 'NA']
    matrix = ad.AnnData(np.arange(8).reshape(4,2).astype(float),
                       obs=pd.DataFrame(index=ids))
    source = tmp_path/'input.h5ad'
    matrix.write_h5ad(source)
    mapping = pd.DataFrame({SAMPLE_KEY: ['a','b','a','b']}, index=ids)
    path = tmp_path/'mapping.csv'
    mapping.to_csv(path)
    table = read_cell_table(path)
    assert list(table.index) == ids
    out = tmp_path/'sample'
    write_subsets(source, table, [dict(outdir=str(out),value='a',request={})])
    subset = ad.read_h5ad(out/'subset.h5ad')
    assert list(subset.obs_names) == ['1', '003']
    np.testing.assert_array_equal(subset.X, matrix.X[[0,2]])
    assert pd.read_csv(out/'input_cells.csv.gz',dtype=str).cell_id.tolist() == ['1','003']


def test_preparation_budget_scales_with_metadata_rows(tmp_path):
    now = time.time()
    node = dict(id='n', cpus=2, cpu_ids=[0,1], memory=8*2**30, memory_headroom=8*2**30,
                observed_at=now, end_time=now+7200)
    rows = [dict(id=str(i),state='queued',attempt=0,cpus=2,memory_gb=32,hours=1,
                 resource_profile=dict(n_cells=cells)) for i,cells in enumerate((5_000_000,100_000))]
    config = dict(directory=str(tmp_path),max_cpu_percent=90,preparation_module='v.prepare',
                  preparation_memory_gb=2,preparation_max_datasets=8)
    assign(dict(nodes={'n':node},datasets=rows),config,now)
    assert [r['admission_memory_gb'] for r in rows] == [6,2]
    assert all(r['state']=='assigned' for r in rows)


def test_preparation_budget_scales_with_large_count_matrix(tmp_path):
    now = time.time()
    node = dict(id='n', cpus=2, cpu_ids=[0, 1], memory=8*2**30,
                memory_headroom=8*2**30, observed_at=now, end_time=now+7200)
    rows = [dict(id=str(i), state='queued', attempt=0, cpus=2, memory_gb=16, hours=1,
                 resource_profile=dict(n_cells=70_000, counts_bytes=size))
            for i, size in enumerate((2**29, 2**30))]
    config = dict(directory=str(tmp_path), max_cpu_percent=90,
                  preparation_module='version.prepare_osp', preparation_memory_gb=2,
                  preparation_max_datasets=8)
    assign(dict(nodes={'n': node}, datasets=rows), config, now)
    assert [r['admission_memory_gb'] for r in rows] == [2, 3]


def test_preparation_oom_retries_with_more_memory_without_repeating_other_failures(tmp_path):
    from ecarsi.batch import retry_finished

    now = time.time()
    row = dict(id='x', name='large', state='failed', attempt=1, node='n',
               admission_phase='preparation', admission_memory_gb=2,
               memory_gb=16, output=str(tmp_path/'run'), log=str(tmp_path/'run.log'),
               cpus=2, hours=1, resource_profile=dict(n_cells=70_970, counts_bytes=2**30),
               exit_code=1)
    log = Path(row['log'])
    log.write_text('[batch] node=n cpus=[0] memory_gb=16 attempt=1\n'
                   '[2026-09-13] error: Detected 1 oom_kill event in StepId=123.4\n')
    receipt = receipt_path(tmp_path, row)
    write_json(receipt, dict(node='n', state='failed', exit_code=1, finished_at=now))
    state = dict(datasets=[row])
    config = dict(directory=str(tmp_path), max_attempts=1, preparation_memory_gb=2)
    retry_finished(state, config, now+1)
    assert row['state'] == 'retry_wait' and row['preparation_memory_floor_gb'] == 4
    assert row['recovery_reason'].endswith('4 GiB')
    assert row['attempt_history'][-1]['termination_reason'] == 'out_of_memory'
    retry_finished(state, config, now+61)
    assert row['state'] == 'queued'
    node = dict(id='n', cpus=1, cpu_ids=[0], memory=4*2**30,
                memory_headroom=4*2**30, observed_at=now+61, end_time=now+7200)
    assign(dict(nodes={'n': node}, datasets=[row]), dict(config, max_cpu_percent=90,
           preparation_module='version.prepare_osp', preparation_max_datasets=8), now+61)
    assert row['state'] == 'assigned' and row['admission_memory_gb'] == 4

    other = dict(id='y', state='failed', attempt=2, node='n', admission_phase='preparation',
                 admission_memory_gb=2, memory_gb=16, output=str(tmp_path/'other'),
                 log=str(log), exit_code=1)
    write_json(receipt_path(tmp_path, other), dict(node='n', state='failed', exit_code=1, finished_at=now))
    retry_finished(dict(datasets=[other]), config, now+1)
    assert other['state'] == 'failed'  # an OOM from a different attempt is not evidence


def test_osp_preparation_admits_below_dataset_peak_and_bounds_backlog(tmp_path):
    now = time.time()
    node = dict(id='n', cpus=3, cpu_ids=[0, 1, 2], memory=12*2**30,
                memory_headroom=12*2**30, observed_at=now, end_time=now+7200)
    model = dict(id='a', attempt=1, state='running', node='n', cpu_ids=[0], memory_gb=8)
    pending = [dict(id=str(i), state='queued', attempt=0, cpus=2, memory_gb=32, hours=1) for i in range(2)]
    state = dict(nodes={'n': node}, datasets=[model, *pending])
    config = dict(directory=str(tmp_path), max_cpu_percent=90, preparation_module='version.prepare_osp',
                  preparation_memory_gb=4, preparation_max_datasets=1)
    assign(state, config, now)
    assert pending[0]['state'] == 'assigned' and reserved(pending[0]) == 4*2**30
    assert pending[0]['memory_gb'] == 32 and pending[0]['cpu_ids'] == [1]
    assert pending[1]['state'] == 'queued'
    write_json(receipt_path(tmp_path, pending[0]), dict(node='n', state='prepared', exit_code=0, finished_at=now))
    reconcile(state, tmp_path)
    assert pending[0]['state'] == 'queued' and pending[0]['preparation_complete']
    assign(state, config, now)
    assert all(r['state'] == 'queued' for r in pending)  # full driver still needs 32 GiB; backlog remains bounded
    node['memory'] = node['memory_headroom'] = 48*2**30
    node['role'] = 'preparation'
    assign(state, config, now)
    assert pending[0]['state'] == 'assigned'  # full backlog uses spare large prep capacity
    assert pending[0]['admission_phase'] == 'driver' and reserved(pending[0]) == 32*2**30


def test_small_control_preparation_node_stays_prep_only(tmp_path):
    now = time.time()
    node = dict(id='n', role='preparation', cpus=2, cpu_ids=[0,1], memory=8*2**30,
                memory_headroom=8*2**30, observed_at=now, end_time=now+7200)
    row = dict(id='ready', state='queued', attempt=1, preparation_complete=True,
               cpus=1, memory_gb=4, hours=1)
    config = dict(directory=str(tmp_path), max_cpu_percent=90,
                  preparation_module='version.prepare_osp', preparation_max_datasets=1)
    assign(dict(nodes={'n':node}, datasets=[row]), config, now)
    assert row['state'] == 'queued'


def test_metadata_planning_keeps_original_sample_identity(tmp_path, monkeypatch):
    import anndata as ad
    import numpy as np
    import pandas as pd
    import ecarsi
    from ecarsi import persample
    from ecarsi.build_stage_runtime import build_preparation
    source = tmp_path/'input.h5ad'
    matrix = ad.AnnData(np.ones((6, 4)), obs=pd.DataFrame({'sample': ['a']*3+['b']*3}, index=list('abcdef')))
    matrix.layers['counts'] = matrix.X.copy()
    matrix.write_h5ad(source)
    monkeypatch.setattr(persample, '_kernel_runtime', lambda _: {'version': 'same'})
    original = tmp_path/'original'
    arguments = [str(source), str(original), '--sample-column', 'sample', '--no-annotate', '--plan-only']
    assert persample.main(arguments) == 0
    bundle = build_preparation(tmp_path/'published', Path(ecarsi.__file__).parent, 'test_preparation_runtime')
    assert (bundle/'prompts/sample_column.md').read_bytes() == (Path(ecarsi.__file__).parent/'prompts/sample_column.md').read_bytes()
    assert (bundle/'driver_budget.py').is_file()
    operational = build_preparation(tmp_path/'operational', Path(ecarsi.__file__).parent,
                                    'test_operational_runtime', dispatch_module='eca_stages.osp_dispatch')
    assert 'from eca_stages.osp_dispatch import drive' in (operational/'persample.py').read_text()
    monkeypatch.syspath_prepend(str(tmp_path/'published'))
    candidate = importlib.import_module('test_preparation_runtime.persample')
    monkeypatch.setattr(candidate, '_kernel_runtime', lambda _: {'version': 'same'})
    def no_full_reader(*args, **kwargs):
        raise AssertionError('preparation loaded AnnData layers')
    monkeypatch.setattr(ad, 'read_h5ad', no_full_reader)
    output = tmp_path/'metadata'
    arguments[1] = str(output)
    assert candidate.main(arguments) == 0
    old, new = [json.loads((root/'manifest.json').read_text()) for root in (original, output)]
    assert old['identity'] == new['identity'] and old['mapping_identity'] == new['mapping_identity']
    assert [s['identity'] for s in old['samples']] == [s['identity'] for s in new['samples']]


def test_preparation_batches_subsets_and_runs_independent_samples(tmp_path, monkeypatch):
    from ecarsi import prepare_osp as prep
    from ecarsi import osp_contract, persample
    import pandas as pd
    import threading

    root = tmp_path/'output'
    unit = root/'unit'
    out = unit/'persample'
    out.mkdir(parents=True)
    plan = {'units': []}
    write_json(root/'organize'/'preparation_plan.json', {'input_identity': 'source', 'plan': plan})
    write_json(out/'manifest.json', {'mapping_identity': 'mapping', 'h5ad': str(tmp_path/'input.h5ad'),
        'samples': [{'value': v, 'n_cells': 100 if v == 'd' else 10, 'identity': v}
                    for v in ('a', 'b', 'd', 'c')],
        'sample_mapping': {'batch_key': None}, 'config': {'annotate': True, 'model': 'test'}, 'runtime': {}})
    monkeypatch.setattr(prep.L, 'units', lambda _: [unit])
    monkeypatch.setattr(prep.L, 'persample_root', lambda _: out)
    monkeypatch.setattr(prep.L, 'report_context', lambda _: {})
    monkeypatch.setattr(persample, 'main', lambda _: 0)
    monkeypatch.setattr(persample, 'mapping_identity', lambda _: 'mapping')
    monkeypatch.setattr(persample, 'build_entries', lambda *a: [
        {'value': v, 'n_cells': 100 if v == 'd' else 10, 'outdir': str(out/v)}
        for v in ('a', 'b', 'd', 'c')])
    monkeypatch.setattr(persample, '_pages', lambda _: None)
    monkeypatch.setattr(osp_contract, 'is_finished', lambda *a: False)
    computed = set()
    monkeypatch.setattr(prep, 'computed', lambda entry: entry['value'] in computed)
    from ecarsi import sample_mapping
    monkeypatch.setattr(sample_mapping, 'read_cell_table', lambda _: pd.DataFrame())
    calls = []
    def pool_call(fn, *args, **kwargs):
        calls.append((fn.__name__, args))
        if fn is prep.source_profiles:
            return ('source', [])
        if fn is persample.write_subsets:
            for entry in args[2]:
                write_json(Path(entry['outdir'])/'request.json', entry['request'])
    monkeypatch.setattr(prep, 'pool_call', pool_call)
    lock = threading.Lock()
    active = peak = 0
    def compute(path, *, compute_only):
        nonlocal active, peak
        value = path.parent.name
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(.05)
        with lock:
            computed.add(value)
            active -= 1
        return 0
    monkeypatch.setenv('PERSAMPLE_PARALLEL', '2')
    import ecarsi.osp_stage
    monkeypatch.setattr(ecarsi.osp_stage, 'run', compute)
    assert prep.main(['source', str(root), '--samples', '2']) == 0
    assert [name for name, _ in calls] == ['source_profiles', 'organize_confirmed', 'write_subsets']
    assert len(calls[-1][1][2]) == 2 and peak == 2
    assert [s['sample'] for s in read_json(root/'osp_prepared.json')['samples']] == ['a', 'b']
    calls.clear()
    assert prep.main(['source', str(root), '--samples', '2', '--max-prepared-samples', '3',
                      '--max-sample-cells', '20']) == 0
    assert [name for name, _ in calls] == ['source_profiles', 'organize_confirmed', 'write_subsets']
    assert [e['value'] for e in calls[-1][1][2]] == ['c']
    assert [s['sample'] for s in read_json(root/'osp_prepared.json')['samples']] == ['a', 'b', 'c']
    calls.clear()
    assert prep.main(['source', str(root), '--samples', '2', '--max-prepared-samples', '3']) == 0
    assert [name for name, _ in calls] == ['source_profiles', 'organize_confirmed']


def test_preparation_offer_counts_only_confirmed_missing_work(tmp_path):
    from ecarsi.preparation_offer import offer
    root = tmp_path/'output'
    sample_root = root/'units'/'u'/'persample'
    samples = []
    for name, cells in [('done', 10), ('computed', 20), ('missing', 30), ('broken', 40)]:
        directory = sample_root/name
        directory.mkdir(parents=True)
        samples.append({'value': name, 'identity': name, 'n_cells': cells, 'dir': str(directory)})
    write_json(sample_root/'manifest.json', {'schema_version': 2, 'mapping_identity': 'confirmed',
                                             'samples': samples})
    write_json(sample_root/'done'/'run_state.json', {'state': 'complete', 'identity': 'done', 'exit_code': 0})
    (sample_root/'computed'/'computed.h5ad').write_bytes(b'checkpoint')
    write_json(sample_root/'computed'/'compute_state.json', {'identity': 'computed',
               'files': {'computed.h5ad': {'size': 10, 'sha256': 'not-read-by-offer'}}})
    write_json(sample_root/'broken'/'compute_state.json', {'identity': 'broken',
               'files': {'computed.h5ad': {'size': 999, 'sha256': 'wrong'}}})
    assert offer(root) == {'prepared_count': 1, 'remaining_count': 2, 'min_missing_cells': 30}


def test_preparation_rechecks_checkpoint_content(tmp_path):
    from ecarsi.prepare_osp import computed
    from ecarsi.run_state import file_identity
    directory = tmp_path/'sample'
    directory.mkdir()
    output = directory/'computed.h5ad'
    output.write_bytes(b'valid')
    write_json(directory/'compute_state.json', {'identity': 'sample',
               'files': {'computed.h5ad': file_identity(output)}})
    entry = {'outdir': str(directory), 'identity': 'sample'}
    assert computed(entry)
    output.write_bytes(b'other')  # same size: an offer may pass, strict reuse must fail
    assert not computed(entry)
    write_json(directory/'compute_state.json', {'identity': 'sample', 'files': ['malformed']})
    assert not computed(entry)


def test_preparation_lends_driver_budget_while_pool_request_waits(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from ecarsi import prepare_osp
    import ecarsi.pool.client

    states = []
    @contextmanager
    def lend():
        states.append('compact')
        try:
            yield
        finally:
            states.append('restore')

    class Future:
        def result(self):
            states.append('result')
            return 'done'

    class Endpoint:
        def __init__(self, mode):
            assert mode == 'pool'
        def __enter__(self):
            return self
        def __exit__(self, *_):
            pass
        def submit(self, *_args, **_kwargs):
            states.append('submit')
            return Future()

    monkeypatch.setattr(prepare_osp, '_remote_wait', lend)
    monkeypatch.setattr(ecarsi.pool.client, 'PoolEndpoint', Endpoint)
    monkeypatch.setenv('ECA_DATASET_PEAK_MEMORY_BYTES', str(8*2**30))
    assert prepare_osp.pool_call(lambda: None, source=str(tmp_path)) == 'done'
    assert states == ['compact', 'submit', 'result', 'restore']


def test_preparation_backfills_small_work_without_unbounding_backlog(tmp_path):
    from ecarsi.batch import preparation_backfill_fits
    now = time.time()
    node = dict(id='n', role='preparation', cpus=2, cpu_ids=[0,1], memory=8*2**30,
                memory_headroom=8*2**30, observed_at=now, end_time=now+12000)
    ready = dict(id='ready', state='queued', attempt=1, preparation_complete=True,
                 cpus=1, memory_gb=32, hours=1)
    candidates = [dict(id=str(i), state='queued', attempt=0, cpus=2, memory_gb=32,
                       hours=1, resource_profile=dict(n_cells=n))
                  for i,n in enumerate((200000, 90000, 50000))]
    state = dict(nodes={'n':node}, datasets=[ready,*candidates],
                 pool_capacity=dict(observed_at=now, workers=[dict(free_cpus=8,
                     free_memory=48*2**30, end_time=now+12000)]))
    config = dict(directory=str(tmp_path), max_cpu_percent=90, preparation_module='v.prepare',
                  preparation_memory_gb=2, preparation_max_datasets=1, preparation_backfill_slots=1)
    assign(state,config,now)
    assert [r['state'] for r in candidates] == ['queued','assigned','queued']
    assert candidates[1]['preparation_backfill'] and reserved(candidates[1]) == 2*2**30
    candidates[1].update(state='queued', preparation_complete=True)
    assert not preparation_backfill_fits(state,config,candidates[2],now)
    candidates[1]['state']='completed'
    assert preparation_backfill_fits(state,config,candidates[2],now)
    assert not preparation_backfill_fits(state,config,candidates[2],now+31)
    state['pool_capacity']['workers'][0]['free_memory']=0
    assert not preparation_backfill_fits(state,config,candidates[2],now)
    state.pop('pool_capacity')
    assert not preparation_backfill_fits(state,config,candidates[2],now)


def test_backfill_offer_respects_worker_slots_and_actual_memory(monkeypatch):
    from contextlib import nullcontext
    from types import SimpleNamespace
    from ecarsi.batch import pool_capacity
    from ecarsi.pool import client
    now = time.time()
    worker = dict(cpus=8, memory=48*2**30, observed_at=now, end_time=now+7200,
                  task_slots=2, rss_bytes=20*2**30)
    task = dict(worker='w', state='running', cpus=1, memory=8*2**30)
    snapshot = dict(workers={'w':worker}, tasks={'t':task})
    monkeypatch.setattr(client,'connect', lambda *a,**k: nullcontext(
        SimpleNamespace(run_on_scheduler=lambda *a: snapshot)))
    offer = pool_capacity('unused')['workers'][0]
    assert offer['free_cpus']==7 and offer['free_memory']==28*2**30
    worker['task_slots']=1
    assert not pool_capacity('unused')['workers']

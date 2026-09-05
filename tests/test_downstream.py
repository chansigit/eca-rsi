from pathlib import Path
import json

import anndata as ad
import numpy as np
import pandas as pd
import pytest

from ecarsi import downstream as D, crosssample, zoomin, loop, layout as L
from ecarsi.run_state import write_json


def matrix(path, ids=('c1', 'c2', 'c3'), counts=1):
    a = ad.AnnData(np.full((len(ids), 2), counts, dtype=float),
                   obs=pd.DataFrame({'eca_sample_id': ['S1']*len(ids)}, index=list(ids)),
                   var=pd.DataFrame(index=['g1','g2']))
    a.layers['counts'] = a.X.copy()
    for key in ('_msp_action', '_msp_verdict', 'msp_ann_coarse', 'msp_ann_fine',
                'zmip_ann_coarse', 'zmip_ann_fine', 'zmip_lineage'):
        a.obs[key] = 'keep'
    a.write_h5ad(path)


def msp_fixture(root):
    source = root / 'source.h5ad'
    matrix(source)
    out = root / 'crosssample'
    out.mkdir()
    matrix(out / 'integrated.h5ad')
    matrix(out / 'annotated.h5ad', ('c1','c2'))
    (out / 'annotation_removed.csv').write_text('cell,annotate_remove\nc3,True\n')
    (out / 'report.html').write_text('<html></html>')
    for name in ('annotation_proposal.json','inspection_proposal.json'):
        write_json(out/name,{})
    return source,out


def test_msp_counts_and_conservation(tmp_path):
    src,out = msp_fixture(tmp_path)
    assert D.validate('msp',[src],out)['n_removed'] == 1
    matrix(out/'annotated.h5ad', ('c1','c2'),counts=2)
    with pytest.raises(ValueError,match='counts changed'):
        D.validate('msp',[src],out)


@pytest.mark.parametrize('damage',['missing_ledger','pending','invented','lost','report'])
def test_msp_rejects_incomplete_outputs(tmp_path, damage):
    src,out = msp_fixture(tmp_path)
    if damage == 'missing_ledger':
        (out/'annotation_removed.csv').unlink()
    elif damage == 'pending':
        (out/'.msp-state').mkdir()
        (out/'.msp-state'/'annotate.pending').touch()
    elif damage == 'invented':
        matrix(out/'annotated.h5ad',('c1','invented'))
    elif damage == 'lost':
        (out/'annotation_removed.csv').write_text('cell\n')
    else:
        (out/'report.html').write_text('broken')
    with pytest.raises(ValueError):
        D.validate('msp',[src],out)


def test_changed_input_and_runtime_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(D,'kernel_runtime',lambda *args: {'version':'A'})
    src=tmp_path/'src';src.write_text('first')
    out=tmp_path/'out'
    D.prepare('python','msp',[src],out,{})
    D.prepare('python','msp',[src],out,{})
    src.write_text('changed')
    with pytest.raises(ValueError,match='changed'):
        D.prepare('python','msp',[src],out,{})
    src.write_text('first')
    monkeypatch.setattr(D,'kernel_runtime',lambda *args: {'version':'B'})
    with pytest.raises(ValueError,match='changed'):
        D.prepare('python','msp',[src],out,{})


def test_failed_front_manifest_rejected(tmp_path):
    p=L.persample_manifest(tmp_path)
    write_json(p,{'schema_version':2,'state':'failed','failed_samples':['S1'],
                  'samples':[{'value':'S1','dir':'S1'}]})
    with pytest.raises(ValueError,match='verified complete'):
        crosssample.load_persample(tmp_path)


def test_legacy_empty_msp_no_longer_returns_success(tmp_path,monkeypatch):
    monkeypatch.setattr(D,'kernel_runtime',lambda *args: {})
    source=tmp_path/'input.h5ad'; source.write_text('input')
    out=tmp_path/'crosssample';out.mkdir()
    for name in L.MSP_CONTRACT:
        (out/name).touch()
    with pytest.raises(ValueError,match='legacy downstream'):
        loop._run_msp_from_h5ad('python',source,out,'sample',None,'test')


def test_zoomin_rejects_unverified_msp(tmp_path):
    r=L.round_dir(tmp_path,1)
    out=L.crosssample_dir(r);out.mkdir(parents=True)
    for name in L.MSP_CONTRACT:
        (out/name).touch()
    with pytest.raises((ValueError,FileNotFoundError)):
        zoomin.main([str(tmp_path),str(r)])


@pytest.mark.parametrize('value',[1,'false',None])
def test_inclusion_requires_boolean(value):
    with pytest.raises(ValueError):
        crosssample.validate_inclusion({'notes':'ok','samples':[{'sample':'S1','include':value,'reason':'ok'}]},['S1'])


def test_zmip_publication_validation(tmp_path):
    from zmip import publication, cache
    src=tmp_path/'source.h5ad';matrix(src)
    out=tmp_path/'zoomin';out.mkdir()
    write_json(out/'.zmip-run.json',{'run_id':'test'})
    write_json(out/'zmip_plan.json',{'lineages':[]})
    with publication.staging(out) as stage:
        matrix(stage/'annotated_zmip.h5ad',('c1','c2'))
        (stage/'zmip_removed.csv').write_text('cell\nc3\n')
        (stage/'zmip_reassigned.csv').write_text('cell\n')
        (stage/'report.html').write_text('<html></html>')
        publication.publish(out,stage)
    assert D.validate('zmip',[src],out)['n_output']==2
    (out/'.zmip-publish.json').write_text('{}')
    with pytest.raises(ValueError,match='publication'):
        D.validate('zmip',[src],out)


def test_options_shared_across_msp_entry_points(monkeypatch,tmp_path):
    monkeypatch.setenv('MSP_N_PCS','25')
    monkeypatch.setenv('MSP_RESOLUTIONS','0.3,1,2')
    monkeypatch.setenv('MSP_HARMONY','{"theta": 1}')
    cmd = crosssample.msp_command('python',['input'],'sample',tmp_path,None,'test')
    for flag in ('--n-pcs 25','--resolutions 0.3 1 2','--harmony theta=1'):
        assert flag in cmd

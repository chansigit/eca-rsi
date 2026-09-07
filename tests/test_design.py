"""Study-design text: derived from obs columns fixed per sample, passed as agent context only."""
import json
from pathlib import Path

import anndata as ad
import numpy as np
import pandas as pd
import pytest

from ecarsi import crosssample, downstream as D, layout as L, zoomin
from ecarsi.design import NOTE, design_table, design_text, render
from ecarsi.run_state import write_json


def unit_with(tmp_path, obs, sample_of_cell):
    unit = tmp_path / 'unit'
    (unit / L.INPUT).mkdir(parents=True)
    (unit / L.PERSAMPLE).mkdir()
    a = ad.AnnData(np.zeros((len(obs), 2)), obs=obs, var=pd.DataFrame(index=['g1', 'g2']))
    a.write_h5ad(L.input_h5ad(unit))
    write_json(L.persample_manifest(unit), {'sample_column': 'eca_sample_id', 'samples': []})
    pd.DataFrame({'eca_sample_id': sample_of_cell}, index=obs.index).to_csv(
        unit / L.PERSAMPLE / L.SAMPLE_MAPPING, index_label='cell_id')
    return unit


def facs_obs():
    return pd.DataFrame({
        'cell_id': [f'A{i}.P.3_8_M' for i in range(6)],
        'subtissue': ['Immune (CD45+)'] * 3 + ['Epithelial'] * 3,
        'mouse.id': ['3_8_M', 'missing', '3_8_M', '3_9_M', '3_9_M', '3_9_M'],  # left-join gap ignored
        'mouse.sex': ['M'] * 6,                       # one value overall
        'cell_type': ['T', 'B', 'T', 'Epi', 'Epi', 'Fib'],  # varies within a sample
        'pct_counts_mt': [0.1, 0.2, 0.3, 0.1, 0.2, 0.3],   # float QC
        'n_genes': [10, 20, 30, 10, 20, 30],
    }, index=[f'c{i}' for i in range(6)])


def test_facs_like_unit_yields_gate_and_mouse_per_plate(tmp_path):
    unit = unit_with(tmp_path, facs_obs(), ['P1'] * 3 + ['P2'] * 3)
    assert design_text(unit) == (
        NOTE + '\n'
        'sample P1: mouse.id=3_8_M, subtissue=Immune (CD45+)\n'
        'sample P2: mouse.id=3_9_M, subtissue=Epithelial')


def test_single_sample_or_missing_input_is_empty(tmp_path):
    assert design_text(unit_with(tmp_path, facs_obs(), ['P1'] * 6)) == ''
    assert design_text(tmp_path / 'nowhere') == ''


def test_many_samples_and_columns_are_summarised():
    n = 130
    obs = pd.DataFrame({chr(c): [f'{chr(c)}{i}' for i in range(n)] for c in range(ord('a'), ord('h'))},
                       index=[f'c{i}' for i in range(n)])
    sample = pd.Series([f'S{i}' for i in range(n)], index=obs.index)
    text = render(design_table(obs, sample))
    assert f'{n} samples (too many to list).' in text
    assert 'column a takes values a0, a1, a10' in text and f'... ({n} values)' in text
    assert 'column g takes' not in text and text.endswith('Also constant within each sample: g')


def test_commands_carry_design_as_agent_context_only(tmp_path, monkeypatch):
    assert "--design-context 'gate per plate'" in crosssample.msp_command(
        'python', ['in'], 's', tmp_path, None, 'm', design='gate per plate')
    assert "--design-context X" in zoomin.zmip_command('python', Path('a'), tmp_path, 'm', None, design='X')
    assert '--design-context' not in crosssample.msp_command('python', ['in'], 's', tmp_path, None, 'm')
    assert '--design-context' not in zoomin.zmip_command('python', Path('a'), tmp_path, 'm', None)
    # a different design text must not invalidate a prepared/completed round
    monkeypatch.setattr(D, 'kernel_runtime', lambda *args: {'version': 'A'})
    src = tmp_path / 'src'
    src.write_text('data')
    cfg = {'batch_col': 's', 'species': None, 'options': D.options('msp')}
    identity = D.prepare('python', 'msp', [src], tmp_path / 'out', cfg)
    assert 'design' not in json.dumps(identity)
    D.prepare('python', 'msp', [src], tmp_path / 'out', cfg)  # same identity, whatever the design text

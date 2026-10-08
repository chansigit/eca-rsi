"""A zoom-in reassignment is for a clean population; cells that still express this lineage's markers are doublets."""
import numpy as np
import pandas as pd
import pytest

from ecarsi.stages.zoomin import REASSIGN_OWN_MARKER_FRACTION, own_marker_positivity, previous_reassignment, reassign_problem, reassign_problems

ad = pytest.importorskip('anndata')


def lineage():
    genes = ['LYZ', 'CD68', 'CD3D']
    rows = [[1, 1, 0]] * 10 + [[1, 1, 1]] * 5 + [[0, 0, 1]] * 5      # myeloid core, T+myeloid doublets, clean T cells
    names = [f'm{i}' for i in range(10)] + [f'd{i}' for i in range(5)] + [f't{i}' for i in range(5)]
    return ad.AnnData(X=np.array(rows, dtype=float), obs=pd.DataFrame(index=names), var=pd.DataFrame(index=genes))


def test_a_mixed_profile_is_refused_and_a_clean_population_passes():
    data = lineage()
    own = ['LYZ', 'CD68', 'ABSENT']
    core = own_marker_positivity(data, own)
    assert core == pytest.approx(30 / 40)
    doublets = [f'd{i}' for i in range(5)]
    problem = reassign_problem(5, own_marker_positivity(data, own, doublets), core, 'T cell', None)
    assert problem.startswith("Reassignment to 'T cell' rejected") and 'doublet' in problem
    clean = [f't{i}' for i in range(5)]
    assert own_marker_positivity(data, own, clean) == 0.0
    assert reassign_problem(5, 0.0, core, 'T cell', None) == ''
    assert reassign_problem(5, REASSIGN_OWN_MARKER_FRACTION * core - 1e-9, core, 'T cell', None) == ''
    assert own_marker_positivity(data, ['ABSENT']) is None and reassign_problem(5, None, core, 'T cell', None) == ''
    assert reassign_problem(5, 0.7, None, 'T cell', None) == ''   # no markers for this lineage: nothing to judge by


def test_an_earlier_move_of_the_same_cells_recurs_whatever_the_labels_and_rounds_between():
    # moved in r04 under one target name, untouched in r05, proposed again in r06: the cells decide (#55 item 3)
    obs = pd.DataFrame({'r04_zmip_reassigned_from': ['Distal nephron'] * 3 + [None, ''],
                        'r04_zmip_ann_coarse': ['Proximal tubule cell'] * 3 + ['x', 'x'],
                        'r05_zmip_reassigned_from': [''] * 5}, index=list('abcde'))
    bounce = previous_reassignment(obs)
    assert bounce == dict(round='r04', share=0.6, cells=3)
    assert previous_reassignment(obs.iloc[3:]) is None                                    # under half moved before
    assert previous_reassignment(obs.drop(columns=['r04_zmip_reassigned_from', 'r05_zmip_reassigned_from'])) is None
    assert previous_reassignment(obs.iloc[:0]) is None
    text = reassign_problem(5, 0.8, 0.8, 'Proximal tubule', bounce)
    assert '60% of these cells were already reassigned in r04' in text


def test_every_refused_entry_is_reported_at_once_with_its_cell_count():
    data = lineage()
    data.obs['msp_leiden_r1.0'] = ['1'] * 10 + ['2'] * 5 + ['3'] * 5
    data.obs['msp_leiden_r2.0'] = ['9'] * 20
    groups = [dict(cluster_id='9', decisions=[
        dict(action='reassign', type_clusters=['2'], reassign_to='T cell'),       # doublets: refused
        dict(action='keep', type_clusters=['1']),
        dict(action='reassign', type_clusters=['3'], reassign_to='T cell'),       # clean: passes
        dict(action='reassign', type_clusters=['1'], reassign_to='B cell')])]     # the core itself: refused
    own = ['LYZ', 'CD68']
    problems = reassign_problems(groups, data, own, own_marker_positivity(data, own))
    assert [p.split(']')[0] for p in problems] == ['[9:2', '[9:1'] and all('rejected' in p for p in problems)
    assert [d.get('n_cells') for d in groups[0]['decisions']] == [5, None, 5, 10]

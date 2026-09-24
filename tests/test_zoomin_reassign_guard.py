"""A zoom-in reassignment is for a clean population; cells that still express this lineage's markers are doublets."""
import numpy as np
import pandas as pd
import pytest

from ecarsi.stages.zoomin import REASSIGN_OWN_MARKER_FRACTION, own_marker_positivity, previous_reassignment, reassign_problem

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


def test_the_previous_round_bounce_is_detected_and_named():
    obs = pd.DataFrame({'r06_zmip_reassigned_from': ['Myeloid'] * 4 + [''],
                        'r06_zmip_ann_coarse': ['T cell'] * 4 + ['Myeloid cell'],
                        'r05_zmip_reassigned_from': [''] * 5}, index=list('abcde'))
    bounce = previous_reassignment(obs, 'Myeloid', 'T cell')
    assert bounce == dict(round='r06', share=0.8, cells=4)
    assert previous_reassignment(obs, 'Myeloid', 'B cell') is None
    assert previous_reassignment(obs.drop(columns=['r06_zmip_reassigned_from', 'r05_zmip_reassigned_from']), 'Myeloid', 'T cell') is None
    assert previous_reassignment(obs.iloc[:0], 'Myeloid', 'T cell') is None
    text = reassign_problem(5, 0.8, 0.8, 'T cell', bounce)
    assert 'already reassigned from this lineage to \'T cell\' in r06 (80% of these cells)' in text

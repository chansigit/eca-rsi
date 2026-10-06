"""#27: msp's minor-sibling fragment QC needs an effect size as well as p, and needs_review lists its removals."""
import json

import numpy as np
import pandas as pd

from ecarsi.stages.common import fragment_reasons
from ecarsi.stages.release import review_items
from msp.integrate.fragments import _mwu_greater, _mwu_test


def test_a_fragment_test_needs_auc_07_as_well_as_p():
    rng = np.random.default_rng(0)
    cores, shifted = rng.normal(0, 1, 2000), rng.normal(0.3, 1, 300)
    p, auc = _mwu_test(shifted, cores)
    assert p < 1e-4 and 0.5 < auc < 0.7 and _mwu_greater(shifted, cores) is False  # significant, but a small shift
    assert _mwu_greater(rng.normal(1.5, 1, 30), cores) is True


def test_removed_fragments_name_their_tests_and_needs_review_counts_them(monkeypatch):
    table = pd.DataFrame({'subcluster': ['c1_1', 'c1_2', 'c2_1'], 'recommend_removal': ['True', 'True', 'False'],
                          'decontX_significant': ['False', 'True', 'False'], 'dissociation_significant': ['True', 'False', 'False'],
                          'doublet_significant': ['False', 'False', 'False'], 'mt_significant': ['True', 'False', 'False'],
                          'pct_drop_upstream': [0.0, 80.0, 0.0]})
    tests = fragment_reasons(table)
    assert tests == {'c1_1': {'tests': ['dissociation', 'mt']}, 'c1_2': {'tests': ['decontX', 'dropped upstream']}}
    reason = lambda fragment: json.dumps([{'code': 'fragment_qc', 'detail': tests[fragment]}])
    exclusions = pd.DataFrame({'round': [1, 1, 1, 2], 'release_stage': ['cross-sample.finalize'] * 3 + ['zoom-in.apply'],
                               'cell_uid': list('abcd'), 'reason': [reason('c1_1'), reason('c1_1'), reason('c1_2'),
                                                                    json.dumps([{'code': 'fragment_qc'}])]})
    monkeypatch.setattr('ecarsi.stages.release.verified', lambda ref: {'skipped_samples': []})
    items = [i for i in review_items(dict(rounds=[], forced_release=False, per_sample=None), exclusions, []) if i.kind == 'fragment_removed']
    assert [(i.round, i.n_cells, i.note.split(': ', 1)[1]) for i in items] == [
        (1, 1, 'decontX, dropped upstream'), (1, 2, 'dissociation, mt'), (2, 1, 'tests not recorded')]

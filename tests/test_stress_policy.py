"""Decision 0017: the rules that decide whether a stress, dissociation or dying removal stands."""
import numpy as np
import pandas as pd

from ecarsi.stages import common
from ecarsi.stages.common import dying_evidence, guard_stress, mark_retained, soft_fragments


def removal(reason, **changes):
    return {'cluster_id': '3', 'action': 'remove', 'remove_reason': reason, 'merge_target': None, **changes}


def qc(mt_target, genes_target, n=20):
    obs = pd.DataFrame({'pct_counts_mt': [mt_target] * n + [2.0] * n, 'n_genes_by_counts': [genes_target] * n + [2000.0] * n})
    obs.iloc[::7, 0] += 0.5  # ties only would still decide, but real data is never constant
    target = np.arange(2 * n) < n
    return obs, target, ~target


def test_dying_needs_a_clearly_higher_mitochondrial_fraction_or_clearly_fewer_genes():
    obs, target, comparison = qc(25.0, 2000.0)
    assert dying_evidence(obs, target, comparison)[0]
    obs, target, comparison = qc(2.0, 300.0)
    assert dying_evidence(obs, target, comparison)[0]
    obs, target, comparison = qc(2.0, 2000.0)
    supported, note = dying_evidence(obs, target, comparison)
    assert not supported and 'AUC' in note
    supported, note = dying_evidence(obs.drop(columns=['pct_counts_mt', 'n_genes_by_counts']), target, comparison)
    assert not supported and 'missing' in note
    obs, target, comparison = qc(25.0, 2000.0, n=5)
    assert not dying_evidence(obs, target, comparison)[0]  # too few cells to tell


def test_a_removal_stands_on_the_stress_gene_rule_or_the_dying_check_and_stays_otherwise():
    flagged = removal('stress')
    assert not guard_stress(flagged, 'remove', 50, True, None) and flagged['action'] == 'remove' and flagged['host_evidence']
    unmarked = removal('dissociation', merge_target='4')
    assert guard_stress(unmarked, 'remove', 50, False, None)
    assert unmarked['action'] == 'keep' and unmarked['remove_reason'] is None and unmarked['review_required']
    assert unmarked['requested_remove_reason'] == 'dissociation' and unmarked['host_adjustment']['n_cells'] == 50
    assert unmarked['merge_target'] == '4'  # the stage decides whether the merge can stay
    dying = removal('dying')
    assert not guard_stress(dying, 'remove', 50, False, lambda: (True, 'pct_counts_mt AUC 0.90'))
    assert guard_stress(removal('dying'), 'remove', 50, True, lambda: (False, 'no'))  # the gene rule is not the dying check
    assert not guard_stress(removal('stress'), 'remove', 9, False, None)  # below 10 cells the agent's reason stands
    for other in (removal('doublet'), {**removal('stress'), 'action': 'keep'}):
        assert not guard_stress(other, 'remove', 50, False, None) and 'host_adjustment' not in other


def test_keep_retains_every_stress_dissociation_or_dying_removal():
    for reason in common.STRESS_STATES:
        entry = removal(reason)
        assert guard_stress(entry, 'keep', 3, True, lambda: (True, ''))
        assert entry['host_adjustment']['stress_policy'] == 'keep' and entry['host_adjustment']['state'] == reason


def test_soft_fragments_are_those_removed_only_by_their_dissociation_or_mitochondrial_test():
    from msp.integrate.fragments import DROP_PCT_THRESH
    assert common.FRAGMENT_DROP_PCT == DROP_PCT_THRESH
    rows = pd.DataFrame([
        dict(subcluster='a', recommend_removal='True', dissociation_significant='True', mt_significant='False', pct_drop_upstream='0'),
        dict(subcluster='b', recommend_removal='True', dissociation_significant='False', mt_significant='True', pct_drop_upstream=''),
        dict(subcluster='c', recommend_removal='True', dissociation_significant='True', doublet_significant='True'),
        dict(subcluster='d', recommend_removal='True', mt_significant='True', pct_drop_upstream='75'),
        dict(subcluster='e', recommend_removal='False', dissociation_significant='True'),
        dict(subcluster='f', recommend_removal='True', decontX_significant='True'),
    ]).fillna('')
    assert soft_fragments(rows) == {'a': 'dissociation', 'b': 'dying'}


def test_a_cell_keeps_the_state_it_was_first_retained_for():
    obs = pd.DataFrame(index=['x', 'y', 'z'])
    mark_retained(obs, {'x': 'stress'})
    mark_retained(obs, {'x': 'dying', 'y': 'dissociation'})
    assert obs['retained_state'].astype(str).tolist() == ['stress', 'dissociation', '']

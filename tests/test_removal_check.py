"""Decision 0025: an agent's removal of 10 or more cells stands only when the code check of its reason supports
it; otherwise its cells stay, labelled. Checks on synthetic cells: 200 kept cells of two types A and B (genes 0-19
and 20-39) and a target group of 30."""
import numpy as np
import pandas as pd
import scipy.sparse as sp

from ecarsi.stages.common import (REMOVAL_CHECK_POLICY, cell_rule_evidence, guard_removal, guard_small_type_removals,
                                  guard_unchecked_drops, removal_evidence, small_type_removals)

RNG = np.random.default_rng(0)


def cells(target_genes, depth, n_target=30):
    """(obs, counts, labels, target): types A and B at full depth, the target expressing target_genes at `depth`."""
    n = 200 + n_target
    counts = np.zeros((n, 70), dtype=np.int32)
    counts[:100, 0:20] = RNG.poisson(5, (100, 20))
    counts[100:200, 20:40] = RNG.poisson(5, (100, 20))
    counts[:200, 40:60] = RNG.poisson(3, (200, 20))
    counts[200:, target_genes] = RNG.poisson(depth, (n_target, len(target_genes)))
    labels = np.array(['A'] * 100 + ['B'] * 100 + ['A'] * n_target)
    target = np.arange(n) >= 200
    obs = pd.DataFrame({'n_genes_by_counts': (counts > 0).sum(1), 'total_counts': counts.sum(1),
                        'pct_counts_mt': np.full(n, 2.0), 'doublet_score': np.full(n, 0.05),
                        'osp_doublet_threshold': np.full(n, 0.3), 'foreign_B': RNG.normal(0, 0.1, n)},
                       index=[f'c{i}' for i in range(n)])
    return obs, sp.csr_matrix(counts), labels, target


def check(reason, obs, counts, labels, target, foreign=None):
    return removal_evidence(reason, obs, target, ~target, counts, labels, foreign)


def test_low_quality_needs_worse_qc_and_no_gene_of_its_own():
    # a shallow copy of type A: fewer genes and counts, nothing of its own
    assert check('low-quality', *cells(list(range(0, 20)), 0.3))[0]
    # just as shallow, but a few genes nobody else expresses: a real cell type, kept
    supported, note = check('low-quality', *cells(list(range(60, 66)), 3))
    assert not supported and 'genes of its own' in note and not note.endswith(' 0 genes of its own')
    # as deep as the rest: nothing wrong with its QC
    assert not check('low-quality', *cells(list(range(0, 20)) + list(range(40, 60)), 5))[0]


def test_a_doublet_is_half_above_its_library_threshold_or_carries_another_lineage_on_top_of_its_own():
    obs, counts, labels, target = cells(list(range(0, 40)), 5)
    assert not check('doublet', obs, counts, labels, target)[0]
    obs.loc[target, 'doublet_score'] = np.where(np.arange(target.sum()) < 20, 0.5, 0.1)  # 2 of 3 above
    assert check('doublet', obs, counts, labels, target)[0]
    obs.loc[target, 'doublet_score'] = 0.05
    obs.loc[target, 'foreign_B'] = 1.0
    assert check('doublet', obs, counts, labels, target, (['foreign_B'], 0.8, 0.9))[0]
    # the other lineage's markers without this lineage's own: misplaced cells, to reassign, not a doublet
    supported, note = check('doublet', obs, counts, labels, target, (['foreign_B'], 0.2, 0.9))
    assert not supported and 'own-marker positivity' in note


def test_ambient_other_and_batch_name_nothing_a_check_could_support():
    obs, counts, labels, target = cells(list(range(0, 20)), 0.3)
    for reason in ('ambient', 'other', 'batch'):
        supported, note = check(reason, obs, counts, labels, target)
        assert not supported and reason in note


def test_guard_removal_keeps_an_unsupported_removal_labelled_and_lets_small_ones_and_stress_pass():
    entry = dict(action='remove', remove_reason='other', type_clusters=['5'])
    assert guard_removal(entry, 166, lambda: (False, 'no check'))
    assert entry['action'] == 'keep' and entry['requested_remove_reason'] == 'other' and entry['review_required']
    assert entry['host_adjustment']['policy'] == REMOVAL_CHECK_POLICY and entry['host_adjustment']['state'] == 'other'
    small = dict(action='remove', remove_reason='other')
    assert not guard_removal(small, 9, lambda: (False, 'never asked')) and small['action'] == 'remove'
    stress = dict(action='remove', remove_reason='stress')
    assert not guard_removal(stress, 50, lambda: (False, 'guard_stress decides')) and stress['action'] == 'remove'
    supported = dict(action='remove', remove_reason='doublet')
    assert not guard_removal(supported, 50, lambda: (True, 'share 0.75')) and supported['host_evidence'] == 'share 0.75'


def test_cross_sample_drops_need_their_verdicts_check_and_cell_rules_theirs():
    proposal = dict(clusters=[dict(cluster='1', verdict='artifact-doublet', action='drop'),
                              dict(cluster='2', verdict='artifact-ambient', action='drop'),
                              dict(cluster='3', verdict='artifact-lowquality', action='drop'),
                              dict(cluster='4', verdict='real', action='keep')],
                    cell_actions=[dict(cluster='4', metric='decontX_contamination', op='>', value=0.5, action='drop', reason='ambient'),
                                  dict(cluster='4', metric='pct_counts_mt', op='>', value=20, action='drop', reason='low-quality'),
                                  dict(cluster='4', metric='pct_counts_mt', op='>', value=30, action='flag', reason='low-quality')])
    verdicts = {'1': (500, True, 'share 0.75'), '2': (300, False, 'no check'), '3': (8, False, 'too small to check')}
    rules = {'decontX_contamination': (40, False, 'not a QC metric'), 'pct_counts_mt': (12, True, 'outliers')}
    adjusted = guard_unchecked_drops(proposal, lambda cluster, reason: verdicts[cluster], lambda rule: rules[rule['metric']])
    actions = {e['cluster']: e['action'] for e in proposal['clusters']}
    assert actions == {'1': 'drop', '2': 'flag', '3': 'drop', '4': 'keep'}
    assert [a['action'] for a in proposal['cell_actions']] == ['flag', 'drop', 'flag']
    assert proposal['cell_actions'][0]['host_adjustment']['n_cells'] == 40 and proposal['cell_actions'][1]['host_evidence'] == 'outliers'
    assert set(adjusted) == {'2', '4'} and adjusted['2']['state'] == 'ambient' and adjusted['4']['state'] == 'ambient'


def mito_cluster(tail_mt=30.0, n_tail=15, tail_genes=()):
    """(obs, counts, cluster mask): cluster A of 200 cells around 5 % mitochondrial reads, `n_tail` of them at
    tail_mt, optionally expressing genes no other cell does; 100 cells of another cluster at 40 %."""
    n = 300
    counts = np.zeros((n, 70), dtype=np.int32)
    counts[:, 0:20] = RNG.poisson(5, (n, 20))
    tail = np.arange(n) < n_tail
    counts[np.ix_(tail, list(tail_genes))] = RNG.poisson(4, (n_tail, len(tail_genes))) if len(tail_genes) else 0
    mt = np.r_[np.full(n_tail, tail_mt), RNG.normal(5, 1, 200 - n_tail), np.full(100, 40.0)]
    genes = np.r_[RNG.normal(2000, 200, 200), np.full(100, 300.0)]
    obs = pd.DataFrame({'pct_counts_mt': mt, 'n_genes_by_counts': genes, 'decontX_contamination': np.zeros(n)},
                       index=[f'c{i}' for i in range(n)])
    return obs, sp.csr_matrix(counts), np.arange(n) < 200


def rule(metric='pct_counts_mt', op='>', value=20, reason='low-quality'):
    return dict(cluster='A', metric=metric, op=op, value=value, reason=reason, action='drop')


def test_a_cell_rule_drops_only_its_clusters_outliers_on_the_bad_side_of_a_qc_metric():
    obs, counts, cluster = mito_cluster()
    n, supported, note = cell_rule_evidence(rule(), obs, cluster, counts)
    assert (n, supported) == (15, True) and '0 genes of their own' in note  # the 40 % cells of the other cluster do not count
    # a line inside the body of the cluster: median 5, MAD about 0.7, so outliers start near 8.5
    n, supported, note = cell_rule_evidence(rule(value=6), obs, cluster, counts)
    assert not supported and n > 30 and 'body of the cluster' in note
    # the wrong side, another metric, another reason
    assert not cell_rule_evidence(rule(op='<', value=3), obs, cluster, counts)[1]
    assert 'high side' in cell_rule_evidence(rule(op='<', value=3), obs, cluster, counts)[2]
    assert not cell_rule_evidence(rule(metric='decontX_contamination', value=0.5), obs, cluster, counts)[1]
    assert not cell_rule_evidence(rule(reason='doublet'), obs, cluster, counts)[1]
    # genes are cut below, on the log1p scale: 2000 +- 200 genes put the bound near 1150
    assert cell_rule_evidence(rule('n_genes_by_counts', '<', 500), obs, cluster, counts)[1]
    assert not cell_rule_evidence(rule('n_genes_by_counts', '<', 1800), obs, cluster, counts)[1]
    # under 10 cut cells, own genes are not counted; over 10, a tail with genes of its own is a cell type
    obs, counts, cluster = mito_cluster(n_tail=5, tail_genes=range(60, 66))
    assert cell_rule_evidence(rule(), obs, cluster, counts)[:2] == (5, True)
    obs, counts, cluster = mito_cluster(tail_genes=range(60, 66))
    supported, note = cell_rule_evidence(rule(), obs, cluster, counts)[1:]
    assert not supported and '6 genes of their own' in note


def test_a_removal_under_ten_cells_needs_both_phases():
    typed = [dict(cluster_id='1', action='remove', remove_reason='doublet'), dict(cluster_id='2', action='remove', remove_reason='other'),
             dict(cluster_id='3', action='remove', remove_reason='low-quality'), dict(cluster_id='4', action='keep')]
    proposal = dict(clusters=[dict(cluster='1', action='keep'), dict(cluster='2', action='drop'), dict(cluster='3', action='keep'),
                              dict(cluster='4', action='keep')])
    sizes = {'1': 6, '2': 4, '3': 40, '4': 100}
    assert small_type_removals(typed, proposal['clusters'], sizes) == ['1']  # 2: both drop it; 3: large, checked
    adjusted = guard_small_type_removals(proposal, typed, sizes)
    one = proposal['clusters'][0]
    assert set(adjusted) == {'1'} and one['action'] == 'flag' and one['requested_action'] == 'keep' and one['review_required']
    assert one['host_adjustment']['policy'] == REMOVAL_CHECK_POLICY and one['host_adjustment']['state'] == 'doublet'
    assert small_type_removals(typed, proposal['clusters'], sizes) == ['1']  # finalize finds the same clusters

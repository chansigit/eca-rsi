"""Zoom-in reassignments reach needs_review from the quality decisions, and the same move in several rounds recurs."""
from ecarsi.review import _mark_recurring
from ecarsi.stages.release import reassign_items


def entry(number):
    return dict(round=number, stage='zoom-in', scope='Myeloid', source=dict(path=f'/x/round{number}/annotation_proposal.json'))


def quality(fine, recurring=None):
    decision = dict(type_clusters=['6'], action='reassign', reassign_to='T cell', fine_label=fine, confidence='high',
                    rationale='canonical T markers')
    if recurring:
        decision['recurring'] = recurring
    return dict(clusters=[dict(cluster_id='9', decisions=[decision, dict(type_clusters=['2'], action='keep', confidence='high')])])


def test_reassignments_become_review_items_and_recur_across_rounds_despite_different_wording():
    items = reassign_items(entry(6), quality('CD3D+ T cell')) + reassign_items(entry(7), quality('T cell', dict(round='r06', share=0.97, cells=67)))
    assert [it.kind for it in items] == ['reassigned', 'reassigned']
    assert items[0].cluster == '9:6' and items[0].action == '→ T cell' and items[0].extra == {'reassign_to': 'T cell'}
    assert items[1].note.startswith('[already moved in r06, 97% of cells] canonical')
    _mark_recurring(items)
    assert items[0].extra['recurs_in_rounds'] == [6, 7] and items[1].extra['recurs_in_rounds'] == [6, 7]
    assert reassign_items(entry(1), dict(clusters=[dict(cluster_id='1', decisions=[dict(action='remove')])])) == []

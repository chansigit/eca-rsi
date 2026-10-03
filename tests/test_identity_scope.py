"""The agent runtime (agent-harness-bridge) is provenance, never identity: a
bridge patch release must not invalidate an in-flight unit (2026-09-12,
0.2.11 -> 0.2.12 did exactly that under the old rule)."""




def test_round_one_is_exempt_from_the_over_budget_review_flag():
    from ecarsi.review import _loop_items

    st = {"frac": 0.25, "removed": 250, "n_in": 1000, "reason": "round 1 never releases"}
    assert _loop_items(1, st, forced=False, last=False) == []
    assert [i.action for i in _loop_items(2, st, forced=False, last=False)] == ["over budget"]


def test_uncertain_boundary_is_visible_in_release_review():
    from ecarsi.review import _annotation_items, to_markdown, to_html
    prop = {"boundary_reviews": [{"coarse_labels": ["Stromal", "Fibroblast"],
                                 "evidence": "Lineage distinction remains unresolved", "uncertain": True}]}
    items = _annotation_items(3, "crosssample", "", prop, {}, {}, "report.html")
    assert len(items) == 1 and items[0].kind == "annotation_boundary"
    assert "Lineage distinction remains unresolved" in to_markdown(items, "test", 3)
    assert "Lineage distinction remains unresolved" in to_html(items)

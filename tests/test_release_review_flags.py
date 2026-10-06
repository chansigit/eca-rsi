"""inspect_flag items (#45): a zoom-in QC cluster's low-confidence keeps share one line, small intersections stay out;
cross-sample flags keep a line each."""
from ecarsi.stages.release import LOW_KEEP_MIN_CELLS, flag_items


def entry(tmp_path, table=True):
    lineage = tmp_path / "Stromal"
    lineage.mkdir()
    if table:  # rows: QC clusters (msp_leiden_r2.0), columns: type clusters (msp_leiden_r1.0)
        (lineage / "type_quality_intersections.csv").write_text("msp_leiden_r2.0,0,1,2\n0,260,3,40\n1,0,1,0\n")
    return dict(round=1, stage="zoom-in", scope="Stromal", source=dict(path=str(lineage / "annotation_proposal.json")))


def decision(types, confidence, action="keep", rationale="why"):
    return dict(type_clusters=types, action=action, confidence=confidence, rationale=rationale)


def zoomin():
    return dict(clusters=[
        dict(cluster_id="0", decisions=[decision(["0"], "high"), decision(["1"], "low", rationale="Single cell, keep."),
                                        decision(["2"], "low", rationale="mixed state"),
                                        decision(["1"], "low", action="reassign", rationale="moves")]),
        dict(cluster_id="1", decisions=[decision(["1"], "low", rationale="one cell")])])


def test_low_confidence_keeps_of_a_qc_cluster_share_a_line_without_small_intersections(tmp_path):
    assert 3 < LOW_KEEP_MIN_CELLS <= 40
    reassign, keep = flag_items(entry(tmp_path), zoomin())
    assert (keep.cluster, keep.n_cells, keep.action, keep.confidence) == ("0", 40, "keep", "low")
    assert keep.note == "types 2 (40 cells): mixed state"
    assert (reassign.action, reassign.note) == ("reassign", "moves")   # a low-confidence move keeps its own line


def test_without_the_intersection_table_every_low_confidence_keep_stays_in_one_line(tmp_path):
    keeps = [i for i in flag_items(entry(tmp_path, table=False), zoomin()) if i.action == "keep"]
    assert [(i.cluster, i.n_cells) for i in keeps] == [("0", None), ("1", None)]
    assert keeps[0].note == "types 1: Single cell, keep. | types 2: mixed state"


def test_cross_sample_flags_keep_a_line_each(tmp_path):
    quality = dict(clusters=[dict(cluster="3", verdict="ambiguous", action="keep", confidence="medium", rationale="a"),
                             dict(cluster="4", action="flag", confidence="high", rationale="b"),
                             dict(cluster="5", action="keep", confidence="high", rationale="c"),
                             dict(cluster="6", action="drop", confidence="low", rationale="d")])
    items = flag_items(dict(round=1, stage="cross-sample", scope="", source=dict(path=str(tmp_path / "inspection_proposal.json"))), quality)
    assert [(i.cluster, i.note) for i in items] == [("3", "a"), ("4", "b"), ("6", "d")]

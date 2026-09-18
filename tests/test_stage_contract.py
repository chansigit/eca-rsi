"""Protocol v4 pieces shared by the stage programs; pure Python, no kernel needed."""
import json

import pytest
from jsonschema import Draft202012Validator

from ecarsi.stages import contract


def test_no_argument_tools_tolerate_a_habitual_offset():
    Draft202012Validator.check_schema(contract.NO_ARGUMENTS)
    validator = Draft202012Validator(contract.NO_ARGUMENTS)
    validator.validate({})
    validator.validate({'offset': 30})   # ignored, never a wasted turn
    assert not validator.is_valid({'path': 'x'})


def test_deg_lookup_asks_for_cluster_or_gene_with_optional_thresholds():
    lookup = Draft202012Validator(contract.deg_lookup_schema())
    assert lookup.is_valid({'cluster': '3'}) and lookup.is_valid({'gene': 'CD3D', 'min_logfc': 1, 'max_padj': None})
    assert not lookup.is_valid({}) and not lookup.is_valid({'top_n': 5}) and not lookup.is_valid({'cluster': '3', 'bogus': 1})
    assert contract.lookup_arguments({'gene': 'CD3D', 'max_padj': None}, 'msp_leiden_r1.0') == {'gene': 'CD3D', 'key': 'msp_leiden_r1.0', 'cluster': ''}


def test_a_proposal_with_a_trailing_quote_is_accepted():
    proposal = '{"clusters": [{"cluster_id": "0"}]}'
    assert contract.proposal({'proposal_json': proposal + '"'}) == {'clusters': [{'cluster_id': '0'}]}
    assert contract.proposal({'proposal_json': {'clusters': []}}) == {'clusters': []}
    with pytest.raises(ValueError):   # real trailing data still reaches the error path
        contract.proposal({'proposal_json': proposal + ', "more": 1}'})
    assert contract.json_hint('Extra data: line 1 column 9') == contract.JSON_NOTE and contract.json_hint('Cover each cluster') == ''


def test_checklists_exist_for_every_stage_session():
    for name in ('crosssample-inclusion', 'crosssample-annotation', 'zoomin-plan', 'zoomin-annotation'):
        assert 'Required order' in contract.checklist(name)


def test_compact_tables_keeps_markers_graph_and_unknown_blocks():
    from ecarsi.stages.execution import compact_tables
    de = "group,names,scores,logfoldchanges,pvals,pvals_adj,pct1,pct2\n" + "\n".join(
        f"{g},G{g}_{i},{30-i},{2.5+i/100:.7f},1e-9,1e-8,0.91234,0.12345" for g in ("0", "1") for i in range(20)) + "\n"
    paga = "leiden_r1.0,0,1,2\n0,0.0,0.9316574,0.01\n1,0.9316574,0.0,0.0\n2,0.01,0.0,0.0\n"
    qc = "metric,value\nmedian_doublet_score,0.0453\n"
    text = "\n\n".join(("de_top_genes_leiden_r1.0.csv\n" + de, "paga_connectivities_leiden_r1.0.csv\n" + paga, "qc_summary.csv\n" + qc))
    out = compact_tables(text)
    assert out.count("G0_") == 15 and "G0_14 2.64 0.91/0.12" in out and "G0_19" not in out
    assert "0: 1 0.93" in out and "\n2: none" in out and "qc_summary.csv\nmetric,value" in out
    assert len(out) < len(text) / 2
    assert compact_tables(text[: len(text) // 3]).startswith("de_top_genes_leiden_r1.0.csv")  # a page cut mid-row

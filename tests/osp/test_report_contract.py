import pytest

from osp.report import _find_cluster_tables, _read_qc_stats


def test_report_rejects_ambiguous_qc_and_cluster_files(tmp_path):
    qc = tmp_path / "qc_figures"
    qc.mkdir()
    (qc / "A_qc_overview.json").write_text("{}", encoding="utf-8")
    (qc / "B_qc_overview.json").write_text("{}", encoding="utf-8")
    with pytest.raises(RuntimeError, match="multiple QC overview"):
        _read_qc_stats(tmp_path)

    (tmp_path / "cluster_summary_leiden_r0.5.csv").write_text("cluster,n_cells\n0,2\n")
    (tmp_path / "cluster_summary_leiden_r1.0.csv").write_text("cluster,n_cells\n0,2\n")
    with pytest.raises(RuntimeError, match="multiple primary cluster summaries"):
        _find_cluster_tables(tmp_path)

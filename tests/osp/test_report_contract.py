from pathlib import Path

import pytest

from osp.report import _find_cluster_tables, _fmt, _read_qc_stats, generate_report


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


def test_fmt_keeps_counts_readable_and_pvalues_scientific():
    assert _fmt(12345.0) == "12345"
    assert _fmt(23456.78) == "23456.8"
    assert _fmt(0.123456) == "0.1235"
    assert _fmt(1.5e-7) == "1.500e-07"
    assert _fmt(float("nan")) == ""
    assert _fmt("<b>") == "&lt;b&gt;"


def _minimal_outdir(root: Path, extra_summary_lines=""):
    (root / "qc_summary.csv").write_text(
        ",0\nsample,S1\nn_cells,1234\npct_low_quality,12.3456789\n" + extra_summary_lines,
        encoding="utf-8",
    )
    (root / "cluster_summary_leiden_r1.0.csv").write_text("leiden_r1.0,n_cells\n0,700\n1,534\n", encoding="utf-8")
    (root / "de_top_genes_leiden_r1.0.csv").write_text(
        "group,names,scores,logfoldchanges,pvals,pvals_adj,pct1,pct2\n"
        "0,GeneA,5.0,2.5,1e-9,2e-8,0.9,0.1\n"
        "1,GeneB,4.0,2.0,1e-7,3e-6,0.8,0.2\n",
        encoding="utf-8",
    )


def test_generate_report_accepts_path_objects_and_formats_summary_numbers(tmp_path):
    _minimal_outdir(tmp_path)
    out = generate_report(tmp_path)
    html = Path(out).read_text(encoding="utf-8")
    assert out == str(tmp_path / "report.html")
    assert "<td>n_cells</td><td>1234</td>" in html
    assert "<td>pct_low_quality</td><td>12.35</td>" in html
    assert "1. QC summary" in html
    assert "2. Clusters" in html
    assert "3. Cluster Identities" in html
    assert 'lang="en"' in html
    assert "degenerate" not in html


def test_generate_report_warns_about_a_degenerate_decontx_fit(tmp_path):
    _minimal_outdir(tmp_path, "decontx_z_source,leiden_fallback\ndecontx_degenerate,True\n")
    (tmp_path / "decontx_top_genes_leiden_r1.0.csv").write_text(
        "cluster,gene,contam_counts,contam_fraction_of_gene_counts,pct_of_total_contamination\n0,GeneA,10,0.5,100\n",
        encoding="utf-8",
    )
    html = Path(generate_report(tmp_path)).read_text(encoding="utf-8")
    assert 'class="warn"' in html
    assert "flagged as degenerate" in html
    assert "coarse Leiden clustering" in html

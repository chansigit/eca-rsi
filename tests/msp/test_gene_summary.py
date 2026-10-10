"""#57: check_genes and check_qc_scores answer without loading the expression matrix. The summary written beside
integrated.h5ad gives gene_table's exact text, and the stage tools use it only for the integrated.h5ad it names."""
import shutil

import anndata as ad
import numpy as np
import pandas as pd
import pytest
import scipy.sparse as sp

from msp.api import gene_summary, gene_table, gene_table_summary, qc_table


def data(seed=0):
    rng = np.random.default_rng(seed)
    n, genes = 240, ["COL2A1", "Acan", "SOX9", "PTPRC", "MT-CO1", "Gapdh"]
    X = rng.choice([0, 0, 0, .25, .5, 1.5, 2.75], size=(n, len(genes))).astype(np.float32)  # sums exact in float32
    obs = pd.DataFrame({"msp_leiden_r1.0": pd.Categorical(rng.choice(["0", "1", "2", "10"], n)),
                        "msp_leiden_r2.0": pd.Categorical(rng.choice(["0", "1", "2", "3", "5,10", "5,2"], n)),
                        "sample": pd.Categorical(rng.choice(["a", "b"], n)), "pct_counts_mt": rng.random(n),
                        "n_genes_by_counts": rng.integers(200, 4000, n)}, index=[f"c{i}" for i in range(n)])
    out = ad.AnnData(sp.csr_matrix(X), obs=obs, var=pd.DataFrame(index=genes))
    out.uns["msp"] = {"batch_col": "sample"}
    return out


QUERIES = [(["col2a1", "ACAN", "Sox9"], None), (["PTPRC", "nope"], ["10", "2"]), (["nothing", "here"], None),
           (["Gapdh"], ["7"]), (["MT-CO1", "COL2A1", "col2a1"], ["1"]), (["Acan"], [])]


@pytest.mark.parametrize("key", ["msp_leiden_r1.0", "msp_leiden_r2.0"])
def test_the_summary_gives_gene_tables_exact_text(key):
    a = data()
    summary = gene_summary(a, ["msp_leiden_r1.0", "msp_leiden_r2.0"])
    for genes, clusters in QUERIES:
        assert gene_table_summary(summary, genes, key, clusters) == gene_table(a, genes, key, clusters), (genes, clusters)


def test_the_stage_tools_use_the_summary_only_for_the_file_it_names(tmp_path, monkeypatch):
    from ecarsi.files import reference
    from ecarsi.stages.common import GENE_SUMMARY, gene_answer, qc_answer, save_gene_summary
    a = data(1)
    a.write_h5ad(tmp_path / "integrated.h5ad")
    save_gene_summary(a, ["msp_leiden_r2.0"], tmp_path)
    bundle = {"files": {name: reference(tmp_path / name) for name in ("integrated.h5ad", GENE_SUMMARY)}}
    never = lambda b: pytest.fail("the matrix was loaded although the summary describes it")
    expected = gene_table(a, ["COL2A1", "Acan"], "msp_leiden_r2.0", ["5,2"])
    assert gene_answer(bundle, ["COL2A1", "Acan"], "msp_leiden_r2.0", ["5,2"], never) == expected
    # the same file under another path (a restored case, #65) still matches: by digest
    moved = tmp_path / "moved"
    moved.mkdir()
    shutil.copy(tmp_path / "integrated.h5ad", moved / "integrated.h5ad")
    relocated = {"files": {"integrated.h5ad": reference(moved / "integrated.h5ad"), GENE_SUMMARY: bundle["files"][GENE_SUMMARY]}}
    assert gene_answer(relocated, ["COL2A1", "Acan"], "msp_leiden_r2.0", ["5,2"], never) == expected
    # an evidence version that rewrote integrated.h5ad but inherited its parent's summary falls back to the matrix
    other = tmp_path / "v1"
    other.mkdir()
    a.obs["note"] = "rewritten"
    a.write_h5ad(other / "integrated.h5ad")
    stale = {"files": {"integrated.h5ad": reference(other / "integrated.h5ad"), GENE_SUMMARY: bundle["files"][GENE_SUMMARY]}}
    loads = []
    load = lambda b: loads.append(b) or ad.read_h5ad(b["files"]["integrated.h5ad"]["path"])
    assert gene_answer(stale, ["COL2A1"], "msp_leiden_r2.0", None, load) == gene_table(a, ["COL2A1"], "msp_leiden_r2.0")
    assert loads == [stale]
    assert gene_answer(bundle, ["COL2A1"], "msp_leiden_r1.0", None, load) == gene_table(a, ["COL2A1"], "msp_leiden_r1.0")
    assert qc_answer(bundle, "msp_leiden_r2.0") == qc_table(a, "msp_leiden_r2.0", "sample")

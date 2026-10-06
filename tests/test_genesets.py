"""The gene sets the kernels share (decision 0020): one mitochondrial rule and one stress panel for every species."""
import csv

import numpy as np
import pandas as pd
import anndata as ad

from genesets import PANEL_FILE, is_mito, is_stress, stress_panel


def test_mitochondrial_genes_are_recognised_in_every_naming():
    named = {"human": "MT-CO1", "mouse": "mt-Co1", "rat Ensembl": "Mt-co1", "rat NCBI": "mt-Co1", "rhesus": "COX1",
             "cynomolgus": "ND4L", "mouse lemur": "CYTB", "fruit fly": "mt:CoI", "worm": "nduo-1"}
    assert [species for species, name in named.items() if not is_mito(name)] == []
    assert not any(is_mito(n) for n in ("PTGS1", "MTOR", "MT1X", "MT2A", "MTND1P23", "ATP6V1A", "ND", "COX4I1"))


def test_the_panel_matches_every_species_by_symbol_or_ensembl_id():
    assert len(stress_panel()) == 35 and stress_panel()[:4] == ("JUN", "JUNB", "JUND", "FOS")
    assert is_stress("FOS") and is_stress("Fos") and is_stress("fosab")  # human, mouse, zebrafish
    assert is_stress("ENSMFAG00000052456")  # cynomolgus JUNB, which Ensembl leaves without a symbol
    assert is_stress("H3F3B") is False and is_stress("HSPB1") is False  # out by the rule (the reason is in the table)
    assert not any(is_stress(g) for g in ("DCN", "LMNA", "SERPINE1", "MT2A", "ACTB", "KLF2", "dnajb6b"))


def test_every_considered_gene_carries_its_decision_and_reason():
    with open(PANEL_FILE) as f:
        rows = list(csv.DictReader((line for line in f if not line.startswith("#")), delimiter="\t"))
    assert {r["decision"] for r in rows} == {"panel", "excluded", "control"}
    assert all(r["reason"] for r in rows) and len({r["gene"] for r in rows}) == len(rows)
    panel = [r for r in rows if r["decision"] == "panel"]
    assert all(float(r["human_r"]) >= 0.15 and float(r["mouse_r"]) >= 0.15 for r in panel)
    assert all(r["gene"] in r["match"].split("|") for r in panel)


def test_osp_measures_the_mitochondrial_fraction_of_a_macaque_sample():
    from osp.qc import qc_one_sample
    names = ["ND1", "COX1", "ACTB", "GAPDH", "FOS", "JUNB"]
    counts = np.array([[5, 5, 40, 30, 1, 0], [0, 10, 40, 30, 0, 1], [10, 0, 40, 30, 2, 2]], dtype=float)
    sample = ad.AnnData(counts, obs=pd.DataFrame({"sample": ["A"] * 3}), var=pd.DataFrame(index=names))
    result, summary = qc_one_sample(sample, run_scrublet=False, run_decontx=False, make_plots=False)
    assert summary["n_mito_genes"] == 2 and summary["n_dissociation_genes"] == 2
    np.testing.assert_allclose(result.obs["pct_counts_mt"], 100 * counts[:, :2].sum(1) / counts.sum(1), rtol=1e-6)
    unknown = ad.AnnData(counts, obs=sample.obs, var=pd.DataFrame(index=[f"G{i}" for i in range(6)]))
    assert qc_one_sample(unknown, run_scrublet=False, run_decontx=False, run_dissociation_score=False,
                         make_plots=False)[1]["n_mito_genes"] == 0


def test_release_lists_samples_without_a_recognised_mitochondrial_gene(tmp_path):
    from ecarsi.files import reference, save
    from ecarsi.stages.release import review_items
    for sample, n in (("a", "13"), ("b", "0")):
        save(tmp_path / f"{sample}.json", dict(sample=sample, validation=dict(qc_summary=dict(n_mito_genes=n))))
    save(tmp_path / "per-sample.json", dict(samples=[reference(tmp_path / "a.json"), reference(tmp_path / "b.json")]))
    unit = dict(per_sample=reference(tmp_path / "per-sample.json"), rounds=[], forced_release=False)
    items = review_items(unit, pd.DataFrame(columns=["round", "release_stage", "reason", "cell_uid"]), [])
    assert [(i.kind, i.scope) for i in items] == [("upstream_review", "b")]
    assert "no mitochondrial gene" in items[0].note

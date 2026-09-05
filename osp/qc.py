"""
Per-sample QC for a single scRNA-seq sample.

Three-layer QC logic (hard thresholds + MAD outliers + Scrublet) plus DecontX
ambient-RNA monitoring. Call it on one sample's AnnData (or a view) at a time
— convenient for per-sample review and for iterating on thresholds in a
notebook. No species/tissue assumption: mt/ribo/hb gene detection is
case-insensitive by default, so both mouse (mt-, Rps6, Hba-a1) and human
(MT-, RPS6, HBA1) naming work; a species preset or custom prefix/regex can
also be passed. Malat1 (a nuclear-retention / cell-integrity indicator — low
expression often means a damaged cell or ambient-dominated droplet) is matched
case-insensitively too, producing obs["pct_counts_malat1"]; if the gene is
absent from the panel it is silently skipped. A "dissociation stress" score
obs["dissociation_score"] is computed with sc.tl.score_genes over
DISSOCIATION_GENES_HS (case-insensitive match, replaceable via
dissociation_genes=) — high scores suggest the cell/sample is dominated by
the stress response induced by tissue dissociation itself.

Design notes:
  - The input may be a view of a larger AnnData (e.g.
    adata[adata.obs['sample']=='FO']); the function .copy()'s internally, so
    the caller's object is never modified and no view-write warnings occur.
  - MAD outliers are computed *within* the sample — the caller must pass data
    from a single sample/batch, otherwise cross-batch systematic differences
    contaminate the MAD (which is why QC runs per sample in the first place).
  - Returns (annotated_adata, summary_dict); summary_dict keys match the
    qc_summary_per_sample.csv columns so multi-sample results concatenate.
  - Plotting is on by default: PNGs go to figdir (default ./qc_figs/), no
    interactive windows.

Usage:
    from osp import qc_one_sample

    ad_fo = adata[adata.obs["sample"] == "FO"]
    ad_fo_qc, summary = qc_one_sample(ad_fo, sample_label="FO", figdir="qc_figs/FO")

    # loop over samples, then concatenate summaries into one table
    rows = []
    for s in adata.obs["sample"].unique():
        _, summ = qc_one_sample(adata[adata.obs["sample"] == s], sample_label=s, make_plots=False)
        rows.append(summ)
    pd.DataFrame(rows).set_index("sample")
"""

import glob
import os
import re

import matplotlib
import numpy as np
import pandas as pd
import scanpy as sc
import scipy.sparse as sp

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from ._io import atomic_write_dataframe_csv, atomic_write_json


def _mad_outlier(x, nmads):
    x = np.asarray(x, dtype=float)
    med = np.median(x)
    mad = np.median(np.abs(x - med))
    if mad == 0:
        return np.zeros(len(x), dtype=bool)
    return np.abs(x - med) > nmads * mad


def _mad_bounds(x, nmads):
    """The keep-range (lo, hi) implied by _mad_outlier: median ± nmads*MAD.
    Only for reporting/plotting — the flag itself comes from _mad_outlier."""
    x = np.asarray(x, dtype=float)
    med = np.median(x)
    mad = np.median(np.abs(x - med))
    return med - nmads * mad, med + nmads * mad


# Scrublet's automatic threshold is only as good as the bimodality of its
# simulated-doublet score histogram, so the summary also reports the score
# distribution and the share of cells above this fixed reference value.
DOUBLET_SCORE_REFERENCE = 0.25


def _doublet_summary(ad):
    """Scrublet threshold plus doublet_score quantiles for the QC summary.

    ``scrublet_threshold`` is ``None`` when scanpy could not find a threshold
    (every predicted_doublet is then False regardless of the scores).
    """
    scores = ad.obs["doublet_score"].to_numpy(dtype=float)
    threshold = ad.uns.get("scrublet", {}).get("threshold")
    return {
        "scrublet_threshold": None if threshold is None else float(threshold),
        "median_doublet_score": float(np.median(scores)),
        "p90_doublet_score": float(np.quantile(scores, 0.9)),
        "p99_doublet_score": float(np.quantile(scores, 0.99)),
        f"pct_doublet_score_above_{DOUBLET_SCORE_REFERENCE:g}": float(100 * (scores > DOUBLET_SCORE_REFERENCE).mean()),
    }


def _safe_expm1(value):
    """Inverse log1p for JSON summaries; ``None`` denotes an open bound."""
    if value > np.log(np.finfo(float).max):
        return None
    result = float(np.expm1(value))
    return round(result, 1) if np.isfinite(result) else None


def _natural_key(s):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", str(s))]


def cluster_order(labels):
    """Order cluster labels numerically when they all parse as numbers —
    Leiden labels are strings, so plain sorted() gives lexicographic
    0,1,10,11,...,2. Composite labels that don't parse as float (e.g.
    subcluster ids like "5,0") get a natural sort on their digit runs;
    plain lexicographic is the last resort."""
    labels = list(dict.fromkeys(labels))
    try:
        return sorted(labels, key=float)
    except (TypeError, ValueError):
        pass
    try:
        return sorted(labels, key=_natural_key)
    except TypeError:
        return sorted(labels, key=str)


def decontx_top_genes(ad, cluster_key=None, top_n=20, counts_layer="counts", decontx_layer="decontX_counts"):
    """DecontX's per-cell contamination score alone is not enough — internally
    it is derived from "how many of this gene's UMIs in this cell are judged
    ambient RNA", and the difference raw - decontX_counts IS the per-gene,
    per-cell contamination amount. This function aggregates it into a ranking
    of *which genes* are contaminating, instead of one aggregate score.

    Returns (global_df, per_cluster_df):
      global_df: the top_n most contaminating genes sample-wide (ungrouped).
      per_cluster_df: computed only when cluster_key is given — re-ranked
        top_n within each group. The per-cluster difference for the same gene
        is usually the clue to the contamination source (e.g. erythrocyte
        genes popping up in non-erythroid clusters means ambient RNA, not
        actual expression). None when cluster_key is None.
    """
    if decontx_layer not in ad.layers:
        raise ValueError(f"{decontx_layer!r} not in ad.layers — run qc_one_sample(..., run_decontx=True) first")
    if isinstance(top_n, bool) or not isinstance(top_n, (int, np.integer)) or top_n < 1:
        raise ValueError(f"top_n must be a positive integer, got {top_n!r}")
    if cluster_key is not None and cluster_key not in ad.obs:
        raise ValueError(f"cluster_key {cluster_key!r} is not present in ad.obs")

    raw = sp.csr_matrix(ad.layers.get(counts_layer, ad.X))
    decon = sp.csr_matrix(ad.layers[decontx_layer])
    if raw.shape != decon.shape:
        raise ValueError(f"raw and DecontX matrices must have the same shape, got {raw.shape} and {decon.shape}")
    removed = raw - decon
    removed.data = np.clip(removed.data, 0, None)  # guard against float noise going slightly negative
    removed.eliminate_zeros()

    def _rank(mask=None):
        r = removed if mask is None else removed[mask]
        x = raw if mask is None else raw[mask]
        gene_removed = np.asarray(r.sum(axis=0)).ravel()
        gene_raw = np.asarray(x.sum(axis=0)).ravel()
        total = gene_removed.sum()
        df = pd.DataFrame(
            {
                "gene": ad.var_names,
                "contam_counts": gene_removed,
                "contam_fraction_of_gene_counts": gene_removed / np.maximum(gene_raw, 1),
                "pct_of_total_contamination": 100 * gene_removed / max(total, 1),
            }
        )
        return df.sort_values("contam_counts", ascending=False).head(top_n).reset_index(drop=True)

    global_df = _rank()

    per_cluster_df = None
    if cluster_key is not None:
        groups = ad.obs[cluster_key].astype(str)
        rows = []
        for cl in cluster_order(groups):
            d = _rank(mask=(groups == cl).values)
            d.insert(0, "cluster", cl)
            rows.append(d)
        per_cluster_df = pd.concat(rows, ignore_index=True)

    return global_df, per_cluster_df


def _decontx_degenerate(obs):
    """Fingerprint of an unusable DecontX fit; return a reason or ``None``.

    OSP always supplies an explicit coarse Leiden ``z`` because the vendored
    implementation requires one. The remaining checks catch fits whose EM
    solution is still pinned near complete contamination. They deliberately
    do not reject a large number of supplied clusters: unlike the historical
    internal DBSCAN initializer, an explicit ``z`` may legitimately be fine.
    """
    c = obs["decontX_contamination"]
    if not np.isfinite(c.to_numpy(dtype=float)).all():
        return "non-finite contamination estimates"
    med = float(c.median())
    if med > 0.5:
        return f"median contamination {med:.2f} > 0.5"
    frac_pinned = float((c > 0.95).mean())
    if frac_pinned > 0.25:
        return f"{frac_pinned:.0%} of cells pinned at contamination > 0.95"
    z_counts = obs["decontX_clusters"].value_counts()
    if len(z_counts) and z_counts.iloc[0] / len(obs) > 0.5:
        top_med = float(c[obs["decontX_clusters"] == z_counts.index[0]].median())
        if top_med > 0.9:
            return (
                f"largest internal cluster holds {z_counts.iloc[0] / len(obs):.0%} "
                f"of cells with median contamination {top_med:.2f}"
            )
    return None


def _coarse_clusters_for_decontx(ad, n_top_genes=2000, n_pcs=30, resolution=1.0):
    """Quick Leiden labels to hand DecontX as an explicit ``z``.

    Deliberately finer-grained than "broad cell types": the degenerate
    direction for DecontX is a z that lumps the dominant lineage into one
    mega-cluster (its profile then matches the ambient profile and the split
    is unidentifiable), so err on the side of more clusters. No scaling step —
    a coarse partition doesn't need it and it keeps this cheap.
    """
    if ad.n_obs < 3 or ad.n_vars < 2:
        raise ValueError(
            f"DecontX needs at least 3 cells and 2 genes to derive a coarse clustering, got "
            f"{ad.shape}; pass run_decontx=False or supply decontx_kwargs={{'z': ...}}"
        )
    tmp = sc.AnnData(ad.X.copy(), var=pd.DataFrame(index=ad.var_names))
    sc.pp.normalize_total(tmp, target_sum=1e4)
    sc.pp.log1p(tmp)
    sc.pp.highly_variable_genes(tmp, n_top_genes=n_top_genes, flavor="seurat")
    if tmp.var["highly_variable"].sum() >= 2:
        tmp = tmp[:, tmp.var["highly_variable"]].copy()
    sc.pp.pca(tmp, n_comps=min(n_pcs, tmp.n_vars - 1, tmp.n_obs - 1), random_state=0)
    sc.pp.neighbors(tmp, n_neighbors=min(15, tmp.n_obs - 1), random_state=0)
    sc.tl.leiden(tmp, resolution=resolution, key_added="z", flavor="igraph", n_iterations=2)
    z = tmp.obs["z"].cat.codes.to_numpy()
    if len(np.unique(z)) < 2:
        raise ValueError(
            "the coarse Leiden clustering for DecontX found a single cluster, so the "
            "ambient profile cannot be separated; pass run_decontx=False or supply "
            "decontx_kwargs={'z': ...} with at least two groups"
        )
    return z


# mt/ribo/hb gene names differ only in case between species (mouse
# mt-/Rps6/Hba-a1, human MT-/RPS6/HBA1); the matches below all use case=False
# so both work without this table. The species parameter is an entry point for
# callers who want to be explicit or align with another convention — an
# unknown species name raises instead of silently applying the wrong regex.
SPECIES_GENE_PATTERNS = {
    "mouse": {"mt_prefix": "mt-", "ribo_regex": r"^Rp[sl]\d", "hb_regex": r"^Hb[ab]"},
    "human": {"mt_prefix": "MT-", "ribo_regex": r"^RP[SL]\d", "hb_regex": r"^HB[AB]"},
}


# "Dissociation stress" gene panel — early-response/stress genes induced by
# the tissue dissociation protocol itself (heat shock, immediate-early genes,
# AP-1 family, ...), not by the biology under study. High expression flags
# cells/samples where dissociation artifact may be dominating the signal,
# independent of the mt%/doublet/ambient-RNA axes already covered above.
# Human gene symbols; matched case-insensitively against var_names so mouse
# data (same symbols, different case convention) works too — genes not
# present in a given panel are simply skipped, no guessing/expanding.
# fmt: off
DISSOCIATION_GENES_HS = [
    "ACTG1", "ANKRD1", "ARID5A", "ATF3", "ATF4", "BAG3", "BHLHE40",
    "CCNL1", "CCRN4L", "CEBPB", "CEBPD", "CEBPG", "CSRNP1", "CXCL1", "CYR61",
    "DCN", "DDX3X", "DDX5", "DES", "DNAJA1", "DNAJB1", "DNAJB4", "DUSP1", "DUSP8",
    "EGR1", "EGR2", "EIF1", "EIF5", "ERF", "ERRFI1", "FAM132B", "FOS", "FOSB",
    "FOSL2", "GADD45A", "GADD45G", "BRD2", "BTG1", "BTG2", "GCC1", "GEM",
    "H3F3B", "HIPK3", "HSP90AA1", "HSP90AB1", "HSPA1A", "HSPA1B", "HSPA5",
    "HSPA8", "HSPB1", "HSPE1", "HSPH1", "ID3", "IDI1", "IER2", "IER3", "IER5",
    "IFRD1", "IL6", "IRF1", "IRF8", "ITPKC", "JUN", "JUNB", "JUND", "KCNE4",
    "KLF2", "KLF4", "KLF6", "KLF9", "LITAF", "LMNA", "MAFF", "MAFK", "MCL1",
    "MIDN", "MIR22HG", "MT1", "MT2", "MYADM", "MYC", "MYD88", "NCKAP5L",
    "NCOA7", "NFKBIA", "NFKBIZ", "NOP58", "NPPC", "NR4A1", "ODC1", "OSGIN1",
    "OXNAD1", "PCF11", "PDE4B", "PER1", "PHLDA1", "PNP", "PNRC1", "PPP1CC",
    "PPP1R15A", "PXDC1", "RAP1B", "RASSF1", "RHOB", "RHOH", "RIPK1", "SAT1",
    "SBNO2", "SDC4", "SERPINE1", "SKIL", "SLC10A6", "SLC38A2", "SLC41A1",
    "SOCS3", "SQSTM1", "SRF", "SRSF5", "SRSF7", "STAT3", "TAGLN2", "TIPARP",
    "TNFAIP3", "TNFAIP6", "TPM3", "TPPP3", "TRA2A", "TRA2B", "TRIB1", "TUBB4B",
    "TUBB6", "UBC", "USP2", "WAC", "ZC3H12A", "ZFAND5", "ZFP36", "ZFP36L1",
    "ZFP36L2", "ZYX",
]
# fmt: on


def assert_single_sample(adata, sample_col="sample"):
    """Assert the adata holds exactly one sample (OSP's input contract)."""
    if adata.n_obs == 0:
        raise ValueError("expected a non-empty single-sample AnnData, got 0 cells")
    if sample_col not in adata.obs.columns:
        return
    labels = adata.obs[sample_col]
    n_missing = int(labels.isna().sum())
    if n_missing:
        raise ValueError(f"expected every cell to have obs[{sample_col!r}], got {n_missing} missing value(s)")
    unique = list(pd.unique(labels))
    if len(unique) != 1:
        raise ValueError(f"expected a single sample, got {len(unique)}: {unique}")


def qc_one_sample(
    adata,
    sample_label=None,
    sample_col="sample",
    counts_layer="counts",
    species=None,
    mt_prefix="mt-",
    ribo_regex=r"^Rp[sl]\d",
    hb_regex=r"^Hb[ab]",
    nmads=5,
    mt_nmads=3,
    mt_soft_pct=8.0,
    hard_min_genes=200,
    hard_min_counts=500,
    hard_max_mt_pct=15.0,
    run_scrublet=True,
    run_decontx=True,
    decontx_kwargs=None,
    run_dissociation_score=True,
    dissociation_genes=None,
    make_plots=True,
    figdir=None,
):
    """Run routine QC on a single sample's adata (or view); returns
    (annotated_copy, summary_dict).

    If `counts_layer` (default "counts") exists in layers, X is swapped for
    that layer first — all QC assumes X holds raw counts, and this corrects
    inputs whose X was already normalized (same convention as
    cluster_and_deg).

    Four QC flags, OR-combined (hitting any of them marks a cell low-quality):
      1. Hard thresholds: n_genes < hard_min_genes / total_counts <
         hard_min_counts / pct_counts_mt > hard_max_mt_pct
      2. Within-sample MAD outliers (default nmads=5): any of
         log1p_total_counts / log1p_n_genes_by_counts /
         pct_counts_in_top_20_genes exceeding median ± nmads * MAD
      3. MT% MAD outlier (default mt_nmads=3) AND pct_counts_mt > mt_soft_pct
         (so MAD doesn't fire on noise when MT% is uniformly low)
      4. With run_scrublet=True, Scrublet doublet detection (run within the
         single sample; no batch_key needed). Its predicted_doublet call rests
         on an automatic threshold that is unreliable when the simulated score
         histogram is not bimodal, so the summary also records the threshold
         (``scrublet_threshold``, ``None`` when scanpy found none) and the
         doublet_score median / p90 / p99 and the share of cells above
         DOUBLET_SCORE_REFERENCE; trust the scores over ``n_doublet``.

    With run_decontx=True, DecontX (osp._decontx, a vendored pure-Python port
    of DecontX) runs additionally for ambient-RNA monitoring — monitoring/reporting only
    (writes obs["decontX_contamination"] / obs["decontX_clusters"] /
    layers["decontX_counts"] and median_contamination in the summary). It
    does NOT feed the low_quality call: contamination is continuous, and
    whether to filter on it is a dataset-specific decision that shouldn't be
    made here on the caller's behalf.

    OSP supplies an explicit coarse Leiden clustering as DecontX's ``z`` by
    default. For compatibility with existing summaries this path is recorded
    as ``decontx_z_source="leiden_fallback"`` even though it is now the normal
    path; a caller-provided ``z`` is recorded as ``"user"``. If the fit is
    still pinned near complete contamination, ``ad.uns["osp_decontx_degenerate"]
    = True`` is set, the summary carries ``decontx_degenerate=True`` (so the
    report and the annotation agent can see it), and cluster_and_deg drops
    the contamination column from its PCA covariates. The estimates remain
    available for inspection rather than being silently discarded.

    Per-cell decontX_contamination is only an aggregate score; "who is
    contaminated by which genes" comes from the difference between raw counts
    and decontX_counts (the decontaminated matrix) — see decontx_top_genes(),
    which runs automatically and stores ad.uns["decontx_top_genes"] (global
    ranking) and ad.uns["decontx_top_genes_by_cluster"] (ranked within
    DecontX's own rough clusters), with companion CSVs + a bar chart.

    mt_prefix/ribo_regex/hb_regex match case-insensitively, so the defaults
    fit both mouse and human naming; for other species or custom panels pass
    your own prefix/regex, or species=... to use a SPECIES_GENE_PATTERNS
    preset (unknown names raise).

    With run_dissociation_score=True, obs["dissociation_score"] is computed —
    a stress/immediate-early gene panel induced by tissue dissociation itself
    (DISSOCIATION_GENES_HS, human symbols, case-insensitive match; replace
    via dissociation_genes=). Scoring uses sc.tl.score_genes (control-gene
    binning, robust to sequencing depth) on a throwaway normalize+log1p copy,
    leaving the raw counts in ad.X untouched (Scrublet/DecontX below need
    them). High scores suggest the cell/sample is dominated by the
    dissociation stress response rather than real biology.
    """
    assert_single_sample(adata, sample_col=sample_col)
    numeric_parameters = {
        "nmads": nmads,
        "mt_nmads": mt_nmads,
        "mt_soft_pct": mt_soft_pct,
        "hard_min_genes": hard_min_genes,
        "hard_min_counts": hard_min_counts,
        "hard_max_mt_pct": hard_max_mt_pct,
    }
    numeric_types = (int, float, np.integer, np.floating)
    invalid_parameters = [
        name
        for name, value in numeric_parameters.items()
        if (isinstance(value, bool) or not isinstance(value, numeric_types) or not np.isfinite(value) or value < 0)
    ]
    if invalid_parameters:
        raise ValueError("QC thresholds must be finite non-negative numbers; invalid: " + ", ".join(invalid_parameters))

    if species is not None:
        if species not in SPECIES_GENE_PATTERNS:
            raise ValueError(
                f"unknown species {species!r}, known: {list(SPECIES_GENE_PATTERNS)}. pass mt_prefix/ribo_regex/hb_regex directly instead."
            )
        mt_prefix, ribo_regex, hb_regex = (
            SPECIES_GENE_PATTERNS[species]["mt_prefix"],
            SPECIES_GENE_PATTERNS[species]["ribo_regex"],
            SPECIES_GENE_PATTERNS[species]["hb_regex"],
        )

    ad = adata.copy()
    if ad.n_vars == 0:
        raise ValueError("expected at least one gene, got an AnnData with 0 variables")
    # All of QC must run on raw counts (calculate_qc_metrics absolute values,
    # Scrublet, DecontX, and the score_genes normalization all assume counts);
    # when the input's X is already normalized with raw counts kept in a layer
    # (common in released h5ad files), swap them back in.
    if counts_layer in ad.layers:
        ad.X = ad.layers[counts_layer].copy()
    count_values = ad.X.data if sp.issparse(ad.X) else np.asarray(ad.X)
    if not np.isfinite(count_values).all() or (count_values < 0).any():
        raise ValueError("QC input counts must contain only finite, non-negative values")

    if sample_label is None:
        sample_label = "sample"

    ad.var_names_make_unique()
    ad.var["mt"] = ad.var_names.str.match(f"^{re.escape(mt_prefix)}", case=False)
    ad.var["ribo"] = ad.var_names.str.match(ribo_regex, case=False)
    ad.var["hb"] = ad.var_names.str.match(hb_regex, case=False)
    qc_vars = ["mt", "ribo", "hb"]
    # Malat1 is a nuclear-retained lncRNA commonly used as a cell/nucleus
    # integrity indicator (low Malat1 ~ damaged cell / ambient-dominated
    # droplet); case-insensitive exact match so it works for mouse (Malat1)
    # and human (MALAT1) alike. If the gene isn't in this panel at all, skip
    # it silently rather than erroring — it's an optional extra signal.
    ad.var["malat1"] = ad.var_names.str.upper() == "MALAT1"
    if ad.var["malat1"].any():
        qc_vars.append("malat1")
    top_n = min(20, ad.n_vars)
    sc.pp.calculate_qc_metrics(ad, qc_vars=qc_vars, percent_top=[top_n], log1p=True, inplace=True)
    if top_n != 20:
        # Preserve OSP's public obs/summary schema for targeted panels with
        # fewer than 20 genes: "top 20" then means every available gene.
        ad.obs.rename(
            columns={f"pct_counts_in_top_{top_n}_genes": "pct_counts_in_top_20_genes"},
            inplace=True,
        )

    if run_dissociation_score:
        diss_genes = dissociation_genes if dissociation_genes is not None else DISSOCIATION_GENES_HS
        diss_set = {g.upper() for g in diss_genes}
        diss_found = ad.var_names[ad.var_names.str.upper().isin(diss_set)].tolist()
        if diss_found:
            # score_genes needs log-normalized expression; run it on a throwaway
            # X-only copy (not ad.copy() — that would duplicate layers too) so
            # ad.X stays raw counts for scrublet/decontx below.
            tmp = sc.AnnData(ad.X.copy(), var=pd.DataFrame(index=ad.var_names))
            sc.pp.normalize_total(tmp, target_sum=1e4)
            sc.pp.log1p(tmp)
            sc.tl.score_genes(tmp, gene_list=diss_found, score_name="dissociation_score", use_raw=False)
            ad.obs["dissociation_score"] = tmp.obs["dissociation_score"].values
            del tmp

    obs = ad.obs
    # kept as three separate flags (not just the OR) so the summary can say
    # WHICH metric fired — on pathological samples one of them can dominate
    # (Liu-2025 FO: pct_top20 alone flagged 69% of all low-quality calls)
    mad_counts_flag = _mad_outlier(obs["log1p_total_counts"], nmads)
    mad_genes_flag = _mad_outlier(obs["log1p_n_genes_by_counts"], nmads)
    mad_top20_flag = _mad_outlier(obs["pct_counts_in_top_20_genes"], nmads)
    mad_flag = mad_counts_flag | mad_genes_flag | mad_top20_flag
    mt_mad_flag = _mad_outlier(obs["pct_counts_mt"], mt_nmads) & (obs["pct_counts_mt"] > mt_soft_pct)
    hard_flag = (
        (obs["n_genes_by_counts"] < hard_min_genes)
        | (obs["total_counts"] < hard_min_counts)
        | (obs["pct_counts_mt"] > hard_max_mt_pct)
    ).values

    if run_scrublet:
        sc.pp.scrublet(ad)
        doublet_flag = ad.obs["predicted_doublet"].astype(bool).values
    else:
        doublet_flag = np.zeros(ad.n_obs, dtype=bool)

    decontx_z_source = None
    if run_decontx:
        from . import _decontx

        ad.uns.pop("osp_decontx_degenerate", None)
        dkw = dict(decontx_kwargs or {})
        dkw.setdefault("seed", 0)
        dkw.setdefault("verbose", False)
        if "z" in dkw:
            decontx_z_source = "user"
            res = _decontx.decontx(ad, **dkw)
        else:
            # The explicit-Leiden path is OSP's standard initialization.
            decontx_z_source = "leiden_fallback"
            z = _coarse_clusters_for_decontx(ad)
            res = _decontx.decontx(ad, z=z, **dkw)
        # decontx() only writes into the AnnData with copy=True; fold the
        # returned DecontXResult in ourselves (same fields as its copy branch)
        ad.obs["decontX_contamination"] = res.contamination
        ad.obs["decontX_clusters"] = pd.Categorical(res.z)
        ad.layers["decontX_counts"] = res.decontx_counts.T.tocsr()
        still = _decontx_degenerate(ad.obs)
        if still is not None:
            # Contamination estimates are untrustworthy even with supplied
            # labels. Keep the values for review, but prevent the downstream
            # PCA from treating this failed fit as a biological signal.
            print(
                f"== decontX degenerate ({still}); flagging uns['osp_decontx_degenerate']",
                flush=True,
            )
            ad.uns["osp_decontx_degenerate"] = True
        ad.uns["decontx_top_genes"], ad.uns["decontx_top_genes_by_cluster"] = decontx_top_genes(
            ad, cluster_key="decontX_clusters"
        )

    low_quality = hard_flag | mad_flag | mt_mad_flag | doublet_flag

    reason = np.array([""] * ad.n_obs, dtype=object)
    for flag, tag in [
        (hard_flag, "hard_threshold"),
        (mad_flag, "mad_outlier"),
        (mt_mad_flag, "high_mito"),
        (doublet_flag, "doublet"),
    ]:
        reason[flag] = np.where(reason[flag] == "", tag, reason[flag] + "+" + tag)
    reason[reason == ""] = "pass"

    ad.obs["qc_hard_fail"] = hard_flag
    ad.obs["qc_mad_outlier"] = mad_flag
    ad.obs["qc_mt_outlier"] = mt_mad_flag
    ad.obs["qc_doublet"] = doublet_flag
    ad.obs["low_quality"] = low_quality
    ad.obs["qc_reason"] = reason

    obs = ad.obs  # re-bind: scrublet/decontx may have replaced the obs frame
    summary = {
        "sample": sample_label,
        "n_cells": ad.n_obs,
        "n_low_quality": int(low_quality.sum()),
        "pct_low_quality": float(100 * low_quality.mean()),
        "n_hard_fail": int(hard_flag.sum()),
        "n_mad_outlier": int(mad_flag.sum()),
        "n_mad_counts": int(mad_counts_flag.sum()),
        "n_mad_genes": int(mad_genes_flag.sum()),
        "n_mad_pct_top20": int(mad_top20_flag.sum()),
        "n_high_mito": int(mt_mad_flag.sum()),
        "n_doublet": int(doublet_flag.sum()),
        "median_counts": float(np.median(obs["total_counts"])),
        "median_genes": float(np.median(obs["n_genes_by_counts"])),
        "median_pct_mt": float(np.median(obs["pct_counts_mt"])),
        "median_pct_top20": float(np.median(obs["pct_counts_in_top_20_genes"])),
    }
    if run_scrublet:
        # n_doublet depends on Scrublet's automatic threshold; the score
        # distribution is the more trustworthy signal, so report both.
        summary.update(_doublet_summary(ad))
    if run_decontx:
        summary["median_contamination"] = float(obs["decontX_contamination"].median())
        top_genes = ad.uns["decontx_top_genes"]
        summary["top_contam_gene"] = top_genes.iloc[0]["gene"] if len(top_genes) else None
        summary["decontx_z_source"] = decontx_z_source
        summary["decontx_degenerate"] = bool(ad.uns.get("osp_decontx_degenerate", False))
    if "pct_counts_malat1" in obs:
        summary["median_pct_counts_malat1"] = float(obs["pct_counts_malat1"].median())
    if "dissociation_score" in obs:
        summary["median_dissociation_score"] = float(obs["dissociation_score"].median())

    resolved_figdir = figdir or ("qc_figs" if make_plots else None)
    if resolved_figdir is not None:
        _remove_stale_sample_qc_outputs(resolved_figdir, sample_label)
    if make_plots:
        figdir = resolved_figdir
        os.makedirs(figdir, exist_ok=True)
        _plot_sample_qc(ad, sample_label, hard_min_genes, hard_min_counts, hard_max_mt_pct, figdir, nmads=nmads)
        if run_decontx:
            _plot_decontx_top_genes(ad, sample_label, figdir)

    return ad, summary


def _remove_stale_sample_qc_outputs(figdir, sample_label):
    """Remove only this sample's generated QC files before a rerun.

    A shared ``figdir`` may contain other samples, so the escaped sample
    prefix is part of every pattern. This also removes optional DecontX files
    when a rerun disables DecontX, preventing a stale report section.
    """
    prefix = glob.escape(str(sample_label))
    patterns = (
        f"{prefix}_qc_*.png",
        f"{prefix}_qc_overview.json",
        f"{prefix}_decontx_top_genes*",
    )
    for pattern in patterns:
        for path in glob.glob(os.path.join(figdir, pattern)):
            if os.path.isfile(path):
                os.unlink(path)


def _annotate_threshold(ax, x, label, y_frac=0.92):
    """Write the threshold value and failing-cell count right next to the
    threshold line, so an agent doesn't have to eyeball pixel positions."""
    ax.text(
        x,
        y_frac,
        f" {label}",
        transform=ax.get_xaxis_transform(),
        color="red",
        fontsize=9,
        va="top",
        ha="left",
        rotation=0,
        bbox={"boxstyle": "round", "fc": "white", "ec": "red", "alpha": 0.85, "pad": 0.2},
    )


_QC_PLOT_FIGSIZE = (6, 4.5)  # every individual QC plot uses this, for consistent sizing
_QC_PLOT_DPI = 150


def _new_qc_ax():
    fig, ax = plt.subplots(figsize=_QC_PLOT_FIGSIZE)
    return fig, ax


def _finish_qc_plot(fig, ax, figdir, sample_label, suffix):
    """No scanpy/matplotlib multi_panel figures here — one file per plot, so
    a human/agent can read a single metric off one image instead of having
    to parse a combined figure. Every plot goes through this so they all
    share the same figsize/dpi."""
    ax.grid(True, which="both", linestyle=":", linewidth=0.4, alpha=0.5)
    fig.tight_layout()
    fig.savefig(os.path.join(figdir, f"{sample_label}_qc_{suffix}.png"), dpi=_QC_PLOT_DPI)
    plt.close(fig)


def _log_bins(values, n=80):
    """Positive, strictly increasing bins for a logarithmic count axis."""
    values = np.asarray(values, dtype=float)
    positive = values[np.isfinite(values) & (values > 0)]
    if not len(positive):
        return np.geomspace(0.5, 1.5, n)
    lo = max(float(positive.min()), 0.5)
    hi = max(float(positive.max()), lo * 1.01)
    return np.geomspace(lo, hi, n)


def _qc_hist(
    obs,
    col,
    color,
    sample_label,
    figdir,
    bins=80,
    logx=False,
    logy=True,
    threshold=None,
    thresh_label=None,
    title=None,
    suffix=None,
):
    fig, ax = _new_qc_ax()
    ax.hist(obs[col], bins=bins, color=color)
    if threshold is not None:
        ax.axvline(threshold, color="red", linestyle="--")
        _annotate_threshold(ax, threshold, thresh_label)
    if logx:
        ax.set_xscale("log")
    if logy:
        ax.set_yscale("log")
    ax.set_xlabel(col)
    ax.set_title(title or col)
    _finish_qc_plot(fig, ax, figdir, sample_label, suffix or col)


def _plot_sample_qc(ad, sample_label, min_genes, min_counts, max_mt_pct, figdir, nmads=5):
    obs = ad.obs
    n = ad.n_obs
    n_low = int(obs["low_quality"].sum())
    has_contam = "decontX_contamination" in obs
    has_malat1 = "pct_counts_malat1" in obs
    has_diss = "dissociation_score" in obs

    n_below_counts = int((obs["total_counts"] < min_counts).sum())
    _qc_hist(
        obs,
        "total_counts",
        "#4C72B0",
        sample_label,
        figdir,
        bins=_log_bins(obs["total_counts"]),
        logx=True,
        threshold=min_counts,
        thresh_label=f"min={min_counts:g}\nn_fail={n_below_counts}",
        title=f"total_counts | median={obs['total_counts'].median():.0f}",
    )

    n_below_genes = int((obs["n_genes_by_counts"] < min_genes).sum())
    _qc_hist(
        obs,
        "n_genes_by_counts",
        "#55A868",
        sample_label,
        figdir,
        bins=_log_bins(obs["n_genes_by_counts"]),
        logx=True,
        threshold=min_genes,
        thresh_label=f"min={min_genes:g}\nn_fail={n_below_genes}",
        title=f"n_genes_by_counts | median={obs['n_genes_by_counts'].median():.0f}",
    )

    n_above_mt = int((obs["pct_counts_mt"] > max_mt_pct).sum())
    _qc_hist(
        obs,
        "pct_counts_mt",
        "#C44E52",
        sample_label,
        figdir,
        threshold=max_mt_pct,
        thresh_label=f"max={max_mt_pct:g}\nn_fail={n_above_mt}",
        title=f"pct_counts_mt | median={obs['pct_counts_mt'].median():.2f}%",
    )

    # pct_top20 is the third metric behind the MAD-outlier rule but has no
    # hard threshold, so unlike counts/genes/mt it gets no line "for free" —
    # draw the MAD keep-range upper bound instead. Low-complexity cell types
    # (neutrophils, erythroid) sit naturally high here, so when this line
    # flags a lot of cells it deserves a manual look, not blind trust.
    top20 = obs["pct_counts_in_top_20_genes"]
    _, top20_hi = _mad_bounds(top20, nmads)
    n_above_top20 = int((top20 > top20_hi).sum())
    _qc_hist(
        obs,
        "pct_counts_in_top_20_genes",
        "#DD8452",
        sample_label,
        figdir,
        suffix="pct_top20",
        threshold=top20_hi,
        thresh_label=f"MAD hi={top20_hi:.1f}\nn_fail={n_above_top20}",
        title=f"pct_counts_in_top_20_genes | median={top20.median():.1f}%",
    )

    # Encode low_quality with both color AND marker shape, so an agent that
    # can only rely on color doesn't misread the plot
    low = obs["low_quality"]
    fig, ax = _new_qc_ax()
    ax.scatter(
        obs.loc[~low, "total_counts"],
        obs.loc[~low, "pct_counts_mt"],
        c="#4C72B0",
        s=3,
        alpha=0.4,
        marker="o",
        label="pass",
    )
    ax.scatter(
        obs.loc[low, "total_counts"],
        obs.loc[low, "pct_counts_mt"],
        c="#C44E52",
        s=10,
        alpha=0.9,
        marker="x",
        label="low_quality",
    )
    ax.set_xscale("log")
    ax.set_xlabel("total_counts (log)")
    ax.set_ylabel("pct_counts_mt")
    ax.legend(loc="upper right", fontsize=8, framealpha=0.9)
    ax.set_title(f"counts vs mt% | n_low_quality={n_low} ({100 * n_low / n:.2f}%)")
    _finish_qc_plot(fig, ax, figdir, sample_label, "counts_vs_mt")

    if has_contam:
        _qc_hist(
            obs,
            "decontX_contamination",
            "#8172B2",
            sample_label,
            figdir,
            suffix="decontx_contamination",
            title=f"decontX_contamination | median={obs['decontX_contamination'].median():.3f}",
        )

    if has_malat1:
        _qc_hist(
            obs,
            "pct_counts_malat1",
            "#937860",
            sample_label,
            figdir,
            title=f"pct_counts_malat1 | median={obs['pct_counts_malat1'].median():.2f}%",
        )

    if has_diss:
        _qc_hist(
            obs,
            "dissociation_score",
            "#64B5CD",
            sample_label,
            figdir,
            logy=False,
            title=f"dissociation_score | median={obs['dissociation_score'].median():.3f}",
        )

    # Every key number readable off the plots also lands in a JSON file: an
    # agent reads this to verify/cite exact values instead of estimating from
    # pixels. JSON is structured data, not an image, so the one-plot-per-file
    # rule doesn't apply to it.
    stats = {
        "sample": sample_label,
        "n_cells": n,
        "n_low_quality": n_low,
        "pct_low_quality": round(100 * n_low / n, 4),
        "thresholds": {
            "min_total_counts": min_counts,
            "min_n_genes": min_genes,
            "max_pct_mt": max_mt_pct,
        },
        **({"doublet": _doublet_summary(ad)} if "doublet_score" in obs else {}),
        "n_fail_min_counts": n_below_counts,
        "n_fail_min_genes": n_below_genes,
        "n_fail_max_mt": n_above_mt,
        "median_total_counts": float(obs["total_counts"].median()),
        "median_n_genes": float(obs["n_genes_by_counts"].median()),
        "median_pct_mt": float(obs["pct_counts_mt"].median()),
        "median_pct_top20": float(top20.median()),
        # the adaptive (±nmads MAD) keep-ranges in ORIGINAL units, plus how
        # many cells each metric flags — the counterpart of "thresholds" for
        # the MAD layer. On healthy samples these ranges are generous; a
        # suspiciously tight range (or one metric dominating the fail counts)
        # means the sample's distribution shape is breaking the MAD assumption.
        "mad_nmads": nmads,
        "mad_keep_ranges": {
            "total_counts": [_safe_expm1(b) for b in _mad_bounds(obs["log1p_total_counts"], nmads)],
            "n_genes_by_counts": [_safe_expm1(b) for b in _mad_bounds(obs["log1p_n_genes_by_counts"], nmads)],
            "pct_counts_in_top_20_genes": [round(float(b), 2) for b in _mad_bounds(top20, nmads)],
        },
        "n_fail_mad": {
            "total_counts": int(_mad_outlier(obs["log1p_total_counts"], nmads).sum()),
            "n_genes_by_counts": int(_mad_outlier(obs["log1p_n_genes_by_counts"], nmads).sum()),
            "pct_counts_in_top_20_genes": int(_mad_outlier(top20, nmads).sum()),
        },
    }
    if has_contam:
        stats["median_decontx_contamination"] = float(obs["decontX_contamination"].median())
    if has_malat1:
        stats["median_pct_counts_malat1"] = float(obs["pct_counts_malat1"].median())
    if has_diss:
        stats["median_dissociation_score"] = float(obs["dissociation_score"].median())
    atomic_write_json(os.path.join(figdir, f"{sample_label}_qc_overview.json"), stats)


def _plot_decontx_top_genes(ad, sample_label, figdir, top_n_plot=15):
    """Which genes specifically contaminate this sample — bar chart plus
    companion CSVs (global ranking + ranked within DecontX's own rough
    clusters), rather than one aggregate score."""
    global_df = ad.uns.get("decontx_top_genes")
    per_cluster_df = ad.uns.get("decontx_top_genes_by_cluster")
    if global_df is None or len(global_df) == 0:
        return

    plot_df = global_df.head(top_n_plot).iloc[::-1]  # reverse so #1 ends up on top of barh
    fig, ax = plt.subplots(figsize=(7, max(3, 0.35 * len(plot_df))))
    ax.barh(plot_df["gene"], plot_df["contam_counts"], color="#C44E52")
    ax.set_xlabel("total contaminating UMIs (raw - decontX_counts, summed over cells)")
    ax.set_title(f"{sample_label}: top {len(plot_df)} contaminating genes (decontX)")
    for y, (_, row) in enumerate(plot_df.iterrows()):
        ax.text(
            row["contam_counts"],
            y,
            f" {row['pct_of_total_contamination']:.1f}% of total contam.",
            va="center",
            fontsize=8,
        )
    fig.tight_layout()
    fig.savefig(os.path.join(figdir, f"{sample_label}_decontx_top_genes.png"), dpi=150)
    plt.close(fig)
    atomic_write_dataframe_csv(
        global_df,
        os.path.join(figdir, f"{sample_label}_decontx_top_genes.csv"),
        index=False,
    )
    if per_cluster_df is not None:
        # "decontx_cluster" (not just "cluster") to disambiguate from the
        # real Leiden clusters computed later in cluster_and_deg — this file
        # uses the coarse Leiden labels supplied to DecontX.
        atomic_write_dataframe_csv(
            per_cluster_df,
            os.path.join(figdir, f"{sample_label}_decontx_top_genes_by_decontx_cluster.csv"),
            index=False,
        )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Run per-sample QC on one sample — quick trial runs / threshold tuning only"
    )
    parser.add_argument("h5ad_path")
    parser.add_argument("--sample-col", default="sample")
    parser.add_argument("--sample", required=True, help="sample name to run on its own")
    parser.add_argument("--figdir", default="qc_figs")
    parser.add_argument("--no-scrublet", action="store_true")
    parser.add_argument("--no-decontx", action="store_true")
    args = parser.parse_args()

    adata = sc.read_h5ad(args.h5ad_path, backed="r")
    try:
        if args.sample_col not in adata.obs:
            raise ValueError(f"sample column {args.sample_col!r} is not present in the input obs")
        mask = adata.obs[args.sample_col].astype(str) == args.sample
        if not mask.any():
            raise ValueError(f"sample {args.sample!r} matches no cells in obs[{args.sample_col!r}]")
        sub = adata[mask].to_memory()
    finally:
        adata.file.close()
    _, summary = qc_one_sample(
        sub,
        sample_label=args.sample,
        sample_col=args.sample_col,
        run_scrublet=not args.no_scrublet,
        run_decontx=not args.no_decontx,
        figdir=args.figdir,
    )
    print(pd.Series(summary))

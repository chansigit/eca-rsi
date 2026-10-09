"""Evidence the agents query: precomputed DEG tables, live DEG, per-cluster
expression / QC / stability views, and the removal mask that keeps every
live test consistent with the precomputed CSVs.

Everything here is read-only over an msp output directory plus the loaded
``integrated.h5ad``; nothing writes back. ``msp.inspect`` and ``msp.annotate``
build their agent tools on top of these functions, and ``zmip`` reuses them
for its per-lineage sessions.
"""

from __future__ import annotations

import csv
import logging
import os

import numpy as np
import pandas as pd
import scipy.sparse as sp

log = logging.getLogger(__name__)

QC_COLS = (
    "pct_counts_mt",
    "n_genes_by_counts",
    "total_counts",
    "doublet_score",
    "decontX_contamination",
    "dissociation_score",
    "pct_counts_malat1",
)


def load_removal_mask(outdir, ad):
    """Cells already proposed for removal before Cluster Annotations ran
    (msp.integrate._build_removal_mask / the Pre-annotation filtering
    UMAP) — excluded from every live DEG test here too (check_deg,
    subcluster's built-in sibling DE), matching the precomputed
    deg_global_*/deg_local_* CSVs exactly. Missing file (older msp output,
    predates this artifact) → nothing excluded. Boolean numpy array,
    aligned to ad.obs_names order."""
    path = os.path.join(outdir, "preannotation_removal.csv")
    if not os.path.exists(path):
        return np.zeros(ad.n_obs, dtype=bool)
    # Cell IDs are opaque strings: preserve leading zeros and literal "NA" IDs.
    df = pd.read_csv(path, dtype={"cell": str}, keep_default_na=False).set_index("cell")
    # Nullable boolean: cells absent from the file are not flagged, without object-dtype downcasting.
    flags = df["recommend_removal"].astype("boolean").reindex(ad.obs_names).fillna(False)
    return flags.to_numpy(dtype=bool)


def cluster_order(labels):
    """Distinct labels in a stable, numeric-aware order.

    Subcluster IDs are comma-joined ("5,10"); every comma-separated part is
    compared as a number, so "5,2" precedes "5,10". Labels that are not
    numeric throughout fall back to plain string order."""
    seen = list(dict.fromkeys(labels))  # first-seen order, any iterable
    try:
        return sorted(seen, key=lambda x: tuple(float(p) for p in str(x).split(",")))
    except ValueError:
        return sorted(seen)


def gene_table(ad, genes, cluster_key, cluster_ids=None):
    """Mean log-normalized expression and expressing fraction per cluster
    for the given genes (case-insensitive symbol match).

    cluster_ids optionally selects comparison clusters, preserving their order;
    omitted or empty retains the complete-table behavior for Python callers.
    """
    upper = {g.upper(): g for g in ad.var_names}
    found = {q: upper[q.upper()] for q in genes if q.upper() in upper}
    missing = [q for q in genes if q.upper() not in upper]
    if not found:
        return f"none of these genes are in var_names: {genes}"
    idx = [ad.var_names.get_loc(g) for g in found.values()]
    X = ad.X[:, idx]
    X = X.toarray() if sp.issparse(X) else np.asarray(X)
    cl = ad.obs[cluster_key].astype(str)
    cols = {}
    selected = cluster_order(cl) if not cluster_ids else list(dict.fromkeys(map(str, cluster_ids)))
    unknown = sorted(set(selected) - set(cl))
    if unknown:
        return f"unknown cluster IDs: {unknown}; available: {cluster_order(cl)}"
    for c in selected:
        m = (cl == c).values
        mean = X[m].mean(axis=0)
        pct = 100 * (X[m] > 0).mean(axis=0)
        cols[c] = [f"{mn:.2f}|{p:.0f}%" for mn, p in zip(mean, pct, strict=True)]
    df = pd.DataFrame(cols, index=list(found.values()))
    out = "mean lognorm expr | pct expressing, per cluster:\n" + df.to_string()
    if missing:
        out += f"\nnot found in var_names: {missing}"
    return out


def gene_summary(ad, keys):
    """What gene_table reads, for every gene and every cluster of each key: per-cluster expression sums (float64)
    and counts of expressing cells. Computed once where integrated.h5ad is written, so check_genes answers from a
    table of a few MB instead of loading the whole matrix for each call (#57: the matrix of a 400k-cell parse-5M
    dataset outgrew the tool's memory). Arrays only, for numpy.savez."""
    X = ad.X if sp.issparse(ad.X) else sp.csr_matrix(ad.X)
    X = X.tocsr()
    out = {"var_names": np.asarray(ad.var_names, dtype=str), "keys": np.asarray(keys, dtype=str)}
    for i, key in enumerate(keys):
        labels = ad.obs[key].astype(str).to_numpy()
        clusters = cluster_order(labels)
        sums = np.zeros((len(clusters), ad.n_vars))
        positive = np.zeros((len(clusters), ad.n_vars), dtype=np.int32)
        sizes = np.zeros(len(clusters), dtype=np.int64)
        for j, c in enumerate(clusters):
            rows = X[np.flatnonzero(labels == c)]
            sums[j] = np.asarray(rows.sum(axis=0, dtype=np.float64)).ravel()
            positive[j] = np.bincount(rows.indices[rows.data > 0], minlength=ad.n_vars)
            sizes[j] = rows.shape[0]
        out.update({f"{i}.clusters": np.asarray(clusters, dtype=str), f"{i}.n": sizes, f"{i}.sum": sums,
                    f"{i}.positive": positive})
    return out


def gene_table_summary(summary, genes, cluster_key, cluster_ids=None):
    """gene_table answered from gene_summary: the same lookup, messages and layout. The means are summed in
    float64 rather than gene_table's float32, so a two-decimal mean can differ from it in the last digit."""
    var_names = [str(g) for g in summary["var_names"]]
    upper = {g.upper(): g for g in var_names}
    found = {q: upper[q.upper()] for q in genes if q.upper() in upper}
    missing = [q for q in genes if q.upper() not in upper]
    if not found:
        return f"none of these genes are in var_names: {genes}"
    i = [str(k) for k in summary["keys"]].index(cluster_key)
    clusters = [str(c) for c in summary[f"{i}.clusters"]]
    position = {g: k for k, g in enumerate(var_names)}
    idx = [position[g] for g in found.values()]
    selected = clusters if not cluster_ids else list(dict.fromkeys(map(str, cluster_ids)))
    unknown = sorted(set(selected) - set(clusters))
    if unknown:
        return f"unknown cluster IDs: {unknown}; available: {clusters}"
    sums, positive, sizes = summary[f"{i}.sum"], summary[f"{i}.positive"], summary[f"{i}.n"]
    cols = {}
    for c in selected:
        j = clusters.index(c)
        mean = sums[j, idx] / sizes[j]
        pct = 100 * (positive[j, idx] / sizes[j])
        cols[c] = [f"{mn:.2f}|{p:.0f}%" for mn, p in zip(mean, pct, strict=True)]
    df = pd.DataFrame(cols, index=list(found.values()))
    out = "mean lognorm expr | pct expressing, per cluster:\n" + df.to_string()
    if missing:
        out += f"\nnot found in var_names: {missing}"
    return out


def qc_table(ad, cluster_key, batch_col):
    """Per-cluster QC (median|p90) + composition: n_samples, dominant-sample
    share, inherited flag/drop fractions — tests (b) and (c) in one view."""
    cols = [c for c in QC_COLS if c in ad.obs]
    cl = ad.obs[cluster_key].astype(str)
    rows = {}
    for c in cluster_order(cl):
        m = (cl == c).values
        sub = ad.obs.loc[m]
        row = [
            int(m.sum()),
            int(sub[batch_col].nunique()),
            f"{sub[batch_col].value_counts(normalize=True).iloc[0]:.2f}",
        ]
        if "_qc_action" in ad.obs:
            act = sub["_qc_action"].dropna().astype(str)
            row += [f"{(act == 'flag').mean():.2f}", f"{(act == 'drop').mean():.2f}"]
        row += [f"{sub[col].median():.3g}|{sub[col].quantile(0.9):.3g}" for col in cols]
        rows[c] = row
    index = ["n_cells", "n_samples", "max_sample_share"]
    if "_qc_action" in ad.obs:
        index += ["frac_flag_inherited", "frac_drop_inherited"]
    index += cols
    df = pd.DataFrame(rows, index=index).T
    df.index.name = cluster_key
    return (
        "per-cluster QC (median|p90) and composition "
        "(frac_* among cells with inherited QC; nan means unavailable):\n" + df.to_string()
    )


def load_paga_neighbors(outdir, key):
    """{cluster: [top-3 PAGA neighbours]} as integrate wrote them (paga_neighbors_<key>.csv);
    {} when absent or empty (including headerless files from older outputs)."""
    path = os.path.join(outdir, f"paga_neighbors_{key}.csv")
    if not os.path.exists(path):
        return {}
    try:
        df = pd.read_csv(path, dtype=str)
    except pd.errors.EmptyDataError:
        return {}
    nb = {}
    for c, g in df.groupby("cluster", sort=False):
        nb[str(c)] = list(g.sort_values("rank", key=lambda s: s.astype(int))["neighbor"].astype(str))
    return nb


# ---------------------------------------------------------------- live DEG


def deg_frame(ad, cluster_key, cluster, ref_groups, remove_mask):
    """Live wilcoxon for one cluster of the CURRENT working clustering — the
    full ranked table (all genes, scanpy's natural score order), or None when
    the cluster has no cells left once remove_mask is excluded. ref_groups is
    "rest" (one-vs-rest, deg_global_* semantics) or a tuple of other cluster
    ids pooled (deg_local_* semantics). remove_mask cells (recommend_removal,
    see Pre-annotation filtering) are excluded first, same as the precomputed
    CSVs."""
    import scanpy as sc  # here, not at the top: the DEG lookup tools import this module and need no scanpy

    from .deg_logging import rank_genes_groups

    base = ad[~remove_mask]
    lab = base.obs[cluster_key].astype(str)
    if cluster not in set(lab):
        return None
    sub = base if ref_groups == "rest" else base[lab.isin([cluster, *ref_groups])].copy()
    rank_genes_groups(sub, cluster_key, groups=[cluster], reference="rest", method="wilcoxon", use_raw=False, pts=True)
    df = sc.get.rank_genes_groups_df(sub, group=cluster)
    # natural scanpy ranking (by test score), not resorted by raw logFC —
    # sorting by logFC alone surfaces near-zero-expression noise genes with
    # huge fold change but pct1==pct2==0 and padj==1, same trap as anywhere
    # else in msp that reads rank_genes_groups_df
    return df.rename(columns={"pct_nz_group": "pct1", "pct_nz_reference": "pct2"}).reset_index(drop=True)


def filter_deg(df, min_logfc=None, max_padj=None, min_pct1=None, max_pct2=None):
    """Row filter shared by every DEG surface; None/0/1 defaults mean no filter."""
    m = np.ones(len(df), dtype=bool)
    if min_logfc:
        m &= df["logfoldchanges"].to_numpy() >= float(min_logfc)
    if max_padj is not None and 0 < float(max_padj) < 1:
        m &= df["pvals_adj"].to_numpy() <= float(max_padj)
    if min_pct1:
        m &= df["pct1"].to_numpy() >= float(min_pct1)
    if max_pct2 is not None and 0 < float(max_pct2) < 1:
        m &= df["pct2"].to_numpy() <= float(max_pct2)
    return df[m]


def format_deg(cluster, ref_desc, df, n_total=None, filters=""):
    """One line per gene, terse — gene logFC padj pct1/pct2 — so a 20-gene
    answer costs ~200 tokens, not 600; the header carries the comparison,
    the filters applied and how many genes passed."""
    head = f"DEG cluster {cluster} vs {ref_desc}"
    if filters:
        head += f" [{filters}]"
    head += f": {len(df)} gene(s)" + (f" of {n_total} passing" if n_total is not None and n_total != len(df) else "")
    head += " (gene logFC padj pct1/pct2, by wilcoxon score):"
    body = ", ".join(
        f"{r.names} {r.logfoldchanges:.1f} {r.pvals_adj:.0e} {r.pct1:.2f}/{r.pct2:.2f}"
        for r in df.itertuples(index=False)
    )
    return head + "\n  " + (body if len(df) else "(none)")


def parse_reference(reference, clusters=None):
    """Parse the existing string API without splitting an exact subcluster ID.

    CSV quoting disambiguates pooled IDs containing commas: '"5,0","5,1"'.
    Without a cluster vocabulary, retain the legacy comma-list interpretation.
    """
    reference = str(reference or "rest").strip() or "rest"
    if reference == "rest":
        return "rest"
    known = None if clusters is None else {str(c) for c in clusters}
    if known is not None and reference in known:
        return (reference,)
    if known is not None and '"' not in reference:
        parts = [p.strip() for p in reference.split(",")]
        if any(",".join(parts[i:j]) in known for i in range(len(parts)) for j in range(i + 2, len(parts) + 1)):
            raise ValueError('ambiguous reference; CSV-quote each ID, e.g. \'"5,0","5,1"\'')
    try:
        groups = tuple(
            sorted({g.strip() for g in next(csv.reader([reference], skipinitialspace=True, strict=True)) if g.strip()})
        )
    except csv.Error as exc:
        raise ValueError(f"invalid reference CSV: {exc}") from exc
    if not groups or (known is not None and any(g not in known for g in groups)):
        raise ValueError(
            'unknown reference cluster(s); use current IDs and CSV-quote IDs containing commas, e.g. \'"5,0","5,1"\''
        )
    return groups


from .deg_tables import DEG_SQL_DOC, DEG_TOOL_DOC, DegTables, _sql_name, filter_desc  # noqa: F401 - their home since #59


class DegCache:
    """check_deg for one agent session: every (clustering key, cluster,
    reference set) is computed at most once, and when the request is exactly
    what integrate already tabulated it is answered from disk instead of
    recomputed — one-vs-rest on an original key == deg_global_<key>.csv, and
    "vs its top-3 PAGA neighbours" == deg_local_<key>.csv, provided this
    session excludes exactly the cells those tables excluded (inspect always
    does; annotate/zmip only when inspect proposed no drops). A live
    one-vs-rest wilcoxon on 59k cells costs ~35 s, so a 40-cluster annotate
    session used to spend 5-10 min recomputing tables it could have read.
    Numbers are identical either way (same test, same exclusion, same
    matrix, same ranking). If too few cached rows pass the requested filters,
    compute the full table before claiming that fewer genes are available."""

    def __init__(self, ad, outdir, remove_mask, label="check_deg"):
        self.ad, self.outdir, self.mask, self.label = ad, outdir, np.asarray(remove_mask, dtype=bool), label
        self._memo = {}  # (key, cluster, ref) -> (df, complete)
        self._csv = {}  # (key, view) -> DataFrame | None
        self._paga = {}  # key -> {cluster: [neighbours]}
        self.tables_usable = bool(np.array_equal(load_removal_mask(outdir, ad), self.mask))
        self.n_computed = self.n_precomputed = self.n_memo = 0

    def _csv_rows(self, key, view, cluster):
        k = (key, view)
        if k not in self._csv:
            path = os.path.join(self.outdir, f"deg_{view}_{key}.csv")
            self._csv[k] = pd.read_csv(path, dtype={"group": str}) if os.path.exists(path) else None
        df = self._csv[k]
        if df is None or "group" not in df:
            return None
        sub = df[df["group"].astype(str) == cluster]
        return sub.reset_index(drop=True) if len(sub) else None

    def _precomputed(self, key, cluster, ref):
        if not self.tables_usable:
            return None
        if ref == "rest":
            return self._csv_rows(key, "global", cluster)
        if key not in self._paga:
            self._paga[key] = load_paga_neighbors(self.outdir, key)
        nbs = self._paga[key].get(cluster)
        if not nbs or set(nbs) != set(ref):
            return None
        return self._csv_rows(key, "local", cluster)

    def _compute(self, key, cluster, ref):
        return deg_frame(self.ad, key, cluster, ref, self.mask)

    def table(self, key, cluster, reference, top_n, min_logfc=None, max_padj=None, min_pct1=None, max_pct2=None):
        ref = parse_reference(reference, self.ad.obs[key].astype(str).unique())
        mk = (key, cluster, ref)
        hit = self._memo.get(mk)
        if hit is not None and (hit[1] or top_n <= len(hit[0])):
            df, source = hit[0], "memo"
            self.n_memo += 1
        else:
            df = self._precomputed(key, cluster, ref) if hit is None else None
            if df is not None and top_n <= len(df):
                self._memo[mk] = (df, False)
                source = "precomputed"
                self.n_precomputed += 1
            else:
                df = self._compute(key, cluster, ref)
                if df is None:
                    return f"cluster {cluster!r} has no cells left once recommend_removal cells are excluded"
                self._memo[mk] = (df, True)
                source = "computed"
                self.n_computed += 1
        ref_desc = "rest" if ref == "rest" else ",".join(ref)
        log.info(f"== [{self.label}] check_deg {cluster} vs {ref_desc}: {source}")
        kept = filter_deg(df, min_logfc, max_padj, min_pct1, max_pct2)
        if not self._memo[mk][1] and len(kept) < top_n:
            df = self._compute(key, cluster, ref)
            if df is None:
                return f"cluster {cluster!r} has no cells left once recommend_removal cells are excluded"
            self._memo[mk] = (df, True)
            self.n_computed += 1
            kept = filter_deg(df, min_logfc, max_padj, min_pct1, max_pct2)
            log.info(f"== [{self.label}] check_deg {cluster} vs {ref_desc}: computed after filtering")
        complete = self._memo[mk][1]
        text = format_deg(
            cluster,
            ref_desc,
            kept.head(top_n),
            len(kept) if complete else None,
            filter_desc(min_logfc, max_padj, min_pct1, max_pct2),
        )
        return text if complete else text + "\n(cached ranked prefix; more genes may pass)"


def palette(ad, col):
    """Assign the annotation palette for an observation column."""
    from .annotate import _palette

    return _palette(ad, col)


def plot_annotation(ad_full, ad_kept, figdir):
    """Render full and retained annotation views into a figure directory."""
    from .annotate import _plot

    return _plot(ad_full, ad_kept, figdir)

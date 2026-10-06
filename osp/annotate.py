"""osp.annotate — what eca-rsi's per-sample annotation session needs from osp: the system prompt and the evidence
it reads (_gene_table, _qc_table, the file inventory, _subcluster_once), and the host side of its proposal
(schema, _validate_proposal, _apply_proposal, _plot_annotation). The session runs in eca-rsi
(ecarsi/stages/persample.py, through osp.api); the standalone agent flow was removed with #28.

Applying a proposal writes obs["_ann_coarse"], obs["_ann_fine"] and obs["_qc_action"] (keep/flag/drop) and
renders the annotation UMAPs (coarse/fine) and a QC-action UMAP. QC actions are advice only, never applied to
filtering here; species and tissue context come from the caller, and the package assumes no cell-type knowledge.
"""

import glob
import operator
import os
from collections import Counter

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import scanpy as sc
import scipy.sparse as sp

from .cluster import (  # importing osp.cluster also selects the Agg backend
    _UMAP_AXES_RECT,
    _UMAP_DPI,
    _UMAP_FIGSIZE,
    QC_OVERLAY_COLS,
    _save_single_umap,
    _square_limits,
)
from .qc import cluster_order
import logging

log = logging.getLogger(__name__)

_OPS = {">": operator.gt, ">=": operator.ge, "<": operator.lt, "<=": operator.le}
_CONFIDENCE_VALUES = {"high", "medium", "low"}
_QC_ACTIONS = {"drop", "flag"}
_QC_REASONS = {
    "doublet",
    "ambient",
    "debris",
    "dissociation-stress",
    "low-quality",
    "other",
}


def _detect_primary_key(outdir):
    paths = sorted(glob.glob(os.path.join(outdir, "cluster_summary_leiden_r*.csv")))
    if not paths:
        raise FileNotFoundError(
            f"no cluster_summary_leiden_r*.csv in {outdir} — run run_one_sample_pipeline(outdir=...) first"
        )
    if len(paths) > 1:
        names = [os.path.basename(path) for path in paths]
        raise RuntimeError(
            f"multiple primary cluster summaries in {outdir}: {names}; rerun clustering to "
            "remove stale tables or pass cluster_key explicitly"
        )
    return os.path.basename(paths[0])[len("cluster_summary_") : -len(".csv")]


def _gene_table(ad, genes, cluster_key):
    """Per-cluster mean lognorm expression | pct expressing for each gene,
    matched case-insensitively against var_names."""
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
    for c in cluster_order(cl):
        m = (cl == c).values
        mean = X[m].mean(axis=0)
        pct = 100 * (X[m] > 0).mean(axis=0)
        cols[c] = [f"{mn:.2f}|{p:.0f}%" for mn, p in zip(mean, pct, strict=True)]
    df = pd.DataFrame(cols, index=list(found.values()))
    out = "mean lognorm expr | pct expressing, per cluster:\n" + df.to_string()
    if missing:
        out += f"\nnot found in var_names: {missing}"
    return out


def _qc_table(ad, cluster_key):
    """Per-cluster median|p90 of every available QC metric."""
    cols = [c for c in QC_OVERLAY_COLS if c in ad.obs]
    cl = ad.obs[cluster_key].astype(str)
    rows = {}
    for c in cluster_order(cl):
        sub = ad.obs.loc[(cl == c).values, cols]
        rows[c] = [f"{sub[col].median():.3g}|{sub[col].quantile(0.9):.3g}" for col in cols]
    df = pd.DataFrame(rows, index=cols).T
    df.insert(0, "n_cells", cl.value_counts()[df.index].values)
    df.index.name = cluster_key
    return "per-cluster QC (median|p90 of each metric):\n" + df.to_string()


def _file_inventory(outdir):
    """Relative paths of everything the agent needs, injected verbatim into
    the prompt — no Glob roundtrips, no guessing at nonexistent paths."""
    patterns = ["*.csv", "qc_figures/*.json", "qc_figures/*.png", "qc_figures/*.csv", "figures/*.png"]
    paths = []
    for pat in patterns:
        paths += sorted(os.path.relpath(p, outdir) for p in glob.glob(os.path.join(outdir, pat)))
    return "\n".join(f"- {p}" for p in paths)


def _subcluster_once(ad, key, cluster, resolution, new_key):
    """Split one cluster with leiden restrict_to. Writes obs[new_key] where
    the target cluster's cells get labels like "5,0"/"5,1" and every other
    cell keeps its previous label. Returns (n_subclusters, summary_text);
    n_subclusters == 0 means no split happened (column removed again)."""
    parent_mask = (ad.obs[key].astype(str) == cluster).values
    sc.tl.leiden(
        ad,
        restrict_to=(key, [cluster]),
        resolution=resolution,
        key_added=new_key,
        flavor="igraph",
        n_iterations=2,
    )
    sub_labels = ad.obs[new_key][parent_mask].astype(str)
    subs = cluster_order(sub_labels)
    if len(subs) < 2:
        del ad.obs[new_key]
        return 0, f"cluster {cluster} did not split at resolution {resolution}; try a higher resolution"

    # quick one-vs-rest wilcoxon among the new subclusters (within the parent
    # cells only) so the agent sees what distinguishes them without extra
    # roundtrips
    sub = ad[parent_mask].copy()
    sub.obs["_sub"] = pd.Categorical(sub_labels.values)
    sc.tl.rank_genes_groups(sub, "_sub", method="wilcoxon", use_raw=False)
    top = sc.get.rank_genes_groups_df(sub, group=None).groupby("group", observed=True).head(10)

    sizes = sub_labels.value_counts()
    lines = [f"cluster {cluster} split into {len(subs)} subclusters at resolution {resolution}:"]
    for s in subs:
        genes = ", ".join(top.loc[top["group"] == s, "names"])
        lines.append(f"  {s} (n={int(sizes[s])}) top genes vs siblings: {genes}")
    return len(subs), "\n".join(lines)


_PROPOSAL_SCHEMA_DOC = """{
  "clusters": [
    {"cluster": "<id>", "label_coarse": "<lineage-level label, e.g. 'Neutrophil' / 'B cell' / 'Stromal'>",
     "label_fine": "<fine-grained label, e.g. 'Immature neutrophil (myelocyte)'>",
     "confidence": "high|medium|low", "evidence_genes": ["..."],
     "doubts": "<open questions; empty string if none>"}
    // must cover EVERY cluster of the current clustering (incl. subcluster ids like "5,0")
  ],
  "qc_actions": [
    // standardized machine-appliable records; omit entries for clean clusters
    // scope "cluster": the whole cluster
    {"cluster": "<id>", "scope": "cluster", "action": "drop|flag",
     "reason": "doublet|ambient|debris|dissociation-stress|low-quality|other",
     "note": "<free text>"},
    // scope "cells": only cells of that cluster satisfying metric op value
    {"cluster": "<id>", "scope": "cells", "metric": "<numeric obs column, e.g. decontX_contamination>",
     "op": ">|>=|<|<=", "value": 0.6, "action": "drop|flag",
     "reason": "...", "note": "..."}
  ],
  "threshold_suggestions": ["<free-text suggestions for the pipeline's QC thresholds>"],
  "overall": "<overall QC assessment of the sample>"
}"""


def _validate_proposal(proposal, clusters, obs):
    """Validate untrusted model JSON without assuming any nested type."""
    problems = []
    if not isinstance(proposal, dict):
        return [f"proposal must be a JSON object, got {type(proposal).__name__}"]

    clusters = [str(cluster) for cluster in clusters]
    cluster_set = set(clusters)
    entries = proposal.get("clusters")
    if not isinstance(entries, list) or not entries:
        problems.append('missing "clusters" list')
    else:
        covered = []
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                problems.append(f"clusters[{index}] must be an object, got {type(entry).__name__}")
                continue
            missing = [
                key
                for key in (
                    "cluster",
                    "label_coarse",
                    "label_fine",
                    "confidence",
                    "evidence_genes",
                    "doubts",
                )
                if key not in entry
            ]
            if missing:
                problems.append(f"clusters[{index}] missing {missing}: {entry}")

            cluster = str(entry.get("cluster"))
            covered.append(cluster)
            for field in ("label_coarse", "label_fine"):
                value = entry.get(field)
                if not isinstance(value, str) or not value.strip():
                    problems.append(f"clusters[{index}].{field} must be a non-empty string")
            if entry.get("confidence") not in _CONFIDENCE_VALUES:
                problems.append(f"clusters[{index}].confidence must be high|medium|low: {entry.get('confidence')!r}")
            evidence = entry.get("evidence_genes")
            if not isinstance(evidence, list) or any(
                not isinstance(gene, str) or not gene.strip() for gene in evidence
            ):
                problems.append(f"clusters[{index}].evidence_genes must be a list of strings")
            if not isinstance(entry.get("doubts"), str):
                problems.append(f"clusters[{index}].doubts must be a string")

        duplicates = sorted(cluster for cluster, count in Counter(covered).items() if count > 1)
        if duplicates:
            problems.append(f"clusters annotated more than once: {duplicates}")
        covered_set = set(covered)
        missed = [cluster for cluster in clusters if cluster not in covered_set]
        if missed:
            problems.append(f"clusters without an annotation: {missed}")
        extra = sorted(covered_set - cluster_set)
        if extra:
            problems.append(f"annotations for unknown clusters: {extra}")

    actions = proposal.get("qc_actions", [])
    if not isinstance(actions, list):
        problems.append('"qc_actions" must be a list (may be empty)')
        actions = []
    for index, action in enumerate(actions):
        if not isinstance(action, dict):
            problems.append(f"qc_actions[{index}] must be an object, got {type(action).__name__}")
            continue
        missing = [key for key in ("cluster", "scope", "action", "reason", "note") if key not in action]
        if missing:
            problems.append(f"qc_actions[{index}] missing {missing}: {action}")
        if action.get("action") not in _QC_ACTIONS:
            problems.append(f'qc_action "action" must be drop|flag: {action}')
        if str(action.get("cluster")) not in cluster_set:
            problems.append(f"qc_action cluster {action.get('cluster')!r} is not a current cluster id: {action}")
        if action.get("reason") not in _QC_REASONS:
            problems.append(f"qc_action reason must be one of {sorted(_QC_REASONS)}: {action}")
        if not isinstance(action.get("note"), str):
            problems.append(f"qc_action note must be a string: {action}")
        scope = action.get("scope")
        if scope == "cells":
            metric = action.get("metric")
            if (
                not isinstance(metric, str)
                or metric not in obs.columns
                or not pd.api.types.is_numeric_dtype(obs[metric])
            ):
                problems.append(f'qc_action "metric" must be a numeric obs column: {action}')
            if action.get("op") not in _OPS:
                problems.append(f'qc_action "op" must be one of {sorted(_OPS)}: {action}')
            try:
                value = float(action.get("value"))
            except (TypeError, ValueError):
                problems.append(f'qc_action "value" must be numeric: {action}')
            else:
                if isinstance(action.get("value"), bool) or not np.isfinite(value):
                    problems.append(f'qc_action "value" must be finite and numeric: {action}')
        elif scope != "cluster":
            problems.append(f'qc_action "scope" must be cluster|cells: {action}')

    suggestions = proposal.get("threshold_suggestions", [])
    if not isinstance(suggestions, list) or any(not isinstance(item, str) for item in suggestions):
        problems.append('"threshold_suggestions" must be a list of strings')
    if "overall" in proposal and not isinstance(proposal["overall"], str):
        problems.append('"overall" must be a string')
    return problems


def _apply_proposal(ad, key, proposal):
    """Map the accepted proposal onto cells: obs["_ann_coarse"],
    obs["_ann_fine"], and obs["_qc_action"] in {keep, flag, drop} — flags
    applied first so drop wins where both match."""
    lab = ad.obs[key].astype(str)
    ad.obs["_ann_coarse"] = lab.map({str(e["cluster"]): e["label_coarse"] for e in proposal["clusters"]}).astype(
        "category"
    )
    ad.obs["_ann_fine"] = lab.map({str(e["cluster"]): e["label_fine"] for e in proposal["clusters"]}).astype("category")

    action = np.array(["keep"] * ad.n_obs, dtype=object)
    for verb in ("flag", "drop"):
        for a in proposal.get("qc_actions", []):
            if a["action"] != verb:
                continue
            mask = (lab == str(a["cluster"])).to_numpy(copy=True)  # pandas 3 CoW: .values is read-only
            if a["scope"] == "cells":
                mask &= _OPS[a["op"]](ad.obs[a["metric"]].to_numpy(dtype=float), float(a["value"]))
            action[mask] = verb
    ad.obs["_qc_action"] = pd.Categorical(action, categories=["keep", "flag", "drop"])


def _plot_annotation(ad, figdir):
    """Annotation UMAPs (coarse/fine) via the shared single-UMAP renderer,
    plus a custom QC-action UMAP: proposed-drop cells as large dark-red dots,
    flagged cells as large dark-yellow dots, the rest as small light-gray
    dots."""
    os.makedirs(figdir, exist_ok=True)
    for col, fname in (("_ann_coarse", "umap_ann_coarse.png"), ("_ann_fine", "umap_ann_fine.png")):
        _save_single_umap(ad, col, os.path.join(figdir, fname), legend_loc="on data", legend_fontsize=5)

    xy = np.asarray(ad.obsm["X_umap"])
    act = ad.obs["_qc_action"].astype(str).values
    # base point size adapts to cell count (same rule as _save_single_umap);
    # flagged/dropped cells are drawn at 1.5x base so they stand out without
    # dwarfing the embedding
    base = 120000 / ad.n_obs
    fig = plt.figure(figsize=_UMAP_FIGSIZE)
    ax = fig.add_axes(_UMAP_AXES_RECT)
    for name, color, size in (
        ("keep", "#d3d3d3", base),
        ("flag", "#b8860b", 1.5 * base),
        ("drop", "#8b0000", 1.5 * base),
    ):
        m = act == name
        if m.any():
            ax.scatter(xy[m, 0], xy[m, 1], s=size, c=color, linewidths=0, label=f"{name} (n={int(m.sum())})")
    xlim, ylim = _square_limits(xy)
    ax.set_xlim(*xlim)
    ax.set_ylim(*ylim)
    ax.set_aspect("equal", adjustable="box")
    ax.set_title("UMAP: proposed QC action")
    ax.legend(loc="upper right", fontsize=8, framealpha=0.9)
    fig.savefig(os.path.join(figdir, "umap_qc_action.png"), dpi=_UMAP_DPI)
    plt.close(fig)


def _system_prompt(outdir, cluster_key, clusters, species, tissue, language):
    context = []
    if species:
        context.append(f"species: {species}")
    if tissue:
        context.append(f"tissue: {tissue}")
    context_line = (
        ("Context — " + ", ".join(context))
        if context
        else (
            "No species/tissue context was provided — infer cautiously from gene-name "
            "casing conventions and expression profiles, and state that inference in your conclusions."
        )
    )
    has_decontx_heatmap = os.path.isfile(os.path.join(outdir, "figures", "decontx_heatmap_by_cluster.png"))
    required_contamination = ", and the decontx heatmap" if has_decontx_heatmap else ""

    return f"""You are a single-cell RNA-seq analysis expert. The current working directory is an \
OSP (one-sample-pipeline) output directory. Task: propose a cell-type annotation for every cluster \
({cluster_key}, {len(clusters)} clusters: {clusters}) and standardized QC actions.
{context_line}

All relevant files (paths relative to the working directory — Read exactly these paths; \
do not Glob and do not guess any other path):
{_file_inventory(outdir)}

What the files are:
- figures/umap_clusters_*.png, figures/paga.png: clustering structure
- figures/umap_qc_*.png, figures/qc_violin_*.png: QC metrics on the UMAP / as per-cluster violins
- figures/decontx_heatmap_by_cluster.png, decontx_top_genes_*.csv: ambient RNA when DecontX was run
- qc_figures/*_qc_*.png, qc_figures/*_qc_overview.json: sample-level QC histograms and key numbers
- de_top_genes_*.csv: top DE genes per cluster (pct1/pct2 = expressing fraction inside/outside the cluster)
- cluster_summary_*.csv, paga_connectivities_*.csv, qc_summary.csv (its n_doublet rests on Scrublet's \
automatic threshold; judge doublets from the doublet_score distribution and check_qc_scores instead)

Mandatory workflow:
1. Figures BEFORE conclusions: view at least umap_clusters, paga, every qc_violin_*{required_contamination}. \
Figures are more direct than tables — especially for judging whether a cluster \
is driven by doublets/contamination/dissociation stress.
2. Read the de_top_genes CSV and form an identity hypothesis per cluster.
3. Verify canonical markers with check_genes (top DE lists often miss canonical markers — active \
verification is required; iterate over multiple calls; distinguish similar subtypes with \
discriminative markers).
4. Check each cluster's QC profile with check_qc_scores to identify QC-driven rather than \
biology-driven clusters.
5. If a cluster looks heterogeneous (bimodal QC violins, mixed marker sets, spatially split on \
the UMAP), split it with the subcluster tool and annotate the resulting subclusters (ids like \
"5,0", "5,1"). check_genes / check_qc_scores automatically follow the refined clustering.
6. Finish by calling submit_annotation — conclusions only in the submitted JSON, not merely in a \
text reply.

Efficiency (keep the number of turns down):
- Reads can run in parallel: issue several Read calls in one turn (e.g. all qc_violin panels at once).
- Batch genes into check_genes: pass a whole hypothesis set (dozens of genes) in one call, not one gene per call.
- check_qc_scores takes no arguments and returns every metric for every cluster — one call is enough.
- Get the submit_annotation JSON right on the first try (format in the tool description) to avoid validation round-trips.

Principles:
- Output language: everything you submit (all text fields except gene symbols) and your final \
narrative must be written in {language}.
- qc_actions must use the standardized record format (scope "cluster" for whole clusters, scope \
"cells" with metric/op/value for per-cell criteria). "drop" proposes removal, "flag" requests \
human review. These are proposals only — nothing is auto-applied.
- When evidence is weak, use low confidence and state the doubts; do not force a guess.
- Distinguish "this gene is genuinely expressed in this cluster" from "this gene is ambient \
contamination here" — the decontx tables and heatmap exist exactly for that."""

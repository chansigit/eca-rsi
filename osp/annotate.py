"""
osp.annotate — interpret an OSP output directory with the selected harness
agent: propose per-cluster cell-type annotations (coarse +
fine) and standardized, machine-appliable QC actions.

The agent follows a fixed workflow over a run_one_sample_pipeline(...,
outdir=...) directory:

  1. Look at the figures BEFORE concluding anything — umap_clusters / paga /
     qc_violin / umap_qc / decontx heatmap (the one-plot-per-file design
     exists for this; the agent Reads PNGs directly as vision input);
  2. Read the de_top_genes CSV (with pct1/pct2) and form an identity
     hypothesis per cluster;
  3. Verify canonical markers per cluster with the check_genes tool (top DE
     lists often miss canonical markers, so active verification is required),
     iterating as needed;
  4. Check each cluster's QC profile with the check_qc_scores tool
     (doublet / ambient / dissociation-stress / low-quality);
  5. Optionally split heterogeneous clusters with the subcluster tool
     (scanpy leiden restrict_to) and annotate the resulting subclusters
     (ids like "5,0"); check_genes / check_qc_scores follow the refined
     clustering automatically;
  6. Submit a structured JSON via the submit_annotation tool — the host
     validates the schema, cluster coverage, and every qc_action record
     (rejecting malformed submissions back to the agent). The host publishes
     outdir/annotation_proposal.json only after all final outputs succeed; the
     agent's narrative is saved to annotation_notes.md.

After a successful run the host applies the proposal to the AnnData
(obs["_ann_coarse"], obs["_ann_fine"], obs["_qc_action"] in
keep/flag/drop), saves the updated clustered.h5ad, renders the annotation
UMAPs (coarse/fine) plus a QC-action UMAP (dark red = proposed drop, dark
yellow = flagged, light gray = keep) into figures/, and refreshes
report.html — the report's "Agent Annotation" section shows all of it.

The agent reads the *ingredients* in outdir (CSV/JSON/individual PNGs), not
report.html itself — the report is a base64 self-contained file meant for
humans; both draw on the same numbers.

QC actions are proposals only, never auto-applied to filtering (same
philosophy as DecontX monitoring). species/tissue context is injected by
the caller; the package itself assumes no cell-type knowledge.

Authentication depends on the selected adapter: Ark uses ``ARK_API_KEY``,
Claude uses its CLI credentials (or ``ANTHROPIC_API_KEY``), and dsh uses its
configured provider. Agent runtimes are optional; the rest of OSP works
without installing ``osp-sc[agent]``.

Usage:
    python -m osp.annotate osp_out/FO --species mouse --tissue "bone marrow"

    # or from Python:
    from osp.annotate import propose_annotation
    proposal = propose_annotation("osp_out/FO", species="mouse", tissue="bone marrow")
"""

import argparse
import asyncio
import glob
import json
import operator
import os
from collections import Counter

import numpy as np
import pandas as pd
import scanpy as sc
import scipy.sparse as sp

from ._io import atomic_write_h5ad, atomic_write_json, atomic_write_text
from .cluster import (
    _UMAP_AXES_RECT,
    _UMAP_DPI,
    _UMAP_FIGSIZE,
    QC_OVERLAY_COLS,
    _save_single_umap,
    _square_limits,
)
from .qc import cluster_order
from .report import generate_report

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
        cols[c] = [f"{mn:.2f}|{p:.0f}%" for mn, p in zip(mean, pct)]
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
        ad, restrict_to=(key, [cluster]), resolution=resolution,
        key_added=new_key, flavor="igraph", n_iterations=2,
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
                problems.append(
                    f"clusters[{index}].confidence must be high|medium|low: {entry.get('confidence')!r}"
                )
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
            problems.append(
                f"qc_action cluster {action.get('cluster')!r} is not a current cluster id: {action}"
            )
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
    ad.obs["_ann_coarse"] = lab.map({str(e["cluster"]): e["label_coarse"] for e in proposal["clusters"]}).astype("category")
    ad.obs["_ann_fine"] = lab.map({str(e["cluster"]): e["label_fine"] for e in proposal["clusters"]}).astype("category")

    action = np.array(["keep"] * ad.n_obs, dtype=object)
    for verb in ("flag", "drop"):
        for a in proposal.get("qc_actions", []):
            if a["action"] != verb:
                continue
            mask = (lab == str(a["cluster"])).values
            if a["scope"] == "cells":
                mask &= _OPS[a["op"]](ad.obs[a["metric"]].to_numpy(dtype=float), float(a["value"]))
            action[mask] = verb
    ad.obs["_qc_action"] = pd.Categorical(action, categories=["keep", "flag", "drop"])


def _plot_annotation(ad, figdir):
    """Annotation UMAPs (coarse/fine) via the shared single-UMAP renderer,
    plus a custom QC-action UMAP: proposed-drop cells as large dark-red dots,
    flagged cells as large dark-yellow dots, the rest as small light-gray
    dots."""
    import matplotlib.pyplot as plt

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
    for name, color, size in (("keep", "#d3d3d3", base), ("flag", "#b8860b", 1.5 * base), ("drop", "#8b0000", 1.5 * base)):
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
    context_line = ("Context — " + ", ".join(context)) if context else (
        "No species/tissue context was provided — infer cautiously from gene-name "
        "casing conventions and expression profiles, and state that inference in your conclusions."
    )
    has_decontx_heatmap = os.path.isfile(
        os.path.join(outdir, "figures", "decontx_heatmap_by_cluster.png")
    )
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
- cluster_summary_*.csv, paga_connectivities_*.csv, qc_summary.csv

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


async def _run_agent(ad, outdir, cluster_key, species, tissue, language, model, effort, max_turns):
    from .harness import ToolSpec, run_agent

    state = {"key": cluster_key, "n_sub": 0}

    def current_clusters():
        return cluster_order(ad.obs[state["key"]].astype(str))

    async def check_genes(args):
        genes = args.get("genes")
        if isinstance(genes, str):
            genes = [g for g in genes.replace(",", " ").split() if g]
        if not isinstance(genes, list) or any(not isinstance(gene, str) for gene in genes):
            return {
                "content": [{"type": "text", "text": "genes must be a list of gene-name strings"}],
                "is_error": True,
            }
        genes = [gene.strip() for gene in genes if gene.strip()]
        if not genes:
            return {"content": [{"type": "text", "text": "genes must not be empty"}], "is_error": True}
        if len(genes) > 200:
            return {
                "content": [{"type": "text", "text": "check at most 200 genes in one call"}],
                "is_error": True,
            }
        return {"content": [{"type": "text", "text": _gene_table(ad, genes, state["key"])}]}

    async def check_qc_scores(args):
        return {"content": [{"type": "text", "text": _qc_table(ad, state["key"])}]}

    async def subcluster(args):
        c = str(args.get("cluster"))
        try:
            res = float(args.get("resolution"))
        except (TypeError, ValueError):
            return {
                "content": [{"type": "text", "text": "resolution must be a finite positive number"}],
                "is_error": True,
            }
        if not np.isfinite(res) or res <= 0:
            return {
                "content": [{"type": "text", "text": "resolution must be a finite positive number"}],
                "is_error": True,
            }
        if c not in current_clusters():
            return {"content": [{"type": "text", "text": f"unknown cluster {c!r}; current clusters: {current_clusters()}"}], "is_error": True}
        new_key = f"ann_sub{state['n_sub'] + 1}"
        n, text = _subcluster_once(ad, state["key"], c, res, new_key)
        if n >= 2:
            state["n_sub"] += 1
            state["key"] = new_key
            text += "\n(the working clustering is now refined; all tools and the final submission use the new ids)"
        return {"content": [{"type": "text", "text": text}]}

    async def submit_annotation(args):
        raw = args.get("proposal_json")
        if not isinstance(raw, str):
            return {
                "content": [{"type": "text", "text": "proposal_json must be a JSON string"}],
                "is_error": True,
            }
        try:
            proposal = json.loads(raw)
        except (json.JSONDecodeError, TypeError) as e:
            return {"content": [{"type": "text", "text": f"JSON parse error, fix and resubmit: {e}"}], "is_error": True}
        problems = _validate_proposal(proposal, current_clusters(), ad.obs)
        if problems:
            return {"content": [{"type": "text", "text": "validation failed, fix and resubmit:\n- " + "\n- ".join(problems)}], "is_error": True}
        proposal["cluster_key"] = state["key"]
        return {
            "content": [{"type": "text", "text": "accepted; the host will finalize the output files"}],
            "_submitted": proposal,
        }

    tools = [
        ToolSpec(
            name="check_genes",
            description="Per-cluster mean expression and expressing-cell fraction for the given genes "
                        "(case-insensitive match against var_names). Use it to verify cell-type markers.",
            input_schema={"genes": list}, handler=check_genes,
        ),
        ToolSpec(
            name="check_qc_scores",
            description="Per-cluster QC overview (median|p90): counts/genes/mt%/doublet/contamination/Malat1/"
                        "dissociation stress. Use it to identify QC-driven clusters. No arguments.",
            input_schema={}, handler=check_qc_scores,
        ),
        ToolSpec(
            name="subcluster",
            description="Split one heterogeneous cluster with leiden restrict_to at the given resolution "
                        "(0.3-1.0 typical). On success the working clustering is refined in place: new ids look "
                        'like "5,0"/"5,1", all other clusters keep their ids, and check_genes / check_qc_scores / '
                        "submit_annotation operate on the refined clustering from then on. Returns subcluster "
                        "sizes and top distinguishing genes.",
            input_schema={"cluster": str, "resolution": float}, handler=subcluster,
        ),
        ToolSpec(
            name="submit_annotation",
            description="Submit the final conclusions (mandatory; the run only completes after validation "
                        "passes). proposal_json is a JSON string with this schema:\n" + _PROPOSAL_SCHEMA_DOC,
            input_schema={"proposal_json": str}, handler=submit_annotation,
        ),
    ]

    result = await run_agent(
        tools=tools, submit_tool="submit_annotation",
        prompt="Interpret this OSP output directory following the workflow in the system prompt "
               "exactly, and finish by submitting via submit_annotation.",
        system_prompt=_system_prompt(outdir, cluster_key, cluster_order(ad.obs[cluster_key].astype(str)), species, tissue, language),
        cwd=os.path.abspath(outdir), model=model, effort=effort, max_turns=max_turns,
        allowed_builtin=("read", "glob", "grep"), label="osp annotate",
    )

    if result.transcript_text:
        await asyncio.to_thread(
            atomic_write_text,
            os.path.join(outdir, "annotation_notes.md"),
            result.transcript_text,
        )
    return result.submitted


def propose_annotation(outdir, species=None, tissue=None, language="English", cluster_key=None, model=None, effort=None, max_turns=80):
    """Run the annotation agent on an OSP output directory; returns the
    proposal dict (see _PROPOSAL_SCHEMA_DOC; "cluster_key" records the
    clustering the annotation refers to, which may be a subclustered
    refinement like "ann_sub1").

    Applies the proposal to the AnnData (obs["_ann_coarse"] /
    obs["_ann_fine"] / obs["_qc_action"]), renders annotation and QC-action
    UMAPs into figures/, saves the updated clustered.h5ad, and refreshes
    report.html. Publishes annotation_proposal.json last, after these steps
    succeed. annotation_notes.md is best-effort narrative text from the
    backend. QC actions remain proposals; no cells are removed here.

    species/tissue: caller-provided context (e.g. "mouse"/"bone marrow");
    verify against the data's own metadata when available. When omitted the
    agent is told to infer cautiously and say so.
    language: language for the annotation output (doubts/notes/overall...),
    default "English".
    model/effort: backend-specific model id and optional reasoning effort;
    defaults follow ``HARNESS``/``MODEL`` and the bridge defaults.
    """
    from .harness import default_model

    ad = sc.read_h5ad(os.path.join(outdir, "clustered.h5ad"))
    if cluster_key is None:
        paga_key = ad.uns.get("paga", {}).get("groups")
        cluster_key = str(paga_key) if paga_key in ad.obs else _detect_primary_key(outdir)
    if cluster_key not in ad.obs:
        raise ValueError(f"cluster_key {cluster_key!r} is not present in clustered.h5ad obs")
    # A previous proposal is a completion marker. Invalidate it before any
    # agent or finalization work so a failed forced rerun remains resumable
    # instead of looking complete because an old proposal survived.
    proposal_path = os.path.join(outdir, "annotation_proposal.json")
    if os.path.isfile(proposal_path):
        os.unlink(proposal_path)

    proposal = asyncio.run(_run_agent(ad, outdir, cluster_key, species, tissue, language,
                                      model or default_model(), effort, max_turns))
    if proposal is None:
        raise RuntimeError("annotation agent returned without a validated proposal")

    _apply_proposal(ad, proposal["cluster_key"], proposal)
    _plot_annotation(ad, os.path.join(outdir, "figures"))
    atomic_write_h5ad(ad, os.path.join(outdir, "clustered.h5ad"))
    # annotation_proposal.json is the outer pipeline's completion signal, so
    # publish it last. The report is rendered from the in-memory proposal and
    # atomically replaced first; an interrupted finalization remains resumable.
    report = generate_report(outdir, annotation_proposal=proposal)
    atomic_write_json(proposal_path, proposal)
    print(f"== report refreshed: {report}", flush=True)
    return proposal


if __name__ == "__main__":
    parser = argparse.ArgumentParser(prog="osp.annotate", description=__doc__)
    parser.add_argument("outdir", help="output directory of run_one_sample_pipeline")
    parser.add_argument("--species", default=None)
    parser.add_argument("--tissue", default=None)
    parser.add_argument("--language", default="English", help='language of the annotation output (default "English")')
    parser.add_argument("--cluster-key", default=None, help="autodetected from cluster_summary_*.csv by default")
    parser.add_argument(
        "--model", default=None,
        help="model id for the selected HARNESS backend (default: MODEL env, then backend default)",
    )
    parser.add_argument(
        "--effort", default=None, choices=["low", "medium", "high", "xhigh", "max"],
        help="reasoning effort for models that support it",
    )
    parser.add_argument("--max-turns", type=int, default=80)
    args = parser.parse_args()

    proposal = propose_annotation(
        args.outdir, species=args.species, tissue=args.tissue, language=args.language,
        cluster_key=args.cluster_key, model=args.model, effort=args.effort, max_turns=args.max_turns,
    )
    for e in proposal["clusters"]:
        print(f"cluster {e['cluster']}: {e['label_coarse']} / {e['label_fine']} [{e['confidence']}]")
    print(f"\nannotation_proposal.json / annotation_notes.md / UMAPs written to {args.outdir}")

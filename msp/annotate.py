"""msp.annotate — the host side of cross-sample annotation: the schema of one cluster entry, its validation and
the application of a complete submission. The agent session that produces it runs in eca-rsi
(ecarsi/stages/crosssample.py, through msp.api); the standalone agent flow was removed with #28.

Unit of annotation: every cluster of the base clustering (msp_leiden_r2.0). Every base cluster needs a validated
entry; merges are resolved deterministically (union-find over merge_target) and inconsistent components are
rejected; adjacent coarse labels need boundary reviews (_check_coarse_boundaries).

Removal is real here (unlike inspect/integrate, which only propose): the final removed set =
preannotation_removal.csv ∪ inspect's obs["_msp_action"] == "drop" ∪ clusters marked action=remove, archived per
cell with its sources in annotation_removed.csv; integrated.h5ad is left intact and annotated.h5ad (removed cells
dropped, msp_ann_* columns added) is the deliverable. A batch-only removal becomes a keep for review
(_guard_batch_annotation, used by zoom-in too).
"""

import logging
import os
import re

import numpy as np
import pandas as pd

from .evidence import (
    cluster_order,
)

log = logging.getLogger(__name__)

BASE_KEY = "msp_leiden_r2.0"
PARENT_KEY = "msp_leiden_r1.0"
CONFIDENCES = ("high", "medium", "low")
# the one list of removal reasons, cross-sample and zoom-in alike; stress, dissociation and dying removals are
# checked by eca-rsi's stress policy (decision 0017)
REMOVE_REASONS = ("doublet", "low-quality", "ambient", "stress", "dissociation", "dying", "batch", "other")


# ---------------------------------------------------------------- evidence


# ---------------------------------------------------------------- proposal

_CLUSTER_SCHEMA_DOC = """{
  "cluster_id": "<base cluster id, e.g. "7">",
  "coarse_label": "<lineage-level label in English, e.g. 'Fibroblast' / 'Macrophage' / 'Endothelial'>",
  "fine_label": "<subtype-level label in English, e.g. 'CTHRC1+ matrix fibroblast'; for removed clusters describe what it is, e.g. 'Fibroblast-immune doublet'>",
  "merge_target": null | "<another base cluster id this one is part of — same population, not a distinct entity>",
  "action": "keep" | "remove",
  "remove_reason": null | "doublet" | "low-quality" | "ambient" | "stress" | "dissociation" | "dying" | "batch" | "other",
  "confidence": "high" | "medium" | "low",
  "evidence": {
    "distinctness": "<step 1: is it distinct from its r1.0 parent / r2.0 siblings? what separates it (local DEG), or nothing?>",
    "markers": "<step 2: the positive markers that fix the identity, verified with check_genes/check_deg>",
    "merge": "<step 3: why merge / why keep separate>"
  },
  "rationale": "<one or two sentences tying evidence to the labels and the merge/remove decision>"
}"""


def _validate_cluster(e, clusters):
    problems = []
    if not isinstance(e, dict):
        return [f"cluster entry must be an object: {e!r}"]
    for k in (
        "cluster_id",
        "coarse_label",
        "fine_label",
        "merge_target",
        "action",
        "confidence",
        "evidence",
        "rationale",
    ):
        if k not in e:
            problems.append(f"missing field {k!r}")
    if problems:
        return problems
    cid = str(e["cluster_id"])
    if cid not in clusters:
        problems.append(f"cluster_id {cid!r} is not a base cluster; base clusters: {clusters}")
    for k in ("coarse_label", "fine_label"):
        if not isinstance(e[k], str) or not e[k].strip():
            problems.append(f"{k} must be a non-empty string")
    if e["action"] not in ("keep", "remove"):
        problems.append("action must be keep|remove")
    if e["action"] == "remove" and e.get("remove_reason") not in REMOVE_REASONS:
        problems.append(f"remove requires remove_reason in {REMOVE_REASONS}")
    if e["confidence"] not in CONFIDENCES:
        problems.append(f"confidence must be one of {CONFIDENCES}")
    mt = e["merge_target"]
    if mt is not None:
        mt = str(mt)
        if mt not in clusters:
            problems.append(f"merge_target {mt!r} is not a base cluster")
        elif mt == cid:
            problems.append("merge_target cannot be the cluster itself")
    ev = e["evidence"]
    if not isinstance(ev, dict) or not all(
        isinstance(ev.get(k), str) and ev[k].strip() for k in ("distinctness", "markers", "merge")
    ):
        problems.append(
            "evidence must provide non-empty text for distinctness / markers / merge; "
            "explain unavailable evidence explicitly"
        )
    if not isinstance(e["rationale"], str) or not e["rationale"].strip():
        problems.append("rationale must be non-empty text")
    return problems


def _components(entries):
    """Union-find over merge_target edges → {cluster_id: component members}
    (sorted, numeric-aware)."""
    parent = {c: c for c in entries}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for c, e in entries.items():
        mt = e.get("merge_target")
        if mt is not None:
            parent[find(c)] = find(str(mt))
    groups = {}
    for c in entries:
        groups.setdefault(find(c), []).append(c)
    comp = {}
    for members in groups.values():
        members = cluster_order(members)
        for c in members:
            comp[c] = members
    return comp


def _validate_final(entries, clusters):
    """Cross-cluster consistency, the deterministic replacement for a
    harmonization agent. Every violation names the clusters to fix."""
    problems = []
    if not isinstance(entries, dict):
        return ["entries must be an object keyed by cluster ID"]
    for c, e in entries.items():
        problems.extend(f"cluster {c}: {p}" for p in _validate_cluster(e, clusters))
        if c not in clusters or (isinstance(e, dict) and str(e.get("cluster_id")) != c):
            problems.append(f"entry key {c!r} must match a current cluster_id")
    if problems:
        return problems
    missing = [c for c in clusters if c not in entries]
    if missing:
        problems.append(f"no submission yet for clusters {missing} — submit_cluster each of them first")
        return problems
    for c, e in entries.items():
        mt = e.get("merge_target")
        if mt is None:
            continue
        mt = str(mt)
        tgt = entries[mt]
        if tgt["action"] == "remove" and e["action"] != "remove":
            problems.append(
                f"cluster {c} merges into {mt}, but {mt} is action=remove — either remove {c} too "
                f"or drop the merge_target"
            )
    comp = _components(entries)
    seen = set()
    for members in comp.values():
        key = tuple(members)
        if key in seen or len(members) < 2:
            continue
        seen.add(key)
        kept = [m for m in members if entries[m]["action"] == "keep"]
        for field in ("coarse_label", "fine_label"):
            vals = {entries[m][field].strip() for m in kept}
            if len(vals) > 1:
                problems.append(
                    f"merged group {'+'.join(members)} disagrees on {field}: "
                    + "; ".join(f"{m}={entries[m][field]!r}" for m in kept)
                    + " — resubmit them with one shared label"
                )
    # one fine label ↔ one coarse label, and fine-label equality == merge
    by_fine = {}
    for c, e in entries.items():
        if e["action"] != "keep":
            continue
        by_fine.setdefault(e["fine_label"].strip(), []).append(c)
    for fine, members in by_fine.items():
        coarse = {entries[m]["coarse_label"].strip() for m in members}
        if len(coarse) > 1:
            problems.append(
                f"fine label {fine!r} sits under several coarse labels {sorted(coarse)} "
                f"(clusters {members}) — one fine label belongs to exactly one coarse label"
            )
        comps = {tuple(comp[m]) for m in members}
        if len(comps) > 1:
            problems.append(
                f"clusters {members} share fine label {fine!r} but are not merged — either "
                "set merge_target between them (same population) or give them distinct fine labels"
            )
    return problems


def _check_coarse_boundaries(entries, paga, reviews):
    """Require an explicit review of each adjacent pair of coarse labels.

    PAGA adjacency asks a question; neither missing local DEG rows nor a
    fold-change cutoff establishes that two clusters are the same cell type.
    Reviews may remain uncertain, and never change labels or remove cells.
    """
    pairs = set()
    for c, neighbours in paga.items():
        e = entries.get(c)
        if not e or e["action"] != "keep":
            continue
        for n in neighbours:
            other = entries.get(n)
            if other and other["action"] == "keep":
                labels = tuple(sorted({e["coarse_label"].strip(), other["coarse_label"].strip()}))
                if len(labels) == 2:
                    pairs.add(labels)
    if not isinstance(reviews, list):
        return ["boundary_reviews_json must encode a list"]
    seen = set()
    for review in reviews:
        if not isinstance(review, dict):
            return ["each boundary review must be an object"]
        labels = review.get("coarse_labels")
        if (
            not isinstance(labels, list)
            or len(labels) != 2
            or not all(isinstance(v, str) and v.strip() for v in labels)
        ):
            return ["each boundary review needs two coarse_labels"]
        pair = tuple(sorted(v.strip() for v in labels))
        if pair not in pairs or pair in seen:
            return [f"unknown, stale or duplicate coarse boundary: {labels}"]
        if not isinstance(review.get("evidence"), str) or not review["evidence"].strip():
            return [f"boundary {labels}: explain the lineage markers or the evidence still missing"]
        if type(review.get("uncertain")) is not bool:
            return [f"boundary {labels}: uncertain must be a boolean"]
        seen.add(pair)
    return [
        f"Review adjacent coarse labels {list(pair)} in boundary_reviews_json: reconcile synonyms, "
        "or explain distinguishing lineage markers (not only cell cycle, stress or prior labels). "
        "If evidence is insufficient, retain the labels with uncertain=true for review."
        for pair in sorted(pairs - seen)
    ]


def _batch_annotation_removal(entry):
    """Recognize explicit batch-artifact claims, not general batch mentions.

    Specific independent QC reasons retain their existing semantics. An
    ``other`` request describing a batch artifact must instead be reviewed;
    mentioning ambient RNA alongside that claim is not a separate QC reason.
    """
    if entry.get("action") != "remove":
        return False
    reason = entry.get("remove_reason")
    if reason == "batch":
        return True
    if reason != "other":
        return False
    pattern = r"\b(?:batch|sample)[\s_-]+art[ie]facts?\b|(?:批次|样本)伪影"
    return any(re.search(pattern, str(entry.get(field, "")), re.IGNORECASE) for field in ("fine_label", "rationale"))


def _guard_batch_annotation(entry):
    """Retain batch-only suspicions without changing the model's explanation (cross-sample and zoom-in)."""
    if _batch_annotation_removal(entry):
        entry.update(
            requested_action=entry["action"],
            requested_remove_reason=entry.get("remove_reason"),
            action="keep",
            remove_reason=None,
            host_adjustment={
                "policy": "batch_annotation_non_destructive_v1",
                "reason": "Sample/batch composition alone does not establish invalid cells; retained for review.",
            },
            review_required=True,
        )
        log.warning("== cluster %s: batch-only removal adjusted to keep for review",
                    entry.get("cluster_id", entry.get("type_clusters")))
    return entry


# ---------------------------------------------------------------- apply


def _apply(ad, proposal, pre_removed, pre_sources):
    """obs columns on the FULL object: msp_ann_cluster (merged id, members
    joined by '+'), msp_ann_coarse / msp_ann_fine, msp_ann_action
    (keep/remove). Returns the removal archive (removed cells only, with
    their sources)."""
    entries = {str(e["cluster_id"]): e for e in proposal["clusters"]}
    if any(_batch_annotation_removal(e) for e in entries.values()):
        raise ValueError("unguarded batch-only annotation removal; normalize and validate the proposal before applying")
    comp = _components(entries)
    base = ad.obs[BASE_KEY].astype(str)
    merged_id = {c: "+".join(members) for c, members in comp.items()}
    ad.obs["msp_ann_cluster"] = base.map(merged_id).astype("category")
    ad.obs["msp_ann_coarse"] = base.map({c: e["coarse_label"].strip() for c, e in entries.items()}).astype("category")
    ad.obs["msp_ann_fine"] = base.map({c: e["fine_label"].strip() for c, e in entries.items()}).astype("category")
    ad.obs["msp_ann_review"] = base.map({c: bool(e.get("review_required", False)) for c, e in entries.items()}).astype(
        bool
    )
    agent_remove = base.isin([c for c, e in entries.items() if e["action"] == "remove"]).values
    removed = pre_removed | agent_remove
    ad.obs["msp_ann_action"] = pd.Categorical(np.where(removed, "remove", "keep"), categories=["keep", "remove"])
    archive = pd.DataFrame(
        {
            "cell": ad.obs_names,
            BASE_KEY: base.values,
            **pre_sources,
            "annotate_remove": agent_remove,
            "remove_reason": base.map(
                {c: e.get("remove_reason") for c, e in entries.items() if e["action"] == "remove"}
            ).values,
        }
    )
    return archive.loc[removed].reset_index(drop=True)


def _palette(ad, col):
    """stanhue hierarchical palette (related labels share a hue family) in
    category order, or None for scanpy's default when stanhue is missing or
    fails; the fallback is announced so it never passes unnoticed. Failing
    here must not lose the annotation run that precedes it."""
    try:
        from stanhue import assign_celltype_colors

        cmap = assign_celltype_colors(np.asarray(ad.obsm["X_umap"]), ad.obs[col].astype(str).to_numpy())
    except Exception as exc:
        log.warning(f"== stanhue palette unavailable ({exc!r}); using scanpy's default palette for {col}")
        return None
    return [cmap.get(str(c), "#999999") for c in ad.obs[col].cat.categories]


def _plot(ad_full, ad_kept, figdir):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    from .plots import UMAP_DPI, save_single_umap, umap_axes

    os.makedirs(figdir, exist_ok=True)
    for col, fname in (("msp_ann_coarse", "annotation_umap_coarse.png"), ("msp_ann_fine", "annotation_umap_fine.png")):
        ad_kept.obs[col] = ad_kept.obs[col].cat.remove_unused_categories()
        if ad_kept.n_obs == 0:
            # Preserve the usual figure files and full-run coordinate scale.
            fig, ax = umap_axes(ad_full)
            ax.text(0.5, 0.5, "No cells retained after annotation", ha="center", va="center", transform=ax.transAxes)
            ax.set_title(f"UMAP: {col} (0 cells)")
            fig.savefig(os.path.join(figdir, fname), dpi=UMAP_DPI)
            plt.close(fig)
            continue
        pal = _palette(ad_kept, col)
        if pal:
            ad_kept.uns[f"{col}_colors"] = pal
        n = ad_kept.obs[col].nunique()
        save_single_umap(
            ad_kept,
            col,
            os.path.join(figdir, fname),
            repel=True,
            repel_fontsize=9 if n > 15 else 11,
            figsize=(9, 9) if n > 15 else None,
        )

    xy = np.asarray(ad_full.obsm["X_umap"])
    act = ad_full.obs["msp_ann_action"].astype(str).values
    base = 120000 / ad_full.n_obs
    fig, ax = umap_axes(ad_full)
    for name, color, size in (("keep", "#d3d3d3", base), ("remove", "#c0392b", 1.5 * base)):
        m = act == name
        if m.any():
            ax.scatter(xy[m, 0], xy[m, 1], s=size, c=color, linewidths=0, label=f"{name} (n={int(m.sum())})")
    ax.set_title("UMAP: cells removed at annotation (all sources)")
    ax.legend(loc="upper right", fontsize=8, framealpha=0.9)
    fig.savefig(os.path.join(figdir, "annotation_umap_removed.png"), dpi=UMAP_DPI)
    plt.close(fig)


# ---------------------------------------------------------------- agent


# ---------------------------------------------------------------- entry

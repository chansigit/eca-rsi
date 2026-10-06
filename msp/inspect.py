"""msp.inspect — the host side of cross-sample inspection: the proposal schema, its validation, the batch guard
and the mapping of verdicts onto obs["_msp_action"] (keep/flag/drop) in integrated.h5ad, plus the subcluster
operation the agent may call. The agent session runs in eca-rsi (ecarsi/stages/crosssample.py, through msp.api);
the standalone agent flow was removed with #28.

Verdicts are proposals only: msp.annotate applies the drops. Cells flagged or dropped per sample
(obs["_qc_action"]) stay in on purpose, since their cross-sample clustering is the evidence weighed here; only
DEG excludes preannotation_removal.csv's cells, matching the precomputed deg_global_*/deg_local_* tables.
"""

import logging
import operator

import matplotlib

matplotlib.use("Agg")
import numpy as np
import pandas as pd
import scanpy as sc

from .deg_logging import rank_genes_groups
from .evidence import (
    QC_COLS,
    DegCache,
    DegTables,
    cluster_order,
)

log = logging.getLogger(__name__)

_OPS = {">": operator.gt, ">=": operator.ge, "<": operator.lt, "<=": operator.le}


def _subcluster_once(ad, key, cluster, resolution, new_key, remove_mask, *, compute_markers=True):
    """Split one cluster; sizes reported are the FULL split (removed cells
    included, so counts stay honest), but the built-in sibling DE excludes
    remove_mask cells — same DEG-only exclusion as check_deg / the
    precomputed deg_global_*/deg_local_* CSVs."""
    parent_mask = (ad.obs[key].astype(str) == cluster).values
    sc.tl.leiden(
        ad, restrict_to=(key, [cluster]), resolution=resolution, key_added=new_key, flavor="igraph", n_iterations=2
    )
    sub_labels = ad.obs[new_key][parent_mask].astype(str)
    subs = cluster_order(sub_labels)
    if len(subs) < 2:
        del ad.obs[new_key]
        return 0, f"cluster {cluster} did not split at resolution {resolution}; try higher"
    sub = ad[parent_mask].copy()
    sub.obs["_sub"] = pd.Categorical(sub_labels.values)
    sub_clean = sub[~remove_mask[parent_mask]].copy()
    top = None
    clean_sizes = sub_clean.obs["_sub"].value_counts()
    clean_sizes = clean_sizes[clean_sizes > 0]
    if compute_markers and len(clean_sizes) >= 2 and clean_sizes.min() >= 2:
        rank_genes_groups(sub_clean, "_sub", method="wilcoxon", use_raw=False)
        top = sc.get.rank_genes_groups_df(sub_clean, group=None).groupby("group", observed=True).head(10)
    sizes = sub_labels.value_counts()
    lines = [f"cluster {cluster} split into {len(subs)} subclusters at resolution {resolution}:"]
    for s in subs:
        genes = ", ".join(top.loc[top["group"] == s, "names"]) if top is not None else ""
        note = "" if genes else " (no DE — too few non-removed cells)"
        lines.append(f"  {s} (n={int(sizes[s])}) top genes vs siblings: {genes}{note}")
    return len(subs), "\n".join(lines)


_PROPOSAL_SCHEMA_DOC = """{
  "clusters": [
    {"cluster": "<id>", "verdict": "real|artifact-doublet|artifact-lowquality|artifact-batch|artifact-ambient|ambiguous",
     "action": "keep|flag|drop", "confidence": "high|medium|low",
     "tests": {"markers": "<test a finding>", "qc": "<test b>", "composition": "<test c>",
               "geometry": "<test d>", "stability": "<test e>"},
     "rationale": "<one or two sentences tying the tests to the verdict>"}
    // must cover EVERY cluster of the current clustering (incl. subcluster ids like "5,0")
  ],
  "cell_actions": [
    // optional finer-than-cluster records: only cells of that cluster matching metric op value
    {"cluster": "<id>", "metric": "<numeric obs column>", "op": ">|>=|<|<=", "value": 0.3,
     "action": "drop|flag", "reason": "doublet|ambient|debris|low-quality|other", "note": "<free text>"}
  ],
  "overall": "<overall assessment of the integration>"
}
Host policy: artifact-batch/drop is adjusted to flag without another agent turn;
requested_action and host_adjustment record the original request and policy.
"""

_VERDICTS = ("real", "artifact-doublet", "artifact-lowquality", "artifact-batch", "artifact-ambient", "ambiguous")


def _validate_proposal(proposal, clusters, obs):
    problems = []
    if not isinstance(proposal, dict):
        return [f"proposal must be a JSON object, got {type(proposal).__name__}"]
    entries = proposal.get("clusters")
    if not isinstance(entries, list) or not entries:
        problems.append('missing "clusters" list')
        entries = []
    seen = set()
    for e in entries:
        if not isinstance(e, dict):
            problems.append(f"cluster entry must be an object: {e!r}")
            continue
        missing = [k for k in ("cluster", "verdict", "action", "confidence", "tests", "rationale") if k not in e]
        if missing:
            problems.append(f"cluster entry missing {missing}: {e}")
            continue
        if e["verdict"] not in _VERDICTS:
            problems.append(f"verdict must be one of {_VERDICTS}: {e}")
        if e["action"] not in ("keep", "flag", "drop"):
            problems.append(f"action must be keep|flag|drop: {e}")
        if e["confidence"] not in ("high", "medium", "low"):
            problems.append(f"confidence must be high|medium|low: {e}")
        if not isinstance(e["tests"], dict) or not all(
            isinstance(e["tests"].get(k), str) and e["tests"][k].strip()
            for k in ("markers", "qc", "composition", "geometry", "stability")
        ):
            problems.append(
                f"tests must provide non-empty text for markers/qc/composition/geometry/stability "
                f"(explain unavailable evidence explicitly): {e}"
            )
        if not isinstance(e["rationale"], str) or not e["rationale"].strip():
            problems.append(f"rationale must be non-empty text: {e}")
        cluster = str(e.get("cluster"))
        if cluster not in clusters:
            problems.append(f"unknown cluster entry: {cluster!r}")
        if cluster in seen:
            problems.append(f"duplicate cluster entry: {cluster!r}")
        seen.add(cluster)
    covered = {str(e.get("cluster")) for e in entries if isinstance(e, dict)}
    missed = [c for c in clusters if c not in covered]
    if missed:
        problems.append(f"clusters without a verdict: {missed}")
    cell_actions = proposal.get("cell_actions", [])
    if not isinstance(cell_actions, list):
        problems.append('"cell_actions" must be a list when present')
        cell_actions = []
    for a in cell_actions:
        if not isinstance(a, dict):
            problems.append(f"cell_action must be an object: {a!r}")
            continue
        if str(a.get("cluster")) not in clusters:
            problems.append(f"cell_action cluster {a.get('cluster')!r} is not a current cluster id: {a}")
        metric = a.get("metric")
        if not isinstance(metric, str) or metric not in obs.columns or not pd.api.types.is_numeric_dtype(obs[metric]):
            problems.append(f'cell_action "metric" must be a numeric obs column: {a}')
        if not isinstance(a.get("op"), str) or a["op"] not in _OPS:
            problems.append(f'cell_action "op" must be one of {sorted(_OPS)}: {a}')
        try:
            if isinstance(a.get("value"), bool) or not np.isfinite(float(a.get("value"))):
                raise ValueError("not a finite number")
        except (TypeError, ValueError, OverflowError):
            problems.append(f'cell_action "value" must be finite and numeric: {a}')
        if a.get("action") not in ("drop", "flag"):
            problems.append(f'cell_action "action" must be drop|flag: {a}')
        if a.get("reason") not in ("doublet", "ambient", "debris", "low-quality", "other"):
            problems.append(f'cell_action "reason" must be doublet|ambient|debris|low-quality|other: {a}')
        if not isinstance(a.get("note"), str) or not a["note"].strip():
            problems.append(f'cell_action "note" must be non-empty text: {a}')
    return problems


def _guard_batch_actions(proposal):
    """Retain sample/batch suspicions without treating them as cell invalidity.

    Normalize in place so callers persist the same proposal applied to AnnData.
    The original requested action and host policy remain auditable. This also
    covers saved proposals, bypassing neither the policy nor agent retries.
    """
    for entry in proposal["clusters"]:
        if entry.get("verdict") == "artifact-batch" and entry.get("action") == "drop":
            entry["requested_action"] = "drop"
            entry["action"] = "flag"
            entry["host_adjustment"] = {
                "policy": "batch_verdict_non_destructive_v1",
                "reason": "Sample/batch composition alone does not establish invalid cells; retained for review.",
            }
            log.warning("== inspect cluster %s: artifact-batch drop adjusted to flag for review", entry["cluster"])
    return proposal


def _apply_proposal(ad, key, proposal):
    """obs["_msp_action"] in keep/flag/drop: cluster actions first, then
    cell_actions refine; within each pass flags before drops so drop wins."""
    _guard_batch_actions(proposal)
    lab = ad.obs[key].astype(str)
    action = np.array(["keep"] * ad.n_obs, dtype=object)
    for verb in ("flag", "drop"):
        for e in proposal["clusters"]:
            if e["action"] == verb:
                action[(lab == str(e["cluster"])).values] = verb
        for a in proposal.get("cell_actions", []):
            if a["action"] == verb:
                mask = (lab == str(a["cluster"])).values
                mask = mask & _OPS[a["op"]](ad.obs[a["metric"]].to_numpy(dtype=float), float(a["value"]))
                action[mask] = verb
    ad.obs["_msp_action"] = pd.Categorical(action, categories=["keep", "flag", "drop"])
    ad.obs["_msp_verdict"] = lab.map({str(e["cluster"]): e["verdict"] for e in proposal["clusters"]}).astype("category")


__all__ = [
    "QC_COLS",
    "DegCache",
    "DegTables",
]

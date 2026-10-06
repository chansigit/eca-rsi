"""zmip.annotate — the host side of one lineage's type annotation: the schema of a cluster entry and its
validation (coverage of the current clustering, union-find over merge_target, the label hierarchy, the reassign
rules). The agent session runs in eca-rsi (ecarsi/stages/zoomin.py, through zmip.api); quality decisions are
validated and applied by zmip.scheduled. The standalone agent flow was removed with #28.

Actions: keep (coarse_label one of this lineage's labels), remove (a reason from msp.api.REMOVE_REASONS),
reassign (another lineage's coarse label; relabel only, the cells are not re-embedded there this round).
"""

import logging

from msp.api import CONFIDENCES, REMOVE_REASONS, components

log = logging.getLogger(__name__)

REMOVE_BUDGET = 0.10  # agent-removed share of a lineage above which finalize asks for a second look

PREVIOUS_COLS = ("msp_ann_coarse", "msp_ann_fine")  # last round's labels, carried on the subset as
PREV_SUFFIX = "_prev"  # msp_ann_coarse_prev / msp_ann_fine_prev


# ---------------------------------------------------------------- context


# ---------------------------------------------------------------- schema / validation

_CLUSTER_SCHEMA_DOC = """{
  "cluster_id": "<current cluster id (subcluster ids like "5,0" once you split)>",
  "coarse_label": "<keep: one of THIS lineage's coarse labels; reassign: equal to reassign_to; remove: descriptive>",
  "fine_label": "<subtype label in English, e.g. 'CTHRC1+ matrix fibroblast'; for removed clusters what it is>",
  "merge_target": null | "<another current cluster id this one is part of>",
  "action": "keep" | "remove" | "reassign",
  "remove_reason": null | "doublet" | "low-quality" | "ambient" | "stress" | "dissociation" | "dying" | "batch" | "other",
  "reassign_to": null | "<a coarse label belonging to a DIFFERENT lineage>",
  "confidence": "high" | "medium" | "low",
  "evidence": {
    "distinctness": "<step 1: distinct from r1.0 parent / siblings, or a splinter?>",
    "markers": "<step 2: positive markers verified with check_genes/check_deg>",
    "foreign": "<step 3: foreign-lineage scores — doublet / ambient / misassignment / shared biology?>",
    "merge": "<step 4: why merge or keep separate>"
  },
  "rationale": "<one or two sentences>"
}"""


def _validate_cluster(e, clusters, lineage_labels, other_labels):
    if not isinstance(e, dict):
        return ["cluster submission must be a JSON object"]
    problems = []
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
        problems.append(f"cluster_id {cid!r} is not a current cluster; current: {clusters}")
    for k in ("coarse_label", "fine_label", "rationale"):
        if not isinstance(e[k], str) or not e[k].strip():
            problems.append(f"{k} must be a non-empty string")
    act = e["action"]
    if act not in ("keep", "remove", "reassign"):
        problems.append("action must be keep|remove|reassign")
    elif act == "keep" and str(e["coarse_label"]).strip() not in lineage_labels:
        problems.append(
            f"keep requires coarse_label in this lineage's labels {lineage_labels}; "
            f"got {e['coarse_label']!r} — if it is really another lineage's cell type, use "
            f"action=reassign with reassign_to"
        )
    elif act == "remove" and e.get("remove_reason") not in REMOVE_REASONS:
        problems.append(f"remove requires remove_reason in {REMOVE_REASONS}")
    elif act == "reassign":
        tgt = e.get("reassign_to")
        if not isinstance(tgt, str) or tgt not in other_labels:
            problems.append(
                f"reassign_to must be a coarse label of another lineage {sorted(other_labels)}; got {tgt!r}"
            )
        elif str(e["coarse_label"]).strip() != tgt:
            problems.append(f"for reassign, coarse_label must equal reassign_to ({tgt!r})")
    if e["confidence"] not in CONFIDENCES:
        problems.append(f"confidence must be one of {CONFIDENCES}")
    mt = e["merge_target"]
    if mt is not None:
        mt = str(mt)
        if mt not in clusters:
            problems.append(f"merge_target {mt!r} is not a current cluster")
        elif mt == cid:
            problems.append("merge_target cannot be the cluster itself")
    ev = e["evidence"]
    if not isinstance(ev, dict) or not all(k in ev for k in ("distinctness", "markers", "foreign", "merge")):
        problems.append("evidence must be an object with distinctness / markers / foreign / merge")
    else:
        for k in ("distinctness", "markers", "foreign", "merge"):
            if not isinstance(ev[k], str) or not ev[k].strip():
                problems.append(f"evidence.{k} must be a non-empty string")
    return problems


def _validate_final(entries, clusters):
    problems = []
    missing = [c for c in clusters if c not in entries]
    stale = [c for c in entries if c not in clusters]
    if missing or stale:
        if missing:
            problems.append(f"no submission for current clusters {missing}")
        if stale:
            problems.append(
                f"submissions for clusters that no longer exist (split since): {stale} — resubmit their subclusters"
            )
        return problems
    for c, e in entries.items():
        mt = e.get("merge_target")
        if mt is None:
            continue
        if str(mt) not in entries:
            problems.append(
                f"cluster {c}: merge_target {mt!r} is no longer a current cluster "
                "— resubmit this cluster with a current target or null"
            )
            continue
        tgt = entries[str(mt)]
        if tgt["action"] != e["action"] or tgt.get("reassign_to") != e.get("reassign_to"):
            problems.append(
                f"cluster {c} ({e['action']}) merges into {mt} ({tgt['action']}"
                f"{', → ' + str(tgt.get('reassign_to')) if tgt.get('reassign_to') else ''}) — "
                "merged clusters must share the same action (and reassign target)"
            )
    if problems:
        return problems
    comp = components(entries)
    seen = set()
    for _c, members in comp.items():
        key = tuple(members)
        if key in seen or len(members) < 2:
            continue
        seen.add(key)
        live = [m for m in members if entries[m]["action"] != "remove"]
        for field in ("coarse_label", "fine_label"):
            vals = {entries[m][field].strip() for m in live}
            if len(vals) > 1:
                problems.append(
                    f"merged group {'+'.join(members)} disagrees on {field}: "
                    + "; ".join(f"{m}={entries[m][field]!r}" for m in live)
                )
    by_fine = {}
    for c, e in entries.items():
        if e["action"] == "remove":
            continue
        by_fine.setdefault(e["fine_label"].strip(), []).append(c)
    for fine, members in by_fine.items():
        coarse = {entries[m]["coarse_label"].strip() for m in members}
        if len(coarse) > 1:
            problems.append(
                f"fine label {fine!r} sits under several coarse labels {sorted(coarse)} (clusters {members})"
            )
        comps = {tuple(comp[m]) for m in members}
        if len(comps) > 1:
            problems.append(
                f"clusters {members} share fine label {fine!r} but are not merged — set merge_target "
                "between them or give them distinct fine labels"
            )
    return problems


# ---------------------------------------------------------------- apply


# ---------------------------------------------------------------- agent


# ---------------------------------------------------------------- entry

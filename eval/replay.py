"""Put one frozen lineage decision to a chosen model and score what comes back.

    HARNESS=claude python eval/replay.py <fixture> --model claude-opus-5 --work <dir>

Runs the production code path, not a reimplementation of it: `zmip.annotate.annotate_lineage()`
on the fixture's own `integrated.h5ad`, with `score_foreign()` recomputed first exactly as
`zmip.lineage` does. Nothing about the task is simplified, so the result is comparable with what
the recorded model was asked.

Why recompute the foreign scores instead of copying them: `score_foreign()` adds
`obs["foreign_<lineage>"]` *after* `integrated.h5ad` is written, so those columns exist on disk
only inside `annotated.h5ad` -- which has already had the removed cells taken out (7,239 of 7,297
in the B cell fixture). Copying them would silently drop the cells the decision was partly about.
`lineage_markers.csv` and `uns["msp"]["resolutions"]` are enough to redo the computation exactly.

Scoring is deliberately two numbers, never one:

  contract   what the host said. Rejections are counted by category (see baseline.py) and a run
             that never reaches a valid submission is a failure outright. Pass/fail, objective.
  agreement  how often the new proposal makes the same call as the recorded one, per cluster.
             This is NOT accuracy. The recorded proposal is one model's answer on one day; a
             disagreement may well be the new model being right. Read it as "how differently
             does this model behave", and look at the disagreements by hand.

Cost and wall time come from the bridge's own summary lines.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import shutil
import sys
import time
from collections import Counter
from pathlib import Path

from baseline import RUN, category
from extract import _safe, file_digest, verify_fixture


class _Watch(logging.Handler):
    """Count what the host said back, straight off the kernel's log stream."""

    def __init__(self):
        super().__init__()
        self.rejections = []
        self.tool_errors = []
        self.cost = None
        self.usage = None

    def emit(self, record):
        import re

        msg = record.getMessage()
        m = re.search(r"tool (error|exception) in (\w*)[:=] ?(.*)", msg)
        if m:
            target = (
                self.rejections
                if m.group(2) in ("submit_cluster", "finalize_annotation")
                else self.tool_errors
            )
            target.append((category(m.group(1), m.group(3)), m.group(3)[:200]))
        m = re.search(r"agent cost: \$([0-9.]+)", msg)
        if m:
            self.cost = (self.cost or 0.0) + float(m.group(1))
        m = RUN.search(msg)
        if m:
            self.usage = {
                key: int(m[key]) for key in ("requests", "input", "output", "reasoning")
            }


def _partition_agreement(a: dict, b: dict, field: str, keys: list) -> float | None:
    """Do the two proposals group the clusters the same way, ignoring wording?

    Exact string equality is the wrong test for a free-text label: "DB B-cell lymphoma cell
    line (PAX5+ CD20+ ...)" and "DB B-cell lymphoma line, non-cycling (PAX5+ EBF1+ ...)" are
    the same call phrased differently and score 0. What is comparable is the *partition* the
    labels induce -- for every pair of clusters, do both proposals put them under the same
    label or not. That is invariant to phrasing and still catches a model that collapses a
    distinction the other one drew (or invents one).
    """
    pairs = [
        (keys[i], keys[j]) for i in range(len(keys)) for j in range(i + 1, len(keys))
    ]
    if not pairs:
        return None
    same = sum(
        (a[x].get(field) == a[y].get(field)) == (b[x].get(field) == b[y].get(field))
        for x, y in pairs
    )
    return same / len(pairs)


def _spread(items) -> dict:
    from collections import Counter

    return dict(Counter(items))


def agreement(new: dict, old: dict) -> dict:
    """Compare the two proposals, and say when a comparison carries no information.

    Clusters only one side mentions are reported separately rather than scored: a model that
    simply omits a cluster should not look like it disagreed about it.

    Every agreement number is published next to the distribution it came from, because on many
    lineages the recorded answer is degenerate -- every cluster kept, one allowed coarse label --
    and "100% agreement" then says nothing about the model. `discriminating` flags exactly that.
    """
    a = {c["cluster_id"]: c for c in new.get("clusters", [])}
    b = {c["cluster_id"]: c for c in old.get("clusters", [])}
    both = sorted(set(a) & set(b))
    act_a, act_b = (
        _spread(a[k].get("action") for k in both),
        _spread(b[k].get("action") for k in both),
    )
    return {
        "clusters_new": len(a),
        "clusters_recorded": len(b),
        "compared": len(both),
        "only_new": sorted(set(a) - set(b)),
        "only_recorded": sorted(set(b) - set(a)),
        "actions_new": act_a,
        "actions_recorded": act_b,
        "confidence_new": _spread(a[k].get("confidence") for k in both),
        "confidence_recorded": _spread(b[k].get("confidence") for k in both),
        "merged_groups_new": len(new.get("merged_groups", [])),
        "merged_groups_recorded": len(old.get("merged_groups", [])),
        "action_agreement": (
            sum(a[k].get("action") == b[k].get("action") for k in both) / len(both)
            if both
            else None
        ),
        "coarse_agreement": (
            sum(a[k].get("coarse_label") == b[k].get("coarse_label") for k in both)
            / len(both)
            if both
            else None
        ),
        "fine_partition_agreement": _partition_agreement(a, b, "fine_label", both),
        "fine_labels_new": len({a[k].get("fine_label") for k in both}),
        "fine_labels_recorded": len({b[k].get("fine_label") for k in both}),
        "discriminating": {
            "action": len(act_b)
            > 1,  # every cluster kept -> action agreement is trivially 1
            "coarse": len({b[k].get("coarse_label") for k in both}) > 1,
        },
        "action_differs": [
            {
                "cluster": k,
                "recorded": b[k].get("action"),
                "new": a[k].get("action"),
                "recorded_label": b[k].get("coarse_label"),
                "new_label": a[k].get("coarse_label"),
            }
            for k in both
            if a[k].get("action") != b[k].get("action")
        ],
    }


def run(fixture: Path, work: Path, model: str, max_turns=200) -> dict:
    import pandas as pd
    import scanpy as sc
    from harness_bridge import resolve_agent_config
    from zmip.annotate import annotate_lineage
    from zmip.foreign import score_foreign
    from zmip.lineage import load_result
    from zmip.merge import _validate_annotation, _validate_partition
    from zmip.runtime import runtime_identity

    if os.environ.get("AGENT_MODEL_POOL"):
        raise ValueError(
            "unset AGENT_MODEL_POOL: each evaluation must use only its named model"
        )
    identity = verify_fixture(fixture)
    if work.exists():
        raise ValueError(
            f"evaluation work directory already exists: {work}; use a new directory"
        )
    config = resolve_agent_config(model=model).as_manifest()
    runtime = runtime_identity()

    answer = json.loads((fixture / "answer.json").read_text())
    call = answer["call"]

    shutil.copytree(
        fixture / "inputs", work
    )  # a fresh directory never inherits another model's checkpoint
    figdir = work / "figures"
    figdir.mkdir(exist_ok=True)

    ad = sc.read_h5ad(work / "integrated.h5ad")
    species = ad.uns["msp"].get("species")
    mk = pd.read_csv(
        work / "lineage_markers.csv",
        keep_default_na=False,
        dtype={"lineage": str, "gene": str},
    )
    markers = {
        g: mk.loc[mk["lineage"] == g, "gene"].tolist() for g in mk["lineage"].unique()
    }
    keys = [f"msp_leiden_r{r}" for r in ad.uns["msp"]["resolutions"] if r in (1.0, 2.0)]

    watch = _Watch()
    logger = logging.getLogger()
    level = logger.level
    logger.addHandler(watch)
    logger.setLevel(logging.INFO)

    started = time.time()
    new, error = None, None
    try:
        foreign_cols = score_foreign(
            ad, markers, call["lineage"], keys, str(work), str(figdir)
        )
        expected = ad.obs_names.copy()
        annotate_lineage(
            ad,
            str(work),
            call["lineage"],
            call["lineage_labels"],
            call["other_labels"],
            foreign_cols,
            species=species,
            model=model,
            max_turns=max_turns,
        )
        result = load_result(work)
        kept = sc.read_h5ad(work / "annotated.h5ad", backed="r")
        try:
            _validate_partition(
                call["lineage"],
                expected,
                kept.obs,
                result["removed"],
                result["reassigned"],
            )
            _validate_annotation(
                call["lineage"],
                kept.obs,
                result["reassigned"],
                call["lineage_labels"],
                call["lineage_labels"] + call["other_labels"],
            )
        finally:
            kept.file.close()
        new = json.loads((work / "annotation_proposal.json").read_text())
    except Exception as exc:
        error = {"type": type(exc).__name__, "message": str(exc)[:1500]}
    finally:
        logger.removeHandler(watch)
        logger.setLevel(level)
    elapsed = time.time() - started
    if verify_fixture(fixture) != identity:
        raise ValueError("fixture changed during evaluation")

    return {
        "fixture": fixture.name,
        "fixture_sha256": identity,
        "evaluator_sha256": {
            name: file_digest(Path(__file__).with_name(name))
            for name in ("replay.py", "extract.py", "baseline.py")
        },
        "model": model,
        "harness": config["harness"],
        "runtime": runtime,
        "max_turns": max_turns,
        "status": "failed" if error else "passed",
        "error": error,
        "wall_s": round(elapsed, 1),
        "cost_usd": round(watch.cost, 4) if watch.cost is not None else None,
        "usage": watch.usage,
        "contract": {
            "passed": error is None,
            "rejections": len(watch.rejections),
            "by_category": dict(Counter(c for c, _ in watch.rejections)),
            "messages": [m for _, m in watch.rejections][:10],
        },
        "tool_errors": {
            "count": len(watch.tool_errors),
            "by_category": dict(Counter(c for c, _ in watch.tool_errors)),
        },
        "recorded_contract": {
            "rejections": sum(
                r["tool"] in ("submit_cluster", "finalize_annotation")
                for r in answer["host"]["rejections"]
            )
            if answer["host"]["log_found"]
            else None,
        },
        "agreement": agreement(new, answer["recorded_proposal"])
        if new is not None
        else None,
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fixtures", nargs="+", type=Path)
    parser.add_argument("--model", required=True)
    parser.add_argument("--work", required=True, type=Path)
    parser.add_argument("--harness")
    parser.add_argument("--max-turns", type=int, default=200)
    args = parser.parse_args(argv)
    if args.harness:
        os.environ["HARNESS"] = args.harness
    if args.max_turns < 1:
        parser.error("--max-turns must be positive")
    suffix = (
        _safe(args.model)[:80]
        + "-"
        + hashlib.sha256(
            (os.environ.get("HARNESS", "") + args.model).encode()
        ).hexdigest()[:8]
    )

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(message)s", stream=sys.stderr
    )
    out = []
    score_file = args.work / f"scores-{suffix}.json"
    if score_file.exists():
        parser.error(f"{score_file} exists; use a new --work directory")
    for f in args.fixtures:
        r = run(
            f.resolve(), args.work / f"{f.name}--{suffix}", args.model, args.max_turns
        )
        out.append(r)
        print(json.dumps(r, indent=2, ensure_ascii=False))
        score_file.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    return int(any(r["status"] != "passed" for r in out))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

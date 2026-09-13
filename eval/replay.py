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

import json
import logging
import os
import shutil
import sys
import time
from pathlib import Path


class _Watch(logging.Handler):
    """Count what the host said back, straight off the kernel's log stream."""

    def __init__(self):
        super().__init__()
        self.rejections: list[tuple[str, str]] = []
        self.cost = 0.0

    def emit(self, record):
        import re

        msg = record.getMessage()
        m = re.search(r"tool (error|exception) in (\w*)[:=] ?(.*)", msg)
        if m:
            sys.path.insert(0, str(Path(__file__).parent))
            from baseline import category

            self.rejections.append((category(m.group(1), m.group(3)), m.group(3)[:200]))
        m = re.search(r"agent cost: \$([0-9.]+)", msg)
        if m:
            self.cost += float(m.group(1))


def _partition_agreement(a: dict, b: dict, field: str, keys: list) -> float | None:
    """Do the two proposals group the clusters the same way, ignoring wording?

    Exact string equality is the wrong test for a free-text label: "DB B-cell lymphoma cell
    line (PAX5+ CD20+ ...)" and "DB B-cell lymphoma line, non-cycling (PAX5+ EBF1+ ...)" are
    the same call phrased differently and score 0. What is comparable is the *partition* the
    labels induce -- for every pair of clusters, do both proposals put them under the same
    label or not. That is invariant to phrasing and still catches a model that collapses a
    distinction the other one drew (or invents one).
    """
    pairs = [(keys[i], keys[j]) for i in range(len(keys)) for j in range(i + 1, len(keys))]
    if not pairs:
        return None
    same = sum((a[x].get(field) == a[y].get(field)) == (b[x].get(field) == b[y].get(field))
               for x, y in pairs)
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
    act_a, act_b = _spread(a[k].get("action") for k in both), _spread(b[k].get("action") for k in both)
    return {
        "clusters_new": len(a), "clusters_recorded": len(b), "compared": len(both),
        "only_new": sorted(set(a) - set(b)), "only_recorded": sorted(set(b) - set(a)),
        "actions_new": act_a, "actions_recorded": act_b,
        "confidence_new": _spread(a[k].get("confidence") for k in both),
        "confidence_recorded": _spread(b[k].get("confidence") for k in both),
        "merged_groups_new": len(new.get("merged_groups", [])),
        "merged_groups_recorded": len(old.get("merged_groups", [])),
        "action_agreement": (sum(a[k].get("action") == b[k].get("action") for k in both) / len(both)
                             if both else None),
        "coarse_agreement": (sum(a[k].get("coarse_label") == b[k].get("coarse_label") for k in both)
                             / len(both) if both else None),
        "fine_partition_agreement": _partition_agreement(a, b, "fine_label", both),
        "fine_labels_new": len({a[k].get("fine_label") for k in both}),
        "fine_labels_recorded": len({b[k].get("fine_label") for k in both}),
        "discriminating": {
            "action": len(act_b) > 1,   # every cluster kept -> action agreement is trivially 1
            "coarse": len({b[k].get("coarse_label") for k in both}) > 1,
        },
        "action_differs": [
            {"cluster": k, "recorded": b[k].get("action"), "new": a[k].get("action"),
             "recorded_label": b[k].get("coarse_label"), "new_label": a[k].get("coarse_label")}
            for k in both if a[k].get("action") != b[k].get("action")
        ],
    }


def run(fixture: Path, work: Path, model: str) -> dict:
    import scanpy as sc
    import pandas as pd
    from zmip.annotate import annotate_lineage
    from zmip.foreign import score_foreign

    answer = json.loads((fixture / "answer.json").read_text())
    call = answer["call"]

    work.mkdir(parents=True, exist_ok=True)
    shutil.copytree(fixture / "inputs", work, dirs_exist_ok=True)   # never write into the fixture
    figdir = work / "figures"
    figdir.mkdir(exist_ok=True)

    ad = sc.read_h5ad(work / "integrated.h5ad")
    species = ad.uns["msp"].get("species")
    mk = pd.read_csv(work / "lineage_markers.csv", keep_default_na=False,
                     dtype={"lineage": str, "gene": str})
    markers = {g: mk.loc[mk["lineage"] == g, "gene"].tolist() for g in mk["lineage"].unique()}
    keys = [f"msp_leiden_r{r}" for r in ad.uns["msp"]["resolutions"] if r in (1.0, 2.0)]

    watch = _Watch()
    logging.getLogger().addHandler(watch)
    logging.getLogger().setLevel(logging.INFO)

    started = time.time()
    foreign_cols = score_foreign(ad, markers, call["lineage"], keys, str(work), str(figdir))
    annotate_lineage(
        ad, str(work), call["lineage"], call["lineage_labels"], call["other_labels"],
        foreign_cols, species=species, model=model,
    )
    elapsed = time.time() - started

    new = json.loads((work / "annotation_proposal.json").read_text())
    from collections import Counter

    return {
        "fixture": fixture.name,
        "model": model,
        "harness": os.environ.get("HARNESS", "openai"),
        "wall_s": round(elapsed, 1),
        "cost_usd": round(watch.cost, 4) or None,
        "contract": {
            "rejections": len(watch.rejections),
            "by_category": dict(Counter(c for c, _ in watch.rejections)),
            "messages": [m for _, m in watch.rejections][:10],
        },
        "recorded_contract": {
            "rejections": len(answer["host"]["rejections"]) if answer["host"]["log_found"] else None,
        },
        "agreement": agreement(new, answer["recorded_proposal"]),
    }


def main(argv: list[str]) -> int:
    model, work = None, None
    for flag in ("--model", "--work"):
        if flag in argv:
            i = argv.index(flag)
            value, argv = argv[i + 1], argv[:i] + argv[i + 2:]
            if flag == "--model":
                model = value
            else:
                work = value
    fixtures = [Path(a) for a in argv if not a.startswith("--")]
    if not fixtures or not model or not work:
        print(__doc__.strip().splitlines()[2], file=sys.stderr)
        return 64

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", stream=sys.stderr)
    out = []
    for f in fixtures:
        r = run(f.resolve(), Path(work) / f"{f.name}--{model}", model)
        out.append(r)
        print(json.dumps(r, indent=2, ensure_ascii=False))
    (Path(work) / f"scores-{model}.json").write_text(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

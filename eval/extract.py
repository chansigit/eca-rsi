"""Freeze one recorded lineage-annotation decision into a replayable fixture.

    python eval/extract.py <round>/zoomin/<Lineage> [<Lineage> ...] --out <fixtures dir>

A ZMIP lineage directory is already a self-contained exam paper: `integrated.h5ad` is
exactly the object `zmip.annotate.annotate_lineage()` was handed, and the diagnostic CSVs
beside it are exactly what the agent was allowed to read. A fixture is that directory with
the *answers* taken out and filed separately:

    <fixtures>/<name>/
        inputs/          integrated.h5ad + every diagnostic table, minus anything the
                         annotation step produced
        answer.json      how annotate_lineage was called, what the recorded model
                         submitted, and what the host said about it

Replay is then `annotate_lineage(read(inputs/integrated.h5ad), inputs/, **call)` under a
different MODEL, and scoring compares the new proposal with `answer.json`.

Fixtures are run products, so they live outside the repo (`--out`, or ECA_EVAL_FIXTURES).
Size is dominated by integrated.h5ad and scales with the lineage: a 6k-cell B cell lineage is
265 MiB, a 40k-cell Melanoma one is 2.2 GiB. Prefer small and medium lineages -- a suite whose
fixtures total tens of GiB is one nobody will copy. Keep them on Oak, not on scratch, or the
90-day purge will quietly delete the benchmark.

The recorded proposal is a *reference*, not a ground truth: it is what one model said on one
day, accepted by the host. Scoring against it measures agreement, and disagreement may mean
the new model is better. Only the host-checkable parts (schema, cell conservation, budget,
island connectivity) are pass/fail.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from pathlib import Path

# Everything annotate_lineage writes; the fixture must not contain the answer.
OUTPUTS = (
    "annotation_proposal.json", "annotation_removed.csv", "annotation_reassigned.csv",
    "annotated.h5ad", "report.html",
)


def call_args(lineage_dir: Path, plan: dict) -> dict:
    """Reconstruct the annotate_lineage(...) arguments from the plan the host validated.

    `foreign_cols` is deliberately not stored: score_foreign() names them obs["foreign_<lineage>"],
    so replay derives them from the frozen h5ad and cannot drift out of sync with it.
    """
    name = lineage_dir.name.replace("_", " ")
    entries = plan.get("lineages", [])
    mine = next((e for e in entries if _safe(e["name"]) == lineage_dir.name), None)
    if mine is None:
        raise SystemExit(f"{lineage_dir.name} is not in the plan (names: "
                         f"{[_safe(e['name']) for e in entries]})")
    every = sorted({lab for e in entries for lab in e.get("coarse_labels", [])})
    return {
        "lineage": mine["name"],
        "lineage_labels": list(mine.get("coarse_labels", [])),
        "other_labels": sorted(set(every) - set(mine.get("coarse_labels", []))),
        "coarse_col": plan.get("coarse_col"),
        "n_cells": mine.get("n_cells"),
        "zoom": mine.get("zoom"),
        "_dir_name": name,
    }


def _safe(name: str) -> str:
    """zmip's directory name for a lineage: the same transform lineage.py applies."""
    return re.sub(r"[^0-9A-Za-z._-]+", "_", name).strip("_")


def host_verdict(lineage: str, logs: list[Path]) -> dict:
    """What the host said to this lineage's agent, from the job logs.

    Lines are prefixed with the lineage label, so one log covering a whole round can be
    filtered down to a single decision. Absence of rejections is meaningful (accepted on
    the first submission); absence of the *label* means we simply have no log, which is
    not the same thing -- hence `log_found`.
    """
    tag, seen, found = f"[{lineage}]", [], False
    for log in logs:
        with log.open(errors="replace") as fh:
            for line in fh:
                if tag not in line:
                    continue
                found = True
                m = re.search(r"tool (error|exception) in (\w*)[:=] ?(.*)", line)
                if m:
                    seen.append({"tool": m.group(2), "message": m.group(3).strip()[:300]})
    return {"log_found": found, "rejections": seen}


def job_logs(lineage_dir: Path) -> list[Path]:
    """The Slurm logs for the run this lineage belongs to.

    Work happens on scratch but the job writes its log beside the Oak input directory, so
    walk up to the run root and follow `mirror.json` rather than guessing a parent depth.
    """
    for parent in Path(lineage_dir).resolve().parents:
        mirror = parent / "mirror.json"
        if mirror.is_file():
            oak = Path(json.loads(mirror.read_text())["mirror"])
            return sorted(oak.parent.glob("rsi-slurm-*.log"))
    return []


def extract(lineage_dir: Path, out_root: Path, logs: list[Path]) -> Path:
    lineage_dir = lineage_dir.resolve()
    proposal_path = lineage_dir / "annotation_proposal.json"
    if not proposal_path.is_file():
        raise SystemExit(f"{lineage_dir} has no annotation_proposal.json — nothing to freeze")
    plan = json.loads((lineage_dir.parent / "zmip_plan.json").read_text())
    proposal = json.loads(proposal_path.read_text())
    call = call_args(lineage_dir, plan)

    # <batch>/<organ>/.../roundNN/zoomin/<Lineage> -> a name that stays unique across runs
    parts = lineage_dir.parts
    rnd = next((p for p in parts if re.fullmatch(r"round\d+", p)), "round??")  # not "rounds"
    unit = parts[parts.index("units") + 1] if "units" in parts else "unit"
    name = f"{unit}-{rnd}-{lineage_dir.name}"

    dest = out_root / name
    inputs = dest / "inputs"
    inputs.mkdir(parents=True, exist_ok=True)
    for src in sorted(lineage_dir.iterdir()):
        if src.name in OUTPUTS or src.name == "figures":
            continue
        if src.is_dir():
            shutil.copytree(src, inputs / src.name, dirs_exist_ok=True)
        else:
            shutil.copy2(src, inputs / src.name)

    (dest / "answer.json").write_text(json.dumps({
        "source": str(lineage_dir),
        "call": call,
        "recorded_proposal": proposal,
        "host": host_verdict(call["lineage"], logs),
        "note": "recorded_proposal is a reference, not ground truth: one model, one day, "
                "accepted by the host. Compare for agreement; only host-checkable rules are pass/fail.",
    }, indent=2, ensure_ascii=False))
    return dest


def main(argv: list[str]) -> int:
    out = os.environ.get("ECA_EVAL_FIXTURES")
    if "--out" in argv:
        i = argv.index("--out")
        out, argv = argv[i + 1], argv[:i] + argv[i + 2:]
    if not out:
        print("need --out <fixtures dir> or ECA_EVAL_FIXTURES", file=sys.stderr)
        return 64
    dirs = [Path(a) for a in argv if not a.startswith("--")]
    if not dirs:
        print(__doc__.strip().splitlines()[2], file=sys.stderr)
        return 64
    logs = job_logs(dirs[0])
    if not logs:
        print("warning: no rsi-slurm-*.log found; answer.json will record log_found=false",
              file=sys.stderr)
    for d in dirs:
        dest = extract(d, Path(out), logs)
        answer = json.loads((dest / "answer.json").read_text())
        n = len(answer["host"]["rejections"])
        size = sum(f.stat().st_size for f in (dest / "inputs").rglob("*") if f.is_file())
        print(f"{dest}  clusters={len(answer['recorded_proposal'].get('clusters', []))} "
              f"rejections={n if answer['host']['log_found'] else 'no log'}  {size / 2**30:.1f} GiB")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

"""ecarsi-crosssample — msp wrapper: sample inclusion, then cross-sample integration.

    python -m ecarsi.crosssample <unit_dir> [round_dir]

Round 1 of the loop; standalone it writes <unit>/rounds/round01 (the loop
passes the round dir explicitly). Runs after ecarsi.persample (which must have completed WITH annotation for
every sample — hard prerequisite). Stages:

  1. RESOLVE (code): samples, batch key, species from persample's manifest.
     The batch key is persample's sample column — when eca-pp's
     identify_columns designated a different batch column, persample wins
     and the eca-pp designation is archived alongside for the audit trail.
  2. INCLUDE (agent, structured output; skipped when there is exactly one
     sample — it is included as is): reads every sample's QC summary,
     annotation proposal AND its UMAP/QC figures; proposes which samples
     enter integration. Whole-sample exclusion is the rare exception (much
     worse than peers, fragmented unexplainable structure); different
     biology is never a reason. The decision is archived in manifest.json
     and reused on resume. Excluded samples stay on disk untouched.
  3. EXECUTE (code): runs the full msp chain (multi-sample pipeline
     package) on the included samples' clustered.h5ad files —
     `python -m msp ... --annotate --model $MODEL`: integrate (concat,
     normalize from raw counts, HVG/PCA recomputed on the merged cells,
     harmony on the batch key) → inspect (per-cluster QC verdict agent,
     proposals only) → annotate (cell-identity agent: coarse/fine labels,
     merges, REAL removal). msp resumes per step on its own, so re-running
     this command finishes whatever step was cut short. Contract:
     <round>/crosssample/{integrated.h5ad, report.html, inspection_proposal.json,
     annotation_proposal.json, annotated.h5ad}.

Single sample (persample found no run column, or the inclusion agent kept
only one): the same chain runs, msp just skips harmony (X_pca_harmony =
X_pca) and its agents are told that sample composition carries no evidence.

Cells enter integration exactly as osp left them: QC-passed cells only,
including every keep/flag/drop cell from the annotation (suspicious cells
may cluster with counterparts from other samples; cluster-level review
happens after integration). The first actual deletion is msp.annotate's:
integrated.h5ad keeps every cell, annotated.h5ad the survivors, and
annotation_removed.csv lists each removed cell with its sources.

Env: MODEL (every agent call, incl. msp's inspect/annotate), MSP_PYTHON
(interpreter with msp[agent] installed; defaults to this interpreter).
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

from . import layout as L

INCLUSION_SCHEMA = {
    "type": "object",
    "properties": {
        "samples": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "sample": {"type": "string"},
                    "include": {"type": "boolean"},
                    "reason": {"type": "string"},
                },
                "required": ["sample", "include", "reason"],
            },
        },
        "notes": {"type": "string"},
    },
    "required": ["samples", "notes"],
}

# what a completed persample sample dir must contain (annotation is a hard
# prerequisite for crosssample: the inclusion agent judges by it)
PS_CONTRACT = L.PS_ANNOTATE_CONTRACT + ("qc_summary.csv",)

MSP_CONTRACT = L.MSP_CONTRACT


# ---------------------------------------------------------------- resolve


def load_persample(unit: Path) -> dict:
    """Persample manifest with per-sample dirs; hard-stop if any sample is
    missing its contract files — crosssample requires persample complete
    with annotation."""
    mpath = L.persample_manifest(unit)
    if not mpath.is_file():
        raise SystemExit(f"no persample manifest at {mpath} — run ecarsi.persample first")
    with open(mpath) as f:
        man = json.load(f)
    if any("dir" not in s for s in man["samples"]):
        raise SystemExit(
            "persample manifest predates the 'dir' field — re-run ecarsi.persample "
            "(--plan-only is enough) to refresh it"
        )
    for s in man["samples"]:  # located under this unit, whatever the manifest recorded
        s["dir"] = str(L.sample_dir(unit, s))
    incomplete = [s["value"] for s in man["samples"]
                  if not all((Path(s["dir"]) / f).is_file() for f in PS_CONTRACT)]
    if incomplete:
        raise SystemExit(
            "persample (with annotation) is a hard prerequisite; incomplete samples: "
            + ", ".join(incomplete)
        )
    return man


def ecapp_batch_designations(unit: Path) -> dict:
    """eca-pp identify_columns batch designations per source unit, from the
    organize global manifest — archived for the audit trail; persample's
    sample column wins any conflict."""
    root = L.root_of(unit)
    out: dict = {}
    if root is None:
        return out
    gm = L.organize_manifest(root)
    if not gm.is_file():
        return out
    with open(gm) as f:
        g = json.load(f)
    for u in g.get("input_units", []):
        icr = u.get("identify_columns_result")
        if icr and Path(icr).is_file():
            with open(icr) as f:
                batch = (json.load(f).get("columns") or {}).get("batch")
            if isinstance(batch, dict):
                out[u["name"]] = batch
    return out


# ---------------------------------------------------------------- include


def _sample_inventory(s: dict) -> dict:
    d = Path(s["dir"])
    inv: dict = {"sample": s["value"], "n_cells_input": s["n_cells"], "dir": str(d)}

    with open(d / "qc_summary.csv") as f:
        rows = list(csv.reader(f))
    inv["qc_summary"] = {r[0]: r[1] for r in rows if len(r) == 2 and r[0]}

    with open(d / "annotation_proposal.json") as f:
        prop = json.load(f)
    inv["annotation"] = {
        "clusters": [
            {
                "cluster": c.get("cluster"),
                "label_coarse": c.get("label_coarse"),
                "confidence": c.get("confidence"),
                "doubts": (c.get("doubts") or "")[:300],
            }
            for c in prop.get("clusters", [])
        ],
        "qc_actions": prop.get("qc_actions", []),
    }

    figs = sorted(str(p) for p in d.glob("figures/*.png")) + sorted(
        str(p) for p in d.glob("qc_figures/*.png")
    )
    inv["figures"] = figs
    return inv


SINGLE_SAMPLE_NOTE = "single sample — inclusion agent not consulted; harmony is skipped downstream"


def propose_inclusion(inventories: list[dict]) -> dict:
    """One sample: nothing to weigh against, include it without a session.
    Two or more: the inclusion agent decides."""
    if len(inventories) == 1:
        return {"samples": [{"sample": inventories[0]["sample"], "include": True, "reason": SINGLE_SAMPLE_NOTE}],
                "notes": SINGLE_SAMPLE_NOTE}
    from .agent_retry import run_with_retry

    # coverage validation lives inside the retried coroutine too: an agent
    # reply that drops/duplicates a sample is the same kind of transient
    # malformed output as a dropped connection, and is just as safe to retry
    # (no partial state, side-effect-free proposal call).
    async def _propose_validated() -> dict:
        result = await _propose(inventories)
        got = [e["sample"] for e in result["samples"]]
        want = [i["sample"] for i in inventories]
        if sorted(got) != sorted(want) or len(got) != len(set(got)):
            raise ValueError(f"inclusion decision must cover every sample exactly once: got {got}, want {want}")
        return result

    return run_with_retry(_propose_validated, label="sample inclusion")


async def _propose(inventories: list[dict]) -> dict:
    from claude_agent_sdk import ClaudeAgentOptions, query

    from . import model

    brief = (Path(__file__).parent / "prompts" / "sample_inclusion.md").read_text()
    prompt = (
        brief
        + "\n\n## Sample inventories\n\n```json\n"
        + json.dumps(inventories, indent=1)
        + "\n```\n"
    )
    options = ClaudeAgentOptions(
        model=model(),
        allowed_tools=["Read", "Glob", "Grep"],  # it must Read the figures
        max_turns=80,
        max_buffer_size=50_000_000,  # figure Reads can exceed the 1MB default
        output_format={"type": "json_schema", "schema": INCLUSION_SCHEMA},
    )
    result = None
    async for msg in query(prompt=prompt, options=options):
        so = getattr(msg, "structured_output", None)
        if so is not None:
            result = so
    if result is None:
        raise RuntimeError("inclusion agent ended without structured output")
    return result


# ---------------------------------------------------------------- execute


def msp_command(py: str, inputs: list[str], batch_col: str, outdir: Path,
                species: str | None, model: str, context: str | None = None) -> str:
    """Full msp chain; --annotate implies --inspect. msp skips steps whose
    contract files exist, so this same command is also the resume command."""
    cmd = [py, "-m", "msp", *inputs, "--batch-col", batch_col, "--outdir", str(outdir),
           "--annotate", "--model", model]
    if species:
        cmd += ["--species", species]
    if context:
        cmd += ["--report-context", context]
    return " ".join(shlex.quote(c) for c in cmd)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="ecarsi.crosssample", description=__doc__)
    ap.add_argument("unit", help="organize unit dir (persample/ completed inside)")
    ap.add_argument("out", nargs="?", help="round dir (default <unit>/rounds/round01)")
    args = ap.parse_args(argv)

    unit = Path(args.unit).resolve()
    ps = load_persample(unit)
    out_root = Path(args.out).resolve() if args.out else L.round_dir(unit, 1)
    # a null sample column means persample ran the whole file as one sample;
    # osp then labels every cell obs["sample"] = "all", which is the batch key
    batch_col = ps["sample_column"] or "sample"
    species = ps.get("species")

    ecapp = ecapp_batch_designations(unit)
    for src, b in ecapp.items():
        if b.get("value") != batch_col:
            print(f"[batch] eca-pp designated {b.get('value')!r} for {src}; "
                  f"persample's {batch_col!r} wins (archived)")

    mpath = out_root / "manifest.json"
    if mpath.is_file():
        with open(mpath) as f:
            man = json.load(f)
        decision = man["inclusion"]
        print("[include] reusing recorded inclusion decision")
    else:
        inventories = [_sample_inventory(s) for s in ps["samples"]]
        decision = propose_inclusion(inventories)
        man = {
            "unit": str(unit),
            "batch_col": batch_col,
            "species": species,
            "ecapp_batch_designations": ecapp,
            "inclusion": decision,
        }
        out_root.mkdir(parents=True, exist_ok=True)
        with open(mpath, "w") as f:
            json.dump(man, f, indent=2)

    by_val = {s["value"]: s for s in ps["samples"]}
    included = [e["sample"] for e in decision["samples"] if e["include"]]
    excluded = [(e["sample"], e["reason"]) for e in decision["samples"] if not e["include"]]
    for sample, reason in excluded:
        print(f"[exclude] {sample}: {reason}")
    print(f"[include] {len(included)}/{len(decision['samples'])} samples enter integration")
    if not included:
        print("[fail] no sample left to integrate")
        return 4
    if len(included) == 1:
        print("[include] single sample: msp runs the same chain without harmony (integrate → inspect → annotate)")

    py = os.environ.get("MSP_PYTHON", sys.executable)
    inputs = [str(Path(by_val[s]["dir"]) / "clustered.h5ad") for s in included]
    idir = L.crosssample_dir(out_root)
    from . import model

    cmd = msp_command(py, inputs, batch_col, idir, species, model(), L.report_context(unit, out_root))
    print(f"[msp] {cmd}")

    # msp's report reads sample_decisions.csv if present — write it before
    # msp runs so its own generate_report() call at the end picks it up
    idir.mkdir(parents=True, exist_ok=True)
    with open(idir / "sample_decisions.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["sample", "decision", "n_cells", "reason"])
        for e in decision["samples"]:
            n = by_val[e["sample"]]["n_cells"]
            w.writerow([e["sample"], "include" if e["include"] else "exclude", n, e["reason"]])

    if all((idir / f).is_file() for f in MSP_CONTRACT):
        print("[msp] contract already satisfied — skipping (resume)")
        return 0
    done = [f for f in MSP_CONTRACT if (idir / f).is_file()]
    if done:
        print(f"[msp] partial contract {done} — msp resumes the remaining steps")
    probe = subprocess.run([py, "-c", "import msp, msp.inspect, msp.annotate"], capture_output=True)
    if probe.returncode != 0:
        print("[pending] msp[agent] not importable in MSP_PYTHON (needs msp + claude-agent-sdk) — "
              "inclusion decision archived, re-run once installed")
        return 4

    ret = subprocess.run(cmd, shell=True).returncode
    if ret != 0:
        print(f"[fail] msp exited {ret}")
        return 1
    missing = [f for f in MSP_CONTRACT if not (idir / f).is_file()]
    if missing:
        print(f"[fail] msp exited 0 but contract files missing: {missing}")
        return 1
    print(f"[done] integration + inspection + annotation at {idir} (annotated.h5ad = survivors)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

"""ecarsi-crosssample — msp wrapper: sample inclusion, then cross-sample integration.

    python -m ecarsi.crosssample <unit_dir> [out_dir]

Runs after ecarsi.persample (which must have completed WITH annotation for
every sample — hard prerequisite). Stages:

  1. RESOLVE (code): samples, batch key, species from persample's manifest.
     The batch key is persample's sample column — when eca-pp's
     identify_columns designated a different batch column, persample wins
     and the eca-pp designation is archived alongside for the audit trail.
  2. INCLUDE (agent, structured output): reads every sample's QC summary,
     annotation proposal AND its UMAP/QC figures; proposes which samples
     enter integration. Whole-sample exclusion is the rare exception (much
     worse than peers, fragmented unexplainable structure); different
     biology is never a reason. The decision is archived in manifest.json
     and reused on resume. Excluded samples stay on disk untouched.
  3. EXECUTE (code): runs msp (multi-sample pipeline package) on the
     included samples' clustered.h5ad files — concat, normalize from raw
     counts, HVG/PCA recomputed on the merged cells, harmony on the batch
     key. Contract: <out>/integrate/integrated.h5ad + report.html.

Cells enter integration exactly as osp left them: QC-passed cells only,
including every keep/flag/drop cell from the annotation (suspicious cells
may cluster with counterparts from other samples; cluster-level review
happens after integration). Cell deletion is a later, separate step.

Env: MODEL (agent call), MSP_PYTHON (interpreter with msp installed;
defaults to this interpreter).
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

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
PS_CONTRACT = ("report.html", "clustered.h5ad", "annotation_proposal.json")

MSP_CONTRACT = ("integrated.h5ad", "report.html")


# ---------------------------------------------------------------- resolve


def load_persample(unit: Path) -> dict:
    """Persample manifest with per-sample dirs; hard-stop if any sample is
    missing its contract files — crosssample requires persample complete
    with annotation."""
    mpath = unit / "persample" / "manifest.json"
    if not mpath.is_file():
        raise SystemExit(f"no persample manifest at {mpath} — run ecarsi.persample first")
    with open(mpath) as f:
        man = json.load(f)
    if any("dir" not in s for s in man["samples"]):
        raise SystemExit(
            "persample manifest predates the 'dir' field — re-run ecarsi.persample "
            "(--plan-only is enough) to refresh it"
        )
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
    gm = unit.parent / "manifest.json"
    out: dict = {}
    if not gm.is_file():
        return out
    with open(gm) as f:
        g = json.load(f)
    for u in g.get("input_units", []):
        icr = u.get("identify_columns_result")
        if icr and Path(icr).is_file():
            with open(icr) as f:
                batch = (json.load(f).get("columns") or {}).get("batch")
            if batch:
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


def propose_inclusion(inventories: list[dict]) -> dict:
    result = asyncio.run(_propose(inventories))
    got = [e["sample"] for e in result["samples"]]
    want = [i["sample"] for i in inventories]
    if sorted(got) != sorted(want) or len(got) != len(set(got)):
        raise ValueError(f"inclusion decision must cover every sample exactly once: got {got}, want {want}")
    return result


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
                species: str | None) -> str:
    cmd = [py, "-m", "msp", *inputs, "--batch-col", batch_col, "--outdir", str(outdir)]
    if species:
        cmd += ["--species", species]
    return " ".join(shlex.quote(c) for c in cmd)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="ecarsi.crosssample", description=__doc__)
    ap.add_argument("unit", help="organize unit dir (persample/ completed inside)")
    ap.add_argument("out", nargs="?", help="output root (default <unit>/crosssample)")
    args = ap.parse_args(argv)

    unit = Path(args.unit).resolve()
    ps = load_persample(unit)
    out_root = Path(args.out).resolve() if args.out else unit / "crosssample"
    batch_col = ps["sample_column"]
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
    if len(included) < 2:
        print("[fail] integration needs at least 2 included samples")
        return 4

    py = os.environ.get("MSP_PYTHON", sys.executable)
    inputs = [str(Path(by_val[s]["dir"]) / "clustered.h5ad") for s in included]
    idir = out_root / "integrate"
    cmd = msp_command(py, inputs, batch_col, idir, species)
    print(f"[msp] {cmd}")

    if all((idir / f).is_file() for f in MSP_CONTRACT):
        print("[msp] contract already satisfied — skipping (resume)")
        return 0
    probe = subprocess.run([py, "-c", "import msp"], capture_output=True)
    if probe.returncode != 0:
        print("[pending] msp package not installed yet — inclusion decision archived, "
              "re-run once msp exists")
        return 4

    ret = subprocess.run(cmd, shell=True).returncode
    if ret != 0:
        print(f"[fail] msp exited {ret}")
        return 1
    missing = [f for f in MSP_CONTRACT if not (idir / f).is_file()]
    if missing:
        print(f"[fail] msp exited 0 but contract files missing: {missing}")
        return 1
    print(f"[done] integration at {idir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

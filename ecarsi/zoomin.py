"""ecarsi-zoomin — zmip wrapper: per-lineage zoom-in after cross-sample integration.

    python -m ecarsi.zoomin <unit_dir> [out_dir]

Runs after ecarsi.crosssample (whose msp contract must be complete —
integrated + inspected + annotated). Stages, all inside zmip:

  1. PLAN (agent): reads the coarse-annotation UMAP + kNN/PAGA connectivity
     and pools coarse labels into UMAP-connected lineages; zooms only those
     with >= ZMIP_MIN_CELLS cells (default 800). Archived in zmip_plan.json.
  2. ZOOM (per lineage, sequential): subset → re-embed (msp.integrate_adata)
     → foreign-lineage scores → annotation agent (refine fine labels,
     remove noise, reassign to another lineage, recluster) → per-lineage
     report.
  3. MERGE (code): annotated_zmip.h5ad (real removal; zmip_ann_coarse /
     zmip_ann_fine / zmip_lineage / zmip_cluster), zmip_removed.csv,
     zmip_reassigned.csv, global report.html.

Contract: <out>/zoomin/{zmip_plan.json, annotated_zmip.h5ad, report.html}.
zmip resumes per lineage, so re-running this command finishes a cut-short
run. No global re-embedding here — that is the next round's job.

Env: MODEL (every agent call), ZMIP_PYTHON (interpreter with zmip installed;
falls back to MSP_PYTHON, then this interpreter), ZMIP_MIN_CELLS.
"""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
from pathlib import Path

from .crosssample import MSP_CONTRACT

ZMIP_CONTRACT = ("zmip_plan.json", "annotated_zmip.h5ad", "report.html")


def zmip_command(py: str, h5ad: Path, outdir: Path, model: str, min_cells: str | None) -> str:
    cmd = [py, "-m", "zmip", str(h5ad), "--outdir", str(outdir), "--model", model]
    if min_cells:
        cmd += ["--min-cells", str(min_cells)]
    return " ".join(shlex.quote(c) for c in cmd)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="ecarsi.zoomin", description=__doc__)
    ap.add_argument("unit", help="organize unit dir (crosssample/ completed inside)")
    ap.add_argument("out", nargs="?", help="output root (default <unit>/crosssample)")
    args = ap.parse_args(argv)

    unit = Path(args.unit).resolve()
    out_root = Path(args.out).resolve() if args.out else unit / "crosssample"
    idir = out_root / "integrate"
    missing = [f for f in MSP_CONTRACT if not (idir / f).is_file()]
    if missing:
        print(f"[fail] crosssample msp contract incomplete in {idir}: missing {missing} — "
              "run ecarsi.crosssample first")
        return 3

    zdir = out_root / "zoomin"
    py = os.environ.get("ZMIP_PYTHON") or os.environ.get("MSP_PYTHON") or sys.executable
    from . import model

    cmd = zmip_command(py, idir / "annotated.h5ad", zdir, model(), os.environ.get("ZMIP_MIN_CELLS"))
    print(f"[zmip] {cmd}")
    if all((zdir / f).is_file() for f in ZMIP_CONTRACT):
        print("[zmip] contract already satisfied — skipping (resume)")
        return 0
    done = [f for f in ZMIP_CONTRACT if (zdir / f).is_file()]
    if done:
        print(f"[zmip] partial contract {done} — zmip resumes finished lineages")
    probe = subprocess.run([py, "-c", "import zmip, msp"], capture_output=True)
    if probe.returncode != 0:
        print("[pending] zmip not importable in ZMIP_PYTHON (needs zmip + msp + claude-agent-sdk) — "
              "re-run once installed")
        return 4

    ret = subprocess.run(cmd, shell=True).returncode
    if ret != 0:
        print(f"[fail] zmip exited {ret}")
        return 1
    missing = [f for f in ZMIP_CONTRACT if not (zdir / f).is_file()]
    if missing:
        print(f"[fail] zmip exited 0 but contract files missing: {missing}")
        return 1
    print(f"[done] zoom-in at {zdir} (annotated_zmip.h5ad = survivors with zmip_ann_* labels)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

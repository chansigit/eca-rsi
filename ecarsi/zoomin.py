"""ecarsi-zoomin — zmip wrapper: per-lineage zoom-in after cross-sample integration.

    python -m ecarsi.zoomin <unit_dir> [round_dir]

Runs after ecarsi.crosssample in the same round dir (default: the latest
<unit>/rounds/roundNN; its msp contract must be complete — integrated +
inspected + annotated). Stages, all inside zmip:

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

Contract: <round>/zoomin/{zmip_plan.json, annotated_zmip.h5ad, report.html}.
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

from . import downstream as D
from . import cost
from . import layout as L

MSP_CONTRACT = L.MSP_CONTRACT
ZMIP_CONTRACT = L.ZMIP_CONTRACT


def zmip_command(py: str, h5ad: Path, outdir: Path, model: str, min_cells: str | None,
                 context: str | None = None) -> str:
    cmd = [py, "-m", "zmip", str(h5ad), "--outdir", str(outdir), "--model", model]
    cmd += D.options("zmip")
    if min_cells and not os.environ.get("ZMIP_MIN_CELLS"):
        cmd += ["--min-cells", str(min_cells)]
    if context:
        cmd += ["--report-context", context]
    return " ".join(shlex.quote(c) for c in cmd)


@D.locked_unit
def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="ecarsi.zoomin", description=__doc__)
    ap.add_argument("unit", help="organize unit dir (a round's crosssample/ completed inside)")
    ap.add_argument("out", nargs="?", help="round dir (default: the latest <unit>/rounds/roundNN)")
    args = ap.parse_args(argv)

    unit = Path(args.unit).resolve()
    if args.out:
        out_root = Path(args.out).resolve()
    else:
        existing = L.rounds(unit)
        out_root = existing[-1] if existing else L.round_dir(unit, 1)
    manifest = out_root / L.MANIFEST
    if manifest.is_file():
        import json

        from . import check_agent_config

        with open(manifest) as fh:
            check_agent_config(json.load(fh), str(manifest))
    idir = L.crosssample_dir(out_root)
    missing = [f for f in MSP_CONTRACT if not (idir / f).is_file()]
    if missing:
        print(f"[fail] crosssample msp contract incomplete in {idir}: missing {missing} — "
              "run ecarsi.crosssample first")
        return 3

    zdir = L.zoomin_dir(out_root)
    py = os.environ.get("ZMIP_PYTHON") or os.environ.get("MSP_PYTHON") or sys.executable
    from . import model

    cmd = zmip_command(py, idir / "annotated.h5ad", zdir, model(), os.environ.get("ZMIP_MIN_CELLS"),
                       L.report_context(unit, out_root))
    print(f"[zmip] {cmd}")
    if list((idir / ".msp-state").glob("*.pending")):
        raise ValueError("MSP has an unfinished step")
    mstate = D.read_json(idir / D.STATE)
    if mstate.get("state") != "complete":
        raise ValueError("MSP has no verified RSI completion state")
    if any(D.file_identity(idir / name) != ident for name, ident in mstate["validation"]["outputs"].items()):
        raise ValueError("MSP output changed since validation")
    identity = D.prepare(py, "zmip", [idir / "annotated.h5ad"], zdir, {"options": D.options("zmip")})
    done = [f for f in ZMIP_CONTRACT if (zdir / f).is_file()]
    if done:
        print(f"[zmip] partial contract {done} — zmip resumes finished lineages")
    probe = subprocess.run([py, "-c", "import zmip, msp"], capture_output=True)
    if probe.returncode != 0:
        print("[pending] zmip not importable in ZMIP_PYTHON (needs compatible zmip, msp and the selected bridge backend) — "
              "re-run once installed")
        return 4

    ret = cost.run_streamed(cmd, unit, f"{out_root.name}/{L.ZOOMIN}")
    if ret != 0:
        print(f"[fail] zmip exited {ret}")
        return 1
    missing = [f for f in ZMIP_CONTRACT if not (zdir / f).is_file()]
    if missing:
        print(f"[fail] zmip exited 0 but contract files missing: {missing}")
        return 1
    D.verify(py, "zmip", [idir / "annotated.h5ad"], zdir, identity)
    print(f"[done] zoom-in at {zdir} (annotated_zmip.h5ad = survivors with zmip_ann_* labels)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

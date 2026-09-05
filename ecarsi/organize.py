#!/usr/bin/env python
"""ecarsi-organize — gatekeeper + h5ad organizer, runs once before the loop.

    python -m ecarsi.organize <input-folder> <out-root>

Three stages:
  1. DETECT (pure rules, no model): every h5ad in the input folder must be an
     eca-pp product (`.../standardize/standardized.h5ad` next to its
     `result.json`). Any bare h5ad → hard stop: run eca-pp first.
  2. PLAN (agent, structured output): given per-file obs/metadata profiles,
     the agent proposes analysis units — merge samples/compartments of one
     study; split ONLY on obs-metadata evidence of organ-scale difference
     (blood vs tumor splits; left vs right lung does not). No human gate:
     the plan executes immediately and lands in the manifest for audit.
  3. EXECUTE (pure code): build one directory per unit with the merged
     h5ad (provenance column = source directory name) + manifest.json.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# ---------------------------------------------------------------- detection


def find_ecapp_units(root: Path) -> tuple[list[dict], list[Path]]:
    from .upstream import discover

    return discover(root)


# ---------------------------------------------------------------- profiling


def profile_unit(unit: dict, max_levels: int = 30) -> dict:
    """Cheap obs/metadata profile the planning agent reasons over.

    Reads upstream result.json verbatim (species, counts, cell numbers) and
    the h5ad obs in backed mode: for every low-cardinality categorical
    column, its value counts — organ/tissue/compartment structure lives
    there, and obs metadata is the ONLY sanctioned evidence for splitting.
    """
    import anndata as ad

    prof = {"name": unit["name"], "h5ad": unit["h5ad"]}
    with open(unit["standardize_result"]) as f:
        std = json.load(f)
    from .upstream import load_evidence, validate_matrix

    prof["species"] = (std.get("species") or {}).get("resolved")
    prof["n_cells"] = (std.get("metrics") or {}).get("n_cells")
    prof["n_vars"] = (std.get("metrics") or {}).get("n_vars")

    a = ad.read_h5ad(unit["h5ad"], backed="r")
    validate_matrix(a, std)
    evidence, _, _ = load_evidence(unit, a.obs)
    prof["upstream_evidence"] = evidence
    prof["upstream_review"] = std.get("reasons", [])
    cols = {}
    for c in a.obs.columns:
        s = a.obs[c]
        nuniq = s.nunique(dropna=True)
        entry: dict = {"dtype": str(s.dtype), "n_unique": int(nuniq)}
        if nuniq <= max_levels and (s.dtype == object or str(s.dtype) == "category"):
            # drop unused categorical levels — phantom zero counts would
            # pollute the profile the agent reasons over
            entry["value_counts"] = {str(k): int(v) for k, v in s.value_counts().items() if v}
        cols[str(c)] = entry
    prof["obs_columns"] = cols
    prof["n_obs"] = int(a.n_obs)
    a.file.close()
    return prof


# ---------------------------------------------------------------- cli


def main(argv: list[str]) -> int:
    import argparse
    from . import layout as L
    from .run_state import digest, file_identity, read_json, writer_lock, write_json
    from .upstream import inspect_unit, verify_snapshots

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("input")
    ap.add_argument("out")
    ap.add_argument("--plan-json", help="explicit analysis-unit plan; skips the planning agent")
    args = ap.parse_args(argv)
    root, out_root = Path(args.input).resolve(), Path(args.out).resolve()
    if not root.is_dir() or out_root == root or root in out_root.parents:
        print("input must exist and output must be outside the input tree")
        return 3
    try:
        with writer_lock(out_root / L.ORGANIZE / ".writer.lock"):
            units, violations = find_ecapp_units(root)
            records, problems = [], [f"undeclared H5AD: {p}" for p in violations]
            for u in units:
                try:
                    records.append(inspect_unit(u))
                except (ValueError, OSError, KeyError) as exc:
                    records.append({**u, "state": "blocked", "error": str(exc)})
                    problems.append(f"{u['name']}: {exc}")
            write_json(out_root / L.ORGANIZE / "source_inventory.json", {"sources": records, "problems": problems})
            if problems:
                raise ValueError("input set is not ready: " + "; ".join(problems))
            accepted = [r for r in records if r["state"] == "accepted"]
            if not accepted:
                raise ValueError("no accepted ECA-PP sources (see source_inventory.json)")
            identity = digest([{
                **{k: r.get(k) for k in ("name", "state", "files")},
                "derived": {k: f["identity"] for k, f in r.get("derived_files", {}).items()},
            } for r in records])
            gm = L.organize_manifest(out_root)
            package = Path(__file__).parent
            adapter = digest({name: file_identity(package / name) for name in (
                "organize.py", "execute.py", "upstream.py", "plan.py", "run_state.py", "prompts/plan.md")})
            old = read_json(gm) if gm.is_file() else None
            if old:
                if (old.get("schema_version") != 2 or old.get("input_identity") != identity
                        or old.get("adapter_identity") != adapter):
                    raise ValueError("organize input/adapter changed or legacy manifest has no verified identity; use a new output root")
                if args.plan_json and read_json(Path(args.plan_json)) != old["plan"]:
                    raise ValueError("organize plan changed; use a new output root")
                plan, profiles = old["plan"], old["profiles"]
                if old.get("state") == "complete":
                    expected = {u["name"] for u in plan["analysis_units"]}
                    if expected != {u["name"] for u in old["units_written"]}:
                        raise ValueError("organize completion record does not cover its plan")
                    for item in old["units_written"]:
                        unit = L.unit_dir(out_root, item["name"])
                        if file_identity(L.input_h5ad(unit)) != item["identity"]:
                            raise ValueError(f"organized input changed: {unit}")
                        if file_identity(L.input_manifest(unit)) != item["manifest_identity"]:
                            raise ValueError(f"unit manifest changed: {unit}")
                        verify_snapshots(L.input_manifest(unit).parent, read_json(L.input_manifest(unit)))
                    print("[organize] all planned units verified; resuming")
                    from .index import write_all
                    write_all(out_root)
                    return 0
            else:
                if L.units(out_root):
                    raise ValueError("existing units without a resumable organize record; use a new output root")
                profiles = [profile_unit(u) for u in accepted]
                from .plan import propose_plan
                plan = read_json(Path(args.plan_json)) if args.plan_json else propose_plan(profiles)
            from .execute import execute_plan
            execute_plan(accepted, profiles, plan, out_root, records=records, input_identity=identity,
                         adapter_identity=adapter)
        return 0
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"[organize] {exc}")
        return 3


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

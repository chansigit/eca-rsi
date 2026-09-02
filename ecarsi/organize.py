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
    """Return (units, violations).

    A unit is a directory `d` holding `d/standardize/standardized.h5ad` and
    `d/standardize/result.json` — its identity is `d`'s name (eca-pp names
    every file standardized.h5ad, so filenames cannot distinguish samples).
    The input root itself may be a unit (single-sample layout).
    Violations are h5ad files not living inside any such unit.
    """
    units: list[dict] = []
    claimed: set[Path] = set()

    candidates = [root] + sorted(p for p in root.rglob("standardize") if p.is_dir())
    for cand in candidates:
        d = cand if cand is root else cand.parent
        h5 = d / "standardize" / "standardized.h5ad"
        rj = d / "standardize" / "result.json"
        if h5.is_file() and rj.is_file() and h5 not in claimed:
            # root-as-unit: staged layouts are often <dataset>/input, where
            # "input" identifies nothing — climb to the dataset directory name
            root_name = root.parent.name if root.name in ("input", "data") else root.name
            unit = {
                "name": root_name if d == root else d.name,
                "dir": str(d),
                "h5ad": str(h5),
                "standardize_result": str(rj),
            }
            ic = d / "identify_columns" / "result.json"
            if ic.is_file():
                unit["identify_columns_result"] = str(ic)
            units.append(unit)
            claimed.add(h5)

    violations = [p for p in sorted(root.rglob("*.h5ad")) if p not in claimed]

    # unit identity is name-only downstream (execute.py keys units_by_name by
    # name) — two source dirs sharing a basename would silently collapse into
    # one, dropping a whole source from the plan with no error raised
    seen: dict[str, str] = {}
    for u in units:
        if u["name"] in seen:
            raise SystemExit(
                f"duplicate unit name {u['name']!r}: {seen[u['name']]} and {u['dir']} "
                "both resolve to it — rename one of the source directories"
            )
        seen[u["name"]] = u["dir"]

    return units, violations


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
    prof["species"] = (std.get("species") or {}).get("resolved")
    prof["n_cells"] = (std.get("metrics") or {}).get("n_cells")
    prof["n_vars"] = (std.get("metrics") or {}).get("n_vars")

    a = ad.read_h5ad(unit["h5ad"], backed="r")
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
    if len(argv) != 2:
        print(__doc__)
        return 2
    root, out_root = Path(argv[0]).resolve(), Path(argv[1]).resolve()

    units, violations = find_ecapp_units(root)
    if violations:
        print("These h5ad files are not eca-pp products — run eca-pp on them first:")
        for p in violations:
            print(f"  {p}")
        print("(expected layout: <dataset-or-sample>/standardize/standardized.h5ad + result.json)")
        return 3
    if not units:
        print(f"no eca-pp units found under {root}")
        return 3

    print(f"[detect] {len(units)} eca-pp unit(s): " + ", ".join(u["name"] for u in units))
    profiles = [profile_unit(u) for u in units]

    from .plan import propose_plan  # agent call, structured output

    plan = propose_plan(profiles)

    from .execute import execute_plan  # deterministic merge/split + manifest

    execute_plan(units, profiles, plan, out_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

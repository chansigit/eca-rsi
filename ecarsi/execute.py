"""Deterministic executor for the organize plan — the agent proposes, this runs.

Output layout (one directory per analysis unit, ready to hand to the loop):

    <out_root>/
      manifest.json                  # global: detection, profiles, plan, warnings
      <unit_name>/input/
        organized.h5ad               # merged (+ filtered) cells, provenance in obs
        manifest.json                # this unit's slice of the plan
"""

from __future__ import annotations

import json
from pathlib import Path


def _load_member(units_by_name: dict, member: dict):
    import anndata as ad

    src = units_by_name[member["source"]]
    a = ad.read_h5ad(src["h5ad"])
    flt = member.get("obs_filter")
    if flt:
        col, values = flt["column"], [str(v) for v in flt["values"]]
        keep = a.obs[col].astype(str).isin(values)
        a = a[keep.values].copy()
    return a


def _barcode_overlap_warnings(parts: dict) -> list[str]:
    """Same barcodes appearing in two members may be the same cells counted
    twice (the double-count trap). Cheap set check; expression identity is
    left to the loop's explore step — the warning just makes it look."""
    warns = []
    names = list(parts)
    for i, n1 in enumerate(names):
        b1 = set(parts[n1].obs_names)
        for n2 in names[i + 1 :]:
            b2 = set(parts[n2].obs_names)
            inter = len(b1 & b2)
            denom = min(len(b1), len(b2)) or 1
            if inter / denom > 0.3:
                warns.append(
                    f"barcode overlap {n1} vs {n2}: {inter} shared "
                    f"({inter / denom:.0%} of smaller) — possible double-count, verify expression identity"
                )
    return warns


def execute_plan(units: list[dict], profiles: list[dict], plan: dict, out_root: Path) -> None:
    import anndata as ad

    units_by_name = {u["name"]: u for u in units}
    out_root.mkdir(parents=True, exist_ok=True)
    global_manifest = {
        "input_units": units,
        "profiles": profiles,
        "plan": plan,
        "units_written": [],
        "warnings": [],
    }

    for au in plan["analysis_units"]:
        name = au["name"]
        parts = {m["source"]: _load_member(units_by_name, m) for m in au["members"]}
        warns = _barcode_overlap_warnings(parts)

        if len(parts) == 1:
            merged = next(iter(parts.values()))
            merged.obs["source_unit"] = next(iter(parts))
        else:
            merged = ad.concat(
                parts, join="outer", label="source_unit", index_unique="::", merge="first"
            )

        udir = out_root / name / "input"
        udir.mkdir(parents=True, exist_ok=True)
        tmp = udir / "organized.tmp.h5ad"
        merged.write_h5ad(tmp)  # never in place: tmp + rename
        tmp.rename(udir / "organized.h5ad")

        unit_manifest = {
            "analysis_unit": au,
            "n_cells": int(merged.n_obs),
            "n_vars": int(merged.n_vars),
            "sources": {k: int(v.n_obs) for k, v in parts.items()},
            "warnings": warns,
        }
        with open(udir / "manifest.json", "w") as f:
            json.dump(unit_manifest, f, indent=2)
        global_manifest["units_written"].append(
            {"name": name, "dir": str(udir.parent), "n_cells": int(merged.n_obs)}
        )
        global_manifest["warnings"].extend(warns)
        print(f"[write] {name}: {merged.n_obs} cells from {list(parts)} -> {udir / 'organized.h5ad'}")

    with open(out_root / "manifest.json", "w") as f:
        json.dump(global_manifest, f, indent=2)
    print(f"[done] {len(plan['analysis_units'])} analysis unit(s); manifest at {out_root / 'manifest.json'}")

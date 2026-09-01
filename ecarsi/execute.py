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


def _conservation_audit(units_by_name: dict, plan: dict) -> dict:
    """Every input cell must land in exactly one analysis unit — no cell
    silently dropped by a filter gap, none double-counted by overlapping
    filters, no source file omitted from the plan. Backed reads (obs only),
    runs BEFORE anything is written; any violation aborts the whole run."""
    import anndata as ad

    taken: dict[str, list[tuple[str, set]]] = {n: [] for n in units_by_name}
    for au in plan["analysis_units"]:
        for m in au["members"]:
            a = ad.read_h5ad(units_by_name[m["source"]]["h5ad"], backed="r")
            flt = m.get("obs_filter")
            if flt:
                vals = [str(v) for v in flt["values"]]
                keep = a.obs[flt["column"]].astype(str).isin(vals)
                names = set(a.obs_names[keep.values])
            else:
                names = set(a.obs_names)
            taken[m["source"]].append((au["name"], names))
            a.file.close()

    unit_expected: dict[str, int] = {}
    for au in plan["analysis_units"]:
        unit_expected[au["name"]] = sum(
            len(names) for src_grabs in taken.values() for uname, names in src_grabs if uname == au["name"]
        )

    audit, errors = {}, []
    for src, grabs in taken.items():
        a = ad.read_h5ad(units_by_name[src]["h5ad"], backed="r")
        total, all_names = a.n_obs, set(a.obs_names)
        a.file.close()
        union = set().union(*(g[1] for g in grabs)) if grabs else set()
        n_assigned = sum(len(g[1]) for g in grabs)
        audit[src] = {"total": total, "assigned": n_assigned, "unique_assigned": len(union)}
        if not grabs:
            errors.append(f"{src}: whole source file absent from the plan ({total} cells lost)")
            continue
        if n_assigned > len(union):
            dupes = n_assigned - len(union)
            errors.append(f"{src}: {dupes} cells assigned to more than one analysis unit")
        missing = all_names - union
        if missing:
            errors.append(f"{src}: {len(missing)} cells covered by no analysis unit")
    if errors:
        raise ValueError("cell conservation violated:\n  " + "\n  ".join(errors))
    return {"sources": audit, "unit_expected": unit_expected}


def execute_plan(units: list[dict], profiles: list[dict], plan: dict, out_root: Path) -> None:
    import anndata as ad

    units_by_name = {u["name"]: u for u in units}
    audit = _conservation_audit(units_by_name, plan)
    print(
        "[audit] cell conservation OK: "
        + ", ".join(f"{k} {v['total']}" for k, v in audit["sources"].items())
    )
    out_root.mkdir(parents=True, exist_ok=True)
    global_manifest = {
        "input_units": units,
        "profiles": profiles,
        "plan": plan,
        "conservation_audit": audit,
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

        expected = audit["unit_expected"][name]
        if int(merged.n_obs) != expected:
            raise ValueError(
                f"unit {name!r}: merged {merged.n_obs} cells but conservation audit expected {expected}"
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

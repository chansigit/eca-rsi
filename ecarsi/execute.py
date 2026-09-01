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


def _keep_mask(obs, flt):
    """Boolean mask an obs_filter selects (None → whole file). Single
    definition shared by audit and executor so the two can never disagree
    on which cells a filter means."""
    if not flt:
        return None
    vals = [str(v) for v in flt["values"]]
    return obs[flt["column"]].astype(str).isin(vals).values


def _load_member(units_by_name: dict, member: dict):
    import anndata as ad

    a = ad.read_h5ad(units_by_name[member["source"]]["h5ad"])
    mask = _keep_mask(a.obs, member.get("obs_filter"))
    return a[mask].copy() if mask is not None else a


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

    src_obs = {}  # obs is in-memory even in backed mode; one read per source
    for src, u in units_by_name.items():
        a = ad.read_h5ad(u["h5ad"], backed="r")
        src_obs[src] = a.obs
        a.file.close()

    taken: dict[str, list[set]] = {src: [] for src in units_by_name}
    unit_expected: dict[str, int] = {}
    for au in plan["analysis_units"]:
        n = 0
        for m in au["members"]:
            obs = src_obs[m["source"]]
            mask = _keep_mask(obs, m.get("obs_filter"))
            names = set(obs.index) if mask is None else set(obs.index[mask])
            taken[m["source"]].append(names)
            n += len(names)
        unit_expected[au["name"]] = n

    audit, errors = {}, []
    for src, grabs in taken.items():
        total, all_names = len(src_obs[src]), set(src_obs[src].index)
        union = set().union(*grabs) if grabs else set()
        n_assigned = sum(map(len, grabs))
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
    species_by_name = {p["name"]: p.get("species") for p in profiles}
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

        # one species per analysis unit is a plan invariant; surface it here so
        # downstream steps (persample --annotate context) need not climb back
        # to the global manifest
        sps = {species_by_name.get(src) for src in parts} - {None}
        unit_manifest = {
            "analysis_unit": au,
            "species": sps.pop() if len(sps) == 1 else None,
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

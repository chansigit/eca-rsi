"""Deterministic executor for the organize plan — the agent proposes, this runs.

Output layout (see ecarsi.layout — one directory per analysis unit, ready
to hand to persample and the loop):

    <out_root>/
      index.html                     # root landing page (ecarsi.index)
      organize/manifest.json         # global: detection, profiles, plan, warnings
      units/<unit_name>/input/
        organized.h5ad               # merged (+ filtered) cells, provenance in obs
        manifest.json                # this unit's slice of the plan
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from . import layout as L
from .run_state import file_identity, read_json, write_json

if TYPE_CHECKING:
    import anndata as ad


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
    from .upstream import load_evidence
    _, values, _ = load_evidence(units_by_name[member["source"]], a.obs)
    a.obs["eca_source_cell_id"] = a.obs_names.astype(str)
    for col, s in values.items():
        a.obs[col] = s.fillna("").astype(str)
    mask = _keep_mask(a.obs, member.get("obs_filter"))
    return a[mask].copy() if mask is not None else a


def _barcode_overlap_warnings(parts: list[tuple[str, "ad.AnnData"]]) -> list[str]:
    """Same barcodes appearing in two members may be the same cells counted
    twice (the double-count trap). Cheap set check; expression identity is
    left to the loop's explore step — the warning just makes it look."""
    warns = []
    for i, (n1, a1) in enumerate(parts):
        b1 = set(a1.obs_names)
        for n2, a2 in parts[i + 1 :]:
            b2 = set(a2.obs_names)
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


def execute_plan(units: list[dict], profiles: list[dict], plan: dict, out_root: Path,
                 *, records: list[dict] | None = None, input_identity: str | None = None,
                 adapter_identity: str | None = None) -> None:
    import anndata as ad

    from .plan import _validate
    from .upstream import snapshot, validate_matrix, verify_snapshots
    _validate(plan, profiles)
    units_by_name = {u["name"]: u for u in units}
    species_by_name = {p["name"]: p.get("species") for p in profiles}
    audit = _conservation_audit(units_by_name, plan)
    print(
        "[audit] cell conservation OK: "
        + ", ".join(f"{k} {v['total']}" for k, v in audit["sources"].items())
    )
    out_root.mkdir(parents=True, exist_ok=True)
    global_manifest = {
        "schema_version": 2, "state": "running", "input_identity": input_identity,
        "adapter_identity": adapter_identity,
        "source_inventory": records or units,
        "input_units": units,
        "profiles": profiles,
        "plan": plan,
        "conservation_audit": audit,
        "units_written": [],
        "warnings": [],
    }
    gm = L.organize_manifest(out_root)
    previous = read_json(gm) if gm.is_file() else {}
    written = {u["name"]: u for u in previous.get("units_written", [])}
    write_json(gm, {**global_manifest, "units_written": list(written.values())})

    for au in plan["analysis_units"]:
        name = au["name"]
        unit = L.unit_dir(out_root, name)
        if name in written:
            item = written[name]
            if (file_identity(L.input_h5ad(unit)) != item["identity"] or
                    file_identity(L.input_manifest(unit)) != item["manifest_identity"]):
                raise ValueError(f"partial organize output was changed: {unit}")
            global_manifest["units_written"].append(item)
            saved = read_json(L.input_manifest(unit))
            verify_snapshots(L.input_manifest(unit).parent, saved)
            global_manifest["warnings"].extend(saved.get("warnings", []))
            continue
        # a list, not a dict keyed by source: two members of the same unit
        # can share a source (different obs_filter slices of one file) — a
        # dict would silently keep only the last and undercount cells
        parts = [(m["source"], _load_member(units_by_name, m)) for m in au["members"]]
        warns = _barcode_overlap_warnings(parts)

        if len(parts) == 1:
            src, merged = parts[0]
            merged.obs["source_unit"] = src
        else:
            merged = ad.concat(
                [a for _, a in parts], join="outer", label="source_unit",
                keys=[src for src, _ in parts], index_unique="::", merge="first", fill_value=0,
            )

        expected = audit["unit_expected"][name]
        if int(merged.n_obs) != expected:
            raise ValueError(
                f"unit {name!r}: merged {merged.n_obs} cells but conservation audit expected {expected}"
            )
        validate_matrix(merged)

        unit = L.unit_dir(out_root, name)
        udir = unit / L.INPUT
        udir.mkdir(parents=True, exist_ok=True)
        tmp = udir / "organized.tmp.h5ad"
        merged.write_h5ad(tmp)  # never in place: tmp + rename
        check = ad.read_h5ad(tmp, backed="r")
        try:
            if check.shape != merged.shape or not check.obs_names.equals(merged.obs_names):
                raise ValueError(f"organized H5AD failed readback: {name}")
        finally:
            check.file.close()
        tmp.rename(udir / "organized.h5ad")

        # one species per analysis unit is a plan invariant; surface it here so
        # downstream steps (persample --annotate context) need not climb back
        # to the global manifest
        sps = {species_by_name.get(src) for src, _ in parts} - {None}
        src_totals: dict[str, int] = {}
        for src, a in parts:
            src_totals[src] = src_totals.get(src, 0) + int(a.n_obs)
        unit_manifest = {
            "schema_version": 2,
            "analysis_unit": au,
            "species": sps.pop() if len(sps) == 1 else None,
            "n_cells": int(merged.n_obs),
            "n_vars": int(merged.n_vars),
            "sources": src_totals,
            "warnings": warns,
            "upstream": {},
        }
        for src in src_totals:
            record = units_by_name[src]
            target = udir / L.UPSTREAM / src
            if "standardize" in record:
                snapshot(record, target)
                unit_manifest["upstream"][src] = {
                    "dir": str(target.relative_to(udir)),
                    "standardize": record["standardize"],
                    "identify_columns": record.get("identify_columns", {}),
                    "source_obs_identity": file_identity(target / "source_obs.csv.gz"),
                    "snapshot_files": {p.name: file_identity(p) for p in sorted(target.iterdir()) if p.is_file()},
                }
        unit_manifest["identity"] = file_identity(L.input_h5ad(unit))
        write_json(L.input_manifest(unit), unit_manifest)
        global_manifest["units_written"].append(
            {"name": name, "dir": str(unit), "n_cells": int(merged.n_obs),
             "identity": unit_manifest["identity"], "manifest_identity": file_identity(L.input_manifest(unit))}
        )
        global_manifest["warnings"].extend(warns)
        write_json(gm, global_manifest)
        L.log_event(unit, f"organize: {merged.n_obs} cells from {list(src_totals)}", echo=False)
        print(f"[write] {name}: {merged.n_obs} cells from {list(src_totals)} -> {udir / 'organized.h5ad'}")

    global_manifest["state"] = "complete"
    write_json(gm, global_manifest)
    from .index import write_all

    write_all(out_root)
    print(f"[done] {len(plan['analysis_units'])} analysis unit(s); manifest at {gm}")

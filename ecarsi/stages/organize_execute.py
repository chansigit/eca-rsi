"""Deterministic executor for the organize plan — the agent proposes, this runs.

Output layout (see ecarsi.layout — one directory per analysis unit, ready
to hand to persample and the loop):

    <out_root>/
      index.html                     # root landing page (ecarsi.ui.index)
      organize/manifest.json         # global: detection, profiles, plan, warnings
      units/<unit_name>/input/
        organized.h5ad               # merged (+ filtered) cells, provenance in obs
        manifest.json                # this unit's slice of the plan
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from .. import layout as L
from ..run_state import file_identity, read_json, write_json

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


def _slim(a) -> None:
    """organized.h5ad is a counts carrier: every consumer (persample subsets,
    OSP QC/cluster, MSP) rebuilds X from layers["counts"], and
    validate_matrix() refuses X as a counts fallback -- so the upstream's
    normalized X was dead weight, half the matrix bytes of a file kept for
    the unit's lifetime and mirrored. X becomes an empty placeholder rather
    than None: anndata's backed mode needs an X group to serve layers
    (persample validates the file backed). Integer counts wider than 4 bytes
    narrow to int32 (a UMI count never nears 2^31); float counts are left
    alone. Same rules as msp 0.3.6 / osp 0.1.6 apply on their own outputs."""
    import numpy as np
    from scipy import sparse

    counts = a.layers["counts"]
    if np.issubdtype(counts.dtype, np.integer) and counts.dtype.itemsize > 4 and counts.max() <= np.iinfo(np.int32).max:
        a.layers["counts"] = counts.astype(np.int32)
    a.X = sparse.csr_matrix(a.shape, dtype=np.float32)
    a.uns["X_placeholder"] = "X is intentionally empty; expression lives in layers['counts'] (ecarsi.stages.organize_execute)"


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
    filters, no source file omitted from the plan. Metadata-only reads,
    runs BEFORE anything is written; any violation aborts the whole run."""
    from .h5ad import read_obs

    src_obs = {src: read_obs(u["h5ad"]) for src, u in units_by_name.items()}

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


def effective_mapping(plan: dict, profiles: list[dict], sample_map: dict | None = None, *,
                      stop: bool = True) -> dict:
    """Each source's experiment decision (decision 0016): the owner's sample map, else ECA-PP's, else the
    organize agent's. An ECA-PP decision that found no usable sample on a platform that needs one stops
    here (`stop`), unless the sample map decides that source; the planning agent cannot fix it, so its
    plan checks pass `stop=False`."""
    explicit = (sample_map or {}).get("sources", {})
    eca_pp = {p["name"]: p["eca_pp_decision"] for p in profiles if p.get("eca_pp_decision")}
    stopped = [f"{name}: {d['error']}" for name, d in eca_pp.items() if "error" in d and name not in explicit]
    if stopped and stop:
        raise ValueError("no usable sample or batch: " + "; ".join(stopped))
    agent = {name: {**d, "source": "agent"} for name, d in plan.get("sample_mapping", {}).items()}
    return {**agent, **eca_pp, **{name: {**d, "source": "sample_map"} for name, d in explicit.items()}}


def unit_batch_key(sources: list[str], mapping: dict, explicit: dict):
    """The unit's batch_key when the owner's sample map names none: ECA-PP's batch when it recommends the
    correction, false (one batch) when it found none, for a unit whose one source ECA-PP decided.
    ponytail: several sources keep batch = sample; their ECA-PP batch values could collide."""
    if "batch_key" in explicit or len(sources) != 1:
        return explicit.get("batch_key")
    decision = mapping[sources[0]]
    if decision.get("source") != "eca_pp":
        return None
    return decision["batch"] or False


def experiment_values(record: dict, decision: dict, obs=None):
    """A source's experiment per cell under its decision: the sample column's values (ECA-PP's derived
    column read from its evidence), "all" for a confirmed single experiment; None when cell IDs name them."""
    import pandas as pd
    from .h5ad import read_obs
    from .upstream import load_evidence, normalize

    if decision.get("derive_from_cell_id") is not None:
        return None
    obs = read_obs(record["h5ad"]) if obs is None else obs
    col = decision.get("sample_column")
    if col is None:
        return pd.Series("all", index=obs.index)
    values = load_evidence(record, obs)[1][col] if col.startswith("eca_pp_") else obs[col]
    values = normalize(values)
    if decision.get("missing_as") is not None:
        values = values.fillna(decision["missing_as"])
    return values.astype("string")


SHARED_MAX_VALUES = 500  # a shared column with more values names cells or wells, not experiments


def shared_experiments(records: list[dict], profiles: list[dict]) -> dict:
    """#72: obs columns naming the same experiments in several sources, as when a library is split by cell
    type into several files. For each column present in two or more sources, with values recurring across them
    and no cell ID shared between those sources: {column: {value: {source: experiment}}}, the experiment being
    that source's ECA-PP experiment holding exactly the value's cells (a source without one gets the value). A
    column where some value does not line up with one experiment is left out. The planning agent reads this
    to propose merges; the host checks every merge again."""
    from .h5ad import read_obs
    from .upstream import normalize

    decisions = {p["name"]: p.get("eca_pp_decision") or {} for p in profiles}
    obs, experiments = {}, {}
    for record in records:
        o = read_obs(record["h5ad"])
        obs[record["name"]] = o
        d = decisions.get(record["name"], {})
        experiments[record["name"]] = experiment_values(record, d, o) if d.get("sample_column") else None
    shared = {}
    for column in sorted({c for o in obs.values() for c in o.columns}):
        sources = [n for n, o in obs.items() if column in o.columns]
        if len(sources) < 2:
            continue
        values = {n: normalize(obs[n][column]).astype("string") for n in sources}
        if any(v.nunique() > SHARED_MAX_VALUES or v.nunique() == len(v) for v in values.values()):
            continue
        ids = [set(obs[n].index.astype(str)) for n in sources]
        if any(ids[i] & ids[j] for i in range(len(ids)) for j in range(i + 1, len(ids))):
            continue
        common = {v for n in sources for v in values[n].dropna().unique()
                  if sum(v in set(values[m].dropna()) for m in sources) >= 2}
        if not common:
            continue
        table, ok = {}, True
        for n in sources:
            exp = experiments[n]
            for value in common & set(values[n].dropna()):
                if exp is None:
                    table.setdefault(value, {})[n] = value
                    continue
                inside = set(exp[values[n].eq(value).fillna(False)].dropna())
                if len(inside) != 1 or set(values[n][exp.eq(next(iter(inside))).fillna(False)].dropna()) != {value}:
                    ok = False
                    break
                table.setdefault(value, {})[n] = next(iter(inside))
            if not ok:
                break
        if ok:
            shared[column] = {v: table[v] for v in sorted(table) if len(table[v]) >= 2}
    return shared


def merge_problems(plan: dict, units_by_name: dict, mapping: dict) -> list[str]:
    """Why the host refuses the plan's cross-source merges (#72): each pools experiments of two or more
    sources of one analysis unit, names each member by its source's experiment value, uses a member once, and
    its members share no cell ID (one library split into files, not two libraries)."""
    unit_of = {m["source"]: au["name"] for au in plan["analysis_units"] for m in au["members"]}
    values, problems, used = {}, [], set()
    for merge in plan.get("merges", []):
        sid, members = merge["sample_id"], [(m["source"], str(m["value"])) for m in merge["members"]]
        if not str(merge.get("evidence", "")).strip():
            problems.append(f"merge {sid}: evidence required")
        if len({s for s, _ in members}) != len(members):
            problems.append(f"merge {sid}: one member per source")
            continue
        if len({unit_of.get(s) for s, _ in members}) != 1 or None in {unit_of.get(s) for s, _ in members}:
            problems.append(f"merge {sid}: its sources must be members of one analysis unit")
            continue
        cells = []
        for source, value in members:
            if source not in values:
                values[source] = experiment_values(units_by_name[source], mapping[source])
            v = values[source]
            if (source, value) in used:
                problems.append(f"merge {sid}: {source}/{value} is already in another merge")
            used.add((source, value))
            if v is None:
                problems.append(f"merge {sid}: {source} names its experiments by cell ID; merge it in the sample map")
            elif not v.eq(value).any():
                problems.append(f"merge {sid}: {source} has no experiment {value!r} (its experiments: "
                                f"{sorted(v.dropna().unique())[:20]})")
            else:
                cells.append(set(v.index[v.eq(value).fillna(False)].astype(str)))
        if any(cells[i] & cells[j] for i in range(len(cells)) for j in range(i + 1, len(cells))):
            problems.append(f"merge {sid}: its members share cell IDs, so they are not one experiment split into files")
    return problems


def _experiment_audit(units_by_name: dict, plan: dict) -> dict:
    """Before writing, prove each source experiment lands in one analysis unit."""
    from .h5ad import read_obs
    from .upstream import load_evidence, normalize

    across = {}
    for source, record in units_by_name.items():
        decision = plan["sample_mapping"][source]
        if decision.get("derive_from_cell_id") is not None:
            # an explicit map naming experiments by cell ID: build_mapping checks the split itself,
            # against the full source obs
            across[source] = {"experiments": None, "complete": True}
            continue
        obs = read_obs(record["h5ad"])
        col = decision.get("sample_column")
        if col is not None and col.startswith("eca_pp_"):
            obs[col] = load_evidence(record, obs)[1][col]
        values = normalize(obs[col]) if col is not None else None
        if values is not None and decision.get("missing_as") is not None:
            values = values.fillna(decision["missing_as"])
        # cells without a value are an error unless the sample map excludes them: build_mapping checks
        # that after the exclusions; here only the experiments that have a value can be split
        owners = {}
        for au in plan["analysis_units"]:
            for member in au["members"]:
                if member["source"] != source:
                    continue
                mask = _keep_mask(obs, member.get("obs_filter"))
                selected = obs.index if mask is None else obs.index[mask]
                selected_values = {"all"} if col is None else set(values.loc[selected].dropna().astype(str))
                for value in selected_values:
                    if value in owners and owners[value] != au["name"]:
                        raise ValueError(f"{source}/{value}: organize split a complete experiment "
                                         f"between {owners[value]} and {au['name']}")
                    owners[value] = au["name"]
        across[source] = {"experiments": len(owners), "complete": True}
    return across


def execute_plan(units: list[dict], profiles: list[dict], plan: dict, out_root: Path,
                 *, records: list[dict] | None = None, input_identity: str | None = None,
                 adapter_identity: str | None = None, sample_map: dict | None = None) -> None:
    """`sample_map` is the owner's explicit map (the dataset spec's `organize.sample_map`): its `sources`
    override the plan's experiment column per source; `merges`, `exclude_cells` and `batch_key` apply to
    every unit (a merge to the unit holding its sources)."""
    import anndata as ad

    from ..plan import _validate
    from .upstream import snapshot, validate_matrix, verify_snapshots
    _validate(plan, profiles)
    if "sample_mapping" in plan:
        from ..plan import validate_sample_mapping
        validate_sample_mapping(plan, profiles)
    units_by_name = {u["name"]: u for u in units}
    species_by_name = {p["name"]: p.get("species") for p in profiles}
    audit = _conservation_audit(units_by_name, plan)
    explicit = (sample_map or {}).get("sources", {})
    unknown = set(explicit) - {p["name"] for p in profiles}
    if unknown:
        raise ValueError(f"sample_map names sources that are not accepted inputs: {sorted(unknown)}")
    effective = ({**plan, "sample_mapping": effective_mapping(plan, profiles, sample_map)}
                 if "sample_mapping" in plan else plan)
    experiment_audit = _experiment_audit(units_by_name, effective) if "sample_mapping" in plan else None
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
        "experiment_audit": experiment_audit,
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
            if "sample_mapping" in plan and "sample_mapping" not in saved:
                raise ValueError(f"confirmed experiment mapping missing: {unit}")
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
        _slim(merged)

        unit = L.unit_dir(out_root, name)
        udir = unit / L.INPUT
        udir.mkdir(parents=True, exist_ok=True)
        tmp = udir / "organized.tmp.h5ad"
        merged.write_h5ad(tmp)  # never in place: tmp + rename
        from .h5ad import open_counts
        check = open_counts(tmp, min_vars=1)
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
        if "sample_mapping" in plan:
            from ..sample_mapping import build_mapping, mapping_identity, normalize, SAMPLE_KEY
            explicit = sample_map or {}
            mapping_spec = {"sources": {src: effective["sample_mapping"][src] for src in src_totals}}
            merges = [m for m in (explicit if "merges" in explicit else plan).get("merges", [])
                      if {member.get("source") for member in m.get("members", [])} & set(src_totals)]
            if merges:
                mapping_spec["merges"] = merges
            for key in ("exclude_cells", "chunk_cells"):
                if key in explicit:
                    mapping_spec[key] = explicit[key]
            batch_key = unit_batch_key(list(src_totals), effective["sample_mapping"], explicit)
            if batch_key == "eca_pp_batch" and normalize(merged.obs[batch_key]).nunique() < 2:
                batch_key = False  # an organ slice of the source holds one ECA-PP batch: nothing to correct
            if batch_key is not None:
                mapping_spec["batch_key"] = batch_key
            table, decision = build_mapping(L.input_h5ad(unit), unit, mapping_spec)
            unmapped = table[SAMPLE_KEY].eq("") & table["excluded_reason"].eq("")
            if len(table) != merged.n_obs or unmapped.any():
                raise ValueError(f"{name}: confirmed experiment mapping does not cover every cell")
            mapping_path = udir / L.SAMPLE_MAPPING
            table.to_csv(mapping_path, index_label="cell_id")
            unit_manifest["sample_mapping"] = {
                "path": L.SAMPLE_MAPPING, "identity": file_identity(mapping_path),
                "mapping_identity": mapping_identity(table), "decision": decision,
            }
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
    print(f"[done] {len(plan['analysis_units'])} analysis unit(s); manifest at {gm}")

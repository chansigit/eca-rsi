"""Worker operations for confirmed samples; no model calls or dataset driver."""
import argparse
import base64
import json
import shutil
from pathlib import Path

from ..warm_pool.state import immutable, reference, verified
from ..warm_pool.state import read, save
from ..run_state import digest, file_identity


def sealed(directory, destination, **metadata):
    files = {str(p.relative_to(directory)): reference(p) for p in sorted(directory.rglob("*"))
             if p.is_file() and not p.name.startswith(".")}
    return immutable(destination, {**metadata, "files": files})


def check_bundle(ref):
    bundle = verified(ref)
    for item in bundle["files"].values():
        if reference(item["path"]) != item:
            raise ValueError("Sample artifact changed")
    return bundle


def partition(spec, offset, destination):
    """One bounded batch per grant, reusing the shared input read within that batch."""
    import anndata as ad
    import pandas as pd
    from ..sample_mapping import read_cell_table, mapping_identity, SAMPLE_KEY
    from ..upstream import verify_snapshots
    unit = Path(spec["unit"])
    metadata = verified(spec["input_manifest"])
    if file_identity(unit / "input/organized.h5ad") != metadata["identity"]:
        raise ValueError("Organize H5AD changed")
    verify_snapshots(unit / "input", metadata)
    mapping = metadata.get("sample_mapping")
    if not mapping:
        raise ValueError("Per-sample requires Organize's confirmed experiment mapping")
    path = unit / "input" / mapping["path"]
    if file_identity(path) != mapping["identity"]:
        raise ValueError("Organize sample mapping changed")
    table = read_cell_table(path)
    if mapping_identity(table) != mapping["mapping_identity"] or not table.index.is_unique:
        raise ValueError("Confirmed sample cell IDs changed")
    full = ad.read_h5ad(unit / "input/organized.h5ad")
    if set(full.obs_names) != set(table.index) or not full.obs_names.is_unique:
        raise ValueError("Organize mapping must cover the exact input cells")
    omitted = table[SAMPLE_KEY].eq("")
    if omitted.any() and ("excluded_reason" not in table or table.loc[omitted, "excluded_reason"].eq("").any()):
        raise ValueError("Unmapped cells need explicit exclusion reasons")
    samples = sorted(table.loc[~omitted, SAMPLE_KEY].unique())
    chosen = samples[offset:offset + spec["batch_size"]]
    entries, total_bytes = [], 0
    for sample in chosen:
        name = "sample-" + digest(sample)[:20]
        folder = destination / name
        folder.mkdir()
        ids = table.index[table[SAMPLE_KEY] == sample]
        data = full[ids].copy()
        data.obs[SAMPLE_KEY] = sample
        batch = mapping["decision"].get("batch_key")
        if batch:
            data.obs[batch["column"]] = batch["of_sample"][sample]
        data.write_h5ad(folder / "subset.h5ad")
        pd.DataFrame({"cell_id": ids, "source_id": data.obs["source_unit"].astype(str).to_numpy(),
                      "source_cell_id": data.obs["eca_source_cell_id"].astype(str).to_numpy()}).to_csv(folder / "input_cells.csv.gz", index=False,
                                             compression={"method": "gzip", "mtime": 0})
        request = {"value": sample, "run_id": spec["run_id"], "n_cells": len(ids), "config": spec["config"],
                   "subset_identity": file_identity(folder / "subset.h5ad"),
                   "identity": digest([spec["input_manifest"], sample, spec["config"]]), "context": None}
        save(folder / "request.json", request)
        entry = sealed(folder, destination / (name + ".json"), sample=sample, n_cells=len(ids))
        size = sum(Path(f["path"]).stat().st_size for f in verified(entry)["files"].values())
        if total_bytes + size > spec["max_batch_bytes"]:
            if not entries:
                raise ValueError("One sample exceeds max_batch_bytes; increase the explicit storage budget")
            shutil.rmtree(folder)
            Path(entry["path"]).unlink()
            break
        total_bytes += size
        entries.append({"sample_id": sample, "bundle": entry, "n_cells": len(ids), "bytes": size})
    excluded = table.loc[omitted].reset_index(names="cell_id")
    excluded.to_csv(destination / "partition_exclusions.csv.gz", index=False)
    save(destination / "partition.json", {"entries": entries, "total_samples": len(samples),
        "next_offset": offset + len(entries), "n_input": len(table), "n_excluded": len(excluded),
        "exclusions": reference(destination / "partition_exclusions.csv.gz")})


def exclusion_ledger(folder, request):
    import pandas as pd
    gone = pd.read_csv(folder / "qc_removed.csv", dtype=str, keep_default_na=False)
    if gone.cell.duplicated().any() or gone.qc_reason.eq("").any():
        raise ValueError("QC exclusions need unique cell IDs and nonempty reasons")
    table = gone.rename(columns={"cell": "cell_uid", "qc_reason": "reason"})
    inputs = pd.read_csv(folder / "input_cells.csv.gz", dtype=str, keep_default_na=False)
    table = table.merge(inputs.rename(columns={"cell_id": "cell_uid"}), on="cell_uid", validate="one_to_one")
    if len(table) != len(gone):
        raise ValueError("QC excluded a cell absent from its input")
    table["sample_id"] = request["value"]
    table["stage"] = "per-sample"
    table["operation"] = "osp.compute"
    table["decision_source"] = "rule"
    table["input_version"] = request["identity"]
    table["run_id"] = request["run_id"]
    table["evidence"] = str(folder / "qc_removed.csv")
    table.to_csv(folder / "cell_exclusions.csv.gz", index=False)


def compute(bundle_ref, destination):
    from ..osp_worker import compute_sample, classify_error
    from ..osp_contract import validate_outputs, is_empty
    bundle = check_bundle(bundle_ref)
    source = Path(bundle["files"]["request.json"]["path"]).parent
    request = read(source / "request.json")
    if "compute_backend" in request["config"]:
        import os
        backend = os.environ.get("RSI_COMPUTE_BACKEND", "cpu")
        desired = request["config"]["compute_backend"]
        if desired == "rapids" and backend != "rapids" or desired == "cpu" and backend != "cpu":
            raise ValueError("compute backend does not match the reserved resources")
        request = {**request, "config": {**request["config"], "compute_backend": backend}}
    folder = destination / "sample"
    folder.mkdir()
    shutil.copyfile(source / "input_cells.csv.gz", folder / "input_cells.csv.gz")
    empty = False
    try:
        compute_sample(request, source, folder)
    except Exception as exc:
        import pandas as pd
        kind, retryable = classify_error(exc, "compute")
        if (folder / "qc_summary.csv").is_file():
            qc = pd.read_csv(folder / "qc_summary.csv", index_col=0).iloc[:, 0]
            survivors = int(qc["n_cells"]) - int(qc["n_low_quality"])
            if 0 <= survivors < 3:
                kind = "qc_zero_survivors" if survivors == 0 else "qc_too_few_survivors"
        save(folder / "run_state.json", {"state": "failed", "identity": request["identity"],
             "failure_kind": kind, "retryable": retryable, "error": str(exc)})
        if not is_empty(folder, request["identity"]):
            raise
        empty = True
    exclusion_ledger(folder, request)
    info = {"sample": bundle["sample"], "empty": empty, "request": request,
            "validation": {"n_input": request["n_cells"], "n_survived": 0, "n_removed": request["n_cells"]}
                          if empty else validate_outputs(folder, False)}
    if not empty:
        from osp.annotate import _detect_primary_key, _system_prompt, _PROPOSAL_SCHEMA_DOC
        from ..downstream import _data
        key = _detect_primary_key(str(folder))
        data = _data(folder / "clustered.h5ad")
        try:
            clusters = sorted(set(data.obs[key].astype(str)))
        finally:
            data.file.close()
        info.update(key=key, proposal_schema=_PROPOSAL_SCHEMA_DOC, prompt=_system_prompt(str(folder), key, clusters,
            request["config"]["species"], request["config"]["tissue"], "English"))
    result = sealed(folder, destination / "computed.json", **info)
    if not empty:
        immutable(destination / "annotation-state.json", {"bundle": result,
            "data": verified(result)["files"]["clustered.h5ad"], "key": key, "version": 0,
            "seen": {"figures": [], "tables": [], "genes": False, "qc": False}})


def annotation_spec(spec, computed_ref, compute_request):
    bundle = verified(computed_ref)
    state = reference(Path(computed_ref["path"]).parent / "annotation-state.json")
    programs = [
        ("read_evidence", {"kind": {"enum": ["figures", "tables"], "type": "string"},
                           "offset": {"type": "integer", "minimum": 0}},
         "Read immutable evidence. Start offset=0; follow next_offset until null. Figures are returned as images. Read both kinds before conclusions.", True, True),
        ("check_genes", {"genes": {"type": "array", "minItems": 1, "maxItems": 200,
                                    "items": {"type": "string", "minLength": 1}}}, "Verify marker expression per current cluster.", False, True),
        ("check_qc_scores", {}, "Read per-cluster QC scores for the current clustering.", False, True),
        ("subcluster", {"cluster": {"type": "string"}, "resolution": {"type": "number", "exclusiveMinimum": 0}},
         "Refine one heterogeneous cluster on a worker. The returned version replaces the previous clustering. Recheck markers and QC after refinement.", True, False),
        ("submit_annotation", {"proposal_json": {"type": "string"}, "version": {"type": "integer", "minimum": 0}},
         "Submit validated annotation for the current version. Errors must be corrected. Schema: " + bundle["proposal_schema"], False, False)]
    tools = []
    for name, props, description, multimodal, read_only in programs:
        tools.append({"name": name, "description": description,
            "parameters": {"type": "object", "properties": props, "required": list(props), "additionalProperties": False},
            "args": ["-m", "ecarsi.stages.persample", "tool", name, "{state}", "{arguments}"],
            **spec["tool_budget"], "inputs": [reference(Path(__file__))],
            "outputs": ["result.json"], "result_file": "result.json", "multimodal": multimodal, "read_only": read_only})
    prompt = bundle["prompt"] + ("\n\nYou have no local Read or execution tools. Use read_evidence for figures and tables, "
        "following next_offset until null. All tools execute on a worker. The initial clustering version is 0. "
        "After subcluster, use the returned version and repeat marker/QC verification. Submit the current version "
        "with submit_annotation; accepted submission finishes automatically. Do not physically remove proposed drop cells.")
    return {"session_id": "osp-" + digest([spec["run_id"], bundle["sample"]])[:24],
        "dataset_id": spec["dataset_id"], "prompt": prompt, "tools": tools, "max_turns": 80,
        "pool_root": spec["pool_root"], "bridge_root": spec["bridge_root"],
        "output_root": str(Path(spec["output_root"]) / ("agent-" + digest(bundle["sample"])[:20])),
        "completion_tool": "submit_annotation", "tool_state": state, "planner": "ecarsi.stages.evidence",
        "trace": {"workflow_id": "persample/" + spec["run_id"], "dataset_id": spec["dataset_id"],
                  "unit_id": "osp.annotate", "sample_id": bundle["sample"], "depends_on": [compute_request]}}


def evidence_files(bundle):
    figures = sorted(n for n in bundle["files"] if n.endswith(".png") and any(
        token in n for token in ("umap_clusters", "/paga", "qc_violin", "decontx_heatmap")))
    tables = sorted(n for n in bundle["files"] if n.endswith(".csv") and n.startswith(
        ("de_top_genes_", "cluster_summary_", "qc_summary", "decontx_top_genes_", "paga_connectivities_")))
    text = "\n\n".join(n + "\n" + Path(bundle["files"][n]["path"]).read_text() for n in tables)
    return figures, text


def tool(name, state_path, arguments_path, destination):
    state, arguments = read(state_path), read(arguments_path)
    bundle = check_bundle(state["bundle"])
    if reference(state["data"]["path"]) != state["data"]:
        raise ValueError("Annotation clustering changed")
    figures, tables = evidence_files(bundle)
    response = {"version": state["version"]}
    data = None
    try:
        if name == "read_evidence":
            offset = arguments["offset"]
            if arguments["kind"] == "figures":
                if not 0 <= offset <= len(figures):
                    raise ValueError("Use the returned figure offset")
                names, image_bytes = [], 0
                for figure in figures[offset:offset + 16]:
                    size = Path(bundle["files"][figure]["path"]).stat().st_size
                    if image_bytes + size > 9 * 2**20:
                        if not names:
                            raise ValueError("One figure exceeds the 9 MiB image budget")
                        break
                    names.append(figure)
                    image_bytes += size
                next_offset = offset + len(names)
                response.update(text="Figures: " + ", ".join(names), images=["data:image/png;base64," +
                    base64.b64encode(Path(bundle["files"][n]["path"]).read_bytes()).decode() for n in names],
                    next_offset=next_offset if next_offset < len(figures) else None)
                state["seen"]["figures"] = sorted(set(state["seen"]["figures"]) | set(names))
            else:
                if offset % 60000 or offset > len(tables):
                    raise ValueError("Use the returned table offset")
                response.update(text=tables[offset:offset + 60000],
                    next_offset=offset + 60000 if offset + 60000 < len(tables) else None)
                state["seen"]["tables"] = sorted(set(state["seen"]["tables"]) | {offset})
        else:
            import scanpy as sc
            from osp.annotate import _gene_table, _qc_table, _subcluster_once, _validate_proposal
            data = sc.read_h5ad(state["data"]["path"])
            key = state["key"]
            clusters = sorted(set(data.obs[key].astype(str)))
            if name == "check_genes":
                response["text"] = _gene_table(data, arguments["genes"], key)
                state["seen"]["genes"] = True
            elif name == "check_qc_scores":
                response["text"] = _qc_table(data, key)
                state["seen"]["qc"] = True
            elif name == "subcluster":
                if arguments["cluster"] not in clusters:
                    raise ValueError("Unknown cluster in current version")
                new_key = "ann_sub" + str(state["version"] + 1)
                n, response["text"] = _subcluster_once(data, key, arguments["cluster"], arguments["resolution"], new_key)
                if n >= 2:
                    data.write_h5ad(destination / "clustered.h5ad")
                    state.update(data=reference(destination / "clustered.h5ad"), key=new_key, version=state["version"] + 1)
                    state["seen"].update(genes=False, qc=False)
                    response["version"] = state["version"]
            elif name == "submit_annotation":
                if arguments["version"] != state["version"]:
                    raise ValueError("Proposal targets an obsolete clustering version")
                seen = state["seen"]
                if (not seen["genes"] or not seen["qc"] or set(figures) - set(seen["figures"]) or
                        set(range(0, max(1, len(tables)), 60000)) - set(seen["tables"])):
                    raise ValueError("Read all required figures/tables and verify current markers and QC before submitting")
                proposal = json.loads(arguments["proposal_json"])
                problems = _validate_proposal(proposal, clusters, data.obs)
                if problems:
                    raise ValueError("; ".join(problems))
                proposal["cluster_key"] = key
                response.update(accepted=True, proposal=proposal)
            else:
                raise ValueError("Unknown annotation tool")
    except ValueError as exc:
        response.update(accepted=False, error=str(exc)[:4000])
    finally:
        if data is not None and data.isbacked:
            data.file.close()
    response["state"] = immutable(destination / "state.json", state)
    save(destination / "result.json", response)


def finalize(computed_ref, annotation_ref, destination):
    from ..osp_contract import validate_outputs
    bundle = check_bundle(computed_ref)
    folder = destination / "sample"
    folder.mkdir()
    for name, ref in bundle["files"].items():
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ref["path"], path)
    validation = bundle["validation"]
    if not bundle["empty"] and annotation_ref is None:
        # Control skipped the annotation (two agent sessions died): survivors keep OSP's clustering
        # and carry 'unannotated' as the prior label cross-sample sees.
        import scanpy as sc
        data = sc.read_h5ad(folder / "clustered.h5ad")
        for column in ("_ann_coarse", "_ann_fine"):
            data.obs[column] = "unannotated"
        data.write_h5ad(folder / "clustered.h5ad")
        validation = validate_outputs(folder, False)
    elif not bundle["empty"]:
        from osp.annotate import _validate_proposal, _apply_proposal, _plot_annotation
        from osp.report import generate_report
        import scanpy as sc
        accepted = verified(annotation_ref)
        state = verified(accepted["state"])
        if state["bundle"] != computed_ref or accepted.get("accepted") is not True:
            raise ValueError("Annotation belongs to another computation or was not accepted")
        if reference(state["data"]["path"]) != state["data"]:
            raise ValueError("Refined clustering changed before finalization")
        data = sc.read_h5ad(state["data"]["path"])
        proposal = accepted["proposal"]
        if proposal["cluster_key"] != state["key"]:
            raise ValueError("Annotation clustering version mismatch")
        problems = _validate_proposal(proposal, sorted(set(data.obs[state["key"]].astype(str))), data.obs)
        if problems:
            raise ValueError("; ".join(problems))
        _apply_proposal(data, state["key"], proposal)
        _plot_annotation(data, str(folder / "figures"))
        data.write_h5ad(folder / "clustered.h5ad")
        generate_report(str(folder), annotation_proposal=proposal)
        save(folder / "annotation_proposal.json", proposal)
        validation = validate_outputs(folder, True)
    sealed(folder, destination / "final.json", sample=bundle["sample"], empty=bundle["empty"],
           validation=validation, computed=computed_ref, annotation=annotation_ref)


def main():
    import logging
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("operation", choices=["partition", "compute", "tool", "finalize"])
    p.add_argument("args", nargs="+")
    a = p.parse_args()
    logging.getLogger(__name__).info("Starting per-sample operation %s", a.operation)
    destination = Path.cwd()
    if a.operation == "partition":
        partition(read(a.args[0]), int(a.args[1]), destination)
    elif a.operation == "compute":
        compute(reference(a.args[0]), destination)
    elif a.operation == "tool":
        tool(a.args[0], Path(a.args[1]), Path(a.args[2]), destination)
    else:
        finalize(reference(a.args[0]), reference(a.args[1]) if a.args[1] != "none" else None, destination)
    logging.getLogger(__name__).info("Completed per-sample operation %s", a.operation)


if __name__ == "__main__":
    main()

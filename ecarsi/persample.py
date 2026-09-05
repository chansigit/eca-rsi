"""Source-scoped experiment mapping and resumable OSP workers.

Every sample requires a successful run record plus validated counts, annotation
and per-cell QC ledger. Input/config/source changes require a new output root.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

from . import cost
from . import layout as L
from .run_state import digest, file_identity, read_json, write_json, writer_lock
from .sample_mapping import SAMPLE_KEY, build_mapping, mapping_identity, obs_profile
from .osp_contract import INPUT_CELLS, REQUEST, is_done

SAMPLE_COL_SCHEMA = {
    "type": "object",
    "properties": {
        "sample_column": {"type": ["string", "null"]},
        "rationale": {"type": "string"},
        "confirmed_single": {"type": "boolean"},
    },
    "required": ["sample_column", "rationale"],
}

CONTRACT = L.PS_CONTRACT
SUBSET_FILE = "subset.h5ad"  # the driver's hand-off to an osp child; removed once the sample is settled
FIXED_BYTES_PER_CHILD = 1 << 30


# ---------------------------------------------------------------- profiling


def profile_obs(h5ad: Path, max_levels: int = 50) -> dict:
    import anndata as ad
    a = ad.read_h5ad(h5ad, backed="r")
    try:
        return obs_profile(a.obs)
    finally:
        a.file.close()


# ------------------------------------------------------- identify (agent)


def _validate_sample_column(decision: dict, profile: dict, *, allow_unknown: bool = False) -> str | None:
    """None if valid, else a problem description (fix-and-resubmit style)."""
    if not isinstance(decision, dict) or "sample_column" not in decision:
        return "decision must contain sample_column"
    col = decision.get("sample_column")
    if not isinstance(decision.get("rationale"), str) or not decision["rationale"].strip():
        return "experiment decision requires a rationale"
    if col is None:
        if allow_unknown and decision.get("confirmed_single") is False:
            return None  # valid uncertainty; execution still requires an explicit partition
        return None if decision.get("confirmed_single") is True else "unknown grouping is not a confirmed single experiment"
    if not isinstance(col, str) or col in ("source_unit", "eca_source_cell_id", "eca_pp_cell_type", SAMPLE_KEY):
        return "sample_column must be an experiment column, not a bookkeeping/cell-type column"
    info = profile["obs_columns"].get(col)
    if info is None:
        return f"picked obs column {col!r}, which does not exist"
    if info["n_unique"] < 1:
        return f"sample column {col!r} has {info['n_unique']} levels — implausible for 10x runs"
    if info.get("n_na", 0) > 0:
        # a column that leaves cells unassigned is not a partition: those
        # cells would become a bogus "nan" sample (the silent-garbage trap)
        return f"sample column {col!r} leaves {info['n_na']} cells NA — not a valid partition"
    return None


def identify_sample_column(profile: dict) -> dict:
    from .agent_retry import run_with_retry

    return run_with_retry(lambda: _identify(profile), label="identify sample column")


async def _identify(profile: dict) -> dict:
    from .harness import ToolSpec, run_agent

    brief = (Path(__file__).parent / "prompts" / "sample_column.md").read_text()
    prompt = (
        brief + "\n\n## obs profile\n\n```json\n" + json.dumps(profile, indent=1) + "\n```\n"
        + "\nFinish by calling submit_sample_column with a JSON string matching the schema above."
    )
    from . import model

    async def submit_sample_column(args: dict) -> dict:
        try:
            decision = json.loads(args["decision_json"])
        except json.JSONDecodeError as exc:
            return {"content": [{"type": "text", "text": f"JSON parse error, fix and resubmit: {exc}"}],
                    "is_error": True}
        missing = [k for k in ("sample_column", "rationale") if k not in decision]
        if missing:
            return {"content": [{"type": "text", "text": f"missing field(s) {missing}, fix and resubmit"}],
                    "is_error": True}
        # Accept an honest unknown decision. build_mapping then stops before
        # computation; repeatedly demanding resubmission would invite guesses.
        problem = _validate_sample_column(decision, profile, allow_unknown=True)
        if problem:
            return {"content": [{"type": "text", "text": f"{problem} — fix and resubmit"}], "is_error": True}
        return {"content": [{"type": "text", "text": "recorded"}], "is_error": False, "_submitted": decision}

    tool = ToolSpec(
        name="submit_sample_column",
        description="Submit the sample-column decision. decision_json is a JSON string with this schema:\n"
                    + json.dumps(SAMPLE_COL_SCHEMA, indent=1),
        input_schema={"decision_json": str},
        handler=submit_sample_column,
    )
    result = await run_agent(
        tools=[tool], submit_tool="submit_sample_column", prompt=prompt,
        cwd=os.getcwd(), model=model(),
        max_turns=5, allowed_builtin=(), label="identify sample column",
    )
    identify_sample_column.last_cost = result.cost_usd  # type: ignore[attr-defined]
    return result.submitted


# ------------------------------------------------------------ sample list


def _safe_name(value: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("_")
    return s or "sample"


def list_samples(h5ad: Path, col: str) -> dict[str, int]:
    import anndata as ad

    a = ad.read_h5ad(h5ad, backed="r")
    s = a.obs[col]
    if int(s.isna().sum()):
        raise ValueError(f"sample column {col!r} has NA cells — astype(str) would forge a 'nan' sample")
    counts = {str(k): int(v) for k, v in s.astype(str).value_counts().items()}
    a.file.close()
    return counts


def build_entries(h5ad, col, counts, out_root, py, annotate, model,
                  species=None, tissue=None, context=None):
    entries = []
    for value, n in sorted(counts.items()):
        name = _safe_name(value)[:80] + "_" + digest(value)[:12]
        outdir = out_root / name
        command = [py, "-m", "ecarsi.osp_worker", str(outdir / REQUEST)]
        entries.append({"value": value, "n_cells": n, "outdir": str(outdir),
                        "command": command})
    return entries


def _upstream_species(unit: Path) -> str | None:
    """Species from the organize manifests: the unit's own manifest first,
    else the global manifest's profiles (older organize outputs). A unit
    holds one species by plan invariant; anything ambiguous returns None."""
    um = L.input_manifest(unit)
    if um.is_file():
        with open(um) as f:
            sp = json.load(f).get("species")
        if sp:
            return sp
    root = L.root_of(unit)
    gm = L.organize_manifest(root) if root else Path("/nonexistent")
    if gm.is_file():
        with open(gm) as f:
            g = json.load(f)
        profs = {p["name"]: p.get("species") for p in g.get("profiles", [])}
        for au in g.get("plan", {}).get("analysis_units", []):
            if au["name"] == unit.name:
                sps = {profs.get(m["source"]) for m in au["members"]} - {None}
                if len(sps) == 1:
                    return sps.pop()
    return None


def _is_done(outdir: Path, annotate: bool = False) -> bool:
    return is_done(outdir, annotate)


# ----------------------------------------------------------- drive (pool)


def _estimate_bytes(n_cells: int) -> int:
    per_cell = float(os.environ.get("PERSAMPLE_MEM_PER_CELL_MB", "0.5")) * (1 << 20)
    return int(n_cells * per_cell) + FIXED_BYTES_PER_CHILD


def plan_concurrency(pending: list[dict]) -> tuple[int, int, int]:
    """(max_parallel, budget_bytes, threads_per_child) from the resources
    this process really has (same recipe as zmip's lineage pool)."""
    from .resources import available_cpus, available_memory_bytes, current_rss_bytes

    cpus = available_cpus()
    cap = os.environ.get("PERSAMPLE_PARALLEL", "")
    if cap.strip().isdigit() and int(cap) > 0:
        max_parallel = min(int(cap), len(pending))
    else:
        max_parallel = max(1, min(len(pending), cpus // 2))
    budget = int(available_memory_bytes() * 0.85) - current_rss_bytes()
    threads = max(1, cpus // max(1, max_parallel))
    return max_parallel, budget, threads


def write_subsets(h5ad: Path, mapping, pending: list[dict]) -> None:
    """Regenerate pending subsets from the verified input, never reuse by name."""
    import anndata as ad
    import pandas as pd
    full = ad.read_h5ad(h5ad)
    for e in pending:
        outdir = Path(e["outdir"])
        outdir.mkdir(parents=True, exist_ok=True)
        ids = mapping.index[mapping[SAMPLE_KEY] == e["value"]]
        with writer_lock(outdir / ".writer.lock"):
            sub = full[ids].copy()
            sub.obs[SAMPLE_KEY] = e["value"]
            tmp = outdir / "subset.tmp.h5ad"
            sub.write_h5ad(tmp)
            os.replace(tmp, outdir / SUBSET_FILE)
            pd.DataFrame({"cell_id": ids}).to_csv(outdir / INPUT_CELLS, index=False,
                                                compression={"method": "gzip", "mtime": 0})
            e["request"]["subset_identity"] = file_identity(outdir / SUBSET_FILE)
            write_json(outdir / REQUEST, e["request"])
        print(f"[subset] {e['value']}: {len(ids)} cells", flush=True)


def _pump(proc: subprocess.Popen, tag: str, tail: deque, unit: Path | None = None) -> None:
    assert proc.stdout is not None
    for raw in proc.stdout:
        line = raw.rstrip("\n")
        tail.append(line)
        print(f"[{tag}] {line}", flush=True)
        if unit is not None:
            m = cost.COST_RE.search(line)
            if m:
                cost.record(unit, f"{L.PERSAMPLE}/{tag}", float(m.group("usd")), (m.group("label") or m.group("pre") or "").strip())


def drive(pending: list[dict], out_root: Path, annotate: bool, on_done=None) -> list[dict]:
    """Run every pending sample's command as a child process under the
    concurrency plan; one retry per sample; failures go to failures.md.
    Returns the entries that did not finish."""
    import resource

    if not pending:
        return []
    pending = sorted(pending, key=lambda e: -e["n_cells"])  # biggest first: it bounds the wall-clock
    max_parallel, budget, threads = plan_concurrency(pending)
    from .resources import available_cpus, available_memory_bytes

    print(f"[drive] {len(pending)} sample(s), up to {max_parallel} at once, {threads} thread(s) each, "
          f"memory budget {budget / 2**30:.1f} GiB ({available_cpus()} cpu(s), "
          f"{available_memory_bytes() / 2**30:.1f} GiB available)", flush=True)
    env = dict(os.environ)
    # Also supports OSP_PYTHON with only the kernel installed.
    env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent) + os.pathsep + env.get("PYTHONPATH", "")
    for k in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMBA_NUM_THREADS",
              "MSP_MAX_THREADS"):
        env[k] = str(threads)

    queue = list(pending)
    running: dict[str, tuple] = {}  # value -> (proc, est, t0, entry, tail)
    attempts: dict[str, int] = {}
    failed: list[dict] = []
    while queue or running:
        for value in list(running):
            proc, est, t0, e, tail = running[value]
            rc = proc.poll()
            if rc is None:
                continue
            del running[value]
            took = (time.time() - t0) / 60
            outdir = Path(e["outdir"])
            if rc == 0 and is_done(outdir, annotate, e.get("identity")):
                print(f"[drive] {value} done in {took:.1f} min", flush=True)
                (outdir / SUBSET_FILE).unlink(missing_ok=True)
                (outdir / "computed.h5ad").unlink(missing_ok=True)
                (outdir / "compute_state.json").unlink(missing_ok=True)
                if on_done:
                    on_done(e, took)
            elif (attempts[value] < 2 and (outdir / L.RUN_STATE).is_file()
                  and read_json(outdir / L.RUN_STATE).get("retryable") is True):
                print(f"[drive] {value} FAILED (exit {rc}) after {took:.1f} min — retrying once", flush=True)
                queue.append(e)
            else:
                print(f"[drive] {value} FAILED (exit {rc}) after {took:.1f} min — recorded, moving on",
                      flush=True)
                with open(out_root / "failures.md", "a") as f:
                    f.write(f"## {value} ({e['n_cells']} cells) — exit {rc}, {time.strftime('%Y-%m-%d %H:%M')}\n\n"
                            f"command: `{shlex.join(e['command'])}`\n\n```\n" + "\n".join(tail) + "\n```\n\n")
                failed.append(e)
        used = sum(est for _, est, _, _, _ in running.values())
        while queue and len(running) < max_parallel:
            e = queue[0]
            est = _estimate_bytes(e["n_cells"])
            if running and used + est > budget:
                break  # wait for memory; an idle pool always admits the next one
            queue.pop(0)
            value = e["value"]
            outdir = Path(e["outdir"])
            outdir.mkdir(parents=True, exist_ok=True)
            attempts[value] = attempts.get(value, 0) + 1
            tail: deque = deque(maxlen=40)
            proc = subprocess.Popen(e["command"], stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, env=env, bufsize=1,
                                    cwd=str(outdir))
            threading.Thread(target=_pump, args=(proc, value, tail, out_root.parent if out_root.name == L.PERSAMPLE else None), daemon=True).start()
            running[value] = (proc, est, time.time(), e, tail)
            used += est
            print(f"[drive] {value} started (attempt {attempts[value]}): {e['n_cells']} cells, "
                  f"est {est / 2**30:.1f} GiB, {len(running)} running, {len(queue)} waiting", flush=True)
        if running:
            time.sleep(5)
    peak = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss * 1024
    print(f"[drive] peak child RSS {peak / 2**30:.1f} GiB (largest sample {pending[0]['n_cells']} cells; "
          f"tune PERSAMPLE_MEM_PER_CELL_MB from this)", flush=True)
    return failed


# ---------------------------------------------------------------- cli


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="ecarsi.persample", description=__doc__)
    ap.add_argument("unit")
    ap.add_argument("out", nargs="?")
    for option in ("annotate", "scrublet", "decontx"):
        ap.add_argument("--" + option, action=argparse.BooleanOptionalAction, default=None)
    for option in ("species", "tissue", "language"):
        ap.add_argument("--" + option)
    ap.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"])
    ap.add_argument("--resolution", type=float)
    group = ap.add_mutually_exclusive_group()
    group.add_argument("--sample-column", help="explicit experiment column, scoped independently per source")
    group.add_argument("--single-sample", action="store_true", help="explicitly confirm one complete experiment")
    group.add_argument("--sample-map", help="JSON source decisions and explicit cross-source merges")
    ap.add_argument("--plan-only", action="store_true")
    args = ap.parse_args(argv)
    unit = Path(args.unit).resolve()
    bare = unit.suffix == ".h5ad"
    h5ad = unit if bare else L.input_h5ad(unit)
    out = Path(args.out).resolve() if args.out else (h5ad.parent / L.PERSAMPLE if bare else L.persample_root(unit))
    try:
        with writer_lock(out / ".driver.lock"):
            return _run(args, unit, h5ad, out, bare)
    except (ValueError, RuntimeError, OSError, KeyError, subprocess.CalledProcessError) as exc:
        print(f"[persample] {exc}")
        return 1


def _kernel_runtime(py: str) -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent) + os.pathsep + env.get("PYTHONPATH", "")
    result = subprocess.run([py, "-c", "import json; from ecarsi.run_state import runtime_identity; print(json.dumps(runtime_identity()))"],
                            capture_output=True, text=True, env=env, check=True)
    return json.loads(result.stdout)


def _run(args, unit, h5ad, out, bare):
    import pandas as pd
    import anndata as ad
    from . import agent_config, model as selected_model
    from .upstream import validate_matrix, verify_snapshots

    identity = file_identity(h5ad)
    a = ad.read_h5ad(h5ad, backed="r")
    try:
        validate_matrix(a)
    finally:
        a.file.close()
    metadata = None if bare else file_identity(L.input_manifest(unit))
    if not bare:
        verify_snapshots(L.input_manifest(unit).parent, read_json(L.input_manifest(unit)))
    path = out / L.MANIFEST
    old = read_json(path) if path.is_file() else None
    if old and old.get("schema_version") != 2:
        raise ValueError("legacy persample is readable but cannot claim verified resume; use a new output directory")
    config = {"annotate": True, "scrublet": True, "decontx": True, "resolution": 1.0,
              "species": None if bare else _upstream_species(unit), "tissue": None,
              "language": "English", "effort": None, **agent_config()}
    if old:
        config.update(old["config"])
    config.update(agent_config())
    for key in ("annotate", "scrublet", "decontx", "resolution", "species", "tissue", "language", "effort"):
        if getattr(args, key) is not None:
            config[key] = getattr(args, key)
    import math
    if not math.isfinite(config["resolution"]) or config["resolution"] <= 0:
        raise ValueError("resolution must be finite and positive")
    py = os.environ.get("OSP_PYTHON", sys.executable)
    runtime = _kernel_runtime(py)
    spec = read_json(Path(args.sample_map)) if args.sample_map else None
    explicit = {"sample_map": spec, "column": args.sample_column, "single": args.single_sample}
    if old:
        if (old["input_identity"] != identity or old.get("metadata_identity") != metadata or
                old["config"] != config or old["runtime"] != runtime):
            raise ValueError("input, configuration or runtime changed; use a new output directory")
        if any((spec is not None, args.sample_column, args.single_sample)) and old["explicit_mapping"] != explicit:
            raise ValueError("experiment mapping changed; use a new output directory")
        table = pd.read_csv(out / L.SAMPLE_MAPPING, index_col=0, dtype=str, keep_default_na=False)
        table.index = table.index.astype(str)
        if mapping_identity(table) != old["mapping_identity"]:
            raise ValueError("recorded cell/sample mapping changed")
        decision = old["sample_mapping"]
    else:
        table, decision = build_mapping(h5ad, None if bare else unit, spec, identify_sample_column,
                                        args.sample_column, args.single_sample)
    counts = {str(k): int(v) for k, v in table[SAMPLE_KEY].value_counts().items()}
    entries = build_entries(h5ad, SAMPLE_KEY, counts, out, py, config["annotate"], selected_model())
    run_identity = digest({"input": identity, "metadata": metadata, "config": config,
                           "runtime": runtime, "mapping": mapping_identity(table)})
    for e in entries:
        e["identity"] = digest([run_identity, e["value"]])
        e["request"] = {"identity": e["identity"], "config": config, "runtime": runtime,
                        "value": e["value"], "n_cells": e["n_cells"],
                        "context": None if bare else L.report_context(unit)}
    man = {"schema_version": 2, "state": old.get("state", "planned") if old else "planned", "h5ad": str(h5ad), **agent_config(),
           "input_identity": identity, "metadata_identity": metadata, "runtime": runtime, "config": config,
           "sample_column": SAMPLE_KEY, "sample_mapping": decision, "explicit_mapping": old["explicit_mapping"] if old else explicit,
           "mapping_identity": mapping_identity(table), "identity": run_identity,
           "species": config["species"], "tissue": config["tissue"], "annotate": config["annotate"],
           "rationale": "source-scoped experiment decisions; see sample_mapping",
           "samples": [{"value": e["value"], "n_cells": e["n_cells"], "dir": e["outdir"], "identity": e["identity"]} for e in entries]}
    if not old:
        table.to_csv(out / L.SAMPLE_MAPPING, index_label="cell_id")
    write_json(path, man)
    if args.plan_only:
        for e in entries:
            print(f"[plan] {e['value']} ({e['n_cells']} cells): {shlex.join(e['command'])}")
        return 0
    pending = [e for e in entries if not is_done(Path(e["outdir"]), config["annotate"], e["identity"])]
    failed = []
    if pending:
        man["state"] = "running"
        write_json(path, man)
        write_subsets(h5ad, table, pending)
        failed = drive(pending, out, config["annotate"])
    missing = [e["value"] for e in entries if not is_done(Path(e["outdir"]), config["annotate"], e["identity"])]
    man["state"] = "failed" if failed or missing else "complete"
    man["failed_samples"] = sorted(set(missing) | {e["value"] for e in failed})
    write_json(path, man)
    _write_review(unit, out, man, bare)
    if not bare:
        L.log_event(unit, f"persample {man['state']}: {len(entries)} experiments; {len(man['failed_samples'])} failed")
        from .index import write_all
        write_all(unit)
    print(f"[persample] {man['state']}: {len(entries)} samples; failures={man['failed_samples']}")
    return 1 if failed or missing else 0


def _write_review(unit, out, man, bare):
    items = []
    if not bare:
        upstream = read_json(L.input_manifest(unit)).get("upstream", {})
        for source, evidence in upstream.items():
            for step in ("standardize", "identify_columns"):
                result = evidence.get(step, {})
                for warning in result.get("reasons", []) + result.get("warnings", []):
                    items.append({"step": step, "source": source, "detail": warning})
    for entry in man["samples"]:
        state_path = out / Path(entry["dir"]).name / L.RUN_STATE
        state = read_json(state_path) if state_path.is_file() else {}
        qc = state.get("validation", {}).get("qc_summary", {})
        if str(qc.get("decontx_degenerate", "")).lower() == "true":
            items.append({"step": "osp", "source": entry["value"], "detail": "DecontX degenerate"})
        if state.get("state") == "failed":
            items.append({"step": "osp", "source": entry["value"], "detail": state.get("error")})
    write_json(out / "needs_review.json", {"items": items})
    (out / "needs_review.md").write_text("# 输入与每样本复核记录\n\n" + "\n".join(
        f"- {i['step']} / {i['source']}: {json.dumps(i['detail'], ensure_ascii=False)}" for i in items) + "\n")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

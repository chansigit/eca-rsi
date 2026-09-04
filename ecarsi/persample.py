"""ecarsi-persample — identify the 10x-run sample column, then run osp per sample.

    python -m ecarsi.persample <unit_dir | organized.h5ad> [out_dir] [--annotate]

Runs after ecarsi.organize, once per analysis unit. Four stages:

  1. PROFILE (code): obs-column profile of the organized h5ad.
  2. IDENTIFY (agent, submit tool): which obs column is the 10x-run-level
     sample column (null → whole file is one run). The decision is
     persisted to <out_dir>/manifest.json — re-runs reuse it.
  3. DRIVE (code): every pending sample's cells are written to
     <sample dir>/subset.h5ad and `python -m osp` runs on that subset as a
     child process; several samples run side by side, concurrency sized
     from what this process may actually use (ecarsi.resources: affinity
     CPUs + cgroup memory — nothing is passed in from the job script) and a
     per-sample memory estimate. A sample is done when its contract files
     exist (report.html + clustered.h5ad, plus annotation_proposal.json
     when annotating); done samples are skipped on resume. A failed sample
     is retried once, then recorded in <out_dir>/failures.md and skipped so
     it never blocks the rest.
  4. VERIFY (code): hard exit listing whatever is still missing.

Env: HARNESS / MODEL (identify agent + osp --annotate; defaults per harness),
OSP_PYTHON (interpreter that has osp installed; defaults to this
interpreter), PERSAMPLE_PARALLEL (hard cap on concurrent samples, 1 =
sequential; default min(#samples, cpus // 2)), PERSAMPLE_MEM_PER_CELL_MB
(RAM an osp run of N cells is assumed to need per cell, default 0.5, plus
1 GiB fixed per child — a 5k-cell Fu2022 sample peaked at 2.5 GiB).
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

SAMPLE_COL_SCHEMA = {
    "type": "object",
    "properties": {
        "sample_column": {"type": ["string", "null"]},
        "rationale": {"type": "string"},
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
    cols = {}
    for c in a.obs.columns:
        s = a.obs[c]
        nuniq = int(s.nunique(dropna=True))
        entry: dict = {"dtype": str(s.dtype), "n_unique": nuniq, "n_na": int(s.isna().sum())}
        if nuniq <= max_levels and (s.dtype == object or str(s.dtype) == "category"):
            # drop unused categorical levels — phantom zero counts would
            # pollute the profile the agent reasons over
            entry["value_counts"] = {str(k): int(v) for k, v in s.value_counts().items() if v}
        cols[str(c)] = entry
    prof = {"n_obs": int(a.n_obs), "obs_columns": cols}
    a.file.close()
    return prof


# ------------------------------------------------------- identify (agent)


def _validate_sample_column(decision: dict, profile: dict) -> str | None:
    """None if valid, else a problem description (fix-and-resubmit style)."""
    col = decision.get("sample_column")
    if col is None:
        return None
    info = profile["obs_columns"].get(col)
    if info is None:
        return f"picked obs column {col!r}, which does not exist"
    if not 1 <= info["n_unique"] <= 200:
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
        problem = _validate_sample_column(decision, profile)
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


def _osp_command(py: str, subset: Path, col: str, value: str, outdir: Path,
                 annotate: bool, model: str, species: str | None, tissue: str | None,
                 context: str | None = None) -> str:
    cmd = [py, "-m", "osp", str(subset), "--sample-col", col, "--sample", value,
           "--outdir", str(outdir)]
    if context:
        cmd += ["--report-context", context]
    if annotate:
        cmd += ["--annotate", "--model", model]
        if species:
            cmd += ["--species", species]
        if tissue:
            cmd += ["--tissue", tissue]
    return " ".join(shlex.quote(c) for c in cmd)


def _whole_file_command(py: str, h5ad: Path, outdir: Path, annotate: bool, model: str,
                        species: str | None, tissue: str | None, context: str | None = None) -> str:
    code = (
        "import scanpy as sc; from osp import run_one_sample_pipeline, generate_report; "
        "from osp.report import write_report_context; "
        f"write_report_context({str(outdir)!r}, {context!r}); "
        f"a = sc.read_h5ad({str(h5ad)!r}); a.obs['sample'] = 'all'; "
        f"run_one_sample_pipeline(a, sample_label='all', outdir={str(outdir)!r}); "
        f"print(generate_report({str(outdir)!r}))"
    )
    if annotate:
        code += (
            "; from osp.annotate import propose_annotation; "
            f"propose_annotation({str(outdir)!r}, model={model!r}, "
            f"species={species!r}, tissue={tissue!r})"
        )
    return " ".join(shlex.quote(c) for c in [py, "-c", code])


def build_entries(h5ad: Path, col: str | None, counts: dict[str, int], out_root: Path,
                  py: str, annotate: bool, model: str,
                  species: str | None = None, tissue: str | None = None,
                  context: str | None = None) -> list[dict]:
    entries: list[dict] = []
    seen: set[str] = set()
    for value, n in counts.items():
        base = _safe_name(value)
        name, i = base, 1
        while name in seen:  # dedup against names actually produced
            name, i = f"{base}_{i}", i + 1
        seen.add(name)
        outdir = out_root / name
        if col is None:
            command = _whole_file_command(py, h5ad, outdir, annotate, model, species, tissue, context)
        else:
            command = _osp_command(py, outdir / SUBSET_FILE, col, value, outdir, annotate, model,
                                   species, tissue, context)
        entries.append({"value": value, "n_cells": n, "outdir": str(outdir), "command": command})
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
    files = CONTRACT + (("annotation_proposal.json",) if annotate else ())
    return all(L.present(outdir / f) for f in files)


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


def write_subsets(h5ad: Path, col: str, pending: list[dict]) -> None:
    """One <sample dir>/subset.h5ad per pending sample (skipped when present),
    the sample column cast to str so osp's `--sample <value>` comparison
    holds whatever dtype the column had."""
    todo = [e for e in pending if not (Path(e["outdir"]) / SUBSET_FILE).is_file()]
    if not todo:
        return
    import scanpy as sc

    print(f"[subset] reading {h5ad.name} once to write {len(todo)} sample subset(s)", flush=True)
    full = sc.read_h5ad(h5ad)
    key = full.obs[col].astype(str)
    for e in todo:
        outdir = Path(e["outdir"])
        outdir.mkdir(parents=True, exist_ok=True)
        sub = full[(key == e["value"]).values].copy()
        sub.obs[col] = sub.obs[col].astype(str)
        tmp = outdir / (SUBSET_FILE + ".tmp")
        sub.write_h5ad(tmp)
        os.replace(tmp, outdir / SUBSET_FILE)
        print(f"[subset] {e['value']}: {sub.n_obs} cells -> {outdir / SUBSET_FILE}", flush=True)
    del full


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

    pending = sorted(pending, key=lambda e: -e["n_cells"])  # biggest first: it bounds the wall-clock
    max_parallel, budget, threads = plan_concurrency(pending)
    from .resources import available_cpus, available_memory_bytes

    print(f"[drive] {len(pending)} sample(s), up to {max_parallel} at once, {threads} thread(s) each, "
          f"memory budget {budget / 2**30:.1f} GiB ({available_cpus()} cpu(s), "
          f"{available_memory_bytes() / 2**30:.1f} GiB available)", flush=True)
    env = dict(os.environ)
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
            proc.stdout.close()
            del running[value]
            took = (time.time() - t0) / 60
            outdir = Path(e["outdir"])
            if rc == 0 and _is_done(outdir, annotate):
                print(f"[drive] {value} done in {took:.1f} min", flush=True)
                (outdir / SUBSET_FILE).unlink(missing_ok=True)
                if on_done:
                    on_done(e, took)
            elif attempts[value] < 2:
                print(f"[drive] {value} FAILED (exit {rc}) after {took:.1f} min — retrying once", flush=True)
                queue.append(e)
            else:
                print(f"[drive] {value} FAILED again (exit {rc}) after {took:.1f} min — recorded, moving on",
                      flush=True)
                with open(out_root / "failures.md", "a") as f:
                    f.write(f"## {value} ({e['n_cells']} cells) — exit {rc}, {time.strftime('%Y-%m-%d %H:%M')}\n\n"
                            f"command: `{e['command']}`\n\n```\n" + "\n".join(tail) + "\n```\n\n")
                (outdir / SUBSET_FILE).unlink(missing_ok=True)
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
            proc = subprocess.Popen(e["command"], shell=True, stdout=subprocess.PIPE,
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
    ap.add_argument("unit", help="organize unit dir (with input/organized.h5ad) or an h5ad path")
    ap.add_argument("out", nargs="?", help="output root (default <unit>/persample)")
    ap.add_argument("--annotate", action=argparse.BooleanOptionalAction, default=None,
                    help="run osp's annotation agent per sample (default on; --no-annotate to "
                         "skip; a resumed run reuses the recorded choice unless overridden)")
    ap.add_argument("--species", default=None, help="context passed to osp --annotate")
    ap.add_argument("--tissue", default=None, help="context passed to osp --annotate")
    ap.add_argument("--plan-only", action="store_true",
                    help="identify the sample column, write the manifest and print the "
                         "per-sample commands, but do not drive any osp run")
    args = ap.parse_args(argv)

    unit = Path(args.unit).resolve()
    bare = unit.suffix == ".h5ad"
    h5ad = unit if bare else L.input_h5ad(unit)
    if not h5ad.is_file():
        print(f"no input h5ad at {h5ad}")
        return 2
    out_root = Path(args.out).resolve() if args.out else (
        h5ad.parent / L.PERSAMPLE if bare else L.persample_root(unit)
    )
    from . import agent_config, check_agent_config, model as _model

    py = os.environ.get("OSP_PYTHON", sys.executable)
    model = _model()
    context = None if bare else L.report_context(unit)

    mpath = out_root / "manifest.json"
    if mpath.is_file():
        # resume must reproduce the recorded run unless the CLI explicitly
        # overrides — a bare re-invocation may not silently change behavior
        with open(mpath) as f:
            man = json.load(f)
        check_agent_config(man, str(mpath))
        col = man["sample_column"]
        counts = {s["value"]: s["n_cells"] for s in man["samples"]}
        annotate = man.get("annotate", True) if args.annotate is None else args.annotate
        species = args.species or man.get("species")
        tissue = args.tissue or man.get("tissue")
        print(f"[identify] reusing recorded sample column: {col!r} (species {species!r})")
        entries = build_entries(h5ad, col, counts, out_root, py, annotate, model,
                                species, tissue, context)
    else:
        profile = profile_obs(h5ad)
        decision = identify_sample_column(profile)
        cost.record(out_root.parent if out_root.name == L.PERSAMPLE else out_root, f"{L.PERSAMPLE}/identify", getattr(identify_sample_column, "last_cost", None), "identify sample column")
        col = decision["sample_column"]
        annotate = True if args.annotate is None else args.annotate
        species = args.species or (None if bare else _upstream_species(unit))
        tissue = args.tissue
        print(f"[identify] sample column: {col!r} (species {species!r}) — {decision['rationale']}")
        counts = list_samples(h5ad, col) if col is not None else {"all": profile["n_obs"]}
        entries = build_entries(h5ad, col, counts, out_root, py, annotate, model,
                                species, tissue, context)
        man = {
            "h5ad": str(h5ad),
            **agent_config(),
            "sample_column": col,
            "rationale": decision["rationale"],
            "species": species,
            "tissue": tissue,
            "annotate": annotate,
            # dir recorded so downstream (crosssample) never re-derives names
            "samples": [{"value": e["value"], "n_cells": e["n_cells"], "dir": e["outdir"]}
                        for e in entries],
        }
        out_root.mkdir(parents=True, exist_ok=True)
        with open(mpath, "w") as f:
            json.dump(man, f, indent=2)
    print(f"[samples] {len(entries)}: " + ", ".join(f"{e['value']}({e['n_cells']})" for e in entries))
    if args.plan_only:
        for e in entries:
            print(f"[plan] {e['value']}: {e['command']}")
        print("[plan-only] manifest written, nothing driven")
        return 0

    in_unit = not bare and L.is_unit(unit)
    if in_unit:
        L.log_event(unit, f"persample start: {len(entries)} sample(s), column {col!r}")
    pending = [e for e in entries if not _is_done(Path(e["outdir"]), annotate)]
    if pending:
        print(f"[drive] {len(pending)} sample(s) pending: " + ", ".join(e["value"] for e in pending))
        if col is not None:
            write_subsets(h5ad, col, pending)

        def on_done(e, took):
            if in_unit:
                L.log_event(unit, f"persample sample {e['value']} done: {e['n_cells']} cells, {took:.1f} min")
                from .index import write_all

                write_all(unit)

        drive(pending, out_root, annotate, on_done)
    missing = [e["value"] for e in entries if not _is_done(Path(e["outdir"]), annotate)]
    if missing:
        if in_unit:
            L.log_event(unit, "persample failed: incomplete samples " + ", ".join(missing))
        print("[fail] samples still incomplete after retry (see failures.md): " + ", ".join(missing))
        return 1
    if in_unit:
        L.log_event(unit, f"persample done: {len(entries)} sample(s)")
        from .index import write_all

        write_all(unit)
    print(f"[done] {len(entries)} sample(s) complete under {out_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

"""ecarsi-persample — identify the 10x-run sample column, then drive osp per sample.

    python -m ecarsi.persample <unit_dir | organized.h5ad> [out_dir] [--annotate]

Runs after ecarsi.organize, once per analysis unit. Four stages:

  1. PROFILE (code): obs-column profile of the organized h5ad.
  2. IDENTIFY (agent, structured output): which obs column is the
     10x-run-level sample column (null → whole file is one run). The
     decision is persisted to <out_dir>/manifest.json — re-runs reuse it.
  3. DRIVE (agent session): this step's own SDK session works through the
     sample checklist, one Task subagent per sample, each running the exact
     osp command it is handed. A sample is done when its contract files
     exist (report.html + clustered.h5ad, plus annotation_proposal.json
     when annotating); done samples are skipped on resume — finish one,
     cross one off.
  4. VERIFY (code): sessions are relaunched while they make progress (one
     no-progress retry), then hard exit listing whatever is missing.

Env: MODEL (both agent calls, default claude-sonnet-5), OSP_PYTHON
(interpreter that has osp installed; defaults to this interpreter).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shlex
import sys
from pathlib import Path

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


def identify_sample_column(profile: dict) -> dict:
    result = asyncio.run(_identify(profile))
    col = result["sample_column"]
    if col is not None:
        info = profile["obs_columns"].get(col)
        if info is None:
            raise ValueError(f"agent picked obs column {col!r}, which does not exist")
        if not 1 <= info["n_unique"] <= 200:
            raise ValueError(
                f"sample column {col!r} has {info['n_unique']} levels — implausible for 10x runs"
            )
        if info.get("n_na", 0) > 0:
            # a column that leaves cells unassigned is not a partition: those
            # cells would become a bogus "nan" sample (the silent-garbage trap)
            raise ValueError(
                f"sample column {col!r} leaves {info['n_na']} cells NA — not a valid partition"
            )
    return result


async def _identify(profile: dict) -> dict:
    from claude_agent_sdk import ClaudeAgentOptions, query

    brief = (Path(__file__).parent / "prompts" / "sample_column.md").read_text()
    prompt = brief + "\n\n## obs profile\n\n```json\n" + json.dumps(profile, indent=1) + "\n```\n"
    from . import model

    options = ClaudeAgentOptions(
        model=model(),
        allowed_tools=[],  # pure judgment over the profile, no tools
        max_turns=5,
        output_format={"type": "json_schema", "schema": SAMPLE_COL_SCHEMA},
    )
    result = None
    async for msg in query(prompt=prompt, options=options):
        so = getattr(msg, "structured_output", None)
        if so is not None:
            result = so
    if result is None:
        raise RuntimeError("sample-column agent ended without structured output")
    return result


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


def _osp_command(py: str, h5ad: Path, col: str, value: str, outdir: Path,
                 annotate: bool, model: str, species: str | None, tissue: str | None) -> str:
    cmd = [py, "-m", "osp", str(h5ad), "--sample-col", col, "--sample", value,
           "--outdir", str(outdir)]
    if annotate:
        cmd += ["--annotate", "--model", model]
        if species:
            cmd += ["--species", species]
        if tissue:
            cmd += ["--tissue", tissue]
    return " ".join(shlex.quote(c) for c in cmd)


def _whole_file_command(py: str, h5ad: Path, outdir: Path, annotate: bool, model: str,
                        species: str | None, tissue: str | None) -> str:
    code = (
        "import scanpy as sc; from osp import run_one_sample_pipeline, generate_report; "
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
                  species: str | None = None, tissue: str | None = None) -> list[dict]:
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
            command = _whole_file_command(py, h5ad, outdir, annotate, model, species, tissue)
        else:
            command = _osp_command(py, h5ad, col, value, outdir, annotate, model, species, tissue)
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
    return all((outdir / f).is_file() for f in files)


# ---------------------------------------------------------- drive (agent)


async def _drive(pending: list[dict], out_root: Path) -> None:
    from claude_agent_sdk import ClaudeAgentOptions, query

    brief = (Path(__file__).parent / "prompts" / "persample_driver.md").read_text()
    checklist = "\n".join(
        f"- sample {p['value']!r} ({p['n_cells']} cells) -> {p['outdir']}\n"
        f"  command: {p['command']}"
        for p in pending
    )
    prompt = brief.replace("{{OUT_ROOT}}", str(out_root)).replace("{{CHECKLIST}}", checklist)
    from . import model

    options = ClaudeAgentOptions(
        model=model(),
        allowed_tools=["Task", "Bash", "BashOutput", "Read", "Glob", "Grep", "Write"],
        permission_mode="bypassPermissions",
        cwd=str(out_root),
        max_turns=500,  # long runs burn many sleep/check turns per sample
    )
    async for _ in query(prompt=prompt, options=options):
        pass


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
    from . import model as _model

    py = os.environ.get("OSP_PYTHON", sys.executable)
    model = _model()

    mpath = out_root / "manifest.json"
    if mpath.is_file():
        # resume must reproduce the recorded run unless the CLI explicitly
        # overrides — a bare re-invocation may not silently change behavior
        with open(mpath) as f:
            man = json.load(f)
        col = man["sample_column"]
        counts = {s["value"]: s["n_cells"] for s in man["samples"]}
        annotate = man.get("annotate", True) if args.annotate is None else args.annotate
        species = args.species or man.get("species")
        tissue = args.tissue or man.get("tissue")
        print(f"[identify] reusing recorded sample column: {col!r} (species {species!r})")
        entries = build_entries(h5ad, col, counts, out_root, py, annotate, model,
                                species, tissue)
    else:
        profile = profile_obs(h5ad)
        decision = identify_sample_column(profile)
        col = decision["sample_column"]
        annotate = True if args.annotate is None else args.annotate
        species = args.species or (None if bare else _upstream_species(unit))
        tissue = args.tissue
        print(f"[identify] sample column: {col!r} (species {species!r}) — {decision['rationale']}")
        counts = list_samples(h5ad, col) if col is not None else {"all": profile["n_obs"]}
        entries = build_entries(h5ad, col, counts, out_root, py, annotate, model,
                                species, tissue)
        man = {
            "h5ad": str(h5ad),
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

    # keep opening driver sessions while they make progress (a session may
    # exhaust its turns mid-checklist on long runs); allow one no-progress
    # retry for transient deaths, then give up — per-sample retries already
    # happened inside the sessions
    in_unit = not bare and L.is_unit(unit)
    if in_unit:
        L.log_event(unit, f"persample start: {len(entries)} sample(s), column {col!r}")
    grace, prev = 1, None
    while True:
        pending = [e for e in entries if not _is_done(Path(e["outdir"]), annotate)]
        if not pending:
            break
        if prev is not None and len(pending) >= prev:
            if grace == 0:
                break
            grace -= 1
        prev = len(pending)
        print(f"[drive] {len(pending)} sample(s) pending: "
              + ", ".join(e["value"] for e in pending))
        asyncio.run(_drive(pending, out_root))
        if in_unit:
            from .index import write_all

            write_all(unit)

    missing = [e["value"] for e in entries if not _is_done(Path(e["outdir"]), annotate)]
    if missing:
        if in_unit:
            L.log_event(unit, "persample failed: incomplete samples " + ", ".join(missing))
        print("[fail] samples still incomplete after retry: " + ", ".join(missing))
        return 1
    if in_unit:
        L.log_event(unit, f"persample done: {len(entries)} sample(s)")
        from .index import write_all

        write_all(unit)
    print(f"[done] {len(entries)} sample(s) complete under {out_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

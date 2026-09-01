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
     (report.html + clustered.h5ad) exist; done samples are skipped on
     resume — finish one, cross one off.
  4. VERIFY (code): every sample's contract files must exist; one driver
     retry for stragglers, then hard exit listing what is missing.

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

SAMPLE_COL_SCHEMA = {
    "type": "object",
    "properties": {
        "sample_column": {"type": ["string", "null"]},
        "rationale": {"type": "string"},
    },
    "required": ["sample_column", "rationale"],
}

CONTRACT = ("report.html", "clustered.h5ad")


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
            entry["value_counts"] = {str(k): int(v) for k, v in s.value_counts().items()}
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
    options = ClaudeAgentOptions(
        model=os.environ.get("MODEL", "claude-sonnet-5"),
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
                 annotate: bool, model: str) -> str:
    cmd = [py, "-m", "osp", str(h5ad), "--sample-col", col, "--sample", value,
           "--outdir", str(outdir)]
    if annotate:
        cmd += ["--annotate", "--model", model]
    return " ".join(shlex.quote(c) for c in cmd)


def _whole_file_command(py: str, h5ad: Path, outdir: Path, annotate: bool, model: str) -> str:
    code = (
        "import scanpy as sc; from osp import run_one_sample_pipeline, generate_report; "
        f"a = sc.read_h5ad({str(h5ad)!r}); a.obs['sample'] = 'all'; "
        f"run_one_sample_pipeline(a, sample_label='all', outdir={str(outdir)!r}); "
        f"print(generate_report({str(outdir)!r}))"
    )
    if annotate:
        code += (
            "; from osp.annotate import propose_annotation; "
            f"propose_annotation({str(outdir)!r}, model={model!r})"
        )
    return " ".join(shlex.quote(c) for c in [py, "-c", code])


def build_entries(h5ad: Path, col: str | None, counts: dict[str, int], out_root: Path,
                  py: str, annotate: bool, model: str) -> list[dict]:
    entries: list[dict] = []
    used: dict[str, int] = {}
    for value, n in counts.items():
        base = _safe_name(value)
        name = base if base not in used else f"{base}_{used[base]}"
        used[base] = used.get(base, 0) + 1
        outdir = out_root / name
        if col is None:
            command = _whole_file_command(py, h5ad, outdir, annotate, model)
        else:
            command = _osp_command(py, h5ad, col, value, outdir, annotate, model)
        entries.append({"value": value, "n_cells": n, "outdir": str(outdir), "command": command})
    return entries


def _is_done(outdir: Path) -> bool:
    return all((outdir / f).is_file() for f in CONTRACT)


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
    options = ClaudeAgentOptions(
        model=os.environ.get("MODEL", "claude-sonnet-5"),
        allowed_tools=["Task", "Bash", "BashOutput", "Read", "Glob", "Grep", "Write"],
        permission_mode="bypassPermissions",
        cwd=str(out_root),
        max_turns=200,
    )
    async for _ in query(prompt=prompt, options=options):
        pass


# ---------------------------------------------------------------- cli


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="ecarsi.persample", description=__doc__)
    ap.add_argument("unit", help="organize unit dir (with input/organized.h5ad) or an h5ad path")
    ap.add_argument("out", nargs="?", help="output root (default <unit>/persample)")
    ap.add_argument("--annotate", action="store_true", help="pass --annotate to each osp run")
    args = ap.parse_args(argv)

    unit = Path(args.unit).resolve()
    h5ad = unit if unit.suffix == ".h5ad" else unit / "input" / "organized.h5ad"
    if not h5ad.is_file():
        print(f"no input h5ad at {h5ad}")
        return 2
    out_root = Path(args.out).resolve() if args.out else (
        h5ad.parent / "persample" if unit.suffix == ".h5ad" else unit / "persample"
    )
    py = os.environ.get("OSP_PYTHON", sys.executable)
    model = os.environ.get("MODEL", "claude-sonnet-5")

    mpath = out_root / "manifest.json"
    if mpath.is_file():
        with open(mpath) as f:
            man = json.load(f)
        col = man["sample_column"]
        counts = {s["value"]: s["n_cells"] for s in man["samples"]}
        print(f"[identify] reusing recorded sample column: {col!r}")
    else:
        profile = profile_obs(h5ad)
        decision = identify_sample_column(profile)
        col = decision["sample_column"]
        print(f"[identify] sample column: {col!r} — {decision['rationale']}")
        counts = list_samples(h5ad, col) if col is not None else {"all": profile["n_obs"]}
        man = {
            "h5ad": str(h5ad),
            "sample_column": col,
            "rationale": decision["rationale"],
            "annotate": bool(args.annotate),
            "samples": [{"value": v, "n_cells": n} for v, n in counts.items()],
        }
        out_root.mkdir(parents=True, exist_ok=True)
        with open(mpath, "w") as f:
            json.dump(man, f, indent=2)

    entries = build_entries(h5ad, col, counts, out_root, py, bool(args.annotate), model)
    print(f"[samples] {len(entries)}: " + ", ".join(f"{e['value']}({e['n_cells']})" for e in entries))

    for attempt in (1, 2):
        pending = [e for e in entries if not _is_done(Path(e["outdir"]))]
        if not pending:
            break
        print(f"[drive] attempt {attempt}: {len(pending)} sample(s) pending: "
              + ", ".join(e["value"] for e in pending))
        asyncio.run(_drive(pending, out_root))

    missing = [e["value"] for e in entries if not _is_done(Path(e["outdir"]))]
    if missing:
        print("[fail] samples still incomplete after retry: " + ", ".join(missing))
        return 1
    print(f"[done] {len(entries)} sample(s) complete under {out_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

"""ecarsi.layout — the ONE place that knows where every step's artefacts live.

Every ecarsi step derives its paths from here, never by spelling directory
names itself, so the whole run of one dataset is a single tree that can be
served as-is (ecarsi.serve) and rendered from disk alone (ecarsi.index):

    <root>/                                  organize's out_root = one dataset run
      index.html                             root landing page (ecarsi.index)
      organize/manifest.json                 detection, profiles, plan, audit
      units/<unit>/
        index.html                           unit landing page (ecarsi.index)
        progress.log                         every event of every step
        input/{organized.h5ad, manifest.json}
        persample/{manifest.json, <sample>/…}            osp, once
        rounds/roundNN/
          manifest.json                      round 1: inclusion decision + batch key
          input.h5ad                         round >= 2: previous survivors, r(N-1)_* priors
          crosssample/                       msp chain (integrate → inspect → annotate)
          zoomin/                            zmip (plan → per-lineage dirs → merge)
          ledger/                            cell_ledger.csv + sankeys (all rounds so far)
          stats.txt  decision.txt
        release/{final.h5ad, summary.md, needs_review.{md,json}, cell_ledger.csv, sankey_coarse.png}
                                             + pruned.json once ecarsi.prune has dropped the round h5ads
                                             (each leaves <file>.pruned; labelled ones also <file>.obs.parquet)

A unit is an analysis unit organize carved out of the input (e.g. one tissue
of one study); persample and the loop run per unit.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

ORGANIZE = "organize"
UNITS = "units"
INDEX = "index.html"
PROGRESS = "progress.log"
INPUT = "input"
PERSAMPLE = "persample"
ROUNDS = "rounds"
CROSSSAMPLE = "crosssample"
ZOOMIN = "zoomin"
LEDGER = "ledger"
RELEASE = "release"
STATS = "stats.txt"
DECISION = "decision.txt"
ROUND_INPUT = "input.h5ad"
MANIFEST = "manifest.json"

# step contracts — a step is complete when every file exists
PS_CONTRACT = ("report.html", "clustered.h5ad")
PS_ANNOTATE_CONTRACT = PS_CONTRACT + ("annotation_proposal.json",)
MSP_CONTRACT = ("integrated.h5ad", "report.html", "inspection_proposal.json",
                "annotation_proposal.json", "annotated.h5ad")
ZMIP_CONTRACT = ("zmip_plan.json", "annotated_zmip.h5ad", "report.html")
ZMIP_LINEAGE_CONTRACT = ("annotation_proposal.json", "annotated.h5ad", "report.html")


# ---------------------------------------------------------------- root / unit

def is_unit(p: Path) -> bool:
    return (p / INPUT / "organized.h5ad").is_file() or (p / INPUT / MANIFEST).is_file()


def is_root(p: Path) -> bool:
    return (p / ORGANIZE / MANIFEST).is_file() or (p / UNITS).is_dir()


def organize_manifest(root: Path) -> Path:
    return root / ORGANIZE / MANIFEST


def units_root(root: Path) -> Path:
    return root / UNITS


def unit_dir(root: Path, name: str) -> Path:
    return root / UNITS / name


def units(root: Path) -> list[Path]:
    ur = units_root(root)
    return sorted(p for p in ur.iterdir() if p.is_dir() and is_unit(p)) if ur.is_dir() else []


def root_of(unit: Path) -> Path | None:
    """The dataset root a unit lives in (None for a unit run outside a root)."""
    return unit.parent.parent if unit.parent.name == UNITS else None


# ---------------------------------------------------------------- unit parts

def input_h5ad(unit: Path) -> Path:
    return unit / INPUT / "organized.h5ad"


def input_manifest(unit: Path) -> Path:
    return unit / INPUT / MANIFEST


def persample_root(unit: Path) -> Path:
    return unit / PERSAMPLE


def persample_manifest(unit: Path) -> Path:
    return unit / PERSAMPLE / MANIFEST


def sample_dir(unit: Path, entry: dict) -> Path:
    """A persample manifest entry's directory, located under THIS unit's
    persample/ by its basename — the manifest records an absolute path,
    which must not break when a run directory is moved or copied."""
    return persample_root(unit) / Path(entry["dir"]).name


def sample_dirs(unit: Path) -> list[Path]:
    """Sample dirs from the persample manifest (else any dir with a report)."""
    import json

    mp = persample_manifest(unit)
    if mp.is_file():
        with open(mp) as f:
            return [sample_dir(unit, s) for s in json.load(f).get("samples", [])]
    pr = persample_root(unit)
    return sorted(p for p in pr.iterdir() if p.is_dir() and (p / "report.html").is_file()) if pr.is_dir() else []


def rounds_root(unit: Path) -> Path:
    return unit / ROUNDS


def round_dir(unit: Path, n: int) -> Path:
    return unit / ROUNDS / f"round{n:02d}"


def round_number(rdir: Path) -> int:
    m = re.fullmatch(r"round(\d+)", rdir.name)
    if not m:
        raise ValueError(f"not a round dir: {rdir}")
    return int(m.group(1))


def rounds(unit: Path) -> list[Path]:
    """Existing round dirs in order (any that has started)."""
    rr = rounds_root(unit)
    return sorted((p for p in rr.glob("round[0-9]*") if p.is_dir()), key=round_number) if rr.is_dir() else []


def crosssample_dir(rdir: Path) -> Path:
    return rdir / CROSSSAMPLE


def zoomin_dir(rdir: Path) -> Path:
    return rdir / ZOOMIN


def ledger_dir(rdir: Path) -> Path:
    return rdir / LEDGER


def release_dir(unit: Path) -> Path:
    return unit / RELEASE


def slug(name: str) -> str:
    """Lineage dir name inside zoomin/ — same rule as msp.plots.slug."""
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(name))


def lineage_dir(zdir: Path, lineage: str) -> Path:
    return zdir / slug(lineage)


PRUNED_SUFFIX = ".pruned"  # marker ecarsi.prune leaves where an intermediate h5ad used to be


def present(p: Path) -> bool:
    """The file is there, or was pruned after doing its job (ecarsi.prune
    leaves <file>.pruned) — either way the step that produced it is done."""
    return p.is_file() or p.with_name(p.name + PRUNED_SUFFIX).is_file()


def complete(d: Path, contract: tuple[str, ...]) -> bool:
    return all(present(d / f) for f in contract)


def report_context(unit: Path, rdir: Path | None = None) -> str:
    """Text the kernels put in their report titles (--report-context):
    'round N · <unit>' inside a round, else '<unit>'."""
    if rdir is not None:
        try:
            return f"round {round_number(rdir)} · {unit.name}"
        except ValueError:
            pass
    return unit.name


# ---------------------------------------------------------------- progress log

def log_event(unit: Path, event: str, echo: bool = True) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {event}"
    if echo:
        print(f"[{unit.name}] {line}", flush=True)
    unit.mkdir(parents=True, exist_ok=True)
    with open(unit / PROGRESS, "a") as f:
        f.write(line + "\n")


def read_log(unit: Path) -> list[tuple[str, str]]:
    """[(timestamp, event)] from progress.log."""
    p = unit / PROGRESS
    if not p.is_file():
        return []
    out = []
    for line in p.read_text().splitlines():
        if len(line) > 20:
            out.append((line[:19], line[20:]))
    return out

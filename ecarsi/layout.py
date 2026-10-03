"""ecarsi.layout — the ONE place that knows where every step's artefacts live.

Every ecarsi step derives its paths from here, never by spelling directory
names itself, so the whole run of one dataset is a single tree that can be
served as-is (ecarsi.serve) and rendered from disk alone (ecarsi.index).

Two layouts. The control plane writes generation 2 (00-organize/, 01-per-sample/, rounds/roundNN/
{02-cross-sample,03-zoom-in}/, publication.json files; the GEN2_* names below). Generation 1 is the
layout of the local path, removed in 0.4.0; its runs are still shown from their display zones, so
the pages keep reading it:

    <root>/                                  organize's out_root = one dataset run
      index.html                             root landing page (ecarsi.index)
      organize/manifest.json                 detection, profiles, plan, audit
      units/<unit>/
        index.html                           unit landing page (ecarsi.index)
        progress.log                         every event of every step
        input/{organized.h5ad, manifest.json}
        persample/{manifest.json, excluded_cells.csv, <sample>/…}   osp, once
        rounds/roundNN/
          manifest.json                      round 1: inclusion decision + batch key
          input.h5ad                         round >= 2: previous survivors, r(N-1)_* priors
          crosssample/                       msp chain (integrate → inspect → annotate)
          zoomin/                            zmip (plan → per-lineage dirs → merge)
          ledger/                            cell_ledger.csv + sankeys (all rounds so far)
          stats.txt  decision.txt
        release/{final.h5ad, summary.md, needs_review.{md,json}, cell_ledger.csv, sankey_coarse.png}
                                             + pruned.json once the local path had dropped the round h5ads
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
RUN_STATE = "run_state.json"
UPSTREAM = "upstream"
SAMPLE_MAPPING = "sample_mapping.csv.gz"
DISPLAY = "display.json"  # root of a display-zone copy: {collection, dataset, run, source, work} (ops/display-zone.py)
EXCLUDED_CELLS = "excluded_cells.csv"  # persample/: cells a sample-map policy dropped before OSP (ledger source)

# step contracts — a step is complete when every file exists
PS_CONTRACT = ("report.html", "clustered.h5ad")
# Strong front-pipeline validation uses these in addition to PS_CONTRACT.
# Legacy display and the frozen downstream contracts stay readable.
PS_QC_CONTRACT = ("qc_summary.csv", "qc_removed.csv")
# What a landing page may rely on to tell a step is finished: only the light
# files a display copy carries (never an h5ad), one per step, all written at
# that step's end. Computation keeps validating against the full contracts.
PS_LIGHT = ("report.html", "qc_summary.csv")
PS_ANNOTATE_LIGHT = PS_LIGHT + ("annotation_proposal.json",)
MSP_INTEGRATED_LIGHT = ("integration_summary.csv",)
MSP_LIGHT = ("inspection_proposal.json", "annotation_proposal.json", "annotation_removed.csv", "report.html")
ZMIP_LIGHT = ("zmip_plan.json", "zmip_removed.csv", "report.html")
ZMIP_LINEAGE_LIGHT = ("annotation_proposal.json", "report.html")


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


# Generation 2 (durable control plane): the same units/<name>/rounds/roundNN skeleton, but the state
# is in publication.json files, organize is published under 00-organize/ and each stage has its own
# numbered directory; the computed artefacts stay in the Pool's request folders.
GEN2_SPEC, GEN2_ORGANIZE, GEN2_PUBLICATION = "spec.json", "00-organize", "publication.json"
GEN2_PERSAMPLE, GEN2_CROSS, GEN2_ZOOM = "01-per-sample", "02-cross-sample", "03-zoom-in"


def is_gen2_root(root: Path) -> bool:
    return (root / GEN2_SPEC).is_file() and (root / GEN2_ORGANIZE).is_dir()


def is_gen2_unit(unit: Path) -> bool:
    if (unit / GEN2_PERSAMPLE / GEN2_PUBLICATION).is_file():
        return True
    if not is_gen2_root(unit.parent.parent):
        return False
    # A unit the control plane is still working on has a stage directory long before
    # anything publishes; the landing page renders mid-run, as it does for generation 1.
    return ((unit / GEN2_PUBLICATION).is_file()
            or any((unit / d).is_dir() for d in (GEN2_PERSAMPLE, GEN2_CROSS, GEN2_ZOOM)))


def gen2_organize_manifest(root: Path) -> Path:
    return root / GEN2_ORGANIZE / ORGANIZE / MANIFEST


def units(root: Path) -> list[Path]:
    ur = units_root(root)
    return sorted(p for p in ur.iterdir() if p.is_dir() and (is_unit(p) or is_gen2_unit(p))) if ur.is_dir() else []


def root_of(unit: Path) -> Path | None:
    """The dataset root a unit lives in (None for a unit run outside a root)."""
    return unit.parent.parent if unit.parent.name == UNITS else None


def fleet_place(path: Path) -> tuple[str, str] | None:
    """(collection, dataset) a path inside a fleet tree names: `<coll>/eca-pp/<dataset>/...`, or the direct
    study layout `<coll>/<study>/{standardize,rsi}` (the study is the dataset). None outside both."""
    path = Path(path)
    parts = path.parts
    if "eca-pp" in parts[1:-1]:
        i = parts.index("eca-pp")
        return parts[i - 1], parts[i + 1]
    if path.name in ("rsi", "standardize") and (path.parent / "standardize" / "result.json").is_file():
        return path.parent.parent.name, path.parent.name
    return None


def base_of(target: Path) -> Path:
    """The tree one run of one dataset occupies: the root, or a bare unit
    run outside any root: what a display zone copies from."""
    return target if is_root(target) else (root_of(target) or target) if is_unit(target) else target


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

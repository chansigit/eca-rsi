"""ecarsi.prune — drop the intermediate h5ads of a released unit.

    python -m ecarsi.prune <root | unit> [--dry-run]

A released run keeps every round's full-matrix h5ads around: for pbmc68k
(68k cells, 4 rounds) that is 31 GB of which release/final.h5ad is 1.2 GB.
Runs now live directly on Oak, so this is what makes many runs affordable.

What goes (only after release/summary.md exists and release/final.h5ad is
readable — otherwise this refuses to touch anything):

    persample/<sample>/{clustered,subset}.h5ad
    rounds/roundNN/input.h5ad
    rounds/roundNN/crosssample/{integrated,annotated}.h5ad
    rounds/roundNN/zoomin/annotated_zmip.h5ad
    rounds/roundNN/zoomin/<lineage>/{integrated,annotated}.h5ad

What stays: input/organized.h5ad (the provenance copy of the input),
release/ (final.h5ad, ledger, umap.json, ...), and every csv / json / png /
report — the audit trail is untouched.

Two things make a pruned unit still behave:
  * every deleted file leaves a `<file>.pruned` marker (size, time, sidecar),
    and layout.complete()/present() treat the marker as satisfying a step
    contract, so resume logic and the landing pages don't think the step
    never ran;
  * files that carry per-cell labels leave their full obs table next to
    them (`<file>.obs.parquet`, or .obs.csv.gz without pyarrow), and
    ledger._obs() falls back to it — so --force-reopen can still rebuild
    the cell ledger across all rounds.

release/pruned.json records what was removed and how much was reclaimed.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import layout as L

# h5ads whose obs carries labels the ledger joins later -> keep obs as a sidecar
LABELLED = {"clustered.h5ad", "annotated.h5ad", "annotated_zmip.h5ad"}


def targets(unit: Path) -> list[Path]:
    out: list[Path] = []
    for d in L.sample_dirs(unit):
        out += [d / "clustered.h5ad", d / "subset.h5ad"]
    for rdir in L.rounds(unit):
        out.append(rdir / L.ROUND_INPUT)
        c, z = L.crosssample_dir(rdir), L.zoomin_dir(rdir)
        out += [c / "integrated.h5ad", c / "annotated.h5ad", z / "annotated_zmip.h5ad"]
        if z.is_dir():
            out += sorted(p for p in z.glob("*/*.h5ad") if p.name in ("integrated.h5ad", "annotated.h5ad"))
    return [p for p in out if p.is_file()]


def _save_obs(h5ad: Path) -> Path:
    import anndata as ad

    a = ad.read_h5ad(h5ad, backed="r")
    obs = a.obs.copy()
    a.file.close()
    obs.index.name = "cell"
    try:
        side = h5ad.with_name(h5ad.name + ".obs.parquet")
        obs.to_parquet(side)
    except Exception:  # no pyarrow: fall back to something that needs nothing
        side = h5ad.with_name(h5ad.name + ".obs.csv.gz")
        obs.to_csv(side, compression="gzip")
    return side


def prune_unit(unit: Path, dry_run: bool = False) -> dict:
    rel = L.release_dir(unit)
    final = rel / "final.h5ad"
    if not (rel / "summary.md").is_file():
        raise SystemExit(f"[prune] {unit.name}: not released — refusing to prune an unfinished run")
    if not final.is_file() or final.stat().st_size == 0:
        raise SystemExit(f"[prune] {unit.name}: {final} missing or empty — refusing to prune")
    files = targets(unit)
    removed, total = [], 0
    for p in files:
        size = p.stat().st_size
        rec = {"file": str(p.relative_to(unit)), "bytes": size}
        if not dry_run:
            if p.name in LABELLED:
                rec["obs"] = _save_obs(p).name
            p.unlink()
            p.with_name(p.name + L.PRUNED_SUFFIX).write_text(json.dumps(
                {"bytes": size, "pruned": time.strftime("%Y-%m-%d %H:%M:%S"), "obs": rec.get("obs")}))
        removed.append(rec)
        total += size
        print(f"[prune] {'would remove' if dry_run else 'removed'} {rec['file']} ({size / 2**30:.2f} GiB)", flush=True)
    summary = {"unit": unit.name, "dry_run": dry_run, "files": len(removed), "bytes": total,
               "kept": [str(L.input_h5ad(unit).relative_to(unit)), L.RELEASE + "/"], "removed": removed,
               "when": time.strftime("%Y-%m-%d %H:%M:%S")}
    if not dry_run:
        (rel / "pruned.json").write_text(json.dumps(summary, indent=2))
        L.log_event(unit, f"prune: removed {len(removed)} intermediate h5ad(s), {total / 2**30:.1f} GiB reclaimed")
    else:
        print(f"[prune] dry run: {len(removed)} file(s), {total / 2**30:.1f} GiB")
    return summary


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="ecarsi.prune", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target", help="organize root (every released unit) or one unit dir")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    t = Path(a.target).resolve()
    units = [t] if L.is_unit(t) else L.units(t) if L.is_root(t) else []
    if not units:
        print(f"[prune] {t} is neither an organize root nor a unit dir")
        return 2
    for u in units:
        if not (L.release_dir(u) / "summary.md").is_file():
            print(f"[prune] {u.name}: not released, skipped")
            continue
        prune_unit(u, a.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

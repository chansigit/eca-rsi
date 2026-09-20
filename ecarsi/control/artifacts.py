"""Copy what a person reads out of the pool and into the run directory.

A finished stage leaves its report, figures and tables inside the pool request that
produced them, and its publication references them by path and digest. That request is
a replay cache the run does not own: archiving or clearing it would orphan every report.
The matrices stay where they are -- only the light artefacts are copied, which is also
what `--mirror` and the release keep.
"""
import shutil
from pathlib import Path

HEAVY = (".h5ad", ".zarr", ".parquet")


def copy_light(files, folder):
    """`files` is a publication's {name: {path, sha256}} map. Copying never raises:
    a missing report is worth a warning, not a failed stage."""
    folder = Path(folder)
    for name, ref in sorted((files or {}).items()):
        if name.endswith(HEAVY):
            continue
        target, source = folder / name, Path(ref["path"])
        try:
            if target.is_file() and target.stat().st_size == source.stat().st_size:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            partial = target.with_name(target.name + ".part")
            shutil.copyfile(source, partial)
            partial.replace(target)
        except OSError as exc:
            print(f"[publish] warning: {folder.name}/{name} not copied: {exc}", flush=True)

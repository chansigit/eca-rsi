"""ecarsi.mirror — keep a long-term copy (Oak) of a run root that lives on fast scratch.

    eca-rsi run|organize|persample|loop ... --mirror DIR

DIR is recorded once in <root>/mirror.json (ecarsi.layout.mirror_file), so
later steps and resumes find it without the flag. From then on:

  * every time a landing page is written (ecarsi.index.write_all — organize
    done, each persample sample done, every round stage, release) the LIGHT
    files of the whole run root are copied to DIR: pages, progress.log,
    manifests / state json, stats / decision, markdown, reports, figures,
    small tables. Never h5ad / parquet / csv.gz / big tables / dot-dirs.
    Unchanged files (same size and mtime) are not rewritten; copies keep the
    source mtime, so the pages' "run state updated" stamp is honest on DIR.
  * at release (after prune) everything that survived is copied, and files
    that no longer exist in the released unit are removed from DIR's copy of
    that unit — nowhere else — so DIR is a complete standalone copy.

The mirror is write-only: no step ever reads DIR to decide anything, and a
failure to mirror is one warning line (stdout + progress.log), never a
failed step. `eca-rsi index <root>` re-mirrors by hand.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from . import layout as L
from .run_state import read_json, write_json

LIGHT_SUFFIXES = {".html", ".md", ".json", ".txt", ".log", ".png", ".svg", L.PRUNED_SUFFIX}
LIGHT_CSV_MAX = 1 << 20      # small tables ride along (qc_summary, decisions); ledgers and DE tables wait for release
NEVER = {".lock", ".tmp"}  # process locks and half-written files
TMP_SUFFIX = ".tmp-mirror"


def is_light(rel: Path, size: int) -> bool:
    """Does this file (path relative to the run root) belong to the light set?"""
    if any(part.startswith(".") for part in rel.parts) or rel.suffix in NEVER:
        return False
    if rel.suffix in LIGHT_SUFFIXES:
        return True
    return rel.suffix == ".csv" and size <= LIGHT_CSV_MAX


def _mirrored(rel: Path, size: int, full: bool) -> bool:
    if full:  # everything but dot-dirs (.cache, release staging) and locks
        return not any(p.startswith(".") for p in rel.parts[:-1]) and rel.suffix not in NEVER
    return is_light(rel, size)


# ---------------------------------------------------------------- config

def configure(base: Path, dest: Path | str) -> Path:
    """Record DIR as the mirror of this run root (idempotent; a new DIR replaces the old)."""
    dest = Path(dest).resolve()
    if dest == base or base in dest.parents or dest in base.parents:
        raise SystemExit(f"[mirror] --mirror {dest} must be outside the run root {base}")
    write_json(L.mirror_file(base), {"mirror": str(dest), "source": str(base)})
    print(f"[mirror] {base} -> {dest}", flush=True)
    return dest


def read(base: Path) -> dict | None:
    p = L.mirror_file(base)
    return read_json(p) if p.is_file() else None


def copy_notice(target: Path) -> str | None:
    """'mirror copy of <source>' when target IS the mirror (its mirror.json
    names itself), for the landing-page footer; None on the source side."""
    base = L.base_of(target)
    rec = read(base)
    if rec and Path(rec.get("mirror", "")) == base.resolve():
        return rec.get("source", "?")
    return None


# ---------------------------------------------------------------- sync

def _warn(target: Path, msg: str) -> None:
    print(f"[mirror] WARNING: {msg}", flush=True)
    if L.is_unit(target):
        L.log_event(target, f"mirror warning: {msg}", echo=False)


def _copy(src: Path, dst: Path) -> bool:
    """copy2 into a temp name then os.replace; False if src vanished meanwhile."""
    tmp = dst.with_name(dst.name + TMP_SUFFIX)
    try:
        shutil.copy2(src, tmp)
        os.replace(tmp, dst)
        return True
    except FileNotFoundError:
        return False
    finally:
        tmp.unlink(missing_ok=True)


def _copy_tree(base: Path, dest: Path, full: bool) -> tuple[int, int]:
    copied = nbytes = 0
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        d = Path(dirpath)
        for name in sorted(filenames):
            if name.endswith(TMP_SUFFIX):
                continue
            src = d / name
            try:
                st = src.stat()
            except OSError:
                continue  # vanished or dangling symlink
            rel = src.relative_to(base)
            if not _mirrored(rel, st.st_size, full):
                continue
            dst = dest / rel
            try:
                ds = dst.stat()
                if (ds.st_size, ds.st_mtime_ns) == (st.st_size, st.st_mtime_ns):
                    continue
            except FileNotFoundError:
                dst.parent.mkdir(parents=True, exist_ok=True)
            if _copy(src, dst):
                copied += 1
                nbytes += st.st_size
    return copied, nbytes


def _remove_extras(base: Path, dest: Path, scope: Path) -> int:
    """Delete files under dest/scope that base/scope no longer has (pruned
    intermediates, superseded release files). Dot-dirs are never touched."""
    removed = 0
    for dirpath, dirnames, filenames in os.walk(dest / scope):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        d = Path(dirpath)
        for name in filenames:
            rel = (d / name).relative_to(dest)
            if not (base / rel).exists():
                (d / name).unlink(missing_ok=True)
                removed += 1
    return removed


def sync(target: Path, full: bool = False) -> dict | None:
    """Mirror the run root of `target` (a root or a unit) if one is configured.
    full=True (release): copy everything and drop what `target`'s subtree lost."""
    base = L.base_of(target)
    rec = read(base)
    if not rec:
        return None
    dest = Path(rec["mirror"])
    try:
        if dest.resolve() == base.resolve() or base.resolve() in dest.resolve().parents:
            raise ValueError("mirror dir is the run root itself")
        copied, nbytes = _copy_tree(base, dest, full)
        removed = _remove_extras(base, dest, target.resolve().relative_to(base.resolve())) if full else 0
    except Exception as e:  # never fail the run over its copy
        _warn(target, f"mirror to {dest} failed: {e}")
        return None
    what = "full copy" if full else "light files"
    print(f"[mirror] {what} -> {dest}: {copied} file(s) copied ({nbytes / 2**20:.1f} MiB), {removed} removed", flush=True)
    return {"mirror": str(dest), "copied": copied, "bytes": nbytes, "removed": removed}

"""A run's external display zone (#25): the files Periscope needs to show it, kept apart from the work tree.

The work tree (`spec['output_root']`) holds everything the system needs to resume and replay a run; the
display zone holds what a person reads, on durable storage, from the first stage on. Which files those are
is decided by Periscope itself: `closure` renders the run's pages, records every file inside the run that
the renderer opens or stats, and follows every link through the pages and their reports. `sync` copies
those that changed since the last sync, keeping relative paths and mtimes, and writes `display.json`
last -- the record Periscope's display_roots scan serves the copy under. ops/display-zone.py proved the
rule on 463 old runs (2026-10-02): 40-150 of up to 28k files, pages identical to the originals.
"""
import json
import os
import re
import shutil
import sys
import time
import uuid
from pathlib import Path

from . import layout as L

_touched: set[str] = set()
_installed = False
_recording = False  # only while closure() renders


def zone(spec) -> dict | None:
    """Where this run's display zone and work archive go (spec['storage']); None for a run without one."""
    storage = spec.get("storage")
    if not storage:
        return None
    collection, dataset = L.fleet_place(Path(spec["input_root"])) or ("other", Path(spec["input_root"]).name)
    run = spec["run_id"]
    return dict(dest=str(Path(storage["display_root"]) / collection / dataset / run),
                record=dict(name=Path(spec["output_root"]).name, collection=collection, dataset=dataset, run=run,
                            source=spec["output_root"],
                            work=str(Path(storage["archive_root"]) / collection / dataset / (run + ".tar.gz"))))


def _install():
    """Record the paths the renderer opens (audit hook) and stats (it decides from existence and mtimes
    too: release/receipt.json makes a unit "released"). Process-wide, so only for the display program."""
    global _installed
    if _installed:
        return
    sys.addaudithook(lambda event, args: _recording and event == "open" and isinstance(args[0], (str, bytes, os.PathLike))
                     and _touched.add(os.path.abspath(os.fsdecode(args[0]))))

    def recorded(real):
        def stat(path, *args, **kwargs):
            result = real(path, *args, **kwargs)
            if _recording and isinstance(path, (str, bytes, os.PathLike)):
                _touched.add(os.path.abspath(os.fsdecode(path)))
            return result
        return stat
    os.stat, os.lstat = recorded(os.stat), recorded(os.lstat)
    _installed = True


def closure(root: Path, name: str) -> set[Path]:
    """Relative paths of the files inside `root` that its Periscope pages need."""
    global _recording
    from .ui import index
    _install()
    root = Path(root).resolve()
    _touched.clear()
    units = sorted(u for u in (root / L.UNITS).iterdir() if u.is_dir()) if (root / L.UNITS).is_dir() else []
    _recording = True
    try:
        pages = [index.render_root(root, name)] + [index.render_unit(u, name) for u in units]
    finally:
        _recording = False
    found = {Path(p) for p in _touched}
    todo, seen = [(page, root) for page in pages], set()
    while todo:
        text, base = todo.pop()
        for target in re.findall(r'(?:href|src)="([^"#?]+)', text):
            if re.match(r"^[a-z]+:|^/_", target):
                continue
            path = Path(os.path.normpath(target if target.startswith("/") else base / target))
            if path in seen or not path.is_file():
                continue
            seen.add(path)
            found.add(path)
            if path.suffix == ".html":
                todo.append((path.read_text(errors="ignore"), path.parent))
    return {p.relative_to(root) for p in found if root in p.parents and p.is_file()}


def sync(root: Path, dest: Path, record: dict) -> dict:
    """Copy the display files that changed into `dest`, then (re)write dest/display.json."""
    root, dest = Path(root).resolve(), Path(dest)
    start, copied = time.time(), 0
    need = closure(root, record["name"])
    for rel in sorted(need):
        source, target = root / rel, dest / rel
        st = source.stat()
        try:
            old = target.stat()
            if old.st_size == st.st_size and int(old.st_mtime) == int(st.st_mtime):
                continue
        except FileNotFoundError:
            target.parent.mkdir(parents=True, exist_ok=True)
        # a name of its own: syncs of one zone can overlap (four began together at the end of test1002-shi)
        partial = target.with_name(f"{target.name}.{uuid.uuid4().hex}.partial")
        shutil.copy2(source, partial)
        os.replace(partial, target)
        copied += 1
    dest.mkdir(parents=True, exist_ok=True)
    partial = dest / f"{L.DISPLAY}.{uuid.uuid4().hex}.partial"
    partial.write_text(json.dumps(dict(record, synced_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"), files=len(need)), indent=1) + "\n")
    os.replace(partial, dest / L.DISPLAY)
    return dict(dest=str(dest), files=len(need), copied=copied, seconds=round(time.time() - start, 1))

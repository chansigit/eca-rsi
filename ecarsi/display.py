"""A run's external display zone (#25): the files Periscope needs to show it, kept apart from the work tree.

The work tree (`spec['output_root']`) holds everything the system needs to resume and replay a run; the
display zone holds what a person reads, on durable storage, from the first stage on. Which files those are
is decided by Periscope itself: `closure` renders the run's pages, records every file inside the run that
the renderer opens or stats, and follows every link through the pages and their reports (a unit page's links
from its unit). `broken_links` follows the same links and names those that reach no file of the zone. `sync` copies
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
from .contracts import check

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


def _pages(root: Path, name: str) -> list[tuple[str, Path]]:
    """The run's landing page and its units' pages, each with the directory its relative links start from."""
    from .ui import index
    units = sorted(u for u in (root / L.UNITS).iterdir() if u.is_dir()) if (root / L.UNITS).is_dir() else []
    return [(index.render_root(root, name), root)] + [(index.render_unit(u, name), u) for u in units]


def _follow(root: Path, pages) -> tuple[set[Path], dict[Path, Path]]:
    """Follow every link of the pages and of the HTML files they reach: (files inside root, {target that is not one:
    the page linking to it}). Periscope's routes are not files: a scheme, /_..., the landing pages it renders."""
    rendered = {base / L.INDEX for _, base in pages}
    todo, seen, found, missing = [(text, base, base / L.INDEX) for text, base in pages], set(), set(), {}
    while todo:
        text, base, page = todo.pop()
        for target in re.findall(r'(?:href|src)="([^"#?]+)', text):
            if re.match(r"^[a-z]+:|^/_", target):
                continue
            path = Path(os.path.normpath(target if target.startswith("/") else base / target))
            if path in seen or path in rendered:
                continue
            seen.add(path)
            if root not in path.parents or not path.is_file():
                missing[path] = page
                continue
            found.add(path)
            if path.suffix == ".html":
                todo.append((path.read_text(errors="ignore"), path.parent, path))
    return found, missing


def closure(root: Path, name: str) -> set[Path]:
    """Relative paths of the files inside `root` that its Periscope pages need."""
    global _recording
    _install()
    root = Path(root).resolve()
    _touched.clear()
    _recording = True
    try:
        pages = _pages(root, name)
    finally:
        _recording = False
    found = {Path(p) for p in _touched} | _follow(root, pages)[0]
    return {p.relative_to(root) for p in found if root in p.parents and p.is_file()}


def broken_links(root: Path, name: str) -> dict[str, str]:
    """{link target: page} for every link of root's pages that does not reach a file inside root (#41): run on a
    display zone, a target the zone lacks or one outside it is a link that fails in Periscope."""
    root = Path(root).resolve()
    show = lambda p: str(p.relative_to(root)) if root in p.parents else str(p)
    return {show(target): show(page) for target, page in _follow(root, _pages(root, name))[1].items()}


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
    record = check("display", dict(record, schema="display/1", synced_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"), files=len(need)))
    partial.write_text(json.dumps(record, indent=1) + "\n")
    os.replace(partial, dest / L.DISPLAY)
    return dict(dest=str(dest), files=len(need), copied=copied, seconds=round(time.time() - start, 1))

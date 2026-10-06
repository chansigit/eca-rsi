"""Which datasets Periscope serves: the dataset list file, the display zones, and the
`ecarsi serve` subcommands that edit the list (scan-add, remove, list, dump, reload)."""

from __future__ import annotations

import argparse
import glob as _glob
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from .. import layout as L
from ..contracts import check
from .common import _n
from .fleet import _dataset_state


def default_registry() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    return base / "ecarsi" / "periscope-datasets.json"


def default_results() -> Path:
    return default_registry().with_name("results.json")


def display_zones(config: Path | None) -> dict[str, Path]:
    """name -> display-zone copy, for every <root>/<collection>/<dataset>/<run>/display.json under the
    results file's display_root and more_display_roots. A name that several copies claim (a dataset run
    again) goes to the newest one; the others are served as <name>-<run>."""
    try:
        results = json.loads(Path(config).read_text()) if config else {}
        roots = [r for r in [results.get("display_root"), *results.get("more_display_roots", [])] if r]
    except (OSError, ValueError, AttributeError, TypeError):
        return {}
    records = []
    for root in roots:
        for path in sorted(Path(root).glob("*/*/*/" + L.DISPLAY)):
            try:
                record = check("display", json.loads(path.read_text()))
            except (OSError, ValueError) as exc:
                sys.stderr.write(f"[serve] {path} skipped: {exc}\n")
                continue
            if "/" not in record["name"]:
                records.append((record.get("synced_at") or record.get("copied_at") or "", record, path.parent))
    found: dict[str, Path] = {}
    for _, record, path in sorted(records, key=lambda r: r[0], reverse=True):
        name = record["name"] if record["name"] not in found else f"{record['name']}-{path.name}"
        found.setdefault(name, path)
    return found


def _check_dataset(path: Path) -> Path:
    path = Path(path)
    if not (L.is_root(path) or L.is_unit(path) or L.is_gen2_unit(path)):
        raise ValueError(
            f"{path} is neither an organize root nor a unit dir (see ecarsi.layout)"
        )
    return path


class Registry:
    """name -> dataset dir. The registry FILE is the truth; this object is a
    cache of it that re-reads on mtime change and writes through on
    bind/unbind. `extra` are per-process additions (serve's positional
    dirs) that are never written to the file."""

    DISPLAY_RESCAN_SECONDS = 600  # ponytail: a timed rescan; watch the roots if new zones must show sooner

    def __init__(self, path: Path, extra: dict[str, Path] | None = None,
                 published: "Callable[[], dict[str, Path]] | None" = None, config: Path | None = None) -> None:
        self.path = Path(path)
        self._config = config
        self._display: dict[str, Path] = {}  # replaced whole by the warmer, never modified in place
        self._extra = dict(extra or {})
        # Runs the control plane publishes about itself (ControlVerdicts.runs): a dataset is on the
        # page from the moment it is submitted, with nobody registering anything. Lowest priority --
        # a name in the file or on the command line keeps its own path.
        self._published = published or (lambda: {})
        self._file: dict[str, Path] = {}
        self._stamp: tuple | None = None
        self._lock = threading.Lock()

    # -- file I/O --
    @staticmethod
    def read_file(path: Path) -> dict[str, Path]:
        if not path.is_file():
            return {}
        data = json.loads(path.read_text() or "{}")
        if not isinstance(data, dict):
            raise ValueError(f"{path}: expected a JSON object {{name: path}}")
        return {str(k): Path(v) for k, v in data.items()}

    @staticmethod
    def write_file(path: Path, items: dict[str, Path]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(
            json.dumps({k: str(v) for k, v in sorted(items.items())}, indent=2) + "\n"
        )
        os.replace(
            tmp, path
        )  # atomic: a concurrent server never sees a half-written file

    def _load_if_changed(self) -> None:
        try:
            st = self.path.stat()
            stamp = (st.st_mtime_ns, st.st_size)
        except FileNotFoundError:
            stamp = None
        if stamp == self._stamp:
            return
        try:
            self._file = self.read_file(self.path) if stamp else {}
        except (ValueError, OSError) as e:
            sys.stderr.write(
                f"[serve] registry {self.path} unreadable, keeping last good list: {e}\n"
            )
            return
        self._stamp = stamp

    # -- reads --
    def snapshot(self) -> dict[str, Path]:
        with self._lock:
            self._load_if_changed()
            published = self._published()
            # A completed run is a result: write it into the file, so it stays on the page when the
            # plane stops publishing (the 2026-09-23 outage emptied the fleet table of them).
            keep = getattr(getattr(self._published, "__self__", None), "completed", {})
            known = set(self._file.values())
            # a run with a display zone is kept by it (#25); its scratch work tree must not shadow it here
            new = {k: v for k, v in keep.items() if k not in self._file and v not in known and k not in self._display}
            if new:
                try:
                    self.write_file(self.path, {**self._file, **new})
                    self._file, self._stamp = {**self._file, **new}, None
                except OSError as exc:
                    sys.stderr.write(f"[serve] registry {self.path}: could not keep completed runs: {exc}\n")
            return self._merge(published)

    def _merge(self, published: dict[str, Path]) -> dict[str, Path]:
        """The three sources as one list: this process's own dirs win on a name clash, and a
        published run whose directory is already bound under another name is not a second row.
        The plane names a run by its directory (07_Swahnetal); the file may hold the same path
        under a qualified name (chondroatlas-g2-07_Swahnetal) -- merging by name alone put five
        chondro runs on the page twice, the bare copies under "other" (2026-09-24)."""
        mine = {**self._file, **self._extra}
        bound = set(mine.values())
        shown = {k: v for k, v in self._display.items() if v not in bound}
        bound |= set(shown.values())
        return {**{k: v for k, v in published.items() if v not in bound}, **shown, **mine}

    def get(self, name: str) -> Path | None:
        return self.snapshot().get(name)

    def cached_snapshot(self) -> dict[str, Path]:
        # Readers never wait on filesystem I/O under the registry write lock.
        # _file is replaced atomically, not modified in place.
        return self._merge(self._published())

    def scan_display(self) -> None:
        self._display = display_zones(self._config)

    def start(self) -> None:
        def refresh():
            scanned = 0.0
            while True:
                try:
                    if time.monotonic() - scanned > self.DISPLAY_RESCAN_SECONDS:
                        self.scan_display()  # the warmer scans; a request never waits on the roots
                        scanned = time.monotonic()
                    self.snapshot()
                except OSError as exc:
                    sys.stderr.write(f'[serve] registry refresh: {exc}\n')
                time.sleep(5)
        threading.Thread(target=refresh, daemon=True, name='registry-warmer').start()

    # -- writes (through to the file) --
    def bind(self, name: str, path: Path, force: bool = False) -> None:
        path = _check_dataset(path)
        if not name or "/" in name:
            raise ValueError("name must be non-empty and contain no '/'")
        with self._lock:
            self._load_if_changed()
            existing = self._file.get(name) or self._extra.get(name)
            if not force and existing is not None and existing != path:
                raise ValueError(
                    f"name {name!r} already bound to {existing} — use --name to disambiguate or remove it first"
                )
            new = dict(self._file)
            new[name] = path
            self.write_file(self.path, new)
            self._file = new
            self._stamp = None  # re-stat next time; our own write changed the mtime

    def unbind(self, names: list[str]) -> None:
        """All-or-nothing, so a typo in one name doesn't half-apply the batch."""
        with self._lock:
            self._load_if_changed()
            missing = [n for n in names if n not in self._file]
            if missing:
                raise ValueError(
                    "nothing bound as "
                    + ", ".join(repr(m) for m in missing)
                    + (
                        ""
                        if not any(n in self._extra for n in missing)
                        else " (given on the serve command line, not in the registry file)"
                    )
                )
            new = {k: v for k, v in self._file.items() if k not in names}
            self.write_file(self.path, new)
            self._file = new
            self._stamp = None


GENERIC_DIR_NAMES = {
    "rsi",
    "root",
    "run",
    "runs",
    "out",
    "output",
    "results",
    "eca-rsi",
    "ecarsi",
    "eca-pp",
    "units",
    "data",
    "sc",
}


def _informative(d: Path) -> list[str]:
    return [c for c in d.parts[1:] if c not in GENERIC_DIR_NAMES] or [d.name]


def _auto_depth(name: str, path: Path) -> int | None:
    """How many components of `path` this function used to make `name`, or None
    if it never would have. A name already qualified once (mca1.1-Bladder) has
    to stay recognisable as ours, or the next batch to arrive reads it as a
    hand-picked name and helps itself to the bare one — the very
    order-dependence #11 is about. The depth is what keeps requalification
    one-way: an entry may gain a component when a new collision demands it,
    never lose one because the collision that caused it is no longer in this
    batch. (Measured 2026-09-21: recomputing from scratch would have renamed
    298 of 459 live entries, every one of them to something *shorter*.)"""
    comps = _informative(path)
    return next((k for k in range(1, len(comps) + 1) if name == "-".join(comps[-k:])), None)


def _scan_names(dirs: list[Path], taken: dict[str, Path]) -> dict[Path, str]:
    """Names for a batch of scanned dirs. Path components that carry no
    information (rsi, eca-pp, units, ...) are dropped; each dir starts with
    its last informative component and every dir whose name collides —
    within the batch or with an existing entry for another path — is
    qualified by one more component, symmetrically (Brain across three
    collections becomes mca1.1-Brain / mca2.0-Brain / mca3.0-Brain, not
    Brain / Brain-rsi / eca-pp-Brain).

    Already-registered dirs that still carry their bare auto-name take part in
    the resolution, so the result does not depend on the order scan-add was
    run in: whoever arrived first does not get to keep `/Bladder/` while every
    other collection is qualified (eca-rsi#11). A bookmarked bare URL then
    stops resolving instead of quietly pointing at another batch's data. A name
    this function would never produce is a deliberate `--name` and is left
    alone — it only bars others from taking it. The caller applies the renames.
    """
    comps: dict[Path, list[str]] = {}
    fixed: dict[str, Path] = {}
    floor: dict[Path, int] = {}
    for name_, path in taken.items():
        if path in dirs or path in comps:
            continue
        held = _auto_depth(name_, path)
        if held is None:
            fixed[name_] = path
        else:
            comps[path], floor[path] = _informative(path), held
    for d in dirs:
        comps[d] = _informative(d)
    order = list(comps)
    # Sharing a last component is what makes a bare name ambiguous, whatever the others are
    # called right now. Keying off the current names instead would let a third collection walk
    # in and take `/Bladder/` simply because the two incumbents had already been qualified.
    shared = {c for c in (comps[d][-1] for d in order)
              if sum(comps[d][-1] == c for d in order) > 1}
    depth = {d: min(max(floor.get(d, 1), 2 if comps[d][-1] in shared else 1), len(comps[d]))
             for d in order}
    name = lambda d: "-".join(comps[d][-depth[d] :])
    while True:
        by: dict[str, list[Path]] = {}
        for d in order:
            by.setdefault(name(d), []).append(d)
        clash = [
            d
            for n, ds in by.items()
            for d in ds
            if len(ds) > 1 or (n in fixed and fixed[n] != d)
        ]
        clash = [d for d in clash if depth[d] < len(comps[d])]  # can't qualify further
        if not clash:
            return {d: name(d) for d in order}
        for d in clash:
            depth[d] += 1


def cmd_scan_add(args: argparse.Namespace) -> int:
    reg = Registry(Path(args.registry).expanduser().resolve())
    matches = sorted(
        {
            Path(m).resolve()
            for pat in args.glob
            for m in _glob.glob(os.path.expandvars(os.path.expanduser(pat)))
        }
    )
    if not matches:
        print("[serve] nothing matched")
        return 1
    taken = reg.snapshot()
    new, skipped = [], []
    for d in matches:
        if not d.is_dir():
            continue
        if not (L.is_root(d) or L.is_unit(d)):
            skipped.append(d)
        elif d not in taken.values():  # already in under some name -> leave it
            new.append(d)
    if args.name:
        if len(new) != 1:
            print(f"[serve] --name needs exactly one new dataset, got {len(new)}")
            return 2
        names = {new[0]: args.name}
    else:
        names = _scan_names(new, taken)
    plan = [(names[d], d) for d in new]
    renames = [
        (old, names[p], p)
        for old, p in sorted(taken.items())
        if p in names and p not in new and names[p] != old
    ]
    for d in skipped:
        print(f"  skip   {d}  (not an organize root / unit)")
    if not plan:
        print(
            f"[serve] nothing new to add ({len(matches)} matched, {len(skipped)} skipped, {len(matches) - len(skipped)} already in)"
        )
        return 0
    for old, new_name, d in renames:
        if args.dry_run:
            print(f"  would  {old:24s} -> {new_name}  (bare name is now ambiguous)")
            continue
        try:
            reg.bind(new_name, d)
            reg.unbind([old])
            print(f"  renamed {old:23s} -> {new_name}")
        except (ValueError, OSError) as e:
            print(f"  FAILED rename {old} -> {new_name}  ({e})")
    n_ok = 0
    for name, d in plan:
        if args.dry_run:
            print(f"  would  {name:24s} {d}")
            continue
        try:
            reg.bind(name, d)
            n_ok += 1
            print(f"  added  {name:24s} {d}")
        except (ValueError, OSError) as e:
            print(f"  FAILED {name:24s} {d}  ({e})")
    if args.dry_run:
        print(f"[serve] dry run: {len(plan)} to add, {len(renames)} to rename, {len(skipped)} skipped")
        return 0
    print(f"[serve] added {n_ok}/{len(plan)}, {len(skipped)} skipped -> {reg.path}")
    return 0 if n_ok == len(plan) else 1


def cmd_remove(args: argparse.Namespace) -> int:
    reg = Registry(Path(args.registry).expanduser().resolve())
    try:
        reg.unbind(args.name)
    except (ValueError, OSError) as e:
        print(f"[serve] remove failed: {e}")
        return 1
    print(f"[serve] removed {', '.join(args.name)} from {reg.path}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    reg = Registry(Path(args.registry).expanduser().resolve())
    items = reg.snapshot()
    if args.json:
        out = {}
        for name, p in sorted(items.items()):
            st = _dataset_state(p)
            out[name] = {"path": str(p), **st}
        print(json.dumps({"registry": str(reg.path), "datasets": out}, indent=2))
        return 0
    print(f"registry {reg.path}" + ("" if items else "  (empty)"))
    for name, p in sorted(items.items()):
        st = _dataset_state(p)
        cells = _n(st["final_cells"]) or "-"
        print(f"  {name:24s} {st['stage']:18s} {cells:>10s}  {p}")
    return 0


def cmd_dump(args: argparse.Namespace) -> int:
    reg_path = Path(args.registry).expanduser().resolve()
    items = Registry.read_file(reg_path)
    if not args.path:
        print(json.dumps({k: str(v) for k, v in sorted(items.items())}, indent=2))
        return 0
    out = Path(args.path).expanduser().resolve()
    Registry.write_file(out, items)
    print(
        f"[serve] wrote {len(items)} entr{'y' if len(items) == 1 else 'ies'} -> {out}"
    )
    return 0


def cmd_reload(args: argparse.Namespace) -> int:
    reg_path = Path(args.registry).expanduser().resolve()
    src = Path(args.path).expanduser().resolve()
    if not src.is_file():
        print(f"[serve] {src} does not exist")
        return 1
    try:
        incoming = Registry.read_file(src)
    except ValueError as e:
        print(f"[serve] {e}")
        return 1
    current = {} if args.replace else Registry.read_file(reg_path)
    merged = {**current, **incoming}  # entries from the file win on a name clash
    Registry.write_file(reg_path, merged)
    print(
        f"[serve] {'replaced with' if args.replace else 'merged'} {len(incoming)} entr{'y' if len(incoming) == 1 else 'ies'} from {src} -> {reg_path} ({len(merged)} total)"
    )
    return 0

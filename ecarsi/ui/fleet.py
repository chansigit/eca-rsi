"""Every dataset's summary for the fleet pages: read from disk, cached in memory, refreshed in
the background and checked against the control plane's verdicts."""

from __future__ import annotations

import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING

from . import index

if TYPE_CHECKING:
    from .registry import Registry


def _dataset_state(root: Path) -> dict:
    """Per-dataset summary read from disk (ecarsi.ui.index), for the navigator / list."""
    blank = {"units": 0, "released": 0, "n_input": None, "final_cells": None, "rounds": 0, "species": "",
             "finished": None, "updated": None, "events": {"organize": [], "release": []}}
    if not root.is_dir():
        return {**blank, "stage": "missing on disk", "cls": "failed"}
    try:
        return index.dataset_state(root)
    except Exception as e:  # a broken run dir must not take the navigator down
        return {**blank, "stage": f"unreadable: {e}", "cls": "failed"}


TERMINAL = {"FAILED": ("failed", "failed"), "TERMINATED": ("failed", "terminated"),
            "TIMED_OUT": ("failed", "timed out"), "CANCELED": ("failed", "cancelled"),
            "CANCELLED": ("failed", "cancelled"), "COMPLETED": ("released", "completed")}


class ControlVerdicts:
    """What the control plane says about each run, read from the file it publishes.

    The fleet table is derived from the files stages write, which is as current as the last stage
    boundary and no more: a run that died inside Temporal writing nothing reads as running until the
    twelve-hour staleness rule notices, and a run just resumed reads as failed until its next
    publication. The control plane knows now, so it publishes what it knows and this reads it. The
    file carries the time it was written: a stale publisher is visible rather than silently believed,
    and a missing one simply leaves the disk-derived row alone."""

    FRESH = 120.0     # a publisher that has not written for this long is not speaking for the fleet
    RECENT = 86400.0  # a terminated run stays on the page this long; completed and failed ones stay

    def __init__(self, path: Path | None):
        self._path, self._mtime, self._data = path, None, {}
        self.completed: dict[str, Path] = {}   # the completed ones among the last runs(), for the registry to keep

    def _load(self) -> None:
        if self._path is None:
            return
        try:
            mtime = self._path.stat().st_mtime
        except OSError:
            self._data = {}
            return
        if mtime == self._mtime:
            return
        try:
            self._data = json.loads(self._path.read_text())
        except (OSError, ValueError):
            return          # a half-written file is never seen (the publisher renames), so this is corruption: keep the last good one
        self._mtime = mtime

    def age(self) -> float | None:
        self._load()
        at = self._data.get("generated_at")
        return None if at is None else max(0.0, time.time() - at)

    def live(self) -> bool:
        age = self.age()
        return age is not None and age <= self.FRESH

    def of(self, run_id: str, unit: str = "") -> dict | None:
        """The verdict on a run, or on one of its units when the control plane names that unit."""
        if not run_id or not self.live():
            return None
        workflows = self._data.get("workflows") or {}
        return workflows.get(f"dataset/{run_id}{unit}")

    def runs(self) -> dict[str, Path]:
        """Every dataset run the control plane owns, by the directory it publishes for it.

        The fleet table used to list only what the registry named, so a dataset the coordinator had
        been running for ten minutes was invisible until someone ran scan-add: the two disagreed
        about what the fleet even was (2026-09-22). The plane publishes each run's output_root from
        the spec it was started with; a directory that exists is a row, named as scan-add would name
        it. Superseded runs whose directories are gone, or a silent publisher, contribute nothing."""
        if not self.live():
            return {}
        datasets = [r for r in (self._data.get("workflows") or {}).values() if r.get("kind") == "DatasetWorkflow"]
        # A run is fleet while it runs, and for a day after it closes -- long enough for a failure
        # to be seen, not long enough for last week's to keep the table. And never once the same
        # dataset has been started again: a resubmission supersedes what it replaced. The first
        # version of this rule bound everything the plane had ever owned and put 31 dead runs back
        # on the page in one refresh, fifteen of them "failed" (2026-09-22).
        newest = {}
        for r in datasets:
            key = r.get("dataset_id")
            if key and r.get("started", 0) > newest.get(key, 0):
                newest[key] = r["started"]
        out, self.completed = {}, {}
        for r in datasets:
            root = r.get("output_root")
            if not root or not Path(root).is_dir():
                continue
            if r.get("status") != "RUNNING":
                if r.get("started", 0) < newest.get(r.get("dataset_id"), 0):
                    continue    # superseded: the dataset was started again
                # A failure never quietly leaves the table (owner, 2026-09-23: "失败了就是失败了,
                # 不要隐藏问题"): it stays until a resubmission supersedes it. Nor does a completed
                # run: nobody registers the plane's releases, so aging them off made the Completed
                # count fall by one each time a release turned a day old (2026-09-23, 314 -> 313).
                # Only terminated runs, which were meant to go, age off after a day.
                if r.get("status") not in ("FAILED", "COMPLETED") and time.time() - (r.get("closed") or 0) > self.RECENT:
                    continue
            out[Path(root).name] = Path(root)
            if r.get("status") == "COMPLETED":
                self.completed[Path(root).name] = Path(root)
        return out


def reconcile(row: dict, verdict: dict | None, precise: bool = True) -> dict:
    """The control plane's verdict wins over what the files imply, and says so.

    Only the verdict changes: the stage text stays, because 'per-sample running' is still what the
    run was last seen doing and the verdict cannot say it. A run the control plane calls finished is
    not running whatever its files suggest, and one it calls running is not failed however old its
    last publication is.

    `precise` is False when the verdict is the whole run's and the row is one unit of it: a finished
    run still has no unit running, but a running run says nothing about the unit that already failed
    inside it, so that direction is left to the files."""
    if not verdict:
        return row
    status = verdict.get("status", "")
    if status == "RUNNING":
        if str(row.get("stage", "")).startswith("stopped · "):   # only silence, never a real failure
            return {**row, "cls": "running", "live": "running",
                    "stage": "no progress 12h+ · " + row["stage"][len("stopped · "):]}
        if precise and row["cls"] in {"failed", "paused", "neutral"}:
            return {**row, "cls": "running", "live": "running"}
        return {**row, "live": "running"} if row["cls"] == "running" else row
    cls, word = TERMINAL.get(status, ("", ""))
    if not cls or row["cls"] == cls:
        return {**row, "live": word or status.lower()}
    stage = f"{word} · last seen {row['stage']}" if row["cls"] == "running" else word
    return {**row, "cls": cls, "stage": stage, "live": word}


def unstale(s: dict, verdicts: "ControlVerdicts | None") -> dict:
    """A dataset the files call `stopped` (running, silent for STALE_AFTER) that the control plane
    says is RUNNING is waiting, not dead: a 4-CPU task queued 17 h writes nothing. It stays in the
    running count, and its stage says how long it has been silent."""
    verdict = verdicts.of(s.get("run_id", "")) if verdicts and s.get("cls") == "failed" else None
    if not verdict or verdict.get("status") != "RUNNING" or not str(s.get("stage", "")).startswith("stopped · "):
        return s
    return {**s, "cls": "running", "stage": "no progress 12h+ · " + s["stage"][len("stopped · "):]}


def storage_of(root: Path) -> str:
    """The filesystem a run directory lives on, by its first path component (/oak, /scratch)."""
    parts = Path(root).parts
    return parts[1] if len(parts) > 1 else str(root)


class StateCache:
    """Fleet requests only read memory, including during a slow storage refresh."""

    def __init__(self, registry: Registry, ttl: float = 60.0, cache_file: Path | None = None):
        self._registry, self._ttl = registry, ttl
        self._states: dict[Path, tuple[float, dict]] = {}
        self._slow: dict[str, float] = {}   # storage_of(root) -> until when its roots are left alone
        self._lock = threading.Lock()
        self._cache_file = cache_file
        self._save_lock = threading.Lock()
        self._last_saved = 0
        if cache_file:
            try:
                stored = json.loads(cache_file.read_text())
                self._states = {Path(root): (record[0], record[1]) for root, record in stored.items()}
            except (OSError, ValueError, TypeError, AttributeError, IndexError):
                pass  # a missing/corrupt disposable cache starts with explicit loading states

    def get(self, root: Path) -> dict:
        with self._lock:
            hit = self._states.get(root)
        if hit:
            return {**hit[1], 'cached_at': hit[0]}
        return dict(units=0, released=0, n_input=None, final_cells=None, rounds=0,
                    species='', finished=None, updated=None, events={'organize': [], 'release': []},
                    stage='Loading status', cls='loading', collection='', cached_at=None)

    def _put(self, root: Path) -> dict:
        st = _dataset_state(root)
        st['collection'] = index.collection_of(root)
        with self._lock:
            self._states[root] = (time.time(), st)
        if self._cache_file and time.monotonic()-self._last_saved >= 5 and self._save_lock.acquire(blocking=False):
            try:
                self._last_saved = time.monotonic()
                with self._lock:
                    stored = {str(p): value for p, value in self._states.items()}
                from ..run_state import write_json
                write_json(self._cache_file, stored)
            except OSError as exc:
                sys.stderr.write(f'[serve] state cache write: {exc}\n')
            finally:
                self._save_lock.release()
        return st

    SETTLED = frozenset({"released"})   # the one state whose files will not move again
    FULL_EVERY = 10                     # sweeps between two rereads of the settled majority
    HUNG_AFTER = 60                     # seconds without a single read finishing: the running ones are stuck in storage
    QUARANTINE = 1800                   # seconds a storage whose read hung is skipped (its rows keep their last state)

    def refresh(self, full: bool = True) -> None:
        roots = set(self._registry.snapshot().values())
        if not full:
            # A released dataset is finished: rereading 270 of them is what pushes the handful that
            # are actually running out to a multi-minute refresh, which is exactly the lag people
            # notice. Reread the unsettled ones every sweep and the rest occasionally, in case one
            # was reopened or arrived while this process was not looking.
            with self._lock:
                settled = {root for root, (_, st) in self._states.items() if st.get("cls") in self.SETTLED}
            roots = {root for root in roots if root not in settled} or roots
        now = time.time()
        with self._lock:
            slow = {fs for fs, until in self._slow.items() if until > now}
        roots = {root for root in roots if storage_of(root) not in slow}
        # Bound filesystem work independently of the number of browser requests. A read that hangs in
        # the filesystem client cannot be cancelled (2026-09-24: Oak OSTs returned I/O errors and all
        # four warmer threads sat in cl_sync_io_wait for hours, so the scratch rows went stale too);
        # a sweep in which nothing finishes for HUNG_AFTER seconds abandons the reads still running,
        # quarantines their storage, and leaves their rows as they were.
        from concurrent.futures import FIRST_COMPLETED, wait
        pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix='dataset-warmer')
        futures = {pool.submit(self._put, root): root for root in roots}
        pending = set(futures)
        while pending:
            done, pending = wait(pending, timeout=self.HUNG_AFTER, return_when=FIRST_COMPLETED)
            if done or not pending:
                continue
            hung = [futures[f] for f in pending if f.running()]
            for f in pending:
                f.cancel()   # not started: read next sweep
            with self._lock:
                for root in hung:
                    self._slow[storage_of(root)] = time.time() + self.QUARANTINE
                    if root not in self._states:
                        self._states[root] = (time.time(), dict(
                            units=0, released=0, n_input=None, final_cells=None, rounds=0, species='',
                            finished=None, updated=None, events={'organize': [], 'release': []},
                            stage=f'storage not responding ({storage_of(root)})', cls='loading', collection=''))
            sys.stderr.write(f"[serve] state warmer: {len(hung)} read(s) hung on "
                             f"{sorted({storage_of(r) for r in hung})}; skipped for {self.QUARANTINE // 60} min\n")
            break
        pool.shutdown(wait=False)   # hung threads end when the storage answers; nothing waits for them
        with self._lock:
            live = set(self._registry.snapshot().values())
            for gone in set(self._states) - live:
                del self._states[gone]

    def start(self) -> None:
        def loop():
            sweep = 0
            while True:
                try:
                    self.refresh(full=sweep % self.FULL_EVERY == 0)
                except Exception as e:
                    sys.stderr.write(f"[serve] state warmer: {e}\n")
                sweep += 1
                time.sleep(self._ttl)

        threading.Thread(target=loop, daemon=True, name="state-warmer").start()

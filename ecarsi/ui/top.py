"""`eca-rsi top`: the control-plane monitor in a terminal, beside Periscope's /_control/ page.

It renders the snapshot the page renders (ui/control.py), from the same published records, in colour,
every --interval seconds (q quits, r refreshes now). Like the page it only reads files: no connection,
no writes (tests/test_monitor_isolation.py), and a record too old to believe shows as silent, never as
its last status. A refresh costs about 0.1 s of CPU on the node it runs on; ops/rtop runs it.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
import os
from pathlib import Path
import re
import select
import sys
import time

from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .control import resource_history, snapshot, status

# Tokyo Night
FG, DIM, TRACK, BORDER = "#c0caf5", "#565f89", "#3b4261", "#414868"
BLUE, CYAN, PURPLE, GREEN, YELLOW, ORANGE, RED = "#7aa2f7", "#7dcfff", "#bb9af7", "#9ece6a", "#e0af68", "#ff9e64", "#f7768e"
STAGE_COLOURS = {"organize": GREEN, "per-sample": BLUE, "cross-sample": CYAN, "zoom-in": PURPLE, "release": YELLOW,
                 "dataset": YELLOW, "agent": ORANGE}
STATES = {"computing": GREEN, "model turn": PURPLE, "queued": YELLOW, "between steps": DIM}
SPARK = "▁▂▃▄▅▆▇█"
HISTORY = 600          # seconds of worker CPU samples behind each sparkline (20 samples of 30 s)
FRESH = 180            # a scheduler, bridge or Temporal record older than this is silent (rule 2)


def load(pct: float) -> str:
    return GREEN if pct < 50 else YELLOW if pct < 75 else ORANGE if pct < 90 else RED


def meter(pct, width: int = 14, colour: str | None = None) -> Text:
    """btop-style: each filled cell takes the colour of its own position, so a full bar runs green to red."""
    out = Text()
    if pct is None:
        return out.append("■" * width, style=TRACK)
    filled = round(max(0.0, min(100.0, pct)) / 100 * width)
    for i in range(width):
        out.append("■", style=(colour or load(100 * (i + .5) / width)) if i < filled else TRACK)
    return out


def spark(values: list, width: int) -> Text:
    values = [v for v in values if v is not None][-width:]
    out = Text(" " * (width - len(values)))
    for v in values:
        out.append(SPARK[min(len(SPARK) - 1, int(v / 100 * len(SPARK)))], style=load(v))
    return out


def ago(seconds) -> str:
    if seconds is None:
        return "—"
    seconds = max(0, seconds)
    return f"{seconds:.0f} s" if seconds < 90 else f"{seconds / 60:.0f} min" if seconds < 5400 else f"{seconds / 3600:.1f} h"


def stage_colour(operation: str) -> str:
    return STAGE_COLOURS.get((operation or "").split(".")[0], FG)


def dot(ok: bool | None, label: str, value: str) -> Text:
    return Text.assemble(("● ", GREEN if ok else DIM if ok is None else RED), (label + " ", DIM), (value, f"bold {FG}"))


def panel(body, title: str, colour: str, height: int | None = None, subtitle: str | None = None) -> Panel:
    return Panel(body, title=Text(f" {title} ", style=f"bold {colour}"), title_align="left", border_style=BORDER,
                 box=box.ROUNDED, height=height, padding=(0, 1),
                 subtitle=Text(f" {subtitle} ", style=DIM) if subtitle else None, subtitle_align="right")


def grid(*columns: tuple) -> Table:
    """Columns are (header, justify[, width]); a negative width or none at all is a column that takes what the
    others leave (at least -width), so the fixed columns never lose their cells to a long name."""
    table = Table(box=None, expand=True, padding=(0, 1), header_style=f"bold {DIM}", show_edge=False, pad_edge=False)
    for name, justify, *width in columns:
        fixed = width and width[0] > 0
        table.add_column(name, justify=justify, no_wrap=True, overflow="ellipsis", width=width[0] if fixed else None,
                         min_width=-width[0] if width and not fixed else None, ratio=None if fixed else 1)
    return table


def running_tasks(snap: dict) -> list[dict]:
    return [t for w in snap.get("workers") or [] for t in w.get("tasks") or []]


def header(snap: dict, now: float, cost: float | None, error: str | None) -> Table:
    sched, service = snap.get("scheduler") or {}, snap.get("temporal_service") or {}
    bridge = snap.get("bridge_summary") or {}
    sched_fresh = now - (sched.get("observed_at") or 0) < FRESH
    temporal_fresh = now - (service.get("observed_at") or 0) < FRESH
    bridge_fresh = now - (bridge.get("updated_at") or 0) < FRESH
    title = Text.assemble(("◆ RSI top ", f"bold {BLUE}"), (snap.get("host", "?").split(".")[0], f"bold {FG}"),
                          ("  " + time.strftime("%H:%M:%S", time.localtime(now)), DIM))
    chips = Text("  ").join([
        dot(sched_fresh and sched.get("state") == "running", "scheduler", sched.get("state", "?") if sched_fresh else "silent"),
        dot(temporal_fresh and service.get("state") == "ready", "temporal", service.get("state", "?") if temporal_fresh else "silent"),
        dot(bridge_fresh and not bridge.get("dispatch_error"), "bridge",
            f"{bridge.get('running', 0)}/{bridge.get('concurrency', '?')}" if bridge_fresh else "silent"),
        dot(snap.get("worker_live_count", 0) > 0, "workers", str(snap.get("worker_live_count", 0))),
        dot(None if not snap.get("running_datasets") else True, "datasets", str(len(snap.get("running_datasets") or []))),
    ])
    table = Table.grid(expand=True)
    table.add_column()
    table.add_column(justify="right")
    table.add_row(title, chips)
    if error:
        table.add_row(Text(f"read failed, showing the last good read: {error}", style=f"bold {RED}"), "")
    return table


def workers_panel(snap: dict, history: dict, now: float, height: int) -> Panel:
    table = grid(("worker", "left", -10), ("job", "left", 8), ("cpu", "left", 8), ("", "right", 4), ("cpu, last 10 min", "left", 20),
                 ("memory", "left", 8), ("", "right", 9), ("slots", "right", 5), ("tasks", "right", 5), ("left", "right", 6))
    shown = [w for w in snap.get("workers") or [] if w.get("reporting") or w.get("tasks")]
    for w in sorted(shown, key=lambda w: (not w.get("reporting"), w.get("host", ""))):
        current = w.get("current") or {}
        cpu, mem = current.get("cpu_percent"), current.get("memory_percent")
        end = (w.get("allocation") or {}).get("end_time")
        left = end - now if end else None
        host = Text(w.get("host", "?"), style=f"bold {FG}" if w.get("reporting") else f"bold {RED}")
        if not w.get("reporting"):
            host.append(" silent", style=RED)
        if current.get("gpu_percent") is not None:
            host.append(f" gpu {current['gpu_percent']:.0f}%", style=PURPLE)
        table.add_row(
            host, Text(str(w.get("slurm_job_id") or "—"), style=DIM),
            meter(cpu, 8), Text(f"{cpu:.0f}%" if cpu is not None else "—", style=load(cpu or 0)),
            spark(history.get(w.get("worker_id"), []), 20),
            meter(mem, 8), Text(f"{current.get('memory_used_gb', 0):.0f}/{current.get('memory_total_gb', 0):.0f} G"
                             if mem is not None else "—", style=FG),
            Text(f"{w.get('reserved_cpus', 0)}/{w.get('cpus', '?')}", style=CYAN),
            Text(str(len(w.get("tasks") or [])), style=f"bold {FG}"),
            Text(ago(left), style=RED if left is not None and left < 7200 else YELLOW if left is not None and left < 21600 else GREEN))
    departed = sum(1 for w in snap.get("workers") or [] if w not in shown)
    body = [table if shown else Text("no worker is reporting", style=f"bold {RED}")]
    room = height - 5 - len(shown)        # borders, two headers, the blank line between
    tasks = sorted(running_tasks(snap), key=lambda t: t.get("started_at") or now)
    if tasks and room > 0:
        listing = grid(("running task", "left", -16), ("dataset", "left", 26), ("on", "left", 12), ("cpus", "right", 4),
                       ("memory", "right", 7), ("for", "right", 7))
        for t in tasks[:room if len(tasks) <= room else room - 1]:
            operation = t.get("operation") or "?"
            listing.add_row(Text(operation, style=stage_colour(operation)), Text((t.get("trace") or {}).get("dataset_id") or "—", style=FG),
                            Text(t.get("host") or "?", style=DIM), Text(str(t.get("cpus") or ""), style=CYAN),
                            Text(f"{(t.get('memory_mb') or 0) / 1024:.0f} G", style=DIM),
                            Text(ago(now - t["started_at"]) if t.get("started_at") else "", style=FG))
        if len(tasks) > room:
            listing.add_row(Text(f"… {len(tasks) - room + 1} more", style=DIM))
        body += [Text(""), listing]
    return panel(Group(*body), "Workers", BLUE, height, f"{departed} departed" if departed else None)


def models_panel(snap: dict, height: int) -> Panel:
    bridge = snap.get("bridge_summary") or {}
    table = grid(("model", "left"), ("now", "right", 3), ("ok", "right", 7), ("fail", "right", 5), ("last", "right", 6))
    for m in bridge.get("models") or []:
        ok, failed = m.get("successes", 0), m.get("failures", 0)
        rate = 100 * failed / (ok + failed) if ok + failed else 0
        latency = m.get("last_latency_seconds")
        state = m.get("state", "?")
        table.add_row(
            Text.assemble(("● ", GREEN if state == "ready" else YELLOW if "cool" in state else RED),
                          (re.sub(r"-\d{6}$", "", (m.get("model") or {}).get("model", "?")), f"bold {FG}"),
                          ("" if state == "ready" else f" {state}", DIM)),
            Text(str(m.get("in_flight", 0)), style=f"bold {PURPLE}"),
            Text(f"{ok:,}", style=GREEN), Text(f"{rate:.1f}%", style=RED if rate > 10 else YELLOW if rate > 3 else DIM),
            Text(ago(latency) if latency is not None else "—", style=GREEN if (latency or 0) < 60 else YELLOW if latency < 180 else RED))
    if bridge.get("dispatch_error"):
        table.add_row(Text(str(bridge["dispatch_error"])[:80], style=RED))
    return panel(table, "Models", PURPLE, height, f"{bridge.get('running', 0)} of {bridge.get('concurrency', '?')} in flight")


def pool_panel(snap: dict, height: int) -> Panel:
    operations = Counter(t.get("operation") or "?" for t in running_tasks(snap))
    table = grid(("running now", "left"), ("", "left", 12), ("", "right", 4))
    top = max(operations.values(), default=1)
    infeasible = ((snap.get("scheduler") or {}).get("release") or {}).get("infeasible") or {}
    for operation, n in operations.most_common(max(1, height - 4 - len(infeasible))):
        table.add_row(Text(operation, style=stage_colour(operation)), meter(100 * n / top, 12, stage_colour(operation)),
                      Text(str(n), style=f"bold {FG}"))
    if not operations:
        table.add_row(Text("nothing running", style=DIM), "", "")
    waiting = snap.get("pool_waiting", 0)
    foot = Text.assemble(("waiting ", DIM), (str(waiting), f"bold {YELLOW if waiting else FG}"),
                         ("   4 h: ", DIM), (f"{snap.get('pool_succeeded', 0):,}", GREEN), (" of ", DIM),
                         (f"{snap.get('pool_total', 0):,}", FG), (" succeeded", DIM))
    for reason, n in infeasible.items():
        foot.append(f"\nno worker can hold: {reason} ({n})", style=f"bold {RED}")
    return panel(Group(table, foot), "Pool", CYAN, height, f"{sum(operations.values())} tasks")


def datasets_panel(snap: dict, now: float, height: int) -> Panel:
    activity = (snap.get("scheduler") or {}).get("datasets") or {}
    turns = (snap.get("bridge_summary") or {}).get("datasets") or {}
    versions = snap.get("running_versions") or {}
    tasks = running_tasks(snap)
    one_version = len(set(versions.values())) <= 1
    table = grid(("dataset", "left", 26), ("state", "left", 15), ("cpu", "right", 3), ("model", "right", 5),
                 ("queued", "right", 6), ("working on", "left"), ("for", "right", 6), *([] if one_version else [("version", "left", 12)]))
    rows = []
    for name in snap.get("running_datasets") or []:
        c = list(activity.get(name, [0, 0, 0, 0]))
        if name in turns:
            c[1] = turns[name]
        state = "computing" if c[0] else "model turn" if c[1] else "queued" if c[2] or c[3] else "between steps"
        mine = [t for t in tasks if (t.get("trace") or {}).get("dataset_id") == name]
        rows.append((list(STATES).index(state), name, state, c, mine))
    for _, name, state, c, mine in sorted(rows):
        collection, _, dataset = name.rpartition(" ")
        doing = Text()
        for operation, n in Counter(t.get("operation") or "?" for t in mine).most_common(3):
            doing.append(f"{operation}{f' ×{n}' if n > 1 else ''}  ", style=stage_colour(operation))
        started = min((t.get("started_at") or now for t in mine), default=None)
        table.add_row(Text.assemble((collection + " ", DIM) if collection else "", (dataset, f"bold {FG}")),
                      Text(f"● {state}", style=STATES[state]),
                      Text(str(c[0] or ""), style=GREEN), Text(str(c[1] or ""), style=PURPLE),
                      Text(str((c[2] + c[3]) or ""), style=YELLOW), doing,
                      Text(ago(now - started) if started else "", style=DIM),
                      *([] if one_version else [Text(versions.get(name, ""), style=DIM)]))
    body = table if rows else Text("no dataset is running", style=DIM)
    counts = Counter(r[2] for r in rows)
    subtitle = "  ".join([f"{n} {s}" for s, n in counts.items()] +
                         [f"version {v}" for v in sorted(set(versions.values())) if one_version and v])
    return panel(body, "Datasets at work", GREEN, height, subtitle or None)


def productivity_panel(snap: dict, height: int) -> Panel:
    table = grid(("node", "left"), ("busy, 15 min", "left", 13), ("4 h", "right", 4), ("done 4 h", "right", 8),
                 ("failed", "right", 6))
    for n in snap.get("productivity") or []:
        e15, e4 = n.get("efficiency_15m"), n.get("efficiency_4h")
        table.add_row(Text.assemble((n.get("host", "?"), f"bold {FG}"), (f" {n.get('cores') or 'gone'}c", DIM)),
                      meter(e15, 8, CYAN) + Text(f" {e15:>3.0f}%" if e15 is not None else "    —", style=CYAN),
                      Text(f"{e4:.0f}%" if e4 is not None else "—", style=DIM),
                      Text(f"{n.get('tasks_done_4h', 0):,}", style=GREEN),
                      Text(str(n.get("tasks_failed_4h") or ""), style=RED))
    return panel(table, "Node productivity", CYAN, height, "core time used of core time held")


def failures_panel(snap: dict, now: float, height: int) -> Panel:
    failures = snap.get("recent_failures") or []
    if not failures:
        return panel(Text("none in the published window", style=GREEN), "Failed attempts", RED, height)
    by_operation = Counter(f.get("operation") or "?" for f in failures)
    summary = Text("  ").join(Text(f"{op} {n}", style=stage_colour(op)) for op, n in by_operation.most_common(6))
    folded = []   # [dataset, operation, count, newest, oldest, request]
    for f in failures:
        at = f.get("at") or now
        if folded and folded[-1][:2] == [f.get("dataset"), f.get("operation")]:
            folded[-1][2] += 1
            folded[-1][4] = at
        else:
            folded.append([f.get("dataset"), f.get("operation"), 1, at, at, f.get("id", "")])
    table = grid(("when", "right", 16), ("dataset", "left", 28), ("operation", "left", 28), ("", "right", 4),
                 ("newest request", "left"))
    for dataset, operation, n, newest, oldest, request in folded[:max(1, height - 4)]:
        age = now - newest
        when = ago(age) + " ago" if n == 1 else f"{ago(now - oldest)}–{ago(age)}"
        table.add_row(Text(when, style=RED if age < 600 else ORANGE if age < 3600 else DIM),
                      Text(dataset or "—", style=FG), Text(operation or "?", style=stage_colour(operation)),
                      Text(f"×{n}" if n > 1 else "", style=f"bold {RED}"), Text(request, style=DIM))
    more = " (newest 50; retried attempts included)" if len(failures) >= 50 else " (retried attempts included)"
    return panel(Group(summary, table), "Failed attempts", RED, height, f"{len(failures)} in the window{more}")


WIDE = 150             # from this many columns the panels sit side by side, below it they stack


def render(snap: dict, history: dict, now: float, cost: float | None = None, error: str | None = None,
           interval: float | None = None, rows: int = 60, columns: int = 200) -> Group:
    """One frame: the facts of a control.snapshot() dict, laid out for a terminal of `rows` x `columns`."""
    workers = [w for w in snap.get("workers") or [] if w.get("reporting") or w.get("tasks")]
    models = (snap.get("bridge_summary") or {}).get("models") or []
    tasks = len(running_tasks(snap))
    wide = columns >= WIDE
    listed = min(tasks, 8 if wide else 5)
    model_height = len(models) + 3
    infeasible = len(((snap.get("scheduler") or {}).get("release") or {}).get("infeasible") or {})
    pool_height = (len(Counter(t.get("operation") for t in running_tasks(snap))) or 1) + 4 + infeasible
    worker_height = len(workers) + 3 + (listed + 2 if tasks else 0)
    middle = max(len(snap.get("running_datasets") or []), 2) + 3
    nodes = len(snap.get("productivity") or []) + 3

    def pair(left, right, ratio=(7, 4)):
        row = Table.grid(expand=True)
        row.add_column(ratio=ratio[0])
        row.add_column(ratio=ratio[1])
        row.add_row(left, right)
        return row

    if wide:
        top = max(worker_height, model_height + pool_height)
        middle = max(middle, nodes)
        side = Table.grid(expand=True)
        side.add_column(ratio=1)
        side.add_row(models_panel(snap, model_height))
        side.add_row(pool_panel(snap, top - model_height))
        panels = [pair(workers_panel(snap, history, now, top), side),
                  pair(datasets_panel(snap, now, middle), productivity_panel(snap, middle))]
        used = top + middle
    else:
        beside = max(model_height, pool_height)
        panels = [workers_panel(snap, history, now, worker_height),
                  pair(models_panel(snap, beside), pool_panel(snap, beside), (1, 1)),
                  datasets_panel(snap, now, middle), productivity_panel(snap, nodes)]
        used = worker_height + beside + middle + nodes
    keys = Text.assemble((" q ", f"bold {BLUE} reverse"), (" quit  ", DIM), (" r ", f"bold {BLUE} reverse"), (" refresh  ", DIM),
                         (f"every {interval:g} s · " if interval else "", DIM),
                         (f"read {cost * 1000:.0f} ms · " if cost is not None else "", DIM),
                         ("published records, read-only · same data as Periscope /_control/", DIM))
    used += 2 + (1 if error else 0)       # the header line and the keys line
    return Group(header(snap, now, cost, error), *panels, failures_panel(snap, now, max(5, rows - used)), keys)


def cpu_history(pool: Path, cache: dict, now: float) -> dict:
    """worker_id -> CPU % samples of the last HISTORY seconds, from the telemetry snapshot() already parsed."""
    out = {}
    for row in resource_history(pool, now - HISTORY, now, cache.setdefault("resource_files", {})):
        out.setdefault(row.get("worker_id"), []).append(row.get("cpu_percent"))
    return out


@contextmanager
def keyboard():
    """Single keys without Enter while the monitor runs; the terminal is restored on exit. None off a terminal."""
    if not sys.stdin.isatty():
        yield None
        return
    import termios
    import tty
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        yield fd
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


def key(fd, timeout: float) -> str:
    if fd is None:
        time.sleep(timeout)
        return ""
    ready, _, _ = select.select([fd], [], [], timeout)
    return os.read(fd, 1).decode(errors="ignore").lower() if ready else ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="eca-rsi top", description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, required=True, help="the control plane's run directory ($BASE)")
    parser.add_argument("--pool-root", type=Path)
    parser.add_argument("--bridge-root", type=Path)
    parser.add_argument("--interval", type=float, default=2.0, help="seconds between refreshes (at least 1; default 2)")
    parser.add_argument("--once", action="store_true", help="print one frame and exit")
    args = parser.parse_args(argv)
    interval, pool = max(1.0, args.interval), args.pool_root or args.root / "pool"
    console, cache = Console(), {}
    last, history, error = {}, {}, None

    def read() -> float | None:
        nonlocal last, history, error
        started = time.perf_counter()
        try:
            last = status(snapshot(args.root, cache=cache, pool_root=args.pool_root, bridge_root=args.bridge_root),
                          cache.get("indexed_since"))
            history, error = cpu_history(pool, cache, time.time()), None
        except Exception as exc:  # noqa: BLE001 - a stalled or failing filesystem must not end the monitor
            error = f"{type(exc).__name__}: {exc}"[:160]
        return time.perf_counter() - started

    def frame(cost):
        return render(last, history, time.time(), cost, error, interval, console.size.height, console.size.width)

    cost = read()
    if args.once:
        console.print(frame(cost))
        return 0 if error is None else 1
    try:
        with keyboard() as fd, Live(frame(cost), console=console, screen=True, auto_refresh=False,
                                    vertical_overflow="crop") as live:
            while key(fd, interval) != "q":
                cost = read()
                live.update(frame(cost), refresh=True)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

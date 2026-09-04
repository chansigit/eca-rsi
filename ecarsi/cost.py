"""ecarsi.cost — agent spend, recorded per step in progress.log and summed at release.

The harness prints `== [label] agent cost: $X` after every agent run (both in
this process and inside the osp / msp / zmip kernels, which are subprocesses).
Nothing persisted that; it scrolled by in the Slurm log. Now:

- kernel subprocesses are run through `run_streamed()`, which echoes their
  output unchanged and, on every cost line, appends
  `cost step=<step> usd=<X> label=<label>` to the unit's progress.log;
- ecarsi's own agent calls call `record()` with the harness result's cost_usd;
- `summarize()` reads those events back (progress.log is the audit trail
  anyway) and release/summary.md gets an "Agent cost" section from it.

Backends that don't report cost (deepseek) simply produce no events — the
section then says so instead of pretending zero.
"""

from __future__ import annotations

import re
import subprocess
from collections import OrderedDict
from pathlib import Path

from . import layout as L

COST_RE = re.compile(r"(?:\[(?P<label>[^\]]*)\]\s*)?(?P<pre>[\w -]*?)\s*agent cost: \$(?P<usd>[0-9]+(?:\.[0-9]+)?)")
EVENT_RE = re.compile(r"^cost step=(?P<step>\S+) usd=(?P<usd>[0-9.]+)(?: label=(?P<label>.*))?$")


def record(unit: Path, step: str, usd: float | None, label: str = "") -> None:
    """One agent run's spend -> progress.log (no-op when the backend gave none)."""
    if usd is None:
        return
    L.log_event(unit, f"cost step={step} usd={usd:.4f}" + (f" label={label}" if label else ""), echo=False)


def run_streamed(cmd: str, unit: Path, step: str) -> int:
    """subprocess.run(cmd, shell=True) with the output passed through line by
    line and every harness cost line also recorded against `step`."""
    proc = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    assert proc.stdout is not None
    for line in proc.stdout:
        print(line, end="", flush=True)
        m = COST_RE.search(line)
        if m:
            label = (m.group("label") or m.group("pre") or "").strip()
            record(unit, step, float(m.group("usd")), label)
    return proc.wait()


def events(unit: Path) -> list[dict]:
    out = []
    for ts, ev in L.read_log(unit):
        m = EVENT_RE.match(ev)
        if m:
            out.append({"time": ts, "step": m.group("step"), "usd": float(m.group("usd")), "label": m.group("label") or ""})
    return out


def summarize(unit: Path) -> dict:
    """{"total": float, "n": int, "by_step": {step: {"usd", "n"}}} in first-seen step order."""
    by: "OrderedDict[str, dict]" = OrderedDict()
    total, n = 0.0, 0
    for e in events(unit):
        d = by.setdefault(e["step"], {"usd": 0.0, "n": 0})
        d["usd"] += e["usd"]
        d["n"] += 1
        total += e["usd"]
        n += 1
    return {"total": round(total, 2), "n": n, "by_step": {k: {"usd": round(v["usd"], 2), "n": v["n"]} for k, v in by.items()}}


def summary_md(unit: Path) -> list[str]:
    s = summarize(unit)
    if not s["n"]:
        return ["## Agent cost", "", "no cost reported (backend does not report spend, or the run predates cost logging)"]
    rows = [f"| {step} | {v['n']} | ${v['usd']:.2f} |" for step, v in s["by_step"].items()]
    return ["## Agent cost", "", f"Total: **${s['total']:.2f}** over {s['n']} agent run(s) — from `cost` events in progress.log", "",
            "| step | agent runs | USD |", "|---|---|---|", *rows]

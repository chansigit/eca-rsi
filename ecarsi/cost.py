"""ecarsi.cost — agent spend and token usage, recorded per step in progress.log
and summed at release.

The harness prints one of two per-run lines to stdout (both in this process
and inside the osp / msp / zmip kernels, which are subprocesses):
`== [label] agent cost: $X` (claude, real dollars) or
`== [label] ... N input / M output tokens ...` (openai/Doubao, no dollar figure
but real token counts). Nothing persisted that; it scrolled by in the Slurm
log. Now:

- kernel subprocesses are run through `run_streamed()`, which echoes their
  output unchanged and, on every cost/token line, appends a `cost ...` event
  to the unit's progress.log;
- ecarsi's own agent calls call `record()` with the harness result's
  cost_usd/tokens_in/tokens_out;
- `summarize()` reads those events back (progress.log is the audit trail
  anyway) and release/summary.md gets an "Agent cost" section from it.

A backend that reports neither (deepseek) simply produces no events — the
section then says so instead of pretending zero.

Separately, the bridge (agent-harness-bridge >= 0.2.8) prints one more line
for every backend regardless of whether it reports cost: `== [label]
resolved backend: harness=H model=M`. `run_streamed()` / persample's `_pump`
also catch that line and record it (`backend_events` / `round_backends`) --
this is how ecarsi finds out which model actually answered inside a kernel
subprocess (osp per-sample worker, msp/zmip round subprocess) when a
fallback pool is in play (eca-rsi#6); release/summary.md's "Backends per
round" section (eca-rsi#5) is built from it.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections import OrderedDict
from pathlib import Path

from . import layout as L

COST_RE = re.compile(r"(?:\[(?P<label>[^\]]*)\]\s*)?(?P<pre>[\w -]*?)\s*agent cost: \$(?P<usd>[0-9]+(?:\.[0-9]+)?)")
TOKEN_RE = re.compile(r"\[(?P<label>[^\]]*)\].*?(?P<tin>\d+) input / (?P<tout>\d+) output tokens")
EVENT_RE = re.compile(
    r"^cost step=(?P<step>\S+)(?: usd=(?P<usd>[0-9.]+))?"
    r"(?: tokens_in=(?P<tin>\d+))?(?: tokens_out=(?P<tout>\d+))?(?: label=(?P<label>.*))?$"
)
BACKEND_RE = re.compile(r"\[(?P<label>[^\]]*)\] resolved backend: harness=(?P<harness>\S+) model=(?P<model>\S+)")
BACKEND_EVENT_RE = re.compile(
    r"^agent step=(?P<step>\S+) harness=(?P<harness>\S+) model=(?P<model>\S+)(?: label=(?P<label>.*))?$"
)
ROUND_RE = re.compile(r"^round(\d+)")


def record(unit: Path, step: str, usd: float | None, label: str = "",
           tokens_in: int | None = None, tokens_out: int | None = None) -> None:
    """One agent run's spend/usage -> progress.log (no-op when the backend gave neither)."""
    if usd is None and tokens_in is None and tokens_out is None:
        return
    parts = [f"step={step}"]
    if usd is not None:
        parts.append(f"usd={usd:.4f}")
    if tokens_in is not None:
        parts.append(f"tokens_in={tokens_in}")
    if tokens_out is not None:
        parts.append(f"tokens_out={tokens_out}")
    if label:
        parts.append(f"label={label}")
    L.log_event(unit, "cost " + " ".join(parts), echo=False)


def record_backend(unit: Path, step: str, harness: str, model: str, label: str = "") -> None:
    """One agent run's {harness, model} -> progress.log, independent of the
    cost/token event above (a single call prints both a cost-or-token line
    and this line; recording them separately avoids double-counting `n` in
    summarize())."""
    line = f"agent step={step} harness={harness} model={model}" + (f" label={label}" if label else "")
    L.log_event(unit, line, echo=False)


def _scan_line(unit: Path, step: str, line: str) -> None:
    """Check one line of kernel subprocess stdout against every pattern this
    module knows how to record; shared by run_streamed() and persample's
    per-sample pump so both capture cost/token/backend lines the same way."""
    m = COST_RE.search(line)
    if m:
        label = (m.group("label") or m.group("pre") or "").strip()
        record(unit, step, float(m.group("usd")), label)
    m = TOKEN_RE.search(line)
    if m:
        record(unit, step, None, m.group("label").strip(), int(m.group("tin")), int(m.group("tout")))
    m = BACKEND_RE.search(line)
    if m:
        record_backend(unit, step, m.group("harness"), m.group("model"), m.group("label").strip())


def run_streamed(cmd: str, unit: Path, step: str) -> int:
    """subprocess.run(cmd, shell=True) with the output passed through line by
    line and every harness cost/token/backend line also recorded against `step`."""
    from harness_bridge.control import safe_point
    safe_point()
    env = dict(os.environ, ECA_RSI_CONTROL=str(unit / L.LOOP_CONTROL))
    proc = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, env=env)
    assert proc.stdout is not None
    for line in proc.stdout:
        print(line, end="", flush=True)
        _scan_line(unit, step, line)
    return proc.wait()


def events(unit: Path) -> list[dict]:
    out = []
    for ts, ev in L.read_log(unit):
        m = EVENT_RE.match(ev)
        if m:
            out.append({
                "time": ts, "step": m.group("step"),
                "usd": float(m.group("usd")) if m.group("usd") else None,
                "tokens_in": int(m.group("tin")) if m.group("tin") else None,
                "tokens_out": int(m.group("tout")) if m.group("tout") else None,
                "label": m.group("label") or "",
            })
    return out


def summarize(unit: Path) -> dict:
    """{"total": float, "tokens_in": int, "tokens_out": int, "n": int,
    "by_step": {step: {"usd", "tokens_in", "tokens_out", "n"}}} in first-seen step order."""
    by: "OrderedDict[str, dict]" = OrderedDict()
    total, tin_total, tout_total, n = 0.0, 0, 0, 0
    for e in events(unit):
        d = by.setdefault(e["step"], {"usd": 0.0, "tokens_in": 0, "tokens_out": 0, "n": 0})
        d["usd"] += e["usd"] or 0.0
        d["tokens_in"] += e["tokens_in"] or 0
        d["tokens_out"] += e["tokens_out"] or 0
        d["n"] += 1
        total += e["usd"] or 0.0
        tin_total += e["tokens_in"] or 0
        tout_total += e["tokens_out"] or 0
        n += 1
    return {
        "total": round(total, 2), "tokens_in": tin_total, "tokens_out": tout_total, "n": n,
        "by_step": {k: {"usd": round(v["usd"], 2), "tokens_in": v["tokens_in"], "tokens_out": v["tokens_out"], "n": v["n"]}
                    for k, v in by.items()},
    }


def backend_events(unit: Path) -> list[dict]:
    out = []
    for ts, ev in L.read_log(unit):
        m = BACKEND_EVENT_RE.match(ev)
        if m:
            out.append({"time": ts, "step": m.group("step"), "harness": m.group("harness"),
                        "model": m.group("model"), "label": m.group("label") or ""})
    return out


def round_backends(unit: Path) -> "OrderedDict[str, list[str]]":
    """{round label ("round03" or "front" for pre-round steps): sorted
    ["harness:model", ...] seen there}, in first-seen order. Distinct
    configs within one round are normal (osp/msp/zmip can each resolve a
    fallback pool differently) -- eca-rsi#5 wants this listed as fact, not
    flagged as a mismatch the way check_agent_config's resume check is."""
    by: "OrderedDict[str, set]" = OrderedDict()
    for e in backend_events(unit):
        m = ROUND_RE.match(e["step"])
        key = m.group(0) if m else "front"
        by.setdefault(key, set()).add(f"{e['harness']}:{e['model']}")
    return OrderedDict((k, sorted(v)) for k, v in by.items())


def backend_summary_md(unit: Path) -> list[str]:
    by_round = round_backends(unit)
    if not by_round:
        return ["## Backends per round", "", "no backend reported (predates backend logging, or no fallback pool was in play)"]
    rows = [f"| {k} | {', '.join(v)} |" for k, v in by_round.items()]
    return ["## Backends per round", "", "| round | backend(s) used |", "|---|---|", *rows]


def summary_md(unit: Path) -> list[str]:
    s = summarize(unit)
    if not s["n"]:
        return ["## Agent cost", "", "no cost/token usage reported (backend does not report it, or the run predates cost logging)"]
    rows = [f"| {step} | {v['n']} | ${v['usd']:.2f} | {v['tokens_in']} | {v['tokens_out']} |" for step, v in s["by_step"].items()]
    return ["## Agent cost", "",
            f"Total: **${s['total']:.2f}**, {s['tokens_in']} input / {s['tokens_out']} output tokens over "
            f"{s['n']} agent run(s) — from `cost` events in progress.log", "",
            "| step | agent runs | USD | tokens in | tokens out |", "|---|---|---|---|---|", *rows]

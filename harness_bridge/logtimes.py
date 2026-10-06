"""Read stage/tool durations off a timestamped pipeline log.

    python -m harness_bridge.logtimes <slurm-or-run.log> [--top 15]

Every line the bridge handler writes starts with "MM-DD HH:MM:SS"; a stage
line ("== normalize/log1p", "[tag] == harmony ...") lasts until the next
stamped line, so the gaps are the stage durations. Tool "took" lines and the
per-run "time: wall ..." summaries are aggregated separately.
"""
from __future__ import annotations

import re
import sys
from collections import defaultdict
from datetime import datetime

STAMP = re.compile(r"^(?:\[[^\]]*\] )?(\d\d-\d\d \d\d:\d\d:\d\d) (.*)$")
TOOK = re.compile(r"== \[([^\]]+)\] (\S+) took ([\d.]+) s")
STAGE = re.compile(r"^(?:\[[^\]]*\] )?== (?!\[)([^:(]{3,60})")


def durations(lines):
    """[(seconds_until_next_stamp, message)] for stamped lines, in order."""
    stamped = []
    for line in lines:
        m = STAMP.match(line.rstrip("\n"))
        if m:
            stamped.append((datetime.strptime(m.group(1), "%m-%d %H:%M:%S"), m.group(2)))
    out = []
    for (t, msg), (t_next, _) in zip(stamped, stamped[1:]):
        gap = (t_next - t).total_seconds()
        out.append((gap if gap >= 0 else gap + 366 * 86400, msg))  # year roll-over
    return out


def report(lines, top=15):
    stages, tools = defaultdict(float), defaultdict(lambda: [0.0, 0])
    for gap, msg in durations(lines):
        m = TOOK.search(msg)
        if m:
            tools[m.group(2)][0] += float(m.group(3)); tools[m.group(2)][1] += 1
            continue
        m = STAGE.match(msg)
        if m and "agent:" not in msg:
            stages[m.group(1).strip()] += gap
    print("stage (time until the next stamped line)")
    for name, secs in sorted(stages.items(), key=lambda kv: -kv[1])[:top]:
        print(f"  {secs:9.0f} s  {name}")
    if tools:
        print("tool calls >= 1 s ('took' lines)")
        for name, (secs, n) in sorted(tools.items(), key=lambda kv: -kv[1][0])[:top]:
            print(f"  {secs:9.1f} s  {name} ×{n}")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print(__doc__); return 2
    top = int(argv[argv.index("--top") + 1]) if "--top" in argv else 15
    with open(argv[0], errors="replace") as fh:
        report(fh.readlines(), top)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

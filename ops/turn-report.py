"""A run's model turns by error class and by size (#31): do the failures go with large contexts or many images?

usage: bash ops/runpy.sh ops/turn-report.py <run root>
Reads only that run: its session files, then each turn's bridge request and attempt results, paced (the bridge and
pool folders hold tens of thousands of requests; never walk them). Turns recorded before the fields existed are
classified from their error text; their image sizes are unknown.
"""
import collections
import os
import re
import sys
import time
from pathlib import Path

from ecarsi.agent.dispatch import error_class
from ecarsi.files import read

PACE = 0.02  # seconds between the bridge and pool reads
TOKENS = ((50_000, "<50k"), (100_000, "50-100k"), (200_000, "100-200k"), (float("inf"), ">=200k"))
IMAGE_BYTES = ((1, "none"), (1 << 20, "<1 MB"), (5 << 20, "1-5 MB"), (float("inf"), ">=5 MB"))
OK = {"success", "no result"}  # no result: still running, or a pool turn whose request the pruner removed


def bucket(value, edges):
    return "unknown" if value is None else next(name for edge, name in edges if value < edge)


def attempts(run, bridge, pool):
    """(session, turn, result.json of each attempt) for every model turn of the run's sessions."""
    sessions = sorted({m.group(1) for path in Path(run).rglob("*.turn-*")
                       if (m := re.match(r"(.+)\.turn-\d+\.", path.name))})
    for session in sessions:
        turn = 0
        while (state := read(bridge / "requests" / f"{session}.turn-{turn}" / "state.json")) is not None:
            for attempt in state.get("attempts", []):
                time.sleep(PACE)
                if "turn_id" in attempt:
                    result = read(bridge / "turns" / attempt["turn_id"] / "result.json")
                else:
                    request = pool / "requests" / attempt["pool_request_id"]
                    current = read(request / "request.json") or {}
                    result = read(request / str(current.get("attempt_id")) / "outputs" / "result.json")
                yield session, turn, result or dict(outcome="no result", error_class="no result")  # running, or pruned
            turn += 1


def main(run):
    rows = []
    for session, turn, r in attempts(run, Path(os.environ["BRIDGE"]), Path(os.environ["POOL"])):
        usage = (r.get("response") or {}).get("usage") or {}
        klass = r["error_class"] if "error_class" in r else error_class(
            r["outcome"], r.get("error"), r.get("error_detail"), r.get("provider_response"))
        rows.append(dict(session=session, turn=turn, outcome=r["outcome"], klass=klass or "success",
                         tokens=r.get("input_tokens", usage.get("tokens_in")), image_bytes=r.get("image_bytes"),
                         images=r.get("images"), error=r.get("error"), detail=(r.get("error_detail") or "")[:120]))
    print(f"{run}: {len(rows)} model-turn attempts in {len({r['session'] for r in rows})} sessions")
    print("by class:", dict(collections.Counter(r["klass"] for r in rows).most_common()))
    for title, key, edges in (("input tokens", "tokens", TOKENS), ("image bytes", "image_bytes", IMAGE_BYTES)):
        print(f"by {title}:")
        groups = collections.defaultdict(collections.Counter)
        for r in rows:
            groups[bucket(r[key], edges)][r["klass"]] += 1
        for name in [n for _, n in edges] + ["unknown"]:
            if name in groups:
                total = sum(groups[name].values())
                failed = {k: v for k, v in groups[name].items() if k not in OK}
                print(f"  {name:>9}: {total:5d} attempts, {sum(failed.values()):4d} failed {failed or ''}")
    for r in [r for r in rows if r["klass"] not in OK][:30]:
        print(f"  {r['klass']:16s} {r['session']}.turn-{r['turn']} tokens={r['tokens']} images={r['images']} "
              f"{r['error']}: {r['detail']}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])

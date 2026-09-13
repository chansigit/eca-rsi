"""Score the models we have already run, from the Slurm logs they left behind.

    python eval/baseline.py [--model NAME] /path/to/logs   # a directory is walked for rsi-slurm-*.log

No API calls: every number here comes from runs that were already paid for. This is the
free half of the exam -- it answers "how often did the incumbent model produce something the
host accepted" without asking any model anything.

What is counted, and why the categories are kept apart:

  format      the submission could not be parsed or violated the schema (bad JSON, an enum
              value outside the allowed set, a missing required field). Purely mechanical;
              a model that trips these cannot drive the pipeline whatever its biology.
  vocabulary  the submission is well-formed but names something that does not exist -- a
              cluster id of 'all', '*' or ''. Read this as the model reaching for an
              expressive power the schema does not offer (it wants to say "every cluster"),
              not as sloppiness. A high rate here is a schema-design signal, not only a
              model score.
  unjustified the shape is legal but the justification fields were left empty ("evidence must
              provide non-empty text for distinctness / markers"). The model answered without
              saying why -- cheap to detect and worth watching, since the whole design assumes
              a decision arrives with its reasoning attached.
  consistency the host caught the submission contradicting itself: clusters merged into one
              group carry different fine labels ("merged group N+N disagrees on fine_label").
              Nothing external is needed to see this is wrong.
  domain      the host's substantive guardrails fired: the removal budget, a lineage plan that
              merges cells the connectivity check placed on separate UMAP islands, a sample
              column that leaves cells NA. The submission was valid and the host disagreed
              with its content.
  navigation  the agent read a path that does not exist -- it guessed a filename instead of
              listing the directory. Costs a turn, never reaches a submission.
  tool        an exception inside a read-only helper the agent called (check_deg, subcluster).
              Usually the agent's arguments, sometimes ours; kept separate because it costs a
              turn but never produces a wrong answer.

Only format/vocabulary/unjustified/consistency/domain count toward the headline rejection
rate: those are submissions the host refused. navigation and tool cost turns and money but
never produced a wrong answer, and lumping them in would flatter a model that submits rarely.

Rejections are counted against the submit calls in the same run, so the headline number is
"host round-trips per submission", not a raw count that grows with fleet size.

WHAT THIS IS NOT
----------------
Not an apples-to-apples benchmark. Each model is scored on whatever tasks it happened to run,
and the task mixes differ enormously -- the incumbent's 24k submissions are mostly zoomin
lineage annotation across six batches, while a few hundred submissions from a pilot are mostly
per-sample OSP annotation. Rejection rates are comparable *within* a model (which category
dominates, whether it improves after a prompt change), not *between* models. A real comparison
needs the same questions put to both, which is what the fixture replay is for.
"""

from __future__ import annotations

import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

# "== [zmip B cell] HARNESS=openai api=responses server_state=on model=doubao-... run: 63 model
#  request(s), 5191909 input / 55736 output tokens (34516 reasoning)"
RUN = re.compile(
    r"HARNESS=(?P<harness>\w+).*?model=(?P<model>[\w.\-]+) run: (?P<requests>\d+) model request\(s\), "
    r"(?P<input>\d+) input / (?P<output>\d+) output tokens \((?P<reasoning>\d+) reasoning\)"
)
# "== [zmip B cell] time: wall 1593 s, tools 73.6 s in 101 call(s) — check_deg 72.4 s ×33, ..."
TOOLS = re.compile(r"time: wall (?P<wall>\d+) s, tools [\d.]+ s in \d+ call\(s\) — (?P<breakdown>.*)")
CALL = re.compile(r"(\w+) [\d.]+ s ×(\d+)")
REJECT = re.compile(r"tool (?P<kind>error|exception) in (?P<tool>[\w]*)[:=] ?(?P<message>.*)")
COST = re.compile(r"agent cost: \$([0-9.]+)")


def category(kind: str, message: str) -> str:
    if kind == "exception":
        return "tool"
    if "JSON parse error" in message:
        return "format"
    if "is not a current cluster id" in message:
        return "vocabulary"
    if ("No such file or directory" in message or "File does not exist" in message
            or "would block or produce infinite out" in message):
        return "navigation"
    if "must provide non-empty text" in message:
        return "unjustified"
    if "not final yet" in message or "disagrees on" in message:
        return "consistency"
    if ("removal budget" in message or "is shared by labels in different lineages" in message
            or "not a valid partition" in message):
        return "domain"
    if ("must be" in message or "missing field" in message or "missing [" in message
            or "needs a safe name and nonempty members" in message):
        return "format"
    return "other"


def scan(path: Path, default_model: str = "unknown") -> dict:
    """One log -> per-model tallies. A log is one Slurm job, so the model is normally
    constant; if a run was resumed under a different model both appear and each run's
    own summary line decides which one owns it."""
    out = defaultdict(lambda: {"runs": 0, "requests": 0, "input": 0, "output": 0, "reasoning": 0,
                               "wall": 0, "submits": 0, "cost": 0.0, "reject": Counter()})
    # The claude adapter emits no "run: N model request(s)" summary, so a log produced by it
    # declares no model and no token counts. Label those with --model; the token columns stay
    # zero because the backend never reported them, not because nothing was spent.
    model = default_model
    with path.open(errors="replace") as fh:
        for line in fh:
            m = RUN.search(line)
            if m:
                model = m["model"]
                s = out[model]
                s["runs"] += 1
                for k in ("requests", "input", "output", "reasoning"):
                    s[k] += int(m[k])
                continue
            m = TOOLS.search(line)
            if m:
                s = out[model]
                s["wall"] += int(m["wall"])
                for name, n in CALL.findall(m["breakdown"]):
                    if name.startswith(("submit_", "finalize_")):
                        s["submits"] += int(n)
                continue
            m = COST.search(line)
            if m:
                out[model]["cost"] += float(m.group(1))
                continue
            m = REJECT.search(line)
            if m:
                out[model]["reject"][category(m["kind"], m["message"])] += 1
    return out


def _self_check() -> None:
    """One real message per category, copied from production logs."""
    cases = [
        ("error", "JSON parse error, fix and resubmit: Expecting ',' delimiter: line 1 column 9", "format"),
        ("error", "validation failed, fix and resubmit:\\n- clusters[3].confidence must be "
                  "high|medium|low: 'medium-high'", "format"),
        ("error", "validation failed, fix and resubmit:\\n- qc_action cluster 'all' is not a "
                  "current cluster id", "vocabulary"),
        ("error", "invalid, fix and resubmit:\\n- evidence must provide non-empty text for "
                  "distinctness / markers", "unjustified"),
        ("error", "not final yet:\\n- merged group 3+7 disagrees on fine_label", "consistency"),
        ("error", "removal budget: you are removing 12.4% of this lineage's cells", "domain"),
        ("error", "fix and resubmit:\\n- island_2 (81 cells) is shared by labels in different "
                  "lineages", "domain"),
        ("error", "[Errno 2] No such file or directory: '/scratch/.../de_top.csv'", "navigation"),
        ("exception", "'JSONDecodeError: Unterminated string starting at: line 1 column 122'", "tool"),
    ]
    for kind, message, want in cases:
        got = category(kind, message)
        assert got == want, f"{message[:40]!r} -> {got}, expected {want}"


def main(argv: list[str]) -> int:
    _self_check()
    paths: list[Path] = []
    label = "unknown"
    if "--model" in argv:
        i = argv.index("--model")
        label, argv = argv[i + 1], argv[:i] + argv[i + 2:]
    for a in argv or ["."]:
        p = Path(a)
        paths += sorted(p.rglob("rsi-slurm-*.log")) if p.is_dir() else [p]
    if not paths:
        print("no logs found", file=sys.stderr)
        return 1

    total = defaultdict(lambda: {"runs": 0, "requests": 0, "input": 0, "output": 0, "reasoning": 0,
                                 "wall": 0, "submits": 0, "cost": 0.0, "reject": Counter()})
    for p in paths:
        for model, s in scan(p, label).items():
            t = total[model]
            for k, v in s.items():
                if k == "reject":
                    t[k].update(v)
                else:
                    t[k] += v

    cats = ("format", "vocabulary", "unjustified", "consistency", "domain", "navigation", "tool", "other")
    print(f"{len(paths)} log(s)\n")
    for model, s in sorted(total.items(), key=lambda kv: -kv[1]["runs"]):
        bad = sum(s["reject"][c] for c in ("format", "vocabulary", "unjustified", "consistency", "domain"))
        print(f"== {model}")
        print(f"   {s['runs']:,} agent run(s), {s['requests']:,} model request(s), "
              f"{s['submits']:,} submission(s), {s['wall']/3600:.1f} h wall")
        print(f"   tokens: {s['input']/1e6:,.0f}M in / {s['output']/1e6:.1f}M out "
              f"({s['reasoning']/1e6:.1f}M reasoning)"
              + (f", cost ${s['cost']:,.2f}" if s["cost"] else ", cost not reported by backend"))
        if s["submits"]:
            print(f"   rejected submissions per accepted one: {bad / s['submits']:.3f}")
        for c in cats:
            n = s["reject"][c]
            if n:
                share = f" ({n / s['submits'] * 100:5.1f}% of submissions)" if s["submits"] else ""
                print(f"     {c:<11} {n:5,}{share}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

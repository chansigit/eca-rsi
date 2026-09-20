"""Shared cell-count convergence policy for legacy and durable workflows."""
import json
from pathlib import Path

RELEASE_FRAC = 0.01        # (1) this round removed < 1% of what entered it ...
RELEASE_MIN_REMOVED = 100  #     ... or fewer than 100 cells
PLATEAU_FRAC = 0.02        # (2) three consecutive rounds each < 2%
PLATEAU_ROUNDS = 3
RELEASE_MAX_REMOVED = 1000 # (3) floor on top of (1)/(2): a round that still removed >= 1000 cells never
                           #     releases — "< 1%" of 400k cells is 4k cells (issue #3; 5 of 6 tome releases)
DEFAULT_CAP = 15           # safety ceiling in auto mode (forced, flagged release); 10 until 2026-09-18


def decide(n: int, stats: list[dict], rounds: int | None, cap: int, extra: int = 0,
           max_removed: int = RELEASE_MAX_REMOVED) -> tuple[str, str]:
    """(decision, reason) after round n; stats includes round n. `extra` keeps
    the loop going that many rounds past the convergence rule (counted through
    the recorded reasons, so it survives a resume); the cap still wins.
    `max_removed` is the absolute floor: neither convergence path may release
    a round that removed at least that many cells."""
    st = stats[-1]
    if rounds is not None:
        return ("release", f"fixed --rounds {rounds}") if n >= rounds else ("continue", f"--rounds {rounds}")
    if n == 1:
        return "continue", "round 1 never releases"
    if n >= cap:
        return "release", f"FORCED: safety cap {cap} rounds reached"
    converged = None
    if st["frac"] < RELEASE_FRAC or st["removed"] < RELEASE_MIN_REMOVED:
        converged = (f"removed {100 * st['frac']:.2f}% < {100 * RELEASE_FRAC:.0f}%" if st["frac"] < RELEASE_FRAC
                     else f"removed {st['removed']} cells < {RELEASE_MIN_REMOVED}")
    else:
        last = stats[-PLATEAU_ROUNDS:]
        if len(last) == PLATEAU_ROUNDS and all(x["frac"] < PLATEAU_FRAC for x in last):
            fracs = ", ".join(f"{100 * x['frac']:.2f}%" for x in last)
            converged = f"last {PLATEAU_ROUNDS} rounds each removed < {100 * PLATEAU_FRAC:.0f}% ({fracs})"
    if converged is None:
        return "continue", f"removed {100 * st['frac']:.2f}% ({st['removed']} cells)"
    if st["removed"] >= max_removed:
        return "continue", f"removed {100 * st['frac']:.2f}% but {st['removed']:,} cells >= {max_removed:,} floor"
    done = sum(1 for x in stats[:-1] if str(x.get("reason", "")).startswith("converged"))
    if done < extra:
        return "continue", f"converged ({converged}); extra round {done + 1}/{extra}"
    return "release", converged + (f"; +{extra} extra round(s) done" if extra else "")


PREV_COLS = ("msp_ann_cluster", "msp_ann_coarse", "msp_ann_fine", "msp_ann_action",
             "zmip_lineage", "zmip_cluster", "zmip_ann_coarse", "zmip_ann_fine", "zmip_reassigned_from",
             "zmip_action", "_msp_action", "_msp_verdict")


# The manual gearbox. Both generations re-read it at the only moment a decision is made -- the
# round boundary -- so the limits can be moved while a unit runs. Generation 2 keeps the policy
# it was admitted with in its immutable spec; this file is the one thing allowed to override it.
CONTROL_FILE = "loop_control.json"
CONTROL_KEYS = {"cap": int, "rounds": int, "extra_rounds_after_convergence": int, "stop_after_round": int,
                "max_removed": int, "pause": bool, "pause_after_stage": str}


def read_control(unit, on_error=None) -> dict:
    """<unit>/loop_control.json, validated; a bad file is reported through `on_error` and
    ignored, never failing a run. cap/rounds/stop_after_round/max_removed >= 1,
    extra_rounds_after_convergence >= 0, rounds may be null."""
    path = Path(unit) / CONTROL_FILE
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text())
        if not isinstance(raw, dict):
            raise ValueError("not a JSON object")
        out = {}
        for key, value in raw.items():
            if key not in CONTROL_KEYS:
                raise ValueError(f"unknown key {key!r} (allowed: {', '.join(CONTROL_KEYS)})")
            if key == "pause":
                if type(value) is not bool:
                    raise ValueError("pause must be a boolean")
                out[key] = value
                continue
            if key == "pause_after_stage":
                if value not in (None, "crosssample", "zoomin"):
                    raise ValueError("pause_after_stage must be crosssample, zoomin or null")
                out[key] = value
                continue
            if value is None:
                if key == "rounds":
                    out[key] = None
                continue
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{key} must be an integer")
            low = 0 if key == "extra_rounds_after_convergence" else 1
            if value < low:
                raise ValueError(f"{key} must be >= {low}")
            out[key] = value
        return out
    except (OSError, ValueError) as exc:
        if on_error is not None:
            on_error(f"loop_control.json ignored: {exc}")
        return {}


def resolve(policy: dict, control: dict) -> dict:
    """The policy a unit was admitted with, with loop_control's overrides applied. Only the four
    decision limits can be moved; the rest of the file is about stopping, not about the rule."""
    return {**policy, **{k: v for k, v in control.items() if k in policy}}


def decide_with_control(n: int, stats: list[dict], policy: dict, control: dict) -> tuple[str, str]:
    """`decide` under the resolved policy, except that a manual stop outranks everything --
    including a release the rule would have taken. Stopping is the one instruction the loop
    cannot infer, so it is never overruled by one it can."""
    p = resolve(policy, control)
    if control.get("pause") or control.get("stop_after_round") == n:
        return "pause", (f"PAUSED: loop_control stopped the unit after round {n}; "
                         "clear the control and resume the dataset to continue")
    return decide(n, stats, p["rounds"], p["cap"], p["extra_rounds_after_convergence"], p["max_removed"])

"""Shared cell-count convergence policy for legacy and durable workflows."""

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

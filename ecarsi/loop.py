"""ecarsi-loop — the self-driving round loop over crosssample (msp) + zoomin (zmip).

    python -m ecarsi.loop <unit_dir> [--rounds N] [--force-reopen]

persample (osp) runs once — QC and doublet calls are per-sample by nature.
Everything after it is repeated on the survivors until the cell count stops
moving:

    round 1   crosssample (sample inclusion agent + msp integrate/inspect/annotate)
              → zoomin (zmip)                    rounds/round01/{crosssample,zoomin,ledger}
    round N   previous round's zoomin/annotated_zmip.h5ad, prior labels renamed
              r(N-1)_* → msp --from-h5ad (re-integrate from raw counts, inspect,
              annotate) → zoomin                 rounds/roundNN/{input.h5ad,crosssample,zoomin,ledger}

Stop rule — cell counts only (labels carry wording randomness and are
deliberately NOT a criterion):
  --rounds N given   run exactly N rounds, release after round N.
  otherwise          release after a round when (1) it removed < 1% of the
                     cells that entered it OR fewer than 100 cells, or
                     (2) the last three rounds each removed < 2%.
                     Round 1 never releases (it works on raw integration);
                     a safety cap (--cap, default 10) forces a flagged
                     release so the loop cannot run forever.
The loop never stops for a human: doubts accumulate as flags and are
reported once, in release/needs_review.{md,json} (ecarsi.review).

Manual overrides — <unit>/loop_control.json, re-read at every round
boundary (the only moment a decision is made), so it can be edited while a
round is running and takes effect when that round ends:
    {"cap": 12,                            # raise/lower the safety cap
     "rounds": 8,                          # or fix the total like --rounds
     "extra_rounds_after_convergence": 2,  # keep going n rounds past the stop rule
     "stop_after_round": 9}                # pause (exit 3, no release) after round 9; re-run to continue
Every override is logged and written into that round's stats reason.
--force-reopen continues past an existing release (the superseded round's
decision becomes 'continue').

Per round: stats.txt (n_in, n_out, removed, frac, decision, reason, elapsed_s),
decision.txt (continue | release), ledger/ (cell_ledger.csv + Sankeys across
all rounds so far); the landing pages (ecarsi.index) are rewritten after
every round. progress.log records every event. Re-running resumes: finished
rounds are skipped via their decision.txt, half-done rounds via the step
contracts. Paths: ecarsi.layout.

Env: MODEL, MSP_PYTHON / ZMIP_PYTHON (as in crosssample / zoomin).
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import cost, crosssample, mirror, prune, review, zoomin
from . import downstream as D
from . import release_state as R
from .run_state import file_identity, read_json, write_json
from . import layout as L
from .index import fmt_elapsed, read_stats, write_all
from .ledger import run_ledger

RELEASE_FRAC = 0.01        # (1) this round removed < 1% of what entered it ...
RELEASE_MIN_REMOVED = 100  #     ... or fewer than 100 cells
PLATEAU_FRAC = 0.02        # (2) three consecutive rounds each < 2%
PLATEAU_ROUNDS = 3
DEFAULT_CAP = 10           # safety ceiling in auto mode (forced, flagged release)


def decide(n: int, stats: list[dict], rounds: int | None, cap: int, extra: int = 0) -> tuple[str, str]:
    """(decision, reason) after round n; stats includes round n. `extra` keeps
    the loop going that many rounds past the convergence rule (counted through
    the recorded reasons, so it survives a resume); the cap still wins."""
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
    done = sum(1 for x in stats[:-1] if str(x.get("reason", "")).startswith("converged"))
    if done < extra:
        return "continue", f"converged ({converged}); extra round {done + 1}/{extra}"
    return "release", converged + (f"; +{extra} extra round(s) done" if extra else "")


CONTROL_KEYS = {"cap": int, "rounds": int, "extra_rounds_after_convergence": int, "stop_after_round": int}


def read_control(unit: Path) -> dict:
    """<unit>/loop_control.json, validated; a bad file is reported and ignored
    (never fails a run). Values: cap/rounds/stop_after_round >= 1,
    extra_rounds_after_convergence >= 0, rounds may be null."""
    p = unit / L.LOOP_CONTROL
    if not p.is_file():
        return {}
    try:
        raw = json.loads(p.read_text())
        if not isinstance(raw, dict):
            raise ValueError("not a JSON object")
        out = {}
        for key, value in raw.items():
            if key not in CONTROL_KEYS:
                raise ValueError(f"unknown key {key!r} (allowed: {', '.join(CONTROL_KEYS)})")
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
        _log(unit, f"loop_control.json ignored: {exc}")
        return {}


def _controls(unit: Path, args, seen: dict) -> tuple[int | None, int, int, int | None]:
    """(rounds, cap, extra, stop_after) for the next decision: CLI values
    overridden by loop_control.json; each change is logged once."""
    ctl = read_control(unit)
    rounds = ctl["rounds"] if "rounds" in ctl else args.rounds
    cap = ctl.get("cap", args.cap)
    extra = ctl.get("extra_rounds_after_convergence", 0)
    stop_after = ctl.get("stop_after_round")
    now = {"rounds": rounds, "cap": cap, "extra_rounds_after_convergence": extra, "stop_after_round": stop_after}
    base = {"rounds": args.rounds, "cap": args.cap, "extra_rounds_after_convergence": 0, "stop_after_round": None}
    for key, value in now.items():
        if seen.get(key, base[key]) != value:
            _log(unit, f"loop_control: {key} {seen.get(key, base[key])} -> {value}")
    seen.update(now)
    return rounds, cap, extra, stop_after


PREV_COLS = ("msp_ann_cluster", "msp_ann_coarse", "msp_ann_fine", "msp_ann_action",
             "zmip_lineage", "zmip_cluster", "zmip_ann_coarse", "zmip_ann_fine", "zmip_reassigned_from",
             "zmip_action", "_msp_action", "_msp_verdict")


_log = L.log_event


def _elapsed_from_log(unit: Path, n: int) -> float | None:
    """Round wall time from progress.log ('round N start' → 'round N stats'),
    for rounds whose stats.txt predates the elapsed_s field."""
    start = end = None
    for ts, ev in L.read_log(unit):
        if ev == f"round {n} start":
            start = time.mktime(time.strptime(ts, "%Y-%m-%d %H:%M:%S"))
        elif ev.startswith(f"round {n} stats") and start is not None:
            end = time.mktime(time.strptime(ts, "%Y-%m-%d %H:%M:%S"))
    # a round resumed from finished outputs logs start and stats in the same
    # second — its real wall time is unknown, not zero
    return (end - start) if (start is not None and end is not None and end > start) else None


def _write_stats(path: Path, st: dict) -> None:
    path.write_text(json.dumps(st) + "\n")


def _n_obs(h5ad: Path) -> int:
    import h5py

    with h5py.File(h5ad, "r") as f:
        idx = f["obs"].attrs["_index"]
        return int(f["obs"][idx].shape[0])


def _prepare_input(prev_h5ad: Path, out_h5ad: Path, prev_round: int) -> None:
    """Previous round's labels ride along under r{prev}_* names (prior
    evidence for this round's agents, columns for the ledger); the live
    msp_/zmip_ names are freed for this round to write."""
    import scanpy as sc

    ad = sc.read_h5ad(prev_h5ad)
    prefix = f"r{prev_round:02d}_"
    ren = {c: prefix + c.lstrip("_") for c in PREV_COLS if c in ad.obs}
    ad.obs = ad.obs.rename(columns=ren)
    for k in list(ad.uns):
        if k.endswith("_colors"):
            del ad.uns[k]
    tmp = out_h5ad.with_suffix(".tmp.h5ad")
    ad.write_h5ad(tmp)
    os.replace(tmp, out_h5ad)


def _run_msp_from_h5ad(py: str, h5ad: Path, outdir: Path, batch_col: str, species: str | None, model: str,
                       context: str | None = None, design: str | None = None) -> int:
    cmd = [py, "-m", "msp", "--from-h5ad", str(h5ad), "--batch-col", batch_col, "--outdir", str(outdir),
           "--annotate", "--model", model]
    if species:
        cmd += ["--species", species]
    if context:
        cmd += ["--report-context", context]
    if design:
        cmd += ["--design-context", design]
    cmd += D.options("msp")
    cmd_s = " ".join(shlex.quote(c) for c in cmd)
    print(f"[msp] {cmd_s}", flush=True)
    identity = D.prepare(py, "msp", [h5ad], outdir,
                         {"batch_col": batch_col, "species": species, "options": D.options("msp")})
    unit = outdir.parent.parent.parent
    ret = cost.run_streamed(cmd_s, unit, f"{outdir.parent.name}/{L.CROSSSAMPLE}")
    if ret != 0:
        return ret
    missing = [f for f in L.MSP_CONTRACT if not (outdir / f).is_file()]
    if missing:
        print(f"[fail] msp exited 0 but contract files missing: {missing}")
        return 1
    D.verify(py, "msp", [h5ad], outdir, identity)
    return 0


# ---------------------------------------------------------------- release

def _write_release(unit: Path, rounds: list[Path], stats: list[dict], forced: bool, superseded: bool, rel: Path) -> None:
    last = rounds[-1]
    final_src = L.zoomin_dir(last) / "annotated_zmip.h5ad"
    final = rel / "final.h5ad"
    if final.exists() or final.is_symlink():
        final.unlink()
    try:
        os.link(final_src, final)
    except OSError:
        shutil.copy2(final_src, final)
    for name in ("sankey_coarse.png", "cell_ledger.csv"):
        src = L.ledger_dir(last) / name
        if src.is_file():
            shutil.copy2(src, rel / name)
    from .umapdata import write_umap_json

    write_umap_json(final, rel / "umap.json")
    items = review.collect(unit, rounds, stats, forced)
    (rel / "needs_review.md").write_text(review.to_markdown(items, unit.name, len(rounds)))
    (rel / "needs_review.json").write_text(review.to_json(items))
    rows = "\n".join(f"| {i} | {s['n_in']} | {s['n_out']} | {s['removed']} | {100 * s['frac']:.2f}% | {s['decision']} "
                     f"| {s.get('reason', '')} | {fmt_elapsed(s.get('elapsed_s'))} |" for i, s in enumerate(stats, 1))
    summary = [f"# Release — {unit.name}", "",
               f"Rounds: {len(rounds)}" + (" (FORCED at the safety cap)" if forced else f" ({stats[-1].get('reason', '')})")
               + (" — supersedes an earlier release (--force-reopen)" if superseded else ""),
               f"Final cells: {stats[-1]['n_out']}", f"Output h5ad: {L.release_dir(unit) / 'final.h5ad'}",
               f"(= {final_src.relative_to(unit)}; zmip_ann_coarse / zmip_ann_fine are the final labels)", "",
               "| round | cells in | cells out | removed | removed % | decision | reason | wall time |",
               "|---|---|---|---|---|---|---|---|", rows, "",
               "Reports: " + ", ".join(f"round {i}: {L.crosssample_dir(r).relative_to(unit)}/report.html, "
                                       f"{L.zoomin_dir(r).relative_to(unit)}/report.html" for i, r in enumerate(rounds, 1)),
               "", f"Ledger + Sankey: {L.RELEASE}/cell_ledger.csv, {L.RELEASE}/sankey_coarse.png",
               f"Flags: {L.RELEASE}/needs_review.md ({len(items)} items: "
               + ", ".join(f"{t} {n}" for _, t, n, _ in review.counts(items)) + ")",
               "", f"Landing page: {L.INDEX} (open directly in a browser; optional live view with python -m ecarsi.serve)",
               "", *cost.summary_md(unit)]
    (rel / "summary.md").write_text("\n".join(summary) + "\n")
    # the same facts as a small machine-readable file, so nothing downstream
    # has to open final.h5ad or parse markdown to get the headline numbers
    (rel / "summary.json").write_text(json.dumps({
        "unit": unit.name, "rounds": len(rounds), "forced": forced, "superseded": superseded,
        "final_cells": stats[-1]["n_out"], "input_cells": stats[0]["n_in"] if stats else None,
        "final_h5ad": str(L.release_dir(unit) / "final.h5ad"), "labels": ["zmip_ann_coarse", "zmip_ann_fine"],
        "round_stats": stats, "needs_review": {t: n for _, t, n, _ in review.counts(items)},
        "agent_cost": cost.summarize(unit)}, indent=2, default=str))


def _release(unit: Path, rounds: list[Path], stats: list[dict], forced: bool, superseded: bool) -> None:
    with R.publication(unit) as stage:
        _write_release(unit, rounds, stats, forced, superseded, stage)
        D.seal_release(unit, rounds, release_dir=stage)
    write_all(unit)
    _log(unit, f"release rounds={len(rounds)} final_cells={stats[-1]['n_out']} forced={forced}")


# ---------------------------------------------------------------- main

@D.locked_unit
def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="ecarsi.loop", description=__doc__)
    ap.add_argument("unit", help="organize unit dir (persample/ completed inside)")
    ap.add_argument("--rounds", type=int, default=None,
                    help="run exactly this many rounds (overrides the convergence rule)")
    ap.add_argument("--cap", type=int, default=DEFAULT_CAP,
                    help=f"auto mode safety ceiling (forced, flagged release; default {DEFAULT_CAP})")
    ap.add_argument("--force-reopen", action="store_true", help="continue past an existing release")
    ap.add_argument("--no-prune", action="store_true",
                    help="keep every round's intermediate h5ads after release (default: ecarsi.prune drops them)")
    ap.add_argument("--mirror", metavar="DIR",
                    help="keep a copy of the run root here: light files after every stage, everything at release (ecarsi.mirror)")
    args = ap.parse_args(argv)
    unit = Path(args.unit).resolve()
    if not L.is_unit(unit):
        print(f"[loop] {unit} is not a unit dir (no {L.INPUT}/organized.h5ad)")
        return 2
    if args.mirror:
        mirror.configure(L.base_of(unit), args.mirror)
    L.rounds_root(unit).mkdir(exist_ok=True)

    R.recover(unit)
    superseded = False
    published_rounds = 0
    summary = L.release_dir(unit) / "summary.md"
    if summary.is_file():
        D.check_release(unit)
        if not args.force_reopen:
            if not args.no_prune:
                prune.prune_unit(unit)
                D.seal_release(unit, L.rounds(unit))
            mirror.sync(unit, full=True)
            print(f"[loop] already released: {summary} (use --force-reopen to continue)")
            return 0
        published_rounds = int(read_json(L.release_dir(unit) / "summary.json")["rounds"])
        c_rounds, c_cap, _, _ = _controls(unit, args, {})
        limit = c_rounds if c_rounds is not None else c_cap
        if limit <= published_rounds:
            raise ValueError("force-reopen requires a round limit greater than the published history")
        superseded = True
        _log(unit, "force-reopen: continuing past existing release")

    from . import model

    py = os.environ.get("MSP_PYTHON", sys.executable)
    rounds: list[Path] = []
    stats: list[dict] = []
    seen_controls: dict = {}
    decision = None
    n = 0
    while True:  # the limits are re-read every round, so loop_control.json can move them while we run
        n += 1
        c_rounds, c_cap, c_extra, c_stop = _controls(unit, args, seen_controls)
        if n > (c_rounds if c_rounds is not None else c_cap):
            break
        rdir = L.round_dir(unit, n)
        dec_p, st_p = rdir / L.DECISION, rdir / L.STATS
        if dec_p.is_file() and st_p.is_file():
            rounds.append(rdir)
            if not superseded or n > published_rounds:
                D.check_round(rdir)
            st = read_stats(st_p)
            st.setdefault("elapsed_s", _elapsed_from_log(unit, n))
            stats.append(st)
            if "reason" not in st:  # stats.txt from before reasons were recorded
                st["reason"] = decide(n, stats, c_rounds, c_cap, c_extra)[1]
            decision = dec_p.read_text().strip()
            _log(unit, f"round {n} already decided: {decision} (resume)")
            if decision == "release" and not (superseded and n == len(stats)):
                break
            if decision == "release":
                # Keep historical decisions immutable; reopening adds a new round.
                _log(unit, f"round {n} historical release retained; continuing (--force-reopen)")
            continue
        if c_stop is not None and n > c_stop:
            _log(unit, f"paused by loop_control after round {c_stop}; re-run to continue")
            write_all(unit)
            return 3
        rdir.mkdir(exist_ok=True)
        rounds.append(rdir)

        _log(unit, f"round {n} start")
        write_all(unit)
        t0 = time.time()
        if n == 1:
            ret = crosssample.main([str(unit), str(rdir)])
            if ret != 0:
                _log(unit, f"round 1 crosssample failed rc={ret}")
                return ret
        else:
            prev = L.round_dir(unit, n - 1)
            man = json.load(open(L.round_dir(unit, 1) / L.MANIFEST))
            from . import check_agent_config

            changed = check_agent_config(man, str(L.round_dir(unit, 1) / L.MANIFEST))
            if changed:
                _log(unit, f"round {n} agent config changed: {changed}")
            requested_batch = os.environ.get("MSP_BATCH_COL")
            if requested_batch and requested_batch != man["batch_col"]:
                raise ValueError("MSP_BATCH_COL changed after round 1; use a new output directory")
            inp = rdir / L.ROUND_INPUT
            src = L.zoomin_dir(prev) / "annotated_zmip.h5ad"
            if not src.is_file() and n - 1 == published_rounds and (L.release_dir(unit) / "final.h5ad").is_file():
                src = L.release_dir(unit) / "final.h5ad"
            receipt = rdir / "input_identity.json"
            source_identity = file_identity(src)
            if inp.is_file():
                saved = read_json(receipt)
                if saved != {"source": source_identity, "input": file_identity(inp)}:
                    raise ValueError("round input changed or comes from a different previous result")
            else:
                _prepare_input(src, inp, n - 1)
                write_json(receipt, {"source": source_identity, "input": file_identity(inp)})
                _log(unit, f"round {n} input prepared from round {n - 1} ({_n_obs(inp)} cells)")
            from .design import design_text

            ret = _run_msp_from_h5ad(py, inp, L.crosssample_dir(rdir), man["batch_col"], man.get("species"), model(),
                                     L.report_context(unit, rdir), design_text(unit))
            if ret != 0:
                _log(unit, f"round {n} msp failed rc={ret}")
                return ret
        write_all(unit)
        ret = zoomin.main([str(unit), str(rdir)])
        if ret != 0:
            _log(unit, f"round {n} zoomin failed rc={ret}")
            return ret

        n_in = _n_obs(L.crosssample_dir(rdir) / "integrated.h5ad")
        n_out = _n_obs(L.zoomin_dir(rdir) / "annotated_zmip.h5ad")
        removed = n_in - n_out
        frac = removed / max(n_in, 1)
        st = {"n_in": n_in, "n_out": n_out, "removed": removed, "frac": frac,
              "elapsed_s": round(time.time() - t0, 1)}
        stats.append(st)
        c_rounds, c_cap, c_extra, c_stop = _controls(unit, args, seen_controls)  # edits made during the round count now
        decision, reason = decide(n, stats, c_rounds, c_cap, c_extra)
        st["decision"], st["reason"] = decision, reason
        _write_stats(st_p, st)
        _log(unit, f"round {n} stats removed={removed}/{n_in} ({100 * frac:.2f}%) decision={decision} "
                   f"[{reason}] elapsed={fmt_elapsed(st['elapsed_s'])}")
        run_ledger(unit, rounds, L.ledger_dir(rdir))
        dec_p.write_text(decision + "\n")
        D.seal_round(rdir)
        write_all(unit)
        if decision == "release":
            break
        if c_stop is not None and n >= c_stop:
            _log(unit, f"paused by loop_control after round {n}; re-run to continue")
            return 3

    if decision != "release":
        raise ValueError("loop ended without a release decision (round limit below the completed history?)")
    forced = str(stats[-1].get("reason", "")).startswith("FORCED")
    _release(unit, rounds, stats, forced, superseded)
    if not args.no_prune:
        prune.prune_unit(unit)
    D.seal_release(unit, rounds)
    mirror.sync(unit, full=True)  # the whole unit incl. h5ads; drops what prune removed from the copy
    print(f"[done] {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

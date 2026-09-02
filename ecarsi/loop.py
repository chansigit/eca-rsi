"""ecarsi-loop — the self-driving round loop over crosssample (msp) + zoomin (zmip).

    python -m ecarsi.loop <unit_dir> [--rounds N] [--force-reopen]

persample (osp) runs once — QC and doublet calls are per-sample by nature.
Everything after it is repeated on the survivors until the cell count stops
moving:

    round 1   crosssample (sample inclusion agent + msp integrate/inspect/annotate)
              → zoomin (zmip)                    rounds/round01/{integrate,zoomin,ledger}
    round N   previous round's zoomin/annotated_zmip.h5ad, prior labels renamed
              r(N-1)_* → msp --from-h5ad (re-integrate from raw counts, inspect,
              annotate) → zoomin                 rounds/roundNN/{input.h5ad,integrate,zoomin,ledger}

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
reported once, in release/needs_review.md. --force-reopen continues past
an existing release (the superseded round's decision becomes 'continue').

Per round: stats.txt (n_in, n_out, removed, frac, decision, reason, elapsed_s),
decision.txt (continue | release), ledger/ (cell_ledger.csv + Sankeys across
all rounds so far).
progress.log records every event. Re-running resumes: finished rounds are
skipped via their decision.txt, half-done rounds via the step contracts.

Env: MODEL, MSP_PYTHON / ZMIP_PYTHON (as in crosssample / zoomin).
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import crosssample, zoomin
from .ledger import run_ledger

RELEASE_FRAC = 0.01        # (1) this round removed < 1% of what entered it ...
RELEASE_MIN_REMOVED = 100  #     ... or fewer than 100 cells
PLATEAU_FRAC = 0.02        # (2) three consecutive rounds each < 2%
PLATEAU_ROUNDS = 3
DEFAULT_CAP = 10           # safety ceiling in auto mode (forced, flagged release)


def decide(n: int, stats: list[dict], rounds: int | None, cap: int) -> tuple[str, str]:
    """(decision, reason) after round n; stats includes round n."""
    st = stats[-1]
    if rounds is not None:
        return ("release", f"fixed --rounds {rounds}") if n >= rounds else ("continue", f"--rounds {rounds}")
    if n == 1:
        return "continue", "round 1 never releases"
    if st["frac"] < RELEASE_FRAC or st["removed"] < RELEASE_MIN_REMOVED:
        return "release", (f"removed {100 * st['frac']:.2f}% < {100 * RELEASE_FRAC:.0f}%" if st["frac"] < RELEASE_FRAC
                           else f"removed {st['removed']} cells < {RELEASE_MIN_REMOVED}")
    last = stats[-PLATEAU_ROUNDS:]
    if len(last) == PLATEAU_ROUNDS and all(x["frac"] < PLATEAU_FRAC for x in last):
        fracs = ", ".join(f"{100 * x['frac']:.2f}%" for x in last)
        return "release", f"last {PLATEAU_ROUNDS} rounds each removed < {100 * PLATEAU_FRAC:.0f}% ({fracs})"
    if n >= cap:
        return "release", f"FORCED: safety cap {cap} rounds reached"
    return "continue", f"removed {100 * st['frac']:.2f}% ({st['removed']} cells)"


PREV_COLS = ("msp_ann_cluster", "msp_ann_coarse", "msp_ann_fine", "msp_ann_action",
             "zmip_lineage", "zmip_cluster", "zmip_ann_coarse", "zmip_ann_fine", "zmip_reassigned_from",
             "zmip_action", "_msp_action", "_msp_verdict")


def _log(unit: Path, event: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {event}"
    print(f"[loop] {line}", flush=True)
    with open(unit / "progress.log", "a") as f:
        f.write(line + "\n")


def _elapsed_from_log(unit: Path, n: int) -> float | None:
    """Round wall time from progress.log ('round N start' → 'round N stats'),
    for rounds whose stats.txt predates the elapsed_s field."""
    log = unit / "progress.log"
    if not log.is_file():
        return None
    start = end = None
    for line in log.read_text().splitlines():
        ev = line[20:]  # after "YYYY-MM-DD HH:MM:SS "
        if ev == f"round {n} start":
            start = time.mktime(time.strptime(line[:19], "%Y-%m-%d %H:%M:%S"))
        elif ev.startswith(f"round {n} stats") and start is not None:
            end = time.mktime(time.strptime(line[:19], "%Y-%m-%d %H:%M:%S"))
    # a round resumed from finished outputs logs start and stats in the same
    # second — its real wall time is unknown, not zero
    return (end - start) if (start is not None and end is not None and end > start) else None


def _fmt_elapsed(sec) -> str:
    if sec is None or sec != sec:
        return "n/a"
    sec = int(sec)
    return f"{sec // 3600}h{(sec % 3600) // 60:02d}m" if sec >= 3600 else f"{sec // 60}m{sec % 60:02d}s"


def _read_stats(path: Path) -> dict:
    """stats.txt: JSON (current) or the older 'k=v k=v' line."""
    text = path.read_text().strip()
    if text.startswith("{"):
        return json.loads(text)
    st = dict(tok.split("=", 1) for tok in text.split())
    return {k: (float(v) if k in ("frac", "elapsed_s") else v if k == "decision" else int(v)) for k, v in st.items()}


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


def _run_msp_from_h5ad(py: str, h5ad: Path, outdir: Path, batch_col: str, species: str | None, model: str) -> int:
    cmd = [py, "-m", "msp", "--from-h5ad", str(h5ad), "--batch-col", batch_col, "--outdir", str(outdir),
           "--annotate", "--model", model]
    if species:
        cmd += ["--species", species]
    cmd_s = " ".join(shlex.quote(c) for c in cmd)
    print(f"[msp] {cmd_s}", flush=True)
    if all((outdir / f).is_file() for f in crosssample.MSP_CONTRACT):
        print("[msp] contract already satisfied — skipping (resume)")
        return 0
    ret = subprocess.run(cmd_s, shell=True).returncode
    if ret != 0:
        return ret
    missing = [f for f in crosssample.MSP_CONTRACT if not (outdir / f).is_file()]
    if missing:
        print(f"[fail] msp exited 0 but contract files missing: {missing}")
        return 1
    return 0


# ---------------------------------------------------------------- flags → needs_review

def _needs_review(rounds: list[Path], forced: bool, stats: list[dict]) -> str:
    lines = ["# Needs review", "",
             "Everything the agents were unsure about or the host overrode, collected once at release. "
             "Nothing here stopped the loop.", ""]
    if forced:
        lines += ["## Forced release", f"- {stats[-1].get('reason')}; last round removed {100 * stats[-1]['frac']:.2f}% "
                  "— the loop did not converge on its own", ""]
    for i, (rdir, st) in enumerate(zip(rounds, stats), 1):
        lines.append(f"## Round {i}")
        if st["frac"] > 0.10:
            lines.append(f"- removed {100 * st['frac']:.1f}% of its cells ({st['removed']}/{st['n_in']}) — above the "
                         "~10% per-round budget")
        dec = rdir / "integrate" / "sample_decisions.csv"
        if dec.is_file():
            for r in csv.DictReader(open(dec)):
                if r["decision"] == "exclude":
                    lines.append(f"- sample {r['sample']} excluded from integration ({r['n_cells']} cells): {r['reason']}")
        insp = rdir / "integrate" / "inspection_proposal.json"
        if insp.is_file():
            for e in json.load(open(insp)).get("clusters", []):
                if e.get("action") == "flag" or e.get("verdict") == "ambiguous" or e.get("confidence") == "low":
                    lines.append(f"- inspect cluster {e['cluster']}: {e['verdict']} → {e['action']} "
                                 f"[{e['confidence']}] — {e['rationale']}")
        ann = rdir / "integrate" / "annotation_proposal.json"
        if ann.is_file():
            for e in json.load(open(ann)).get("clusters", []):
                if e.get("confidence") == "low" or (e.get("action") == "remove" and e.get("confidence") != "high"):
                    lines.append(f"- msp annotate cluster {e['cluster_id']}: {e['coarse_label']} / {e['fine_label']} "
                                 f"[{e['action']}, {e['confidence']}] — {e['rationale']}")
        zdir = rdir / "zoomin"
        plan_p = zdir / "zmip_plan.json"
        if plan_p.is_file():
            for ln in json.load(open(plan_p))["lineages"]:
                if "host:" in ln.get("reason", ""):
                    lines.append(f"- zmip lineage {ln['name']} not zoomed: {ln['reason']}")
                prop_p = zdir / ln["name"].replace("/", "_") / "annotation_proposal.json"
                if not prop_p.is_file():
                    continue
                prop = json.load(open(prop_p))
                if prop.get("budget_exceeded"):
                    lines.append(f"- zmip {ln['name']}: agent removed {100 * prop['agent_removed_fraction']:.1f}% "
                                 "of the lineage after a forced second look (budget_exceeded)")
                for e in prop.get("clusters", []):
                    if e.get("action") == "reassign":
                        lines.append(f"- zmip {ln['name']} cluster {e['cluster_id']} reassigned → {e['reassign_to']} "
                                     f"({e['fine_label']}) [{e['confidence']}]")
                    elif e.get("confidence") == "low" or (e.get("action") == "remove" and e.get("confidence") != "high"):
                        lines.append(f"- zmip {ln['name']} cluster {e['cluster_id']}: {e['fine_label']} "
                                     f"[{e['action']}, {e['confidence']}] — {e['rationale']}")
        lines.append("")
    return "\n".join(lines)


def _release(unit: Path, rounds: list[Path], stats: list[dict], forced: bool, superseded: bool) -> None:
    rel = unit / "release"
    rel.mkdir(exist_ok=True)
    last = rounds[-1]
    final_src = last / "zoomin" / "annotated_zmip.h5ad"
    final = rel / "final.h5ad"
    if final.exists() or final.is_symlink():
        final.unlink()
    try:
        os.link(final_src, final)
    except OSError:
        shutil.copy2(final_src, final)
    for name in ("sankey_coarse.png", "cell_ledger.csv"):
        src = last / "ledger" / name
        if src.is_file():
            shutil.copy2(src, rel / name)
    (rel / "needs_review.md").write_text(_needs_review(rounds, forced, stats))
    rows = "\n".join(f"| {i} | {s['n_in']} | {s['n_out']} | {s['removed']} | {100 * s['frac']:.2f}% | {s['decision']} "
                     f"| {s.get('reason', '')} | {_fmt_elapsed(s.get('elapsed_s'))} |" for i, s in enumerate(stats, 1))
    summary = [f"# Release — {unit.name}", "",
               f"Rounds: {len(rounds)}" + (" (FORCED at the safety cap)" if forced else f" ({stats[-1].get('reason', '')})")
               + (" — supersedes an earlier release (--force-reopen)" if superseded else ""),
               f"Final cells: {stats[-1]['n_out']} → release/final.h5ad "
               f"(= {final_src.relative_to(unit)}; zmip_ann_coarse / zmip_ann_fine are the final labels)", "",
               "| round | cells in | cells out | removed | removed % | decision | reason | wall time |",
               "|---|---|---|---|---|---|---|---|", rows, "",
               "Reports: " + ", ".join(f"round {i}: {r.relative_to(unit)}/integrate/report.html, "
                                       f"{r.relative_to(unit)}/zoomin/report.html" for i, r in enumerate(rounds, 1)),
               "", "Ledger + Sankey: release/cell_ledger.csv, release/sankey_coarse.png",
               "Flags: release/needs_review.md"]
    (rel / "summary.md").write_text("\n".join(summary) + "\n")
    write_index(unit, rounds, stats, forced)
    _log(unit, f"release rounds={len(rounds)} final_cells={stats[-1]['n_out']} forced={forced}")


def write_index(unit: Path, rounds: list[Path], stats: list[dict], forced: bool) -> None:
    """unit/index.html — a landing page linking every report of every round,
    the Sankey and the flags. Written at the UNIT root with unit-relative
    links so `python -m http.server` in the unit dir lands here and every
    asset resolves (a symlink into release/ would break relative paths)."""
    import html as _h

    rel = unit / "release"
    rows = "".join(f"<tr><td>{i}</td><td>{s['n_in']}</td><td>{s['n_out']}</td><td>{s['removed']}</td>"
                   f"<td>{100 * s['frac']:.2f}%</td><td>{s['decision']}</td><td>{_h.escape(str(s.get('reason', '')))}</td>"
                   f"<td>{_fmt_elapsed(s.get('elapsed_s'))}</td>"
                   f'<td><a href="{r.relative_to(unit)}/integrate/report.html">msp</a> · '
                   f'<a href="{r.relative_to(unit)}/zoomin/report.html">zmip</a> · '
                   f'<a href="{r.relative_to(unit)}/ledger/sankey_coarse.png">sankey</a></td></tr>'
                   for i, (r, s) in enumerate(zip(rounds, stats), 1))
    nr = _h.escape((rel / "needs_review.md").read_text())
    doc = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>release — {_h.escape(unit.name)}</title>
<style>body{{font-family:system-ui,sans-serif;max-width:1400px;margin:2rem auto;padding:0 1rem;color:#222}}
table{{border-collapse:collapse}}td,th{{border:1px solid #ddd;padding:.3rem .6rem;text-align:right}}th{{background:#f4f4f4}}
td:last-child{{text-align:left}}pre{{white-space:pre-wrap;background:#f7f7f7;padding:1rem;border-radius:6px;font-size:.85rem}}
img{{max-width:100%;border:1px solid #ddd}}</style></head><body>
<h1>Release — {_h.escape(unit.name)}</h1>
<p>{len(rounds)} round(s){" (FORCED at the safety cap)" if forced else " — " + _h.escape(str(stats[-1].get('reason', '')))} · final cells {stats[-1]['n_out']}
· <code>release/final.h5ad</code> (<code>zmip_ann_coarse</code> / <code>zmip_ann_fine</code> = final labels)</p>
<table><tr><th>round</th><th>cells in</th><th>cells out</th><th>removed</th><th>removed %</th><th>decision</th><th>reason</th><th>wall time</th><th>reports</th></tr>{rows}</table>
<h2>Cell identity across steps and rounds</h2><img src="release/sankey_coarse.png">
<p><a href="release/cell_ledger.csv">cell_ledger.csv</a> — one row per input cell, status + labels per stage · \
<a href="release/summary.md">summary.md</a> · <a href="release/needs_review.md">needs_review.md</a></p>
<h2>Needs review</h2><pre>{nr}</pre>
</body></html>"""
    link = unit / "index.html"
    if link.is_symlink():
        link.unlink()
    link.write_text(doc)


# ---------------------------------------------------------------- main

def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="ecarsi.loop", description=__doc__)
    ap.add_argument("unit", help="organize unit dir (persample/ completed inside)")
    ap.add_argument("--rounds", type=int, default=None,
                    help="run exactly this many rounds (overrides the convergence rule)")
    ap.add_argument("--cap", type=int, default=DEFAULT_CAP,
                    help=f"auto mode safety ceiling (forced, flagged release; default {DEFAULT_CAP})")
    ap.add_argument("--force-reopen", action="store_true", help="continue past an existing release")
    args = ap.parse_args(argv)
    unit = Path(args.unit).resolve()
    rounds_root = unit / "rounds"
    rounds_root.mkdir(exist_ok=True)

    superseded = False
    if (unit / "release" / "summary.md").is_file():
        if not args.force_reopen:
            print(f"[loop] already released: {unit / 'release' / 'summary.md'} (use --force-reopen to continue)")
            return 0
        superseded = True
        _log(unit, "force-reopen: continuing past existing release")

    from . import model

    py = os.environ.get("MSP_PYTHON", sys.executable)
    rounds: list[Path] = []
    stats: list[dict] = []
    last_round = args.rounds if args.rounds is not None else args.cap
    for n in range(1, last_round + 1):
        rdir = rounds_root / f"round{n:02d}"
        rdir.mkdir(exist_ok=True)
        rounds.append(rdir)
        dec_p, st_p = rdir / "decision.txt", rdir / "stats.txt"
        if dec_p.is_file() and st_p.is_file():
            st = _read_stats(st_p)
            st.setdefault("elapsed_s", _elapsed_from_log(unit, n))
            stats.append(st)
            if "reason" not in st:  # stats.txt from before reasons were recorded
                st["reason"] = decide(n, stats, args.rounds, args.cap)[1]
            decision = dec_p.read_text().strip()
            _log(unit, f"round {n} already decided: {decision} (resume)")
            if decision == "release" and not (superseded and n == len(stats)):
                break
            if decision == "release":
                # reopened past this release: the round is no longer the last one
                st["decision"] = decision = "continue"
                st["reason"] = "reopened (--force-reopen)"
                _write_stats(st_p, st)
                dec_p.write_text("continue\n")
                _log(unit, f"round {n} decision rewritten release → continue (force-reopen)")
            continue

        _log(unit, f"round {n} start")
        t0 = time.time()
        if n == 1:
            ret = crosssample.main([str(unit), str(rdir)])
            if ret != 0:
                _log(unit, f"round 1 crosssample failed rc={ret}")
                return ret
        else:
            prev = rounds_root / f"round{n - 1:02d}"
            man = json.load(open(rounds_root / "round01" / "manifest.json"))
            inp = rdir / "input.h5ad"
            if not inp.is_file():
                _prepare_input(prev / "zoomin" / "annotated_zmip.h5ad", inp, n - 1)
                _log(unit, f"round {n} input prepared from round {n - 1} ({_n_obs(inp)} cells)")
            ret = _run_msp_from_h5ad(py, inp, rdir / "integrate", man["batch_col"], man.get("species"), model())
            if ret != 0:
                _log(unit, f"round {n} msp failed rc={ret}")
                return ret
        ret = zoomin.main([str(unit), str(rdir)])
        if ret != 0:
            _log(unit, f"round {n} zoomin failed rc={ret}")
            return ret

        n_in = _n_obs(rdir / "integrate" / "integrated.h5ad")
        n_out = _n_obs(rdir / "zoomin" / "annotated_zmip.h5ad")
        removed = n_in - n_out
        frac = removed / max(n_in, 1)
        st = {"n_in": n_in, "n_out": n_out, "removed": removed, "frac": frac,
              "elapsed_s": round(time.time() - t0, 1)}
        stats.append(st)
        decision, reason = decide(n, stats, args.rounds, args.cap)
        st["decision"], st["reason"] = decision, reason
        _write_stats(st_p, st)
        _log(unit, f"round {n} stats removed={removed}/{n_in} ({100 * frac:.2f}%) decision={decision} "
                   f"[{reason}] elapsed={_fmt_elapsed(st['elapsed_s'])}")
        run_ledger(unit, rounds, rdir / "ledger")
        dec_p.write_text(decision + "\n")
        if decision == "release":
            break

    forced = str(stats[-1].get("reason", "")).startswith("FORCED")
    _release(unit, rounds, stats, forced, superseded)
    print(f"[done] {unit / 'release' / 'summary.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

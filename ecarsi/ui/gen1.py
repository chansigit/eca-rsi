"""Generation-1 units (the local path removed in 0.4.0): their state and page, read from
manifests, stats/decision files and progress.log. Old runs are still shown from their display zones;
delete this module with them."""

from __future__ import annotations

import csv
import html as _h
import json
import re
from pathlib import Path

from .. import layout as L
from .. import review
from .common import EXPLAIN, SANKEY_JS, UMAP_JS, _bar, _json, _n, _n_obs, _pct, _stat, fmt_elapsed, read_stats


def persample_state(unit: Path) -> dict:
    man = _json(L.persample_manifest(unit), {})
    samples = []
    for s in man.get("samples", []):
        d = L.sample_dir(unit, s)
        contract = L.PS_ANNOTATE_LIGHT if man.get("annotate", True) else L.PS_LIGHT  # light: renders from a display copy
        done = L.complete(d, contract)
        if man.get("schema_version") == 2:
            # Display the recorded validation; actual resume rehashes and
            # rereads outputs in osp_contract. Never hash H5AD on HTTP GET.
            state = _json(d / L.RUN_STATE, {})
            done = (done and state.get("state") == "complete" and state.get("exit_code") == 0
                    and state.get("identity") == s.get("identity")
                    and s["value"] not in man.get("failed_samples", [])
                    and all((d / f).is_file() for f in L.PS_QC_CONTRACT))
        empty = s["value"] in man.get("empty_samples", [])  # QC removed every cell; finished without outputs
        samples.append({"name": d.name, "value": s["value"], "n_cells": s["n_cells"], "dir": d,
                        "done": done or empty, "empty": empty, "report": (d / "report.html").is_file()})
    return {"manifest": bool(man), "sample_column": man.get("sample_column"), "species": man.get("species"),
            "n_excluded": sum(r["n_cells"] for r in (man.get("sample_mapping") or {}).get("exclude_cells", [])),
            "samples": samples, "n_done": sum(s["done"] for s in samples), "n": len(samples),
            "done": bool(samples) and all(s["done"] for s in samples)}


def _round_step(rdir: Path) -> str:
    """What a round without a decision is currently doing, from the light
    step markers only — the page must say the same thing on a display copy,
    which carries no h5ad."""
    cdir, zdir = L.crosssample_dir(rdir), L.zoomin_dir(rdir)
    if not L.complete(cdir, L.MSP_LIGHT):
        if not L.complete(cdir, L.MSP_INTEGRATED_LIGHT):
            return "crosssample · integrate" if (cdir.is_dir() or (rdir / L.ROUND_INPUT).is_file()) else "starting"
        if not (cdir / "inspection_proposal.json").is_file():
            return "crosssample · inspect"
        return "crosssample · annotate"
    if not L.complete(zdir, L.ZMIP_LIGHT):
        plan = _json(zdir / "zmip_plan.json")
        if not plan:
            return "zoomin · plan"
        zoomed = [ln["name"] for ln in plan["lineages"] if ln["zoom"]]
        done = [n for n in zoomed if L.complete(L.lineage_dir(zdir, n), L.ZMIP_LINEAGE_LIGHT)]
        return f"zoomin · lineages {len(done)}/{len(zoomed)}"
    if not (L.ledger_dir(rdir) / "cell_ledger.csv").is_file():
        return "ledger"
    return "deciding"


def rounds_state(unit: Path) -> list[dict]:
    out = []
    for rdir in L.rounds(unit):
        n = L.round_number(rdir)
        cdir, zdir = L.crosssample_dir(rdir), L.zoomin_dir(rdir)
        st_p, dec_p = rdir / L.STATS, rdir / L.DECISION
        r = {"n": n, "dir": rdir, "stats": None, "decision": None, "step": None,
             "msp_report": (cdir / "report.html").is_file(), "zmip_report": (zdir / "report.html").is_file(),
             "sankey": (L.ledger_dir(rdir) / "sankey_coarse.png").is_file()}
        if st_p.is_file() and dec_p.is_file():
            r["stats"] = read_stats(st_p)
            r["decision"] = dec_p.read_text().strip()
        else:
            r["step"] = _round_step(rdir)
            n_in = _round_input_cells(unit, n)  # from progress.log, so a display copy knows it too
            if n_in is None and (cdir / "integrated.h5ad").is_file():
                n_in = _n_obs(cdir / "integrated.h5ad")
            if n_in is not None:
                r["n_in"] = n_in
        out.append(r)
    return out


def _round_input_cells(unit: Path, n: int) -> int | None:
    """Cells entering round n, as the loop logged it ('round N input prepared
    from round M (X cells)'); round 1 has no such line."""
    pat = re.compile(rf"^round {n} input prepared from round \d+ \((\d+) cells\)")
    for _, event in reversed(L.read_log(unit)):
        m = pat.match(event)
        if m:
            return int(m.group(1))
    return None


_EVENT_ORGANIZE = re.compile(r"^organize: (\d+) cells")


_EVENT_RELEASE = re.compile(r"^release rounds=\d+ final_cells=(\d+)")


def _epoch(ts: str) -> float:
    import time

    return time.mktime(time.strptime(ts, "%Y-%m-%d %H:%M:%S"))


def log_events(log: list[tuple[str, str]], unit: Path | None = None, n_input: int | None = None) -> dict:
    """When cells entered and left a unit, as (epoch seconds, cells): the first
    'organize: N cells' line and the last 'release ... final_cells=N' line.
    Derived, never recorded: what is on disk now is the whole history.

    The log is not the only record of the arrival, and it is not always intact.
    `mca3.0/PeripheralBlood` organized at 21:19 on 2026-09-05 and its unit log opens an
    hour later on 'persample complete' -- a restart rebuilt the log, and a resume does not
    re-announce an organize step it is skipping. The dataset then had no arrival at all and
    dropped out of the fleet's cells-over-time chart. So fall back to what organize actually
    left on disk: its manifest's mtime, paired with the unit's own input count, which is how
    a generation-2 unit reports the same event. One arrival per unit either way, so a
    multi-unit run still sums to its own total rather than counting the dataset twice."""
    org = rel = None
    for ts, event in log:
        m = _EVENT_ORGANIZE.match(event)
        if m and org is None:
            org = (_epoch(ts), int(m.group(1)))
        m = _EVENT_RELEASE.match(event)
        if m:
            rel = (_epoch(ts), int(m.group(1)))
    if org is None and unit is not None and n_input:
        manifest = L.organize_manifest(unit.parent.parent)
        if manifest.is_file():
            org = (manifest.stat().st_mtime, int(n_input))
    return {"organize": org, "release": rel}


def _round_started_after(log: list[tuple[str, str]], n: int) -> tuple[int, str] | None:
    """(round, timestamp) of the newest 'round N start' with N > n, else None."""
    pat = re.compile(r"^round (\d+) start$")
    for ts, event in reversed(log):
        m = pat.match(event)
        if m and int(m.group(1)) > n:
            return int(m.group(1)), ts
    return None


def unit_state(unit: Path) -> dict:
    """Everything the pages need about a generation-1 unit, read from disk."""
    im = _json(L.input_manifest(unit), {})
    ps = persample_state(unit)
    rounds = rounds_state(unit)
    rel = L.release_dir(unit)
    released = (rel / "summary.md").is_file()
    log = L.read_log(unit)
    last = log[-1] if log else None
    # "persample complete: N experiments; 0 failed" is the success line, not a failure
    failed = bool(last) and "failed" in last[1] and not last[1].startswith("persample complete")
    if failed:
        stage, cls = f"failed — {last[1]}", "failed"
    elif last and last[1].startswith("paused by loop_control"):
        stage, cls = f"paused after round {len(rounds)} (loop_control.json) — re-run to continue", "running"
    elif released and (not rounds or rounds[-1]["decision"] is not None):
        stage, cls = "released", "released"
    elif rounds and rounds[-1]["decision"] is None:
        stage, cls = f"round {rounds[-1]['n']} · {rounds[-1]['step']}", "running"
    elif rounds:
        nxt = _round_started_after(log, rounds[-1]["n"])
        if nxt:  # a display copy carries the log line before any file of the new round
            stage, cls = f"round {nxt[0]} · crosssample (since {nxt[1][11:16]})", "running"
        else:
            stage, cls = f"round {rounds[-1]['n']} done, next round pending", "running"
    elif ps["manifest"] and not ps["done"]:
        stage, cls = f"persample {ps['n_done']}/{ps['n']} samples", "running"
    elif ps["done"]:
        stage, cls = "persample done, loop not started", "running"
    else:
        stage, cls = "organized, persample not started", "running"
    # cells now: survivors of the last finished round (final once released)
    done = [r for r in rounds if r["stats"]]
    final_cells = done[-1]["stats"]["n_out"] if done else None
    # the h5ad a reader should take: release/final.h5ad once released, else
    # the latest finished round's survivors (still moving while the loop runs)
    output_h5ad, output_note = None, ""
    if released and (rel / "final.h5ad").is_file():
        output_h5ad, output_note = rel / "final.h5ad", "final"
    else:
        with_h5ad = [r for r in done if (L.zoomin_dir(r["dir"]) / "annotated_zmip.h5ad").is_file()]
        if with_h5ad:
            output_h5ad = L.zoomin_dir(with_h5ad[-1]["dir"]) / "annotated_zmip.h5ad"
            output_note = f"latest survivors, round {with_h5ad[-1]['n']} — not final, the loop is still running"
    dec_rows = {}
    if rounds:
        dec = L.crosssample_dir(rounds[0]["dir"]) / "sample_decisions.csv"
        if dec.is_file():
            with open(dec) as f:
                dec_rows = {r["sample"]: r for r in csv.DictReader(f)}
    finished = next((ts for ts, ev in reversed(log) if ev.startswith("release ")), None) if released else None
    return {"name": unit.name, "dir": unit, "n_input": im.get("n_cells"), "species": im.get("species") or ps["species"], "finished": finished,
            "persample": ps, "rounds": rounds, "released": released, "stage": stage, "stage_class": cls,
            "last_event": f"{last[0]} {last[1]}" if last else "", "final_cells": final_cells,
            "output_h5ad": output_h5ad, "output_note": output_note,
            "sample_decisions": dec_rows, "forced": _forced(rounds),
            "events": log_events(log, unit, im.get("n_cells"))}


def _forced(rounds: list[dict]) -> bool:
    return bool(rounds) and bool(rounds[-1]["stats"]) and str(rounds[-1]["stats"].get("reason", "")).startswith("FORCED")


def _unit_body(unit: Path, s: dict, base: str = "") -> str:
    """Sections of a unit page. `base` prefixes every unit-relative link so the
    same body can sit inline on a one-unit root page ('units/<u>/')."""
    e = _h.escape
    rel = lambda p: base + e(str(p.relative_to(unit)))  # noqa: E731
    done_rounds = [r for r in s["rounds"] if r["stats"]]
    ps = s["persample"]
    items = review.collect(unit, [r["dir"] for r in done_rounds], [r["stats"] for r in done_rounds], s["forced"])
    umap_p = L.release_dir(unit) / "umap.json"
    umap_text = umap_p.read_text(encoding="utf-8") if umap_p.is_file() else None
    n_labels: dict[str, int] = {}
    if umap_text:
        try:
            n_labels = {k: len(v["labels"]) for k, v in json.loads(umap_text)["layers"].items()}
        except (ValueError, TypeError, KeyError, AttributeError):
            pass

    # at a glance
    n_in, n_fin = s["n_input"], s["final_cells"]
    removed_frac = (1 - n_fin / n_in) if n_in and n_fin is not None else None
    total_s = sum((r["stats"].get("elapsed_s") or 0) for r in done_rounds)
    running_round = bool(s["rounds"]) and not s["rounds"][-1]["stats"]
    n_excl = sum(1 for d in s["sample_decisions"].values() if d["decision"] == "exclude")
    glance = [
        _stat(_n(n_in) or "–", "input cells", e(str(s["species"] or ""))),
        _stat(_n(n_fin) or "–", "final cells" if s["released"] else "cells now",
              "" if s["released"] or n_fin is None else "after the last finished round"),
        _stat(_pct(removed_frac) if removed_frac is not None else "–", "removed overall",
              "QC, excluded samples and rounds", "tone-bad" if removed_frac and removed_frac > 0.3 else ""),
        _stat(str(len(done_rounds)) + (" <small>+1 running</small>" if running_round else ""), "rounds",
              f"{fmt_elapsed(total_s)} wall time" if total_s else ""),
        _stat(f'{n_labels.get("coarse", "–")} / {n_labels.get("fine", "–")}', "coarse / fine labels",
              "in the release" if n_labels else "known at release"),
        _stat(f'{ps["n_done"]}/{ps["n"]}' if ps["n"] else "–", "samples done", f"{n_excl} excluded" if n_excl else ""),
        _stat(str(len(items)), "needs review", "items, see below" if items else "nothing so far",
              "tone-warn" if items else ""),
    ]
    parts = ['<div class="glance">' + "".join(glance) + "</div>"]

    jumps = [("files", "Files"), ("rounds", "Rounds"), ("samples", "Samples"), ("sankey", "Cell identity"),
             ("umap", "Final UMAP"), ("review", "Needs review")]
    parts.append('<nav class="jump" aria-label="sections">' + "".join(f'<a href="#{k}">{t}</a>' for k, t in jumps) + "</nav>")

    # files
    files = []
    if s["output_h5ad"] is not None:
        out = s["output_h5ad"]
        files.append(("h5ad", f'<code class="path">{e(str(out))}</code><span class="muted">{e(s["output_note"])}</span> · '
                              f'<a href="{rel(out)}">download</a>'))
    rd = L.release_dir(unit)
    if s["released"]:
        files.append(("release dir", f'<code class="path">{e(str(rd))}</code>'
                      + (' <span class="pill failed">forced at the safety cap</span>' if s["forced"] else "")))
        for fn, what in (("summary.md", "what happened, round by round"), ("needs_review.md", "the review items below, as text"),
                         ("needs_review.json", "same, machine-readable"), ("cell_ledger.csv", "one row per input cell: status and labels per stage"),
                         ("umap.json", "final embedding and labels behind the UMAP panels")):
            if (rd / fn).is_file():
                files.append((fn, f'<a href="{base}{L.RELEASE}/{fn}">{fn}</a> <span class="muted">{what}</span>'))
    parts.append(f'<section class="block" id="files"><h2>Files</h2><p class="lede">{EXPLAIN["files"]}</p>'
                 + ('<dl class="files">' + "".join(f"<dt>{e(k)}</dt><dd>{v}</dd>" for k, v in files) + "</dl>"
                    if files else '<p class="empty">No output yet: the first round has not finished.</p>') + "</section>")

    # rounds
    rows = []
    for r in s["rounds"]:
        rp = rel(r["dir"])
        links = " · ".join(x for x in [
            f'<a href="{rp}/{L.CROSSSAMPLE}/report.html">msp</a>' if r["msp_report"] else "",
            f'<a href="{rp}/{L.ZOOMIN}/report.html">zmip</a>' if r["zmip_report"] else "",
            f'<a href="{rp}/{L.LEDGER}/sankey_coarse.png">sankey</a>' if r["sankey"] else ""] if x)
        st = r["stats"]
        if st:
            dec = r["decision"]
            pill = "failed" if str(st.get("reason", "")).startswith("FORCED") else "released" if dec == "release" else "neutral"
            rows.append(f'<tr><td class="num">{r["n"]}</td><td class="num">{_n(st["n_in"])}</td><td class="num">{_n(st["n_out"])}</td>'
                        f'<td class="num">{_n(st["removed"])}</td><td class="num">{_pct(st["frac"])}{_bar(st["frac"])}</td>'
                        f'<td><span class="pill {pill}">{e(str(dec))}</span></td><td class="reason">{e(str(st.get("reason", "")))}</td>'
                        f'<td class="num">{fmt_elapsed(st.get("elapsed_s"))}</td><td>{links}</td></tr>')
        else:
            rows.append(f'<tr><td class="num">{r["n"]}</td><td class="num">{_n(r.get("n_in"))}</td><td></td><td></td><td></td>'
                        f'<td><span class="pill running">running</span></td><td class="reason st running">{e(str(r["step"]))}</td>'
                        f'<td></td><td>{links}</td></tr>')
    parts.append(f'<section class="block" id="rounds"><h2>Rounds <span class="count">{len(done_rounds)} finished'
                 + (", 1 running" if running_round else "") + f'</span></h2><p class="lede">{EXPLAIN["rounds"]}</p>'
                 + ('<div class="wrap"><table><thead><tr><th class="r">round</th><th class="r">cells in</th><th class="r">cells out</th>'
                    '<th class="r">removed</th><th class="r">removed %</th><th>decision</th><th>reason</th><th class="r">wall time</th><th>reports</th></tr></thead>'
                    f'<tbody>{"".join(rows)}</tbody></table></div>' if rows else '<p class="empty">No round started yet.</p>') + "</section>")

    # per-sample
    prow = []
    for smp in ps["samples"]:
        d = s["sample_decisions"].get(smp["name"]) or s["sample_decisions"].get(smp["value"]) or {}
        link = (f'<a href="{rel(smp["dir"])}/report.html">osp report</a>' if smp["report"]
                else ('<span class="st running">running</span>' if not smp["done"] else ""))
        dec = d.get("decision", "")
        dpill = f'<span class="pill {e(dec)}">{e(dec)}</span>' if dec else '<span class="muted">–</span>'
        if dec == "exclude" and d.get("reason"):  # reason folded behind a red "?" — click opens, click again closes (CSS-only <details>)
            dpill += (f'<details class="why"><summary title="why excluded?" aria-label="why excluded?">?</summary>'
                      f'<div class="why-body"><b>{e(smp["name"])} excluded:</b> {e(d["reason"])}</div></details>')
        status_pill = ('<span class="pill empty-sample" title="OSP QC removed every cell; see qc_removed.csv">empty</span>'
                       if smp.get("empty") else '<span class="pill released">done</span>' if smp["done"]
                       else '<span class="pill running">pending</span>')
        prow.append(f'<tr><td>{e(smp["name"])}</td><td class="num">{_n(smp["n_cells"])}</td>'
                    f'<td>{status_pill}</td><td class="why-cell">{dpill}</td><td>{link}</td></tr>')
    meta = [f'{ps["n_done"]}/{ps["n"]} done']
    if ps["sample_column"]:
        meta.append(f'sample column <code>{e(str(ps["sample_column"]))}</code>')
    if ps.get("n_excluded"):
        meta.append(f'{ps["n_excluded"]:,} cells excluded before OSP by sample-map policy '
                    f'(<a href="{base}{L.PERSAMPLE}/{L.EXCLUDED_CELLS}">excluded_cells.csv</a>)')
    parts.append(f'<section class="block" id="samples"><h2>Samples <span class="count">{" · ".join(meta)}</span></h2>'
                 f'<p class="lede">{EXPLAIN["samples"]}</p>'
                 + ('<div class="wrap"><table><thead><tr><th>sample</th><th class="r">input cells</th><th>osp</th><th>integration</th>'
                    f'<th>report</th></tr></thead><tbody>{"".join(prow)}</tbody></table></div>'
                    if prow else '<p class="empty">Per-sample processing has not started.</p>') + "</section>")

    # sankey + ledger
    last_done = [r for r in s["rounds"] if r["sankey"]]
    if last_done:
        ldir = L.ledger_dir(last_done[-1]["dir"])
        ld = rel(ldir)
        data_p = ldir / "sankey_coarse.json"
        if data_p.is_file():
            data = data_p.read_text().replace("</", "<\\/")
            fig = (f'<div id="sankey-vis" class="wrap"></div><script>const SANKEY_DATA = {data};{SANKEY_JS}</script>'
                   f'<figcaption>Every label is shown, nothing pooled. Static version: <a href="{ld}/sankey_coarse.png">sankey_coarse.png</a>'
                   f' · <a href="{ld}/cell_ledger.csv">cell_ledger.csv</a> has one row per input cell with its status and labels per stage.</figcaption>')
        else:
            fig = (f'<figure><a href="{ld}/sankey_coarse.png"><img src="{ld}/sankey_coarse.png" alt="Sankey diagram of coarse cell labels across steps and rounds"></a>'
                   f'<figcaption><a href="{ld}/cell_ledger.csv">cell_ledger.csv</a> has one row per input cell with its status and labels per stage.</figcaption></figure>')
        parts.append(f'<section class="block" id="sankey"><h2>Cell identity across steps and rounds <span class="count">coarse labels · through round {last_done[-1]["n"]}</span></h2>'
                     f'<p class="lede">{EXPLAIN["sankey"]}</p>{fig}</section>')

    # final UMAP (interactive) — data extracted at release into release/umap.json
    if umap_text is not None:
        # Script elements are raw text even with application/json. Escape '<'
        # so labels and cell IDs cannot terminate the element with </script>.
        data = umap_text.replace("<", "\\u003c")
        parts.append(f'<section class="block" id="umap"><h2>Final UMAP <span class="count">every released cell · coarse and fine labels</span></h2>'
                     f'<p class="lede">{EXPLAIN["umap"]}</p>'
                     f'<div id="umap-vis"><p class="umap-status">loading {base}{e(str(umap_p.relative_to(unit)))}… (JavaScript required)</p><div class="umap-row"></div></div>'
                     f'<script type="application/json" id="umap-data">{data}</script>'
                     f'<script>{UMAP_JS}</script></section>')

    # needs review — from disk, so it exists mid-run too
    cs = review.counts(items)
    brief = " · ".join(f"{n} {t.lower()}" for _, t, n, _ in cs) if cs else "nothing to review"
    parts.append(f'<section class="block" id="review"><h2>Needs review <span class="count">{len(items)} items — {e(brief)}</span></h2>'
                 f'<p class="lede">{EXPLAIN["review"]}' + ("" if s["released"] else " The loop is still running, so this list is still growing.")
                 + "</p>" + review.to_html(items, base) + "</section>")
    return "".join(parts)

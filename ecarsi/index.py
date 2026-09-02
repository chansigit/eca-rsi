"""ecarsi.index — landing pages generated from the artefacts on disk.

    python -m ecarsi.index <root | unit>       (re)write the static pages

Nothing here is told what happened: the state of a run is read back from
manifests, contract files, stats/decision files and progress.log, so the
same function renders a finished release and a run that is halfway through
round 2 (ecarsi.serve re-renders on every request, which is what makes
mid-run monitoring possible). Every step also writes the static pages when
it finishes, so a directory that is only copied around still has them.

    <root>/index.html         one row per unit: stage, cells, rounds, links
    <root>/units/<u>/index.html   per-sample reports, rounds table, sankey, needs-review
"""

from __future__ import annotations

import csv
import html as _h
import json
import sys
from pathlib import Path

from . import layout as L
from . import review

CSS = """
:root{--bg:#f4f5f7;--card:#fff;--ink:#1f2328;--muted:#656d76;--line:#e6e8eb;--accent:#3b5bdb;
 --ok:#2f9e44;--ok-bg:#e9f7ec;--warn:#d9480f;--warn-bg:#fff1e6;--bad:#c92a2a;--bad-bg:#fdecec;--gray-bg:#f1f3f5}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,Helvetica,Arial,sans-serif}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
code{font:.88em ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;background:var(--gray-bg);padding:.1em .35em;border-radius:4px}
.page{max-width:1380px;margin:0 auto;padding:1.5rem 1.5rem 4rem}
header.top{display:flex;align-items:flex-end;justify-content:space-between;flex-wrap:wrap;gap:.8rem;margin:.5rem 0 1.2rem}
header.top h1{margin:0;font-size:1.9rem;font-weight:650;letter-spacing:-.01em}
.crumb{color:var(--muted);font-size:.9rem;margin-bottom:.35rem}.crumb a{color:var(--muted)}
.event{color:var(--muted);font-size:.85rem}
.pill{display:inline-block;padding:.22em .75em;border-radius:999px;font-size:.85rem;font-weight:600;white-space:nowrap;vertical-align:middle}
.pill.running{background:var(--warn-bg);color:var(--warn)}.pill.released{background:var(--ok-bg);color:var(--ok)}
.pill.failed{background:var(--bad-bg);color:var(--bad)}.pill.neutral{background:var(--gray-bg);color:var(--muted)}
.pill.include{background:var(--ok-bg);color:var(--ok)}.pill.exclude{background:var(--bad-bg);color:var(--bad)}
.cards{display:flex;flex-wrap:wrap;gap:.8rem;margin:0 0 1.2rem}
.card{flex:1 1 150px;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:.8rem 1rem;box-shadow:0 1px 2px rgba(0,0,0,.04)}
.card .num{display:block;font-size:1.6rem;font-weight:650;line-height:1.15;letter-spacing:-.01em}
.card .lbl{display:block;color:var(--muted);font-size:.82rem;margin-top:.15rem}
.card .sub{display:block;color:var(--muted);font-size:.78rem}
a.card{color:var(--ink)}a.card:hover{text-decoration:none;border-color:var(--accent)}
.review-cards .card{flex:1 1 170px}
.callout{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--accent);border-radius:8px;padding:.7rem 1rem;margin:0 0 1.2rem;font-size:.92rem}
.callout .path{font:.86rem ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;word-break:break-all;background:none;padding:0}
.callout.warn{border-left-color:var(--warn)}.callout.bad{border-left-color:var(--bad)}
nav.jump{display:flex;gap:1.2rem;flex-wrap:wrap;font-size:.9rem;margin:0 0 1.2rem;padding:.55rem 0;border-top:1px solid var(--line);border-bottom:1px solid var(--line)}
section{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:1.1rem 1.4rem 1.3rem;margin:0 0 1.2rem;box-shadow:0 1px 2px rgba(0,0,0,.04)}
section h2{margin:0 0 .6rem;font-size:1.2rem;font-weight:650;display:flex;align-items:baseline;gap:.6rem;flex-wrap:wrap}
section h2 small{font-weight:400;color:var(--muted);font-size:.85rem}
h3{margin:1.6rem 0 .3rem;font-size:1.02rem;font-weight:650;display:flex;align-items:center;gap:.5rem}
h3 .count{background:var(--gray-bg);color:var(--muted);border-radius:999px;padding:.05em .6em;font-size:.8rem;font-weight:600}
h3.kind-convergence .count,h3.kind-removed .count{background:var(--bad-bg);color:var(--bad)}
h3.kind-sample_excluded .count,h3.kind-reassigned .count{background:var(--warn-bg);color:var(--warn)}
.card.kind-convergence .num,.card.kind-removed .num{color:var(--bad)}
.card.kind-sample_excluded .num,.card.kind-reassigned .num{color:var(--warn)}
p.desc{color:var(--muted);font-size:.88rem;margin:0 0 .5rem;max-width:90ch}
.wrap{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:.9rem}
th,td{padding:.45rem .65rem;border-bottom:1px solid var(--line);text-align:right;vertical-align:top}
th{color:var(--muted);font-weight:600;font-size:.78rem;text-transform:uppercase;letter-spacing:.03em;text-align:right;border-bottom:2px solid var(--line)}
th:first-child,td:first-child,th.l,td.l,td.note,td.reason{text-align:left}
tbody tr:hover{background:#fafbfc}
td.num{font-variant-numeric:tabular-nums}
td.note,td.reason{font-size:.85rem;color:#3a3f45;min-width:28ch;max-width:70ch}
table.review th,table.review td{text-align:left}table.review td.c-cells{text-align:right;font-variant-numeric:tabular-nums}
table.review td.c-label{max-width:34ch}
.badge{display:inline-block;padding:.08em .55em;border-radius:999px;font-size:.78rem;font-weight:600}
.conf-high{background:var(--ok-bg);color:var(--ok)}.conf-medium{background:var(--gray-bg);color:var(--muted)}.conf-low{background:var(--warn-bg);color:var(--warn)}
.act-remove{color:var(--bad);font-weight:600}.act{color:var(--ink)}
.bar{display:inline-block;vertical-align:middle;width:70px;height:7px;background:var(--gray-bg);border-radius:4px;margin-left:.5rem;overflow:hidden}
.bar i{display:block;height:100%;background:var(--bad);opacity:.75}
.muted{color:var(--muted)}.running-cell{color:var(--warn);font-weight:600}
figure{margin:0}figure img{max-width:100%;border:1px solid var(--line);border-radius:8px;background:#fff}
figcaption{color:var(--muted);font-size:.85rem;margin-top:.4rem}
p.empty{color:var(--muted)}
footer{color:var(--muted);font-size:.8rem;margin-top:2rem}
ul.warn{margin:.3rem 0 0 1.2rem;padding:0}
@media (max-width:700px){.page{padding:1rem}section{padding:.9rem 1rem}}
"""


def _json(p: Path, default=None):
    if not p.is_file():
        return default
    with open(p) as f:
        return json.load(f)


def _n_obs(h5ad: Path) -> int | None:
    try:
        import h5py

        with h5py.File(h5ad, "r") as f:
            return int(f["obs"][f["obs"].attrs["_index"]].shape[0])
    except Exception:
        return None


def fmt_elapsed(sec) -> str:
    if sec is None or sec != sec:
        return "n/a"
    sec = int(sec)
    return f"{sec // 3600}h{(sec % 3600) // 60:02d}m" if sec >= 3600 else f"{sec // 60}m{sec % 60:02d}s"


def read_stats(path: Path) -> dict:
    text = path.read_text().strip()
    if text.startswith("{"):
        return json.loads(text)
    st = dict(tok.split("=", 1) for tok in text.split())
    return {k: (float(v) if k in ("frac", "elapsed_s") else v if k == "decision" else int(v)) for k, v in st.items()}


# ---------------------------------------------------------------- unit state

def persample_state(unit: Path) -> dict:
    man = _json(L.persample_manifest(unit), {})
    samples = []
    for s in man.get("samples", []):
        d = L.sample_dir(unit, s)
        contract = L.PS_ANNOTATE_CONTRACT if man.get("annotate", True) else L.PS_CONTRACT
        samples.append({"name": d.name, "value": s["value"], "n_cells": s["n_cells"], "dir": d,
                        "done": L.complete(d, contract), "report": (d / "report.html").is_file()})
    return {"manifest": bool(man), "sample_column": man.get("sample_column"), "species": man.get("species"),
            "samples": samples, "n_done": sum(s["done"] for s in samples), "n": len(samples),
            "done": bool(samples) and all(s["done"] for s in samples)}


def _round_step(rdir: Path) -> str:
    """What a round without a decision is currently doing, from its contracts."""
    cdir, zdir = L.crosssample_dir(rdir), L.zoomin_dir(rdir)
    if not L.complete(cdir, L.MSP_CONTRACT):
        if not (cdir / "integrated.h5ad").is_file():
            return "crosssample · integrate" if (cdir.is_dir() or (rdir / L.ROUND_INPUT).is_file()) else "starting"
        if not (cdir / "inspection_proposal.json").is_file():
            return "crosssample · inspect"
        return "crosssample · annotate"
    if not L.complete(zdir, L.ZMIP_CONTRACT):
        plan = _json(zdir / "zmip_plan.json")
        if not plan:
            return "zoomin · plan"
        zoomed = [ln["name"] for ln in plan["lineages"] if ln["zoom"]]
        done = [n for n in zoomed if L.complete(L.lineage_dir(zdir, n), L.ZMIP_LINEAGE_CONTRACT)]
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
            if (cdir / "integrated.h5ad").is_file():
                r["n_in"] = _n_obs(cdir / "integrated.h5ad")
        out.append(r)
    return out


def unit_state(unit: Path) -> dict:
    """Everything the pages need, read from disk."""
    im = _json(L.input_manifest(unit), {})
    ps = persample_state(unit)
    rounds = rounds_state(unit)
    rel = L.release_dir(unit)
    released = (rel / "summary.md").is_file()
    log = L.read_log(unit)
    last = log[-1] if log else None
    failed = bool(last) and "failed" in last[1]
    if failed:
        stage, cls = f"failed — {last[1]}", "failed"
    elif released and (not rounds or rounds[-1]["decision"] is not None):
        stage, cls = f"released after {len(rounds)} round(s)", "released"
    elif rounds and rounds[-1]["decision"] is None:
        stage, cls = f"round {rounds[-1]['n']} · {rounds[-1]['step']}", "running"
    elif rounds:
        stage, cls = f"round {rounds[-1]['n']} done, next round pending", "running"
    elif ps["manifest"] and not ps["done"]:
        stage, cls = f"persample {ps['n_done']}/{ps['n']} samples", "running"
    elif ps["done"]:
        stage, cls = "persample done, loop not started", "running"
    else:
        stage, cls = "organized, persample not started", "running"
    final_cells = None
    if rounds and rounds[-1]["stats"]:
        final_cells = rounds[-1]["stats"]["n_out"]
    # the h5ad a reader should take: release/final.h5ad once released, else
    # the latest finished round's survivors (still moving while the loop runs)
    output_h5ad, output_note = None, ""
    if released and (rel / "final.h5ad").is_file():
        output_h5ad, output_note = rel / "final.h5ad", "final"
    else:
        done = [r for r in rounds if r["stats"] and (L.zoomin_dir(r["dir"]) / "annotated_zmip.h5ad").is_file()]
        if done:
            output_h5ad = L.zoomin_dir(done[-1]["dir"]) / "annotated_zmip.h5ad"
            output_note = f"latest survivors, round {done[-1]['n']} — not final, the loop is still running"
    dec_rows = {}
    if rounds:
        dec = L.crosssample_dir(rounds[0]["dir"]) / "sample_decisions.csv"
        if dec.is_file():
            with open(dec) as f:
                dec_rows = {r["sample"]: r for r in csv.DictReader(f)}
    return {"name": unit.name, "dir": unit, "n_input": im.get("n_cells"), "species": im.get("species") or ps["species"],
            "persample": ps, "rounds": rounds, "released": released, "stage": stage, "stage_class": cls,
            "last_event": f"{last[0]} {last[1]}" if last else "", "final_cells": final_cells,
            "output_h5ad": output_h5ad, "output_note": output_note,
            "sample_decisions": dec_rows, "forced": _forced(rounds)}


def _forced(rounds: list[dict]) -> bool:
    return bool(rounds) and bool(rounds[-1]["stats"]) and str(rounds[-1]["stats"].get("reason", "")).startswith("FORCED")


# ---------------------------------------------------------------- unit page

def _pct(x) -> str:
    return f"{100 * x:.2f}%"


def _n(x) -> str:
    return "" if x is None or x == "" else f"{int(x):,}"


def _bar(frac: float) -> str:
    return f'<span class="bar" title="{_pct(frac)}"><i style="width:{min(100, 100 * frac):.1f}%"></i></span>'


def _card(num, label, sub="", cls="") -> str:
    return (f'<div class="card {cls}"><span class="num">{num}</span><span class="lbl">{_h.escape(label)}</span>'
            + (f'<span class="sub">{sub}</span>' if sub else "") + "</div>")


def render_unit(unit: Path) -> str:
    s = unit_state(unit)
    e = _h.escape
    root = L.root_of(unit)
    crumb = (f'<div class="crumb"><a href="../../{L.INDEX}">{e(root.name)}</a> / {L.UNITS} / {e(s["name"])}</div>'
             if root else "")
    header = (f'<header class="top"><div>{crumb}<h1>{e(s["name"])} <span class="pill {s["stage_class"]}">{e(s["stage"])}</span></h1></div>'
              + (f'<div class="event">last event · {e(s["last_event"])}</div>' if s["last_event"] else "") + "</header>")

    done_rounds = [r for r in s["rounds"] if r["stats"]]
    total_s = sum((r["stats"].get("elapsed_s") or 0) for r in done_rounds)
    removed_total = sum(r["stats"]["removed"] for r in done_rounds)
    cards = [_card(_n(s["n_input"]), "input cells", e(str(s["species"] or ""))),
             _card(_n(s["final_cells"]) or "–", "final cells" if s["released"] else "cells now",
                   f"−{_n(removed_total)} removed in rounds" if done_rounds else ""),
             _card(f'{s["persample"]["n_done"]}/{s["persample"]["n"]}' if s["persample"]["n"] else "–", "samples (osp)",
                   f'{sum(1 for d in s["sample_decisions"].values() if d["decision"] == "exclude")} excluded'
                   if s["sample_decisions"] else ""),
             _card(str(len(done_rounds)) + ("" if s["released"] else " <small>+1 running</small>" if s["rounds"] and not s["rounds"][-1]["stats"] else ""),
                   "rounds done", f"{fmt_elapsed(total_s)} wall time" if total_s else "")]
    parts = [header, '<div class="cards">' + "".join(cards) + "</div>"]

    if s["output_h5ad"] is not None:
        out = s["output_h5ad"]
        cls = "" if s["released"] else "warn"
        parts.append(f'<div class="callout {cls}"><b>Output h5ad</b> <span class="muted">({e(s["output_note"])})</span><br>'
                     f'<code class="path">{e(str(out))}</code> &nbsp;<a href="{e(str(out.relative_to(unit)))}">download</a>'
                     f'<br><span class="muted">labels: <code>zmip_ann_coarse</code> / <code>zmip_ann_fine</code></span></div>')
    if s["released"]:
        parts.append('<div class="callout"><b>Release</b>' + (' <span class="pill failed">forced at the safety cap</span>' if s["forced"] else "")
                     + f'<br><code class="path">{e(str(L.release_dir(unit)))}</code><br>'
                     f'<a href="{L.RELEASE}/summary.md">summary.md</a> · <a href="{L.RELEASE}/needs_review.md">needs_review.md</a> · '
                     f'<a href="{L.RELEASE}/needs_review.json">needs_review.json</a> · <a href="{L.RELEASE}/cell_ledger.csv">cell_ledger.csv</a></div>')

    jumps = ['<a href="#rounds">Rounds</a>', '<a href="#samples">Samples</a>', '<a href="#sankey">Cell identity</a>',
             '<a href="#review">Needs review</a>']
    parts.append('<nav class="jump">' + "".join(jumps) + "</nav>")

    # rounds
    rows = []
    for r in s["rounds"]:
        rp = e(str(r["dir"].relative_to(unit)))
        links = " · ".join(x for x in [
            f'<a href="{rp}/{L.CROSSSAMPLE}/report.html">msp</a>' if r["msp_report"] else "",
            f'<a href="{rp}/{L.ZOOMIN}/report.html">zmip</a>' if r["zmip_report"] else "",
            f'<a href="{rp}/{L.LEDGER}/sankey_coarse.png">sankey</a>' if r["sankey"] else ""] if x)
        st = r["stats"]
        if st:
            dec = r["decision"]
            pill = "failed" if str(st.get("reason", "")).startswith("FORCED") else "released" if dec == "release" else "neutral"
            rows.append(f'<tr><td>{r["n"]}</td><td class="num">{_n(st["n_in"])}</td><td class="num">{_n(st["n_out"])}</td>'
                        f'<td class="num">{_n(st["removed"])}</td><td class="num">{_pct(st["frac"])}{_bar(st["frac"])}</td>'
                        f'<td class="l"><span class="pill {pill}">{e(str(dec))}</span></td><td class="reason">{e(str(st.get("reason", "")))}</td>'
                        f'<td class="num">{fmt_elapsed(st.get("elapsed_s"))}</td><td class="l">{links}</td></tr>')
        else:
            rows.append(f'<tr><td>{r["n"]}</td><td class="num">{_n(r.get("n_in"))}</td><td></td><td></td><td></td>'
                        f'<td class="l"><span class="pill running">running</span></td><td class="reason running-cell">{e(str(r["step"]))}</td>'
                        f'<td></td><td class="l">{links}</td></tr>')
    parts.append('<section id="rounds"><h2>Rounds <small>crosssample (msp) → zoomin (zmip), on the survivors each time</small></h2>'
                 + ('<div class="wrap"><table><thead><tr><th>round</th><th>cells in</th><th>cells out</th><th>removed</th>'
                    '<th>removed %</th><th class="l">decision</th><th class="l">reason</th><th>wall time</th><th class="l">reports</th></tr></thead>'
                    f'<tbody>{"".join(rows)}</tbody></table></div>' if rows else '<p class="empty">no round started yet</p>') + "</section>")

    # per-sample
    ps = s["persample"]
    prow = []
    for smp in ps["samples"]:
        d = s["sample_decisions"].get(smp["name"]) or s["sample_decisions"].get(smp["value"]) or {}
        link = (f'<a href="{e(str(smp["dir"].relative_to(unit)))}/report.html">osp report</a>' if smp["report"]
                else ('<span class="running-cell">running</span>' if not smp["done"] else ""))
        dec = d.get("decision", "")
        dpill = f'<span class="pill {dec}">{e(dec)}</span>' if dec else '<span class="muted">–</span>'
        reason = e(d.get("reason", "")) if dec == "exclude" else ""
        prow.append(f'<tr><td>{e(smp["name"])}</td><td class="num">{_n(smp["n_cells"])}</td>'
                    f'<td class="l">{"<span class=\"pill released\">done</span>" if smp["done"] else "<span class=\"pill running\">pending</span>"}</td>'
                    f'<td class="l">{dpill}</td><td class="l">{link}</td><td class="reason">{reason}</td></tr>')
    parts.append(f'<section id="samples"><h2>Samples <small>osp runs once per sample · {ps["n_done"]}/{ps["n"]} done'
                 + (f' · sample column <code>{e(str(ps["sample_column"]))}</code>' if ps["sample_column"] else "") + "</small></h2>"
                 + ('<div class="wrap"><table><thead><tr><th>sample</th><th>input cells</th><th class="l">osp</th><th class="l">integration</th>'
                    f'<th class="l">report</th><th class="l">exclusion reason</th></tr></thead><tbody>{"".join(prow)}</tbody></table></div>'
                    if prow else '<p class="empty">persample has not started</p>') + "</section>")

    # sankey + ledger
    last_done = [r for r in s["rounds"] if r["sankey"]]
    if last_done:
        ld = e(str(L.ledger_dir(last_done[-1]["dir"]).relative_to(unit)))
        parts.append(f'<section id="sankey"><h2>Cell identity across steps and rounds <small>coarse labels · through round {last_done[-1]["n"]}</small></h2>'
                     f'<figure><a href="{ld}/sankey_coarse.png"><img src="{ld}/sankey_coarse.png" alt="Sankey"></a>'
                     f'<figcaption>Every input cell flows left to right; cells removed at a stage end in that stage\'s red sink. '
                     f'<a href="{ld}/cell_ledger.csv">cell_ledger.csv</a> — one row per input cell, status + labels per stage.</figcaption></figure></section>')

    # needs review — from disk, so it exists mid-run too
    items = review.collect(unit, [r["dir"] for r in done_rounds], [r["stats"] for r in done_rounds], s["forced"])
    parts.append('<section id="review"><h2>Needs review <small>'
                 + ("everything the agents were unsure about or the host overrode; nothing here stopped the loop"
                    if s["released"] else "so far — the loop is still running") + "</small></h2>" + review.to_html(items) + "</section>")
    return _page(s["name"], "".join(parts))


# ---------------------------------------------------------------- root page

def render_root(root: Path) -> str:
    e = _h.escape
    om = _json(L.organize_manifest(root), {})
    units = L.units(root)
    states = [unit_state(u) for u in units]
    rows = []
    for u, s in zip(units, states):
        rows.append(f'<tr><td><a href="{L.UNITS}/{e(u.name)}/{L.INDEX}"><b>{e(u.name)}</b></a></td>'
                    f'<td class="l">{e(str(s["species"] or ""))}</td><td class="num">{_n(s["n_input"])}</td>'
                    f'<td class="num">{s["persample"]["n_done"]}/{s["persample"]["n"]}</td><td class="num">{len(s["rounds"])}</td>'
                    f'<td class="num">{_n(s["final_cells"])}</td>'
                    f'<td class="l"><span class="pill {s["stage_class"]}">{e(s["stage"])}</span></td>'
                    f'<td class="l muted">{e(s["last_event"])}</td></tr>')
    n_rel = sum(1 for s in states if s["released"])
    header = (f'<header class="top"><div><div class="crumb">ecarsi run</div><h1>{e(root.name)}</h1></div>'
              f'<div class="event">{n_rel}/{len(units)} unit(s) released</div></header>')
    cards = [_card(str(len(om.get("input_units", []))) if om else "–", "input units (eca-pp)",
                   e(", ".join(u["name"] for u in om.get("input_units", []))) if om else ""),
             _card(str(len(units)), "analysis units"),
             _card(_n(sum(s["n_input"] or 0 for s in states)) or "–", "input cells"),
             _card(_n(sum(s["final_cells"] or 0 for s in states if s["released"])) or "–", "released cells")]
    parts = [header, '<div class="cards">' + "".join(cards) + "</div>"]
    if om and om.get("warnings"):
        parts.append('<div class="callout warn"><b>organize warnings</b><ul class="warn">'
                     + "".join(f"<li>{e(w)}</li>" for w in om["warnings"]) + "</ul></div>")
    parts.append('<section><h2>Units <small>one analysis unit = one merged dataset, run independently</small></h2>'
                 + ('<div class="wrap"><table><thead><tr><th>unit</th><th class="l">species</th><th>input cells</th><th>samples</th>'
                    '<th>rounds</th><th>final cells</th><th class="l">stage</th><th class="l">last event</th></tr></thead>'
                    f'<tbody>{"".join(rows)}</tbody></table></div>' if rows else '<p class="empty">no units yet</p>') + "</section>")
    if om:
        parts.append(f'<p class="muted">organize plan and cell-conservation audit: <a href="{L.ORGANIZE}/{L.MANIFEST}">{L.ORGANIZE}/{L.MANIFEST}</a></p>')
    return _page(root.name, "".join(parts))


def _page(title: str, body: str) -> str:
    import time

    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    return (f'<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>{_h.escape(title)}</title><style>{CSS}</style></head><body><div class=\"page\">{body}"
            f'<footer>rendered {stamp} from the run directory by ecarsi.index · reload for the current state</footer></div></body></html>')


# ---------------------------------------------------------------- writers

def write_unit_index(unit: Path) -> Path:
    p = unit / L.INDEX
    if p.is_symlink():
        p.unlink()
    p.write_text(render_unit(unit))
    return p


def write_root_index(root: Path) -> Path:
    p = root / L.INDEX
    if p.is_symlink():
        p.unlink()
    p.write_text(render_root(root))
    return p


def write_all(target: Path) -> list[Path]:
    """Static pages for a unit (and its root, if it has one) or a whole root."""
    written = []
    if L.is_unit(target):
        written.append(write_unit_index(target))
        root = L.root_of(target)
        if root is not None:
            written.append(write_root_index(root))
    elif L.is_root(target):
        for u in L.units(target):
            written.append(write_unit_index(u))
        written.append(write_root_index(target))
    else:
        raise SystemExit(f"{target} is neither an organize root nor a unit dir")
    return written


def main(argv: list[str]) -> int:
    if len(argv) != 1 or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0 if argv and argv[0] in ("-h", "--help") else 2
    for p in write_all(Path(argv[0]).resolve()):
        print(f"[index] {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

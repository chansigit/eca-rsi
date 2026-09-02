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

CSS = """body{font-family:system-ui,sans-serif;max-width:1400px;margin:2rem auto;padding:0 1rem;color:#222}
table{border-collapse:collapse;margin:.5rem 0}td,th{border:1px solid #ddd;padding:.3rem .6rem;text-align:right;vertical-align:top}
th{background:#f4f4f4;text-align:center}td.l,td.note,td.reason{text-align:left}td.note{max-width:70ch;font-size:.85rem}
td.reason{max-width:60ch;font-size:.85rem}table.review td,table.review-summary td{text-align:left}
img{max-width:100%;border:1px solid #ddd}.stage{font-weight:600}.running{color:#b35c00}.released{color:#1a7f37}
.failed{color:#b3261e}p.desc{color:#555;font-size:.9rem;margin:.2rem 0 .4rem}small{color:#777}
code{background:#f4f4f4;padding:0 .2rem}pre{white-space:pre-wrap;background:#f7f7f7;padding:1rem;border-radius:6px;font-size:.85rem}
.meta{color:#555;font-size:.9rem}"""


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


def render_unit(unit: Path) -> str:
    s = unit_state(unit)
    e = _h.escape
    head =(f"<h1>{e(s['name'])}</h1><p class=\"meta\">species {e(str(s['species']))} · input cells {s['n_input']}"
            + (f" · final cells {s['final_cells']}" if s["final_cells"] is not None else "")
            + f" · <span class=\"stage {s['stage_class']}\">{e(s['stage'])}</span>"
            + (f"<br><small>last event: {e(s['last_event'])}</small>" if s["last_event"] else "") + "</p>")
    parts = [head]
    if s["output_h5ad"] is not None:
        out = s["output_h5ad"]
        parts.append(f'<p><b>Output h5ad</b> ({e(s["output_note"])}): <code>{e(str(out))}</code> '
                     f'<a href="{e(str(out.relative_to(unit)))}">download</a> · '
                     f"<code>zmip_ann_coarse</code> / <code>zmip_ann_fine</code> = labels</p>")
    if s["released"]:
        parts.append(f'<p><b>Release:</b> <code>{e(str(L.release_dir(unit)))}</code> · '
                     f'<a href="{L.RELEASE}/summary.md">summary.md</a> · <a href="{L.RELEASE}/needs_review.md">needs_review.md</a> · '
                     f'<a href="{L.RELEASE}/cell_ledger.csv">cell_ledger.csv</a>'
                     + (" · <b>forced at the safety cap</b>" if s["forced"] else "") + "</p>")

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
            rows.append(f"<tr><td>{r['n']}</td><td>{st['n_in']}</td><td>{st['n_out']}</td><td>{st['removed']}</td>"
                        f"<td>{_pct(st['frac'])}</td><td>{e(str(r['decision']))}</td><td class=\"l\">{e(str(st.get('reason', '')))}</td>"
                        f"<td>{fmt_elapsed(st.get('elapsed_s'))}</td><td class=\"l\">{links}</td></tr>")
        else:
            rows.append(f"<tr><td>{r['n']}</td><td>{r.get('n_in') or ''}</td><td></td><td></td><td></td>"
                        f"<td class=\"running\">running</td><td class=\"l running\">{e(str(r['step']))}</td><td></td><td class=\"l\">{links}</td></tr>")
    parts.append("<h2>Rounds</h2><table><tr><th>round</th><th>cells in</th><th>cells out</th><th>removed</th>"
                 "<th>removed %</th><th>decision</th><th>reason</th><th>wall time</th><th>reports</th></tr>"
                 + "".join(rows) + "</table>" if rows else "<h2>Rounds</h2><p>none started</p>")

    # per-sample
    ps = s["persample"]
    if ps["samples"]:
        prow = []
        for smp in ps["samples"]:
            d = s["sample_decisions"].get(smp["name"]) or s["sample_decisions"].get(smp["value"]) or {}
            link = (f'<a href="{e(str(smp["dir"].relative_to(unit)))}/report.html">osp report</a>' if smp["report"]
                    else ('<span class="running">running</span>' if not smp["done"] else ""))
            reason = e(d.get("reason", "")) if d.get("decision") == "exclude" else ""
            prow.append(f"<tr><td class=\"l\">{e(smp['name'])}</td><td>{smp['n_cells']}</td><td>{'done' if smp['done'] else 'pending'}</td>"
                        f"<td>{e(d.get('decision', ''))}</td><td class=\"l\">{link}</td><td class=\"reason\">{reason}</td></tr>")
        parts.append(f"<h2>Per-sample (osp, run once) — {ps['n_done']}/{ps['n']} done"
                     f"{', sample column <code>' + e(str(ps['sample_column'])) + '</code>' if ps['sample_column'] else ''}</h2>"
                     "<table><tr><th>sample</th><th>input cells</th><th>osp</th><th>integration</th><th>report</th><th>exclusion reason</th></tr>"
                     + "".join(prow) + "</table>")

    # sankey + ledger
    last_done = [r for r in s["rounds"] if r["sankey"]]
    if last_done:
        ld = L.ledger_dir(last_done[-1]["dir"])
        parts.append(f"<h2>Cell identity across steps and rounds</h2><img src=\"{e(str(ld.relative_to(unit)))}/sankey_coarse.png\">"
                     f'<p><a href="{e(str(ld.relative_to(unit)))}/cell_ledger.csv">cell_ledger.csv</a> — one row per input cell, '
                     "status + labels per stage</p>")

    # needs review — from disk, so it exists mid-run too
    done_rounds = [r for r in s["rounds"] if r["stats"]]
    items = review.collect(unit, [r["dir"] for r in done_rounds], [r["stats"] for r in done_rounds], s["forced"])
    parts.append("<h2>Needs review" + ("" if s["released"] else " <small>(so far — the loop is still running)</small>") + "</h2>"
                 + review.to_html(items))
    return _page(s["name"], "".join(parts), up=("../../index.html" if L.root_of(unit) else None))


# ---------------------------------------------------------------- root page

def render_root(root: Path) -> str:
    e = _h.escape
    om = _json(L.organize_manifest(root), {})
    rows = []
    for u in L.units(root):
        s = unit_state(u)
        rows.append(f'<tr><td class="l"><a href="{L.UNITS}/{e(u.name)}/index.html">{e(u.name)}</a></td>'
                    f"<td>{e(str(s['species']))}</td><td>{s['n_input'] or ''}</td>"
                    f"<td>{s['persample']['n_done']}/{s['persample']['n']}</td><td>{len(s['rounds'])}</td>"
                    f"<td>{'' if s['final_cells'] is None else s['final_cells']}</td>"
                    f"<td class=\"l stage {s['stage_class']}\">{e(s['stage'])}</td><td class=\"l\"><small>{e(s['last_event'])}</small></td></tr>")
    parts = [f"<h1>{e(root.name)}</h1>"]
    if om:
        srcs = ", ".join(f"{e(u['name'])}" for u in om.get("input_units", []))
        parts.append(f'<p class="meta">organize: {len(om.get("input_units", []))} input unit(s) [{srcs}] → '
                     f"{len(om.get('units_written', []))} analysis unit(s) · <a href=\"{L.ORGANIZE}/{L.MANIFEST}\">manifest.json</a></p>")
        if om.get("warnings"):
            parts.append("<p class=\"failed\">organize warnings:</p><ul>" + "".join(f"<li>{e(w)}</li>" for w in om["warnings"]) + "</ul>")
    parts.append("<table><tr><th>unit</th><th>species</th><th>input cells</th><th>persample</th><th>rounds</th>"
                 "<th>final cells</th><th>stage</th><th>last event</th></tr>" + "".join(rows) + "</table>"
                 if rows else "<p>no units yet</p>")
    return _page(root.name, "".join(parts))


def _page(title: str, body: str, up: str | None = None) -> str:
    nav = f'<p><a href="{up}">← all units</a></p>' if up else ""
    return (f'<!DOCTYPE html><html><head><meta charset="utf-8"><title>{_h.escape(title)}</title>'
            f"<style>{CSS}</style></head><body>{nav}{body}</body></html>")


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

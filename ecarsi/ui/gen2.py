"""Generation-2 units (the durable control plane): their state and page, read from the
publication.json files."""

from __future__ import annotations

import html as _h
from pathlib import Path

from .. import layout as L
from .. import review
from .common import EXPLAIN, SANKEY_JS, UMAP_JS, _bar, _json, _n, _pct, _stat, _when, fmt_elapsed


def _gen2_rounds(unit: Path) -> list[dict]:
    out = []
    # A round's input is settled the moment the round opens: it is what the previous round
    # left, or what per-sample published. Waiting for cross-sample to restate it leaves the
    # column blank for the first half of every round, which reads as "unknown", not "pending".
    survivors = _json(unit / L.GEN2_PERSAMPLE / L.GEN2_PUBLICATION, {}).get("n_survived")
    for rdir in sorted((unit / L.ROUNDS).glob("round*")):
        record = _json(rdir / L.GEN2_PUBLICATION, {})
        stats = record.get("stats") or {}
        started = min((p.stat().st_mtime for p in (rdir / L.GEN2_CROSS).glob("spec.json")), default=None)
        finished = (rdir / L.GEN2_PUBLICATION).stat().st_mtime if (rdir / L.GEN2_PUBLICATION).is_file() else None
        row = {"n": record.get("round") or L.round_number(rdir), "dir": rdir, "stats": None, "decision": None,
               "step": None, "reason": stats.get("reason", ""),
               "sankey": (rdir / L.LEDGER / "sankey.json").is_file(),
               "seconds": (finished - started) if started and finished else None,
               "msp_report": (rdir / L.GEN2_CROSS / "report.html").is_file(),
               "zmip_report": (rdir / L.GEN2_ZOOM / "report.html").is_file()}
        if stats:
            row["stats"] = {k: stats.get(k) for k in ("n_in", "n_out", "removed", "frac")}
            row["decision"] = stats.get("decision")
            survivors = stats.get("n_out")
        else:
            cross = _json(rdir / L.GEN2_CROSS / L.GEN2_PUBLICATION, {})
            zoom = _json(rdir / L.GEN2_ZOOM / L.GEN2_PUBLICATION, {})
            row["step"] = ("zoom-in done, deciding" if zoom else "zoom-in" if cross
                           else "cross-sample" if (rdir / L.GEN2_CROSS).is_dir() else "starting")
            row["n_in"] = cross.get("n_input") or survivors
            if cross:
                # The round's own number arrives only when it ends. Cross-sample publishes its
                # half as soon as it is done, which is a subtotal, not a forecast: on this batch
                # cross-sample removed 61 cells of a round that went on to remove thousands.
                if cross.get("n_removed") is not None and cross.get("n_input"):
                    row["partial"] = {"stage": "cross-sample", "removed": cross["n_removed"],
                                      "frac": cross["n_removed"] / cross["n_input"]}
        out.append(row)
    return out


def batch_source(manifest: dict) -> str:
    """Where a unit's samples and batch came from (decision 0016), one line for its Samples header."""
    decision = (manifest.get("sample_mapping") or {}).get("decision") or {}
    sources = decision.get("sources") or {}
    if not sources:
        return ""
    by = sorted({d.get("source", "agent") for d in sources.values()})
    columns = sorted({d.get("sample_column") or "whole source" for d in sources.values()})
    platforms = sorted({d["platform"] for d in sources.values() if d.get("platform")})
    batch = decision.get("batch_key") or {}
    batch_text = ("one batch, no Harmony" if batch.get("single") else
                  f"batch {batch['column']}" if batch.get("column") not in (None, "eca_batch") else "batch = sample")
    n_chunks = sum(len(c) for c in (decision.get("chunks") or {}).values())
    return "; ".join(x for x in (
        f"samples from {'/'.join(by)}: {', '.join(columns)}", batch_text,
        f"{n_chunks} chunks" if n_chunks else "", f"platform {', '.join(platforms)}" if platforms else "") if x)


def _gen2_unit_state(unit: Path) -> dict:
    published = _json(unit / L.GEN2_PUBLICATION, {})
    per = _json(unit / L.GEN2_PERSAMPLE / L.GEN2_PUBLICATION, {})
    manifest = _json(unit.parent.parent / L.GEN2_ORGANIZE / L.UNITS / unit.name / L.INPUT / L.MANIFEST, {})
    rounds = _gen2_rounds(unit)
    release = L.release_dir(unit)
    released = (release / "receipt.json").is_file()
    done = [r for r in rounds if r["stats"]]
    failed_samples = per.get("failed_samples") or []
    skipped = per.get("skipped_samples") or []
    # A unit's own failure is recorded by the dataset that waited for it, not inside the unit.
    # A resumed dataset does not rewrite that record until it finishes, so a failure older
    # than the unit's newest publication has already been superseded by the work that followed.
    dataset_path = unit.parent.parent / L.GEN2_PUBLICATION
    dataset = _json(dataset_path, {})
    failure = next((f for f in dataset.get("failed_units", []) if f.get("unit") == unit.name), None)
    if failure is not None and dataset_path.is_file():
        newest = max((p.stat().st_mtime for p in [unit / L.GEN2_PERSAMPLE / L.GEN2_PUBLICATION,
                                                  *[r["dir"] / L.GEN2_PUBLICATION for r in rounds],
                                                  *(unit / L.ROUNDS).glob("round*/0*/" + L.GEN2_PUBLICATION)]
                      if p.is_file()), default=0)
        if newest > dataset_path.stat().st_mtime:
            failure = None
    if released:
        stage, cls = "released", "released"
    elif failure and str(failure.get("error", "")).startswith("PAUSED"):
        # A unit stopped by loop_control fails its workflow so it stays resumable; that is a
        # held run waiting on a person, not a broken one.
        stage, cls = f"paused — {failure['error'].split(':', 1)[-1].strip()}"[:120], "paused"
    elif failure:
        stage, cls = f"failed — {failure.get('error', '')}"[:120], "failed"
    elif failed_samples:
        stage, cls = f"{len(failed_samples)} sample(s) failed", "failed"
    elif rounds and rounds[-1]["stats"] is None:
        stage, cls = f"round {rounds[-1]['n']} · {rounds[-1]['step']}", "running"
    elif rounds:
        stage, cls = f"round {rounds[-1]['n']} done, next round pending", "running"
    elif per:
        stage, cls = ("per-sample done, first round pending" if per.get("state") == "complete"
                      else f"per-sample {len(per.get('samples', []))} sample(s)"), "running"
    elif any((unit / L.GEN2_PERSAMPLE).glob("*")):
        stage, cls = "per-sample running", "running"
    else:
        stage, cls = "organized, per-sample not started", "running"
    n_input = published.get("n_input") or per.get("n_input") or manifest.get("n_cells")
    final_cells = published.get("n_survived") if released else (done[-1]["stats"]["n_out"] if done else None)
    events = {"organize": None, "release": None}
    organized = unit.parent.parent / L.GEN2_ORGANIZE / L.GEN2_PUBLICATION
    if organized.is_file() and n_input:
        events["organize"] = (organized.stat().st_mtime, n_input)
    if released and final_cells is not None:
        events["release"] = ((release / "receipt.json").stat().st_mtime, final_cells)
    return {"name": unit.name, "dir": unit, "generation": 2, "n_input": n_input,
            "species": manifest.get("species") or "",
            "finished": _when(events["release"][0]) if events["release"] else None,
            "persample": {"manifest": bool(per), "n": len(per.get("samples", [])) + len(failed_samples),
                          "n_done": len(per.get("samples", [])), "done": per.get("state") == "complete",
                          "samples": [], "species": manifest.get("species"), "sample_column": None,
                          "n_excluded": per.get("n_removed"), "skipped": skipped, "failed": failed_samples,
                          "batch_source": batch_source(manifest)},
            "rounds": rounds, "released": released, "stage": stage, "stage_class": cls,
            "last_event": published.get("reason", "") or (rounds[-1]["reason"] if rounds else ""),
            "final_cells": final_cells,
            "output_h5ad": (release / "final.h5ad") if (release / "final.h5ad").is_file() else None,
            "output_note": "final" if released else "", "sample_decisions": {},
            "forced": bool(published.get("forced_release")), "events": events}


def _gen2_unit_body(unit: Path, s: dict, base: str = "") -> str:
    e = _h.escape
    release = L.release_dir(unit)
    items = review.from_json(release / "needs_review.json") if (release / "needs_review.json").is_file() else []
    done = [r for r in s["rounds"] if r["stats"]]
    n_in, n_fin = s["n_input"], s["final_cells"]
    removed_frac = (1 - n_fin / n_in) if n_in and n_fin is not None else None
    per = s["persample"]
    glance = [
        _stat(_n(n_in) or "–", "input cells", e(str(s["species"] or ""))),
        _stat(_n(n_fin) or "–", "final cells" if s["released"] else "cells now",
              "" if s["released"] or n_fin is None else "after the last finished round"),
        _stat(_pct(removed_frac) if removed_frac is not None else "–", "removed overall",
              "QC, excluded samples and rounds", "tone-bad" if removed_frac and removed_frac > 0.3 else ""),
        _stat(str(len(done)) + (" <small>+1 running</small>" if s["rounds"] and not s["rounds"][-1]["stats"] else ""), "rounds"),
        _stat(f'{per["n_done"]}/{per["n"]}' if per["n"] else "–", "samples annotated",
              f'{len(per["skipped"])} skipped' if per["skipped"] else ""),
        _stat(str(len(items)), "needs review", "items, see below" if items else "nothing so far",
              "tone-warn" if items else ""),
    ]
    reports = sorted((unit / L.GEN2_PERSAMPLE).glob("*/report.html"))
    sections = (("files", "Files"), ("rounds", "Rounds"), ("samples", "Samples"), ("sankey", "Cell identity"),
                ("umap", "Final UMAP"), ("review", "Needs review"))
    parts = ['<div class="glance">' + "".join(glance) + "</div>",
             '<nav class="jump" aria-label="sections">'
             + "".join(f'<a href="#{k}">{t}</a>' for k, t in sections) + "</nav>"]
    files = []
    if s["released"]:
        for name, what in (("summary.json", "what happened, round by round"),
                           ("needs_review.md", "the review items below, as text"),
                           ("needs_review.json", "same, machine-readable"),
                           ("cell_ledger.csv.gz", "one row per input cell: status and labels per stage"),
                           ("cell_exclusions.csv.gz", "every removal with its reason"),
                           ("final.h5ad", "the released matrix"),
                           ("umap.json", "final embedding and labels behind the UMAP panels")):
            if (release / name).is_file():
                files.append((name, f'<a href="{base}{L.RELEASE}/{name}">{name}</a> <span class="muted">{what}</span>'))
    parts.append('<section class="block" id="files"><h2>Files</h2>'
                 "<p class=\"lede\">A generation-2 run keeps its computed artefacts in the warm pool's request folders, "
                 'each referenced by the publication that accepted it; the unit directory holds those publications and, '
                 'once released, the copied release.</p>'
                 + ('<dl class="files">' + "".join(f"<dt>{e(k)}</dt><dd>{v}</dd>" for k, v in files) + "</dl>"
                    if files else '<p class="empty">No release yet: the loop has not converged.</p>') + "</section>")
    rows = []
    for r in s["rounds"]:
        rp = f'{base}{L.ROUNDS}/{r["dir"].name}'
        links = " · ".join(x for x in [
            f'<a href="{rp}/{L.GEN2_CROSS}/report.html">msp</a>' if r.get("msp_report") else "",
            f'<a href="{rp}/{L.GEN2_ZOOM}/report.html">zmip</a>' if r.get("zmip_report") else "",
            f'<a href="{rp}/{L.LEDGER}/cell_ledger.csv.gz">ledger</a>' if r.get("sankey") else ""] if x)
        st = r["stats"]
        elapsed = fmt_elapsed(r["seconds"]) if r.get("seconds") else ""
        if st:
            dec, frac = r["decision"], st.get("frac")
            pill = ("failed" if str(r.get("reason", "")).startswith("FORCED") else
                    "released" if dec == "release" else "neutral")
            rows.append(f'<tr><td class="num">{r["n"]}</td><td class="num">{_n(st["n_in"])}</td>'
                        f'<td class="num">{_n(st["n_out"])}</td><td class="num">{_n(st["removed"])}</td>'
                        f'<td class="num">{_pct(frac) + _bar(frac) if frac is not None else ""}</td>'
                        f'<td><span class="pill {pill}">{e(str(dec))}</span></td>'
                        f'<td class="reason">{e(str(r.get("reason") or ""))}</td>'
                        f'<td class="num">{elapsed}</td><td>{links}</td></tr>')
        else:
            part = r.get("partial")
            so_far = (f'<td class="num">{_n(part["removed"])}</td>'
                      f'<td class="num">{100 * part["frac"]:.2f}%<span class="sofar" '
                      f'title="{e(part["stage"])} only; the round is still removing">so far</span></td>'
                      if part else "<td></td><td></td>")
            rows.append(f'<tr><td class="num">{r["n"]}</td><td class="num">{_n(r.get("n_in"))}</td><td></td>{so_far}'
                        f'<td><span class="pill running">running</span></td>'
                        f'<td class="reason st running">{e(str(r["step"] or ""))}</td>'
                        f'<td class="num">{elapsed}</td><td>{links}</td></tr>')
    running_round = bool(s["rounds"]) and s["rounds"][-1]["stats"] is None
    parts.append(f'<section class="block" id="rounds"><h2>Rounds <span class="count">{len(done)} finished'
                 + (", 1 running" if running_round else "") + f'</span></h2><p class="lede">{EXPLAIN["rounds"]}</p>'
                 + ('<div class="wrap"><table><thead><tr><th class="r">round</th><th class="r">cells in</th><th class="r">cells out</th>'
                    '<th class="r">removed</th><th class="r">removed %</th><th>decision</th><th>reason</th>'
                    '<th class="r">wall time</th><th>reports</th></tr></thead>'
                    f'<tbody>{"".join(rows)}</tbody></table></div>' if rows else '<p class="empty">No round started yet.</p>')
                 + "</section>")
    summary = _json(unit / L.GEN2_PERSAMPLE / "samples.json", [])
    if not summary:  # published before the summary existed: the reports still name the samples
        summary = [{"sample": r.parent.name} for r in reports]
    first = unit / L.ROUNDS / "round01" / L.GEN2_CROSS / "inclusion.json"
    decided = {d["sample"]: d for d in _json(first, {}).get("samples", [])}
    rows = []
    for row in summary:
        folder = unit / L.GEN2_PERSAMPLE / row["sample"]
        link = (f'<a href="{base}{L.GEN2_PERSAMPLE}/{row["sample"]}/report.html">osp report</a>'
                if (folder / "report.html").is_file() else '<span class="muted">–</span>')
        state = row.get("state", "")
        status = ('<span class="pill empty-sample" title="OSP QC removed every cell; see qc_removed.csv">empty</span>'
                  if state == "empty" else '<span class="pill failed">failed</span>' if state == "failed"
                  else '<span class="pill running" title="two agent sessions died; the survivors carry no annotation">unannotated</span>'
                  if state == "unannotated" else '<span class="pill released">done</span>' if state
                  else '<span class="pill running">pending</span>')
        d = decided.get(row["sample"])
        dec = "" if d is None else "include" if d.get("include") else "exclude"
        pill = f'<span class="pill {e(dec)}">{e(dec)}</span>' if dec else '<span class="muted">–</span>'
        if dec == "exclude" and d.get("reason"):
            pill += (f'<details class="why"><summary title="why excluded?" aria-label="why excluded?">?</summary>'
                     f'<div class="why-body"><b>{e(row["sample"])} excluded:</b> {e(d["reason"])}</div></details>')
        rows.append(f'<tr><td>{e(row["sample"])}</td><td class="num">{_n(row.get("n_input"))}</td>'
                    f'<td>{status}</td><td class="why-cell">{pill}</td><td>{link}</td></tr>')
    meta = [f'{per["n_done"]}/{per["n"]} done'] if per["n"] else []
    if per.get("n_excluded"):
        meta.append(f'{per["n_excluded"]:,} cells excluded before OSP')
    if per.get("batch_source"):
        meta.append(e(per["batch_source"]))
    parts.append(f'<section class="block" id="samples"><h2>Samples <span class="count">{" · ".join(meta)}</span></h2>'
                 f'<p class="lede">{EXPLAIN["samples"]}</p>'
                 + ('<div class="wrap"><table><thead><tr><th>sample</th><th class="r">input cells</th><th>osp</th>'
                    f'<th>integration</th><th>report</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>'
                    if rows else '<p class="empty">Per-sample processing has not started.</p>') + "</section>")
    if per["skipped"] or per["failed"]:
        detail = "".join(f'<li><b>{e(str(x.get("sample", "")))}</b> {e(str(x.get("error", ""))[:200])}</li>'
                         for x in list(per["skipped"]) + list(per["failed"]))
        parts.append('<section class="block"><h2>Samples needing attention</h2><ul class="warn">' + detail + "</ul></section>")
    # Released: the final ledger. Still looping: the newest round that published one, so the
    # Sankey is there from round one instead of only after convergence.
    sankey = next((p for p in [release / "sankey.json"]
                   + [r["dir"] / L.LEDGER / "sankey.json" for r in reversed(s["rounds"])] if p.is_file()), None)
    if sankey is not None:
        data = sankey.read_text(encoding="utf-8").replace("<", "\\u003c")
        through = ("the release" if sankey.parent == release
                   else "round " + str(next(r["n"] for r in s["rounds"] if r["dir"] / L.LEDGER == sankey.parent)))
        parts.append('<section class="block" id="sankey"><h2>Cell identity across steps and rounds '
                     f'<span class="count">coarse labels · through {through}</span></h2>'
                     f'<p class="lede">{EXPLAIN["sankey"]}</p>'
                     f'<div id="sankey-vis" class="wrap"></div><script>const SANKEY_DATA = {data};{SANKEY_JS}</script></section>')
    umap = release / "umap.json"
    if umap.is_file():
        data = umap.read_text(encoding="utf-8").replace("<", "\\u003c")
        parts.append('<section class="block" id="umap"><h2>Final UMAP '
                     '<span class="count">every released cell · coarse and fine labels</span></h2>'
                     f'<p class="lede">{EXPLAIN["umap"]}</p><div id="umap-vis">'
                     f'<p class="umap-status">loading {base}{L.RELEASE}/umap.json… (JavaScript required)</p>'
                     '<div class="umap-row"></div></div>'
                     f'<script type="application/json" id="umap-data">{data}</script><script>{UMAP_JS}</script></section>')
    counts = review.counts(items)
    brief = " · ".join(f"{n} {t.lower()}" for _, t, n, _ in counts) if counts else "nothing to review"
    parts.append(f'<section class="block" id="review"><h2>Needs review <span class="count">{len(items)} items — {e(brief)}</span></h2>'
                 f'<p class="lede">{EXPLAIN["review"]}</p>' + review.to_html(items, base) + "</section>")
    return "".join(parts)

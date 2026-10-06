"""ecarsi.ui.index — landing pages generated from the artefacts on disk.

    python -m ecarsi index <root | unit>       (re)write the static pages

Nothing here is told what happened: the state of a run is read back from
manifests, contract files, stats/decision files and progress.log, so the
same function renders a finished release and a run that is halfway through
round 2 (ecarsi.ui.serve re-renders on every request, which is what makes
mid-run monitoring possible). Every step also writes the static pages when
it finishes, so a directory that is only copied around still has them.

    <root>/index.html         header card + units table (a one-unit run shows that unit inline)
    <root>/units/<u>/index.html   at-a-glance numbers, files, rounds, samples, sankey, UMAP, needs-review
"""

from __future__ import annotations

import hashlib
import html as _h
import json
import sys
from pathlib import Path

from .. import layout as L
from ..degraded import read as read_degraded
from . import gen1
from .common import EXPLAIN, _json, _n, _when, asset
from .gen1 import _unit_body
from .gen2 import _gen2_unit_body, _gen2_unit_state

CSS = asset("page.css")


# ---------------------------------------------------------------- unit state


# ---------------------------------------------- generation 2 (durable control plane)
# A gen-2 run records its state in publication.json files instead of progress.log / stats.txt /
# decision.txt, and keeps its computed artefacts in the Pool's request folders; only release/ is
# copied into the unit. These readers return the state a gen-1 unit returns, so one navigator lists
# both generations and the release sections (ledger, sankey, UMAP, needs review) are shared.


# ---------------------------------------------------------------- unit page


def unit_state(unit: Path) -> dict:
    """Everything the pages need, read from disk."""
    return _gen2_unit_state(unit) if L.is_gen2_unit(unit) else gen1.unit_state(unit)


def _tree_collection(path: Path) -> str:
    """The collection a path inside a fleet tree names: `<coll>/eca-pp/<dataset>/...`, or the
    direct study layout `<coll>/<study>/{standardize,rsi}`."""
    place = L.fleet_place(path)
    return place[0] if place else ""


def collection_of(path: Path) -> str:
    """Collection for eca-pp fleet trees or direct study/standardize layouts. A run kept outside
    those trees -- a control-plane run directory -- was organized from an input that is in one,
    and its spec records that input (2026-09-24: Tabula Sapiens and chondroatlas plane runs sat
    under "other" beside the same studies' Oak rows, and every new plane run would have too)."""
    path = Path(path)
    if coll := _tree_collection(path):
        return coll
    if coll := _json(path / L.DISPLAY, {}).get("collection"):  # a display-zone copy records its own
        return coll
    try:
        root = json.loads((path / L.GEN2_SPEC).read_text()).get("input_root")
    except (OSError, ValueError, AttributeError):
        return ""
    return _tree_collection(Path(root)) if isinstance(root, str) else ""


def collections(direct: dict) -> dict:
    """Fill in the collections a path could not give. `direct` is name -> collection_of()
    (empty where the path says nothing). A run kept outside the fleet tree -- a
    control-plane run directory, a one-off -- still belongs to the collection its name
    carries: `<collection>-<rest>` is the prefix the pages already strip from the name.
    Takes the collections rather than the paths so the fleet pages never touch disk."""
    known = sorted({c for c in direct.values() if c}, key=len, reverse=True)
    return {name: c or next((k for k in known if name.startswith(k + "-")), "")
            for name, c in direct.items()}


def display_name(root: Path) -> str:
    """'Stomach' for .../eca-pp/Stomach/rsi, else the directory name."""
    parts = Path(root).parts
    return parts[parts.index("eca-pp") + 1] if "eca-pp" in parts[:-1] else root.name


# A live run rewrites a state file every few minutes at the very worst (a long msp
# integration still checkpoints). Nothing on disk records that a run was stopped --
# a killed driver, an expired Slurm job and a terminated workflow all leave the last
# state behind -- so a "running" run whose files stopped moving this long ago is
# reported as stopped instead of pretending it is still working.
STALE_AFTER = 12 * 3600


def _stalled(cls: str, stage: str, updated: float | None) -> tuple[str, str]:
    import time

    if cls != "running" or not updated or time.time() - updated < STALE_AFTER:
        return cls, stage
    return "failed", f"stopped · {stage}"


# Where a round's removal sits: the rule releases below 1 % (round_policy.RELEASE_FRAC) and calls
# three rounds under 2 % a plateau, so a run is doing well well before it stops.
# A band is a judgement on one round, not a lifecycle state, so it has its own class names:
# reusing .released/.running here tied the dot's colour to whatever a dataset pill happened to be.
TREND_BANDS = ((0.015, "band-good"), (0.03, "band-watch"))   # under 1.5 % green, under 3 % amber, else red
# The same three colours the dots get, as variables the SVG gradient can read.
BAND_INK = {"band-good": "--ok", "band-watch": "--run", "band-high": "--bad"}


def trend_band(frac: float) -> str:
    return next((cls for edge, cls in TREND_BANDS if frac < edge), "band-high")


def round_trend(states: list[dict]) -> list[dict]:
    """One point per round: how much of what entered it the round removed. A settled point is a
    finished round; an unsettled one is the part a running round has published so far and will
    grow. The unit with the most rounds speaks for a dataset -- the others are shorter runs of
    the same decision, and a mean would hide the one still removing."""
    rounds = max((s.get("rounds") or [] for s in states), key=len, default=[])
    points = []
    for r in rounds:
        stats, partial = r.get("stats"), r.get("partial")
        if stats and stats.get("frac") is not None:
            points.append({"n": r["n"], "frac": stats["frac"], "settled": True})
        elif partial:
            points.append({"n": r["n"], "frac": partial["frac"], "settled": False})
    return points


def dataset_state(root: Path, states: list[dict] | None = None) -> dict:
    """Aggregate of a run root (or a unit bound on its own) for the fleet
    pages; `states` = unit_state() per unit when the caller already has them."""
    if states is None:
        states = [unit_state(u) for u in ([root] if L.is_unit(root) or L.is_gen2_unit(root) else L.units(root))]
    released = sum(1 for s in states if s["released"])
    final = [s["final_cells"] for s in states if s["final_cells"] is not None]
    n_in = [s["n_input"] for s in states if s["n_input"] is not None]
    if not states:
        stage, cls = "Not started", "neutral"
    elif released == len(states):
        stage, cls = "released", "released"
    elif any(s["stage_class"] == "failed" for s in states):
        stage, cls = "failed", "failed"
    elif any(s["stage_class"] == "paused" for s in states):
        # A unit held by loop_control stops the dataset too, but it is waiting on a person.
        stage, cls = (states[0]["stage"] if len(states) == 1 else "paused"), "paused"
    else:
        # A multi-unit run that is working said "0/3 released", which reads as a finished run
        # that released nothing -- the running colour was the only hint it was alive, and on a
        # page of 290 rows nobody reads the colour before the words. Name the work instead: with
        # nothing released yet the release fraction carries no information at all, so drop it.
        running = sum(1 for s in states if s["stage_class"] == "running")
        stage = (states[0]["stage"] if len(states) == 1
                 else f"{running}/{len(states)} units running" if not released
                 else f"{released}/{len(states)} released, {running} running")
        cls = "running"
    fin = [s["finished"] for s in states if s.get("finished")]
    events = {k: [s["events"][k] for s in states if s.get("events") and s["events"][k]] for k in ("organize", "release")}
    updated = state_mtime(root)
    cls, stage = _stalled(cls, stage, updated)
    degraded = read_degraded(root)  # steps that failed without failing the run (decision 0013)
    return {"units": len(states), "released": released, "n_input": sum(n_in) if n_in else None, "events": events,
            "final_cells": sum(final) if final else None, "rounds": max((len(s["rounds"]) for s in states), default=0),
            "species": ", ".join(sorted({str(s["species"]) for s in states if s["species"]})),
            "finished": max(fin) if fin and released == len(states) else None,
            "updated": updated, "stage": stage, "cls": cls, "trend": round_trend(states),
            "unit_rows": [dict(unit_row(s, unit_order(root)), degraded=sum(r.get("unit") in (None, s["name"]) for r in degraded))
                          for s in states], "run_id": run_id(root), "degraded": degraded,
            "awaiting_start": False}


def run_id(root: Path) -> str:
    """The control plane's name for this run, from the spec it was submitted with. It is the only
    key that ties a directory on disk to a workflow, and so the only way the fleet table can ask the
    control plane what it thinks of a run rather than inferring it from files. Empty for generation
    one, which has no control plane to ask."""
    spec = _json(root / L.GEN2_SPEC, {})
    return str(spec.get("run_id") or "")


def unit_order(root: Path) -> list:
    """The plan's unit order, which is the order the control plane numbers its unit workflows in.
    Without it a unit row can only be told what the control plane thinks of the whole run, which is
    not the same thing: a run is still running when one of its units has already failed."""
    plan = _json(root / L.GEN2_ORGANIZE / L.GEN2_PUBLICATION, {})
    return [str(u.get("name") or "") for u in (plan.get("units") or [])]


def unit_row(state: dict, order: list | None = None) -> dict:
    """One analysis unit as the fleet table sees it: its own rounds, its own clock. The dataset
    aggregate cannot carry these -- a dataset's trend was the longest unit's curve standing in for
    every unit, and its stage was a count of how many were running."""
    updated = state_mtime(state["dir"])
    cls, stage = _stalled(state["stage_class"], state["stage"], updated)
    index = (order or []).index(state["name"]) if state["name"] in (order or []) else None
    return {"name": state["name"], "stage": stage, "cls": cls, "released": state["released"], "index": index,
            "n_input": state["n_input"], "final_cells": state["final_cells"],
            "rounds": len(state["rounds"]), "trend": round_trend([state]),
            "species": str(state["species"] or ""), "updated": updated}


TREND_CEILING = 0.10   # a first round often removes 20-36 %; drawn to scale it flattens the rest
TREND_STEP = 11        # px per round: the safety cap is 15 rounds, which still fits the frame


def sparkline(points: list[dict], width: int = 108, height: int = 22) -> str:
    """Per-round removal as a share of what entered the round, oldest left. The scale is the run's
    own worst round up to TREND_CEILING, so an early clear-out does not squash the settling that
    follows; anything above the ceiling is drawn on it and keeps its true value in the tooltip.
    The colour is absolute, so two runs compare at a glance. A hollow point is still removing."""
    if not points:
        return '<span class="muted">–</span>'
    top = min(max(max(p["frac"] for p in points), 0.03), TREND_CEILING)
    # One round is one step, left to right from the frame's edge -- not stretched to fill it.
    # A run of two rounds spread across the full width reads like a long history of two states;
    # at a fixed step its shortness is the first thing visible. Long runs compress to fit.
    step = min(TREND_STEP, (width - 6) / max(len(points) - 1, 1))
    x = lambda i: 3 + i * step
    y = lambda f: height - 3 - (height - 6) * (min(f, top) / top)
    path = " ".join(("M" if i == 0 else "L") + f"{x(i):.1f} {y(p['frac']):.1f}" for i, p in enumerate(points))
    # The area under the line, washed in the colours of the rounds above it: a horizontal ramp
    # whose stops sit under their own point, so the tint between two rounds is the blend of the
    # two, faded out downward. Ids are derived from the path so two identical charts share one
    # definition and nothing collides with a neighbouring row.
    uid = hashlib.md5(path.encode()).hexdigest()[:8]
    ramp = "".join(f'<stop offset="{(x(i) - 3) / max(width - 6, 1):.4f}" '
                   f'style="stop-color:var({BAND_INK[trend_band(p["frac"])]})"/>' for i, p in enumerate(points))
    high = min(y(p["frac"]) for p in points)
    fade = (f'<linearGradient id="f{uid}" gradientUnits="userSpaceOnUse" x1="0" y1="{high:.1f}" x2="0" y2="{height - 3}">'
            '<stop offset="0" stop-color="#fff" stop-opacity=".55"/>'
            '<stop offset="1" stop-color="#fff" stop-opacity="0"/></linearGradient>')
    area = (f'{path} L{x(len(points) - 1):.1f} {height - 3} L{x(0):.1f} {height - 3} Z'
            if len(points) > 1 else "")
    defs = (f'<defs><linearGradient id="c{uid}" x1="0" y1="0" x2="1" y2="0">{ramp}</linearGradient>{fade}'
            f'<mask id="m{uid}"><rect width="{width}" height="{height}" fill="url(#f{uid})"/></mask></defs>')
    wash = f'<path d="{area}" fill="url(#c{uid})" mask="url(#m{uid})" class="sp-area"/>' if area else ""
    # A 2.6 px dot is hard to point at, so each round gets a wide invisible target carrying the
    # reading, with its own dot drawn immediately after it -- the pair lets CSS grow the dot the
    # pointer is over (`circle.sp-hit:hover + circle.sp`) without any script.
    marks = "".join(
        f'<circle cx="{x(i):.1f}" cy="{y(p["frac"]):.1f}" r="7" class="sp-hit">'
        f'<title>round {p["n"]}: {100 * p["frac"]:.2f}% removed'
        + ("" if p["settled"] else " so far (cross-sample; the round is still removing)")
        + f'</title></circle><circle cx="{x(i):.1f}" cy="{y(p["frac"]):.1f}" r="2.6" '
          f'class="sp {trend_band(p["frac"])}' + ('"' if p["settled"] else ' open"') + "/>"
        for i, p in enumerate(points))
    last = points[-1]
    return (f'<svg class="spark" viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
            f'aria-label="removal per round, last {100 * last["frac"]:.2f}%">{defs}{wash}'
            f'<path d="{path}" class="sp-line"/>{marks}</svg>')


SPARK_JS = asset("spark.js")


def _hero(s_cls: str, s_stage: str, title: str, crumb: str = "", sub: str = "", facts=(), next_: str = "") -> str:
    e = _h.escape
    return (f'<header class="hero">{crumb}<div class="title"><h1>{e(title)}</h1><span class="pill {s_cls}">{e(s_stage)}</span></div>'
            + (f'<div class="sub">{sub}</div>' if sub else "")
            + '<dl class="facts">' + "".join(f"<div><dt>{e(k)}</dt><dd>{v}</dd></div>" for k, v in facts if v) + "</dl>"
            + (f'<p class="next">{next_}</p>' if next_ else "") + "</header>")


def _unit_facts(s: dict) -> list:
    arrow = f'{_n(s["n_input"]) or "–"} → {_n(s["final_cells"]) or "–"}'
    return [("species", _h.escape(str(s["species"] or ""))), ("cells in → out", arrow), ("rounds", str(len(s["rounds"]))),
            ("finished", _h.escape(s["finished"] or "")) if s["released"] else ("last event", _h.escape(s["last_event"]))]


def render_unit(unit: Path, dataset: str | None = None) -> str:
    """One analysis unit; `dataset` is the name it is served under (crumb)."""
    s = unit_state(unit)
    e = _h.escape
    root = L.root_of(unit)
    ds = dataset or (display_name(root) if root else "")
    crumb = f'<div class="crumb"><a href="../../{L.INDEX}">{e(ds)}</a> / {L.UNITS} / {e(s["name"])}</div>' if root else ""
    hero = _hero(s["stage_class"], s["stage"], s["name"], crumb, (f"analysis unit of <b>{e(ds)}</b> · " if ds else "") + f'<code class="path">{e(str(unit))}</code>',
                 _unit_facts(s), "Start with the numbers below, then the final UMAP; Needs review lists what the agents were unsure about.")
    body = _gen2_unit_body(unit, s) if s.get("generation") == 2 else _unit_body(unit, s)
    return _page(f"{s['name']} — {ds} · eca-rsi" if root else f"{s['name']} — eca-rsi unit", unit, hero + body)


# ---------------------------------------------------------------- root page

def render_root(root: Path, name: str | None = None) -> str:
    """A run root; `name` is the name it is served under. With exactly one
    unit its whole content is shown inline, so the page is never a dead end."""
    e = _h.escape
    manifest_path = L.gen2_organize_manifest(root) if L.is_gen2_root(root) else L.organize_manifest(root)
    om = _json(manifest_path, {})
    units = L.units(root)
    states = [unit_state(u) for u in units]
    ds = dataset_state(root, states)
    title = name or display_name(root)
    coll = collection_of(root)
    sub = (f"collection <b>{e(coll)}</b> · " if coll else "") + f'<code class="path">{e(str(root))}</code>'
    arrow = f'{_n(ds["n_input"]) or "–"} → {_n(ds["final_cells"]) or "–"}'
    facts = [("species", e(ds["species"])), ("cells in → out", arrow), ("rounds", str(ds["rounds"])),
             ("finished", e(ds["finished"] or "")) if ds["finished"] else ("last updated", _when(ds["updated"]))]
    if len(units) == 1:
        u, s = units[0], states[0]
        next_ = (f'This run has one analysis unit, <b>{e(u.name)}</b>, shown below in full '
                 f'(<a href="{L.UNITS}/{e(u.name)}/{L.INDEX}">open it on its own page</a>). '
                 "Start with the numbers, then the final UMAP; Needs review lists what the agents were unsure about.")
        inline = _gen2_unit_body if s.get("generation") == 2 else _unit_body
        body = _hero(ds["cls"], ds["stage"], title, "", sub, facts, next_) + inline(u, s, f"{L.UNITS}/{e(u.name)}/")
    else:
        rows = []
        for u, s in zip(units, states):
            rows.append(f'<tr><td><a href="{L.UNITS}/{e(u.name)}/{L.INDEX}"><b>{e(u.name)}</b></a></td>'
                        f'<td>{e(str(s["species"] or ""))}</td><td class="num">{_n(s["n_input"])}</td>'
                        f'<td class="num">{s["persample"]["n_done"]}/{s["persample"]["n"]}</td><td class="num">{len(s["rounds"])}</td>'
                        f'<td class="num">{_n(s["final_cells"])}</td>'
                        f'<td><span class="pill {s["stage_class"]}">{e(s["stage"])}</span></td>'
                        f'<td class="muted">{e(s["last_event"])}</td></tr>')
        next_ = (f'This run has {len(units)} analysis units; open one below for its rounds, final UMAP and files.' if units
                 else "No analysis unit has been planned yet; organize has not finished.")
        body = (_hero(ds["cls"], ds["stage"], title, "", sub, facts, next_)
                + f'<section class="block" id="units"><h2>Units <span class="count">{ds["released"]}/{len(units)} released</span></h2>'
                + f'<p class="lede">{EXPLAIN["units"]}</p>'
                + ('<div class="wrap"><table><thead><tr><th>unit</th><th>species</th><th class="r">input cells</th><th class="r">samples</th>'
                   '<th class="r">rounds</th><th class="r">final cells</th><th>stage</th><th>last event</th></tr></thead>'
                   f'<tbody>{"".join(rows)}</tbody></table></div>' if rows else '<p class="empty">No units yet.</p>') + "</section>")
    extra = []
    if ds["degraded"]:
        extra.append('<div class="callout tone-warn"><b>degraded steps</b>: failed without failing the run; '
                     'each is a bug to fix (decision 0013)<ul class="warn">'
                     + "".join(f'<li>{e(r.get("unit") or "dataset")} · {e(r.get("stage", ""))} · {e(r.get("scope", ""))}: '
                               f'{e(r["what"])}: {e(r["error"])}</li>' for r in ds["degraded"]) + "</ul></div>")
    if om and om.get("warnings"):
        extra.append('<div class="callout tone-warn"><b>organize warnings</b><ul class="warn">'
                     + "".join(f"<li>{e(w)}</li>" for w in om["warnings"]) + "</ul></div>")
    if om:
        extra.append(f'<p class="muted">Organize plan and cell-conservation audit: <a href="{manifest_path.relative_to(root).as_posix()}">{manifest_path.relative_to(root).as_posix()}</a>'
                     + (f' · input units from eca-pp: {e(", ".join(u["name"] for u in om.get("input_units", [])))}' if om.get("input_units") else "") + "</p>")
    return _page(f"{title} — eca-rsi run", root, body + "".join(extra))


# the files a page is derived from: their newest mtime is "when the run state
# last changed" — and, since the display sync (ecarsi.display) copies with mtimes, how fresh a copy is
STATE_GLOBS = (L.PROGRESS, f"{L.UNITS}/*/{L.PROGRESS}", f"{L.ORGANIZE}/{L.MANIFEST}", f"{L.INPUT}/{L.MANIFEST}",
               f"{L.PERSAMPLE}/{L.MANIFEST}", f"{L.PERSAMPLE}/*/{L.RUN_STATE}", f"{L.ROUNDS}/*/{L.MANIFEST}",
               f"{L.ROUNDS}/*/{L.STATS}", f"{L.ROUNDS}/*/{L.DECISION}", f"{L.RELEASE}/summary.json", f"{L.RELEASE}/pruned.json",
               # spec.json is written once, at submission, before any stage has produced
               # anything -- the floor that gives a just-queued dataset a "last updated" at all
               # (2026-09-21: mouse-pansci-lung_WT_p2of5 organized in 21s and spent 30+ min in
               # per-sample without a single matching glob, so it read as never updated).
               L.GEN2_SPEC, f"{L.GEN2_ORGANIZE}/{L.GEN2_PUBLICATION}",
               L.GEN2_PUBLICATION, f"{L.UNITS}/*/{L.GEN2_PUBLICATION}", f"{L.UNITS}/*/{L.ROUNDS}/*/{L.GEN2_PUBLICATION}",
               # a round publishes only when it ends; its stages publish as they finish, so a
               # long round still moves the clock
               f"{L.UNITS}/*/{L.ROUNDS}/*/*/{L.GEN2_PUBLICATION}",
               f"{L.UNITS}/*/{L.GEN2_PERSAMPLE}/{L.GEN2_PUBLICATION}", f"{L.RELEASE}/receipt.json",
               f"{L.UNITS}/*/{L.RELEASE}/receipt.json",
               # the same gen-2 publications seen from a unit directory, for the fleet table's unit
               # rows: without these a unit has no clock at all (they cost nothing at dataset level,
               # where a dataset root has no rounds/ of its own)
               f"{L.ROUNDS}/*/{L.GEN2_PUBLICATION}", f"{L.ROUNDS}/*/*/{L.GEN2_PUBLICATION}",
               f"{L.GEN2_PERSAMPLE}/{L.GEN2_PUBLICATION}")

# A stage publishes only when it ends, so a unit can spend an hour in per-sample, or a cross-sample
# session hundreds of model turns, without one glob above matching: the page then reports the run as
# last touched at the previous stage boundary (2026-09-21: a unit writing agent state every few
# seconds read as 13 minutes idle). A directory's mtime moves as soon as an entry is created in it,
# which a working stage does constantly, and one stat per stage directory is cheap where a walk over
# its thousands of agent files would not be.
# Directory mtimes were tried here as a proxy for "work is happening" and withdrawn (2026-09-22).
# A copy (the display sync, ecarsi.display, and the --mirror copies before it) keeps files' mtimes but
# creates the directories with mkdir -- so every directory in a copy is as new as the last sync, and a
# run dead for a week read as fresh on exactly the copy that exists to be served. A long per-sample really can write nothing
# into the run directory for half an hour; that gap is answered by the control plane's verdict, which
# knows the run is alive, and not by a filesystem signal that cannot tell work from a copy.


def state_mtime(d: Path) -> float | None:
    ts = []
    for g in STATE_GLOBS:
        for p in d.glob(g):
            try:
                ts.append(p.stat().st_mtime)
            except OSError:
                pass  # vanished between glob and stat
    return max(ts) if ts else None


def _page(title: str, where: Path, body: str) -> str:
    import time

    fmt = "%Y-%m-%d %H:%M:%S"
    shown = _json(L.base_of(where) / L.DISPLAY, {}).get("source")
    origin = f"the display copy of {_h.escape(shown)}" if shown else "the run directory"
    t = state_mtime(where)
    updated = f" · run state updated {time.strftime(fmt, time.localtime(t))}" if t else ""
    return (f'<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>{_h.escape(title)}</title><style>{CSS}</style></head><body><main class=\"page\">{body}"
            f'<footer>rendered {time.strftime(fmt)} from {origin} by ecarsi.index{updated} · reload for the current state</footer></main></body></html>')


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

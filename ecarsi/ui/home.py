"""The fleet pages: the navigator (sidebar and frame) and the overview page `/` opens, with the
fleet history chart."""

from __future__ import annotations

import html as _h
import json
import time
from pathlib import Path

from .. import layout as L
from . import index
from .common import _k, _n, _when, asset
from .fleet import ControlVerdicts, _dataset_state, reconcile, unstale


APP = "Periscope"


# The mark: a periscope raised above the waterline — you are outside the cluster looking in.
# Stroke-only and currentColor, so it takes the colour of wherever it is placed and scales with
# the font (see LOGO_CSS). Single-quoted attributes and no '#' so the same string can go straight
# into a data: URI for the favicon without an encoder.
LOGO_SVG = (
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' aria-hidden='true' fill='none' "
    "stroke='currentColor' stroke-width='2.2' stroke-linecap='round' stroke-linejoin='round'>"
    "<path d='M8 18.5V9.5A4.5 4.5 0 0 1 12.5 5h3.8'/><circle cx='18.5' cy='5' r='2.1'/>"
    "<path d='M2.5 21.5c1.3-1.4 2.6-1.4 3.9 0s2.6 1.4 3.9 0 2.6-1.4 3.9 0 2.6 1.4 3.9 0 2.6-1.4 3.9 0'/></svg>"
)


FAVICON = '<link rel="icon" href="data:image/svg+xml,' + LOGO_SVG.replace("currentColor", "rgb(36,86,196)") + '">'


LOGO_CSS = ".logo{display:inline-flex;vertical-align:-.12em;color:var(--accent)}.logo svg{width:1em;height:1em}h1 .logo{margin-right:.3em}"


def logo() -> str:
    return f'<span class="logo">{LOGO_SVG}</span>'


NAV_JS = asset("nav.js")


NAV_CSS = asset("nav.css")


def group_tally(counts: dict[str, int]) -> str:
    """A dot and a count per state (the word is the tooltip): a collection row has no room for
    '12 Completed · 3 Running · 1 Failed'. Same states and order as the overview and filters.
    The dot is the page's own status colour — emoji bring their own palette and their own
    advance widths, which a column of counts cannot line up."""
    return " ".join(f'<span class="st {cls}" title="{label}"><i class="dot"></i>{counts[cls]}</span>'
                    for cls, label in DATASET_STATES if counts.get(cls))


DATASET_STATES = (("released", "Completed"), ("running", "Running"), ("queued", "Queued"),
                  ("paused", "Paused"), ("neutral", "Not started"), ("failed", "Failed"))


def _navigator_html(items: dict[str, Path], registry_path: Path, state=_dataset_state, control: bool = False) -> str:
    """Shell: datasets grouped by collection down the left, the selected
    dataset's own pages (root landing page -> its units -> ...) in an iframe on
    the right. The iframe keeps the address in the hash (#/<name>/...), so
    reload / back / bookmarks land on the same page; `/` opens the overview."""
    from .. import model_web
    e = _h.escape
    groups: dict[str, list[str]] = {}
    tally: dict[str, dict[str, int]] = {}
    species: dict[str, int] = {}
    states = {name: state(p) for name, p in sorted(items.items())}
    colls = index.collections({name: (st['collection'] if 'collection' in st else index.collection_of(items[name]))
                               for name, st in states.items()})
    for name, p in sorted(items.items()):
        st = states[name]
        coll = colls[name] or "other"
        short = name[len(coll) + 1:] if name.startswith(coll + "-") else name
        # A released run's count is its result; a running one's is what the last finished round
        # left, which is worth reading as provisional. A failed run has no count to report.
        cells = _k(st["final_cells"]) if st["cls"] in ("released", "running") else ""
        sp = st.get("species") or ""
        species[sp] = species.get(sp, 0) + 1
        t = tally.setdefault(coll, {})
        t[st["cls"]] = t.get(st["cls"], 0) + 1
        groups.setdefault(coll, []).append(
            f'<a class="item" href="/{e(name)}/" data-name="{e(name)}" title="{e(name)} · {e(st["stage"])} · {e(str(p))}" '
            f'data-cells="{st["final_cells"] or 0}" data-cls="{e(st["cls"])}" data-species="{e(sp)}" '
            f'data-text="{e((name + " " + coll + " " + sp + " " + str(p) + " " + st["stage"]).lower())}">'
            f'<input class="sel" type="checkbox" value="{e(name)}" aria-label="select {e(name)} for unbind">'
            f'<span class="dot {e(st["cls"])}" title="{e(st["stage"])}"></span>'
            f'<span class="nm">{e(short)}</span>'
            + (f'<span class="cells {e(st["cls"])}" title="{"cells released" if st["cls"] == "released" else "cells left by the last finished round"}">{cells}</span>'
               if cells else "") + "</a>"
        )
    rows = "".join(
        f'<details class="group"><summary>{e(coll)}<span class="gn">{group_tally(tally[coll])}</span></summary><div class="items">{"".join(rs)}</div></details>'
        for coll, rs in sorted(groups.items())
    )
    sp_options = "".join(
        f'<option value="{e(sp)}">{e(sp or "unknown")} ({k})</option>' for sp, k in sorted(species.items(), key=lambda kv: (kv[0] == "", kv[0]))
    )
    hint = (
        "A bindable directory is an eca-rsi <b>organize root</b> (contains <code>organize/manifest.json</code> or a "
        "<code>units/</code> dir — e.g. <code>&lt;dataset&gt;/rsi</code>, the <code>&lt;root&gt;</code> you gave "
        "<code>eca-rsi run</code>) or a single <b>unit</b> (contains <code>input/organized.h5ad</code> or "
        "<code>input/manifest.json</code> — e.g. <code>&lt;root&gt;/units/&lt;unit&gt;</code>). "
        "Absolute path on the server host; a raw eca-pp <code>standardize/</code> dir or a bare h5ad is not bindable."
    )
    empty_note = '<p class="muted" style="padding:8px">nothing bound yet</p>'
    # The three destinations are the whole application; the sidebar below is one of them --
    # the dataset picker, and every control in it (filter, sorts, bind, registry path) serves
    # only that. Keeping them in one column read as an undifferentiated stack, and collapsing
    # the sidebar took the brand and the navigation away with the list.
    topbar = (
        '<header class="tb" id="tb">'
        f'<a class="tb-brand" id="brand" href="/_home" title="overview">{logo()}<b>{APP}</b></a>'
        '<nav class="tb-nav" aria-label="sections">'
        '<a class="tb-link" id="home-item" href="/_home" data-name="__home__">Overview</a>'
        + ('<a class="tb-link" id="control-item" href="/_control/" data-name="_control"'
           ' title="Temporal, warm pool and bridge of the run directory">Operations</a>' if control else '')
        + '<button class="tb-link" id="models-item" title="Primary and fallback model inventory">Agent Bridge</button>'
        '</nav></header>'
    )
    sidebar = (
        '<aside class="sb" id="sb" aria-label="datasets"><div class="sb-resizer" id="sb-resizer" title="drag to resize"></div>'
        '<div class="sb-head">'
        f'<div class="brand"><span class="sb-title">Datasets<small><span id="nav-n">{len(items)}</span> bound</small></span>'
        '<button class="icon" id="sb-toggle" title="hide sidebar" aria-label="hide sidebar">&#9776;</button></div>'
        '<input id="nav-q" type="search" placeholder="Filter datasets…" aria-label="filter datasets" autocomplete="off">'
        '<div class="sort-row"><span class="ctl"><label for="nav-sort">sort</label><select id="nav-sort">'
        '<option value="name">name</option><option value="cells">cells</option>'
        '<option value="status">status</option></select></span>'
        f'<span class="ctl"><label for="nav-sp">species</label><select id="nav-sp"><option value="">all</option>{sp_options}</select></span>'
        '<span class="ctl"><label for="nav-st">status</label><select id="nav-st"><option value="">all</option>'
        + ''.join(f'<option value="{cls}">{label}</option>' for cls, label in DATASET_STATES)
        + '</select></span></div>'
        "</div>"
        f'<div class="sb-list" id="sb-list">{rows or empty_note}</div>'
        '<div class="sb-foot">'
        '<div class="row"><button id="bind-open" class="btn plain">+ Bind…</button><button id="unbind-go" class="btn danger" disabled>Unbind…</button></div>'
        '<div id="bind-form" class="callout" style="display:none"><b>Directory to bind</b>'
        '<input id="bind-path" type="text" placeholder="/oak/…/<dataset>/rsi" aria-label="directory path" autocomplete="off" spellcheck="false">'
        '<input id="bind-name" type="text" placeholder="name (default: directory basename)" aria-label="name" autocomplete="off">'
        f"<p>{hint}</p>"
        '<div class="row"><button id="bind-go" class="btn">Bind</button><button id="bind-cancel" class="btn plain">Cancel</button></div></div>'
        '<div id="nav-msg" class="callout" style="display:none" role="status"></div>'
        f'<div class="reg" title="registry file: bind/unbind edit it; nothing in the run directories is touched">{e(str(registry_path))}</div>'
        "</div></aside>"
    )
    main = (
        '<main class="shell"><div class="mbar"><button class="icon" id="sb-show" title="show sidebar" aria-label="show sidebar">&#9776;</button>'
        '<span id="crumb"></span><button class="icon" id="reload" title="reload page" aria-label="reload page">&#8635;</button>'
        '<a class="icon" id="open" href="/" target="_blank" title="open in a new tab" aria-label="open in a new tab">&#8599;</a></div>'
        '<iframe id="frame" name="frame" title="dataset"></iframe>'
        # Everything the sidebar already knows about the dataset, shown the instant it is clicked.
        # A dataset page is rendered from disk on every request; on a cold run directory that is
        # seconds, and until now the pane kept showing the previous dataset all the way through.
        # The same skeleton the page itself uses -- page > hero > block -- so the real page
        # replaces it in place instead of everything jumping when it arrives.
        '<div id="pending" hidden aria-live="polite"><main class="page">'
        '<header class="hero"><div class="title"><h1></h1><span class="pill"></span></div>'
        '<dl class="facts"></dl></header>'
        '<section class="block"><h2>Reading the run directory<span class="count">…</span></h2>'
        '<div class="ph-bars"><i></i><i></i><i></i></div></section></main></div>'
        f'<section id="model-panel" hidden aria-label="Agent Bridge"></section>'
        '<div id="empty" style="display:none"><h2>Nothing bound yet</h2><p>Use <b>+ Bind…</b> in the sidebar or, on the server host, '
        "<code>eca-rsi serve scan-add &lt;dir-or-glob&gt;</code>. The server picks up registry changes on the next request.</p>"
        f"<p>{hint}</p></div></main>"
    )
    return (
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width,initial-scale=1"><title>{APP} · ECA-RSI</title>{FAVICON}'
        f"<style>{index.CSS}{NAV_CSS}{LOGO_CSS}{model_web.CSS}</style></head><body>{topbar}<div class='below'>{sidebar}{main}</div>"
        f"<script>{model_web.JS}</script><script>{NAV_JS}</script></body></html>"
    )


HOME_CSS = asset("home.css")


def fleet_history(states: dict) -> dict:
    """Per-dataset organize / release events (epoch seconds, cells) for the
    curve; `states` = {name: (dataset_state, path)}. Derived from the logs of
    what is bound now, so unbinding a dataset removes it from the past too."""
    out = {}
    colls = index.collections({n: (s['collection'] if 'collection' in s else index.collection_of(p))
                               for n, (s, p) in states.items()})
    for name, (s, p) in sorted(states.items()):
        ev = s.get("events") or {}
        out[name] = {"collection": colls[name], "species": s["species"],
                     "organize": [list(e) for e in ev.get("organize", [])], "release": [list(e) for e in ev.get("release", [])],
                     "state": s["cls"], "input_cells": s.get("n_input") or 0,
                     "awaiting_start": s.get("awaiting_start", s["cls"] == "queued"),
                     "final_cells": s.get("final_cells") or 0}
    return {"datasets": out}


def history_at(hist: dict, at: float) -> dict:
    """The curve read at one moment: cells in / released and how many datasets had started / released."""
    cin = rel = din = drel = 0
    for d in hist["datasets"].values():
        o = [n for t, n in d["organize"] if t <= at]
        r = [n for t, n in d["release"] if t <= at]
        cin += sum(o); rel += sum(r); din += bool(o); drel += bool(r)
    return {"at": at, "cells_in": cin, "cells_released": rel, "datasets_started": din, "datasets_released": drel}


def fleet_totals(hist: dict) -> dict:
    """Current cards and the curve share the same dated input/release events."""
    result = history_at(hist, time.time())
    rows = list(hist["datasets"].values())
    result["cells_queued"] = sum(max(0, d["input_cells"] - sum(n for _, n in d["organize"]))
                                 for d in rows if d["awaiting_start"])
    result["undated_input"] = sum(max(0, d["input_cells"] - sum(n for _, n in d["organize"]))
                                  for d in rows if not d["awaiting_start"] and d["state"] != "neutral")
    released = [d for d in rows if d["state"] == "released"]
    denominator = sum(d["input_cells"] for d in released)
    result["kept"] = 100 * sum(d["final_cells"] for d in released) / denominator if denominator else None
    return result


def _parse_at(text: str) -> float:
    try:
        return float(text)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return time.mktime(time.strptime(text, fmt))
        except ValueError:
            continue
    raise ValueError(f"unparseable time {text!r}; use YYYY-MM-DDTHH:MM or epoch seconds")


HISTORY_JS = asset("history.js")


HOME_JS = asset("home.js")


def _home_html(items: dict[str, Path], state=_dataset_state, verdicts: "ControlVerdicts | None" = None) -> str:
    """Overview: what this site is, fleet numbers, and a filterable, sortable
    table of every dataset. This is the page `/` opens."""
    import time

    e = _h.escape
    states = {name: (unstale(state(p), verdicts), p) for name, p in items.items()}
    cached = [s.get('cached_at') for s, _ in states.values() if 'cached_at' in s]
    freshness = ''
    if cached:
        known = [stamp for stamp in cached if stamp is not None]
        oldest = _when(min(known)) if known else 'not yet available'
        freshness = (f'<p class="muted" role="status">Dataset summaries: {len(known)} / {len(cached)} loaded. '
                     f'Oldest refresh: {oldest}. Showing the last available data while refreshing in the background.</p>')
    if verdicts is not None:
        # Say which clock the status column is on. A reader who cannot tell a live verdict from a
        # guess made out of file dates has no way to catch the page being wrong, which is how a
        # dataset sat here reading "running" for hours after it died.
        age = verdicts.age()
        source = (f'status from the control plane, {int(age)}s old' if verdicts.live() and age is not None
                  else 'status inferred from files: the control plane is not publishing'
                  if age is None else f'status inferred from files: the control plane last published {_when(time.time() - age)}')
        freshness += f'<p class="muted" role="status">Run status: {e(source)}.</p>'
    by = lambda c: sum(1 for s, _ in states.values() if s["cls"] == c)  # noqa: E731
    history = fleet_history(states)
    totals = fleet_totals(history)
    stats = [(str(len(items)), "datasets", "", "datasets")] + [
             (str(by(cls)), label, cls, "state-" + cls)
             for cls, label in DATASET_STATES if by(cls) or cls in {"released", "running", "queued", "failed"}]
    compact = lambda n: f'{n/1_000_000:.2f} M' if n >= 10_000_000 else f'{n:,}'
    cell_stats = [
             (compact(totals["cells_in"]), "cells in", "", "cells-in"),
             (compact(totals["cells_queued"]), "cells awaiting start", "", "cells-queued"),
             (_n(totals["cells_released"]) or "0", "cells released", "", "cells-released"),
             (f'{totals["kept"]:.0f}%' if totals["kept"] is not None else "—", "kept in completed datasets", "", "kept")]
    def stat_html(rows):
        return "".join(f'<div class="stat" data-stat="{key}"><span class="v{" st " + c if c and int(v) else ""}">{e(v)}</span><span class="k">{e(k)}</span></div>'
                       for v, k, c, key in rows)
    rank = {cls: i for i, (cls, _) in enumerate(DATASET_STATES)}
    rows = []
    colls = index.collections({n: (s['collection'] if 'collection' in s else index.collection_of(p))
                               for n, (s, p) in states.items()})
    for name, (s, p) in sorted(states.items()):
        coll = colls[name]
        short = name[len(coll) + 1:] if coll and name.startswith(coll + "-") else name  # the collection has its own column
        # One row per analysis unit: a unit is what actually runs rounds, so it is the only row that
        # can carry an honest convergence curve and status. A dataset that has not organized yet has
        # no unit, and a state cached before this column existed has no unit_rows; both fall back to
        # the dataset aggregate so the fleet page never goes blank while the cache warms.
        units = s.get("unit_rows") or [dict(name="", stage=s["stage"], cls=s["cls"], n_input=s["n_input"], degraded=len(s.get("degraded") or []),
                                            final_cells=s["final_cells"], rounds=s["rounds"],
                                            trend=s.get("trend") or [], species=s["species"], updated=s["updated"])]
        # The control plane numbers its unit workflows in plan order, which the organize publication
        # records, so a unit row usually gets the verdict on that very unit. Where the order is not
        # on disk the run's own verdict stands in, and then it may only close a row, never reopen it.
        run = s.get("run_id", "")
        run_verdict = verdicts.of(run) if verdicts else None
        rows_out = []
        for u in units:
            own = verdicts.of(run, f"/unit-{u['index']}") if verdicts and u.get("index") is not None else None
            rows_out.append(reconcile(u, own or run_verdict, precise=own is not None))
        units = rows_out
        for u in units:
            kept = 100 * u["final_cells"] / u["n_input"] if u["n_input"] and u["final_cells"] is not None else None
            species = u.get("species") or s["species"]
            rows.append(
                f'<tr data-text="{e((name + " " + u["name"] + " " + coll + " " + species + " " + u["stage"]).lower())}">'
                f'<td><a href="/{e(name)}/" title="{e(name)}"><b>{e(short)}</b></a></td>'
                f'<td class="nw unit">{e(u["name"])}</td><td class="nw">{e(coll)}</td><td>{e(species)}</td>'
                f'<td class="num" data-v="{u["n_input"] or 0}">{_k(u["n_input"])}</td>'
                f'<td class="num" data-v="{u["final_cells"] or 0}">{_k(u["final_cells"])}</td>'
                f'<td class="num" data-v="{kept if kept is not None else -1}">{f"{kept:.0f}%" if kept is not None else ""}</td>'
                f'<td class="num" data-v="{u["rounds"]}">{u["rounds"] or ""}</td>'
                f'<td>{index.sparkline(u["trend"])}</td>'
                f'<td data-v="{rank.get(u["cls"], 9)}"><span class="pill {e(u["cls"])}">{e(u["stage"])}</span>'
                + (f' <span class="pill tone-warn" title="steps that failed without failing the run; see the dataset page">'
                   f'{u["degraded"]} degraded</span>' if u.get("degraded") else "") + '</td>'
                f'<td class="num nw" data-v="{u["updated"] or 0}">{_when(u["updated"])}</td></tr>')
    def th(t, num=False):
        attrs = ' class="r" data-num' if num else ""
        return f'<th{attrs} aria-sort="none"><button type="button">{t}</button></th>'
    table = ('<div class="wrap"><table id="ds-table"><thead><tr>' + th("dataset") + th("unit") + th("collection") + th("species")
             + th("cells in", True) + th("cells out", True) + th("kept", True) + th("rounds", True)
             # not sortable: the shape is the point, and one number cannot stand for it
             + '<th class="r">convergence</th>' + th("status", True) + th("last updated", True)
             + f'</tr></thead><tbody>{"".join(rows)}</tbody></table></div>' if rows
             else '<p class="empty">No dataset is bound yet. Use <b>+ Bind…</b> in the sidebar or <code>eca-rsi serve scan-add</code> on the server host.</p>')
    return (
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width,initial-scale=1"><title>{APP} — overview</title>{FAVICON}'
        f'<style>{index.CSS}{LOGO_CSS}{HOME_CSS}</style></head><body><main class="page">'
        f'<header class="hero"><div class="title"><h1>{logo()}{APP}</h1><span class="sub">ECA-RSI runs</span></div>'
        '<p class="sub" style="max-width:80ch;margin-top:8px">Recursive self-improving annotation of single-cell atlases. Each dataset below was '
        "processed per sample (QC, clustering), integrated across samples and annotated in rounds by agents, with low-quality cells removed "
        "until the loop converged. A dataset page shows the numbers, the rounds, the final UMAP with coarse and fine labels, "
        "the cell-identity Sankey, the review items and where the result files live.</p>"
        '<p class="next">Pick a dataset in the table or the sidebar. Green = released, amber = still running, red = failed.</p></header>'
        f'{freshness}<div class="glance">{stat_html(stats)}</div>'
        f'<div class="glance cell-glance" aria-label="Cell counts">{stat_html(cell_stats)}</div>'
        '<section class="block" id="history"><h2>Cells over time <span class="count" id="hist-n"></span></h2>'
        '<p class="lede">Counted at each dataset\'s recorded organize and release steps, for whatever is bound right now. '
        'Hover to read a moment, drag to zoom. <a href="#" id="hist-more">What is counted?</a></p>'
        '<p class="lede" id="hist-detail" hidden>Cells in excludes queued inputs, which are shown separately as '
        '<b>Cells awaiting start</b>; Cells released includes released units of a dataset still running. Unbind a '
        'dataset and it leaves the past too. The table filter applies to the cards and the curve; a time zoom only to the curve.</p>'
        f'<p class="muted" id="hist-undated"{" hidden" if not totals["undated_input"] else ""}>'
        f'{totals["undated_input"]:,} input cells lack a recorded start time and are excluded from Cells in and the time curve.</p>'
        '<div class="toolbar hist-range"><button type="button" data-r="1">24h</button><button type="button" data-r="7">7d</button>'
        '<button type="button" data-r="30">30d</button><button type="button" data-r="0" class="on">all</button>'
        # A batch is weeks of history in which the interesting part is the last few hours; on a
        # clock axis those hours are a sliver. Log time spaces points by age from the right edge.
        # Off by default: on it, equal horizontal distances are no longer equal durations.
        '<span class="sep"></span>'
        # Cells in climbs far faster than cells released (organize is cheap, release is the
        # whole loop), so on a shared axis its line dwarfs the released line into a flat
        # smear near zero. Hidden by default; the released curve is the one worth reading.
        '<button type="button" id="hist-show-in" aria-pressed="false" title="cells in rises much faster than cells released and compresses it on a shared axis">show cells in</button>'
        '<button type="button" id="hist-log" aria-pressed="false" title="space by age instead of by clock, so the newest hours get most of the width">log time</button></div>'
        '<div id="hist" class="hist"></div><div id="hist-tip" class="sk-tip" style="display:none"></div>'
'</section>'
        f'<section class="block" id="datasets"><h2>Datasets <span class="count" id="ds-n">{len(rows)} units</span></h2>'
        '<p class="lede">Input counts include declared queued inputs. Cells out shows the latest output count; kept is out / in. Click a column header to sort.</p>'
        '<div class="toolbar"><label for="ds-q">Filter</label><input id="ds-q" type="search" placeholder="name, collection, species, status…" autocomplete="off"></div>'
        f"{table}</section>"
        f'<footer>rendered {time.strftime("%Y-%m-%d %H:%M:%S")} by {APP} (ecarsi serve) from the registry · reload for the current state</footer>'
        f"</main><script>const HISTORY_DATA = {json.dumps(history)};</script>"
        f"<script>{HOME_JS}</script><script>{HISTORY_JS}</script><script>{index.SPARK_JS}</script></body></html>"
    )


def _render_index(root: Path, sub: str, name: str | None = None) -> str | None:
    """HTML for a landing page rendered from disk right now, or None if the
    request isn't for one. Never writes into the dataset directory (the
    pipeline steps write their own static index.html for offline use)."""
    parts = [p for p in sub.split("/") if p]
    if parts and parts[-1] == L.INDEX:
        parts = parts[:-1]
    elif parts and not sub.endswith("/"):
        return None  # a file, not a directory landing page
    if L.is_unit(root) or L.is_gen2_unit(root):
        return index.render_unit(root, name) if not parts else None
    if not parts:
        return index.render_root(root, name)
    if len(parts) == 2 and parts[0] == L.UNITS and (L.is_unit(root / L.UNITS / parts[1])
                                                    or L.is_gen2_unit(root / L.UNITS / parts[1])):
        return index.render_unit(root / L.UNITS / parts[1], name)
    return None

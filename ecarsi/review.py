"""ecarsi.review — everything a human should look at, collected once, grouped by
what it is rather than by when it happened.

Items are gathered from the artefacts on disk (never from memory) and
rendered three ways from the same records: needs_review.json (machine),
needs_review.md (archive) and an HTML fragment for the landing page.

Sections, most consequential first:

  convergence     the loop did not converge on its own / a round overshot the
                  ~10% per-round removal budget
  removed         cells an agent deleted with less than high confidence
                  (irreversible — the one thing worth re-checking first),
                  plus zoom-ins whose removal exceeded the soft budget
  sample_excluded whole samples the inclusion agent kept out of integration
  reassigned      clusters a zoom-in moved to another lineage (a cluster that
                  keeps moving every round is a labelling problem upstream)
  inspect_flag    inspection verdicts that were flagged / ambiguous / low
                  confidence (advisory to annotate; nothing was removed here)
  lineage_skipped lineages the host refused to zoom (too few cells)
  low_confidence  kept clusters labelled with low confidence — merged
                  splinters listed apart from clusters that stand alone
"""

from __future__ import annotations

import csv
import html as _h
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import layout as L

OVER_BUDGET_FRAC = 0.10  # per-round removal budget the loop treats as "too much"

KINDS: list[tuple[str, str, str]] = [
    ("convergence", "Loop convergence",
     "The loop did not stop on its own, or a round removed more than the per-round budget."),
    ("removed", "Cells removed below high confidence",
     "Irreversible. Each row is a cluster an agent deleted with medium/low confidence, or a lineage whose "
     "zoom-in removal exceeded the soft budget after a forced second look."),
    ("sample_excluded", "Samples excluded from integration",
     "Whole samples the inclusion agent kept out. They stay on disk untouched (persample/)."),
    ("reassigned", "Clusters moved between lineages",
     "Zoom-in reassignments. The same population moving every round means the coarse label upstream is unstable."),
    ("inspect_flag", "Inspection flags",
     "Cluster QC verdicts flagged, ambiguous or low-confidence. Advisory to annotate; nothing was removed here."),
    ("lineage_skipped", "Lineages not zoomed",
     "The host refused to zoom these (below --min-cells); their msp labels are final for the round."),
    ("low_confidence", "Low-confidence labels (kept)",
     "Clusters kept with low confidence. 'merged into' = the agent folded it into a sibling cluster."),
]
KIND_INDEX = {k: i for i, (k, _, _) in enumerate(KINDS)}


@dataclass
class Item:
    kind: str
    round: int
    step: str                      # loop | crosssample | zoomin
    scope: str = ""                # sample or lineage
    cluster: str = ""
    n_cells: int | None = None
    label: str = ""
    action: str = ""
    confidence: str = ""
    merged_into: str = ""
    note: str = ""
    link: str = ""                 # unit-relative report path
    extra: dict = field(default_factory=dict)


# ---------------------------------------------------------------- readers

def _json(p: Path):
    with open(p) as f:
        return json.load(f)


def _cluster_sizes(d: Path, key: str) -> dict[str, int]:
    p = d / f"cluster_qc_{key}.csv"
    if not p.is_file():
        return {}
    with open(p) as f:
        return {r[key]: int(float(r["n_cells"])) for r in csv.DictReader(f) if r.get("n_cells")}


def _agent_removed_counts(p: Path, cluster_col: str, lineage: str | None = None) -> dict[str, int]:
    if not p.is_file():
        return {}
    out: dict[str, int] = {}
    with open(p) as f:
        for r in csv.DictReader(f):
            if r.get("annotate_remove", "False") != "True":
                continue
            if lineage is not None and r.get("lineage") != lineage:
                continue
            out[r[cluster_col]] = out.get(r[cluster_col], 0) + 1
    return out


def _rel(p: Path, unit: Path) -> str:
    try:
        return str(p.relative_to(unit))
    except ValueError:
        return str(p)


# ---------------------------------------------------------------- collectors

def _loop_items(n: int, st: dict, forced: bool, last: bool) -> list[Item]:
    items = []
    if forced and last:
        items.append(Item("convergence", n, "loop", action="forced release",
                          note=f"{st.get('reason')}; last round removed {100 * st['frac']:.2f}% "
                               f"({st['removed']}/{st['n_in']}) — the loop did not converge on its own"))
    if st["frac"] > OVER_BUDGET_FRAC:
        items.append(Item("convergence", n, "loop", n_cells=st["removed"], action="over budget",
                          note=f"removed {100 * st['frac']:.1f}% of the cells that entered the round "
                               f"({st['removed']}/{st['n_in']}), above the ~{100 * OVER_BUDGET_FRAC:.0f}% per-round budget"))
    return items


def _crosssample_items(n: int, cdir: Path, unit: Path) -> list[Item]:
    items = []
    report = _rel(cdir / "report.html", unit)
    dec = cdir / "sample_decisions.csv"
    if dec.is_file():
        with open(dec) as f:
            for r in csv.DictReader(f):
                if r["decision"] == "exclude":
                    items.append(Item("sample_excluded", n, "crosssample", scope=r["sample"],
                                      n_cells=int(r["n_cells"]), action="exclude", note=r["reason"],
                                      link=_rel(L.persample_root(unit) / r["sample"] / "report.html", unit)))
    insp = cdir / "inspection_proposal.json"
    if insp.is_file():
        prop = _json(insp)
        sizes = _cluster_sizes(cdir, prop.get("cluster_key", "msp_leiden_r1.0"))
        for e in prop.get("clusters", []):
            if e.get("action") == "flag" or e.get("verdict") == "ambiguous" or e.get("confidence") == "low":
                items.append(Item("inspect_flag", n, "crosssample", cluster=str(e["cluster"]),
                                  n_cells=sizes.get(str(e["cluster"])), label=str(e.get("verdict", "")),
                                  action=str(e.get("action", "")), confidence=str(e.get("confidence", "")),
                                  note=e.get("rationale", ""), link=report))
    ann = cdir / "annotation_proposal.json"
    if ann.is_file():
        prop = _json(ann)
        key = prop.get("cluster_key", "msp_leiden_r2.0")
        sizes = _cluster_sizes(cdir, key)
        removed = _agent_removed_counts(cdir / "annotation_removed.csv", key)
        items += _annotation_items(n, "crosssample", "", prop, sizes, removed, report)
    return items


def _annotation_items(n: int, step: str, scope: str, prop: dict, sizes: dict, removed: dict,
                      report: str) -> list[Item]:
    items = []
    for e in prop.get("clusters", []):
        cid = str(e["cluster_id"])
        label = f"{e.get('coarse_label', '')} / {e.get('fine_label', '')}"
        conf = str(e.get("confidence", ""))
        act = str(e.get("action", ""))
        if act == "remove" and conf != "high":
            reason = e.get("remove_reason") or ""
            it = Item("removed", n, step, scope, cid, removed.get(cid, sizes.get(cid)), label, "remove", conf,
                      note=(f"[{reason}] " if reason else "") + e.get("rationale", ""), link=report)
        elif act == "reassign":
            it = Item("reassigned", n, step, scope, cid, sizes.get(cid), label, f"→ {e.get('reassign_to', '')}", conf,
                      note=e.get("rationale", ""), link=report, extra={"reassign_to": e.get("reassign_to", "")})
        elif conf == "low":
            it = Item("low_confidence", n, step, scope, cid, sizes.get(cid), label, act, conf,
                      merged_into=str(e.get("merge_target") or ""), note=e.get("rationale", ""), link=report)
        else:
            continue
        items.append(it)
    return items


def _zoomin_items(n: int, zdir: Path, unit: Path) -> list[Item]:
    items = []
    plan_p = zdir / "zmip_plan.json"
    if not plan_p.is_file():
        return items
    for ln in _json(plan_p)["lineages"]:
        name = ln["name"]
        if "host:" in ln.get("reason", ""):
            items.append(Item("lineage_skipped", n, "zoomin", scope=name, n_cells=ln.get("n_cells"),
                              action="not zoomed", note=ln["reason"], link=_rel(zdir / "report.html", unit)))
        ldir = L.lineage_dir(zdir, name)
        prop_p = ldir / "annotation_proposal.json"
        if not prop_p.is_file():
            continue
        prop = _json(prop_p)
        report = _rel(ldir / "report.html", unit)
        key = prop.get("cluster_key", "msp_leiden_r2.0")
        sizes = _cluster_sizes(ldir, key)
        removed = _agent_removed_counts(ldir / "annotation_removed.csv", "cluster", lineage=name)
        if prop.get("budget_exceeded"):
            items.append(Item("removed", n, "zoomin", scope=name, action="budget exceeded",
                              note=f"agent removed {100 * prop.get('agent_removed_fraction', 0):.1f}% of the lineage "
                                   "after a forced second look", link=report))
        items += _annotation_items(n, "zoomin", name, prop, sizes, removed, report)
    return items


def collect(unit: Path, rounds: list[Path], stats: list[dict], forced: bool) -> list[Item]:
    """All review items of a unit, ordered by section then round."""
    items: list[Item] = []
    for i, (rdir, st) in enumerate(zip(rounds, stats), 1):
        items += _loop_items(i, st, forced, last=(i == len(stats)))
        items += _crosssample_items(i, L.crosssample_dir(rdir), unit)
        items += _zoomin_items(i, L.zoomin_dir(rdir), unit)
    _mark_recurring(items)
    items.sort(key=lambda it: (KIND_INDEX[it.kind], it.round, it.step, it.scope, _num(it.cluster)))
    return items


def _num(s: str):
    return (0, int(s)) if s.isdigit() else (1, s)


def _mark_recurring(items: list[Item]) -> None:
    """A reassignment of the same population (lineage → target, same fine
    label) in several rounds is one recurring problem, not several."""
    groups: dict[tuple, list[Item]] = {}
    for it in items:
        if it.kind == "reassigned":
            groups.setdefault((it.scope, it.extra.get("reassign_to"), it.label), []).append(it)
    for g in groups.values():
        rs = sorted({it.round for it in g})
        if len(rs) > 1:
            for it in g:
                it.extra["recurs_in_rounds"] = rs


# ---------------------------------------------------------------- rendering

def counts(items: list[Item]) -> list[tuple[str, str, int, int]]:
    """(kind, title, n_items, n_cells) per non-empty section."""
    out = []
    for kind, title, _ in KINDS:
        sel = [it for it in items if it.kind == kind]
        if sel:
            out.append((kind, title, len(sel), sum(it.n_cells or 0 for it in sel)))
    return out


_COLS = ("round", "step", "scope", "cluster", "cells", "label", "action", "confidence", "merged into", "note")


def _row(it: Item) -> list[str]:
    note = it.note
    if it.extra.get("recurs_in_rounds"):
        note = f"[recurs in rounds {', '.join(map(str, it.extra['recurs_in_rounds']))}] " + note
    return [str(it.round), it.step, it.scope, it.cluster, "" if it.n_cells is None else str(it.n_cells),
            it.label, it.action, it.confidence, it.merged_into, note]


def _used_cols(rows: list[list[str]]) -> list[int]:
    return [j for j in range(len(_COLS)) if any(r[j] for r in rows)]


def to_markdown(items: list[Item], unit_name: str, n_rounds: int) -> str:
    lines = [f"# Needs review — {unit_name}", "",
             f"Everything the agents were unsure about or the host overrode across {n_rounds} round(s), "
             "collected once at release from the artefacts on disk. Nothing here stopped the loop.", ""]
    cs = counts(items)
    if not cs:
        lines += ["Nothing to review.", ""]
        return "\n".join(lines)
    lines += ["| section | items | cells |", "|---|---|---|"]
    lines += [f"| {title} | {n} | {c or ''} |" for _, title, n, c in cs]
    lines.append("")
    for kind, title, desc in KINDS:
        sel = [it for it in items if it.kind == kind]
        if not sel:
            continue
        lines += [f"## {title}", "", desc, ""]
        rows = [_row(it) for it in sel]
        used = _used_cols(rows)
        esc = lambda s: s.replace("|", "\\|").replace("\n", " ")  # noqa: E731
        lines.append("| " + " | ".join(_COLS[j] for j in used) + " |")
        lines.append("|" + "---|" * len(used))
        lines += ["| " + " | ".join(esc(r[j]) for j in used) + " |" for r in rows]
        lines.append("")
    return "\n".join(lines)


def to_html(items: list[Item], base: str = "") -> str:
    """HTML fragment (sections + tables) with report links; `base` prefixes
    the unit-relative links (e.g. 'units/x/' on the root page)."""
    cs = counts(items)
    if not cs:
        return "<p>Nothing to review.</p>"
    out = ['<table class="review-summary"><tr><th>section</th><th>items</th><th>cells</th></tr>']
    out += [f'<tr><td><a href="#review-{k}">{_h.escape(t)}</a></td><td>{n}</td><td>{c or ""}</td></tr>'
            for k, t, n, c in cs]
    out.append("</table>")
    for kind, title, desc in KINDS:
        sel = [it for it in items if it.kind == kind]
        if not sel:
            continue
        rows = [_row(it) for it in sel]
        used = _used_cols(rows)
        out.append(f'<h3 id="review-{kind}">{_h.escape(title)} <small>({len(sel)})</small></h3><p class="desc">{_h.escape(desc)}</p>')
        out.append('<table class="review"><tr>' + "".join(f"<th>{_h.escape(_COLS[j])}</th>" for j in used) + "<th></th></tr>")
        for it, r in zip(sel, rows):
            cells = "".join(f'<td class="{"note" if _COLS[j] == "note" else ""}">{_h.escape(r[j])}</td>' for j in used)
            link = f'<a href="{_h.escape(base + it.link)}">report</a>' if it.link else ""
            out.append(f"<tr>{cells}<td>{link}</td></tr>")
        out.append("</table>")
    return "\n".join(out)


def to_json(items: list[Item]) -> str:
    return json.dumps([asdict(it) for it in items], indent=1)


def from_json(p: Path) -> list[Item]:
    return [Item(**d) for d in _json(p)]

"""ecarsi-ledger — per-cell identity ledger across the steps, and its Sankey.

    python -m ecarsi.ledger <unit_dir> [out_dir]

Every step of the chain leaves a per-cell record (osp qc_removed.csv +
clustered.h5ad obs, crosssample sample_decisions.csv + annotation_removed.csv
+ annotated.h5ad obs, zmip zmip_removed.csv + annotated_zmip.h5ad obs).
This module joins them into ONE table, one row per input cell, one column
group per stage:

    cell, sample,
    osp_status  (kept | removed:<qc_reason>)       osp_coarse, osp_fine, osp_qc_action
    msp_status  (kept | excluded-sample | removed:<source>)   msp_coarse, msp_fine
    zmip_status (kept | not-zoomed | removed:<source>)        zmip_lineage, zmip_coarse, zmip_fine

and draws the Sankey of coarse identity stage → stage → stage, with the cells
removed at each stage flowing into a red sink so no cell disappears from the
picture. Rounds of the loop add further stage column groups (round02_msp_*,
round02_zmip_*) and further Sankey columns — the renderer is generic over an
ordered list of (stage label, label column, status column).

Outputs: <out>/cell_ledger.csv, <out>/sankey_coarse.png (+ sankey_fine_<lineage>.png
per zoomed lineage: fine labels within one lineage, msp → zmip).
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.patches import PathPatch
from matplotlib.path import Path as MPath

REMOVED_PREFIX = "removed:"
OTHER_MIN_FRAC = 0.01  # labels below this share of a stage are pooled into "other"


# ---------------------------------------------------------------- ledger

def _obs(path: Path, cols: list[str]) -> pd.DataFrame:
    import anndata as ad

    a = ad.read_h5ad(path, backed="r")
    df = a.obs[[c for c in cols if c in a.obs.columns]].copy()
    a.file.close()
    df.index.name = "cell"
    return df.astype(object)


def build_ledger(unit: Path, cs_root: Path) -> pd.DataFrame:
    """Join every stage's per-cell records into one table (one row per cell
    that entered persample, i.e. every organized cell)."""
    # persample: survivors' obs + the QC ledger of dropped cells
    frames = []
    ps_root = unit / "persample"
    man = json.load(open(ps_root / "manifest.json")) if (ps_root / "manifest.json").is_file() else {}
    sample_dirs = [Path(s["dir"]) for s in man.get("samples", [])] or sorted(
        p for p in ps_root.iterdir() if p.is_dir() and (p / "clustered.h5ad").is_file())
    for d in sample_dirs:
        if not (d / "clustered.h5ad").is_file():
            continue
        o = _obs(d / "clustered.h5ad", ["_ann_coarse", "_ann_fine", "_qc_action"])
        o = o.rename(columns={"_ann_coarse": "osp_coarse", "_ann_fine": "osp_fine", "_qc_action": "osp_qc_action"})
        o.insert(0, "sample", d.name)
        o.insert(1, "osp_status", "kept")
        frames.append(o)
        rm = d / "qc_removed.csv"
        if rm.is_file():
            r = pd.read_csv(rm, index_col="cell")
            frames.append(pd.DataFrame({
                "sample": d.name,
                "osp_status": REMOVED_PREFIX + r["qc_reason"].astype(str).fillna("low_quality"),
            }, index=r.index))
    if not frames:
        sys.exit(f"no persample outputs under {ps_root}")
    ledger = pd.concat(frames)
    ledger = ledger[~ledger.index.duplicated(keep="first")]

    # crosssample: excluded samples, msp removal ledger, msp labels
    idir = cs_root / "integrate"
    ledger["msp_status"] = np.where(ledger["osp_status"] == "kept", "kept", "")
    dec = idir / "sample_decisions.csv"
    if dec.is_file():
        excluded = {r["sample"] for r in csv.DictReader(open(dec)) if r["decision"] == "exclude"}
        m = ledger["sample"].isin(excluded) & (ledger["msp_status"] == "kept")
        ledger.loc[m, "msp_status"] = "excluded-sample"
    rm = idir / "annotation_removed.csv"
    if rm.is_file():
        r = pd.read_csv(rm, index_col="cell")
        src = np.where(r.get("annotate_remove", False), "agent:" + r["remove_reason"].astype(str).fillna(""),
                       np.where(r.get("inspect_drop", False), "inspect", "preannotation"))
        ledger.loc[ledger.index.intersection(r.index), "msp_status"] = (
            REMOVED_PREFIX + pd.Series(src, index=r.index)).reindex(ledger.index.intersection(r.index))
    ann = idir / "annotated.h5ad"
    if ann.is_file():
        o = _obs(ann, ["msp_ann_coarse", "msp_ann_fine"]).rename(
            columns={"msp_ann_coarse": "msp_coarse", "msp_ann_fine": "msp_fine"})
        ledger = ledger.join(o, how="left")

    # zoomin: not-zoomed lineages, zmip removal ledger, zmip labels
    zdir = cs_root / "zoomin"
    ledger["zmip_status"] = np.where(ledger["msp_status"] == "kept", "kept", "")
    plan_p = zdir / "zmip_plan.json"
    if plan_p.is_file():
        plan = json.load(open(plan_p))
        not_zoomed = {lab for ln in plan["lineages"] if not ln["zoom"] for lab in ln["coarse_labels"]}
        m = (ledger["zmip_status"] == "kept") & ledger.get("msp_coarse", pd.Series("", index=ledger.index)).isin(not_zoomed)
        ledger.loc[m, "zmip_status"] = "not-zoomed"
    rm = zdir / "zmip_removed.csv"
    if rm.is_file():
        r = pd.read_csv(rm, index_col="cell")
        src = np.where(r["annotate_remove"], "agent:" + r["remove_reason"].astype(str).fillna(""), "preannotation")
        idx = ledger.index.intersection(r.index)
        ledger.loc[idx, "zmip_status"] = (REMOVED_PREFIX + pd.Series(src, index=r.index)).reindex(idx)
    ann = zdir / "annotated_zmip.h5ad"
    if ann.is_file():
        o = _obs(ann, ["zmip_lineage", "zmip_ann_coarse", "zmip_ann_fine"]).rename(
            columns={"zmip_ann_coarse": "zmip_coarse", "zmip_ann_fine": "zmip_fine"})
        ledger = ledger.join(o, how="left")
    return ledger


# ---------------------------------------------------------------- sankey

def _stage_nodes(ledger: pd.DataFrame, label_col: str | None, status_col: str, keep_mask: np.ndarray):
    """Per-row node id for one stage: the label for cells still in play,
    'removed (<source>)' for cells the stage removed, None for cells already
    gone before this stage. Small labels pooled into 'other'."""
    status = ledger[status_col].astype(str) if status_col in ledger else pd.Series("kept", index=ledger.index)
    node = pd.Series(None, index=ledger.index, dtype=object)
    alive = keep_mask & ~status.str.startswith(REMOVED_PREFIX).values & (status != "excluded-sample").values
    if label_col and label_col in ledger:
        lab = ledger[label_col].astype(str).where(ledger[label_col].notna(), "unlabelled")
        node[alive] = lab[alive]
        vc = lab[alive].value_counts()
        small = set(vc[vc < OTHER_MIN_FRAC * alive.sum()].index)
        if len(small) > 1:
            node[alive & lab.isin(small).values] = f"other ({len(small)} labels)"
    else:
        node[alive] = "cells"
    gone = keep_mask & ~alive
    src = status[gone].str.replace(REMOVED_PREFIX, "", regex=False)
    node[gone] = "removed: " + src.str.replace(r"^agent:", "", regex=True)
    return node, alive


def _bezier(ax, x0, y0a, y0b, x1, y1a, y1b, color, alpha=0.45):
    dx = (x1 - x0) * 0.5
    verts = [(x0, y0a), (x0 + dx, y0a), (x1 - dx, y1a), (x1, y1a),
             (x1, y1b), (x1 - dx, y1b), (x0 + dx, y0b), (x0, y0b), (x0, y0a)]
    codes = [MPath.MOVETO, MPath.CURVE4, MPath.CURVE4, MPath.CURVE4,
             MPath.LINETO, MPath.CURVE4, MPath.CURVE4, MPath.CURVE4, MPath.CLOSEPOLY]
    ax.add_patch(PathPatch(MPath(verts, codes), facecolor=color, edgecolor="none", alpha=alpha))


def draw_sankey(ledger: pd.DataFrame, stages: list[tuple[str, str | None, str]], out_png: Path,
                title: str, min_label_frac: float = 0.01):
    """stages: ordered [(stage title, label column or None, status column)].
    Node = label (or removal sink) at that stage; flow = cells moving between
    consecutive stages. Removed cells sit at the bottom of the stage that
    removed them (red) and stop there."""
    keep = np.ones(len(ledger), dtype=bool)
    cols = []
    for _, label_col, status_col in stages:
        node, alive = _stage_nodes(ledger, label_col, status_col, keep)
        cols.append(node)
        keep = alive.values if hasattr(alive, "values") else alive
    total = len(ledger)
    palette = plt.get_cmap("tab20")
    color_of: dict[str, tuple] = {}

    def color(name):
        if name.startswith("removed:"):
            return (0.75, 0.22, 0.17, 1.0)
        if name.startswith("other"):
            return (0.6, 0.6, 0.6, 1.0)
        if name not in color_of:
            color_of[name] = palette(len(color_of) % 20)
        return color_of[name]

    n_st = len(stages)
    fig_h = 9
    fig, ax = plt.subplots(figsize=(4.2 * n_st + 2, fig_h))
    gap = 0.012
    x_pos = [i * 1.0 for i in range(n_st)]
    bar_w = 0.08
    layout = []  # per stage: {node: (y_top, y_bottom)} in [0,1] top-down
    for i, node in enumerate(cols):
        vc = node.value_counts()
        kept_nodes = sorted([n for n in vc.index if not n.startswith("removed:")], key=lambda n: -vc[n])
        rm_nodes = sorted([n for n in vc.index if n.startswith("removed:")], key=lambda n: -vc[n])
        order = kept_nodes + rm_nodes
        y = 0.0
        pos = {}
        for n in order:
            h = vc[n] / total
            pos[n] = (y, y + h)
            y += h + gap
        layout.append(pos)
        for n, (y0, y1) in pos.items():
            ax.add_patch(plt.Rectangle((x_pos[i], -y1), bar_w, y1 - y0, facecolor=color(n), edgecolor="white", lw=0.5))
            frac = vc[n] / total
            if frac >= min_label_frac:
                label = f"{n} ({vc[n]})"
                side_x = x_pos[i] + bar_w + 0.02 if i == n_st - 1 else x_pos[i] - 0.02
                ha = "left" if i == n_st - 1 else "right"
                ax.text(side_x, -(y0 + y1) / 2, label, va="center", ha=ha, fontsize=7,
                        color="#7a1f16" if n.startswith("removed:") else "black")
    # flows
    for i in range(n_st - 1):
        a, b = cols[i], cols[i + 1]
        m = a.notna() & b.notna() & ~a.astype(str).str.startswith("removed:")
        ct = pd.crosstab(a[m], b[m])
        src_off = {n: layout[i][n][0] for n in ct.index}
        dst_off = {n: layout[i + 1][n][0] for n in ct.columns}
        for s in ct.index:
            for d in ct.columns:
                v = ct.loc[s, d]
                if v == 0:
                    continue
                h = v / total
                y0a, y0b = src_off[s], src_off[s] + h
                y1a, y1b = dst_off[d], dst_off[d] + h
                src_off[s] += h
                dst_off[d] += h
                _bezier(ax, x_pos[i] + bar_w, -y0a, -y0b, x_pos[i + 1], -y1a, -y1b,
                        color(d) if d.startswith("removed:") else color(s))
    for i, (name, _, _) in enumerate(stages):
        ax.text(x_pos[i] + bar_w / 2, 0.03, name, ha="center", va="bottom", fontsize=11, fontweight="bold")
    ax.set_xlim(-0.9, x_pos[-1] + bar_w + 0.9)
    ax.set_ylim(-1.15, 0.08)
    ax.axis("off")
    ax.set_title(title, fontsize=12)
    fig.tight_layout()
    fig.savefig(out_png, dpi=150)
    plt.close(fig)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="ecarsi.ledger", description=__doc__)
    ap.add_argument("unit", help="organize unit dir")
    ap.add_argument("out", nargs="?", help="crosssample root (default <unit>/crosssample); outputs go to <root>/ledger")
    args = ap.parse_args(argv)
    unit = Path(args.unit).resolve()
    cs_root = Path(args.out).resolve() if args.out else unit / "crosssample"
    out = cs_root / "ledger"
    out.mkdir(parents=True, exist_ok=True)

    ledger = build_ledger(unit, cs_root)
    ledger.to_csv(out / "cell_ledger.csv")
    print(f"[ledger] {len(ledger)} cells → {out / 'cell_ledger.csv'}")
    for col in ("osp_status", "msp_status", "zmip_status"):
        if col in ledger:
            vc = ledger[col].replace("", np.nan).dropna().value_counts()
            print(f"  {col}: " + ", ".join(f"{k}={v}" for k, v in vc.items()))

    stages = [("per-sample (osp)", "osp_coarse", "osp_status"),
              ("cross-sample (msp)", "msp_coarse", "msp_status"),
              ("zoom-in (zmip)", "zmip_coarse", "zmip_status")]
    draw_sankey(ledger, stages, out / "sankey_coarse.png", "Cell identity across steps (coarse labels)")
    print(f"[sankey] {out / 'sankey_coarse.png'}")
    plan_p = cs_root / "zoomin" / "zmip_plan.json"
    if plan_p.is_file() and "msp_fine" in ledger:
        plan = json.load(open(plan_p))
        for ln in plan["lineages"]:
            if not ln["zoom"]:
                continue
            sub = ledger[(ledger["msp_status"] == "kept") & ledger["msp_coarse"].isin(ln["coarse_labels"])]
            draw_sankey(sub, [("cross-sample (msp)", "msp_fine", "msp_status"),
                              ("zoom-in (zmip)", "zmip_fine", "zmip_status")],
                        out / f"sankey_fine_{ln['name']}.png", f"{ln['name']}: fine labels msp → zmip",
                        min_label_frac=0.0)
            print(f"[sankey] {out / f'sankey_fine_{ln['name']}.png'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

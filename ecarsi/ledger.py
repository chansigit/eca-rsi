"""ecarsi-ledger — per-cell identity ledger across the steps and rounds, and its Sankey.

    python -m ecarsi.ledger <unit_dir> [round_dir ...]

Every removal step leaves a per-cell record (osp qc_removed.csv, crosssample
sample_decisions.csv + msp annotation_removed.csv, zmip zmip_removed.csv) and
every annotation step leaves labels in an h5ad. This module joins them into
ONE table — one row per cell that entered persample — with a column group
per stage:

    cell, sample
    osp_status  (kept | removed:<qc_reason>)                osp_coarse, osp_fine
    rNN_msp_status  (kept | excluded-sample | removed:<source>)   rNN_msp_coarse, rNN_msp_fine
    rNN_zmip_status (kept | not-zoomed | removed:<source>)        rNN_zmip_lineage, rNN_zmip_coarse, rNN_zmip_fine

for every round directory given (a round dir holds crosssample/ and
zoomin/). Default rounds: every <unit>/rounds/roundNN that has started.

Sankey: one column per stage (osp, then msp/zmip per round); node = label,
cells removed at a stage flow into a red sink in that column and stop there,
so no cell ever disappears from the picture. Labels under 1% of a column are
pooled into "other".

Outputs (in the last round dir's ledger/): cell_ledger.csv, sankey_coarse.png,
sankey_fine_<lineage>.png per zoomed lineage of the last round (msp → zmip).
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

from . import layout as L

REMOVED_PREFIX = "removed:"
OTHER_MIN_FRAC = 0.01  # labels below this share of a stage are pooled into "other"


# ---------------------------------------------------------------- ledger

def _obs_source(path: Path) -> Path | None:
    """The h5ad itself, or the obs sidecar ecarsi.prune left in its place."""
    if path.is_file():
        return path
    for suf in (".obs.parquet", ".obs.csv.gz"):
        side = path.with_name(path.name + suf)
        if side.is_file():
            return side
    return None


def _obs(path: Path, cols: list[str]) -> pd.DataFrame:
    src = _obs_source(path)
    if src is None:
        raise FileNotFoundError(path)
    if src.suffix == ".parquet":
        obs = pd.read_parquet(src)
    elif src.name.endswith(".csv.gz"):
        obs = pd.read_csv(src, index_col=0, low_memory=False)
    else:
        import anndata as ad

        a = ad.read_h5ad(src, backed="r")
        obs = a.obs
        df = obs[[c for c in cols if c in obs.columns]].copy()
        a.file.close()
        df.index.name = "cell"
        return df.astype(object)
    df = obs[[c for c in cols if c in obs.columns]].copy()
    df.index.name = "cell"
    return df.astype(object)


def _persample_frames(unit: Path) -> list[pd.DataFrame]:
    frames = []
    ps_root = L.persample_root(unit)
    for d in L.sample_dirs(unit):
        if _obs_source(d / "clustered.h5ad") is None:
            continue
        o = _obs(d / "clustered.h5ad", ["_ann_coarse", "_ann_fine"])
        o = o.rename(columns={"_ann_coarse": "osp_coarse", "_ann_fine": "osp_fine"})
        o.insert(0, "sample", d.name)
        o.insert(1, "osp_status", "kept")
        frames.append(o)
        rm = d / "qc_removed.csv"
        if rm.is_file():
            r = pd.read_csv(rm, index_col="cell")
            frames.append(pd.DataFrame({"sample": d.name,
                                        "osp_status": REMOVED_PREFIX + r["qc_reason"].astype(str)}, index=r.index))
    if not frames:
        sys.exit(f"no persample outputs under {ps_root}")
    return frames


def _apply_round(ledger: pd.DataFrame, rdir: Path, prefix: str, alive_col: str) -> None:
    """Add one round's msp + zmip column groups in place. alive_col: the
    previous stage's status column (cells 'kept' there enter this round)."""
    idir, zdir = L.crosssample_dir(rdir), L.zoomin_dir(rdir)
    ms, zs = f"{prefix}msp_status", f"{prefix}zmip_status"
    ledger[ms] = np.where(ledger[alive_col] == "kept", "kept", "")
    dec = idir / "sample_decisions.csv"
    if dec.is_file():
        excluded = {r["sample"] for r in csv.DictReader(open(dec)) if r["decision"] == "exclude"}
        ledger.loc[ledger["sample"].isin(list(excluded)) & (ledger[ms] == "kept"), ms] = "excluded-sample"
    rm = idir / "annotation_removed.csv"
    if rm.is_file():
        r = pd.read_csv(rm, index_col="cell")
        agent = r["annotate_remove"].astype(bool) if "annotate_remove" in r else pd.Series(False, index=r.index)
        insp = r["inspect_drop"].astype(bool) if "inspect_drop" in r else pd.Series(False, index=r.index)
        src = np.where(agent, "agent:" + r.get("remove_reason", pd.Series("", index=r.index)).astype(str),
                       np.where(insp, "inspect", "preannotation"))
        idx = ledger.index.intersection(r.index)
        ledger.loc[idx, ms] = (REMOVED_PREFIX + pd.Series(src, index=r.index)).reindex(idx)
    ann = idir / "annotated.h5ad"
    if _obs_source(ann) is not None:
        o = _obs(ann, ["msp_ann_coarse", "msp_ann_fine"]).rename(
            columns={"msp_ann_coarse": f"{prefix}msp_coarse", "msp_ann_fine": f"{prefix}msp_fine"})
        for c in o.columns:
            ledger[c] = o[c].reindex(ledger.index)

    ledger[zs] = np.where(ledger[ms] == "kept", "kept", "")
    plan_p = zdir / "zmip_plan.json"
    if plan_p.is_file() and f"{prefix}msp_coarse" in ledger:
        plan = json.load(open(plan_p))
        not_zoomed = [lab for ln in plan["lineages"] if not ln["zoom"] for lab in ln["coarse_labels"]]
        m = (ledger[zs] == "kept") & ledger[f"{prefix}msp_coarse"].isin(not_zoomed)
        ledger.loc[m, zs] = "not-zoomed"
    rm = zdir / "zmip_removed.csv"
    if rm.is_file():
        r = pd.read_csv(rm, index_col="cell")
        src = np.where(r["annotate_remove"].astype(bool), "agent:" + r["remove_reason"].astype(str), "preannotation")
        idx = ledger.index.intersection(r.index)
        ledger.loc[idx, zs] = (REMOVED_PREFIX + pd.Series(src, index=r.index)).reindex(idx)
    ann = zdir / "annotated_zmip.h5ad"
    if _obs_source(ann) is not None:
        o = _obs(ann, ["zmip_lineage", "zmip_ann_coarse", "zmip_ann_fine"]).rename(
            columns={"zmip_lineage": f"{prefix}zmip_lineage", "zmip_ann_coarse": f"{prefix}zmip_coarse",
                     "zmip_ann_fine": f"{prefix}zmip_fine"})
        for c in o.columns:
            ledger[c] = o[c].reindex(ledger.index)


def build_ledger(unit: Path, round_dirs: list[Path]) -> pd.DataFrame:
    ledger = pd.concat(_persample_frames(unit))
    ledger = ledger[~ledger.index.duplicated(keep="first")]
    alive = "osp_status"
    for i, rdir in enumerate(round_dirs, 1):
        prefix = f"r{i:02d}_"
        _apply_round(ledger, rdir, prefix, alive)
        alive = f"{prefix}zmip_status"
    return ledger


def stage_list(n_rounds: int) -> list[tuple[str, str, str]]:
    """(stage title, label column, status column) for osp + every round."""
    stages = [("per-sample (osp)", "osp_coarse", "osp_status")]
    for i in range(1, n_rounds + 1):
        p = f"r{i:02d}_"
        stages += [(f"round {i} · msp", f"{p}msp_coarse", f"{p}msp_status"),
                   (f"round {i} · zmip", f"{p}zmip_coarse", f"{p}zmip_status")]
    return stages


# ---------------------------------------------------------------- sankey

def _stage_nodes(ledger: pd.DataFrame, label_col: str | None, status_col: str, keep_mask: np.ndarray,
                 pool: bool = True):
    """Per-row node id for one stage: the label for cells still in play,
    'removed: <source>' for cells the stage removed, None for cells already
    gone before this stage. Small labels pooled into 'other' when pool."""
    status = ledger[status_col].astype(str) if status_col in ledger else pd.Series("kept", index=ledger.index)
    node = pd.Series([None] * len(ledger), index=ledger.index, dtype=object)
    gone_here = status.str.startswith(REMOVED_PREFIX).values | (status == "excluded-sample").values
    alive = keep_mask & ~gone_here
    if label_col and label_col in ledger:
        lab = ledger[label_col].astype(object).where(ledger[label_col].notna(), "unlabelled").astype(str)
        node[alive] = lab[alive]
        vc = lab[alive].value_counts()
        small = set(vc[vc < OTHER_MIN_FRAC * max(alive.sum(), 1)].index)
        if pool and len(small) > 1:
            node[alive & lab.isin(small).values] = f"other ({len(small)} labels)"
    else:
        node[alive] = "cells"
    gone = keep_mask & gone_here
    src = status[gone].str.replace(REMOVED_PREFIX, "", regex=False).str.replace(r"^agent:", "", regex=True)
    node[gone] = "removed: " + src
    return node, alive


def _bezier(ax, x0, y0a, y0b, x1, y1a, y1b, color, alpha=0.45):
    dx = (x1 - x0) * 0.5
    verts = [(x0, y0a), (x0 + dx, y0a), (x1 - dx, y1a), (x1, y1a),
             (x1, y1b), (x1 - dx, y1b), (x0 + dx, y0b), (x0, y0b), (x0, y0a)]
    codes = [MPath.MOVETO, MPath.CURVE4, MPath.CURVE4, MPath.CURVE4,
             MPath.LINETO, MPath.CURVE4, MPath.CURVE4, MPath.CURVE4, MPath.CLOSEPOLY]
    ax.add_patch(PathPatch(MPath(verts, codes), facecolor=color, edgecolor="none", alpha=alpha))


def sankey_data(ledger: pd.DataFrame, stages: list[tuple[str, str | None, str]]) -> dict:
    """The Sankey as data, every label kept (no 'other' pooling) so an
    interactive renderer can show the tiny clusters on hover:
    {stages: [title], nodes: [{stage, name, count, removed}], flows: [{src, dst, count}]}
    (src/dst index into nodes)."""
    keep = np.ones(len(ledger), dtype=bool)
    cols = []
    for _, label_col, status_col in stages:
        node, alive = _stage_nodes(ledger, label_col, status_col, keep, pool=False)
        cols.append(node)
        keep = np.asarray(alive)
    nodes, idx = [], {}
    for i, node in enumerate(cols):
        vc = node.value_counts()
        kept = sorted([n for n in vc.index if not n.startswith("removed:")], key=lambda n: -vc[n])
        rm = sorted([n for n in vc.index if n.startswith("removed:")], key=lambda n: -vc[n])
        for n in kept + rm:
            idx[(i, n)] = len(nodes)
            nodes.append({"stage": i, "name": n, "count": int(vc[n]), "removed": n.startswith("removed:")})
    flows = []
    for i in range(len(cols) - 1):
        a, b = cols[i], cols[i + 1]
        m = a.notna() & b.notna() & ~a.astype(str).str.startswith("removed:")
        if not m.any():
            continue
        ct = pd.crosstab(a[m], b[m])
        for s_ in ct.index:
            for d in ct.columns:
                v = int(ct.loc[s_, d])
                if v:
                    flows.append({"src": idx[(i, s_)], "dst": idx[(i + 1, d)], "count": v})
    return {"stages": [t for t, _, _ in stages], "total": int(len(ledger)), "nodes": nodes, "flows": flows}


def draw_sankey(ledger: pd.DataFrame, stages: list[tuple[str, str | None, str]], out_png: Path,
                title: str, min_label_frac: float = 0.012):
    """stages: ordered [(stage title, label column or None, status column)]."""
    keep = np.ones(len(ledger), dtype=bool)
    cols = []
    for _, label_col, status_col in stages:
        node, alive = _stage_nodes(ledger, label_col, status_col, keep)
        cols.append(node)
        keep = np.asarray(alive)
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
    fig, ax = plt.subplots(figsize=(4.2 * n_st + 2, 9))
    gap, bar_w = 0.012, 0.08
    x_pos = [float(i) for i in range(n_st)]
    layout = []
    for i, node in enumerate(cols):
        vc = node.value_counts()
        kept_nodes = sorted([n for n in vc.index if not n.startswith("removed:")], key=lambda n: -vc[n])
        rm_nodes = sorted([n for n in vc.index if n.startswith("removed:")], key=lambda n: -vc[n])
        y, pos = 0.0, {}
        for n in kept_nodes + rm_nodes:
            h = vc[n] / total
            pos[n] = (y, y + h)
            y += h + gap
        layout.append(pos)
        for n, (y0, y1) in pos.items():
            ax.add_patch(plt.Rectangle((x_pos[i], -y1), bar_w, y1 - y0, facecolor=color(n), edgecolor="white", lw=0.5))
            if vc[n] / total >= min_label_frac:
                right = i == n_st - 1
                ax.text(x_pos[i] + bar_w + 0.02 if right else x_pos[i] - 0.02, -(y0 + y1) / 2,
                        f"{n} ({vc[n]})", va="center", ha="left" if right else "right", fontsize=7,
                        color="#7a1f16" if n.startswith("removed:") else "black")
    for i in range(n_st - 1):
        a, b = cols[i], cols[i + 1]
        m = a.notna() & b.notna() & ~a.astype(str).str.startswith("removed:")
        if not m.any():
            continue
        ct = pd.crosstab(a[m], b[m])
        src_off = {n: layout[i][n][0] for n in ct.index}
        dst_off = {n: layout[i + 1][n][0] for n in ct.columns}
        for s_ in ct.index:
            for d in ct.columns:
                v = ct.loc[s_, d]
                if v == 0:
                    continue
                h = v / total
                y0a, y1a = src_off[s_], dst_off[d]
                src_off[s_] += h
                dst_off[d] += h
                _bezier(ax, x_pos[i] + bar_w, -y0a, -(y0a + h), x_pos[i + 1], -y1a, -(y1a + h),
                        color(d) if d.startswith("removed:") else color(s_))
    for i, (name, _, _) in enumerate(stages):
        ax.text(x_pos[i] + bar_w / 2, 0.03, name, ha="center", va="bottom", fontsize=11, fontweight="bold")
    ax.set_xlim(-0.9, x_pos[-1] + bar_w + 0.9)
    ax.set_ylim(-1.15, 0.08)
    ax.axis("off")
    ax.set_title(title, fontsize=12)
    fig.tight_layout()
    fig.savefig(out_png, dpi=150)
    plt.close(fig)


# ---------------------------------------------------------------- entry

def run_ledger(unit: Path, round_dirs: list[Path], out: Path) -> pd.DataFrame:
    out.mkdir(parents=True, exist_ok=True)
    ledger = build_ledger(unit, round_dirs)
    ledger.to_csv(out / "cell_ledger.csv")
    print(f"[ledger] {len(ledger)} cells, {len(round_dirs)} round(s) → {out / 'cell_ledger.csv'}")
    for col in [c for c in ledger.columns if c.endswith("_status")]:
        vc = ledger[col].replace("", np.nan).dropna().value_counts()
        print(f"  {col}: " + ", ".join(f"{k}={v}" for k, v in vc.items()))
    stages = stage_list(len(round_dirs))
    draw_sankey(ledger, stages, out / "sankey_coarse.png", "Cell identity across steps and rounds (coarse labels)")
    with open(out / "sankey_coarse.json", "w") as f:
        json.dump(sankey_data(ledger, stages), f)
    print(f"[sankey] {out / 'sankey_coarse.png'}")
    last = round_dirs[-1]
    p = f"r{len(round_dirs):02d}_"
    plan_p = L.zoomin_dir(last) / "zmip_plan.json"
    if plan_p.is_file() and f"{p}msp_fine" in ledger:
        for ln in json.load(open(plan_p))["lineages"]:
            if not ln["zoom"]:
                continue
            sub = ledger[(ledger[f"{p}msp_status"] == "kept") & ledger[f"{p}msp_coarse"].isin(ln["coarse_labels"])]
            draw_sankey(sub, [(f"round {len(round_dirs)} · msp", f"{p}msp_fine", f"{p}msp_status"),
                              (f"round {len(round_dirs)} · zmip", f"{p}zmip_fine", f"{p}zmip_status")],
                        out / f"sankey_fine_{L.slug(ln['name'])}.png", f"{ln['name']}: fine labels msp → zmip",
                        min_label_frac=0.0)
    return ledger


def default_rounds(unit: Path) -> list[Path]:
    rounds = [r for r in L.rounds(unit) if L.crosssample_dir(r).is_dir()]
    if not rounds:
        sys.exit(f"no started rounds under {L.rounds_root(unit)}")
    return rounds


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="ecarsi.ledger", description=__doc__)
    ap.add_argument("unit", help="organize unit dir")
    ap.add_argument("rounds", nargs="*", help="round dirs in order (each holds crosssample/ and zoomin/)")
    args = ap.parse_args(argv)
    unit = Path(args.unit).resolve()
    round_dirs = [Path(r).resolve() for r in args.rounds] or default_rounds(unit)
    run_ledger(unit, round_dirs, L.ledger_dir(round_dirs[-1]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

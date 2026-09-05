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
        obs = pd.read_csv(src, dtype=str, keep_default_na=False)
        obs = obs.set_index(obs.columns[0])
    else:
        import anndata as ad

        a = ad.read_h5ad(src, backed="r")
        obs = a.obs
        df = obs[[c for c in cols if c in obs.columns]].copy()
        a.file.close()
        df.index.name = "cell"
        _cell_ids(df.index, str(src))
        return df.astype(object)
    df = obs[[c for c in cols if c in obs.columns]].copy()
    df.index.name = "cell"
    _cell_ids(df.index, str(src))
    return df.astype(object)


def _cell_ids(values, source):
    ids = pd.Index(values)
    if ids.hasnans or not ids.is_unique or any(not isinstance(v, str) or not v for v in ids):
        raise ValueError(f"invalid/duplicate cell IDs: {source}")
    return set(ids)


def _table(path, columns):
    if not path.is_file():
        raise ValueError(f"missing ledger: {path}")
    table = pd.read_csv(path, dtype=str, keep_default_na=False)
    if not set(columns) <= set(table.columns):
        raise ValueError(f"missing ledger columns {columns}: {path}")
    if "cell" in columns:
        _cell_ids(table["cell"], str(path))
        table = table.set_index("cell")
    return table


def _partition(expected, survivors, removed, stage):
    kept = _cell_ids(survivors, stage + " survivors")
    gone = _cell_ids(removed, stage + " removed")
    if kept & gone or kept | gone != expected:
        raise ValueError(f"{stage} cell conservation failed: survivors and removals must partition the input")


def _boolean(table, column, path):
    if column not in table:
        return pd.Series(False, index=table.index)
    values = table[column].str.lower()
    if not values.isin(["true", "false"]).all():
        raise ValueError(f"invalid boolean {column}: {path}")
    return values.eq("true")


def _input_ids(path, expected):
    """Pruned input matrices have no obs sidecar; their predecessor is retained."""
    if _obs_source(path) is not None:
        if _cell_ids(_obs(path, []).index, str(path)) != expected:
            raise ValueError(f"input cell set differs from preceding stage: {path}")
    elif not path.with_name(path.name + L.PRUNED_SUFFIX).is_file():
        raise ValueError(f"missing stage input: {path}")


def _persample_frames(unit: Path) -> list[pd.DataFrame]:
    frames = []
    manifest = json.loads(L.persample_manifest(unit).read_text())
    entries = manifest["samples"]
    sample_ids = {L.sample_dir(unit, item).name: item["value"] for item in entries}
    if len(sample_ids) != len(entries) or len(set(sample_ids.values())) != len(entries):
        raise ValueError("duplicate persample manifest samples/directories")
    for item in entries:
        d = L.sample_dir(unit, item)
        o = _obs(d / "clustered.h5ad", ["_ann_coarse", "_ann_fine"])
        r = _table(d / "qc_removed.csv", ["cell", "qc_reason"])
        if r["qc_reason"].eq("").any():
            raise ValueError(f"missing QC removal reason: {d}")
        expected_path = d / "input_cells.csv.gz"
        if expected_path.is_file():
            inputs = _table(expected_path, ["cell_id"])
            expected = _cell_ids(inputs.cell_id, str(expected_path))
            _partition(expected, o.index, r.index, f"OSP {item['value']}")
        else:
            # Older/pruned runs predate per-sample input ID snapshots. Their
            # global union is still checked against organized.h5ad below.
            if set(o.index) & set(r.index):
                raise ValueError(f"OSP survivor/removal overlap: {d}")
        if "n_cells" in item and len(o) + len(r) != int(item["n_cells"]):
            raise ValueError(f"OSP cell count differs from manifest: {d}")
        o = o.rename(columns={"_ann_coarse": "osp_coarse", "_ann_fine": "osp_fine"})
        o.insert(0, "sample", sample_ids[d.name])
        o.insert(1, "osp_status", "kept")
        frames.append(o)
        frames.append(pd.DataFrame({"sample": sample_ids[d.name],
                                    "osp_status": REMOVED_PREFIX + r["qc_reason"]}, index=r.index))
    if not frames:
        raise ValueError(f"no persample outputs under {L.persample_root(unit)}")
    all_ids = pd.concat(frames).index
    actual = _cell_ids(all_ids, "persample ledger")
    if _obs_source(L.input_h5ad(unit)) is not None:
        if actual != set(_obs(L.input_h5ad(unit), []).index):
            raise ValueError("persample ledger does not cover organized input")
    elif any(not (L.sample_dir(unit, item) / "input_cells.csv.gz").is_file() for item in entries):
        raise ValueError("cannot verify legacy persample input without organized.h5ad")
    return frames


def _apply_round(ledger: pd.DataFrame, rdir: Path, prefix: str, alive_col: str) -> None:
    """Require exact stage partitions before assigning any successful status.

    Partial output is an error, never an implicit retained-cell result. Both
    kept and not-zoomed cells survive into the following round.
    """
    idir, zdir = L.crosssample_dir(rdir), L.zoomin_dir(rdir)
    ms, zs = f"{prefix}msp_status", f"{prefix}zmip_status"
    entering = set(ledger.index[ledger[alive_col].isin(["kept", "not-zoomed"])])
    round_input = rdir / L.ROUND_INPUT
    later = alive_col != "osp_status"
    if later:
        _input_ids(round_input, entering)
    excluded = set()
    dec = idir / "sample_decisions.csv"
    if dec.is_file():
        decisions = _table(dec, ["sample", "decision"])
        samples = set(ledger.loc[list(entering), "sample"])
        if decisions["sample"].duplicated().any() or not decisions.decision.isin(["include", "exclude"]).all():
            raise ValueError(f"invalid sample decisions: {dec}")
        if set(decisions["sample"]) != samples:
            raise ValueError(f"sample decisions do not cover entering samples: {dec}")
        excluded_samples = set(decisions.loc[decisions.decision.eq("exclude"), "sample"])
        excluded = entering & set(ledger.index[ledger["sample"].isin(excluded_samples)])
    elif not later:
        raise ValueError(f"missing ledger: {dec}")
    expected = entering - excluded
    _input_ids(idir / "integrated.h5ad", expected)
    removed_path = idir / "annotation_removed.csv"
    removed = _table(removed_path, ["cell"])
    agent = _boolean(removed, "annotate_remove", removed_path)
    inspected = _boolean(removed, "inspect_drop", removed_path)
    reasons = removed.get("remove_reason", pd.Series("", index=removed.index))
    source = np.where(agent, "agent:" + reasons, np.where(inspected, "inspect", "preannotation"))
    obs = _obs(idir / "annotated.h5ad", ["msp_ann_coarse", "msp_ann_fine"])
    _partition(expected, obs.index, removed.index, "MSP")
    for column in ("msp_ann_coarse", "msp_ann_fine"):
        if column not in obs or obs[column].isna().any() or obs[column].eq("").any():
            raise ValueError(f"missing survivor annotation: {column}")
    ledger[ms] = ""
    ledger.loc[list(excluded), ms] = "excluded-sample"
    ledger.loc[obs.index, ms] = "kept"
    ledger.loc[removed.index, ms] = REMOVED_PREFIX + pd.Series(source, index=removed.index)
    for column in obs:
        ledger[prefix + column.replace("msp_ann_", "msp_", 1)] = obs[column].reindex(ledger.index)

    removed_path = zdir / "zmip_removed.csv"
    removed = _table(removed_path, ["cell", "annotate_remove", "remove_reason"])
    agent = _boolean(removed, "annotate_remove", removed_path)
    source = np.where(agent, "agent:" + removed.remove_reason, "preannotation")
    zoom = _obs(zdir / "annotated_zmip.h5ad", ["zmip_lineage", "zmip_ann_coarse", "zmip_ann_fine"])
    _partition(set(obs.index), zoom.index, removed.index, "ZMIP")
    for column in ("zmip_lineage", "zmip_ann_coarse", "zmip_ann_fine"):
        if column not in zoom or zoom[column].isna().any() or zoom[column].eq("").any():
            raise ValueError(f"missing survivor annotation: {column}")
    plan_path = zdir / "zmip_plan.json"
    if not plan_path.is_file():
        raise ValueError(f"missing ZMIP plan: {plan_path}")
    plan = json.loads(plan_path.read_text())
    if any(type(ln["zoom"]) is not bool for ln in plan["lineages"]):
        raise ValueError("ZMIP plan zoom must be boolean")
    not_zoomed = {lab for ln in plan["lineages"] if not ln["zoom"] for lab in ln["coarse_labels"]}
    ledger[zs] = ""
    ledger.loc[zoom.index, zs] = "kept"
    if f"{prefix}msp_coarse" in ledger:
        mask = ledger.index.isin(zoom.index) & ledger[f"{prefix}msp_coarse"].isin(not_zoomed)
        ledger.loc[mask, zs] = "not-zoomed"
    ledger.loc[removed.index, zs] = REMOVED_PREFIX + pd.Series(source, index=removed.index)
    names = {"zmip_lineage": "zmip_lineage", "zmip_ann_coarse": "zmip_coarse", "zmip_ann_fine": "zmip_fine"}
    for column in zoom:
        ledger[prefix + names[column]] = zoom[column].reindex(ledger.index)


def build_ledger(unit: Path, round_dirs: list[Path]) -> pd.DataFrame:
    ledger = pd.concat(_persample_frames(unit))
    if not ledger.index.is_unique:
        raise ValueError("duplicate cell IDs in persample ledger")
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
    status = ledger[status_col].astype(str) if status_col in ledger else pd.Series("", index=ledger.index)
    node = pd.Series([None] * len(ledger), index=ledger.index, dtype=object)
    gone_here = status.str.startswith(REMOVED_PREFIX).values | (status == "excluded-sample").values
    alive = keep_mask & status.isin(["kept", "not-zoomed"]).values
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

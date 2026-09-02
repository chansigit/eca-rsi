"""ecarsi.umapdata — compact UMAP + labels of an h5ad for the landing page's
interactive scatter (release/umap.json).

    python -m ecarsi.umapdata <h5ad> <out.json> [--coarse zmip_ann_coarse] [--fine zmip_ann_fine]

Read with h5py only (no anndata load): coordinates quantised to 16-bit ints
over the bounding box, labels as category indices, one colour per category
(uns['<col>_colors'] when the h5ad carries it, else the stanhue palette if
importable, else evenly spaced hues). No cell ids, no expression: the page
only needs identity per point.
"""

from __future__ import annotations

import argparse
import colorsys
import json
import os
import sys
from pathlib import Path

import h5py
import numpy as np

STANHUE_DIR = os.path.expanduser("~/.claude/skills/stanhue/scripts")
Q = 65535  # quantisation range


def _categorical(f, col):
    g = f["obs"][col]
    if isinstance(g, h5py.Group) and "categories" in g:
        cats = [c.decode() if isinstance(c, bytes) else str(c) for c in g["categories"][()]]
        codes = np.asarray(g["codes"][()], dtype=np.int64)
        return cats, codes
    vals = [v.decode() if isinstance(v, bytes) else str(v) for v in g[()]]
    cats = sorted(set(vals))
    idx = {c: i for i, c in enumerate(cats)}
    return cats, np.array([idx[v] for v in vals], dtype=np.int64)


def _uns_colors(f, col, n):
    key = f"{col}_colors"
    if key in f["uns"]:
        cols = [c.decode() if isinstance(c, bytes) else str(c) for c in f["uns"][key][()]]
        if len(cols) == n:
            return cols
    return None


def _palette(xy, cats, codes):
    try:
        if STANHUE_DIR not in sys.path:
            sys.path.insert(0, STANHUE_DIR)
        from scatter_colormap import assign_celltype_colors  # type: ignore[import-not-found]

        labels = np.array(cats, dtype=object)[np.clip(codes, 0, len(cats) - 1)]
        cmap = assign_celltype_colors(xy, labels)
        return [cmap.get(c, "#999999") for c in cats]
    except Exception:
        n = max(len(cats), 1)
        return ["#%02x%02x%02x" % tuple(int(255 * v) for v in colorsys.hsv_to_rgb((i * 0.618034) % 1, 0.55, 0.85))
                for i in range(n)]


DEFAULT_MAX_POINTS = 100_000
KEEP_ALL_BELOW = 300  # labels with fewer cells than this are never thinned


def _keep_mask(codes: np.ndarray, max_points: int, seed: int = 0) -> np.ndarray:
    """Stratified thinning to about max_points: every cell of a small fine
    label stays (rare populations must remain visible), large labels are
    sampled proportionally. Returns a boolean mask over cells."""
    n = len(codes)
    if n <= max_points:
        return np.ones(n, dtype=bool)
    rng = np.random.default_rng(seed)
    keep = np.zeros(n, dtype=bool)
    counts = np.bincount(codes[codes >= 0])
    small = np.isin(codes, np.flatnonzero(counts < KEEP_ALL_BELOW)) | (codes < 0)
    keep[small] = True
    budget = max_points - int(keep.sum())
    big = np.flatnonzero(~small)
    if budget > 0 and len(big) > budget:
        keep[rng.choice(big, size=budget, replace=False)] = True
    elif budget > 0:
        keep[big] = True
    return keep


def write_umap_json(h5ad: Path, out: Path, coarse_col="zmip_ann_coarse", fine_col="zmip_ann_fine",
                    extra_cols=("zmip_lineage", "sample", "project"), max_points: int = DEFAULT_MAX_POINTS) -> Path:
    with h5py.File(h5ad, "r") as f:
        xy = np.asarray(f["obsm"]["X_umap"][()], dtype=np.float64)[:, :2]
        n_all = len(xy)
        lo, hi = xy.min(axis=0), xy.max(axis=0)
        span = np.where(hi - lo > 0, hi - lo, 1.0)
        q = np.rint((xy - lo) / span * Q).astype(np.int64)
        strat_col = fine_col if fine_col in f["obs"] else coarse_col
        keep = _keep_mask(_categorical(f, strat_col)[1], max_points) if strat_col in f["obs"] else np.ones(n_all, dtype=bool)
        layers = {}
        for name, col in (("coarse", coarse_col), ("fine", fine_col)):
            if col not in f["obs"]:
                continue
            cats, codes = _categorical(f, col)
            colors = _uns_colors(f, col, len(cats)) or _palette(xy, cats, codes)
            counts = np.bincount(codes[codes >= 0], minlength=len(cats)).tolist()  # full counts, not the thinned ones
            layers[name] = {"column": col, "labels": cats, "colors": colors, "counts": counts, "idx": codes[keep].tolist()}
        extra = {}
        for col in extra_cols:
            if col in f["obs"]:
                cats, codes = _categorical(f, col)
                if len(cats) <= 200:
                    extra[col] = {"labels": cats, "idx": codes[keep].tolist()}
    q = q[keep]
    data = {"n": int(len(q)), "n_total": int(n_all), "x": q[:, 0].tolist(), "y": q[:, 1].tolist(),
            "bbox": [float(lo[0]), float(lo[1]), float(hi[0]), float(hi[1])], "layers": layers, "extra": extra,
            "source": str(h5ad)}
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp.json")
    with open(tmp, "w") as fh:
        json.dump(data, fh, separators=(",", ":"))
    os.replace(tmp, out)
    return out


def main(argv):
    ap = argparse.ArgumentParser(prog="ecarsi.umapdata", description=__doc__)
    ap.add_argument("h5ad")
    ap.add_argument("out")
    ap.add_argument("--coarse", default="zmip_ann_coarse")
    ap.add_argument("--fine", default="zmip_ann_fine")
    ap.add_argument("--max-points", type=int, default=DEFAULT_MAX_POINTS,
                    help=f"stratified thinning above this many cells (default {DEFAULT_MAX_POINTS}); small labels are kept whole")
    a = ap.parse_args(argv)
    p = write_umap_json(Path(a.h5ad), Path(a.out), a.coarse, a.fine, max_points=a.max_points)
    print(f"[umapdata] {p} ({p.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

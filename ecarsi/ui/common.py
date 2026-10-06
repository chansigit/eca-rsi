"""What every Periscope page module shares: reading a JSON file that may be missing, the number
formats, the stat tiles and the static assets (ecarsi/ui/static)."""

from __future__ import annotations

import html as _h
import json
from pathlib import Path


def asset(name: str) -> str:
    """The text of a file in ecarsi/ui/static; the pages inline their CSS and JS."""
    return (Path(__file__).with_name("static") / name).read_text(encoding="utf-8")


SANKEY_JS = asset("sankey.js")


UMAP_JS = asset("umap.js")


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


def _pct(x) -> str:
    return f"{100 * x:.2f}%"


def _n(x) -> str:
    return "" if x is None or x == "" else f"{int(x):,}"


def _k(x) -> str:
    """Thousands, two decimals: a column of counts is read for its size, and 201.28k compares
    at a glance where 201,278 has to be counted. Rounded, so 1,017 is 1.02k."""
    return "" if x is None or x == "" else f"{int(x) / 1000:.2f}k"


def _bar(frac: float) -> str:
    return f'<span class="bar" title="{_pct(frac)}"><i style="width:{min(100, 100 * frac):.1f}%"></i></span>'


def _stat(v, k, sub="", cls="") -> str:
    return (f'<div class="stat {cls}"><span class="v">{v}</span><span class="k">{_h.escape(k)}</span>'
            + (f'<span class="sub">{sub}</span>' if sub else "") + "</div>")


def _when(ts: float | None) -> str:
    import time

    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else ""


EXPLAIN = {
    "rounds": "Each round re-embeds the surviving cells across samples (msp), re-annotates them and zooms into each lineage (zmip), "
              "then removes what the agents judged low quality. The loop stops when a round removes less than 1 % (or fewer than 100 cells).",
    "samples": "OSP runs QC, clustering and a first annotation once per sample; in round 1 an agent decides which samples enter "
               "integration. Excluded and emptied samples stay on disk untouched.",
    "sankey": "Every input cell flows left to right through per-sample QC and each round's msp and zmip step, coloured by coarse label; "
              "a ribbon ending in a red sink is the cells removed at that step. Hover a bar or a ribbon for counts.",
    "umap": "The released embedding with the final coarse and fine labels. Hover a point for its labels; scroll to zoom, drag to pan, "
            "double-click to reset; click a legend entry to isolate one label. The two panels stay in sync.",
    "review": "Everything the agents were unsure about or the host overrode, grouped by category. Nothing here stopped the loop; "
              "removals are irreversible, the rest is advisory.",
    "files": "Where the results live on the server host. The h5ad carries the final labels in obs columns "
             "<code>zmip_ann_coarse</code> and <code>zmip_ann_fine</code>.",
    "units": "One analysis unit is one merged dataset (one species) run through the loop on its own. Open a unit for its rounds, "
             "final UMAP and files.",
}

"""zmip.lineage — one lineage's working subset (subset_for: the lineage's cells with last round's labels carried
as *_prev columns) and its directory name (lineage_dir). eca-rsi's zoom-in stage re-embeds, scores and annotates
each lineage in its own pool tasks; the standalone lineage runner (subprocess pool, resume, CLI) was removed
with #28.
"""

from __future__ import annotations

import logging
import os


from .annotate import PREV_SUFFIX, PREVIOUS_COLS
from .plan import _lineage_slugs

log = logging.getLogger(__name__)


def lineage_dir(outdir, name):
    directory = os.path.join(outdir, _lineage_slugs([name])[name])
    root = os.path.realpath(outdir)
    resolved = os.path.realpath(directory)
    if resolved == root or os.path.commonpath([root, resolved]) != root:
        raise ValueError(f"lineage directory escapes output directory: {directory!r}")
    return directory


def subset_for(ad, labels, coarse_col, fine_col):
    """The lineage's cells with last round's labels carried as *_prev columns."""
    selected = ad[ad.obs[coarse_col].astype(str).isin(labels).values]
    sub = selected.to_memory() if selected.isbacked else selected.copy()
    for c in PREVIOUS_COLS:
        src = {"msp_ann_coarse": coarse_col, "msp_ann_fine": fine_col}[c]
        sub.obs[c + PREV_SUFFIX] = sub.obs[src].astype(str).astype("category")
    return sub


# ---------------------------------------------------------------- the pool


# ---------------------------------------------------------------- subprocess entry

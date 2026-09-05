"""
OSP HTML report generator

Bundles everything a single-sample-pipeline (OSP) run produces into one
self-contained HTML file. Two audiences at once:

  - Human: images are embedded inline (base64, no broken links when the file
    is moved/emailed), CSS gives a clean readable layout, long tables are
    collapsed behind <details> per cluster so the page isn't a wall of text,
    a top nav jumps between sections. Related single-metric images (one file
    per plot — see osp.cluster/osp.qc, no scanpy multi_panel
    figures) are tiled into a CSS grid here so a family of plots (e.g. every
    QC metric on UMAP) still reads as one coherent view.
  - Agent: every number that appears in a figure also appears in a plain
    <table> next to it, so an agent reading the HTML source doesn't have to
    OCR a PNG to get exact values. Section headers are plain <h2>/<h3> text,
    not icons, so grepping/parsing the raw HTML is enough — no JS rendering
    required. Each plot being its own file also means an agent inspecting
    just the figures/ directory can read one signal at a time instead of
    parsing a combined multi-panel image.

Section order:
  1. QC summary        — which cells were kept/dropped and why (cell-level)
  2. Clusters           — structure: UMAP+PAGA, per-resolution summary +
                           connectivity tables
  3. QC UMAP            — QC metrics overlaid on the embedding, QC-by-cluster
                           violins (needs clusters from step 2)
  4. Ambient Contamination — DecontX: sample-wide top genes, then the same
                           signal broken down by cluster (needs step 2)
  5. Cluster Identities — marker scores on UMAP + top DE genes per cluster,
                           i.e. what each cluster actually *is*

Usage:
    python -m osp.report /path/to/osp_out [--out report.html]

    # or from Python, right after run_one_sample_pipeline(..., outdir="osp_out/MN"):
    from osp import generate_report
    generate_report("osp_out/MN")
"""

import argparse
import base64
import glob
import html
import json
import math
import os

import pandas as pd

from ._io import atomic_write_text

# Fixed display order for the well-known QC metric suffixes — anything not
# in this list still shows up, just after these, alphabetically.
_QC_METRIC_ORDER = [
    "total_counts",
    "n_genes_by_counts",
    "pct_counts_mt",
    "counts_vs_mt",
    "doublet_score",
    "decontX_contamination",
    "decontx_contamination",
    "pct_counts_malat1",
    "dissociation_score",
]


def _img_data_uri(path):
    ext = os.path.splitext(path)[1].lstrip(".") or "png"
    with open(path, "rb") as fh:
        b64 = base64.b64encode(fh.read()).decode("ascii")
    return f"data:image/{ext};base64,{b64}"


def _fmt(v):
    """Compact cell text: integral floats print as integers, large values
    keep their digits (no 1.2e+04 for cell counts), tiny p-values go to
    scientific notation, everything else to 4 significant digits."""
    if isinstance(v, float):
        if math.isnan(v):
            return ""
        if v == int(v) and abs(v) < 1e15:
            return str(int(v))
        if abs(v) < 1e-3:
            return f"{v:.3e}"
        if abs(v) >= 1e4:
            return f"{v:.1f}"
        return f"{v:.4g}"
    return html.escape(str(v))


def _coerce_scalar(text):
    """CSV cell text -> int / float when it parses, else the original string;
    qc_summary.csv holds mixed types in one column, so pandas reads it as
    object and the numbers would otherwise render unformatted."""
    if not isinstance(text, str):
        return text
    stripped = text.strip()
    for parse in (int, float):
        try:
            return parse(stripped)
        except ValueError:
            continue
    return text


def _df_to_table(df, index_name=None, table_id=None):
    cols = ([index_name] if index_name else []) + list(df.columns)
    id_attr = f' id="{table_id}"' if table_id else ""
    head = "".join(f"<th>{html.escape(str(c))}</th>" for c in cols)
    rows = []
    for idx, row in df.iterrows():
        cells = ""
        if index_name:
            cells += f"<td>{html.escape(str(idx))}</td>"
        cells += "".join(f"<td>{_fmt(v)}</td>" for v in row)
        rows.append(f"<tr>{cells}</tr>")
    return f"<table{id_attr}><thead><tr>{head}</tr></thead><tbody>{''.join(rows)}</tbody></table>"


def _kv_table(d, table_id=None):
    id_attr = f' id="{table_id}"' if table_id else ""
    rows = "".join(f"<tr><td>{html.escape(str(k))}</td><td>{_fmt(v)}</td></tr>" for k, v in d.items())
    return f"<table{id_attr}><tbody>{rows}</tbody></table>"


def _heatmap_cell(value, vmax, decimals=2, color=(37, 99, 235)):
    """One <td> for a numeric matrix, background shaded by value/vmax (0 ->
    white, vmax -> solid `color`) — cell shading substitutes for scanning
    raw numbers, so the table doubles as a heatmap without a separate image."""
    alpha = 0 if vmax <= 0 else max(0.0, min(1.0, float(value) / vmax))
    r, g, b = color
    text_color = "#fff" if alpha > 0.55 else "inherit"
    style = f"background-color: rgba({r},{g},{b},{alpha:.3f}); color: {text_color};"
    return f'<td style="{style}">{value:.{decimals}f}</td>'


def _df_to_heatmap_table(df, index_name=None, table_id=None, decimals=2, color=(37, 99, 235)):
    """Like _df_to_table but for a numeric matrix (e.g. PAGA connectivities):
    values rounded to `decimals` and each cell shaded by magnitude."""
    vmax = float(df.to_numpy(dtype=float).max()) if df.size else 0.0
    cols = ([index_name] if index_name else []) + list(df.columns)
    id_attr = f' id="{table_id}"' if table_id else ""
    head = "".join(f"<th>{html.escape(str(c))}</th>" for c in cols)
    rows = []
    for idx, row in df.iterrows():
        cells = f"<td>{html.escape(str(idx))}</td>" if index_name else ""
        cells += "".join(_heatmap_cell(v, vmax, decimals, color) for v in row)
        rows.append(f"<tr>{cells}</tr>")
    return f"<table{id_attr}><thead><tr>{head}</tr></thead><tbody>{''.join(rows)}</tbody></table>"


def _img(path, alt):
    return f'<img class="fig" src="{_img_data_uri(path)}" alt="{html.escape(alt)}">' if os.path.exists(path) else ""


def _sort_by_metric_suffix(paths, suffix_of):
    def key(p):
        suffix = suffix_of(p)
        try:
            return (0, _QC_METRIC_ORDER.index(suffix))
        except ValueError:
            return (1, suffix)

    return sorted(paths, key=key)


def _img_grid(paths, suffix_of, grid_id=None):
    """Tile a family of single-metric plots (one file per metric, see module
    docstring) into a CSS grid, each with a small text caption — the caption
    is the exact obs-column/plot suffix, so an agent can match a tile to a
    number elsewhere in the report without OCR-ing the image title."""
    if not paths:
        return ""
    paths = _sort_by_metric_suffix(paths, suffix_of)
    id_attr = f' id="{grid_id}"' if grid_id else ""
    items = "".join(
        f'<figure class="grid-item">{_img(p, suffix_of(p))}'
        f"<figcaption>{html.escape(suffix_of(p))}</figcaption></figure>"
        for p in paths
    )
    return f'<div class="grid"{id_attr}>{items}</div>'


def _read_qc_summary(outdir):
    path = os.path.join(outdir, "qc_summary.csv")
    if not os.path.exists(path):
        return None
    s = pd.read_csv(path, index_col=0, dtype=str, keep_default_na=False).iloc[:, 0]
    return {key: _coerce_scalar(value) for key, value in s.items()}


def _read_qc_stats(outdir):
    """QC overview JSON written by ``qc_one_sample`` under ``qc_figures``."""
    json_paths = glob.glob(os.path.join(outdir, "qc_figures", "*_qc_overview.json"))
    if not json_paths:
        return None
    if len(json_paths) > 1:
        raise RuntimeError(
            f"multiple QC overview files in {outdir!r}; use one output directory per sample: "
            f"{sorted(os.path.basename(path) for path in json_paths)}"
        )
    with open(json_paths[0], encoding="utf-8") as fh:
        return json.load(fh)


def _find_cluster_tables(outdir):
    """Returns list of (resolution_key, cluster_summary_df, de_df) sorted by resolution key.
    cluster_and_deg only computes a cluster_summary (and DE/PAGA/DecontX) for
    the primary resolution, so in practice this list has exactly one entry —
    that entry's key IS the primary resolution, used elsewhere to find the
    matching umap_clusters_{key}.png / paga_connectivities_{key}.csv etc."""
    summary_paths = sorted(glob.glob(os.path.join(outdir, "cluster_summary_leiden_r*.csv")))
    if len(summary_paths) > 1:
        raise RuntimeError(
            f"multiple primary cluster summaries in {outdir!r}; rerun clustering to remove "
            f"stale tables: {[os.path.basename(path) for path in summary_paths]}"
        )
    results = []
    for sp in summary_paths:
        key = os.path.basename(sp)[len("cluster_summary_") : -len(".csv")]
        summary_df = pd.read_csv(sp, index_col=0)
        de_path = os.path.join(outdir, f"de_top_genes_{key}.csv")
        de_df = pd.read_csv(de_path) if os.path.exists(de_path) else None
        results.append((key, summary_df, de_df))
    return results


def _cluster_detail_blocks(df, group_col, n_cells_by_cluster, label):
    """Shared renderer for the per-cluster <details> blocks used by both the
    DE-genes table and the DecontX-top-genes-per-cluster table — same shape
    (long-format df with a grouping column), same collapsed-by-default UI."""
    parts = []
    for group, sub in df.groupby(group_col):
        sub_display = sub.drop(columns=[group_col])
        n_cells = n_cells_by_cluster.get(str(group), "?")
        parts.append(
            f"<details><summary>cluster {html.escape(str(group))} "
            f"(n_cells={n_cells}, {len(sub_display)} {label})</summary>"
            f"{_df_to_table(sub_display)}</details>"
        )
    return "\n".join(parts)


def _section_qc(outdir, qc_summary, qc_stats):
    """1. QC summary — cell-level filtering only (hard thresholds / MAD outliers /
    doublets). Ambient contamination is monitored separately (section 4) and
    does not affect this pass/fail call."""
    if qc_summary is None:
        return ""
    parts = [
        '<h2 id="qc">QC summary</h2>',
        '<p class="hint">Per-cell filtering: hard thresholds + MAD outliers + doublet detection. See "Ambient Contamination" below for DecontX, which is monitored separately.</p>',
        _kv_table(qc_summary, table_id="qc-summary"),
    ]

    qc_pngs = glob.glob(os.path.join(outdir, "qc_figures", "*_qc_*.png"))

    def suffix_of(p):
        name = os.path.basename(p)[: -len(".png")]
        idx = name.rfind("_qc_")
        return name[idx + len("_qc_") :] if idx != -1 else name

    grid = _img_grid(qc_pngs, suffix_of, grid_id="qc-grid")
    if grid:
        parts.append(grid)

    if qc_stats:
        parts.append("<h3>QC thresholds</h3>")
        parts.append(_kv_table(qc_stats.get("thresholds", {}), table_id="qc-thresholds"))
        mad_ranges = qc_stats.get("mad_keep_ranges")
        if mad_ranges:
            nmads = qc_stats.get("mad_nmads", "?")
            n_fail = qc_stats.get("n_fail_mad", {})
            parts.append(f"<h3>MAD keep-ranges (median ± {nmads} MADs, per sample)</h3>")
            parts.append(
                '<p class="hint">Cells outside any range are flagged mad_outlier. '
                "A tight range (or one metric dominating the fail counts) means this "
                "sample's distribution shape is stressing the MAD assumption — check "
                "which cell types are being flagged before trusting the calls.</p>"
            )
            parts.append(
                _kv_table(
                    {k: f"[{v[0]:g}, {v[1]:g}]  (n_fail={n_fail.get(k, '?')})" for k, v in mad_ranges.items()},
                    table_id="qc-mad-ranges",
                )
            )
    return "\n".join(p for p in parts if p)


def _section_clusters(outdir, cluster_tables):
    """2. Clustering structure: UMAP+PAGA side by side, per-resolution summary
    + connectivity tables. Just the structure — QC/contamination/identity
    diagnostics for these same clusters are in their own sections below."""
    if not cluster_tables:
        return ""
    parts = ['<h2 id="clusters">Clusters</h2>']

    primary_key = cluster_tables[0][0]
    # primary-resolution cluster UMAP and PAGA connectivity graph side by
    # side — same clusters, two views (spatial layout vs. abstracted
    # connectivity). Both saved at matching figsize/axes-rect (see
    # osp.cluster._UMAP_FIGSIZE/_UMAP_AXES_RECT) so the two panels line up.
    row_imgs = []
    for fname, alt, caption in [
        (f"umap_clusters_{primary_key}.png", "Clusters UMAP", f"Clusters ({primary_key})"),
        ("paga.png", "PAGA graph", "PAGA connectivity"),
    ]:
        img = _img(os.path.join(outdir, "figures", fname), alt)
        if img:
            row_imgs.append(f"<div><h3>{html.escape(caption)}</h3>{img}</div>")
    if row_imgs:
        parts.append('<div class="row">' + "".join(row_imgs) + "</div>")

    # any other resolutions computed (resolutions= had more than one entry)
    other_umaps = sorted(
        p
        for p in glob.glob(os.path.join(outdir, "figures", "umap_clusters_*.png"))
        if os.path.basename(p) != f"umap_clusters_{primary_key}.png"
    )
    if other_umaps:
        parts.append("<h3>Other resolutions</h3>")
        parts.append(_img_grid(other_umaps, lambda p: os.path.basename(p)[len("umap_clusters_") : -len(".png")]))

    conn_paths = {
        os.path.basename(p)[len("paga_connectivities_") : -len(".csv")]: p
        for p in glob.glob(os.path.join(outdir, "paga_connectivities_*.csv"))
    }
    for key, summary_df, _ in cluster_tables:
        parts.append(f"<h3>{html.escape(key)}: cluster summary</h3>")
        parts.append(_df_to_table(summary_df, index_name=summary_df.index.name or "cluster"))
        if key in conn_paths:
            conn_df = pd.read_csv(conn_paths[key], index_col=0)
            parts.append("<h4>PAGA connectivity matrix</h4>")
            parts.append(_df_to_heatmap_table(conn_df, index_name=conn_df.index.name or "cluster"))

    return "\n".join(parts)


def _section_qc_umap(outdir):
    """3. QC metrics revisited on the clustered embedding — same numbers as
    section 1, now spatially/by-cluster instead of sample-wide histograms."""
    parts = []

    umap_qc_pngs = glob.glob(os.path.join(outdir, "figures", "umap_qc_*.png"))
    grid = _img_grid(
        umap_qc_pngs, lambda p: os.path.basename(p)[len("umap_qc_") : -len(".png")], grid_id="umap-qc-grid"
    )
    if grid:
        parts.append("<h3>QC metrics on UMAP</h3>")
        parts.append(grid)

    violin_pngs = glob.glob(os.path.join(outdir, "figures", "qc_violin_*.png"))
    grid = _img_grid(
        violin_pngs, lambda p: os.path.basename(p)[len("qc_violin_") : -len(".png")], grid_id="qc-violin-grid"
    )
    if grid:
        parts.append("<h3>QC metrics by cluster</h3>")
        parts.append(grid)

    if not parts:
        return ""
    return '<h2 id="qc-umap">QC UMAP</h2>\n' + "\n".join(parts)


def _section_contamination(outdir, cluster_tables, qc_summary=None):
    """4. DecontX ambient RNA — sample-wide top genes first, then the same
    signal broken down by the real clusters from section 2 (which cluster,
    which gene)."""
    sample_png = glob.glob(os.path.join(outdir, "qc_figures", "*_decontx_top_genes.png"))
    sample_csv = glob.glob(os.path.join(outdir, "qc_figures", "*_decontx_top_genes.csv"))
    heatmap = _img(
        os.path.join(outdir, "figures", "decontx_heatmap_by_cluster.png"),
        "DecontX gene x cluster heatmap",
    )
    decontx_paths = {
        os.path.basename(p)[len("decontx_top_genes_") : -len(".csv")]: p
        for p in glob.glob(os.path.join(outdir, "decontx_top_genes_*.csv"))
    }
    if not sample_png and not sample_csv and not heatmap and not decontx_paths:
        return ""

    parts = ['<h2 id="contamination">Ambient Contamination</h2>']

    qc_summary = qc_summary or {}
    if str(qc_summary.get("decontx_degenerate")).lower() == "true":
        parts.append(
            '<div class="warn">DecontX fit flagged as degenerate: the estimates stayed '
            "pinned near complete contamination, so the contamination fraction was excluded "
            "from the PCA covariates. The values below are kept for inspection only; do not "
            "read them as an ambient-RNA measurement.</div>"
        )
    z_source = qc_summary.get("decontx_z_source")
    if z_source is not None:
        z_text = (
            "caller-supplied cell groups"
            if str(z_source) == "user"
            else "OSP's coarse Leiden clustering of this sample"
        )
        parts.append(
            f'<p class="hint">DecontX was initialized with {z_text} '
            f"(<code>decontx_z_source={html.escape(str(z_source))}</code>).</p>"
        )

    if sample_png or sample_csv:
        parts.append("<h3>Sample-wide</h3>")
        parts.append(
            '<p class="hint">Top genes ranked by total UMIs judged ambient RNA '
            "(raw counts - decontX_counts, summed over all cells).</p>"
        )
        if sample_png:
            parts.append(_img(sample_png[0], "DecontX top genes"))
        if sample_csv:
            parts.append(_df_to_table(pd.read_csv(sample_csv[0])))

    if heatmap or decontx_paths:
        parts.append("<h3>By cluster</h3>")
        parts.append(
            '<p class="hint">Fraction of each gene\'s UMIs judged ambient RNA, per cluster. '
            'A gene that\'s "contaminated" in one cluster but not another is a strong signal '
            "it's real biology there and ambient noise elsewhere (e.g. neutrophil transcripts "
            "leaking into non-myeloid clusters).</p>"
        )
        if heatmap:
            parts.append(heatmap)
        for key, summary_df, _ in cluster_tables:
            if key not in decontx_paths:
                continue
            n_cells_by_cluster = dict(zip(summary_df.index.astype(str), summary_df["n_cells"], strict=True))
            decontx_df = pd.read_csv(decontx_paths[key])
            parts.append(f"<h4>{html.escape(key)}: top contaminating genes per cluster</h4>")
            parts.append(_cluster_detail_blocks(decontx_df, "cluster", n_cells_by_cluster, "contaminating genes"))

    return "\n".join(parts)


def _section_agent_annotation(outdir, proposal=None):
    """6. Present only when osp.annotate has been run: agent-proposed cluster
    annotations (coarse = lineage-level, fine = detailed) + standardized QC
    actions. A proposal for human review, not an applied annotation."""
    if proposal is None:
        path = os.path.join(outdir, "annotation_proposal.json")
        if not os.path.exists(path):
            return ""
        with open(path, encoding="utf-8") as fh:
            proposal = json.load(fh)

    parts = [
        '<h2 id="agent-annotation">Agent Annotation</h2>',
        (
            '<p class="hint">Proposed by the annotation agent (osp.annotate) — a proposal for '
            "human review, not an applied annotation. Full reasoning in annotation_notes.md.</p>"
        ),
    ]

    # coarse/fine annotation UMAPs side by side — same figsize/axes rect as
    # every other UMAP panel, so they line up
    row_imgs = []
    for fname, caption in [
        ("umap_ann_coarse.png", "Annotation (coarse, lineage-level)"),
        ("umap_ann_fine.png", "Annotation (fine)"),
    ]:
        img = _img(os.path.join(outdir, "figures", fname), caption)
        if img:
            row_imgs.append(f"<div><h3>{html.escape(caption)}</h3>{img}</div>")
    if row_imgs:
        parts.append('<div class="row">' + "".join(row_imgs) + "</div>")

    qc_img = _img(os.path.join(outdir, "figures", "umap_qc_action.png"), "Proposed QC action")
    if qc_img:
        parts.append("<h3>Proposed QC actions on UMAP</h3>")
        parts.append(
            '<p class="hint">Dark red = cells proposed to <b>drop</b>, dark yellow = '
            "<b>flagged</b> for review, light gray = keep.</p>"
        )
        parts.append(qc_img)

    entries = proposal.get("clusters") or []
    if entries:
        df = pd.DataFrame(entries)
        cols = [
            c
            for c in ("cluster", "label_coarse", "label_fine", "label", "confidence", "evidence_genes", "doubts")
            if c in df.columns
        ]
        df = df[cols]
        if "evidence_genes" in df.columns:
            df["evidence_genes"] = df["evidence_genes"].map(lambda g: ", ".join(g) if isinstance(g, list) else g)
        parts.append("<h3>Proposed cluster labels</h3>")
        parts.append(_df_to_table(df, table_id="agent-labels"))

    # standardized machine-appliable action records (v2 schema); fall back to
    # the legacy free-text clusters_to_flag if an old proposal is present
    actions = proposal.get("qc_actions") or []
    legacy_qc = proposal.get("qc_recommendations") or {}
    if actions:
        adf = pd.DataFrame(actions)
        cols = [
            c for c in ("cluster", "scope", "action", "metric", "op", "value", "reason", "note") if c in adf.columns
        ]
        parts.append("<h3>Proposed QC actions</h3>")
        parts.append(_df_to_table(adf[cols], table_id="agent-qc-flags"))
    elif legacy_qc.get("clusters_to_flag"):
        fdf = pd.DataFrame(legacy_qc["clusters_to_flag"])
        cols = [c for c in ("cluster", "issue", "action", "rationale") if c in fdf.columns]
        parts.append("<h3>QC flags</h3>")
        parts.append(_df_to_table(fdf[cols], table_id="agent-qc-flags"))

    thresholds = proposal.get("threshold_suggestions") or legacy_qc.get("threshold_suggestions") or []
    if thresholds:
        parts.append("<h3>Threshold suggestions</h3>")
        parts.append("<ul>" + "".join(f"<li>{html.escape(str(t))}</li>" for t in thresholds) + "</ul>")
    overall = proposal.get("overall") or legacy_qc.get("overall")
    if overall:
        parts.append("<h3>Overall assessment</h3>")
        parts.append(f'<p class="hint">{html.escape(str(overall))}</p>')

    notes_path = os.path.join(outdir, "annotation_notes.md")
    if os.path.exists(notes_path):
        with open(notes_path, encoding="utf-8") as fh:
            notes = fh.read()
        parts.append(
            "<details><summary>Agent notes (full reasoning)</summary>"
            f'<pre class="notes">{html.escape(notes)}</pre></details>'
        )

    return "\n".join(parts)


def _section_cluster_identities(outdir, cluster_tables, top_n_de_display):
    """5. What each cluster actually is: marker scores on UMAP (if a
    marker_genes set was given) + top DE genes per cluster."""
    parts = []

    marker_pngs = glob.glob(os.path.join(outdir, "figures", "umap_markers_*.png"))
    grid = _img_grid(
        marker_pngs, lambda p: os.path.basename(p)[len("umap_markers_") : -len(".png")], grid_id="markers-grid"
    )
    if grid:
        parts.append("<h3>Marker scores on UMAP</h3>")
        parts.append(grid)

    for key, summary_df, de_df in cluster_tables:
        if de_df is None:
            continue
        n_cells_by_cluster = dict(zip(summary_df.index.astype(str), summary_df["n_cells"], strict=True))
        de_display = de_df.groupby("group", observed=True).head(top_n_de_display)
        parts.append(f"<h3>{html.escape(key)}: top DE genes per cluster</h3>")
        parts.append(_cluster_detail_blocks(de_display, "group", n_cells_by_cluster, "genes"))

    if not parts:
        return ""
    return '<h2 id="cluster-identities">Cluster Identities</h2>\n' + "\n".join(parts)


CSS = """
body { font-family: -apple-system, Helvetica, Arial, sans-serif; margin: 2rem auto; max-width: 1400px; color: #1a1a1a; padding: 0 1rem; }
h1 { border-bottom: 2px solid #333; padding-bottom: .3rem; margin-bottom: .3rem; }
h2 { margin-top: 3rem; border-bottom: 1px solid #ccc; padding-bottom: .2rem; scroll-margin-top: 1rem; }
h3 { margin-top: 1.5rem; }
h4 { margin-top: 1rem; }
table { border-collapse: collapse; margin: .5rem 0 1rem 0; font-size: .9rem; }
th, td { border: 1px solid #ddd; padding: .3rem .6rem; text-align: right; }
th:first-child, td:first-child { text-align: left; }
thead th { background: #f2f2f2; }
tr:nth-child(even) { background: #fafafa; }
img.fig { max-width: 100%; display: block; border: 1px solid #ddd; }
.row { display: flex; gap: 1.5rem; flex-wrap: wrap; align-items: flex-start; }
.row > div { flex: 1 1 45%; min-width: 320px; }
.row img.fig { width: 100%; }
.grid { display: flex; flex-wrap: wrap; gap: 1.2rem; margin: .5rem 0 1rem 0; }
.grid-item { flex: 0 0 260px; margin: 0; }
.grid-item img.fig { width: 260px; }
.grid-item figcaption { font-size: .78rem; color: #555; text-align: center; margin-top: .25rem; }
details { margin: .3rem 0; }
summary { cursor: pointer; font-weight: 600; }
#agent-labels td, #agent-qc-flags td { text-align: left; vertical-align: top; }
pre.notes { white-space: pre-wrap; font-size: .85rem; background: #f7f7f7; padding: .8rem; border-radius: 6px; }
.meta { color: #666; font-size: .9rem; margin: .2rem 0 1rem 0; }
.hint { color: #555; font-size: .88rem; margin: .3rem 0 1rem 0; max-width: 75ch; }
.warn { background: #fdecea; border: 1px solid #c0392b; border-radius: 4px; padding: .5rem .8rem; margin: .5rem 0; max-width: 75ch; }
.layout { display: flex; gap: 2rem; align-items: flex-start; }
.content { flex: 1; min-width: 0; }
nav.toc { position: sticky; top: 1rem; z-index: 10; flex: 0 0 200px;
          display: flex; flex-direction: column; gap: .5rem;
          background: #f7f7f7; border: 1px solid #e0e0e0; border-radius: 6px;
          padding: .8rem 1rem; font-size: .9rem;
          box-shadow: 0 2px 4px rgba(0,0,0,.06); }
nav.toc a { color: #24578a; text-decoration: none; }
nav.toc a:hover { text-decoration: underline; }
@media (max-width: 800px) {
  .layout { flex-direction: column; }
  nav.toc { position: static; flex-direction: row; flex-wrap: wrap; width: auto; }
}
"""


_SECTION_LABELS = {
    "qc": "QC summary",
    "clusters": "Clusters",
    "qc-umap": "QC UMAP",
    "contamination": "Ambient Contamination",
    "cluster-identities": "Cluster Identities",
    "agent-annotation": "Agent Annotation",
}


def _number_sections(section_htmls):
    """Sections are written unnumbered ("<h2 id=X>Label</h2>"); number them
    sequentially here based on what actually rendered, so a missing section
    (e.g. no DecontX run, no marker_genes) doesn't leave a "1, 3, 5" gap.
    Returns (numbered_section_htmls, toc_html).
    """
    present = [
        (anchor, label)
        for anchor, label in _SECTION_LABELS.items()
        if any(f'<h2 id="{anchor}">{label}</h2>' in s for s in section_htmls)
    ]
    numbered = {anchor: f"{i}. {label}" for i, (anchor, label) in enumerate(present, start=1)}

    numbered_htmls = []
    for s in section_htmls:
        for anchor, label in present:
            s = s.replace(f'<h2 id="{anchor}">{label}</h2>', f'<h2 id="{anchor}">{numbered[anchor]}</h2>', 1)
        numbered_htmls.append(s)

    toc = "".join(f'<a href="#{anchor}">{html.escape(numbered[anchor])}</a>' for anchor, _ in present)
    return numbered_htmls, (f'<nav class="toc">{toc}</nav>' if toc else "")


CONTEXT_FILE = "report_context.txt"


def report_context(outdir):
    """Where this sample sits (e.g. the analysis unit name), written by the
    driver via --report-context into outdir; '' when absent."""
    p = os.path.join(outdir, CONTEXT_FILE)
    if os.path.isfile(p):
        with open(p, encoding="utf-8") as f:
            return f.read().strip()
    return ""


def write_report_context(outdir, text):
    if text:
        atomic_write_text(os.path.join(outdir, CONTEXT_FILE), text.strip() + "\n")


def generate_report(
    outdir,
    out_html=None,
    title=None,
    top_n_de_display=10,
    annotation_proposal=None,
):
    """Build a single self-contained HTML report from an OSP output directory
    (whatever cluster_and_deg / run_pipeline wrote with outdir=...).

    Returns the path to the written HTML file.
    """
    outdir = os.fspath(outdir)
    out_html = os.fspath(out_html) if out_html else os.path.join(outdir, "report.html")

    qc_summary = _read_qc_summary(outdir)
    qc_stats = _read_qc_stats(outdir)
    cluster_tables = _find_cluster_tables(outdir)

    sample = (qc_summary or {}).get("sample") or (qc_stats or {}).get("sample") or os.path.basename(outdir.rstrip("/"))
    ctx = report_context(outdir)
    title = title or f"{sample} — per-sample QC & clustering (osp)" + (f" · {ctx}" if ctx else "")

    sections = [
        _section_qc(outdir, qc_summary, qc_stats),
        _section_clusters(outdir, cluster_tables),
        _section_qc_umap(outdir),
        _section_contamination(outdir, cluster_tables, qc_summary),
        _section_cluster_identities(outdir, cluster_tables, top_n_de_display),
        _section_agent_annotation(outdir, proposal=annotation_proposal),
    ]
    sections, toc = _number_sections(sections)

    header = f'<h1>{html.escape(title)}</h1><p class="meta">source dir: {html.escape(os.path.abspath(outdir))}</p>'
    body = f'{header}<div class="layout">{toc}<div class="content">{"".join(sections)}</div></div>'

    html_doc = (
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{html.escape(title)}</title><style>{CSS}</style></head>"
        f"<body>{body}</body></html>"
    )

    atomic_write_text(out_html, html_doc)

    return out_html


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("outdir")
    parser.add_argument("--out", default=None)
    parser.add_argument("--top-n-de", type=int, default=10)
    args = parser.parse_args()

    path = generate_report(args.outdir, out_html=args.out, top_n_de_display=args.top_n_de)
    print(f"wrote {path}")

"""python -m osp: single-sample QC → clustering → DEG → HTML report, end to end.

With --annotate, the annotation agent (osp.annotate, needs the optional
agent dependencies) runs afterwards and its proposal is folded into
the report.
"""

import argparse
import os

import scanpy as sc

from .cluster import run_one_sample_pipeline
from .report import generate_report, write_report_context

parser = argparse.ArgumentParser(prog="osp", description=__doc__)
parser.add_argument("h5ad_path")
parser.add_argument("--sample-col", default="sample")
parser.add_argument("--sample", required=True, help="sample name to run on its own")
parser.add_argument("--outdir", default="osp_out")
parser.add_argument("--no-scrublet", action="store_true", help="skip Scrublet doublet detection")
parser.add_argument("--no-decontx", action="store_true", help="skip DecontX ambient-RNA estimation")
parser.add_argument("--resolution", type=float, default=1.0, help="Leiden resolution (default 1.0)")
parser.add_argument(
    "--annotate", action="store_true", help="after the pipeline, run the annotation agent and refresh the report"
)
parser.add_argument("--species", default=None, help="context passed to --annotate")
parser.add_argument("--tissue", default=None, help="context passed to --annotate")
parser.add_argument("--language", default="English", help='annotation output language (default "English")')
parser.add_argument(
    "--harness",
    choices=["deepseek", "openai", "claude"],
    default=None,
    help="agent runtime backend (default: HARNESS env, then openai)",
)
parser.add_argument("--model", default=None, help="model id for the selected HARNESS backend")
parser.add_argument(
    "--effort",
    default=None,
    choices=["low", "medium", "high", "xhigh", "max"],
    help="reasoning effort for --annotate (models that support it)",
)
parser.add_argument(
    "--report-context",
    default=None,
    metavar="TEXT",
    help="where this sample sits, for the report title (e.g. the analysis unit name)",
)
args = parser.parse_args()
if args.harness:
    os.environ["HARNESS"] = args.harness

adata = sc.read_h5ad(args.h5ad_path, backed="r")
try:
    if args.sample_col not in adata.obs:
        raise ValueError(f"sample column {args.sample_col!r} is not present in the input obs")
    mask = adata.obs[args.sample_col].astype(str) == args.sample
    if not mask.any():
        raise ValueError(f"sample {args.sample!r} matches no cells in obs[{args.sample_col!r}]")
    sub = adata[mask].to_memory()
finally:
    adata.file.close()

write_report_context(args.outdir, args.report_context)
_, _, cluster_summary, *_ = run_one_sample_pipeline(
    sub,
    sample_label=args.sample,
    sample_col=args.sample_col,
    qc_kwargs={"run_scrublet": not args.no_scrublet, "run_decontx": not args.no_decontx},
    cluster_kwargs={"resolutions": (args.resolution,), "primary_resolution": args.resolution},
    outdir=args.outdir,
)
print(cluster_summary)
print(f"report: {generate_report(args.outdir)}")

if args.annotate:
    from .annotate import propose_annotation
    from .harness import backend_name, default_model

    model = args.model or default_model()
    print(f"[agent] harness={backend_name()} model={model}")

    propose_annotation(
        args.outdir,
        species=args.species,
        tissue=args.tissue,
        language=args.language,
        model=model,
        effort=args.effort,
    )

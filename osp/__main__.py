"""python -m osp: single-sample QC → clustering → DEG → HTML report, end to end.

With --annotate, the Claude annotation agent (osp.annotate, needs the
optional claude-agent-sdk) runs afterwards and its proposal is folded into
the report.
"""

import argparse

import scanpy as sc

from .cluster import run_one_sample_pipeline
from .report import generate_report

parser = argparse.ArgumentParser(prog="osp", description=__doc__)
parser.add_argument("h5ad_path")
parser.add_argument("--sample-col", default="sample")
parser.add_argument("--sample", required=True, help="sample name to run on its own")
parser.add_argument("--outdir", default="osp_out")
parser.add_argument("--no-scrublet", action="store_true")
parser.add_argument("--resolution", type=float, default=1.0)
parser.add_argument("--annotate", action="store_true",
                    help="after the pipeline, run the Claude annotation agent and refresh the report")
parser.add_argument("--species", default=None, help="context passed to --annotate")
parser.add_argument("--tissue", default=None, help="context passed to --annotate")
parser.add_argument("--language", default="English", help='annotation output language (default "English")')
parser.add_argument("--model", default=None, help='model for --annotate, e.g. "claude-fable-5" / "claude-sonnet-5"')
parser.add_argument("--effort", default=None, choices=["low", "medium", "high", "xhigh", "max"],
                    help="reasoning effort for --annotate (models that support it)")
args = parser.parse_args()

adata = sc.read_h5ad(args.h5ad_path)
sub = adata[adata.obs[args.sample_col] == args.sample]
_, _, cluster_summary, *_ = run_one_sample_pipeline(
    sub,
    sample_label=args.sample,
    sample_col=args.sample_col,
    qc_kwargs={"run_scrublet": not args.no_scrublet},
    cluster_kwargs={"resolutions": (args.resolution,), "primary_resolution": args.resolution},
    outdir=args.outdir,
)
print(cluster_summary)
print(f"report: {generate_report(args.outdir)}")

if args.annotate:
    from .annotate import propose_annotation

    propose_annotation(
        args.outdir, species=args.species, tissue=args.tissue, language=args.language,
        model=args.model, effort=args.effort,
    )

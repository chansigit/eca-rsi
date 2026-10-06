"""Driver: run the full OSP pipeline + report on one sample of an h5ad.

Loads the h5ad in backed mode and pulls only the requested sample into
memory, so each Slurm array task doesn't load the whole matrix. With
--annotate, the selected annotation backend runs afterwards and needs its
provider credentials on the node.

Usage: python examples/run_one_sample.py <h5ad_path> <sample> <output_root> [--annotate] [--model M]
"""

import argparse

import scanpy as sc

from osp import generate_report, run_one_sample_pipeline

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("h5ad_path")
parser.add_argument("sample")
parser.add_argument("outroot")
parser.add_argument("--annotate", action="store_true")
parser.add_argument("--model", default=None)
args = parser.parse_args()

a = sc.read_h5ad(args.h5ad_path, backed="r")
try:
    mask = a.obs["sample"].astype(str) == args.sample
    if not mask.any():
        raise ValueError(f"sample {args.sample!r} matches no cells in obs['sample']")
    sub = a[mask].to_memory()
finally:
    a.file.close()
print(f"sample {args.sample}: {sub.shape}", flush=True)

outdir = f"{args.outroot}/{args.sample}"
run_one_sample_pipeline(sub, sample_label=args.sample, outdir=outdir)
print("report:", generate_report(outdir), flush=True)

if args.annotate:
    from osp.annotate import propose_annotation

    propose_annotation(outdir, model=args.model)

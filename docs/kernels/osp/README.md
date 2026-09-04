# OSP — One-Sample Pipeline

**Better single-cell analysis starts with carefully reviewed samples.**

OSP takes one sample through quality control, clustering, and marker-gene
analysis, then brings the results together in a report you can open in a
browser. An optional AI assistant can suggest cell-type labels and highlight
cells or clusters that deserve a closer look.

OSP is part of the **[ECA-RSI](https://github.com/chansigit/eca-rsi)**
ecosystem — **Ensemble Cell Atlas - Recursive Self Improvement** — for
iterative quality review and cell-type annotation of single-cell datasets.

## Why start with one sample?

**Data quality shapes every downstream conclusion.** Damaged cells,
doublets, and ambient RNA can influence which genes appear variable, how
cells cluster, and which cell types we think we have found. Careful quality
control is therefore part of the scientific analysis itself. A dedicated
pipeline helps apply those checks consistently and makes the evidence and
removal decisions available for review.

A sample from a single experimental batch is the simplest starting point:
you can examine its quality and cell populations without first having to
separate differences between batches from biological variation. Sequencing
depth and contamination can vary between samples, so reviewing each one
individually makes its own quality patterns easier to interpret. Technical
variation within a sample still needs attention.

**Our recommended workflow is to review each sample with OSP before
integrating samples.** Inspect the report, consider whether the QC decisions
make biological sense, and then use
**[MSP — Multi-Sample Pipeline](https://github.com/chansigit/msp)** to bring
the reviewed samples together. MSP handles integration and supports further
quality review and cell-type annotation across samples. Together, the two
stages support a careful, traceable path from individual samples to a shared
analysis.

## What you get

| Result | What it helps you do |
| --- | --- |
| **A browser report** | Review cell quality, explore clusters, and inspect the genes that distinguish them. Plots are embedded, so the HTML file can be shared on its own. |
| **An analyzed dataset** | Continue working in Python with retained cells, preserved raw counts, cluster labels, and visualization coordinates. |
| **Tables and individual plots** | Check which cells were removed and why, examine marker genes, and reuse figures in your own analysis. |
| **Optional AI suggestions** | Review proposed cell-type labels, supporting genes, uncertainties, and additional QC actions alongside the analysis. |

## How it works

```mermaid
flowchart LR
    A["One sample"] --> B["Check cell quality"]
    B --> C["Cluster retained cells"]
    C --> D["Find marker genes"]
    D --> E["Open the report"]
    E -. "Optional" .-> F["Add AI annotation"]
```

Quality control checks sequencing depth, detected genes, mitochondrial RNA,
and possible doublets—two cells captured together. OSP also estimates
ambient RNA contamination: RNA present in a droplet that may come from
other cells. The report lets you examine these measurements alongside the
cell populations they affect.

## Run your first sample

### 1. Install OSP

Use a Python 3.10 or newer environment:

```bash
pip install osp-sc
```

The package is named `osp-sc` on PyPI; the command and Python import use
`osp`. To install the latest code from this repository instead:

```bash
pip install "git+https://github.com/chansigit/osp.git"
```

### 2. Prepare your input

You need an **H5AD file**, the AnnData format commonly used with Scanpy,
containing:

- **Raw expression counts** in `layers["counts"]`, or in `X` if that layer
  is absent. Already normalized expression alone is not a counts input.
- **A sample identifier for each cell**, in `obs["sample"]` by default.

The file may contain several samples; each run selects one. In the example
below, replace `data.h5ad` with your file and `SAMPLE_A` with a sample label
from your data. If your sample column has another name, add
`--sample-col YOUR_COLUMN`.

### 3. Run the analysis and open the report

```bash
python -m osp data.h5ad --sample SAMPLE_A --outdir results/SAMPLE_A
```

When the command finishes successfully, open
**`results/SAMPLE_A/report.html`** in your browser. If you ran OSP on a
remote server, download that HTML file to view it locally.

Start with the QC summary to see how many cells were retained and which
checks removed cells. Then look at the cluster plots and marker-gene
tables to understand the remaining populations.

The same directory also contains:

| File | Contents |
| --- | --- |
| `clustered.h5ad` | The analyzed cells that passed QC, with raw counts preserved in a layer. |
| `qc_removed.csv` | Cells removed during QC, with reasons and available quality measurements. |
| `de_top_genes_*.csv` | Genes that distinguish each primary cluster from the remaining cells. |

Use a separate output directory for each sample. Large inputs are read in
backed mode so only the selected sample's expression matrix is brought into
memory; that sample and its analysis still need to fit in available RAM.

## Add AI annotation

Once you have a report, you can ask the optional annotation assistant to
inspect the plots, check marker genes and QC measurements, and propose
cell-type labels. It can also split a heterogeneous cluster for closer
inspection.

Install the agent dependencies:

```bash
pip install "osp-sc[agent]"
```

The default setup uses Doubao through **Volcengine Ark** and requires an
Ark API key. Set your credentials in the environment, then annotate the
existing results:

```bash
export ARK_API_KEY="YOUR_ARK_API_KEY"
python -m osp.annotate results/SAMPLE_A --species mouse --tissue "bone marrow"
```

Replace the species and tissue with your sample's context. The assistant
updates the report and dataset with proposed labels and QC actions, and
saves its structured conclusions in `annotation_proposal.json`.

**You review the decisions.** An AI action of `drop` marks a suggested
removal; it does not delete that cell from the dataset. Review the supporting
genes, uncertainties, and QC evidence before using the labels or filtering
further. The regular analysis and report work without AI or an API key.

## A few choices to understand

- **The full pipeline removes cells that fail QC before clustering.**
  Inspect the report and removed-cell table: default thresholds may need
  adjustment for your sample or cell populations.
- **Clustering includes quality measurements by default.** OSP adds available
  QC measurements to gene expression when calculating principal components
  (PCA), which can influence which cells group together. Python users can
  select expression-only PCA with
  `cluster_kwargs={"qc_pca_covariates": None}` in
  `run_one_sample_pipeline`.
- **Ambient RNA estimates support interpretation.** DecontX estimates do
  not directly remove cells, and its corrected counts do not replace raw
  counts as the starting point for clustering.
- **Rerunning replaces results in the same directory.** Keep separate
  directories for analyses you want to compare, and check that a run
  finished successfully before relying on its output.

## Part of the ECA-RSI ecosystem

These projects work together within **Ensemble Cell Atlas - Recursive Self
Improvement (ECA-RSI)**. You can use OSP on its own or as the first analysis
stage in the wider workflow.

| Project | Role |
| --- | --- |
| [OSP — One-Sample Pipeline](https://github.com/chansigit/osp) | Review quality and cell populations within each sample before integration. |
| [MSP — Multi-Sample Pipeline](https://github.com/chansigit/msp) | Integrate reviewed samples, inspect populations across samples, and annotate cell types. |
| [ECA-RSI](https://github.com/chansigit/eca-rsi) | Coordinate the wider curation workflow, including iterative review, annotation, and focused reanalysis. |

Continue with [MSP's input guide](https://github.com/chansigit/msp#prepare-your-data)
when your samples are ready for joint analysis, or explore
[ECA-RSI](https://github.com/chansigit/eca-rsi) for the complete workflow.
If these tools help your work, stars, issues, and feedback on the related
repositories help others discover them and guide their development.

## Further reading

- [Input and output reference](docs/input-output.md) — matrix contents,
  output fields, Python return values, and completion rules.
- [Python sample driver](examples/run_one_sample.py) — run one sample from a
  larger input file.
- [Slurm job-array example](examples/submit_array.sbatch) — process samples
  as separate cluster jobs.
- [Report an issue](https://github.com/chansigit/osp/issues) — describe a
  problem or suggest an improvement.

OSP is distributed under the [MIT license](LICENSE). See
[third-party notices](THIRD_PARTY_NOTICES.md) for included components.

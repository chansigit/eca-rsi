# OSP: One-Sample Pipeline

**Better single-cell analysis starts with carefully reviewed samples.**

OSP combines **sample-level quality control with AI-assisted cell-type
annotation**. It checks cell quality, finds cell populations, and uses an AI
assistant to interpret marker genes and quality measurements together. You
get proposed labels, supporting evidence, and a browser report to review
before bringing samples together.

OSP is part of **[ECA-RSI](https://github.com/chansigit/eca-rsi)**
(**Ensemble Cell Atlas: Recursive Self Improvement**), an ecosystem for
iterative quality review and cell-type annotation of single-cell datasets.

## Why start with one sample?

Better inputs make downstream results easier to trust. OSP adds a dedicated
review before integration:

- **QC in the sample's own context.** Adaptive checks use each sample's
  quality distribution, so differences in depth or contamination remain
  visible before pooling.
- **Problems are easier to locate.** Within a single experimental batch,
  inspect suspicious populations without cross-sample batch differences
  complicating the picture.
- **Labels come with evidence.** The AI assistant checks marker genes and
  QC profiles, can refine mixed clusters, and records uncertainty and
  proposed actions for your review.

Start with OSP, then use **[MSP: Multi-Sample Pipeline](https://github.com/chansigit/msp)**
to integrate reviewed samples and continue annotation across samples.

## What you get

| Result | What it helps you do |
| --- | --- |
| **AI-assisted annotation** | Review proposed cell-type labels, supporting genes, uncertainties, and QC actions alongside the analysis. |
| **A browser report** | Review cell quality, explore clusters, and inspect the genes that distinguish them. Plots are embedded, so the HTML file can be shared on its own. |
| **An analyzed dataset** | Continue working in Python with retained cells, preserved raw counts, cluster labels, and visualization coordinates. |
| **Tables and individual plots** | Check which cells were removed and why, examine marker genes, and reuse figures in your own analysis. |

## How it works

**1. Assess the sample's quality.**
OSP checks sequencing depth, detected genes, mitochondrial RNA, and possible
doublets (two cells captured together). It also estimates ambient RNA from
other cells. Cells that fail QC are excluded from clustering, with their
measurements and removal reasons recorded for review.

**2. Explore the retained cell populations.**
OSP groups similar cells, builds a two-dimensional cell map (UMAP), and
identifies marker genes that distinguish each cluster. Quality measurements
are shown alongside the clusters to help you judge whether a population
reflects biology or a technical problem.

**3. Interpret the populations with AI.**
The annotation assistant examines plots, checks marker genes and quality
measurements, and proposes cell-type labels and QC actions. It can split
heterogeneous clusters for a closer look. Review its evidence and
uncertainties in the report before continuing to MSP.

## Run your first sample

### 1. Install OSP

Use a Python 3.10 or newer environment and install OSP with its AI
dependencies:

```bash
pip install "osp-sc[agent]"
```

The package is named `osp-sc` on PyPI; the command and Python import use
`osp`. To install the latest code from this repository instead:

```bash
pip install "osp-sc[agent] @ git+https://github.com/chansigit/osp.git"
```

QC and plotting run in Python, including the bundled DecontX implementation.
**No R installation is required.**

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

### 3. Run QC and AI annotation

The default AI backend uses Doubao through **Volcengine Ark**. Set your Ark
API key, then run the analysis with annotation enabled. Replace the species
and tissue below with your sample's context.

```bash
export ARK_API_KEY="YOUR_ARK_API_KEY"
python -m osp data.h5ad --sample SAMPLE_A --outdir results/SAMPLE_A \
    --annotate --species mouse --tissue "bone marrow"
```

The recommended command includes AI annotation through `--annotate`.
For QC and clustering alone, omit that flag; no API key is needed.

When the command finishes successfully, open
**`results/SAMPLE_A/report.html`** in your browser. If you ran OSP on a
remote server, download that HTML file to view it locally.

Start with the QC summary to see how many cells were retained and why.
Then review the proposed cell types alongside their marker genes, quality
profiles, and the assistant's notes.

The same directory also contains:

| File | Contents |
| --- | --- |
| `clustered.h5ad` | Cells that passed QC, with raw counts preserved in a layer and proposed cell-type labels and QC actions after annotation. |
| `annotation_proposal.json` | Structured cell-type labels, supporting genes, uncertainties, and QC proposals. |
| `qc_removed.csv` | Cells removed during QC, with reasons and available quality measurements. |
| `de_top_genes_*.csv` | Genes that distinguish each primary cluster from the remaining cells. |

Use a separate output directory for each sample. Large inputs are read in
backed mode so only the selected sample's expression matrix is brought into
memory; that sample and its analysis still need to fit in available RAM.

## Review the annotation

The report brings proposed cell types together with their supporting genes,
confidence, and open questions. Check whether the labels fit the marker
expression and whether populations flagged for QC have a plausible
biological explanation.

**You review the decisions.** An AI action of `drop` marks a suggested
removal; it does not delete that cell from the dataset. Review the supporting
genes, uncertainties, and QC evidence before using the labels or filtering
further.

To annotate existing pipeline results, or rerun annotation without repeating
QC and clustering:

```bash
python -m osp.annotate results/SAMPLE_A --species mouse --tissue "bone marrow"
```

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

These projects work together within **Ensemble Cell Atlas: Recursive Self
Improvement (ECA-RSI)**. You can use OSP on its own or as the first analysis
stage in the wider workflow.

| Project | Role |
| --- | --- |
| [OSP: One-Sample Pipeline](https://github.com/chansigit/osp) | Review quality and cell populations within each sample before integration. |
| [MSP: Multi-Sample Pipeline](https://github.com/chansigit/msp) | Integrate reviewed samples, inspect populations across samples, and annotate cell types. |
| [ECA-RSI](https://github.com/chansigit/eca-rsi) | Coordinate the wider curation workflow, including iterative review, annotation, and focused reanalysis. |

Continue with [MSP's input guide](https://github.com/chansigit/msp#prepare-your-data)
when your samples are ready for joint analysis, or explore
[ECA-RSI](https://github.com/chansigit/eca-rsi) for the complete workflow.
If these tools help your work, stars, issues, and feedback on the related
repositories help others discover them and guide their development.

## Further reading

- [Input and output reference](docs/input-output.md): matrix contents,
  output fields, Python return values, and completion rules.
- [Python sample driver](examples/run_one_sample.py): run one sample from a
  larger input file.
- [Slurm job-array example](examples/submit_array.sbatch): process samples
  as separate cluster jobs.
- [Report an issue](https://github.com/chansigit/osp/issues): describe a
  problem or suggest an improvement.

OSP is distributed under the [MIT license](LICENSE). See
[third-party notices](THIRD_PARTY_NOTICES.md) for included components.

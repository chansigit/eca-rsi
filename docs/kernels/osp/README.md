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

<br>

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

<br>

## What you get

- **Cell-type labels with evidence.** Proposed identities, supporting genes,
  uncertainties, and quality concerns for each cluster.

- **A report you can share.** Quality measurements, cell populations, and
  annotation results together in one HTML file with embedded plots.

- **Data ready for further analysis.** An analyzed H5AD with preserved raw
  counts, plus marker tables, individual plots, and a record of QC removals.

<br>

## How it works

### Quality control in context

OSP combines fixed and sample-adaptive thresholds with doublet detection
and contamination estimates to assess cell quality in context.

<details>
<summary>QC measurements and filtering rules</summary>

OSP measures several aspects of cell quality together:

| Measurements | What they help assess |
| --- | --- |
| Total counts and number of detected genes | Library size and expression complexity |
| Fraction of counts in the top 20 genes | Whether expression is dominated by a small number of genes |
| Mitochondrial, ribosomal, and hemoglobin count fractions | Cell quality and expression composition in the context of the tissue |
| Scrublet doublet score and prediction | Possible capture of two cells in one droplet |
| DecontX contamination fraction and contaminating-gene rankings | How much ambient RNA is estimated, and which genes contribute to it |
| MALAT1 count fraction and dissociation-stress score | Additional cell-integrity and stress signals, when the relevant genes are present |

**Filtering combines fixed thresholds with sample-adaptive thresholds.**
By default, fixed checks flag cells with fewer than 200 detected genes,
fewer than 500 counts, or more than 15% mitochondrial counts. Adaptive
checks flag observations more than five median absolute deviations (MADs)
from the sample median for log1p counts, log1p gene counts, and the
top-20-gene fraction. The additional mitochondrial flag requires a deviation
of more than three MADs from the median and a mitochondrial fraction above
8%. Scrublet-predicted doublets are also flagged.

These checks combine baseline limits with the sample's own distribution.
Their thresholds are configurable, and the report shows the resulting
ranges and failure counts so you can assess whether they fit the biology.
The full pipeline excludes cells flagged by these checks before cluster
analysis and records the reasons. DecontX, ribosomal and hemoglobin
fractions, MALAT1, and stress scores provide supporting evidence without
directly triggering this filter.

</details>

<br>

### Interpreting each cluster

The AI assistant combines marker genes, cluster relationships, and quality
evidence to propose cell identities and flag populations for closer review.

<details>
<summary>How the assistant builds and checks each interpretation</summary>

The AI assistant is instructed to build and verify an explanation for every
cluster using several kinds of evidence:

1. **Place the cluster in context.** Inspect the quality distributions and
   PAGA graph, which summarizes connections between cell populations.
   Connectivity provides context for interpreting related populations;
   marker and QC evidence support the biological interpretation.

2. **Read differential expression with its coverage.** OSP computes
   Wilcoxon differential expression for each primary cluster against the
   remaining cells, using the full normalized gene matrix. Marker tables
   include log fold changes, adjusted p-values, and the fractions of cells
   expressing each gene inside and outside the cluster (`pct1`/`pct2`).
   These fractions help distinguish broadly expressed markers from signals
   carried by a small subset of cells.

3. **Actively verify the proposed identity.** The assistant queries
   canonical and discriminating markers, including genes absent from the
   top DEG list. It receives mean expression and the percentage of cells
   expressing each queried gene across clusters.

4. **Check whether quality explains the signal.** Per-cluster QC summaries
   include medians and 90th percentiles. DecontX tables rank the estimated
   ambient contribution of individual genes from the difference between raw
   and corrected counts, helping assess whether apparent markers reflect
   contamination.

5. **Resolve mixed populations more locally.** When a cluster appears
   heterogeneous, the assistant can split it. OSP then computes DEG between
   the resulting subclusters within that parent population, giving a more
   focused comparison than the initial sample-wide contrast.

The assistant submits cell-type labels, supporting genes, confidence,
unresolved questions, and QC proposals. OSP checks the submission's structure
and cluster coverage before writing the results for review.

</details>

<br>

## Get started

### Recommended: let ECA-RSI coordinate the analysis

Prepare your data with **[ECA-PP](https://github.com/chansigit/eca-pp)**,
then let **[ECA-RSI](https://github.com/chansigit/eca-rsi)** organize samples
and run OSP and the downstream analyses. ECA-PP handles counts validation,
gene standardization, and sample or batch metadata identification. It can
also recover counts from supported log-normalized data.

Follow the [ECA-PP guide](https://github.com/chansigit/eca-pp#try-it) to
prepare your data, then the
[ECA-RSI setup guide](https://github.com/chansigit/eca-rsi/blob/main/INSTALL.md)
to run the workflow.

<details>
<summary>How ECA-PP recovers counts from log-normalized data</summary>

ECA-PP uses our [stancounts](https://github.com/chansigit/stancounts) method
to recover integer counts from supported log1p-normalized matrices. It
reverses the log transform and infers each cell's scaling factor from the
discrete expression values, without requiring the original normalization
target. Recovery depends on the retained count structure and precision;
unsupported or ambiguous inputs are reported for review.

</details>

<br>

### Run OSP on its own

For a standalone sample analysis, follow the three steps below.

#### 1. Install

Use Python 3.10 or newer. Install the current GitHub version with AI support:

```bash
pip install "osp-sc[agent] @ git+https://github.com/chansigit/osp.git"
```

**No R installation is required**, including for DecontX and plotting.

<details>
<summary>Install a published release from PyPI</summary>

```bash
pip install "osp-sc[agent]"
```

The package is named `osp-sc`; its Python import and command use `osp`.

</details>

<br>

#### 2. Prepare your input

Provide an **H5AD file**, the AnnData format commonly used with Scanpy,
containing:

- **Raw expression counts** in `layers["counts"]`, or in `X` if that layer
  is absent. Already normalized expression alone is not a counts input.

- **A sample identifier for each cell**, in `obs["sample"]` by default.

The file may contain several samples; each run selects one. In the example
below, replace `data.h5ad` with your file and `SAMPLE_A` with a sample label
from your data. If your sample column has another name, add
`--sample-col YOUR_COLUMN`.

<br>

#### 3. Run QC and AI annotation

The default AI backend uses Doubao through **Volcengine Ark**. Set your Ark
API key, then run the analysis with annotation enabled. Replace the species
and tissue below with your sample's context, and use a separate output
directory for each sample.

```bash
export ARK_API_KEY="YOUR_ARK_API_KEY"
python -m osp data.h5ad --sample SAMPLE_A --outdir results/SAMPLE_A \
    --annotate --species mouse --tissue "bone marrow"
```

When the command finishes successfully, open
**`results/SAMPLE_A/report.html`** in your browser. If you ran OSP on a
remote server, download that HTML file to view it locally.

Start with the QC summary to see how many cells were retained and why.
Then review the proposed cell types alongside their marker genes, quality
profiles, and the assistant's notes.

<details>
<summary>The same directory also contains</summary>

| File | Contents |
| --- | --- |
| `clustered.h5ad` | Cells that passed QC, with raw counts preserved in a layer and proposed cell-type labels and QC actions after annotation. |
| `annotation_proposal.json` | Structured cell-type labels, supporting genes, uncertainties, and QC proposals. |
| `qc_removed.csv` | Cells removed during QC, with reasons and available quality measurements. |
| `de_top_genes_*.csv` | Genes that distinguish each primary cluster from the remaining cells. |

</details>

<br>

<details>
<summary>Review the annotation</summary>

The report brings proposed cell types together with their supporting genes,
confidence, and open questions. Check whether the labels fit the marker
expression and whether populations flagged for QC have a plausible
biological explanation.

To annotate existing pipeline results, or rerun annotation without repeating
QC and clustering:

```bash
python -m osp.annotate results/SAMPLE_A --species mouse --tissue "bone marrow"
```

See [Does OSP remove cells?](#does-osp-remove-cells) for how QC filtering
and AI proposals affect your data.

</details>

<br>

## FAQ

### Does OSP remove cells?

OSP preserves your original input file and writes a separate analysis
dataset. Cells flagged by the configured QC thresholds or doublet detection
are excluded from that output, with the reasons recorded in `qc_removed.csv`.
Review the thresholds against your sample's biology, especially for naturally
low-complexity populations.

The AI annotation stage only records proposed actions. Cells marked `drop`
by the assistant remain in OSP's `clustered.h5ad` for downstream processing.
For subsequent filtering, see [MSP's output guide](https://github.com/chansigit/msp#find-and-understand-your-results).

### Can I use OSP without AI?

Yes. Omit `--annotate` to run QC, clustering, and report generation without
an API key. For installation without the AI dependencies, omit `[agent]`
from the package name.

### Does DecontX change the expression matrix used for analysis?

OSP preserves raw counts and uses them as the starting point for
normalization and clustering. DecontX stores corrected counts separately
and provides contamination estimates for interpretation. Its contamination
score does not directly trigger the initial QC filter.

### Can I rerun an analysis?

Yes. Rerunning replaces results in the same output directory. Use a new
directory to compare settings or preserve an earlier analysis, and check
that a run finished successfully before relying on its output.

### Can I use a large H5AD containing multiple samples?

Yes. The CLI loads only the selected sample's expression matrix into memory.
Allow enough RAM for that sample and its analysis. For many samples, use
ECA-RSI to manage the runs or adapt the
[Slurm job-array example](examples/submit_array.sbatch).

<br>

## Further reading

### Related projects in the ECA-RSI ecosystem

Use these companion projects to prepare inputs and continue from individual
samples to a shared, iteratively reviewed analysis.

| Project | Role |
| --- | --- |
| [ECA-PP](https://github.com/chansigit/eca-pp) | Prepare counts, gene identifiers, and metadata evidence for the analysis workflow. |
| [OSP: One-Sample Pipeline](https://github.com/chansigit/osp) | Review quality and cell populations within each sample before integration. |
| [MSP: Multi-Sample Pipeline](https://github.com/chansigit/msp) | Integrate reviewed samples, inspect populations across samples, and annotate cell types. |
| [ZMIP: Zoom-In Pipeline](https://github.com/chansigit/zmip) | Refine retained cells within individual lineages after MSP, reviewing labels and remaining quality concerns. |
| [ECA-RSI](https://github.com/chansigit/eca-rsi) | Coordinate the wider curation workflow, including iterative review, annotation, and focused reanalysis. |

If these tools help your work, stars, issues, and feedback on the related
repositories help others discover them and guide their development.

<br>

### Documentation and examples

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

# Cross-sample type annotation

Design prompt: not yet connected to the runtime. The host must prepend the Prompt section of `precomputed-deg-prompt.md`, attach the versioned evidence manifest, and provide the existing submission schemas and tools.

## Prompt

Identify the cell types of the assigned base clusters before model-based quality annotation. Use the supplied tissue/species context, parent-level evidence, expression summaries, and precomputed DEG. No prior quality-agent decision is required at this stage.

1. Verify the available clustering keys and evidence version. Retrieve each assigned cluster's global and local markers with `deg_lookup`; use bounded `deg_sql` queries for comparisons across stored evidence. Follow the shared precomputed-DEG instructions before requesting new computation.
2. Assign the most specific coarse/fine identity supported by the evidence. Check positive and conflicting markers and distinguish cell identity from transient state, stress, and technical contamination. Retain uncertainty rather than inventing a subtype.
3. Use existing expression evidence first. If needed, request targeted `check_genes` evidence or an uncovered DEG comparison through the provided tools. Do not run a numerical pipeline yourself or repeat a comparison already present in the database.
4. Submit the type labels, supporting evidence, uncertainty, and any supported merge proposals using the host's submission schema. Record any required removal recommendation as a proposal only. Do not delete cells, apply merges, or change the evidence snapshot.
5. Ensure all assigned clusters are covered and the label hierarchy and merge proposals are consistent. Correct validation errors through the provided submission tools. The host passes accepted proposals to quality annotation.

For a targeted review requested after quality-driven refinement, use the supplied new clustering version and matching evidence. Review only the affected clusters. A parent's type is context, not a confirmed label for every child.

## Cell-exclusion accounting

For every proposed exclusion, provide a specific reason and evidence reference using the host's schema, and identify the affected cluster, sample, or cell scope and its version. Preserve all supported reasons when they overlap. A removal proposal is not an applied exclusion; the host expands accepted decisions to exact cell IDs, writes the exclusion ledger when filtering is applied, and validates input/output conservation. Do not manually enumerate large cell lists, count temporary DEG masks as discarded cells, or count reassignment as removal.

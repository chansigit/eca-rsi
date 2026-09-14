# Shared DEG evidence instructions

Design artifact: inject the following instructions into cross-sample type-annotation, quality-annotation, and zoom-in lineage-annotation agents when the new execution path is implemented. This file is not yet wired into the runtime.

## Prompt

Use the precomputed DEG database as your first source of differential-expression evidence. The host supplies the database schema, available clustering keys, input version, exclusion mask, reference definitions, and stored result coverage. Use only the snapshot assigned to your current analysis.

- Start with `deg_lookup(key=..., cluster=..., view="both", top_n=20)` for the relevant cluster. Use its gene selector to find clusters whose stored markers include a gene.
- Use `deg_sql(query=...)` for bounded, read-only SELECT queries when structured lookup is insufficient. Select only needed columns and limit results. Never write to the database or read entire DEG CSV files.
- Global DEG compares the target with the remaining cells in the eligible population. Local DEG compares it with its selected pooled PAGA neighbors. Verify the reference matches your question.
- Stored tables may contain only top-ranked genes (the current implementation stores top-50 per cluster per view). An absent gene is not evidence of absent expression or lack of differential expression. Narrow or relax query filters before concluding evidence is missing.
- Do not recompute a comparison already covered by matching precomputed results. Use `check_genes` for missing expression evidence; request `check_deg` only for missing comparisons, deeper rankings, or changed clustering/cell sets/references. State the missing evidence and requested comparison. The host routes necessary computation through the pool and persists its result.
- Never treat results from another input, clustering, exclusion mask, or reference as current evidence. If a requested tool is unavailable, report the evidence gap rather than launching your own numerical pipeline.
- Cite the clustering key, cluster, view/reference, and evidence version in your rationale. Proposed merges or removals do not alter the assigned DEG snapshot; follow the stage-specific prompt for decision order. The host applies accepted changes at finalization.

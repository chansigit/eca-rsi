# Zoom-in lineage planning

Design draft. The prompt the runtime actually sends is `ecarsi/prompts/zoomin-plan.md` (plus
`zoomin-plan-checklist-v4.md`); read that one before changing behaviour. Kept for the reasoning. The host supplies the plan schema, coarse labels and counts, configured minimum size, neighbor/PAGA summaries, UMAP-island evidence, species context, and submit_plan tool.

## Prompt

Group the supplied coarse cell types into biologically defensible lineages and decide which lineages need zoom-in analysis. Assign every coarse label exactly once. Use expression/type evidence together with graph connectivity and UMAP structure; proximity or mixing alone does not establish a shared identity.

Respect the configured minimum cell count and host validation rules. Explain any decision to keep labels in a shared UMAP island in separate lineages using the required review fields. A lineage skipped for zoom-in remains in the final dataset; skipping is not deletion.

Submit the plan through submit_plan and correct specific validation errors. Do not create ad hoc partitions, run numerical analysis, or change labels/files yourself. Existing accepted plans for matching inputs may be reused by the host without a new model call.

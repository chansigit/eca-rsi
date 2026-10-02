# Bridge acceptance records — 2026-09-14/15

Moved from `docs/control-plane/AGENT_BRIDGE_V2.md` on 2026-10-02. Historical record: hosts, images, paths and limits are those of September 2026.

## Agent workflows with worker tools, 2026-09-14

Real acceptance on 2026-09-14 used two H5AD inputs and a separate recovery session:
three completed Temporal Workflows, nine Bridge model-turn requests and six
successful worker programs. The programs read dimensions and computed count
matrix totals on `sh04-14n18`; none ran on the Bridge node `sh03-01n03`.
Coordinator and Bridge were stopped while one tool ran. The worker saved its
receipt independently; after restart the original workflow resumed, with no
duplicate tool execution. This tests process recovery, not node/database loss.
Specs, pinned test program, logs, histories, receipts and `acceptance.json` are at
`/scratch/users/chensj16/eca-runs/warmpool-v2-development/agent-tool-handoff-20260914-194008/`.
The observatory draws explicit model → tool → model dependencies; worker-tool
waiting is not included in the new Bridge turn bars.

The default timeline groups dataset → stage workflow → named step. Separate
workflow IDs stay separate even when they share a dataset or stage name. Clicking
a step opens its model calls and worker tasks; `Workers` retains the exact
execution-placement view. Dataset pages are selected before the task limit, so
a busy dataset cannot remove its neighbors from the viewer. Truncated task
history is labeled and can be narrowed by dataset/time. Stage spans summarize
loaded records and include gaps; they are not authoritative workflow status.
Dataset colors persist in the browser, with additional hue separation for small
comparisons. Labels remain the identity cue for large collections.

### Worker routing acceptance (2026-09-15)

Three concurrent Temporal Agent workflows completed six real model turns (four
Turbo, two Pro) and exactly three registered worker programs. The calls executed
on `sh03-01n54` and `sh04-14n18`; every program returned the verified sum 500500.
A separate recovery check stopped Bridge after a Worker accepted a model call.
The Worker saved its successful receipt while Bridge was down; restarting Bridge
resumed the same workflow to completion. This does not claim provider-side
cancellation or a sustained scientific throughput benchmark.

Evidence is under `durable-control-20260915/agent-worker-acceptance` in the shared
v2 run directory: `real-acceptance.json` and `bridge-inflight-recovery.json`.
The container regression passed 29 tests, including local HTTP timeout/fallback,
portable tool and image continuation, model health and the existing Bridge tests.

## Evidence packing, 2026-09-15

In the 2026-09-15 development batch, Prostate received six sample inventories in
one response; Eye's next model request advanced from inventory offset 2 to 10.
The first twelve completed batches returned 46 registered observations with no
tool errors. Forty-two focused tests passed, including full text/Unicode delivery,
oversized-image read-state rollback, native model batches, pinned execution plans,
and recovery with a verified replacement model attempt. These checks establish
evidence delivery and recovery behavior, not sustained high Pool utilization.
Long retained workflow histories still make cold recovery expensive; large model
responses can also exhaust the configured deadline. The Testis type-annotation
turn hit three 300-second deadlines in this observation window and needs separate
diagnosis; evidence packing must not be reported as a fix for that timeout.

## Dispatcher cache, 2026-09-15

`dispatch_scan_seconds` records loop processing time. In the 2026-09-15 live
acceptance with over 1,100 retained requests, update intervals fell from about
7.5 seconds to 1.03 seconds; hot scans took 15–30 ms. Both calls running during
the dispatcher replacement finished normally. This measures dispatch overhead,
not model inference speed or end-to-end scientific throughput.
The first 24 subsequent model requests had a median admission delay of 0.50 seconds.

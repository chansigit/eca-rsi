"""What the stage workflows share: activity calls, waiting on pool requests, the stage query, the history
budget and the DEG fan-out of cross-sample and zoom-in. Workflows import from here, never from each other."""
import asyncio

from temporalio import workflow
from temporalio.exceptions import ApplicationError

SKIPPED_CELL_LIMIT = 0.10  # a stage whose skipped samples or lineages hold more of its input cells fails instead
# Continue as new past this many history events, at a point with nothing in flight. A Zoomin round
# reached 20-38k events; replaying that after a coordinator restart took 15-60 s against a 10 s
# workflow task timeout (63 zoom-ins stuck on 2026-09-23), and the cached copy of every such
# workflow is what filled the coordinators' memory (~50 MB each). Everything a stage does is
# idempotent -- content-addressed pool requests, saved session turns -- so a continued execution
# re-drives finished steps in a handful of events each.
HISTORY_LIMIT = 5000
# Comparisons per DEG pool request. One request per comparison made a lineage of 40 comparisons 40
# requests, each paying a process start, a numba warm-up and a SHA pass over the shared buffers; eight
# per request keeps the fan-out (max_in_flight_deg counts requests) while cutting requests eightfold.
DEG_BATCH_SIZE = 8
# Above this many cells a comparison is slow enough that the per-request cost no longer matters (deg_batches).
DEG_BATCH_CELLS = 50_000


def deg_batches(n, n_cells):
    """The DEG requests of n comparisons over n_cells cells, and the factor on max_in_flight_deg.

    Eight comparisons per request up to 50k cells, fewer above, one from 400k: in the 2026-10-05 scale test
    (418k cells) one request of eight ran 75 min and timed out once while most of a 64-core node sat idle.
    The in-flight limit grows by the same factor, so about max_in_flight_deg x 8 comparisons stay in flight.
    Each comparison runs on its own either way (stages.common.deg_batch loops _deg_one), so results
    do not change. n_cells 0 (not known) keeps eight per request."""
    size = max(1, min(DEG_BATCH_SIZE, DEG_BATCH_SIZE * DEG_BATCH_CELLS // max(1, n_cells)))
    return [list(range(i, min(i + size, n))) for i in range(0, n, size)], -(-DEG_BATCH_SIZE // size)


async def run_degs(count, start, limit):
    """Cross-sample's and zoom-in's DEG fan-out: start(k) for k < count in order with at most limit()
    in flight (read at every start, so set_deg_limit applies at once); the results in k order."""
    pending, results, next_index = {}, {}, 0
    while next_index < count or pending:
        while next_index < count and len(pending) < limit():
            pending[asyncio.create_task(start(next_index))] = next_index
            next_index += 1
        done, _ = await workflow.wait(pending, return_when=asyncio.FIRST_COMPLETED)
        for task in sorted(done, key=lambda t: pending[t]):
            index = pending.pop(task)
            results[index] = await task
    return [results[i] for i in sorted(results)]


def handoff(path):
    """A stage document as a workflow may hold it: without the per-sample manifest.

    `read` used to return the whole file, and Temporal keeps every activity result in the
    workflow history and ships it again on every replay. inspected.json carries one entry
    per sample -- its QC summary and its full annotation proposal -- plus a reference to
    every figure, so it grows with the sample count: 2 KB for 14 Tabula Sapiens samples,
    4.3 MB for a 215-sample PanSci dataset. That is over Temporal's payload limit, and two
    mouse datasets failed at cross-sample on 2026-09-21 with PayloadsTooLarge.

    The workflows never needed any of it. They count the samples and branch on
    previous_round; every activity that wants the content reads the file itself, by path.
    So what enters history keeps each sample's name and size and drops the rest. Old
    histories hold the full document, which is a superset, so replay is unaffected."""
    from ..files import reference, verified
    doc = verified(reference(path))
    if isinstance(doc.get('samples'), list):
        doc['samples'] = [{k: s[k] for k in ('sample', 'n_cells') if k in s} if isinstance(s, dict) else s
                          for s in doc['samples']]
    doc.pop('files', None)
    return doc


async def call(fn, *args):
    from .coordinator import SHORT, activity_retry
    return await workflow.execute_activity(fn, args=args, start_to_close_timeout=SHORT, retry_policy=activity_retry(fn))


def stage_with_waits(owner):
    """A workflow's stage query: its stage plus why its pool requests wait (#18), e.g. 'DEG comparisons;
    waiting: infeasible: no worker that holds 4 cpus / 12288 MB has 7230 s left'."""
    waits = sorted(set(getattr(owner, '_waits', {}).values()))
    return '; '.join([getattr(owner, '_stage', 'created')] + ['waiting: ' + why for why in waits])


async def await_pool(spec, request):
    from .coordinator import check_pool
    waits = workflow.instance().__dict__.setdefault('_waits', {})
    try:
        while True:
            result = await call(check_pool, spec["pool_root"], request["id"], request["output"])
            if result["state"] == "ready":
                return result["path"]
            if result["state"] != "waiting":
                raise ApplicationError(f"{request['id']}: {result['state']}: {result.get('detail')}", non_retryable=True)
            if result.get("detail"):
                waits[request["id"]] = result["detail"]
            else:
                waits.pop(request["id"], None)
            await workflow.sleep(2)
    finally:
        waits.pop(request["id"], None)

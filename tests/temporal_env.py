"""Workflow tests on Temporal's time-skipping test server.

The real workflow classes run on a real worker; what they schedule -- activities and child workflows -- is
answered by fakes registered under the real names (`crosssample_step`, `check_pool`, `AgentWorkflow`, ...).
No test reaches into a module to replace its internals, so moving a function between modules never breaks a
test, and a workflow is exercised with Temporal's own ordering, timers, updates, queries and continue-as-new.
"""

import contextlib
import inspect
from concurrent.futures import ThreadPoolExecutor

from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

QUEUE = "tests"


def fakes(**functions):
    """Activities under the names the workflows schedule, e.g. fakes(crosssample_step=step, check_pool=ready).
    Each is a fresh wrapper: a function such as `ready` can serve any number of tests."""
    def wrap(name, function):
        if inspect.iscoroutinefunction(function):
            async def fake(*args):
                return await function(*args)
        else:
            def fake(*args):
                return function(*args)
        return activity.defn(name=name)(fake)
    return [wrap(name, function) for name, function in functions.items()]


def ready(pool_root, request_id, output):
    """check_pool for a pool that finished every request at once: its path is the request id."""
    return {"state": "ready", "path": request_id}


@contextlib.asynccontextmanager
async def temporal(workflows, activities):
    """A client of a fresh time-skipping server whose worker runs `workflows` and `activities` (sync ones
    in a thread pool, async ones on the worker's loop); timers such as await_pool's polls are skipped."""
    activities = list(activities)
    if "before_child" not in {a.__temporal_activity_definition.name for a in activities}:
        # 0022: every parent asks before_child before a child; a test that does not fake it keeps its queue, no brake
        activities += fakes(before_child=lambda control: dict(task_queue=None, brake=None))
    async with await WorkflowEnvironment.start_time_skipping() as env:
        with ThreadPoolExecutor(16) as threads:
            async with Worker(
                env.client,
                task_queue=QUEUE,
                workflows=list(workflows),
                activities=list(activities),
                activity_executor=threads,
                workflow_runner=UnsandboxedWorkflowRunner(),
            ):
                yield env.client

"""A fake AgentWorkflow for workflow tests (tests/temporal_env.py): each session asks the test's `agent_outcome`
activity what to do -- {"result": ...}, {"died": message} or {"wait": True} (poll again) -- so a test steers its
sessions from test state without the workflow ever reading that state itself."""

from datetime import timedelta

from temporalio import workflow
from temporalio.exceptions import ApplicationError


@workflow.defn(name="AgentWorkflow")
class Agent:
    @workflow.run
    async def run(self, session: dict) -> str:
        for _ in range(200):
            outcome = await workflow.execute_activity(
                "agent_outcome",
                args=[session, workflow.info().workflow_id],
                start_to_close_timeout=timedelta(seconds=30),
            )
            if outcome.get("wait"):
                await workflow.sleep(1)
                continue
            if outcome.get("died"):
                raise ApplicationError(outcome["died"], non_retryable=True)
            return outcome["result"]
        raise ApplicationError("the session waited for good", non_retryable=True)

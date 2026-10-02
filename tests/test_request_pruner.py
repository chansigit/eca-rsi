"""The request pruner keeps a superseded failed run's requests for a week, so it stays resumable."""
import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location("request_pruner", Path(__file__).parents[1] / "container" / "request-pruner.py")
pruner = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pruner)


def test_a_superseded_failed_run_waits_a_week():
    now = 1_000_000_000.0
    week = pruner.FAILED_KEEP_SECONDS

    def fleet(failed_closed):
        return {"workflows": {
            "dataset/a-1": dict(kind="DatasetWorkflow", dataset_id="A", status="FAILED", closed=failed_closed),
            "dataset/a-2": dict(kind="DatasetWorkflow", dataset_id="A", status="COMPLETED", closed=now - 60),
            "dataset/b-1": dict(kind="DatasetWorkflow", dataset_id="B", status="FAILED", closed=now - 2 * week)}}
    assert set(pruner.prunable_runs(fleet(now - 3600), now)) == {"dataset/a-2"}
    assert set(pruner.prunable_runs(fleet(now - week - 1), now)) == {"dataset/a-1", "dataset/a-2"}
    assert set(pruner.prunable_runs(fleet(None), now)) == {"dataset/a-2"}  # no close time: keep

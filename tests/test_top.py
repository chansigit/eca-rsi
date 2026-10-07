"""eca-rsi top renders a control.snapshot() in the terminal: every published fact shows, stale records read as
silent (rule 2 of test_monitor_isolation), and an empty or partial snapshot still renders, wide and narrow."""
import io

from rich.console import Console

from ecarsi.ui import top

NOW = 1_800_000_000.0


def snap(**over):
    task = {"id": "run-a.deg-batch-1", "operation": "zoom-in.deg", "state": "running", "submitted_at": NOW - 70,
            "started_at": NOW - 60, "host": "node-a", "worker_id": "node-a-1", "cpus": 1, "memory_mb": 4096,
            "trace": {"dataset_id": "atlas heart_male"}}
    data = {
        "host": "plane-1.int",
        "scheduler": {"state": "running", "observed_at": NOW - 1, "datasets": {"atlas heart_male": [1, 0, 0, 0]},
                      "release": {"infeasible": {"gpu: no live worker": 2}}},
        "temporal_service": {"state": "ready", "observed_at": NOW - 3},
        "bridge_summary": {"running": 1, "concurrency": 256, "updated_at": NOW - 1, "datasets": {"atlas eye": 1},
                           "models": [{"model": {"model": "doubao-seed-2-1-turbo-260628"}, "state": "ready", "in_flight": 1,
                                       "successes": 97, "failures": 3, "last_latency_seconds": 40}]},
        "worker_live_count": 1,
        "workers": [{"worker_id": "node-a-1", "host": "node-a", "slurm_job_id": "123", "reporting": True, "cpus": 8,
                     "allocation": {"end_time": NOW + 3600}, "reserved_cpus": 1, "tasks": [task],
                     "current": {"cpu_percent": 62.0, "memory_percent": 40.0, "memory_used_gb": 12.8, "memory_total_gb": 32}},
                    {"worker_id": "node-b-1", "host": "node-b", "reporting": False, "tasks": []}],
        "productivity": [{"host": "node-a", "cores": 8, "efficiency_15m": 71.0, "efficiency_4h": 48.0, "tasks_done_4h": 2716}],
        "running_datasets": ["atlas eye", "atlas heart_male"], "running_versions": {"atlas eye": "abc", "atlas heart_male": "abc"},
        "pool_waiting": 2, "pool_total": 10, "pool_succeeded": 8,
        "recent_failures": [{"id": f"run-a.tool-{i}", "at": NOW - 100 - i, "operation": "check_genes",
                             "dataset": "atlas eye"} for i in range(3)],
    }
    return {**data, **over}


def text(frame, width):
    console = Console(file=io.StringIO(), width=width, height=60, color_system=None)
    console.print(frame)
    return console.file.getvalue()


def test_every_published_fact_shows_wide_and_narrow():
    for width in (190, 120):
        out = text(top.render(snap(), {"node-a-1": [10, 50, 95]}, NOW, 0.1, None, 2, 60, width), width)
        for fact in ("scheduler running", "temporal ready", "bridge 1/256", "node-a", "123", "62%", "13/32 G", "60 min",
                     "doubao-seed-2-1-turbo", "3.0%", "zoom-in.deg", "heart_male", "computing", "model turn",
                     "version abc", "71%", "2,716", "check_genes", "×3", "gpu: no live worker (2)", "waiting 2", "1 departed"):
            assert fact in out, (width, fact)
        assert "260628" not in out  # the model's date suffix is dropped


def test_a_stale_record_is_silent_not_its_last_status():
    old = snap(scheduler={"state": "running", "observed_at": NOW - 600}, temporal_service={"state": "ready"},
               bridge_summary={"running": 0, "updated_at": NOW - 900})
    out = text(top.render(old, {}, NOW, columns=190), 190)
    assert "scheduler silent" in out and "temporal silent" in out and "bridge silent" in out


def test_an_empty_snapshot_renders():
    out = text(top.render({}, {}, NOW, error="OSError: [Errno 5] Input/output error", columns=100), 100)
    assert "no worker is reporting" in out and "no dataset is running" in out and "Errno 5" in out

"""The release layer's timings come from the pool's task journals, not from knobs (D8 step 2, 2026-09-25)."""
import json
import time
from collections import deque

from ecarsi.warm_pool.measure import measure, quantile, write
from ecarsi.warm_pool.state import read


def journal(pool, worker, day, rows):
    directory = pool / "workers" / worker
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / f"tasks-{day}.jsonl").open("a") as stream:
        for row in rows:
            stream.write(json.dumps(row) + "\n")


def row(op, duration, finished, gpu=False, wait=10.0, state="succeeded", request_id=None):
    return dict(request_id=request_id or f"{op}-1", operation=op, state=state, duration_s=duration, finished_at=finished,
                queue_wait_s=wait, gpu_ids=[0] if gpu else [])


def test_measure_reads_recent_journals_per_operation_and_mode(tmp_path):
    now = time.time()
    today = time.strftime("%Y-%m-%d", time.gmtime(now))
    journal(tmp_path, "w1", today, [row("zoom-in.compute", d, now - 100) for d in (80, 90, 100)]
            + [row("zoom-in.compute", d, now - 100, gpu=True, wait=200) for d in (40, 50)]
            + [row("zoom-in.deg", 8, now - 50, wait=15), row("zoom-in.deg", 30, now - 40, wait=300),
               row("agent.call", 70, now - 30, request_id="agent-x"), row("read_evidence", 5, now - 20, request_id="zoom-a.tool-1"),
               row("zoom-in.deg", 999, now - 10, state="failed"),          # failures are not timings
               row("zoom-in.deg", 7, now - 5 * 86400)])                     # too old, even in a recent file
    journal(tmp_path, "w2", "2026-01-01", [row("zoom-in.deg", 500, now - 100)])   # an old file is skipped by name
    result = measure(tmp_path, days=3, now=now)
    ops = result["operations"]
    assert ops["zoom-in.compute"]["cpu"] == dict(n=3, median_s=90, p90_s=100)
    assert ops["zoom-in.compute"]["gpu"] == dict(n=2, median_s=50, p90_s=50)
    assert ops["zoom-in.compute"]["wait"]["n"] == 5 and ops["zoom-in.compute"]["wait"]["p90_s"] == 200
    assert ops["zoom-in.deg"]["cpu"] == dict(n=2, median_s=30, p90_s=30) and ops["zoom-in.deg"]["gpu"] == dict(n=0)
    assert result["completions_per_second"] == round(9 / 3600, 4)   # nine succeeded within the hour
    assert result["work_p90_s"] == 100                              # model turns and session tools are not batch work
    assert quantile([3, 1, 2], .5) == 2 and quantile([1], .9) == 1
    write(tmp_path, days=3)
    assert read(tmp_path / "measured.json")["operations"]["zoom-in.deg"]["cpu"]["n"] == 2


def scheduler(measured, **release):
    from ecarsi.warm_pool.backend import HyperQueue, RELEASE_DEFAULTS
    hq = HyperQueue.__new__(HyperQueue)
    hq.release, hq.overrides, hq.measured = dict(RELEASE_DEFAULTS, **release), set(release), measured
    hq.receipts_seen, hq.tick_seconds = deque(), 2.0
    return hq


MEASURED = dict(generated_at=1.0, work_p90_s=31,
                operations={"zoom-in.compute": dict(cpu=dict(n=9, median_s=81, p90_s=235), gpu=dict(n=5, median_s=44, p90_s=94),
                                                    wait=dict(n=9, median_s=18, p90_s=625)),
                            "zoom-in.deg": dict(cpu=dict(n=9, median_s=8, p90_s=31), gpu=dict(n=0), wait=dict(n=9, median_s=15, p90_s=40))})


def test_measurements_replace_the_knobs_unless_a_knob_is_configured():
    hq = scheduler(MEASURED)
    assert hq.run_seconds("zoom-in.compute", True) == 44 and hq.run_seconds("zoom-in.compute", False) == 81
    assert hq.run_seconds("zoom-in.deg", True) == 120           # never ran on a card: the knob
    assert hq.run_seconds("cross-sample.compute", False) == 360  # never measured at all: the knob
    assert hq.wait_limit("zoom-in.compute") == 625 and hq.wait_limit("zoom-in.deg") == 60   # p90 wait, a minute at least
    assert hq.wait_limit("unknown") == 600
    assert hq.drain_limit() == 62                                # twice the p90 run of batch work
    configured = scheduler(MEASURED, gpu_task_seconds=200, drain_age_seconds=900, max_drain_seconds=900)
    assert configured.run_seconds("zoom-in.compute", True) == 200 and configured.run_seconds("zoom-in.compute", False) == 81
    assert configured.wait_limit("zoom-in.compute") == 900 and configured.drain_limit() == 900
    assert scheduler({}).drain_limit() == 900 and scheduler({}).wait_limit("zoom-in.deg") == 600


def test_backlog_follows_the_start_rate_the_scheduler_saw():
    hq = scheduler(MEASURED)
    assert hq.backlog(1000.0, 48) == 16                         # no receipt seen yet: the knob (48 / 3)
    hq.receipts_seen.extend([600.0] + [990.0] * 150)            # 150 receipts in the last five minutes, one too old
    assert hq.backlog(1000.0, 48) == 16 and len(hq.receipts_seen) == 150   # 2 s ticks at 0.5/s: two starts, floor 16
    hq.receipts_seen.extend([995.0] * 3000)
    assert hq.backlog(1000.0, 48) == int(2 * 3150 / 300 * 2.0) == 42       # two ticks of starts at 10.5 per second
    assert scheduler(MEASURED, backlog_per_cpu=1).backlog(1000.0, 48) == 48   # configured: cpus x knob


def test_measured_file_is_reloaded_when_it_changes(tmp_path):
    from ecarsi.warm_pool.backend import HyperQueue
    from ecarsi.warm_pool.state import save
    (tmp_path / "requests").mkdir()
    save(tmp_path / "config.json", dict(hq="/bin/false", executor="/usr/bin/python3", runtime={}))
    hq = HyperQueue(tmp_path)
    hq._reload_measured()
    assert hq.measured == {} and hq.run_seconds("zoom-in.compute", True) == 120
    save(tmp_path / "measured.json", MEASURED)
    hq._reload_measured()
    assert hq.run_seconds("zoom-in.compute", True) == 44

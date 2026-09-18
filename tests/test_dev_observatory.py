import json
from pathlib import Path
import tempfile
import unittest

from ecarsi.observatory import resource_history, snapshot, summarize_resources, task_timeline, worker_inventory


def put(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


class ObservatoryTest(unittest.TestCase):
    def test_failed_requests_are_refreshed_after_audited_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bridge = root / 'bridge'
            bridge.mkdir(mode=0o700)
            put(bridge / 'config.json', {'concurrency': 1})
            folder = bridge / 'requests/turn'
            put(folder / 'request.json', {'submitted_at': 1, 'spec': {}})
            put(folder / 'result.json', {'state': 'failed', 'reason': 'timeout'})
            cache = {}
            self.assertEqual(snapshot(root, temporal_port=0, cache=cache)['bridge_requests'][0]['state'], 'failed')
            from ecarsi.warm_pool.state import digest
            put(folder / 'state.json', {'state': 'queued', 'retry_of': digest({'state': 'failed', 'reason': 'timeout'})})
            self.assertEqual(snapshot(root, temporal_port=0, cache=cache)['bridge_requests'][0]['state'], 'queued')

    def test_dataset_pages_cover_history_before_task_limit(self):
        rows = [dict(id=f"d{d}-t{t}", operation="organize.prepare", state="succeeded",
                     submitted_at=d * 30 + t + 1, started_at=d * 30 + t + 1,
                     finished_at=d * 30 + t + 2,
                     trace=dict(dataset_id=f"dataset-{d}", workflow_id=f"run-{d}", unit_id="organize.prepare"))
                for d in range(100) for t in range(30)]
        seen = set()
        for page in range(10):
            result = task_timeline(rows, [], 0, 4000, dataset_page=page)
            self.assertEqual(result["dataset_total"], 100)
            self.assertFalse(result["truncated"])
            names = {t["trace"]["dataset_id"] for t in result["tasks"]}
            self.assertEqual(len(names), 10)
            self.assertFalse(seen & names)
            seen.update(names)
        self.assertEqual(len(seen), 100)
        limited = task_timeline(rows, [], 0, 4000, limit=20, dataset_page=0)
        self.assertTrue(limited["truncated"])
        self.assertEqual(len({t["trace"]["dataset_id"] for t in limited["tasks"]}), 10)
        self.assertEqual(task_timeline(rows, [], 0, 4000, dataset="dataset-99", dataset_page=9)["dataset_page"], 0)

    def test_inventory_retains_departed_workers_and_distinguishes_same_host_budgets(self):
        with tempfile.TemporaryDirectory() as directory:
            pool = Path(directory)
            for worker, job, times in (("a", "11", [950, 980]), ("b", "12", [975]),
                                       ("old", "10", [50])):
                folder = pool / "workers" / worker
                put(folder / "identity.json", dict(worker_id=worker, host="shared-host",
                    slurm_job_id=job, cpu_ids=[0, 1], memory_mb=1024))
                samples = [dict(worker_id=worker, host="shared-host", observed_at=t,
                    cpu_ids=[0, 1], cpu_percent=20 + i * 40, memory_used_bytes=2**30,
                    memory_total_bytes=4 * 2**30, gpus=[]) for i, t in enumerate(times)]
                (folder / "1970-01-01.jsonl").write_text(
                    "".join(json.dumps(s) + "\n" for s in samples) + '{"observed_at":')
            tasks = [dict(id="active", worker_id="a", state="cancel_requested", cpus=1,
                          memory_mb=512, started_at=940, finished_at=None),
                     dict(id="done", worker_id="a", cpus=2, memory_mb=1024,
                          started_at=920, finished_at=930)]
            cache = {}
            workers = worker_inventory(pool, tasks, 1000, cache)
            a, b, old = workers
            self.assertTrue(a["reporting"] and b["reporting"])
            self.assertFalse(old["reporting"])
            self.assertEqual(old["last_seen"], 50)
            self.assertEqual(a["mean_5m"]["cpu_percent"], 40)
            self.assertEqual(a["current"]["cpu_percent"], 60)
            self.assertIsNone(a["current"]["gpu_percent"])
            self.assertEqual(a["reserved_cpus"], 1)
            self.assertEqual(a["reserved_memory_mb"], 512)
            self.assertEqual([w["slurm_job_id"] for w in workers], ["11", "12", "10"])
            self.assertEqual(worker_inventory(pool, tasks, 1000, cache), workers)
            legacy = worker_inventory(pool, tasks + [dict(id="legacy", host="old-host",
                worker_id=None, submitted_at=1, started_at=2, finished_at=3)], 1000, cache)[-1]
            self.assertTrue(legacy["history_only"])
            self.assertEqual(legacy["last_activity"], 3)
            self.assertIsNone(legacy["last_seen"])

    def test_gpu_registration_is_distinct_from_detection_and_usage(self):
        with tempfile.TemporaryDirectory() as directory:
            pool = Path(directory)
            folder = pool / "workers/a"
            put(folder / "identity.json", dict(worker_id="a", host="gpu-host", cpu_ids=[0],
                gpu_ids=["GPU-a"], allocation={"allocated_tres": "cpu=1,gres/gpu=1"}))
            device = dict(uuid="GPU-a", name="Test GPU", memory_total_mb=24576,
                          memory_used_mb=6144, utilization_percent=80)
            (folder / "1970-01-01.jsonl").write_text(json.dumps(dict(worker_id="a", host="gpu-host",
                observed_at=980, cpu_ids=[0], gpus=[device])) + "\n")
            task = dict(worker_id="a", started_at=975, gpu_ids=["GPU-a"])
            worker = worker_inventory(pool, [task], 1000, {})[0]
            self.assertEqual(worker["gpu_ids"], ["GPU-a"])
            self.assertEqual(worker["gpu_devices"], [device])
            self.assertEqual(worker["reserved_gpus"], 1)
            self.assertEqual(worker["current"]["gpu_memory_percent"], 25)

    def test_generic_timeline_window_filter_limit_and_resources(self):
        pool = [{"id": f"task-{i}", "operation": "per-sample.compute", "state": "succeeded",
                 "trace": {"workflow_id": f"osp/{i}", "dataset_id": f"dataset-{i}",
                           "unit_id": "per-sample.compute"},
                 "submitted_at": i + 1, "started_at": i + 2, "finished_at": i + 3,
                "host": "node-a", "worker_id": "worker-a", "cpu_ids": [7]}
                for i in range(300)]
        pool[42]["trace"]["depends_on"] = ["task-41"]
        timeline = task_timeline(pool, [], 0, 400, limit=2000)
        self.assertEqual(timeline["total"], 300)
        self.assertFalse(timeline["truncated"])
        self.assertEqual(timeline["tasks"][42]["trace"]["dataset_id"], "dataset-42")
        self.assertEqual(timeline["tasks"][42]["trace"]["depends_on"], ["task-41"])
        self.assertEqual(timeline["tasks"][42]["worker_id"], "worker-a")
        self.assertEqual(task_timeline(pool, [], 0, 400, "dataset-42")["total"], 1)
        self.assertTrue(task_timeline(pool, [], 0, 400, limit=20)["truncated"])
        self.assertEqual(task_timeline(pool, [], 100, 110)["total"], 13)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "workers/worker-a/1970-01-01.jsonl"
            path.parent.mkdir(parents=True)
            path.write_text('{"observed_at": 2, "host": "node-a", "worker_id": "worker-a", "cpu_percent": 25, "memory_used_bytes": 50, "memory_total_bytes": 100, "gpus": [{"utilization_percent": 40, "memory_used_mb": 30, "memory_total_mb": 100}]}\n')
            rows = resource_history(Path(directory), 0, 10)
            self.assertEqual(rows[0]["cpu_percent"], 25)
            summary = summarize_resources(rows, 0, 10)[0]
            self.assertEqual((summary["memory_percent"], summary["gpu_percent"],
                              summary["gpu_memory_percent"]), (50, 40, 30))
            with path.open("a") as stream:
                stream.write('{"observed_at": 4')
            cache = {}
            self.assertEqual(len(resource_history(Path(directory), 0, 10, cache)), 1)
            with path.open("a") as stream:
                stream.write(', "host": "node-a", "worker_id": "worker-a"}\n')
            self.assertEqual(len(resource_history(Path(directory), 0, 10, cache)), 2)

    def test_snapshot_is_bounded_to_public_status_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pool = root / "pool"
            bridge = root / "bridge"
            pool.mkdir(mode=0o700)
            bridge.mkdir(mode=0o700)
            put(pool / "config.json", {"protocol": 1})
            put(pool / "scheduler.json", {"state": "stopped", "observed_at": 1})
            request = pool / "requests/sample.prepare"
            put(request / "request.json", {"attempt_id": "a", "submitted_at": 2,
                 "spec": {"request_id": "sample.prepare", "operation_id": "organize.prepare",
                          "cpus": 1, "memory_mb": 128}})
            put(request / "a/receipt.json", {"state": "succeeded", "finished_at": 3,
                                               "peak_rss_bytes": 42})
            put(bridge / "config.json", {"concurrency": 1})
            model = bridge / "requests/sample.plan"
            put(model / "request.json", {"submitted_at": 4, "brief": "private prompt"})
            put(model / "result.json", {"state": "reply_saved", "response": {
                "model": {"harness": "openai", "model": "example"},
                "usage": {"tokens_in": 10, "tokens_out": 5},
                "transcript": "private transcript"}})

            result = snapshot(root, temporal_port=0)
            self.assertEqual(result["pool_requests"][0]["state"], "succeeded")
            self.assertEqual(result["bridge_requests"][0]["usage"]["tokens_in"], 10)
            self.assertNotIn("private prompt", json.dumps(result))
            self.assertNotIn("private transcript", json.dumps(result))


if __name__ == "__main__":
    unittest.main()


def test_release_rows_read_a_batch_directory(tmp_path):
    from ecarsi.observatory import release_rows, render_releases
    dataset = tmp_path / "Organ"
    unit = dataset / "units" / "organ"
    put(unit / "per-sample.json", {"n_input": 100, "n_removed": 30, "n_survived": 70})
    put(unit / "rounds/round01/publication.json", {"stats": {"removed": 7, "n_in": 70, "frac": 0.1}})
    put(unit / "release/needs_review.json", [{"kind": "removed"}, {"kind": "low_confidence"}, {"kind": "removed"}])
    put(unit / "publication.json", {"n_input": 100, "n_survived": 63, "forced_release": False, "reason": "converged",
                                     "per_sample": {"path": str(unit / "per-sample.json")},
                                     "rounds": [{"path": str(unit / "rounds/round01/publication.json")}]})
    put(dataset / "publication.json", {"dataset_id": "TS / Organ", "state": "complete", "units": [{"path": str(unit / "publication.json")}]})
    rows = release_rows([tmp_path])
    assert rows[0]["osp_removed"] == 30 and rows[0]["rounds"][0]["frac"] == 0.1 and rows[0]["review"] == {"removed": 2, "low_confidence": 1}
    assert "TS / Organ" in render_releases(rows) and "30%" in render_releases(rows)

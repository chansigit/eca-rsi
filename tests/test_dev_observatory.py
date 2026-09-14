import json
from pathlib import Path
import tempfile
import unittest

from ecarsi.dev_observatory import resource_history, snapshot, summarize_resources, task_timeline


def put(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


class ObservatoryTest(unittest.TestCase):
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
            pool = root / "organize-v2-pool"
            bridge = root / "organize-v2-bridge"
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
            output = root / "organize-v2-sample" / "temporal-output"
            put(output / "publication.json", {"accepted": True})
            put(output / "organize/manifest.json", {"state": "complete",
                "experiment_audit": {"sample": {"experiments": 2}},
                "units_written": [{"n_cells": 21}]})
            put(root / "multinode-1/run-1/acceptance.json",
                {"passed": True, "tests": ["worker_reconnection"],
                 "nodes": [{"host": "node-a", "worker_cpu": 7}]})

            result = snapshot(root, temporal_port=0)
            self.assertEqual(result["pool_requests"][0]["state"], "succeeded")
            self.assertEqual(result["bridge_requests"][0]["usage"]["tokens_in"], 10)
            self.assertEqual(result["outputs"][0]["samples"], 2)
            self.assertEqual(result["outputs"][0]["cells"], 21)
            self.assertTrue(result["acceptance"]["passed"])
            self.assertNotIn("private prompt", json.dumps(result))
            self.assertNotIn("private transcript", json.dumps(result))


if __name__ == "__main__":
    unittest.main()

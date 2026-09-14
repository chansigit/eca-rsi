import json
from pathlib import Path
import tempfile
import unittest

from ecarsi.dev_observatory import snapshot


def put(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


class ObservatoryTest(unittest.TestCase):
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

import importlib
import json
import sys
import threading
import time
import types
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from ecarsi import compute_policy


def test_backend_record_is_pinned_and_bad_record_fails(tmp_path):
    ad = SimpleNamespace(n_obs=6000)
    output = tmp_path / "new"
    assert compute_policy._choose(output, ad, lambda _: True) == "gpu"
    assert compute_policy._choose(output, ad, lambda _: False) == "gpu"
    record = output / ".rsi-compute-backend.json"
    assert json.loads(record.read_text())["backend"] == "gpu"
    (output / "integrated.h5ad").touch()
    assert compute_policy._choose(output, ad, lambda _: False) == "cpu"
    assert json.loads(record.read_text())["backend"] == "cpu"
    archived = list((output / ".msp-history").glob("compute-backend-*.json"))
    assert len(archived) == 1 and json.loads(archived[0].read_text())["backend"] == "gpu"
    (output / ".msp-state").mkdir()
    (output / ".msp-state" / "integrate.pending").touch()
    assert compute_policy._choose(output, ad, lambda _: True) == "cpu"  # interrupted rebuild
    record.write_text('{"backend": "broken"}')
    with pytest.raises(ValueError, match="invalid compute backend record"):
        compute_policy._choose(output, ad, lambda _: False)

    rebuild = tmp_path / "stale-legacy-result"
    rebuild.mkdir()
    (rebuild / "integrated.h5ad").touch()
    # ZMIP calls integrate_adata only after cache.valid rejects this file;
    # MSP archives it immediately. It must not force the old GPU backend.
    assert compute_policy._choose(rebuild, ad, lambda _: False) == "cpu"
    assert json.loads((rebuild / ".rsi-compute-backend.json").read_text())["backend"] == "cpu"


def test_installed_aliases_keep_concurrent_lineages_independent(tmp_path, monkeypatch):
    policy = importlib.reload(compute_policy)
    msp = types.ModuleType("msp")
    msp.__path__ = []
    compute = types.ModuleType("msp.compute")
    integrate = types.ModuleType("msp.integrate")
    integrate.__path__ = []
    pipeline = types.ModuleType("msp.integrate.pipeline")
    deg = types.ModuleType("msp.integrate.deg")
    barrier = threading.Barrier(2)
    compute.gpu_requested = lambda: False
    pipeline.gpu_requested = deg.gpu_requested = compute.gpu_requested

    def original(ad, batch_col, outdir):
        barrier.wait(timeout=5)
        return pipeline.gpu_requested(), deg.gpu_requested(), str(outdir)

    pipeline.integrate_adata = integrate.integrate_adata = original
    integrate.pipeline, integrate.deg = pipeline, deg
    msp.compute, msp.integrate = compute, integrate
    for name, module in (("msp", msp), ("msp.compute", compute), ("msp.integrate", integrate),
                         ("msp.integrate.pipeline", pipeline), ("msp.integrate.deg", deg)):
        monkeypatch.setitem(sys.modules, name, module)

    policy.install(probe=lambda ad: ad.gpu)
    with ThreadPoolExecutor(max_workers=2) as pool:
        one = pool.submit(integrate.integrate_adata, SimpleNamespace(n_obs=6000, gpu=True), "batch", tmp_path / "one")
        two = pool.submit(integrate.integrate_adata, SimpleNamespace(n_obs=6000, gpu=False), "batch", tmp_path / "two")
        assert one.result()[:2] == (True, True)
        assert two.result()[:2] == (False, False)
    assert compute.gpu_requested() is False  # context was reset
    assert json.loads((tmp_path / "one" / ".rsi-compute-backend.json").read_text())["backend"] == "gpu"
    assert json.loads((tmp_path / "two" / ".rsi-compute-backend.json").read_text())["backend"] == "cpu"


def test_gpu_probe_requires_a_free_compatible_slot(monkeypatch):
    from ecarsi.pool import client as pool_client

    now = time.time()
    runtime = {"python": "3.11", "ecarsi": "same", "msp": "same", "numpy": "same",
               "scipy": "same", "scanpy": "same", "anndata": "same",
               "rapids-singlecell-cu12": "same", "cupy-cuda12x": "same"}
    worker = {"cpus": 4, "memory": 16 << 30, "gpus": 1, "gpu_ids": ["gpu0"],
              "end_time": now + 3600, "observed_at": now, "runtime": runtime,
              "runtimes": {"default": runtime}, "task_slots": 2,
              "gpu_stats": [{"uuid": "gpu0", "memory_total_mib": 24576, "memory_used_mib": 512}]}
    state = {"workers": {"worker0": worker}, "tasks": {}}

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *unused):
            pass

        def run_on_scheduler(self, *unused):
            return state

        def scheduler_info(self):
            return {"workers": {"worker0": {}}}

    monkeypatch.setattr(pool_client, "connect", lambda **unused: Client())
    monkeypatch.setattr(pool_client, "runtime", lambda: runtime)
    ad = SimpleNamespace(n_obs=6000, X=SimpleNamespace(nbytes=1 << 20))
    assert compute_policy.gpu_available(ad)
    state["tasks"]["busy"] = {"state": "running", "worker": "worker0", "cpus": 1,
                               "memory": 1 << 30, "gpus": 1, "gpu_ids": ["gpu0"]}
    assert not compute_policy.gpu_available(ad)
    state["tasks"].clear()
    worker["runtimes"]["default"] = {**runtime, "rapids-singlecell-cu12": "different"}
    assert not compute_policy.gpu_available(ad)

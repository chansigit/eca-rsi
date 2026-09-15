import tomllib

import pytest

from ecarsi.warm_pool.backend import gpu_jobfile
from ecarsi.warm_pool.state import validate
from ecarsi.warm_pool.worker import assigned_gpu


def test_gpu_alternatives_preserve_budget_and_never_use_inherited_visibility(tmp_path, monkeypatch):
    spec = validate(dict(request_id="a", operation_id="osp.compute", cpus=2, memory_mb=4096,
                         timeout_seconds=60, args=["-c", "pass"], outputs=["result.json"],
                         gpu={"mode": "preferred", "memory_mb": 8192}))
    request = dict(spec=spec, attempt_id="attempt", runtime_digest="runtime")
    attempt = tmp_path / "requests/a/attempt"
    data = tomllib.loads(gpu_jobfile(request, attempt, "test", "/python", "/code"))
    task = data["task"][0]
    assert task["pin"] == "taskset" and task["crash_limit"] == "never-restart"
    gpu, cpu = task["request"]
    assert gpu["resources"]["gpus/nvidia"] == 1 and gpu["resources"]["gpuMemoryMB"] == 8192
    assert "gpus/nvidia" not in cpu["resources"]
    assert gpu["time_request"] == cpu["time_request"] == "90s"
    assert str(tmp_path) in task["command"]
    monkeypatch.setattr("ecarsi.warm_pool.allocation.gpu_device", lambda _: {"memory_mb": 24576})
    assert assigned_gpu(spec, {"HQ_RESOURCE_VARIANT": "1", "CUDA_VISIBLE_DEVICES": "GPU-worker"}) is None
    assert assigned_gpu(spec, {"HQ_RESOURCE_VARIANT": "0", "HQ_RESOURCE_VALUES_gpus_nvidia": "GPU-test"}) == "GPU-test"
    with pytest.raises(ValueError):
        assigned_gpu(spec, {"HQ_RESOURCE_VARIANT": "0"})
    with pytest.raises(ValueError):
        assigned_gpu(spec, {"HQ_RESOURCE_VARIANT": "1", "HQ_RESOURCE_VALUES_gpus_nvidia": "GPU-test"})
    spec["gpu"]["mode"] = "required"
    assert len(tomllib.loads(gpu_jobfile(request, attempt, "test", "/python", "/code"))["task"][0]["request"]) == 1
    with pytest.raises(ValueError):
        assigned_gpu(spec, {"HQ_RESOURCE_VARIANT": "1"})
    for invalid in ({"mode": "preferred", "memory_mb": True}, {"mode": "magic", "memory_mb": 10}):
        with pytest.raises(ValueError):
            validate({**spec, "gpu": invalid})

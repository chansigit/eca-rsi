import tomllib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from ecarsi.warm_pool.backend import GPU_SLOT_LIMIT, gpu_jobfile, gpu_resources, resource_sample
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
    gpu, cpu = task["request"][0], task["request"][-1]
    assert len(task["request"]) == GPU_SLOT_LIMIT + 1
    assert gpu["resources"]["gpuSlot/0"] == 1 and gpu["resources"]["gpuMemoryMB/0"] == 8192
    assert not any(key.startswith("gpu") for key in cpu["resources"])
    assert gpu["time_request"] == cpu["time_request"] == "90s"
    assert str(tmp_path) in task["command"]
    monkeypatch.setattr("ecarsi.warm_pool.allocation.gpu_device", lambda _: {"memory_mb": 24576})
    slot = {"ECA_POOL_GPU_LAYOUT": "slots-v1", "HQ_RESOURCE_VARIANT": "1", "HQ_RESOURCE_VALUES_gpuSlot_1": "GPU-test"}
    assert assigned_gpu(spec, slot) == "GPU-test"
    assert assigned_gpu(spec, {"ECA_POOL_GPU_LAYOUT": "slots-v1", "HQ_RESOURCE_VARIANT": str(GPU_SLOT_LIMIT)}) is None
    for wrong in ({**slot, "HQ_RESOURCE_VARIANT": "0"}, {**slot, "HQ_RESOURCE_VALUES_gpuSlot_0": "GPU-other"},
                  {"ECA_POOL_GPU_LAYOUT": "slots-v1", "HQ_RESOURCE_VARIANT": "1"}):
        with pytest.raises(ValueError):
            assigned_gpu(spec, wrong)
    assert assigned_gpu(spec, {"HQ_RESOURCE_VARIANT": "1", "CUDA_VISIBLE_DEVICES": "GPU-worker"}) is None
    assert assigned_gpu(spec, {"HQ_RESOURCE_VARIANT": "0", "HQ_RESOURCE_VALUES_gpus_nvidia": "GPU-test"}) == "GPU-test"
    with pytest.raises(ValueError):
        assigned_gpu(spec, {"HQ_RESOURCE_VARIANT": "0"})
    with pytest.raises(ValueError):
        assigned_gpu(spec, {"HQ_RESOURCE_VARIANT": "1", "HQ_RESOURCE_VALUES_gpus_nvidia": "GPU-test"})
    spec["gpu"]["mode"] = "required"
    assert len(tomllib.loads(gpu_jobfile(request, attempt, "test", "/python", "/code"))["task"][0]["request"]) == GPU_SLOT_LIMIT
    with pytest.raises(ValueError):
        assigned_gpu(spec, {"HQ_RESOURCE_VARIANT": "1"})
    for invalid in ({"mode": "preferred", "memory_mb": True}, {"mode": "magic", "memory_mb": 10}):
        with pytest.raises(ValueError):
            validate({**spec, "gpu": invalid})


def test_multigpu_memory_is_per_device_and_telemetry_excludes_unallocated_cards(monkeypatch):
    devices = [dict(uuid="GPU-a1", memory_mb=16384), dict(uuid="GPU-b2", memory_mb=49152)]
    args = gpu_resources(devices)
    assert 'gpuSlot/0=[GPU-a1]' in args and 'gpuSlot/1=[GPU-b2]' in args
    assert "gpuMemoryMB/0=sum(14745)" in args and "gpuMemoryMB/1=sum(44236)" in args
    assert len(args) == 8
    from types import SimpleNamespace
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=
        "GPU-a1, A, 20, 100, 16384\nGPU-b2, B, 40, 200, 49152\nGPU-c3, C, 90, 100, 16384\n"))
    sample, _ = resource_sample(list(os.sched_getaffinity(0)), gpu_ids=["GPU-a1", "GPU-b2"])
    assert [g["uuid"] for g in sample["gpus"]] == ["GPU-a1", "GPU-b2"]


@pytest.mark.skipif(not os.environ.get("HQ_TEST_BINARY"), reason="opt-in native HQ scheduling acceptance")
def test_native_hq_one_worker_schedules_multiple_gpu_slots(tmp_path):
    """Real HQ and subprocesses, synthetic GPU descriptors; no GPU arithmetic claim."""
    cpus = sorted(os.sched_getaffinity(0))[:3]
    if len(cpus) < 3:
        pytest.skip("requires three explicitly available CPUs")
    command = [os.environ["HQ_TEST_BINARY"], "--server-dir", str(tmp_path / "hq"), "--output-mode", "json"]
    actors = []
    def call(*args):
        result = subprocess.run(command + list(args), capture_output=True, text=True, check=True, timeout=10)
        return json.loads(result.stdout) if result.stdout.strip() else None
    def wait(check):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                if value := check():
                    return value
            except (subprocess.SubprocessError, FileNotFoundError):
                pass
            time.sleep(.1)
        raise AssertionError(f"native HQ acceptance timed out; inspect {tmp_path}")
    def record(name):
        return json.loads((tmp_path / (name + ".json")).read_text())
    def submit_probe(name, gpu_mb=None, hold=False, host_mb=64, mode="required"):
        target = tmp_path / (name + ".json")
        release = tmp_path / (name + ".release")
        code = ("import json,os,time; from pathlib import Path; "
                f"p=Path({str(target)!r}); r=Path({str(release)!r}); "
                "p.write_text(json.dumps(dict(gpu=next((v for k,v in os.environ.items() if k.startswith('HQ_RESOURCE_VALUES_gpuSlot_')),None),"
                "variant=os.environ.get('HQ_RESOURCE_VARIANT'),started=time.time()))); "
                + ("\nwhile not r.exists(): time.sleep(.1)\n" if hold else "\n") +
                "data=json.loads(p.read_text()); data['finished']=time.time(); p.write_text(json.dumps(data))")
        if gpu_mb is None:
            return call("submit", "--cpus", "1", "--resource", f"mem={host_mb}", "--pin", "taskset",
                        "--directives", "off", "--stdout", str(tmp_path / (name + ".stdout")),
                        "--stderr", str(tmp_path / (name + ".stderr")), sys.executable, "-c", code)
        request = dict(spec=validate(dict(request_id=name, operation_id="test.gpu", cpus=1, memory_mb=host_mb,
                       timeout_seconds=60, args=["-c", "pass"], outputs=["result.json"],
                       gpu={"mode": mode, "memory_mb": gpu_mb})), attempt_id="attempt", runtime_digest="test")
        attempt = tmp_path / "requests" / name / "attempt"
        attempt.mkdir(parents=True)
        text = gpu_jobfile(request, attempt, name, sys.executable, str(Path(__file__).resolve().parents[1]))
        # Keep the production resource request; replace only the scientific payload.
        text = "\n".join("command = " + json.dumps([sys.executable, "-c", code])
                         if line.startswith("command = ") else line for line in text.splitlines())
        path = attempt / "job.toml"
        path.write_text(text)
        return call("job", "submit-file", str(path))
    try:
        with (tmp_path / "actors.log").open("a") as log:
            actors.append(subprocess.Popen(command + ["server", "start", "--host", "127.0.0.1"], stdout=log, stderr=log))
            wait(lambda: call("server", "info"))
            submit_probe("large", gpu_mb=32 * 1024, hold=True)
            assert not (tmp_path / "large.json").exists()
            devices = [dict(uuid="GPU-a1", memory_mb=16384), dict(uuid="GPU-b2", memory_mb=49152)]
            actors.append(subprocess.Popen(command + ["worker", "start", "--manager", "none", "--detect-resources", "none",
                "--cpus", json.dumps(cpus), "--resource", "mem=sum(160)", "--resource", "runtime/test=sum(3)",
                "--time-limit", "120s", "--work-dir", str(tmp_path / "worker")] + gpu_resources(devices), stdout=log, stderr=log))
            workers = wait(lambda: call("worker", "list"))
            assert len(workers) == 1
            submit_probe("oversized", gpu_mb=50 * 1024)
            submit_probe("host-oversized", gpu_mb=1024, host_mb=161)
            assert wait(lambda: record("large"))["gpu"] == "GPU-b2"
            submit_probe("small", gpu_mb=8 * 1024, hold=True)
            assert wait(lambda: record("small"))["gpu"] == "GPU-a1"
            submit_probe("third", gpu_mb=2048)
            submit_probe("cpu", host_mb=32)
            wait(lambda: record("cpu").get("finished"))
            submit_probe("fallback", gpu_mb=2048, host_mb=32, mode="preferred")
            wait(lambda: record("fallback").get("finished"))
            assert record("fallback")["gpu"] is None and record("fallback")["variant"] == str(GPU_SLOT_LIMIT)
            assert not (tmp_path / "third.json").exists(), "a busy GPU was granted twice"
            assert record("cpu")["gpu"] is None
            assert not (tmp_path / "oversized.json").exists(), "GPU memories were incorrectly summed"
            assert not (tmp_path / "host-oversized.json").exists(), "host memory budget was exceeded"
            (tmp_path / "large.release").touch()
            wait(lambda: record("large").get("finished"))
            wait(lambda: record("third").get("finished"))
            assert record("third")["gpu"] == "GPU-b2"
            assert record("third")["started"] >= record("large")["finished"]
            submit_probe("preferred", gpu_mb=2048, host_mb=32, mode="preferred")
            wait(lambda: record("preferred").get("finished"))
            assert record("preferred")["gpu"] == "GPU-b2"
            (tmp_path / "small.release").touch()
            wait(lambda: record("small").get("finished"))
            assert not (tmp_path / "oversized.json").exists()
            (tmp_path / "acceptance.json").write_text(json.dumps(dict(
                kind="native HQ scheduling with synthetic GPU descriptors", worker_count=1,
                large=record("large"), small=record("small"), third=record("third"), cpu=record("cpu"),
                fallback=record("fallback"), preferred=record("preferred")), indent=2))
    finally:
        for proc in reversed(actors):
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=10)

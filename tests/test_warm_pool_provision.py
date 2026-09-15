from pathlib import Path
import shlex
from types import SimpleNamespace
import time

import pytest

from ecarsi.warm_pool import provision
from ecarsi.warm_pool.state import read, save


def profile(**changes):
    return dict(dict(host="node1", job_id="123", cpu_ids=[4, 7], cpus=2,
                     process_memory=32 * 2**30, allocated_tres="cpu=2,mem=32G",
                     end_time=time.time() + 600), **changes)


def test_gpu_grant_selects_step_and_ignores_unallocated_visible_hardware(monkeypatch, tmp_path):
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "0")
    monkeypatch.setenv("SLURM_JOB_GPUS", "0")
    cpu = provision.worker_command(tmp_path, profile(), ["python"], tmp_path, [4, 7], 100, None)
    assert "--gpu" not in cpu and "srun" not in cpu
    with pytest.raises(ValueError, match="no GPU grant"):
        provision.worker_command(tmp_path, profile(), ["python"], tmp_path, [4, 7], 100, True)
    monkeypatch.delenv("CUDA_VISIBLE_DEVICES")
    monkeypatch.delenv("SLURM_JOB_GPUS")
    monkeypatch.delenv("SLURM_STEP_GPUS", raising=False)
    granted = profile(allocated_tres="cpu=2,mem=32G,gres/gpu=1,gres/gpu:rtx=1")
    command = provision.worker_command(tmp_path, granted, ["python"], tmp_path, [4, 7], 100, None)
    assert command[0] == "srun" and "--jobid=123" in command and "--gpus-per-task=1" in command
    assert "--gpu" in command and "--nodelist=node1" in command
    assert provision.allocated_gpus(granted) == 1
    assert provision.allocated_gpus(profile(allocated_tres="gres/gpu:rtx=2")) == 2
    multi = profile(allocated_tres="gres/gpu=2")
    command = provision.worker_command(tmp_path, multi, ["python"], tmp_path, [4, 7], 100, None)
    assert command.count("slurm-worker") == 1 and "--gpus-per-task=2" in command
    # A partial inherited step must not silently hide the allocation's second GPU.
    monkeypatch.setenv("SLURM_STEP_GPUS", "0")
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "0")
    assert provision.worker_command(tmp_path, multi, ["python"], tmp_path, [4, 7], 100, None)[0] == "srun"
    monkeypatch.setenv("SLURM_STEP_GPUS", "0,1")
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "0,1")
    assert provision.worker_command(tmp_path, multi, ["python"], tmp_path, [4, 7], 100, None)[0] != "srun"


def test_runtime_uses_configured_image_and_pythonpath():
    runtime = dict(image={"path": "/images/science-current.sif"}, command=["/runtime/python"], pythonpath=["/repo", "/science"])
    command = provision.runtime_prefix(runtime, ["/shared", "/fast"])
    assert command[:3] == ["apptainer", "exec", "--cleanenv"]
    assert command[-2:] == ["/images/science-current.sif", "/runtime/python"]
    assert "PYTHONPATH=/repo:/science" in command and command.count("--bind") == 2


def test_default_budget_startup_and_duplicate_registration(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    save(tmp_path / "config.json", dict(runtime={"command": ["python"]}))
    monkeypatch.setattr(provision, "inventory", lambda *_: profile())
    registrations = []
    monkeypatch.setattr(provision, "registered_workers", lambda *_: registrations)
    owner = {"pid": 123, "start_ticks": 4, "boot_id": "boot"}
    monkeypatch.setattr(provision, "process_identity", lambda _: owner)
    launches = []
    def start(command, **kwargs):
        launches.append(command)
        work = Path(command[command.index("--work-dir") + 1])
        save(work / "worker.json", {"pid": 123})
        identity_dir = tmp_path / "workers" / "worker1"
        identity_dir.mkdir(parents=True)
        save(identity_dir / "identity.json", dict(host="node1", slurm_job_id="123", cpu_ids=[4, 7], memory_mb=29491,
                                                  gpu_ids=[], work_dir=str(work)))
        registrations.append(dict(id=58, ended=None, configuration={"work_dir": str(work)}))
        assert kwargs["start_new_session"] is True
        return SimpleNamespace(pid=123)
    monkeypatch.setattr(provision.subprocess, "Popen", start)
    result = provision.add_worker(tmp_path)
    assert result["state"] == "online" and result["memory_mb"] == 29491 and result["cpus"] == 2
    assert launches[0][launches[0].index("--cpus") + 1] == "4,7"
    assert read(Path(result["work_dir"]) / "startup.json")["process"] == owner
    again = provision.add_worker(tmp_path)
    assert again["already_running"] is True and len(launches) == 1
    assert again.keys() == result.keys() and again["hq_worker_id"] == result["hq_worker_id"]
    with pytest.raises(ValueError, match="90%"):
        provision.add_worker(tmp_path, memory_mb=32768)
    with pytest.raises(ValueError, match="outside"):
        provision.add_worker(tmp_path, cpu_ids=[0])
    with pytest.raises(ValueError, match="differs"):
        provision.add_worker(tmp_path, job_id="456")


def test_startup_failure_reports_log_without_claiming_online(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    save(tmp_path / "config.json", dict(runtime={"command": ["python"]}))
    monkeypatch.setattr(provision, "inventory", lambda *_: profile())
    monkeypatch.setattr(provision, "registered_workers", lambda *_: [])
    monkeypatch.setattr(provision, "process_identity", lambda _: None)
    monkeypatch.setattr(provision.subprocess, "Popen", lambda *a, **k: SimpleNamespace(pid=123))
    with pytest.raises(RuntimeError, match="startup failed; see .*startup.log"):
        provision.add_worker(tmp_path)


def test_remote_command_preserves_paths_and_rejects_shell_hostname(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    save(tmp_path / "config.json", dict(runtime={"command": ["python"]}))
    calls = []
    monkeypatch.setattr(provision.subprocess, "run", lambda command, **_: calls.append(command))
    provision.add_worker(tmp_path, "node1", host_python="/shared/a b/python", job_id="123", gpu=False)
    command = shlex.split(calls[0][-1])
    assert "/shared/a b/python" in command and "--no-gpu" in command and command[command.index("--job-id") + 1] == "123"
    with pytest.raises(ValueError, match="hostname"):
        provision.add_worker(tmp_path, "node1; echo invalid")
    assert len(calls) == 1


def test_timed_out_startup_is_reused_and_cannot_change_budget(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    save(tmp_path / "config.json", dict(runtime={"command": ["python"]}))
    monkeypatch.setattr(provision, "inventory", lambda *_: profile())
    monkeypatch.setattr(provision, "registered_workers", lambda *_: [])
    owner = {"pid": 123, "start_ticks": 4, "boot_id": "boot"}
    monkeypatch.setattr(provision, "process_identity", lambda _: owner)
    launches = []
    monkeypatch.setattr(provision.subprocess, "Popen", lambda *a, **k: (launches.append(a), SimpleNamespace(pid=123))[1])
    ticks = iter(range(100))
    monkeypatch.setattr(provision.time, "monotonic", lambda: next(ticks))
    for _ in range(2):
        with pytest.raises(TimeoutError, match="left running"):
            provision.add_worker(tmp_path, wait_seconds=1)
    assert len(launches) == 1
    with pytest.raises(ValueError, match="different settings"):
        provision.add_worker(tmp_path, memory_mb=1024)

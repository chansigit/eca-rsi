import os
from pathlib import Path
import socket
import time

import pytest

from ecarsi.warm_pool import allocation
from ecarsi.warm_pool.state import read, save


def test_slurm_profile_cannot_cross_jobs_or_expand_the_grant(tmp_path, monkeypatch):
    monkeypatch.setattr(allocation, "current_job", lambda: "123")
    cpus = [min(os.sched_getaffinity(0))]
    profile = dict(job_id="123", host=socket.gethostname().split(".")[0],
                   boot_id=Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
                   cpu_ids=cpus, memory=2**20, allocation_memory=2**30,
                   observed_at=time.time(), end_time=time.time() + 600)
    path = tmp_path / "allocation.json"
    save(path, profile)
    assert allocation.validate_profile(path, cpus, 1) == profile
    with pytest.raises(ValueError, match="slurm-worker"):
        allocation.validate_profile(None, cpus, 1)
    for change in ({"job_id": "456"}, {"observed_at": time.time() - 100},
                   {"end_time": time.time() + 30}, {"memory": 2**31}, {"end_time": float("inf")}):
        if change.get("end_time") == float("inf"):
            path.write_text(__import__("json").dumps({**profile, **change}))
        else:
            save(path, {**profile, **change})
        with pytest.raises(ValueError):
            allocation.validate_profile(path, cpus, 1)


def test_shared_memory_ledger_keeps_uncertain_workers_reserved(tmp_path, monkeypatch):
    from ecarsi.pool.budget import reserve
    monkeypatch.setenv("HOME", str(tmp_path))
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir(); second.mkdir()
    profile = dict(host="test", job_id="123", cpu_ids=[0], memory=80, allocation_memory=100)
    reserve(profile, "first", first / "owner.lock", role="worker", worker_directory=first)
    # A released owner lock alone cannot prove detached executors have exited.
    with pytest.raises(ValueError, match="exceed"):
        reserve({**profile, "cpu_ids": [1], "memory": 30}, "second", second / "owner.lock",
                role="worker", worker_directory=second)
    save(first / "launcher.json", dict(state="stopped", updated_at=time.time()))
    reserve({**profile, "cpu_ids": [1], "memory": 30}, "second", second / "owner.lock",
            role="worker", worker_directory=second)
    entries = read(tmp_path / ".cache/ecarsi-pool/test/budget-123.json")
    assert set(entries) == {"second"}


def test_slurm_launch_passes_every_granted_gpu_to_one_worker(tmp_path, monkeypatch):
    profile = dict(job_id="123", gpu_ids=["GPU-a1", "GPU-b2"], cpu_ids=[4, 7])
    monkeypatch.setattr("ecarsi.pool.slurm.inventory", lambda *a, **k: profile)
    monkeypatch.setenv("APPTAINER_NV", "0")
    monkeypatch.setenv("APPTAINERENV_CUDA_VISIBLE_DEVICES", "")
    commands = []
    monkeypatch.setattr(allocation.os, "execvp", lambda executable, args: commands.append(args))
    allocation.launch(tmp_path, [4, 7], 1024, tmp_path / "worker", ["python"], job_id="123", gpu=True)
    assert len(commands) == 1 and commands[0].count("--gpu") == 2
    assert commands[0][-4:] == ["--gpu", "GPU-a1", "--gpu", "GPU-b2"]
    assert os.environ["APPTAINERENV_CUDA_VISIBLE_DEVICES"] == "GPU-a1,GPU-b2"
    assert read(next((tmp_path / "worker").glob("allocation-*.json")))["gpu_ids"] == profile["gpu_ids"]

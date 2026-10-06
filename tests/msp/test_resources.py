"""Resource discovery against cgroup fixture trees and patched affinity."""

import os

from msp.resources import available_cpus

GiB = 1 << 30


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_available_cpus_uses_affinity_and_the_thread_cap(monkeypatch):
    monkeypatch.setattr(os, "sched_getaffinity", lambda pid: {0, 1, 2, 3, 4, 5}, raising=False)
    monkeypatch.delenv("MSP_MAX_THREADS", raising=False)
    assert available_cpus() == 6
    monkeypatch.setenv("MSP_MAX_THREADS", "2")
    assert available_cpus() == 2
    monkeypatch.setenv("MSP_MAX_THREADS", "not a number")
    assert available_cpus() == 6
    monkeypatch.setattr(os, "sched_getaffinity", lambda pid: (_ for _ in ()).throw(AttributeError()), raising=False)
    assert available_cpus() >= 1



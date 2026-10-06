"""What this process may actually use — inside a Slurm allocation the node's
totals are a lie (os.cpu_count() reports the node's 32 cores while the job
was given 8; psutil sees 250 GB while --mem was 48G). Nothing here needs the
caller to pass anything in: CPU comes from the scheduler-set affinity mask,
memory from the enforced cgroup limit (walking up the hierarchy and taking
the tightest bound — Slurm's step cgroup can be tighter than the job's).
Outside a cgroup/affinity-restricted environment both fall back to the
machine totals. Used for parallelism decisions (integrate's DEG pool, zmip's
per-lineage concurrency)."""

from __future__ import annotations

import os

_UNLIMITED = 1 << 60  # cgroup v1 reports ~9.2e18 for "no limit"


def _env_int(name: str) -> int | None:
    try:
        v = int(os.environ.get(name, ""))
        return v if v > 0 else None
    except ValueError:
        return None


def available_cpus() -> int:
    """CPUs this process may use: the scheduler affinity mask (what Slurm /
    docker --cpuset actually grant), capped by MSP_MAX_THREADS when a parent
    that runs several of us side by side (zmip's lineage pool) sets it. Never
    raises: no affinity API (macOS), no /proc, or a container that hides it
    all fall back to os.cpu_count(), then to 1."""
    try:
        n = len(os.sched_getaffinity(0))  # type: ignore[attr-defined]
    except Exception:
        n = 0
    if n <= 0:
        try:
            n = os.cpu_count() or 1
        except Exception:
            n = 1
    cap = _env_int("MSP_MAX_THREADS")
    return max(1, min(n, cap) if cap else n)

"""Retry wrapper for the cheap, side-effect-free agent-only calls (plan,
sample-column identification): under concurrent Slurm job start, many
`claude` CLI subprocesses initializing at once can blow the SDK's control
handshake ("Control request timeout: initialize") or die with a transient
connection error. These calls do nothing but read + return structured
output, so a bare retry is safe — no partial state to clean up.

Heavier steps (persample driving, crosssample, zoomin) are not wrapped here:
they write real files and are already resumable at the eca-rsi step level
(the sbatch wrapper retries `eca-rsi run` itself, which reuses that resume).
"""

from __future__ import annotations

import time
from typing import Callable, Coroutine, TypeVar

T = TypeVar("T")

MAX_ATTEMPTS = 6
BACKOFF_SECONDS = 20  # linear: 20s, 40s, 60s, 80s, 100s


def run_with_retry(coro_fn: Callable[[], Coroutine[object, object, T]], label: str) -> T:
    """asyncio.run(coro_fn()) with retries on transient SDK/agent failures."""
    import asyncio

    last_exc: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return asyncio.run(coro_fn())
        except Exception as e:  # noqa: BLE001 - deliberately broad, see module docstring
            last_exc = e
            if attempt == MAX_ATTEMPTS:
                break
            wait = BACKOFF_SECONDS * attempt
            print(f"[retry] {label} attempt {attempt}/{MAX_ATTEMPTS} failed "
                  f"({type(e).__name__}: {e}); retrying in {wait}s")
            time.sleep(wait)
    assert last_exc is not None
    raise last_exc

"""Compute endpoint: *where* a heavy step runs. One backend, ``local`` (same process, a direct call);
``MSP_COMPUTE_ENDPOINT`` stays as the switch and any other value raises, so a stale setting never passes
unnoticed. eca-rsi runs msp inside its own pool tasks (the dask backends were removed with #28).

    with resolve_endpoint() as ep:
        result = ep.submit(pure_fn, array_in, ...).result()
"""

from __future__ import annotations

import os
from collections.abc import Callable
from concurrent.futures import Future
from typing import Any, Protocol


class ComputeEndpoint(Protocol):
    """Shaped like ``concurrent.futures.Executor``."""

    def submit(self, fn: Callable[..., Any], *args: Any, tier: str = "cpu", **kwargs: Any) -> Future: ...
    def __enter__(self) -> ComputeEndpoint: ...
    def __exit__(self, *exc: Any) -> None: ...


class LocalEndpoint:
    """Runs ``fn`` synchronously in-process. Same call, same thread, same
    timing as calling ``fn`` directly -- the default, and the only backend
    that needs no extra dependency. ``tier`` is ignored: in-process, the
    GPU is whatever this process can see."""

    def submit(self, fn: Callable[..., Any], *args: Any, tier: str = "cpu", **kwargs: Any) -> Future:
        fut: Future = Future()
        try:
            fut.set_result(fn(*args, **kwargs))
        except BaseException as exc:  # mirror Executor.submit: exceptions surface via .result()
            fut.set_exception(exc)
        return fut

    def __enter__(self) -> LocalEndpoint:
        return self

    def __exit__(self, *exc: Any) -> None:
        pass


def gpu_requested() -> bool:
    """``MSP_COMPUTE_GPU=1``: call sites pick their rapids-singlecell
    implementation and submit it with ``tier="gpu"``. Unset (default) is
    the CPU path, byte for byte. Numerics differ between the two -- accepted."""
    return os.environ.get("MSP_COMPUTE_GPU", "0") == "1"


def resolve_endpoint() -> ComputeEndpoint:
    """``MSP_COMPUTE_ENDPOINT`` (default ``local``) picks the backend. An
    unknown name raises loudly instead of falling back to ``local``."""
    kind = os.environ.get("MSP_COMPUTE_ENDPOINT", "local")
    if kind == "local":
        return LocalEndpoint()
    raise ValueError(f"unknown MSP_COMPUTE_ENDPOINT={kind!r}")

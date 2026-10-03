"""ecarsi.cost -- record an agent call's spend and tokens as a `cost` event in the unit's progress.log."""

from __future__ import annotations

from pathlib import Path

from . import layout as L


def record(unit: Path, step: str, usd: float | None, label: str = "",
           tokens_in: int | None = None, tokens_out: int | None = None) -> None:
    """One agent run's spend/usage -> progress.log (no-op when the backend gave neither)."""
    if usd is None and tokens_in is None and tokens_out is None:
        return
    parts = [f"step={step}"]
    if usd is not None:
        parts.append(f"usd={usd:.4f}")
    if tokens_in is not None:
        parts.append(f"tokens_in={tokens_in}")
    if tokens_out is not None:
        parts.append(f"tokens_out={tokens_out}")
    if label:
        parts.append(f"label={label}")
    L.log_event(unit, "cost " + " ".join(parts), echo=False)

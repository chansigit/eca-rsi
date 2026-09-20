"""Copy what a person reads out of the pool and into the run directory.

A finished stage leaves its report, figures and tables inside the pool request that
produced them, and its publication references them by path and digest. That request is
a replay cache the run does not own: archiving or clearing it would orphan every report.
The matrices stay where they are -- only the light artefacts are copied, which is also
what `--mirror` and the release keep.

The copy itself lives with the stage programs: a stage assembling a report out of other
requests' outputs needs exactly the same operation before it can render.
"""
from ..stages.contract import HEAVY, copy_light

__all__ = ["HEAVY", "copy_light"]

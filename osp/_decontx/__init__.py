"""osp._decontx -- vendored DecontX implementation.

Vendored (not an external dependency) from ``pydecontx`` /
``py-decontx`` (https://github.com/omicverse/py-decontx), itself a
pure-Python port of the Bioconductor ``decontX`` package (Yang et al.,
*Genome Biology* 2020). Licensed Apache License 2.0 -- see
../../THIRD_PARTY_NOTICES.md for the full license text and attribution.

DecontX models each cell's observed UMI counts as a Bayesian
two-component multinomial mixture of a *native* gene distribution
``phi`` and a *contamination* distribution ``eta`` (a weighted blend of
every other population). Inference is by variational EM and yields a
per-cell contamination fraction and a decontaminated count matrix.

Internal to osp -- only :func:`decontx_one_sample.qc_one_sample` (via
``osp.qc``) calls into this subpackage; it is not part of the public
osp API.
"""
from __future__ import annotations

from ._core import (
    calculate_native_matrix,
    decontx_em,
    decontx_initialize,
    decontx_loglik,
)
from ._dirichlet import fit_dirichlet
from .decontx import DecontXResult, decontx

__all__ = [
    "decontx",
    "DecontXResult",
    "decontx_initialize",
    "decontx_em",
    "decontx_loglik",
    "calculate_native_matrix",
    "fit_dirichlet",
]

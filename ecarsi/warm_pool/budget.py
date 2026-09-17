"""Persist conservative downstream budgets from accepted upstream measurements."""
import math
from pathlib import Path

from .state import immutable, reference, verified
from .state import digest, read


# Measured 2026-09-16 over 40,286 succeeded receipts of the 28-tissue Tabula Sapiens batch:
# ceiling = 2 x max peak RSS + 1 GiB, rounded up to 256 MiB. Only operations without
# per-request measured sizing are listed (the others use from_compute / from_artifact /
# from_deg_buffers). Fixed stage budgets reserved 7x what ran (1,395 vs 199 GiB-hours) and
# three 24 GiB tool calls filled two nodes. An attempt that still exceeds its ceiling is
# retried at 2x by check_pool, so a larger dataset costs one short failed attempt.
MEASURED_CEILING_MB = {
    'check_genes': 9472, 'check_qc_scores': 9984, 'read_evidence': 3072, 'deg_sql': 1792,
    'deg_lookup': 1792, 'list_evidence': 5376, 'annotation_status': 5632, 'type_context': 6400,
    'sample_inventory': 1536, 'submit_quality': 5632, 'submit_decision': 9984,
    'submit_annotation': 4096, 'submit_types': 5632, 'submit_plan': 5632,
    'subcluster': 6144, 'zoom-in.assemble': 2560,
    'cross-sample.assemble': 2560, 'zoom-in.apply': 11008, 'persample.partition': 7424,
    'zoom-in.lineage.prepare': 1536, 'zoom-in.plan.prepare': 1536,
    'cross-sample.type.prepare': 1536, 'cross-sample.quality.prepare': 1536,
    'cross-sample.inclusion.prepare': 1536, 'inspect_source': 5120, 'organize.prepare': 8192,
}


# cpu_seconds / wall over 11.6k DEG and 280 compute receipts (2026-09-16, 15:53-20:40): DEG runs
# exactly one core on its 2-CPU grant; zoom-in compute/markers peak at 1.0-1.4 cores of 4. The
# thread caps (OMP/OPENBLAS/NUMBA) follow spec["cpus"], so a smaller grant changes nothing they did.
MEASURED_CPUS = {'zoom-in.deg': 1, 'cross-sample.deg': 1, 'zoom-in.compute': 2, 'zoom-in.markers': 2}


def measured_ceiling(spec):
    """Cap a fixed stage budget at the measured ceilings; never raise, never touch unknown operations."""
    memory = min(spec['memory_mb'], MEASURED_CEILING_MB.get(spec.get('operation_id'), spec['memory_mb']))
    cpus = min(spec['cpus'], MEASURED_CPUS.get(spec.get('operation_id'), spec['cpus']))
    if (memory, cpus) == (spec['memory_mb'], spec['cpus']):
        return spec
    return dict(spec, memory_mb=memory, cpus=cpus)


def replayed(request, policy_path, pool_root, kind):
    """A request the pool already holds is replayed as its saved plan; the base digest only guards a
    plan that was never submitted (a resumed stage rebuilds requests from current code: Eye's DEG
    requests differed only by the program hash after the 2026-09-17 cutover)."""
    saved = read(policy_path)
    submitted = (Path(pool_root) / 'requests' / request['request_id'] / 'request.json').exists()
    if saved is not None:
        if not submitted and saved['base_digest'] != digest(request):
            raise ValueError(kind + ' request changed')
        return saved['request']
    return request if submitted else None


def from_compute(request, computed, policy_path, pool_root):
    """Never change an existing request or use another pool's unaccepted estimate."""
    planned = replayed(request, policy_path, pool_root, 'Resource plan base')
    if planned is not None:
        return planned
    optimized = request
    source = Path(computed['path']).resolve()
    try:
        source.relative_to(Path(pool_root).resolve() / 'requests')
    except ValueError:
        return request
    receipt_path = source.parent.parent / 'receipt.json'
    receipt = read(receipt_path, {})
    peak = receipt.get('peak_rss_bytes')
    if (receipt.get('state') == 'succeeded' and type(peak) in (int, float) and math.isfinite(peak) and peak > 0
            and computed in [{k: o[k] for k in ('path', 'sha256')} for o in receipt.get('outputs', [])]):
        verified(computed)
        # Retain headroom for sampled peaks and downstream materialization. This
        # is an upstream-based ceiling reduction, not instantaneous RSS lending.
        memory = max(2048, math.ceil((2 * peak / 2**20 + 1024) / 256) * 256)
        if memory < request['memory_mb']:
            optimized = dict(request, memory_mb=memory,
                             inputs=[*request['inputs'], reference(receipt_path)])
    immutable(policy_path, dict(base_digest=digest(request), request=optimized))
    return optimized


def from_artifact(request, ref, policy_path, pool_root, *, copies=2, fixed_mb=1024):
    """Raise a ceiling for a step that loads one artifact whole, from its real size.

    A fixed per-stage budget cannot follow dataset size: the 4 GiB Zoom-in
    preparation killed the two largest tissues while smaller ones peaked near
    300 MiB. Measured peak was 1.1-1.2x the H5AD on disk; two copies plus a
    fixed allowance keeps headroom without reserving a whole node.
    """
    planned = replayed(request, policy_path, pool_root, 'Artifact resource')
    if planned is not None:
        return planned
    size = Path(ref['path']).stat().st_size / 2**20
    memory = max(request['memory_mb'], math.ceil((copies * size + fixed_mb) / 256) * 256)
    optimized = request if memory == request['memory_mb'] else dict(request, memory_mb=memory)
    immutable(policy_path, dict(base_digest=digest(request), artifact=ref,
                                artifact_mb=round(size, 1), request=optimized))
    return optimized


def from_deg_buffers(request, prepared, policy_path, pool_root):
    """Budget the mapped DEG workspace, not the dataset's counts and graph layers."""
    planned = replayed(request, policy_path, pool_root, 'DEG resource')
    if planned is not None:
        return planned
    source = Path(prepared['path']).resolve()
    try:
        source.relative_to(Path(pool_root).resolve() / 'requests')
    except ValueError:
        return request
    receipt_path = source.parent.parent / 'receipt.json'
    receipt = read(receipt_path, {})
    if (receipt.get('state') != 'succeeded' or request.get('gpu') or
            prepared not in [{k: o[k] for k in ('path', 'sha256')} for o in receipt.get('outputs', [])]):
        return request
    bundle = verified(prepared)
    files = {k.removeprefix('deg_input/'): v for k, v in bundle.get('files', {}).items() if k.startswith('deg_input/')}
    if set(files) not in ({'metadata.h5ad', 'matrix.npy'}, {'metadata.h5ad', 'data.npy', 'indices.npy', 'indptr.npy'}):
        return request  # Unknown input layouts keep their original reservation.
    sizes = {name: Path(ref['path']).stat().st_size for name, ref in files.items()}
    # CPU comparisons map the shared expression buffers and own their mutable
    # sparse workspace/subset. Allow four full buffer copies plus 2 GiB for
    # imports, metadata, rank chunks and result tables; local tasks use the same
    # conservative full-input estimate. Never extrapolate from instantaneous RSS.
    memory = math.ceil((2048 + 4 * sum(sizes.values()) / 2**20) / 256) * 256
    optimized = dict(request, memory_mb=min(request['memory_mb'], memory))
    immutable(policy_path, dict(base_digest=digest(request), input=prepared,
        buffer_bytes=sizes, workspace_copies=4, fixed_memory_mb=2048, request=optimized))
    return optimized

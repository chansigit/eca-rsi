"""Persist conservative downstream budgets from accepted upstream measurements."""
import math
from pathlib import Path

from .agent_session import immutable, reference, verified
from .warm_pool.state import digest, read


def from_compute(request, computed, policy_path, pool_root):
    """Never change an existing request or use another pool's unaccepted estimate."""
    saved = read(policy_path)
    if saved is not None:
        if saved['base_digest'] != digest(request):
            raise ValueError('Resource plan base request changed')
        return saved['request']
    if (Path(pool_root) / 'requests' / request['request_id'] / 'request.json').exists():
        return request
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


def from_deg_buffers(request, prepared, policy_path, pool_root):
    """Budget the mapped DEG workspace, not the dataset's counts and graph layers."""
    saved = read(policy_path)
    if saved is not None:
        if saved['base_digest'] != digest(request):
            raise ValueError('DEG resource request changed')
        return saved['request']
    if (Path(pool_root) / 'requests' / request['request_id'] / 'request.json').exists():
        return request
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

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

import pytest

from ecarsi.agent_session import reference
from ecarsi.operation_budget import from_compute
from ecarsi.warm_pool.state import save


def test_upstream_budget_is_conservative_pinned_and_keeps_existing_requests(tmp_path):
    pool = tmp_path / 'pool'
    output = pool / 'requests/compute/attempt/outputs/computed.json'
    output.parent.mkdir(parents=True)
    save(output, {'sample': 'S'})
    computed = reference(output)
    receipt = output.parent.parent / 'receipt.json'
    save(receipt, dict(state='succeeded', peak_rss_bytes=2**30, outputs=[computed]))
    request = dict(request_id='tool', memory_mb=12288, inputs=[computed])
    policy = tmp_path / 'resources.json'
    result = from_compute(request, computed, policy, pool)
    assert result['memory_mb'] == 3072  # Twice upstream peak, plus 1 GiB.
    assert reference(receipt) in result['inputs']
    assert from_compute(request, computed, policy, pool) == result
    with pytest.raises(ValueError, match='base request changed'):
        from_compute(dict(request, memory_mb=8192), computed, policy, pool)
    prior = pool / 'requests/old/request.json'
    prior.parent.mkdir()
    save(prior, {'spec': dict(request, request_id='old')})
    old = dict(request, request_id='old')
    assert from_compute(old, computed, tmp_path / 'old.json', pool) == old
    small = dict(request, request_id='small', memory_mb=2048)
    assert from_compute(small, computed, tmp_path / 'small.json', pool) == small

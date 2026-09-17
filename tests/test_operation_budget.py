import pytest

from ecarsi.agent_session import reference
from ecarsi.operation_budget import from_compute, from_deg_buffers
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
    # Once the pool holds the request, a stage rebuilt from newer code replays the saved plan.
    (pool / 'requests/tool').mkdir()
    save(pool / 'requests/tool/request.json', {'spec': result})
    assert from_compute(dict(request, memory_mb=8192), computed, policy, pool) == result
    prior = pool / 'requests/old/request.json'
    prior.parent.mkdir()
    save(prior, {'spec': dict(request, request_id='old')})
    old = dict(request, request_id='old')
    assert from_compute(old, computed, tmp_path / 'old.json', pool) == old
    small = dict(request, request_id='small', memory_mb=2048)
    assert from_compute(small, computed, tmp_path / 'small.json', pool) == small


def test_deg_buffer_budget_requires_accepted_known_inputs_and_is_pinned(tmp_path):
    pool = tmp_path/'pool'
    output = pool/'requests/compute/attempt/outputs/prepared.json'
    output.parent.mkdir(parents=True)
    files = {}
    for name in ('metadata.h5ad','data.npy','indices.npy','indptr.npy'):
        path = output.parent/name;path.write_bytes(b'fixed-buffer')
        files['deg_input/'+name] = reference(path)
    save(output, dict(files=files));prepared = reference(output)
    receipt_path = output.parent.parent/'receipt.json'
    request = dict(request_id='deg', memory_mb=12288, inputs=[prepared])
    assert from_deg_buffers(request, prepared, tmp_path/'missing.json', pool) == request
    save(receipt_path, dict(state='succeeded', outputs=[prepared]))
    result = from_deg_buffers(request, prepared, tmp_path/'policy.json', pool)
    assert result['memory_mb'] == 2304
    assert from_deg_buffers(request, prepared, tmp_path/'policy.json', pool) == result
    with pytest.raises(ValueError, match='request changed'):
        from_deg_buffers(dict(request, memory_mb=8192), prepared, tmp_path/'policy.json', pool)
    old = pool/'requests/old/request.json';old.parent.mkdir();save(old, {})
    assert from_deg_buffers(dict(request,request_id='old'), prepared, tmp_path/'old.json', pool)['memory_mb'] == 12288
    assert from_deg_buffers(dict(request,gpu={'mode':'preferred'}), prepared, tmp_path/'gpu.json', pool)['memory_mb'] == 12288
    assert from_deg_buffers(dict(request,memory_mb=1024), prepared, tmp_path/'small.json', pool)['memory_mb'] == 1024
    save(output, dict(files={**files,'deg_input/new-format.bin':reference(output.parent/'data.npy')}))
    unknown = reference(output);save(receipt_path, dict(state='succeeded', outputs=[unknown]))
    assert from_deg_buffers(request, unknown, tmp_path/'unknown.json', pool) == request

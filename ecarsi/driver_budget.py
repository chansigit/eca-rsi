"""Cooperative driver memory loans at explicit matrix-free boundaries.

A shared work lock serializes local matrix operations within one dataset.
The controller reserves one expansion window before admitting more datasets,
so parked model sessions can always regain a full computation budget.
"""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import threading
import time
import uuid

from ecarsi.run_state import write_json

_local = threading.local()
_lock = threading.RLock()


def _directory():
    value = os.environ.get('ECA_DRIVER_LEASE_DIRECTORY')
    return Path(value) if value else None


def _request(directory, mode, *, wait, memory_bytes=None):
    token = uuid.uuid4().hex
    request = dict(token=token, mode=mode, at=time.time())
    if memory_bytes is not None:
        if type(memory_bytes) is not int or memory_bytes <= 0:
            raise ValueError('work memory estimate must be a positive byte count')
        request['work_memory_bytes'] = memory_bytes
    write_json(directory/'request.json', request)
    if wait:
        from harness_bridge.control import safe_point
        while True:
            safe_point()
            try:
                ack = json.loads((directory/'grant.json').read_text())
                if ack.get('token') == token and ack.get('mode') == mode:
                    state = json.loads((directory.parents[1]/'status.json').read_text())
                    if any(r.get('memory_grant_token') == token and r.get('memory_state') == 'active'
                           for r in state['datasets']):
                        return
            except FileNotFoundError:
                pass
            time.sleep(.25)


@contextmanager
def work(memory_bytes=None):
    """Reserve a measured stage estimate, or the full budget for unprofiled work."""
    directory = _directory()
    if directory is None:
        yield
        return
    with _lock:
        depth = getattr(_local, 'depth', 0)
        if depth:
            if _local.memory is not None and (memory_bytes is None or memory_bytes > _local.memory):
                _request(directory, 'full', wait=True, memory_bytes=memory_bytes)
                _local.memory = memory_bytes
            _local.depth += 1
            try:
                yield
            finally:
                _local.depth -= 1
            return
        directory.mkdir(parents=True, exist_ok=True)
        with (directory/'work.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            _request(directory, 'full', wait=True, memory_bytes=memory_bytes)
            _local.depth = 1
            _local.memory = memory_bytes
            try:
                yield
            finally:
                _local.depth = 0
                _request(directory, 'compact', wait=False)


def restore():
    """Before returning to an unmodified outer driver, restore its full budget."""
    directory = _directory()
    if directory is not None:
        directory.mkdir(parents=True, exist_ok=True)
        with (directory/'work.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            _request(directory, 'full', wait=True)


@contextmanager
def model_wait():
    """Lend memory only while an agent call has no matrix work in flight."""
    directory = _directory()
    if directory is None:
        yield
        return
    with _lock:
        if getattr(_local, 'depth', 0):
            yield  # work() still owns its full reservation
            return
        directory.mkdir(parents=True, exist_ok=True)
        with (directory/'work.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            _request(directory, 'compact', wait=False)
            try:
                yield
            finally:
                _request(directory, 'full', wait=True)


def reserved(row):
    peak = row.get('admission_memory_gb', row['memory_gb']) * 2**30
    return min(peak, row.get('reserved_memory_bytes', peak)) if row.get('memory_lending_protocol') == 1 else peak


def matrix_working_bytes(path):
    """Conservative expression-work estimate from actual HDF5 array shapes.

    Compression/file size is not a RAM estimate. Count all expression/embedding
    arrays and allow copies plus dense HVG intermediates, without loading them.
    """
    import h5py
    size = cells = genes = 0
    with h5py.File(path, 'r') as f:
        matrix = f['layers/counts'] if 'layers/counts' in f else f['X']
        cells, genes = matrix.shape if isinstance(matrix, h5py.Dataset) else matrix.attrs['shape']
        def count(_, obj):
            nonlocal size
            if isinstance(obj, h5py.Dataset):
                size += obj.size * obj.dtype.itemsize
        for key in ('X', 'layers', 'raw', 'obsm', 'obsp', 'varm', 'varp'):
            if key not in f:
                continue
            if isinstance(f[key], h5py.Dataset):
                count(key, f[key])
            else:
                f[key].visititems(count)
    return int(2**30 + 8*size + 3*int(cells)*min(2000, int(genes))*8)


def expansion_room(node, rows):
    owners = {node['id'], *node.get('predecessors', [])}
    return max((r.get('admission_memory_gb', r['memory_gb']) * 2**30 - reserved(r) for r in rows
                if r.get('node') in owners and (r['state'] in {'assigned', 'running'} or r.get('reservation_held'))), default=0)


def acknowledge(directory, request):
    # Every node and the controller reconcile this lease. Rewriting an identical
    # ack under the fleet lock creates unnecessary shared-filesystem traffic.
    path = directory/'grant.json'
    grant = dict(token=request['token'], mode=request['mode'])
    try:
        if json.loads(path.read_text()) == grant:
            return
    except FileNotFoundError:
        pass
    write_json(path, grant)


def settle(state, config, now):
    """Handle requests before new admissions, with full memory accounting."""
    from ecarsi.batch import capacity
    if not any(r.get('memory_lending_protocol') == 1 for r in state['datasets']):
        return
    root = Path(config['directory'])/'memory-leases'
    for row in state['datasets']:
        if row.get('memory_lending_protocol') != 1 or row['state'] != 'running':
            continue
        directory = root/f"{row['id']}-{row['attempt']}"
        try:
            req = json.loads((directory/'request.json').read_text())
        except FileNotFoundError:
            continue
        if req.get('token') == row.get('memory_grant_token') and req.get('mode') == 'full':
            # Queue publication may have preceded a lost acknowledgement write.
            acknowledge(directory, req)
            continue
        if req.get('mode') not in {'full', 'compact'} or not isinstance(req.get('token'), str):
            continue
        node = state['nodes'].get(row['node'])
        if node is None or now-node.get('observed_at', 0) > 90:
            continue
        peak = row.get('admission_memory_gb', row['memory_gb']) * 2**30
        target = peak
        margin = config.get('driver_model_memory_margin_gb', 2) * 2**30
        estimate = req.get('work_memory_bytes')
        if req['mode'] == 'full' and estimate is not None:
            if type(estimate) is not int or estimate <= 0 or now-row.get('updated_at', 0) > 15:
                continue
            # Other model sessions in this driver tree still count. Unprofiled
            # operations and the outer driver continue to request the full peak.
            target = min(peak, row.get('rss_bytes', peak) + estimate + margin)
        if req['mode'] == 'compact':
            # Only the managed runtime declares a matrix-free boundary. RSS is
            # an additional floor, never evidence for entering that boundary.
            if now-row.get('updated_at', 0) > 15 or row.get('driver_pid') is None:
                continue
            rss = row.get('rss_bytes', peak)
            floor = 2**30 if row.get('admission_phase') == 'preparation' else 2*2**30
            target = min(peak, max(floor, rss + max(margin, rss*.25)))
        additional = target-reserved(row)
        if additional > 0 and capacity(node, state['datasets'], now)[1] < additional:
            row['memory_state'] = 'waiting_memory'
            continue
        row.update(reserved_memory_bytes=target, memory_grant_token=req['token'],
                   memory_state='model_wait' if req['mode'] == 'compact' else 'active')
        # The caller holds the durable queue transaction lock. Actors verify
        # queue publication as well as this acknowledgement before computing.
        acknowledge(directory, req)

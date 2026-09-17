"""Command-line status: HQ job names classify into operation and dataset; the report renders without records."""
import time

from ecarsi.observatory import classify_job, render_status


def test_classify_job_names():
    assert classify_job('rsi.agent-d271047bf7dda986ded8e568eff90b7e.199ee721d39d4cf6a6f7175836df79fc') == ('agent', 'model calls')
    assert classify_job('rsi.cross-4bfb9d1f8794c42e5341bd7c.tool-f577172463244fea.23ae259eef614d8f9b58dd8bbc5a3a3b') == ('tool', 'cross sessions')
    assert classify_job('rsi.scale2-ts-trachea-2939f81bfe0ccaa265e4.deg-888d928e74fa2d24.27dee001ba494f548ffad76222595bda') == ('deg', 'scale2-ts-trachea')
    assert classify_job('rsi.scale2-ts-fat-08c050b577d23224340e.compute-round-73fb4d8a2b1c9e0f.0123456789abcdef0123456789abcdef') == ('compute-round', 'scale2-ts-fat')


def test_render_status_without_records():
    report = {'generated_at': time.time(), 'host': 'h', 'root': '/r', 'scheduler': {}, 'bridge': {}, 'hq_error': 'no server',
              'workers': [], 'jobs': {'running': {'total': 0, 'by_class': {}, 'by_dataset': {}}},
              'temporal': {'error': 'ConnectionError()'}}
    text = render_status(report)
    assert 'CONTROL PLANE' in text and 'no server' in text and 'ConnectionError' in text


def test_render_status_worker_gpu_columns():
    worker = {'id': 1, 'host': 'gpu-node', 'slurm_job_id': 7, 'cpus': 8, 'memory_gb': 64.0, 'gpus': 1, 'running_tasks': 2,
              'cpu_cores_used': 3.0, 'node_memory_used_gb': 20.0, 'node_memory_gb': 128.0, 'seen_seconds_ago': 5.0, 'hours_left': 3.0,
              'gpu_percent': 42.0, 'gpu_memory_used_gb': 12.5, 'gpu_memory_gb': 79.6}
    silent = dict(worker, id=2, host='gpu-quiet', gpu_percent=None, gpu_memory_used_gb=None, gpu_memory_gb=None)
    report = {'generated_at': time.time(), 'host': 'h', 'root': '/r', 'scheduler': {}, 'bridge': {}, 'hq_error': None,
              'workers': [worker, silent], 'jobs': {}, 'temporal': None}
    text = render_status(report)
    assert ' 42 %   12.5/79.6' in text and 'no telemetry' in text

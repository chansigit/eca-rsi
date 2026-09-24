"""Operator reports over the control plane's records: `ecarsi observatory status|releases|tokens`.

The monitor Periscope serves at /_control/ moved to `ecarsi.ui.control` (see its docstring for why
the boundary matters). These commands are a human at a terminal, so they may query Temporal.
"""
import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import socket
import time

from .agent import status as bridge_status
from .warm_pool.state import lock, read, status as pool_status
# Re-exported so existing callers and tests keep their import path.
from .ui.control import (MAX_WINDOW, ControlPlane, resource_history,  # noqa: F401
                         snapshot, summarize_resources, task_timeline, worker_inventory)




async def temporal_ui(database: Path, port: int, ui_port: int, bind: str) -> None:
    """Experimental Temporal SQLite viewer; shared-filesystem recovery is unvalidated."""
    from temporalio.testing import WorkflowEnvironment

    database = database.resolve()
    database.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.umask(0o077)
    with lock(database.parent / "server.lock", blocking=False):
        environment = await WorkflowEnvironment.start_local(
            ip=bind, port=port, ui=True, ui_port=ui_port,
            dev_server_database_filename=str(database),
            download_dest_dir=str(database.parent),
        )
        print(f"Temporal experimental development UI: http://127.0.0.1:{ui_port}/", flush=True)
        try:
            await asyncio.Event().wait()
        finally:
            await environment.shutdown()


# ---------------------------------------------------------------------------
# Command-line status: the same records as the page, read from the cheap
# sources only (HQ server, summary files, worker telemetry tails, Temporal
# visibility). It never walks the request folders unless --sessions asks.

def classify_job(name):
    """HQ job names are 'rsi.<request id>.<attempt>': operation class and dataset run."""
    request = name.removeprefix('rsi.').rsplit('.', 1)[0] if name.startswith('rsi.') else name
    if request.startswith('agent-'):
        return 'agent', 'model calls'
    if '.tool-' in request:
        return 'tool', request.split('.tool-', 1)[0].split('-', 1)[0] + ' sessions'
    run, _, operation = request.rpartition('.')
    return re.sub(r'-[0-9a-f]{8,}$', '', operation) or operation, re.sub(r'-[0-9a-f]{16,}$', '', run) or run


def hq_view(pool):
    """Workers and jobs as the HQ server sees them; an unreachable server yields empty lists."""
    from .warm_pool.backend import HyperQueue
    try:
        hq = HyperQueue(pool)
        workers = hq.call('worker', 'list') or []
        jobs = {state: hq.call('job', 'list', '--filter', state) or [] for state in ('running', 'waiting')}
    except (RuntimeError, OSError, ValueError) as exc:
        return {'error': str(exc)[:200], 'workers': [], 'jobs': {'running': [], 'waiting': []}}
    for worker in workers:
        try:
            info = (hq.call('worker', 'info', str(worker['id'])) or [{}])[0]
            worker['running_tasks'] = next(iter((info.get('runtime_info') or {}).values()), {}).get('running_tasks')
        except (RuntimeError, OSError, ValueError, StopIteration):
            worker['running_tasks'] = None
    return {'workers': workers, 'jobs': jobs}


def worker_rows(pool, hq, now):
    identities = {}
    for path in (pool / 'workers').glob('*/identity.json'):
        identity = read(path, {})
        job = str((identity.get('allocation') or {}).get('job_id') or identity.get('slurm_job_id') or '')
        identities[(identity.get('host'), job)] = identity
    latest = {}
    for row in resource_history(pool, now - 120, now):
        latest[row['host']] = row
    rows = []
    for worker in hq['workers']:
        host = worker['configuration']['hostname'].split('.')[0]
        resources = {r['name']: r for r in worker['configuration']['resources']['resources']}
        # One host can carry several Slurm grants over time; the work directory names the current one.
        job = Path(worker['configuration'].get('work_dir', '')).name.rpartition('-')[2]
        identity = identities.get((host, job), {})
        allocation = identity.get('allocation') or {}
        sample = latest.get(host)
        cpus = len(resources.get('cpus', {}).get('values', []))
        gpus = (sample or {}).get('gpus') or []
        busy = [g['utilization_percent'] for g in gpus if g.get('utilization_percent') is not None]
        rows.append({
            'id': worker['id'], 'host': host, 'slurm_job_id': identity.get('slurm_job_id') or allocation.get('job_id'),
            'cpus': cpus, 'memory_gb': resources.get('mem', {}).get('size', 0) / 10000 / 1024,
            'gpus': sum(1 for name in resources if name.startswith('gpuSlot')),
            'running_tasks': worker.get('running_tasks'),
            'cpu_cores_used': sample['cpu_percent'] * cpus / 100 if sample and sample.get('cpu_percent') is not None else None,
            'node_memory_used_gb': sample['memory_used_bytes'] / 2**30 if sample else None,
            'node_memory_gb': sample['memory_total_bytes'] / 2**30 if sample else None,
            'seen_seconds_ago': now - sample['observed_at'] if sample else None,
            'gpu_percent': sum(busy) / len(busy) if busy else None,
            'gpu_memory_used_gb': sum(g.get('memory_used_mb') or 0 for g in gpus) / 1024 if gpus else None,
            'gpu_memory_gb': sum(g.get('memory_total_mb') or 0 for g in gpus) / 1024 if gpus else None,
            'hours_left': (allocation['end_time'] - now) / 3600 if allocation.get('end_time') else None,
        })
    return rows


async def temporal_view(service_root, now):
    from datetime import datetime, timedelta, timezone
    from temporalio.client import Client
    from temporalio.runtime import Runtime, TelemetryConfig
    from .control.temporal import endpoint
    # This is a read-only client that exits as soon as it has its counts. The SDK's optional
    # heartbeat thread has nothing to report for it and can race the native runtime's teardown,
    # which segfaults the process after it has already printed everything -- rare, and invisible
    # except as a non-zero exit. `control.coordinator` disables it for the same reason.
    client = await Client.connect(endpoint(service_root)['endpoint'],
                                  runtime=Runtime(telemetry=TelemetryConfig(), worker_heartbeat_interval=None))
    counts = {}
    for state in ('Running', 'Completed', 'Failed', 'Terminated'):
        counts[state.lower()] = sum([1 async for _ in client.list_workflows(
            f"WorkflowType = 'DatasetWorkflow' AND ExecutionStatus = '{state}'")])
    datasets = {}
    async for w in client.list_workflows("ExecutionStatus = 'Running' AND WorkflowType != 'DatasetWorkflow' "
                                         "AND WorkflowType != 'AnalysisUnitWorkflow'"):
        stage, _, rest = w.id.partition('/')
        run = rest.split('/')[0]
        entry = datasets.setdefault(re.sub(r'-[0-9a-f]{16,}$', '', run), {'stage': stage, 'workflows': []})
        entry['workflows'].append({'type': w.workflow_type, 'id': w.id.split('/')[-1],
                                   'age_minutes': (now - w.start_time.timestamp()) / 60})
    since = datetime.fromtimestamp(now - 6 * 3600, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    failures = [{'type': w.workflow_type, 'id': w.id, 'closed_at': w.close_time.timestamp()}
                async for w in client.list_workflows(f"ExecutionStatus = 'Failed' AND CloseTime > '{since}'")]
    return {'dataset_counts': counts, 'running': datasets, 'failures_6h': failures}


def session_stats(bridge, pool, hours, now):
    """Opt-in scan of recent request folders: turns per session and submission rejections."""
    cutoff = now - hours * 3600
    turns, started = {}, set()
    for entry in os.scandir(bridge / 'requests'):
        if '.turn-' not in entry.name or entry.stat().st_mtime < cutoff:
            continue
        session, number = entry.name.rsplit('.turn-', 1)
        turns[session] = max(turns.get(session, 0), int(number) + 1)
        if number == '0':
            started.add(session)
    kinds = {}
    for session in started:
        # A session counts once complete_tool saved result.json beside its session.json:
        # turns of a session still running would understate the real length.
        first = read(bridge / 'requests' / f'{session}.turn-0' / 'request.json', {})
        session_file = ((first.get('spec') or {}).get('session') or {}).get('path')
        kind = kinds.setdefault(session.split('-', 1)[0], {'started': 0, 'finished': []})
        kind['started'] += 1
        if session_file and (Path(session_file).parent / 'result.json').is_file():
            kind['finished'].append(turns[session])
    submissions = {}
    for entry in os.scandir(pool / 'requests'):
        if '.tool-' not in entry.name or entry.stat().st_mtime < cutoff:
            continue
        request = read(Path(entry.path) / 'request.json', {})
        operation = request.get('spec', {}).get('operation_id', '')
        if not operation.startswith('submit_'):
            continue
        result = read(Path(entry.path) / request.get('attempt_id', '') / 'outputs' / 'result.json')
        if result is None:
            continue
        bucket = submissions.setdefault(operation, {'accepted': 0, 'rejected': 0})
        bucket['rejected' if result.get('is_error') else 'accepted'] += 1
    return {'hours': hours,
            'sessions': {kind: {'started': v['started'], 'finished': len(v['finished']),
                                'turns_p50': sorted(v['finished'])[len(v['finished']) // 2] if v['finished'] else None,
                                'turns_max': max(v['finished']) if v['finished'] else None}
                         for kind, v in kinds.items()},
            'submissions': submissions}


def status_report(root, pool_root=None, bridge_root=None, temporal_service_root=None, sessions_hours=None):
    root = Path(root)
    pool = Path(pool_root) if pool_root else root / 'pool'
    bridge = Path(bridge_root) if bridge_root else root / 'bridge'
    now = time.time()
    hq = hq_view(pool)
    jobs = {}
    for state, items in hq['jobs'].items():
        by_class, by_dataset = {}, {}
        for job in items:
            kind, dataset = classify_job(job['name'])
            by_class[kind] = by_class.get(kind, 0) + 1
            by_dataset[dataset] = by_dataset.get(dataset, 0) + 1
        jobs[state] = {'total': len(items), 'by_class': by_class, 'by_dataset': by_dataset}
    report = {'generated_at': now, 'host': socket.gethostname(), 'root': str(root),
              'scheduler': read(pool / 'scheduler.json', {}), 'bridge': read(bridge / 'summary.json', {}),
              'hq_error': hq.get('error'), 'workers': worker_rows(pool, hq, now), 'jobs': jobs}
    if temporal_service_root:
        try:
            report['temporal'] = asyncio.run(temporal_view(temporal_service_root, now))
        except Exception as exc:  # noqa: BLE001 - the report must still print the rest
            report['temporal'] = {'error': repr(exc)[:200]}
    if sessions_hours:
        report['sessions'] = session_stats(bridge, pool, sessions_hours, now)
    return report


def render_status(report):
    now = report['generated_at']
    ago = lambda t: f"{now - t:.0f} s ago" if t else 'never'
    lines = [f"RSI v2 status  {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now))}  {report['host']}  {report['root']}",
             '', 'CONTROL PLANE']
    scheduler = report['scheduler']
    lines.append(f"  scheduler  {scheduler.get('state', 'unknown'):8s} {scheduler.get('host', '?'):16s} scan {scheduler.get('dispatch_scan_seconds', 0):.1f} s  seen {ago(scheduler.get('observed_at'))}")
    bridge = report['bridge']
    counts = bridge.get('counts', {})
    lines.append(f"  bridge     in flight {bridge.get('running', 0)}/{bridge.get('concurrency', '?')}  queued {counts.get('queued', 0)}  "
                 f"replies {counts.get('reply_saved', 0)}  failed {counts.get('failed', 0)}  scan {bridge.get('dispatch_scan_seconds', 0):.1f} s  seen {ago(bridge.get('updated_at'))}")
    for model in bridge.get('models', []):
        lines.append(f"    {model['model'].get('model', '?'):32s} {model.get('state', '?'):9s} in flight {model.get('in_flight', 0):3d}  "
                     f"last {model.get('last_latency_seconds') or 0:6.1f} s  ok {model.get('successes', 0)}  fail {model.get('failures', 0)}  timeout {model.get('timeouts', 0)}")
    temporal = report.get('temporal')
    if temporal:
        if 'error' in temporal:
            lines.append('  temporal   ' + temporal['error'])
        else:
            lines.append('  temporal   datasets ' + ', '.join(f"{k} {v}" for k, v in temporal['dataset_counts'].items())
                         + f"; failed workflows in 6 h: {len(temporal['failures_6h'])}")
    lines += ['', f"WORKERS ({len(report['workers'])} in HQ)" + (f"  HQ error: {report['hq_error']}" if report['hq_error'] else '')]
    lines.append('  id   host          job        cpus   used   mem GiB   node mem GiB   tasks  time left  seen          gpu  util  gpu mem GiB')
    for w in report['workers']:
        used = f"{w['cpu_cores_used']:5.1f}" if w['cpu_cores_used'] is not None else '    ?'
        mem = f"{w['memory_gb']:6.0f}   " + (f"{w['node_memory_used_gb']:5.0f}/{w['node_memory_gb']:<5.0f}" if w['node_memory_used_gb'] is not None else '     ?     ')
        left = f"{w['hours_left']:6.1f} h" if w['hours_left'] is not None else '       ?'
        seen = f"{w['seen_seconds_ago']:.0f} s" if w['seen_seconds_ago'] is not None else 'no telemetry'
        gpu = ''
        if w['gpus']:
            gpu = f"  {w['gpus']:3d}  " + (f"{w['gpu_percent']:3.0f} %  {w['gpu_memory_used_gb']:5.1f}/{w['gpu_memory_gb']:<5.1f}"
                                          if w.get('gpu_percent') is not None else 'no telemetry')
        lines.append(f"  {w['id']:<4} {w['host']:13s} {str(w['slurm_job_id'] or '?'):10s} {w['cpus']:3d}   {used}   {mem}    {str(w['running_tasks'] if w['running_tasks'] is not None else '?'):>5s}  {left}  {seen}{gpu}")
    for state in ('running', 'waiting'):
        jobs = report['jobs'].get(state, {'total': 0, 'by_class': {}, 'by_dataset': {}})
        lines += ['', f"POOL {state.upper()} {jobs['total']}  " + ', '.join(f"{k} {v}" for k, v in sorted(jobs['by_class'].items(), key=lambda kv: -kv[1]))]
        if jobs['by_dataset']:
            lines.append('  ' + ', '.join(f"{k} {v}" for k, v in sorted(jobs['by_dataset'].items(), key=lambda kv: -kv[1])[:12]))
    if temporal and 'running' in temporal:
        lines += ['', f"DATASETS RUNNING ({len(temporal['running'])})"]
        for name, entry in sorted(temporal['running'].items()):
            parts = []
            for kind in ('CrosssampleWorkflow', 'ZoominWorkflow', 'AgentWorkflow'):
                ages = sorted(w['age_minutes'] for w in entry['workflows'] if w['type'] == kind)
                if ages:
                    parts.append(f"{kind.removesuffix('Workflow')} x{len(ages)} {ages[0]:.0f}-{ages[-1]:.0f} min" if len(ages) > 1
                                 else f"{kind.removesuffix('Workflow')} {ages[0]:.0f} min")
            lines.append(f"  {name:28s} {entry['stage']:13s} " + '; '.join(parts))
        for failure in temporal['failures_6h'][-5:]:
            lines.append(f"  FAILED {time.strftime('%H:%M', time.localtime(failure['closed_at']))} {failure['type']} {failure['id'][:80]}")
    sessions = report.get('sessions')
    if sessions:
        lines += ['', f"SESSIONS (last {sessions['hours']:g} h)"]
        for kind, v in sorted(sessions['sessions'].items()):
            lines.append(f"  {kind:6s} started {v['started']:4d}  finished {v['finished']:4d}  turns p50 {v['turns_p50'] if v['turns_p50'] is not None else '-':>3}  max {v['turns_max'] if v['turns_max'] is not None else '-':>3}")
        for op, v in sorted(sessions['submissions'].items()):
            total = v['accepted'] + v['rejected']
            lines.append(f"  {op:18s} accepted {v['accepted']:4d}  rejected {v['rejected']:4d}  ({100 * v['rejected'] / max(1, total):.0f} %)")
    return '\n'.join(lines)


def release_rows(roots):
    """One row per analysis unit under each dataset root (a root without publication.json is a batch
    directory: its children are read): what went in, what OSP QC and each round removed, how it ended."""
    import collections
    rows = []
    for root in roots:
        root = Path(root)
        candidates = [root] if (root / 'publication.json').is_file() else sorted(c for c in root.iterdir() if (c / 'publication.json').is_file())
        for dataset in candidates:
            pub = read(dataset / 'publication.json', {})
            for ref in pub.get('units', []):
                unit = read(ref['path'], {}) if isinstance(ref, dict) else {}
                if not unit:
                    continue
                unit_dir = Path(ref['path']).parent
                per_sample = read(unit['per_sample']['path'], {}) if isinstance(unit.get('per_sample'), dict) else {}
                rounds = [read(r['path'], {}).get('stats', {}) for r in unit.get('rounds', []) if isinstance(r, dict)]
                review = read(unit_dir / 'release' / 'needs_review.json', []) or []
                rows.append(dict(dataset=pub.get('dataset_id') or dataset.name, unit=unit_dir.name, state=pub.get('state'),
                    n_input=unit.get('n_input'), osp_removed=per_sample.get('n_removed'), osp_survived=per_sample.get('n_survived'),
                    rounds=[dict(removed=s.get('removed'), n_in=s.get('n_in'), frac=s.get('frac')) for s in rounds],
                    n_survived=unit.get('n_survived'), forced=bool(unit.get('forced_release')), reason=unit.get('reason'),
                    review=dict(collections.Counter(e.get('kind') for e in review if isinstance(e, dict)))))
    return rows


def render_releases(rows):
    lines = [f"{'dataset':<30} {'unit':<14} {'input':>7} {'osp qc':>6} {'rounds':>6}  {'removal % per round':<34} {'final':>7} {'kept':>5}  reason | review kinds"]
    for r in rows:
        n = r['n_input'] or 0
        pct = lambda v: f"{100 * (v or 0) / n:.0f}%" if n else '-'
        per = ' '.join(f"{100 * (s['frac'] or 0):.1f}" for s in r['rounds'])
        review = ' '.join(f"{k}={v}" for k, v in sorted(r['review'].items(), key=lambda kv: -kv[1]))
        lines.append(f"{(r['dataset'] or '')[:30]:<30} {r['unit'][:14]:<14} {n:>7} {pct(r['osp_removed']):>6} {len(r['rounds']):>6}  "
                     f"{per[:34]:<34} {r['n_survived'] or 0:>7} {pct(r['n_survived']):>5}  {'FORCED ' if r['forced'] else ''}{(r['reason'] or '')[:48]} | {review}")
    return '\n'.join(lines)


def token_rows(bridge_root):
    """Per dataset: model turns and prompt / completion tokens, summed over every saved model reply under the
    bridge (the reply's usage is what the provider billed for that turn). One paced walk; run it when a batch
    is done, not every minute."""
    import collections
    totals = {}
    for n, entry in enumerate(os.scandir(Path(bridge_root) / "requests")):
        if not entry.is_dir():
            continue
        if n % 20 == 0:
            time.sleep(0.01)  # shares the control node's Lustre client with the coordinators
        result = read(Path(entry.path) / "result.json")
        if not result or result.get("state") != "reply_saved":
            continue
        response = result.get("response") or {}
        usage = response.get("usage") or {}
        trace = ((read(Path(entry.path) / "request.json", {}).get("spec") or {}).get("trace") or {})
        # persample/<run>-<hex20>, cross-sample/<run>-<hex20>, organize/<run>-organize: one row per dataset run
        run = re.sub(r"^[a-z-]+/", "", trace.get("workflow_id") or "?")
        run = re.sub(r"-(organize|[0-9a-f]{20})$", "", run)
        # Keyed by dataset, not by run: a stage workflow id keeps only 20 characters of the run id, so sibling
        # datasets (pansci-lung_WT_p1of5..p5of5, Azizi2018_breast_10x / _indrop) share one run prefix (2026-09-24).
        row = totals.setdefault(trace.get("dataset_id") or run, dict(run=run, dataset=trace.get("dataset_id") or "?", turns=0, tokens_in=0, tokens_out=0,
                                          kinds=collections.Counter(), models=collections.Counter()))
        row["turns"] += 1
        row["tokens_in"] += int(usage.get("tokens_in") or 0)
        row["tokens_out"] += int(usage.get("tokens_out") or 0)
        row["kinds"][entry.name.split("-", 1)[0]] += 1
        row["models"][(response.get("model") or {}).get("model") or "?"] += 1
    rows = sorted(totals.values(), key=lambda r: -r["tokens_in"])
    return [dict(r, kinds=dict(r["kinds"]), models=dict(r["models"])) for r in rows]


def render_tokens(rows):
    kinds = ("org", "osp", "cross", "zoom")
    lines = [f"{'run':<26} {'dataset':<26} {'turns':>6} {'tokens in':>12} {'tokens out':>11} {'in/turn':>8}  " + " ".join(f"{k:>5}" for k in kinds) + "  models"]
    total = dict(turns=0, tokens_in=0, tokens_out=0)
    for r in rows:
        for k in total:
            total[k] += r[k]
        lines.append(f"{r['run'][:26]:<26} {r['dataset'][:26]:<26} {r['turns']:>6} {r['tokens_in']:>12,} {r['tokens_out']:>11,} {r['tokens_in'] // max(1, r['turns']):>8,}  "
                     + " ".join(f"{r['kinds'].get(k, 0):>5}" for k in kinds) + "  " + ", ".join(f"{m} {n}" for m, n in sorted(r["models"].items(), key=lambda kv: -kv[1])))
    lines.append(f"{'TOTAL':<53} {total['turns']:>6} {total['tokens_in']:>12,} {total['tokens_out']:>11,} {total['tokens_in'] // max(1, total['turns']):>8,}")
    return "\n".join(lines)


def productivity_rows(root, pool_root=None):
    """What the Operations page shows per node, plus the pool line: earned standard core-hours,
    tasks done, efficiency (earned / cores x time on duty) and speed (earned / granted), 15 min and 4 h."""
    from .ui.control import snapshot
    snap = snapshot(Path(root), pool_root=pool_root)
    rows = snap["productivity"]
    pool = {"host": "POOL", "cores": sum(r["cores"] for r in rows)}
    for name in ("15m", "4h"):
        earned = sum(r.get("earned_core_hours_" + name) or 0 for r in rows)
        with_duty = [r for r in rows if r.get("efficiency_" + name) is not None]
        pool["earned_core_hours_" + name] = earned
        pool["tasks_done_" + name] = sum(r.get("tasks_done_" + name) or 0 for r in rows)
        pool["efficiency_" + name] = (sum(r["efficiency_" + name] * r["cores"] for r in with_duty)
                                      / sum(r["cores"] for r in with_duty)) if with_duty else None
        pool["speed_" + name] = None
    pool["tasks_failed_4h"] = sum(r.get("tasks_failed_4h") or 0 for r in rows)
    return rows + [pool]


def render_productivity(rows):
    fmt = lambda v, w=5: f"{v:{w}.0f}" if isinstance(v, (int, float)) else " " * (w - 1) + "-"
    lines = [f"node productivity  {time.strftime('%Y-%m-%d %H:%M:%S')}  (efficiency %: earned standard core-hours / cores x time on duty; speed: earned / granted, 1 = pool-typical)",
             f"{'node':14s} {'cores':>5s} {'eff15m':>6s} {'eff4h':>6s} {'spd15m':>6s} {'spd4h':>6s} {'core-h4h':>8s} {'done15m':>7s} {'done4h':>6s} {'fail4h':>6s}"]
    for r in rows:
        speed = lambda v: f"{v:6.2f}" if isinstance(v, (int, float)) else "     -"
        lines.append(f"{r['host']:14s} {r['cores']:5d} {fmt(r.get('efficiency_15m'), 6)} {fmt(r.get('efficiency_4h'), 6)} "
                     f"{speed(r.get('speed_15m'))} {speed(r.get('speed_4h'))} {r.get('earned_core_hours_4h', 0):8.1f} "
                     f"{r.get('tasks_done_15m', 0):7d} {r.get('tasks_done_4h', 0):6d} {r.get('tasks_failed_4h', 0):6d}")
    return "\n".join(lines)


def timeline_rows(root, pool_root=None, hours=1.0, host=None, dataset=None, running=False):
    from .ui import records
    pool = Path(pool_root) if pool_root else Path(root) / "pool"
    now = time.time()
    rows = [records.as_timeline_row(r) for r in records.tasks(pool, now - hours * 3600, now, {})]
    if host:
        rows = [r for r in rows if r.get("host") == host]
    if dataset:
        rows = [r for r in rows if dataset in (r["trace"].get("dataset_id") or "")]
    if running:
        rows = [r for r in rows if r.get("state") == "running"]
    return sorted(rows, key=lambda r: r.get("started_at") or r.get("submitted_at") or 0)


def render_timeline(rows):
    clock = lambda t: time.strftime("%H:%M:%S", time.localtime(t)) if t else "   -    "
    lines = [f"worker timeline  {len(rows)} tasks  (times local)",
             f"{'started':8s} {'finished':8s} {'dur s':>6s} {'node':11s} {'cpus':>4s} {'state':9s} {'operation':30s} {'dataset':34s} {'request':44s}"]
    for r in rows:
        dur = f"{r['finished_at'] - r['started_at']:6.0f}" if r.get("finished_at") and r.get("started_at") else "     -"
        lines.append(f"{clock(r.get('started_at')):8s} {clock(r.get('finished_at')):8s} {dur} "
                     f"{(r.get('host') or '-'):11s} {(r.get('cpus') or 0):4d} {(r.get('state') or '-'):9s} "
                     f"{(r.get('operation') or '')[:30]:30s} {(r['trace'].get('dataset_id') or '')[:34]:34s} {r['id'][:44]}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    history = commands.add_parser("temporal-ui")
    history.add_argument("--database", type=Path, required=True)
    history.add_argument("--port", type=int, default=7233)
    history.add_argument("--ui-port", type=int, default=8233)
    history.add_argument("--bind", default="127.0.0.1")
    text = commands.add_parser("status", help="print the same records as the page, from the cheap sources only")
    text.add_argument("--root", type=Path, required=True)
    text.add_argument("--pool-root", type=Path)
    text.add_argument("--bridge-root", type=Path)
    text.add_argument("--temporal-service-root", type=Path)
    text.add_argument("--sessions", type=float, metavar="HOURS", help="also scan recent sessions: turns and rejected submissions")
    text.add_argument("--json", action="store_true")
    rel = commands.add_parser("releases", help="one row per released analysis unit: input, OSP QC, per-round removal, final, review kinds")
    rel.add_argument("roots", nargs="+", type=Path, help="dataset output roots, or batch directories holding them")
    rel.add_argument("--json", action="store_true")
    tok = commands.add_parser("tokens", help="per dataset: model turns and prompt / completion tokens summed over the saved replies")
    tok.add_argument("--bridge-root", type=Path, required=True)
    tok.add_argument("--json", action="store_true")
    prod = commands.add_parser("productivity", help="the Operations page's node productivity table, from the task journals")
    prod.add_argument("--root", type=Path, required=True, help="run directory (pool/ and bridge/ under it, or --pool-root)")
    prod.add_argument("--pool-root", type=Path)
    prod.add_argument("--json", action="store_true")
    tl = commands.add_parser("timeline", help="the Operations page's worker timeline as rows: what ran where, when, how long")
    tl.add_argument("--root", type=Path, required=True)
    tl.add_argument("--pool-root", type=Path)
    tl.add_argument("--hours", type=float, default=1.0, help="how far back (journals cover at most 4 h)")
    tl.add_argument("--host", help="one node only, e.g. sh04-01n20")
    tl.add_argument("--dataset", help="substring of the dataset id")
    tl.add_argument("--running", action="store_true", help="only tasks still running")
    tl.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if args.command == "productivity":
        rows = productivity_rows(args.root, args.pool_root)
        print(json.dumps(rows, indent=2) if args.json else render_productivity(rows))
    elif args.command == "timeline":
        rows = timeline_rows(args.root, args.pool_root, args.hours, args.host, args.dataset, args.running)
        print(json.dumps(rows, indent=2) if args.json else render_timeline(rows))
    elif args.command == "tokens":
        rows = token_rows(args.bridge_root)
        print(json.dumps(rows, indent=2) if args.json else render_tokens(rows))
    elif args.command == "releases":
        rows = release_rows(args.roots)
        print(json.dumps(rows, indent=2) if args.json else render_releases(rows))
    elif args.command == "status":
        report = status_report(args.root, args.pool_root, args.bridge_root, args.temporal_service_root, args.sessions)
        print(json.dumps(report, indent=2, default=str) if args.json else render_status(report))
    else:
        asyncio.run(temporal_ui(args.database, args.port, args.ui_port, args.bind))


if __name__ == "__main__":
    main()

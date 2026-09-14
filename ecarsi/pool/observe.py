"""Persistent pool telemetry and optional host process-tree sampling via SSH.

Run on a host with Dask, SSH and shared paths; never requests Slurm resources.
"""
import argparse
import collections
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import shlex
import socket
import subprocess
import sys
import time


def record(handler, value):
    handler.handle(logging.LogRecord('pool-observer', logging.INFO, __file__, 0,
                                     json.dumps(value, default=str), (), None))


def resource_averages(samples, now, seconds=600):
    """Sample means over the trailing window; missing readings are not zeros."""
    rows = [s for s in samples if now-seconds < s['observed_at'] <= now]
    workers, gpus = {}, {}
    for sample in rows:
        for w in sample.get('workers', []):
            if w.get('state') == 'stale':
                continue
            values = workers.setdefault(w['address'], dict(cpu_percent=[], memory_percent=[], rss_bytes=[], times=[]))
            values['times'].append(sample['observed_at'])
            cpu, rss = w['metrics'].get('cpu'), w['metrics'].get('memory')
            if cpu is not None:
                values['cpu_percent'].append(cpu/w['cpus'])
            if rss is not None:
                values['rss_bytes'].append(rss)
                values['memory_percent'].append(100*rss/w['memory'])
        # A GPU shared by multiple workers counts once per observation.
        devices = {g['uuid']: g for w in sample.get('workers', []) if w.get('state') != 'stale'
                   for g in w.get('gpu_stats', [])}
        for uuid, g in devices.items():
            if g.get('utilization_percent') is not None:
                gpus.setdefault(uuid, []).append(g['utilization_percent'])
    mean = lambda values: sum(values)/len(values) if values else None
    return dict(window_seconds=seconds, observed_at=now,
                started=rows[0]['observed_at'] if rows else None, samples=len(rows),
                max_sample_gap_seconds=max((b['observed_at']-a['observed_at'] for a,b in zip(rows, rows[1:])), default=0),
                workers={address: {**{key:mean(v) for key,v in values.items() if key != 'times'},
                                   'samples':len(values['times']), 'started':min(values['times']),
                                   'finished':max(values['times'])} for address,values in workers.items()},
                gpu_utilization_percent={uuid:mean(values) for uuid,values in gpus.items()})


def changes(previous, sample):
    """Observed transitions, including short tasks completed between polls."""
    for section, identity, fields in (
        ('workers', 'address', ('state', 'draining')),
        ('tasks', 'id', ('state', 'worker', 'reason', 'worker_lost')),
        ('datasets', 'id', ('state', 'attempt', 'node', 'admission_phase', 'memory_state', 'queue_reason')),
    ):
        if section not in sample:
            continue  # failed collection does not imply departures
        old = {r[identity]: r for r in previous.get(section, [])}
        if section in {'workers', 'tasks'} and previous.get('epoch') != sample.get('epoch'):
            old = {}
        new = {r[identity]: r for r in sample[section]}
        for key, row in new.items():
            before = old.get(key)
            if before is None or any(before.get(f) != row.get(f) for f in fields):
                yield dict(observed_at=sample['observed_at'], observed_at_utc=sample['observed_at_utc'],
                           event=section+'.observed', epoch=sample.get('epoch'),
                           previous={f: before.get(f) for f in fields} if before else None, value=row)
        if section == 'workers':
            for key in old.keys()-new.keys():
                yield dict(observed_at=sample['observed_at'], observed_at_utc=sample['observed_at_utc'],
                           event='workers.departed', value=old[key])


def recent_history(root, cutoff, max_bytes):
    # Read a bounded tail even when upgrading an old, unrotated log.
    for path in (root/'utilization.jsonl.1', root/'utilization.jsonl'):
        if not path.exists():
            continue
        with path.open('rb') as stream:
            start = max(0, path.stat().st_size-max_bytes)
            stream.seek(start)
            if start:
                stream.readline()
            for line in stream:
                try:
                    value = json.loads(line)
                    if value['observed_at'] > cutoff:
                        yield value
                except (ValueError, KeyError, TypeError):
                    continue


def counters(requests):
    processes={}
    for path in Path('/proc').glob('[0-9]*/stat'):
        try:
            f=path.read_text().rsplit(')',1)[1].split()
            processes[int(path.parent.name)]=(int(f[1]),sum(int(x) for x in f[11:15]),int(f[21]))
        except (OSError,ValueError,IndexError):continue
    result={}
    for request in requests:
        pid=request['pid'];members={pid}
        while True:
            expanded=members|{p for p,v in processes.items() if v[0] in members}
            if members==expanded:break
            members=expanded
        result[pid]=(sum(processes[p][1] for p in members if p in processes),
                     sum(processes[p][2] for p in members if p in processes))
    return result


def probe(requests):
    host=socket.gethostname().split('.')[0]
    boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    births={}
    for r in requests:
        assert r['host']==host and r['boot_id']==boot
        assert set(os.sched_getaffinity(r['pid']))==set(r['cpu_ids'])
        args=Path(f"/proc/{r['pid']}/cmdline").read_bytes().split(b'\0')
        assert any(a in args for a in (b'ecarsi.pool.slurm',b'eca_services.pool.slurm'))
        births[r['pid']]=Path(f"/proc/{r['pid']}/stat").read_text().rsplit(')',1)[1].split()[19]
    before=counters(requests);start=time.monotonic();time.sleep(2);after=counters(requests)
    elapsed=time.monotonic()-start
    root=Path.home()/'.cache/ecarsi-pool'/host
    root.mkdir(parents=True,exist_ok=True)
    for r in requests:
        pid=r['pid']
        assert Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()[19]==births[pid]
        result=dict(r,observed_at=time.time(),cpu_percent=max(0,after[pid][0]-before[pid][0])/os.sysconf('SC_CLK_TCK')/elapsed*100/len(r['cpu_ids']),
                    rss_bytes=after[pid][1]*os.sysconf('SC_PAGE_SIZE'),source='process_tree')
        path=root/f'worker-{pid}.usage.json';tmp=path.with_suffix('.tmp')
        tmp.write_text(json.dumps(result));os.replace(tmp,path)
    print(json.dumps(dict(host=host,workers=len(requests))))


def host_probe(host, requests, python):
    if not re.fullmatch(r'[A-Za-z0-9._-]+',host): return 'invalid probe host'
    command = shlex.join([python,str(Path(__file__).resolve()),'--probe-json',json.dumps(requests)])
    try:
        subprocess.run(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=5',host,command],
                       capture_output=True,text=True,check=True,timeout=15)
    except subprocess.SubprocessError as exc: return host+': '+str(exc)


def observe(args):
    from ecarsi.run_state import writer_lock
    with writer_lock(args.directory/'.observer.lock'):
        return _observe(args)


def _observe(args):
    from .status import summarize
    from .client import connect
    from ecarsi.pool.scheduler import dispatch
    from ecarsi.run_state import write_json
    root = args.directory
    root.mkdir(parents=True,exist_ok=True)
    queue = args.queue
    last_report = root/'ten-minute-latest.json'
    try:
        cutoff = json.loads(last_report.read_text())['finished']
    except (OSError, ValueError, KeyError, TypeError):
        cutoff = 0
    max_bytes = int(getattr(args, 'log_max_mb', 64)*2**20)
    logs = {name: RotatingFileHandler(root/name, maxBytes=max_bytes,
            backupCount=getattr(args, 'log_backups', 7), encoding='utf-8')
            for name in ('utilization.jsonl', 'events.jsonl', 'ten-minute-windows.jsonl')}
    recovered = list(recent_history(root, min(cutoff, time.time()-600), max_bytes))
    window = [s for s in recovered if s['observed_at'] > cutoff]
    rolling = collections.deque(dict(observed_at=s['observed_at'], workers=s.get('workers', []))
                                for s in recovered if s['observed_at'] > time.time()-600)
    try:
        previous = json.loads((root/'utilization-latest.json').read_text())
    except (OSError, ValueError):
        previous = {}
    last_window = time.monotonic() - (time.time()-window[0]['observed_at'] if window else 0)
    client = None
    next_probe = 0
    probe_errors, probe_time = [], None
    while True:
        started = time.monotonic()
        now = time.time()
        sample = dict(schema_version=2, observed_at=now,
                      observed_at_utc=datetime.fromtimestamp(now, timezone.utc).isoformat(),
                      sample_gap_seconds=now-previous['observed_at'] if previous else None,
                      requested_interval_seconds=args.interval)
        if args.queue:
            try:
                state = json.loads(queue.read_text())
                sample['states'] = dict(collections.Counter(r['state'] for r in state['datasets']))
                sample['waiting_reasons'] = dict(collections.Counter(r.get('queue_reason','unknown') for r in state['datasets'] if r['state']=='queued'))
                sample['queue_age_seconds'] = now-state['updated_at'] if state.get('updated_at') else None
                heartbeat = state.get('controller', {}).get('observed_at')
                sample['controller_age_seconds'] = now-heartbeat if heartbeat else None
                sample['datasets'] = [{k: r.get(k) for k in (
                    'id', 'name', 'state', 'attempt', 'node', 'admission_phase', 'preparation_complete',
                    'memory_state', 'queue_reason')} for r in state['datasets']]
                sample['nodes'] = [{k: n.get(k) for k in (
                    'id', 'host', 'cpus', 'memory', 'memory_headroom', 'cpu_ids',
                    'available_cpu_ids', 'predecessors', 'draining', 'observed_at', 'end_time', 'role')}
                    for n in state.get('nodes', {}).values()]
                for node in sample['nodes']:
                    owners = {node['id'], *(node.get('predecessors') or [])}
                    active = [r for r in state['datasets'] if r.get('node') in owners
                              and (r['state'] in {'assigned', 'running'} or r.get('reservation_held'))]
                    node.update(heartbeat_age_seconds=now-node['observed_at'], active_datasets=len(active),
                        measured_rss_bytes=sum(r.get('rss_bytes') or 0 for r in active),
                        rss_missing_datasets=sum(r.get('rss_bytes') is None for r in active),
                        reserved_memory_bytes=sum(r.get('reserved_memory_bytes', r.get('admission_memory_gb', r['memory_gb'])*2**30)
                            if r.get('memory_lending_protocol') == 1 else r.get('admission_memory_gb', r['memory_gb'])*2**30 for r in active),
                        model_wait_datasets=sum(r.get('memory_state') == 'model_wait' for r in active))
                sample['drivers'] = []
                for row in state['datasets']:
                    if row['state'] != 'running': continue
                    value = {k:row.get(k) for k in ('id','name','node','rss_bytes','memory_gb','cpu_percent','cpu_ids','attempt',
                                                   'admission_phase','admission_memory_gb','updated_at',
                                                   'memory_lending_protocol','memory_state','reserved_memory_bytes')}
                    value['measurement_age_seconds'] = now-row['updated_at'] if row.get('updated_at') else None
                    path = Path(row['log'])
                    value['log_path'] = str(path)
                    try:
                        value['log_age_seconds'] = now-path.stat().st_mtime
                    except OSError:
                        value['log_age_seconds'] = None
                    sample['drivers'].append(value)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                sample['queue_error'] = str(exc)
        try:
            if client is None:
                client = connect(args.scheduler, set_as_default=False, timeout=15)
            pool = client.run_on_scheduler(dispatch,'status')
            metrics = client.scheduler_info()['workers']
            if time.monotonic() >= next_probe:
                groups = collections.defaultdict(list)
                for w in pool['workers'].values():
                    pid = w.get('supervisor',{}).get('pid')
                    if pid: groups[w['host']].append(dict(host=w['host'],pid=pid,boot_id=w['boot_id'],cpu_ids=w['cpu_ids']))
                with ThreadPoolExecutor(max_workers=4) as executor:
                    probes = [executor.submit(host_probe,host,requests,args.host_python) for host,requests in groups.items()]
                    probe_errors = [error for f in probes if (error := f.result())]
                probe_time = time.time()
                next_probe = time.monotonic()+30
            sample['host_probe_errors'] = probe_errors
            sample['host_probe_observed_at'] = probe_time
            measured = {w['address']:w for w in summarize(pool,metrics)['workers']}
            sample['epoch'] = pool['epoch']
            sample['workers'] = [dict(address=a,host=w['host'],cpus=w['cpus'],memory=w['memory'],
                gpus=w['gpus'],gpu_stats=w.get('gpu_stats',[]),draining=w.get('draining'),
                observed_at=w['observed_at'],end_time=w['end_time'],
                metrics=dict(cpu=measured[a]['cpu_percent']*w['cpus'] if measured[a]['cpu_percent'] is not None else None,
                             memory=measured[a]['rss_bytes'],time=measured[a]['metrics_time']),
                metrics_source=measured[a]['metrics_source']) for a,w in pool['workers'].items()]
            sample['tasks'] = list(pool['tasks'].values())
            measured_at = time.time()
            for worker in sample['workers']:
                detail = measured[worker['address']]
                inventory = pool['workers'][worker['address']]
                active = [t for t in sample['tasks'] if t.get('worker') == worker['address']
                          and t['state'] in {'granted', 'running', 'stopping'}]
                worker.update(state=detail['state'], job_id=inventory['job_id'], cpu_ids=inventory['cpu_ids'],
                    runtime=inventory.get('runtime', {}), shared_roots=inventory.get('roots', []),
                    allocation_memory_bytes=inventory['allocation_memory'], cpu_percent=detail['cpu_percent'],
                    cpu_cores_used=detail['cpu_percent']*worker['cpus']/100 if detail['cpu_percent'] is not None else None,
                    rss_bytes=detail['rss_bytes'], remaining_seconds=detail['remaining_seconds'],
                    heartbeat_age_seconds=measured_at-worker['observed_at'],
                    metrics_age_seconds=measured_at-detail['metrics_time'] if detail['metrics_time'] else None,
                    active_task_ids=[t['id'] for t in active],
                    reserved_cpus=sum(t['cpus'] for t in active), reserved_memory_bytes=sum(t['memory'] for t in active),
                    reserved_gpus=sum(t['gpus'] for t in active))
            sample['compute_states'] = dict(collections.Counter(t['state'] for t in sample['tasks']))
            sample['compute_waiting_reasons'] = dict(collections.Counter(t.get('reason', 'unknown') for t in sample['tasks'] if t['state']=='queued'))
        except Exception as exc:
            sample['error'] = str(exc)
            if client:
                try: client.close(timeout=2)
                except Exception: pass
            client = None
        sample['collection_seconds'] = time.monotonic()-started
        sample['collection_started_at'] = now
        sample['observed_at'] = time.time()
        sample['observed_at_utc'] = datetime.fromtimestamp(sample['observed_at'], timezone.utc).isoformat()
        sample['sample_gap_seconds'] = sample['observed_at']-previous['observed_at'] if previous else None
        events = list(changes(previous, sample))
        for event in events:
            record(logs['events.jsonl'], event)
        errors = {k: sample.get(k) for k in ('error', 'queue_error', 'host_probe_errors') if sample.get(k)}
        old_errors = {k: previous.get(k) for k in ('error', 'queue_error', 'host_probe_errors') if previous.get(k)}
        if errors != old_errors:
            record(logs['events.jsonl'], dict(observed_at=sample['observed_at'], observed_at_utc=sample['observed_at_utc'],
                                             event='collector.health', errors=errors))
        # The latest snapshot stays complete; history stores finished tasks only
        # on change instead of copying an hour of old tasks on every sample.
        history_sample = sample.copy()
        if 'tasks' in sample:
            changed = {e['value']['id'] for e in events if e['event']=='tasks.observed'}
            history_sample['tasks'] = [t for t in sample['tasks'] if t['state'] in {'queued', 'granted', 'running', 'stopping'} or t['id'] in changed]
        record(logs['utilization.jsonl'], history_sample)
        write_json(root/'utilization-latest.json',sample)
        rolling.append(dict(observed_at=sample['observed_at'], workers=sample.get('workers', [])))
        while rolling and rolling[0]['observed_at'] <= sample['observed_at']-600:
            rolling.popleft()
        write_json(root/'utilization-averages.json', resource_averages(rolling, sample['observed_at']))
        window.append(history_sample)
        previous = sample
        if time.monotonic()-last_window >= args.report_interval:
            completed = { (s.get('epoch'),t['id']):t for s in window for t in s.get('tasks',[])
                         if t.get('finished',0) > (cutoff or window[0].get('collection_started_at', window[0]['observed_at'])) }
            worker_cpu = collections.defaultdict(list)
            for s in window:
                for w in s.get('workers',[]):
                    cpu = w['metrics'].get('cpu')
                    if cpu is not None: worker_cpu[w['address']].append(cpu/w['cpus'])
            report = dict(started=window[0]['observed_at'],finished=sample['observed_at'],samples=len(window),
                states_before=window[0].get('states'),states_after=sample.get('states'),
                waiting_reasons=sample.get('waiting_reasons'),
                mean_worker_cpu_percent={k:sum(v)/len(v) for k,v in worker_cpu.items()},
                kernel_outcomes=dict(collections.Counter(t['state'] for t in completed.values())),
                kernels_by_stage=dict(collections.Counter(t['label'] for t in completed.values())),
                kernel_seconds_by_stage={label:sum(t.get('seconds_actual',0) for t in completed.values() if t['label']==label)
                                         for label in {t['label'] for t in completed.values()}},
                compute_queue_samples=sum(any(t['state']=='queued' for t in s.get('tasks',[])) for s in window),
                errors=[s['error'] for s in window if s.get('error')],
                queue_errors=[s['queue_error'] for s in window if s.get('queue_error')],
                host_probe_errors=[e for s in window for e in s.get('host_probe_errors',[])],
                metric_sources=dict(collections.Counter(w.get('metrics_source','dask_process')
                    for s in window for w in s.get('workers',[]))))
            report['worker_hosts'] = {w['address']: w['host'] for s in window for w in s.get('workers', [])}
            rss = collections.defaultdict(list)
            gpu = collections.defaultdict(list)
            for s in window:
                for w in s.get('workers', []):
                    if w['metrics'].get('memory') is not None:
                        rss[w['address']].append(w['metrics']['memory'])
                    for device in w.get('gpu_stats', []):
                        if device.get('utilization_percent') is not None:
                            gpu[device['uuid']].append(device['utilization_percent'])
            report['peak_worker_rss_bytes'] = {k: max(v) for k, v in rss.items()}
            report['mean_gpu_utilization_percent'] = {k: sum(v)/len(v) for k, v in gpu.items()}
            report['max_sample_gap_seconds'] = max((s.get('sample_gap_seconds') or 0 for s in window), default=0)
            record(logs['ten-minute-windows.jsonl'], report)
            write_json(root/'ten-minute-latest.json',report)
            print(json.dumps(report),flush=True)
            cutoff = sample['observed_at']
            window = []
            last_window = time.monotonic()
        if args.once:
            if client: client.close()
            for handler in logs.values(): handler.close()
            return
        time.sleep(max(0, args.interval-(time.monotonic()-started)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory',type=Path)
    parser.add_argument('--queue',type=Path,help='optional dataset status.json')
    parser.add_argument('--scheduler',default=None)
    parser.add_argument('--host-python',default=sys.executable)
    parser.add_argument('--interval',type=float,default=15)
    parser.add_argument('--report-interval',type=float,default=600)
    parser.add_argument('--log-max-mb',type=int,default=64,help='rotate each JSONL log at this size (default: 64 MiB)')
    parser.add_argument('--log-backups',type=int,default=7,help='rotated files retained per JSONL log (default: 7)')
    parser.add_argument('--once',action='store_true')
    parser.add_argument('--probe-json',help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.probe_json:
        probe(json.loads(args.probe_json))
    else:
        if not args.directory or args.interval <= 0 or args.report_interval <= 0 or args.log_max_mb <= 0 or args.log_backups <= 0:
            parser.error('directory and positive intervals/log limits are required')
        observe(args)


if __name__ == '__main__':
    main()

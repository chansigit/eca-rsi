"""Where a batch's time goes (#60): per dataset, per pool operation, per model turn and per integration step.

usage: bash ops/runpy.sh ops/run-health.py <run root | batch dir> ...    (a batch dir: its subfolders with spec.json)
       bash ops/runpy.sh ops/run-health.py --tag <text>                 (runs whose folders are gone, e.g. -b2-)
Reads the worker task journals (pool/workers/*/tasks-*.jsonl, the lines naming the runs), each run folder once
(model sessions need it), then each turn's bridge records and each integration's stdout.log by name, paced: the
bridge and pool request folders are never walked. The pruner deletes a finished run's pool requests within the hour,
so the integration steps of a run finished earlier show as pruned.
"""
import collections
import glob
import json
import os
import re
import sys
import time
from pathlib import Path

from ecarsi.files import read

PACE = 0.01  # seconds between reads by name
INTEGRATIONS = ('cross-sample.compute', 'cross-sample.compute-round', 'zoom-in.compute')
STEP = re.compile(r'^(\d\d-\d\d \d\d:\d\d:\d\d) == (.*)$')
TURN = re.compile(r'(.+)\.turn-\d+\.')


def q(values, p):
    values = sorted(values)
    return values[min(len(values) - 1, int(len(values) * p))] if values else 0


def union(intervals):
    total, end = 0, -1e18
    for a, b in sorted(intervals):
        if b > end:
            total, end = total + b - max(a, end), b
    return total


def journal(pool, tags):
    """{tag: [journal row]} for the rows whose line names a tag (run id, or the --tag text)."""
    rows = collections.defaultdict(list)
    for path in sorted(glob.glob(f'{pool}/workers/*/tasks-*.jsonl')):
        with open(path) as stream:
            for n, line in enumerate(stream):
                if n % 500 == 0:
                    time.sleep(0.002)
                tag = next((t for t in tags if t in line), None)
                if tag:
                    try:
                        rows[tag].append(json.loads(line))
                    except ValueError:
                        pass
    return rows


def layer(row):
    if '.tool-' in (row.get('request_id') or ''):
        return 'tool'
    op = row.get('operation') or ''
    return 'deg' if op.endswith('.deg') else 'agent' if op == 'agent.call' else 'display' if op.startswith('dataset.') else 'compute'


def timeline(rows_by_run):
    print('\n== elapsed time per run (hours; a layer counts only where no layer left of it runs; gap = model turns, orchestration)')
    print(f"{'run':40} {'elapsed':>8} {'deg':>6} {'compute':>7} {'tool':>6} {'queue':>6} {'gap':>6}")
    total = collections.Counter()
    for run, rows in sorted(rows_by_run.items()):
        started = [r for r in rows if r.get('started_at')]
        if not started:
            continue
        elapsed = max(r['finished_at'] for r in rows) - min(r['submitted_at'] for r in rows)
        covered, part = [], {}
        for name in ('deg', 'compute', 'tool', 'queue'):
            before = union(covered)
            covered += ([(r['submitted_at'], r['started_at']) for r in started] if name == 'queue'
                        else [(r['started_at'], r['finished_at']) for r in started if layer(r) == name])
            part[name] = union(covered) - before
        part['gap'], part['elapsed'] = elapsed - union(covered), elapsed
        total.update(part)
        print(f"{run[:40]:40} {elapsed/3600:8.1f} " + ' '.join(f'{part[k]/3600:{w}.1f}' for k, w in
                                                            (('deg', 6), ('compute', 7), ('tool', 6), ('queue', 6), ('gap', 6))))
    if total['elapsed']:
        print(f"{'TOTAL':40} {total['elapsed']/3600:8.1f} " + ' '.join(
            f"{total[k]/3600:{w}.1f}" for k, w in (('deg', 6), ('compute', 7), ('tool', 6), ('queue', 6), ('gap', 6))))
        print('shares: ' + ', '.join(f"{k} {total[k]/total['elapsed']:.1%}" for k in ('gap', 'compute', 'deg', 'tool', 'queue')))


def operations(rows):
    print('\n== pool operations (core-h = duration x cpus; eff = cpu-h / core-h; seconds; GiB)')
    print(f"{'operation':34} {'n':>6} {'fail':>5} {'core_h':>7} {'eff':>5} {'med_s':>7} {'p90_s':>7} {'max_s':>7} {'wait90':>6} {'rssmax':>6}")
    groups = collections.defaultdict(list)
    for r in rows:
        groups[(r.get('operation') or '?') + (' [tool]' if layer(r) == 'tool' else '')].append(r)
    table = []
    for op, rs in groups.items():
        d = [r.get('duration_s') or 0 for r in rs]
        core = sum((r.get('duration_s') or 0) * (r.get('cpus') or 1) for r in rs) / 3600
        cpu = sum(r.get('cpu_seconds') or 0 for r in rs) / 3600
        table.append((core, op, len(rs), sum(r.get('state') != 'succeeded' for r in rs), cpu / core if core else 0,
                      q(d, .5), q(d, .9), max(d), q([r.get('queue_wait_s') or 0 for r in rs], .9),
                      max((r.get('peak_rss_bytes') or 0) for r in rs) / 2**30))
    for core, op, n, fail, eff, med, p90, mx, w90, rss in sorted(table, reverse=True):
        print(f'{op[:34]:34} {n:6} {fail:5} {core:7.1f} {eff:5.2f} {med:7.1f} {p90:7.1f} {mx:7.0f} {w90:6.0f} {rss:6.1f}')
    failed = [r for r in rows if r.get('state') != 'succeeded']
    wasted = sum((r.get('duration_s') or 0) * (r.get('cpus') or 1) for r in failed) / 3600
    print(f'failed attempts: {len(failed)}, {wasted:.1f} core-h of {sum(t[0] for t in table):.1f}; RSS-budget kills: '
          f"{sum('RSS' in (r.get('error') or '') for r in failed)}")
    for (op, error), n in collections.Counter((r.get('operation'), (r.get('error') or '')[:90]) for r in failed).most_common(10):
        print(f'  {n:5} {op:28} {error}')


def integration_steps(pool, rows):
    print('\n== integration steps (from each integration stdout.log; minutes)')
    steps, read_logs, pruned = collections.defaultdict(list), 0, 0
    for r in rows:
        if r.get('operation') not in INTEGRATIONS or r.get('state') != 'succeeded':
            continue
        time.sleep(PACE)
        path = Path(pool) / 'requests' / r['request_id'] / r['attempt_id'] / 'stdout.log'
        try:
            lines = path.read_text(errors='replace').splitlines()
        except OSError:
            pruned += 1
            continue
        read_logs += 1
        stamped = [(time.mktime(time.strptime('2000-' + m.group(1), '%Y-%m-%d %H:%M:%S')), m.group(2))
                   for m in map(STEP.match, lines) if m]
        for (t0, text), (t1, _) in zip(stamped, stamped[1:]):
            steps[(r['operation'], re.split(r' \(| \[|:| on | /', text)[0][:40])].append((t1 - t0) % (366 * 86400) / 60)
    print(f'{read_logs} logs read, {pruned} pruned')
    for (op, step), minutes in sorted(steps.items(), key=lambda kv: -sum(kv[1])):
        print(f'  {op:26} {step:40} n {len(minutes):4}  median {q(minutes, .5):6.1f}  max {max(minutes):6.1f}  total_h {sum(minutes)/60:6.1f}')


def model_turns(runs, bridge):
    print('\n== model turns (hours; between = time between a session\'s turns: its tool calls and orchestration)')
    stages, turns = collections.defaultdict(collections.Counter), []
    for run in runs:
        sessions = sorted({(m.group(1), str(p.parent.relative_to(run))) for p in run.rglob('*.turn-*') if (m := TURN.match(p.name))})
        for session, where in sessions:
            n, first, last, submits = 0, None, None, collections.defaultdict(list)
            stage = re.sub(r'round\d+', 'rN', re.sub(r'(01-per-sample|02-cross-sample|03-zoom-in)/.*', r'\1',
                                                       where.split('units/')[-1].split('/', 1)[-1]))
            counter = stages[stage]
            while (state := read(Path(bridge) / 'requests' / f'{session}.turn-{n}' / 'state.json')) is not None:
                for attempt in state.get('attempts', []):
                    time.sleep(PACE)
                    result = read(Path(bridge) / 'turns' / attempt['turn_id'] / 'result.json') if 'turn_id' in attempt else None
                    if not result or not result.get('latency_s'):
                        continue
                    usage = (result.get('provider_response') or {}).get('usage') or {}
                    submitted = attempt.get('submitted_at') or 0
                    if last and submitted:
                        counter['between'] += submitted - last
                    first, last = first or submitted, submitted + (result.get('elapsed_seconds') or 0)
                    used = {str(x) for x in ((((result.get('response') or {}).get('sdk_state') or {})
                                             .get('last_processed_response') or {}).get('tools_used') or []) if str(x).startswith('submit_')}
                    for name in used:
                        submits[name].append(result['latency_s'])
                    turns.append(dict(lat=result['latency_s'], out=result.get('output_tokens') or 0, tin=result.get('input_tokens') or 0,
                                      reason=(usage.get('output_tokens_details') or {}).get('reasoning_tokens') or 0,
                                      cached=(usage.get('input_tokens_details') or {}).get('cached_tokens') or 0,
                                      submit=bool(used)))
                    counter.update(turns=1, model=result['latency_s'], tin=turns[-1]['tin'], cached=turns[-1]['cached'])
                n += 1
            if first and last:
                counter.update(sessions=1, wall=last - first, resub=sum(sum(v[:-1]) for v in submits.values()),
                               resub_n=sum(len(v) - 1 for v in submits.values()))
    print(f"{'stage':32} {'sess':>5} {'turns':>6} {'wall_h':>7} {'model_h':>7} {'between':>7} {'Mtok_in':>7} {'cached':>6} {'resub_n':>7} {'resub_h':>7}")
    total = collections.Counter()
    for stage, c in sorted(stages.items()) + [('TOTAL', total)]:
        if stage != 'TOTAL':
            total.update(c)
        print(f"{stage[:32]:32} {c['sessions']:5} {c['turns']:6} {c['wall']/3600:7.2f} {c['model']/3600:7.2f} {c['between']/3600:7.2f} "
              f"{c['tin']/1e6:7.1f} {c['cached']/max(c['tin'], 1):6.0%} {c['resub_n']:7} {c['resub']/3600:7.2f}")
    if turns:
        for key in ('lat', 'out', 'reason', 'tin'):
            v = [t[key] for t in turns]
            print(f'  {key:6} median {q(v, .5):9.0f}  p90 {q(v, .9):9.0f}  max {max(v):9.0f}')
        rate = [t['out'] / t['lat'] for t in turns if t['lat'] > 5]
        print(f"  output tokens/s median {q(rate, .5):.1f}; reasoning share of output {sum(t['reason'] for t in turns)/max(1, sum(t['out'] for t in turns)):.0%}; "
              f"submit turns {sum(t['lat'] for t in turns if t['submit'])/3600:.1f} of {sum(t['lat'] for t in turns)/3600:.1f} model-h")


def main(argv):
    pool, bridge = os.environ['POOL'], os.environ['BRIDGE']
    runs, tags = [], argv[1:] if argv[:1] == ['--tag'] and len(argv) == 2 else []
    if not tags:
        for arg in map(Path, argv):
            runs += [arg] if (arg / 'spec.json').exists() else [s.parent for s in sorted(arg.glob('*/spec.json'))]
        tags = [read(run / 'spec.json')['run_id'] for run in runs]
    if not tags:
        sys.exit(__doc__)
    by_run = journal(pool, tags)
    rows = [r for rs in by_run.values() for r in rs]
    print(f"{len(tags)} run(s) selected, {len(rows)} journal rows; datasets {len({r.get('dataset_id') for r in rows})}")
    if not rows:
        return
    if not runs:  # --tag: one row per dataset
        by_run = collections.defaultdict(list)
        for r in rows:
            by_run[r.get('dataset_id') or '?'].append(r)
    timeline(by_run)
    operations(rows)
    integration_steps(pool, rows)
    if runs:
        model_turns(runs, bridge)


if __name__ == '__main__':
    main(sys.argv[1:])

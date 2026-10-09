"""Replay a frozen hard case (decision 0021) with today's code and a chosen bridge's models, and compare it with the
original session (#14 step 3).

usage: VERSION=<v> bash ops/runsci.sh ops/replay-case.py prepare <case folder> <out> [--label L] [--bridge-root B]
                                                                   [--run-spec <run's spec.json>]
       VERSION=<v> bash ops/runpy.sh ops/replay-case.py start <out>      the session as an AgentWorkflow on v's queue
       VERSION=<v> bash ops/runpy.sh ops/replay-case.py wait <out>       one long Temporal call: run it in the background
       bash ops/runpy.sh ops/replay-case.py report <case folder> <out> ...   process metrics, the original first
A case folder is <archive_root>/_cases/<collection>/<dataset>/<run>/<session path> (its case.json). prepare restores
the case's files under <out>/restored with every reference rewritten (ecarsi.stages.cases.restore), finds the run's
spec.json in a display root of ~/.config/ecarsi/results.json, and rebuilds the session with this version's prompt and
tools under run id replay-<label>: a fresh session id, as the bridge answers a known request id with its stored reply.
Its tools run as pool tasks on the workers; its model turns go to --bridge-root (default the original's bridge, whose
catalog is production's). Only cross-sample and zoom-in sessions replay. Nothing is pruned: delete <out> and the
replay's pool requests (named after its session id) by hand.
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from ecarsi.files import read, save

RESULTS = Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'ecarsi' / 'results.json'


def run_spec(case):
    """The run's spec.json in a display zone: <display root>/<collection>/<dataset>/<run>/spec.json."""
    parts = Path(case).parts
    collection, dataset, run = parts[parts.index('_cases') + 1:parts.index('_cases') + 4]
    results = read(RESULTS, {})
    for root in [results.get('display_root'), *results.get('more_display_roots', [])]:
        if root and (path := Path(root) / collection / dataset / run / 'spec.json').is_file():
            return path
    sys.exit(f'no spec.json of run {run} under the display roots of {RESULTS}; give --run-spec')


def prepare(args):
    from ecarsi import version
    from ecarsi.stages import cases
    case, out = Path(args.case).resolve(), Path(args.out)
    if out.exists():
        sys.exit(f'{out} exists: a replay writes a fresh folder')
    out.mkdir(mode=0o700, parents=True)
    spec_path = Path(args.run_spec) if args.run_spec else run_spec(case)
    label = args.label or time.strftime('%Y%m%d-%H%M%S')
    spec = cases.replay_spec(case, out, read(spec_path), label, args.bridge_root)
    save(out / 'agent.json', spec)
    save(out / 'replay.json', dict(case=str(case), run_spec=str(spec_path), label=label, version=(version() or {}).get('name'),
                                   bridge_root=spec['bridge_root'], session_id=spec['session_id'], session=spec['output_root'],
                                   original=read(case / 'case.json')['session_id'], prepared_at=round(time.time())))
    print(spec['session_id'], spec['output_root'])


def start(args):
    from ecarsi import version
    replay = read(Path(args.out) / 'replay.json')
    if replay['version'] != (version() or {}).get('name'):
        sys.exit(f"prepared on version {replay['version']}: start it with VERSION={replay['version']}")
    subprocess.run([sys.executable, '-m', 'ecarsi.control', '--service-root', os.environ['CONTROL'],
                    'start-agent', str(Path(args.out) / 'agent.json')], check=True)


def wait(args):
    import asyncio
    from temporalio.client import Client
    from ecarsi.control.temporal import endpoint
    replay = read(Path(args.out) / 'replay.json')

    async def result():
        client = await Client.connect(endpoint(os.environ['CONTROL'])['endpoint'])
        return await client.get_workflow_handle('agent/' + replay['session_id']).result()
    print(asyncio.run(result()))


def metrics(folder, files=None):
    """Process metrics of one session folder; files = a case's files/ for outputs the pool no longer holds."""
    def output(ref):
        path = Path(ref['path'])
        if not path.is_file() and files is not None:
            path = files / ref['sha256']
        return read(path, {}) if path.is_file() else {}
    folder = Path(folder)
    turns = sorted(folder.glob('*.turn-*.continuation.json'), key=lambda p: int(p.name.rsplit('.turn-', 1)[1].split('.')[0]))
    calls, rejected, usage, last = {}, 0, {}, None
    for path in turns:
        record = read(path)
        for result in record.get('results', []):
            calls[result['name']] = calls.get(result['name'], 0) + 1
            if result['name'].startswith('submit_'):
                answer = output(result.get('output') or {})
                rejected += bool(answer.get('is_error'))
                if answer.get('accepted') or (answer and not answer.get('is_error')):
                    last = answer
        usage = record.get('usage_total') or usage
    started = (folder / 'session.json').stat().st_mtime if (folder / 'session.json').exists() else None
    ended = turns[-1].stat().st_mtime if turns else None
    proposal = (last or {}).get('quality') or (last or {}).get('proposal') or {}
    actions = {}
    for entry in proposal.get('clusters', []) if isinstance(proposal, dict) else []:
        for decision in entry.get('decisions', [entry]):
            actions[decision.get('action', '?')] = actions.get(decision.get('action', '?'), 0) + 1
    return dict(session=folder.name, turns=len(turns), tool_calls=sum(calls.values()), submits=sum(n for k, n in calls.items() if k.startswith('submit_')),
                rejected=rejected, completed=(folder / 'result.json').is_file(), tokens_in=usage.get('tokens_in'),
                tokens_out=usage.get('tokens_out'), minutes=round((ended - started) / 60, 1) if started and ended else None,
                actions=actions, calls=calls)


def report(args):
    from ecarsi.stages.cases import case_run
    case = Path(args.case)
    rows = [dict(metrics(case / 'session', case_run(case) / 'files'), replay='original', model=read(case / 'case.json')['model'].get('model'))]
    for out in args.outs:
        replay = read(Path(out) / 'replay.json')
        rows.append(dict(metrics(replay['session']), replay=replay['label'], model=replay['bridge_root']))
    keys = ('replay', 'turns', 'tool_calls', 'submits', 'rejected', 'completed', 'tokens_in', 'tokens_out', 'minutes', 'actions')
    for row in rows:
        print('  '.join(f'{k}={row[k]}' for k in keys), '| model', row['model'])
    if args.json:
        print(json.dumps(rows, indent=1))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest='command', required=True)
    p = commands.add_parser('prepare')
    p.add_argument('case')
    p.add_argument('out')
    p.add_argument('--label')
    p.add_argument('--bridge-root')
    p.add_argument('--run-spec')
    for name in ('start', 'wait'):
        commands.add_parser(name).add_argument('out')
    p = commands.add_parser('report')
    p.add_argument('case')
    p.add_argument('outs', nargs='*')
    p.add_argument('--json', action='store_true')
    args = parser.parse_args()
    dict(prepare=prepare, start=start, wait=wait, report=report)[args.command](args)


if __name__ == '__main__':
    main()

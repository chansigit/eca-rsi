#!/bin/bash
# Retire a published version (decision 0019): stop its coordinators and runners once no execution runs on its task
# queue. Refused for the current version and while its queue has running executions. Lists the datasets of that
# queue whose last run ended unfinished (failed, paused, terminated): a resume runs on the queue the run started
# on, so it needs this version's coordinators again (ops/start-version.sh <name>). The directory stays: pool
# requests and pinned stage files of its runs point into it.
# usage: retire-version.sh <name>
set -euo pipefail
set -a; . "$HOME/.config/ecarsi/deployment.env"; set +a
NAME=${1:?version name}; VERSIONS=$CODE_HOME/versions
[ -f "$VERSIONS/$NAME/version.json" ] || { echo "not published: $NAME"; exit 2; }
[ "$(readlink "$VERSIONS/current" 2>/dev/null)" != "$NAME" ] || { echo "$NAME is current: make another version current first"; exit 2; }
bash "$(dirname "$0")/run.sh" control -c "
import asyncio, os, sys
from temporalio.client import Client
from ecarsi.control.dataset import UNFINISHED
from ecarsi.control.temporal import endpoint
QUEUE = 'ecarsi-$NAME'
async def main():
    client = await Client.connect(endpoint(os.environ['CONTROL'])['endpoint'])
    running = [w.id async for w in client.list_workflows('ExecutionStatus=\"Running\"') if w.task_queue == QUEUE]
    if running:
        print(QUEUE + ':', len(running), 'running executions, e.g.', *running[:3]); return 3
    last = {}
    async for w in client.list_workflows('WorkflowType=\"DatasetWorkflow\"'):
        if w.task_queue == QUEUE and (w.id not in last or w.start_time > last[w.id].start_time): last[w.id] = w
    unfinished = sorted(i for i, w in last.items() if w.status.name in UNFINISHED)
    print(QUEUE + ': no running executions;', len(unfinished), 'datasets ended unfinished (to resume: start-version.sh $NAME)')
    for i in unfinished: print('  ', i)
    return 0
code = asyncio.run(main()); sys.stdout.flush(); os._exit(code)"
VERSION=$NAME bash "$(dirname "$0")/control-plane.sh" stop coordinators runners

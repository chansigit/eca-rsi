#!/bin/bash
# Make a published version current (decision 0019): ops/run.sh, and so start-dataset and the ops helpers, use it
# from now on. Executions already running stay on the version that started them. Refused unless the version's
# task queue has a coordinator polling it and, when the bridge uses resident runners, one of its runners is fresh.
# usage: set-current.sh <name>
set -euo pipefail
set -a; . "$HOME/.config/ecarsi/deployment.env"; set +a
NAME=${1:?version name}; VERSIONS=$CODE_HOME/versions
[ -f "$VERSIONS/$NAME/version.json" ] || { echo "not published: $NAME"; exit 2; }
VERSION=$NAME bash "$(dirname "$0")/run.sh" control -c "
import asyncio, glob, json, os, time
from temporalio.client import Client
from temporalio.api.enums.v1 import TaskQueueType
from temporalio.api.taskqueue.v1 import TaskQueue
from temporalio.api.workflowservice.v1 import DescribeTaskQueueRequest
from ecarsi import task_queue
from ecarsi.control.temporal import endpoint
async def pollers():
    client = await Client.connect(endpoint(os.environ['CONTROL'])['endpoint'])
    r = await client.workflow_service.describe_task_queue(DescribeTaskQueueRequest(namespace=client.namespace,
        task_queue=TaskQueue(name=task_queue()), task_queue_type=TaskQueueType.TASK_QUEUE_TYPE_WORKFLOW))
    return len(r.pollers)
n = asyncio.run(pollers())
if not n:
    raise SystemExit(task_queue() + ': no coordinator polls it (ops/start-version.sh $NAME)')
bridge = os.environ['BRIDGE']
service = (json.load(open(bridge + '/config.json')).get('service') or {}).get('models')
beats = [json.load(open(p)) for p in glob.glob(bridge + '/runners/$NAME.*.json')]
if service and not any(time.time() - b.get('observed_at', 0) < 120 and not b.get('draining') for b in beats):
    raise SystemExit('$NAME: the bridge uses resident runners and none of this version is alive')
print(task_queue() + ':', n, 'pollers;', len(beats), 'runners')"
ln -sfn "$NAME" "$VERSIONS/.current.new"
mv -T "$VERSIONS/.current.new" "$VERSIONS/current"
echo "current: $(readlink "$VERSIONS/current")"

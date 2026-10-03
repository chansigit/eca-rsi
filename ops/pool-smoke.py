"""Submit N trivial pool requests and watch them through the scheduler: an end-to-end check of a
scheduler deploy without any dataset. usage: pool-smoke.py [N] [sleep_seconds]"""
import os, sys, time, json
from pathlib import Path
from ecarsi.warm_pool.state import submit, status, read

POOL = Path(os.environ['POOL'])  # ops/run.sh passes it
n, nap = int(sys.argv[1]) if len(sys.argv) > 1 else 24, int(sys.argv[2]) if len(sys.argv) > 2 else 5
stamp = time.strftime('%H%M%S')
ids = []
for i in range(n):
    rid = f'smoke-{stamp}-{i:03d}'
    submit(str(POOL), dict(request_id=rid, operation_id='ops.smoke',
           trace=dict(workflow_id='ops/smoke', dataset_id='ops-smoke', unit_id='ops.smoke'),
           args=['-c', f"import time; open('out.json','w').write('{{}}'); time.sleep({nap})"],
           cpus=1, memory_mb=256, timeout_seconds=120, outputs=['out.json']))
    ids.append(rid)
print(f'submitted {n} at {stamp}', flush=True)
start = time.time()
while time.time() - start < 400:
    states = {}
    for rid in ids:
        states[status(str(POOL), rid)['state']] = states.get(status(str(POOL), rid)['state'], 0) + 1
    rel = (read(POOL / 'scheduler.json') or {}).get('release') or {}
    print(f"{time.time()-start:5.0f}s {states} release: candidates={rel.get('candidates')} released={rel.get('released')} "
          f"hq_waiting={rel.get('hq_waiting')} skipped={rel.get('skipped')} gpu_queue={rel.get('gpu_queue_seconds')} measured_at={rel.get('measured_at')}", flush=True)
    if states.get('succeeded', 0) + states.get('failed', 0) == n:
        break
    time.sleep(10)
print('done' if states.get('succeeded') == n else f'NOT ALL SUCCEEDED: {states}')

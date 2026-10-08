import json, os, collections
from ecarsi.observatory import timeline_rows
B, POOL = os.environ['BASE'], os.environ['POOL']  # ops/run.sh passes them
rows = timeline_rows(B, POOL, 1.2, None, None, False)
rows = [r for r in rows if r.get('state') == 'failed']
print('failed rows in last 72 min:', len(rows))
if rows:
    print('keys:', list(rows[0].keys()))
buckets = collections.Counter()
reasons = collections.Counter()
ex = {}
for r in rows:
    t = r.get('ended') or r.get('finished') or r.get('updated') or r.get('time') or r.get('ts') or ''
    buckets[str(t)[11:15] + '0'] += 1
    rid = r.get('id') or r.get('request_id')
    folder = f"{POOL}/requests/{rid}"
    try:
        req = json.load(open(folder + '/request.json'))
    except Exception:
        reasons[('?', 'request.json missing')] += 1
        continue
    p = f"{folder}/{req['attempt_id']}/receipt.json"
    cur = json.load(open(p)).get('state') if os.path.exists(p) else 'retry in flight'
    txt = ''
    for a in sorted(d for d in os.listdir(folder) if os.path.isdir(folder + '/' + d)):
        q = f"{folder}/{a}/receipt.json"
        if os.path.exists(q):
            rc = json.load(open(q))
            if rc.get('state') == 'failed':
                txt = str(rc.get('error') or rc.get('failure') or rc.get('message') or rc.get('reason'))[:90]
    key = (str(t)[11:15] + '0', cur, txt)
    reasons[key] += 1
    ex.setdefault(key, rid)
print('per 10 min:', sorted(buckets.items()))
for k, v in sorted(reasons.items()):
    print(' ', v, '|', k, '| e.g.', str(ex.get(k, ''))[:50])

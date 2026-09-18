"""The web entry and direct routes agree with the actual scheduler lifecycle."""
import base64
import http.server
import json
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from functools import partial

import pytest

from ecarsi import pool_web, serve


@pytest.mark.skipif(not shutil.which('node'), reason='node not on PATH')
def test_pool_average_render_and_cell_filter_formatting(tmp_path):
    """Exercise the real browser scripts, including missing and stale telemetry."""
    cards = serve.HISTORY_JS.split('  function cards(){', 1)[1].split('  // -- drawing --', 1)[0]
    script = tmp_path/'metrics.js'
    script.write_text('''
const assert = require('node:assert/strict');
const elements = new Map();
global.document = {querySelectorAll:()=>[], getElementById:id=>{
  if(!elements.has(id)) elements.set(id,{hidden:true,innerHTML:'',textContent:'',replaceChildren(){this.innerHTML='';}});
  return elements.get(id);
}};
global.window = {}; global.setTimeout=()=>0;
let data={observed_at:1000, workers:[{address:'a',host:'node',job_id:'1',state:'idle',
  cpus:4,memory:1000,allocation_memory:1000,gpus:0,cpu_percent:50,rss_bytes:300,gpu_stats:[],observed_at:1000}],active:[],queued:[],
  averages:{observed_at:1000,started:410,samples:40,max_sample_gap_seconds:20,
    workers:{a:{cpu_percent:25,memory_percent:20,finished:1000}},gpu_utilization_percent:{}}};
global.fetch=async()=>({ok:true,json:async()=>data});
''' + pool_web.JS + '''
(async()=>{
  assert.equal(await window.poolMonitor.open(),true);
  assert.match(elements.get('pool-gauges').innerHTML,/10 min avg · 25.0%/);
  assert.match(elements.get('pool-workers').innerHTML,/10 min avg · 20.0%/);
  data.observed_at=1200;
  assert.equal(await window.poolMonitor.open(),true);
  assert.match(elements.get('pool-average-window').textContent,/stale/);
  assert.doesNotMatch(elements.get('pool-gauges').innerHTML,/25.0%/);
  data.averages=null;
  assert.equal(await window.poolMonitor.open(),true);
  assert.match(elements.get('pool-average-window').textContent,/waiting for the resource observer/);
  const cellCards=['cells-in','cells-queued','cells-released'].map(key=>({dataset:{stat:key},v:{},querySelector(){return this.v;}}));
  document.querySelectorAll=()=>cellCards;
  const names=()=>['x'], now=()=>1000, totals=()=>({cin:12345678,rel:12345678});
  const D={datasets:{x:{state:'queued',input_cells:12345678,organize:[]}}};
  function cards(){''' + cards + '''
  cards();
  assert.deepEqual(cellCards.map(c=>c.v.textContent),['12.35 M','12.35 M','12,345,678']);
})().catch(e=>{console.error(e);process.exitCode=1;});
''')
    subprocess.run(['node', str(script)], check=True, timeout=10)


def test_unconfigured_pool_needs_no_dask(monkeypatch):
    calls = []
    async def forbidden(_):
        calls.append(True)
        raise AssertionError("must not connect without configuration")

    monkeypatch.setattr(pool_web, "_snapshot", forbidden)
    assert pool_web.snapshot(None) is None
    assert not calls
    html = serve._navigator_html({}, serve.default_registry())
    assert 'id="pool-item" disabled aria-disabled="true"' in html
    assert 'id="pool-panel" hidden' in html
    assert '__pool__' not in serve.NAV_JS
    assert 'color-scheme:dark' not in pool_web.CSS


def test_monitor_routes_follow_live_pool_and_auth(tmp_path, monkeypatch):
    distributed = pytest.importorskip("distributed")
    from ecarsi.pool.scheduler import PoolScheduler, dispatch

    cluster = distributed.LocalCluster(n_workers=1, threads_per_worker=1, processes=False,
                                       protocol="tcp", dashboard_address=None, memory_limit=2**30)
    client = distributed.Client(cluster)
    target = str(tmp_path / "scheduler.json")
    client.write_scheduler_file(target)
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), partial(
        serve.Handler, registry=serve.Registry(tmp_path / "registry.json"),
        auth="test:password", pool_scheduler=target))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{httpd.server_port}"

    def get(path, authenticated=True):
        headers = {"Authorization": "Basic " + base64.b64encode(b"test:password").decode()} if authenticated else {}
        try:
            response = urllib.request.urlopen(urllib.request.Request(url + path, headers=headers), timeout=10)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, response.headers, response.read()

    try:
        assert get("/_pool", False)[0] == 401
        assert get("/_models/status.json", False)[0] == 401
        assert get("/_models/status.json")[0] == 200
        assert get("/_models/status.json")[1]["Cache-Control"] == "no-store"
        assert get("/_pool/status.json", False)[0] == 401
        assert get("/_pool/health", False)[0] == 401
        # A reachable ordinary Dask scheduler must not enable our pool monitor.
        assert json.loads(get("/_pool/health")[2]) == {"available": False}
        assert get("/_pool")[0] == 404
        client.register_plugin(PoolScheduler())
        assert json.loads(get("/_pool/status.json")[2])["workers"] == []
        address = next(iter(client.scheduler_info()["workers"]))
        now = time.time()
        profile = dict(cpus=1, cpu_ids=[0], memory=2**29, allocation_memory=2**30,
                       gpus=0, gpu_ids=[], host="node</script><script>alert(1)</script>", job_id="123",
                       runtime={}, roots=[], end_time=now+3600, observed_at=now)
        client.run_on_scheduler(dispatch, "register", {"address": address, "profile": profile})
        status, headers, body = get("/_pool/status.json")
        assert status == 200 and headers["Cache-Control"] == "no-store"
        state = json.loads(body)
        assert state["workers"][0]["host"] == profile["host"]
        assert state["active"] == state["queued"] == []
        task = dict(id='hung', owner=client.id, cpus=1, memory=1, gpus=0, seconds=1,
                    runtime={}, roots=[], execution_timeout=.01)
        client.run_on_scheduler(dispatch, 'enqueue', task)
        client.run_on_scheduler(dispatch, 'claim', dict(id='hung', worker=address))
        time.sleep(.02)
        stopping = json.loads(get('/_pool/status.json')[2])
        assert stopping['active'][0]['state'] == stopping['workers'][0]['state'] == 'stopping'
        status, headers, body = get("/")
        assert status == 200 and b"Warm pool" in body
        assert b'id="pool-panel" hidden' in body
        assert b"node</script>" not in body  # monitoring data is fetched only after admission
        for path in ("/_pool", "/_pool/", "/%5fpool?direct=1", "/__pool__"):
            assert get(path)[0] == 404
        assert json.loads(get("/_pool/health")[2]) == {"available": True}
        assert get("/_pool/status.json")[0] == 200
        assert get("/_pool/unknown")[0] == 404
        client.close(); cluster.close()
        assert (tmp_path / "scheduler.json").exists()
        # A stale scheduler file and previously successful reads do not grant access.
        for path in ("/_pool", "/_pool/", "/%5fpool?direct=1", "/_pool/status.json"):
            status, headers, _ = get(path)
            assert status == (503 if path.endswith('status.json') else 404)
            assert headers["Cache-Control"] == "no-store"
        assert json.loads(get("/_pool/health")[2]) == {"available": False}
        assert get("/")[0] == get("/_home")[0] == 200
    finally:
        client.close(); cluster.close()
        httpd.shutdown(); httpd.server_close(); thread.join(timeout=5)


def test_driver_queue_is_distinct_from_worker_queue():
    assert 'Dataset execution' not in pool_web.panel()
    assert 'Datasets waiting for driver' not in pool_web.panel()
    assert 'Workflow activity in Overview' in pool_web.JS
    assert 'Compute tasks waiting for worker' in pool_web.panel()
    assert 'No compute tasks waiting for a worker.' in pool_web.JS

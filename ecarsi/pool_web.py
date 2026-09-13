"""Read-only Periscope view of a live pool; no pool process lifecycle management."""
from __future__ import annotations

import asyncio
import json

from . import index


async def _snapshot(target):
    # Optional dependencies are loaded only when a pool was configured.
    from .pool.client import connect
    from .pool.scheduler import dispatch
    from .pool.status import summarize

    async with connect(target, asynchronous=True, timeout=2, set_as_default=False) as client:
        state = await client.run_on_scheduler(dispatch, "status")
        workers = (await client.scheduler.identity())["workers"]
        result = summarize(state, workers)
        result["active"] = [t for t in state["tasks"].values()
                            if t["state"] in {"granted", "running"}]
        return result


def snapshot(target):
    """Probe on every request: a leftover scheduler file is not proof of life."""
    if not target:
        return None

    async def read():
        return await asyncio.wait_for(_snapshot(target), timeout=3)

    try:
        return asyncio.run(read())
    except Exception:
        # An absent, incompatible or unreachable pool must not break Periscope.
        return None


CSS = """
.pool-head{display:flex;justify-content:space-between;align-items:baseline;gap:16px;flex-wrap:wrap;margin:12px 0 24px}
.pool-head p{margin:6px 0 0;color:var(--muted)}
.pool-summary{display:flex;gap:24px;flex-wrap:wrap;border-block:1px solid var(--line-strong);padding:16px 0;margin:0 0 24px}
.pool-summary div{min-width:110px}.pool-summary dt{color:var(--muted);font-size:var(--t3)}
.pool-summary dd{font-size:var(--t6);font-weight:650;margin:3px 0 0;font-variant-numeric:tabular-nums}
.pool-workers{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,380px),1fr));gap:16px;margin:16px 0 28px}
.pool-worker{border:1px solid var(--line);border-radius:var(--r);padding:20px;background:var(--card);min-width:0}
.pool-worker header{display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap}
.pool-worker h3{font-size:var(--t5)}.pool-meta{color:var(--muted);font-size:var(--t3);margin:6px 0 18px}
.pool-meter{margin:12px 0}.pool-meter label{display:flex;justify-content:space-between;gap:12px;font-size:var(--t3)}
.pool-meter progress{display:block;appearance:none;width:100%;height:8px;border:0;border-radius:4px;overflow:hidden;margin-top:5px;background:var(--line)}
.pool-meter progress::-webkit-progress-bar{background:var(--line)}
.pool-meter progress::-webkit-progress-value{background:var(--accent)}
.pool-meter progress::-moz-progress-bar{background:var(--accent)}
.pool-meter progress.hot::-webkit-progress-value{background:var(--run)}
.pool-meter progress.hot::-moz-progress-bar{background:var(--run)}
.pool-task{margin:16px 0 0;border-top:1px solid var(--line);padding-top:12px;overflow-wrap:anywhere;font-size:var(--t3)}
.pool-worker details{margin-top:14px;font-size:var(--t3)}.pool-worker summary{cursor:pointer;color:var(--muted)}
.pool-worker dl{margin:8px 0 0}.pool-worker dt{color:var(--muted)}.pool-worker dd{margin:0 0 8px;overflow-wrap:anywhere}
.pool-table{overflow:auto}.pool-table table{width:100%;border-collapse:collapse;font-size:var(--t3)}
.pool-table th,.pool-table td{text-align:left;padding:10px 12px;border-bottom:1px solid var(--line);vertical-align:top}
.pool-table th{color:var(--muted);font-weight:600}.pool-table td:first-child{overflow-wrap:anywhere;min-width:160px}
.pool-note{font-size:var(--t3);color:var(--muted);margin-top:16px;max-width:80ch}
#pool-updated{font-size:var(--t3);color:var(--muted)}
"""

JS = r"""
const esc = value => String(value ?? '—').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const number = (value, digits=1) => value == null || !Number.isFinite(Number(value)) ? '—' : Number(value).toFixed(digits);
const gib = value => number(value == null ? null : value / 2**30);
function duration(seconds){
  if (seconds == null) return '—';
  if (seconds < 60) return `${Math.max(0,Math.floor(seconds))}s`;
  const mins = Math.max(0, Math.floor(seconds / 60));
  return mins >= 60 ? `${Math.floor(mins/60)}h ${mins%60}m` : `${mins}m`;
}
function meter(label, text, percent){
  const known = percent != null && Number.isFinite(percent), value = known ? Math.max(0,Math.min(100,percent)) : 0;
  return `<div class="pool-meter"><label><span>${esc(label)}</span><span class="num">${esc(text)}</span></label>
    <progress max="100" value="${value}" class="${value>=85?'hot':''}" aria-label="${esc(label+': '+text)}"></progress></div>`;
}
const tones = {idle:'released',running:'running',granted:'running',draining:'tone-warn',stale:'failed',expiring:'tone-warn'};
const reasons = {busy:'Compatible workers busy',no_workers:'No workers registered',cpus:'CPU requirement',memory:'Memory requirement',
  gpus:'GPU requirement',runtime:'Software mismatch',shared_paths:'Shared paths unavailable',remaining_time:'Insufficient time remaining',draining_or_stale:'Workers draining or stale'};
function render(data){
  const workers = data.workers, active = new Map(data.active.map(t=>[t.worker,t]));
  const online = workers.filter(w=>w.state!=='stale'), allocations = new Map();
  workers.forEach(w=>allocations.set(`${w.host}/${w.job_id}`,w.allocation_memory));
  const sum = key => workers.reduce((n,w)=>n+w[key],0);
  const facts = [['Workers online',`${online.length} / ${workers.length}`],['Running / assigned',data.active.length],['Queued',data.queued.length],
    ['Worker CPUs',sum('cpus')],['Worker memory',`${gib(sum('memory'))} GiB`],['Slurm memory',`${gib([...allocations.values()].reduce((a,b)=>a+b,0))} GiB`],['GPUs',sum('gpus')]];
  document.getElementById('pool-summary').innerHTML = facts.map(([k,v])=>`<div><dt>${esc(k)}</dt><dd>${esc(v)}</dd></div>`).join('');
  document.getElementById('pool-workers').innerHTML = workers.length ? workers.map(w=>{
    const task = active.get(w.address), age = Math.max(0, data.observed_at-w.observed_at);
    const gpu = (w.gpu_stats||[]).map(g=>meter(g.name,`${number(g.utilization_percent)}% GPU`,g.utilization_percent)+
      meter('GPU memory',`${number(g.memory_used_mib==null?null:g.memory_used_mib/1024)} / ${number(g.memory_total_mib==null?null:g.memory_total_mib/1024)} GiB`,
        g.memory_used_mib==null?null:100*g.memory_used_mib/g.memory_total_mib)).join('');
    return `<article class="pool-worker"><header><h3>${esc(w.host)}</h3><span class="pill ${tones[w.state]||'neutral'}">${esc(w.state)}</span></header>
      <p class="pool-meta">Job ${esc(w.job_id)} · ${esc(w.cpus)} CPUs · ${esc(w.gpus)} GPUs · <strong>${esc(duration(w.remaining_seconds))} left</strong></p>
      ${meter('Worker CPU',`${number(w.cpu_percent)}%`,w.cpu_percent)}
      ${meter('Worker memory',`${gib(w.rss_bytes)} / ${gib(w.memory)} GiB`,w.rss_bytes==null?null:100*w.rss_bytes/w.memory)}${gpu}
      <p class="pool-task">${task?`<strong>${esc(task.label||task.id)}</strong><br>${task.state==='running'?'Running for':'Assigned for'} ${esc(duration(data.observed_at-(task.started||task.submitted)))}`:'No active task'}</p>
      <details><summary>Allocation details</summary><dl><dt>Requested</dt><dd>${esc(w.requested_tres)}</dd><dt>Allocated</dt><dd>${esc(w.allocated_tres)}</dd>
      <dt>Slurm memory</dt><dd>${gib(w.allocation_memory)} GiB</dd><dt>Worker address</dt><dd>${esc(w.address)}</dd>
      <dt>Inventory age</dt><dd>${number(age,0)} seconds</dd></dl></details></article>`;
  }).join('') : '<p class="muted">Scheduler is running. Start a worker inside an allocated Slurm job to add capacity.</p>';
  const rows = data.queued.map(t=>`<tr><td>${esc(t.label||t.id)}</td><td>${esc(duration(data.observed_at-t.submitted))}</td>
    <td>${esc(t.cpus)} CPU / ${gib(t.memory)} GiB / ${esc(t.gpus)} GPU</td><td>${esc(duration(t.seconds))}</td>
    <td>${esc((t.reason||'Waiting').split(',').map(r=>reasons[r]||r).join('; '))}</td></tr>`).join('');
  document.getElementById('pool-queue').innerHTML = rows ? `<table><thead><tr><th>Task</th><th>Waiting</th><th>Requested resources</th><th>Estimated runtime</th><th>Waiting reason</th></tr></thead><tbody>${rows}</tbody></table>` : '<p class="muted">No tasks waiting.</p>';
  document.getElementById('pool-updated').textContent = `Updated ${new Date(data.observed_at*1000).toLocaleTimeString()}`;
}
async function refresh(){
  try {
    const r = await fetch('/_pool/status.json',{cache:'no-store',signal:AbortSignal.timeout(8000)});
    if(!r.ok) throw new Error('unavailable');
    render(await r.json());
    setTimeout(refresh,5000);
  } catch(error) {
    document.getElementById('pool-content').replaceChildren();
    document.getElementById('pool-updated').textContent = 'Disconnected';
    document.getElementById('pool-offline').hidden = false;
    if(parent!==window) parent.postMessage({type:'pool-unavailable'},location.origin);
  }
}
render(JSON.parse(document.getElementById('pool-data').textContent));
setTimeout(refresh,5000);
"""


def page(data):
    payload = json.dumps(data).replace("<", "\\u003c")
    return (
        '<!doctype html><html lang="en"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>Warm pool — Periscope</title><style>{index.CSS}{CSS}</style>'
        '<body><main class="page"><header class="pool-head"><div><h1>Warm pool</h1>'
        '<p>Slurm workers, current tasks and available capacity.</p></div>'
        '<span id="pool-updated" role="status"></span></header>'
        '<div id="pool-offline" class="callout tone-bad" role="alert" hidden>'
        'Warm pool is unavailable. <a href="/" target="_top">Return to Periscope</a>.</div>'
        '<div id="pool-content"><dl id="pool-summary" class="pool-summary"></dl>'
        '<h2>Workers</h2><div id="pool-workers" class="pool-workers"></div>'
        '<h2>Queue</h2><div id="pool-queue" class="pool-table"></div>'
        '<p class="pool-note">Refreshes every 5 seconds. CPU is worker usage divided by assigned CPUs; '
        'memory is worker RSS. GPU and Slurm inventory refresh about every 30 seconds. '
        'These measurements do not describe every process on the host.</p></div>'
        f'<script type="application/json" id="pool-data">{payload}</script>'
        f'<script>{JS}</script></main></body></html>'
    )

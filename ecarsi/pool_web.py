"""Read-only Periscope view of a live pool; no pool process lifecycle management."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path


def driver_queue():
    """Dataset admission is separate from the pool's compute-task queue."""
    path = os.environ.get("ECA_PERISCOPE_BATCH_STATUS")
    if not path:
        return []
    try:
        batch = json.loads(Path(path).read_text())
        return [{"name": row["name"],
                 "reason": row.get("queue_reason", "Waiting for driver CPU/memory capacity"),
                 "submitted": batch.get("submitted_at")}
                for row in batch.get("datasets", []) if row.get("state") == "queued"]
    except (OSError, ValueError, KeyError, TypeError):
        return []



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
        result["driver_queue"] = driver_queue()
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
#pool-panel{container-type:inline-size;--gpu:var(--accent-ink);flex:1;min-height:0;overflow:auto;padding:28px;background:radial-gradient(ellipse at 12% 0,var(--paper-light),transparent 65%),var(--bg)}
#pool-panel[hidden]{display:none}
.pool-command{display:grid;grid-template-columns:minmax(240px,1fr) minmax(360px,1.15fr);gap:24px;align-items:center;padding:10px 0 26px;position:relative}
.pool-head{min-width:0}.pool-head h1{font-size:clamp(30px,3vw,42px);letter-spacing:-.04em;font-weight:650;display:flex;align-items:center;gap:14px}
.pool-symbol{width:38px;height:38px;flex:none;color:var(--accent);filter:drop-shadow(0 0 12px color-mix(in srgb,var(--accent) 25%,transparent))}
.pool-head p{margin:12px 0 18px;color:var(--muted);font-size:var(--t3);max-width:38ch;line-height:1.7}
.pool-connection{display:flex;align-items:center;gap:14px;flex-wrap:wrap;font-size:var(--t2)}
#pool-link-state{display:inline-flex;align-items:center;gap:7px;color:var(--ok)}
#pool-link-state::before{content:'';width:6px;height:6px;border-radius:50%;background:currentColor;box-shadow:0 0 9px color-mix(in srgb,var(--ok) 40%,transparent)}
#pool-updated{color:var(--muted)}
.pool-gauges{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;padding:18px 12px;border:1px solid var(--line);border-radius:var(--r);background:var(--card);box-shadow:0 0 20px color-mix(in srgb,var(--accent) 8%,transparent)}
.pool-gauge{text-align:center;min-width:0;--signal:var(--accent)}.pool-gauge.ram{--signal:var(--ok)}.pool-gauge.gpu{--signal:var(--gpu)}
.pool-dial{position:relative;width:100%;max-width:100px;aspect-ratio:1;margin:0 auto 6px}.pool-dial svg{display:block;width:100%;height:100%;transform:rotate(-90deg)}
.pool-dial .track{fill:none;stroke:var(--line);stroke-width:6}.pool-dial .arc{fill:none;stroke:var(--signal);stroke-width:6;stroke-linecap:round;filter:drop-shadow(0 0 3px color-mix(in srgb,var(--signal) 50%,transparent))}
.pool-dial strong{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-size:23px;font-weight:550;letter-spacing:-.04em;font-variant-numeric:tabular-nums}
.pool-dial strong small{font-size:12px;color:var(--muted);margin:5px 0 0 3px}
.pool-gauge>span{font-size:var(--t2);color:var(--muted)}
.pool-summary{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:16px;border-block:1px solid var(--line);padding:20px 0;margin:0 0 30px}
.pool-summary div{min-width:0}.pool-summary dt{color:var(--muted);font-size:12px;line-height:1.5;min-height:36px}
.pool-summary dd{font-size:clamp(15px,1.6vw,21px);font-weight:550;margin:3px 0 0;font-variant-numeric:tabular-nums;white-space:nowrap;letter-spacing:-.025em}
.pool-section-heading{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:16px}
.pool-section-heading h2{font-size:18px;font-weight:550;display:flex;align-items:center;gap:10px}
.pool-section-heading span{font-size:12px;color:var(--muted)}
.pool-workers{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,370px),1fr));gap:18px;margin:0 0 30px}
.pool-worker{border:1px solid var(--line);border-radius:var(--r);padding:20px;background:var(--surface);box-shadow:var(--paper-shadow);min-width:0;position:relative;--node-color:var(--accent);transition:border-color .2s,box-shadow .2s}
.pool-worker.gpu-node{--node-color:var(--gpu)}
.pool-worker:hover{border-color:color-mix(in srgb,var(--node-color) 65%,var(--line));box-shadow:0 0 18px color-mix(in srgb,var(--node-color) 12%,transparent)}
.pool-worker[data-state=stale]{border-color:var(--bad)}
.pool-worker header{display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap}
.pool-node-title{display:flex;align-items:center;gap:11px;min-width:0}.pool-chip{width:30px;height:30px;color:var(--node-color);flex:none}
.pool-worker h3{font-family:var(--mono);font-size:16px;font-weight:600}.pool-worker .pill{font-size:12px;border:1px solid color-mix(in srgb,var(--st) 20%,transparent);background:color-mix(in srgb,var(--st) 9%,transparent)}
.pool-meta{display:flex;gap:10px;flex-wrap:wrap;color:var(--muted);font-size:12px;margin:14px 0 20px;padding-bottom:14px;border-bottom:1px solid var(--line)}
.pool-meta strong{color:var(--ink-soft);font-weight:500;margin-left:auto}
.pool-meter{margin:13px 0;--meter-color:var(--accent)}.pool-meter.gpu{--meter-color:var(--gpu)}
.pool-meter label{display:flex;justify-content:space-between;gap:12px;font-size:12px;color:var(--muted)}.pool-meter .num{color:var(--ink-soft);white-space:nowrap}
.pool-meter progress{display:block;appearance:none;width:100%;height:6px;border:0;border-radius:2px;overflow:hidden;margin-top:8px;background:var(--line)}
.pool-meter progress::-webkit-progress-bar{background:var(--line)}
.pool-meter progress::-webkit-progress-value{background:var(--meter-color);box-shadow:0 0 10px var(--meter-color)}
.pool-meter progress::-moz-progress-bar{background:var(--meter-color)}
.pool-meter progress.hot::-webkit-progress-value{background:var(--run)}.pool-meter progress.hot::-moz-progress-bar{background:var(--run)}
.pool-task{margin:18px 0 0;border-top:1px solid var(--line);padding-top:14px;overflow-wrap:anywhere;font-size:12px;color:var(--muted)}.pool-task strong{color:var(--ink);font-weight:500}
.pool-task-idle{display:flex;align-items:center;gap:8px}.pool-task-idle::before{content:'';width:5px;height:5px;border-radius:50%;background:var(--muted)}
.pool-worker details{margin-top:14px;font-size:12px}.pool-worker summary{cursor:pointer;color:var(--muted)}.pool-worker summary:hover{color:var(--accent)}
.pool-worker dl{margin:12px 0 0;display:grid;grid-template-columns:105px minmax(0,1fr);gap:8px}.pool-worker dt{color:var(--muted)}.pool-worker dd{margin:0;overflow-wrap:anywhere;color:var(--ink-soft)}
.pool-table{overflow:auto;border:1px solid var(--line);border-radius:var(--r);background:var(--card)}
.pool-table table{width:100%;border-collapse:collapse;font-size:var(--t3)}
.pool-table th,.pool-table td{text-align:left;padding:14px 16px;border-bottom:1px solid var(--line);vertical-align:top}
.pool-table th{color:var(--muted);font-size:12px;font-weight:500;background:var(--row-alt)}.pool-table td:first-child{overflow-wrap:anywhere;min-width:160px}
.pool-table tr:last-child td{border-bottom:0}.pool-table tbody tr:hover{background:var(--row-hover)}
.pool-empty{padding:22px;display:flex;align-items:center;gap:12px;color:var(--muted);font-size:13px}.pool-empty svg{width:28px;height:28px;color:var(--accent);flex:none}.pool-empty p{margin:0}
.pool-note{font-size:12px;color:var(--muted);margin-top:18px;max-width:90ch;line-height:1.7}
@container(max-width:850px){.pool-command{grid-template-columns:1fr}.pool-gauges{max-width:540px;width:100%}.pool-summary{grid-template-columns:repeat(4,minmax(0,1fr))}.pool-summary dt{min-height:0}.pool-summary dd{font-size:18px}}
@media(max-width:460px){#pool-panel{padding:20px 16px 32px}}
@container(max-width:460px){.pool-command{gap:20px}.pool-head h1{font-size:32px}.pool-summary{grid-template-columns:repeat(2,minmax(0,1fr));row-gap:20px}.pool-worker{padding:16px}.pool-section-heading span{display:none}.pool-meta strong{margin-left:0}.pool-gauges{padding:14px 6px;gap:4px}.pool-dial{max-width:85px}.pool-dial strong{font-size:20px}}
@media(prefers-reduced-transparency:reduce){.pool-worker{background:var(--card)}}
@media(prefers-reduced-motion:reduce){#pool-panel *,#pool-panel *::before,#pool-panel *::after{transition:none!important;animation:none!important}}
"""

JS = r"""
(function(){
const esc = value => String(value ?? '—').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const number = (value, digits=1) => value == null || !Number.isFinite(Number(value)) ? '—' : Number(value).toFixed(digits);
const gib = value => number(value == null ? null : value / 2**30);
function duration(seconds){
  if (seconds == null) return '—';
  if (seconds < 60) return `${Math.max(0,Math.floor(seconds))}s`;
  const mins = Math.max(0, Math.floor(seconds / 60));
  return mins >= 60 ? `${Math.floor(mins/60)}h ${mins%60}m` : `${mins}m`;
}
function meter(label, text, percent, kind='cpu'){
  const known = percent != null && Number.isFinite(percent), value = known ? Math.max(0,Math.min(100,percent)) : 0;
  return `<div class="pool-meter ${kind}"><label><span>${esc(label)}</span><span class="num">${esc(text)}</span></label>
    <progress max="100" value="${value}" class="${value>=85?'hot':''}" aria-label="${esc(label+': '+text)}"></progress></div>`;
}
function gauge(label, percent, kind){
  const known = percent != null && Number.isFinite(percent), value = known ? Math.max(0,Math.min(100,percent)) : 0;
  return `<div class="pool-gauge ${kind}" role="img" aria-label="${esc(label)}: ${known?number(percent)+'%':'unavailable'}"><div class="pool-dial">
    <svg viewBox="0 0 100 100" aria-hidden="true"><circle class="track" cx="50" cy="50" r="40"/><circle class="arc" cx="50" cy="50" r="40" stroke-dasharray="${value*2.51327} 251.327"/></svg>
    <strong>${known?number(percent):'—'}${known?'<small>%</small>':''}</strong></div><span>${esc(label)}</span></div>`;
}
const chip = '<svg class="pool-chip" viewBox="0 0 32 32" fill="none" stroke="currentColor" stroke-width="1.2" aria-hidden="true"><rect x="7" y="7" width="18" height="18" rx="4"/><rect x="12" y="12" width="8" height="8" rx="1"/><path d="M12 3v4m8-4v4M12 25v4m8-4v4M3 12h4m-4 8h4m18-8h4m-4 8h4"/></svg>';
const tones = {idle:'released',running:'running',granted:'running',draining:'tone-warn',stale:'failed',expiring:'tone-warn'};
const reasons = {busy:'Compatible workers busy',no_workers:'No workers registered',cpus:'CPU requirement',memory:'Memory requirement',
  gpus:'GPU requirement',runtime:'Software mismatch',shared_paths:'Shared paths unavailable',remaining_time:'Insufficient time remaining',draining_or_stale:'Workers draining or stale'};
function render(data){
  const expanded = new Set([...document.querySelectorAll('.pool-worker details[open]')].map(d=>d.closest('article').dataset.worker));
  const workers = data.workers, active = new Map(data.active.map(t=>[t.worker,t]));
  const online = workers.filter(w=>w.state!=='stale'), allocations = new Map();
  workers.forEach(w=>allocations.set(`${w.host}/${w.job_id}`,w.allocation_memory));
  const sum = key => workers.reduce((n,w)=>n+w[key],0);
  const cpu = online.filter(w=>w.cpu_percent!=null), ram = online.filter(w=>w.rss_bytes!=null);
  const gpu = online.flatMap(w=>w.gpu_stats||[]).filter(g=>g.utilization_percent!=null);
  document.getElementById('pool-gauges').innerHTML = gauge('Worker CPU',cpu.length?cpu.reduce((n,w)=>n+w.cpu_percent*w.cpus,0)/cpu.reduce((n,w)=>n+w.cpus,0):null,'cpu')+
    gauge('Worker memory',ram.length?100*ram.reduce((n,w)=>n+w.rss_bytes,0)/ram.reduce((n,w)=>n+w.memory,0):null,'ram')+
    gauge('GPU utilization',gpu.length?gpu.reduce((n,g)=>n+g.utilization_percent,0)/gpu.length:null,'gpu');
  const cores = cpu.reduce((n,w)=>n+w.cpu_percent*w.cpus/100,0);
  const facts = [['Workers online',`${online.length} / ${workers.length}`],['Running / assigned',data.active.length],['Compute tasks queued',data.queued.length],
    ['CPU cores in use',cpu.length?`${number(cores)} / ${sum('cpus')}`:'—'],
    ['Worker memory',`${gib(sum('memory'))} GiB`],['Slurm memory',`${gib([...allocations.values()].reduce((a,b)=>a+b,0))} GiB`],['GPUs',sum('gpus')]];
  document.getElementById('pool-summary').innerHTML = facts.map(([k,v])=>`<div><dt>${esc(k)}</dt><dd>${esc(v)}</dd></div>`).join('');
  document.getElementById('pool-workers').innerHTML = workers.length ? workers.map(w=>{
    const task = active.get(w.address), age = Math.max(0, data.observed_at-w.observed_at);
    const gpu = (w.gpu_stats||[]).map(g=>meter(g.name,`${number(g.utilization_percent)}% GPU`,g.utilization_percent,'gpu')+
      meter('GPU memory',`${number(g.memory_used_mib==null?null:g.memory_used_mib/1024)} / ${number(g.memory_total_mib==null?null:g.memory_total_mib/1024)} GiB`,
        g.memory_used_mib==null?null:100*g.memory_used_mib/g.memory_total_mib,'gpu')).join('');
    return `<article class="pool-worker ${w.gpus?'gpu-node':''}" data-state="${esc(w.state)}" data-worker="${esc(w.address)}"><header><div class="pool-node-title">${chip}<h3>${esc(w.host)}</h3></div><span class="pill ${tones[w.state]||'neutral'}">${esc(w.state)}</span></header>
      <p class="pool-meta"><span>Job ${esc(w.job_id)}</span><span>${esc(w.cpus)} CPUs / ${esc(w.gpus)} GPUs</span><strong>${esc(duration(w.remaining_seconds))} left</strong></p>
      ${meter('Worker CPU',`${number(w.cpu_percent)}% · ${number(w.cpu_percent==null?null:w.cpu_percent*w.cpus/100)} cores`,w.cpu_percent)}
      ${meter('Worker memory',`${gib(w.rss_bytes)} / ${gib(w.memory)} GiB`,w.rss_bytes==null?null:100*w.rss_bytes/w.memory)}${gpu}
      <p class="pool-task">${task?`<strong>${esc(task.label||task.id)}</strong><br>${task.state==='running'?'Running for':'Assigned for'} ${esc(duration(data.observed_at-(task.started||task.submitted)))}`:'<span class="pool-task-idle">No active task</span>'}</p>
      <details${expanded.has(w.address)?' open':''}><summary>Allocation details</summary><dl><dt>Requested</dt><dd>${esc(w.requested_tres)}</dd><dt>Allocated</dt><dd>${esc(w.allocated_tres)}</dd>
      <dt>Slurm memory</dt><dd>${gib(w.allocation_memory)} GiB</dd><dt>Worker address</dt><dd>${esc(w.address)}</dd>
      <dt>Inventory age</dt><dd>${number(age,0)} seconds</dd></dl></details></article>`;
  }).join('') : '<p class="muted">Scheduler is running. Start a worker inside an allocated Slurm job to add capacity.</p>';
  const rows = data.queued.map(t=>`<tr><td>${esc(t.label||t.id)}</td><td>${esc(duration(data.observed_at-t.submitted))}</td>
    <td>${esc(t.cpus)} CPU / ${gib(t.memory)} GiB / ${esc(t.gpus)} GPU</td><td>${esc(duration(t.seconds))}</td>
    <td>${esc((t.reason||'Waiting').split(',').map(r=>reasons[r]||r).join('; '))}</td></tr>`).join('');
  document.getElementById('pool-queue').innerHTML = rows ? `<table><thead><tr><th>Task</th><th>Waiting</th><th>Requested resources</th><th>Estimated runtime</th><th>Waiting reason</th></tr></thead><tbody>${rows}</tbody></table>` : '<div class="pool-empty"><p>No compute tasks waiting for a worker.</p></div>';
  const datasets = data.driver_queue || [];
  document.getElementById('pool-driver-queue').innerHTML = datasets.length ? `<table><thead><tr><th>Dataset</th><th>Waiting</th><th>Waiting reason</th></tr></thead><tbody>${datasets.map(d=>`<tr><td>${esc(d.name)}</td><td>${esc(d.submitted==null?'—':duration(data.observed_at-d.submitted))}</td><td>${esc(d.reason)}</td></tr>`).join('')}</tbody></table>` : '<div class="pool-empty"><p>No datasets waiting for a driver.</p></div>';
  document.getElementById('pool-updated').textContent = `Updated ${new Date(data.observed_at*1000).toLocaleTimeString()}`;
}
let generation = 0, timer;
const panel = document.getElementById('pool-panel');
function close(){
  generation++;
  clearTimeout(timer);
  panel.hidden = true;
  for(const id of ['pool-workers','pool-gauges','pool-summary','pool-queue','pool-driver-queue']) document.getElementById(id).replaceChildren();
}
async function refresh(token){
  try {
    const r = await fetch('/_pool/status.json',{cache:'no-store',signal:AbortSignal.timeout(8000)});
    if(!r.ok) throw new Error('unavailable');
    const data = await r.json();
    if(token!==generation) return false;
    render(data);
    panel.hidden = false;
    timer = setTimeout(()=>refresh(token),5000);
    return true;
  } catch(error) {
    if(token!==generation) return false;
    close();
    window.dispatchEvent(new Event('pool-unavailable'));
    return false;
  }
}
window.poolMonitor = {close, isOpen:()=>!panel.hidden, open:()=>{close();return refresh(generation);}};
})();
"""


def panel():
    return (
        '<section id="pool-panel" hidden aria-label="Warm pool"><div class="pool-command"><header class="pool-head"><h1>'
        '<svg class="pool-symbol" viewBox="0 0 40 40" fill="none" stroke="currentColor" stroke-width="1.3" aria-hidden="true">'
        '<path d="M12 12 20 20m8-8-8 8m-8 8 8-8m8 8-8-8"/><rect x="4" y="4" width="10" height="10" rx="3"/>'
        '<rect x="26" y="4" width="10" height="10" rx="3"/><rect x="4" y="26" width="10" height="10" rx="3"/>'
        '<rect x="26" y="26" width="10" height="10" rx="3"/><circle cx="20" cy="20" r="3" fill="currentColor"/></svg>'
        'Warm pool</h1><p>Slurm workers, current tasks and available capacity.</p>'
        '<div class="pool-connection"><span id="pool-link-state">Connected</span>'
        '<span id="pool-updated" role="status"></span></div></header>'
        '<div id="pool-gauges" class="pool-gauges"></div></div>'
        '<div id="pool-content"><dl id="pool-summary" class="pool-summary"></dl>'
        '<div class="pool-section-heading"><h2>Workers</h2><span>Live Slurm allocations</span></div>'
        '<div id="pool-workers" class="pool-workers"></div>'
        '<div class="pool-section-heading"><h2>Datasets waiting for driver</h2></div>'
        '<div id="pool-driver-queue" class="pool-table"></div>'
        '<p class="pool-note">These datasets have not reached the compute queue. Their drivers need capacity before they can submit work.</p>'
        '<div class="pool-section-heading"><h2>Compute tasks waiting for worker</h2><span>Arrival order / first compatible worker</span></div>'
        '<div id="pool-queue" class="pool-table"></div>'
        '<p class="pool-note">Refreshes every 5 seconds. CPU is worker usage divided by assigned CPUs; '
        'memory is worker RSS. GPU and Slurm inventory refresh about every 30 seconds. '
        'These measurements do not describe every process on the host.</p></div>'
        '</section>'
    )

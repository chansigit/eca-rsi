"""Read-only model routing inventory; no inference calls or credential exposure."""
from __future__ import annotations

import html
import json
import os
from pathlib import Path


def snapshot(environ=None):
    from harness_bridge import parse_model_pool, resolve_agent_config

    env = os.environ if environ is None else environ
    path = Path(env.get('ECA_MODEL_CATALOG', str(Path.home() / '.config/ecarsi/model-pool.json')))
    catalog = json.loads(path.read_text()) if path.is_file() else {}
    spec = env.get('AGENT_MODEL_POOL', '').strip()
    source = 'Periscope process configuration'
    if spec:
        candidates = parse_model_pool(spec)
    elif env.get('HARNESS') or env.get('MODEL'):
        candidates = [resolve_agent_config(environ=env)]
    elif catalog.get('candidates'):
        candidates = parse_model_pool(catalog['candidates'])
        source = str(catalog.get('source', 'Model catalog'))
    else:
        candidates = [resolve_agent_config(environ=env)]
        source = 'Bridge default; no explicit pool configured'
    # Only explicitly public fields leave this process. Never serialize the
    # environment, arbitrary catalog keys, provider URLs or exception details.
    verified = {}
    for record in catalog.get('validation', []):
        cfg = parse_model_pool(record['candidate'])
        if len(cfg) != 1:
            raise ValueError('validation must identify one model')
        verified[str(cfg[0])] = {**cfg[0].as_manifest(),
                               'checked_at': str(record['checked_at']),
                               'scope': str(record['scope'])}
    chain = [{**c.as_manifest(), 'position': i + 1, 'validation': verified.get(str(c))}
             for i, c in enumerate(candidates)]
    chosen = {str(c) for c in candidates}
    return {'source': source, 'chain': chain,
            'alternatives': [v for k, v in verified.items() if k not in chosen],
            'validated_count': len(verified), 'live_availability': 'not_probed'}


def render(data):
    e = lambda value: html.escape(str(value), quote=True)
    def card(model, position=None):
        label = ('Primary' if position == 1 else f'Fallback {position - 1}') if position else 'Validated alternative'
        validation = model.get('validation') if position else model
        check = (f'<span class="st released">Passed recorded check</span> · {e(validation["checked_at"])}'
                 f'<p>{e(validation["scope"])}</p>') if validation else '<span class="muted">No recorded validation</span>'
        return (f'<article class="model-card"><div class="model-order">{f"{position:02d}" if position else "—"} · {label}</div>'
                f'<h3>{e(model["model"])}</h3><p class="model-backend">{e(model["harness"])}</p>'
                f'<div class="model-check">{check}</div></article>')
    chain = ''.join(card(m, m['position']) for m in data['chain'])
    alternatives = ''.join(card(m) for m in data['alternatives'])
    return (f'<header class="hero"><h1>Agent model pool</h1><p class="lede">Primary and fallback models, in calling order.</p>'
            f'<dl class="facts"><div><dt>Configured models</dt><dd>{len(data["chain"])}</dd></div>'
            f'<div><dt>Fallbacks</dt><dd>{max(0,len(data["chain"])-1)}</dd></div>'
            f'<div><dt>Previously validated</dt><dd>{data["validated_count"]}</dd></div></dl></header>'
            '<section class="block"><h2>Calling order</h2>'
            f'<p class="lede">Source: {e(data["source"])}</p><div class="model-chain">{chain}</div>'
            + ('<p class="model-note">No fallback is configured.</p>' if len(data['chain']) == 1 else '') + '</section>'
            + (f'<section class="block"><h2>Validated alternatives</h2><p class="lede">Not in the configured fallback chain.</p>'
               f'<div class="model-chain">{alternatives}</div></section>' if alternatives else '')
            + '<p class="model-note">Recorded checks describe earlier RSI runs; live availability, account quota and concurrent calls are not monitored here. '
              'Each driver can override its model chain. This view does not change running jobs or send model requests.</p>')


CSS = """
#model-panel{flex:1;min-height:0;overflow:auto;padding:28px}
#model-panel[hidden]{display:none}
#model-panel>div{max-width:1200px;margin:0 auto}
.model-chain{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,280px),1fr));gap:16px}
.model-card{min-width:0;border:1px solid var(--line);border-radius:var(--r);padding:20px;background:var(--surface);box-shadow:var(--paper-shadow)}
.model-order{font:var(--t2) var(--mono);color:var(--muted);margin-bottom:14px}
.model-card h3{font-size:20px;overflow-wrap:anywhere}.model-backend{font:var(--t2) var(--mono);color:var(--muted);margin:10px 0 20px}
.model-check{border-top:1px solid var(--line);padding-top:14px;font-size:var(--t3);color:var(--muted)}.model-check p{margin:6px 0 0}
.model-note{color:var(--muted);font-size:var(--t3);margin:16px 0 0}
@media(max-width:700px){#model-panel{padding:16px}}
@media(prefers-reduced-transparency:reduce){.model-card{background:var(--card)}}
"""

JS = """
(function(){
 const panel=document.getElementById('model-panel');let generation=0,timer;
 function close(){generation++;clearTimeout(timer);panel.hidden=true;panel.replaceChildren();}
 async function refresh(token){
  try{
   const r=await fetch('/_models/status.json',{cache:'no-store',signal:AbortSignal.timeout(8000)});
   if(!r.ok)throw Error('unavailable');const data=await r.json();if(token!==generation)return false;
   const body=document.createElement('div');body.innerHTML=data.html;panel.replaceChildren(body);panel.hidden=false;
   timer=setTimeout(()=>refresh(token),30000);return true;
  }catch(e){
   if(token!==generation)return false;
   panel.textContent='Model configuration is unavailable. Use reload to try again.';panel.hidden=false;return true;
  }
 }
 window.modelMonitor={close,isOpen:()=>!panel.hidden,open:()=>{close();return refresh(generation);}};
})();
"""

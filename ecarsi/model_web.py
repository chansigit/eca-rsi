"""Model inventory and settings; no inference calls or credential exposure."""
from __future__ import annotations

import html
import json
import os
from pathlib import Path


# Environment names are bridge contracts; model names and URLs live only in the catalog.
PROVIDERS = {
    'openai': ('ARK_API_KEY', 'DOUBAO_BASE_URL'),
    'openai@openrouter': ('OPENROUTER_API_KEY', 'OPENROUTER_BASE_URL'),
    'openai@vllm': ('VLLM_API_KEY', 'VLLM_BASE_URL'),
}


def catalog_path(environ=None):
    env = os.environ if environ is None else environ
    return Path(env.get('ECA_MODEL_CATALOG', str(Path.home() / '.config/ecarsi/model-pool.json')))


def read_catalog(environ=None):
    import hashlib
    path = catalog_path(environ)
    raw = path.read_bytes() if path.is_file() else b'{}'
    return json.loads(raw), hashlib.sha256(raw).hexdigest()


def normalized_models(catalog):
    from harness_bridge import parse_model_pool
    from urllib.parse import urlsplit
    entries = catalog.get('models')
    if entries is None:
        entries = [c.as_manifest() for c in parse_model_pool(catalog['candidates'])] if catalog.get('candidates') else []
    if not isinstance(entries, list) or len(entries) > 32:
        raise ValueError('Use at most 32 model entries.')
    result, seen, urls = [], set(), {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get('harness'), str) or not isinstance(entry.get('model'), str):
            raise ValueError('Each entry needs a backend and model name.')
        if set(entry) - {'harness', 'model', 'url'}:
            raise ValueError('Only backend, model and URL belong in the catalog. Put keys in bashrc.')
        configs = parse_model_pool(entry['harness'] + ':' + entry['model'])
        if len(configs) != 1 or any(c.isspace() for c in entry['model']) or len(entry['model']) > 200:
            raise ValueError('Enter one model identifier per row, without whitespace.')
        cfg = configs[0]
        if str(cfg) in seen:
            raise ValueError('Duplicate model in the calling order.')
        seen.add(str(cfg))
        url = entry.get('url', '')
        if not isinstance(url, str) or len(url) > 2048:
            raise ValueError('Invalid model URL.')
        url = url.strip().rstrip('/')
        if url:
            try:
                parsed = urlsplit(url)
                valid = parsed.scheme in ('http', 'https') and parsed.hostname and not (parsed.username or parsed.password or parsed.query or parsed.fragment)
                parsed.port
            except ValueError:
                valid = False
            if not valid or any(c.isspace() for c in url):
                raise ValueError('Use an HTTP(S) API base URL without credentials, query or fragment.')
            if cfg.backend not in PROVIDERS:
                raise ValueError('This backend manages its connection through its own CLI, not a model URL.')
        if cfg.backend in urls and urls[cfg.backend] != url:
            raise ValueError('Models using the same backend must share its API base URL.')
        urls[cfg.backend] = url
        result.append({**cfg.as_manifest(), 'url': url})
    return result


def save_models(models, revision, environ=None):
    from .run_state import writer_lock, write_json
    path = catalog_path(environ)
    clean = normalized_models({'models': models})
    with writer_lock(path.with_suffix('.lock')):
        catalog, current = read_catalog(environ)
        if revision != current:
            raise FileExistsError('Configuration changed. Reload before saving.')
        catalog.pop('candidates', None)
        catalog.update(models=clean, source='User model configuration')
        write_json(path, catalog)
    return {'ok': True}


def admin_matches(token, environ=None):
    import hmac
    try:
        expected = catalog_path(environ).with_name('model-admin-token').read_text().strip()
        return len(expected) >= 32 and hmac.compare_digest(token, expected)
    except OSError:
        return False


def key_presence(environ=None, bashrc=None):
    import re
    import shlex
    env = os.environ if environ is None else environ
    catalog, _ = read_catalog(env)
    models = normalized_models(catalog)
    path = Path(bashrc) if bashrc is not None else Path.home() / '.bashrc'
    source = path.read_text() if path.is_file() else ''
    def filled(value):
        return bool(value.strip()) and value.strip().lower() not in {'paste_real_key_here', 'your_api_key', 'your-key', 'changeme', 'placeholder'}
    result = []
    for key in sorted({PROVIDERS[m['harness']][0] for m in models if m['harness'] in PROVIDERS}):
        state = 'not_found'
        for line in source.splitlines():
            if not re.match(r'^\s*(?:export\s+)?' + key + r'\s*=', line):
                continue
            try:
                words = shlex.split(line.strip().removeprefix('export ').strip(), comments=True)
                value = words[0].partition('=')[2] if len(words) == 1 else None
                state = ('indirect' if value is None or '$' in value or '`' in value else
                         'present_literal' if filled(value) else 'empty_or_placeholder')
            except ValueError:
                state = 'indirect'
        result.append({'variable': key, 'bashrc': state, 'process': filled(env.get(key, ''))})
    return {'keys': result, 'live_test': False}


def launch_exports(models):
    import shlex
    if not models:
        return '# Add a model before starting a driver.'
    lines = ['export AGENT_MODEL_POOL=' + shlex.quote(','.join(m['harness'] + ':' + m['model'] for m in models))]
    for backend, (_, variable) in PROVIDERS.items():
        found = next((m for m in models if m['harness'] == backend and m['url']), None)
        if found:
            lines.append('export ' + variable + '=' + shlex.quote(found['url']))
    return '\n'.join(lines)


def snapshot(environ=None):
    from harness_bridge import parse_model_pool

    catalog, revision = read_catalog(environ)
    models = normalized_models(catalog)
    source = str(catalog.get('source', 'Model catalog'))
    # Only explicitly public fields leave this process. Never serialize the
    # environment, arbitrary catalog keys or exception details.
    verified = {}
    for record in catalog.get('validation', []):
        cfg = parse_model_pool(record['candidate'])
        if len(cfg) != 1:
            raise ValueError('validation must identify one model')
        verified[str(cfg[0])] = {**cfg[0].as_manifest(),
                               'checked_at': str(record['checked_at']),
                               'scope': str(record['scope'])}
    chain = [{**m, 'position': i + 1, 'validation': verified.get(m['harness'] + ':' + m['model'])}
             for i, m in enumerate(models)]
    chosen = {m['harness'] + ':' + m['model'] for m in models}
    return {'source': source, 'chain': chain, 'revision': revision, 'exports': launch_exports(models),
            'alternatives': [v for k, v in verified.items() if k not in chosen],
            'validated_count': len(verified), 'live_availability': 'not_probed'}


def render(data):
    e = lambda value: html.escape(str(value), quote=True)
    def card(model, position=None):
        provider = {'openai': 'Ark / Doubao', 'openai@openrouter': 'OpenRouter',
                    'openai@vllm': 'Self-hosted vLLM', 'claude': 'Claude Code',
                    'deepseek': 'dsh'}.get(model['harness'], model['harness'])
        label = ('Primary' if position == 1 else f'Fallback {position - 1}') if position else 'Validated alternative'
        validation = model.get('validation') if position else model
        check = (f'<span class="st released">Passed recorded check</span> · {e(validation["checked_at"])}'
                 f'<p>{e(validation["scope"])}</p>') if validation else '<span class="muted">No recorded validation</span>'
        return (f'<article class="model-card"><div class="model-order">{f"{position:02d}" if position else "—"} · {label}</div>'
                f'<h3>{e(model["model"])}</h3><p class="model-backend">{e(provider)}</p>'
                + (f'<p class="model-url">{e(model["url"])}</p>' if model.get('url') else '') +
                f'<div class="model-check">{check}</div></article>')
    chain = ''.join(card(m, m['position']) for m in data['chain']) or '<p class="muted">No models configured.</p>'
    alternatives = ''.join(card(m) for m in data['alternatives'])
    return (f'<header class="hero"><h1>Agent Bridge</h1><p class="lede">Primary and fallback models, in calling order.</p>'
            f'<dl class="facts"><div><dt>Configured models</dt><dd>{len(data["chain"])}</dd></div>'
            f'<div><dt>Fallbacks</dt><dd>{max(0,len(data["chain"])-1)}</dd></div>'
            f'<div><dt>Previously validated</dt><dd>{data["validated_count"]}</dd></div></dl></header>'
            '<section class="block"><div class="model-tools"><h2>Model routing</h2><button class="btn plain" data-model-action="edit">Edit models</button><button class="btn plain" data-model-action="keys">Check API keys</button></div>'
            + controls() +
            f'<p class="lede">Source: {e(data["source"])}</p><div class="model-chain">{chain}</div>'
            + ('<p class="model-note">No fallback is configured.</p>' if len(data['chain']) == 1 else '') + '</section>'
            + (f'<section class="block"><h2>Validated alternatives</h2><p class="lede">Not in the configured fallback chain.</p>'
               f'<div class="model-chain">{alternatives}</div></section>' if alternatives else '')
            + settings(data)
            + '<p class="model-note">Recorded checks describe earlier RSI runs; live availability, account quota and concurrent calls are not monitored here. '
              'Each driver can override its model chain. Saved settings do not change running jobs. Apply the launch exports below when starting new drivers. No model requests are sent.</p>')


def controls():
    return ('<div id="model-admin" class="callout" hidden><label>Management key <input type="password" id="model-admin-key" autocomplete="off"></label>'
            '<button class="btn plain" data-model-action="unlock">Unlock settings</button>'
            '<p class="model-note">Public edits require the management key stored on the server in <code>~/.config/ecarsi/model-admin-token</code>. This is not a model API key.</p></div>'
            '<p id="model-message" class="model-note" role="status"></p><div id="model-key-results"></div>')


def settings(data):
    e = lambda value: html.escape(str(value), quote=True)
    keys = sorted({PROVIDERS[m['harness']][0] for m in data['chain'] if m['harness'] in PROVIDERS})
    instructions = '\n'.join(f"export {key}='PASTE_REAL_KEY_HERE'" for key in keys)
    return ('<section class="block"><h2>Credentials &amp; new runs</h2>'
            '<p class="lede">Set these variables in <code>~/.bashrc</code> on the driver host. Keep API keys out of model URLs and this page.</p>'
            f'<pre class="model-code">{e(instructions) if instructions else "No API key variables required by the configured adapters."}</pre>'
            '<p class="model-note">vLLM only needs a key if its server enforces authentication. Claude Code uses its own login; dsh uses its own configuration.</p>'
            '<p class="model-note">The check reports direct assignments in bashrc and variables inherited by this web process. It does not run bashrc, follow sourced files, validate credentials with a provider, or reveal key values. After editing, run <code>source ~/.bashrc</code> before starting a new driver; existing processes keep their old environment.</p>'
            '<details class="model-launch"><summary>Launch exports for new drivers</summary>'
            f'<pre class="model-code">{e(data["exports"])}</pre></details></section>'
            '<section id="model-edit" class="block" hidden><h2>Edit calling order</h2><p class="lede">First is primary; following entries are fallbacks. API URLs are shared by models using the same backend.</p>'
            '<div id="model-rows"></div><div class="model-tools"><button class="btn plain" data-model-action="add">Add model</button>'
            '<button class="btn" data-model-action="save">Save configuration</button><button class="btn plain" data-model-action="cancel">Cancel</button></div></section>')


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
.model-tools{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin:12px 0}.model-tools h2{margin-right:auto}
.model-code{overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere;background:var(--none-bg);padding:16px;border-radius:var(--r);font:var(--t2)/1.6 var(--mono)}
.model-url{overflow-wrap:anywhere;font:var(--t2) var(--mono);color:var(--muted)}
.model-edit-row{display:grid;grid-template-columns:180px minmax(0,1fr) minmax(0,1.5fr);gap:12px;padding:16px 0;border-bottom:1px solid var(--line)}
.model-edit-row label{min-width:0;font-size:var(--t3);color:var(--muted)}
.model-edit-row input,.model-edit-row select,#model-admin-key{display:block;width:100%;font:var(--t3) var(--sans);background:var(--card);color:var(--ink);border:1px solid var(--line-strong);border-radius:var(--r);padding:8px;margin-top:5px}
.model-edit-row .model-tools{grid-column:1/-1;margin:0}.model-launch{margin-top:16px}
@media(max-width:900px){.model-edit-row{grid-template-columns:1fr}}
@media(max-width:700px){#model-panel{padding:16px}}
@media(prefers-reduced-transparency:reduce){.model-card{background:var(--card)}}
"""

JS = r"""
(function(){
 const panel=document.getElementById('model-panel');let generation=0,timer,state,editing=false,managementKey='',pendingAction;
 const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
 const el=id=>document.getElementById(id);
 function close(){generation++;clearTimeout(timer);editing=false;panel.hidden=true;panel.replaceChildren();}
 async function refresh(token){
  try{
   const r=await fetch('/_models/status.json',{cache:'no-store',signal:AbortSignal.timeout(8000)});
   if(!r.ok)throw Error('unavailable');const data=await r.json();if(token!==generation)return false;
   state=data;const body=document.createElement('div');body.innerHTML=data.html;panel.replaceChildren(body);panel.hidden=false;
   timer=setTimeout(()=>{if(!editing)refresh(token);},30000);return true;
  }catch(e){
   if(token!==generation)return false;
   panel.textContent='Model configuration is unavailable. Use reload to try again.';panel.hidden=false;return true;
  }
 }
 async function post(action,body={}){
  const r=await fetch('/_models/'+action,{method:'POST',headers:{'Content-Type':'application/json','X-Model-Admin':managementKey},body:JSON.stringify(body),signal:AbortSignal.timeout(10000)});
  const data=await r.json();if(r.status===403){clearTimeout(timer);el('model-admin').hidden=false;el('model-admin').scrollIntoView({block:'nearest'});el('model-admin-key').focus({preventScroll:true});throw Error('Unlock model settings with the management key first.');}
  if(!r.ok)throw Error(data.error||'Request failed');return data;
 }
 function addRow(model={harness:'openai',model:'',url:''}){
  const row=document.createElement('div');row.className='model-edit-row';
  row.innerHTML=`<label>Backend<select class="model-harness">${['openai','openai@openrouter','openai@vllm','claude','deepseek'].map(h=>`<option${h===model.harness?' selected':''}>${h}</option>`).join('')}</select></label><label>Model<input class="model-name" value="${esc(model.model)}" autocomplete="off"></label><label>API base URL<input class="model-endpoint" type="url" value="${esc(model.url)}" placeholder="https://provider.example/v1" autocomplete="off"></label><div class="model-tools"><button class="btn plain" data-model-action="up" aria-label="Move model up">↑</button><button class="btn plain" data-model-action="down" aria-label="Move model down">↓</button><button class="btn plain" data-model-action="remove">Remove</button></div>`;
  el('model-rows').appendChild(row);
 }
 async function action(name,button){
  const token=generation;
  if(['edit','keys'].includes(name)){pendingAction=name;await post('access');if(token!==generation)return;}
  if(name==='edit'){editing=true;clearTimeout(timer);el('model-rows').replaceChildren();state.chain.forEach(addRow);el('model-edit').hidden=false;el('model-edit').scrollIntoView({block:'nearest'});}
  if(name==='add')addRow();
  if(name==='cancel'){editing=false;await refresh(token);}
  if(name==='up'||name==='down'||name==='remove'){
   const row=button.closest('.model-edit-row');
   if(name==='remove')row.remove();else if(name==='up'&&row.previousElementSibling)row.parentNode.insertBefore(row,row.previousElementSibling);else if(name==='down'&&row.nextElementSibling)row.parentNode.insertBefore(row.nextElementSibling,row);
  }
  if(name==='save'){
   const models=[...panel.querySelectorAll('.model-edit-row')].map(row=>({harness:row.querySelector('.model-harness').value,model:row.querySelector('.model-name').value.trim(),url:row.querySelector('.model-endpoint').value.trim()}));
   await post('save',{models,revision:state.revision});if(token!==generation)return;
   editing=false;await refresh(token);if(token===generation){el('model-message').textContent='Saved. Apply the launch exports to new drivers; running jobs are unchanged.';el('model-message').scrollIntoView({block:'nearest'});}
  }
  if(name==='unlock'){
   managementKey=el('model-admin-key').value;await post('access');if(token!==generation)return;
   el('model-admin-key').value='';el('model-admin').hidden=true;el('model-message').textContent='Model settings unlocked.';
   if(pendingAction)await action(pendingAction);
  }
  if(name==='keys'){
   const result=await post('keys');if(token!==generation)return;
   const labels={present_literal:'Direct non-placeholder value found',not_found:'No direct assignment found',empty_or_placeholder:'Empty or placeholder',indirect:'Indirect expression — not evaluated'};
   el('model-key-results').innerHTML=result.keys.map(k=>`<p class="model-note"><b>${esc(k.variable)}</b><br>bashrc: ${labels[k.bashrc]}<br>Web process: ${k.process?'Variable present':'Not inherited'}</p>`).join('')||'<p class="model-note">These adapters do not require an API key variable; check their CLI login separately.</p>';
   el('model-message').textContent='Presence check only; no credential was sent to a provider.';
   el('model-key-results').scrollIntoView({block:'nearest'});
  }
 }
 panel.addEventListener('click',async event=>{
  const button=event.target.closest('[data-model-action]');if(!button)return;
  const token=generation;button.disabled=true;
  try{await action(button.dataset.modelAction,button);}catch(e){if(token===generation&&el('model-message')){el('model-message').textContent=e.message;if(el('model-admin').hidden)el('model-message').scrollIntoView({block:'nearest'});}}
  finally{button.disabled=false;}
 });
 window.modelMonitor={close,isOpen:()=>!panel.hidden,open:()=>{close();return refresh(generation);}};
})();
"""

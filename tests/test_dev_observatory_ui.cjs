const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const html = fs.readFileSync(require('node:path').join(__dirname, '../ecarsi/ui/observatory.html'), 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1].replace(/refresh\(\); setInterval\(refresh,10000\);/, '');
const elements = new Map();
const element = id => {
  if (!elements.has(id)) elements.set(id, {value: '', hidden: false, listeners: {}, textContent: '',
    checked: false, dataset: {}, classList: {remove() {}, add() {}, toggle() {}},
    querySelector: () => null, querySelectorAll: () => [], setAttribute() {}, removeAttribute() {},
    addEventListener(name, fn) { this.listeners[name] = fn; }});
  return elements.get(id);
};
const context = vm.createContext({
  document: {getElementById: element, querySelector: () => ({addEventListener() {}}),
             querySelectorAll: () => []},
  window: {addEventListener() {}}, Date, Map, Set, URLSearchParams, JSON, Math, Number, String, Array,
  location: {hash: ''}, history: {replaceState() {}}, localStorage: {getItem: () => null, setItem() {}},
  setTimeout: () => 1, clearTimeout() {},
});
vm.runInContext(script, context);
element("timeline-view").value="workers";

// The wheel is the vertical scroll and nothing else: a zoom handler on the same surface made the
// two gestures fight, and the page is no longer an archive to zoom around in.
assert.equal(element('timeline').listeners.wheel, undefined);
assert.equal(context.zoomWindow, undefined);
assert.equal(vm.runInContext('JSON.stringify(SPANS)', context), '[900,3600,14400]');
assert.equal(vm.runInContext('view.span', context), 3600);
assert.equal(vm.runInContext('typeof timeAtFraction', context), 'undefined');

const task = (id, start) => ({id, service: 'pool', operation: 'compute', worker_id: 'worker-a',
  submitted_at: start - 100, started_at: start, finished_at: start + 200, state: 'succeeded',
  trace: {dataset_id: id, workflow_id: 'run', unit_id: 'compute'}});
context.renderTimeline({since: 0, until: 600, tasks: [task('a', 100), task('b', 150)], total: 2,
  resources: [{worker_id: 'worker-a', observed_at: 200, cpu_percent: 50, memory_percent: 20,
    cpu_cores_allocated: 4, cpu_cores_used: 2, memory_total_gb: 16, memory_used_gb: 3,
    gpu_percent: null, gpu_memory_percent: null, samples: 1}], source: 'test'});
const chart = element('timeline').innerHTML;
assert.equal((chart.match(/data-group="worker-a"/g)||[]).length, 1);
// Collapsed by default: one fixed-height load strip, no per-task node, no stack of slots.
assert(chart.includes('class="timeline-track load"'));
assert(chart.includes('height:30px'));
assert(!chart.includes('top:41px'));   // no second stacked slot: the lane never grows
assert(!chart.includes('timeline-bar'));
assert(chart.includes('peak 2 · 0 running · 2 finished'));
assert(chart.includes('class="load-seg"'));
assert(element('timeline-note').textContent.includes("tasks-<day>.jsonl"));

// The sweep: two tasks overlapping in the middle give 1, 2, 1 concurrent.
const segments = vm.runInContext('loadSegments', context)([{task: task('a', 100)}, {task: task('b', 150)}], 0, 600, 600);
assert.equal(JSON.stringify(segments.map(s => [s.a, s.b, s.n])), '[[100,150,1],[150,300,2],[300,350,1]]');
// Equal-count, same-dataset neighbours merge instead of becoming two nodes.
const one = task('a', 100);
assert.equal(vm.runInContext('loadSegments', context)([{task: one}, {task: {...one, id: 'a2'}}], 0, 600, 600).length, 1);

// Clicking a lane opens that worker's individual bars, and only that worker's.
element('timeline').listeners.click({target: {closest: sel => sel === '[data-worker]' ? {dataset: {worker: 'worker-a'}} : null}});
const opened = element('timeline').innerHTML;
assert(opened.includes('top:41px'));   // expanded: the second concurrent task gets its own slot
assert(opened.includes('data-tip-title="a" data-tip-step="compute"'));
assert(opened.includes('Duration: 200.0 s'));
element('timeline').listeners.click({target: {closest: sel => sel === '[data-worker]' ? {dataset: {worker: 'worker-a'}} : null}});
assert(!element('timeline').innerHTML.includes('top:41px'));

context.renderTimeline({since: 0, until: 600, total: 4, resources: [], source: 'test', tasks: [
  {...task('legacy-a', 100), worker_id: null, host: 'node-a', cpu_ids: [7]},
  {...task('legacy-b', 150), worker_id: null, host: 'node-a', cpu_ids: [8]},
  {...task('call-a', 100), service: 'bridge'}, {...task('call-b', 150), service: 'bridge'},
]});
const labels = element('timeline').innerHTML;
assert.equal((labels.match(/data-group="node-a"/g)||[]).length,1);
assert(labels.includes('peak 2 ·'));
assert(!labels.includes('node-a · CPU'));
assert.equal((labels.match(/data-group="Agent Bridge"/g)||[]).length,1);
assert(labels.includes('data-worker="Agent Bridge"'));
// Per-task detail is behind the lane, not in it: a worker keyed by host expands the same way.
element('timeline').listeners.click({target:{closest:sel=>sel==='[data-worker]'?{dataset:{worker:'node-a'}}:null}});
assert(element('timeline').innerHTML.includes('CPU affinity: logical IDs 7'));
element('timeline').listeners.click({target:{closest:sel=>sel==='[data-worker]'?{dataset:{worker:'node-a'}}:null}});
assert(!labels.includes(' · calls '));

// A window extending past the clock must not extend a running task there.
context.Date = class extends Date { static now() { return 200000; } };
context.renderTimeline({since: 100, until: 400, total: 1, tasks: [
  {...task('live', 150), finished_at: null, state: 'running'},
]});
const liveWidth=Number(element('timeline').innerHTML.match(/width:([\d.]+)%/)[1]);
assert(Math.abs(liveWidth-100*50/300)<.001);
context.Date=Date;

const stage = (id, workflow, dataset, operation, start, end, depends_on) => ({
  id, service: operation === 'organize.plan' ? 'bridge' : 'pool', operation,
  started_at: start, finished_at: end, state: operation === 'organize.plan' ? 'reply_saved' : 'succeeded',
  trace: {workflow_id: workflow, dataset_id: dataset, unit_id: operation, ...(depends_on ? {depends_on} : {})},
});
const prepare = stage('run-a.prepare', 'organize/run-a', 'dataset-a', 'organize.prepare', 100, 150);
const plan = stage('run-a.plan', 'organize/run-a', 'dataset-a', 'organize.plan', 170, 200, ['run-a.prepare']);
const execute = stage('run-a.execute', 'organize/run-a', 'dataset-a', 'organize.execute', 220, 250);
const otherRun = stage('run-b.execute', 'organize/run-b', 'dataset-a', 'organize.execute', 260, 290, ['run-a.prepare']);
const unrelated = stage('unrelated', 'other/run', 'dataset-a', 'compute', 300, 330);
const links = context.buildFlowEdges([prepare, plan, execute, otherRun, unrelated]);
assert.equal(links.length, 3);
assert.equal(links[2].parent.id, "run-a.prepare"); // Explicit dependencies can cross stage workflow IDs.
assert.equal(links[0].source, 'recorded dependency');
assert.equal(links[1].source, 'known Organize order');
assert.equal(links[1].parent.id, 'run-a.plan');
const root = element('timeline');
root.clientWidth = 870;
root.scrollHeight = 300;
root.style = {setProperty() {}};
root.getBoundingClientRect = () => ({left: 0, top: 0});
root.querySelector = selector => selector === '.timeline-scale' ? {getBoundingClientRect: () => ({left: 245, right: 870, width: 625})} : null;
root.querySelectorAll = () => [prepare, plan, execute].map((task, index) => ({
  dataset: {taskId: task.id, workflow: task.trace.workflow_id}, offsetHeight: 25,
  getBoundingClientRect: () => ({top: 50 + index * 45, bottom: 75 + index * 45,
    left: 350 + index * 100, right: 390 + index * 100}),
}));
root.insertAdjacentHTML = (_, svg) => { root.flowSvg = svg; };
assert.equal(context.drawFlow(links), 2);
assert(root.flowSvg.includes('data-workflow="organize/run-a"'));
assert(root.flowSvg.includes('class="flow-edge"'));
assert(root.flowSvg.includes('M 390 50 C 417 50, 423 95, 450 95 L 450 120 C 423 120, 417 75, 390 75 Z'));
assert(!root.flowSvg.includes('stroke-width'));
assert(!root.flowSvg.includes('<circle'));
assert(!root.flowSvg.includes('NaN'));
assert.equal(context.datasetColor('dataset-a'), context.datasetColor('dataset-a'));
assert.equal(context.connectorRibbonPath(10,1,5,20,2,6),
  'M 10 1 C 14.5 1, 15.5 2, 20 2 L 20 6 C 15.5 6, 14.5 5, 10 5 Z');

// Dependencies must survive narrow gaps, overlapping minimum-size glyphs, and both lane directions.
for (const gap of [-5, 0, 1, 60]) for (const direction of [-1, 1]) {
  root.querySelectorAll = () => [prepare, plan, execute].map((task, index) => ({
    dataset: {taskId: task.id, workflow: task.trace.workflow_id},
    getBoundingClientRect: () => ({top: 140 + direction * index * 45, bottom: 165 + direction * index * 45,
      left: 350 + index * (40 + gap), right: 390 + index * (40 + gap)}),
  }));
  assert.equal(context.drawFlow(links), 2, `gap=${gap}, direction=${direction}`);
  assert(root.flowSvg.includes('data-parent="run-a.prepare" data-child="run-a.plan"'));
  assert(root.flowSvg.includes('data-parent="run-a.plan" data-child="run-a.execute"'));
  if (Math.abs(gap) <= 5) assert(root.flowSvg.includes('--edge-min-opacity:.5'));
}
// Arbitrary unit names, fan-out, fan-in and another iteration use explicit request dependencies.
const generic = (id, parents) => stage(id, 'analysis/run-1', 'dataset-a', id, 200, 300, parents);
const graph = [generic('input', []), generic('sample-a', ['input']), generic('sample-b', ['input']),
  generic('merge', ['sample-a', 'sample-b']), generic('sample-a-iteration-2', ['merge'])];
// Clock skew/status do not erase a declared dependency.
graph[0].state = 'failed';
const graphLinks = context.buildFlowEdges(graph);
assert.equal(graphLinks.length, 5);
const collision = stage('input', 'analysis/another-run', 'dataset-b', 'input', 1, 2, []);
assert.equal(context.buildFlowEdges([...graph, collision]).length, 5);

const poolWorker=(id,reporting=true)=>({worker_id:id,host:'same-host',slurm_job_id:id,cpus:2,
  memory_mb:1024,reporting,last_seen:100,current:{cpu_percent:0,memory_percent:25,
  memory_used_gb:1,memory_total_gb:4,gpu_percent:null},mean_5m:{cpu_percent:10},
  sample_count_5m:10,tasks:[],reserved_cpus:0,reserved_memory_mb:0});
context.renderComputePool({workers:[poolWorker('11'),poolWorker('12'),poolWorker('old',false)],pool_waiting:3});
const poolHtml=element('pool-hosts').innerHTML;
assert.equal((poolHtml.match(/data-pool-host=/g)||[]).length,1);
assert.equal((poolHtml.match(/data-pool-worker=/g)||[]).length,2);
assert.equal((poolHtml.match(/Host RAM/g)||[]).length,2); // label + accessible meter label, once per host
assert(poolHtml.includes('0.0% · 5m 10.0%'));
assert(poolHtml.includes('GPU —'));
assert(!poolHtml.includes('Idle · no unfinished tasks'));
assert(!poolHtml.includes('samples in the last 5 minutes'));
assert(element('pool-history').innerHTML.includes('old'));
assert(element('compute-pool-summary').textContent.includes('4 CPU / 2.0 GiB'));
context.renderComputePool({workers:[]});
assert(element('pool-hosts').innerHTML.includes('No workers'));

// Business hierarchy preserves separate runs and exposes readable steps without hover.
assert.notEqual(context.datasetColor('Tabula Sapiens SS2 / Prostate'),context.datasetColor('Tabula Sapiens SS2 / Uterus'));
const flows=[stage('prepare-1','organize/first','Prostate','organize.prepare',100,120,[]),
  stage('plan-1','organize/first','Prostate','organize.plan',121,140,['prepare-1']),
  stage('prepare-2','organize/second','Prostate','organize.prepare',150,160,[]),
  stage('plan-2','organize/second','Prostate','organize.plan',161,180,['prepare-2']),
  stage('uterus','organize/uterus','Uterus','organize.prepare',100,120,[])];
const grouped=context.flowGroups(flows);
assert.equal(grouped.find(d=>d.name==='Prostate').runs.size,2);
assert.equal(context.stepName(flows[1]),'Plan experiments');
element('timeline-view').value='datasets';
root.querySelectorAll=()=>[];
context.renderTimeline({since:90,until:200,tasks:flows,total:flows.length});
assert(element('timeline').innerHTML.includes('Prostate'));
assert(element('timeline').innerHTML.includes('Uterus'));
assert(element('timeline').innerHTML.includes('2 stage runs'));
assert(element('timeline').innerHTML.includes('Prepare inputs'));
assert(element('timeline').innerHTML.includes('Plan experiments'));
const stepKey=JSON.stringify(['Prostate','organize/second','organize.plan']);
element('timeline').listeners.click({target:{closest:sel=>sel==='[data-worker]'?null:{dataset:{stepSelect:stepKey}}}});
assert.equal(element('flow-detail').hidden,false);
assert(element('flow-detail').innerHTML.includes('Plan experiments'));
assert(element('flow-detail').innerHTML.includes('Model call'));
assert(element('flow-detail').innerHTML.includes('plan-2'));

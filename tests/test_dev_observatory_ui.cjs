const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const html = fs.readFileSync(require('node:path').join(__dirname, '../ecarsi/dev_observatory.html'), 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1].replace(/refresh\(\); setInterval\(refresh,10000\);/, '');
const elements = new Map();
const element = id => {
  if (!elements.has(id)) elements.set(id, {value: '', hidden: false, listeners: {},
    classList: {remove() {}}, addEventListener(name, fn) { this.listeners[name] = fn; }});
  return elements.get(id);
};
const context = vm.createContext({
  document: {getElementById: element, querySelector: () => ({addEventListener() {}})},
  window: {addEventListener() {}}, Date, Map, URLSearchParams, setTimeout: () => 1, clearTimeout() {},
});
vm.runInContext(script, context);
const now = Date.now() / 1000;
const original = {since: now - 3600, until: now};
const zoomed = context.zoomWindow(original, .25, -100);
assert(zoomed.until - zoomed.since < 3600);
assert(Math.abs(zoomed.since + (zoomed.until - zoomed.since) * .25 - (original.since + 900)) < .01);
const widest = context.zoomWindow({since: now - 80000, until: now}, .5, 500);
assert(Math.abs(widest.until - widest.since - 86400) < .01);

const task = (id, start) => ({id, service: 'pool', operation: 'compute', worker_id: 'worker-a',
  submitted_at: start - 100, started_at: start, finished_at: start + 200, state: 'succeeded',
  trace: {dataset_id: id, workflow_id: 'run', unit_id: 'compute'}});
context.renderTimeline({since: 0, until: 600, tasks: [task('a', 100), task('b', 150)], total: 2,
  resources: [{worker_id: 'worker-a', observed_at: 200, cpu_percent: 50, memory_percent: 20,
    cpu_cores_allocated: 4, cpu_cores_used: 2, memory_total_gb: 16, memory_used_gb: 3,
    gpu_percent: null, gpu_memory_percent: null, samples: 1}], source: 'test'});
const chart = element('timeline').innerHTML;
assert(chart.includes('worker-a · lane 2'));
assert(chart.indexOf('worker-a · lane 2') < chart.indexOf('worker-a · CPU'));
assert(!chart.includes('timeline-queue'));
assert(element('timeline-note').textContent.includes('started-task queue time is in task details'));
let prevented = false;
element('timeline').listeners.wheel({target: {closest: () => ({getBoundingClientRect: () => ({left: 0, width: 100})})},
  clientX: 25, deltaY: -100, preventDefault() { prevented = true; }});
assert(prevented);
assert.equal(element('timeline-range').value, 'custom');
assert.equal(element('timeline-from').hidden, false);
const fitted = context.latestActivityWindow([
  {started_at: 100, finished_at: 150}, {started_at: 200, finished_at: 250},
  {started_at: 1000, finished_at: 1040}, {started_at: 1080, finished_at: 1120},
], 2000);
assert(fitted.since < 1000 && fitted.since > 900);
assert(fitted.until > 1120 && fitted.until < 1200);
assert.equal(Math.round(fitted.until-fitted.since), 139);

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
assert.equal(links.length, 2);
assert.equal(links[0].source, 'recorded dependency');
assert.equal(links[1].source, 'known Organize order');
assert.equal(links[1].parent.id, 'run-a.plan');
const root = element('timeline');
root.clientWidth = 870;
root.scrollHeight = 300;
root.style = {setProperty() {}};
root.getBoundingClientRect = () => ({left: 0, top: 0});
root.querySelector = () => ({getBoundingClientRect: () => ({left: 245, right: 870, width: 625})});
root.querySelectorAll = () => [prepare, plan, execute].map((task, index) => ({
  dataset: {taskId: task.id}, offsetHeight: 25,
  getBoundingClientRect: () => ({top: 50 + index * 45}),
}));
root.insertAdjacentHTML = (_, svg) => { root.flowSvg = svg; };
assert.equal(context.drawFlow(links, 0, 400), 2);
assert(root.flowSvg.includes('data-workflow="organize/run-a"'));
assert(root.flowSvg.includes('class="flow-edge"'));
assert(!root.flowSvg.includes('NaN'));
assert.equal(context.datasetColor('dataset-a'), context.datasetColor('dataset-a'));

context.renderTimeline({since: 0, until: 1000, tasks: [], total: 0, source: 'test', resources:
  Array.from({length: 240}, (_, i) => ({worker_id: 'worker-a', observed_at: i * 4,
    cpu_percent: i % 100, memory_percent: 20, gpu_percent: null, gpu_memory_percent: null,
    cpu_cores_allocated: 4, cpu_cores_used: 2, memory_total_gb: 16, memory_used_gb: 3, samples: 1}))});
const denseChart = element('timeline').innerHTML;
assert(denseChart.includes('<polyline'));
assert((denseChart.match(/pointer-events="all"/g)||[]).length <= 80);
assert(!denseChart.includes('<line '));

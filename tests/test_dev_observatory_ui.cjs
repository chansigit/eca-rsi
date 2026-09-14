const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const html = fs.readFileSync(require('node:path').join(__dirname, '../ecarsi/dev_observatory.html'), 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1].replace(/refresh\(\); setInterval\(refresh,10000\);/, '');
const elements = new Map();
const element = id => {
  if (!elements.has(id)) elements.set(id, {value: '', hidden: false, listeners: {}, addEventListener(name, fn) { this.listeners[name] = fn; }});
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

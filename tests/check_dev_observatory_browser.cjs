// Optional browser check: NODE_PATH=<playwright installation> node tests/check_dev_observatory_browser.cjs <viewer URL>
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require('playwright');

(async () => {
  const browser = await chromium.launch({headless: true});
  try {
    const page = await browser.newPage({viewport: {width: 1440, height: 1100}});
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    // Freeze polling while checking a sequence of layouts; scientific services stay untouched.
    await page.addInitScript(() => { window.setInterval = () => 0; });
    await page.goto(process.argv[2] || 'http://127.0.0.1:8765/');
    await page.waitForSelector('.timeline-bar');
    const inventory = await page.evaluate(async () => {
      const data = await (await fetch('/api/status')).json();
      return {expected: data.workers.filter(w => w.reporting).map(w => w.worker_id).sort(),
        displayed: [...document.querySelectorAll('[data-pool-worker]')].map(e => e.dataset.poolWorker).sort(),
        summary: document.getElementById('compute-pool-summary').textContent};
    });
    assert.deepEqual(inventory.displayed, inventory.expected);
    console.log('compute pool', JSON.stringify(inventory));
    const cardWidths = await page.evaluate(async () => {
      const cards = [...document.querySelectorAll('[data-pool-host]')];
      if (!cards.length) return null;
      const before = cards[0].getBoundingClientRect().width;
      cards.slice(1).forEach(card => card.remove());
      const alone = cards[0].getBoundingClientRect().width;
      renderComputePool(await (await fetch('/api/status')).json());
      return {before, alone};
    });
    if (cardWidths) {
      assert.equal(cardWidths.alone, cardWidths.before, 'A remaining node must not stretch');
      assert(cardWidths.alone <= 360, 'Node cards have a bounded width');
      console.log('node widths', JSON.stringify(cardWidths));
    }
    if (process.env.OBSERVATORY_ARTIFACTS) {
      fs.mkdirSync(process.env.OBSERVATORY_ARTIFACTS, {recursive: true});
      await page.locator('#compute-pool').screenshot({path: path.join(process.env.OBSERVATORY_ARTIFACTS, 'compute-pool.png')});
    }
    const placement = await page.evaluate(async () => {
      const {since,until}=currentTimelineWindow;
      const data=await (await fetch('/api/timeline?'+new URLSearchParams({since,until}))).json();
      const tasks=new Map(data.tasks.map(t=>[taskKey(t),t]));
      const bars=[...document.querySelectorAll('#timeline .timeline-bar')];
      const problems=[];
      for (const bar of bars) {
        const task=tasks.get(JSON.stringify([bar.dataset.workflow,bar.dataset.taskId]));
        const group=task.service==='bridge'?'Agent Bridge':task.worker_id||task.host||'Pool worker unknown';
        if (bar.closest('[data-group]').dataset.group!==group) problems.push('wrong worker: '+task.id);
        const track=bar.parentElement.getBoundingClientRect(), box=bar.getBoundingClientRect();
        const start=Math.max(since,task.started_at), end=Math.min(until,task.finished_at||Date.now()/1000);
        if (Math.abs(box.left-track.left-(start-since)/(until-since)*track.width)>.1) problems.push('wrong start: '+task.id);
        if (task.finished_at && Math.abs(box.width-Math.max(1,(end-start)/(until-since)*track.width))>.1) problems.push('wrong duration: '+task.id);
      }
      const groups=[...document.querySelectorAll('#timeline [data-group]')].map(row=>{
        const items=[...row.querySelectorAll('.timeline-bar')].map(bar=>({bar,task:tasks.get(JSON.stringify([bar.dataset.workflow,bar.dataset.taskId]))}));
        for(let i=0;i<items.length;i++) for(let j=i+1;j<items.length;j++) {
          const a=items[i],b=items[j];
          if(Math.min(a.task.finished_at||Infinity,b.task.finished_at||Infinity)>Math.max(a.task.started_at,b.task.started_at) && a.bar.dataset.slot===b.bar.dataset.slot) problems.push('overlap hidden: '+a.task.id+' / '+b.task.id);
        }
        return {group:row.dataset.group,tasks:items.length,slots:new Set(items.map(x=>x.bar.dataset.slot)).size};
      });
      return {bars:bars.length,groups,problems,resourceChartsInTimeline:document.querySelectorAll('#timeline .timeline-spark').length};
    });
    assert.deepEqual(placement.problems,[]);
    assert.equal(placement.resourceChartsInTimeline,0);
    console.log('recorded placement / duration / overlap',JSON.stringify(placement));
    async function checkEdges(label) {
      const counts = await page.evaluate(() => {
        const bars = new Map([...document.querySelectorAll('.timeline-bar')].map(bar => [
          JSON.stringify([bar.dataset.workflow, bar.dataset.taskId]), bar.getBoundingClientRect()]));
        const pair = (workflow, parent, child) => JSON.stringify([workflow, parent, child]);
        const expected = currentFlowEdges.filter(edge => bars.has(taskKey(edge.parent)) && bars.has(taskKey(edge.child)));
        const rendered = [...document.querySelectorAll('.flow-edge')];
        const actual = new Set(rendered.map(edge => pair(edge.dataset.workflow, edge.dataset.parent, edge.dataset.child)));
        const root = document.getElementById('timeline').getBoundingClientRect();
        const misplaced = rendered.filter(edge => {
          const source = bars.get(JSON.stringify([edge.dataset.workflow, edge.dataset.parent]));
          const start = edge.getAttribute('d').match(/^M (\S+) (\S+)/);
          return !source || Math.abs(Number(start[1]) - (source.right - root.left)) > .1 ||
            Math.abs(Number(start[2]) - (source.top - root.top)) > .1;
        });
        return {expected: expected.length, rendered: rendered.length, misplaced: misplaced.length,
          missing: expected.filter(edge => !actual.has(pair(edge.child.trace.workflow_id, edge.parent.id, edge.child.id)))
            .map(edge => `${edge.parent.id} -> ${edge.child.id}`)};
      });
      assert.deepEqual(counts.missing, [], label);
      assert.equal(counts.rendered, counts.expected, label);
      assert.equal(counts.misplaced, 0, label);
      console.log(label, JSON.stringify(counts));
      return counts;
    }
    const initial = await checkEdges('real / latest activity');
    assert(initial.expected > 0, 'The viewer must have real workflow dependencies to exercise');
    if (process.env.OBSERVATORY_ARTIFACTS) {
      fs.mkdirSync(process.env.OBSERVATORY_ARTIFACTS, {recursive: true});
      await page.locator('.timeline-wrap').first().screenshot({path: path.join(process.env.OBSERVATORY_ARTIFACTS, 'timeline-complete.png')});
    }
    await page.selectOption('#timeline-range', '86400');
    await page.waitForFunction(() => currentTimelineWindow.until - currentTimelineWindow.since === 86400);
    await checkEdges('real / 24 hours');
    await page.setViewportSize({width: 1000, height: 1100});
    await page.waitForTimeout(180);
    await checkEdges('real / resized');

    const benchmark = await page.evaluate(() => {
      const now = Date.now() / 1000, tasks = [];
      for (let i = 0; i < 100; i++) {
        const start = now - 1000 + i * 2;
        const units = [['input', [], 0, .4], ['a', ['input'], .5, 1], ['b', ['input'], .6, 1.2],
          ['join', ['a', 'b'], 1.3, 1.5], ['iteration-2', ['join'], 1.6, 2]];
        for (const [id, parents, begin, end] of units) tasks.push({
          id, operation: id, service: id === 'join' ? 'bridge' : 'pool', worker_id: `worker-${id}`,
          state: 'succeeded', submitted_at: start + begin, started_at: start + begin, finished_at: start + end,
          trace_source: 'explicit', trace: {workflow_id: `workflow-${i}`, dataset_id: `dataset-${i}`,
            unit_id: id, depends_on: parents},
        });
      }
      const before = performance.now();
      renderTimeline({since: now - 86400, until: now, tasks, total: tasks.length, resources: [], source: 'browser test'});
      return {tasks: tasks.length, milliseconds: Math.round(performance.now() - before)};
    });
    const large = await checkEdges('100 datasets / fan-out + fan-in + iteration / subpixel task durations');
    assert.equal(large.expected, 500);
    console.log('render', JSON.stringify(benchmark));
    assert.deepEqual(errors, []);
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });

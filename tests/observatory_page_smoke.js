// Smoke-run every render path of observatory.html against a stub DOM. Extract the script FROM DISK first:
//   python3 -c "import re,pathlib;h=pathlib.Path('ecarsi/observatory.html').read_text();pathlib.Path('/tmp/g2/observatory-inline.js').write_text(chr(10).join(re.findall(r'<script[^>]*>(.*?)</script>',h,flags=re.S)))"
const fs=require("fs"); let src=fs.readFileSync("/tmp/g2/observatory-inline.js","utf8");
// Each element keeps its own value/checked: one shared backing store made the dataset filter
// overwrite the view select and hid a real bug behind a fake one.
const nodes=new Map();
function makeNode(id) {
  const node={style:{},classList:{toggle(){},add(){},remove(){},contains(){return false}},dataset:{},addEventListener(){},
    querySelectorAll(){return []},querySelector(){return el()},getBoundingClientRect(){return {left:0,right:1000,top:0,bottom:100,width:1000,height:100}},
    setPointerCapture(){},closest(){return null},appendChild(){},remove(){},insertAdjacentHTML(){},setAttribute(){},removeAttribute(){},
    value:id==='timeline-view'?'workers':'', checked:id==='timeline-log', hidden:false, textContent:'', innerHTML:'', className:'',
    clientWidth:1000, scrollHeight:300};
  return new Proxy(node,{get(t,k){return k in t?t[k]:(k==='then'?undefined:(t[k]=el()))},set(t,k,v){t[k]=v;return true}});
}
function el(id) {
  if (id===undefined) return makeNode();
  if (!nodes.has(id)) nodes.set(id,makeNode(id));
  return nodes.get(id);
}
global.el=el;
global.document={getElementById:id=>el(id),querySelector:()=>el(),querySelectorAll:()=>[],createElement:()=>el(),body:el(),addEventListener(){}};
global.window={addEventListener(){},modelMonitor:{},poolMonitor:{}}; global.location={hash:""}; global.history={replaceState(){}};
global.localStorage={getItem:()=>null,setItem(){}}; global.matchMedia=()=>({matches:false}); let fetchCount=0, counting=false;
global.fetch=()=>{ if (counting) fetchCount++; return new Promise(()=>{}); };
global.COUNT=on=>{ counting=on; fetchCount=0; };
global.FETCHES=()=>fetchCount; global.setInterval=()=>0; global.setTimeout=(f,ms)=>{ if(ms===400||ms===300) f(); return 0; }; global.clearTimeout=()=>{};
const now=Math.floor(Date.now()/1000);
global.task=(id,a,b,extra={})=>({id,operation:"osp.compute",state:"succeeded",submitted_at:a-10,started_at:a,finished_at:b,host:"n1",worker_id:"n1-w",cpus:4,memory_mb:1024,service:"pool",trace:{workflow_id:"persample/a",dataset_id:"D / Organ",unit_id:"osp.compute",depends_on:[]},trace_source:"explicit",...extra});
src+=`
;(function(){
  const base=()=>({since:${now-604800},until:${now},resources:[{observed_at:${now-95000},host:"n1",cpu_percent:50,memory_used_bytes:1e10,memory_total_bytes:2e11,gpus:[]}],indexing:false,truncated:false,
    tasks:[task("a",${now-604000},${now-603000}),task("b",${now-95000},${now-93000}),
           task("t",${now-94000},${now-93500},{service:"bridge",operation:"agent.turn",state:"reply_saved",model:{model:"m"},trace:{workflow_id:"cross-sample/b",dataset_id:"E",unit_id:"agent.turn",depends_on:["b"]}}),
           task("f",${now-94500},${now-94400},{state:"failed"})],total:4});
  for (const log of [true,false]) for (const v of ["workers","datasets"]) {
    const d=base(); d.tasks.forEach(t=>t.trace.dataset_id=t.trace.dataset_id);
    el("timeline-log").checked=log; el("timeline-view").value=v;
    frameActivity(d,${now-604000}); renderTimeline(d); renderResourceHistory(d); timelineAxis(d.since,d.until);
  }
  renderFailures([{id:"f",at:${now-94400},operation:"osp.compute",dataset:"D / Organ"}]);
  setWindow({since:${now-7200},until:${now-3600}}); resetTimelineView(); frameWorkflow("persample/a"); readHash(); writeHash(); syncControls();
  const busy={...base(),tasks:[task("c",${now-100},null)]}; frameActivity(busy,Infinity);
  if (busy.until!==${now}) throw new Error("a running task must keep the edge at now");
  // zoom / pan must redraw from the loaded set and only ask the server when it cannot answer
  COUNT(true);
  const full=base(); loaded={since:${now}-300000,until:full.until,truncated:false};
  full.framed={since:full.since,until:full.until}; fullTimeline=full; renderWindow();
  const inside={since:${now}-200000,until:${now}-80000};
  setWindow(inside); navigated();
  if (FETCHES()) throw new Error("a window inside the loaded range must not refetch");
  if (timelineData.tasks.length!==3) throw new Error("clip kept "+timelineData.tasks.length+" of the 3 overlapping tasks");
  setWindow({since:${now}-500000,until:${now}-400000}); navigated();
  if (timelineData.until<=timelineData.since) throw new Error("window inverted");
  if (!FETCHES()) throw new Error("a window outside the loaded range must refetch");
  loaded.truncated=true; COUNT(true); setWindow(inside); navigated();
  if (!FETCHES()) throw new Error("a truncated load must refetch when zoomed");
  // double-click returns to the page as it opens
  el("timeline-view").value="datasets"; el("timeline-log").checked=false;
  el("timeline-dataset").value="Organ"; setWindow({since:${now}-7200,until:${now}-3600});
  resetTimelineView();
  if (!(view.live && view.span==="all")) throw new Error("reset must return to the live whole-history window");
  if (el("timeline-view").value!=="workers") throw new Error("reset must return to worker lanes");
  if (el("timeline-log").checked!==true) throw new Error("reset must turn log time back on");
  if (el("timeline-dataset").value!=="") throw new Error("reset must clear the dataset filter");
  console.log("all render paths ok; client-side zoom and double-click reset verified");
})();`;
try { new Function(src)(); } catch (e) { console.log("SMOKE ERROR:", e.stack.split("\n").slice(0,3).join(" | ")); process.exit(1); }

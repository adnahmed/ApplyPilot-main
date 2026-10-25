// Offline presentation contracts; run with: node tools/check_pipeline_ui.mjs [report.json]
import assert from 'node:assert/strict';
import { readFile, writeFile } from 'node:fs/promises';
const source=new URL('../src/applypilot/web/static/runtime-health.js', import.meta.url);
const text=await readFile(source,'utf8');
const { runtimeHealth, stageQueues }=await import('data:text/javascript;base64,'+Buffer.from(text).toString('base64'));
const checks=[];
function check(name,run){run();checks.push({check:name,passed:true});}
check('configured runtime executes without host telemetry',()=>{
 const html=runtimeHealth({current:true,shared:{mode:'configured',telemetry_available:false,host_admission_reason:'host_memory_pressure',sizing_reason:'host_memory_pressure'},active:2,queued:12});
 assert.match(html,/Work is executing/);assert.doesNotMatch(html,/launches are paused|waiting for memory|Browser launches remain paused/);
});
check('configured stage occupancy is local',()=>{
 const html=runtimeHealth({current:true,shared:{mode:'configured',admission_blocking_reason:'lane_capacity_occupied'},active:0,queued:12});
 assert.match(html,/other stages remain independent/);
});
check('stage queue shows producer pause without hiding an independent consumer',()=>{
 const html=stageQueues({score:{state:'buffer_wait',reason:'Qualified buffer full',active_tasks:0,pending:8},network_research:{state:'running',active_tasks:1,pending:2}},{current:true,shared:{mode:'configured'}});
 assert.match(html,/Buffer full/);assert.match(html,/Working/);assert.match(html,/Network research/);
});
check('reports still escape untrusted text and mark stale activity unknown',()=>{
 assert.match(runtimeHealth({error:'<img src=x onerror=alert(1)>'}),/&lt;img/);
 assert.match(runtimeHealth({current:false,active:0,queued:0}),/Current activity is unknown/);
});
check('historical exports retain their original host-pressure explanation',()=>{
 assert.match(runtimeHealth({current:true,shared:{mode:'auto',telemetry_available:true,host_admission_reason:'host_memory_pressure'},active:0,queued:10}),/waiting for memory/);
});
const report={node:process.version,passed:checks.length,failed:0,scope:'DOM-free rendering assertions; not an interactive browser run',checks};
if (process.argv[2]) await writeFile(process.argv[2],JSON.stringify(report,null,2)+'\n');
console.log(JSON.stringify(report,null,2));

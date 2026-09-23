const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../monitor/monitor.js'),'utf8');
const api=source.slice(source.indexOf('async function fetchMonitorData()'),source.indexOf('function getPreferredTheme()'));
const constants=['API_URL','POLL_INTERVAL_MS','REQUEST_TIMEOUT_MS'].map(name=>source.match(new RegExp(`const ${name} = [^;]+;`))[0]).join('\n');
const flush=async()=>{for(let i=0;i<12;i++)await Promise.resolve();};

function harness(){
  let next=1,renders=0;
  const timers=new Map(),requests=[],statuses=[];
  const state={requestSequence:0,appliedSequence:0,requestController:null,pollTimer:null,bridgeConnected:true};
  const document={visibilityState:'visible'};
  const context=vm.createContext({
    AbortController,Date,document,state,console:{warn:()=>{}},
    window:{setTimeout:(callback,delay)=>{const id=next++;timers.set(id,{callback,delay});return id;},clearTimeout:id=>timers.delete(id)},
    normalizePayload:payload=>payload,renderMonitor:()=>renders++,setConnectionState:(...args)=>statuses.push(args),
    fetch:(url,{signal})=>new Promise((resolve,reject)=>{
      requests.push({url,finish:()=>resolve({ok:true,json:async()=>({markets:[],holdings:[]})})});
      signal.addEventListener('abort',()=>{const error=new Error('aborted');error.name='AbortError';reject(error);},{once:true});
    })
  });
  vm.runInContext(constants+api,context);
  const fire=id=>{const timer=timers.get(id);assert.ok(timer);timers.delete(id);timer.callback();};
  return {context,state,document,timers,requests,statuses,fire,get renders(){return renders;}};
}

test('Monitor는 응답 완료 5초 후 다음 요청을 예약하고 진행 중 요청을 중복하지 않는다',async()=>{
  const h=harness();h.context.startPolling();await flush();
  assert.equal(h.requests.length,1);assert.equal(h.state.pollTimer,null);
  await h.context.pollMonitor();assert.equal(h.requests.length,1);
  h.requests[0].finish();await flush();
  assert.equal(h.timers.get(h.state.pollTimer).delay,5000);
  h.fire(h.state.pollTimer);await flush();
  assert.equal(h.requests.length,2);assert.equal(h.state.pollTimer,null);
  await h.context.pollMonitor();assert.equal(h.requests.length,2);
  h.requests[1].finish();await flush();assert.equal(h.renders,2);
});

test('Monitor 숨김은 요청을 취소하고 복귀 시 즉시 조회하며 오류로 표시하지 않는다',async()=>{
  const h=harness();h.context.startPolling();await flush();
  h.document.visibilityState='hidden';h.context.stopPolling();await flush();
  assert.equal(h.state.requestController,null);assert.equal(h.timers.size,0);assert.deepEqual(h.statuses,[]);
  h.context.scheduleNextPoll();await h.context.pollMonitor();assert.equal(h.requests.length,1);
  h.document.visibilityState='visible';h.context.startPolling();await flush();
  assert.equal(h.requests.length,2);h.requests[1].finish();await flush();
  assert.equal(h.renders,1);assert.equal(h.timers.get(h.state.pollTimer).delay,5000);
});

test('Monitor timeout 후에도 중복 없이 다음 주기에서 복구된다',async()=>{
  const h=harness();h.context.startPolling();await flush();
  const timeout=[...h.timers.keys()][0];h.fire(timeout);await flush();
  assert.deepEqual(h.statuses,[[false,'API 지연']]);
  assert.equal(h.state.requestController,null);assert.equal(h.timers.get(h.state.pollTimer).delay,5000);
  h.fire(h.state.pollTimer);await flush();assert.equal(h.requests.length,2);
  h.requests[1].finish();await flush();assert.equal(h.statuses.at(-1)[0],true);assert.equal(h.renders,1);
});

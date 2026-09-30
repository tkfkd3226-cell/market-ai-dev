const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');

const source=fs.readFileSync(path.join(__dirname,'../monitor/monitor.js'),'utf8')
  .replace(/bootstrap\(\);\s*$/,'');

function normalize(snapshot,marketState='closed',bridgeConnected=true){
  const context=vm.createContext({
    document:{documentElement:{},querySelector:()=>null,getElementById:()=>null},
  });
  vm.runInContext(source,context);
  const payload={
    bridge_connected:bridgeConnected,
    market_state:marketState,
    dashboard_display_tickers:['069500'],
    dashboard_names:{'069500':'KODEX 200'},
    dashboard_market_states:{'069500':marketState},
    monitor_snapshots:snapshot?[{symbol:'KRX:069500',price:111335,observed_at:'2026-09-30T06:30:00Z',...snapshot}]:[],
  };
  const {holdings}=context.normalizePayload(payload);
  return {holding:holdings[0],summary:context.summarizeHoldings(holdings)};
}

test('구독 오류는 저장된 장마감/시간외 가격보다 우선하고 오류 합계에 포함된다',()=>{
  for(const marketState of ['closed','extended']){
    for(const subscriptionState of ['error','not_subscribed']){
      const {holding,summary}=normalize({state:'unavailable',subscription_state:subscriptionState,subscription_error:'SC_R subscription failed'},marketState);
      assert.equal(holding.status,'error');
      assert.equal(holding.price,111335);
      assert.equal(summary.error,1);
      assert.equal(summary.closed,0);
      assert.equal(summary.extended,0);
    }
  }
});

test('미구독 대기와 Bridge 단절을 장마감/시간외로 승격하지 않는다',()=>{
  assert.equal(normalize({state:'unavailable',subscription_state:'not_subscribed'},'closed').holding.status,'warming');
  assert.equal(normalize({state:'unavailable',subscription_state:'requested'},'extended').holding.status,'warming');
  assert.equal(normalize({state:'unavailable',subscription_state:'unknown'},'closed',false).holding.status,'stale');
  assert.equal(normalize(null,'extended').holding.status,'warming');
});

test('구독 복구 후 backend의 정상/장마감/지연 상태를 유지한다',()=>{
  for(const [state,session,expected] of [
    ['live','open','live'],['live','extended','extended'],
    ['closed','closed','closed'],['stale','extended','stale'],
    ['warming','open','warming'],['error','closed','error'],
  ]){
    assert.equal(normalize({state,subscription_state:'subscribed'},session).holding.status,expected);
  }
});

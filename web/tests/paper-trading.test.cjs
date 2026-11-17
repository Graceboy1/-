"use strict";

// Real client controller and JSX, disposable response fixtures only. No model,
// broker, credential file, browser, or running production service is touched.
const test=require("node:test");
const assert=require("node:assert/strict");
const fs=require("node:fs");
const path=require("node:path");
const vm=require("node:vm");
const ts=require("typescript");
const React=require("react");
const {renderToStaticMarkup}=require("react-dom/server");
const flush=()=>new Promise(resolve=>setImmediate(resolve));
const clone=value=>structuredClone(value);
const DAY="2026-11-13";
const SEATS=["baseline","livermore","buffett"];

function fixture(overrides={}) {
  const accounts=SEATS.map((seat_id,index)=>({seat_id,label:seat_id,initial_cash:100000,cash:index?100000:84995,market_value:index?0:15300,equity:index?100000:100295,daily_pnl:index?0:295,daily_return_pct:index?0:.295,total_pnl:index?0:295,total_return_pct:index?0:.295,realized_pnl:index?0:-5,unrealized_pnl:index?0:300,buy_count:index?0:1,sell_count:0,position_count:index?0:1,status:"completed",missing:index?["缺少可执行的竞价或财报证据"]:[]}));
  return {mode:"historical",trading_date:DAY,calendar_date:"2026-11-16",simulation:true,
    engine:{status:"idle",message:"可自主扫描全部已下载主板股票",progress:{processed:0,total:3049}},accounts,
    orders:{items:[{id:"1",seat_id:"baseline",code:"600519",name:"测试甲公司",side:"buy",timestamp:`${DAY}T09:30:00+08:00`,date:DAY,price:10,quantity:1500,amount:15000,fees:5,reason:"冻结条件通过，按历史开盘价模拟",execution_mode:"historical_daily_assumed",source_hash:"a".repeat(64)}],total:1,limit:20,offset:0},
    positions:[{seat_id:"baseline",code:"600519",name:"测试甲公司",quantity:1500,available_quantity:0,entry_date:DAY,cost_price:10,mark_price:10.2,market_value:15300,unrealized_pnl:300,return_pct:2,updated_at:`${DAY}T15:00:00+08:00`}],
    decisions:{items:[{seat_id:"buffett",code:"600519",name:"测试甲公司",action:"hold",status:"blocked",reason:"年度原始版本未核验，不补造财报",missing:["股数与人工生意确认证据缺失"]}],total:3049},notice:["历史成交为模拟假设，不是真实交易。"],...overrides};
}
function integration(overrides={}) {
  return {version:"buffett-data-integration/1",scope:"whole_universe",decision_date:"2026-11-12",status:"partial_verified",
    reference_stocks:3049,reference_year_rows:9147,reference_complete_stocks:3015,
    verified_stocks:6,verified_report_count:18,verified_field_count:72,strict_pit_stocks:0,manual_confirmation_available:false,
    remaining:["maintenance_capex","历史更正版本核验","能力圈与护城河人工确认"],limitations:["原文事实不自动放行交易"],
    samples:[{code:"600519",name:"测试甲公司",report_count:3,field_count:12,pit_verified:false,missing_fields:["maintenance_capex","shares_outstanding"]}],...overrides};
}
function livermoreIntegration(overrides={}) {
  return {version:"livermore-data-integration/1",scope:"whole_universe",decision_date:"2026-11-12",execution_date:"2026-11-13",status:"partial_connected",
    universe_stocks:3049,history_120_stocks:3000,low_position_stocks:418,source_limit_up_event_stocks:12,
    verified_limit_stocks:2,verified_security_status_stocks:1,verified_stock_auction_stocks:0,verified_index_auction_stocks:0,
    regional_confirmation_stocks:0,candidate_stocks:4,execution_ready_stocks:0,
    remaining:["security_status","stock_auction","index_auction","regional_confirmation","future_custom_key","constructor"],limitations:["源标注不是精确限价核验，不回填今日行情"],
    samples:[{code:"600519",name:"价量测试甲公司",position_120_pct:23.4,limit_up_confirmed:null,limit_price_verified:false,auction_verified:false,missing_fields:["limit_prices","stock_auction"]}],...overrides};
}
function walk(node,predicate) {
  if(!node || typeof node!=="object")return null;
  if(predicate(node))return node;
  for(const child of React.Children.toArray(node.props?.children)){const result=walk(child,predicate);if(result)return result;}
  return null;
}
function plain(node) {
  if(node==null || typeof node==="boolean")return "";
  if(typeof node!=="object")return String(node);
  if(typeof node.type==="function")return plain(node.type(node.props));
  return React.Children.toArray(node.props?.children).map(plain).join("");
}
function harness({initial=fixture(),view="research"}={}) {
  const states=[],refs=[],effects=[],calls=[],replies=[],events=[],timers=new Map(),listeners=new Map();
  let si=0,ri=0,ei=0,tid=0,tree;
  if(initial!==null)replies.push(typeof initial?.then==="function" ? initial : {data:initial});
  const react={...React,
    useState(initial){const i=si++;if(!(i in states))states[i]=typeof initial==="function"?initial():initial;return[states[i],value=>states[i]=typeof value==="function"?value(states[i]):value];},
    useRef(initial){const i=ri++;if(!(i in refs))refs[i]={current:initial};return refs[i];},
    useEffect(callback,deps){const i=ei++,old=effects[i];if(!old || deps.some((value,index)=>value!==old.deps[index]))effects[i]={deps,callback,cleanup:old?.cleanup,pending:true};},
  };
  const document={hidden:false,addEventListener(name,callback){if(!listeners.has(name))listeners.set(name,new Set());listeners.get(name).add(callback);},removeEventListener(name,callback){listeners.get(name)?.delete(callback);}};
  const filename=path.join(__dirname,"../app/paper-trading-desk.tsx");
  const code=ts.transpileModule(fs.readFileSync(filename,"utf8"),{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX},fileName:filename}).outputText;
  const exports={};
  vm.runInNewContext(code,{exports,module:{exports},URLSearchParams,AbortController,Event,console,Date,document,
    require(name){if(name==="react")return react;if(name==="./paper-replay")return{default:()=>null};if(name.endsWith(".css"))return{};return require(name);},
    window:{setTimeout(callback,delay){timers.set(++tid,{callback,delay});return tid;},clearTimeout(id){timers.delete(id);},dispatchEvent(event){events.push(event.type);}},
    fetch:async(url,options)=>{calls.push({url,...options,submitted:options.body?JSON.parse(options.body):null});assert.ok(replies.length,`Unexpected request ${url}`);const result=await replies.shift();return{status:result.status??200,ok:(result.status??200)<400,json:async()=>result.data};},
  },{filename});
  function render(){si=0;ri=0;ei=0;tree=React.createElement(exports.default,{view});tree=tree.type(tree.props);effects.forEach(effect=>{if(effect.pending){effect.pending=false;effect.cleanup?.();effect.cleanup=effect.callback();}});return tree;}
  render();
  return {states,calls,replies,events,timers,listeners,render,
    reply(data,status=200){replies.push({data,status});},
    deferred(){let finish;replies.push(new Promise(resolve=>finish=resolve));return finish;},
    async settle(){await flush();render();await flush();render();},
    button(label){const button=walk(tree,node=>node.type==="button"&&plain(node)===label);assert.ok(button,`Button ${label} missing`);return button;},
    click(label){const button=this.button(label);if(!button.props.disabled)button.props.onClick();render();},
    select(label,value){const input=walk(tree,node=>node.props?.["aria-label"]===label&&["select","input"].includes(node.type));assert.ok(input);input.props.onChange({target:{value}});render();},
    async tick(){const entry=timers.entries().next().value;assert.ok(entry,"Missing scheduled refresh");timers.delete(entry[0]);entry[1].callback();await this.settle();},
    async hidden(value){document.hidden=value;listeners.get("visibilitychange")?.forEach(callback=>callback());await this.settle();},
    changeView(next){view=next;render();},
    unmount(){effects.forEach(effect=>effect.cleanup?.());},
    get tree(){return tree;},get text(){return plain(tree);},get markup(){return renderToStaticMarkup(tree);},
  };
}

test("paper desk loads a bounded owner dashboard without starting trading or selecting one stock",async()=>{
  const h=harness();await h.settle();
  assert.equal(h.calls.length,1);assert.equal(h.calls[0].method,"GET");
  assert.equal(h.calls[0].url,"/contest-api/paper/dashboard?mode=historical&seat_id=all&limit=20&offset=0");
  assert.equal(h.calls[0].credentials,"same-origin");assert.equal(h.calls[0].redirect,"error");assert.equal(h.calls[0].cache,"no-store");
  assert.match(h.text,/自主模拟交易/);assert.match(h.text,/基础策略席位/);assert.match(h.text,/利弗莫尔席位/);assert.match(h.text,/巴菲特席位/);
  assert.match(h.text,/历史模拟交易日2026-11-13|历史模拟交易日　2026-11-13/);assert.match(h.text,/实际日历日期.*2026-11-16/);
  assert.equal(h.timers.size,1);h.unmount();
});

test("Buffett integration appears in main research and audit, not only the single-stock tool, without any write",async()=>{
  const h=harness({initial:fixture({buffett_data:integration()})});await h.settle();
  assert.match(h.text,/巴菲特全市场数据接入/);assert.match(h.text,/公开参考覆盖3,049 只/);
  assert.match(h.text,/9,147 条年度记录/);assert.match(h.text,/18 个年度 · 72 项字段/);
  assert.match(h.text,/严格时点核验0 只/);assert.match(h.text,/尚无人工确认，不会自动勾选/);
  assert.match(h.text,/维持性资本开支/);assert.doesNotMatch(h.text,/maintenance_capex|shares_outstanding/);
  h.changeView("audit");assert.match(h.text,/巴菲特全市场数据接入/);assert.match(h.text,/自主决策审计/);
  assert.doesNotMatch(h.text,/虚拟总资产|启用自动模拟|暂停运行/);
  h.changeView("replay");assert.doesNotMatch(h.text,/巴菲特全市场数据接入/);
  assert.equal(h.calls.length,1);assert.ok(h.calls.every(call=>call.method==="GET"));h.unmount();
});

test("legacy dashboard stays compatible without manufacturing Buffett integration counts",async()=>{
  const h=harness();await h.settle();assert.doesNotMatch(h.text,/巴菲特全市场数据接入/);assert.match(h.text,/100,295.00 元/);h.unmount();
});

test("Livermore whole-market integration is read-only in research and audit and absent from replay",async()=>{
  const h=harness({initial:fixture({buffett_data:integration(),livermore_data:livermoreIntegration()})});await h.settle();
  assert.match(h.text,/利弗莫尔全市场数据接入/);assert.match(h.text,/巴菲特全市场数据接入/);
  assert.match(h.text,/决策日 2026-11-12 · 执行日 2026-11-13/);
  assert.match(h.text,/120日日线覆盖3,000 只/);assert.match(h.text,/股票范围 3,049 只/);
  assert.match(h.text,/低位位置门槛418 只/);assert.match(h.text,/源标注涨停参考12 只/);
  assert.match(h.text,/非已核验精确限价/);assert.match(h.text,/个股真实竞价已接入0 只/);
  assert.match(h.text,/精确限价已核验2 只/);assert.match(h.text,/历史证券状态已核验1 只/);
  assert.match(h.text,/结构候选4 只/);assert.match(h.text,/执行证据齐备0 只/);
  assert.match(h.text,/尚未核验/);assert.match(h.text,/不得以涨幅达到10%代替封板确认/);
  assert.match(h.text,/其他待核对项目/);assert.doesNotMatch(h.text,/security_status|stock_auction|index_auction|regional_confirmation|future_custom_key|limit_prices|constructor/);
  h.changeView("audit");assert.match(h.text,/利弗莫尔全市场数据接入/);assert.match(h.text,/自主决策审计/);
  assert.doesNotMatch(h.text,/虚拟总资产|启用自动模拟|暂停运行/);
  h.changeView("replay");assert.doesNotMatch(h.text,/利弗莫尔全市场数据接入|巴菲特全市场数据接入/);
  assert.equal(h.calls.length,1);assert.ok(h.calls.every(call=>call.method==="GET"));h.unmount();
});

test("legacy dashboards do not invent Livermore data summaries",async()=>{
  const h=harness();await h.settle();assert.doesNotMatch(h.text,/利弗莫尔全市场数据接入/);assert.match(h.text,/测试甲公司600519/);h.unmount();
});

test("invalid Livermore summary fields, impossible dates, future dates and oversized payloads do not hide the ledger",async()=>{
  const sample=livermoreIntegration().samples[0];
  for(const value of [livermoreIntegration({scope:"one_stock"}),livermoreIntegration({history_120_stocks:6001}),livermoreIntegration({low_position_stocks:-1}),livermoreIntegration({candidate_stocks:NaN}),livermoreIntegration({execution_ready_stocks:undefined}),livermoreIntegration({decision_date:"2026-02-30"}),livermoreIntegration({decision_date:"2026-11-13"}),livermoreIntegration({execution_date:"2026-11-17"}),livermoreIntegration({samples:Array.from({length:21},()=>sample)}),livermoreIntegration({samples:[{...sample,position_120_pct:Infinity}]}),livermoreIntegration({samples:[{...sample,limit_up_confirmed:"unknown"}]})]){
    const h=harness({initial:fixture({livermore_data:value})});await h.settle();
    assert.match(h.text,/利弗莫尔数据接入摘要的字段或日期校验未通过/);assert.doesNotMatch(h.text,/利弗莫尔全市场数据接入/);
    assert.match(h.text,/测试甲公司600519/);assert.match(h.text,/100,295.00 元/);
    assert.equal(h.calls.length,1);assert.equal(h.calls[0].method,"GET");h.unmount();
  }
});

test("independent summary failures preserve both warnings and the existing ledger",async()=>{
  const h=harness({initial:fixture({buffett_data:integration({scope:"bad"}),livermore_data:livermoreIntegration({scope:"bad"})})});await h.settle();
  assert.match(h.text,/巴菲特数据接入摘要校验未通过/);assert.match(h.text,/利弗莫尔数据接入摘要的字段或日期校验未通过/);
  assert.doesNotMatch(h.text,/巴菲特全市场数据接入|利弗莫尔全市场数据接入/);assert.match(h.text,/测试甲公司600519/);h.unmount();
});

test("Livermore sample rows remain bounded, escaped and independent from whole-market totals",async()=>{
  const sample=livermoreIntegration().samples[0];
  const h=harness({initial:fixture({livermore_data:livermoreIntegration({remaining:['<img src=x onerror="bad">'],samples:Array.from({length:20},(_,index)=>({...sample,code:`600${String(index).padStart(3,"0")}`,name:index===0?'<script>bad()</script>':`价量样本${index}`,limit_up_confirmed:index===1?true:index===2?false:null}))})})});await h.settle();
  assert.doesNotMatch(h.markup,/<img|<script/);assert.match(h.markup,/&lt;script/);
  assert.match(h.text,/价量样本9/);assert.doesNotMatch(h.text,/价量样本10/);
  assert.match(h.text,/已核对封板/);assert.match(h.text,/未封涨停/);assert.match(h.text,/源标注涨停参考12 只/);
  assert.equal(h.calls.length,1);h.unmount();
});

test("integration summary rejects malformed scopes, negative counts and oversized samples without losing the ledger",async()=>{
  for(const value of [integration({scope:"one_stock"}),integration({verified_stocks:-1}),integration({strict_pit_stocks:7}),integration({samples:Array.from({length:21},()=>integration().samples[0])}),integration({verified_report_count:undefined})]){
    const h=harness({initial:fixture({buffett_data:value})});await h.settle();
    assert.match(h.text,/数据接入摘要校验未通过/);assert.doesNotMatch(h.text,/巴菲特全市场数据接入/);
    assert.match(h.text,/测试甲公司600519/);assert.equal(h.calls.length,1);h.unmount();
  }
});

test("integration notes and sample identities are escaped and only bounded samples are rendered",async()=>{
  const sample=integration().samples[0];const h=harness({initial:fixture({buffett_data:integration({remaining:['<img src=x onerror="bad">'],samples:Array.from({length:20},(_,index)=>({...sample,code:`600${String(index).padStart(3,"0")}`,name:index===0?'<script>bad()</script>':`核对样本${index}`}))})})});await h.settle();
  assert.doesNotMatch(h.markup,/<img|<script/);assert.match(h.markup,/&lt;img/);assert.match(h.markup,/&lt;script/);
  assert.match(h.text,/核对样本9/);assert.doesNotMatch(h.text,/核对样本10/);h.unmount();
});

test("account P&L and trade/position identity are server data, including T+1 availability",async()=>{
  const h=harness();await h.settle();
  assert.match(h.text,/100,295.00 元/);assert.match(h.text,/\+295.00 元/);assert.match(h.text,/已实现损益-5.00 元/);assert.match(h.text,/持仓浮盈亏\+300.00 元/);
  assert.match(h.text,/测试甲公司600519/);assert.match(h.text,/模拟买入/);assert.match(h.text,/15,000.00 元/);assert.match(h.text,/1,500 \/ 0 股/);
  assert.match(h.text,/年度原始版本未核验/);assert.match(h.text,/股数与人工生意确认证据缺失/);h.unmount();
});

test("one explicit enable starts the whole scanner and never submits a stock or model request",async()=>{
  const h=harness();await h.settle();h.reply({accepted:true});h.reply(fixture({engine:{status:"running",message:"正在扫描",progress:{processed:25,total:3049}}}));
  h.click("启用自动模拟");await h.settle();
  assert.deepEqual(h.calls.map(call=>call.method),["GET","POST","GET"]);
  assert.equal(h.calls[1].url,"/contest-api/paper/start");assert.deepEqual(h.calls[1].submitted,{mode:"historical"});
  assert.equal(h.calls[1].headers["Content-Type"],"application/json");assert.match(h.text,/25 \/ 3,049/);
  assert.deepEqual([...h.timers.values()].map(timer=>timer.delay),[2000]);h.unmount();
});

test("a repeated enable click during a pending request does not duplicate startup",async()=>{
  const h=harness();await h.settle();const finish=h.deferred();h.click("启用自动模拟");h.click("正在启用…");
  assert.equal(h.calls.filter(call=>call.method==="POST").length,1);h.reply(fixture({engine:{status:"running"}}));finish({data:{accepted:true}});await h.settle();h.unmount();
});

test("completed historical sessions cannot be restarted or silently reset by the page",async()=>{
  const h=harness({initial:fixture({engine:{status:"completed"}})});await h.settle();
  assert.equal(h.button("本轮已运行，账本已保存").props.disabled,true);h.click("本轮已运行，账本已保存");assert.equal(h.calls.length,1);h.unmount();
});

test("pause is an explicit idempotent mode command, including a blocked worker",async()=>{
  const h=harness({initial:fixture({engine:{status:"blocked",message:"行情数据待更新"}})});await h.settle();
  h.reply({accepted:true});h.reply(fixture({engine:{status:"paused"}}));h.click("暂停运行");await h.settle();
  assert.equal(h.calls[1].url,"/contest-api/paper/stop");assert.deepEqual(h.calls[1].submitted,{mode:"historical"});
  assert.equal(h.calls.filter(call=>call.method==="POST").length,1);assert.match(h.text,/已暂停/);h.unmount();
});

test("a failed enable is not silently retried and leaves existing ledger intact",async()=>{
  const h=harness();await h.settle();h.reply({detail:"模拟任务尚未启动"},503);h.click("启用自动模拟");await h.settle();
  assert.equal(h.calls.filter(call=>call.method==="POST").length,1);assert.match(h.text,/模拟任务尚未启动/);assert.match(h.text,/测试甲公司600519/);h.unmount();
});

test("live mode does not relabel historical orders as today's trades or backfill them",async()=>{
  const h=harness();await h.settle();h.reply(fixture({mode:"live",trading_date:"2026-11-16",engine:{status:"waiting",message:"休市等待；启用前没有订单"},orders:{items:[],total:0},positions:[],decisions:{items:[],total:0}}));
  h.click("实时自动模拟");await h.settle();
  assert.equal(h.calls[1].method,"GET");assert.match(h.calls[1].url,/mode=live/);assert.match(h.text,/账本交易日　2026-11-16/);
  assert.doesNotMatch(h.text,/历史模拟交易日　2026|模拟交易日账本/);assert.match(h.text,/不追补过去成交/);assert.match(h.text,/暂无模拟成交/);h.unmount();
});

test("mode changes cancel an old dashboard response rather than painting it under a new mode",async()=>{
  let finish;const initial=new Promise(resolve=>finish=resolve);const h=harness({initial});
  h.reply(fixture({mode:"live",trading_date:"2026-11-16",orders:{items:[],total:0}}));
  h.click("实时自动模拟");await h.settle();finish({data:fixture()});await h.settle();
  assert.equal(h.states[1]?.mode,"live");assert.match(h.text,/账本交易日　2026-11-16/);h.unmount();
});

test("failed polling retains the last saved orders and explains the error without inventing success",async()=>{
  const h=harness();await h.settle();h.reply({detail:"缓存暂不可读"},503);await h.tick();
  assert.match(h.text,/缓存暂不可读/);assert.match(h.text,/测试甲公司600519/);assert.match(h.text,/不会转换成示例交易/);h.unmount();
});

test("malformed or wrong-date/foreign-market data is rejected without replacing the current ledger",async()=>{
  for(const override of [
    {simulation:false},{mode:"live"},{orders:{items:[{...fixture().orders.items[0],code:"688001"}],total:1}},
    {orders:{items:[{...fixture().orders.items[0],date:"2026-11-16"}],total:1}},
    {orders:{items:Array.from({length:21},(_,id)=>({...fixture().orders.items[0],id})),total:21}},
  ]){
    const h=harness();await h.settle();h.reply(fixture(override));await h.tick();
    assert.match(h.text,/日期或股票范围校验未通过/);assert.equal(h.states[1].mode,"historical");assert.equal(h.states[1].orders.items.length,1);h.unmount();
  }
});

test("server text is escaped in ledger reasons, names and notices",async()=>{
  const value=fixture();value.orders.items[0].name='<img src=x onerror="bad">';value.orders.items[0].reason='<script>bad()</script>';value.notice=['<svg onload="bad">'];
  const h=harness({initial:value});await h.settle();assert.doesNotMatch(h.markup,/<img|<script|<svg/);assert.match(h.markup,/&lt;img/);assert.match(h.markup,/&lt;script/);h.unmount();
});

test("seat and order pagination stay bounded and never restart simulation",async()=>{
  const h=harness({initial:fixture({orders:{...fixture().orders,total:45}})});await h.settle();
  h.reply(fixture({orders:{items:[],total:45,offset:20}}));h.click("下一页");await h.settle();assert.match(h.calls[1].url,/offset=20/);
  h.reply(fixture({orders:{items:[],total:0}}));h.select("账本席位","buffett");await h.settle();assert.match(h.calls[2].url,/seat_id=buffett/);assert.match(h.calls[2].url,/offset=0/);
  assert.ok(h.calls.every(call=>call.method==="GET"));h.unmount();
});

test("switching workbench views preserves selected mode and performs no extra startup",async()=>{
  const h=harness();await h.settle();h.reply(fixture({mode:"live",trading_date:"2026-11-16",orders:{items:[],total:0}}));h.click("实时自动模拟");await h.settle();
  const before=h.calls.length;h.changeView("replay");assert.match(h.text,/交易账本与持仓/);h.changeView("audit");assert.match(h.text,/自主决策审计/);
  assert.equal(h.calls.length,before);assert.equal(h.states[0].mode,"live");assert.doesNotMatch(h.text,/模拟交易日账本/);h.unmount();
});

test("clicking an already selected mode preserves ready data rather than clearing it",async()=>{
  const h=harness();await h.settle();const prior=h.states[1];h.click("历史数据演示");await h.settle();assert.equal(h.states[1],prior);assert.equal(h.calls.length,1);h.unmount();
});

test("hidden pages pause polling and visible pages check the saved ledger once",async()=>{
  const h=harness();await h.settle();await h.hidden(true);assert.equal(h.timers.size,0);
  const before=h.calls.length;h.reply(fixture());await h.hidden(false);assert.equal(h.calls.length,before+1);assert.equal(h.timers.size,1);h.unmount();
  assert.equal(h.timers.size,0);assert.equal(h.listeners.get("visibilitychange").size,0);
});

test("unmount aborts a pending dashboard and cannot adopt its later response",async()=>{
  let finish;const pending=new Promise(resolve=>finish=resolve);
  const h=harness({initial:pending});h.unmount();assert.ok(h.calls.at(-1).signal.aborted);
  finish({data:fixture()});await h.settle();assert.equal(h.timers.size,0);assert.equal(h.states[1],null);
});

test("unauthenticated responses notify the account gate and do not call another endpoint",async()=>{
  const h=harness();await h.settle();h.reply({detail:"会话过期"},401);await h.tick();assert.deepEqual(h.events,["contest-session-expired"]);assert.match(h.text,/会话过期/);assert.ok(h.calls.every(call=>call.url.includes("/paper/dashboard?")));h.unmount();
});

test("decision status codes are Chinese, including holding limits and unknown future codes",async()=>{
  const codes={position_cap:"已达持仓上限",already_held:"已有持仓，不重复买入",cash_rejected:"可用资金不足",price_rejected:"价格不符，未成交",data_rejected:"数据校验未通过",duplicate:"重复请求，已忽略",not_running:"模拟未运行",invalid_data:"数据无效",future_internal_code:"其他状态"};
  const decisions=Object.keys(codes).map(status=>({...fixture().decisions.items[0],action:status,status}));
  const h=harness({initial:fixture({decisions:{items:decisions,total:decisions.length}}),view:"audit"});await h.settle();
  for(const [code,label]of Object.entries(codes)){assert.match(h.text,new RegExp(label));assert.ok(!h.text.includes(code),`Raw code leaked: ${code}`);}h.unmount();
});

test("every paginated owned ledger order can open daily replay beyond the first shortcut page",async()=>{
  const entry={...fixture().orders.items[0],id:101},h=harness({initial:fixture({orders:{items:[entry],total:101,limit:20,offset:100}}),view:"replay"});await h.settle();
  const button=walk(h.tree,node=>node.type==="button"&&node.props["aria-label"]==="回放测试甲公司订单101");assert.ok(button);button.props.onClick();h.render();
  const replay=walk(h.tree,node=>typeof node.type==="function"&&node.props.date===DAY&&node.props.seat==="all");assert.ok(replay);assert.equal(replay.props.orderId,101);assert.equal(h.calls.length,1);assert.ok(h.calls.every(call=>call.method==="GET"));h.unmount();
});

test("research alone exposes the account dashboard and trading controls",async()=>{
  const h=harness();await h.settle();
  assert.match(h.markup,/class="paper-accounts"/);
  assert.ok(h.button("启用自动模拟"));assert.ok(h.button("暂停运行"));
  assert.ok(walk(h.tree,node=>node.props?.className==="paper-engine-line"));
  assert.equal(walk(h.tree,node=>typeof node.type==="function"&&node.props.date===DAY&&node.props.seat==="all"),null);
  h.unmount();
});

test("replay opens its own read-only playback and ledger rather than the account dashboard",async()=>{
  const h=harness({view:"replay"});await h.settle();
  assert.match(h.text,/交易账本与持仓/);
  assert.doesNotMatch(h.markup,/class="paper-accounts"/);
  assert.equal(walk(h.tree,node=>node.props?.className==="paper-engine-line"),null);
  assert.equal(walk(h.tree,node=>node.type==="button"&&/启用自动模拟|继续自主运行|暂停运行|正在启用|正在暂停/.test(plain(node))),null);
  assert.ok(walk(h.tree,node=>typeof node.type==="function"&&node.props.date===DAY&&node.props.seat==="all"));
  assert.ok(walk(h.tree,node=>node.props?.id==="paper-ledger-title"));
  assert.ok(walk(h.tree,node=>node.props?.id==="paper-position-title"));
  assert.equal(walk(h.tree,node=>node.props?.id==="paper-decision-title"),null);
  assert.ok(h.calls.every(call=>call.method==="GET"));h.unmount();
});

test("audit opens decision reasons and rules without accounts, trade ledger or replay",async()=>{
  const h=harness({view:"audit"});await h.settle();
  assert.match(h.text,/自主决策审计/);assert.match(h.text,/年度原始版本未核验/);
  assert.doesNotMatch(h.markup,/class="paper-accounts"/);
  assert.equal(walk(h.tree,node=>node.props?.className==="paper-engine-line"),null);
  assert.equal(walk(h.tree,node=>node.type==="button"&&/启用自动模拟|继续自主运行|暂停运行|正在启用|正在暂停/.test(plain(node))),null);
  assert.equal(walk(h.tree,node=>typeof node.type==="function"&&node.props.date===DAY&&node.props.seat==="all"),null);
  assert.equal(walk(h.tree,node=>node.props?.id==="paper-ledger-title"),null);
  assert.equal(walk(h.tree,node=>node.props?.id==="paper-position-title"),null);
  assert.ok(walk(h.tree,node=>node.props?.id==="paper-decision-title"));
  const rules=walk(h.tree,node=>node.type==="details"&&node.props?.className==="paper-method");
  assert.equal(rules?.props.open,true);assert.ok(h.calls.every(call=>call.method==="GET"));h.unmount();
});

test("all workbench views retain read-only filters and preserve the selected book without submitting trades",async()=>{
  const h=harness();await h.settle();
  h.reply(fixture({mode:"live",trading_date:"2026-11-16",orders:{items:[],total:0},positions:[]}));
  h.click("实时自动模拟");await h.settle();
  h.reply(fixture({mode:"live",trading_date:"2026-11-16",accounts:[fixture().accounts[2]],orders:{items:[],total:0},positions:[]}));
  h.select("账本席位","buffett");await h.settle();
  h.reply(fixture({mode:"live",trading_date:"2026-11-13",accounts:[fixture().accounts[2]],orders:{items:[],total:0},positions:[]}));
  h.select("账本日期","2026-11-13");await h.settle();
  const prior={...h.states[0]},before=h.calls.length;
  const headlines=[];
  for(const view of ["replay","audit","research"]){
    h.changeView(view);
    assert.deepEqual({...h.states[0]},prior);assert.equal(h.calls.length,before);
    const mode=walk(h.tree,node=>node.props?.["aria-label"]==="模拟运行模式");assert.ok(mode);
    const seat=walk(h.tree,node=>node.props?.["aria-label"]==="账本席位");assert.equal(seat?.props.value,"buffett");
    const date=walk(h.tree,node=>node.props?.["aria-label"]==="账本日期");assert.equal(date?.props.value,"2026-11-13");
    assert.ok(walk(h.tree,node=>node.type==="button"&&/刷新/.test(plain(node))));
    headlines.push(plain(walk(h.tree,node=>node.type==="header"&&node.props?.className==="paper-title")));
  }
  assert.equal(new Set(headlines).size,3);assert.ok(h.calls.every(call=>call.method==="GET"));h.unmount();
});

test("audit overview uses saved cumulative counts, not its bounded reason sample",async()=>{
  const value=fixture();
  value.accounts[0].counts={buy:2,hold:100,position_cap:17};
  value.accounts[1].counts={hold:200};
  value.accounts[2].counts={hold:300,data_rejected:5};
  value.decisions.total=1;
  const h=harness({initial:value,view:"audit"});await h.settle();
  assert.match(h.markup,/aria-label="自主决策审计概览"/);
  assert.match(h.markup,/<dt>累计判断次数<\/dt><dd>624<\/dd>/);
  assert.match(h.markup,/<dt>观望次数<\/dt><dd>600<\/dd>/);
  assert.match(h.markup,/<dt>模拟买入记录<\/dt><dd>2<\/dd>/);
  assert.match(h.text,/已达持仓上限\s*17/);assert.match(h.text,/原因明细为有界样本/);
  h.reply(clone(value));h.select("账本席位","buffett");await h.settle();
  assert.match(h.markup,/<dt>累计判断次数<\/dt><dd>305<\/dd>/);
  assert.match(h.markup,/<dt>观望次数<\/dt><dd>300<\/dd>/);
  assert.doesNotMatch(h.markup,/已达持仓上限/);assert.ok(h.calls.every(call=>call.method==="GET"));h.unmount();
});

test("missing audit counts stay unknown rather than borrowing the visible sample total",async()=>{
  const h=harness({view:"audit"});await h.settle();
  assert.match(h.markup,/<dt>累计判断次数<\/dt><dd>—<\/dd>/);
  assert.match(h.text,/缺少可核对的累计计数，未用样本数量代替/);
  assert.ok(h.calls.every(call=>call.method==="GET"));h.unmount();
});

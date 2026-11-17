"use strict";

// Execute the real React event controllers with in-memory HTTP responses.
// No credentials, model, account file, browser or production service is used.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const ts = require("typescript");
const React = require("react");

const RECORD = "a".repeat(32), NEXT_RECORD = "b".repeat(32), CUTOFF = "2026-10-23";
const SEATS = [
  {id:"livermore",label:"利弗莫尔席位",description:"独立规则",modes:["offline"]},
  {id:"buffett",label:"巴菲特席位",description:"独立价值规则",modes:["offline"]},
  {id:"baseline",label:"基础策略席位",description:"基础规则",modes:["offline","ai"]},
];
const CASES = SEATS.map(seat => ({id:`${seat.id}-case`,seat_id:seat.id,label:`${seat.label}情景`,name:"模拟名称",symbol:"SYNTHETIC",decision_date:CUTOFF,description:"时点证据"}));
const CONTEXT = {case_id:"livermore-case",seat_id:"livermore",version:"fixture/v2",data_mode:"synthetic",news:{id:"g14",title:"深圳储能配套规划",summary:"虚构情景，不代表真实订单。",source:"本地模拟",time:"11:20"},stock:{id:"s1",label:"电网样本甲"},sectors:[{id:"grid",label:"电网设备"}]};
const SELECTION = {newsId:"g14",stockId:"s1",seatId:"livermore",sectorId:"grid"};
const EMPTY_RESEARCH = {recordId:null,context:null,archived:false,historyOpen:false,historyVersion:0,loading:false,error:""};
const clone = value => JSON.parse(JSON.stringify(value));
const flush = () => new Promise(resolve => setImmediate(resolve));

function makeRun(overrides = {}) {
  return {
    run_id:"1".repeat(32),research_id:RECORD,mode:"offline",model:"离线规则",engine_version:"test/v1",seat:SEATS[0],
    evidence:{case_id:"livermore-case",symbol:"SYNTHETIC",decision_date:CUTOFF,evidence_ids:["bar-1"],bars:[{date:CUTOFF,open:10,high:10.5,low:9.8,close:10.3,volume:1200}],metrics:{data_valid:true}},
    plan:{action:"simulate",rationale:"冻结的独立席位规则",evidence_ids:["bar-1"],entry_low:10,entry_high:11,position_pct:5,stop_loss:null,take_profit:null},
    validation:{ok:true,checks:[{code:"history",label:"时点",passed:true,detail:"只使用决策日前证据"}]},trace:[{step:"freeze",title:"冻结",detail:"已保存"}],frozen_at:"2026-10-23T07:00:00Z",decision_hash:"f".repeat(64),...overrides,
  };
}
function makeReplay() {
  return {seat_id:"livermore",status:"completed",reason:"保存的回放",events:[{date:"2026-10-26",type:"buy",price:10.4,detail:"按冻结规则"}],metrics:{return_pct:1.5},replay_bars:[{date:"2026-10-26",open:10.4,high:10.8,low:10.2,close:10.7,volume:1300}]};
}

function walk(node, predicate) {
  if (!node || typeof node !== "object") return null;
  if (predicate(node)) return node;
  for (const child of React.Children.toArray(node.props?.children)) {
    const found = walk(child,predicate);
    if (found) return found;
  }
  return null;
}
function text(node) {
  if (node == null || typeof node === "boolean") return "";
  if (typeof node !== "object") return String(node);
  return React.Children.toArray(node.props?.children).map(text).join("");
}
function compileComponent(name, mockReact, stubs, globals = {}) {
  const filename = path.join(__dirname,"../app",`${name}.tsx`);
  const code = ts.transpileModule(fs.readFileSync(filename,"utf8"),{
    compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX,strict:true},fileName:filename,
  }).outputText;
  const exports = {};
  vm.runInNewContext(code,{
    exports,module:{exports},AbortController,URL,URLSearchParams,Event,console,
    require(name) {
      if (name === "react") return mockReact;
      if (stubs[name]) return stubs[name];
      return require(name);
    },...globals,
  },{filename});
  return exports.default;
}

function deskHarness({view="news",run=null,replay=null,research={},seatId="livermore",caseId,initialCatalog} = {}) {
  const calls = [], replies = [], refs = [], focus = [], events = [], effects = [];
  const states = [view,initialCatalog ?? {seats:clone(SEATS),cases:clone(CASES),ai_available:false},"",0,seatId,caseId ?? `${seatId}-case`,"offline",run,replay,null,null,{...EMPTY_RESEARCH,...research}];
  let stateIndex = 0, refIndex = 0, tree;
  function NewsStub() { return null; }
  function HistoryStub() { return null; }
  function MarketStub() { return null; }
  const react = {...React,
    useState(initial) {
      const index = stateIndex++;
      if (!(index in states)) states[index] = typeof initial === "function" ? initial() : initial;
      return [states[index],value => {states[index] = typeof value === "function" ? value(states[index]) : value;}];
    },
    useRef(initial) { const index=refIndex++; if (!(index in refs)) refs[index]={current:initial}; return refs[index]; },
    useEffect(callback) {if (!tree) effects.push(callback);},
  };
  const Component = compileComponent("decision-desk",react,{
    "./news-desk":{default:NewsStub},"./research-history":{default:HistoryStub},"./historical-market":{default:MarketStub},
    "./paper-trading-desk":{default:() => null},
    "./price-chart":{PriceChart:() => null},"./buffett-evidence":{BuffettEvidence:() => null},
  },{
    window:{location:{href:`http://127.0.0.1:3001/?desk=${view}`},history:{replaceState(){}},dispatchEvent(event){events.push(event.type);}},
    document:{getElementById(id){return {focus(){focus.push(id);}};}},
    fetch:async (url,options={}) => {
      calls.push({url,method:options.method ?? "GET",body:options.body?JSON.parse(options.body):undefined,signal:options.signal});
      assert.ok(replies.length,`Unexpected network call: ${url}`);
      const reply = replies.shift();
      if (reply instanceof Error) throw reply;
      const result = await reply;
      return {ok:(result.status ?? 200)<400,status:result.status ?? 200,json:async()=>clone(result.data)};
    },
  });
  function render() { stateIndex=0;refIndex=0;tree=Component();assert.equal(stateIndex,13);return tree; }
  function button(label) { const found=walk(tree,node=>node.type==="button" && text(node)===label);assert.ok(found,`Missing button ${label}`);return found; }
  function history() {
    if (!states[11].historyOpen) {button("研究记录").props.onClick();render();}
    const found=walk(tree,node=>node.type===HistoryStub);assert.ok(found,"History should mount only when opened");return found;
  }
  render();
  return {
    states,calls,events,focus,render,button,history,
    startEffects(){const cleanups=effects.map(fn=>fn());return()=>cleanups.forEach(fn=>fn?.());},
    pickHistory(sample,evidence){const found=walk(tree,node=>node.type===MarketStub);assert.ok(found);found.props.onSelect(sample,evidence);},
    field(id){const found=walk(tree,node=>node.props?.id===id);assert.ok(found,`Missing ${id}`);return found;},
    reply(data,status=200) {replies.push({data,status});},
    deferred(promise) {replies.push(promise);},
    get tree(){return tree;},
    get research(){return states[11];},
    enter(selection=SELECTION) {const found=walk(tree,node=>node.type===NewsStub);assert.ok(found);found.props.onResearch(selection);},
  };
}

const REAL_SAMPLE={id:"history-20261113-600000",seat_id:"baseline",symbol:"600000",name:"测试银行",label:"600000 测试银行 · 历史行情",decision_date:"2026-11-12",description:"真实历史"};
const REAL_EVIDENCE={case_id:REAL_SAMPLE.id,symbol:"600000",decision_date:"2026-11-12",bars:[{date:"2026-11-12",open:10,high:10.5,low:9.8,close:10.3,volume:10000}],metrics:{last_close:10.3,data_valid:true},evidence_ids:["real-bar"]};
const REAL_DETAIL={sample:REAL_SAMPLE,evidence:REAL_EVIDENCE,revealed:false};
const realSample=seatId=>({...REAL_SAMPLE,seat_id:seatId});
const realDetail=(seatId="baseline",close=10.3)=>({...REAL_DETAIL,sample:realSample(seatId),evidence:{...REAL_EVIDENCE,name:"测试银行",seat_id:seatId,metrics:{...REAL_EVIDENCE.metrics,last_close:close}}});
const realCatalog=()=>({data_source:"historical",seats:clone(SEATS),cases:SEATS.map(seat=>realSample(seat.id)),ai_available:false,archive:{available:true,end_date:"2026-11-13",decision_date:"2026-11-12"}});

test("initial load defaults to the bounded real archive without generating a plan or revealing Friday",async()=>{
  const h=deskHarness({view:"research"});
  h.reply({...realCatalog(),ai_available:true,model:"configured-fixture"});h.reply(realDetail());
  const dispose=h.startEffects();await flush();h.render();
  assert.deepEqual(h.calls.map(c=>[c.url,c.method]),[
    ["/contest-api/cases","GET"],["/contest-api/market/stocks/600000?seat_id=baseline","GET"],
  ]);
  assert.equal(h.states[4],"baseline");assert.equal(h.states[5],REAL_SAMPLE.id);assert.equal(h.states[6],"offline");
  assert.equal(h.states[7],null);assert.equal(h.states[8],null,"Loading an archive cannot create trades");
  assert.equal(h.states[12].preview.metrics.last_close,10.3);
  assert.equal(h.states[12].preview.decision_date,"2026-11-12");
  assert.equal(h.states[12].endDate,"2026-11-13");dispose();
});

test("a late price response cannot replace another seat's evidence for the same real stock",async()=>{
  const h=deskHarness({view:"research"});let finishPreview;
  h.reply(realCatalog());h.deferred(new Promise(resolve=>{finishPreview=resolve;}));
  const dispose=h.startEffects();await flush();h.render();
  h.reply(realDetail("buffett",22));
  h.field("seat-select").props.onChange({target:{value:"buffett"}});h.render();
  await flush();h.render();assert.ok(h.calls[1].signal.aborted);
  finishPreview({data:realDetail("baseline",99)});await flush();h.render();
  assert.equal(h.states[4],"buffett");assert.equal(h.states[5],REAL_SAMPLE.id);assert.equal(h.states[7],null);
  assert.equal(h.states[12].sample.seat_id,"buffett");assert.equal(h.states[12].preview.metrics.last_close,22);
  assert.equal(h.calls.filter(call=>call.method==="POST").length,0);dispose();
});

test("a pending real-stock preview cannot be mistaken for a frozen plan or trigger a model",async()=>{
  const h=deskHarness({view:"research"});let finishPreview;
  h.reply(realCatalog());h.deferred(new Promise(resolve=>finishPreview=resolve));
  const dispose=h.startEffects();await flush();h.render();
  assert.equal(h.button("生成并冻结预案").props.disabled,true);
  h.button("生成并冻结预案").props.onClick();await flush();assert.equal(h.calls.length,2);assert.equal(h.states[7],null);
  finishPreview({data:realDetail()});await flush();h.render();
  assert.equal(h.button("生成并冻结预案").props.disabled,false);dispose();
});

test("missing archives and future-contaminated responses cannot masquerade as real research",async()=>{
  for (const futureLeak of [false,true]) {
    const h=deskHarness({view:"research"});h.reply(futureLeak ? realCatalog() : {...realCatalog(),cases:[],archive:{available:false}});
    if(futureLeak)h.reply({...realDetail(),evidence:{...REAL_EVIDENCE,bars:[{...REAL_EVIDENCE.bars[0],date:"2026-11-13"}]}});
    const dispose=h.startEffects();await flush();h.render();
    assert.equal(h.states[4],"baseline");assert.equal(h.states[5],futureLeak ? REAL_SAMPLE.id : "");assert.equal(h.states[12].preview,null);
    assert.ok(h.states[12].error);assert.equal(h.button("生成并冻结预案").props.disabled,true);
    assert.equal(h.states[1].cases.some(item=>!item.id.startsWith("history-")),false);dispose();
  }
});

test("selecting real evidence synchronizes the research desk and reselecting preserves a frozen plan",async()=>{
  const h=deskHarness({view:"research",seatId:"baseline",initialCatalog:realCatalog()});h.pickHistory(REAL_SAMPLE,REAL_EVIDENCE);h.render();
  assert.equal(h.states[4],"baseline");assert.equal(h.states[5],REAL_SAMPLE.id);assert.equal(h.states[7],null);assert.equal(h.calls.length,0);
  assert.equal(h.states[12].preview,REAL_EVIDENCE);
  const frozen=makeRun({seat:SEATS[2],evidence:REAL_EVIDENCE}),replayed={status:"blocked",events:[],metrics:{}};
  h.states[7]=frozen;h.states[8]=replayed;h.render();h.pickHistory(REAL_SAMPLE,REAL_EVIDENCE);h.render();
  assert.equal(h.states[7],frozen);assert.equal(h.states[8],replayed);assert.equal(h.calls.length,0);
  assert.equal(walk(h.tree,n=>n.type==="button" && text(n)==="合成规则演示"),null);
});

test("an unmounted initial load is aborted before it can replace the current selection",async()=>{
  const h=deskHarness({view:"research"});let finishPreview;
  h.reply(realCatalog());h.deferred(new Promise(resolve=>finishPreview=resolve));
  const dispose=h.startEffects();await flush();dispose();
  finishPreview({data:realDetail()});await flush();h.render();
  assert.equal(h.calls.length,2);assert.ok(h.calls.every(call=>call.signal.aborted));assert.equal(h.states[12].preview,null);
});

test("all three seat changes preserve the same real code and request independent evidence without trading",async()=>{
  const h=deskHarness({view:"research",seatId:"baseline",initialCatalog:realCatalog()});
  h.pickHistory(REAL_SAMPLE,{...REAL_EVIDENCE,seat_id:"baseline"});h.render();
  for(const seatId of ["buffett","livermore","baseline"]){
    h.reply(realDetail(seatId));h.field("seat-select").props.onChange({target:{value:seatId}});await flush();h.render();
    assert.equal(h.states[4],seatId);assert.equal(h.states[5],REAL_SAMPLE.id);assert.equal(h.states[12].preview.seat_id,seatId);
    assert.equal(h.states[1].cases.find(item=>item.id===REAL_SAMPLE.id && item.seat_id===seatId).name,"测试银行");
    assert.equal(h.calls.at(-1).url,`/contest-api/market/stocks/600000?seat_id=${seatId}`);
    assert.equal(h.states[7],null);assert.equal(h.states[8],null);
  }
  assert.ok(h.calls.every(call=>call.method==="GET"));
});

test("a preview for the wrong seat cannot make a real-stock plan available",async()=>{
  const h=deskHarness({view:"research",seatId:"baseline",initialCatalog:realCatalog()});h.pickHistory(REAL_SAMPLE,REAL_EVIDENCE);h.render();
  h.reply(realDetail("baseline"));h.field("seat-select").props.onChange({target:{value:"buffett"}});await flush();h.render();
  assert.equal(h.states[4],"buffett");assert.equal(h.states[12].preview,null);assert.match(h.states[12].error,/席位证据/);
  assert.equal(h.button("生成并冻结预案").props.disabled,true);assert.equal(h.calls.length,1);
});

test("repeated stock selections retain at most forty summaries per seat without loading the universe",()=>{
  const h=deskHarness({view:"research",seatId:"baseline",initialCatalog:realCatalog()});
  for(let i=0;i<60;i++){
    const code=String(600100+i),sample={...REAL_SAMPLE,id:`history-20261113-${code}`,symbol:code,name:`测试${i}`,label:`${code} 测试${i}`};
    h.pickHistory(sample,{...REAL_EVIDENCE,case_id:sample.id,symbol:code,seat_id:"baseline"});h.render();
  }
  assert.equal(h.states[1].cases.filter(item=>item.seat_id==="baseline").length,40);
  assert.equal(h.states[1].cases.filter(item=>item.seat_id==="buffett").length,1);
  assert.equal(h.states[5],"history-20261113-600159");assert.equal(h.calls.length,0);
});

test("a cached financial-stock shortcut loads its canonical name in the current seat and never analyzes",async()=>{
  const catalog={...realCatalog(),financial_profiles:[{code:"600519",report_count:3,status:"reference_only"}]};
  const h=deskHarness({view:"research",seatId:"buffett",initialCatalog:catalog});
  h.states[12].loading=false;h.render();
  const sample={...realSample("buffett"),id:"history-20261113-600519",symbol:"600519",name:"测试公司",label:"600519 测试公司 · 历史行情"};
  h.reply({sample,evidence:{...REAL_EVIDENCE,case_id:sample.id,symbol:"600519",seat_id:"buffett",name:"测试公司"},revealed:false});
  h.button("600519 · 3期").props.onClick();await flush();h.render();
  assert.equal(h.calls.length,1);assert.equal(h.calls[0].url,"/contest-api/market/stocks/600519?seat_id=buffett");
  assert.equal(h.states[4],"buffett");assert.equal(h.states[5],sample.id);assert.equal(h.states[12].sample.name,"测试公司");
  assert.equal(h.states[1].cases.find(item=>item.id===sample.id && item.seat_id==="buffett").name,"测试公司");
  assert.equal(h.states[7],null);assert.equal(h.states[8],null);
});

test("real historical archives restore names and seat identities for Buffett and Livermore",async()=>{
  for(const seatId of ["buffett","livermore"]){
    const seat=SEATS.find(item=>item.id===seatId),h=deskHarness({view:"research",initialCatalog:realCatalog()});
    const stored=makeRun({seat,evidence:{...REAL_EVIDENCE,name:"测试银行",seat_id:seatId}});
    h.reply({record_id:RECORD,status:"frozen",case_id:REAL_SAMPLE.id,seat_id:seatId,context:{data_mode:"historical",stock:{label:"测试银行"}},analysis:stored,replay:null,replay_available:false});
    h.history().props.onOpen(RECORD);await flush();h.render();
    assert.equal(h.states[4],seatId);assert.equal(h.states[5],REAL_SAMPLE.id);assert.equal(h.states[12].sample.name,"测试银行");
    assert.equal(h.states[1].cases.find(item=>item.id===REAL_SAMPLE.id && item.seat_id===seatId).name,"测试银行");
    assert.equal(h.calls.length,1);assert.equal(h.research.archived,true);
  }
});

test("reselecting the current financial shortcut preserves its ready evidence and enabled plan action",()=>{
  const catalog={...realCatalog(),financial_profiles:[{code:"600000",report_count:3,status:"reference_only"}]};
  const h=deskHarness({view:"research",seatId:"buffett",initialCatalog:catalog});
  const evidence={...REAL_EVIDENCE,seat_id:"buffett"};
  h.pickHistory(realSample("buffett"),evidence);h.render();
  assert.equal(h.button("生成并冻结预案").props.disabled,false);
  h.button("600000 · 3期").props.onClick();h.render();
  assert.equal(h.states[12].preview,evidence);assert.equal(h.button("生成并冻结预案").props.disabled,false);
  assert.equal(h.calls.length,0);
});

test("legacy synthetic archives remain read-only and are not added as a formal demo source",async()=>{
  const h=deskHarness({view:"research",initialCatalog:realCatalog()});
  h.reply({record_id:RECORD,title:"旧价值规则案例",status:"replayed",case_id:"buffett-case",seat_id:"buffett",context:{stock:{label:"旧合成样例"},data_mode:"synthetic"},analysis:makeRun({seat:SEATS[1],evidence:{...makeRun().evidence,case_id:"buffett-case"}}),replay:null,replay_available:true});
  h.history().props.onOpen(RECORD);await flush();h.render();
  assert.equal(h.states[4],"buffett");assert.equal(h.research.archived,true);
  assert.equal(h.button("历史预案只读").props.disabled,true);
  h.button("历史预案只读").props.onClick();await flush();assert.equal(h.calls.length,1);
  assert.ok(h.button("返回真实股票研究"));assert.equal(walk(h.tree,n=>n.type==="button" && text(n)==="合成规则演示"),null);
});

test("news entry creates a separate canonical draft and does not analyze or reveal anything",async()=>{
  const old=makeRun(), before=JSON.stringify(old),h=deskHarness({run:old});
  h.reply({record_id:NEXT_RECORD,case_id:"livermore-case",seat_id:"livermore",context:clone(CONTEXT),status:"draft"});
  h.enter();await flush();h.render();
  assert.deepEqual(h.calls.map(item=>[item.url,item.method]),[["/contest-api/research/start","POST"]]);
  assert.deepEqual(h.calls[0].body,{news_id:"g14",stock_id:"s1",seat_id:"livermore",sector_id:"grid"});
  assert.equal(h.states[0],"research");assert.equal(h.research.recordId,NEXT_RECORD);
  assert.equal(h.research.context.news.id,"g14");assert.equal(h.states[7],null);
  assert.equal(JSON.stringify(old),before,"The previous frozen decision must not be mutated");
  assert.equal(h.research.loading,false);assert.equal(h.research.archived,false);
});

test("mapping failure and wrong server context preserve the current frozen research",async()=>{
  for (const bad of [
    {status:404,data:{detail:"没有明确股票映射"}},
    {status:200,data:{record_id:NEXT_RECORD,case_id:"livermore-case",seat_id:"livermore",context:{...clone(CONTEXT),stock:{id:"unrelated",label:"无关标的"}}}},
    {status:200,data:{record_id:NEXT_RECORD,case_id:"buffett-case",seat_id:"buffett",context:clone(CONTEXT)}},
  ]) {
    const old=makeRun(),h=deskHarness({run:old,research:{recordId:RECORD,context:clone(CONTEXT)}});
    h.reply(bad.data,bad.status);h.enter();await flush();h.render();
    assert.equal(h.states[7],old);assert.equal(h.research.recordId,RECORD);
    assert.equal(h.states[0],"news");assert.ok(h.research.error);assert.equal(h.research.loading,false);
    assert.equal(h.calls.length,1);
  }
});

test("a second news click during a pending start cannot create duplicate records",async()=>{
  const h=deskHarness();let release;
  h.deferred(new Promise(resolve=>{release=resolve;}));
  h.enter();h.render();h.enter();
  assert.equal(h.calls.length,1);
  release({data:{record_id:RECORD,case_id:"livermore-case",seat_id:"livermore",context:clone(CONTEXT)}});
  await flush();h.render();assert.equal(h.research.loading,false);
});

test("draft analysis submits its exact record identity and only freezes after explicit action",async()=>{
  const h=deskHarness({view:"research",research:{recordId:RECORD,context:clone(CONTEXT)}});
  h.reply(makeRun());
  h.button("生成并冻结预案").props.onClick();await flush();h.render();
  assert.equal(h.calls.length,1);assert.equal(h.calls[0].url,"/contest-api/analyze");
  assert.deepEqual(h.calls[0].body,{seat_id:"livermore",case_id:"livermore-case",mode:"offline",research_id:RECORD});
  assert.equal(h.states[7].research_id,RECORD);assert.equal(h.research.recordId,RECORD);
});

test("generating another news-backed plan first creates a different record and preserves the original on failure",async()=>{
  const old=makeRun(),h=deskHarness({view:"research",run:old,research:{recordId:RECORD,context:clone(CONTEXT)}});
  h.reply({record_id:NEXT_RECORD});h.reply({detail:"本地存储暂不可用"},503);
  h.button("新建另一份预案").props.onClick();await flush();h.render();
  assert.deepEqual(h.calls.map(item=>item.url),["/contest-api/research/start","/contest-api/analyze"]);
  assert.equal(h.calls[1].body.research_id,NEXT_RECORD);
  assert.notEqual(h.calls[1].body.research_id,old.research_id);
  assert.equal(h.states[7],old);assert.equal(h.research.recordId,RECORD);assert.ok(h.states[10]);
});

test("opening a saved archive restores only saved evidence and replay without recomputing",async()=>{
  const h=deskHarness({view:"research"}),savedRun=makeRun(),savedReplay=makeReplay();
  h.reply({record_id:RECORD,status:"replayed",case_id:"livermore-case",seat_id:"livermore",context:clone(CONTEXT),analysis:savedRun,replay:savedReplay,replay_available:false});
  h.history().props.onOpen(RECORD);await flush();h.render();
  assert.deepEqual(h.calls.map(item=>[item.url,item.method]),[[`/contest-api/research/records/${RECORD}`,"GET"]]);
  assert.equal(h.states[7].decision_hash,savedRun.decision_hash);assert.deepEqual(h.states[8],savedReplay);
  assert.equal(h.research.archived,true);assert.equal(h.research.historyOpen,false);
  assert.equal(h.button("历史预案只读").props.disabled,true);
  assert.equal(h.calls.filter(item=>/analyze|replay$/.test(item.url)).length,0);
});

test("an archive with no saved replay cannot reveal future data or rerun its original plan",async()=>{
  const h=deskHarness({view:"research",run:makeRun(),research:{recordId:RECORD,context:clone(CONTEXT),archived:true}});
  assert.equal(h.button("历史预案只读").props.disabled,true);
  assert.equal(h.button("档案未保存后续回放").props.disabled,true);
  // Even an invocation bypassing the disabled UI must be stopped by the controller.
  h.button("历史预案只读").props.onClick();h.button("档案未保存后续回放").props.onClick();await flush();
  assert.equal(h.calls.length,0);
});

test("reopening a real-stock archive restores its on-demand case after restart",async()=>{
  const h=deskHarness({view:"research"}),caseId="history-20261113-600000";
  const stored=makeRun({seat:SEATS[2],evidence:{...makeRun().evidence,case_id:caseId,symbol:"600000",decision_date:"2026-11-12",bars:[{date:"2026-11-12"}]}});
  h.reply({record_id:RECORD,status:"frozen",case_id:caseId,seat_id:"baseline",context:{data_mode:"historical",stock:{label:"测试银行"}},analysis:stored,replay:null,replay_available:false});
  h.history().props.onOpen(RECORD);await flush();h.render();
  assert.equal(h.states[1].cases.find(item=>item.id===caseId).symbol,"600000");
  assert.equal(h.states[5],caseId);assert.equal(h.research.archived,true);
  h.button("以当前情景新建研究").props.onClick();h.render();
  assert.equal(h.research.archived,false);assert.equal(h.button("生成并冻结预案").props.disabled,false);
  assert.equal(h.calls.length,1,"Opening or starting a new draft must not call the model");
});

test("a real-stock archive with a different symbol cannot replace the current research",async()=>{
  const old=makeRun(),h=deskHarness({view:"research",run:old}),caseId="history-20261113-600000";
  const stored=makeRun({seat:SEATS[2],evidence:{...old.evidence,case_id:caseId,symbol:"600001",decision_date:"2026-11-12",bars:[{date:"2026-11-12"}]}});
  h.reply({record_id:RECORD,status:"frozen",case_id:caseId,seat_id:"baseline",context:null,analysis:stored,replay:null,replay_available:false});
  h.history().props.onOpen(RECORD);await flush();h.render();
  assert.equal(h.states[7],old);assert.ok(h.research.error);assert.ok(!h.states[1].cases.some(item=>item.id===caseId));
});

test("malformed historical dates or mismatched replay seat cannot replace the active decision",async()=>{
  for (const changes of [
    {analysis:{...makeRun(),evidence:{...makeRun().evidence,bars:[{date:"2026-10-26"}]}}},
    {replay:{...makeReplay(),seat_id:"buffett"}},
    {replay:{...makeReplay(),replay_bars:[{date:CUTOFF}]}},
    {analysis:{...makeRun(),validation:{ok:false,checks:[]}},replay:{...makeReplay(),status:"blocked",replay_bars:[]}},
  ]) {
    const old=makeRun(),h=deskHarness({view:"research",run:old});
    h.reply({record_id:RECORD,status:"replayed",case_id:"livermore-case",seat_id:"livermore",context:clone(CONTEXT),analysis:makeRun(),replay:makeReplay(),replay_available:false,...changes});
    h.history().props.onOpen(RECORD);await flush();h.render();
    assert.equal(h.states[7],old);assert.ok(h.research.error);assert.equal(h.calls.length,1);
  }
});

test("history lookup ignores path-like and malformed record identities without any request",async()=>{
  const h=deskHarness({view:"research"});const onOpen=h.history().props.onOpen;
  for (const value of ["../records","x".repeat(32),"",null,"a".repeat(33)]) onOpen(value);
  await flush();assert.equal(h.calls.length,0);
});

function historyHarness() {
  const states=[],effects=[],requests=[],events=[],replies=[];
  let stateIndex=0,effectIndex=0,tree;
  const opened=[];
  const react={...React,
    useState(initial){const index=stateIndex++;if(!(index in states))states[index]=typeof initial==="function"?initial():initial;return[states[index],value=>{states[index]=typeof value==="function"?value(states[index]):value;}];},
    useEffect(callback,deps){const index=effectIndex++,old=effects[index];if(!old||deps.some((value,i)=>!Object.is(value,old.deps[i])))effects[index]={callback,deps,cleanup:old?.cleanup,pending:true};},
  };
  const Component=compileComponent("research-history",react,{}, {
    window:{dispatchEvent(event){events.push(event.type);}},
    fetch:async(url,options)=>{requests.push({url,options});assert.ok(replies.length,`Unexpected history request ${url}`);const response=await replies.shift();return{ok:(response.status??200)<400,status:response.status??200,json:async()=>clone(response.data)};},
  });
  let props={version:0,disabled:false,onOpen:id=>opened.push(id)};
  function render(next={}){props={...props,...next};stateIndex=0;effectIndex=0;tree=Component(props);for(const effect of effects){if(effect.pending){effect.cleanup?.();effect.pending=false;effect.cleanup=effect.callback();}}return tree;}
  function button(label){const found=walk(tree,node=>node.type==="button"&&text(node)===label);assert.ok(found,`Missing history button ${label}`);return found;}
  return{states,requests,events,opened,render,button,reply(data,status=200){replies.push({data,status});},deferred(p){replies.push(p);},field(label){const found=walk(tree,node=>node.props?.["aria-label"]===label);assert.ok(found);return found;},dispose(){for(const effect of effects)effect.cleanup?.();}};
}
const recordItem=(id=RECORD)=>({record_id:id,status:"frozen",created_at:"2026-11-14T02:00:00Z",updated_at:"2026-11-14T02:00:00Z",case_id:"livermore-case",seat_id:"livermore",stock_label:"电网样本甲",news_title:"深圳配电规划"});

test("history loads a bounded page with authenticated same-origin/no-store requests and opens the selected record",async()=>{
  const h=historyHarness();h.reply({items:[recordItem()],total:1,limit:10,offset:0});h.render();await flush();h.render();
  const request=h.requests[0];assert.equal(request.url,"/contest-api/research/records?limit=10&offset=0");
  assert.equal(request.options.credentials,"same-origin");assert.equal(request.options.cache,"no-store");
  h.button("打开记录").props.onClick();assert.deepEqual(h.opened,[RECORD]);assert.equal(h.button("上一页").props.disabled,true);assert.equal(h.button("下一页").props.disabled,true);
  h.dispose();assert.equal(request.options.signal.aborted,true);
});

test("history pages ten records at a time, resets offset on date/seat changes, and cancels superseded reads",async()=>{
  const h=historyHarness(),items=Array.from({length:10},(_,i)=>recordItem(i.toString(16).padStart(32,"0")));
  h.reply({items,total:25,limit:10,offset:0});h.render();await flush();h.render();
  const firstSignal=h.requests[0].options.signal;
  h.reply({items,total:25,limit:10,offset:10});h.button("下一页").props.onClick();h.render();await flush();h.render();
  assert.match(h.requests[1].url,/offset=10/);assert.equal(firstSignal.aborted,true);
  h.reply({items:[recordItem()],total:1,limit:10,offset:0});h.field("研究记录创建日期").props.onChange({target:{value:"2026-11-14"}});h.render();await flush();h.render();
  assert.match(h.requests[2].url,/offset=0.*date=2026-11-14/);
  h.reply({items:[recordItem()],total:1,limit:10,offset:0});h.field("研究记录席位").props.onChange({target:{value:"buffett"}});h.render();await flush();h.render();
  assert.match(h.requests[3].url,/date=2026-11-14.*seat_id=buffett/);
  h.dispose();
});

test("oversized history responses are rejected and unauthenticated reads notify the login gate",async()=>{
  const oversized=historyHarness();oversized.reply({items:Array.from({length:11},()=>recordItem()),total:11,limit:10,offset:0});oversized.render();await flush();oversized.render();
  assert.equal(oversized.states[3],null);assert.match(oversized.states[4],/格式不正确/);oversized.dispose();
  const expired=historyHarness();expired.reply({detail:"登录已失效"},401);expired.render();await flush();expired.render();
  assert.deepEqual(expired.events,["contest-session-expired"]);assert.match(expired.states[4],/登录已失效/);expired.dispose();
});

test("an aborted history request cannot leak stale rows into a later filter",async()=>{
  const h=historyHarness();let finishOld;
  h.deferred(new Promise(resolve=>{finishOld=resolve;}));h.render();
  h.reply({items:[],total:0,limit:10,offset:0});h.field("研究记录创建日期").props.onChange({target:{value:"2026-11-14"}});h.render();await flush();h.render();
  finishOld({data:{items:[recordItem()],total:1,limit:10,offset:0}});await flush();h.render();
  assert.equal(h.states[3].items.length,0,"Older results must not overwrite the active date page");
  h.dispose();
});

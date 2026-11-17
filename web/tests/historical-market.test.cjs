"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const ts = require("typescript");
const React = require("react");
const flush = () => new Promise(resolve => setImmediate(resolve));
const sample = {id:"history-20261113-600000",symbol:"600000",seat_id:"baseline",name:"测试银行",label:"历史行情",decision_date:"2026-11-12"};
const detail = {sample,source:"sina",history_start:"2024-11-01",history_bar_count:494,replay_day:"2026-11-13",revealed:false,evidence:{case_id:sample.id,symbol:sample.symbol,metrics:{last_close:10.1},evidence_ids:["fixture-bar"],decision_date:"2026-11-12",bars:[{date:"2026-11-12",open:10,close:10.1,high:10.2,low:9.9,volume:100}],limitations:["不模拟成交"]}};
function walk(node,predicate) {
  if (!node || typeof node !== "object") return null;
  if (predicate(node)) return node;
  for (const child of React.Children.toArray(node.props?.children)) {const found=walk(child,predicate);if(found)return found;}
  return null;
}
function text(node) {
  if (node == null || typeof node === "boolean") return "";
  if (typeof node !== "object") return String(node);
  return React.Children.toArray(node.props?.children).map(text).join("");
}
function harness(disabled=false,seatId="baseline") {
  let selectedCode;
  const states=[],refs=[],effects=[],calls=[],replies=[],selected=[],events=[],timers=new Map();
  let si=0,ri=0,ei=0,tid=0,tree;
  const react={...React,
    useState(initial){const i=si++;if(!(i in states))states[i]=initial;return[states[i],v=>states[i]=typeof v==="function"?v(states[i]):v];},
    useRef(initial){const i=ri++;if(!(i in refs))refs[i]={current:initial};return refs[i];},
    useEffect(fn,deps){const i=ei++,old=effects[i];if(!old||deps.some((v,j)=>v!==old.deps[j])){old?.cleanup?.();effects[i]={deps,cleanup:fn()};}},
  };
  const filename=path.join(__dirname,"../app/historical-market.tsx");
  const code=ts.transpileModule(fs.readFileSync(filename,"utf8"),{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX},fileName:filename}).outputText;
  const exports={};
  function Chart(){return null;}
  vm.runInNewContext(code,{exports,module:{exports},AbortController,Event,
    require(name){if(name==="react")return react;if(name==="./price-chart")return{PriceChart:Chart};return require(name);},
    window:{setTimeout(fn){timers.set(++tid,fn);return tid;},clearTimeout(id){timers.delete(id);},dispatchEvent(event){events.push(event.type);}},
    fetch:async(url,options)=>{calls.push({url,...options});assert.ok(replies.length,`Unexpected fetch ${url}`);const r=await replies.shift();return{status:r.status??200,ok:(r.status??200)<400,json:async()=>r.data};},
  },{filename});
  function render(){si=0;ri=0;ei=0;tree=exports.default({disabled,seatId,selectedCode,onSelect:s=>selected.push(s)});return tree;}
  render();
  return {states,calls,replies,selected,events,render,
    reply(data,status=200){replies.push({data,status});},
    async directory(){this.reply({downloaded:1,eligible_total:1,complete_for_target:1});this.reply({items:[{code:"600000",name:"测试银行",bar_count:494,last_date:"2026-11-13",sample:{...sample,seat_id:seatId}}],total:1});const queued=[...timers.values()];timers.clear();queued.forEach(fn=>fn());await flush();render();},
    async click(label){const button=walk(tree,n=>n.type==="button"&&text(n).includes(label));assert.ok(button,`Missing ${label}`);await button.props.onClick();await flush();render();},
    find(predicate){return walk(tree,predicate);},
    setDisabled(value){disabled=value;render();},
    setSeat(value){seatId=value;render();},
    setSelectedCode(value){selectedCode=value;render();},
    unmount(){effects.forEach(effect=>effect.cleanup?.());},
    get text(){return text(tree);},
  };
}
test("history fetches only a paged directory; selecting a stock never requests future data",async()=>{
  const h=harness();await h.directory();assert.equal(h.calls.length,2);
  assert.ok(h.calls[1].url.endsWith("stocks?q=&limit=20&offset=0&seat_id=baseline"));
  h.reply(detail);await h.click("600000");assert.ok(h.calls[2].url.endsWith("stocks/600000?seat_id=baseline"));
  assert.ok(!h.calls[2].url.includes("reveal"));assert.ok(h.text.includes("不模拟成交"));
  assert.equal(h.selected[0].id,sample.id,"Picking a stock immediately brings its evidence into the research desk");
  await h.click("查看下方基础席位研究");assert.equal(h.selected[1].id,sample.id);assert.equal(h.calls.length,3);
});
test("future observation requires a separate explicit click",async()=>{
  const h=harness();await h.directory();h.reply(detail);await h.click("600000");
  h.reply({...detail,revealed:true,observed_bars:[{...detail.evidence.bars[0],date:"2026-11-13"}]});
  await h.click("仅揭示周五行情");assert.ok(h.calls[3].url.endsWith("?seat_id=baseline&reveal=true"));assert.ok(h.text.includes("不是预案成交回放"));
});
test("a leaked future bar is rejected before a stock can enter research",async()=>{
  const h=harness();await h.directory();h.reply({...detail,evidence:{...detail.evidence,bars:[{...detail.evidence.bars[0],date:"2026-11-13"}]}});
  await h.click("600000");assert.ok(h.text.includes("已拒绝展示"));assert.equal(h.find(n=>n.type==="button"&&text(n)==="查看下方基础席位研究"),null);
});
test("stock requests abort on reselection and unmount",async()=>{
  const h=harness();await h.directory();let finishFirst,finishSecond;
  h.replies.push(new Promise(resolve=>finishFirst=resolve));await h.click("600000");
  h.replies.push(new Promise(resolve=>finishSecond=resolve));await h.click("600000");
  assert.ok(h.calls[2].signal.aborted);assert.ok(!h.calls[3].signal.aborted);
  h.unmount();assert.ok(h.calls.every(call=>call.signal.aborted));
  finishFirst({data:detail});finishSecond({data:detail});await flush();h.render();
  assert.equal(h.find(n=>n.type==="button"&&text(n)==="查看下方基础席位研究"),null);
});
test("expired sessions are handed back to login without model calls",async()=>{
  const h=harness();await h.directory();h.reply({detail:"会话已过期"},401);await h.click("600000");
  assert.deepEqual(h.events,["contest-session-expired"]);assert.ok(h.text.includes("会话已过期"));assert.equal(h.selected.length,0);
});
test("disabled research prevents stock details and callback",async()=>{
  const h=harness(true);await h.directory();await h.click("600000");assert.equal(h.calls.length,2);assert.equal(h.selected.length,0);
});

test("a pending stock preview is cancelled when a plan operation locks selection",async()=>{
  const h=harness();await h.directory();let finish;
  h.replies.push(new Promise(resolve=>finish=resolve));await h.click("600000");
  h.setDisabled(true);assert.ok(h.calls[2].signal.aborted);
  finish({data:detail});await flush();h.render();assert.equal(h.selected.length,0);
});

for(const seatId of ["buffett","livermore"]){
  test(`${seatId} stock picker reads real directory and evidence for its current seat`,async()=>{
    const h=harness(false,seatId);await h.directory();
    const data={...detail,sample:{...sample,seat_id:seatId},evidence:{...detail.evidence,seat_id:seatId}};
    h.reply(data);await h.click("600000");
    assert.ok(h.calls[1].url.endsWith(`&seat_id=${seatId}`));assert.ok(h.calls[2].url.endsWith(`?seat_id=${seatId}`));
    assert.equal(h.selected[0].seat_id,seatId);assert.equal(h.selected[0].symbol,"600000");assert.equal(h.calls.length,3);
    assert.ok(h.text.includes(seatId==="buffett"?"查看下方巴菲特席位研究":"查看下方利弗莫尔席位研究"));
  });
}

test("changing a picker seat cancels pending details and cannot adopt a late old-seat quote",async()=>{
  const h=harness();await h.directory();let release;
  h.replies.push(new Promise(resolve=>release=resolve));await h.click("600000");
  h.setSeat("buffett");assert.ok(h.calls[2].signal.aborted);
  release({data:detail});await flush();h.render();assert.equal(h.selected.length,0);
  assert.ok(!h.text.includes("查看下方基础席位研究"));
});

test("an external stock selection aborts an older picker request before it can reselect its stock",async()=>{
  const h=harness();await h.directory();let release;
  h.replies.push(new Promise(resolve=>release=resolve));await h.click("600000");
  h.setSelectedCode("600519");assert.ok(h.calls[2].signal.aborted);
  release({data:detail});await flush();h.render();assert.equal(h.selected.length,0);
  assert.ok(!h.text.includes("查看下方基础席位研究"));
});

test("the picker rejects a fabricated later evidence cutoff before displaying an unrevealed chart",async()=>{
  const h=harness();await h.directory();
  h.reply({...detail,evidence:{...detail.evidence,decision_date:"2026-11-13",bars:[{...detail.evidence.bars[0],date:"2026-11-13"}]}});
  await h.click("600000");assert.ok(h.text.includes("已拒绝展示"));assert.equal(h.selected.length,0);
  assert.equal(h.find(n=>n.type==="button"&&text(n)==="查看下方基础席位研究"),null);
});

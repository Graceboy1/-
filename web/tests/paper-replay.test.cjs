"use strict";

// Disposable owner-ledger fixtures only. No production account, network,
// credentials, model service, or trade engine is read or executed.
const test=require("node:test");
const assert=require("node:assert/strict");
const fs=require("node:fs");
const path=require("node:path");
const vm=require("node:vm");
const ts=require("typescript");
const React=require("react");
const {renderToStaticMarkup}=require("react-dom/server");
const flush=()=>new Promise(resolve=>setImmediate(resolve));
const DAY="2026-11-13";
const SEATS=["baseline","livermore","buffett"];
function order(overrides={}){return{id:1,order_id:1,seat_id:"baseline",code:"600519",name:"测试回放公司",date:DAY,timestamp:`${DAY}T09:30:00+08:00`,side:"buy",price:12,quantity:100,amount:1200,fees:5,reason:"测试冻结条件通过",execution_mode:"historical_open_estimate",...overrides};}
function playback(overrides={}){
  const selection=order(),bars=["2026-11-10","2026-11-11","2026-11-12",DAY].map((date,index)=>({date,open:10+index,high:11+index,low:9+index,close:10.5+index,volume:100+index,source:"fixture",complete:true,bar_status:"历史日K",asof:`${date}T15:00:00+08:00`}));
  return{mode:"historical",as_of:DAY,timeframe:"1d",selection,trades:[selection],trade_total:1,bars,markers:[{...selection,type:"buy",bar_index:3,matched_bar:true}],context_start:bars[0].date,last_available_date:DAY,has_trade_day:true,limitations:["日线不能判断日内高低点先后。"],source_hash:"a".repeat(64),...overrides};
}
function statistics(overrides={}){
  return{mode:"historical",as_of:DAY,start_date:DAY,scope:"all_saved_ledger_through_as_of",accounts:SEATS.map(seat_id=>({seat_id,data_as_of:DAY,status:"completed",initial_cash:100000,cash:97995,market_value:2300,equity:100295,total_pnl:295,total_return_pct:.295,period_pnl:295,period_return_pct:.295,realized_pnl:-5,unrealized_pnl:300,fees:5,buy_count:1,sell_count:0,closed_trade_count:0,win_count:0,loss_count:0,flat_count:0,win_rate_pct:null,profit_factor:null,average_win:null,average_loss:null,max_closed_profit:null,max_closed_loss:null,max_drawdown_pct:0,drawdown_peak_date:DAY,drawdown_trough_date:DAY,equity_curve:[{date:DAY,kind:"opening",equity:100000,return_pct:0},{date:DAY,kind:"daily_snapshot",equity:100295,return_pct:.295}],closed_trades:[],warnings:[]})),limitations:["统计使用完整已保存账本，不受订单分页限制。"],...overrides};
}
function plain(node){if(node==null||typeof node==="boolean")return"";if(typeof node!=="object")return String(node);if(typeof node.type==="function")return plain(node.type(node.props));return React.Children.toArray(node.props?.children).map(plain).join("");}
function walk(node,predicate){if(!node||typeof node!=="object")return null;if(predicate(node))return node;for(const child of React.Children.toArray(node.props?.children)){const result=walk(child,predicate);if(result)return result;}return null;}
function harness({stats=statistics(),chart=playback(),props={}}={}){
  const states=[],refs=[],effects=[],calls=[],replies=[stats,chart].map(data=>({data})),events=[],timers=new Map(),listeners=new Map(),selected=[];
  let si=0,ri=0,ei=0,tid=0,tree;
  let properties={mode:"historical",date:DAY,seat:"all",onSelectOrder:id=>selected.push(id),...props};
  const react={...React,useState(initial){const i=si++;if(!(i in states))states[i]=typeof initial==="function"?initial():initial;return[states[i],value=>states[i]=typeof value==="function"?value(states[i]):value];},useRef(initial){const i=ri++;if(!(i in refs))refs[i]={current:initial};return refs[i];},useEffect(callback,deps){const i=ei++,old=effects[i];if(!old||deps.some((value,index)=>value!==old.deps[index]))effects[i]={deps,callback,cleanup:old?.cleanup,pending:true};}};
  const document={hidden:false,addEventListener(name,callback){if(!listeners.has(name))listeners.set(name,new Set());listeners.get(name).add(callback);},removeEventListener(name,callback){listeners.get(name)?.delete(callback);}};
  const filename=path.join(__dirname,"../app/paper-replay.tsx"),exports={};
  const code=ts.transpileModule(fs.readFileSync(filename,"utf8"),{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX},fileName:filename}).outputText;
  vm.runInNewContext(code,{exports,module:{exports},URLSearchParams,AbortController,Event,console,Date,document,require(name){if(name==="react")return react;if(name.endsWith(".css"))return{};return require(name);},window:{setTimeout(callback,delay){timers.set(++tid,{callback,delay});return tid;},clearTimeout(id){timers.delete(id);},dispatchEvent(event){events.push(event.type);}},fetch:async(url,options)=>{calls.push({url,...options});assert.ok(replies.length,`Unexpected GET ${url}`);const reply=await replies.shift();return{status:reply.status??200,ok:(reply.status??200)<400,json:async()=>reply.data};}},{filename});
  function render(){si=0;ri=0;ei=0;tree=exports.default(properties);effects.forEach(effect=>{if(effect.pending){effect.pending=false;effect.cleanup?.();effect.cleanup=effect.callback();}});return tree;}
  render();
  return{calls,replies,states,timers,selected,events,listeners,exports,render,
    reply(stats=statistics(),chart=playback()){replies.push({data:stats},{data:chart});},
    async settle(){await flush();render();await flush();render();},
    button(label){const node=walk(tree,node=>node.type==="button"&&plain(node)===label);assert.ok(node,`Missing button ${label}`);return node;},
    click(label){const node=this.button(label);if(!node.props.disabled)node.props.onClick();render();},
    input(label,value){const node=walk(tree,node=>node.props?.["aria-label"]===label);assert.ok(node,`Missing input ${label}`);node.props.onChange({target:{value}});render();},
    change(props){properties={...properties,...props};render();},
    async tick(){const next=timers.entries().next().value;assert.ok(next,"Missing playback timer");timers.delete(next[0]);next[1].callback();await this.settle();},
    async hidden(value){document.hidden=value;listeners.get("visibilitychange")?.forEach(callback=>callback());await this.settle();},
    unmount(){effects.forEach(effect=>effect.cleanup?.());},
    get tree(){return tree;},get text(){return plain(tree);},get markup(){return renderToStaticMarkup(tree);},
  };
}

test("replay reads owner performance and daily playback only, never starts or re-executes trades",async()=>{
  const h=harness();await h.settle();assert.deepEqual(h.calls.map(call=>call.url),[`/contest-api/paper/performance?mode=historical&date=${DAY}&seat_id=all`,`/contest-api/paper/playback?mode=historical&date=${DAY}&seat_id=all`]);
  for(const call of h.calls){assert.equal(call.method,undefined);assert.equal(call.body,undefined);assert.equal(call.credentials,"same-origin");assert.equal(call.redirect,"error");assert.equal(call.cache,"no-store");}
  assert.match(h.text,/测试回放公司.*600519/);assert.match(h.text,/逐笔指每一笔模拟订单，并非逐笔行情/);assert.match(h.text,/只读回放，不重新下单/);h.unmount();
});

test("full statistics expose separate seats and do not count floating gains as closed wins",async()=>{
  const h=harness();await h.settle();assert.match(h.text,/基础策略席位/);assert.match(h.text,/利弗莫尔席位/);assert.match(h.text,/巴菲特席位/);assert.match(h.text,/已实现损益-5.00 元/);assert.match(h.text,/持仓浮盈亏\+300.00 元/);assert.match(h.text,/已平仓胜率—/);assert.doesNotMatch(h.text,/已平仓胜率0.00%/);assert.match(h.text,/盈亏因子—/);assert.match(h.text,/日快照最大回撤/);assert.match(h.text,/浮盈亏不计入胜率/);assert.doesNotMatch(h.text,/年化收益|夏普比率/);h.unmount();
});

test("same-date opening and saved close use distinct x positions in equity charts",async()=>{
  const h=harness();await h.settle();const chart=walk(h.tree,node=>typeof node.type==="function"&&node.props.points);const markup=renderToStaticMarkup(chart);assert.match(markup,/cx="52"/);assert.match(markup,/cx="780"/);assert.match(markup,/期初基线/);assert.match(markup,/日快照/);h.unmount();
});

test("incomplete ledgers disclose integrity gaps and never present verified subsets as a full win rate",async()=>{
  const stats=statistics();stats.accounts=stats.accounts.slice(0,1).map(account=>({...account,ledger_integrity:"incomplete",excluded_order_count:1,unmatched_sell_count:1,verified_closed_trade_count:1,closed_trade_count:null,win_count:null,loss_count:null,flat_count:null,win_rate_pct:null,profit_factor:null,average_win:null,average_loss:null,max_closed_profit:null,max_closed_loss:null,realized_pnl:null}));
  const h=harness({stats});await h.settle();assert.match(h.text,/账本记录不完整，胜率等交易统计暂不展示；下方明细仅为已核对样本。/);assert.match(h.text,/已平仓交易— 笔/);assert.match(h.text,/盈利 \/ 亏损 \/ 持平— \/ — \/ —/);assert.match(h.text,/已平仓胜率—/);assert.doesNotMatch(h.text,/已平仓胜率100|已平仓胜率0/);assert.doesNotMatch(h.text,/尚无已平仓交易/);h.unmount();
});

test("hidden future candles and future trade markers cannot affect the revealed chart",async()=>{
  const h=harness();await h.settle();const base=playback(),changed=structuredClone(base);changed.bars[3].high=999;changed.bars[3].volume=999999999;changed.markers[0].price=900;
  const render=(value,cursor)=>renderToStaticMarkup(React.createElement(h.exports.DailyReplayChart,{bars:value.bars,markers:value.markers,cursor}));
  assert.equal(render(base,2),render(changed,2));assert.doesNotMatch(render(base,2),/data-marker-order/);assert.match(render(base,4),/data-marker-order="1"/);assert.notEqual(render(base,4),render(changed,4));h.unmount();
});

test("play, pause, steps, speed, slider and reveal-all are local and never write orders",async()=>{
  const h=harness();await h.settle();assert.equal(h.states[4],1);const count=h.calls.length;
  h.click("下一根");assert.equal(h.states[4],2);h.click("上一根");assert.equal(h.states[4],1);h.input("日K播放速度","400");h.click("播放日K");assert.equal([...h.timers.values()][0].delay,400);await h.tick();assert.equal(h.states[4],2);h.click("暂停播放");assert.equal(h.timers.size,0);h.input("日K回放进度","3");assert.equal(h.states[4],3);h.click("显示全部日K");assert.equal(h.states[4],4);assert.match(h.text,/已揭示买卖标记 1 个/);assert.equal(h.calls.length,count);h.unmount();
});

test("missing current-day candles stay empty instead of recycling archived days",async()=>{
  const trade=order(),h=harness({chart:playback({bars:[],has_trade_day:false,markers:[{...trade,type:"buy",bar_index:null,matched_bar:false}],last_available_date:null})});await h.settle();assert.match(h.text,/暂无可播放的真实日K/);assert.match(h.text,/不挪用旧日K线绘制标记/);assert.equal(h.button("播放日K").props.disabled,true);assert.doesNotMatch(h.markup,/data-candle-date=/);h.unmount();
});

test("a selected owned order beyond the shortcut page remains available",async()=>{
  const selected=order({id:101,order_id:101,name:"账本后页公司"}),h=harness({chart:playback({selection:selected,trade_total:101,markers:[{...selected,type:"buy",bar_index:3,matched_bar:true}]}),props:{orderId:101}});await h.settle();assert.match(h.calls[1].url,/order_id=101/);assert.match(h.text,/账本后页公司/);assert.match(h.text,/所选账本订单/);assert.match(h.text,/共101笔可回看交易/);h.input("回放交易","1");assert.deepEqual(h.selected,[1]);h.unmount();
});

test("selection and mode changes abort stale reads and stop playback",async()=>{
  const h=harness();await h.settle();h.click("播放日K");h.reply(statistics(),playback({selection:order({id:2,order_id:2}),markers:[{...order({id:2,order_id:2}),type:"buy",bar_index:3,matched_bar:true}]}));h.change({orderId:2});await h.settle();assert.ok(h.calls[0].signal.aborted);assert.equal(h.states[1].selection.id,2);assert.equal(h.states[5],false);assert.equal(h.timers.size,0);assert.match(h.calls.at(-1).url,/order_id=2/);h.unmount();
});

test("hiding and unmounting stop timers, abort reads and release listeners",async()=>{
  const h=harness();await h.settle();h.click("播放日K");await h.hidden(true);assert.equal(h.timers.size,0);assert.equal(h.states[5],false);assert.ok(h.calls[0].signal.aborted);h.reply();await h.hidden(false);assert.equal(h.calls.length,4);h.click("播放日K");h.unmount();assert.equal(h.timers.size,0);assert.equal(h.listeners.get("visibilitychange").size,0);assert.ok(h.calls.at(-1).signal.aborted);
});

test("date/seat/source validation rejects wrong data without fake fallback candles",async()=>{
  for(const chart of [playback({as_of:"2026-11-16"}),playback({bars:[{...playback().bars[0],date:"2026-11-16"}]}),playback({selection:order({code:"688001"})}),playback({bars:Array.from({length:261},()=>playback().bars[0])})]){
    const h=harness({chart});await h.settle();assert.match(h.text,/校验未通过/);assert.doesNotMatch(h.markup,/data-candle-date=/);assert.equal(h.states[1],null);h.unmount();
  }
});

test("failed refresh keeps previously verified ledger facts and never invents a result",async()=>{
  const h=harness();await h.settle();h.replies.push({status:503,data:{detail:"资料暂不可读"}},{data:playback()});h.click("刷新复盘资料");await h.settle();assert.match(h.text,/资料暂不可读/);assert.match(h.text,/测试回放公司/);assert.match(h.text,/不会重新撮合、补单或用示例收益代替/);assert.ok(h.calls.every(call=>!call.body));h.unmount();
});

test("statistics range filtering is read-only and remains capped at the chosen cutoff",async()=>{
  const h=harness();await h.settle();h.reply(statistics({start_date:"2026-11-01"}));h.input("统计起始日","2026-11-01");await h.settle();assert.match(h.calls.at(-2).url,/start_date=2026-11-01/);const before=h.calls.length;h.input("统计起始日","2026-11-16");await h.settle();assert.equal(h.calls.length,before);assert.match(h.text,/2026-11-01 至 2026-11-13/);h.unmount();
});

test("expired session is reported to the login gate without exposing data or retrying",async()=>{
  const h=harness();await h.settle();h.replies.push({status:401,data:{detail:"会话已过期"}},{data:playback()});h.click("刷新复盘资料");await h.settle();assert.deepEqual(h.events,["contest-session-expired"]);assert.match(h.text,/会话已过期/);assert.equal(h.calls.length,4);h.unmount();
});

test("names, reasons and limitations render as text, not executable HTML",async()=>{
  const trade=order({name:"<script>bad()</script>",reason:"<img src=x onerror=bad()>"}),h=harness({chart:playback({selection:trade,trades:[trade],markers:[{...trade,type:"buy",bar_index:3,matched_bar:true}],limitations:["<iframe src=bad>"]})});await h.settle();assert.doesNotMatch(h.markup,/<script|<img|<iframe/);assert.match(h.markup,/&lt;script/);assert.match(h.markup,/&lt;img/);h.unmount();
});

"use client";

import { useEffect, useRef, useState } from "react";
import "./paper-replay.css";

type Seat = "baseline" | "livermore" | "buffett";
type Mode = "historical" | "live";
type Trade = {id:number;order_id?:number;seat_id:Seat;code:string;name:string;date:string;timestamp:string;side:"buy"|"sell";price:number;quantity:number;amount?:number;fees:number;reason:string;execution_mode?:string};
type Bar = {date:string;open:number;high:number;low:number;close:number;volume:number;complete?:boolean;bar_status?:string;asof?:string};
type Marker = Trade & {type:"buy"|"sell";bar_index:number|null;matched_bar:boolean};
type Playback = {mode:Mode;as_of:string;timeframe:"1d";selection:Trade|null;trades:Trade[];trade_total:number;bars:Bar[];markers:Marker[];marker_total?:number;context_start:string|null;last_available_date:string|null;has_trade_day:boolean;limitations:string[];source_hash:string|null};
type Point = {date:string;kind:string;equity:number;return_pct:number};
type Closed = {code:string;name:string;quantity:number;entry_date:string;exit_date:string;entry_price:number;exit_price:number;pnl_net:number;return_pct:number;holding_calendar_days:number};
type Account = {seat_id:Seat;data_as_of:string|null;status:string;initial_cash:number|null;cash:number|null;market_value:number|null;equity:number|null;total_pnl:number|null;total_return_pct:number|null;period_pnl:number|null;period_return_pct:number|null;realized_pnl:number|null;unrealized_pnl:number|null;fees:number|null;buy_count:number;sell_count:number;closed_trade_count:number|null;win_count:number|null;loss_count:number|null;flat_count:number|null;ledger_integrity?:"complete"|"incomplete";verified_closed_trade_count?:number;excluded_order_count?:number;unmatched_sell_count?:number;win_rate_pct:number|null;profit_factor:number|null;average_win:number|null;average_loss:number|null;max_closed_profit:number|null;max_closed_loss:number|null;max_drawdown_pct:number|null;drawdown_peak_date:string|null;drawdown_trough_date:string|null;equity_curve:Point[];closed_trades:Closed[];warnings:string[]};
type Performance = {mode:Mode;as_of:string;start_date:string|null;scope:string;accounts:Account[];limitations:string[]};
type Props = {mode:Mode;date:string;seat:string;orderId?:number|null;onSelectOrder?:(id:number|null)=>void;refreshVersion?:string|number};
const SEATS:Record<Seat,string>={baseline:"基础策略席位",livermore:"利弗莫尔席位",buffett:"巴菲特席位"};
const DAY=/^\d{4}-\d{2}-\d{2}$/;
const CODE=/^(?:000|001|002|003|600|601|603|605)\d{3}$/;
const numeric=(n:unknown):n is number=>typeof n==="number"&&Number.isFinite(n);
const number=(value:number|null|undefined,digits=2)=>numeric(value)?value.toLocaleString("zh-CN",{minimumFractionDigits:digits,maximumFractionDigits:digits}):"—";
const money=(value:number|null|undefined)=>numeric(value)?`${number(value)} 元`:"—";
const percentage=(value:number|null|undefined)=>numeric(value)?`${number(value)}%`:"—";
const change=(value:number|null|undefined)=>numeric(value)?`${value>0?"+":""}${number(value)} 元`:"—";
const tone=(value:number|null|undefined)=>numeric(value)&&value!==0?value>0?"rise":"fall":"";
const seatValid=(value:unknown):value is Seat=>typeof value==="string"&&Object.hasOwn(SEATS,value);
const failure=(cause:unknown)=>cause instanceof Error?cause.message:"复盘资料暂不可读。";

async function read(path:string,signal:AbortSignal):Promise<unknown>{
  const response=await fetch(`/contest-api/paper/${path}`,{signal,credentials:"same-origin",redirect:"error",cache:"no-store"});
  const value=await response.json().catch(()=>null);
  if(response.status===401&&!signal.aborted)window.dispatchEvent(new Event("contest-session-expired"));
  if(!response.ok)throw new Error(typeof value?.detail==="string"?value.detail:`复盘请求失败（HTTP ${response.status}）。`);
  return value;
}
function tradeValid(value:Trade,date:string){return value&&Number.isSafeInteger(value.id)&&value.id>0&&seatValid(value.seat_id)&&CODE.test(value.code)&&DAY.test(value.date)&&value.date<=date&&["buy","sell"].includes(value.side)&&numeric(value.price)&&value.price>0&&numeric(value.quantity)&&value.quantity>0;}
function validatePlayback(raw:unknown,mode:Mode,date:string,seat:string,orderId?:number|null):Playback{
  const value=raw as Playback;
  if(!value||value.mode!==mode||value.as_of!==date||value.timeframe!=="1d"||!Array.isArray(value.bars)||value.bars.length>260
    ||!Array.isArray(value.trades)||value.trades.length>50||!Number.isSafeInteger(value.trade_total)||value.trade_total<0
    ||!Array.isArray(value.markers)||value.markers.length>500||!Array.isArray(value.limitations)
    ||value.selection&&!tradeValid(value.selection,date)||orderId&&value.selection?.id!==orderId
    ||seat!=="all"&&value.selection&&value.selection.seat_id!==seat
    ||value.trades.some(item=>!tradeValid(item,date)||seat!=="all"&&item.seat_id!==seat)
    ||value.bars.some((bar,index)=>!DAY.test(bar.date)||bar.date>date||index>0&&bar.date<=value.bars[index-1].date||![bar.open,bar.high,bar.low,bar.close,bar.volume].every(numeric)||bar.low<=0||bar.volume<0||bar.low>Math.min(bar.open,bar.close)||Math.max(bar.open,bar.close)>bar.high)
    ||value.markers.some(mark=>!tradeValid(mark,date)||!value.selection||mark.code!==value.selection.code||mark.seat_id!==value.selection.seat_id||mark.matched_bar&&(mark.bar_index==null||!Number.isSafeInteger(mark.bar_index)||value.bars[mark.bar_index]?.date!==mark.date))){
    throw new Error("回放日期、交易归属或日K校验未通过，未绘制不匹配资料。");
  }
  return value;
}
function validatePerformance(raw:unknown,mode:Mode,date:string,seat:string):Performance{
  const value=raw as Performance;
  if(!value||value.mode!==mode||value.as_of!==date||!Array.isArray(value.accounts)||value.accounts.length>3||!Array.isArray(value.limitations)
    ||value.accounts.some(row=>!seatValid(row.seat_id)||seat!=="all"&&row.seat_id!==seat||!Array.isArray(row.equity_curve)||row.equity_curve.length>366||!Array.isArray(row.closed_trades)||row.closed_trades.length>50||!Array.isArray(row.warnings)
      ||row.data_as_of!=null&&(!DAY.test(row.data_as_of)||row.data_as_of>date)
      ||row.equity_curve.some(point=>!DAY.test(point.date)||point.date>date||!numeric(point.equity)||!numeric(point.return_pct))
      ||row.closed_trades.some(item=>!CODE.test(item.code)||item.exit_date>date))){
    throw new Error("收益统计范围或快照日期校验未通过，未使用其他日期收益补齐。");
  }
  return value;
}

export function DailyReplayChart({bars,markers,cursor}:{bars:Bar[];markers:Marker[];cursor:number}){
  // Neither price scale nor volume scale may reveal unseen future candles.
  const shown=bars.slice(0,cursor).slice(-60), dates=new Map(shown.map((bar,index)=>[bar.date,index]));
  const marks=markers.filter(mark=>mark.matched_bar&&dates.has(mark.date));
  if(!shown.length)return <p className="paper-empty">暂无可播放的真实日K；不会用旧日期或模拟柱补齐。</p>;
  const w=920,h=330,left=64,right=902,top=20,bottom=234,volBottom=295,step=(right-left)/shown.length;
  const values=[...shown.flatMap(bar=>[bar.high,bar.low]),...marks.map(mark=>mark.price)];
  const lo=Math.min(...values),hi=Math.max(...values),span=Math.max(.02,hi-lo),low=lo-span*.08,high=hi+span*.08;
  const y=(price:number)=>top+(high-price)/(high-low)*(bottom-top),maxVol=Math.max(1,...shown.map(bar=>bar.volume));
  const x=(index:number)=>left+step*(index+.5),width=Math.min(13,Math.max(2,step*.58));
  return <figure className="paper-daily-chart"><svg viewBox={`0 0 ${w} ${h}`} role="img" aria-label={`截至${shown.at(-1)?.date}的已揭示日K与模拟买卖标记`}>
    <title>按已保存模拟订单查看真实日K</title><desc>红色阳线、绿色阴线。圆点为模拟买入，方块为模拟卖出。只绘制当前播放进度已揭示的日线与成交量，不表示日内价格先后顺序。</desc>
    {Array.from({length:5},(_,index)=>{const price=high-(high-low)*index/4;return <g key={index}><line className="paper-chart-grid" x1={left} x2={right} y1={y(price)} y2={y(price)}/><text x={left-8} y={y(price)+4} textAnchor="end">{number(price)}</text></g>;})}
    {shown.map((bar,index)=><g key={bar.date} data-candle-date={bar.date} className={bar.close>=bar.open?"paper-candle-up":"paper-candle-down"}><title>{`${bar.date} 开${number(bar.open)} 高${number(bar.high)} 低${number(bar.low)} 收${number(bar.close)} 量${number(bar.volume,0)}`}</title><line x1={x(index)} x2={x(index)} y1={y(bar.high)} y2={y(bar.low)}/><rect x={x(index)-width/2} y={Math.min(y(bar.open),y(bar.close))} width={width} height={Math.max(1,Math.abs(y(bar.open)-y(bar.close)))}/><rect opacity=".55" x={x(index)-width/2} y={volBottom-bar.volume/maxVol*40} width={width} height={bar.volume/maxVol*40}/></g>)}
    {marks.map((mark,index)=>{const bx=x(dates.get(mark.date)!),by=y(mark.price),label=mark.side==="buy"?"模拟买入":"模拟卖出";return <g key={`${mark.id}-${index}`} data-marker-order={mark.id}><title>{`${mark.date} ${label} ${number(mark.price)}元 ${number(mark.quantity,0)}股`}</title>{mark.side==="buy"?<circle className="paper-mark-buy" cx={bx} cy={by} r="6"/>:<rect className="paper-mark-sell" x={bx-5} y={by-5} width="10" height="10"/>}<text className="paper-mark-label" x={bx+8} y={by+(index%2?-9:17)}>{mark.side==="buy"?"买":"卖"}</text></g>;})}
    <text x={left} y={h-10}>{shown[0].date}</text><text x={right} y={h-10} textAnchor="end">{shown.at(-1)?.date}</text><text x={left-8} y={volBottom-20} textAnchor="end">成交量</text>
  </svg><figcaption>当前仅显示已揭示的最近{shown.length}根日K · 买入圆点 / 卖出方块 · 每根为一天，不是分时或逐笔行情</figcaption></figure>;
}
function EquityChart({points,label}:{points:Point[];label:string}){
  if(!points.length)return <p className="section-note">暂无可核对的净值快照，不补造收益曲线。</p>;
  const lo=Math.min(...points.map(point=>point.equity)),hi=Math.max(...points.map(point=>point.equity)),span=Math.max(1,hi-lo),min=lo-span*.12,max=hi+span*.12;
  const x=(index:number)=>52+index/Math.max(1,points.length-1)*728,y=(equity:number)=>20+(max-equity)/(max-min)*138;
  return <figure className="paper-equity-curve"><svg viewBox="0 0 800 190" role="img" aria-label={`${label}已保存日快照净值曲线`}><title>{`${label}独立账户净值`}</title><line className="paper-chart-grid" x1="52" x2="780" y1="158" y2="158"/><polyline points={points.map((point,index)=>`${x(index)},${y(point.equity)}`).join(" ")} fill="none" stroke="currentColor" strokeWidth="2"/>{points.map((point,index)=><circle key={`${point.date}-${point.kind}-${index}`} cx={x(index)} cy={y(point.equity)} r="3"><title>{`${point.date} ${point.kind==="opening"?"期初基线":"日快照"} ${money(point.equity)}`}</title></circle>)}<text x="52" y="181">{points[0].date}</text><text x="780" y="181" textAnchor="end">{points.at(-1)?.date}</text><text x="48" y="22" textAnchor="end">{number(max,0)}</text><text x="48" y="158" textAnchor="end">{number(min,0)}</text></svg><figcaption>期初基线＋已保存日快照；不是逐笔净值或盘中最大回撤。</figcaption></figure>;
}

export default function PaperReplay({mode,date,seat,orderId,onSelectOrder,refreshVersion}:Props){
  const [performance,setPerformance]=useState<Performance|null>(null),[playback,setPlayback]=useState<Playback|null>(null);
  const [error,setError]=useState(""),[loading,setLoading]=useState(false),[cursor,setCursor]=useState(0),[playing,setPlaying]=useState(false),[speed,setSpeed]=useState(1000),[revision,setRevision]=useState(0),[startDate,setStartDate]=useState("");
  const current=useRef<Playback|null>(null),identity=useRef("");
  const queryKey=`${mode}:${date}:${seat}:${orderId ?? "default"}`;
  useEffect(()=>{
    if(!DAY.test(date)||!(seat==="all"||seatValid(seat)))return;
    let alive=true;const controller=new AbortController();
    const changed=identity.current!==queryKey;identity.current=queryKey;
    if(changed){setPerformance(null);setPlayback(null);current.current=null;setCursor(0);setPlaying(false);}
    setLoading(true);setError("");
    const parameters=new URLSearchParams({mode,date,seat_id:seat});if(orderId)parameters.set("order_id",String(orderId));
    const stats=new URLSearchParams({mode,date,seat_id:seat});if(startDate)stats.set("start_date",startDate);
    const load=async()=>{
      try{
        const [statsRaw,chartRaw]=await Promise.all([read(`performance?${stats}`,controller.signal),read(`playback?${parameters}`,controller.signal)]);
        if(!alive||controller.signal.aborted||document.hidden)return;
        const nextPerformance=validatePerformance(statsRaw,mode,date,seat),next=validatePlayback(chartRaw,mode,date,seat,orderId);
        const previous=current.current;current.current=next;
        setPerformance(nextPerformance);setPlayback(next);
        if(!previous||previous.selection?.id!==next.selection?.id){
          const entry=next.selection?next.bars.findIndex(bar=>bar.date===next.selection!.date):-1;
          setCursor(next.bars.length?Math.max(1,entry>0?entry-4:1):0);setPlaying(false);
        }else setCursor(value=>Math.min(value,next.bars.length));
      }catch(cause){if(alive&&!controller.signal.aborted)setError(failure(cause));}
      finally{if(alive&&!controller.signal.aborted)setLoading(false);}
    };
    const visibility=()=>{if(document.hidden){controller.abort();setPlaying(false);setLoading(false);}else setRevision(value=>value+1);};
    document.addEventListener("visibilitychange",visibility);if(!document.hidden)void load();else setLoading(false);
    return()=>{alive=false;controller.abort();document.removeEventListener("visibilitychange",visibility);};
  },[mode,date,seat,orderId,refreshVersion,revision,startDate,queryKey]);
  useEffect(()=>{
    if(!playing||!playback||document.hidden||cursor>=playback.bars.length){if(playing)setPlaying(false);return;}
    const timer=window.setTimeout(()=>setCursor(value=>Math.min(value+1,playback.bars.length)),speed);
    return()=>window.clearTimeout(timer);
  },[playing,cursor,speed,playback]);
  const active=playback?.selection,visibleDate=cursor&&playback?playback.bars[cursor-1]?.date:null;
  const visibleMarkers=playback?.markers.filter(mark=>mark.matched_bar&&mark.bar_index!=null&&mark.bar_index<cursor) ?? [];
  const missingMarkers=playback?.markers.filter(mark=>!mark.matched_bar) ?? [];
  function choose(id:string){setPlaying(false);onSelectOrder?.(id?Number(id):null);}
  return <section className="paper-replay-tools" aria-label="日K回放与收益统计">
    <section className="paper-section" aria-labelledby="paper-performance-title"><div className="paper-section-title"><div><p className="eyebrow">完整账本统计 · 不受订单分页限制</p><h2 id="paper-performance-title">席位收益统计</h2></div><button className="secondary" type="button" disabled={loading} onClick={()=>setRevision(value=>value+1)}>刷新复盘资料</button></div>
      <div className="paper-stat-range"><label>统计起始日<input type="date" aria-label="统计起始日" value={startDate} max={date} onChange={event=>{const value=event.target.value;if(!value||DAY.test(value)&&value<=date)setStartDate(value);}}/></label><span>{performance?.start_date || "首次启用"} 至 {date} · 截止日沿用账本日期</span></div>
      {error&&<div className="callout danger" role="alert"><strong>复盘资料未完整载入</strong><p>{error}</p><p>不会重新撮合、补单或用示例收益代替。</p></div>}
      {loading&&!performance&&<p role="status">正在读取已保存的成交与日快照…</p>}
      <div className="paper-performance-grid">{performance?.accounts.map(account=><article className="paper-performance-account" key={account.seat_id}><div className="paper-section-title"><h3>{SEATS[account.seat_id]}</h3><span>数据截至 {account.data_as_of || "无快照"}</span></div>{account.ledger_integrity==="incomplete"&&<p className="callout danger" role="status">账本记录不完整，胜率等交易统计暂不展示；下方明细仅为已核对样本。</p>}<dl className="paper-stat-grid"><div><dt>累计净收益</dt><dd className={tone(account.total_pnl)}>{change(account.total_pnl)}</dd></div><div><dt>累计收益率</dt><dd className={tone(account.total_return_pct)}>{percentage(account.total_return_pct)}</dd></div><div><dt>区间收益</dt><dd className={tone(account.period_pnl)}>{change(account.period_pnl)}</dd></div><div><dt>区间收益率</dt><dd>{percentage(account.period_return_pct)}</dd></div><div><dt>已实现损益</dt><dd>{change(account.realized_pnl)}</dd></div><div><dt>持仓浮盈亏</dt><dd>{change(account.unrealized_pnl)}</dd></div><div><dt>区间费用</dt><dd>{money(account.fees)}</dd></div><div><dt>买入 / 卖出笔数</dt><dd>{number(account.buy_count,0)} / {number(account.sell_count,0)}</dd></div><div><dt>已平仓交易</dt><dd>{number(account.closed_trade_count,0)} 笔</dd></div><div><dt>盈利 / 亏损 / 持平</dt><dd>{number(account.win_count,0)} / {number(account.loss_count,0)} / {number(account.flat_count,0)}</dd></div><div><dt>已平仓胜率</dt><dd>{percentage(account.win_rate_pct)}</dd></div><div><dt>盈亏因子</dt><dd>{number(account.profit_factor)}{account.profit_factor==null&&<small>无亏损分母或样本不足</small>}</dd></div><div><dt>平均盈利 / 亏损</dt><dd>{money(account.average_win)} / {money(account.average_loss)}</dd></div><div><dt>最大单笔盈利 / 亏损</dt><dd>{money(account.max_closed_profit)} / {money(account.max_closed_loss)}</dd></div><div><dt>日快照最大回撤</dt><dd>{percentage(account.max_drawdown_pct)}<small>{account.drawdown_peak_date&&account.drawdown_trough_date?`${account.drawdown_peak_date} → ${account.drawdown_trough_date}`:"需有效快照"}</small></dd></div></dl><EquityChart points={account.equity_curve} label={SEATS[account.seat_id]}/>{account.closed_trade_count===0&&<p className="section-note">尚无已平仓交易，浮盈亏不计入胜率；不能据一天数据评价长期收益。</p>}{account.warnings.length>0&&<details><summary>统计数据缺口</summary><ul>{account.warnings.map((note,index)=><li key={index}>{note}</li>)}</ul></details>}{account.closed_trades.length>0&&<details><summary>已平仓交易明细（最多50笔）</summary><div className="table-scroll"><table><thead><tr><th>股票</th><th>开仓 / 平仓日</th><th>数量</th><th>净损益</th><th>净收益率</th><th>持有自然日</th></tr></thead><tbody>{account.closed_trades.map((trade,index)=><tr key={`${trade.code}-${index}`}><td>{trade.name}<code>{trade.code}</code></td><td>{trade.entry_date} / {trade.exit_date}</td><td>{number(trade.quantity,0)}</td><td className={tone(trade.pnl_net)}>{change(trade.pnl_net)}</td><td>{percentage(trade.return_pct)}</td><td>{number(trade.holding_calendar_days,0)}</td></tr>)}</tbody></table></div></details>}</article>)}</div>
      {performance&&performance.accounts.length===0&&<p className="paper-empty">所选范围尚无可统计账户，不补造收益。</p>}
      {performance?.limitations.length ? <details className="paper-stat-limits"><summary>统计口径与限制</summary><ul>{performance.limitations.map((note,index)=><li key={index}>{note}</li>)}</ul></details>:null}
    </section>
    <section className="paper-section" aria-labelledby="paper-playback-title"><div className="paper-section-title"><div><p className="eyebrow">每笔模拟订单 · 真实日K</p><h2 id="paper-playback-title">交易K线回放</h2></div><span>只读回放，不重新下单</span></div>
      <label className="paper-trade-select">快捷选择交易（前50笔）<select aria-label="回放交易" value={active?.id || ""} disabled={loading||!playback?.trades.length} onChange={event=>choose(event.target.value)}><option value="">选择已保存的交易</option>{active&&!playback?.trades.some(trade=>trade.id===active.id)&&<option value={active.id}>{active.code} {active.name} · 所选账本订单</option>}{playback?.trades.map(trade=><option key={trade.id} value={trade.id}>{trade.date} {trade.code} {trade.name} · {SEATS[trade.seat_id]} · {trade.side==="buy"?"买入":"卖出"}</option>)}</select></label>
      <p className="section-note">共{playback?.trade_total ?? 0}笔可回看交易。也可在下方任意一页账本中点击“K线回放”，不限于快捷列表。逐笔指每一笔模拟订单，并非逐笔行情；当前仅有日K，无法知道日内高低点先后。</p>
      {active?<div className="paper-replay-identity"><strong>{active.name} <code>{active.code}</code></strong><span>{SEATS[active.seat_id]} · {active.date} · {active.side==="buy"?"模拟买入":"模拟卖出"} {number(active.quantity,0)}股 · {number(active.price)}元</span><p>{active.reason}</p></div>:!loading&&<p className="paper-empty">当前范围没有已保存的模拟成交，先运行自动模拟后再回看；不会用虚构交易填充。</p>}
      {playback&&<><div className="paper-replay-controls"><button type="button" className="secondary" aria-label="上一根日K" disabled={cursor<=1||loading} onClick={()=>{setPlaying(false);setCursor(value=>Math.max(1,value-1));}}>上一根</button><button type="button" className="primary" disabled={!playback.bars.length||loading} onClick={()=>{if(cursor>=playback.bars.length)setCursor(1);setPlaying(value=>!value);}}>{playing?"暂停播放":"播放日K"}</button><button type="button" className="secondary" aria-label="下一根日K" disabled={cursor>=playback.bars.length||loading} onClick={()=>{setPlaying(false);setCursor(value=>Math.min(playback.bars.length,value+1));}}>下一根</button><label>速度<select aria-label="日K播放速度" value={speed} onChange={event=>setSpeed(Number(event.target.value))}><option value={1500}>慢速</option><option value={1000}>标准</option><option value={400}>快速</option></select></label><button className="secondary" type="button" disabled={!playback.bars.length||loading} onClick={()=>{setPlaying(false);setCursor(playback.bars.length);}}>显示全部日K</button><span aria-live="polite">已揭示 {cursor} / {playback.bars.length} 根 · {visibleDate || "暂无日K"}</span></div><input className="paper-replay-progress" type="range" aria-label="日K回放进度" min={playback.bars.length?1:0} max={playback.bars.length} value={cursor} disabled={!playback.bars.length||loading} onChange={event=>{setPlaying(false);setCursor(Number(event.target.value));}}/>
      <DailyReplayChart bars={playback.bars} markers={playback.markers} cursor={cursor}/>
      <p className="section-note">已揭示买卖标记 {visibleMarkers.length} 个；{playback.bars.some(bar=>bar.complete===false)?"包含采集时仍在形成的日K快照，不是官方收盘日K。":"数据为已保存的日线或收盘报价快照，不表示官方成交核验。"}</p>
      {missingMarkers.length>0&&<p className="callout">有{missingMarkers.length}笔模拟交易缺少对应日期的已保存K线，只保留账本事实，不挪用旧日K线绘制标记。</p>}
      {playback.limitations.length>0&&<details className="paper-stat-limits"><summary>K线来源与回放限制</summary><ul>{playback.limitations.map((note,index)=><li key={index}>{note}</li>)}</ul>{playback.source_hash&&<code className="paper-hash">资料指纹 {playback.source_hash}</code>}</details>}</>}
    </section>
  </section>;
}

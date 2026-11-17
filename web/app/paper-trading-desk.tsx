"use client";

import { useEffect, useRef, useState } from "react";
import PaperReplay from "./paper-replay";
import "./paper-trading.css";

type PaperMode = "historical" | "live";
type SeatId = "baseline" | "livermore" | "buffett";
type View = "research" | "replay" | "audit";
type Account = {
  seat_id: SeatId; label: string; initial_cash?: number; cash?: number; market_value?: number; equity?: number;
  daily_pnl?: number; daily_return_pct?: number; total_pnl?: number; total_return_pct?: number;
  realized_pnl?: number; unrealized_pnl?: number; buy_count?: number; sell_count?: number;
  position_count?: number; status?: string; missing?: string[]; counts?: Record<string,number>;
};
type Order = {
  id: string | number; seat_id: SeatId; code: string; name: string; side: "buy" | "sell";
  timestamp: string; date: string; price: number; quantity: number; amount: number; fees: number;
  reason: string; execution_mode?: string; source_hash?: string;
};
type Position = {
  seat_id: SeatId; code: string; name: string; quantity: number; available_quantity?: number;
  entry_date?: string; cost_price?: number; mark_price?: number; market_value?: number;
  unrealized_pnl?: number; return_pct?: number; updated_at?: string;
};
type Decision = {seat_id: SeatId; code: string; name: string; action: string; status?: string; reason: string; missing?: string[]; timestamp?: string};
type BuffettData = {
  version:"buffett-data-integration/1";scope:"whole_universe";decision_date:string;
  status:"partial_verified"|"reference_only"|"unavailable";
  reference_stocks:number;reference_year_rows:number;reference_complete_stocks:number;
  verified_stocks:number;verified_report_count:number;verified_field_count:number;strict_pit_stocks:number;
  manual_confirmation_available:boolean;remaining:string[];limitations:string[];
  samples:{code:string;name?:string;report_count:number;field_count:number;pit_verified:boolean;missing_fields:string[]}[];
};
type LivermoreData = {
  version:"livermore-data-integration/1";scope:"whole_universe";decision_date:string;execution_date:string;
  status:"partial_connected"|"reference_only"|"unavailable";
  universe_stocks:number;history_120_stocks:number;low_position_stocks:number;source_limit_up_event_stocks:number;
  verified_limit_stocks:number;verified_security_status_stocks:number;verified_stock_auction_stocks:number;
  verified_index_auction_stocks:number;regional_confirmation_stocks:number;candidate_stocks:number;execution_ready_stocks:number;
  remaining:string[];limitations:string[];
  samples:{code:string;name?:string;position_120_pct:number|null;limit_up_confirmed:boolean|null;limit_price_verified:boolean;auction_verified:boolean;missing_fields:string[]}[];
};
type Dashboard = {
  mode: PaperMode; trading_date: string; calendar_date: string; simulation: true;
  engine: {status: string; started_at?: string; last_run?: string; progress?: {processed?: number;total?: number};message?: string};
  accounts: Account[]; orders: {items: Order[];total: number;limit?: number;offset?: number};
  positions: Position[]; decisions: {items: Decision[];total: number}; notice?: string[];
  buffett_data?:BuffettData;buffett_data_error?:string;
  livermore_data?:LivermoreData;livermore_data_error?:string;
};
type Query = {mode: PaperMode; date: string; seat: string;offset: number};

const SEATS: Record<SeatId, string> = {baseline: "基础策略席位",livermore: "利弗莫尔席位",buffett: "巴菲特席位"};
const STATUS: Record<string,string> = {idle:"尚未启用",ready:"准备就绪",running:"自主运行中",paused:"已暂停",completed:"本轮已完成",waiting:"等待交易时段",blocked:"条件受限",failed:"运行失败",error:"运行失败",stopped:"已停止",interrupted:"运行已中断"};
const ACTION: Record<string,string> = {buy:"模拟买入",sell:"模拟卖出",simulate:"条件预案",hold:"观望",blocked:"风控拦截",skip:"跳过",rejected:"参数未通过",filled:"模拟成交",position_cap:"已达持仓上限",already_held:"已有持仓，不重复买入",cash_rejected:"可用资金不足",price_rejected:"价格不符，未成交",data_rejected:"数据校验未通过",duplicate:"重复请求，已忽略",not_running:"模拟未运行",invalid_data:"数据无效",invalid_quote:"报价无效",closed_or_stale_quote:"休市或报价已过期",not_running_or_before_start:"未启用或报价早于启用",quote_not_executable:"报价不满足撮合条件",one_price_or_invalid_range:"一字价格或价格区间异常",invalid_volume:"成交量无效"};
const actionLabel=(value?:string)=>value?ACTION[value] || "其他状态":"—";
const EXECUTION:Record<string,string>={historical_open_estimate:"历史开盘价估算",historical_daily_assumed:"历史日线估算",live_last_quote_estimate:"后续有效报价估算",live_ask_quote_estimate:"后续卖一报价估算"};
const executionLabel=(value?:string)=>EXECUTION[value || ""] || "模拟撮合估算";
const DAY = /^\d{4}-\d{2}-\d{2}$/;
const CODE = /^(?:000|001|002|003|600|601|603|605)\d{3}$/;
const number = (value: number | undefined, digits = 2) => typeof value === "number" && Number.isFinite(value)
  ? value.toLocaleString("zh-CN",{minimumFractionDigits:digits,maximumFractionDigits:digits}) : "—";
const money = (value: number | undefined) => `${number(value)}${value == null ? "" : " 元"}`;
const signed = (value: number | undefined, suffix = " 元") => typeof value === "number" && Number.isFinite(value)
  ? `${value > 0 ? "+" : ""}${number(value)}${suffix}` : "—";
const tone = (value?: number) => value == null || value === 0 ? "" : value > 0 ? "rise" : "fall";
const dateTime = (value?: string) => {
  if(!value)return "—";
  const parsed=new Date(value);
  return Number.isFinite(parsed.getTime()) ? parsed.toLocaleString("zh-CN",{timeZone:"Asia/Shanghai",hour12:false}) : value;
};
const validSeat = (value: unknown): value is SeatId => typeof value === "string" && Object.hasOwn(SEATS,value);
const message = (error: unknown) => error instanceof Error ? error.message : "模拟交易服务暂不可用。";
const FINANCIAL_FIELDS:Record<string,string>={operating_cashflow:"经营现金流",maintenance_capex:"维持性资本开支",net_profit:"归母净利润",roe_pct:"加权平均ROE",debt_to_equity:"负债 / 权益比",shares_outstanding:"同期总股数",reported_capex:"报表总资本开支",total_liabilities:"总负债",total_equity:"总权益",point_in_time:"历史版本核验",within_circle:"能力圈确认",moat_supported:"护城河证据"};
const financialField=(value:string)=>FINANCIAL_FIELDS[value] || (/^[a-z0-9_]+$/i.test(value)?"其他待核对项目":value);
const LIVERMORE_FIELDS:Record<string,string>={history_120:"120日历史行情",history_120_bars:"120日历史行情",price_history:"历史价量资料",low_position:"120日低位门槛",limit_up:"历史涨停价",limit_down:"历史跌停价",limit_up_confirmed:"历史封板确认",limit_prices:"精确涨跌停价",limit_price:"精确涨跌停价",precise_limits:"精确涨跌停价",security_status:"历史证券状态",historical_status:"历史证券状态",stock_auction:"个股真实竞价",opening_auction:"开盘竞价",auction_amount:"竞价成交额",auction_volume_shares:"竞价成交股数",index_auction:"指数真实竞价",regional_confirmation:"区域确认",region_confirmation:"区域确认",regional_ok:"区域确认",entry_execution:"执行条件",auction_execution:"竞价执行条件",execution_policy:"撮合与执行规则"};
const livermoreField=(value:string)=>Object.hasOwn(LIVERMORE_FIELDS,value)?LIVERMORE_FIELDS[value]:(/[\u3400-\u9fff]/.test(value)?value:"其他待核对项目");

function validLivermoreIntegration(value:LivermoreData,calendarDate:string):boolean {
  const count=(n:unknown)=>typeof n==="number"&&Number.isSafeInteger(n)&&n>=0&&n<=6000;
  const notes=(rows:unknown)=>Array.isArray(rows)&&rows.length<=30&&rows.every(row=>typeof row==="string"&&row.length<=1000);
  const day=(date:unknown)=>typeof date==="string"&&DAY.test(date)&&Number.isFinite(Date.parse(`${date}T00:00:00Z`))&&new Date(`${date}T00:00:00Z`).toISOString().slice(0,10)===date;
  return !!value&&value.version==="livermore-data-integration/1"&&value.scope==="whole_universe"
    &&day(value.decision_date)&&day(value.execution_date)&&day(calendarDate)
    &&value.decision_date<value.execution_date&&value.execution_date<=calendarDate
    &&["partial_connected","reference_only","unavailable"].includes(value.status)
    &&[value.universe_stocks,value.history_120_stocks,value.low_position_stocks,value.source_limit_up_event_stocks,
      value.verified_limit_stocks,value.verified_security_status_stocks,value.verified_stock_auction_stocks,
      value.verified_index_auction_stocks,value.regional_confirmation_stocks,value.candidate_stocks,value.execution_ready_stocks].every(count)
    &&notes(value.remaining)&&notes(value.limitations)&&Array.isArray(value.samples)&&value.samples.length<=20
    &&value.samples.every(row=>row&&typeof row.code==="string"&&CODE.test(row.code)&&(row.name===undefined||typeof row.name==="string"&&row.name.length<=100)
      &&(row.position_120_pct===null||typeof row.position_120_pct==="number"&&Number.isFinite(row.position_120_pct)&&row.position_120_pct>=0&&row.position_120_pct<=100)
      &&(row.limit_up_confirmed===null||typeof row.limit_up_confirmed==="boolean")&&typeof row.limit_price_verified==="boolean"
      &&typeof row.auction_verified==="boolean"&&notes(row.missing_fields));
}

function validIntegration(value:BuffettData):boolean {
  const count=(n:unknown,max=1000000)=>typeof n==="number"&&Number.isSafeInteger(n)&&n>=0&&n<=max;
  const notes=(rows:unknown,max:number)=>Array.isArray(rows)&&rows.length<=max&&rows.every(row=>typeof row==="string"&&row.length<=1000);
  return !!value&&value.version==="buffett-data-integration/1"&&value.scope==="whole_universe"&&DAY.test(value.decision_date)
    &&["partial_verified","reference_only","unavailable"].includes(value.status)
    &&[value.reference_stocks,value.reference_complete_stocks,value.verified_stocks,value.strict_pit_stocks].every(n=>count(n,6000))
    &&count(value.reference_year_rows,30000)&&count(value.verified_report_count,30000)&&count(value.verified_field_count,270000)
    &&value.reference_complete_stocks<=value.reference_stocks&&value.strict_pit_stocks<=value.verified_stocks
    &&typeof value.manual_confirmation_available==="boolean"&&notes(value.remaining,20)&&notes(value.limitations,20)
    &&Array.isArray(value.samples)&&value.samples.length<=20
    &&value.samples.every(row=>row&&CODE.test(row.code)&&(row.name===undefined||typeof row.name==="string"&&row.name.length<=100)
      &&count(row.report_count,5)&&count(row.field_count,45)&&typeof row.pit_verified==="boolean"&&notes(row.missing_fields,20));
}

async function request(path: string, signal: AbortSignal, body?: object): Promise<unknown> {
  const response=await fetch(`/contest-api/paper/${path}`,{
    method:body === undefined ? "GET" : "POST",credentials:"same-origin",redirect:"error",cache:"no-store",signal,
    headers:body === undefined ? undefined : {"Content-Type":"application/json"},
    body:body === undefined ? undefined : JSON.stringify(body),
  });
  const value=await response.json().catch(()=>null);
  if(response.status===401 && !signal.aborted)window.dispatchEvent(new Event("contest-session-expired"));
  if(!response.ok)throw new Error(typeof value?.detail === "string" ? value.detail : `模拟交易请求失败（HTTP ${response.status}）。`);
  return value;
}

function validate(value: unknown, query: Query): Dashboard {
  const data=value as Dashboard;
  if(!data || data.simulation!==true || data.mode!==query.mode || !DAY.test(data.trading_date) || !DAY.test(data.calendar_date)
    || query.date && data.trading_date!==query.date || !data.engine || typeof data.engine.status!=="string"
    || !Array.isArray(data.accounts) || data.accounts.length>3 || !Array.isArray(data.positions) || data.positions.length>30
    || !Array.isArray(data.orders?.items) || data.orders.items.length>20 || !Number.isSafeInteger(data.orders.total) || data.orders.total<0
    || !Array.isArray(data.decisions?.items) || data.decisions.items.length>100 || !Number.isSafeInteger(data.decisions.total) || data.decisions.total<0
    || data.accounts.some(row=>!validSeat(row.seat_id)) || data.orders.items.some(row=>!validSeat(row.seat_id) || !CODE.test(row.code) || !["buy","sell"].includes(row.side) || row.date!==data.trading_date)
    || data.positions.some(row=>!validSeat(row.seat_id) || !CODE.test(row.code)) || data.decisions.items.some(row=>!validSeat(row.seat_id) || !CODE.test(row.code))) {
    throw new Error("模拟账本的数据、日期或股票范围校验未通过，未用示例交易替代。");
  }
  let result=data;
  if(data.buffett_data!==undefined&&!validIntegration(data.buffett_data))result={...result,buffett_data:undefined,buffett_data_error:"巴菲特数据接入摘要校验未通过，已隐藏该摘要；原交易账本不受影响。"};
  if(data.livermore_data!==undefined&&!validLivermoreIntegration(data.livermore_data,data.calendar_date))result={...result,livermore_data:undefined,livermore_data_error:"利弗莫尔数据接入摘要的字段或日期校验未通过，已隐藏该摘要；原交易账本不受影响。"};
  return result;
}

function Accounts({accounts,period}: {accounts: Account[];period: string}) {
  return <div className="paper-accounts" aria-label="三个席位的独立模拟账户">
    {(["baseline","livermore","buffett"] as SeatId[]).map(id=>{
      const account=accounts.find(row=>row.seat_id===id);
      return <article className="paper-account" key={id}>
        <div className="paper-account-head"><h3>{SEATS[id]}</h3><span className="pill">{account ? STATUS[account.status || ""] || "独立账户" : "等待创建"}</span></div>
        <p className="paper-account-label">虚拟总资产</p><strong className="paper-account-equity">{money(account?.equity)}</strong>
        <div className="paper-account-pnl"><span>{period}盈亏</span><strong className={tone(account?.daily_pnl)}>{signed(account?.daily_pnl)}</strong><small className={tone(account?.daily_return_pct)}>{signed(account?.daily_return_pct,"%")}</small></div>
        <dl>
          <div><dt>可用现金</dt><dd>{money(account?.cash)}</dd></div><div><dt>持仓市值</dt><dd>{money(account?.market_value)}</dd></div>
          <div><dt>累计盈亏</dt><dd className={tone(account?.total_pnl)}>{signed(account?.total_pnl)}</dd></div><div><dt>累计收益率</dt><dd className={tone(account?.total_return_pct)}>{signed(account?.total_return_pct,"%")}</dd></div>
          <div><dt>已实现损益</dt><dd className={tone(account?.realized_pnl)}>{signed(account?.realized_pnl)}</dd></div><div><dt>持仓浮盈亏</dt><dd className={tone(account?.unrealized_pnl)}>{signed(account?.unrealized_pnl)}</dd></div>
        </dl>
        <div className="paper-account-counts"><span>买入 <b>{number(account?.buy_count,0)}</b> 笔</span><span>卖出 <b>{number(account?.sell_count,0)}</b> 笔</span><span>持仓 <b>{number(account?.position_count,0)}</b> 只</span></div>
        {(account?.missing?.length ?? 0)>0 && <details className="paper-missing"><summary>查看本席位未成交或受限原因</summary><ul>{account?.missing?.slice(0,10).map((reason,index)=><li key={index}>{reason}</li>)}</ul></details>}
        <p className="paper-initial">初始虚拟资金 {money(account?.initial_cash)} · 与其他席位独立</p>
      </article>;
    })}
  </div>;
}

function AuditOverview({data,seat}: {data:Dashboard;seat:string}) {
  const accounts=data.accounts.filter(row=>seat==="all" || row.seat_id===seat);
  const complete=accounts.length>0 && accounts.every(row=>row.counts && Object.values(row.counts).every(value=>Number.isSafeInteger(value)&&value>=0));
  const count=(kind?:string)=>complete?accounts.reduce((sum,row)=>sum+Object.entries(row.counts!).reduce((total,[key,value])=>total+(!kind || key===kind?value:0),0),0):undefined;
  const outcomes=complete?Array.from(new Set(accounts.flatMap(row=>Object.keys(row.counts!)))).map(kind=>({kind,count:count(kind)!})).filter(row=>row.count>0).sort((a,b)=>b.count-a.count):[];
  return <section className="paper-audit-overview" aria-label="自主决策审计概览" aria-labelledby="paper-audit-overview-title">
    <div className="paper-section-title"><div><p className="eyebrow">当日累计计数 · 只读</p><h2 id="paper-audit-overview-title">决策审计概览</h2></div><span>核对为什么买、为什么没买</span></div>
    <dl className="paper-audit-metrics"><div><dt>累计判断次数</dt><dd>{number(count(),0)}</dd></div><div><dt>观望次数</dt><dd>{number(count("hold"),0)}</dd></div><div><dt>模拟买入记录</dt><dd>{number(count("buy"),0)}</dd></div><div><dt>模拟卖出记录</dt><dd>{number(count("sell"),0)}</dd></div></dl>
    {outcomes.length>0 && <div className="paper-audit-outcomes" aria-label="全部已保存判断的结果分布">{outcomes.map(row=><span key={row.kind}>{actionLabel(row.kind)} <b>{number(row.count,0)}</b></span>)}</div>}
    <p className="section-note">计数来自已保存的当日记录，不等于股票去重数量；下方原因明细为有界样本，不代表每次判断都有明细。{!complete && "当前缺少可核对的累计计数，未用样本数量代替。"}</p>
    <details className="paper-audit-runtime"><summary>查看运行状态与审计范围</summary><p>{data.engine.message || "未保存运行说明。"}</p><p>最近更新：{dateTime(data.engine.last_run)} · 这里只查看记录；启用或暂停请到研究台。</p></details>
  </section>;
}

function BuffettIntegration({data}:{data:BuffettData}) {
  const status={partial_verified:"部分原文已核对",reference_only:"参考已接入，待核验",unavailable:"资料暂不可读"}[data.status];
  return <section className="paper-buffett-integration" aria-label="巴菲特全市场数据接入">
    <div className="paper-section-title"><div><p className="eyebrow">全市场扫描共用 · 只读数据状态</p><h2>巴菲特全市场数据接入</h2></div><span>{status} · 证据截止 {data.decision_date}</span></div>
    <dl className="paper-buffett-metrics"><div><dt>公开参考覆盖</dt><dd>{number(data.reference_stocks,0)} 只<small>{number(data.reference_year_rows,0)} 条年度记录</small></dd></div><div><dt>三年参考完整</dt><dd>{number(data.reference_complete_stocks,0)} 只<small>不等于策略条件通过</small></dd></div><div><dt>原文数值核对</dt><dd>{number(data.verified_stocks,0)} 只<small>{number(data.verified_report_count,0)} 个年度 · {number(data.verified_field_count,0)} 项字段</small></dd></div><div><dt>严格时点核验</dt><dd>{number(data.strict_pit_stocks,0)} 只<small>仍需逐项满足买入纪律</small></dd></div></dl>
    <p className="paper-buffett-note">这批资料用于巴菲特席位的全市场判断，不仅用于单股查看。原文数值核对不等于历史更正版本审查或买入放行；{data.manual_confirmation_available?"人工确认仍独立校验。":"能力圈和护城河尚无人工确认，不会自动勾选。"}本页不会启动核验、重跑订单或下单。</p>
    {data.remaining.length>0&&<div className="paper-buffett-remaining"><strong>仍需补齐 / 核对</strong><ul>{data.remaining.slice(0,12).map((note,index)=><li key={index}>{financialField(note)}</li>)}</ul></div>}
    {data.samples.length>0&&<details className="paper-buffett-samples"><summary>查看已核对资料样本（最多10只，不是买入名单）</summary><div className="table-scroll"><table><thead><tr><th>股票</th><th>年度 / 字段数</th><th>历史版本</th><th>剩余项目</th></tr></thead><tbody>{data.samples.slice(0,10).map((row,index)=><tr key={`${row.code}-${index}`}><td>{row.name || "已核对股票"}<code>{row.code}</code></td><td>{number(row.report_count,0)} / {number(row.field_count,0)}</td><td>{row.pit_verified?"时点核验完成":"更正版本仍待核验"}</td><td>{row.missing_fields.length?row.missing_fields.slice(0,12).map(financialField).join("、"):"未列出缺项，仍需策略校验"}</td></tr>)}</tbody></table></div></details>}
    {data.limitations.length>0&&<details className="paper-buffett-samples"><summary>数据使用边界</summary><ul>{data.limitations.slice(0,12).map((note,index)=><li key={index}>{note}</li>)}</ul></details>}
  </section>;
}

function LivermoreIntegration({data}:{data:LivermoreData}) {
  const status={partial_connected:"部分资料已接入",reference_only:"参考资料，待核验",unavailable:"资料暂不可读"}[data.status];
  return <section className="paper-buffett-integration paper-livermore-integration" aria-label="利弗莫尔全市场数据接入">
    <div className="paper-section-title"><div><p className="eyebrow">全市场扫描共用 · 只读数据状态</p><h2>利弗莫尔全市场数据接入</h2></div><span>{status} · 决策日 {data.decision_date} · 执行日 {data.execution_date}</span></div>
    <dl className="paper-buffett-metrics"><div><dt>120日日线覆盖</dt><dd>{number(data.history_120_stocks,0)} 只<small>股票范围 {number(data.universe_stocks,0)} 只</small></dd></div><div><dt>低位位置门槛</dt><dd>{number(data.low_position_stocks,0)} 只<small>不代表其余条件已通过</small></dd></div><div><dt>源标注涨停参考</dt><dd>{number(data.source_limit_up_event_stocks,0)} 只<small>非已核验精确限价</small></dd></div><div><dt>个股真实竞价已接入</dt><dd>{number(data.verified_stock_auction_stocks,0)} 只<small>日线成交量不能替代竞价</small></dd></div></dl>
    <dl className="paper-livermore-status-line"><div><dt>精确限价已核验</dt><dd>{number(data.verified_limit_stocks,0)} 只</dd></div><div><dt>历史证券状态已核验</dt><dd>{number(data.verified_security_status_stocks,0)} 只</dd></div><div><dt>指数竞价已接入</dt><dd>{number(data.verified_index_auction_stocks,0)} 只</dd></div><div><dt>区域确认</dt><dd>{number(data.regional_confirmation_stocks,0)} 只</dd></div><div><dt>结构候选</dt><dd>{number(data.candidate_stocks,0)} 只</dd></div><div><dt>执行证据齐备</dt><dd>{number(data.execution_ready_stocks,0)} 只</dd></div></dl>
    <p className="paper-buffett-note">源标注涨停仅为参考，不等于已核验精确涨停价；不得以涨幅达到10%代替封板确认。日线总量不能替代真实开盘竞价，当前行情不回填历史。数据接入和执行证据齐备均不等于买入放行，仍须通过策略与风控。本区只读，不启动下载、核验、模型调用或模拟交易。</p>
    {data.remaining.length>0&&<div className="paper-buffett-remaining"><strong>仍需补齐 / 核对</strong><ul>{data.remaining.slice(0,12).map((note,index)=><li key={index}>{livermoreField(note)}</li>)}</ul></div>}
    {data.samples.length>0&&<details className="paper-buffett-samples"><summary>查看价量证据样本（最多10只，不是买入名单）</summary><div className="table-scroll"><table><thead><tr><th>股票</th><th>120日位置</th><th>封板确认</th><th>精确限价 / 真实竞价</th><th>剩余项目</th></tr></thead><tbody>{data.samples.slice(0,10).map((row,index)=><tr key={`${row.code}-${index}`}><td>{row.name || "证据样本"}<code>{row.code}</code></td><td>{row.position_120_pct===null?"待补齐":`${number(row.position_120_pct)}%`}</td><td>{row.limit_up_confirmed===null?"尚未核验":row.limit_up_confirmed?"已核对封板":"未封涨停"}</td><td>{row.limit_price_verified?"限价已核验":"限价待核验"} / {row.auction_verified?"竞价已核验":"竞价待补齐"}</td><td>{row.missing_fields.length?row.missing_fields.slice(0,12).map(livermoreField).join("、"):"未列出缺项，仍需策略校验"}</td></tr>)}</tbody></table></div></details>}
    {data.limitations.length>0&&<details className="paper-buffett-samples"><summary>利弗莫尔数据使用边界</summary><ul>{data.limitations.slice(0,12).map((note,index)=><li key={index}>{livermoreField(note)}</li>)}</ul></details>}
  </section>;
}

export default function PaperTradingDesk({view}: {view: View}) {
  const [query,setQuery]=useState<Query>({mode:"historical",date:"",seat:"all",offset:0});
  const [data,setData]=useState<Dashboard | null>(null);
  const [error,setError]=useState("");
  const [pending,setPending]=useState<"start"|"stop"|null>(null);
  const [refresh,setRefresh]=useState(0);
  const [replayOrderId,setReplayOrderId]=useState<number|null>(null);
  const mutation=useRef<AbortController|null>(null);
  const generation=useRef(0);

  useEffect(()=>{
    const current=++generation.current;
    let alive=true,timer:number|null=null,controller:AbortController|null=null;
    const schedule=(delay:number)=>{if(alive && !document.hidden){if(timer!==null)window.clearTimeout(timer);timer=window.setTimeout(()=>{timer=null;void load();},delay);}};
    const load=async()=>{
      if(!alive || document.hidden)return;
      if(mutation.current){schedule(2000);return;}
      controller?.abort();controller=new AbortController();const active=controller;
      const parameters=new URLSearchParams({mode:query.mode,seat_id:query.seat,limit:"20",offset:String(query.offset)});
      if(query.date)parameters.set("date",query.date);
      let interval=15000;
      try{
        const result=validate(await request(`dashboard?${parameters}`,active.signal),query);
        if(!alive || active.signal.aborted || current!==generation.current)return;
        setData(result);setError("");interval=result.engine.status==="running"?2000:15000;
      }catch(cause){if(alive && !active.signal.aborted && current===generation.current)setError(message(cause));}
      finally{if(controller===active)controller=null;schedule(interval);}
    };
    const visibility=()=>{
      if(document.hidden){if(timer!==null)window.clearTimeout(timer);timer=null;controller?.abort();mutation.current?.abort();}
      else {if(timer!==null)window.clearTimeout(timer);timer=null;void load();}
    };
    document.addEventListener("visibilitychange",visibility);void load();
    return()=>{alive=false;controller?.abort();if(timer!==null)window.clearTimeout(timer);document.removeEventListener("visibilitychange",visibility);};
  },[query.mode,query.date,query.seat,query.offset,refresh]);
  useEffect(()=>()=>{mutation.current?.abort();mutation.current=null;},[]);

  function select(next: Partial<Query>) {
    if(mutation.current)return;
    const updated={...query,...next,offset:next.offset ?? 0};
    if(updated.mode===query.mode && updated.date===query.date && updated.seat===query.seat && updated.offset===query.offset)return;
    setQuery(updated);
    if(updated.mode!==query.mode || updated.date!==query.date || updated.seat!==query.seat)setReplayOrderId(null);
    // Never paint an older mode/date's P&L under the newly selected heading.
    setData(null);setError("");
  }
  async function command(action:"start"|"stop") {
    if(view!=="research" || mutation.current || document.hidden)return;
    const controller=new AbortController();mutation.current=controller;setPending(action);setError("");
    try{
      await request(action,controller.signal,{mode:query.mode});
      if(!controller.signal.aborted)setRefresh(value=>value+1);
    }catch(cause){if(!controller.signal.aborted)setError(message(cause));}
    finally{if(mutation.current===controller){mutation.current=null;setPending(null);}}
  }
  const historical=query.mode==="historical";
  const period=historical?"模拟当日":"当日";
  const status=data?.engine.status || "idle";
  const running=["running","waiting"].includes(status);
  const stoppable=running || status==="blocked";
  const progress=data?.engine.progress;
  const finished=historical && status==="completed";
  const buyCount=data?.accounts.reduce((sum,row)=>sum+(row.buy_count || 0),0) || 0;
  const sellCount=data?.accounts.reduce((sum,row)=>sum+(row.sell_count || 0),0) || 0;
  const positionCount=data?.accounts.reduce((sum,row)=>sum+(row.position_count || 0),0) || 0;
  const title=view==="replay"?"交易账本与持仓":view==="audit"?"自主决策审计":"自主模拟交易";
  const description=view==="replay"?"回看已保存的买卖、日K与收益；只读复盘，不重新选股或下单。":view==="audit"?"核对席位的决定、风控限制和未成交原因；这里只读记录，不执行交易。":"系统按席位规则自主扫描、决定和记录模拟买卖，不需要逐只股票操作。";

  return <section className={`paper-desk paper-desk-${view}`} aria-label={view==="research"?"自主模拟交易工作区":view==="replay"?"模拟交易复盘工作区":"自主决策审计工作区"} data-workbench={view}>
    <header className="paper-title"><div><p className="eyebrow">{view==="research"?"独立虚拟账户 · 不连接券商":view==="replay"?"复盘台 · 已保存交易与收益":"审计台 · 决策与风控记录"}</p><h1 id="page-title">{title}</h1><p>{description}</p></div><span className={`paper-engine-status ${status}`} role="status">{STATUS[status] || "其他运行状态"}</span></header>
    <div className="paper-controls">
      <div className="paper-mode-switch" role="group" aria-label="模拟运行模式"><button type="button" aria-pressed={historical} disabled={pending!==null} onClick={()=>select({mode:"historical",date:""})}>历史数据演示</button><button type="button" aria-pressed={!historical} disabled={pending!==null} onClick={()=>select({mode:"live",date:""})}>实时自动模拟</button></div>
      <div className="paper-actions">{view==="research" && <><button className="primary" type="button" disabled={pending!==null || running || finished || !data} onClick={()=>void command("start")}>{pending==="start"?"正在启用…":finished?"本轮已运行，账本已保存":running?"自动模拟已启用":status==="paused"?"继续自主运行":"启用自动模拟"}</button><button className="secondary" type="button" disabled={pending!==null || !stoppable} onClick={()=>void command("stop")}>{pending==="stop"?"正在暂停…":"暂停运行"}</button></>}<button className="secondary" type="button" disabled={pending!==null} onClick={()=>setRefresh(value=>value+1)}>{view==="audit"?"刷新审计记录":view==="replay"?"刷新复盘账本":"刷新账本"}</button></div>
    </div>
    <div className="paper-date-line"><strong>{historical?"历史模拟交易日":"账本交易日"}　{data?.trading_date || "读取中…"}</strong><span>实际日历日期　{data?.calendar_date || "—"}</span><span>{historical?"历史记录不冒充今日实时交易":"从启用时开始记录，不追补过去成交"}</span></div>
    {error && <div className="callout danger" role="alert"><strong>模拟交易请求未完成</strong><p>{error}</p><p>已显示的账本保留；错误不会转换成示例交易或虚构收益。</p></div>}
    {view==="research" && <><div className="paper-engine-line" aria-live="polite"><p>{data?.engine.message || (data?"启用后自主扫描整个可用股票范围；重复查看不会下单。":"正在读取本机模拟账户与账本…")}</p>{progress && <span>已扫描 {number(progress.processed,0)} / {number(progress.total,0)} 只</span>}</div>
    {data?.engine.started_at && <p className="paper-runtime">启用于 {dateTime(data.engine.started_at)}{data.engine.last_run && <> · 最近运行 {dateTime(data.engine.last_run)}</>} · 北京时间</p>}
    {data && <Accounts accounts={data.accounts} period={period} />}</>}
    {!data && !error && <div className="paper-loading" role="status">读取独立账户中，不填入虚构资产或交易。</div>}

    <div className="paper-ledger-filters"><label>查看席位<select aria-label="账本席位" value={query.seat} disabled={pending!==null} onChange={event=>select({seat:event.target.value})}><option value="all">全部席位</option>{Object.entries(SEATS).map(([id,label])=><option key={id} value={id}>{label}</option>)}</select></label><label>账本日期<input aria-label="账本日期" type="date" value={query.date || data?.trading_date || ""} disabled={pending!==null || historical} onChange={event=>{if(!event.target.value || DAY.test(event.target.value))select({date:event.target.value});}} /></label><p>账户资金相互独立，不将三席位资产相加冒充单个账户。</p></div>

    {view==="replay" && data && <PaperReplay key={`${query.mode}:${data.trading_date}:${query.seat}`} mode={query.mode} date={data.trading_date} seat={query.seat} orderId={replayOrderId} onSelectOrder={setReplayOrderId} refreshVersion={`${refresh}:${data.orders.total}:${data.engine.status}`} />}
    {view==="audit" && data && <AuditOverview data={data} seat={query.seat} />}
    {view!=="replay"&&data?.buffett_data&&<BuffettIntegration data={data.buffett_data}/>}
    {view!=="replay"&&data?.buffett_data_error&&<p className="callout" role="status">{data.buffett_data_error}</p>}
    {view!=="replay"&&data?.livermore_data&&<LivermoreIntegration data={data.livermore_data}/>}
    {view!=="replay"&&data?.livermore_data_error&&<p className="callout" role="status">{data.livermore_data_error}</p>}

    {view!=="audit" && <>
      <section className="paper-section" aria-labelledby="paper-ledger-title"><div className="paper-section-title"><h2 id="paper-ledger-title">{historical?"模拟交易日账本":"交易日账本"}</h2><span>买入 {buyCount} 笔 · 卖出 {sellCount} 笔 · 持仓 {positionCount} 只</span></div>
        <p className="paper-execution-note">以下均为虚拟账户的模拟成交；历史成交采用明确的撮合假设，不是券商订单或真实成交证明。</p>
        {data?.orders.items.length ? <div className="table-scroll" tabIndex={0} role="region" aria-label="自动模拟买卖账本"><table className="paper-order-table"><thead><tr><th>股票名称 / 代码</th><th>席位</th><th>时间（北京时间）</th><th>方向</th><th>成交价</th><th>数量</th><th>成交金额</th><th>费用</th><th>决策依据</th>{view==="replay"&&<th>逐笔回看</th>}</tr></thead><tbody>{data.orders.items.map(order=><tr key={order.id}><td><strong>{order.name}</strong><code>{order.code}</code></td><td>{SEATS[order.seat_id]}</td><td>{dateTime(order.timestamp)}</td><td className={order.side==="buy"?"rise":"fall"}>{order.side==="buy"?"模拟买入":"模拟卖出"}</td><td>{number(order.price)}</td><td>{number(order.quantity,0)} 股</td><td>{money(order.amount)}</td><td>{money(order.fees)}</td><td><details><summary>查看原因</summary><p>{order.reason}</p><p>撮合方式：{executionLabel(order.execution_mode)}</p>{order.source_hash && <code className="paper-hash">行情指纹 {order.source_hash}</code>}</details></td>{view==="replay"&&<td><button className="secondary" type="button" aria-label={`回放${order.name}订单${order.id}`} onClick={()=>{const id=Number(order.id);if(Number.isSafeInteger(id)&&id>0){setReplayOrderId(id);document.getElementById?.("paper-playback-title")?.scrollIntoView?.({behavior:"smooth",block:"start"});}}}>K线回放</button></td>}</tr>)}</tbody></table></div> : <div className="paper-empty"><strong>{data?"本交易日暂无模拟成交":"正在读取交易账本"}</strong><p>{data?running?"系统正在自主判断；条件未满足不强制买入。可以在审计台查看决策与受限原因。":finished?"本轮扫描已完成，符合条件的成交将记录于此；本席位没有成交时，不补造买卖。":"启用自动模拟后，系统自主扫描并生成账本，不需要手工选择股票。":"只有服务器已保存的订单才会显示。"}</p></div>}
        <div className="paper-pagination"><span>共 {data?.orders.total ?? 0} 笔 · 每页20笔</span><button className="secondary" type="button" disabled={pending!==null || query.offset===0} onClick={()=>select({offset:Math.max(0,query.offset-20)})}>上一页</button><button className="secondary" type="button" disabled={pending!==null || !data || query.offset+data.orders.items.length>=data.orders.total} onClick={()=>select({offset:query.offset+20})}>下一页</button></div>
      </section>
      <section className="paper-section" aria-labelledby="paper-position-title"><div className="paper-section-title"><h2 id="paper-position-title">当前模拟持仓</h2><span>浮盈亏不等于已实现收益 · T+1可卖数量单独显示</span></div>{data?.positions.length ? <div className="table-scroll" tabIndex={0} role="region" aria-label="自动模拟持仓"><table><thead><tr><th>股票名称 / 代码</th><th>席位</th><th>持有 / 可卖</th><th>成本价</th><th>估值价</th><th>持仓市值</th><th>浮盈亏</th><th>持仓收益率</th><th>开仓日 / 估值时间</th></tr></thead><tbody>{data.positions.map(position=><tr key={`${position.seat_id}-${position.code}`}><td><strong>{position.name}</strong><code>{position.code}</code></td><td>{SEATS[position.seat_id]}</td><td>{number(position.quantity,0)} / {number(position.available_quantity,0)} 股</td><td>{number(position.cost_price)}</td><td>{number(position.mark_price)}</td><td>{money(position.market_value)}</td><td className={tone(position.unrealized_pnl)}>{signed(position.unrealized_pnl)}</td><td className={tone(position.return_pct)}>{signed(position.return_pct,"%")}</td><td>{position.entry_date || "—"}<small>{dateTime(position.updated_at)}</small></td></tr>)}</tbody></table></div> : <div className="paper-empty"><p>{data?"当前没有模拟持仓。未成交不代表遗漏股票，须结合自动决策原因查看。":"正在读取持仓…"}</p></div>}</section>
    </>}
    {(view==="research" || view==="audit") && <section className="paper-section" aria-labelledby="paper-decision-title"><div className="paper-section-title"><h2 id="paper-decision-title">自主决策与未成交原因</h2><span>显示有界审计样本 · 总计 {data?.decisions.total ?? 0} 条</span></div>{data?.decisions.items.length ? <div className="table-scroll" tabIndex={0} role="region" aria-label="自主模拟决策记录"><table><thead><tr><th>股票名称 / 代码</th><th>席位</th><th>决定</th><th>状态</th><th>决策与风控依据</th></tr></thead><tbody>{data.decisions.items.map((decision,index)=><tr key={`${decision.seat_id}-${decision.code}-${index}`}><td><strong>{decision.name}</strong><code>{decision.code}</code></td><td>{SEATS[decision.seat_id]}</td><td>{actionLabel(decision.action)}</td><td>{actionLabel(decision.status)}</td><td><p>{decision.reason}</p>{(decision.missing?.length ?? 0)>0 && <ul>{decision.missing?.map((note,n)=><li key={n}>{note}</li>)}</ul>}</td></tr>)}</tbody></table></div> : <div className="paper-empty"><p>{data?"暂无已保存的自主决策；启用后会显示选择、观望、风控及未成交原因。":"正在读取审计记录…"}</p></div>}</section>}
    <details className="paper-method" open={view==="audit"}><summary>运行规则、费用与数据边界</summary><p>每个席位独立虚拟资金，自动遵守现金、仓位、整手和T+1约束。不连接券商、不下真实订单；按规则缺少证据时允许观望，不为展示收益而强制买入。</p><p>已实现损益与持仓浮盈亏分别展示；当日盈亏和累计收益由服务器账户账本计算。历史模拟交易日不等于实际日历日期，一天后续数据不能证明可在买入当天卖出。</p>{data?.notice?.slice(0,12).map((note,index)=><p key={index}>{note}</p>)}</details>
  </section>;
}

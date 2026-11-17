"use client";

import { useEffect, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { PriceChart, type Bar } from "./price-chart";
import { BuffettEvidence, type BuffettFundamentals } from "./buffett-evidence";
import NewsDesk, { type NewsResearchRequest } from "./news-desk";
import ResearchHistory, { type ResearchContext } from "./research-history";
import HistoricalMarket, { type HistoricalSample, type HistoricalEvidence } from "./historical-market";
import PaperTradingDesk from "./paper-trading-desk";

const API_BASE = "/contest-api";

type Mode = "offline" | "ai";
const deskViews = [
  { id: "research", label: "研究台" },
  { id: "replay", label: "复盘台" },
  { id: "audit", label: "审计台" },
  { id: "news", label: "新闻页" },
] as const;
type DeskView = typeof deskViews[number]["id"];
type Seat = { id: string; label: string; description: string; modes: Mode[] };
const seatLabel = (seat: Seat) => seat.id === "baseline" ? "基础策略席位" : seat.label;
type Sample = { id: string; seat_id: string; label: string; symbol: string; name: string; decision_date: string; description: string };
type Catalog = { seats: Seat[]; cases: Sample[]; ai_available: boolean; model?: string | null; data_source?: "historical"; archive?: {available?:boolean;end_date?:string;decision_date?:string};financial_profiles?:{code:string;name?:string;status?:string;report_count?:number}[] };

function retainSample(items:Sample[],sample:Sample):Sample[] {
  // Keep the sidebar a small summary, not another copy of the full directory.
  const sameSeat=items.filter(item=>item.seat_id===sample.seat_id && item.id!==sample.id);
  return [...items.filter(item=>item.seat_id!==sample.seat_id),...[...sameSeat,sample].slice(-40)];
}
type Evidence = {
  case_id: string; symbol: string; decision_date: string; bars: Bar[]; evidence_ids: string[];
  fundamentals?: BuffettFundamentals; seat_id?: string; name?: string;
  execution?: {ready?:boolean;status?:string;missing?:string[];note?:string}; limitations?:string[];
  metrics: { data_valid?: boolean; data_issues?: string[]; bar_count?: number; tradable_count?: number; last_close?: number | null; sma5?: number | null; sma20?: number | null; return_5d_pct?: number | null; volume_ratio_5d?: number | null;
    range_position_120_pct?: number | null; closed_at_limit_up?: boolean | null; eligible?: boolean | null };
};
type Analysis = {
  research_id?: string;
  run_id: string; mode: Mode; model: string; engine_version?: string; evidence: Evidence;
  seat: Seat;
  execution_policy?: { limit_up_deferrals?: number; uses_price_thresholds?: boolean; max_holding_sessions?: number | null };
  plan: { action: "simulate" | "hold"; rationale: string; evidence_ids: string[]; entry_low: number | null; entry_high: number | null; position_pct: number | null; stop_loss: number | null; take_profit: number | null };
  validation: { ok: boolean; checks: { code: string; label: string; passed: boolean; detail: string }[] };
  trace: { step: string; title: string; detail: string }[];
  frozen_at: string; decision_hash: string;
};

type SavedResearch = {
  title?: string;
  record_id: string; status: "draft" | "frozen" | "replayed"; case_id: string; seat_id: string;
  context: ResearchContext | null; analysis: Analysis | null; replay: Replay | null; replay_available: boolean;
};
type ResearchState = { recordId: string | null; context: ResearchContext | null; archived: boolean; historyOpen: boolean; historyVersion: number; loading: boolean; error: string };
type MarketState = { available: boolean | null; loading: boolean; error: string; sample: HistoricalSample | null; preview: Evidence | null; endDate: string; decisionDate: string };
type Replay = {
  status: "completed" | "no_fill" | "hold" | "blocked" | "pending";
  observation_only?: boolean;
  seat_id?: string;
  reason: string;
  events: { date: string; type: string; price: number | null; detail: string }[];
  replay_bars?: Bar[];
  metrics: {
    starting_cash?: number; ending_cash?: number; ending_equity?: number; realized_pnl?: number; unrealized_pnl?: number;
    return_pct?: number; fees?: number; shares?: number; open_shares?: number; holding_sessions?: number;
    entry_budget?: number | null; used_limit_up_deferrals?: number;
    is_open?: boolean; mark_price?: number | null; fee_note?: string; execution_note?: string;
  };
};

const number = (value: number | null | undefined, digits = 2) => value != null && Number.isFinite(value) ? value.toLocaleString("zh-CN", { maximumFractionDigits: digits, minimumFractionDigits: digits }) : "—";
const percent = (value: number | null | undefined, signed = false) => value != null && Number.isFinite(value) ? `${signed && value > 0 ? "+" : ""}${number(value)}%` : "—";
const money = (value: number | null | undefined) => value != null && Number.isFinite(value) ? `${number(value)} 元` : "—";
const yesNo = (value: boolean | null | undefined) => value === true ? "是" : value === false ? "否" : "—";
const errorMessage = (error: unknown) => error instanceof Error ? error.message : "服务暂不可用，请重试。";
const time = (value: string) => {
  const date = new Date(value);
  return Number.isFinite(date.getTime()) ? `${date.toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false })} 北京时间` : value;
};

async function request<T>(path: string, signal: AbortSignal, body?: object): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, {
      cache: "no-store", credentials: "same-origin", signal, method: body === undefined ? "GET" : "POST",
      headers: body === undefined ? undefined : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (error) {
    if (signal.aborted) throw error;
    throw new Error("无法连接参赛版服务，请检查本地服务是否已启动。");
  }
  const result = await response.json().catch(() => null);
  if (response.status === 401 && !signal.aborted) window.dispatchEvent(new Event("contest-session-expired"));
  if (!response.ok) {
    throw new Error(typeof result?.detail === "string" ? result.detail : `服务请求失败（HTTP ${response.status}），请重试。`);
  }
  if (!result || typeof result !== "object" || Array.isArray(result)) throw new Error("服务返回格式不正确，请重试。");
  return result as T;
}

function EvidencePanel({ evidence, seat }: { evidence: Evidence; seat: Seat }) {
  const m = evidence.metrics;
  const change = m.return_5d_pct;
  const isBuffett = seat.id === "buffett";
  return <section className="card evidence-card" aria-labelledby="evidence-title">
    <div className="section-heading"><div><p className="eyebrow">时点证据</p><h2 id="evidence-title">{isBuffett ? "价值评估证据" : "决策日行情"}</h2></div><span className="pill">截至 {evidence.decision_date}</span></div>
    <div className="quote"><strong title="决策日收盘价">{number(m.last_close)}</strong>{isBuffett ? <span>决策日收盘价 · 元</span> : <span className={change == null || change === 0 ? "" : change > 0 ? "rise" : "fall"}>{percent(change, true)} / 5 日</span>}</div>
    {evidence.case_id.startsWith("history-") && <div className="historical-evidence-source"><p className="section-note">{evidence.symbol}　{evidence.name || "真实历史股票"} · {seatLabel(seat)} · 非实时 · 未复权 · 成交量单位：股。</p>
      <p className="section-note">{evidence.execution?.note || "真实价格已接入。是否满足席位买入条件、能否回放成交，须分别核验；不会用未知涨跌停价或竞价数据补齐。"}</p>
      {(evidence.execution?.missing?.length ?? 0)>0 && <details className="disclosure"><summary>待补充的执行证据</summary><ul>{evidence.execution?.missing?.map((note,index)=><li key={index}>{note}</li>)}</ul></details>}
    </div>}
    {m.data_valid === false && <div className="callout danger"><strong>发现数据质量问题</strong><ul>{(m.data_issues ?? []).map((issue, index) => <li key={index}>{issue}</li>)}</ul></div>}
    {isBuffett && <BuffettEvidence fundamentals={evidence.fundamentals} />}
    <PriceChart bars={evidence.bars} />
    {isBuffett ? <p className="section-note">日线只辅助观察价格。价值门槛不使用短线量比、涨停或竞价信号。</p> : <dl className="metric-grid compact-metrics">
      <div><dt>5 日量比</dt><dd>{m.volume_ratio_5d != null ? `${number(m.volume_ratio_5d)}×` : "—"}</dd></div>
      <div><dt>5 日均线</dt><dd>{number(m.sma5)}</dd></div>
      <div><dt>20 日均线</dt><dd>{number(m.sma20)}</dd></div>
    </dl>}
    {seat.id === "livermore" && <dl className="metric-grid seat-metrics" aria-label="利弗莫尔席位条件">
      <div><dt>120 日价格区间位置</dt><dd>{percent(m.range_position_120_pct)}</dd></div>
      <div><dt>决策日收盘涨停</dt><dd>{yesNo(m.closed_at_limit_up)}</dd></div>
      <div><dt>满足席位选股条件</dt><dd>{yesNo(m.eligible)}</dd></div>
    </dl>}
    <details className="disclosure">
      <summary>查看 {evidence.bars.length} 条原始日线与证据编号</summary>
      <p className="section-note">记录数 {number(m.bar_count, 0)} · 可交易日 {number(m.tradable_count, 0)}</p>
      <div className="table-scroll" tabIndex={0} role="region" aria-label="时点证据日线表">
        <table><thead><tr><th scope="col">日期</th><th scope="col">开</th><th scope="col">高</th><th scope="col">低</th><th scope="col">收</th><th scope="col">量</th><th scope="col">涨停价</th><th scope="col">跌停价</th><th scope="col">状态</th></tr></thead><tbody>
          {evidence.bars.map((bar, index) => <tr key={`${bar.date}-${index}`}><th scope="row">{bar.date}</th><td>{number(bar.open)}</td><td>{number(bar.high)}</td><td>{number(bar.low)}</td><td>{number(bar.close)}</td><td>{number(bar.volume, 0)}</td><td>{number(bar.limit_up)}</td><td>{number(bar.limit_down)}</td><td>{evidence.case_id.startsWith("history-") ? "停复牌未核验" : bar.suspended ? "停牌" : "非停牌"}</td></tr>)}
        </tbody></table>
      </div>
      <div className="evidence-ids">{evidence.evidence_ids.map((id) => <code key={id}>{id}</code>)}</div>
    </details>
  </section>;
}

function DecisionPanel({ run, children, audit = false }: { run: Analysis; children?: ReactNode; audit?: boolean }) {
  const p = run.plan;
  const isLivermore = run.seat.id === "livermore";
  const isBuffett = run.seat.id === "buffett";
  const hasPriceThresholds = run.execution_policy?.uses_price_thresholds ?? (!isLivermore && !isBuffett);
  const frozenRecord = <>
    {(isLivermore || isBuffett) && <p className="section-note">席位来源：{run.seat.description} 仅回放本案例单一标的，不比较其他股票。</p>}
    <div className="freeze-box"><strong>决策已冻结</strong><span>{time(run.frozen_at)}</span><small>SHA-256 决策哈希</small><code>{run.decision_hash}</code>{run.engine_version && <small>引擎 {run.engine_version}</small>}</div>
    <div className="citation"><strong>引用证据</strong><div className="evidence-ids">{p.evidence_ids.length ? p.evidence_ids.map((id) => <code key={id}>{id}</code>) : <span>无</span>}</div></div>
  </>;
  return <section className="card decision-card" aria-labelledby="decision-title">
    <div className="section-heading"><h2 id="decision-title">{audit ? "冻结预案" : "决策预案"}</h2><span className={`pill ${run.validation.ok ? "good" : "danger"}`}>{run.validation.ok ? "风控通过" : "风控拦截"}</span></div>
    <h3>{p.action === "simulate" ? "条件满足才模拟入场" : "观望 · 不下单"}</h3>
    <p className="run-mode">{seatLabel(run.seat)} · {run.mode === "ai" ? `模型：${run.model}` : "离线规则，未调用大模型"}</p>
    <p className="rationale">{p.rationale}</p>
    {p.action === "simulate" && <dl className="metric-grid plan-metrics">
      <div><dt>{isLivermore ? "竞价准入价格区间" : isBuffett ? "含安全边际的买入上限" : "虚拟入场区间"}</dt><dd>{isBuffett ? `≤ ${number(p.entry_high)} 元` : `${number(p.entry_low)} – ${number(p.entry_high)}`}</dd></div>
      <div><dt>{isLivermore ? "冻结仓位上限" : "仓位上限"}</dt><dd>{percent(p.position_pct)}</dd></div>
      {hasPriceThresholds && <><div><dt>保护价</dt><dd>{number(p.stop_loss)}</dd></div><div><dt>目标价</dt><dd>{number(p.take_profit)}</dd></div></>}
    </dl>}
    {isLivermore && p.action === "simulate" && <div className="callout">
      <strong>竞价分档入场 · 隔日收盘退出</strong>
      <p>预案仓位只是上限；是否成交及实际竞价档位在回放时判定。计划在入场后下一交易日收盘退出；遇收盘涨停，按 Z1 最多顺延 {run.execution_policy?.limit_up_deferrals ?? 1} 次。跌停或停牌可能限制成交。</p>
    </div>}
    {isBuffett && <div className="callout"><strong>价值纪律 · 不设隔日强制退出</strong>
      <p>仅在冻结的买入上限内尝试次一交易日开盘成交。持仓以长期基本面复核为前提，不用短线止盈止损替代价值判断。本版未自动执行后续财报驱动的卖出；回放末尾未卖出时只展示浮动权益，不算已实现收益，也不能证明长期策略有效。</p>
    </div>}
    {children}
    {audit ? frozenRecord : <details className="disclosure"><summary>查看冻结记录与引用证据</summary>{frozenRecord}</details>}
  </section>;
}

function AuditPanels({ run }: { run: Analysis }) {
  return <div className="audit-grid">
    <div className="audit-plan"><DecisionPanel run={run} audit><div className="audit-evidence"><PriceChart bars={run.evidence.bars} /></div></DecisionPanel></div>
    <div className="audit-rail">
    <section className="card" aria-labelledby="risk-title"><div className="section-heading"><div><p className="eyebrow">{run.seat.id !== "baseline" ? "独立策略校验" : "独立校验"}</p><h2 id="risk-title">风控校验</h2></div><span className={`pill ${run.validation.ok ? "good" : "danger"}`}>{run.validation.checks.filter((check) => check.passed).length} / {run.validation.checks.length} 项通过</span></div>
      <ul className="checks">{run.validation.checks.map((check, index) => <li key={`${check.code}-${index}`}><span className={`pill ${check.passed ? "good" : "danger"}`}>{check.passed ? "通过" : "拦截"}</span><div><strong>{check.label}</strong><p>{check.detail}</p></div></li>)}</ul>
    </section>
    <section className="card" aria-labelledby="trace-title"><div className="section-heading"><div><p className="eyebrow">可追溯过程</p><h2 id="trace-title">调用轨迹</h2></div><span className="pill">{run.trace.length} 步</span></div>
      <ol className="timeline">{run.trace.map((step, index) => <li key={`${step.step}-${index}`}><span className="timeline-marker" aria-hidden="true">{String(index + 1).padStart(2, "0")}</span><div><strong>{step.title}</strong><p>{step.detail}</p></div></li>)}</ol>
    </section>
    </div>
  </div>;
}

const statusLabels: Record<Replay["status"], string> = {
  completed: "回放完成 · 已平仓", no_fill: "未成交", hold: "观望 · 未交易", blocked: "已拦截", pending: "回放未结束",
};
const eventLabels: Record<string, string> = {
  blocked: "风控拦截", hold: "观望", no_fill: "未成交", buy: "虚拟买入", t_plus_one: "T+1 限制",
  monitor: "持仓观察", exit_deferred: "延后退出", sell: "虚拟卖出",
};

function ReplaySummary({ replay, run }: { replay: Replay | null; run: Analysis | null }) {
  const m = replay?.metrics ?? {};
  const budgetPct = m.entry_budget != null && m.starting_cash && m.starting_cash > 0 ? m.entry_budget / m.starting_cash * 100 : null;
  const returnClass = m.return_pct ? (m.return_pct > 0 ? "rise" : "fall") : undefined;
  const blocked = replay?.status === "blocked";
  const observation = replay?.observation_only === true;
  const hasBudget = run?.seat.id === "livermore" && m.entry_budget != null && (m.shares ?? 0) > 0;
  return <aside className="card replay-summary" aria-labelledby="replay-summary-title">
    <div className="replay-summary-head"><h2 id="replay-summary-title">模拟结果</h2><span className={`pill replay-status${blocked ? " danger" : ""}`}>{replay ? run?.seat.id === "buffett" && m.is_open ? "持有观察 · 未平仓" : statusLabels[replay.status] : "待回放"}</span></div>
    {!replay || blocked || observation ? <p className="section-note">{observation ? "本次仅观察真实后续价格，未创建买卖订单、没有持仓，不统计成交收益。" : replay?.reason ?? "回放完成后显示本案例的权益、收益率与费用。"}</p> : <>
      <div className="replay-equity">
        <p>期末虚拟权益</p>
        <strong>{money(m.ending_equity)}</strong>
        <span className={returnClass}>{m.is_open ? "权益变动（含浮盈亏）" : "收益率"} {percent(m.return_pct, true)}</span>
      </div>
      <dl className="replay-stat-list">
        <div><dt>初始虚拟资金</dt><dd>{money(m.starting_cash)}</dd></div>
        <div><dt>期末现金</dt><dd>{money(m.ending_cash)}</dd></div>
        <div><dt>已实现损益</dt><dd>{money(m.realized_pnl)}</dd></div>
        <div><dt>模拟费用</dt><dd>{money(m.fees)}</dd></div>
        <div><dt>成交股数</dt><dd>{number(m.shares, 0)}</dd></div>
        <div><dt>持有交易日</dt><dd>{number(m.holding_sessions, 0)}</dd></div>
        {m.is_open && <>
          <div><dt>未平仓股数</dt><dd>{number(m.open_shares, 0)}</dd></div>
          <div><dt>估值价</dt><dd>{money(m.mark_price)}</dd></div>
          <div><dt>未实现损益</dt><dd>{money(m.unrealized_pnl)}</dd></div>
        </>}
        {hasBudget && <>
          <div><dt>实际竞价档预算</dt><dd>{money(m.entry_budget)}</dd></div>
          <div><dt>档位预算 / 初始资金</dt><dd>{percent(budgetPct)}</dd></div>
          <div><dt>Z1 已顺延</dt><dd>{m.used_limit_up_deferrals == null ? "—" : `${number(m.used_limit_up_deferrals, 0)} 次`}</dd></div>
        </>}
      </dl>
      {replay.reason && <p className="section-note">{replay.reason}</p>}
      {(m.execution_note || m.fee_note) && <details className="replay-notes disclosure">
        <summary>成交与费用说明</summary>
        {m.execution_note && <p className="section-note">{m.execution_note}</p>}
        {m.fee_note && <p className="section-note">{m.fee_note}</p>}
      </details>}
    </>}
  </aside>;
}

function TradeOverview({ selected, run, replay }: { selected?: Sample; run: Analysis | null; replay: Replay | null }) {
  const historical = Boolean((run?.evidence.case_id ?? selected?.id)?.startsWith("history-"));
  const fills = replay?.events.filter(event => event.type === "buy" || event.type === "sell") ?? [];
  const buys = fills.filter(event => event.type === "buy");
  const identity = historical ? `${run?.evidence.symbol ?? selected?.symbol ?? ""} ${selected?.name ?? run?.evidence.name ?? "历史股票"}` : selected?.label ?? "尚未选择股票";
  return <section className="trade-overview" aria-label="本次研究与交易状态">
    <div><span className="eyebrow">{historical ? "真实历史股票" : selected ? "旧合成研究记录 · 非真实股票" : "真实股票研究"}</span><strong>{identity}</strong><p>单标的研究记录，不是证券账户；不会自动扫描或下单。</p></div>
    <dl><div><dt>预案</dt><dd>{!run ? "未生成" : run.plan.action === "hold" ? "观望预案 · 不下单" : "已冻结条件预案 · 不是成交"}</dd></div><div><dt>模拟买入</dt><dd>{!replay ? "尚未回放 · 无成交记录" : `${buys.length} 笔`}</dd></div><div><dt>当前模拟持仓</dt><dd>{!replay ? "未建立" : replay.metrics.is_open === true ? `${number(replay.metrics.open_shares, 0)} 股` : buys.length ? replay.metrics.is_open === false || replay.status === "completed" ? "已平仓" : "状态未知 · 待核对" : "无持仓"}</dd></div></dl>
    <p className="trade-status-note">{historical ? replay?.reason || run?.evidence.execution?.note || "三个席位使用同一批真实历史股票，但各自核验独立规则。生成预案不等于买入；缺少财报、竞价或执行证据时明确观望或拦截，不编造成交与收益。" : "此处为以前保存的合成记录，不代表真实股票。各研究资金独立，不能相加当作账户收益。"}</p>
    {fills.length > 0 && <div className="table-scroll" tabIndex={0} role="region" aria-label="本次模拟买卖账本"><table className="trade-ledger"><thead><tr><th>研究对象</th><th>交易日 / 时点</th><th>方向</th><th>价格（元）</th><th>股数</th><th>成交金额（未扣费用）</th></tr></thead><tbody>{fills.map((event, index) => {
      // The replay engine holds one fixed-size position. Do not manufacture
      // quantities for legacy records whose share count is absent.
      const shares = replay?.metrics.shares;
      const amount = event.price != null && shares != null && Number.isFinite(shares) && shares > 0 ? event.price * shares : null;
      return <tr key={`${event.type}-${index}`}><td>{identity}</td><td>{event.date} · {event.type === "buy" ? "开盘" : /开盘/.test(event.detail) ? "开盘" : /收盘/.test(event.detail) ? "收盘" : "见回放明细"}</td><td>{event.type === "buy" ? "模拟买入" : "模拟卖出"}</td><td>{number(event.price)}</td><td>{shares != null && shares > 0 ? number(shares, 0) : "—"}</td><td>{money(amount)}</td></tr>;
    })}</tbody></table></div>}
  </section>;
}

function ReplayEvents({ replay }: { replay: Replay }) {
  return replay.events.length > 0 ? <div className="replay-table table-scroll" tabIndex={0} role="region" aria-label="回放明细表，可横向滚动">
    <table aria-labelledby="replay-title">
      <thead><tr><th scope="col">交易日</th><th scope="col">事件</th><th scope="col">价格（元）</th><th scope="col">说明</th></tr></thead>
      <tbody>{replay.events.map((event, index) => <tr key={`${event.date}-${event.type}-${index}`}>
        <td><time dateTime={event.date}>{event.date}</time></td>
        <td className="replay-event">{eventLabels[event.type] ?? event.type}</td>
        <td>{number(event.price)}</td>
        <td>{event.detail}</td>
      </tr>)}</tbody>
    </table>
  </div> : <p className="section-note">本次回放没有可展示的事件。</p>;
}

export default function DecisionDesk() {
  const [view, setView] = useState<DeskView>("research");
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [catalogError, setCatalogError] = useState("");
  const [retryCatalog, setRetryCatalog] = useState(0);
  const [seatId, setSeatId] = useState("");
  const [caseId, setCaseId] = useState("");
  const [mode, setMode] = useState<Mode>("offline");
  const [run, setRun] = useState<Analysis | null>(null);
  const [replay, setReplay] = useState<Replay | null>(null);
  const [busy, setBusy] = useState<"analyze" | "replay" | null>(null);
  const [operationError, setOperationError] = useState<{ phase: "analyze" | "replay"; message: string } | null>(null);
  const [research, setResearch] = useState<ResearchState>({ recordId: null, context: null, archived: false, historyOpen: false, historyVersion: 0, loading: false, error: "" });
  const [market, setMarket] = useState<MarketState>({available:null,loading:true,error:"",sample:null,preview:null,endDate:"2026-11-13",decisionDate:"2026-11-12"});
  const actionRef = useRef<AbortController | null>(null);
  const selectionTouched = useRef(false);
  const previewRef = useRef<AbortController | null>(null);
  const seat = catalog?.seats.find((item) => item.id === seatId);
  const seatCases = catalog?.cases.filter((item) => item.seat_id === seatId) ?? [];
  const selected = seatCases.find((item) => item.id === caseId);
  const isHistorical = Boolean(selected?.id.startsWith("history-"));
  const visibleCases = seatCases.filter(item => item.id.startsWith("history-") || item.id === selected?.id && research.archived);
  const isLivermore = seat?.id === "livermore";
  const canUseAI = Boolean(seat?.modes.includes("ai") && catalog?.ai_available);
  const canAnalyze = Boolean(seat?.modes.includes(mode) && (mode === "offline" || canUseAI) && (catalog?.data_source !== "historical" || isHistorical)
    && (!isHistorical || !market.loading && market.preview?.case_id===caseId && (market.preview.seat_id ?? seatId)===seatId));
  const auditPassed = run?.validation.checks.filter((check) => check.passed).length ?? 0;

  function openDesk(next: DeskView) {
    setView(next);
    const url = new URL(window.location.href);
    url.searchParams.set("desk", next);
    window.history.replaceState(null, "", url);
    document.getElementById(`desk-tab-${next}`)?.focus();
  }

  function navigateTabs(event: KeyboardEvent<HTMLButtonElement>, index: number) {
    const last = deskViews.length - 1;
    const next = event.key === "ArrowRight" ? (index + 1) % deskViews.length
      : event.key === "ArrowLeft" ? (index + last) % deskViews.length
      : event.key === "Home" ? 0 : event.key === "End" ? last : null;
    if (next === null) return;
    event.preventDefault();
    openDesk(deskViews[next].id);
  }

  useEffect(() => {
    const requested = new URLSearchParams(window.location.search).get("desk");
    const initial = deskViews.find(item => item.id === requested);
    if (initial) setView(initial.id);
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    setCatalogError("");
    void request<Catalog>("/cases", controller.signal).then(async (data) => {
      if (controller.signal.aborted) return;
      if (!Array.isArray(data.seats)) throw new Error("参赛服务未返回席位信息，请确认已启动当前版本。");
      if (!Array.isArray(data.cases)) throw new Error("真实股票目录格式不正确。");
      const availableSeats = data.seats.filter((item) => ["baseline","livermore","buffett"].includes(item.id) && Array.isArray(item.modes) && item.modes.length > 0);
      const defaultSeat = availableSeats.find(item => item.id === "baseline") ?? availableSeats[0];
      if (!defaultSeat) throw new Error("没有可用的策略席位。");
      const realCases = data.cases.filter(item => /^history-20261113-(?:000|001|002|003|600|601|603|605)\d{3}$/.test(item.id)
        && item.symbol === item.id.slice(-6) && item.decision_date === "2026-11-12" && availableSeats.some(seat => seat.id === item.seat_id));
      setCatalog(previous => ({...data,data_source:"historical",seats:availableSeats,cases:selectionTouched.current && previous ? previous.cases : realCases}));
      if (selectionTouched.current) return;
      setSeatId(defaultSeat.id);
      setMode("offline");
      const firstCase = realCases.find(item => item.seat_id === defaultSeat.id);
      setCaseId(firstCase?.id ?? "");
      setMarket(previous => ({...previous,endDate:data.archive?.end_date || "2026-11-13",decisionDate:data.archive?.decision_date || "2026-11-12"}));
      if (!firstCase) {
        setMarket(previous => ({...previous,available:false,loading:false,sample:null,preview:null,error:"尚无可读的真实历史股票，请先导入或检查已下载档案；不会改用虚构股票演示。"}));
        return;
      }
      await previewHistorical(firstCase, controller.signal);
    }).catch((error) => { if (!controller.signal.aborted) {setCatalogError(errorMessage(error));setMarket(previous=>({...previous,loading:false,error:"真实股票服务暂不可用，未改用合成数据。"}));} });
    return () => controller.abort();
  }, [retryCatalog]);
  useEffect(() => () => {actionRef.current?.abort();previewRef.current?.abort();}, []);

  async function previewHistorical(sample: HistoricalSample, parentSignal?: AbortSignal) {
    previewRef.current?.abort();
    const controller = new AbortController(); previewRef.current = controller;
    const abort = () => controller.abort();
    parentSignal?.addEventListener("abort",abort,{once:true});
    setMarket(previous=>({...previous,loading:true,error:"",sample,preview:null}));
    try {
      const detail = await request<{sample:HistoricalSample;evidence:Evidence;revealed:boolean;observed_bars?:Bar[]}>(`/market/stocks/${sample.symbol}?seat_id=${sample.seat_id}`,controller.signal);
      if (controller.signal.aborted || previewRef.current !== controller) return;
      if (detail.sample?.id !== sample.id || detail.sample.symbol !== sample.symbol || detail.sample.seat_id !== sample.seat_id
        || detail.evidence?.case_id !== sample.id || detail.evidence.symbol !== sample.symbol || detail.evidence.decision_date !== sample.decision_date
        || detail.evidence.seat_id != null && detail.evidence.seat_id !== sample.seat_id || !detail.evidence.metrics
        || !Array.isArray(detail.evidence.bars) || !Array.isArray(detail.evidence.evidence_ids) || detail.revealed !== false
        || (detail.observed_bars?.length ?? 0) > 0 || detail.evidence.bars.some(bar=>bar.date>sample.decision_date)) throw new Error("真实股票或席位证据与当前选择不一致，已拒绝展示。");
      setMarket(previous=>({...previous,available:true,loading:false,error:"",sample:detail.sample,preview:detail.evidence,decisionDate:detail.evidence.decision_date}));
      setCatalog(previous=>previous ? {...previous,cases:previous.cases.map(item=>item.id===sample.id && item.seat_id===sample.seat_id ? detail.sample : item)} : previous);
    } catch (error) {
      if(!controller.signal.aborted && previewRef.current===controller)setMarket(previous=>({...previous,loading:false,error:errorMessage(error),preview:null}));
    } finally {
      parentSignal?.removeEventListener("abort",abort);
      if(previewRef.current===controller)previewRef.current=null;
    }
  }

  function reset(clearResearch = true) {
    actionRef.current?.abort();
    previewRef.current?.abort();previewRef.current=null;
    setMarket(previous=>({...previous,loading:false}));
    actionRef.current = null;
    setBusy(null);
    setRun(null);
    setReplay(null);
    setOperationError(null);
    if (clearResearch) setResearch(previous => ({ ...previous, recordId: null, context: null, archived: false, loading: false, error: "" }));
    else setResearch(previous => ({ ...previous, recordId: run ? null : previous.recordId, archived: false, loading: false, error: "" }));
  }

  async function enterNewsResearch(selection: NewsResearchRequest) {
    if (!catalog || actionRef.current) return;
    const controller = new AbortController();
    actionRef.current = controller;
    setResearch(previous => ({ ...previous, loading: true, error: "" }));
    try {
      // IDs only: the server reconstructs and snapshots the association. Iframe prose is not trusted.
      const draft = await request<{ record_id: string; case_id: string; seat_id: string; context: ResearchContext }>("/research/start", controller.signal,
        { news_id: selection.newsId, stock_id: selection.stockId, seat_id: selection.seatId, sector_id: selection.sectorId });
      if (controller.signal.aborted) return;
      const targetSeat = catalog.seats.find(item => item.id === draft.seat_id);
      const targetCase = catalog.cases.find(item => item.id === draft.case_id && item.seat_id === draft.seat_id);
      if (!/^[a-f0-9]{32}$/.test(draft.record_id) || !targetSeat || !targetCase || draft.seat_id !== selection.seatId
        || draft.context?.news?.id !== selection.newsId || draft.context?.stock?.id !== selection.stockId) throw new Error("新闻研究映射不完整，未切换当前预案。");
      setRun(null); setReplay(null); setOperationError(null);
      selectionTouched.current = true;
      setSeatId(targetSeat.id); setCaseId(targetCase.id);
      setMode(targetSeat.modes.includes("offline") ? "offline" : "ai");
      setResearch(previous => ({ ...previous, recordId: draft.record_id, context: draft.context, archived: false, historyVersion: previous.historyVersion + 1 }));
      openDesk("research");
    } catch (error) {
      if (!controller.signal.aborted) setResearch(previous => ({ ...previous, error: errorMessage(error) }));
    } finally {
      if (actionRef.current === controller) { actionRef.current = null; setResearch(previous => ({ ...previous, loading: false })); }
    }
  }

  async function openResearchRecord(id: string) {
    if (actionRef.current || !catalog || !/^[a-f0-9]{32}$/.test(id)) return;
    selectionTouched.current = true;
    previewRef.current?.abort();previewRef.current=null;
    const controller = new AbortController();
    actionRef.current = controller;
    setResearch(previous => ({ ...previous, loading: true, error: "" }));
    try {
      const saved = await request<SavedResearch>(`/research/records/${id}`, controller.signal);
      if (controller.signal.aborted) return;
      const targetSeat = catalog.seats.find(item => item.id === saved.seat_id);
      if (!targetSeat || saved.record_id !== id || !["draft", "frozen", "replayed"].includes(saved.status)) throw new Error("研究记录的席位或格式不正确。");
      const stored = saved.analysis;
      if (stored && (stored.seat?.id !== saved.seat_id || stored.evidence?.case_id !== saved.case_id
        || !stored.decision_hash || !Array.isArray(stored.evidence?.bars) || !Array.isArray(stored.validation?.checks)
        || !Array.isArray(stored.trace) || stored.evidence.bars.some(bar => bar.date > stored.evidence.decision_date))) throw new Error("历史预案或时点证据不完整，未替换当前研究。");
      const archivedReplay = saved.replay;
      if (archivedReplay && (!stored || !Object.hasOwn(statusLabels, archivedReplay.status) || !Array.isArray(archivedReplay.events)
        || !archivedReplay.metrics || (archivedReplay.seat_id != null && archivedReplay.seat_id !== stored.seat.id)
        || archivedReplay.replay_bars?.some(bar => bar.date <= stored.evidence.decision_date)
        || ((!stored.validation.ok && archivedReplay.status !== "blocked") || (archivedReplay.status === "blocked"
          && ((archivedReplay.replay_bars?.length ?? 0) > 0 || archivedReplay.events.some(event => event.date > stored.evidence.decision_date)))))) throw new Error("历史回放与冻结预案不一致。");
      if (!stored && !catalog.cases.some(item => item.id === saved.case_id && item.seat_id === saved.seat_id)) throw new Error("该待研究情景已不在当前目录中。");
      // Dated stock cases are loaded on demand, not in the global catalog.
      // Restore their summary when opening a durable record after a restart.
      if (stored && /^history-20261113-(?:000|001|002|003|600|601|603|605)\d{3}$/.test(saved.case_id)) {
        const symbol = saved.case_id.slice(-6);
        if (stored.evidence.symbol !== symbol || stored.evidence.decision_date !== "2026-11-12") throw new Error("历史股票档案与行情时点不匹配。");
        const name = saved.context?.stock?.label || symbol;
        const historical = { id: saved.case_id, seat_id: saved.seat_id, symbol, name,
          label: `${symbol} ${name} · 历史行情`, decision_date: "2026-11-12", description: "真实历史日线条件研究，不模拟真实成交。" };
        setCatalog(previous => previous ? { ...previous, cases: retainSample(previous.cases,historical) } : previous);
        setMarket(previous=>({...previous,loading:false,error:"",available:true,sample:historical,preview:stored.evidence}));
      } else if(stored && !catalog.cases.some(item=>item.id===saved.case_id && item.seat_id===saved.seat_id)) {
        // Retained legacy examples remain inspectable, never a new real-stock
        // research source or an implicit company identity.
        const legacy={id:saved.case_id,seat_id:saved.seat_id,symbol:stored.evidence.symbol,
          name:saved.context?.stock?.label || "旧合成情景",label:saved.title || "旧合成研究记录",decision_date:stored.evidence.decision_date,description:"以前保存的合成记录，只读查看。"};
        setCatalog(previous=>previous ? {...previous,cases:retainSample(previous.cases,legacy)} : previous);
      }
      setRun(stored); setReplay(archivedReplay); setOperationError(null);
      setSeatId(saved.seat_id); setCaseId(saved.case_id); setMode(stored?.mode ?? "offline");
      setResearch(previous => ({ ...previous, recordId: saved.record_id, context: saved.context, archived: Boolean(stored && (!saved.replay_available || catalog.data_source === "historical" && !saved.case_id.startsWith("history-"))), historyOpen: false }));
      openDesk("research");
    } catch (error) {
      if (!controller.signal.aborted) setResearch(previous => ({ ...previous, error: errorMessage(error) }));
    } finally {
      if (actionRef.current === controller) { actionRef.current = null; setResearch(previous => ({ ...previous, loading: false })); }
    }
  }

  function chooseSeat(nextId: string) {
    const nextSeat = catalog?.seats.find((item) => item.id === nextId);
    if (!nextSeat || nextId === seatId) return;
    selectionTouched.current = true;
    reset();
    setSeatId(nextId);
    setMode(nextSeat.modes.includes("offline") ? "offline" : "ai");
    const current = isHistorical ? selected : market.sample;
    const nextSample = current ? {...current,seat_id:nextId} : catalog?.cases.find(item=>item.seat_id===nextId && item.id.startsWith("history-"));
    if(nextSample){
      setCatalog(previous=>previous ? {...previous,cases:retainSample(previous.cases,nextSample)} : previous);
      setCaseId(nextSample.id);void previewHistorical(nextSample);
    }else{
      setCaseId("");setMarket(previous=>({...previous,preview:null,sample:null,loading:false,error:"尚无已下载的真实股票，请先检查历史档案。"}));
    }
  }

  function selectHistorical(sample: HistoricalSample, evidence?: HistoricalEvidence) {
    if (busy || actionRef.current || research.loading || !catalog || sample.seat_id !== seatId || !catalog.seats.some(item=>item.id===sample.seat_id)
      || !/^history-20261113-(?:000|001|002|003|600|601|603|605)\d{3}$/.test(sample.id)
      || sample.symbol !== sample.id.slice(-6) || sample.decision_date !== "2026-11-12") return;
    selectionTouched.current = true;
    if (evidence && (evidence.case_id !== sample.id || evidence.symbol !== sample.symbol || evidence.decision_date !== sample.decision_date
      || evidence.seat_id != null && evidence.seat_id !== sample.seat_id
      || evidence.bars.some(bar => bar.date > sample.decision_date))) return;
    // Clicking the current stock or returning to its chart must not erase an
    // already frozen plan or saved replay.
    if (sample.id === caseId && seatId === sample.seat_id) {
      if (evidence) {
        previewRef.current?.abort();previewRef.current=null;
        setMarket(previous => ({...previous,available:true,loading:false,error:"",sample,preview:evidence}));
      } else if (!market.loading && !research.archived && (market.preview?.case_id !== sample.id || market.preview?.seat_id !== sample.seat_id)) {
        void previewHistorical(sample);
      }
      return;
    }
    previewRef.current?.abort();previewRef.current=null;
    setMarket(previous => ({...previous,available:true,loading:false,error:"",sample,preview:evidence ?? null}));
    reset();
    setCatalog(previous => previous ? {...previous,cases:retainSample(previous.cases,sample)} : previous);
    setSeatId(sample.seat_id); setCaseId(sample.id); setMode("offline");
    if(!evidence)void previewHistorical(sample);
    openDesk("research");
  }

  function selectFinancialStock(code: string) {
    if(!/^(?:000|001|002|003|600|601|603|605)\d{3}$/.test(code) || !catalog?.financial_profiles?.some(item=>item.code===code))return;
    const existing=catalog.cases.find(item=>item.symbol===code && item.seat_id===seatId);
    const profile=catalog.financial_profiles.find(item=>item.code===code);
    selectHistorical(existing ?? {id:`history-20261113-${code}`,seat_id:seatId,symbol:code,name:profile?.name || "",label:`${code} ${profile?.name || "正在读取公司名称"}`,decision_date:"2026-11-12",description:"真实历史股票与已缓存的财务资料；需核验时点与版本。"});
  }

  async function analyze() {
    if (!seat || !selected || !canAnalyze || actionRef.current || research.archived) return;
    selectionTouched.current = true;
    const controller = new AbortController();
    actionRef.current = controller;
    setBusy("analyze");
    setOperationError(null);
    try {
      let recordId = run ? null : research.recordId;
      if ((run || !recordId) && research.context?.news && research.context.stock) {
        const draft = await request<{ record_id: string }>("/research/start", controller.signal, { news_id: research.context.news.id, stock_id: research.context.stock.id, seat_id: seat.id, sector_id: null });
        if (!/^[a-f0-9]{32}$/.test(draft.record_id)) throw new Error("新研究记录创建失败。");
        recordId = draft.record_id;
      }
      const result = await request<Analysis>("/analyze", controller.signal, { seat_id: seat.id, case_id: selected.id, mode, ...(recordId ? { research_id: recordId } : {}) });
      if (controller.signal.aborted) return;
      if (!result.run_id || !result.decision_hash || !result.plan || !Array.isArray(result.plan.evidence_ids)
        || !Array.isArray(result.validation?.checks) || !Array.isArray(result.trace) || !Array.isArray(result.evidence?.bars)
        || !Array.isArray(result.evidence.evidence_ids) || !result.evidence.metrics) throw new Error("预案或证据不完整，请重新生成。");
      if (result.mode !== mode) throw new Error("返回的运行模式与所选模式不一致，请重新生成。");
      if (result.seat?.id !== seat.id) throw new Error("返回的预案与所选席位不一致，请重新生成。");
      if (result.evidence.case_id !== selected.id || result.evidence.decision_date !== selected.decision_date
        || result.evidence.bars.some((bar) => typeof bar.date !== "string" || bar.date > selected.decision_date)) {
        throw new Error("返回证据超出当前案例的决策时点，已拒绝展示和回放。");
      }
      setRun(result);
      setReplay(null);
      setResearch(previous => ({ ...previous, recordId: result.research_id ?? recordId, archived: false, historyVersion: previous.historyVersion + 1 }));
    } catch (error) {
      if (!controller.signal.aborted) setOperationError({ phase: "analyze", message: errorMessage(error) });
    } finally {
      if (actionRef.current === controller) { actionRef.current = null; setBusy(null); }
    }
  }

  async function reveal() {
    if (!run || actionRef.current || research.archived) return;
    const controller = new AbortController();
    actionRef.current = controller;
    setBusy("replay");
    setReplay(null);
    setOperationError(null);
    try {
      // The only execution input is the frozen run ID. The flag opts into chart data, not a different plan.
      const result = await request<Replay>("/replay", controller.signal, { run_id: run.run_id, include_bars: run.validation.ok });
      if (controller.signal.aborted) return;
      if (!Object.hasOwn(statusLabels, result.status) || !Array.isArray(result.events) || !result.metrics) throw new Error("回放结果格式不正确，请重试。");
      if (result.seat_id != null && result.seat_id !== run.seat.id) throw new Error("回放结果与冻结席位不一致，已拒绝展示。");
      const cutoff = run.evidence.decision_date;
      if (result.replay_bars !== undefined && (!Array.isArray(result.replay_bars)
        || result.replay_bars.some((bar) => !bar || typeof bar.date !== "string" || bar.date <= cutoff))) {
        throw new Error("回放行情与冻结的决策日期不一致，已拒绝展示。");
      }
      if ((!run.validation.ok && result.status !== "blocked") || (result.status === "blocked"
        && ((result.replay_bars?.length ?? 0) > 0 || result.events.some((event) => event.date > cutoff)))) {
        throw new Error("风控未通过，已拒绝显示后续行情。");
      }
      setReplay(result);
      setResearch(previous => ({ ...previous, historyVersion: previous.historyVersion + 1 }));
    } catch (error) {
      if (!controller.signal.aborted) setOperationError({ phase: "replay", message: errorMessage(error) });
    } finally {
      if (actionRef.current === controller) { actionRef.current = null; setBusy(null); }
    }
  }

  return <div className="desk">
    <div className="desk-tabs" role="tablist" aria-label="策略工作台">
      {deskViews.map(({ id, label }, index) => <button
        type="button" className="desk-tab" key={id} id={`desk-tab-${id}`} role="tab"
        aria-selected={view === id} aria-controls={`desk-panel-${id}`} tabIndex={view === id ? 0 : -1}
        onClick={() => openDesk(id)} onKeyDown={(event) => navigateTabs(event, index)}
      >{label}</button>)}
    </div>
    {view === "news" && <header className="instrument-bar"><div className="instrument-id"><h1 id="page-title">新闻页</h1><strong>全球新闻</strong></div><span className="context">真实新闻聚合 · 关联待核验</span></header>}
    {view !== "news" && <div className="desk-panel" id={`desk-panel-${view}`} role="tabpanel" aria-labelledby={`desk-tab-${view}`} tabIndex={0}><PaperTradingDesk view={view} /></div>}
    <details className="manual-research" hidden={view === "news"}>
    <summary>单股研究工具（辅助，不负责自主账户下单）</summary>
    <header className="instrument-bar">
      <div className="instrument-id"><h1 id="manual-page-title">{deskViews.find((desk) => desk.id === view)?.label} · 单股工具</h1>{view === "news" ? <strong>全球新闻</strong> : selected && <strong>{selected.label}</strong>}</div>
      <span className="context">{view === "news" ? "真实新闻聚合 · 关联待核验" : selected ? <>决策日 <time dateTime={selected.decision_date}>{selected.decision_date}</time> · {mode === "ai" ? "AI 分析" : seatId !== "baseline" ? "席位规则" : "离线规则"}</> : "策略研究 · 模拟决策"}</span>
    </header>

    <div className="research-record-bar">
      <button className="secondary" type="button" aria-expanded={research.historyOpen} onClick={() => setResearch(previous => ({ ...previous, historyOpen: !previous.historyOpen }))}>研究记录</button>
      {research.loading && <span role="status">正在载入研究…</span>}
      {research.recordId && !research.loading && <span>{research.archived ? "历史档案 · 只读" : run ? "冻结预案已保存" : "新研究已创建，等待生成预案"}</span>}
    </div>
    {research.error && <div className="callout danger" role="alert">{research.error}</div>}
    {research.historyOpen && <ResearchHistory version={research.historyVersion} disabled={research.loading || busy !== null} onOpen={id => void openResearchRecord(id)} />}
    {view !== "news" && research.context?.news && <section className="research-source" aria-label="本次研究的新闻来源">
      <div><span className="eyebrow">来源新闻 · 模拟</span><h2>{research.context.news.title}</h2><p>{research.context.stock?.label} · {research.context.sectors?.map(item => item.label).join(" / ")}</p></div>
      <details className="disclosure"><summary>查看保存的新闻快照</summary><p>{research.context.news.summary}</p><p className="section-note">{research.context.news.source} · {research.context.news.time} · 关联版本 {research.context.version}</p><p className="section-note">新闻关联仅作为研究背景；本次预案仍按所选席位规则生成，不将新闻直接当成买卖结论。</p></details>
    </section>}
    {research.archived && view !== "news" && <div className="archive-notice"><p>当前为已保存的历史档案，原预案与回放不会重算或覆盖。尚未保存的后续行情不会自动揭示。{!isHistorical && "旧合成记录只读保留；正式演示使用已下载的真实股票。"}</p><button className="secondary" disabled={research.loading || busy !== null} onClick={() => {
      if(!isHistorical && catalog?.data_source==="historical"){
        const sample=catalog.cases.find(item=>item.seat_id===seatId && item.id.startsWith("history-"));
        if(sample)selectHistorical(sample);return;
      }
      if (research.context?.news && research.context.stock) void enterNewsResearch({ newsId: research.context.news.id, stockId: research.context.stock.id, seatId, sectorId: null });
      else { reset(); openDesk("research"); }
    }}>{!isHistorical && catalog?.data_source==="historical" ? "返回真实股票研究" : "以当前情景新建研究"}</button></div>}

    {view !== "news" && <>
      <div className="research-data-context">
        <span className={`pill ${isHistorical ? "good" : ""}`}>{isHistorical ? "真实历史数据" : selected ? "旧合成研究档案" : "等待真实股票数据"}</span>
        <p>{isHistorical ? `档案行情截至 ${market.endDate} · 决策证据截至 ${selected?.decision_date} · ${market.endDate} 为后续观察日。决策日保留前一交易日，是为了避免使用未来信息，不是行情没更新。` : selected ? "当前仅查看以前保存的合成研究记录，不是真实股票，不参与本次真实行情演示。" : "请载入已下载的真实历史股票。正式演示不自动补入虚构股票、财报或成交。"}</p>
      </div>
      <TradeOverview selected={selected} run={run} replay={replay} />
    </>}

    <div className="desk-panel" id="manual-panel-research" hidden={view !== "research"} tabIndex={0}>
    {view === "research" && <>
    <div className="research-source-switch" aria-label="研究数据来源">
      <strong>真实历史演示 · 三席位共用股票目录</strong>
      <span className="section-note">{market.loading ? "正在读取当前股票的席位证据；不生成预案、不交易…" : market.error || "切换席位保留同一股票，分别展示基础、竞价纪律和价值规则的判断与缺口。"}</span>
      <button className="secondary" type="button" disabled={busy!==null || research.loading || market.loading} onClick={()=>{if(!run)selectionTouched.current=false;setRetryCatalog(value=>value+1);}}>重新读取历史目录</button>
    </div>
    {seatId==="buffett" && (catalog?.financial_profiles?.length ?? 0)>0 && <section className="financial-quick-picks" aria-label="已缓存真实财务资料股票"><div><strong>已有真实财务资料</strong><p>当前抓取的历史年度资料，原始历史版本尚未核验；可查看数据，不等于已通过价值买入条件。</p></div><div>{catalog?.financial_profiles?.slice(0,40).map(profile=><button className="secondary" type="button" key={profile.code} disabled={busy!==null || research.loading || market.loading} onClick={()=>selectFinancialStock(profile.code)}>{profile.code}{profile.name ? ` ${profile.name}` : ""} · {profile.report_count ?? 0}期</button>)}</div></section>}
    <HistoricalMarket disabled={busy !== null || research.loading || !seat} onSelect={selectHistorical} selectedCode={isHistorical ? selected?.symbol : undefined} seatId={seatId || "baseline"} />
    <div className="research-grid" aria-label="研究工作区">
      <aside className="casebook" aria-labelledby="case-list-title">
        <div className="seat-field">
          <label htmlFor="seat-select">策略席位</label>
          <select id="seat-select" value={seatId} disabled={!catalog || research.loading || busy!==null} onChange={(event) => chooseSeat(event.target.value)}>
            {!catalog && <option value="">{catalogError ? "席位暂不可用" : "正在读取席位…"}</option>}
            {catalog?.seats.map((item) => <option value={item.id} key={item.id}>{seatLabel(item)}</option>)}
          </select>
        </div>
        <p className="section-note">{seatId==="buffett" ? "当前股票的真实价格与已缓存财报用于价值核验；未下载财报、维持性开支、能力圈或护城河证据缺失时明确观望，不根据K线补造。" : seatId==="livermore" ? "当前股票的真实日线用于120日位置和趋势核验；缺真实涨停价、竞价或指数竞价证据时不强行放行。" : "基础席位使用当前股票真实历史日线。条件预案与成交回放分开，执行证据缺失时不编造买卖。"}</p>
        <div className="case-heading"><h2 id="case-list-title">{selected && !isHistorical ? "旧研究记录" : "真实股票研究对象"}</h2>{visibleCases.length > 0 && <span>{visibleCases.length} 个</span>}</div>
        <p className="section-note">侧栏仅保留初始与最近所选股票摘要，每席位最多40只；完整已下载目录请在上方分页搜索。</p>
        {catalogError && <div className="callout danger" role="alert"><p>{catalogError}</p><button className="secondary" type="button" onClick={() => setRetryCatalog((previous) => previous + 1)}>重新读取案例</button></div>}
        <ol className="case-list">
          {visibleCases.map((item) => <li key={item.id}>
            <button className={`case-option${item.id === caseId ? " active" : ""}`} type="button" disabled={research.loading || busy !== null || !item.id.startsWith("history-")} aria-pressed={item.id === caseId} onClick={() => { if (item.id !== caseId && item.id.startsWith("history-")) selectHistorical(item); }}>
              <strong>{item.label}</strong>
            </button>
          </li>)}
        </ol>
        {catalog && <div className="case-controls">
          {seat?.modes.includes("ai") && <fieldset className="mode-control"><legend>生成模式</legend>
            <label><input type="radio" name="decision-mode" checked={mode === "offline"} disabled={!seat.modes.includes("offline") || research.loading || research.archived} onChange={() => { selectionTouched.current = true; reset(false); setMode("offline"); }} /><span><strong>规则分析</strong><small>规则引擎，不调用模型</small></span></label>
            <label><input type="radio" name="decision-mode" checked={mode === "ai"} disabled={!canUseAI || research.loading || research.archived} onChange={() => { selectionTouched.current = true; reset(false); setMode("ai"); }} /><span><strong>AI 分析</strong><small>{canUseAI ? catalog.model ?? "服务端已配置" : "未配置模型"}</small></span></label>
          </fieldset>}
          <button className="primary" type="button" disabled={!selected || !canAnalyze || busy !== null || research.loading || research.archived} onClick={() => void analyze()}>{busy === "analyze" ? "正在生成预案…" : research.archived ? "历史预案只读" : run ? "新建另一份预案" : "生成并冻结预案"}</button>
        </div>}
      </aside>

      <div className="evidence-column" aria-busy={busy === "analyze"}>
        {run ? <EvidencePanel evidence={run.evidence} seat={run.seat} /> : isHistorical && market.preview?.case_id === caseId && seat ? <EvidencePanel evidence={market.preview} seat={seat} /> : <section className="card evidence-card empty-panel" aria-labelledby="evidence-title">
          <div className="section-heading"><div><p className="eyebrow">时点证据</p><h2 id="evidence-title">{seatId === "buffett" ? "价值评估证据" : "决策日行情"}</h2></div>{selected && <span className="pill">截至 {selected.decision_date}</span>}</div>
          <div className="panel-message"><strong>{selected?.label ?? (catalogError ? "情景暂不可用" : "正在读取研究情景…")}</strong>
            <p>{selected ? seatId === "buffett" ? "生成预案后显示决策时点的财报、生意评估和安全边际；不拿后续财报倒推。" : "生成预案后显示决策日及以前的日线和成交量。" : catalogError ? "请从左侧重新读取案例。" : "连接本地服务后即可选择研究情景。"}</p>
          </div>
        </section>}
      </div>

      <div className="plan-column" aria-busy={busy !== null}>
          {run ? <DecisionPanel run={run}>
          <div className="plan-actions">
            <button className={replay ? "secondary" : "primary"} type="button" disabled={!replay && (busy !== null || research.archived || research.loading)} onClick={() => { openDesk("replay"); if (!replay) void reveal(); }}>
              {replay ? "查看回放明细 →" : research.archived ? "档案未保存后续回放" : busy === "replay" ? "正在回放…" : !run.validation.ok ? "验证回放拦截" : isHistorical ? "揭示真实后续走势 →" : "揭示后续并回放 →"}
            </button>
          </div>
          {operationError?.phase === "analyze" && <div className="callout danger" role="alert"><strong>新预案生成失败，原预案仍保留</strong><p>{operationError.message}</p></div>}
          {operationError?.phase === "replay" && <div className="callout danger" role="alert"><strong>回放失败</strong><p>{operationError.message}</p></div>}
        </DecisionPanel> : <section className="card decision-card empty-panel" aria-labelledby="decision-title">
          <div className="section-heading"><h2 id="decision-title">决策预案</h2><span className="pill">待冻结</span></div>
          <div className="panel-message"><p>{busy === "analyze" ? "正在读取证据并独立校验。" : "先生成并冻结预案，再揭示后续行情。"}</p></div>
          {operationError?.phase === "analyze" && <div className="callout danger" role="alert"><strong>生成失败</strong><p>{operationError.message}</p>{mode === "ai" && seat?.modes.includes("offline") && <p>可手动切换到规则分析。</p>}</div>}
        </section>}
      </div>
    </div>
    </>}
    </div>

    <div className="desk-panel" id="manual-panel-replay" hidden={view !== "replay"} tabIndex={0}>
      {view === "replay" && <div className="replay-stage" aria-busy={busy === "replay"}>
        <section className="card replay-header" aria-labelledby="replay-desk-title">
          <div className="section-heading">
            <div><p className="eyebrow">冻结预案 · 后续走势</p><h2 id="replay-desk-title">{isHistorical ? "真实历史观察" : "模拟回放"}</h2></div>
            {!run || replay ? <button type="button" className="secondary" onClick={() => openDesk("research")}>返回研究台</button>
              : <button type="button" className="primary" disabled={busy !== null || research.archived || research.loading} onClick={() => void reveal()}>{research.archived ? "档案未保存后续回放" : busy === "replay" ? "正在回放…" : run.validation.ok ? isHistorical ? "揭示真实后续走势 →" : "揭示后续并回放 →" : "验证回放拦截"}</button>}
          </div>
          {operationError?.phase === "replay" && <div className="callout danger" role="alert"><strong>回放失败</strong><p>{operationError.message}</p></div>}
        </section>
        <div className="replay-workspace">
          <section className="card replay-lead" aria-labelledby="replay-chart-title">
            <div className="section-heading"><h2 id="replay-chart-title">{run ? `${selected?.label ?? "当前研究情景"} · 日线回放` : "日线回放"}</h2>{run && <span className="pill">决策日 {run.evidence.decision_date}</span>}</div>
            {run ? <>
              <PriceChart bars={run.evidence.bars} replay={{ decisionDate: run.evidence.decision_date, bars: replay?.replay_bars ?? [], events: replay?.events, revealed: Boolean(replay && replay.status !== "blocked") }} />
              {!run.validation.ok && <p className="section-note">风控未通过，回放只验证拦截，不显示后续行情。</p>}
              {replay && replay.status !== "blocked" && replay.replay_bars === undefined && <p className="section-note">服务未返回回放 K 线，以下成交明细仍可查看。</p>}
            </> : <div className="panel-message replay-locked"><p>{busy === "analyze" ? "预案正在生成，完成后即可回放。" : "在研究台选择案例并冻结预案后，才能揭示后续行情。"}</p></div>}
          </section>
          <ReplaySummary replay={replay} run={run} />
        </div>
        {replay && run && <section className="card replay-results" aria-labelledby="replay-title">
          <div className="section-heading"><h2 id="replay-title">回放明细</h2><span className="pill">{replay.events.length} 条事件</span></div>
          <ReplayEvents replay={replay} />
        </section>}
      </div>}
    </div>

    <div className="desk-panel" id="manual-panel-audit" hidden={view !== "audit"} tabIndex={0}>
      {view === "audit" && <>
        <section className="card audit-lead" aria-labelledby="audit-lead-title">
          <div><p className="eyebrow">决策闸门</p><h2 id="audit-lead-title">{!run ? "等待冻结预案" : run.validation.ok ? isHistorical ? "观望预案通过，可观察后续价格" : "风控放行，允许模拟回放" : "风控拦截，不揭示后续行情"}</h2>
            <p className="section-note">{run ? "冻结的预案只使用决策日及以前的数据。切换工作台不会重新生成。" : busy === "analyze" ? "正在生成预案并独立校验。" : "在研究台生成预案后，这里会显示独立校验、引用证据和调用轨迹。"}</p></div>
          <div className="audit-controls">
            {run && <div className={`audit-score${run.validation.ok ? "" : " danger"}`}><strong>{auditPassed} / {run.validation.checks.length}</strong><small>校验通过</small></div>}
            <button type="button" className="secondary" onClick={() => openDesk(run ? "replay" : "research")}>{run ? "前往复盘台 →" : "返回研究台"}</button>
          </div>
        </section>
        {run && <AuditPanels run={run} />}
      </>}
    </div>

    <div className="bottom-strip" role="status" hidden={view === "news"}>
      <strong className={run && !run.validation.ok ? "danger" : undefined}>{replay ? statusLabels[replay.status] : busy === "replay" ? "正在回放" : run && !run.validation.ok ? "预案已冻结 · 风控拦截" : run ? "预案已冻结 · 风控通过" : busy === "analyze" ? "正在生成预案" : "后续行情尚未揭示"}</strong>
      <span>{replay ? replay.status === "blocked" ? "后续行情未揭示" : replay.observation_only ? "仅观察后续价格 · 未产生买卖收益" : `${replay.metrics.is_open ? "权益变动（含浮盈亏）" : "虚拟净值"} ${percent(replay.metrics.return_pct, true)} · ${replay.events.length} 条回放事件` : run && !run.validation.ok ? "可验证后端拦截，不请求后续行情。" : run ? "回放仅使用当前冻结预案。" : "决策只使用截至所选日期的证据。"}</span>
    </div>
    </details>

    <div className="desk-panel" id="desk-panel-news" role="tabpanel" aria-labelledby="desk-tab-news" hidden={view !== "news"} tabIndex={0}>
      {view === "news" && <NewsDesk seatId={seatId} onResearch={selection => void enterNewsResearch(selection)} />}
    </div>

  </div>;
}

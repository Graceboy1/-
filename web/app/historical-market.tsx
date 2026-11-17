"use client";

import { useEffect, useRef, useState } from "react";
import { PriceChart, type Bar } from "./price-chart";

export type HistoricalSample = { id: string; seat_id: string; label: string; symbol: string; name: string; decision_date: string; description: string; data_mode?: string };
export type HistoricalEvidence = { case_id: string; symbol: string; seat_id?:string; decision_date: string; bars: Bar[]; evidence_ids: string[]; metrics: { data_valid?: boolean; last_close?: number | null; [key: string]: unknown }; limitations?: string[] };
type Stock = { code: string; name: string; first_date: string; last_date: string; bar_count: number; sample: HistoricalSample };
type Page = { items: Stock[]; total: number; limit: number; offset: number };
type Status = { available: boolean; status: string; downloaded: number; failed?: number; pending?: number; eligible_total?: number; complete_for_target?: number; end_date: string; decision_date: string; warning?: string };
type Detail = { sample: HistoricalSample; source: string; history_start: string; history_bar_count: number; replay_day: string; revealed: boolean;
  evidence: HistoricalEvidence & { limitations: string[] }; observed_bars?: Bar[] };

async function read<T>(path: string, signal: AbortSignal): Promise<T> {
  const response = await fetch(`/contest-api/market/${path}`, { credentials: "same-origin", redirect: "error", cache: "no-store", signal });
  if (response.status === 401) window.dispatchEvent(new Event("contest-session-expired"));
  const value = await response.json().catch(() => null);
  if (!response.ok) throw new Error(typeof value?.detail === "string" ? value.detail : "历史行情读取失败，请重试。");
  if (!value || typeof value !== "object") throw new Error("历史行情格式不正确。");
  return value as T;
}

export default function HistoricalMarket({ disabled, onSelect, selectedCode, seatId="baseline" }: { disabled: boolean; onSelect: (sample: HistoricalSample, evidence?: HistoricalEvidence) => void; selectedCode?: string; seatId?:string }) {
  const [status, setStatus] = useState<Status | null>(null);
  const [q, setQ] = useState("");
  const [offset, setOffset] = useState(0);
  const [page, setPage] = useState<Page | null>(null);
  const [code, setCode] = useState("");
  const [detail, setDetail] = useState<Detail | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [revision, setRevision] = useState(0);
  const detailRequest = useRef<AbortController | null>(null);
  const selection = useRef(onSelect);
  selection.current = onSelect;
  const seatName=seatId==="buffett" ? "巴菲特席位" : seatId==="livermore" ? "利弗莫尔席位" : "基础席位";

  useEffect(() => {
    const controller = new AbortController();
    setError(""); setPage(null);
    const timer = window.setTimeout(() => {
      void Promise.all([read<Status>("status", controller.signal), read<Page>(`stocks?q=${encodeURIComponent(q)}&limit=20&offset=${offset}&seat_id=${seatId}`, controller.signal)])
        .then(([current, result]) => {
          if (controller.signal.aborted) return;
          if (!Array.isArray(result.items) || result.items.length > 20 || !Number.isSafeInteger(result.total) || result.total < 0 || result.items.some(s => !/^(?:000|001|002|003|600|601|603|605)\d{3}$/.test(s.code) || s.sample?.seat_id !== seatId)) throw new Error("股票目录或席位格式不正确。");
          setStatus(current); setPage(result);
        }).catch(e => { if (!controller.signal.aborted) setError(e instanceof Error ? e.message : "历史数据暂不可读。"); });
    }, 200);
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [q, offset, revision, seatId]);
  useEffect(() => () => detailRequest.current?.abort(), []);
  useEffect(() => { if (disabled) { detailRequest.current?.abort(); setBusy(false); } }, [disabled]);
  useEffect(()=>{detailRequest.current?.abort();setDetail(null);setBusy(false);setError("");},[seatId]);
  useEffect(()=>{
    // Parent sidebar/financial shortcuts can change the stock independently
    // of this picker. Never let an older detail request reselect that stock.
    if ((selectedCode || "") !== code) {
      detailRequest.current?.abort();detailRequest.current=null;
      setDetail(null);setBusy(false);setError("");setCode(selectedCode || "");
    }
  },[selectedCode]);

  async function load(next: string, reveal = false) {
    if (disabled || !["baseline","livermore","buffett"].includes(seatId) || !/^(?:000|001|002|003|600|601|603|605)\d{3}$/.test(next)) return;
    detailRequest.current?.abort();
    const controller = new AbortController(); detailRequest.current = controller;
    setBusy(true); setError(""); setDetail(null); setCode(next);
    try {
      const data = await read<Detail>(`stocks/${next}?seat_id=${seatId}${reveal ? "&reveal=true" : ""}`, controller.signal);
      if (controller.signal.aborted) return;
      if (data.sample?.id !== `history-20261113-${next}` || data.sample.symbol !== next || data.sample.seat_id !== seatId
        || data.sample.decision_date !== "2026-11-12" || data.evidence?.decision_date !== data.sample.decision_date || !Array.isArray(data.evidence?.bars)
        || data.evidence.seat_id != null && data.evidence.seat_id !== seatId
        || data.evidence.case_id !== data.sample.id || data.evidence.symbol !== next || !data.evidence.metrics || !Array.isArray(data.evidence.evidence_ids)
        || data.evidence.bars.some(b => b.date > data.evidence.decision_date) || data.revealed !== reveal
        || (!reveal && (data.observed_bars?.length ?? 0) > 0)) throw new Error("行情超出研究时点，已拒绝展示。");
      setDetail(data);
      // Picking a stock also updates the research desk. Merely reading prices
      // never generates a plan, calls a model, or simulates a trade.
      if (!reveal) selection.current(data.sample, data.evidence);
    } catch (e) { if (!controller.signal.aborted) setError(e instanceof Error ? e.message : "读取失败。"); }
    finally { if (detailRequest.current === controller) { detailRequest.current = null; setBusy(false); } }
  }

  return <section className="historical-market" aria-label="真实历史股票行情">
    <div className="section-heading"><div><p className="eyebrow">真实历史行情 · 非实时</p><h2>2026年11月13日 · 本地行情档案</h2></div><button className="secondary" type="button" onClick={() => setRevision(v => v + 1)}>刷新下载状态</button></div>
    <p className="section-note">{status ? `已下载 ${status.downloaded} / ${status.eligible_total ?? "—"} 只，${status.complete_for_target ?? 0} 只含周五记录；失败 ${status.failed ?? 0}，待下载 ${status.pending ?? 0}。` : "正在读取本地数据…"} 日线不复权，成交量单位：股。</p>
    <p className="section-note">档案行情截至 {status?.end_date ?? "2026-11-13"}；研究决策日 {status?.decision_date ?? "2026-11-12"}；周五 {status?.end_date ?? "2026-11-13"} 留作后续观察，不提前用于预案。下载不是自动交易。</p>
    <p className="section-note">沪深主板非ST名单，非历史成分重建。三个席位共用真实股票日线，当前为{seatName}；竞价、五档、逐笔及历史涨跌停价缺失时不补造，价值财务资料另行核验。</p>
    <div className="historical-workspace">
      <div className="historical-picker"><label className="historical-search">搜索代码或名称<input type="search" maxLength={32} value={q} onChange={e => {setQ(e.target.value);setOffset(0);}} placeholder="例如 600000 / 浦发银行" /></label>
        <div className="historical-list" aria-label="历史股票列表">{page?.items.map(stock => <button className={`historical-stock${(selectedCode || code) === stock.code ? " active" : ""}`} key={stock.code} disabled={disabled} type="button" onClick={() => void load(stock.code)} aria-pressed={(selectedCode || code) === stock.code}>
          <strong>{stock.code}　{stock.name}</strong><small>{stock.bar_count} 根日线 · 最后 {stock.last_date}{stock.last_date < "2026-11-13" ? " · 周五无记录" : ""}</small>
        </button>)}{page?.items.length === 0 && <p>没有匹配的已下载股票。</p>}</div>
        <div className="history-pagination"><span>{page ? `共 ${page.total} 只` : "读取中…"}</span><button className="secondary" disabled={!page || offset === 0} onClick={() => setOffset(v => Math.max(0, v - 20))}>上一页</button><button className="secondary" disabled={!page || offset + 20 >= page.total} onClick={() => setOffset(v => v + 20)}>下一页</button></div>
      </div>
      <div className="historical-detail" aria-busy={busy}>{error && <div className="callout danger" role="alert">{error}</div>}
        {detail ? <><div className="section-heading"><h3>{detail.sample.symbol}　{detail.sample.name}</h3><span className="pill">来源：{detail.source === "sina" ? "新浪财经" : "腾讯证券"}</span></div>
          <p className="section-note">归档起始 {detail.history_start} · 共 {detail.history_bar_count} 根日线。研究只读截至 {detail.evidence.decision_date} 的最近120根，不使用周五结果。</p>
          <PriceChart bars={detail.evidence.bars} replay={{decisionDate: detail.evidence.decision_date, bars:detail.observed_bars ?? [],revealed:detail.revealed}} />
          <div className="historical-actions"><button className="primary" disabled={disabled || busy} onClick={() => onSelect(detail.sample, detail.evidence)}>查看下方{seatName}研究</button><button className="secondary" disabled={disabled || busy || detail.revealed} onClick={() => void load(code, true)}>{detail.revealed ? "已揭示周五行情" : "仅揭示周五行情（不模拟成交）"}</button></div>
          {detail.revealed && <p className="section-note">仅观察历史价格，不是预案成交回放。{detail.observed_bars?.length ? "周五买入当天仍受T+1约束，不能据当日涨跌计算可兑现收益。" : "该股票周五没有返回有效日线，可能涉及停牌或缺失，未补造记录。"}</p>}
          <p className="section-note">{detail.evidence.limitations.join(" ")}</p>
        </> : <p className="empty-copy">{busy ? "正在按股票读取历史数据…" : selectedCode ? `下方研究台已选中 ${selectedCode}。点击左侧股票可同步更换研究对象；按20只分页、按单只加载，不自动交易。` : "点击左侧股票，同步显示下方决策日行情；不需要再次带入，不会自动生成预案或交易。"}</p>}
      </div>
    </div>
  </section>;
}

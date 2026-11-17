"use client";

import { useEffect, useState } from "react";

export type ResearchContext = {
  case_id: string; seat_id: string; data_mode: string; version?: string;
  news?: { id: string; title: string; time?: string; source?: string; summary?: string };
  stock?: { id: string; label: string; code?: string | null };
  sectors?: { id: string; label: string; reason?: string }[];
};
type RecordSummary = {
  record_id: string; status: string; created_at: string; updated_at: string; case_id: string; seat_id: string;
  stock_label?: string | null; news_title?: string | null; decision_date?: string | null;
  title?: string;
  return_pct?: number | null; has_replay?: boolean;
};
type RecordPage = { items: RecordSummary[]; total: number; limit: number; offset: number };
const labels: Record<string, string> = { livermore: "利弗莫尔席位", buffett: "巴菲特席位", baseline: "基础策略席位" };

export default function ResearchHistory({ version, disabled, onOpen }: { version: number; disabled: boolean; onOpen: (id: string) => void }) {
  const [date, setDate] = useState("");
  const [seat, setSeat] = useState("");
  const [offset, setOffset] = useState(0);
  const [page, setPage] = useState<RecordPage | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true); setError(""); setPage(null);
    const query = new URLSearchParams({ limit: "10", offset: String(offset) });
    if (date) query.set("date", date);
    if (seat) query.set("seat_id", seat);
    void fetch(`/contest-api/research/records?${query}`, { credentials: "same-origin", cache: "no-store", signal: controller.signal })
      .then(async response => {
        const result = await response.json();
        if (response.status === 401) window.dispatchEvent(new Event("contest-session-expired"));
        if (!response.ok) throw new Error(typeof result?.detail === "string" ? result.detail : "研究记录读取失败。");
        if (!Array.isArray(result.items) || !Number.isSafeInteger(result.total) || result.total < 0
          || result.items.length > 10 || result.items.some((item: RecordSummary) => !/^[a-f0-9]{32}$/.test(item.record_id))) throw new Error("研究记录格式不正确。");
        if (!controller.signal.aborted) setPage(result);
      }).catch(error => { if (!controller.signal.aborted) setError(error instanceof Error ? error.message : "研究记录暂不可用。"); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [date, seat, offset, version, retry]);
  return <section className="research-history" aria-labelledby="research-history-title" aria-busy={loading}>
    <div className="section-heading"><h2 id="research-history-title">研究记录</h2><span className="section-note">保存在本机 · 按创建日查找</span></div>
    <div className="history-filters">
      <label>日期<input aria-label="研究记录创建日期" type="date" value={date} onChange={event => { setDate(event.target.value); setOffset(0); }} /></label>
      <label>席位<select aria-label="研究记录席位" value={seat} onChange={event => { setSeat(event.target.value); setOffset(0); }}><option value="">全部席位</option>{Object.entries(labels).map(([id, label]) => <option key={id} value={id}>{label}</option>)}</select></label>
      {(date || seat) && <button type="button" className="secondary" onClick={() => { setDate(""); setSeat(""); setOffset(0); }}>清空筛选</button>}
    </div>
    {loading && <p role="status">正在读取研究记录…</p>}
    {error && <div role="alert" className="callout danger"><p>{error}</p><button type="button" className="secondary" onClick={() => setRetry(value => value + 1)}>重试</button></div>}
    {page && !page.items.length && <p className="section-note">当前筛选下还没有研究记录。进入新闻研究或冻结预案后会自动保存。</p>}
    {page && page.items.length > 0 && <>
      <div className="table-scroll" role="region" aria-label="历史研究列表" tabIndex={0}><table className="history-table"><thead><tr><th>创建时间</th><th>研究对象 / 来源</th><th>席位</th><th>状态</th><th>回放收益</th><th>操作</th></tr></thead><tbody>
        {page.items.map(item => <tr key={item.record_id}><td>{new Date(item.created_at).toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false })}</td><td><strong>{item.stock_label || item.title || "研究情景"}</strong>{item.news_title && <small>{item.news_title}</small>}</td><td>{labels[item.seat_id] || item.seat_id}</td><td>{item.status === "draft" ? "待研究" : item.has_replay || item.status === "replayed" ? "已回放" : "预案已冻结"}</td><td>{item.return_pct != null && Number.isFinite(item.return_pct) ? `${item.return_pct > 0 ? "+" : ""}${item.return_pct.toFixed(2)}%` : "—"}</td><td><button className="secondary" type="button" disabled={disabled} onClick={() => onOpen(item.record_id)}>打开记录</button></td></tr>)}
      </tbody></table></div>
      <div className="history-pagination"><span>共 {page.total} 条 · 第 {Math.floor(offset / 10) + 1} 页</span><button className="secondary" disabled={loading || offset === 0} onClick={() => setOffset(value => Math.max(0, value - 10))}>上一页</button><button className="secondary" disabled={loading || offset + page.items.length >= page.total} onClick={() => setOffset(value => value + 10)}>下一页</button></div>
    </>}
  </section>;
}

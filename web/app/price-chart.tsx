export type Bar = {
  date: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  limit_up: number | null;
  limit_down: number | null;
  suspended: boolean;
};

type ReplayChart = {
  decisionDate: string;
  bars: Bar[];
  revealed?: boolean;
  events?: { date: string; type: string; price: number | null }[];
};

const priceFormat = new Intl.NumberFormat("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const volumeFormat = new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 0 });
const isDate = (date: unknown): date is string => typeof date === "string" && /^\d{4}-\d{2}-\d{2}$/.test(date);

function validBar(bar: Bar) {
  return bar != null && isDate(bar.date) && [bar.open, bar.high, bar.low, bar.close, bar.volume].every(Number.isFinite)
    && bar.low > 0 && bar.high >= Math.max(bar.open, bar.close) && bar.low <= Math.min(bar.open, bar.close) && bar.volume >= 0;
}

// The replay response is already gated server-side. Also separate the two data sources by
// the frozen cutoff here so a mislabeled bar cannot be drawn on the wrong side of the line.
export function PriceChart({ bars, replay }: { bars: Bar[]; replay?: ReplayChart }) {
  const cutoff = replay?.decisionDate;
  const validCutoff = isDate(cutoff);
  const history = bars.filter((bar) => validBar(bar) && (!replay || (validCutoff && bar.date <= cutoff)));
  if (replay) history.sort((a, b) => a.date.localeCompare(b.date));
  const plotted = history.slice(-45);
  if (!plotted.length) return <p className="empty-copy">没有可绘制的有效日线。原始证据及异常记录可在研究台查看。</p>;

  const future = replay && validCutoff && replay.revealed !== false
    ? replay.bars.filter((bar) => validBar(bar) && bar.date > cutoff).sort((a, b) => a.date.localeCompare(b.date)) : [];
  const observed = [...plotted, ...future];
  const min = Math.min(...observed.map((bar) => bar.low));
  const max = Math.max(...observed.map((bar) => bar.high));
  const padding = Math.max((max - min) * 0.07, max * 0.002);
  const axisLow = min - padding;
  const axisHigh = max + padding;
  const y = (price: number) => 202 - (price - axisLow) / (axisHigh - axisLow) * 182;
  const maxVolume = Math.max(1, ...observed.map((bar) => bar.volume));
  const viewWidth = replay ? 1000 : 660;
  const viewHeight = replay ? 320 : 290;
  const left = replay ? 68 : 56;
  const right = viewWidth - 12;
  const plotWidth = right - left;
  const futureWidth = replay ? plotWidth * 0.22 : 0;
  const cutoffX = right - futureWidth;
  const historyStep = (cutoffX - left) / plotted.length;
  const futureStep = futureWidth / Math.max(1, future.length);
  const futureX = (index: number) => cutoffX + futureStep * (index + 0.5);
  const volumeBase = replay ? 283 : 261;
  const volumeRange = replay ? 57 : 42;
  const guides = Array.from({ length: 5 }, (_, index) => axisHigh - (axisHigh - axisLow) * index / 4);
  const lastHistory = plotted[plotted.length - 1];
  const lastFuture = future[future.length - 1];
  const notDrawn = bars.length - history.length;
  const futureIndices = new Map(future.map((bar, index) => [bar.date, index]));
  const marks = (replay?.events ?? []).flatMap((event, index) => {
    const barIndex = futureIndices.get(event.date);
    if ((event.type !== "buy" && event.type !== "sell") || barIndex == null || typeof event.price !== "number"
      || !Number.isFinite(event.price) || event.price < axisLow || event.price > axisHigh) return [];
    return [{ ...event, price: event.price, barIndex, index }];
  });
  const hasBuy = marks.some((mark) => mark.type === "buy");
  const hasSell = marks.some((mark) => mark.type === "sell");
  const futureNote = replay?.revealed ? "无可显示的回放日线" : "后续待揭示";
  const label = replay
    ? `截至${cutoff}的${plotted.length}根证据日K及成交量；虚线右侧${lastFuture ? `显示${future.length}根回放日K${marks.length ? `及${marks.length}个模拟成交标记` : ""}` : futureNote}`
    : `截至${lastHistory.date}的${plotted.length}根有效日K及成交量`;

  function candle(bar: Bar, index: number, x: number, step: number, source: "evidence" | "replay") {
    const open = y(bar.open);
    const close = y(bar.close);
    const width = Math.min(14, Math.max(2, step * 0.57));
    const volumeHeight = bar.volume / maxVolume * volumeRange;
    return <g key={`${source}-${bar.date}-${index}`} className={bar.suspended ? "candle-flat" : bar.close >= bar.open ? "candle-up" : "candle-down"}>
      <title>{`${bar.date}：开 ${priceFormat.format(bar.open)}，高 ${priceFormat.format(bar.high)}，低 ${priceFormat.format(bar.low)}，收 ${priceFormat.format(bar.close)}，量 ${volumeFormat.format(bar.volume)}`}</title>
      <line x1={x} x2={x} y1={y(bar.high)} y2={y(bar.low)} />
      <rect x={x - width / 2} y={Math.min(open, close)} width={width} height={Math.max(1, Math.abs(close - open))} />
      <rect x={x - width / 2} y={volumeBase - volumeHeight} width={width} height={volumeHeight} opacity="0.6" />
    </g>;
  }

  const graph = <svg viewBox={`0 0 ${viewWidth} ${viewHeight}`} role="img" aria-label={label}>
      <title>{replay ? "冻结时点与后续模拟回放日线" : "决策日以前的日线证据"}</title>
      <desc>上半部为K线，下半部为成交量。红色表示收盘不低于开盘，绿色表示收盘低于开盘，灰色表示停牌。{replay && "虚线左侧是决策日及以前的证据，右侧是回放区。"}</desc>
      {replay && <rect className="future-zone" x={cutoffX} y="0" width={futureWidth} height={volumeBase} />}
      {guides.map((value, index) => <g key={index}>
        <line x1={left} x2={right} y1={y(value)} y2={y(value)} className="chart-grid" />
        <text className="axis" x={left - 9} y={y(value) + 4} textAnchor="end">{priceFormat.format(value)}</text>
      </g>)}
      <line x1={left} x2={right} y1={volumeBase} y2={volumeBase} className="chart-grid" />
      <text className="axis" x={left - 9} y={volumeBase - 24} textAnchor="end">成交量</text>
      {plotted.map((bar, index) => candle(bar, index, left + historyStep * (index + 0.5), historyStep, "evidence"))}
      {future.map((bar, index) => candle(bar, index, futureX(index), futureStep, "replay"))}
      {replay && <line className="cutoff" x1={cutoffX} x2={cutoffX} y1="0" y2={volumeBase} />}
      {replay && !lastFuture && <text className="chart-note" x={cutoffX + futureWidth / 2} y="113" textAnchor="middle">{futureNote}</text>}
      {marks.map((mark) => {
        const cx = futureX(mark.barIndex);
        const cy = y(mark.price);
        const title = <title>{`${mark.date} · ${mark.type === "buy" ? "模拟买入" : "模拟卖出"} ${priceFormat.format(mark.price)} 元`}</title>;
        return mark.type === "buy"
          ? <circle key={mark.index} className="execution-buy" cx={cx} cy={cy} r="6">{title}</circle>
          : <rect key={mark.index} className="execution-sell" x={cx - 5} y={cy - 5} width="10" height="10">{title}</rect>;
      })}
      <text className="axis" x={left} y={viewHeight - 9}>{plotted[0].date}</text>
      {(plotted.length > 1 || replay) && <text className="axis" x={cutoffX - (replay ? 6 : 0)} y={viewHeight - 9} textAnchor="end">{replay ? cutoff : lastHistory.date}</text>}
      {lastFuture && <text className="axis" x={right} y={viewHeight - 9} textAnchor="end">{lastFuture.date}</text>}
  </svg>;

  return <figure className={replay ? "chart replay-chart" : "chart"}>
    {replay ? <div className="replay-chart-frame" role="region" aria-label="复盘行情图，可横向滚动" tabIndex={0}>{graph}</div> : graph}
    <figcaption>{replay ? <>
      <span>虚线：决策日 {cutoff} · 左侧 {plotted.length} 根证据日线{lastFuture ? ` · 右侧 ${future.length} 根回放日线` : ` · ${futureNote}`}</span>
      <span>{[hasBuy ? "圆点：模拟买入" : "", hasSell ? "方块：模拟卖出" : ""].filter(Boolean).join(" · ") || "日线与成交量 · 红涨绿跌"}</span>
    </> : <>最近 {plotted.length} 根有效日线 · 红涨绿跌{notDrawn ? ` · ${notDrawn} 条异常记录未绘制` : ""}</>}</figcaption>
  </figure>;
}

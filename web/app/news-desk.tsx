"use client";

import { useEffect, useRef, useState } from "react";

/** The adopted globe is bundled with this app, not served by a preview process.
 * Mount only while the news tab is active: removing the frame releases its canvas,
 * listeners and document. No credentials or model requests enter this module.
 */
export type NewsResearchRequest = { newsId: string; stockId: string; seatId: string; sectorId: string | null };

export default function NewsDesk({ seatId, onResearch }: { seatId: string; onResearch?: (selection: NewsResearchRequest) => void }) {
  const frame = useRef<HTMLIFrameElement>(null);
  const researchHandler = useRef(onResearch);
  researchHandler.current = onResearch;
  const [status, setStatus] = useState<"loading" | "ready" | "error">("loading");
  const [attempt, setAttempt] = useState(0);
  const seat = ["livermore", "buffett", "baseline"].includes(seatId) ? seatId : "livermore";

  useEffect(() => {
    setStatus("loading");
    const receive = (event: MessageEvent) => {
      if (event.origin !== window.location.origin || event.source !== frame.current?.contentWindow
        || event.data?.channel !== "news-earth") return;
      if (event.data.type === "ready") setStatus("ready");
      if (event.data.type === "error") setStatus("error");
      if (event.data.type === "session-expired") window.dispatchEvent(new Event("contest-session-expired"));
      if (event.data.type === "research") {
        const { newsId, stockId, seatId: selectedSeat, sectorId } = event.data;
        const validId = (value: unknown): value is string => typeof value === "string" && /^[A-Za-z0-9_-]{1,64}$/.test(value);
        if (!validId(newsId) || !validId(stockId) || !["livermore", "buffett", "baseline"].includes(selectedSeat)
          || (sectorId !== null && sectorId !== undefined && !validId(sectorId))) return;
        researchHandler.current?.({ newsId, stockId, seatId: selectedSeat, sectorId: sectorId ?? null });
      }
    };
    window.addEventListener("message", receive);
    const timeout = window.setTimeout(() => setStatus(current => current === "loading" ? "error" : current), 15000);
    return () => { window.removeEventListener("message", receive); window.clearTimeout(timeout); };
  }, [attempt, seat]);

  return <section className="news-desk" aria-label="地球新闻与板块股票关联" aria-busy={status === "loading"}>
    {status !== "ready" && <div className="news-loading" role={status === "error" ? "alert" : "status"}>
      <strong>{status === "error" ? "新闻页暂未载入" : "正在载入新闻地图…"}</strong>
      {status === "error" && <button className="secondary" type="button" onClick={() => { setStatus("loading"); setAttempt(value => value + 1); }}>重新加载新闻页</button>}
    </div>}
    <iframe key={`${seat}-${attempt}`} ref={frame} className="news-frame"
      title="新闻页：地球定位、板块关联与股票列表"
      src={`/news-earth/index.html?view=orbit&embed=1&mode=live&seat=${encodeURIComponent(seat)}`}
      referrerPolicy="no-referrer" onError={() => setStatus("error")} />
  </section>;
}

"""Personal, bounded news reader. Public feeds never become trading evidence.

Only allowlisted sources are fetched, never article bodies or caller URLs.
The cache is independent of accounts, model credentials and research records.
"""
from __future__ import annotations

import asyncio
from contextlib import closing, suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import hashlib
from html import unescape
import json
import os
from pathlib import Path
import re
import sqlite3
import time
from typing import Annotated
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import xml.etree.ElementTree as ET

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query

from .access import WorkspaceSession, require_ready
from . import market_history, news_relations
from .seats import SEATS

router = APIRouter(prefix="/news", tags=["global news"])
Ready = Annotated[WorkspaceSession, Depends(require_ready)]
ROOT = Path(__file__).resolve().parents[1]
REFRESH_SECONDS, RETENTION_DAYS, MAX_ROWS = 300, 7, 5000
MAX_FEED_BYTES, MAX_ITEMS_PER_SOURCE = 2_000_000, 100


@dataclass(frozen=True)
class Source:
    id: str
    label: str
    url: str
    hosts: tuple[str, ...]
    language: str = "zh"
    kind: str = "rss"
    category: str = "国际新闻"


# Checked against the publishers' public feed endpoints. No proxy or scraping
# fallback is used when a source denies access. See docs/全球新闻使用说明.md.
SOURCES = (
    Source("sina-global", "新浪财经 · 全球市场", "https://feed.mix.sina.com.cn/api/roll/get?pageid=384&lid=2676&num=20&page=1", ("finance.sina.com.cn",), kind="sina", category="全球财经"),
    Source("chinanews-world", "中国新闻网 · 国际", "https://www.chinanews.com.cn/rss/world.xml", ("chinanews.com.cn",)),
    Source("fed-press", "美联储 · 官方公告", "https://www.federalreserve.gov/feeds/press_all.xml", ("federalreserve.gov",), "en", category="全球宏观公告"),
    Source("ecb-press", "欧洲央行 · 官方公告", "https://www.ecb.europa.eu/rss/press.html", ("ecb.europa.eu",), "en", category="全球宏观公告"),
)
_refresh_task: asyncio.Task | None = None
_scheduler_task: asyncio.Task | None = None
_last_trigger = 0.0


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def stamp(value: datetime | None = None) -> str:
    return (value or utcnow()).astimezone(timezone.utc).isoformat(timespec="seconds")


def db_path() -> Path:
    return Path(os.environ.get("CONTEST_NEWS_DB") or ROOT / "data" / "global-news.sqlite3").resolve()


def connect() -> sqlite3.Connection:
    path = db_path()
    if not os.environ.get("CONTEST_NEWS_DB") and path.parent == ROOT / "data":
        path.parent.mkdir(exist_ok=True)
    if not path.parent.is_dir():
        raise sqlite3.OperationalError("News cache parent is missing")
    conn = sqlite3.connect(path, timeout=3)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    with conn:
        conn.execute("CREATE TABLE IF NOT EXISTS articles (id TEXT PRIMARY KEY,source_id TEXT NOT NULL,url TEXT NOT NULL UNIQUE,title TEXT NOT NULL,summary TEXT NOT NULL,published_at TEXT NOT NULL,first_seen TEXT NOT NULL,last_seen TEXT NOT NULL,geo_json TEXT,location_basis TEXT NOT NULL)")
        conn.execute("CREATE INDEX IF NOT EXISTS articles_date ON articles(published_at DESC,id)")
        conn.execute("CREATE TABLE IF NOT EXISTS sources (id TEXT PRIMARY KEY,status TEXT NOT NULL DEFAULT 'pending',last_attempt TEXT,last_success TEXT,etag TEXT,modified TEXT,error TEXT,blocked INTEGER NOT NULL DEFAULT 0,latest_published TEXT)")
        conn.executemany("INSERT OR IGNORE INTO sources(id) VALUES(?)", [(s.id,) for s in SOURCES])
    return conn


def clean_text(value: str | None, limit: int) -> str:
    # Remote text is untrusted and never interpolated as HTML by the client.
    value = re.sub(r"(?is)<(script|style)\b[^>]*>.*?</\1>", " ", str(value or ""))
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]*>", " ", value))).strip()[:limit]


def article_url(value: str, source: Source) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parts = urlsplit(unescape(value).strip())
        host = (parts.hostname or "").lower()
        if parts.scheme not in {"http", "https"} or parts.username or parts.password or parts.port not in {None, 80, 443}:
            return None
        if not any(host == allowed or host.endswith("." + allowed) for allowed in source.hosts):
            return None
        query = urlencode([(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                           if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid"}])
        # Publisher links are displayed, not fetched. Use their HTTPS service.
        return urlunsplit(("https", host, parts.path or "/", query, ""))[:2000]
    except (ValueError, TypeError):
        return None


def published(value: str | None, now: datetime) -> str | None:
    try:
        if not value:
            return None
        try:
            parsed = parsedate_to_datetime(value.strip())
        except (ValueError, TypeError):
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None  # Do not invent a publisher timezone.
        parsed = parsed.astimezone(timezone.utc)
        if parsed > now + timedelta(minutes=15) or parsed < now - timedelta(days=RETENTION_DAYS):
            return None
        return stamp(parsed)
    except (ValueError, TypeError, OverflowError):
        return None


def parse_feed(content: bytes, source: Source, *, now: datetime | None = None) -> list[dict]:
    now = now or utcnow()
    if len(content) > MAX_FEED_BYTES or re.search(br"<!\s*(DOCTYPE|ENTITY)\b", content, re.I):
        raise ValueError("Unsafe or oversized feed")
    root = ET.fromstring(content)
    items = root.findall("./channel/item")
    atom = "{http://www.w3.org/2005/Atom}"
    if not items:
        items = root.findall(f"{atom}entry")
    if len(items) > 1000 or (not items and root.tag not in {"rss", atom + "feed", "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}RDF"}):
        raise ValueError("Not a supported news feed")
    result, seen = [], set()
    for item in items[:MAX_ITEMS_PER_SOURCE]:
        title = clean_text(item.findtext("title") or item.findtext(atom + "title"), 280)
        raw_url = item.findtext("link")
        if not raw_url:
            link = item.find(atom + "link")
            raw_url = link.get("href", "") if link is not None else ""
        url = article_url(raw_url or "", source)
        date = published(item.findtext("pubDate") or item.findtext(atom + "published") or item.findtext(atom + "updated"), now)
        if not title or not url or not date or url in seen:
            continue
        seen.add(url)
        # Keep only a short publisher-supplied excerpt; never retrieve full text.
        excerpt = clean_text(item.findtext("description") or item.findtext(atom + "summary"), 160)
        if source.language != "zh":
            excerpt = " ".join(excerpt.split()[:24])
        geo, basis = news_relations.infer_location(title + " " + excerpt)
        result.append({"id": "live-" + hashlib.sha256(url.encode()).hexdigest()[:24], "source_id": source.id,
                       "url": url, "title": title, "summary": excerpt, "published_at": date,
                       "geo": geo, "location_basis": basis})
    return result


def parse_sina(content: bytes, source: Source, *, now: datetime | None = None) -> list[dict]:
    now = now or utcnow()
    if len(content) > MAX_FEED_BYTES:
        raise ValueError("Oversized news response")
    payload = json.loads(content)
    result = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(result, dict) or not isinstance(result.get("status"), dict):
        raise ValueError("Not a global-market response")
    items = result.get("data")
    if result.get("status", {}).get("code") != 0 or not isinstance(items, list) or len(items) > 1000:
        raise ValueError("Not a valid global-market response")
    articles, seen = [], set()
    for item in items[:MAX_ITEMS_PER_SOURCE]:
        if not isinstance(item, dict):
            continue
        url = article_url(item.get("url", ""), source)
        title = clean_text(item.get("title"), 280)
        try:
            # result.timestamp is the request time, NOT the article's time.
            date = published(datetime.fromtimestamp(int(item["ctime"]), timezone.utc).isoformat(), now)
        except (ValueError, TypeError, KeyError, OverflowError, OSError):
            continue
        if not url or not title or not date or url in seen:
            continue
        seen.add(url)
        excerpt = clean_text(item.get("intro"), 160)
        geo, basis = news_relations.infer_location(title + " " + excerpt)
        articles.append({"id": "live-" + hashlib.sha256(url.encode()).hexdigest()[:24], "source_id": source.id,
                         "url": url, "title": title, "summary": excerpt, "published_at": date,
                         "geo": geo, "location_basis": basis})
    return articles


def source_rows() -> dict[str, dict]:
    with closing(connect()) as conn:
        return {r["id"]: dict(r) for r in conn.execute("SELECT * FROM sources")}


def save_result(source: Source, *, articles: list[dict] | None = None, etag: str | None = None,
                modified: str | None = None, error: str | None = None, blocked: bool = False) -> None:
    checked = stamp()
    with closing(connect()) as conn, conn:
        if error:
            conn.execute("UPDATE sources SET status=?,last_attempt=?,error=?,blocked=? WHERE id=?",
                         ("blocked" if blocked else "error", checked, error[:180], int(blocked), source.id))
        else:
            if articles is not None:
                for item in articles:
                    conn.execute("INSERT INTO articles VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET title=excluded.title,summary=excluded.summary,published_at=excluded.published_at,last_seen=excluded.last_seen,geo_json=excluded.geo_json,location_basis=excluded.location_basis",
                                 (item["id"], source.id, item["url"], item["title"], item["summary"], item["published_at"], checked, checked,
                                  json.dumps(item["geo"], ensure_ascii=False) if item["geo"] else None, item["location_basis"]))
            latest = conn.execute("SELECT MAX(published_at) FROM articles WHERE source_id=?", (source.id,)).fetchone()[0]
            conn.execute("UPDATE sources SET status='ok',last_attempt=?,last_success=?,error=NULL,blocked=0,etag=COALESCE(?,etag),modified=COALESCE(?,modified),latest_published=? WHERE id=?",
                         (checked, checked, etag[:300] if etag else None, modified[:100] if modified else None, latest, source.id))
        conn.execute("DELETE FROM articles WHERE published_at<?", (stamp(utcnow() - timedelta(days=RETENTION_DAYS)),))
        conn.execute("DELETE FROM articles WHERE id IN (SELECT id FROM articles ORDER BY published_at DESC,id LIMIT -1 OFFSET ?)", (MAX_ROWS,))
        # Bound write-ahead log size while leaving periodic SQLite compaction to
        # normal page reuse; do not load the whole cache for pruning.
    with closing(connect()) as conn:
        conn.execute("PRAGMA wal_checkpoint(PASSIVE)")


async def refresh_once(*, transport: httpx.AsyncBaseTransport | None = None) -> dict:
    previous = source_rows()
    semaphore = asyncio.Semaphore(3)
    async with httpx.AsyncClient(timeout=httpx.Timeout(12, connect=6), follow_redirects=False,
                                 headers={"User-Agent": "SmartTradingAssistant-PersonalNewsReader/1.0"}, transport=transport) as client:
        async def fetch(source: Source):
            row = previous.get(source.id, {})
            # Authorization/WAF denials are stop conditions, not retry targets.
            if row.get("blocked"):
                return
            headers = {}
            if row.get("etag"):
                headers["If-None-Match"] = row["etag"]
            if row.get("modified"):
                headers["If-Modified-Since"] = row["modified"]
            try:
                async with semaphore, client.stream("GET", source.url, headers=headers) as response:
                    if response.status_code == 304:
                        save_result(source)
                        return
                    if response.status_code in {401, 403}:
                        save_result(source, error=f"HTTP {response.status_code}：来源拒绝访问，已停止此源，未绕过限制。", blocked=True)
                        return
                    if response.is_redirect:
                        save_result(source, error="来源返回重定向，需核对订阅地址；未跟随到未知服务。")
                        return
                    response.raise_for_status()
                    content = bytearray()
                    async for block in response.aiter_bytes(chunk_size=65_536):
                        content.extend(block)
                        if len(content) > MAX_FEED_BYTES:
                            raise ValueError("Feed exceeds size limit")
                    items = parse_sina(bytes(content), source) if source.kind == "sina" else parse_feed(bytes(content), source)
                    save_result(source, articles=items, etag=response.headers.get("etag"), modified=response.headers.get("last-modified"))
            except httpx.TimeoutException:
                save_result(source, error="连接或读取超时；保留旧缓存，等待下次正常更新。")
            except httpx.HTTPStatusError as exc:
                save_result(source, error=f"新闻源返回 HTTP {exc.response.status_code}，未取得新数据。")
            except httpx.RequestError:
                save_result(source, error="新闻源连接失败；未使用模拟新闻补齐。")
            except (ValueError, ET.ParseError, TypeError, KeyError):
                save_result(source, error="新闻源格式或数据校验失败，未覆盖旧缓存。")
        await asyncio.gather(*(fetch(s) for s in SOURCES))
    return cache_meta()


def cache_meta() -> dict:
    rows = source_rows()
    with closing(connect()) as conn:
        total = conn.execute("SELECT COUNT(*) FROM articles WHERE published_at>=?", (stamp(utcnow() - timedelta(days=RETENTION_DAYS)),)).fetchone()[0]
    sources = [{"id": s.id, "label": s.label, "url": s.url, "language": s.language,
                **{k: rows.get(s.id, {}).get(k) for k in ("status", "last_attempt", "last_success", "error", "latest_published")}} for s in SOURCES]
    successes = [s["last_success"] for s in sources if s["last_success"]]
    attempts = [s["last_attempt"] for s in sources if s["last_attempt"]]
    fresh_after = stamp(utcnow() - timedelta(seconds=REFRESH_SECONDS * 2))
    fresh = any(s["status"] == "ok" and s["last_success"] >= fresh_after for s in sources if s["last_success"])
    return {"status": "empty" if total == 0 else "live" if fresh else "stale", "total": total,
            "checked_at": max(attempts) if attempts else None, "updated_at": max(successes) if successes else None,
            "refresh_interval_seconds": REFRESH_SECONDS, "retention_days": RETENTION_DAYS,
            "max_cached_articles": MAX_ROWS, "sources": sources,
            "updating": _refresh_task is not None and not _refresh_task.done(),
            "refreshing": _refresh_task is not None and not _refresh_task.done(),
            "partial": any(s["status"] in {"error", "blocked"} for s in sources),
            "notice": "真实新闻来源标注在原文链接处；外文保留原文。默认关键词产业候选，主动点击可用已验证API分析板块；AI与股票关联仍待核验，不自动调用AI、不倒灌历史预案。"}


def candidate_catalog() -> dict:
    """Only current locally verified eligible names may appear as candidates."""
    value = news_relations.catalog()
    supplied = {s["code"]: s for s in value["stocks"] if re.fullmatch(r"(?:000|001|002|003|600|601|603|605)\d{3}", s.get("code", ""))}
    if not supplied:
        return {"stocks": [], "stock_links": []}
    try:
        with closing(market_history.connection()) as conn:
            names = {r["code"]: r["name"] for r in conn.execute("SELECT code,name FROM stocks WHERE status='ok' AND code IN (" + ",".join("?" for _ in supplied) + ")", tuple(supplied))}
    except (HTTPException, sqlite3.Error):
        names = {}
    stocks = [{**s, "label": names[code], "synthetic": False, "rule_results": {}}
              for code, s in supplied.items() if code in names and "ST" not in names[code].upper() and "退" not in names[code]]
    ids = {s["id"] for s in stocks}
    return {"stocks": stocks, "stock_links": [link for link in value["stock_links"] if link["stock_id"] in ids]}


def feed(*, limit: int = 40, offset: int = 0, q: str = "") -> dict:
    meta = cache_meta()
    term = q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    params = (stamp(utcnow() - timedelta(days=RETENTION_DAYS)), f"%{term}%", f"%{term}%")
    where = "published_at>=? AND (title LIKE ? ESCAPE '\\' OR summary LIKE ? ESCAPE '\\')"
    with closing(connect()) as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM articles WHERE {where}", params).fetchone()[0]
        rows = conn.execute(f"SELECT * FROM articles WHERE {where} ORDER BY published_at DESC,id LIMIT ? OFFSET ?", (*params, limit, offset)).fetchall()
    sources = {s.id: s for s in SOURCES}
    news = []
    for row in rows:
        source = sources.get(row["source_id"])
        if not source:
            continue
        news.append({"id": row["id"], "title": row["title"], "shortTitle": row["title"][:44], "summary": row["summary"],
                     "url": row["url"], "published_at": row["published_at"], "time": row["published_at"],
                     "source": source.label, "source_id": source.id, "language": source.language,
                     "category": source.category, "geo": json.loads(row["geo_json"]) if row["geo_json"] else None,
                     "location_basis": row["location_basis"], "synthetic": False})
    return {"schema_version": 1, "news_mode": "live", "preview_only": False, "model_called": False,
            "impact_mode": "keyword_hypothesis", "geo_mode": "reported_mentions", "news": news,
            "sectors": news_relations.SECTORS, "news_links": [link for item in news for link in news_relations.extract_relations(item)],
            **candidate_catalog(), "seats": [seat.public() for seat in SEATS.values()],
            "meta": {**meta, "total": total, "limit": limit, "offset": offset, "has_more": offset + len(news) < total}}


def begin_refresh() -> dict:
    global _refresh_task, _last_trigger
    now = time.monotonic()
    if _refresh_task is not None and not _refresh_task.done():
        return {"accepted": False, "status": "updating", "retry_after": 2}
    if _last_trigger and now - _last_trigger < REFRESH_SECONDS:
        return {"accepted": False, "status": "cooldown", "retry_after": max(1, int(REFRESH_SECONDS - (now - _last_trigger)))}
    _last_trigger = now
    _refresh_task = asyncio.create_task(refresh_once())
    # Consume exceptions without exposing response bodies or crashing lifespan.
    _refresh_task.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
    return {"accepted": True, "status": "updating", "retry_after": 2}


async def start() -> None:
    global _scheduler_task
    if os.environ.get("CONTEST_NEWS_AUTO_REFRESH", "1").lower() in {"0", "false", "off"}:
        return
    if _scheduler_task is not None and not _scheduler_task.done():
        return
    async def loop():
        while True:
            try:
                begin_refresh()
            except (sqlite3.Error, OSError):
                pass  # Health/login remain usable even when the cache is unwritable.
            await asyncio.sleep(REFRESH_SECONDS)
    _scheduler_task = asyncio.create_task(loop())


async def stop() -> None:
    global _scheduler_task, _refresh_task, _last_trigger
    for task in (_scheduler_task, _refresh_task):
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
    _scheduler_task = _refresh_task = None
    _last_trigger = 0.0


@router.get("/feed")
def news_feed(_workspace: Ready, limit: Annotated[int, Query(ge=1, le=40)] = 40,
              offset: Annotated[int, Query(ge=0, le=10000)] = 0,
              q: Annotated[str, Query(max_length=100)] = ""):
    try:
        from .news_ai import enrich_feed
        return enrich_feed(feed(limit=limit, offset=offset, q=q), _workspace)
    except (sqlite3.Error, OSError, ValueError):
        raise HTTPException(503, "新闻缓存暂不可读；未改用模拟新闻。") from None


@router.get("/status")
def news_status(_workspace: Ready):
    try:
        return cache_meta()
    except (sqlite3.Error, OSError):
        raise HTTPException(503, "新闻缓存暂不可读。") from None


@router.post("/refresh")
async def news_refresh(_workspace: Ready):
    return begin_refresh()

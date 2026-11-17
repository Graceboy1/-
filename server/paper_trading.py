"""Authenticated autonomous paper portfolios; no broker and no paid model.

One explicit start scans the universe, rather than requiring per-stock actions.
Historical estimates and from-now live quotes have separate books and dates.
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
import hashlib
import sqlite3
import threading
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from .access import WorkspaceSession, require_ready
from . import market_history, paper_engine, paper_market
from . import paper_playback, paper_statistics, buffett_verified, livermore_data
from .seats import SEATS

CN = timezone(timedelta(hours=8))
HISTORICAL_DAY = market_history.REPLAY_DAY
LIVE_INTERVAL = 30
LIVE_BATCH = 10
MAX_JOBS = 4
Mode = Literal["historical", "live"]
Ready = Annotated[WorkspaceSession, Depends(require_ready)]
router = APIRouter(prefix="/paper", tags=["autonomous paper trading"])


class Control(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Mode = "historical"


@dataclass
class Job:
    stop: threading.Event = field(default_factory=threading.Event)
    task: asyncio.Task | None = None
    day: str = ""


_jobs: dict[tuple[str, str], Job] = {}
_historical_gate = asyncio.Semaphore(1)


def utcnow():
    return datetime.now(timezone.utc)


def today():
    return utcnow().astimezone(CN).date().isoformat()


def session_open(now: datetime | None = None) -> bool:
    value = (now or utcnow()).astimezone(CN)
    minute = value.time().replace(tzinfo=None)
    # A short closing-observation grace window prevents 30s polling from
    # missing a fresh 15:00 quote. It never permits a new post-close buy.
    return value.weekday() < 5 and (time(9, 30) <= minute <= time(11, 30) or time(13) <= minute <= time(15, 1, 30))


def _key(username, mode):
    return hashlib.sha256(username.encode()).hexdigest(), mode


def _state(username, mode, day, status, message, **extra):
    # This is progress/status only. The engine remains the sole authority for
    # account arithmetic, idempotency, eligibility and estimated fills.
    return paper_engine.update_session(username, mode, day, status="error" if status == "failed" else status, detail=message, **extra)


async def _historical(username: str, job: Job, total: int):
    async with _historical_gate:
        if job.stop.is_set():
            return
        await asyncio.to_thread(paper_engine.run_historical, username,
                                paper_market.iter_historical_candidates(HISTORICAL_DAY, cancel=job.stop.is_set),
                                HISTORICAL_DAY, total, job.stop.is_set)


async def _live(username: str, job: Job, total: int):
    cursor = 0
    watched: dict[str, str] = {}
    watched_day = ""
    while not job.stop.is_set():
        day = today()
        job.day = day
        if watched_day != day:
            watched.clear()
            cursor = 0
            watched_day = day
            await asyncio.to_thread(paper_engine.create_session, username, "live", day)
        if not session_open():
            await asyncio.to_thread(_state, username, "live", day, "waiting",
                                    "当前不在连续交易时段，等待下一交易时段；不会补填今天早前的订单。", total=total)
            await asyncio.sleep(LIVE_INTERVAL)
            continue
        await asyncio.to_thread(paper_engine.begin_scan, username, "live", day, total)
        current = await asyncio.to_thread(paper_engine.dashboard, username, "live", day)
        held = {row["code"]: row["name"] for row in current.get("positions", [])}
        # Only bounded batches are requested. This is a rolling universe scan,
        # not a claim that every security is monitored at the same instant.
        closing = utcnow().astimezone(CN).time().replace(tzinfo=None) >= time(15)
        batch = [] if closing else await asyncio.to_thread(paper_market.read_universe_page, cursor, LIVE_BATCH)
        if not batch and not closing:
            cursor = 0
            batch = await asyncio.to_thread(paper_market.read_universe_page, 0, LIVE_BATCH)
        cursor += len(batch)
        request_names = {**watched, **held, **{row["code"]: row["name"] for row in batch}}
        request_codes = list(request_names)[:50]
        if not request_codes:
            await asyncio.to_thread(_state, username, "live", day, "waiting",
                                    "收盘观察窗口没有待估值持仓；不会新建收盘后买入。")
            await asyncio.sleep(LIVE_INTERVAL)
            continue
        try:
            response = await paper_market.fetch_quotes(request_codes)
            quotes = {quote["code"]: quote for quote in response.get("quotes", [])}
            for quote in quotes.values():
                if job.stop.is_set():
                    break
                # Existing decisions must see the next fresh observation before
                # new candidates are frozen; no fill at a past opening price.
                applied = await asyncio.to_thread(paper_engine.apply_quote, username, quote)
                if applied.get("accepted"):
                    # Recording a chart snapshot must never repeat or roll
                    # back an already committed estimated trade.
                    with suppress(paper_engine.PaperError, OSError, ValueError):
                        await asyncio.to_thread(paper_playback.capture_quote, username, quote)
            for row in batch:
                if job.stop.is_set():
                    break
                quote = quotes.get(row["code"])
                if quote is None or not quote.get("executable"):
                    continue
                history = await paper_market.fetch_recent_history(row["code"])
                if job.stop.is_set():
                    break
                candidate = paper_market.make_live_candidate(row["code"], row["name"], history, quote)
                await asyncio.to_thread(paper_engine.record_live_candidate, username, day, candidate)
                watched[row["code"]] = row["name"]
            if job.stop.is_set():
                break
            # Keep last twenty pending securities, plus all held securities.
            watched = dict(list(watched.items())[-20:])
            for code, name in held.items():
                watched[code] = name
            await asyncio.to_thread(_state, username, "live", day, "running",
                                    f"按每轮{LIVE_BATCH}只滚动扫描；最新核对目录位置{cursor}/{total}。只有冻结后下一条有效报价才可能估算成交。",
                                    processed=min(cursor, total), total=total)
        except paper_market.SourceBlocked:
            await asyncio.to_thread(_state, username, "live", day, "blocked",
                                    "行情或日线源拒绝访问/限频，已停止自动请求；未绕过限制或编造成交。")
            return
        except (ValueError, OSError, sqlite3.Error, paper_engine.PaperError, paper_market.MarketDataError):
            await asyncio.to_thread(_state, username, "live", day, "waiting",
                                    "本轮行情或账户核对未完成，保留原订单与持仓；没有以旧报价补单。")
        await asyncio.sleep(LIVE_INTERVAL)


async def _run(username: str, mode: str, job: Job, total: int):
    try:
        if mode == "historical":
            await _historical(username, job, total)
        else:
            await _live(username, job, total)
    except asyncio.CancelledError:
        job.stop.set()
        raise
    except Exception:
        # Never log raw private portfolio state, provider bodies or credentials.
        with suppress(Exception):
            await asyncio.to_thread(_state, username, mode, job.day, "failed", "自动模拟任务中断，已保存订单和持仓未清空；可检查后继续，不能补造交易。")
    finally:
        if job.stop.is_set():
            with suppress(Exception):
                await asyncio.to_thread(paper_engine.stop_session, username, mode, job.day)
        _jobs.pop(_key(username, mode), None)


def dashboard(username: str, mode: str, day: str | None = None, seat_id: str | None = None, limit=20, offset=0):
    if mode == "historical" and day not in {None, HISTORICAL_DAY}:
        raise ValueError("This archive supports only its labeled historical date")
    if mode == "live" and day is not None and day > today():
        raise ValueError("A future live book cannot exist")
    value = paper_engine.dashboard(username, mode, day, seat_id, limit, offset)
    session = value.get("session") or {}
    day = value.get("simulation_date") or (day or (HISTORICAL_DAY if mode == "historical" else today()))
    status = session.get("status", "idle")
    status = {"ready": "idle", "error": "failed", "interrupted": "paused"}.get(status, status)
    livermore_summary = livermore_data.integration_summary()
    accounts = []
    for item in value.get("accounts", []):
        seat = item["seat_id"]
        counts = item.get("counts") or {}
        accounts.append({**item, "label": SEATS[seat].label, "buy_count": item.get("buy_count", counts.get("buy", 0)), "sell_count": item.get("sell_count", counts.get("sell", 0)),
                         "status": status, "missing": (([livermore_data.LIVE_LIMITATION] if mode == "live" else livermore_summary["remaining"][:4]) if seat == "livermore" else
                         ["财报原始历史版本、维持性开支、股数或人工生意确认未齐全，价值规则可能观望。"] if seat == "buffett" else [])})
    positions = []
    for row in value.get("positions", []):
        qty = row.get("shares", 0)
        cost = row.get("cost_including_fees", 0)
        positions.append({**row, "quantity": qty, "available_quantity": row.get("sellable_shares", 0),
                          "cost_price": cost / qty if qty else None, "updated_at": row.get("mark_at"),
                          "return_pct": row.get("unrealized_pnl", 0) / cost * 100 if cost else 0})
    orders = value.get("orders") or {"items": [], "total": 0, "limit": limit, "offset": offset}
    orders = {**orders, "items": [{**row, "timestamp": row.get("event_time"), "date": row.get("day"),
                                  "quantity": row.get("shares"), "fees": row.get("fee"),
                                  "execution_mode": row.get("execution_assumption")} for row in orders["items"]]}
    events = value.get("events") or {"items": [], "total": 0}
    decisions = {**events, "items": [{**row, "timestamp": row.get("created_at"), "action": row.get("kind"),
                                     "status": row.get("kind"), "missing": []} for row in events["items"]]}
    notice = ["三个独立虚拟账户各10万元，不接券商，不下真实订单；按注册规则自动运行，不循环调用付费AI。",
              "历史演示和实时账户分账。历史开盘/实时报价成交均为明确假设的纸面估算，不证明真实排队、滑点或可兑现收益。",
              "每席位最多5只持仓、单只不超过策略仓位及初始虚拟资金15%的上限；100股整数手、计费、T+1。缺条件的席位可以一笔不买，不为展示收益强行交易。",
              "实时持仓按最后有效报价估值，不冒称官方收盘结算；本版未重建分红送转和真实滑点。"]
    notice.append("历史模拟交易日为2026-11-13，信号只用11月12日及以前数据；当日买入只能收盘估值，浮盈亏不是已实现收益。" if mode == "historical" else
                  "实时模式只从启用后记录；每30秒按10只分批核对，并持续观察候选与持仓，不是瞬时全市场逐笔行情。休市或报价过期不补单，服务重启后须重新启用。")
    return {"mode": mode, "trading_date": day, "calendar_date": today(), "simulation": True,
            "engine": {"status": status, "started_at": session.get("started_at"), "last_run": session.get("updated_at"),
                       "progress": {"processed": session.get("processed", 0), "total": session.get("total", 0)},
                       "message": session.get("detail") or "尚未启动自主模拟；启用一次后会自动扫描并处理，不需要逐只点股票。"},
            "accounts": accounts, "orders": orders, "positions": positions, "decisions": decisions, "notice": notice,
            "buffett_data": buffett_verified.integration_summary(),
            "livermore_data": livermore_summary}


def _replay_day(mode: str, value: str | None) -> str:
    day = value or (HISTORICAL_DAY if mode == "historical" else today())
    try:
        if datetime.fromisoformat(day).date().isoformat() != day:
            raise ValueError()
    except (ValueError, TypeError):
        raise HTTPException(422, "复盘日期不正确。") from None
    if mode == "historical" and day != HISTORICAL_DAY:
        raise HTTPException(422, "本批历史档案的模拟交易日为2026-11-13。")
    if mode == "live" and day > today():
        raise HTTPException(422, "不能查看尚未发生的实时交易日。")
    return day


@router.get("/performance")
def get_performance(workspace: Ready, mode: Mode = "historical",
                    date: Annotated[str | None, Query(pattern=r"^\d{4}-\d{2}-\d{2}$")] = None,
                    start_date: Annotated[str | None, Query(pattern=r"^\d{4}-\d{2}-\d{2}$")] = None,
                    seat_id: Literal["all", "baseline", "livermore", "buffett"] = "all"):
    day = _replay_day(mode, date)
    try:
        return paper_statistics.performance(workspace.username, mode, day, start_date,
                                            None if seat_id == "all" else seat_id)
    except ValueError:
        raise HTTPException(422, "收益统计的日期或席位范围不正确。") from None
    except (paper_engine.PaperError, sqlite3.Error, OSError):
        raise HTTPException(503, "已保存的收益统计暂不可读，未重算或修改交易。") from None


@router.get("/playback")
def get_playback(workspace: Ready, mode: Mode = "historical",
                 date: Annotated[str | None, Query(pattern=r"^\d{4}-\d{2}-\d{2}$")] = None,
                 order_id: Annotated[int | None, Query(ge=1, le=9223372036854775807)] = None,
                 seat_id: Literal["all", "baseline", "livermore", "buffett"] = "all",
                 code: Annotated[str | None, Query(pattern=r"^(?:000|001|002|003|600|601|603|605)\d{3}$")] = None):
    day = _replay_day(mode, date)
    try:
        return paper_playback.playback(workspace.username, mode, day, order_id,
                                       None if seat_id == "all" else seat_id, code)
    except paper_playback.PlaybackMissing:
        raise HTTPException(404, "未找到这条已保存的模拟订单。") from None
    except ValueError:
        raise HTTPException(422, "K线回放的订单或范围不正确。") from None
    except (paper_engine.PaperError, sqlite3.Error, OSError):
        raise HTTPException(503, "已保存交易或日K暂不可读，未改用示例K线。") from None


@router.get("/dashboard")
def get_dashboard(workspace: Ready, mode: Mode = "historical",
                  date: Annotated[str | None, Query(pattern=r"^\d{4}-\d{2}-\d{2}$")] = None,
                  seat_id: Literal["all", "baseline", "livermore", "buffett"] = "all",
                  limit: Annotated[int, Query(ge=1, le=20)] = 20,
                  offset: Annotated[int, Query(ge=0, le=100000)] = 0):
    try:
        return dashboard(workspace.username, mode, date, None if seat_id == "all" else seat_id, limit, offset)
    except ValueError:
        raise HTTPException(422, "日期、席位或分页参数不正确。") from None
    except (sqlite3.Error, OSError, paper_engine.PaperError):
        raise HTTPException(503, "模拟账户暂不可读，已保存数据未重置。") from None


@router.post("/start")
async def start_paper(body: Control, workspace: Ready):
    key = _key(workspace.username, body.mode)
    if key in _jobs:
        return {"accepted": False, "message": "此账户的自动模拟已在运行，不重复启动。"}
    if len(_jobs) >= MAX_JOBS:
        raise HTTPException(429, "本机自动模拟任务已达并发上限，请等待或暂停其他任务。")
    try:
        info = paper_market.universe_info()
        if not info.get("available") or not info.get("total"):
            raise HTTPException(409, "没有可用的真实股票目录，未使用虚构数据启动。")
        day = HISTORICAL_DAY if body.mode == "historical" else today()
        session = paper_engine.create_session(workspace.username, body.mode, day)
        if body.mode == "historical" and session.get("status") == "completed":
            return {"accepted": False, "message": "该历史交易日已完成，账本保持原样，不重复成交或重置收益。"}
        paper_engine.begin_scan(workspace.username, body.mode, day, info["total"])
        job = Job(day=day)
        _jobs[key] = job
        job.task = asyncio.create_task(_run(workspace.username, body.mode, job, info["total"]))
        return {"accepted": True, "mode": body.mode, "trading_date": day, "message": "自动模拟已启动，账户与订单会持续保存。"}
    except (sqlite3.Error, OSError, paper_engine.PaperError, paper_market.MarketDataError):
        raise HTTPException(503, "模拟任务未启动，账户存储暂不可用。") from None


@router.post("/stop")
async def stop_paper(body: Control, workspace: Ready):
    job = _jobs.get(_key(workspace.username, body.mode))
    if job:
        job.stop.set()
    try:
        paper_engine.stop_session(workspace.username, body.mode, job.day if job else HISTORICAL_DAY if body.mode == "historical" else today())
    except (sqlite3.Error, OSError, ValueError, paper_engine.PaperError):
        raise HTTPException(503, "暂停请求未完成，请检查账户状态；原账本未清空。") from None
    return {"accepted": True, "message": "已请求暂停；已保存的订单、持仓和收益不重置。"}


async def start():
    try:
        paper_engine.recover_after_restart()
    except (sqlite3.Error, OSError, paper_engine.PaperError):
        pass  # A failed portfolio DB must not make login/news unavailable.


async def stop():
    jobs = list(_jobs.values())
    for job in jobs:
        job.stop.set()
        if job.task and not job.task.done():
            job.task.cancel()
    for job in jobs:
        if job.task:
            with suppress(asyncio.CancelledError, Exception):
                await job.task
    _jobs.clear()

"""Bounded, read-only access to the downloaded historical market archive."""
from __future__ import annotations

from contextlib import closing
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
from statistics import fmean
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from .access import WorkspaceSession, require_ready

router = APIRouter(prefix="/market", tags=["historical market"])
DATA_DIR = Path(__file__).resolve().parents[1] / "data" / "market-history" / "2026-11-13"
CUTOFF, REPLAY_DAY = "2026-11-12", "2026-11-13"
Ready = Annotated[WorkspaceSession, Depends(require_ready)]
SeatId = Literal["baseline", "livermore", "buffett"]
ELIGIBLE_SQL = "status='ok' AND length(code)=6 AND code NOT GLOB '*[^0-9]*' AND substr(code,1,3) IN ('000','001','002','003','600','601','603','605') AND trim(name)<>'' AND instr(upper(name),'ST')=0 AND instr(name,'退')=0"


def directory() -> Path:
    return Path(os.environ.get("CONTEST_MARKET_DATA_DIR") or DATA_DIR).resolve()


def status() -> dict:
    path = directory() / "manifest.json"
    if not path.is_file():
        return {"available": False, "status": "missing", "downloaded": 0, "decision_date": CUTOFF, "end_date": REPLAY_DAY}
    try:
        if path.stat().st_size > 100_000:
            raise ValueError("Manifest too large")
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if manifest.get("schema") != "market-history/v1" or manifest.get("end_date") != REPLAY_DAY:
            raise ValueError("Unsupported archive")
        allowed = ("source", "universe_source", "start_date", "end_date", "decision_date", "adjustment", "volume_unit", "amount_unit",
                   "eligible_total", "downloaded", "failed", "pending", "complete_for_target", "warning", "status", "updated_at", "universe_captured_at")
        return {"available": manifest.get("downloaded", 0) > 0, **{key: manifest.get(key) for key in allowed}}
    except (OSError, ValueError, TypeError) as exc:
        raise HTTPException(503, "历史数据清单暂不可读，请重试。") from exc


def connection():
    path = directory() / "index.sqlite3"
    if not path.is_file():
        raise HTTPException(503, "历史数据尚未下载完成。")
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)
    conn.row_factory = sqlite3.Row
    return conn


def sample(code: str, name: str, seat_id: str = "baseline") -> dict:
    if seat_id not in {"baseline", "livermore", "buffett"}:
        raise HTTPException(404, "未找到这个历史研究席位。")
    return {"id": "history-20261113-" + code, "seat_id": seat_id, "symbol": code, "name": name,
            "label": f"{code} {name} · 历史行情", "decision_date": CUTOFF, "data_mode": "historical",
            "description": "三个席位共用真实股票日线，各自读取所需证据；缺失的财报、竞价或执行条件不由合成数据补齐。"}


def load_case(case_id: str) -> dict:
    match = re.fullmatch(r"history-20261113-((?:000|001|002|003|600|601|603|605)\d{3})", case_id)
    if match is None:
        raise HTTPException(404, "未找到此历史股票。")
    code = match.group(1)
    try:
        with closing(connection()) as conn:
            row = conn.execute("SELECT * FROM stocks WHERE code=? AND status='ok'", (code,)).fetchone()
        if row is None:
            raise HTTPException(404, "此股票暂无已校验的历史数据。")
        if not isinstance(row["name"], str) or not row["name"].strip() or "ST" in row["name"].upper() or "退" in row["name"]:
            raise HTTPException(404, "ST、退市或名称缺失的股票不在研究范围内。")
        with gzip.open(directory() / "daily" / f"{code}.json.gz", "rb") as handle:
            raw = handle.read(2_000_001)
        if len(raw) > 2_000_000 or hashlib.sha256(raw).hexdigest() != row["sha256"]:
            raise ValueError("History hash mismatch")
        payload = json.loads(raw)
        if payload.get("data_mode") != "historical" or payload.get("adjustment") != "none" or payload.get("code") != code or payload.get("volume_unit") != "shares":
            raise ValueError("Invalid market payload")
        rows = payload["bars"]
        if not 1 <= len(rows) <= 1000 or [bar["date"] for bar in rows] != sorted({bar["date"] for bar in rows}):
            raise ValueError("Invalid history ordering")
        for bar in rows:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", bar["date"]) or not "2024-11-01" <= bar["date"] <= REPLAY_DAY:
                raise ValueError("Out-of-range date")
            if not all(type(bar.get(k)) in (int, float) and math.isfinite(bar[k]) for k in ("open", "high", "low", "close", "volume")):
                raise ValueError("Nonfinite market record")
            if not 0 < bar["low"] <= min(bar["open"], bar["close"]) <= max(bar["open"], bar["close"]) <= bar["high"] or bar["volume"] <= 0:
                raise ValueError("Invalid OHLCV")
        bars = [{**{key: bar[key] for key in ("date", "open", "high", "low", "close", "volume")},
                 "limit_up": None, "limit_down": None, "suspended": False} for bar in rows]
        return {**sample(code, row["name"]), "bars": bars, "archive_sha256": row["sha256"],
                "source": payload["source"], "downloaded_at": payload["downloaded_at"]}
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
        raise HTTPException(503, "历史数据校验未通过，请重新下载后重试。") from exc


def read_evidence(case: dict) -> dict:
    # A bounded price-only evidence profile: unknown limits are NOT filled with
    # nominal 10% values, and future bars never become evidence or metrics.
    bars = [dict(bar) for bar in case["bars"] if bar["date"] <= CUTOFF][-120:]
    closes = [bar["close"] for bar in bars]
    issues = []
    if len(bars) < 20 or not bars or bars[-1]["date"] != CUTOFF:
        issues.append("缺少至少20根有效日线或决策日收盘记录")
    last = closes[-1] if closes else None
    mean_volume = fmean(b["volume"] for b in bars[-6:-1]) if len(bars) >= 6 else 0
    metrics = {"bar_count": len(bars), "tradable_count": len(bars), "last_close": last,
               "sma5": fmean(closes[-5:]) if len(closes) >= 5 else None,
               "sma20": fmean(closes[-20:]) if len(closes) >= 20 else None,
               "return_5d_pct": (last / closes[-6] - 1) * 100 if len(closes) >= 6 else None,
               "volume_ratio_5d": bars[-1]["volume"] / mean_volume if mean_volume else None,
               "data_valid": not issues, "data_issues": issues}
    return {"case_id": case["id"], "symbol": case["symbol"], "decision_date": CUTOFF,
            "data_mode": "historical", "source": case["source"], "volume_unit": "shares", "adjustment": "none",
            "limitations": ["未取得历史涨跌停价格、停复牌日历与竞价记录，仅生成价格条件研究；不模拟真实成交。", "无真实财报，不使用巴菲特价值规则。", "名单按下载时筛选，非历史时点成分重建。"],
            "bars": bars, "metrics": metrics,
            "evidence_ids": [f"{case['id']}:metrics", *[f"{case['id']}:bar:{b['date']}" for b in bars]]}


@router.get("/status")
def market_status(_workspace: Ready):
    return status()


@router.get("/stocks")
def market_stocks(_workspace: Ready, q: Annotated[str, Query(max_length=32)] = "",
                  limit: Annotated[int, Query(ge=1, le=50)] = 20,
                  offset: Annotated[int, Query(ge=0, le=10000)] = 0,
                  seat_id: SeatId = "baseline"):
    try:
        term = q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        sql = ELIGIBLE_SQL + " AND (code LIKE ? ESCAPE '\\' OR name LIKE ? ESCAPE '\\')"
        with closing(connection()) as conn:
            total = conn.execute(f"SELECT COUNT(*) FROM stocks WHERE {sql}", (f"%{term}%", f"%{term}%")).fetchone()[0]
            rows = conn.execute(f"SELECT code,name,first_date,last_date,bar_count FROM stocks WHERE {sql} ORDER BY code LIMIT ? OFFSET ?", (f"%{term}%", f"%{term}%", limit, offset)).fetchall()
        return {"items": [{**dict(row), "sample": sample(row["code"], row["name"], seat_id)} for row in rows], "total": total, "limit": limit, "offset": offset, "seat_id": seat_id}
    except sqlite3.Error as exc:
        raise HTTPException(503, "历史股票目录暂不可读。") from exc


@router.get("/stocks/{code}")
def market_stock(code: str, _workspace: Ready, reveal: bool = False, seat_id: SeatId = "baseline"):
    from . import historical_seats
    case = {**load_case("history-20261113-" + code), "seat_id": seat_id}
    result = {"sample": {key: case[key] for key in ("id", "seat_id", "symbol", "name", "label", "decision_date", "description")},
              "evidence": historical_seats.read_evidence(case, seat_id), "source": case["source"], "downloaded_at": case["downloaded_at"],
              "history_start": case["bars"][0]["date"], "history_bar_count": len(case["bars"]),
              "replay_day": REPLAY_DAY, "revealed": reveal}
    if reveal:
        result["observed_bars"] = [bar for bar in case["bars"] if bar["date"] > CUTOFF]
    return result

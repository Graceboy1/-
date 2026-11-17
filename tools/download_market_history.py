"""Download a dated, unadjusted local market archive; never invent missing data.

Run from the project with its Python environment. No model, account or research
database is accessed. Vendor data is kept out of the public source release.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import gzip
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3

import httpx

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DIR = ROOT / "data" / "market-history" / "2026-11-13"
END = "2026-11-13"
START = "2024-11-01"
DECISION = "2026-11-12"
WARNING = "股票名单及名称按下载时快照筛选，不能证明历史当日成分、历史ST状态或无幸存者偏差。未获取真实财报、竞价、五档和逐笔数据。"


def eligible(code: str, name: str) -> bool:
    return bool(re.fullmatch(r"(?:000|001|002|003|600|601|603|605)\d{3}", code)) and bool(name) and "ST" not in name.upper() and "退" not in name


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def atomic_json(path: Path, data: dict) -> None:
    temp = path.with_suffix(path.suffix + ".part")
    temp.write_text(json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(",", ":")), encoding="utf-8")
    temp.replace(path)


def parse_eastmoney(data: dict) -> list[dict]:
    rows = []
    for raw in data.get("klines") or []:
        cells = raw.split(",")
        if len(cells) < 11:
            raise ValueError("Incomplete daily record")
        day = cells[0]
        if not START <= day <= END:
            continue
        op, close, high, low, lots, amount = map(float, cells[1:7])
        if not all(math.isfinite(v) for v in (op, close, high, low, lots, amount)) or not 0 < low <= min(op, close) <= max(op, close) <= high or lots <= 0 or amount < 0:
            raise ValueError("Invalid vendor OHLCV")
        # Eastmoney documents daily volume as lots; preserve both raw lots and
        # normalized shares. Do not estimate absent amount/turnover fields.
        rows.append({"date": day, "open": op, "close": close, "high": high, "low": low,
                     "volume": lots * 100, "volume_lots": lots, "amount": amount,
                     "change_amount": float(cells[9]), "turnover_rate": float(cells[10])})
    if not rows or [r["date"] for r in rows] != sorted({r["date"] for r in rows}):
        raise ValueError("Empty, duplicate or unordered daily history")
    return rows


async def universe(client: httpx.AsyncClient) -> tuple[list[dict], int]:
    found = {}
    count_response = await client.get("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeStockCount", params={"node": "hs_a"})
    count_response.raise_for_status()
    total = int(count_response.json())
    for page in range(1, 101):
        for attempt in range(4):
            try:
                response = await client.get("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData", params={
                    "page": page, "num": 100, "sort": "symbol", "asc": 1, "node": "hs_a"})
                break
            except httpx.HTTPError:
                if attempt == 3:
                    raise
                await asyncio.sleep(.7 * (attempt + 1))
        response.raise_for_status()
        batch = response.json()
        if not batch:
            break
        for row in batch:
            code, name = str(row.get("code", "")), str(row.get("name", ""))
            if re.fullmatch(r"\d{6}", code):
                found[code] = {"code": code, "name": name, "market": "SH" if str(row.get("symbol", "")).startswith("sh") else "SZ"}
        if len(found) >= total:
            break
        await asyncio.sleep(.15)
    if total is None or total < 1000 or len(found) != total:
        raise ValueError(f"Incomplete universe: {len(found)}/{total}")
    return [row for row in found.values() if eligible(row["code"], row["name"])], total


def parse_tencent(data: dict, symbol: str) -> list[dict]:
    rows = []
    for raw in (data.get(symbol) or {}).get("day") or []:
        day = str(raw[0])
        if not START <= day <= END:
            continue
        op, close, high, low, lots = map(float, raw[1:6])
        if not all(math.isfinite(v) for v in (op, close, high, low, lots)) or not 0 < low <= min(op, close) <= max(op, close) <= high or lots <= 0:
            raise ValueError("Invalid vendor OHLCV")
        rows.append({"date": day, "open": op, "close": close, "high": high, "low": low,
                     "volume": lots * 100, "volume_lots": lots, "amount": None, "turnover_rate": None})
    if not rows or [r["date"] for r in rows] != sorted({r["date"] for r in rows}):
        raise ValueError("Empty, duplicate or unordered daily history")
    return rows


def parse_sina(text: str) -> list[dict]:
    match = re.search(r"var _data=\((.*)\)\s*;?\s*$", text, re.S)
    if match is None:
        raise ValueError("Invalid Sina daily response")
    rows = []
    for item in json.loads(match.group(1)):
        day = item["day"][:10]
        if not START <= day <= END:
            continue
        op, high, low, close, volume = (float(item[key]) for key in ("open", "high", "low", "close", "volume"))
        if not all(math.isfinite(v) for v in (op, high, low, close, volume)) or not 0 < low <= min(op, close) <= max(op, close) <= high or volume <= 0:
            raise ValueError("Invalid vendor OHLCV")
        # This source uses shares, not Tencent/Eastmoney lots.
        rows.append({"date": day, "open": op, "high": high, "low": low, "close": close,
                     "volume": volume, "amount": None, "turnover_rate": None})
    if not rows or [r["date"] for r in rows] != sorted({r["date"] for r in rows}):
        raise ValueError("Empty, duplicate or unordered daily history")
    return rows


def connect(directory: Path):
    conn = sqlite3.connect(directory / "index.sqlite3")
    conn.execute("CREATE TABLE IF NOT EXISTS stocks (code TEXT PRIMARY KEY,name TEXT NOT NULL,market TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',first_date TEXT,last_date TEXT,bar_count INTEGER DEFAULT 0,sha256 TEXT,downloaded_at TEXT,error TEXT)")
    if "source" not in {column[1] for column in conn.execute("PRAGMA table_info(stocks)")}:
        conn.execute("ALTER TABLE stocks ADD COLUMN source TEXT DEFAULT 'tencent'")
        conn.commit()
    return conn


async def download(directory: Path, limit: int | None = None, source: str = "sina") -> dict:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "daily").mkdir(exist_ok=True)
    async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
        universe_path = directory / "universe.json"
        if universe_path.exists():
            snapshot = json.loads(universe_path.read_text(encoding="utf-8"))
            stocks, total = snapshot["stocks"], snapshot["source_total"]
        else:
            stocks, total = await universe(client)
            snapshot = {"captured_at": now(), "source": "sina-current-A-share-list", "source_total": total,
                        "warning": WARNING, "stocks": stocks}
            atomic_json(universe_path, snapshot)
        conn = connect(directory)
        with conn:
            conn.executemany("INSERT OR IGNORE INTO stocks(code,name,market) VALUES(?,?,?)", [(s["code"], s["name"], s["market"]) for s in stocks])
        manifest = {"schema": "market-history/v1", "data_mode": "historical", "source": "tencent+sina", "universe_source": snapshot["source"],
                    "start_date": START, "end_date": END, "decision_date": DECISION,
                    "adjustment": "none", "volume_unit": "shares", "amount_unit": "CNY",
                    "universe_captured_at": snapshot["captured_at"], "source_total": total,
                    "eligible_total": len(stocks), "warning": WARNING, "status": "downloading"}

        def progress():
            counts = dict(conn.execute("SELECT status,COUNT(*) FROM stocks GROUP BY status"))
            manifest.update(updated_at=now(), downloaded=counts.get("ok", 0), failed=counts.get("failed", 0),
                            pending=counts.get("pending", 0), complete_for_target=conn.execute("SELECT COUNT(*) FROM stocks WHERE status='ok' AND last_date=?", (END,)).fetchone()[0])
            manifest["sources"] = dict(conn.execute("SELECT source,COUNT(*) FROM stocks WHERE status='ok' GROUP BY source"))
            atomic_json(directory / "manifest.json", manifest)

        todo = [s for s in stocks if conn.execute("SELECT status FROM stocks WHERE code=?", (s["code"],)).fetchone()[0] != "ok"]
        if limit is not None:
            todo = todo[:limit]
        queue = asyncio.Queue()
        for stock in todo:
            queue.put_nowait(stock)
        pacing_lock = asyncio.Lock()
        last_request = [0.0]
        completed = [0]

        async def worker():
            while not queue.empty():
                stock = queue.get_nowait()
                code = stock["code"]
                error = None
                for attempt in range(3):
                    try:
                        async with pacing_lock:
                            loop = asyncio.get_running_loop()
                            await asyncio.sleep(max(0, .3 - (loop.time() - last_request[0])))
                            last_request[0] = loop.time()
                        symbol = stock["market"].lower() + code
                        if source == "sina":
                            response = await client.get("https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_data=/CN_MarketDataService.getKLineData", params={
                                "symbol": symbol, "scale": "240", "ma": "no", "datalen": "1023"})
                        else:
                            response = await client.get("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get", params={"param": f"{symbol},day,{START},{END},640,bfq"})
                        if response.status_code in {401, 403} or "waf.tencent.com" in response.text[:2000]:
                            raise RuntimeError("Provider access blocked; stopped this source without bypassing it.")
                        response.raise_for_status()
                        rows = parse_sina(response.text) if source == "sina" else parse_tencent(response.json().get("data") or {}, symbol)
                        payload = {"schema": "market-daily/v1", "data_mode": "historical", "source": source,
                                   **stock, "downloaded_at": now(), "adjustment": "none", "volume_unit": "shares", "bars": rows}
                        content = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
                        digest = hashlib.sha256(content).hexdigest()
                        target = directory / "daily" / f"{code}.json.gz"
                        temp = target.with_suffix(target.suffix + ".part")
                        with gzip.open(temp, "wb") as handle:
                            handle.write(content)
                        temp.replace(target)
                        with conn:
                            conn.execute("UPDATE stocks SET status='ok',first_date=?,last_date=?,bar_count=?,sha256=?,downloaded_at=?,error=NULL,source=? WHERE code=?",
                                         (rows[0]["date"], rows[-1]["date"], len(rows), digest, payload["downloaded_at"], source, code))
                        error = None
                        break
                    except httpx.HTTPStatusError as exc:
                        if exc.response.status_code in {401, 403}:
                            raise RuntimeError("Provider requires authorization; download stopped without bypassing it.") from exc
                        error = f"HTTP {exc.response.status_code}"
                    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                        error = type(exc).__name__ + ": " + str(exc)[:150]
                    await asyncio.sleep(.5 * (attempt + 1))
                if error:
                    with conn:
                        conn.execute("UPDATE stocks SET status='failed',error=? WHERE code=?", (error, code))
                completed[0] += 1
                if completed[0] % 25 == 0:
                    progress()
                    print(json.dumps({k: manifest[k] for k in ("downloaded", "failed", "pending", "complete_for_target")}), flush=True)
                queue.task_done()

        progress()
        print(f"Universe eligible={len(stocks)}; queued={len(todo)}; target={END}", flush=True)
        try:
            await asyncio.gather(*(worker() for _ in range(4)))
        finally:
            manifest["status"] = "partial" if conn.execute("SELECT COUNT(*) FROM stocks WHERE status!='ok'").fetchone()[0] else "complete"
            progress()
            conn.close()
        print(json.dumps(manifest, ensure_ascii=False), flush=True)
        return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--source", choices=("sina", "tencent"), default="sina")
    args = parser.parse_args()
    if args.limit is not None and not 1 <= args.limit <= 10_000:
        parser.error("limit must be positive")
    asyncio.run(download(DEFAULT_DIR, args.limit, args.source))

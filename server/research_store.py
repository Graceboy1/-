"""Owner-scoped, paginated local research archives, separate from login secrets.

Only explicit research snapshots are stored. Authentication sessions and model
configuration are deliberately not accepted by this module. The in-memory run
gate remains authoritative for starting a new replay.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
from uuid import uuid4


MAX_CONTEXT_BYTES = 200_000
MAX_SNAPSHOT_BYTES = 2_000_000
_lock = threading.RLock()
_DEFAULT_DB = Path(__file__).resolve().parents[1] / ".research-records.sqlite3"


class ArchiveError(Exception):
    """Safe generic storage failure; callers must not expose DB paths/content."""


class RecordMissing(Exception):
    pass


class RecordConflict(Exception):
    pass


def _path() -> Path:
    configured = os.getenv("CONTEST_RESEARCH_DB")
    return Path(configured).expanduser().resolve() if configured else _DEFAULT_DB


def _owner(username: str) -> str:
    if not isinstance(username, str) or not username:
        raise ArchiveError("A verified owner is required.")
    return hashlib.sha256(("research-owner/v1:" + username).encode("utf-8")).hexdigest()


def _json(value: dict, maximum: int = MAX_SNAPSHOT_BYTES) -> str:
    try:
        if not isinstance(value, dict):
            raise ValueError("snapshot must be an object")
        content = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        if len(content.encode("utf-8")) > maximum:
            raise ValueError("snapshot too large")
        return content
    except (ValueError, TypeError, OverflowError) as exc:
        raise ArchiveError("Invalid archive snapshot.") from exc


def _now() -> tuple[str, str]:
    instant = datetime.now(timezone.utc)
    return instant.isoformat(timespec="microseconds"), (instant + timedelta(hours=8)).date().isoformat()


@contextmanager
def _connection():
    try:
        with _lock:
            path = _path()
            if not path.exists():
                try:
                    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                except FileExistsError:
                    pass
                else:
                    os.close(fd)
            with closing(sqlite3.connect(path, timeout=3)) as conn, conn:
                conn.row_factory = sqlite3.Row
                conn.execute("CREATE TABLE IF NOT EXISTS research_records ("
                             "record_id TEXT PRIMARY KEY, owner TEXT NOT NULL, status TEXT NOT NULL, "
                             "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, local_day TEXT NOT NULL, "
                             "case_id TEXT NOT NULL, seat_id TEXT NOT NULL, title TEXT NOT NULL, "
                             "stock_label TEXT NOT NULL DEFAULT '', news_title TEXT NOT NULL DEFAULT '', "
                             "decision_date TEXT NOT NULL DEFAULT '', run_id TEXT UNIQUE, "
                             "context_json TEXT NOT NULL, analysis_json TEXT, replay_json TEXT, "
                             "replay_status TEXT, return_pct REAL)")
                conn.execute("CREATE INDEX IF NOT EXISTS research_owner_date "
                             "ON research_records(owner, local_day, seat_id, created_at DESC)")
                yield conn
    except (OSError, sqlite3.Error, ValueError) as exc:
        raise ArchiveError("Local archive unavailable.") from exc


def _detail(row) -> dict:
    try:
        return {"record_id": row["record_id"], "status": row["status"],
                "created_at": row["created_at"], "updated_at": row["updated_at"],
                "case_id": row["case_id"], "seat_id": row["seat_id"],
                "title": row["title"], "decision_date": row["decision_date"],
                "context": json.loads(row["context_json"]),
                "analysis": json.loads(row["analysis_json"]) if row["analysis_json"] else None,
                "replay": json.loads(row["replay_json"]) if row["replay_json"] else None}
    except (TypeError, ValueError, KeyError) as exc:
        raise ArchiveError("Invalid archive data.") from exc


def create_draft(username: str, case_id: str, seat_id: str, context: dict, *, title: str = "") -> dict:
    owner = _owner(username)
    context_json = _json(context, MAX_CONTEXT_BYTES)
    now, local_day = _now()
    record_id = uuid4().hex
    stock = context.get("stock") or {}
    news = context.get("news") or {}
    title = str(title or stock.get("label") or case_id)[:160]
    with _connection() as conn:
        conn.execute("INSERT INTO research_records "
                     "(record_id,owner,status,created_at,updated_at,local_day,case_id,seat_id,title,stock_label,news_title,context_json) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     (record_id, owner, "draft", now, now, local_day, case_id, seat_id, title,
                      str(stock.get("label") or "")[:160], str(news.get("title") or "")[:400], context_json))
    return get_record(username, record_id)


def get_record(username: str, record_id: str) -> dict:
    with _connection() as conn:
        row = conn.execute("SELECT * FROM research_records WHERE record_id=? AND owner=?",
                           (record_id, _owner(username))).fetchone()
        if row is None:
            raise RecordMissing()
        return _detail(row)


def list_records(username: str, *, limit: int = 10, offset: int = 0,
                 date: str | None = None, seat_id: str | None = None) -> dict:
    if not 1 <= limit <= 50 or not 0 <= offset <= 100_000:
        raise ValueError("Invalid page bounds.")
    clauses, parameters = ["owner=?"], [_owner(username)]
    if date:
        # Validate even when called directly, independently of HTTP validation.
        if datetime.strptime(date, "%Y-%m-%d").date().isoformat() != date:
            raise ValueError("Invalid calendar date.")
        clauses.append("local_day=?")
        parameters.append(date)
    if seat_id:
        clauses.append("seat_id=?")
        parameters.append(seat_id)
    where = " AND ".join(clauses)
    with _connection() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM research_records WHERE {where}", parameters).fetchone()[0]
        # Lists never load large evidence/plan/replay JSON into memory.
        rows = conn.execute("SELECT record_id,status,created_at,updated_at,case_id,seat_id,title,stock_label,news_title,"
                            "(replay_json IS NOT NULL) AS has_replay,"
                            f"decision_date,replay_status,return_pct FROM research_records WHERE {where} "
                            "ORDER BY created_at DESC,record_id DESC LIMIT ? OFFSET ?",
                            (*parameters, limit, offset)).fetchall()
        return {"items": [{**dict(row), "has_replay": bool(row["has_replay"])} for row in rows],
                "total": total, "limit": limit, "offset": offset}


def freeze(username: str, record_id: str, analysis: dict) -> None:
    content = _json(analysis)
    now, _ = _now()
    with _connection() as conn:
        changed = conn.execute("UPDATE research_records SET status='frozen',updated_at=?,analysis_json=?,"
                               "run_id=?,decision_date=? WHERE record_id=? AND owner=? AND status='draft' "
                               "AND case_id=? AND seat_id=?",
                               (now, content, analysis["run_id"], analysis["evidence"]["decision_date"], record_id,
                                _owner(username), analysis["evidence"]["case_id"], analysis["seat"]["id"])).rowcount
        if not changed:
            if conn.execute("SELECT 1 FROM research_records WHERE record_id=? AND owner=?",
                            (record_id, _owner(username))).fetchone() is None:
                raise RecordMissing()
            raise RecordConflict()


def save_replay(username: str, record_id: str, result: dict) -> None:
    content = _json(result)
    now, _ = _now()
    with _connection() as conn:
        row = conn.execute("SELECT status,replay_json FROM research_records WHERE record_id=? AND owner=?",
                           (record_id, _owner(username))).fetchone()
        if row is None:
            raise RecordMissing()
        if row["status"] not in {"frozen", "replayed"}:
            raise RecordConflict()
        if row["replay_json"]:
            previous = json.loads(row["replay_json"])
            strip_bars = lambda item: {key: value for key, value in item.items() if key != "replay_bars"}
            if strip_bars(previous) != strip_bars(result):
                raise RecordConflict()
            if "replay_bars" in previous and "replay_bars" in result and previous["replay_bars"] != result["replay_bars"]:
                raise RecordConflict()
            if "replay_bars" in previous:
                # An optional later non-chart request must not erase saved bars.
                content = row["replay_json"]
        conn.execute("UPDATE research_records SET status='replayed',updated_at=?,replay_json=?,"
                     "replay_status=?,return_pct=? WHERE record_id=? AND owner=?",
                     (now, content, result.get("status"), result.get("metrics", {}).get("return_pct"),
                      record_id, _owner(username)))

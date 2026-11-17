"""Local account registration and authentication for the loopback-only desk.

Legacy owner credentials migrate atomically into the account registry. The old
table stays intact; registration never resets or replaces another account.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import sqlite3
import threading
import time
from collections import OrderedDict, deque
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from .agent import ModelConfig, ModelFailure
from .model_setup import verify_workspace_model

router = APIRouter(prefix="/workspace", tags=["workspace"])
COOKIE_NAME = "contest_session"
COOKIE_PATH = "/contest-api"
SESSION_SECONDS = 8 * 60 * 60
MAX_SESSIONS = 16
PBKDF2_ROUNDS = 600_000
_DEFAULT_ACCOUNT_DB = Path(__file__).resolve().parents[1] / ".workspace-account.sqlite3"


class Credentials(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=2, max_length=32)
    password: Annotated[SecretStr, Field(min_length=1, max_length=128)]

    @field_validator("username")
    @classmethod
    def normalize_username(cls, value: str) -> str:
        name = value.strip()
        if re.fullmatch(r"[\w.-]{2,32}", name) is None:
            raise ValueError("账号须为 2–32 个字、数字、字母、点、下划线或短横线。")
        return name


class SetupCredentials(Credentials):
    password: Annotated[SecretStr, Field(min_length=10, max_length=128)]


class RegistrationCredentials(SetupCredentials):
    confirm_password: Annotated[SecretStr, Field(min_length=10, max_length=128)]


class ModelSetup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str | None = Field(default=None, max_length=512)
    model: str | None = Field(default=None, max_length=120)
    api_key: Annotated[SecretStr, Field(max_length=2048)] | None = None
    use_server_config: bool = Field(default=False, strict=True)


@dataclass(frozen=True)
class _Account:
    username: str
    salt: bytes = field(repr=False)
    digest: bytes = field(repr=False)
    is_owner: bool = False


@dataclass
class WorkspaceSession:
    session_id: str = field(repr=False)
    username: str
    ready: bool = False
    model_config: ModelConfig | None = field(default=None, repr=False)
    expires_at: float = field(default_factory=lambda: time.monotonic() + SESSION_SECONDS, repr=False)
    model_probe_in_progress: bool = field(default=False, repr=False)
    can_use_server_config: bool = False


_db_lock = threading.Lock()
_session_lock = threading.Lock()
_sessions: OrderedDict[str, WorkspaceSession] = OrderedDict()
_login_attempts: deque[float] = deque()
_registration_attempts: deque[float] = deque()
_probe_attempts: deque[float] = deque()


def _account_db() -> Path:
    configured = os.getenv("CONTEST_ACCOUNT_DB")
    return Path(configured).expanduser().resolve() if configured else _DEFAULT_ACCOUNT_DB


def _ensure_accounts(conn: sqlite3.Connection) -> None:
    """Run within an IMMEDIATE transaction before all account reads/writes."""
    conn.execute("CREATE TABLE IF NOT EXISTS accounts (username TEXT PRIMARY KEY COLLATE BINARY, "
                 "salt BLOB NOT NULL, digest BLOB NOT NULL, is_owner INTEGER NOT NULL DEFAULT 0)")
    if "is_owner" not in {field[1] for field in conn.execute("PRAGMA table_info(accounts)")}:
        conn.execute("ALTER TABLE accounts ADD COLUMN is_owner INTEGER NOT NULL DEFAULT 0")
    legacy = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='owner'").fetchone()
    row = conn.execute("SELECT username,salt,digest FROM owner WHERE id=1").fetchone() if legacy else None
    if row is not None:
        existing = conn.execute("SELECT salt,digest FROM accounts WHERE username=?", (row[0],)).fetchone()
        if existing is None:
            conn.execute("INSERT INTO accounts(username,salt,digest,is_owner) VALUES(?,?,?,1)", row)
        elif not (secrets.compare_digest(existing[0], row[1]) and secrets.compare_digest(existing[1], row[2])):
            # A partial/conflicting migration must not silently change who owns
            # legacy research records or which password authenticates the owner.
            raise HTTPException(503, "本地账号迁移状态不一致，请保留数据库并联系维护者。")
        else:
            conn.execute("UPDATE accounts SET is_owner=1 WHERE username=?", (row[0],))


@contextmanager
def _account_connection(*, create: bool = False):
    try:
        with _db_lock:
            db = _account_db()
            if not db.exists():
                if not create:
                    yield None
                    return
                try:
                    fd = os.open(db, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                except FileExistsError:
                    pass
                else:
                    os.close(fd)
            with closing(sqlite3.connect(f"{db.as_uri()}?mode=rw", uri=True, timeout=3)) as conn, conn:
                conn.execute("BEGIN IMMEDIATE")
                _ensure_accounts(conn)
                yield conn
    except (OSError, sqlite3.Error) as exc:
        raise HTTPException(503, "本地账号存储暂不可用。") from exc


def _read_account(username: str | None = None) -> _Account | None:
    with _account_connection() as conn:
        if conn is None:
            return None
        if username is None:
            # Used only to report whether any local account exists. Never return
            # a username/credential list through the public status response.
            row = conn.execute("SELECT username,salt,digest,is_owner FROM accounts ORDER BY username LIMIT 1").fetchone()
        else:
            row = conn.execute("SELECT username,salt,digest,is_owner FROM accounts WHERE username=?", (username,)).fetchone()
    return _Account(*row) if row else None


def _password_hash(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS)


def _create_account(credentials: SetupCredentials, *, first_only: bool = False) -> _Account:
    salt = secrets.token_bytes(16)
    account = _Account(credentials.username, salt, _password_hash(credentials.password.get_secret_value(), salt))
    with _account_connection(create=True) as conn:
        has_accounts = conn.execute("SELECT 1 FROM accounts LIMIT 1").fetchone() is not None
        if first_only and has_accounts:
            raise HTTPException(409, "本机已设置账号，请登录或使用注册入口创建其他账号。")
        if conn.execute("SELECT 1 FROM accounts WHERE username=?", (account.username,)).fetchone() is not None:
            raise HTTPException(409, "这个账号已存在，请更换账号名或直接登录。")
        try:
            conn.execute("INSERT INTO accounts(username,salt,digest,is_owner) VALUES(?,?,?,?)",
                         (account.username, account.salt, account.digest, int(not has_accounts)))
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, "这个账号已存在，请更换账号名或直接登录。") from exc
    return _Account(account.username, account.salt, account.digest, not has_accounts)


def _prune(now: float) -> None:
    while _sessions and next(iter(_sessions.values())).expires_at <= now:
        _sessions.popitem(last=False)


def _token_id(request: Request) -> str | None:
    token = request.cookies.get(COOKIE_NAME, "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
        return None
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _session(request: Request) -> WorkspaceSession | None:
    token_id = _token_id(request)
    with _session_lock:
        _prune(time.monotonic())
        return _sessions.get(token_id) if token_id is not None else None


def _require_session(request: Request) -> WorkspaceSession:
    session = _session(request)
    if session is None:
        raise HTTPException(401, "请先登录智慧交易助手。")
    return session


def require_ready(request: Request) -> WorkspaceSession:
    session = _require_session(request)
    if not session.ready:
        raise HTTPException(403, "请先配置模型或选择离线模式。")
    return session


def _start_session(request: Request, response: Response, username: str, *, can_use_server_config: bool = False) -> WorkspaceSession:
    previous = _token_id(request)
    token = secrets.token_urlsafe(32)
    token_id = hashlib.sha256(token.encode("ascii")).hexdigest()
    with _session_lock:
        _prune(time.monotonic())
        if previous is not None:
            _sessions.pop(previous, None)
        while len(_sessions) >= MAX_SESSIONS:
            _sessions.popitem(last=False)
        session = WorkspaceSession(token_id, username, can_use_server_config=can_use_server_config)
        _sessions[token_id] = session
    # The supported launcher is HTTP bound to loopback; do not deploy these cookie settings publicly.
    response.set_cookie(COOKIE_NAME, token, max_age=SESSION_SECONDS, path=COOKIE_PATH, httponly=True, samesite="strict")
    return session


def _status(session: WorkspaceSession | None, *, account_exists: bool | None = None) -> dict[str, bool | str | None]:
    if account_exists is None:
        account_exists = session is not None or _read_account() is not None
    with _session_lock:
        ready = bool(session and session.ready)
        config = session.model_config if session else None
    return {"account_exists": account_exists, "registration_available": True,
            "authenticated": session is not None, "ready": ready,
            "username": session.username if session else None, "ai_available": bool(ready and config),
            "model": config.model if ready and config else None,
            "server_model_available": bool(session and session.can_use_server_config and ModelConfig.from_env())}


def _take_attempt(bucket: deque[float], maximum: int, period: int, detail: str) -> None:
    """Call while holding _session_lock; one bounded bucket across local clients."""
    now = time.monotonic()
    while bucket and bucket[0] <= now - period:
        bucket.popleft()
    if len(bucket) >= maximum:
        retry = max(1, int(period - (now - bucket[0])) + 1)
        raise HTTPException(429, detail, headers={"Retry-After": str(retry)})
    bucket.append(now)


def _assert_active(session: WorkspaceSession) -> None:
    """Call under lock after any provider await to avoid activating a revoked session."""
    _prune(time.monotonic())
    if _sessions.get(session.session_id) is not session:
        raise HTTPException(401, "登录已失效，请重新登录。")


@router.get("/status")
def workspace_status(request: Request):
    return _status(_session(request))


@router.post("/setup")
def workspace_setup(body: SetupCredentials, request: Request, response: Response):
    # Compatibility for older first-run clients; never overwrite an existing
    # account or serve as a password reset endpoint.
    with _session_lock:
        _take_attempt(_registration_attempts, 5, 60, "注册尝试过多，请稍后重试。")
    account = _create_account(body, first_only=True)
    return _status(_start_session(request, response, account.username, can_use_server_config=bool(account.is_owner)), account_exists=True)


@router.post("/register")
def workspace_register(body: RegistrationCredentials, request: Request, response: Response):
    with _session_lock:
        _take_attempt(_registration_attempts, 5, 60, "注册尝试过多，请稍后重试。")
    if not secrets.compare_digest(body.password.get_secret_value().encode("utf-8"),
                                  body.confirm_password.get_secret_value().encode("utf-8")):
        raise HTTPException(422, "两次输入的密码不一致。")
    account = _create_account(body)
    return _status(_start_session(request, response, account.username, can_use_server_config=bool(account.is_owner)), account_exists=True)


@router.post("/login")
def workspace_login(body: Credentials, request: Request, response: Response):
    with _session_lock:
        _take_attempt(_login_attempts, 8, 60, "登录尝试过多，请稍后重试。")
    account = _read_account(body.username)
    digest = _password_hash(body.password.get_secret_value(), account.salt if account else b"\x00" * 16)
    password_ok = secrets.compare_digest(digest, account.digest if account else b"\x00" * 32)
    if account is None or not password_ok or body.username != account.username:
        raise HTTPException(401, "账号或密码不正确。")
    return _status(_start_session(request, response, account.username, can_use_server_config=bool(account.is_owner)), account_exists=True)


@router.post("/logout")
def workspace_logout(request: Request, response: Response):
    token_id = _token_id(request)
    if token_id is not None:
        with _session_lock:
            _sessions.pop(token_id, None)
    response.delete_cookie(COOKIE_NAME, path=COOKIE_PATH, httponly=True, samesite="strict")
    return _status(None)


@router.post("/model")
async def workspace_model(body: ModelSetup, request: Request):
    session = _require_session(request)
    if body.use_server_config:
        if not session.can_use_server_config:
            raise HTTPException(403, "此账号不能使用服务器预设，请配置自己的模型或选择离线模式。")
        if body.base_url is not None or body.model is not None or body.api_key is not None:
            raise HTTPException(422, "服务器预设和手动配置只能选择一种。")
    elif not (body.base_url and body.model and body.api_key and body.api_key.get_secret_value().strip()):
        raise HTTPException(422, "请填写模型地址、模型名称和 API Key。")
    with _session_lock:
        _assert_active(session)
        if session.model_probe_in_progress:
            raise HTTPException(409, "正在验证模型，请等待本次请求结束。")
        _take_attempt(_probe_attempts, 6, 120, "验证模型过于频繁，请稍后重试。")
        session.model_probe_in_progress = True
    try:
        config = await verify_workspace_model(base_url=body.base_url, model=body.model,
                                              api_key=body.api_key.get_secret_value() if body.api_key else None,
                                              use_server_config=body.use_server_config)
        with _session_lock:
            _assert_active(session)
            session.model_config = config
            session.ready = True
    except ModelFailure as exc:
        raise HTTPException(exc.status, exc.detail) from exc
    finally:
        with _session_lock:
            session.model_probe_in_progress = False
    return _status(session, account_exists=True)


@router.post("/offline")
def workspace_offline(request: Request):
    session = _require_session(request)
    with _session_lock:
        _assert_active(session)
        if session.model_probe_in_progress:
            raise HTTPException(409, "正在验证模型，请等待本次请求结束。")
        session.model_config = None
        session.ready = True
    return _status(session, account_exists=True)

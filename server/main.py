from __future__ import annotations

import asyncio
from contextlib import closing
import hashlib
import json
import os
import re
import sqlite3
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import uuid4

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .access import WorkspaceSession, require_ready, router as workspace_router
from .agent import ModelFailure, generate_plan
from .domain import Plan, replay
from .seats import SEATS
from . import research_store
from .news_research import build_research_context
from . import market_history
from . import global_news
from . import news_ai
from . import historical_seats
from . import historical_fundamentals
from . import paper_trading

load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)


@asynccontextmanager
async def app_lifespan(_app: FastAPI):
    await paper_trading.start()
    await global_news.start()
    try:
        yield
    finally:
        await paper_trading.stop()
        await global_news.stop()


app = FastAPI(title="智慧交易助手 · 多席位策略研究与交易复盘", version="0.3.1", lifespan=app_lifespan)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])
app.include_router(workspace_router)
app.include_router(market_history.router)
app.include_router(global_news.router)
app.include_router(news_ai.router)
app.include_router(paper_trading.router)
ReadyWorkspace = Annotated[WorkspaceSession, Depends(require_ready)]
LOCAL_WEB_ORIGINS = {"http://127.0.0.1:3001", "http://localhost:3001"}
ENGINE_VERSION = "evidence-desk/0.3.1"
RUN_TTL_SECONDS = 3600
MAX_RUNS = 100
try:
    MODEL_CALL_BUDGET = max(1, min(200, int(os.getenv("CONTEST_LLM_MAX_RUNS", "20"))))
except ValueError:
    MODEL_CALL_BUDGET = 20
model_runs = 0


class AnalyzeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case_id: str = Field(min_length=1, max_length=64)
    mode: Literal["offline", "ai"] = "offline"
    seat_id: str = Field(default="baseline", min_length=1, max_length=40)
    research_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")


class ResearchStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    news_id: str = Field(min_length=1, max_length=64)
    stock_id: str = Field(min_length=1, max_length=64)
    seat_id: str = Field(min_length=1, max_length=40)
    sector_id: str | None = Field(default=None, min_length=1, max_length=64)


class ReplayRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    include_bars: bool = Field(default=False, strict=True)


@dataclass(frozen=True)
class FrozenRun:
    case_id: str
    plan: Plan
    validation: dict[str, Any]
    expires: float
    seat_id: str = "baseline"
    decision_date: str = ""
    session_id: str = ""
    evidence_hash: str = ""
    research_id: str = ""
    archive_sha256: str = ""


runs: OrderedDict[str, FrozenRun] = OrderedDict()


def _rules_version(seat_id: str, *, historical: bool = False) -> str:
    root = Path(__file__).resolve().parent
    files = [root / "seats.py", root / "domain.py"]
    if historical:
        files.extend([root / "market_history.py", root / "historical_seats.py", root / "historical_fundamentals.py"])
    files.append(root / f"seat_{seat_id}.py" if seat_id in {"livermore", "buffett"} else root / "agent.py")
    try:
        return hashlib.sha256(b"".join(path.read_bytes() for path in files)).hexdigest()
    except OSError as exc:
        raise research_store.ArchiveError("Rule source unavailable.") from exc


def _evidence_hash(snapshot: dict) -> str:
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _case_for(seat, case_id):
    if case_id.startswith("history-"):
        return {**market_history.load_case(case_id), "seat_id": seat.id}
    return seat.cases.get(case_id)


def _snapshot_for(seat, case):
    return historical_seats.read_evidence(case, seat.id) if case.get("data_mode") == "historical" else seat.read_evidence(case)


@app.middleware("http")
async def protect_local_api(request: Request, call_next):
    origin = request.headers.get("origin")
    if request.method not in {"GET", "HEAD", "OPTIONS"} and (
        request.headers.get("sec-fetch-site") in {"cross-site", "same-site"}
        or (origin is not None and origin not in LOCAL_WEB_ORIGINS)
    ):
        return JSONResponse({"detail": "仅接受本地参赛页发起的请求。"}, status_code=403, headers={"Cache-Control": "no-store"})
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(RequestValidationError)
async def safe_validation_error(_request: Request, _error: RequestValidationError):
    # Pydantic's raw validation details may contain the submitted password/API Key.
    return JSONResponse({"detail": "输入格式不正确，请检查填写内容。"}, status_code=422)


@app.exception_handler(research_store.ArchiveError)
async def safe_archive_error(_request: Request, _error: research_store.ArchiveError):
    return JSONResponse({"detail": "本地研究记录暂时无法读写，操作未完成，请检查存储后重试。"}, status_code=503)


@app.exception_handler(research_store.RecordMissing)
async def missing_archive(_request: Request, _error: research_store.RecordMissing):
    return JSONResponse({"detail": "未找到这条研究记录。"}, status_code=404)


@app.exception_handler(research_store.RecordConflict)
async def conflicting_archive(_request: Request, _error: research_store.RecordConflict):
    return JSONResponse({"detail": "此研究记录已冻结或结果已保存，不能覆盖。请新建研究记录。"}, status_code=409)


@app.get("/health")
async def health():
    return {"service": "evidence-desk-contest", "version": ENGINE_VERSION}


@app.get("/cases")
async def cases(workspace: ReadyWorkspace, source: Literal["historical", "examples"] = "historical"):
    config = workspace.model_config
    if source == "historical":
        archive = market_history.status()
        samples = []
        financial_profiles = []
        if archive.get("available"):
            try:
                # The full universe is paginated by /market/stocks. A small
                # initial catalog gives every seat the same real securities.
                with closing(market_history.connection()) as conn:
                    rows = conn.execute(f"SELECT code,name FROM stocks WHERE {market_history.ELIGIBLE_SQL} ORDER BY code LIMIT 20").fetchall()
                    references = historical_fundamentals.profile_catalog()
                    if references:
                        codes = tuple(row["code"] for row in references[:40])
                        names = {row["code"]: row["name"] for row in conn.execute(
                            f"SELECT code,name FROM stocks WHERE {market_history.ELIGIBLE_SQL} AND code IN (" + ",".join("?" for _ in codes) + ")", codes)}
                        financial_profiles = [{**row, "name": names[row["code"]]} for row in references[:40] if row["code"] in names]
                for seat in SEATS.values():
                    samples.extend(market_history.sample(row["code"], row["name"], seat.id) for row in rows
                                   if re.fullmatch(r"(?:000|001|002|003|600|601|603|605)\d{3}", row["code"]))
            except sqlite3.Error:
                raise HTTPException(503, "真实历史股票目录暂不可读；未改用合成情景。") from None
        return {"seats": [seat.public() for seat in SEATS.values()], "cases": samples,
                "data_source": "historical", "archive": archive,
                "financial_profiles": financial_profiles,
                "ai_available": bool(config), "model": config.model if config else None}
    # Kept only as an explicit development/legacy compatibility path. The
    # formal desk never uses these test fixtures for a new demonstration.
    # Never send the fixtures wholesale. Their forward bars belong to replay only.
    public_fields = ("id", "label", "symbol", "name", "decision_date", "description")
    return {"seats": [seat.public() for seat in SEATS.values()],
            "cases": [{**{key: case[key] for key in public_fields}, "seat_id": seat.id}
                      for seat in SEATS.values() for case in seat.cases.values()],
            "data_source": "synthetic", "ai_available": bool(config), "model": config.model if config else None}


@app.post("/research/start")
async def start_research(body: ResearchStartRequest, workspace: ReadyWorkspace):
    try:
        context = build_research_context(body.news_id, body.stock_id, body.seat_id, body.sector_id)
    except (ValueError, KeyError) as exc:
        raise HTTPException(422, "新闻、股票、板块或席位关联不匹配，无法新建研究。") from exc
    seat = SEATS[context["seat_id"]]
    context["evidence_hash_at_start"] = _evidence_hash(seat.read_evidence(seat.cases[context["case_id"]]))
    record = research_store.create_draft(workspace.username, context["case_id"], context["seat_id"], context)
    return {key: record[key] for key in ("record_id", "case_id", "seat_id", "context", "status")}


@app.get("/research/records")
async def research_records(workspace: ReadyWorkspace,
                           limit: Annotated[int, Query(ge=1, le=50)] = 10,
                           offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
                           date: Annotated[str | None, Query(pattern=r"^\d{4}-\d{2}-\d{2}$")] = None,
                           seat_id: Annotated[str | None, Query(max_length=40)] = None):
    if seat_id is not None and seat_id not in SEATS:
        raise HTTPException(422, "未找到这个策略席位。")
    try:
        return research_store.list_records(workspace.username, limit=limit, offset=offset, date=date, seat_id=seat_id)
    except ValueError as exc:
        raise HTTPException(422, "日期或分页参数不正确。") from exc


@app.get("/research/records/{record_id}")
async def research_record(record_id: str, workspace: ReadyWorkspace):
    if len(record_id) != 32 or any(char not in "0123456789abcdef" for char in record_id):
        raise HTTPException(404, "未找到这条研究记录。")
    record = research_store.get_record(workspace.username, record_id)
    run_id = (record["analysis"] or {}).get("run_id")
    active = runs.get(run_id)
    record["replay_available"] = bool(active and active.session_id == workspace.session_id
                                      and active.expires > time.monotonic())
    return record


@app.post("/analyze")
async def analyze(body: AnalyzeRequest, workspace: ReadyWorkspace):
    global model_runs
    seat = SEATS.get(body.seat_id)
    if seat is None:
        raise HTTPException(404, "未找到这个策略席位。")
    if body.mode not in seat.modes:
        raise HTTPException(422, "此席位按已登记规则生成预案，不支持大模型替换买卖规则。")
    case = _case_for(seat, body.case_id)
    if case is None:
        raise HTTPException(404, "该席位没有此合成案例。")
    archive = None
    if body.research_id:
        archive = research_store.get_record(workspace.username, body.research_id)
        if archive["case_id"] != body.case_id or archive["seat_id"] != seat.id:
            raise HTTPException(409, "研究记录与当前案例或席位不匹配，请新建研究。")
        if archive["status"] != "draft":
            raise research_store.RecordConflict()
    snapshot = _snapshot_for(seat, case)
    rules_version = _rules_version(seat.id, historical=case.get("data_mode") == "historical")
    if archive is not None:
        old_version = archive["context"].get("rule", {}).get("version")
        old_evidence = archive["context"].get("evidence_hash_at_start")
        if ((old_version and old_version != rules_version)
                or (old_evidence and old_evidence != _evidence_hash(snapshot))):
            raise HTTPException(409, "新建研究后，席位规则或决策证据已变更；原记录已保留，请新建研究。")
    if body.mode == "ai":
        config = workspace.model_config
        if config is None:
            raise HTTPException(503, "本次会话没有已验证的模型。请打开 API 设置，或切换离线规则。")
        if model_runs >= MODEL_CALL_BUDGET:
            raise HTTPException(429, "本进程的模型预案额度已用完。离线演示不受影响。")
        model_runs += 1  # Reserve before the first await; failed attempts also count.
        try:
            plan, trace = await asyncio.wait_for(generate_plan(config, snapshot), timeout=28)
        except asyncio.TimeoutError as exc:
            raise HTTPException(504, "模型分析超过本次时间限制，可重试或选择离线规则演示。") from exc
        except ModelFailure as exc:
            raise HTTPException(exc.status, str(exc)) from exc
        model = config.model
    else:
        model = "离线规则"
        plan = historical_seats.propose(case, seat.id, snapshot) if case.get("data_mode") == "historical" else seat.propose(snapshot)
        trace = [
            {"step": "tool", "title": "本地读取历史证据", "detail": f"读取截至 {snapshot['decision_date']} 的{'真实历史' if case.get('data_mode') == 'historical' else '合成'}日线，未向模型或外部站点发送数据。"},
            {"step": "offline", "title": "席位生成条件预案", "detail": f"{seat.label}：{plan.rationale}"},
        ]
        if seat.id == "buffett":
            historical = case.get("data_mode") == "historical"
            trace.insert(1, {"step": "fundamentals", "title": "核对真实股票的时点财报与缺口" if historical else "按公告时点核验合成财报",
                             "detail": "核对真实年度参考的公告日期与原始版本确认状态；未验证当时版本的资料单独展示，不进入严格历史决策。缺失字段与人工能力圈/护城河确认不补造，证据不足保持观望。" if historical else "只使用决策日前可见的年度财报与人工业务评估；复算现金流代理、情景估值和安全边际。参数为本项目演示假设，不是模型推断或巴菲特本人标准。"})
    validation = historical_seats.validate(case, seat.id, plan, snapshot) if case.get("data_mode") == "historical" else seat.validate(plan, snapshot)
    risk_detail = "通过，允许模拟回放。" if validation["ok"] else "未通过；回放会拒绝执行。"
    if case.get("data_mode") == "historical" and validation["ok"]:
        risk_detail = "观望预案通过，可主动观察真实后续价格；未启用成交，也未建立持仓。"
    trace.append({"step": "risk", "title": "服务器独立风控", "detail": risk_detail})
    frozen_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if archive is None:
        archive = research_store.create_draft(workspace.username, body.case_id, seat.id,
                    {"version": "research-context/v1", "data_mode": case.get("data_mode", "synthetic"), "source": "research-desk",
                     "case_id": body.case_id, "seat_id": seat.id,
                     "stock": {"label": case["name"], "symbol": case["symbol"], "synthetic": case.get("data_mode") != "historical"},
                     **({"provenance": {"source": case["source"], "archive_sha256": case["archive_sha256"], "downloaded_at": case["downloaded_at"]}} if case.get("data_mode") == "historical" else {})},
                    title=case["label"])
    record = {"engine_version": ENGINE_VERSION, "rule_version": rules_version,
              "research_id": archive["record_id"], "research_context": archive["context"],
              "mode": body.mode, "model": model, "seat": seat.public(),
              "execution_policy": {"max_holding_sessions": seat.policy.max_holding_sessions,
                                   "uses_price_thresholds": seat.policy.use_price_thresholds,
                                   "limit_up_deferrals": seat.policy.limit_up_deferrals,
                                   "force_next_open_on_limit_down": seat.policy.force_next_open_on_limit_down,
                                   "commission_rate": str(seat.policy.commission_rate),
                                   "transfer_rate": str(seat.policy.transfer_rate),
                                   "sell_tax_rate": str(seat.policy.sell_tax_rate)},
              "evidence": snapshot,
              "plan": plan.model_dump(mode="json"), "validation": validation, "frozen_at": frozen_at}
    digest = hashlib.sha256(json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    run_id = uuid4().hex
    now = time.monotonic()
    while runs and (len(runs) >= MAX_RUNS or next(iter(runs.values())).expires <= now):
        runs.popitem(last=False)
    evidence_hash = _evidence_hash(snapshot)
    trace.append({"step": "freeze", "title": "预案已冻结", "detail": "本次预案与证据已计算 SHA-256；回放只接受此会话标识，不接受修改后的预案。"})
    response = {"run_id": run_id, **record, "trace": trace, "decision_hash": digest}
    # Durable write must succeed before advertising a frozen run to the client.
    research_store.freeze(workspace.username, archive["record_id"], response)
    runs[run_id] = FrozenRun(body.case_id, plan, validation, now + RUN_TTL_SECONDS,
                             seat_id=seat.id, decision_date=snapshot["decision_date"], session_id=workspace.session_id,
                             evidence_hash=evidence_hash, research_id=archive["record_id"], archive_sha256=case.get("archive_sha256", ""))
    return response


@app.post("/replay")
async def run_replay(body: ReplayRequest, workspace: ReadyWorkspace):
    stored = runs.get(body.run_id)
    if stored is None or stored.expires <= time.monotonic():
        runs.pop(body.run_id, None)
        raise HTTPException(404, "预案不存在或会话已过期，请重新生成。")
    if stored.session_id != workspace.session_id:
        raise HTTPException(404, "预案不存在或会话已过期，请重新生成。")
    seat = SEATS[stored.seat_id]
    case = _case_for(seat, stored.case_id)
    if case is None or case.get("decision_date") != stored.decision_date:
        raise HTTPException(409, "案例的决策时点已变更，请重新生成预案。")
    if stored.archive_sha256 and stored.archive_sha256 != case.get("archive_sha256"):
        raise HTTPException(409, "历史数据文件已变更，请新建研究；旧预案已保留。")
    current_snapshot = _snapshot_for(seat, case)
    current_hash = hashlib.sha256(json.dumps(current_snapshot, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    if stored.evidence_hash and current_hash != stored.evidence_hash:
        raise HTTPException(409, "决策时点的证据已变更，请重新生成预案。")
    if case.get("data_mode") == "historical":
        result = historical_seats.replay(case, seat.id, stored.plan, stored.validation, include_bars=body.include_bars)
    else:
        result = replay(case, stored.plan, stored.validation,
                        policy=seat.policy, validator=seat.validate, evidence_reader=seat.read_evidence,
                        include_bars=body.include_bars)
    response = {"seat_id": seat.id, **result}
    if stored.research_id:
        research_store.save_replay(workspace.username, stored.research_id, response)
    return response

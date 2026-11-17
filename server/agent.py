from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import ValidationError

from .domain import Plan


class ModelFailure(Exception):
    def __init__(self, detail: str, status: int = 502):
        super().__init__(detail)
        self.detail = detail
        self.status = status


@dataclass(frozen=True)
class ModelConfig:
    base_url: str
    model: str
    key: str = field(repr=False)

    @classmethod
    def from_env(cls) -> ModelConfig | None:
        base = os.environ.get("CONTEST_LLM_BASE_URL", "").strip().rstrip("/")
        name = os.environ.get("CONTEST_LLM_MODEL", "").strip()
        key = os.environ.get("CONTEST_LLM_API_KEY", "").strip()
        if not all((base, name, key)) or key.startswith("replace-") or len(name) > 120:
            return None
        try:
            url = urlsplit(base)
            loopback = url.hostname in {"127.0.0.1", "localhost", "::1"}
            secure = url.scheme == "https" or (loopback and url.scheme == "http")
            if not secure or not url.hostname or url.username or url.password or url.query or url.fragment:
                return None
            _ = url.port
        except ValueError:
            return None
        return cls(base, name, key)


TOOL_NAME = "get_history_evidence"
SYSTEM = """你是智慧交易助手的模拟策略助手。证券和行情均为合成教学案例。不能交易、不能访问互联网、不能读取未来行情。
先调用 get_history_evidence，参数只能为当前 case_id。读取工具结果后，选择 simulate（条件化模拟）或 hold（观望）。
只输出 JSON 对象，不输出思维链，不预测或承诺收益。用短段落写可查证的理由和不确定性，evidence_ids 只用工具提供的 ID。
数据无效或证据不足就观望。模拟须满足：入场区间围绕决策日收盘价，仓位 0 到 20（单位百分数），止损低于入场下沿，止盈高于入场上沿。
未来成交结果由服务器回放，不能由你推测或写入。观望例：
{"action":"hold","rationale":"价格方向不明确，等待证据。","evidence_ids":["替换成工具返回的证据ID"],"entry_low":null,"entry_high":null,"position_pct":null,"stop_loss":null,"take_profit":null}。
simulate 使用相同字段并填入数值。不要输出其他字段。"""


async def generate_plan(
    config: ModelConfig, snapshot: dict[str, Any], *, transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[Plan, list[dict[str, str]]]:
    """The model sees a single pre-cutoff snapshot. Replay bars never enter this module."""
    # Public demo slugs (e.g. "limit-up-no-fill") are outcome hints. The
    # provider receives neutral IDs; map its citations back after validation.
    case_id = "sample"
    citation_map = {f"sample:{ref.split(':', 1)[1]}": ref for ref in snapshot["evidence_ids"]}
    safe_snapshot = {**snapshot, "case_id": case_id, "evidence_ids": list(citation_map)}
    system = SYSTEM
    if snapshot.get("data_mode") == "historical":
        system = SYSTEM.replace("证券和行情均为合成教学案例。", "当前为真实历史价格研究，账户与预案仅作模拟；不使用当时未披露信息。缺少财报、竞价、涨跌停与盘口记录，不得编造这些数据。股票名称和历史表现不代表未来收益。")
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps({"case_id": case_id, "decision_date": snapshot["decision_date"], "request": "按证据生成历史模拟预案"}, ensure_ascii=False)},
    ]
    tool = {"type": "function", "function": {"name": TOOL_NAME, "description": "读取当前研究情景在决策日收盘及以前的日线和指标", "parameters": {
        "type": "object", "properties": {"case_id": {"type": "string", "enum": [case_id]}},
        "required": ["case_id"], "additionalProperties": False,
    }}}
    options: dict[str, Any] = {"model": config.model, "max_tokens": 1000, "stream": False}
    if urlsplit(config.base_url).hostname == "api.deepseek.com":
        options["thinking"] = {"type": "disabled"}
    try:
        async with httpx.AsyncClient(
            headers={"Authorization": f"Bearer {config.key}"}, timeout=httpx.Timeout(13, connect=5),
            transport=transport, follow_redirects=False,
        ) as client:
            async def completion(**extra: Any) -> dict[str, Any]:
                response = await client.post(f"{config.base_url}/chat/completions", json={**options, **extra})
                response.raise_for_status()
                return response.json()["choices"][0]["message"]

            first = await completion(messages=messages, tools=[tool], tool_choice={"type": "function", "function": {"name": TOOL_NAME}})
            calls = first.get("tool_calls", [])
            if len(calls) != 1:
                raise ModelFailure("模型没有执行唯一的行情证据工具调用；未生成预案。")
            call = calls[0]
            function = call["function"]
            if call["type"] != "function" or not isinstance(call["id"], str) or not call["id"] or function["name"] != TOOL_NAME or json.loads(function["arguments"]) != {"case_id": case_id}:
                raise ModelFailure("模型请求了未获授权的工具或案例；未读取其他数据。")
            # Preserve protocol fields, not private reasoning or arbitrary provider metadata.
            messages.append({"role": "assistant", "content": first.get("content"), "tool_calls": [call]})
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": json.dumps(safe_snapshot, ensure_ascii=False, allow_nan=False)})
            final = await completion(messages=messages, response_format={"type": "json_object"})
            plan = Plan.model_validate_json(final["content"])
            plan = plan.model_copy(update={"evidence_ids": [citation_map.get(ref, f"unverified:{ref}") for ref in plan.evidence_ids]})
    except httpx.TimeoutException as exc:
        raise ModelFailure("模型响应超时，可重试或选择离线规则演示。", 504) from exc
    except httpx.HTTPStatusError as exc:
        raise ModelFailure(f"模型服务返回 HTTP {exc.response.status_code}，请检查模型名、权限或额度。") from exc
    except httpx.RequestError as exc:
        raise ModelFailure("无法连接配置的模型服务；离线规则演示仍可用。") from exc
    except (KeyError, IndexError, TypeError, ValueError, ValidationError, AttributeError) as exc:
        raise ModelFailure("模型输出不符合预案格式；已拒绝，不会自动改成离线结果。") from exc
    return plan, [
        {"step": "tool", "title": "真实模型工具调用", "detail": f"{TOOL_NAME}：返回 {len(snapshot['bars'])} 根截至 {snapshot['decision_date']} 的日线。"},
        {"step": "model", "title": "模型返回结构化预案", "detail": f"{config.model} 返回 {plan.action}，接下来由独立规则校验。"},
    ]


def offline_plan(snapshot: dict[str, Any]) -> Plan:
    metrics = snapshot["metrics"]
    last = metrics.get("last_close")
    ids = [f"{snapshot['case_id']}:metrics"]
    if not metrics.get("data_valid") or not last:
        return Plan(action="hold", rationale="离线规则：输入行情有缺口或异常，不生成交易参数。", evidence_ids=ids)
    # Use only the decision close; no fixture identifier or future price decides the plan.
    return Plan(
        action="simulate", rationale="离线规则：以决策收盘价为锚，限定次日开盘入场区间、虚拟仓位及退出条件；不满足条件则不成交。",
        evidence_ids=ids + [f"{snapshot['case_id']}:bar:{snapshot['decision_date']}"],
        entry_low=round(last * .995, 2), entry_high=round(last * 1.015, 2), position_pct=15,
        stop_loss=round(last * .98, 2), take_profit=round(last * 1.06, 2),
    )

"""Seat registration: strategy evidence, proposal and execution remain independent."""

from dataclasses import dataclass
from typing import Callable

from . import seat_buffett, seat_livermore
from .agent import offline_plan
from .domain import Plan, ReplayPolicy, evidence, validate_plan
from .fixtures import CASES


@dataclass(frozen=True)
class Seat:
    id: str
    label: str
    description: str
    modes: tuple[str, ...]
    cases: dict[str, dict]
    read_evidence: Callable[[dict], dict]
    propose: Callable[[dict], Plan]
    validate: Callable[[Plan, dict], dict]
    policy: ReplayPolicy

    def public(self) -> dict:
        return {"id": self.id, "label": self.label, "description": self.description, "modes": list(self.modes)}


BASELINE = Seat(
    "baseline", "基础策略席位", "以决策日收盘价为锚的价格条件研究；可选择离线规则或已验证模型。预案不是成交。",
    ("offline", "ai"), CASES, evidence, offline_plan, validate_plan, ReplayPolicy(),
)
LIVERMORE = Seat(
    "livermore", "利弗莫尔席位", "按旧包 v2.1 核心规则重实现：低位涨停筛选、次日竞价分档、隔日收盘退出；必要证据缺失时观望，不补造竞价。",
    ("offline",), seat_livermore.CASES, seat_livermore.seat_evidence, seat_livermore.make_plan,
    seat_livermore.check_plan, seat_livermore.POLICY,
)
BUFFETT = Seat(
    "buffett", "巴菲特席位", "巴菲特价值思想启发的透明演示规则：财务质量、人工能力圈/护城河确认与保守安全边际。非本人模型。",
    ("offline",), seat_buffett.CASES, seat_buffett.seat_evidence, seat_buffett.make_plan,
    seat_buffett.check_plan, seat_buffett.POLICY,
)
SEATS = {seat.id: seat for seat in (LIVERMORE, BUFFETT, BASELINE)}

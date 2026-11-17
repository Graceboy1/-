"""Canonical offline news-to-research mapping; never trust a frame's snapshots.

Only fixed local synthetic fixtures are imported. No accounts, model setup,
network calls, application startup, credentials, or future bars are loaded here.
"""
from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
import hashlib
import importlib.util
from pathlib import Path
import re

from .seats import SEATS

VERSION = "news-research/v1"
_SOURCE = Path(__file__).resolve().parents[1] / "design-preview" / "news-earth" / "serve_earth.py"


@lru_cache(maxsize=1)
def _payload() -> dict:
    spec = importlib.util.spec_from_file_location("_canonical_news_research_fixture", _SOURCE)
    if spec is None or spec.loader is None:
        raise ValueError("新闻研究数据暂不可用。")
    source = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(source)
    payload = source.build_payload()
    if (payload.get("news_mode") != "synthetic" or payload.get("preview_only") is not True
            or payload.get("model_called") is not False):
        raise ValueError("新闻研究仅接受已登记的模拟数据。")
    return payload


def build_research_context(news_id: str, stock_id: str, seat_id: str,
                           sector_id: str | None = None) -> dict:
    """Return an independently reconstructed context for one explicit chain.

    A rejected seat sample can still be studied; eligible is never rewritten.
    The selected seat's exact case must exist. No other seat/case is substituted.
    """
    identifiers = (news_id, stock_id, seat_id) + ((sector_id,) if sector_id is not None else ())
    if any(not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value)
           for value in identifiers):
        raise ValueError("新闻、股票、板块或席位标识不正确。")
    data = _payload()
    news = next((item for item in data["news"] if item["id"] == news_id), None)
    stock = next((item for item in data["stocks"] if item["id"] == stock_id), None)
    seat = SEATS.get(seat_id)
    if news is None or stock is None or seat is None:
        raise ValueError("未找到所选新闻、股票或席位。")
    memberships = {link["sector_id"]: link for link in data["stock_links"] if link["stock_id"] == stock_id}
    links = [link for link in data["news_links"]
             if link["news_id"] == news_id and link["sector_id"] in memberships
             and (sector_id is None or link["sector_id"] == sector_id)]
    if not links:
        raise ValueError("这条新闻与所选股票之间没有已登记的板块关联。")
    rule = stock.get("rule_results", {}).get(seat_id)
    case_id = rule.get("case_id") if isinstance(rule, dict) else None
    if not isinstance(case_id, str) or case_id not in seat.cases:
        raise ValueError("当前席位没有这只模拟股票的对应研究案例。")
    # The version fingerprints the actual rule implementation and registry,
    # not an invented strategy name or a model's confidence score.
    root = Path(__file__).resolve().parent
    rule_files = [root / "seats.py", root / "domain.py"]
    if seat_id in {"livermore", "buffett"}:
        rule_files.append(root / f"seat_{seat_id}.py")
    else:
        rule_files.append(root / "agent.py")
    rule_version = hashlib.sha256(b"".join(path.read_bytes() for path in rule_files)).hexdigest()
    frozen_rule = deepcopy(rule)
    frozen_rule.update({"version": rule_version, "seat_label": seat.label,
                        "seat_description": seat.description})
    sectors = []
    for link in links:
        sector = next(item for item in data["sectors"] if item["id"] == link["sector_id"])
        sectors.append({**deepcopy(link), "id": sector["id"], "label": sector["label"],
                        "stock_match": deepcopy(memberships[sector["id"]])})
    return {"case_id": case_id, "seat_id": seat_id, "version": VERSION,
            "data_mode": "synthetic", "news": deepcopy(news),
            "stock": {key: deepcopy(stock[key]) for key in ("id", "label", "sector_id", "synthetic", "code", "ticker", "business_profile") if key in stock},
            "sectors": sectors, "rule": frozen_rule}

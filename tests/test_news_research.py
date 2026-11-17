"""Canonical news research contracts: local fixtures only, no account access."""
from copy import deepcopy
import json

import pytest

from server import news_research as news


def test_exact_seat_case_and_four_part_context():
    result = news.build_research_context("g1", "s1", "livermore")
    assert result["case_id"] == "livermore-normal"
    assert result["seat_id"] == "livermore"
    assert result["version"] == "news-research/v1"
    assert result["data_mode"] == "synthetic"
    assert result["news"]["source"] == "本地虚构情景 · 非真实新闻"
    assert result["news"]["summary"].startswith("假想新闻：")
    assert [item["id"] for item in result["sectors"]] == ["grid"]
    assert result["sectors"][0]["kind"] == "mentioned"
    assert result["sectors"][0]["evidence_excerpt"]
    assert "待核验" in result["sectors"][0]["reason"]
    assert "人工模拟" in result["sectors"][0]["stock_match"]["reason"]
    assert "不代表真实" in result["sectors"][0]["stock_match"]["reason"]
    assert result["rule"]["checks"]
    assert len(result["rule"]["version"]) == 64
    assert result["rule"]["seat_label"] == "利弗莫尔席位"
    serialized = json.dumps(result)
    for forbidden in ["future_bars", "replay_result", "api_key", "password", "session_id"]:
        assert forbidden not in serialized


def test_rejected_sample_is_studied_without_changing_gate_or_seat():
    result = news.build_research_context("g1", "s2", "livermore", "storage")
    assert result["case_id"] == "livermore-no-candidate"
    assert result["rule"]["eligible"] is False
    assert result["sectors"][0]["kind"] == "inferred"
    assert news.build_research_context("g1", "s2", "buffett")["case_id"] == "buffett-expensive"


@pytest.mark.parametrize("args", [
    ("missing", "s1", "livermore"), ("g1", "missing", "livermore"),
    ("g1", "s1", "missing"), ("g1", "s4", "livermore"),
    ("g1", "s1", "livermore", "storage"), ("g1", "s1", "livermore", "compute"),
    ("g1", "s1", "livermore", ""), ("../g1", "s1", "livermore"),
    (None, "s1", "livermore"), ("g1", 1, "livermore"),
    ("g1", "s1", "livermore", ["grid"]), ("g1", "s1", "livermore", "g" * 65),
])
def test_invalid_or_unassociated_targets_rejected(args):
    with pytest.raises(ValueError):
        news.build_research_context(*args)


def test_no_missing_seat_case_substitution(monkeypatch):
    data = deepcopy(news._payload())
    data["stocks"][0]["rule_results"].pop("livermore")
    monkeypatch.setattr(news, "_payload", lambda: data)
    with pytest.raises(ValueError, match="对应研究案例"):
        news.build_research_context("g1", "s1", "livermore")
    assert news.build_research_context("g1", "s1", "buffett")["case_id"] == "buffett-value"


def test_unknown_case_id_rejected_even_if_rule_present(monkeypatch):
    data = deepcopy(news._payload())
    data["stocks"][0]["rule_results"]["livermore"]["case_id"] = "normal-fill"
    monkeypatch.setattr(news, "_payload", lambda: data)
    with pytest.raises(ValueError, match="对应研究案例"):
        news.build_research_context("g1", "s1", "livermore")


def test_context_is_fresh_and_never_mutates_canonical_fixture():
    first = news.build_research_context("g14", "s1", "baseline", "grid")
    expected = deepcopy(first)
    first["news"]["title"] = "changed"
    first["stock"]["label"] = "changed"
    first["rule"]["eligible"] = False
    first["sectors"][0]["stock_match"]["reason"] = "changed"
    assert news.build_research_context("g14", "s1", "baseline", "grid") == expected


def test_reasons_identify_distinct_news_not_one_shared_template():
    first = news.build_research_context("g1", "s1", "baseline")["sectors"][0]
    second = news.build_research_context("g9", "s1", "baseline")["sectors"][0]
    assert first["reason"] != second["reason"]
    assert first["evidence_excerpt"] != second["evidence_excerpt"]

"""Pure, deterministic news annotations; never call a real news/model service."""
import re

import pytest

from server.news_relations import SECTORS, catalog, extract_relations, infer_location


@pytest.mark.parametrize("text,country,province,city", [
    ("深圳储能设备需求增长", "中国", "广东省", "深圳市"),
    ("Hangzhou plans a new solar power project", "中国", "浙江省", "杭州市"),
    ("法国巴黎举行会议", "法国", None, "巴黎"),
    ("London airline workers discuss a deal", "英国", None, "伦敦"),
    ("广东省发布电网规划", "中国", "广东省", None),
    ("Japan reviews new energy policy", "日本", None, None),
    ("US oil inventory report", "美国", None, None),
    ("South Sudan receives humanitarian support", "南苏丹", None, None),
    ("印度尼西亚扩大太阳能建设", "印度尼西亚", None, None),
    ("巴西利亚举行会议", "巴西", None, "巴西利亚"),
])
def test_clear_text_mentions(text, country, province, city):
    geo, basis = infer_location(text)
    assert (geo["country"], geo["province"], geo["city"]) == (country, province, city)
    assert -180 <= geo["lon"] <= 180 and -90 <= geo["lat"] <= 90
    assert "示意" in basis and "发生地" in basis


@pytest.mark.parametrize("text", [
    "Oil supply talks involve the United States and China",
    "日本与韩国计划重启航空运输谈判",
    "Paris and London discuss rail links",
    "北京与东京磋商",
])
def test_multiple_countries_are_not_forced_to_one_place(text):
    geo, basis = infer_location(text)
    assert geo is None and "多个国家" in basis


@pytest.mark.parametrize("text", [
    "United Nations issues a global outlook", "BBC reports a new inflation outlook",
    "Reuters reports a new energy outlook", "Air China signs a supply deal",
    "Bank of China issues bonds", "中国石油宣布产量计划", "中国国航增加国际航班", "Technology helps us plan tomorrow", "Georgia reviews policy",
    "Newscaster describes an event without a location", None, {"source": "New York"},
])
def test_no_location_guessed_from_provider_or_ambiguous_text(text):
    assert infer_location(text)[0] is None


def test_multiple_cities_same_country_reduce_to_country():
    geo, _ = infer_location("Beijing and Shanghai expand solar projects")
    assert geo["country"] == "中国" and geo["precision"] == "country" and geo["city"] is None
    geo, _ = infer_location("深圳和浙江省签署合作")
    assert geo["precision"] == "country"
    geo, _ = infer_location("广东省深圳市发布规划")
    assert geo["city"] == "深圳市"


def test_markup_is_not_executed_or_used_as_hidden_keyword_evidence():
    assert infer_location('<script>Beijing</script><b>Global economy</b>')[0] is None
    assert extract_relations({"id": "n1", "title": "<script>crude oil</script> ordinary story"}) == []
    links = extract_relations({"id": "n1", "title": "<b>Solar panels</b> demand", "summary": "<style>gold</style>"})
    assert [row["sector_id"] for row in links] == ["solar"]
    assert "<" not in links[0]["evidence_excerpt"]


@pytest.mark.parametrize("title", ["Oil prices fall sharply", "Oil prices rise sharply", "原油暴跌", "原油供应中断"])
def test_price_or_supply_keywords_do_not_fabricate_market_direction(title):
    links = extract_relations({"id": "n1", "title": title})
    assert len(links) == 1
    assert links[0]["direction"] == "unknown" and links[0]["impact_level"] == "unknown"
    assert links[0]["verified"] is False and links[0]["kind"] == "inferred"
    assert "关键词产业假设" in links[0]["reason"]


def test_all_related_sectors_are_returned_not_only_first():
    links = extract_relations({"id": "n1", "title": "Solar power and battery storage require a new power grid", "summary": "Semiconductor chips and shipping also discussed"})
    assert {row["sector_id"] for row in links} == {"solar", "storage", "grid", "semiconductor", "shipping"}
    assert all(row["news_id"] == "n1" for row in links)
    assert len(links) <= 8 and len(links) == len({row["sector_id"] for row in links})
    assert all(row["evidence_excerpt"] and len(row["evidence_excerpt"]) <= 150 for row in links)


@pytest.mark.parametrize("text", ["Vegas bookings rise", "spoiler alert", "shipment of potato chips", "The goldfish swam", "a scholarship winner", "She wins a gold medal", "黄金周出行人数公布"])
def test_english_keywords_have_word_boundaries(text):
    assert extract_relations({"id": "n1", "title": text}) == []


def test_bad_ids_or_inputs_never_make_relations():
    for value in (None, [], {}, {"id": "../n1", "title": "oil"}, {"id": "n" * 65, "title": "gold"}):
        assert extract_relations(value) == []


def test_bounded_processing_ignores_oversized_input_tail():
    assert infer_location("x" * 12_001 + "Beijing")[0] is None
    assert extract_relations({"id": "n1", "title": "x" * 12_001 + " oil"}) == []


def test_catalog_contains_real_business_references_not_seat_judgements():
    data = catalog()
    assert 4 <= len(data["stocks"]) <= 8
    sector_ids = {row["id"] for row in SECTORS}
    stocks = {row["id"]: row for row in data["stocks"]}
    assert len(stocks) == len(data["stocks"])
    for row in stocks.values():
        assert re.fullmatch(r"(?:000|001|002|003|600|601|603|605)\d{3}", row["code"])
        assert "ST" not in row["label"].upper()
        assert row["synthetic"] is False and row["rule_results"] == {}
        assert row["candidate_only"] is True and "case_id" not in row
        assert row["business_profile"] and row["source_url"].startswith("https://")
    for row in data["stock_links"]:
        assert row["sector_id"] in sector_ids and row["stock_id"] in stocks
        assert row["verified_news_exposure"] is False
        assert row["source_url"].startswith("https://") and row["verification_needed"]
        assert "case_id" not in row and "eligible" not in row


def test_catalog_returns_independent_copies():
    first, second = catalog(), catalog()
    first["stocks"][0]["rule_results"]["baseline"] = {"eligible": True}
    first["stock_links"][0]["reason"] = "tampered"
    assert second["stocks"][0]["rule_results"] == {}
    assert catalog()["stock_links"][0]["reason"] != "tampered"

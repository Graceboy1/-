"""Conservative, bounded news annotations; never a price forecast or seat result.

All functions are pure and perform no network/database/model calls. Location
anchors refer to text mentions, not a verified event venue. Sector candidates
come from explicit keyword matches; positive/negative returns are not inferred.
The small company catalog is business-reference data, not a screened universe.
The caller must intersect it with its current eligible stock directory.
"""
from __future__ import annotations

from copy import deepcopy
from html.parser import HTMLParser
import re
import unicodedata

VERSION = "news-relations/1"
SOURCE_CHECKED_DATE = "2026-11-16"
MAX_TEXT_CHARS = 12_000


class _PlainText(HTMLParser):
    """Discard markup and executable/style content; preserve word boundaries."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.ignored = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.ignored += 1
        elif not self.ignored:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.ignored = max(0, self.ignored - 1)
        if not self.ignored:
            self.parts.append(" ")

    def handle_data(self, value):
        if not self.ignored:
            self.parts.append(value)


def _text(value) -> str:
    if not isinstance(value, str):
        return ""
    parser = _PlainText()
    parser.feed(value[:MAX_TEXT_CHARS])
    return " ".join(unicodedata.normalize("NFKC", "".join(parser.parts)).split())


def _pattern(aliases: tuple[str, ...]) -> re.Pattern:
    # English words use ASCII-letter boundaries: oil != spoiler, gas != Vegas.
    parts = []
    for term in aliases:
        escaped = re.escape(term).replace(r"\ ", r"\s+")
        parts.append(r"(?<![A-Za-z])" + escaped + r"(?![A-Za-z])" if term.isascii() else escaped)
    return re.compile("|".join(parts), re.I)


# ISO-3166 numeric IDs agree with the existing Natural Earth country features.
# These points are coarse map anchors, not precise venues or sovereignty claims.
_COUNTRIES = (
    ("中国", "156", 103, 35, ("中国", "China", "PRC")),
    ("美国", "840", -102, 39, ("美国", "United States", "United States of America", "USA", "U.S.")),
    ("英国", "826", -3, 55, ("英国", "United Kingdom", "Britain", "UK", "U.K.")),
    ("法国", "250", 2, 47, ("法国", "France")),
    ("德国", "276", 10, 51, ("德国", "Germany")),
    ("日本", "392", 138, 38, ("日本", "Japan")),
    ("韩国", "410", 128, 36, ("韩国", "South Korea", "Republic of Korea")),
    ("印度", "356", 79, 23, ("印度", "India")),
    ("印度尼西亚", "360", 118, -3, ("印度尼西亚", "印尼", "Indonesia")),
    ("越南", "704", 107, 16, ("越南", "Vietnam", "Viet Nam")),
    ("泰国", "764", 101, 16, ("泰国", "Thailand")),
    ("新加坡", "702", 103.8, 1.35, ("新加坡", "Singapore")),
    ("马来西亚", "458", 102, 4, ("马来西亚", "Malaysia")),
    ("澳大利亚", "036", 134, -25, ("澳大利亚", "澳洲", "Australia")),
    ("新西兰", "554", 173, -41, ("新西兰", "New Zealand")),
    ("加拿大", "124", -106, 58, ("加拿大", "Canada")),
    ("墨西哥", "484", -103, 24, ("墨西哥", "Mexico")),
    ("巴西", "076", -52, -11, ("巴西", "Brazil")),
    ("阿根廷", "032", -65, -36, ("阿根廷", "Argentina")),
    ("俄罗斯", "643", 97, 61, ("俄罗斯", "Russia", "Russian Federation")),
    ("乌克兰", "804", 31, 49, ("乌克兰", "Ukraine")),
    ("意大利", "380", 13, 43, ("意大利", "Italy")),
    ("西班牙", "724", -4, 40, ("西班牙", "Spain")),
    ("荷兰", "528", 5, 52, ("荷兰", "Netherlands")),
    ("瑞士", "756", 8, 47, ("瑞士", "Switzerland")),
    ("沙特阿拉伯", "682", 45, 24, ("沙特阿拉伯", "沙特", "Saudi Arabia")),
    ("阿联酋", "784", 54, 24, ("阿联酋", "United Arab Emirates", "UAE")),
    ("伊朗", "364", 54, 32, ("伊朗", "Iran")),
    ("以色列", "376", 35, 31, ("以色列", "Israel")),
    ("巴勒斯坦", "275", 35.2, 31.9, ("巴勒斯坦", "Palestine", "Gaza", "加沙")),
    ("黎巴嫩", "422", 35.8, 33.8, ("黎巴嫩", "Lebanon")),
    ("叙利亚", "760", 38, 35, ("叙利亚", "Syria")),
    ("埃及", "818", 30, 27, ("埃及", "Egypt")),
    ("南非", "710", 25, -29, ("南非", "South Africa")),
    ("尼日利亚", "566", 8, 9, ("尼日利亚", "Nigeria")),
    ("肯尼亚", "404", 38, 1, ("肯尼亚", "Kenya")),
    ("土耳其", "792", 35, 39, ("土耳其", "Türkiye", "Turkiye")),
    ("巴基斯坦", "586", 69, 30, ("巴基斯坦", "Pakistan")),
    ("孟加拉国", "050", 90, 24, ("孟加拉国", "Bangladesh")),
    ("苏丹", "729", 30, 16, ("苏丹", "Sudan")),
    ("南苏丹", "728", 30, 7, ("南苏丹", "South Sudan")),
    ("刚果（金）", "180", 23, -3, ("刚果民主共和国", "刚果（金）", "Democratic Republic of the Congo", "DR Congo")),
)
_COUNTRY_BY_NAME = {row[0]: row for row in _COUNTRIES}
_COUNTRY_PATTERNS = [(row, _pattern(row[4])) for row in _COUNTRIES]

# Country is inferred only from the explicitly matched administrative locality.
# Ambiguous Washington/Georgia/Jordan and provider headquarters are not anchors.
_LOCALITIES = (
    ("中国", "北京市", "北京市", 116.4, 39.9, ("北京", "Beijing")),
    ("中国", "上海市", "上海市", 121.5, 31.2, ("上海", "Shanghai")),
    ("中国", "天津市", "天津市", 117.2, 39.1, ("天津", "Tianjin")),
    ("中国", "重庆市", "重庆市", 106.6, 29.6, ("重庆", "Chongqing")),
    ("中国", "广东省", "深圳市", 114.1, 22.5, ("深圳", "Shenzhen")),
    ("中国", "广东省", "广州市", 113.3, 23.1, ("广州", "Guangzhou")),
    ("中国", "浙江省", "杭州市", 120.2, 30.3, ("杭州", "Hangzhou")),
    ("中国", "江苏省", "南京市", 118.8, 32.1, ("南京", "Nanjing")),
    ("中国", "江苏省", "苏州市", 120.6, 31.3, ("苏州", "Suzhou")),
    ("中国", "四川省", "成都市", 104.1, 30.7, ("成都", "Chengdu")),
    ("中国", "湖北省", "武汉市", 114.3, 30.6, ("武汉", "Wuhan")),
    ("中国", "陕西省", "西安市", 108.9, 34.3, ("西安市", "Xi'an")),
    ("中国", "广东省", None, 113.3, 23.5, ("广东省", "广东", "Guangdong")),
    ("中国", "浙江省", None, 120, 29.2, ("浙江省", "浙江", "Zhejiang")),
    ("中国", "江苏省", None, 119, 33, ("江苏省", "江苏", "Jiangsu")),
    ("中国", "山东省", None, 118, 36.4, ("山东省", "山东", "Shandong")),
    ("美国", None, "纽约", -74, 40.7, ("纽约", "New York City", "New York")),
    ("美国", None, "旧金山", -122.4, 37.8, ("旧金山", "San Francisco")),
    ("美国", None, "洛杉矶", -118.2, 34.1, ("洛杉矶", "Los Angeles")),
    ("美国", None, "华盛顿哥伦比亚特区", -77, 38.9, ("华盛顿特区", "Washington DC", "Washington D.C.")),
    ("英国", None, "伦敦", -.1, 51.5, ("伦敦", "London")),
    ("法国", None, "巴黎", 2.35, 48.9, ("巴黎", "Paris")),
    ("德国", None, "柏林", 13.4, 52.5, ("柏林", "Berlin")),
    ("瑞士", None, "日内瓦", 6.1, 46.2, ("日内瓦", "Geneva")),
    ("日本", None, "东京", 139.7, 35.7, ("东京", "Tokyo")),
    ("韩国", None, "首尔", 127, 37.6, ("首尔", "Seoul")),
    ("印度", None, "新德里", 77.2, 28.6, ("新德里", "New Delhi")),
    ("巴西", None, "巴西利亚", -47.9, -15.8, ("巴西利亚", "Brasilia", "Brasília")),
    ("加拿大", None, "渥太华", -75.7, 45.4, ("渥太华", "Ottawa")),
    ("俄罗斯", None, "莫斯科", 37.6, 55.8, ("莫斯科", "Moscow")),
    ("澳大利亚", None, "堪培拉", 149.1, -35.3, ("堪培拉", "Canberra")),
    ("沙特阿拉伯", None, "利雅得", 46.7, 24.7, ("利雅得", "Riyadh")),
    ("阿联酋", None, "迪拜", 55.3, 25.2, ("迪拜", "Dubai")),
)
_LOCALITY_PATTERNS = [(row, _pattern(row[5])) for row in _LOCALITIES]


def infer_location(text: str) -> tuple[dict | None, str]:
    """Return one explicit text-mention anchor, never a guessed event location.

    Several countries => no unique point. Several cities in one country => only
    a country anchor. A country match does not mean the article occurred there.
    """
    content = _text(text)
    # Do not treat an organization name, even a known national carrier, as an
    # explicit country mention. No provider-name or byline argument is accepted.
    content = re.sub(r"\b(?:Air China|Bank of China|China Eastern|China Southern|China Mobile|United States Steel)\b", "", content, flags=re.I)
    content = re.sub(r"中国(?:银行|石油|石化|国航|船舶|海油|移动|联通|电信|稀土|中车|中铁|铁建|中冶|建筑|交建|铝业|中免|人寿|平安)", "", content)
    # Prefer the longer country name; Sudan must not double-match South Sudan,
    # India must not match Chinese 印度尼西亚, Brazil must not match 巴西利亚.
    countries = set()
    spans = []
    matches = [(match.start(), match.end(), row[0]) for row, pattern in _COUNTRY_PATTERNS for match in pattern.finditer(content)]
    # Uppercase US is a country abbreviation; lowercase "us" is a pronoun.
    matches.extend((match.start(), match.end(), "美国") for match in re.finditer(r"(?<![A-Za-z])US(?![A-Za-z])", content))
    for start, end, country in sorted(matches, key=lambda m: m[1] - m[0], reverse=True):
        if any(start >= left and end <= right for left, right in spans):
            continue
        countries.add(country)
        spans.append((start, end))
    localities = [row for row, pattern in _LOCALITY_PATTERNS if pattern.search(content)]
    countries.update(row[0] for row in localities)
    if len(countries) > 1:
        return None, "文本提及多个国家或跨境地点，未强行选择唯一发生地。"
    if not countries:
        return None, "未识别到可唯一定位的明确地名；不按新闻来源或机构总部猜测。"
    country = next(iter(countries))
    descriptor = _COUNTRY_BY_NAME[country]
    cities = [row for row in localities if row[2] is not None]
    provinces = [row for row in localities if row[2] is None]
    chosen = None
    if len(cities) == 1:
        chosen = cities[0]
        # A different province explicitly mentioned alongside a city is not one
        # unambiguous city point, even when both localities are in China.
        if any(row[1] != chosen[1] for row in provinces):
            chosen = None
    elif not cities and len(provinces) == 1:
        chosen = provinces[0]
    if chosen:
        geo = {"country": country, "country_id": descriptor[1], "province": chosen[1], "city": chosen[2],
               "lon": chosen[3], "lat": chosen[4], "precision": "city" if chosen[2] else "province"}
        return geo, "新闻文本明确提及该行政地名；坐标仅作地理示意，不证明事件具体发生地。"
    geo = {"country": country, "country_id": descriptor[1], "province": None, "city": None,
           "lon": descriptor[2], "lat": descriptor[3], "precision": "country"}
    return geo, "新闻文本明确提及该国家；使用国家示意中心，不补猜省市或事件发生地。"


_SECTOR_RULES = (
    ("energy", "油气能源", ("石油", "原油", "天然气", "液化天然气", "crude oil", "oil", "natural gas", "LNG", "OPEC", "petroleum")),
    ("grid", "电网设备", ("电网", "输电", "配电", "变电", "power grid", "electricity grid", "transmission line", "electric grid")),
    ("storage", "储能", ("储能", "电池储存", "energy storage", "battery storage", "grid-scale battery", "BESS")),
    ("solar", "光伏", ("光伏", "太阳能", "photovoltaic", "photovoltaics", "solar power", "solar panel", "solar panels", "solar energy")),
    ("semiconductor", "半导体", ("半导体", "芯片", "集成电路", "晶圆", "semiconductor", "semiconductors", "microchip", "microchips", "chips", "chip", "wafer", "wafers")),
    ("shipping", "航运港口", ("航运", "集装箱", "海运", "shipping", "container ship", "container ships", "maritime", "freight vessel")),
    ("aviation", "航空运输", ("航空运输", "航班", "航空公司", "客机", "airline", "airlines", "air travel", "passenger flight", "passenger flights", "aviation")),
    ("gold", "黄金", ("黄金", "金价", "gold", "bullion", "gold mining")),
)
SECTORS = [{"id": row[0], "label": row[1]} for row in _SECTOR_RULES]
_SECTOR_PATTERNS = [(row, _pattern(row[2])) for row in _SECTOR_RULES]


def extract_relations(news: dict) -> list[dict]:
    """Annotate at most eight sector hypotheses, citing the matching RSS text.

    Keyword presence alone cannot identify supply/demand recipients, commercial
    exposure, sentiment strength, or an A-share price effect. Keep these unknown.
    """
    if not isinstance(news, dict) or not isinstance(news.get("id"), str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", news["id"]):
        return []
    content = _text(_text(news.get("title")) + " " + _text(news.get("summary")))
    result = []
    for (sector_id, label, _), pattern in _SECTOR_PATTERNS:
        sector_text = content
        if sector_id == "semiconductor":
            sector_text = re.sub(r"\b(?:potato|tortilla|corn|poker|casino|fish\s+and)\s+chips?\b|\bchips?\s+and\s+fish\b", " ", sector_text, flags=re.I)
        elif sector_id == "gold":
            sector_text = re.sub(r"\bgold\s+(?:medals?|medallists?|medalists?)\b|黄金周", " ", sector_text, flags=re.I)
        match = pattern.search(sector_text)
        if match is None:
            continue
        excerpt = sector_text[max(0, match.start() - 40):min(len(sector_text), match.end() + 70)]
        result.append({"news_id": news["id"], "sector_id": sector_id, "direction": "unknown", "impact_level": "unknown",
                       "kind": "inferred", "reason": f"文本出现“{match.group(0)}”，可关注{label}相关产业链；仅为关键词产业假设，未证实对A股公司收入、订单或股价的影响。",
                       "evidence_excerpt": excerpt, "method": VERSION, "verified": False})
    return result


# Business descriptions below are short paraphrases, not financial data or
# strategy judgements. The dates denote source review, not guaranteed freshness.
_COMPANIES = (
    ("601857", "中国石油", ("energy",), "涉及油气勘探开发、炼化以及成品油和天然气销售。",
     "https://static.cninfo.com.cn/finalpage/2026-08-31/1225535179.PDF", "中国石油2026年半年度报告：公司基本情况简介"),
    ("601012", "隆基绿能", ("solar",), "从事光伏硅片、电池组件及相关太阳能解决方案业务。",
     "https://static.cninfo.com.cn/finalpage/2026-04-29/1225251679.PDF", "隆基绿能2025年年度报告摘要：主要业务简介"),
    ("600406", "国电南瑞", ("grid", "storage"), "提供电网智能化产品，并涉及新型储能系统相关业务。",
     "https://static.cninfo.com.cn/finalpage/2026-04-30/1225257886.PDF", "国电南瑞2025年年度报告：主要业务"),
    ("600584", "长电科技", ("semiconductor",), "面向半导体客户提供集成电路封装与测试相关服务。",
     "https://static.cninfo.com.cn/finalpage/2025-08-21/1224525817.PDF", "长电科技2025年半年度报告：业务及经营情况（仍须复核后续公告）"),
    ("601919", "中远海控", ("shipping",), "主要从事集装箱航运和码头相关业务。",
     "https://hold.coscoshipping.com/cms_files/filemanager/1631/attach/20253/4AECD90D2D47D2E3A9DF2BBFBF03F2C5.pdf", "中远海控2024年年度报告：业务情况"),
    ("601111", "中国国航", ("aviation",), "经营航空客运等航空运输相关业务。",
     "https://www.airchina.com.cn/zh-CN/content/about_us/company/", "中国国航官网：公司简介"),
    ("600547", "山东黄金", ("gold",), "从事黄金矿山开采、选冶及相关黄金产品业务。",
     "https://www.sdhjgf.com.cn/column/84/", "山东黄金矿业股份有限公司官网：公司简介"),
)


def catalog() -> dict:
    """Return fresh business-candidate records; none has a seat pass or case ID."""
    stocks, links = [], []
    for code, label, sectors, business, url, source_title in _COMPANIES:
        identifier = "a" + code
        source = {"source_url": url, "source_title": source_title, "source_checked_at": SOURCE_CHECKED_DATE}
        stocks.append({"id": identifier, "code": code, "ticker": code, "label": label, "sector_id": sectors[0],
                       "synthetic": False, "business_profile": business, "rule_results": {},
                       "candidate_only": True, **source})
        for sector_id in sectors:
            links.append({"stock_id": identifier, "sector_id": sector_id, "business_profile": business,
                          "reason": "依据公司公开业务说明建立产业观察候选关联，不表示公司参与此新闻事件或会从中获益。",
                          "verification_needed": "须复核当前股票名称/ST状态、最新业务及该新闻的实际收入或订单敞口；未计算当前席位条件，不作为买卖建议。",
                          "kind": "business_reference", "verified_news_exposure": False, **source})
    return deepcopy({"stocks": stocks, "stock_links": links})

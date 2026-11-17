"""Read-only local geographic-news design preview; never the formal app.

The map is a pinned local public dataset. All news, locations, sector links and
stock identities are explicitly synthetic. Only offline seat gates are reused.
No outbound requests, account reads, writes, model calls or live news at runtime.
"""
from __future__ import annotations

import argparse
import hashlib
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
from pathlib import Path
from urllib.parse import unquote, urlsplit

PREVIEW_DIR = Path(__file__).resolve().parent
SYNTHETIC_SOURCE = PREVIEW_DIR.parent / "news-network" / "serve_news.py"
SHARED_SPHERE_CORE = PREVIEW_DIR.parent / "news-spheres" / "sphere-core.js"
HOST = "127.0.0.1"
DEFAULT_PORT = 3008
MAX_JSON_BYTES = 1_000_000
MAX_STATIC_BYTES = 2_000_000
MAX_REQUEST_TARGET = 2048
STATIC_TYPES = {
    "index.html": "text/html; charset=utf-8",
    "earth.css": "text/css; charset=utf-8",
    "earth-app.js": "text/javascript; charset=utf-8",
    "earth-renderer.js": "text/javascript; charset=utf-8",
    "geo-core.js": "text/javascript; charset=utf-8",
    "sphere-core.js": "text/javascript; charset=utf-8",
    "vendor/d3-array.min.js": "text/javascript; charset=utf-8",
    "vendor/d3-geo.min.js": "text/javascript; charset=utf-8",
    "vendor/topojson-client.min.js": "text/javascript; charset=utf-8",
    "vendor/countries-110m.json": "application/json; charset=utf-8",
    "vendor/d3-array.LICENSE": "text/plain; charset=utf-8",
    "vendor/d3-geo.LICENSE": "text/plain; charset=utf-8",
    "vendor/topojson-client.LICENSE": "text/plain; charset=utf-8",
    "vendor/world-atlas.LICENSE": "text/plain; charset=utf-8",
}

# Coordinates are approximate city anchors for fictional scenarios, not event sites.
# Province/city describe only the selected demo anchor; missing regions stay null.
# country_id is a zero-padded numeric ISO id matching the pinned atlas features.
# Each place = lon, lat, country, label, province, city, country_id.
PLACES = {
    "beijing": (116.4074, 39.9042, "中国", "北京", "北京市", "北京市", "156"),
    "washington": (-77.0369, 38.9072, "美国", "华盛顿", None, "华盛顿", "840"),
    "tokyo": (139.6917, 35.6895, "日本", "东京", None, "东京", "392"),
    "seoul": (126.9780, 37.5665, "韩国", "首尔", None, "首尔", "410"),
    "berlin": (13.4050, 52.5200, "德国", "柏林", None, "柏林", "276"),
    "paris": (2.3522, 48.8566, "法国", "巴黎", None, "巴黎", "250"),
    "london": (-0.1276, 51.5072, "英国", "伦敦", None, "伦敦", "826"),
    "brasilia": (-47.8825, -15.7942, "巴西", "巴西利亚", None, "巴西利亚", "076"),
    "canberra": (149.1300, -35.2809, "澳大利亚", "堪培拉", None, "堪培拉", "036"),
    "riyadh": (46.6753, 24.7136, "沙特阿拉伯", "利雅得", None, "利雅得", "682"),
    "ottawa": (-75.6972, 45.4215, "加拿大", "渥太华", None, "渥太华", "124"),
    "shenzhen": (114.0579, 22.5431, "中国", "深圳", "广东省", "深圳市", "156"),
    "hangzhou": (120.1551, 30.2741, "中国", "杭州", "浙江省", "杭州市", "156"),
}

# news id, place key, time, category, full title, compact title, summary, links
# each link = sector, direction, ordinal impact, explicit-text/inferred relation
NEWS_SEEDS = (
    ("g1", "beijing", "09:40", "电力建设", "配电网更新计划进入设备采购阶段", "配电网更新计划", "虚构规划提出推进配电设备更新；储能配套为人工推测关系。", (("grid", "opportunity", "high", "mentioned"), ("storage", "opportunity", "low", "inferred"))),
    ("g2", "beijing", "09:46", "算力建设", "算力园区扩容与电力配套同步规划", "算力园区扩容", "虚构园区提出扩容，涉及算力基础设施；电力配套需求尚只是情景推测。", (("compute", "opportunity", "high", "mentioned"), ("grid", "opportunity", "medium", "inferred"))),
    ("g3", "washington", "09:52", "产业投资", "数据中心建设预算出现上调情景", "数据中心预算上调", "虚构预算情景提高数据中心建设支出，同时增加节能与电力配套需求。", (("compute", "opportunity", "high", "mentioned"), ("efficiency", "opportunity", "medium", "inferred"), ("grid", "opportunity", "low", "inferred"))),
    ("g4", "tokyo", "10:00", "设备需求", "工业节能改造订单需求增加", "节能改造需求", "虚构采购清单增加工业节能设备；不代表任何现实公司的订单。", (("efficiency", "opportunity", "medium", "mentioned"),)),
    ("g5", "seoul", "10:08", "成本变化", "储能材料供应出现阶段性收紧", "储能材料供应收紧", "虚构材料供给情景提高储能设备成本；影响方向和等级仅用于界面展示。", (("storage", "risk", "high", "inferred"),)),
    ("g6", "berlin", "10:16", "能效标准", "工业能效要求与改造投入同步提高", "工业能效要求提高", "虚构标准调整增加节能设备需求，但算力设施面临改造成本与效率收益并存的情景。", (("efficiency", "opportunity", "high", "mentioned"), ("compute", "mixed", "medium", "inferred"))),
    ("g7", "paris", "10:24", "电网投资", "电网灵活性项目拟增加储能配置", "电网灵活性项目", "虚构项目讨论储能与电网设备配置；没有真实项目、地点或供货关系核验。", (("storage", "opportunity", "medium", "mentioned"), ("grid", "opportunity", "medium", "mentioned"))),
    ("g8", "london", "10:32", "融资环境", "设备项目融资成本上升情景", "项目融资成本上升", "虚构融资成本变化可能压缩设备项目预算；仅演示负面关联，不是宏观预测。", (("grid", "risk", "medium", "inferred"), ("storage", "risk", "low", "inferred"))),
    ("g9", "brasilia", "10:40", "基础设施", "输配电升级项目公布采购清单", "输配电升级采购", "虚构输配电采购清单涉及电网设备，未绑定真实证券或项目。", (("grid", "opportunity", "high", "mentioned"),)),
    ("g10", "canberra", "10:48", "能源配置", "储能调峰项目进入方案评审", "储能调峰方案评审", "虚构调峰方案讨论储能部署；电网协同仅用于演示潜在关联。", (("storage", "opportunity", "high", "mentioned"), ("grid", "opportunity", "low", "inferred"))),
    ("g11", "riyadh", "10:56", "产业建设", "算力设施与节能系统联合建设", "算力节能联合建设", "虚构建设方案直接涉及算力和节能系统；不代表真实政策或投资。", (("compute", "opportunity", "medium", "mentioned"), ("efficiency", "opportunity", "high", "mentioned"))),
    ("g12", "ottawa", "11:04", "能源约束", "电力接入容量限制数据中心扩建", "电力接入容量约束", "虚构接入限制增加算力扩建约束，也可能引出电网升级需求；二者为不同板块的演示方向。", (("compute", "risk", "high", "mentioned"), ("grid", "opportunity", "medium", "inferred"))),
    ("g13", None, "11:12", "全球议题", "全球设备需求讨论：地点尚未明确", "全球设备需求讨论", "虚构全球议题没有明确发生地，仅保留于全量列表，不将其强行定位到某个国家。", (("efficiency", "unknown", "low", "inferred"),)),
    ("g14", "shenzhen", "11:20", "储能建设", "深圳储能配套与配电设备更新进入规划阶段", "深圳储能配套规划", "虚构深圳项目讨论储能配置与配电设备更新；省市仅为情景定位，不代表真实项目或订单。", (("storage", "opportunity", "high", "mentioned"), ("grid", "opportunity", "medium", "mentioned"))),
    ("g15", "hangzhou", "11:28", "算力建设", "杭州算力园区扩容同步评估节能改造", "杭州算力园区扩容", "虚构杭州园区讨论算力扩容与节能系统改造；未核验任何现实建设或投资安排。", (("compute", "opportunity", "high", "mentioned"), ("efficiency", "opportunity", "medium", "inferred"))),
)


@lru_cache(maxsize=1)
def synthetic_source():
    spec = importlib.util.spec_from_file_location("_earth_synthetic_source", SYNTHETIC_SOURCE)
    if spec is None or spec.loader is None:
        raise RuntimeError("Local offline fixture is unavailable.")
    source = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(source)
    return source


def build_payload() -> dict:
    payload = synthetic_source().build_payload()
    sector_labels = {item["id"]: item["label"] for item in payload["sectors"]}
    transmission_questions = {
        "grid": "待核验输配电接入或设备更新需求、项目审批、采购清单与供应资格。",
        "storage": "待核验储能配置规模、材料成本、采购安排和成本转嫁能力。",
        "compute": "待核验算力建设预算、能源约束、项目进度及实际设备采购。",
        "efficiency": "待核验能效改造范围、节能设备投入和实际采购主体。",
    }
    news, links = [], []
    for news_id, place_key, time, category, title, short_title, summary, relations in NEWS_SEEDS:
        geo = None
        if place_key is not None:
            lon, lat, country, label, province, city, country_id = PLACES[place_key]
            geo = {"lon": lon, "lat": lat, "country": country, "label": label,
                   "province": province, "city": city, "country_id": country_id}
        news.append({
            "id": news_id, "title": title, "shortTitle": short_title,
            "time": f"{time}（演示）", "category": category,
            "summary": f"假想新闻：{summary}", "source": "本地虚构情景 · 非真实新闻",
            "synthetic": True, "geo": geo,
            "geo_note": "省市与城市坐标仅为交互演示锚点，不是真实新闻发生地点。" if geo else "无明确地理位置，不显示地图定位点。",
        })
        for sector_id, direction, impact, kind in relations:
            relation = "文本直接涉及" if kind == "mentioned" else "产业传导假设"
            links.append({
                "id": f"nl-{news_id}-{sector_id}", "news_id": news_id,
                "sector_id": sector_id, "kind": kind, "direction": direction,
                "impact_level": impact,
                "evidence_excerpt": summary,
                "reason": f"{title}：{relation}{sector_labels[sector_id]}。{transmission_questions[sector_id]}不表示已发生的事实、实际订单或股票受益。",
            })
    for stock in payload["stocks"]:
        label = sector_labels[stock["sector_id"]]
        stock["business_profile"] = f"模拟业务设定：{label}方向的研究样本；不是现实公司主营业务资料。"
        for seat_id, rule in stock["rule_results"].items():
            fingerprint = json.dumps(rule, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            rule["snapshot_version"] = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()
    for link in payload["stock_links"]:
        stock = next(item for item in payload["stocks"] if item["id"] == link["stock_id"])
        link["reason"] = f"{stock['label']}按人工模拟业务设定映射到{sector_labels[link['sector_id']]}；不代表真实行业归属、客户关系或订单。"
        link["business_profile"] = stock["business_profile"]
        link["verification_needed"] = "尚无现实公司的主营收入占比、供货资格或订单证据，不能据此确认受益。"
    payload.update({
        "news": news, "news_links": links, "geo_mode": "synthetic_locations",
        "research_version": "news-research/v1",
        "map_source": "Natural Earth / world-atlas 2.0.2 · 1:110m 简化地理示意",
        "map_notice": "简化地理示意，非标准地图；边界依底图数据，不表达主权或法律立场。",
        "geo_notice": "新闻及位置关联均为虚构演示；省市与城市坐标仅为交互锚点，不是真实事件位置；未明确的地区不补填。",
    })
    return payload


@lru_cache(maxsize=1)
def news_data_bytes() -> bytes:
    payload = build_payload()
    if (payload.get("preview_only") is not True or payload.get("news_mode") != "synthetic"
            or payload.get("model_called") is not False or payload.get("impact_mode") != "synthetic_ordinal"
            or payload.get("geo_mode") != "synthetic_locations"):
        raise ValueError("Only explicitly marked synthetic data is allowed.")
    content = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    if len(content) > MAX_JSON_BYTES:
        raise ValueError("Preview fixture is too large.")
    return content


def static_name(target: str) -> str | None:
    if not isinstance(target, str) or not target.startswith("/") or target.startswith("//") or len(target) > MAX_REQUEST_TARGET:
        return None
    try:
        parsed = urlsplit(target)
        if parsed.scheme or parsed.netloc or parsed.fragment:
            return None
        name = unquote(parsed.path, encoding="utf-8", errors="strict")
    except (ValueError, UnicodeError):
        return None
    if name == "/":
        return "index.html"
    if name == "/news-data.json":
        return "news-data.json"
    return name[1:] if name[1:] in STATIC_TYPES else None


def create_server(port: int = DEFAULT_PORT, *, directory: Path = PREVIEW_DIR) -> ThreadingHTTPServer:
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("Port must be an integer between 0 and 65535.")
    public_root = Path(directory).resolve(strict=True)
    if not public_root.is_dir():
        raise ValueError("Preview root must be a directory.")
    content_data = news_data_bytes()

    class Handler(BaseHTTPRequestHandler):
        server_version = "LocalEarthPreview"
        sys_version = ""

        def log_message(self, _format, *_args):
            return

        def send_payload(self, status, body, content_type, *, head=False):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self.end_headers()
            if not head:
                self.wfile.write(body)

        def dispatch_read(self, *, head=False):
            bound_port = self.server.server_address[1]
            allowed_hosts = {f"{HOST}:{bound_port}", f"localhost:{bound_port}"}
            if bound_port == 80:
                allowed_hosts.update({HOST, "localhost"})
            hosts = self.headers.get_all("Host", [])
            if len(hosts) != 1 or hosts[0] not in allowed_hosts:
                self.send_payload(400, b"Invalid local host.", "text/plain; charset=utf-8", head=head)
                return
            origin = self.headers.get("Origin")
            if origin is not None and origin not in {f"http://{host}" for host in allowed_hosts}:
                self.send_payload(403, b"Cross-origin access denied.", "text/plain; charset=utf-8", head=head)
                return
            if len(self.path) > MAX_REQUEST_TARGET:
                self.send_payload(414, b"Request target too long.", "text/plain; charset=utf-8", head=head)
                return
            if self.headers.get("Transfer-Encoding") or self.headers.get("Content-Length", "0") != "0":
                self.close_connection = True
                self.send_payload(400, b"Read-only requests cannot have bodies.", "text/plain; charset=utf-8", head=head)
                return
            name = static_name(self.path)
            if name == "news-data.json":
                self.send_payload(200, content_data, "application/json; charset=utf-8", head=head)
                return
            if name is None:
                self.send_payload(404, b"Not found.", "text/plain; charset=utf-8", head=head)
                return
            try:
                # This one fixed shared source is independent of the request path.
                if name == "sphere-core.js":
                    public_file = SHARED_SPHERE_CORE.resolve(strict=True)
                else:
                    public_file = (public_root / name).resolve(strict=True)
                    if not public_file.is_relative_to(public_root):
                        raise FileNotFoundError
                if not public_file.is_file():
                    raise FileNotFoundError
                with public_file.open("rb") as stream:
                    content = stream.read(MAX_STATIC_BYTES + 1)
                if len(content) > MAX_STATIC_BYTES:
                    self.send_payload(413, b"Preview asset too large.", "text/plain; charset=utf-8", head=head)
                    return
            except (OSError, RuntimeError):
                self.send_payload(404, b"Not found.", "text/plain; charset=utf-8", head=head)
                return
            self.send_payload(200, content, STATIC_TYPES[name], head=head)

        def do_GET(self):
            self.dispatch_read()

        def do_HEAD(self):
            self.dispatch_read(head=True)

        def reject_write(self):
            self.close_connection = True
            self.send_response(405)
            self.send_header("Allow", "GET, HEAD")
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()

        do_POST = reject_write
        do_PUT = reject_write
        do_PATCH = reject_write
        do_DELETE = reject_write
        do_OPTIONS = reject_write
        do_TRACE = reject_write
        do_CONNECT = reject_write

    server = ThreadingHTTPServer((HOST, port), Handler)
    server.daemon_threads = True
    return server


def main():
    parser = argparse.ArgumentParser(description="地理新闻三版预览（全为演示数据，只读）")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    with create_server(args.port) as server:
        print(f"地理新闻三版预览：http://{HOST}:{server.server_port}/（演示数据，未调用模型）", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()

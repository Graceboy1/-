"""Loopback-only, read-only preview of a hypothetical news association graph.

All articles and industry/security mappings below are deliberately invented.
Only the three seats' proposal gates come from existing deterministic rules.
Nothing imports the formal app, reads accounts/configuration, calls a model,
downloads news, or uses forward replay returns to qualify a sample.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
from urllib.parse import unquote, urlsplit


PREVIEW_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Registration imports pure synthetic fixtures/offline functions. Do not import
# server.main/access/model_setup or call ModelConfig.from_env/generate_plan.
from server.seats import SEATS  # noqa: E402


HOST = "127.0.0.1"
DEFAULT_PORT = 3006
MAX_JSON_BYTES = 1_000_000
MAX_STATIC_BYTES = 2_000_000
MAX_REQUEST_TARGET = 2048
STATIC_TYPES = {
    "index.html": "text/html; charset=utf-8",
    "news.css": "text/css; charset=utf-8",
    "news-app.js": "text/javascript; charset=utf-8",
    "layout-layers.js": "text/javascript; charset=utf-8",
    "layout-layers.css": "text/css; charset=utf-8",
    "layout-orbit.js": "text/javascript; charset=utf-8",
    "layout-orbit.css": "text/css; charset=utf-8",
    "layout-timeline.js": "text/javascript; charset=utf-8",
    "layout-timeline.css": "text/css; charset=utf-8",
}


NEWS = (
    {"id": "n1", "title": "储能项目招标节奏加快", "shortTitle": "储能招标加快", "time": "09:40（演示）",
     "category": "项目与订单", "summary": "假想新闻：某类储能项目拟加快设备招标，直接提及储能设备；配套电网需求仅用于演示推测关联，不代表真实订单或利好。",
     "source": "本地演示内容", "synthetic": True},
    {"id": "n2", "title": "算力园区扩建计划发布", "shortTitle": "算力园区扩建", "time": "09:48（演示）",
     "category": "产业建设", "summary": "假想新闻：一个虚构算力园区提出扩建计划，直接涉及算力基础设施；用电配套可能形成电网设备关联，但未进行事实核验。",
     "source": "本地演示内容", "synthetic": True},
    {"id": "n3", "title": "电网改造投资规划推进", "shortTitle": "电网改造规划", "time": "10:02（演示）",
     "category": "投资规划", "summary": "假想新闻：一份虚构规划提出推进电网设备改造，储能配套只是人工延伸的示例，不能认定实际投资落地或股票受益。",
     "source": "本地演示内容", "synthetic": True},
    {"id": "n4", "title": "铜材供应成本抬升", "shortTitle": "铜材成本抬升", "time": "10:16（演示）",
     "category": "成本与风险", "summary": "假想新闻：一种虚构铜材采购情景出现成本抬升；向电网与算力设备的成本风险传导均为演示推测，不是已证实的公司影响。",
     "source": "本地演示内容", "synthetic": True},
    {"id": "n5", "title": "节能设备采购需求增加", "shortTitle": "节能设备采购", "time": "10:24（演示）",
     "category": "设备需求", "summary": "假想新闻：一个虚构采购需求清单增加工业节能设备，直接提及该类设备；未引用任何真实采购、企业公告或证券代码。",
     "source": "本地演示内容", "synthetic": True},
    {"id": "n6", "title": "数据中心能效标准调整", "shortTitle": "数据中心能效", "time": "10:38（演示）",
     "category": "标准与约束", "summary": "假想新闻：一种虚构数据中心能效标准调整情景直接涉及算力设施，可能同时增加改造需求与成本；工业节能关联仅为人工演示推测。",
     "source": "本地演示内容", "synthetic": True},
)

SECTORS = (
    {"id": "storage", "label": "储能设备", "synthetic": True},
    {"id": "grid", "label": "电网设备", "synthetic": True},
    {"id": "compute", "label": "算力基础设施", "synthetic": True},
    {"id": "efficiency", "label": "工业节能", "synthetic": True},
)

# Hand-authored ordinal samples for preview brightness only. These are not
# measured impacts, confidence, probabilities, or a mentioned/inferred ranking.
# They must never participate in any seat's proposal/eligibility calculation.
NEWS_LINKS = (
    {"id": "nl-n1-storage", "news_id": "n1", "sector_id": "storage", "kind": "mentioned", "direction": "opportunity", "impact_level": "high",
     "reason": "假想内容直接提及储能设备招标，明确关联仅表示被文中提及，不保证订单或收益。"},
    {"id": "nl-n1-grid", "news_id": "n1", "sector_id": "grid", "kind": "inferred", "direction": "opportunity", "impact_level": "low",
     "reason": "人工演示推测储能项目可能需要电网配套，未核验真实投资或供货关系。"},
    {"id": "nl-n2-compute", "news_id": "n2", "sector_id": "compute", "kind": "mentioned", "direction": "opportunity", "impact_level": "medium",
     "reason": "假想扩建计划直接提及算力园区设施，只建立文字关联，不代表项目已经落地。"},
    {"id": "nl-n2-grid", "news_id": "n2", "sector_id": "grid", "kind": "inferred", "direction": "opportunity", "impact_level": "medium",
     "reason": "人工演示推测算力扩建可能增加电力配套需求，不能据此确认电网设备订单。"},
    {"id": "nl-n3-grid", "news_id": "n3", "sector_id": "grid", "kind": "mentioned", "direction": "opportunity", "impact_level": "high",
     "reason": "假想规划直接提及电网改造，明确指文本涉及该板块，不是证券投资结论。"},
    {"id": "nl-n3-storage", "news_id": "n3", "sector_id": "storage", "kind": "inferred", "direction": "opportunity", "impact_level": "medium",
     "reason": "储能配套为人工演示延伸，假想规划并未提供真实储能采购证据。"},
    {"id": "nl-n4-grid", "news_id": "n4", "sector_id": "grid", "kind": "inferred", "direction": "risk", "impact_level": "high",
     "reason": "人工演示推测铜材成本可能传导至电网设备，未核验合同、锁价或成本转嫁能力。"},
    {"id": "nl-n4-compute", "news_id": "n4", "sector_id": "compute", "kind": "inferred", "direction": "risk", "impact_level": "low",
     "reason": "人工演示推测部分算力配套设备可能受原材料成本影响，不能认定实际毛利变化。"},
    {"id": "nl-n5-efficiency", "news_id": "n5", "sector_id": "efficiency", "kind": "mentioned", "direction": "opportunity", "impact_level": "low",
     "reason": "假想采购清单直接涉及工业节能设备，只说明文本关联，不代表真实需求或收入。"},
    {"id": "nl-n6-compute", "news_id": "n6", "sector_id": "compute", "kind": "mentioned", "direction": "mixed", "impact_level": "medium",
     "reason": "假想能效调整直接涉及算力设施，演示中同时存在改造需求与合规成本两种方向。"},
    {"id": "nl-n6-efficiency", "news_id": "n6", "sector_id": "efficiency", "kind": "inferred", "direction": "opportunity", "impact_level": "high",
     "reason": "人工演示推测能效改造可能带来节能设备需求，尚无真实采购或受益主体证据。"},
)

# Symbol-free names and industry placement are manual illustration only. Case
# identity below maps each seat to its own existing, independently checked data.
STOCK_MAPPING = (
    ("s1", "电网样本甲", "grid", {"livermore": "livermore-normal", "buffett": "buffett-value", "baseline": "normal-fill"}),
    ("s2", "储能样本乙", "storage", {"livermore": "livermore-no-candidate", "buffett": "buffett-expensive", "baseline": "limit-up-no-fill"}),
    ("s3", "储能样本丙", "storage", {"livermore": "livermore-reduced", "buffett": "buffett-cashflow", "baseline": "delayed-exit"}),
    ("s4", "算力样本丁", "compute", {"livermore": "livermore-z1", "buffett": "buffett-gap", "baseline": "normal-fill"}),
    ("s5", "节能样本戊", "efficiency", {"livermore": "livermore-r0", "buffett": "buffett-missing", "baseline": "invalid-data"}),
    ("s6", "电网样本己", "grid", {"livermore": "livermore-r2", "buffett": "buffett-invalid", "baseline": "limit-up-no-fill"}),
)


def offline_rule_result(seat_id: str, case_id: str) -> dict:
    """Only decision-time proposal gates: no future execution or return filter."""
    seat = SEATS[seat_id]
    case = deepcopy(seat.cases[case_id])
    evidence = seat.read_evidence(case)
    plan = seat.propose(evidence)
    validation = seat.validate(plan, evidence)
    checks = validation["checks"]
    return {
        "case_id": case_id, "case_label": case["label"],
        "eligible": validation["ok"] is True and plan.action == "simulate",
        "passed": sum(check["passed"] is True for check in checks),
        "total": len(checks), "action": plan.action, "rationale": plan.rationale,
        "decision_date": evidence["decision_date"], "checks": checks,
    }


def build_payload() -> dict:
    stocks = []
    stock_links = []
    for stock_id, label, sector_id, mapping in STOCK_MAPPING:
        stocks.append({
            "id": stock_id, "label": label, "sector_id": sector_id, "synthetic": True,
            "rule_results": {seat_id: offline_rule_result(seat_id, case_id) for seat_id, case_id in mapping.items()},
        })
        stock_links.append({
            "id": f"sl-{sector_id}-{stock_id}", "sector_id": sector_id, "stock_id": stock_id,
            "kind": "illustrative", "reason": "人工演示映射，不代表真实行业归属",
        })
    return {
        "preview_only": True, "news_mode": "synthetic", "model_called": False,
        "impact_mode": "synthetic_ordinal",
        "news": deepcopy(NEWS), "sectors": deepcopy(SECTORS), "stocks": stocks,
        "seats": [seat.public() for seat in SEATS.values()],
        "news_links": deepcopy(NEWS_LINKS), "stock_links": stock_links,
    }


@lru_cache(maxsize=1)
def news_data_bytes() -> bytes:
    data = json.dumps(build_payload(), ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    if len(data) > MAX_JSON_BYTES:
        raise ValueError("News preview exceeds its fixed byte limit.")
    return data


def static_name(target: str) -> str | None:
    if not isinstance(target, str) or len(target) > MAX_REQUEST_TARGET or not target.startswith("/") or target.startswith("//"):
        return None
    try:
        parsed = urlsplit(target)
        if parsed.scheme or parsed.netloc or parsed.fragment:
            return None
        name = unquote(parsed.path, encoding="utf-8", errors="strict")
    except (UnicodeError, ValueError):
        return None
    if name == "/":
        return "index.html"
    if name == "/news-data.json":
        return "news-data.json"
    return name[1:] if name.startswith("/") and name[1:] in STATIC_TYPES else None


def create_server(port: int = DEFAULT_PORT, *, directory: Path = PREVIEW_DIR) -> ThreadingHTTPServer:
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("Port must be an integer between 0 and 65535.")
    public_root = directory.resolve(strict=True)
    data = news_data_bytes()

    class Handler(BaseHTTPRequestHandler):
        server_version = "LocalNewsPreview"
        sys_version = ""

        def log_message(self, _format, *_args):
            return

        def send_payload(self, status: int, body: bytes, content_type: str, *, head: bool = False):
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

        def dispatch_read(self, *, head: bool = False):
            bound_port = self.server.server_address[1]
            allowed_hosts = {f"{HOST}:{bound_port}", f"localhost:{bound_port}"}
            if bound_port == 80:
                allowed_hosts.update({HOST, "localhost"})
            if self.headers.get("Host", "") not in allowed_hosts:
                self.send_payload(400, b"Invalid local host.", "text/plain; charset=utf-8", head=head)
                return
            if len(self.path) > MAX_REQUEST_TARGET:
                self.send_payload(414, b"Request target too long.", "text/plain; charset=utf-8", head=head)
                return
            if self.headers.get("Transfer-Encoding") or self.headers.get("Content-Length", "0") != "0":
                self.close_connection = True
                self.send_payload(400, b"Read-only requests must not contain a body.", "text/plain; charset=utf-8", head=head)
                return
            name = static_name(self.path)
            if name == "news-data.json":
                self.send_payload(200, data, "application/json; charset=utf-8", head=head)
                return
            if name is None:
                self.send_payload(404, b"Not found.", "text/plain; charset=utf-8", head=head)
                return
            try:
                public_file = (public_root / name).resolve(strict=True)
                if public_file.parent != public_root or not public_file.is_file():
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

    server = ThreadingHTTPServer((HOST, port), Handler)
    server.daemon_threads = True
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="新闻关联三版独立预览（假想内容，仅本地规则）")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    arguments = parser.parse_args()
    if not 0 <= arguments.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    with create_server(arguments.port) as server:
        print(f"新闻关联独立预览：http://{HOST}:{server.server_port}/（假想新闻，未调用模型）", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()

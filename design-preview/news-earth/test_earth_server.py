"""Offline fixture and local ephemeral-port tests; never restart formal apps."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import http.client
import importlib.util
import json
from pathlib import Path
import tempfile
from threading import Thread
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("tested_earth_preview", Path(__file__).with_name("serve_earth.py"))
assert SPEC and SPEC.loader
preview = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(preview)


class FixtureTests(unittest.TestCase):
    def test_synthetic_boundary_and_no_future_data(self):
        data = preview.build_payload()
        self.assertIs(data["preview_only"], True)
        self.assertIs(data["model_called"], False)
        self.assertEqual(data["news_mode"], "synthetic")
        self.assertEqual(data["geo_mode"], "synthetic_locations")
        self.assertEqual(data["impact_mode"], "synthetic_ordinal")
        self.assertTrue(all(item["synthetic"] is True for kind in ("news", "stocks", "sectors") for item in data[kind]))
        self.assertNotIn("future_bars", json.dumps(data))
        self.assertNotIn("replay_result", json.dumps(data))

    def test_fifteen_articles_have_explicit_location_or_null(self):
        data = preview.build_payload()
        self.assertEqual(len(data["news"]), 15)
        self.assertEqual({item["id"] for item in data["news"]}, {f"g{i}" for i in range(1, 16)})
        located = [item for item in data["news"] if item["geo"]]
        self.assertEqual(len(located), 14)
        self.assertIs(next(item for item in data["news"] if item["id"] == "g13")["geo"], None)
        self.assertEqual(data["news"][0]["geo"], data["news"][1]["geo"])
        for item in located:
            geo = item["geo"]
            self.assertEqual(set(geo), {"lon", "lat", "country", "label", "province", "city", "country_id"})
            self.assertTrue(-180 <= geo["lon"] <= 180)
            self.assertTrue(-90 <= geo["lat"] <= 90)
            self.assertRegex(geo["country_id"], r"^\d{3}$")
            self.assertTrue(geo["city"])
            self.assertIn("不是真实", item["geo_note"])
        self.assertGreater(len({item["geo"]["country_id"] for item in located}), 8)

    def test_chinese_city_anchors_include_province_and_municipality(self):
        articles = {item["id"]: item for item in preview.build_payload()["news"]}
        expected = {
            "g1": ("北京市", "北京市", 116.4074, 39.9042),
            "g2": ("北京市", "北京市", 116.4074, 39.9042),
            "g14": ("广东省", "深圳市", 114.0579, 22.5431),
            "g15": ("浙江省", "杭州市", 120.1551, 30.2741),
        }
        for news_id, (province, city, lon, lat) in expected.items():
            geo = articles[news_id]["geo"]
            self.assertEqual((geo["province"], geo["city"]), (province, city))
            self.assertEqual((geo["lon"], geo["lat"]), (lon, lat))
            self.assertEqual((geo["country"], geo["country_id"]), ("中国", "156"))
        self.assertNotEqual(articles["g14"]["geo"], articles["g1"]["geo"])
        self.assertNotEqual(articles["g15"]["geo"], articles["g1"]["geo"])

    def test_missing_region_and_unknown_event_location_are_not_invented(self):
        data = preview.build_payload()
        for item in data["news"]:
            geo = item["geo"]
            if geo and geo["country_id"] != "156":
                self.assertIsNone(geo["province"])
                self.assertEqual(geo["city"], geo["label"])
        unknown = next(item for item in data["news"] if item["id"] == "g13")
        self.assertIsNone(unknown["geo"])
        self.assertIn("无明确地理位置", unknown["geo_note"])
        self.assertIn("不是真实事件位置", data["geo_notice"])
        self.assertNotIn("首都坐标", data["geo_notice"])

    def test_links_are_explicit_and_resolve_to_known_records(self):
        data = preview.build_payload()
        news_ids = {item["id"] for item in data["news"]}
        sector_ids = {item["id"] for item in data["sectors"]}
        stock_ids = {item["id"] for item in data["stocks"]}
        self.assertEqual({link["news_id"] for link in data["news_links"]}, news_ids)
        self.assertGreater(sum(link["news_id"] == "g3" for link in data["news_links"]), 1)
        for link in data["news_links"]:
            self.assertIn(link["news_id"], news_ids)
            self.assertIn(link["sector_id"], sector_ids)
            self.assertIn(link["impact_level"], {"low", "medium", "high"})
            self.assertIn(link["direction"], {"opportunity", "risk", "mixed", "unknown"})
        for link in data["stock_links"]:
            self.assertIn(link["sector_id"], sector_ids)
            self.assertIn(link["stock_id"], stock_ids)

    def test_offline_seat_gates_preserve_independence(self):
        data = preview.build_payload()
        self.assertEqual({seat["id"] for seat in data["seats"]}, {"livermore", "buffett", "baseline"})
        for seat, expected in {"livermore": 5, "buffett": 2, "baseline": 5}.items():
            self.assertEqual(sum(item["rule_results"][seat]["eligible"] is True for item in data["stocks"]), expected)

    def test_records_are_fresh_and_serialization_bounded(self):
        original = preview.build_payload()
        expected = deepcopy(original)
        original["news"][0]["geo"]["lat"] = 0
        original["stocks"][0]["rule_results"]["livermore"]["eligible"] = False
        self.assertEqual(preview.build_payload(), expected)
        preview.news_data_bytes.cache_clear()
        first = preview.news_data_bytes()
        self.assertIs(preview.news_data_bytes(), first)
        self.assertLess(len(first), preview.MAX_JSON_BYTES)

    def test_unmarked_payload_is_rejected(self):
        data = preview.build_payload()
        for field, value in (("preview_only", False), ("news_mode", "live"), ("model_called", True), ("geo_mode", "live"), ("impact_mode", "measured")):
            preview.news_data_bytes.cache_clear()
            with patch.object(preview, "build_payload", return_value={**data, field: value}), self.assertRaises(ValueError):
                preview.news_data_bytes()
        preview.news_data_bytes.cache_clear()

    def test_pinned_map_geometry_is_local_and_contains_anchor_countries(self):
        raw = (preview.PREVIEW_DIR / "vendor/countries-110m.json").read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), "2516c915867c7baf18ddec727aec46c315541a07cfb3d79a6559b05d5e94eee8")
        world = json.loads(raw)
        self.assertEqual(world["type"], "Topology")
        self.assertGreater(len(world["arcs"]), 100)
        country_ids = {str(item.get("id")) for item in world["objects"]["countries"]["geometries"]}
        for geo in preview.PLACES.values():
            self.assertIn(geo[-1], country_ids)

    def test_pinned_script_bytes_and_license_files(self):
        hashes = {
            "d3-array.min.js": "80aa70d0cd17dabddf6d056494ea17926a45a69da8b7850220aace331bad671d",
            "d3-geo.min.js": "23e574f1e8d4716d622bce45356a379b66a277162b145f5f147e4c8daefb73d2",
            "topojson-client.min.js": "25cd02ae486cc5063e0215a4e4cfb15de83700c87ac48bac4d57dc6aaf3ebb89",
        }
        for name, expected in hashes.items():
            raw = (preview.PREVIEW_DIR / "vendor" / name).read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(), expected)
        for name in ("d3-array", "d3-geo", "topojson-client", "world-atlas"):
            self.assertGreater((preview.PREVIEW_DIR / "vendor" / f"{name}.LICENSE").stat().st_size, 500)

    def test_host_port_and_request_target_boundaries(self):
        self.assertEqual(preview.HOST, "127.0.0.1")
        self.assertEqual(preview.DEFAULT_PORT, 3008)
        for value in (-1, 65536, True, "3008", None):
            with self.assertRaises(ValueError):
                preview.create_server(value)
        for target in ("", None, 42, "//evil.example/", "http://evil.example/", "/../.env", "/%2e%2e/.env", "/serve_earth.py", "/test_earth_server.py", "/DATA-SOURCES.md", "/index.html#x", "/%FF", "/vendor/../serve_earth.py", "/vendor/countries-110m.json/", "/.workspace-account.sqlite3"):
            self.assertIsNone(preview.static_name(target), repr(target))
        self.assertEqual(preview.static_name("/?view=atlas"), "index.html")
        self.assertEqual(preview.static_name("/vendor/countries-110m.json?v=2"), "vendor/countries-110m.json")


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="earth-preview-test-")
        cls.directory = Path(cls.temp.name)
        for name in preview.STATIC_TYPES:
            file = cls.directory / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(f"fixture:{name}", encoding="utf-8")
        cls.server = preview.create_server(0, directory=cls.directory)
        cls.port = cls.server.server_port
        cls.thread = Thread(target=cls.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        cls.temp.cleanup()

    def request(self, method="GET", target="/", headers=None, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            connection.request(method, target, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_exact_static_files_with_cache_and_type_headers(self):
        for name, content_type in preview.STATIC_TYPES.items():
            status, headers, body = self.request(target=f"/{name}?view=atlas")
            self.assertEqual(status, 200)
            self.assertEqual(headers["Cache-Control"], "no-store")
            self.assertEqual(headers["Content-Type"], content_type)
            self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
            expected = preview.SHARED_SPHERE_CORE.read_bytes() if name == "sphere-core.js" else f"fixture:{name}".encode()
            self.assertEqual(body, expected)

    def test_json_is_synthetic_and_head_has_no_body(self):
        status, headers, body = self.request(target="/news-data.json")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["geo_mode"], "synthetic_locations")
        status2, headers2, body2 = self.request("HEAD", "/news-data.json")
        self.assertEqual(status2, 200)
        self.assertEqual(headers2["Content-Length"], headers["Content-Length"])
        self.assertEqual(body2, b"")

    def test_unknown_and_source_paths_are_not_exposed(self):
        for target in ("/.env", "/serve_earth.py", "/test_earth_server.py", "/DATA-SOURCES.md", "/../news-spheres/sphere-core.js", "/vendor/../../.env"):
            self.assertEqual(self.request(target=target)[0], 404)

    def test_bad_host_and_cross_origin_are_rejected(self):
        self.assertEqual(self.request(headers={"Host": "evil.example"})[0], 400)
        self.assertEqual(self.request(headers={"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.request(headers={"Origin": "null"})[0], 403)
        self.assertEqual(self.request(headers={"Host": f"localhost:{self.port}"})[0], 200)
        self.assertEqual(self.request(headers={"Origin": f"http://127.0.0.1:{self.port}"})[0], 200)

    def test_read_requests_cannot_carry_body(self):
        self.assertEqual(self.request(body=b"x")[0], 400)
        self.assertEqual(self.request(headers={"Transfer-Encoding": "chunked"})[0], 400)

    def test_write_methods_are_rejected(self):
        for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT"):
            status, headers, body = self.request(method)
            self.assertEqual(status, 405)
            self.assertEqual(headers["Allow"], "GET, HEAD")
            self.assertEqual(body, b"")

    def test_byte_and_target_caps(self):
        self.assertEqual(self.request(target="/" + "x" * preview.MAX_REQUEST_TARGET)[0], 414)
        with patch.object(preview, "MAX_STATIC_BYTES", 4):
            self.assertEqual(self.request(target="/earth.css")[0], 413)
        self.assertEqual(self.request(target="/earth.css")[0], 200)


if __name__ == "__main__":
    unittest.main()

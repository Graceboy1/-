"""Dated public-pool references are never exact limit or execution evidence.

All source-looking records below are generated test fixtures. No private archive,
network, production index, or account database is accessed.
"""
from copy import deepcopy
import gzip
import hashlib
import json
from pathlib import Path

import pytest

from server import livermore_reference as lr, market_history


DAY, OTHER, CODE = "2026-11-12", "2026-11-17", "000001"


@pytest.fixture(autouse=True)
def clear_reader_cache():
    lr._read.cache_clear()
    yield
    lr._read.cache_clear()


def payload():
    return {"schema": lr.SCHEMA, "requested_date": DAY, "server_qdate_raw": OTHER,
            "trusted": False, "verification_status": "source_reference_only",
            "source_url": lr.SOURCE_URL, "source_response_hash": "a" * 64,
            "total_source_rows": 1, "rows": [
                {"code": CODE, "name": "测试来源股票", "date": DAY,
                 "reported_price": 11.0, "archive_close": 11.0,
                 "price_matches_archive": True, "source_reported_limit_up": True,
                 "verification_status": "source_reference_only", "source_url": lr.SOURCE_URL,
                 "source_hash": "a" * 64}]}


def raw_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def save(value=None, *, raw=None, immutable=False, path=None):
    value = payload() if value is None else value
    root = market_history.directory() / "livermore"
    root.mkdir(parents=True, exist_ok=True)
    raw = raw_bytes(value) if raw is None else raw
    digest = hashlib.sha256(raw).hexdigest()
    filename = path or (f"reference-{DAY}-{digest[:12]}.json.gz" if immutable else f"reference-{DAY}.json.gz")
    (root / filename).write_bytes(gzip.compress(raw))
    index = {"schema": lr.INDEX_SCHEMA, "files": {DAY: {"path": filename, "sha256": digest}}}
    (root / "references-index.json").write_text(json.dumps(index), encoding="utf-8")
    return root, index


def write_index(root, value):
    (root / "references-index.json").write_text(json.dumps(value), encoding="utf-8")


def test_missing_reference_is_read_only_and_never_creates_an_archive():
    root = market_history.directory() / "livermore"
    assert not root.exists()
    found = lr.load_reference(CODE, DAY, 11.0)
    assert found["status"] == "missing" and found["source_reported_limit_up"] is None
    assert not root.exists()


@pytest.mark.parametrize("immutable", [False, True])
def test_requested_event_day_and_matching_archive_close_accept_different_raw_server_qdate(immutable):
    save(immutable=immutable)
    found = lr.load_reference(CODE, DAY, 11.0)
    assert found["status"] == "reference_match"
    assert found["date"] == DAY and found["server_qdate_raw"] == OTHER
    assert found["source_reported_limit_up"] is True
    assert found["verification_status"] == "source_reference_only"
    assert "limit_up" not in found and "auction" not in found and "trusted" not in found


def test_absent_stock_is_unknown_not_a_verified_negative_or_a_neighboring_day_fact():
    save()
    found = lr.load_reference("600519", DAY, 11.0)
    assert found["status"] == "not_reported"
    assert found["source_reported_limit_up"] is None and found["reported_price"] is None
    assert found["source_hash"] == "a" * 64
    assert lr.load_reference(CODE, OTHER, 11.0)["status"] == "missing"


@pytest.mark.parametrize("value", [None, 11.0, 11.004])
def test_optional_archive_close_uses_explicit_bounded_price_cross_check(value):
    save()
    assert lr.load_reference(CODE, DAY, value)["status"] == "reference_match"


@pytest.mark.parametrize("value", [0, -1, True, "11", float("nan"), float("inf"), 11.01, 1_000_001])
def test_incompatible_current_or_wrong_close_cannot_validate_historical_reference(value):
    save()
    assert lr.load_reference(CODE, DAY, value)["status"] == "invalid"


@pytest.mark.parametrize("code,day", [("300001", DAY), ("688001", DAY), ("920001", DAY),
                                      ("../000001", DAY), (1, DAY), (CODE, "2026-02-30"),
                                      (CODE, "2026-11-12T15:00:00"), (CODE, None)])
def test_invalid_query_stays_unknown_without_loading_reference(code, day):
    save()
    found = lr.load_reference(code, day, 11.0)
    assert found["status"] == "invalid" and found["source_reported_limit_up"] is None


@pytest.mark.parametrize("field,value", [("requested_date", OTHER), ("schema", "wrong"), ("trusted", True),
                                         ("verification_status", "source_verified"),
                                         ("source_url", "https://example.com/pool"),
                                         ("source_response_hash", "bad"), ("total_source_rows", True),
                                         ("total_source_rows", 1001), ("total_source_rows", 0), ("rows", {})])
def test_invalid_source_metadata_rejects_the_whole_reference(field, value):
    data = payload(); data[field] = value; save(data)
    assert lr.load_reference(CODE, DAY, 11.0)["status"] == "invalid"


@pytest.mark.parametrize("field,value", [("date", OTHER), ("source_url", "https://example.com/pool"),
                                         ("source_hash", "b" * 64), ("verification_status", "source_verified"),
                                         ("reported_price", 11.01), ("archive_close", 10.0),
                                         ("reported_price", True), ("reported_price", float("nan")),
                                         ("price_matches_archive", False), ("source_reported_limit_up", False),
                                         ("name", "*ST测试"), ("name", "退市测试"), ("name", ""),
                                         ("name", "名" * 101), ("code", "300001"), ("code", 1)])
def test_wrong_date_price_identity_status_or_source_row_rejects_reference(field, value):
    data = payload(); data["rows"][0][field] = value; save(data)
    found = lr.load_reference(CODE, DAY, 11.0)
    assert found["status"] == "invalid" and found["source_reported_limit_up"] is None


def test_duplicate_code_cannot_replace_an_earlier_row():
    data = payload(); data["rows"].append(deepcopy(data["rows"][0])); data["total_source_rows"] = 2
    save(data)
    assert lr.load_reference(CODE, DAY, 11.0)["status"] == "invalid"


def test_empty_pool_is_valid_but_all_stock_limit_states_remain_unknown():
    data = payload(); data.update(rows=[], total_source_rows=0); save(data)
    found = lr.load_reference(CODE, DAY, 11.0)
    assert found["status"] == "not_reported" and found["source_reported_limit_up"] is None


def test_maximum_source_row_count_and_index_file_count_are_inclusive():
    data = payload(); row = data["rows"][0]
    data["rows"] += [{**row, "code": f"600{i:03}"} for i in range(999)]
    data["total_source_rows"] = 1000
    root, index = save(data)
    index["files"].update({str(i): {} for i in range(59)})
    write_index(root, index)
    assert lr.load_reference(CODE, DAY, 11.0)["status"] == "reference_match"


@pytest.mark.parametrize("path", ["../reference-2026-11-12.json.gz", "other.json.gz",
                                  "reference-2026-11-17.json.gz", "reference-2026-11-12-000000000000.json.gz",
                                  "C:/outside.json.gz"])
def test_only_exact_dated_or_content_hash_filename_is_allowed(path):
    root, index = save(); index["files"][DAY]["path"] = path; write_index(root, index)
    assert lr.load_reference(CODE, DAY, 11.0)["status"] == "invalid"


def test_archive_checksum_cannot_be_changed_via_index_without_matching_content():
    root, index = save(); index["files"][DAY]["sha256"] = "b" * 64; write_index(root, index)
    assert lr.load_reference(CODE, DAY, 11.0)["status"] == "invalid"


def test_same_path_replacement_is_not_reused_from_cached_reference():
    root, _ = save()
    assert lr.load_reference(CODE, DAY, 11.0)["status"] == "reference_match"
    data = payload(); data["rows"][0].update(reported_price=12.0, archive_close=12.0)
    save(data)
    assert lr.load_reference(CODE, DAY, 11.0)["status"] == "invalid"
    assert lr.load_reference(CODE, DAY, 12.0)["status"] == "reference_match"
    assert lr._read.cache_info().currsize <= 2


def test_reference_file_symlink_cannot_escape_archive(tmp_path):
    root, index = save()
    file = root / index["files"][DAY]["path"]
    external = tmp_path / "outside-reference.gz"
    external.write_bytes(file.read_bytes()); file.unlink()
    try:
        file.symlink_to(external)
    except OSError:
        pytest.skip("This Windows account cannot create symlinks")
    assert lr.load_reference(CODE, DAY, 11.0)["status"] == "invalid"


def test_resolved_path_escape_is_rejected_without_requiring_windows_symlink_privilege(tmp_path, monkeypatch):
    root, index = save()
    filename = index["files"][DAY]["path"]
    original_resolve = Path.resolve

    def resolved_path(path, *args, **kwargs):
        if path == root / filename:
            return tmp_path / "outside-reference.gz"
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolved_path)
    assert lr.load_reference(CODE, DAY, 11.0)["status"] == "invalid"


@pytest.mark.parametrize("raw", [b"[]", b"null", b"{", b"not JSON"])
def test_bad_json_payload_is_rejected(raw):
    save(raw=raw)
    assert lr.load_reference(CODE, DAY, 11.0)["status"] == "invalid"


def test_index_bound_and_file_count_are_enforced():
    root, index = save()
    index["padding"] = "x" * 50_000; write_index(root, index)
    assert lr.load_reference(CODE, DAY, 11.0)["status"] == "invalid"
    root, index = save(); index["files"].update({str(i): {} for i in range(60)}); write_index(root, index)
    assert lr.load_reference(CODE, DAY, 11.0)["status"] == "invalid"


def test_decompressed_reference_maximum_is_inclusive_and_bounded():
    original = raw_bytes(payload())
    exact = original + b" " * (lr.MAX_BYTES - len(original))
    save(raw=exact)
    assert lr.load_reference(CODE, DAY, 11.0)["status"] == "reference_match"
    save(raw=exact + b" ")
    assert lr.load_reference(CODE, DAY, 11.0)["status"] == "invalid"


def test_compressed_file_size_is_bounded_before_gzip_loading():
    root, index = save()
    (root / index["files"][DAY]["path"]).write_bytes(b"x" * (lr.MAX_BYTES + 1))
    assert lr.load_reference(CODE, DAY, 11.0)["status"] == "invalid"


@pytest.mark.parametrize("cut", [1, 8, 30])
def test_truncated_gzip_is_reported_invalid_not_an_uncaught_loader_exception(cut):
    root, index = save()
    file = root / index["files"][DAY]["path"]
    file.write_bytes(file.read_bytes()[:-cut])
    assert lr.load_reference(CODE, DAY, 11.0)["status"] == "invalid"


def test_corrupt_deflate_is_reported_invalid_not_an_uncaught_loader_exception():
    root, index = save()
    file = root / index["files"][DAY]["path"]
    damaged = bytearray(file.read_bytes())
    damaged[10] = (damaged[10] & 0xF8) | 7  # Reserved DEFLATE block type.
    file.write_bytes(damaged)
    assert lr.load_reference(CODE, DAY, 11.0)["status"] == "invalid"

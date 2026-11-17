"""Disposable synthetic *test fixtures* only; no genuine verified facts imported."""
from __future__ import annotations

import json

import pytest

from server import livermore_data, market_history
from tools import import_livermore_evidence as tool

CODE = "600000"


def fixture_profile():
    # Fake provenance is confined to tmp_path fixtures, never a CLI template.
    common = {"source_url": "https://www.szse.cn/test-fixture-only", "source_hash": "a" * 64, "verification_status": "source_verified"}
    return {"schema": livermore_data.SCHEMA, "code": CODE, "records": {
        "limits": [{**common, "date": "2026-11-12", "asof": "2026-11-12T15:00:00+08:00", "limit_up": 11., "limit_down": 9., "security_name": "单元测试样本", "is_st": False, "suspended": False}],
        "auctions": [{**common, "date": "2026-11-13", "asof": "2026-11-13T09:25:00+08:00", "phase": "opening_uncrossed", "open": 11.33, "auction_amount": 113300., "amount_unit": "yuan", "auction_volume_shares": 10000}],
        "index_auctions": [{**common, "date": "2026-11-13", "asof": "2026-11-13T09:25:00+08:00", "phase": "opening_uncrossed", "symbol": "000001.SH", "change_pct": 0.2}],
        "regions": [{**common, "verification_status": "human_confirmed", "date": "2026-11-13", "available_at": "2026-11-13T09:20:00+08:00", "regional_ok": True, "note": "仅供单元测试的人工说明", "reviewed_by": "fixture reviewer"}],
    }}


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    directory = tmp_path / "archive"
    directory.mkdir()
    monkeypatch.setenv("CONTEST_MARKET_DATA_DIR", str(directory))
    monkeypatch.setattr(market_history, "load_case", lambda case_id: {"symbol": CODE, "name": "单元测试样本"})
    path = tmp_path / "fixture.json"
    path.write_text(json.dumps(fixture_profile(), ensure_ascii=False), encoding="utf-8")
    return directory, path


def test_no_file_read_without_required_explicit_arguments():
    with pytest.raises(SystemExit):
        tool.main([])


@pytest.mark.parametrize("filename", ["https://example.invalid/evidence.json", "http://localhost/evidence.json", "\\\\server\\share\\evidence.json", "//server/share/evidence.json"])
def test_remote_input_refused(filename):
    with pytest.raises(tool.ImportValidationError):
        tool.read_input(filename)


def test_missing_file_refused(tmp_path):
    with pytest.raises(tool.ImportValidationError):
        tool.read_input(str(tmp_path / "missing.json"))


def test_configuration_file_not_read(tmp_path):
    path = tmp_path / ".env"
    path.write_text("should-not-be-read", encoding="utf-8")
    with pytest.raises(tool.ImportValidationError):
        tool.read_input(str(path))


def test_oversized_file_refused(tmp_path):
    path = tmp_path / "oversized.json"
    path.write_bytes(b" " * (livermore_data.MAX_PROFILE_BYTES + 1))
    with pytest.raises(tool.ImportValidationError):
        tool.read_input(str(path))


@pytest.mark.parametrize("contents", ["not json", "[]", "null", '"not an object"'])
def test_bad_json_refused(tmp_path, contents):
    path = tmp_path / "bad.json"
    path.write_text(contents, encoding="utf-8")
    with pytest.raises(tool.ImportValidationError):
        tool.read_input(str(path))


@pytest.mark.parametrize("code", ["300001", "688001", "920001", "600000/..", "60000", "abcdef"])
def test_nonmainboard_and_invalid_code_refused(isolated, code):
    _, path = isolated
    with pytest.raises(tool.ImportValidationError):
        tool.main(["--input", str(path), "--code", code, "--dry-run"])


@pytest.mark.parametrize("name", ["*ST测试", "ST测试", "退市样本", " "])
def test_excluded_archive_name_refused(isolated, monkeypatch, name):
    _, path = isolated
    monkeypatch.setattr(market_history, "load_case", lambda case_id: {"symbol": CODE, "name": name})
    with pytest.raises(tool.ImportValidationError):
        tool.main(["--input", str(path), "--code", CODE, "--dry-run"])


def test_archive_missing_or_checksum_failure_refused(isolated, monkeypatch):
    _, path = isolated
    monkeypatch.setattr(market_history, "load_case", lambda _: (_ for _ in ()).throw(ValueError("archive checksum mismatch")))
    with pytest.raises(tool.ImportValidationError):
        tool.main(["--input", str(path), "--code", CODE, "--dry-run"])


@pytest.mark.parametrize("change", [
    lambda p: p.update(schema="wrong-schema"),
    lambda p: p.update(code="600001"),
    lambda p: p.update(api_key="must-not-be-saved"),
    lambda p: p["records"].update(unrecognized=[]),
    lambda p: p["records"]["limits"][0].update(verification_status="source_reference_only"),
    lambda p: p["records"]["limits"][0].update(limit_up=11.001),
    lambda p: p["records"]["limits"][0].update(is_st="false"),
    lambda p: p["records"]["limits"][0].update(code="600001"),
    lambda p: p["records"]["auctions"][0].update(asof="2026-11-13T10:00:00+08:00"),
    lambda p: p["records"]["auctions"][0].update(amount_unit="shares"),
    lambda p: p["records"]["auctions"][0].update(auction_amount=100),
    lambda p: p["records"]["index_auctions"][0].update(symbol="unknown"),
    lambda p: p["records"]["regions"][0].update(available_at="2026-11-13T10:00:00+08:00"),
    lambda p: p["records"]["regions"][0].update(regional_ok=1),
    lambda p: p["records"]["limits"][0].update(source_url="https://www.szse.cn/data?api_key=not-for-output"),
])
def test_original_field_validation_reused(change):
    profile = fixture_profile()
    change(profile)
    with pytest.raises(tool.ImportValidationError):
        tool.validate_import(CODE, profile)


def test_duplicate_dates_refused():
    profile = fixture_profile()
    profile["records"]["limits"] *= 2
    with pytest.raises(tool.ImportValidationError):
        tool.validate_import(CODE, profile)


def test_bounded_groups_refused():
    profile = fixture_profile()
    profile["records"]["limits"] *= livermore_data.MAX_RECORDS + 1
    with pytest.raises(tool.ImportValidationError):
        tool.validate_import(CODE, profile)


def test_empty_template_not_importable():
    with pytest.raises(tool.ImportValidationError):
        tool.validate_import(CODE, {"schema": livermore_data.SCHEMA, "code": CODE, "records": {}})


def test_valid_dry_run_creates_no_evidence(isolated, monkeypatch, capsys):
    directory, path = isolated
    monkeypatch.setattr(livermore_data, "save_profile", lambda *args: (_ for _ in ()).throw(AssertionError("must not save")))
    assert tool.main(["--input", str(path), "--code", CODE, "--dry-run"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "validated_not_saved" and result["dry_run"]
    assert result["record_counts"] == {group: 1 for group in livermore_data.GROUPS}
    assert list(directory.iterdir()) == []


def test_formal_save_only_in_isolated_fixture(isolated, capsys):
    directory, path = isolated
    assert tool.main(["--input", str(path), "--code", CODE]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "saved" and not result["dry_run"]
    assert (directory / "livermore" / "index.json").is_file()
    assert livermore_data.load_decision(CODE, "2026-11-12")["status"] == "verified"
    assert livermore_data.load_execution(CODE, "2026-11-13")["status"] == "verified"
    assert sorted(p.name for p in directory.iterdir()) == ["livermore"]


def test_valid_partial_evidence_keeps_missing_groups():
    profile = fixture_profile()
    profile["records"] = {"limits": profile["records"]["limits"]}
    safe = tool.validate_import(CODE, profile)
    assert safe["records"]["limits"] and safe["records"]["auctions"] == []
    assert profile["records"] != safe["records"]  # caller input not mutated

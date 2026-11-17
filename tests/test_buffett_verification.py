"""Generated text/PDF identities only; never production PDFs or network."""
import copy
import gzip
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from server import historical_fundamentals as finance
from tools import verify_buffett_originals as verify

CODE = "000333"


def item(year=2025):
    return {"code": CODE, "year": year, "period_end": f"{year}-12-31",
            "publication_date": f"{year+1}-03-20", "announcement_id": "1234567890",
            "file": f"{CODE}/1234567890.pdf", "sha256": "a"*64,
            "source_url": f"https://static.cninfo.com.cn/finalpage/{year+1}-03-20/1234567890.PDF",
            "status": "downloaded", "revision_or_correction": False, "size": 9}


def reference():
    return {"operating_cashflow": 20, "reported_capex": 3, "net_profit": 15,
            "roe_pct": 18, "total_liabilities": 4, "total_equity": 20, "debt_to_equity": .2}


def pages():
    return [(2, "2025年年度报告 000333\n主要会计数据\n2025年 2024年\n"
             "归属于上市公司股东的净利润（元） 15,000,000 13,000,000\n"
             "经营活动产生的现金流量净额（元） 20,000,000 19,000,000\n"
             "加权平均净资产收益率 18% 17%"),
            (50, "合并资产负债表\n单位：元 币种：人民币\n2025年12月31日 2024年12月31日\n"
             "流动负债合计 2,000,000 1,000,000\n非流动负债合计 2,000,000 2,000,000\n"
             "负债合计 4,000,000 3,000,000\n归属于母公司所有者权益合计 19,000,000 18,000,000\n"
             "所有者权益合计 20,000,000 19,000,000"),
            (54, "合并现金流量表\n单位：元 币种：人民币\n2025年度 2024年度\n"
             "经营活动产生的现金流量净额 20,000,000 19,000,000\n"
             "购建固定资产、无形资产和其他长期资产支付的现金 3,000,000 2,000,000"),
            (70, "股份变动情况\n单位：股\n本次变动前 本次变动增减 本次变动后\n"
             "三、股份总数 1,500,000 100.00 500,000 2,000,000 100.00")]


def extract(values=None, refs=None):
    return verify.extract_fields(values if values is not None else pages(), refs if refs is not None else reference(), item())


def test_current_year_values_are_cross_checked_and_shares_are_actual_quantities():
    result = extract()
    for key,value in reference().items():
        assert result[key] == pytest.approx(value)
    assert result["shares_outstanding"] == 2
    assert result["maintenance_capex"] is None
    for field,proof in result["field_evidence"].items():
        assert 1 <= proof["page"] <= verify.MAX_PAGES
        assert proof["document_sha256"] == "a"*64
        assert proof["verification_status"] == "matched_original"
        assert len(proof["quote"]) <= 500
        assert proof["value"] == pytest.approx(result[field])
        assert proof["reported_value"] * proof["scale"] == pytest.approx(proof["value"])


@pytest.mark.parametrize("unit,multiplier,printed", [("元",1e-6,"15,000,000"),("千元",.001,"15,000"),("万元",.01,"1,500"),("百万元",1,"15"),("亿元",100,"0.15")])
def test_explicit_currency_units_are_scaled_to_million_yuan(unit,multiplier,printed):
    result = extract([(2,f"2025年度\n归属于上市公司股东的净利润（{unit}） {printed} 1")])
    assert result["net_profit"] == 15
    assert result["field_evidence"]["net_profit"]["scale"] == multiplier


def test_previous_year_comparison_column_is_not_selected_to_match_reference():
    result=extract([(2,"2025年年度报告\n归属于上市公司股东的净利润（元） 99,000,000 15,000,000")])
    assert result["net_profit"] is None


def test_unlabelled_money_units_and_unlabelled_roe_are_not_guessed():
    result=extract([(2,"2025年度\n归属于上市公司股东的净利润 15,000,000\n加权平均净资产收益率 18 17")])
    assert result["net_profit"] is None and result["roe_pct"] is None


def test_ambiguous_page_currency_units_are_not_chosen_opportunistically():
    result=extract([(50,"合并资产负债表 2025年度 单位：元 单位：万元\n负债合计 4,000,000")])
    assert result["total_liabilities"] is None


def test_parent_company_tables_cannot_replace_consolidated_values():
    data=[(50,pages()[1][1].replace("合并资产负债表","母公司资产负债表")),
          (54,pages()[2][1].replace("合并现金流量表","母公司现金流量表"))]
    result=extract(data)
    for field in ("total_liabilities","total_equity","debt_to_equity","operating_cashflow","reported_capex"):
        assert result[field] is None


def test_explicit_consolidated_and_company_side_by_side_uses_first_consolidated_column():
    values=[(50,"合并及公司资产负债表\n2025年12月31日\n（除特别注明外，金额单位为人民币千元）\n"
             "2025年 2024年 2025年 2024年\n合并 合并 公司 公司\n"
             "负债合计 4,000 3,000 1,000 900\n股东权益合计 20,000 19,000 6,000 5,000")]
    result=extract(values)
    assert result["total_liabilities"] == 4 and result["total_equity"] == 20
    assert result["debt_to_equity"] == .2


def test_note_section_clears_statement_context_before_similar_labels():
    values=[(50,"合并资产负债表\n单位：元\n2025年"),
            (51,"2025年度财务报表附注\n单位：元\n负债合计 4,000,000\n所有者权益合计 20,000,000")]
    result=extract(values)
    assert result["total_liabilities"] is None and result["total_equity"] is None


def test_current_year_reference_disagreement_is_a_gap_not_a_promotion():
    refs=reference();refs["roe_pct"]=19;refs["reported_capex"]=4
    result=extract(refs=refs)
    assert result["roe_pct"] is None and result["reported_capex"] is None
    assert result["net_profit"] == 15


def test_percent_decimal_precision_cannot_hide_a_half_percentage_point_mismatch():
    result=extract([(2,"2025年度\n加权平均净资产收益率 14.70% 13.00%")],{**reference(),"roe_pct":15.20})
    assert result["roe_pct"] is None


def test_currency_decimal_precision_cannot_use_whole_yuan_tolerance():
    result=extract([(2,"2025年度\n归属于上市公司股东的净利润（元） 15,000,000.25 12,000,000.25")],
                   {**reference(),"net_profit":15.00000075})
    assert result["net_profit"] is None
    assert verify._matches_reference(15,15.0004,.001,"15,000")


def test_existing_proof_precision_migration_removes_old_loose_roe_match():
    r=extract([(2,"2025年度\n加权平均净资产收益率 14.70% 13.00%")],{**reference(),"roe_pct":14.7})
    r.update(period_end="2025-12-31")
    value={"verified_reports":[r]}
    verify._recheck_numeric_proofs(value,{"2025-12-31":{**reference(),"roe_pct":15.2}})
    assert r["roe_pct"] is None and "roe_pct" not in r["field_evidence"]


def test_existing_proof_precision_migration_preserves_exact_known_facts():
    r=extract();r["period_end"]="2025-12-31"
    before=copy.deepcopy(r)
    verify._recheck_numeric_proofs({"verified_reports":[r]},{"2025-12-31":reference()})
    assert r==before


def test_ratio_is_removed_if_one_original_component_fails_recheck():
    r=extract();r["period_end"]="2025-12-31"
    verify._recheck_numeric_proofs({"verified_reports":[r]},{"2025-12-31":{**reference(),"total_equity":21}})
    assert r["total_equity"] is None and r["debt_to_equity"] is None


def test_multiple_different_actual_share_totals_are_ambiguous():
    data=pages()+[(71,pages()[-1][1].replace("2,000,000","3,000,000"))]
    result=extract(data)
    assert result["shares_outstanding"] is None
    assert any("shares_outstanding" in issue and "ambiguous" in issue for issue in result["issues"])


def test_wrapped_share_total_reads_ending_quantity_not_opening_quantity():
    result=extract([(70,"单位：股\n本次变动前 本次变动增减 本次变动后\n"
                     "三、股份总数 10,000,000 100.00\n-2,000,000 8,000,000 100.00")])
    assert result["shares_outstanding"]==8


def test_truncated_share_row_cannot_promote_visible_opening_quantity():
    result=extract([(70,"单位：股\n本次变动前 本次变动增减 本次变动后\n三、股份总数 10,000,000 100.00")])
    assert result["shares_outstanding"] is None


def test_explicit_share_table_year_conflict_is_rejected():
    result=extract([(70,"单位：股\n2024年末 2023年末\n三、股份总数 1,500,000 100 2,000,000 100")])
    assert result["shares_outstanding"] is None


def test_share_capital_currency_or_eps_never_produces_share_count():
    result=extract([(70,"2025年末\n单位：元\n股本 2,000,000\n三、股份总数 1,500,000 2,000,000\n基本每股收益 7.5")])
    assert result["shares_outstanding"] is None


def test_shares_do_not_borrow_another_page_unit():
    result=extract([(69,"单位：万股"),(70,"2025年末 本次变动后\n三、股份总数 1,500,000 100 2,000,000 100")])
    assert result["shares_outstanding"] is None


def test_note_number_is_never_taken_as_a_financial_value():
    result=extract([(54,"合并现金流量表 单位：元 2025年度\n购建固定资产、无形资产和其他长期资产支付的现金 四(3) 3,000,000")],
                   {**reference(),"reported_capex":.000003})
    assert result["reported_capex"] is None


def test_continued_cashflow_page_without_unit_is_not_given_an_earlier_unit():
    result=extract([(30,"合并现金流量表\n单位：元\n2025年度"),
                    (31,"经营活动产生的现金流量净额 20,000,000 19,000,000")])
    assert result["operating_cashflow"] is None


@pytest.mark.parametrize("change", [{"code":"600519"},{"period_end":"2024-12-31"},{"status":"ambiguous"},
                                  {"publication_date":"2026-11-12"},{"publication_date":"bad"},
                                  {"file":"../outside.pdf"},{"source_url":"http://static.cninfo.com.cn/a.pdf"},
                                  {"sha256":"0"*64},{"size":1}])
def test_original_identity_availability_path_and_checksum_fail_closed(tmp_path,change):
    r=item();path=tmp_path/r["file"];path.parent.mkdir();path.write_bytes(b"%PDF-test")
    r.update(size=path.stat().st_size,sha256=hashlib.sha256(path.read_bytes()).hexdigest());r.update(change)
    with pytest.raises(ValueError):
        verify.validate_document(tmp_path,CODE,2025,r,finance.CUTOFF)


def test_valid_original_document_is_bound_to_one_exact_file(tmp_path):
    r=item();path=tmp_path/r["file"];path.parent.mkdir();path.write_bytes(b"%PDF-test")
    r.update(size=path.stat().st_size,sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    assert verify.validate_document(tmp_path,CODE,2025,r,finance.CUTOFF) == path


def test_artifact_and_index_are_checksums_without_pit_or_human_promotion(tmp_path,monkeypatch):
    monkeypatch.setenv("CONTEST_MARKET_DATA_DIR",str(tmp_path))
    value={"schema":verify.SCHEMA,"code":CODE,"decision_date":finance.CUTOFF,
           "verified_reports":[],"point_in_time_verified":False,"business_assessment":None}
    verify.save_artifact(value)
    root=finance.directory()/"verified"
    raw=gzip.decompress((root/f"{CODE}.json.gz").read_bytes())
    saved=json.loads(raw)
    index=json.loads((root/"index.json").read_bytes())
    assert index["files"][CODE]==hashlib.sha256(raw).hexdigest()
    assert saved["point_in_time_verified"] is False and saved["business_assessment"] is None


def test_bounded_original_text_rejects_excess_pages_or_page_size():
    for data in [[(verify.MAX_PAGES+1,"text")],[(1,"x"*150001)]]:
        with pytest.raises(ValueError):extract(data)

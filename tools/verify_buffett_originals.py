"""Offline, field-level cross-checks of downloaded original annual reports.

No PDF is changed, no network/model/account service is called.  A match proves
only the named value occurs in the original disclosure: it does not certify a
complete correction history, maintenance capex, or the user's business judgment.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import gc
import gzip
import hashlib
import json
import math
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from server import historical_fundamentals as finance

SCHEMA = "buffett-verified/v1"
MAX_PDF = 20_000_000
MAX_PAGES = 800
MAX_MANIFEST = 2_000_000
MAX_PROFILE = 250_000
FIELDS = ("operating_cashflow", "net_profit", "roe_pct", "total_liabilities",
          "total_equity", "debt_to_equity", "shares_outstanding", "reported_capex", "maintenance_capex")
NUM = re.compile(r"(?<![\d.,])[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?")
MONEY_SCALES = {"元": 1e-6, "千元": .001, "万元": .01, "百万元": 1.0, "亿元": 100.0}
SHARE_SCALES = {"股": 1e-6, "万股": .01, "百万股": 1.0}
LABELS = {
    "operating_cashflow": r"经营活动产生的现金流量净额",
    "net_profit": r"归属于(?:上市公司|母公司)(?:股东|所有者)的净利润",
    "roe_pct": r"加权平均净资产收益率",
    "total_liabilities": r"(?<!流动)负债合计",
    "total_equity": r"(?<!母公司)(?:所有者权益|股东权益)合计",
    "reported_capex": r"购建固定资产[、，,]?无形资产和其他长期资产支付的现金",
}


def _raw_json(path, limit):
    if path.stat().st_size > limit:
        raise ValueError("Metadata exceeds bounded size")
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise ValueError("Invalid JSON metadata")
    return value


def _number(text):
    try:
        value = Decimal(text.replace(",", ""))
    except (InvalidOperation, AttributeError):
        return None
    return float(value) if value.is_finite() and abs(value) < Decimal("1e16") else None


def _compact(text):
    # Whitespace separates financial columns.  Removing it between digits
    # would silently turn "0.15 1" into "0.151" and corrupt a matched value.
    text = text.replace("−", "-").replace("－", "-").replace("，", ",")
    text = re.sub(r"(?<=\d)\s+(?=[-+\d])", "|", text)
    return re.sub(r"\s+", "", text)


def _money_unit(text):
    # Never assume that an unlabelled table uses yuan.  Match the longest unit.
    found = set()
    for unit in ("百万元", "千元", "万元", "亿元", "元"):
        if re.search(r"单位(?:为|[:：])?(?:人民币)?" + unit, text) or re.search(r"[（(]" + unit + r"[）)]", text):
            found.add(unit)
    return next(iter(found)) if len(found) == 1 else None


def _matches_reference(value, reference, scale, printed=None):
    if type(reference) not in (float, int) or isinstance(reference, bool):
        return False
    if not math.isfinite(reference) or not math.isfinite(value):
        return False
    # Rounding is defined by the printed decimal precision, not merely the
    # currency/percent unit. 14.70% cannot match a reference of 15.20%.
    exponent = Decimal(printed.replace(",", "")).as_tuple().exponent if printed is not None else 0
    printed_step = 10.0 ** min(0, exponent)
    tolerance = abs(scale) * printed_step * .51
    machine_error = max(math.ulp(float(value)),math.ulp(float(reference))) * 2
    return abs(value - reference) <= max(tolerance,machine_error)


def _proof(item, page, quote, unit, scale, raw_value, value):
    return {"page": page, "quote": quote[:500], "unit": unit, "scale": scale,
            "reported_value": raw_value, "value": value,
            "document_sha256": item["sha256"], "source_url": item["source_url"],
            "verification_status": "matched_original"}


def extract_fields(pages, reference, item):
    """Parse generated text pages; only exact current-year primary columns match.

    The first printed amount follows a label in the first/current-year column.
    Other columns are never searched for a convenient matching older number.
    Detailed balance/cashflow values require consolidated-statement context.
    """
    candidates = {field: [] for field in FIELDS}
    context = None
    previous_tail = ""
    seen_cashflow_statement = False
    for page, text in pages:
        if page > MAX_PAGES or not isinstance(text, str) or len(text) > 150_000:
            raise ValueError("Unbounded original report text")
        compact = _compact(text)
        # Only the annual KPI summary may provide profit/ROE/CFO outside the
        # consolidated statements.  Quarter tables later in the report cannot.
        summary = page <= 25 and str(item["year"]) in compact
        headings = list(re.finditer(r"合并(?:及公司|及母公司)?(?:资产负债表|现金流量表|利润表)|母公司(?:资产负债表|现金流量表|利润表)", compact))
        if seen_cashflow_statement and "财务报表附注" in compact[:150] and not headings:
            # Only primary statements are eligible.  Stop before a long notes
            # section can offer a different year/company's convenient number.
            break
        if "财务报表附注" in compact[:150] and not headings:
            context = None
        segments = []
        start = 0
        for heading in headings:
            segments.append((compact[start:heading.start()], context))
            context = next((kind for suffix,kind in (("资产负债表","balance"),("现金流量表","cashflow"),("利润表","income")) if heading[0].startswith("合并") and heading[0].endswith(suffix)),None)
            if page > 25 and context == "cashflow":
                seen_cashflow_statement = True
            start = heading.end()
        segments.append((compact[start:], context))
        for segment, statement in segments:
            for field, label in LABELS.items():
                allowed = (field in {"net_profit", "roe_pct", "operating_cashflow"} and summary)
                allowed |= field in {"total_liabilities", "total_equity"} and statement == "balance"
                allowed |= field in {"operating_cashflow", "reported_capex"} and statement == "cashflow"
                allowed |= field == "net_profit" and statement == "income"
                if not allowed:
                    continue
                for match in re.finditer(label, segment):
                    tail = segment[match.end():match.end()+90]
                    # The field label can carry its own unit, otherwise the
                    # statement must explicitly supply a unit on this page.
                    raw_match = NUM.search(tail)
                    if raw_match is None or raw_match.start() > 28:
                        continue
                    prefix = re.sub(r"[（(](?:人民币)?(?:百万元|千元|万元|亿元|元|%)[）)]", "", tail[:raw_match.start()])
                    if prefix.strip(":：-—|"):
                        # A note marker such as 四(27), rather than an amount,
                        # must never be mistaken for the first financial value.
                        continue
                    raw_value = _number(raw_match[0])
                    if raw_value is None:
                        continue
                    if field == "roe_pct":
                        # ROE is explicitly a percent, not an unlabelled ratio.
                        if "%" not in tail[:raw_match.end()+2] and "%" not in tail[:raw_match.start()]:
                            continue
                        unit, scale = "%", 1.0
                    else:
                        unit = _money_unit(tail[:raw_match.start()]) or _money_unit(segment[:match.start()])
                        if unit is None:
                            # A page-wide unit may occur after the heading but
                            # before the rows; never borrow a different page.
                            unit = _money_unit(compact)
                        if unit is None:
                            continue
                        scale = MONEY_SCALES[unit]
                    value = raw_value * scale
                    if not _matches_reference(value, reference.get(field), scale,raw_match[0]):
                        continue
                    quote = segment[max(0,match.start()-45):min(len(segment),match.end()+90)]
                    candidates[field].append(_proof(item,page,quote,unit,scale,raw_value,value))

        # The actual ordinary-share total is the final quantity in the share
        # movement row.  Capital amounts and EPS-derived shares are not read.
        share_context = compact
        unit_match = re.search(r"单位[:：](百万股|万股|股)(?![份东])", share_context)
        if unit_match and re.search(r"本次变动后|年末|期末", share_context):
            lines = text.splitlines()
            for index, line in enumerate(lines):
                if not re.search(r"三[、．.](?:普通股)?股份总数", _compact(line)):
                    continue
                row = line.strip()
                for continuation in lines[index+1:index+5]:
                    if re.fullmatch(r"[\d\s.,+%()（）—–-]+",continuation.strip()):
                        row += " " + continuation.strip()
                    else:
                        break
                if len(row) > 500:
                    continue
                years = re.findall(r"(20\d{2})年(?:末|12月31日)",compact)
                if years and int(years[0]) != item["year"]:
                    continue
                quantities = [(_number(value[0]),value[0]) for value in NUM.finditer(row)]
                # Percent columns are <=100; changes may be large but precede
                # the final year-end total in the mandated movement layout.
                quantities = [(value,raw) for value,raw in quantities if value is not None and value >= 1_000_000 and float(value).is_integer()]
                row_numbers=list(NUM.finditer(row))
                # A complete total row includes opening AND ending quantities
                # and ends with the total's 100% share. A single visible
                # opening quantity is not evidence of the year-end total.
                if len(quantities)<2 or not row_numbers or _number(row_numbers[-1][0]) != 100:
                    continue
                raw_value = quantities[-1][0]
                unit = unit_match[1]; scale = SHARE_SCALES[unit]
                value = raw_value * scale
                candidates["shares_outstanding"].append(_proof(item,page,row,unit,scale,raw_value,value))
        previous_tail = text[-1200:]

    output = {field: None for field in FIELDS}
    evidence = {}
    issues = []
    for field, values in candidates.items():
        if field in {"maintenance_capex", "debt_to_equity"}:
            continue
        distinct = {round(value["value"], 8) for value in values}
        if len(distinct) == 1:
            output[field] = values[0]["value"]
            evidence[field] = values[0]
        elif values:
            issues.append(f"{field}: original current-year candidates are ambiguous; not selected")
        else:
            issues.append(f"{field}: no unambiguous explicitly-unit-labelled original match")
    if output["total_liabilities"] is not None and output["total_equity"] is not None and output["total_equity"] > 0:
        ratio = output["total_liabilities"] / output["total_equity"]
        if _matches_reference(ratio, reference.get("debt_to_equity"), 1e-6):
            output["debt_to_equity"] = ratio
            left, right = evidence["total_liabilities"], evidence["total_equity"]
            evidence["debt_to_equity"] = _proof(item,left["page"],f"负债/权益；负债PDF页{left['page']}：{left['quote']}；权益PDF页{right['page']}：{right['quote']}","倍",1.0,ratio,ratio)
            evidence["debt_to_equity"]["component_pages"] = {"total_liabilities": left["page"], "total_equity": right["page"]}
    if output["debt_to_equity"] is None:
        issues.append("debt_to_equity: both consolidated components and reference ratio must match")
    issues.extend(["maintenance_capex: total capital expenditure is not maintenance spending",
                   "Original values cross-checked only; correction history and PIT admission remain unverified"])
    return {**output, "field_evidence": evidence, "issues": issues}


def validate_document(root, code, year, item, cutoff):
    if not isinstance(item,dict) or item.get("status") != "downloaded" or item.get("code") != code or item.get("year") != year or item.get("period_end") != f"{year}-12-31":
        raise ValueError("Original document identity/status mismatch")
    notice = item.get("publication_date")
    if not isinstance(notice,str) or not f"{year}-12-31" < notice < cutoff or date.fromisoformat(notice).isoformat() != notice:
        raise ValueError("Original document availability mismatch")
    identity = item.get("announcement_id")
    if not isinstance(identity,str) or re.fullmatch(r"\d{6,20}",identity) is None or item.get("file") != f"{code}/{identity}.pdf":
        raise ValueError("Unsafe original document path")
    if re.fullmatch(r"https://static\.cninfo\.com\.cn/finalpage/\d{4}-\d{2}-\d{2}/"+identity+r"\.[Pp][Dd][Ff]",item.get("source_url", "")) is None:
        raise ValueError("Unapproved original document URL")
    path = (root / item["file"]).resolve()
    if not path.is_relative_to(root.resolve()) or path.stat().st_size > MAX_PDF or path.stat().st_size != item.get("size"):
        raise ValueError("Original PDF size/path mismatch")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        if stream.read(4) != b"%PDF":
            raise ValueError("Not an original PDF")
        stream.seek(0)
        while block := stream.read(262144):
            digest.update(block)
    if digest.hexdigest() != item.get("sha256"):
        raise ValueError("Original PDF checksum mismatch")
    return path


def verify_code(code, *, reader_factory=None):
    if not isinstance(code,str) or re.fullmatch(finance.CODE_PATTERN,code) is None:
        raise ValueError("Invalid mainboard code")
    root = finance.directory()
    manifest = _raw_json(root/"originals"/"manifest.json",MAX_MANIFEST)
    if manifest.get("schema") != "buffett-originals/v1" or manifest.get("decision_date") != finance.CUTOFF or not isinstance(manifest.get("reports"),dict):
        raise ValueError("Invalid original report manifest")
    profile = finance.load_profile(code)
    refs = {row["period_end"]:row for row in profile["reports"]}
    if reader_factory is None:
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise ValueError("Install optional pypdf dependency or use the bundled authoring Python") from exc
        reader_factory = PdfReader
    reports=[]
    for period in finance.EXPECTED_YEARS:
        year=int(period[:4]); item=manifest["reports"].get(f"{code}:{year}")
        if item is None:
            continue
        path=validate_document(root/"originals",code,year,item,finance.CUTOFF)
        reader=reader_factory(path)
        if reader.is_encrypted or not 1 <= len(reader.pages) <= MAX_PAGES:
            raise ValueError("Encrypted or oversized original report")
        try:
            texts=((index+1,page.extract_text() or "") for index,page in enumerate(reader.pages))
            fields=extract_fields(texts,refs.get(period,{}),item)
        finally:
            close = getattr(reader,"close",None)
            if callable(close):
                close()
            del reader
            gc.collect()
        reports.append({"period_end":period,"available_on":item["publication_date"],
                        "point_in_time_verified":False,"verification_kind":"original_values_cross_checked",
                        "document_sha256":item["sha256"],"source_url":item["source_url"],"announcement_id":item["announcement_id"],**fields})
    return {"schema":SCHEMA,"code":code,"decision_date":finance.CUTOFF,"verified_reports":reports,
            "point_in_time_verified":False,"business_assessment":None,
            "provenance":{"source":"CNINFO original PDFs cross-checked against archived public references",
                          "verified_at":datetime.now(timezone.utc).isoformat(timespec="seconds"),
                          "verification_kind":"original_values_cross_checked","restatement_status":"not_verified"}}


def save_artifact(value):
    root=finance.directory()/"verified"; root.mkdir(parents=True,exist_ok=True)
    code=value["code"]
    if value.get("schema") != SCHEMA or re.fullmatch(finance.CODE_PATTERN,code) is None:
        raise ValueError("Invalid verified artifact")
    raw=json.dumps(value,ensure_ascii=False,sort_keys=True,allow_nan=False).encode("utf-8")
    if len(raw)>MAX_PROFILE:
        raise ValueError("Verified profile exceeds size cap")
    index_path=root/"index.json"
    index=_raw_json(index_path,finance.MAX_INDEX_BYTES) if index_path.is_file() else {"schema":SCHEMA,"files":{}}
    if index.get("schema") != SCHEMA or not isinstance(index.get("files"),dict) or len(index["files"])>finance.MAX_PROFILES:
        raise ValueError("Invalid verified artifact index")
    index["files"][code]=hashlib.sha256(raw).hexdigest()
    index_raw=json.dumps(index,sort_keys=True,allow_nan=False).encode("utf-8")
    if len(index["files"])>finance.MAX_PROFILES or len(index_raw)>finance.MAX_INDEX_BYTES:
        raise ValueError("Verified index exceeds cap")
    path=root/f"{code}.json.gz"; temporary=path.with_name(path.name+".tmp")
    temporary.write_bytes(gzip.compress(raw,mtime=0));temporary.replace(path)
    temporary=index_path.with_name(index_path.name+".tmp");temporary.write_bytes(index_raw);temporary.replace(index_path)


def recheck_share_proofs(code):
    """Re-evaluate only existing share evidence after a parser safety update.

    This bounded migration reads its original source page for every existing
    share proof. It cannot add other fields or turn a missing proof into data.
    All non-share facts remain byte-for-byte equivalent JSON values.
    """
    if not isinstance(code,str) or re.fullmatch(finance.CODE_PATTERN,code) is None:
        raise ValueError("Invalid mainboard code")
    root=finance.directory(); index=_raw_json(root/"verified"/"index.json",finance.MAX_INDEX_BYTES)
    if index.get("schema")!=SCHEMA or not isinstance(index.get("files"),dict):
        raise ValueError("Invalid verified index")
    with gzip.open(root/"verified"/f"{code}.json.gz","rb") as stream:
        raw=stream.read(MAX_PROFILE+1)
    if len(raw)>MAX_PROFILE or hashlib.sha256(raw).hexdigest()!=index["files"].get(code):
        raise ValueError("Verified profile checksum mismatch")
    value=json.loads(raw)
    if value.get("schema")!=SCHEMA or value.get("code")!=code or value.get("decision_date")!=finance.CUTOFF or value.get("point_in_time_verified") is not False:
        raise ValueError("Not a mutable field-only generated profile")
    manifest=_raw_json(root/"originals"/"manifest.json",MAX_MANIFEST)
    if manifest.get("schema")!="buffett-originals/v1" or manifest.get("decision_date")!=finance.CUTOFF:
        raise ValueError("Invalid original manifest")
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise ValueError("Optional pypdf dependency is required") from exc
    for row in value["verified_reports"]:
        proof=row.get("field_evidence",{}).get("shares_outstanding")
        if proof is None:
            if row.get("shares_outstanding") is not None:
                raise ValueError("Existing share value has no proof")
            continue
        period=row["period_end"];year=int(period[:4]);item=manifest["reports"][f"{code}:{year}"]
        path=validate_document(root/"originals",code,year,item,finance.CUTOFF)
        page=proof.get("page")
        if type(page)is not int or not 1<=page<=MAX_PAGES:
            raise ValueError("Invalid original share page")
        reader=PdfReader(path)
        try:
            if reader.is_encrypted or not page<=len(reader.pages)<=MAX_PAGES:
                raise ValueError("Invalid original share PDF")
            facts=extract_fields([(page,reader.pages[page-1].extract_text() or "")],{},item)
        finally:
            reader.close();del reader;gc.collect()
        row["shares_outstanding"]=facts["shares_outstanding"]
        if "shares_outstanding" in facts["field_evidence"]:
            row["field_evidence"]["shares_outstanding"]=facts["field_evidence"]["shares_outstanding"]
        else:
            row["field_evidence"].pop("shares_outstanding",None)
            row.setdefault("issues",[]).append("shares_outstanding: original share row/year failed complete-row recheck; value cleared")
    refs={row["period_end"]:row for row in finance.load_profile(code)["reports"]}
    _recheck_numeric_proofs(value,refs)
    value.setdefault("provenance",{})["share_row_rechecked_at"]=datetime.now(timezone.utc).isoformat(timespec="seconds")
    value["provenance"]["printed_precision_rechecked_at"]=datetime.now(timezone.utc).isoformat(timespec="seconds")
    return value


def _recheck_numeric_proofs(value, references):
    """Only retain facts whose exact original printed precision still matches.

    The original proof already names its primary page and current-year column.
    Rechecking that captured first-column token avoids reopening thousands of
    PDF pages after tightening the numeric tolerance. No missing fact is added.
    """
    for row in value["verified_reports"]:
        ref=references.get(row["period_end"],{})
        evidence=row.get("field_evidence",{})
        for field,label in LABELS.items():
            proof=evidence.get(field)
            if proof is None:
                continue
            quote=proof.get("quote","")
            tokens=[]
            for match in re.finditer(label,quote):
                tail=quote[match.end():match.end()+90]
                number=NUM.search(tail)
                if number is None:
                    continue
                parsed=_number(number[0]);reported=proof.get("reported_value")
                if parsed is not None and type(reported)in(int,float) and abs(parsed-reported)<=max(math.ulp(float(parsed)),math.ulp(float(reported)))*2:
                    tokens.append(number[0])
            valid=(len(tokens)==1 and type(proof.get("scale"))in(int,float)
                   and _matches_reference(row.get(field),ref.get(field),proof["scale"],tokens[0]))
            if not valid:
                row[field]=None;evidence.pop(field,None)
                row.setdefault("issues",[]).append(f"{field}: source token/printed precision no longer matches public reference; value cleared")
        if "debt_to_equity" in evidence:
            left,right=row.get("total_liabilities"),row.get("total_equity")
            valid=(left is not None and right is not None and right>0
                   and _matches_reference(left/right,ref.get("debt_to_equity"),1e-6)
                   and abs(left/right-row["debt_to_equity"])<=1e-12)
            if not valid:
                row["debt_to_equity"]=None;evidence.pop("debt_to_equity",None)
                row.setdefault("issues",[]).append("debt_to_equity: a component or ratio failed printed-precision recheck; value cleared")


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codes",nargs="+",default=["000333","000858","600406","600519","600887","600900"])
    parser.add_argument("--recheck-shares","--recheck-values",dest="recheck_shares",action="store_true",help="Recheck saved share source pages and exact printed precision of every saved financial fact")
    args=parser.parse_args(argv)
    codes=list(dict.fromkeys(args.codes))
    if not 1<=len(codes)<=10:
        parser.error("One to ten explicit stocks only")
    failures=0
    for code in codes:
        try:
            value=recheck_share_proofs(code) if args.recheck_shares else verify_code(code)
            save_artifact(value)
            print(json.dumps({"code":code,"annual_reports":len(value["verified_reports"]),"verified_fields":{r["period_end"]:list(r["field_evidence"]) for r in value["verified_reports"]},"strict_pit_verified":False},ensure_ascii=False),flush=True)
        except (OSError,ValueError,KeyError,TypeError) as exc:
            failures+=1;print(f"{code}: original verification unavailable ({type(exc).__name__}); not admitted for trading",flush=True)
    return 1 if failures else 0


if __name__=="__main__":
    raise SystemExit(main())

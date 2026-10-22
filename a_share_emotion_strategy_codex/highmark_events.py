"""Bounded, read-only collection of republished exchange abnormal-move events.

Pagination completeness here attests the provider's response, not official
exchange completeness. This module never promotes the absence of a provider
record into an official finding that a stock has no abnormal-move risk.
"""

from __future__ import annotations

import base64
from datetime import date, datetime
import hashlib
import json
import math
from pathlib import Path
import re
import time
import unicodedata
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import requests

from research_store import atomic_json, private_root


URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
REPORT_NAME = "RPT_DAILYBILLBOARD_DETAILSNEW"
COLUMNS = "SECURITY_CODE,SECUCODE,SECURITY_NAME_ABBR,TRADE_DATE,EXPLANATION"
PAGE_SIZE = 500
MAX_PAGES = 30
REQUEST_TIMEOUT = 6
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
TZ = ZoneInfo("Asia/Shanghai")
HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://data.eastmoney.com/"}


class EventFeedError(RuntimeError):
    pass


def _date(value):
    value = str(value or "")[:10]
    date.fromisoformat(value)
    return value


def _integer(value, label):
    if isinstance(value, bool):
        raise EventFeedError("invalid_" + label)
    try:
        result = int(value)
    except (ValueError, TypeError, OverflowError):
        raise EventFeedError("invalid_" + label)
    if str(result) != str(value) or result < 0:
        raise EventFeedError("invalid_" + label)
    return result


def _request(params, raw_dir, label, sources):
    """One public request, six-second timeout, no retries or auth changes."""
    url = URL + "?" + urlencode(params)
    fetched = datetime.now(TZ).isoformat(timespec="seconds")
    evidence = {"url": url, "fetched_at": fetched, "source_kind": "provider_republication"}
    response = None
    chunks = []
    size = 0
    complete_body = False
    started = time.monotonic()
    try:
        response = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT,
                                allow_redirects=False, stream=True)
        evidence["http_status"] = response.status_code
        for chunk in response.iter_content(chunk_size=65536):
            if time.monotonic() - started > REQUEST_TIMEOUT:
                raise EventFeedError("request_time_budget_exceeded")
            if not chunk:
                continue
            room = MAX_RESPONSE_BYTES - size
            chunks.append(chunk[:room])
            size += min(len(chunk), room)
            if len(chunk) > room:
                raise EventFeedError("response_too_large")
        complete_body = True
        raw = b"".join(chunks)
        evidence.update({"http_status": response.status_code,
                         "raw_sha256": hashlib.sha256(raw).hexdigest(),
                         "raw_body_base64": base64.b64encode(raw).decode("ascii"),
                         "raw_body_complete": True})
        if response.status_code != 200:
            raise EventFeedError("http_" + str(response.status_code))
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise EventFeedError("response_not_object")
        evidence["payload"] = payload
        atomic_json(raw_dir / (label + ".json"), evidence)
        sources.append({"label": "东财交易公开信息转载 " + label,
                        "url": url, "retrieved_at": fetched,
                        "raw_sha256": evidence["raw_sha256"],
                        "source_kind": "provider_republication",
                        "official_complete": False})
        return payload, url
    except Exception as exc:
        category = str(exc) if isinstance(exc, EventFeedError) else type(exc).__name__
        raw = b"".join(chunks)
        evidence.update({"raw_sha256": hashlib.sha256(raw).hexdigest(),
                         "raw_body_base64": base64.b64encode(raw).decode("ascii"),
                         "raw_body_complete": complete_body})
        evidence.update({"error_category": category, "error_type": type(exc).__name__,
                         "error": str(exc)})
        atomic_json(raw_dir / (label + ".failure.json"), evidence)
        raise EventFeedError(category)
    finally:
        if response is not None:
            response.close()


def _page(payload):
    if payload.get("success") is not True:
        raise EventFeedError("provider_unsuccessful")
    result = payload.get("result")
    if not isinstance(result, dict):
        raise EventFeedError("missing_result_metadata")
    count = _integer(result.get("count"), "count")
    pages = _integer(result.get("pages"), "pages")
    if count == 0:
        if pages not in (0, 1):
            raise EventFeedError("empty_count_pages_conflict")
    elif pages != math.ceil(count / PAGE_SIZE):
        raise EventFeedError("count_pages_conflict")
    if pages > MAX_PAGES:
        raise EventFeedError("page_budget_exceeded")
    rows = result.get("data")
    if count == 0 and rows is None:
        rows = []
    if not isinstance(rows, list):
        raise EventFeedError("missing_page_rows")
    return count, pages, rows


def _reason_kind(reason, code):
    """Only the requested main-board price rules yield reset events."""
    text = re.sub(r"\s+", "", unicodedata.normalize("NFKC", reason))
    three = bool(re.search(r"连续(?:三|3)个交易日内", text))
    ten = bool(re.search(r"连续(?:十|10)个交易日内", text))
    thirty = bool(re.search(r"连续(?:三十|30)个交易日内", text))
    price = "偏离" in text and any(word in text for word in ("涨幅", "跌幅", "涨跌幅"))
    upward = "涨幅" in text
    downward = "跌幅" in text and "涨跌幅" not in text
    if "涨跌幅" in text:
        upward = bool(re.search(r"(?:达到|超过|达)\+?(?:100|200)(?:\.0+)?%", text))
        downward = bool(re.search(r"(?:达到|超过|达)[-−](?:50|70)(?:\.0+)?%", text))
    is_mainboard = code.startswith(("000", "001", "002", "003", "600", "601", "603", "605"))
    if is_mainboard and three and price and "累计" in text and re.search(r"20(?:\.0+)?%", text):
        return "normal"
    if is_mainboard and price and (
        (ten and ((upward and re.search(r"100(?:\.0+)?%", text))
                  or (downward and re.search(r"50(?:\.0+)?%", text))))
        or (thirty and ((upward and re.search(r"200(?:\.0+)?%", text))
                        or (downward and re.search(r"70(?:\.0+)?%", text))))
    ):
        return "severe"
    if is_mainboard and ten and re.search(r"(?:四|4)次", text) and "同" in text and "异常波动" in text:
        return "severe"
    # The Shenzhen republished category omits the words "10 trading days" in
    # its 100% category title. Its explicit "severe abnormal period" wording
    # is a positive event, not an inference from an ordinary daily board.
    # This alias was cross-checked against an original exchange-hosted notice.
    if is_mainboard and text.startswith("严重异常期间") and price and "累计" in text and (
        (upward and re.search(r"(?:100|200)(?:\.0+)?%", text))
        or (downward and re.search(r"(?:50|70)(?:\.0+)?%", text))
    ):
        return "severe"
    if is_mainboard and text.startswith("严重异常期间") and re.search(r"(?:四|4)次", text) and "异常波动" in text:
        return "severe"
    # Main-board abnormal turnover is also an ORDINARY abnormal-move event,
    # so it can reset the three-session indicator. It does not count toward the
    # separate 10-session/four-price-abnormalities condition. A truncated reason
    # missing one of the two thresholds remains unresolved, never ignored.
    if is_mainboard and three and "日均换手率" in text:
        if re.search(r"前(?:五|5)个?交易日", text) and "30倍" in text and "累计" in text and re.search(r"20(?:\.0+)?%", text):
            return "normal_turnover"
        return "unknown_abnormal_reason"
    # Other board rules do not attest a normal-mainboard reset. They are still
    # retained in the exact raw response and counted in coverage.
    if not is_mainboard:
        return "ignored_other_board"
    if three and price and re.search(r"(?:12|15|30)(?:\.0+)?%", text):
        return "ignored_other_rule"
    if any(word in text for word in ("无价格涨跌幅限制", "无涨跌幅限制", "退市整理")):
        return "ignored_other_rule"
    if any(word in text for word in ("异常波动", "严重异常", "连续", "累计偏离")):
        return "unknown_abnormal_reason"
    if any(word in text for word in ("日涨幅", "日跌幅", "日收盘价格", "日价格振幅", "日振幅值", "日换手率", "当日涨幅", "当日跌幅")):
        return "ignored_daily_rule"
    if any(word in text for word in ("换手率", "价格振幅", "涨幅偏离值达到7%", "跌幅偏离值达到7%")):
        return "ignored_daily_rule"
    return "unknown_reason"


def _row(raw, start, end):
    if not isinstance(raw, dict):
        raise EventFeedError("invalid_row_object")
    code = str(raw.get("SECURITY_CODE", ""))
    if not re.fullmatch(r"\d{6}", code):
        raise EventFeedError("invalid_security_code")
    name = str(raw.get("SECURITY_NAME_ABBR") or "").strip()
    if not name:
        raise EventFeedError("missing_security_name")
    symbol = str(raw.get("SECUCODE") or "")
    if symbol and not re.fullmatch(re.escape(code) + r"\.(?:SH|SZ|BJ)", symbol):
        raise EventFeedError("security_identity_conflict")
    try:
        day = _date(raw.get("TRADE_DATE"))
    except (ValueError, TypeError):
        raise EventFeedError("invalid_trade_date")
    if not start <= day <= end:
        raise EventFeedError("row_outside_requested_dates")
    reason = str(raw.get("EXPLANATION") or "").strip()
    if not reason:
        raise EventFeedError("missing_disclosure_reason")
    return code, day, reason


def _collect_events(start_date, end_date, raw_dir, requester):
    """Collect all provider pages, retaining positive events and audit gaps.

    ``provider_complete`` requires complete stable pagination, unique raw keys,
    valid identity/date evidence, and recognised disclosure reasons. It never
    means ``official_complete``. A partial positive event can remain visible,
    but callers must not use a partial collection to assert reset absence.
    """
    start, end = _date(start_date), _date(end_date)
    if start > end:
        raise ValueError("start_date must not exceed end_date")
    if end > datetime.now(TZ).date().isoformat():
        raise ValueError("end_date must not be in the future")
    root = private_root().resolve()
    raw_dir = Path(raw_dir).expanduser().resolve()
    if raw_dir != root and root not in raw_dir.parents:
        raise ValueError("raw_dir must be inside the private research store")
    raw_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    report = {
        "status": "unavailable", "coverage_start": start, "coverage_end": end,
        "normal": {}, "severe": {}, "sources": [], "missing": [],
        "provider": "eastmoney_republished_exchange_records",
        "provider_complete": False, "official_complete": False,
        "reference_only": True, "pagination_complete": False,
        "raw_row_count": 0, "unique_raw_count": 0, "event_count": 0,
        "ignored_row_count": 0, "unknown_reasons": [],
        "unresolved_codes": [],
        "columns": COLUMNS,
        "publication_date_meaning": "转载交易公开信息公布日；不是公司PDF落款或供应商首次发布时间",
    }
    params = {
        "reportName": REPORT_NAME, "columns": COLUMNS,
        "filter": "(TRADE_DATE>='" + start + "')(TRADE_DATE<='" + end + "')",
        "pageSize": str(PAGE_SIZE), "pageNumber": "1",
        "sortColumns": "TRADE_DATE,SECURITY_CODE,EXPLANATION", "sortTypes": "1,1,1",
        "source": "WEB", "client": "WEB",
    }
    raw_rows = []
    seen = set()
    merged = {}
    page_counts = []
    try:
        payload, source_url = requester(params, raw_dir, "page-001", report["sources"])
        expected, pages, first_rows = _page(payload)
        report.update({"expected_raw_count": expected, "expected_pages": pages})
        page_sequence = range(1, max(pages, 1) + 1)
        for page in page_sequence:
            if page == 1:
                current_payload, current_url = payload, source_url
            else:
                params["pageNumber"] = str(page)
                current_payload, current_url = requester(params, raw_dir, "page-%03d" % page, report["sources"])
            count, page_total, rows = _page(current_payload)
            if count != expected or page_total != pages:
                raise EventFeedError("pagination_metadata_changed")
            expected_page_rows = min(PAGE_SIZE, max(expected - (page - 1) * PAGE_SIZE, 0))
            if len(rows) != expected_page_rows:
                raise EventFeedError("page_row_count_mismatch")
            page_counts.append(len(rows))
            for raw in rows:
                raw_rows.append(raw)
                code, day, reason = _row(raw, start, end)
                key = (code, day, reason)
                if key in seen:
                    raise EventFeedError("duplicate_raw_primary_key")
                seen.add(key)
                kind = _reason_kind(reason, code)
                reason_type = "turnover" if kind == "normal_turnover" else "price"
                if kind == "normal_turnover":
                    kind = "normal"
                if kind in ("normal", "severe"):
                    event_key = (code, day, kind)
                    event = merged.setdefault(event_key, {
                        "published_date": day, "reason": reason, "reasons": [],
                        "source_url": current_url, "source_urls": [],
                        "source_kind": "provider_republication", "official_verified": False,
                        "reason_types": [], "counts_toward_10d_price_events": False,
                    })
                    event["reasons"].append(reason)
                    event["reason"] = "；".join(event["reasons"])
                    if current_url not in event["source_urls"]:
                        event["source_urls"].append(current_url)
                    if reason_type not in event["reason_types"]:
                        event["reason_types"].append(reason_type)
                    if kind == "normal" and reason_type == "price":
                        event["counts_toward_10d_price_events"] = True
                elif kind.startswith("unknown"):
                    report["unknown_reasons"].append({"code": code, "published_date": day,
                                                       "reason": reason, "kind": kind})
                else:
                    report["ignored_row_count"] += 1
        if len(raw_rows) != expected or len(seen) != expected:
            raise EventFeedError("total_unique_rows_mismatch")
        # A final read of the same first page confirms total/pages and its
        # stable identities, catching inserts/deletes/reordering during paging.
        params["pageNumber"] = "1"
        final_payload, _ = requester(params, raw_dir, "metadata-after", report["sources"])
        final_count, final_pages, final_rows = _page(final_payload)
        if final_count != expected or final_pages != pages:
            raise EventFeedError("final_metadata_changed")
        if final_rows != first_rows:
            raise EventFeedError("first_page_changed_during_collection")
        report["pagination_complete"] = True
        if report["unknown_reasons"]:
            report["missing"].append("unrecognised_disclosure_reason")
        else:
            report["provider_complete"] = True
    except EventFeedError as exc:
        report["missing"].append(str(exc))
    report["raw_row_count"] = len(raw_rows)
    report["unique_raw_count"] = len(seen)
    report["page_row_counts"] = page_counts
    for (code, day, kind), event in sorted(merged.items()):
        report[kind].setdefault(code, []).append(event)
    report["event_count"] = len(merged)
    report["unresolved_codes"] = sorted({item["code"] for item in report["unknown_reasons"]})
    report["status"] = "complete" if report["provider_complete"] else "partial" if raw_rows else "unavailable"
    report["missing"] = sorted(set(report["missing"]))
    atomic_json(raw_dir / "collection.json", report)
    return report


def collect_events(start_date, end_date, raw_dir):
    """Collect a bounded fresh provider snapshot; see module provenance rules."""
    return _collect_events(start_date, end_date, raw_dir, _request)


def reparse_archive(source_dir, output_dir):
    """Reparse saved exact response bytes without fetching or replacing them.

    Retrieval clocks and raw fingerprints are inherited from each original
    response. The new parsing clock is separate. A failed/incomplete original
    collection cannot be upgraded by borrowing files from another attempt.
    """
    root = private_root().resolve()
    source_dir = Path(source_dir).expanduser().resolve()
    output_dir = Path(output_dir).expanduser().resolve()
    if root != source_dir and root not in source_dir.parents:
        raise ValueError("source_dir must be inside the private research store")
    if source_dir == output_dir:
        raise ValueError("offline parsing cannot replace its source archive")
    original = json.loads((source_dir / "collection.json").read_text(encoding="utf-8"))
    parsed_at = datetime.now(TZ).isoformat(timespec="seconds")

    def replay(params, raw_dir, label, sources):
        path = source_dir / (label + ".json")
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
            body = base64.b64decode(saved["raw_body_base64"], validate=True)
            if hashlib.sha256(body).hexdigest() != saved.get("raw_sha256"):
                raise EventFeedError("archive_fingerprint_mismatch")
            payload = json.loads(body.decode("utf-8"))
            if payload != saved.get("payload"):
                raise EventFeedError("archive_payload_conflict")
            if saved.get("url") != URL + "?" + urlencode(params):
                raise EventFeedError("archive_request_conflict")
            # Older successful archives predate the explicit completeness flag;
            # matching complete JSON bytes and payload remain verifiable.
            if saved.get("raw_body_complete") is False or saved.get("http_status") != 200:
                raise EventFeedError("archive_response_not_complete")
            if not saved.get("fetched_at"):
                raise EventFeedError("archive_missing_retrieval_clock")
            copied = dict(saved)
            copied.update({"reparsed_at": parsed_at, "source_archive": str(path),
                           "execution_mode": "offline_reparse"})
            atomic_json(raw_dir / (label + ".json"), copied)
            sources.append({"label": "东财原始响应离线重析 " + label,
                            "url": saved["url"], "retrieved_at": saved["fetched_at"],
                            "reparsed_at": parsed_at, "raw_sha256": saved["raw_sha256"],
                            "source_kind": "provider_republication", "official_complete": False})
            return payload, saved["url"]
        except EventFeedError:
            raise
        except Exception as exc:
            raise EventFeedError("archive_" + type(exc).__name__)

    report = _collect_events(original["coverage_start"], original["coverage_end"], output_dir, replay)
    report.update({"execution_mode": "offline_reparse", "reparsed_at": parsed_at,
                   "source_archive": str(source_dir), "network_requests": 0,
                   "source_original_status": original.get("status")})
    atomic_json(output_dir / "collection.json", report)
    return report
